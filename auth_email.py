"""Password-based signup (email OTP verification) for VooHr.

An additional auth path alongside the existing Google OAuth flow in auth.py.
Follows the same security patterns: blind_index for lookups, KMS envelope
encryption for reversible PII, and auth.py's session/_record_active_session
for the signed-in session.

Flow:
  1. onboarding.html POSTs org details -> session["pending_org"]
  2. email-verify.html POSTs {email, password}  -> /auth/email/start
       - stores an OTP doc in `otp_verifications`, emails the 6-digit code
  3. otp-verify.html POSTs {otp}                -> /auth/email/verify-otp
       - on match, creates the org + user and starts a session
  4. otp-verify.html can POST /auth/email/resend-otp to get a fresh code
  5. returning users sign in via POST /auth/password/signin
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request, session

from login_flow import _login_result_for_user, _record_active_session
from blind_index import blind_index
from email_service import send_otp_email
from extensions import get_db, check_rate_limit, record_rate_limit_event
from field_encryption import decrypt_fields, encrypt_fields
from password_utils import hash_password, password_strength_ok, verify_password

auth_email_bp = Blueprint("auth_email", __name__)

_OTP_TTL = timedelta(minutes=10)
_MAX_ATTEMPTS = 5
_RESEND_COOLDOWN = timedelta(seconds=60)
_LOCKOUT_AFTER = 5
_LOCKOUT_TTL = timedelta(minutes=15)

# Rate-limit constants for OTP sending.
_OTP_MAX_PER_EMAIL = 5       # max OTP sends per email per window
_OTP_MAX_PER_IP = 20         # max OTP sends per IP per window
_OTP_RATE_WINDOW = 900       # 15-minute sliding window in seconds


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt) -> datetime | None:
    """MongoDB returns naive UTC datetimes; make them aware before comparing
    (same pattern as employees.py)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _generate_otp() -> str:
    return f"{secrets.randbelow(900000) + 100000:06d}"


def _otp_hash(otp: str) -> str:
    return hashlib.sha256(otp.encode()).hexdigest()


def _client_ip():
    """Return the real client IP, respecting X-Forwarded-For from a reverse proxy."""
    return request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()


def _check_otp_send_rate_limit(db, email, ip):
    """Check per-email and per-IP rate limits for OTP sending.

    Returns None on success, or a (response, status_code) tuple on 429.
    Records a rate-limit event on success.
    """
    email_key = f"otp_email:{email.lower().strip()}"
    ip_key = f"otp_ip:{ip}"

    allowed, retry_after = check_rate_limit(db, email_key, _OTP_MAX_PER_EMAIL, _OTP_RATE_WINDOW)
    if not allowed:
        msg = "Too many OTP requests. Please wait a few minutes before requesting another code."
        return jsonify({"error": msg, "retry_after": retry_after}), 429

    allowed, retry_after = check_rate_limit(db, ip_key, _OTP_MAX_PER_IP, _OTP_RATE_WINDOW)
    if not allowed:
        msg = "Too many OTP requests. Please wait a few minutes before requesting another code."
        return jsonify({"error": msg, "retry_after": retry_after}), 429

    record_rate_limit_event(db, email_key, ttl_seconds=_OTP_RATE_WINDOW)
    record_rate_limit_event(db, ip_key, ttl_seconds=_OTP_RATE_WINDOW)
    return None


@auth_email_bp.route("/email/start", methods=["POST"])
def email_start():
    """Create an OTP verification for {email, password} and email the code."""
    pending_org = session.get("pending_org")
    if not pending_org:
        return jsonify({"error": "missing_org"}), 400

    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""
    if not email or not password:
        return jsonify({"error": "missing_fields"}), 400

    if not password_strength_ok(password):
        return jsonify({"error": "weak_password"}), 400

    email_hash = blind_index(email)
    db = get_db()
    if db.users.find_one({"email_hash": email_hash}):
        return jsonify({"error": "already_registered"}), 409

    rate_limit_resp = _check_otp_send_rate_limit(db, email, _client_ip())
    if rate_limit_resp:
        return rate_limit_resp

    otp = _generate_otp()
    now = _now()

    db.otp_verifications.update_one(
        {"email_hash": email_hash},
        {
            "$set": {
                "otp_hash": _otp_hash(otp),
                "password_hash": hash_password(password),
                "pending_org": pending_org,
                "attempts": 0,
                "created_at": now,
                "expires_at": now + _OTP_TTL,
                "last_sent_at": now,
            }
        },
        upsert=True,
    )

    if not send_otp_email(email, otp):
        return jsonify({"error": "email_failed"}), 500

    # Plaintext email kept in the session temporarily so the OTP-verify page
    # doesn't have to round-trip it through the URL.
    session["pending_email"] = email
    return jsonify({"ok": True}), 200


@auth_email_bp.route("/email/verify-otp", methods=["POST"])
def verify_otp():
    """Verify the 6-digit code; on match, create org + user and sign in."""
    email = session.get("pending_email")
    if not email:
        return jsonify({"error": "session_expired"}), 400

    data = request.get_json(silent=True) or {}
    otp = (data.get("otp") or "").strip()
    if not otp:
        return jsonify({"error": "invalid_otp"}), 400

    email_hash = blind_index(email)
    db = get_db()
    doc = db.otp_verifications.find_one({"email_hash": email_hash})
    now = _now()

    expires_at = _aware(doc.get("expires_at")) if doc else None
    if expires_at is None or expires_at < now:
        return jsonify({"error": "expired"}), 400

    if doc.get("attempts", 0) >= _MAX_ATTEMPTS:
        return jsonify({"error": "too_many_attempts"}), 429

    if _otp_hash(otp) != doc.get("otp_hash"):
        db.otp_verifications.update_one(
            {"email_hash": email_hash},
            {"$inc": {"attempts": 1}},
        )
        return jsonify({"error": "invalid_otp"}), 400

    # --- match: create org + user (mirrors the Google register flow) ---
    pending_org = doc.get("pending_org") or {}
    name = email.split("@")[0]
    encrypted_fields, wrapped_dek = encrypt_fields({"name": name, "email": email})

    org_doc = {
        "name": pending_org.get("orgName", ""),
        "industry": pending_org.get("industry", ""),
        "company_size": pending_org.get("companySize", ""),
        "created_at": now,
    }
    org_id = db.organizations.insert_one(org_doc).inserted_id

    user_doc = {
        "email_hash": email_hash,
        "encrypted": encrypted_fields,
        "wrapped_dek": wrapped_dek,
        # Already argon2-hashed by email_start — never re-hash.
        "password_hash": doc["password_hash"],
        "org_id": org_id,
        "role": "admin",
        "created_at": now,
        "last_login": now,
    }
    user_id = db.users.insert_one(user_doc).inserted_id

    db.otp_verifications.delete_one({"email_hash": email_hash})
    session.pop("pending_org", None)
    session.pop("pending_email", None)

    session.permanent = True
    session["user_id"] = str(user_id)
    session["org_id"] = str(org_id)
    session["just_registered"] = True
    _record_active_session(db, user_id)
    return jsonify({"ok": True, "redirect": "/onboarding-complete.html"}), 200


@auth_email_bp.route("/email/resend-otp", methods=["POST"])
def resend_otp():
    """Generate a fresh OTP for the pending email (with a 60s cooldown)."""
    email = session.get("pending_email")
    if not email:
        return jsonify({"error": "session_expired"}), 400

    email_hash = blind_index(email)
    db = get_db()
    doc = db.otp_verifications.find_one({"email_hash": email_hash})
    if not doc:
        return jsonify({"error": "expired"}), 400

    now = _now()
    last_sent = _aware(doc.get("last_sent_at"))
    if last_sent:
        elapsed = now - last_sent
        if elapsed < _RESEND_COOLDOWN:
            retry_after = int((_RESEND_COOLDOWN - elapsed).total_seconds()) + 1
            return jsonify({
                "error": "Too many OTP requests. Please wait before requesting another code.",
                "retry_after": retry_after,
            }), 429, {"Retry-After": str(retry_after)}

    rate_limit_resp = _check_otp_send_rate_limit(db, email, _client_ip())
    if rate_limit_resp:
        return rate_limit_resp

    otp = _generate_otp()
    db.otp_verifications.update_one(
        {"email_hash": email_hash},
        {
            "$set": {
                "otp_hash": _otp_hash(otp),
                "expires_at": now + _OTP_TTL,
                "last_sent_at": now,
                "attempts": 0,
            }
        },
    )

    if not send_otp_email(email, otp):
        return jsonify({"error": "email_failed"}), 500
    return jsonify({"ok": True}), 200


@auth_email_bp.route("/password/signin", methods=["POST"])
def password_signin():
    """Returning-user sign-in with email + password."""
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""

    email_hash = blind_index(email)
    db = get_db()
    user = db.users.find_one({"email_hash": email_hash})
    if not user or not user.get("password_hash"):
        # Same response for "no such user" and "Google-only account" so we
        # don't reveal which case it is.
        return jsonify({"error": "invalid_credentials"}), 401

    now = _now()
    lockout_until = _aware(user.get("lockout_until"))
    if lockout_until and lockout_until > now:
        retry_after = int((lockout_until - now).total_seconds()) + 1
        return jsonify({"error": "locked", "retry_after": retry_after}), 423

    if not verify_password(password, user["password_hash"]):
        attempts = user.get("failed_login_attempts", 0) + 1
        update = {"$set": {"failed_login_attempts": attempts, "last_login_attempt": now}}
        if attempts >= _LOCKOUT_AFTER:
            update["$set"]["lockout_until"] = now + _LOCKOUT_TTL
        db.users.update_one({"_id": user["_id"]}, update)
        return jsonify({"error": "invalid_credentials"}), 401

    db.users.update_one(
        {"_id": user["_id"]},
        {
            "$set": {"failed_login_attempts": 0, "last_login": now},
            "$unset": {"lockout_until": ""},
        },
    )

    pii = decrypt_fields(user.get("encrypted"), user.get("wrapped_dek", ""))
    session.permanent = True
    session["user_id"] = str(user["_id"])
    session["org_id"] = str(user["org_id"])
    session["user_name"] = pii.get("name", "")
    session["user_email"] = pii.get("email", "")
    _record_active_session(db, user["_id"])

    # TOTP gate: return the appropriate redirect so the frontend can
    # send the user to TOTP verification or forced setup when needed.
    result = _login_result_for_user(db, user)
    return jsonify({"ok": True, **result}), 200


@auth_email_bp.route("/email/signin", methods=["POST"])
def email_signin():
    """Email-based sign-in: verify credentials, then either sign in directly
    or start OTP verification depending on account type.

    Flow:
      1. If password matches → sign in directly (like /password/signin)
      2. If password doesn't match → treat as "account exists but wrong password"
         → send OTP to the email on file so the user can verify via code
      3. If no user with password hash → return invalid_credentials (Google-only account)
    """
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify({"error": "missing_fields"}), 400

    email_hash = blind_index(email)
    db = get_db()
    user = db.users.find_one({"email_hash": email_hash})

    if not user:
        # No account at all — Google-only or never-registered
        return jsonify({"error": "invalid_credentials"}), 401

    if not user.get("password_hash"):
        # User exists but is Google-only (no password hash) — OTP won't help,
        # send them to Google sign-in instead.  Return the same generic error
        # as "no account" to prevent email enumeration.
        return jsonify({"error": "invalid_credentials"}), 401

    # User has a password hash — verify the provided password
    now = _now()
    lockout_until = _aware(user.get("lockout_until"))
    if lockout_until and lockout_until > now:
        retry_after = int((lockout_until - now).total_seconds()) + 1
        return jsonify({"error": "locked", "retry_after": retry_after}), 423

    if not verify_password(password, user["password_hash"]):
        # Password incorrect: instead of just rejecting, send OTP to the
        # on-file email so the user can recover via verification code.
        # This supports the "email or password" sign-in UX where a wrong
        # password triggers OTP recovery.
        attempts = user.get("failed_login_attempts", 0) + 1
        update = {"$set": {"failed_login_attempts": attempts, "last_login_attempt": now}}
        if attempts >= _LOCKOUT_AFTER:
            update["$set"]["lockout_until"] = now + _LOCKOUT_TTL
        db.users.update_one({"_id": user["_id"]}, update)
        # Send OTP to the email on file
        pending_email = user.get("user_email") or user.get("email") or email
        # Decrypt PII to get the actual email
        try:
            pii = decrypt_fields(user.get("encrypted"), user.get("wrapped_dek", ""))
            pending_email = pii.get("email", email)
        except Exception:
            pending_email = email

        otp = _generate_otp()
        db.otp_verifications.update_one(
            {"email_hash": blind_index(pending_email)},
            {
                "$set": {
                    "otp_hash": _otp_hash(otp),
                    "password_hash": user["password_hash"],
                    "pending_org": session.get("pending_org"),
                    "attempts": 0,
                    "created_at": now,
                    "expires_at": now + _OTP_TTL,
                    "last_sent_at": now,
                }
            },
            upsert=True,
        )

        if not send_otp_email(pending_email, otp):
            return jsonify({"error": "email_failed"}), 500

        session["pending_email"] = pending_email
        return jsonify({"ok": True, "requires_otp": True}), 200

    # Password correct — sign in directly
    db.users.update_one(
        {"_id": user["_id"]},
        {
            "$set": {"failed_login_attempts": 0, "last_login": now},
            "$unset": {"lockout_until": ""},
        },
    )
    pii = decrypt_fields(user.get("encrypted"), user.get("wrapped_dek", ""))
    session.permanent = True
    session["user_id"] = str(user["_id"])
    session["org_id"] = str(user["org_id"])
    session["user_name"] = pii.get("name", "")
    session["user_email"] = pii.get("email", "")
    _record_active_session(db, user["_id"])

    result = _login_result_for_user(db, user)
    return jsonify({"ok": True, **result}), 200


@auth_email_bp.route("/forgot-password", methods=["POST"])
def forgot_password():
    """Placeholder for password reset request. Always returns success to avoid email enumeration."""
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    # In a real implementation, you would send a reset email if the account exists.
    # For now, we just acknowledge the request.
    return jsonify({"ok": True, "message": "If the email exists, a reset link has been sent."}), 200

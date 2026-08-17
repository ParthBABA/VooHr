"""TOTP setup & verification routes for VooHr.

These routes handle the mandatory TOTP enrollment that every admin must
complete after registration.  The flow is:

  1. Frontend calls POST /auth/totp/setup  (requires an active session)
       → generates a secret, stores it in the session, returns QR + URI
  2. Frontend calls POST /auth/totp/verify-setup  { "code": "123456" }
       → verifies the code against the pending secret, then persists
         totp_enabled / totp_secret on the user doc and clears the session
         pending state.  Also generates 10 single-use backup codes (shown
         to the user exactly once).
  3. GET /auth/totp/status  → { "totp_enabled": true/false }
       → used by the frontend to decide whether to show the setup page or
         redirect straight to /dashboard.
  4. POST /auth/totp/verify-login-backup  { "code": "XXXX-XXXX" }
       → allows a user who has lost their authenticator device to log in
         with a one-time backup code.
  5. POST /auth/totp/regenerate-backup-codes
       → invalidates existing backup codes and returns a fresh batch.
         Requires an already TOTP-verified session.
"""

import time
from datetime import datetime, timezone

from bson import ObjectId
from flask import Blueprint, jsonify, request, session

from extensions import get_db
from totp_utils import (
    generate_backup_codes,
    generate_secret,
    hash_backup_code,
    provisioning_uri,
    qr_code_data_url,
    verify_backup_code,
    verify_code,
)

totp_bp = Blueprint("totp", __name__)

# ── Rate limiting for backup-code brute-force protection ──────────────
_BACKUPCodeAttempts: dict[str, list[float]] = {}
_BACKUPCodeAttempts_MAX = 5
_BACKUPCodeAttempts_WINDOW = 900  # 15 minutes in seconds


def _check_backup_rate_limit(user_id_str: str) -> bool:
    """Return True if the attempt is allowed, False if rate-limited."""
    now = time.time()
    attempts = _BACKUPCodeAttempts.setdefault(user_id_str, [])
    # Prune old entries outside the window
    cutoff = now - _BACKUPCodeAttempts_WINDOW
    attempts[:] = [t for t in attempts if t > cutoff]
    if len(attempts) >= _BACKUPCodeAttempts_MAX:
        return False
    attempts.append(now)
    return True


def _current_user_id():
    """Return the ObjectId for the logged-in user, or None."""
    uid = session.get("user_id")
    if not uid:
        return None
    try:
        return ObjectId(uid)
    except Exception:
        return None


@totp_bp.route("/totp/setup", methods=["POST"])
def totp_setup():
    """Generate a fresh TOTP secret, stash it in the session, and return
    the provisioning URI + QR code so the frontend can display them.

    The secret is *not* written to the DB yet — that happens only after
    the user successfully verifies a code via /totp/verify-setup.
    """
    user_id = _current_user_id()
    if not user_id:
        return jsonify({"error": "unauthenticated"}), 401

    db = get_db()
    user = db.users.find_one(
        {"_id": user_id},
        {"email_hash": 1, "encrypted": 1, "wrapped_dek": 1, "totp_enabled": 1},
    )
    if not user:
        return jsonify({"error": "user_not_found"}), 404

    # If TOTP is already enabled there's nothing to set up.
    if user.get("totp_enabled") is True:
        return jsonify({"error": "totp_already_enabled"}), 409

    # Derive a display name for the authenticator app from the encrypted PII.
    from field_encryption import decrypt_fields

    pii = decrypt_fields(user.get("encrypted"), user.get("wrapped_dek", ""))
    email = pii.get("email", "")
    display_name = email or str(user_id)

    secret = generate_secret()
    uri = provisioning_uri(secret, display_name)
    qr = qr_code_data_url(uri)

    # Keep the secret in the session until verify-setup confirms it.
    session["pending_totp_secret"] = secret

    return jsonify({"ok": True, "secret": secret, "uri": uri, "qr": qr}), 200


@totp_bp.route("/totp/verify-setup", methods=["POST"])
def totp_verify_setup():
    """Verify the user-supplied 6-digit code against the pending secret
    and, on success, persist TOTP credentials on the user document.

    Also generates 10 single-use backup codes, stores them hashed, and
    returns the plaintext codes once — this is the only time they are shown.
    """
    user_id = _current_user_id()
    if not user_id:
        return jsonify({"error": "unauthenticated"}), 401

    pending_secret = session.get("pending_totp_secret")
    if not pending_secret:
        return jsonify({"error": "no_pending_setup"}), 400

    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip()
    if not code:
        return jsonify({"error": "missing_code"}), 400

    if not verify_code(pending_secret, code):
        return jsonify({"error": "invalid_code"}), 400

    # Generate backup codes: store hashed, return plaintext once.
    plaintext_codes = generate_backup_codes()
    hashed_codes = [
        {"code_hash": hash_backup_code(c), "used": False}
        for c in plaintext_codes
    ]

    db = get_db()
    db.users.update_one(
        {"_id": user_id},
        {"$set": {
            "totp_enabled": True,
            "totp_secret": pending_secret,
            "totp_backup_codes": hashed_codes,
        }},
    )
    session.pop("pending_totp_secret", None)

    return jsonify({"ok": True, "backup_codes": plaintext_codes}), 200


@totp_bp.route("/totp/verify-login", methods=["POST"])
def totp_verify_login():
    """Verify a TOTP code for an existing session.

    Called on every new login where TOTP is already enabled.  On success
    the session is marked as TOTP-verified so the before_request guard
    lets future requests through.
    """
    user_id = _current_user_id()
    if not user_id:
        return jsonify({"error": "unauthenticated"}), 401

    db = get_db()
    user = db.users.find_one({"_id": user_id}, {"totp_enabled": 1, "totp_secret": 1})
    if not user:
        return jsonify({"error": "user_not_found"}), 404
    if user.get("totp_enabled") is not True:
        return jsonify({"error": "totp_not_enabled"}), 400

    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip()
    if not code:
        return jsonify({"error": "missing_code"}), 400

    if not verify_code(user["totp_secret"], code):
        return jsonify({"error": "invalid_code"}), 400

    session["totp_verified_session"] = session.get("session_token", "")
    return jsonify({"ok": True}), 200


@totp_bp.route("/totp/status", methods=["GET"])
def totp_status():
    """Return whether the current user has TOTP enabled."""
    user_id = _current_user_id()
    if not user_id:
        return jsonify({"error": "unauthenticated"}), 401

    db = get_db()
    user = db.users.find_one({"_id": user_id}, {"totp_enabled": 1})
    if not user:
        return jsonify({"error": "user_not_found"}), 404

    return jsonify({"totp_enabled": user.get("totp_enabled") is True}), 200


# ── Backup code verification (login recovery) ─────────────────────────

@totp_bp.route("/totp/verify-login-backup", methods=["POST"])
def totp_verify_login_backup():
    """Verify a single-use backup code for an existing session.

    Called when a user has lost their authenticator device.  On success the
    session is marked as TOTP-verified (same as totp_verify_login) and the
    used code is flagged so it can never be reused.
    """
    user_id = _current_user_id()
    if not user_id:
        return jsonify({"error": "unauthenticated"}), 401

    uid_str = str(user_id)
    if not _check_backup_rate_limit(uid_str):
        return jsonify({
            "error": "too_many_attempts",
            "message": "Too many attempts. Please try again later.",
        }), 429

    db = get_db()
    user = db.users.find_one(
        {"_id": user_id},
        {"totp_enabled": 1, "totp_backup_codes": 1},
    )
    if not user:
        return jsonify({"error": "user_not_found"}), 404
    if user.get("totp_enabled") is not True:
        return jsonify({"error": "totp_not_enabled"}), 400

    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip()
    if not code:
        return jsonify({"error": "missing_code"}), 400

    backup_codes = user.get("totp_backup_codes") or []
    match_index = verify_backup_code(code, backup_codes)
    if match_index is None:
        return jsonify({"error": "invalid_code"}), 400

    # Mark the code as used (atomic update).
    db.users.update_one(
        {"_id": user_id},
        {"$set": {f"totp_backup_codes.{match_index}.used": True}},
    )

    # Mark session as TOTP-verified (same as totp_verify_login).
    session["totp_verified_session"] = session.get("session_token", "")

    # Reset rate limit on success.
    _BACKUPCodeAttempts.pop(uid_str, None)

    codes_remaining = sum(1 for c in backup_codes if not c.get("used")) - 1
    resp = {"ok": True, "codes_remaining": codes_remaining}
    if codes_remaining <= 2:
        resp["regenerate_recommended"] = True
    return jsonify(resp), 200


# ── Backup code regeneration ──────────────────────────────────────────

@totp_bp.route("/totp/regenerate-backup-codes", methods=["POST"])
def totp_regenerate_backup_codes():
    """Invalidate all existing backup codes and generate a fresh batch.

    Requires an already TOTP-verified session to prevent unauthenticated
    callers from nuking a user's recovery capability.
    """
    user_id = _current_user_id()
    if not user_id:
        return jsonify({"error": "unauthenticated"}), 401

    session_token = session.get("session_token")
    if session.get("totp_verified_session") != session_token:
        return jsonify({"error": "totp_not_verified"}), 403

    db = get_db()
    user = db.users.find_one({"_id": user_id}, {"totp_enabled": 1})
    if not user:
        return jsonify({"error": "user_not_found"}), 404
    if user.get("totp_enabled") is not True:
        return jsonify({"error": "totp_not_enabled"}), 400

    plaintext_codes = generate_backup_codes()
    hashed_codes = [
        {"code_hash": hash_backup_code(c), "used": False}
        for c in plaintext_codes
    ]

    db.users.update_one(
        {"_id": user_id},
        {"$set": {"totp_backup_codes": hashed_codes}},
    )

    return jsonify({"ok": True, "backup_codes": plaintext_codes}), 200


# ── Backup code count (for Settings UI) ───────────────────────────────

@totp_bp.route("/totp/backup-codes-status", methods=["GET"])
def totp_backup_codes_status():
    """Return the count of unused backup codes for the current user.

    Requires an already TOTP-verified session.
    """
    user_id = _current_user_id()
    if not user_id:
        return jsonify({"error": "unauthenticated"}), 401

    session_token = session.get("session_token")
    if session.get("totp_verified_session") != session_token:
        return jsonify({"error": "totp_not_verified"}), 403

    db = get_db()
    user = db.users.find_one(
        {"_id": user_id},
        {"totp_enabled": 1, "totp_backup_codes": 1},
    )
    if not user or user.get("totp_enabled") is not True:
        return jsonify({"codes_remaining": 0, "has_backup_codes": False})

    codes = user.get("totp_backup_codes") or []
    remaining = sum(1 for c in codes if not c.get("used"))
    return jsonify({"codes_remaining": remaining, "has_backup_codes": True}), 200

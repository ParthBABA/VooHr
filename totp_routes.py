"""TOTP setup & verification routes for VooHr.

These routes handle the mandatory TOTP enrollment that every admin must
complete after registration.  The flow is:

  1. Frontend calls POST /auth/totp/setup  (requires an active session)
       → generates a secret, stores it in the session, returns QR + URI
  2. Frontend calls POST /auth/totp/verify-setup  { "code": "123456" }
       → verifies the code against the pending secret, then persists
         totp_enabled / totp_secret on the user doc and clears the session
         pending state.
  3. GET /auth/totp/status  → { "totp_enabled": true/false }
       → used by the frontend to decide whether to show the setup page or
         redirect straight to /dashboard.
"""

from bson import ObjectId
from flask import Blueprint, jsonify, request, session

from extensions import get_db
from totp_utils import generate_secret, provisioning_uri, qr_code_data_url, verify_code

totp_bp = Blueprint("totp", __name__)


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

    db = get_db()
    db.users.update_one(
        {"_id": user_id},
        {"$set": {"totp_enabled": True, "totp_secret": pending_secret}},
    )
    session.pop("pending_totp_secret", None)

    return jsonify({"ok": True}), 200


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

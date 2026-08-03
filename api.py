from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, jsonify, request, session

from extensions import get_db
from field_encryption import decrypt_fields, encrypt_fields

api_bp = Blueprint("api", __name__)


@api_bp.route("/onboarding/org", methods=["POST"])
def save_pending_org():
    """Step 1 of sign-up: stash org details in the session until the user
    verifies their identity with Google (email-verify.html -> /auth/google/register).
    """
    data = request.get_json(silent=True) or {}
    org_name = (data.get("orgName") or "").strip()
    industry = (data.get("industry") or "").strip()
    company_size = (data.get("companySize") or "").strip()

    if not org_name or not industry or not company_size:
        return jsonify({"error": "missing_fields"}), 400

    session["pending_org"] = {
        "orgName": org_name,
        "industry": industry,
        "companySize": company_size,
    }
    return jsonify({"ok": True})


@api_bp.route("/me")
def me():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    try:
        user = db.users.find_one({"_id": ObjectId(user_id)})
    except InvalidId:
        session.clear()
        return jsonify({"error": "not_authenticated"}), 401

    if not user:
        session.clear()
        return jsonify({"error": "not_authenticated"}), 401

    org = db.organizations.find_one({"_id": user["org_id"]})

    pii = decrypt_fields(user.get("encrypted"), user.get("wrapped_dek", ""))

    return jsonify(
        {
            "id": str(user["_id"]),
            "name": pii.get("name", ""),
            "email": pii.get("email", ""),
            "role": user["role"],
            "picture": user.get("picture"),
            "just_registered": session.pop("just_registered", False),
            "organization": (
                {
                    "id": str(org["_id"]),
                    "name": org["name"],
                    "industry": org["industry"],
                    "company_size": org["company_size"],
                }
                if org
                else None
            ),
        }
    )


@api_bp.route("/me", methods=["PATCH"])
def update_me():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    try:
        user = db.users.find_one({"_id": ObjectId(user_id)})
    except InvalidId:
        session.clear()
        return jsonify({"error": "not_authenticated"}), 401

    if not user:
        session.clear()
        return jsonify({"error": "not_authenticated"}), 401

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name_required"}), 400
    if len(name) > 100:
        return jsonify({"error": "name_too_long"}), 400

    # Decrypt the existing PII so untouched fields (e.g. email) survive the
    # re-encryption, then encrypt the whole dict again with the updated name —
    # the same envelope-encryption pattern auth.py uses at signup.
    pii = decrypt_fields(user.get("encrypted"), user.get("wrapped_dek", ""))
    pii["name"] = name
    encrypted_fields, wrapped_dek = encrypt_fields(pii)

    db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"encrypted": encrypted_fields, "wrapped_dek": wrapped_dek}},
    )

    session["user_name"] = name

    return jsonify({"ok": True, "name": name})

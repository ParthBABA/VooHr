from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, jsonify, request, session

import re

from extensions import get_db
from employees import _session_is_active, TOTPRequired
from field_encryption import decrypt_fields, encrypt_fields
from login_flow import _hash_session_token

api_bp = Blueprint("api", __name__)


def _check_auth():
    """Validate session + TOTP for API routes that do manual checks.

    Returns the user_id string on success.  Raises TOTPRequired if the
    admin session needs TOTP verification.  Returns None (after clearing
    the session) for invalid/expired sessions.
    """
    user_id = session.get("user_id")
    if not user_id:
        return None
    if not _session_is_active(user_id, session.get("session_token")):
        session.clear()
        return None
    try:
        uid = ObjectId(user_id)
    except InvalidId:
        session.clear()
        return None
    db = get_db()
    user = db.users.find_one({"_id": uid}, {"role": 1, "totp_enabled": 1})
    if user and user.get("role") == "admin" and user.get("totp_enabled") is True:
        if session.get("totp_verified_session") != session.get("session_token"):
            raise TOTPRequired()
    return user_id


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
    user_id = _check_auth()
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
    user_id = _check_auth()
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


VALID_ORG_INDUSTRIES = {
    "Technology",
    "Finance",
    "Healthcare",
    "Education",
    "Manufacturing",
    "Retail",
    "Real Estate",
    "Media",
    "Legal",
    "Consulting",
    "Other",
}
VALID_ORG_SIZES = {"1-10", "11-50", "51-200", "201-1000", "1000+"}


@api_bp.route("/organization", methods=["PUT"])
def update_organization():
    user_id = _check_auth()
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

    if user.get("role") != "admin":
        return jsonify({"error": "forbidden"}), 403

    org = db.organizations.find_one({"_id": user["org_id"]})
    if not org:
        return jsonify({"error": "org_not_found"}), 404

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    industry = (data.get("industry") or "").strip()
    company_size = (data.get("company_size") or "").strip()

    if not name:
        return jsonify({"error": "org_name_required"}), 400
    if len(name) > 150:
        return jsonify({"error": "org_name_too_long"}), 400
    if industry not in VALID_ORG_INDUSTRIES:
        return jsonify({"error": "invalid_industry"}), 400
    if company_size not in VALID_ORG_SIZES:
        return jsonify({"error": "invalid_company_size"}), 400

    db.organizations.update_one(
        {"_id": org["_id"]},
        {"$set": {"name": name, "industry": industry, "company_size": company_size}},
    )

    return jsonify(
        {
            "ok": True,
            "organization": {
                "id": str(org["_id"]),
                "name": name,
                "industry": industry,
                "company_size": company_size,
            },
        }
    )


@api_bp.route("/organization/notification-prefs")
def get_organization_notification_prefs():
    user_id = _check_auth()
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
    prefs = (org or {}).get("notification_prefs") or {}
    return jsonify({"risk_alerts": bool(prefs.get("risk_alerts", True))})


@api_bp.route("/organization/notification-prefs", methods=["PUT"])
def update_organization_notification_prefs():
    user_id = _check_auth()
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

    if user.get("role") != "admin":
        return jsonify({"error": "forbidden"}), 403

    org = db.organizations.find_one({"_id": user["org_id"]})
    if not org:
        return jsonify({"error": "org_not_found"}), 404

    data = request.get_json(silent=True) or {}
    risk_alerts = data.get("risk_alerts")
    if not isinstance(risk_alerts, bool):
        return jsonify({"error": "invalid_risk_alerts"}), 400

    db.organizations.update_one(
        {"_id": org["_id"]},
        {"$set": {"notification_prefs.risk_alerts": risk_alerts}},
    )

    return jsonify({"ok": True, "risk_alerts": risk_alerts})


def _extract_version(ua: str, marker: str, max_parts=None):
    """Grab the version number that follows `marker` (e.g. "Chrome/" ->
    "128", "Mac OS X " -> "10.15.7"), normalizing underscores to dots.
    Returns None if absent."""
    idx = ua.find(marker)
    if idx == -1:
        return None
    rest = ua[idx + len(marker):]
    m = re.match(r"(\d+(?:[._]\d+)*)", rest)
    if not m:
        return None
    parts = m.group(1).replace("_", ".").split(".")
    if max_parts:
        parts = parts[:max_parts]
    return ".".join(parts)


_WINDOWS_VERSIONS = {
    "10.0": "10",
    "6.3": "8.1",
    "6.2": "8",
    "6.1": "7",
    "6.0": "Vista",
    "5.1": "XP",
    "5.0": "2000",
}


def _parse_device(user_agent: str) -> dict:
    """Device breakdown for the sessions list. Returns
    {"device_type", "browser", "os"} — missing pieces fall back to simple
    names or are omitted, never crash or show "undefined".
    """
    ua = user_agent or ""

    # device_type
    if "iPad" in ua:
        device_type = "Tablet"
    elif "iPhone" in ua or "iPod" in ua:
        device_type = "Mobile"
    elif "Android" in ua:
        device_type = "Mobile" if "Mobile" in ua else "Tablet"
    else:
        device_type = "Desktop"

    # browser + version
    browser = "Unknown"
    browser_version = None
    if "Edg/" in ua:
        browser = "Edge"
        browser_version = _extract_version(ua, "Edg/", max_parts=1)
    elif "Chrome" in ua:
        browser = "Chrome"
        browser_version = _extract_version(ua, "Chrome/", max_parts=1)
    elif "Firefox" in ua:
        browser = "Firefox"
        browser_version = _extract_version(ua, "Firefox/", max_parts=1)
    elif "Safari" in ua:
        browser = "Safari"
        browser_version = _extract_version(ua, "Version/", max_parts=1)
    elif "MSIE" in ua or "Trident" in ua:
        browser = "Internet Explorer"
        browser_version = _extract_version(ua, "MSIE ", max_parts=1)
    browser_label = browser if browser_version is None else f"{browser} {browser_version}"

    # os + version
    os_name = "Unknown"
    os_version = None
    if "iPad" in ua:
        os_name = "iPadOS"
        os_version = _extract_version(ua, "CPU OS ") or _extract_version(ua, "CPU iPhone OS ")
    elif "iPhone" in ua or "iPod" in ua:
        os_name = "iOS"
        os_version = _extract_version(ua, "CPU iPhone OS ") or _extract_version(ua, "CPU OS ")
    elif "Android" in ua:
        os_name = "Android"
        os_version = _extract_version(ua, "Android ")
    elif "Windows NT" in ua:
        os_name = "Windows"
        nt = _extract_version(ua, "Windows NT ")
        os_version = _WINDOWS_VERSIONS.get(nt, nt)
    elif "Mac OS X" in ua or "Macintosh" in ua:
        os_name = "macOS"
        os_version = _extract_version(ua, "Mac OS X ")
    elif "Linux" in ua:
        os_name = "Linux"
    os_label = os_name if os_version is None else f"{os_name} {os_version}"

    return {"device_type": device_type, "browser": browser_label, "os": os_label}


@api_bp.route("/sessions/active")
def list_active_sessions():
    user_id = _check_auth()
    if not user_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    current_token = session.get("session_token")
    current_hash = _hash_session_token(current_token) if current_token else None
    docs = db.active_sessions.find({"user_id": ObjectId(user_id)}).sort("last_seen", -1)

    sessions = []
    for d in docs:
        sessions.append(
            {
                "id": str(d["_id"]),
                "device": _parse_device(d.get("user_agent", "")),
                "location": d.get("location"),
                "created_at": d.get("created_at").isoformat() if d.get("created_at") else None,
                "last_seen": d.get("last_seen").isoformat() if d.get("last_seen") else None,
                "is_current": d.get("session_token") == current_hash,
            }
        )
    return jsonify({"sessions": sessions})


@api_bp.route("/sessions/revoke/<session_doc_id>", methods=["POST"])
def revoke_active_session(session_doc_id):
    user_id = _check_auth()
    if not user_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    try:
        doc_id = ObjectId(session_doc_id)
    except InvalidId:
        return jsonify({"error": "not_found"}), 404

    doc = db.active_sessions.find_one({"_id": doc_id, "user_id": ObjectId(user_id)})
    if not doc:
        return jsonify({"error": "not_found"}), 404

    if doc.get("session_token") == _hash_session_token(session.get("session_token", "")):
        return jsonify({"error": "cannot_revoke_current"}), 400

    db.active_sessions.delete_one({"_id": doc_id})
    return jsonify({"ok": True})

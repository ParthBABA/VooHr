from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, jsonify, make_response, request, session

import re
import threading
from datetime import datetime, timezone

from audit_log import (
    ACTION_ACCOUNT_DELETE,
    ACTION_ACCOUNT_EXPORT,
    ACTION_ORG_UPDATE,
    ACTION_SESSION_REVOKE,
    log_audit_event,
)
from extensions import check_rate_limit, get_db, record_rate_limit_event
from employees import _session_is_active, TOTPRequired
from field_encryption import decrypt_fields, encrypt_fields
from login_flow import (
    _clean_ch,
    _client_ip,
    _hash_session_token,
    _is_private_ip,
    _lookup_location,
)
from password_utils import verify_password
from totp_utils import verify_backup_code, verify_code

api_bp = Blueprint("api", __name__)

# Rate-limit constants for self-service account data export (mirrors the
# per-email / per-IP pattern used for OTP sends in auth_email.py).
_ACCOUNT_EXPORT_MAX_PER_USER = 5     # max exports per user per window
_ACCOUNT_EXPORT_MAX_PER_IP = 20      # max exports per IP per window
_ACCOUNT_EXPORT_RATE_WINDOW = 900    # 15-minute sliding window in seconds


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
            "linked_employee_id": str(user["linked_employee_id"]) if user.get("linked_employee_id") else None,
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


@api_bp.route("/me/export")
def export_my_data():
    """Self-service data portability: download the *logged-in user's own*
    decrypted profile plus their organization's details as a JSON attachment.

    This is deliberately distinct from /api/employees/<id>/export (which
    downloads a single *employee's* HR record on behalf of the org).  Here we
    export only the account holder's own data — no other employees' data — and
    we rate-limit it like other sensitive routes.
    """
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

    ip = _client_ip()
    user_key = f"account_export_user:{user_id}"
    ip_key = f"account_export_ip:{ip}"
    for key, max_events in (
        (user_key, _ACCOUNT_EXPORT_MAX_PER_USER),
        (ip_key, _ACCOUNT_EXPORT_MAX_PER_IP),
    ):
        allowed, retry_after = check_rate_limit(db, key, max_events, _ACCOUNT_EXPORT_RATE_WINDOW)
        if not allowed:
            return jsonify({"error": "rate_limited", "retry_after": retry_after}), 429
    # Record the allowed event so the limits count toward future requests.
    record_rate_limit_event(db, user_key, ttl_seconds=_ACCOUNT_EXPORT_RATE_WINDOW)
    record_rate_limit_event(db, ip_key, ttl_seconds=_ACCOUNT_EXPORT_RATE_WINDOW)

    org = db.organizations.find_one({"_id": user["org_id"]})
    pii = decrypt_fields(user.get("encrypted"), user.get("wrapped_dek", ""))

    export_data = {
        "account": {
            "id": str(user["_id"]),
            "name": pii.get("name", ""),
            "email": pii.get("email", ""),
            "role": user.get("role", ""),
            "picture": user.get("picture"),
            "created_at": user.get("created_at").isoformat() if user.get("created_at") else None,
            "last_login": user.get("last_login").isoformat() if user.get("last_login") else None,
            "totp_enabled": user.get("totp_enabled") is True,
        },
        "organization": (
            {
                "id": str(org["_id"]),
                "name": org.get("name"),
                "industry": org.get("industry"),
                "company_size": org.get("company_size"),
                "created_at": org.get("created_at").isoformat() if org.get("created_at") else None,
            }
            if org
            else None
        ),
        "exported_at": datetime.now(timezone.utc).isoformat(),
    }

    log_audit_event(
        db, user.get("org_id"), user_id, session.get("user_name") or "",
        ACTION_ACCOUNT_EXPORT,
        target_type="account", target_id=user_id,
        target_label="My account data export",
    )

    resp = make_response(jsonify(export_data))
    resp.headers["Content-Disposition"] = 'attachment; filename="voovr_account_export.json"'
    return resp


@api_bp.route("/me", methods=["DELETE"])
def delete_me():
    """Self-service account deletion — destructive and irreversible.

    Re-confirms identity by requiring the user's current password (if they
    have one) OR a valid TOTP / recovery code (if TOTP is enabled).  A `role`
    is always "admin" in this data model, so if this user is the *only* member
    of their org, deleting the account would orphan the organization and all
    of its tenant data (employees/sessions/notifications).  We choose to
    CASCADE-DELETE that org + its data so no PII is left behind pointing at a
    dead account.  (Blocking instead would trap the sole user with no one to
    transfer to, since no non-admin role is wired up yet.)
    """
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
    password = data.get("password") or ""
    totp_code = data.get("totpCode") or ""

    has_password = bool(user.get("password_hash"))
    totp_enabled = user.get("totp_enabled") is True

    # Re-confirmation is required only if the account has a configurable
    # factor (password and/or TOTP).  A Google-OAuth-only account has no
    # password_hash and no TOTP, so there is nothing further to verify —
    # the already-validated session is the confirmation.
    needs_confirmation = has_password or totp_enabled
    method = None
    if needs_confirmation:
        if has_password and password:
            if verify_password(password, user["password_hash"]):
                method = "password"
        if totp_enabled and totp_code and method is None:
            if verify_code(user["totp_secret"], totp_code, valid_window=2):
                method = "totp"
            elif verify_backup_code(totp_code, user.get("totp_backup_codes") or []) is not None:
                method = "backup"
        if method is None:
            return jsonify({"error": "reconfirmation_required"}), 400

    org_id = user.get("org_id")

    # Decide whether this account is the last member of its organization.
    is_last_user = db.users.count_documents({"org_id": org_id}) <= 1
    delete_org = is_last_user

    # Log BEFORE deleting anything.  If the org is also deleted, this entry
    # goes with it — that's expected and records the account removal.
    log_audit_event(
        db, org_id, user_id, session.get("user_name") or "",
        ACTION_ACCOUNT_DELETE,
        target_type="account", target_id=user_id,
        meta={"method": method, "deleted_organization": delete_org},
    )

    # Revoke every auth session for this user, then delete the user doc.
    db.active_sessions.delete_many({"user_id": user["_id"]})
    db.users.delete_one({"_id": user["_id"]})

    # If this was the only user in the org, cascade-delete the orphaned
    # organization and its tenant data so nothing points at a dead account.
    if delete_org and org_id:
        db.employees.delete_many({"org_id": org_id})
        db.sessions.delete_many({"org_id": org_id})
        db.notifications.delete_many({"org_id": org_id})
        db.audit_log.delete_many({"org_id": org_id})
        db.organizations.delete_one({"_id": org_id})

    session.clear()

    return jsonify({"ok": True})


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

    log_audit_event(
        db, user["org_id"], user_id, session.get("user_name") or "",
        ACTION_ORG_UPDATE,
        target_type="organization", target_id=str(org["_id"]),
        target_label=name or "Organization",
        meta={"name": name, "industry": industry, "company_size": company_size},
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
    # NOTE: "10.0" is intentionally NOT mapped here.  Modern Chromium
    # reports "Windows NT 10.0" for BOTH Windows 10 and Windows 11, so the
    # User-Agent alone cannot distinguish them — see _parse_device, which
    # resolves that case via Sec-CH-UA-Platform-Version or falls back to a
    # plain "Windows" label instead of guessing.
    "6.3": "8.1",
    "6.2": "8",
    "6.1": "7",
    "6.0": "Vista",
    "5.1": "XP",
    "5.0": "2000",
}


def _windows_display_name(nt_version, ch_platform=None, ch_platform_version=None):
    """Resolve the Windows label for an NT kernel version.

    UA-CH semantics (https://wicg.github.io/ua-client-hints): Windows 11
    reports platform version major >= 13; Windows 10 reports 1-10.  The
    hint is only trusted when its companion Sec-CH-UA-Platform agrees with
    (or is absent alongside) a Windows User-Agent.  When nothing reliable
    is available — e.g. Firefox/Safari, or sessions recorded before hints
    were captured — the generic "Windows" is returned rather than a guess.
    """
    if nt_version != "10.0":
        return _WINDOWS_VERSIONS.get(nt_version, nt_version)

    if ch_platform and ch_platform.strip().lower() != "windows":
        return None
    raw = (ch_platform_version or "").strip()
    m = re.match(r"(\d+)", raw)
    if not m:
        return None
    try:
        major = int(m.group(1))
    except ValueError:
        return None
    if major >= 13:
        return "11"
    if 1 <= major <= 10:
        return "10"
    return None


def _parse_device(user_agent: str, ch_platform=None, ch_platform_version=None) -> dict:
    """Device breakdown for the sessions list. Returns
    {"device_type", "browser", "os"} — missing pieces fall back to simple
    names or are omitted, never crash or show "undefined".

    `ch_platform` / `ch_platform_version` are the Sec-CH-UA-Platform /
    Sec-CH-UA-Platform-Version values captured at login time (stored in the
    active_sessions doc).  They are required to tell Windows 11 apart from
    Windows 10; without them the OS degrades to plain "Windows".
    """
    # Rows recorded before login_flow._clean_ch existed may hold the raw
    # structured-field values WITH surrounding quotes (e.g. '"Windows"').
    ch_platform = _clean_ch(ch_platform)
    ch_platform_version = _clean_ch(ch_platform_version)
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
        win_display = _windows_display_name(nt, ch_platform, ch_platform_version)
        os_version = win_display
    elif "Mac OS X" in ua or "Macintosh" in ua:
        os_name = "macOS"
        os_version = _extract_version(ua, "Mac OS X ")
    elif "Linux" in ua:
        os_name = "Linux"
    os_label = os_name if os_version is None else f"{os_name} {os_version}"

    return {"device_type": device_type, "browser": browser_label, "os": os_label}


def _sanitize_location(raw) -> dict:
    """Reduce a stored location doc to the public shape
    {"city", "region", "country"} (nulls when unknown).  Returns None when
    nothing is known.  Guarantees no extra fields (e.g. the server-side IP)
    can ever leak into the API response."""
    if not isinstance(raw, dict):
        return None
    clean = {
        key: (raw.get(key) or None)
        for key in ("city", "region", "country")
    }
    if not any(clean.values()):
        return None
    return clean


def _location_is_known(raw) -> bool:
    return isinstance(raw, dict) and any(
        raw.get(k) for k in ("city", "region", "country")
    )


def _ip_is_local(doc) -> bool:
    """True when a stored session row came from a non-routable address
    (loopback/RFC1918/CGNAT/link-local) or recorded no usable address.
    Lets the UI explain an absent location without ever exposing the IP."""
    return _is_private_ip(doc.get("ip") or "")


def _backfill_location_by_id(db, doc_id, ip):
    """Background geo backfill for a single stale session row. Never raises."""
    try:
        loc = _lookup_location(ip)
        if loc:
            db.active_sessions.update_one(
                {"_id": doc_id}, {"$set": {"location": loc}}
            )
    except Exception:
        pass


def _backfill_stale_sessions(db, docs, current_hash):
    """Enrich sessions recorded before Client-Hint capture / trusted-IP geo
    existed, so the Active Sessions UI shows precise metadata for the user's
    EXISTING logins instead of generic fallbacks.

      * Client Hints: a row stored without Sec-CH-UA-Platform-Version can
        never be parsed as Windows 10 vs 11 on its own.  When the requesting
        browser presents hints AND its User-Agent exactly matches the row's
        stored User-Agent (same browser ⇒ same hint values), adopt the
        incoming hints and persist them onto the row.
      * Location: a row with no location is retried using its own public IP;
        for the CURRENT session row the request's resolved public IP is also
        eligible (same browser, same login ⇒ same approximate network).
        At most ONE synchronous lookup runs per request (bounded latency);
        further eligible rows are backfilled by daemon threads so the next
        visit already sees them.

    Mutates the doc dicts in place.  Never raises; failures leave rows as-is.
    """
    ua_in = request.headers.get("User-Agent", "")
    ch_platform = request.headers.get("Sec-CH-UA-Platform")
    ch_version = request.headers.get("Sec-CH-UA-Platform-Version")

    sync_lookups_left = 1
    current_ip = None

    for d in docs:
        # ── Client-Hint backfill ────────────────────────────────────
        if (
            ch_version
            and not (d.get("ch_platform_version") or "").strip()
            and ua_in
            and d.get("user_agent") == ua_in
        ):
            updates = {
                "ch_platform": ch_platform or "",
                "ch_platform_version": ch_version,
            }
            try:
                db.active_sessions.update_one(
                    {"_id": d["_id"]}, {"$set": updates}
                )
                d.update(updates)
            except Exception:
                pass

        # ── Location backfill ───────────────────────────────────────
        if _location_is_known(d.get("location")):
            continue
        candidates = []
        doc_ip = d.get("ip") or ""
        if doc_ip and not _is_private_ip(doc_ip):
            candidates.append(doc_ip)
        if d.get("session_token") == current_hash:
            if current_ip is None:
                current_ip = _client_ip()
            if current_ip and not _is_private_ip(current_ip):
                candidates.append(current_ip)
        if not candidates:
            continue
        ip = candidates[0]
        if sync_lookups_left > 0:
            sync_lookups_left -= 1
            loc = _lookup_location(ip)
            if loc:
                try:
                    db.active_sessions.update_one(
                        {"_id": d["_id"]}, {"$set": {"location": loc}}
                    )
                except Exception:
                    pass
                d["location"] = loc
        else:
            threading.Thread(
                target=_backfill_location_by_id,
                args=(db, d["_id"], ip),
                daemon=True,
            ).start()


@api_bp.route("/sessions/active")
def list_active_sessions():
    user_id = _check_auth()
    if not user_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    current_token = session.get("session_token")
    current_hash = _hash_session_token(current_token) if current_token else None
    docs = list(
        db.active_sessions.find({"user_id": ObjectId(user_id)}).sort("last_seen", -1)
    )

    _backfill_stale_sessions(db, docs, current_hash)

    sessions = []
    for d in docs:
        sessions.append(
            {
                "id": str(d["_id"]),
                "device": _parse_device(
                    d.get("user_agent", ""),
                    d.get("ch_platform"),
                    d.get("ch_platform_version"),
                ),
                "location": _sanitize_location(d.get("location")),
                # Lets the UI distinguish WHY a location is absent: a truly
                # local/private address ("Local network") vs a routable IP
                # the geo provider couldn't resolve ("Location unavailable",
                # e.g. ISP CGNAT like Jio's 100.64.x.x).
                "ip_private": _ip_is_local(d),
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

    current_hash = _hash_session_token(session.get("session_token", ""))
    is_self_revoke = doc.get("session_token") == current_hash
    if is_self_revoke:
        return jsonify({"error": "cannot_revoke_current"}), 400

    device = _parse_device(
        doc.get("user_agent", ""),
        doc.get("ch_platform"),
        doc.get("ch_platform_version"),
    )
    device_label = _device_label(device)

    db.active_sessions.delete_one({"_id": doc_id})

    org_id = db.users.find_one({"_id": ObjectId(user_id)}, {"org_id": 1})
    log_audit_event(
        db, (org_id or {}).get("org_id"), user_id, session.get("user_name") or "",
        ACTION_SESSION_REVOKE,
        target_type="session", target_id=session_doc_id,
        target_label=device_label,
        meta={"self_revoke": is_self_revoke},
    )

    return jsonify({"ok": True})


def _device_label(device) -> str:
    """Human-readable device label for the audit log, mirroring the frontend's
    renderer (e.g. "Laptop · Chrome on Windows")."""
    if not device:
        return "Unknown device"
    d_type = device.get("device_type")
    typ = {"Mobile": "Phone", "Tablet": "Tablet"}.get(d_type, "Laptop")
    parts = []
    if device.get("browser") and device["browser"] != "Unknown":
        parts.append(device["browser"])
    if device.get("os") and device["os"] != "Unknown":
        parts.append(device["os"])
    return typ + (" · " + " on ".join(parts) if parts else "")

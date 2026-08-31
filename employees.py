import logging
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, jsonify, request, session, make_response

from blind_index import blind_index
from employee_scoring import score_employee
from extensions import get_db, next_employee_id
from field_encryption import decrypt_fields, encrypt_fields
from login_flow import _hash_session_token

logger = logging.getLogger(__name__)

employees_bp = Blueprint("employees", __name__)

# ── Employee field length limits ───────────────────────────────────────
# Prevent uncontrolled MongoDB document growth via oversized user input.
# Limits are generous enough for real-world HR data while blocking abuse.
MAX_EMPLOYEE_NAME_LEN = 200        # characters
MAX_EMPLOYEE_EMAIL_LEN = 320       # RFC 5321 max mailbox length
MAX_EMPLOYEE_PHONE_LEN = 30        # characters
MAX_EMPLOYEE_DEPT_LEN = 100        # characters
MAX_EMPLOYEE_POSITION_LEN = 100    # characters
MAX_EMPLOYEE_ET_LEN = 50           # employment_type
MAX_EMPLOYEE_WM_LEN = 50           # work_mode
MAX_EMPLOYEE_DATE_LEN = 30         # joining_date (ISO string)
MAX_EMPLOYEE_STATUS_LEN = 30       # status enum
MAX_PHOTO_BYTES = 2 * 1024 * 1024  # 2 MB for base64 data-URL string


class TOTPRequired(Exception):
    """Raised by _require_auth() when the admin session needs TOTP verification."""
    pass


def _session_is_active(user_id, session_token):
    """True if a matching active_sessions record exists for this login. Also
    serves as a cheap heartbeat: refreshes last_seen at most every 5 minutes
    rather than on every request.
    """
    if not user_id or not session_token:
        return False
    try:
        ObjectId(user_id)
    except InvalidId:
        return False
    db = get_db()
    rec = db.active_sessions.find_one(
        {"user_id": ObjectId(user_id), "session_token": _hash_session_token(session_token)}
    )
    if not rec:
        return False
    now = datetime.now(timezone.utc)
    last_seen = rec.get("last_seen")
    # MongoClient isn't tz_aware, so datetimes read back from the DB are
    # naive while `now` is aware — normalize before comparing.
    if last_seen and last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    if not last_seen or (now - last_seen) > timedelta(minutes=5):
        db.active_sessions.update_one(
            {"_id": rec["_id"]}, {"$set": {"last_seen": now}}
        )
    return True


def _require_auth():
    user_id = session.get("user_id")
    org_id = session.get("org_id")
    if not user_id or not org_id:
        return None
    try:
        ObjectId(user_id)
        ObjectId(org_id)
    except InvalidId:
        session.clear()
        return None
    if not _session_is_active(user_id, session.get("session_token")):
        session.clear()
        return None
    if _totp_required():
        raise TOTPRequired()
    return org_id


def _totp_required():
    """Return True if the current admin session has TOTP enabled but the
    session has not been verified.  Non-admin users and users without TOTP
    are never blocked."""
    user_id = session.get("user_id")
    session_token = session.get("session_token")
    if not user_id or not session_token:
        return False
    try:
        uid = ObjectId(user_id)
    except Exception:
        return False
    db = get_db()
    user = db.users.find_one({"_id": uid}, {"role": 1, "totp_enabled": 1})
    if not user or user.get("role") != "admin":
        return False
    if user.get("totp_enabled") is not True:
        return False
    return session.get("totp_verified_session") != session_token


def _next_employee_id(db, org_id: str) -> str:
    return next_employee_id(db, org_id)


def _employee_to_json(emp) -> dict:
    pii = decrypt_fields(emp.get("encrypted"), emp.get("wrapped_dek", ""))
    signals = emp.get("signals") or {}
    ai = emp.get("ai_wellness")

    has_real_signals = any(
        v not in (0, None) for v in signals.values()
    ) if signals else False

    if ai:
        # Wellness score derived from the AI's reading of an actual HR
        # conversation transcript — this takes priority once it exists.
        wellness_score = ai.get("score")
        wellness_status = ai.get("status")
        attrition_risk_pct = ai.get("attrition_risk_pct")
        reasons = ai.get("risk_factors", [])
        wellness_source = "ai_transcript_analysis"
    elif has_real_signals:
        result = score_employee(
            overtime_hours_last_3w=signals.get("overtime_hours_last_3w", 0),
            absences_last_30d=signals.get("absences_last_30d", 0),
            performance_delta_pct=signals.get("performance_delta_pct", 0),
            missed_deadlines_last_30d=signals.get("missed_deadlines_last_30d", 0),
            engagement_survey_score=signals.get("engagement_survey_score"),
        )
        wellness_score = result.wellness_score
        wellness_status = result.status
        attrition_risk_pct = result.attrition_risk_pct
        reasons = result.reasons
        wellness_source = "hr_signals"
    else:
        # No transcript analyzed yet and no HR signals entered — don't
        # invent a score (previously this silently defaulted to 100).
        wellness_score = None
        wellness_status = "not_assessed"
        attrition_risk_pct = None
        reasons = []
        wellness_source = None

    return {
        "id": str(emp["_id"]),
        "employee_id": emp.get("employee_id"),
        "name": pii.get("name", ""),
        "email": pii.get("email", ""),
        "phone": pii.get("phone", ""),
        "department": emp.get("department", ""),
        "position": emp.get("position", ""),
        "employment_type": emp.get("employment_type", ""),
        "work_mode": emp.get("work_mode", ""),
        "joining_date": emp.get("joining_date", ""),
        "status": emp.get("status", "active"),
        "wellness_score": wellness_score,
        "wellness_status": wellness_status,
        "attrition_risk_pct": attrition_risk_pct,
        "burnout_index": (ai or {}).get("burnout_index"),
        "wellness_source": wellness_source,
        "reasons": reasons,
        "photo": emp.get("photo"),
        "created_at": emp.get("created_at").isoformat() if emp.get("created_at") else None,
        "updated_at": emp.get("updated_at").isoformat() if emp.get("updated_at") else None,
    }


@employees_bp.route("/employees", methods=["POST"])
def create_employee():
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()
    phone = (data.get("phone") or "").strip()
    department = (data.get("department") or "").strip()
    position = (data.get("position") or "").strip()
    employment_type = (data.get("employment_type") or "").strip()
    work_mode = (data.get("work_mode") or "").strip()
    joining_date = (data.get("joining_date") or "").strip()

    if not name:
        return jsonify({"error": "name_required"}), 400

    # ── Field length guards ─────────────────────────────────────────
    if len(name) > MAX_EMPLOYEE_NAME_LEN:
        return jsonify({"error": "name_too_long"}), 400
    if email and len(email) > MAX_EMPLOYEE_EMAIL_LEN:
        return jsonify({"error": "email_too_long"}), 400
    if phone and len(phone) > MAX_EMPLOYEE_PHONE_LEN:
        return jsonify({"error": "phone_too_long"}), 400
    if len(department) > MAX_EMPLOYEE_DEPT_LEN:
        return jsonify({"error": "department_too_long"}), 400
    if len(position) > MAX_EMPLOYEE_POSITION_LEN:
        return jsonify({"error": "position_too_long"}), 400
    if len(employment_type) > MAX_EMPLOYEE_ET_LEN:
        return jsonify({"error": "employment_type_too_long"}), 400
    if len(work_mode) > MAX_EMPLOYEE_WM_LEN:
        return jsonify({"error": "work_mode_too_long"}), 400
    if len(joining_date) > MAX_EMPLOYEE_DATE_LEN:
        return jsonify({"error": "joining_date_too_long"}), 400

    db = get_db()
    employee_id = _next_employee_id(db, org_id)

    encrypted_fields, wrapped_dek = encrypt_fields({
        "name": name,
        "email": email if email else None,
        "phone": phone if phone else None,
    })

    signals = {
        "overtime_hours_last_3w": float(data.get("overtime_hours_last_3w", 0)),
        "absences_last_30d": int(data.get("absences_last_30d", 0)),
        "performance_delta_pct": float(data.get("performance_delta_pct", 0)),
        "missed_deadlines_last_30d": int(data.get("missed_deadlines_last_30d", 0)),
        "engagement_survey_score": data.get("engagement_survey_score"),
    }
    if signals["engagement_survey_score"] is not None:
        signals["engagement_survey_score"] = int(signals["engagement_survey_score"])

    photo = (data.get("photo") or "").strip()
    if photo and not photo.startswith("data:image/"):
        photo = ""
    # ── Photo size guard (base64 data-URL string) ───────────────────
    if photo and len(photo) > MAX_PHOTO_BYTES:
        return jsonify({"error": "photo_too_large"}), 413

    emp_doc = {
        "org_id": ObjectId(org_id),
        "employee_id": employee_id,
        "department": department,
        "position": position,
        "employment_type": employment_type,
        "work_mode": work_mode,
        "joining_date": joining_date,
        "status": "active",
        "email_hash": blind_index(email) if email else None,
        "encrypted": encrypted_fields,
        "wrapped_dek": wrapped_dek,
        "signals": signals,
        "photo": photo or None,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    result = db.employees.insert_one(emp_doc)
    emp_doc["_id"] = result.inserted_id

    return jsonify(_employee_to_json(emp_doc)), 201


# ── Employee list pagination ───────────────────────────────────────────
# Prevents the list-employees endpoint from returning the entire org's
# employee roster (and running decryption + scoring over every row) in one
# response, which grows unbounded as the org scales.
DEFAULT_PAGE_LIMIT = 50    # employees per page
MAX_PAGE_LIMIT = 200       # hard upper bound, clamps any larger request


@employees_bp.route("/employees")
def list_employees():
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    query: dict = {"org_id": ObjectId(org_id)}

    search = (request.args.get("search") or "").strip().lower()
    dept = (request.args.get("department") or "").strip()
    emp_status = (request.args.get("status") or "").strip()

    # Pagination params: page (1-based) and limit.  limit is clamped to
    # [1, MAX_PAGE_LIMIT]; page is >= 1.
    try:
        page = max(1, int(request.args.get("page", 1) or 1))
    except ValueError:
        page = 1
    try:
        limit = int(request.args.get("limit", DEFAULT_PAGE_LIMIT) or DEFAULT_PAGE_LIMIT)
    except ValueError:
        limit = DEFAULT_PAGE_LIMIT
    limit = min(max(1, limit), MAX_PAGE_LIMIT)

    if dept:
        query["department"] = dept
    if emp_status:
        query["status"] = emp_status

    # Base total across the (pre-search) filters, for pagination metadata.
    base_total = db.employees.count_documents(query)

    cursor = db.employees.find(query).sort("created_at", -1).skip((page - 1) * limit).limit(limit)
    employees = list(cursor)

    # Client-side name/email search (fields are encrypted, can't query
    # directly).
    result = []
    # When a name/email search is supplied we cannot paginate inside MongoDB
    # (the fields are encrypted), so we over-fetch one page and filter.
    for emp in employees:
        item = _employee_to_json(emp)
        if search:
            if search not in item["name"].lower() and search not in item["email"].lower() and search not in item.get("department", "").lower():
                continue
        result.append(item)

    # For pagination metadata we reflect the effective (filtered) page size.
    page_size = len(result)
    offset = (page - 1) * limit
    has_more = base_total > (offset + page_size)

    return jsonify({
        "employees": result,
        "total": base_total,
        "page": page,
        "limit": limit,
        "has_more": has_more,
    })


@employees_bp.route("/employees/<emp_id>")
def get_employee(emp_id: str):
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    try:
        emp = db.employees.find_one({"_id": ObjectId(emp_id), "org_id": ObjectId(org_id)})
    except InvalidId:
        return jsonify({"error": "invalid_id"}), 400

    if not emp:
        return jsonify({"error": "not_found"}), 404

    return jsonify(_employee_to_json(emp))


@employees_bp.route("/employees/<emp_id>", methods=["PUT"])
def update_employee(emp_id: str):
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    try:
        emp = db.employees.find_one({"_id": ObjectId(emp_id), "org_id": ObjectId(org_id)})
    except InvalidId:
        return jsonify({"error": "invalid_id"}), 400

    if not emp:
        return jsonify({"error": "not_found"}), 404

    data = request.get_json(silent=True) or {}
    set_fields: dict = {}
    unset_fields: list = []

    # Plain fields — with length guards
    for field, max_len in (
        ("department", MAX_EMPLOYEE_DEPT_LEN),
        ("position", MAX_EMPLOYEE_POSITION_LEN),
        ("employment_type", MAX_EMPLOYEE_ET_LEN),
        ("work_mode", MAX_EMPLOYEE_WM_LEN),
        ("joining_date", MAX_EMPLOYEE_DATE_LEN),
        ("status", MAX_EMPLOYEE_STATUS_LEN),
    ):
        if field in data:
            val = (data[field] or "").strip()
            if len(val) > max_len:
                return jsonify({"error": f"{field}_too_long"}), 400
            set_fields[field] = val

    # Photo — validate data-URL prefix, or clear
    if "photo" in data:
        photo = (data["photo"] or "").strip()
        if photo and photo.startswith("data:image/"):
            if len(photo) > MAX_PHOTO_BYTES:
                return jsonify({"error": "photo_too_large"}), 413
            set_fields["photo"] = photo
        else:
            unset_fields.append("photo")

    # Signals
    signals = dict(emp.get("signals") or {})
    changed = False
    for field in ("overtime_hours_last_3w", "absences_last_30d", "performance_delta_pct", "missed_deadlines_last_30d"):
        if field in data:
            signals[field] = float(data[field])
            changed = True
    if "engagement_survey_score" in data:
        val = data["engagement_survey_score"]
        if val is None:
            signals.pop("engagement_survey_score", None)
        else:
            signals["engagement_survey_score"] = int(val)
        changed = True

    if changed:
        set_fields["signals"] = signals

    # PII fields — re-encrypt if provided
    pii_changes = {}
    for field, max_len in (
        ("name", MAX_EMPLOYEE_NAME_LEN),
        ("email", MAX_EMPLOYEE_EMAIL_LEN),
        ("phone", MAX_EMPLOYEE_PHONE_LEN),
    ):
        if field in data:
            val = (data[field] or "").strip()
            if val and len(val) > max_len:
                return jsonify({"error": f"{field}_too_long"}), 400
            if val:
                pii_changes[field] = val
            else:
                pii_changes[field] = None

    if pii_changes:
        old_encrypted = emp.get("encrypted") or {}
        merged = dict(old_encrypted)
        for key, val in pii_changes.items():
            if val is not None:
                merged[key] = val
            else:
                merged.pop(key, None)
        # Re-encrypt with a fresh DEK
        new_encrypted, new_wrapped_dek = encrypt_fields(merged)
        set_fields["encrypted"] = new_encrypted
        set_fields["wrapped_dek"] = new_wrapped_dek
        if pii_changes.get("email"):
            set_fields["email_hash"] = blind_index(pii_changes["email"])
        elif pii_changes.get("email") is None and "email_hash" in emp:
            unset_fields.append("email_hash")

    if not set_fields and not unset_fields:
        return jsonify({"error": "no_fields_to_update"}), 400

    set_fields["updated_at"] = datetime.now(timezone.utc)

    update: dict = {"$set": set_fields}
    if unset_fields:
        update["$unset"] = {f: "" for f in unset_fields}

    db.employees.update_one({"_id": ObjectId(emp_id)}, update)
    emp = db.employees.find_one({"_id": ObjectId(emp_id)})

    return jsonify(_employee_to_json(emp))


@employees_bp.route("/employees/<emp_id>", methods=["DELETE"])
def delete_employee(emp_id: str):
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    try:
        emp_oid = ObjectId(emp_id)
    except InvalidId:
        return jsonify({"error": "invalid_id"}), 400

    emp = db.employees.find_one({"_id": emp_oid, "org_id": ObjectId(org_id)})
    if not emp:
        return jsonify({"error": "not_found"}), 404

    try:
        db.sessions.delete_many({"employee_id": emp_oid, "org_id": ObjectId(org_id)})
        db.notifications.delete_many({"employee_id": emp_oid, "org_id": ObjectId(org_id)})
        db.employees.delete_one({"_id": emp_oid, "org_id": ObjectId(org_id)})
    except Exception:
        logger.exception("Employee deletion failed (employee=%s)", emp_id)
        return jsonify({"error": "deletion_failed"}), 500

    return jsonify({"ok": True})


@employees_bp.route("/employees/<emp_id>/export", methods=["GET"])
def export_employee(emp_id: str):
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    try:
        emp_oid = ObjectId(emp_id)
    except InvalidId:
        return jsonify({"error": "invalid_id"}), 400

    emp = db.employees.find_one({"_id": emp_oid, "org_id": ObjectId(org_id)})
    if not emp:
        return jsonify({"error": "not_found"}), 404

    pii = decrypt_fields(emp.get("encrypted"), emp.get("wrapped_dek", ""))
    signals = emp.get("signals") or {}
    ai = emp.get("ai_wellness") or {}

    export_data = {
        "employee_id": emp.get("employee_id"),
        "name": pii.get("name", ""),
        "email": pii.get("email", ""),
        "phone": pii.get("phone", ""),
        "department": emp.get("department", ""),
        "position": emp.get("position", ""),
        "employment_type": emp.get("employment_type", ""),
        "work_mode": emp.get("work_mode", ""),
        "joining_date": emp.get("joining_date", ""),
        "status": emp.get("status", "active"),
        "signals": signals,
        "ai_wellness": {
            "score": ai.get("score"),
            "status": ai.get("status"),
            "attrition_risk_pct": ai.get("attrition_risk_pct"),
            "burnout_index": ai.get("burnout_index"),
            "risk_factors": ai.get("risk_factors", []),
            "source_session_id": ai.get("source_session_id"),
            "updated_at": ai.get("updated_at").isoformat() if ai.get("updated_at") else None,
        } if ai else None,
        "photo": emp.get("photo"),
        "created_at": emp.get("created_at").isoformat() if emp.get("created_at") else None,
        "updated_at": emp.get("updated_at").isoformat() if emp.get("updated_at") else None,
    }

    resp = make_response(jsonify(export_data))
    safe_name = (emp.get("employee_id") or "employee").replace(" ", "_")
    resp.headers["Content-Disposition"] = f'attachment; filename="{safe_name}_export.json"'
    return resp

import csv
import io
import logging
import secrets
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, jsonify, request, session, make_response, url_for

from audit_log import (
    ACTION_EMPLOYEE_BULK_EXPORT,
    ACTION_EMPLOYEE_BULK_IMPORT,
    ACTION_EMPLOYEE_CREATE,
    ACTION_EMPLOYEE_DELETE,
    ACTION_EMPLOYEE_EXPORT,
    ACTION_EMPLOYEE_UPDATE,
    ACTION_MANAGER_INVITE_ACCEPTED,
    ACTION_MANAGER_INVITE_SENT,
    ACTION_MANAGER_INVITE_SUPERSEDED,
    log_audit_event,
)
from blind_index import blind_index
from config import Config
from email_service import send_manager_invite_email
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

# ── CSV bulk import / export limits ─────────────────────────────────────
# Import is capped by row count and by file size.  The byte cap stays below
# the app-wide Config.MAX_CONTENT_LENGTH (50 MB) so a huge upload is rejected
# here with a clear message rather than silently swallowed by the global cap.
MAX_IMPORT_ROWS = 500              # max data rows per import
MAX_IMPORT_BYTES = int(Config.MAX_CONTENT_LENGTH)  # reuse app's global upload cap

# Ordered CSV columns (also the downloadable template header row).
CSV_COLUMNS = [
    "name",
    "email",
    "phone",
    "department",
    "position",
    "employment_type",
    "work_mode",
    "joining_date",
]


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
        "reports_to": str(emp["reports_to"]) if emp.get("reports_to") else None,
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

    # reports_to — optional ObjectId referencing a manager employee in the same org
    reports_to = data.get("reports_to")
    if reports_to is not None and reports_to != "" and reports_to != "null":
        try:
            reports_to_oid = ObjectId(reports_to)
        except (InvalidId, TypeError):
            return jsonify({"error": "invalid_reports_to"}), 400
        if not db.employees.find_one({"_id": reports_to_oid, "org_id": ObjectId(org_id)}):
            return jsonify({"error": "reports_to_not_found"}), 400
        reports_to = reports_to_oid
    else:
        reports_to = None

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
        "reports_to": reports_to,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    result = db.employees.insert_one(emp_doc)
    emp_doc["_id"] = result.inserted_id

    log_audit_event(
        db, org_id, session.get("user_id"), session.get("user_name") or "",
        ACTION_EMPLOYEE_CREATE,
        target_type="employee", target_id=str(emp_doc["_id"]),
        target_label=employee_id or name,
        meta={"employee_id": employee_id, "department": department},
    )

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

    # Manager-role scoping: restrict to the session user's direct reports.
    # Admin sessions are unaffected (empty extra filter).  Fail-closed for
    # any manager whose linked_employee_id is missing/malformed.
    query.update(_employee_scope_filter(db, org_id))

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


# NOTE: these bulk routes are registered deliberately BEFORE the
# /employees/<emp_id> routes below.  Flask matches route patterns in
# registration order, and "<emp_id>" (default string converter) would swallow
# the literal path segments "import" / "export-csv" / "csv-template" if they
# were registered after it.


def _validate_import_row(name, email, phone, department, position,
                         employment_type, work_mode, joining_date):
    """Run the same field length/required checks as create_employee() for one
    CSV row, reusing the shared MAX_* constants.  Returns None when valid, or
    an error-code string matching create_employee()'s JSON responses (e.g.
    "name_required", "email_too_long")."""
    if not name:
        return "name_required"
    if len(name) > MAX_EMPLOYEE_NAME_LEN:
        return "name_too_long"
    if email and len(email) > MAX_EMPLOYEE_EMAIL_LEN:
        return "email_too_long"
    if phone and len(phone) > MAX_EMPLOYEE_PHONE_LEN:
        return "phone_too_long"
    if len(department) > MAX_EMPLOYEE_DEPT_LEN:
        return "department_too_long"
    if len(position) > MAX_EMPLOYEE_POSITION_LEN:
        return "position_too_long"
    if len(employment_type) > MAX_EMPLOYEE_ET_LEN:
        return "employment_type_too_long"
    if len(work_mode) > MAX_EMPLOYEE_WM_LEN:
        return "work_mode_too_long"
    if len(joining_date) > MAX_EMPLOYEE_DATE_LEN:
        return "joining_date_too_long"
    return None


@employees_bp.route("/employees/import", methods=["POST"])
def import_employees_csv():
    """Bulk-create employees from an uploaded CSV (multipart/form-data).

    Columns match CSV_COLUMNS / create_employee()'s accepted fields.  Rows are
    validated with the same MAX_* guards, processed in a single pass, and bad
    rows are collected instead of aborting the whole import (partial success is
    normal and expected).  Logs one audit entry for the whole import — not one
    per row, which would flood the audit log.
    """
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "file_required"}), 400

    raw = file.read()
    if len(raw) == 0:
        return jsonify({"error": "empty_file"}), 400
    # Reuse the app's global upload cap (Config.MAX_CONTENT_LENGTH via
    # MAX_IMPORT_BYTES) so a huge CSV is rejected here with a clear message.
    if len(raw) > MAX_IMPORT_BYTES:
        return jsonify({"error": "file_too_large"}), 413

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return jsonify({"error": "invalid_encoding"}), 400

    db = get_db()
    created = 0
    failed = []

    try:
        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames is None:
            return jsonify({"error": "invalid_csv"}), 400

        for row_no, row in enumerate(reader, start=2):  # header is row 1
            data_row = row_no - 1
            if data_row > MAX_IMPORT_ROWS:
                break

            name = (row.get("name") or "").strip()
            email = (row.get("email") or "").strip()
            phone = (row.get("phone") or "").strip()
            department = (row.get("department") or "").strip()
            position = (row.get("position") or "").strip()
            employment_type = (row.get("employment_type") or "").strip()
            work_mode = (row.get("work_mode") or "").strip()
            joining_date = (row.get("joining_date") or "").strip()

            err = _validate_import_row(
                name, email, phone, department, position,
                employment_type, work_mode, joining_date,
            )
            if err:
                failed.append({"row": row_no, "error": err})
                continue

            employee_id = _next_employee_id(db, org_id)
            encrypted_fields, wrapped_dek = encrypt_fields({
                "name": name,
                "email": email if email else None,
                "phone": phone if phone else None,
            })

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
                "signals": {
                    "overtime_hours_last_3w": 0.0,
                    "absences_last_30d": 0,
                    "performance_delta_pct": 0.0,
                    "missed_deadlines_last_30d": 0,
                    "engagement_survey_score": None,
                },
                "photo": None,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
            db.employees.insert_one(emp_doc)
            created += 1
    except csv.Error:
        return jsonify({"error": "invalid_csv"}), 400

    log_audit_event(
        db, org_id, session.get("user_id"), session.get("user_name") or "",
        ACTION_EMPLOYEE_BULK_IMPORT,
        target_type="employee",
        meta={"created": created, "failed": len(failed)},
    )

    return jsonify({"created": created, "failed": failed})


@employees_bp.route("/employees/export-csv")
def export_employees_csv():
    """Download the org's full employee list as a CSV.

    Org-scoped (same org_id filter as list_employees()) and decrypts PII the
    same way _employee_to_json() does.  A bulk PII export — logged once, with
    the same seriousness as the single-employee export.
    """
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    query: dict = {"org_id": ObjectId(org_id)}
    query.update(_employee_scope_filter(db, org_id))
    cursor = db.employees.find(query).sort("created_at", -1)
    employees = list(cursor)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    for emp in employees:
        pii = decrypt_fields(emp.get("encrypted"), emp.get("wrapped_dek", ""))
        writer.writerow({
            "name": pii.get("name", ""),
            "email": pii.get("email", ""),
            "phone": pii.get("phone", ""),
            "department": emp.get("department", ""),
            "position": emp.get("position", ""),
            "employment_type": emp.get("employment_type", ""),
            "work_mode": emp.get("work_mode", ""),
            "joining_date": emp.get("joining_date", ""),
        })

    log_audit_event(
        db, org_id, session.get("user_id"), session.get("user_name") or "",
        ACTION_EMPLOYEE_BULK_EXPORT,
        target_type="employee",
        meta={"count": len(employees)},
    )

    resp = make_response(buf.getvalue())
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    resp.headers["Content-Disposition"] = 'attachment; filename="employees_export.csv"'
    return resp


@employees_bp.route("/employees/csv-template")
def employee_csv_template():
    """Download a CSV template (just the header row) so users know the exact
    expected column order for the bulk import."""
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    buf.seek(0)

    resp = make_response(buf.getvalue())
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    resp.headers["Content-Disposition"] = 'attachment; filename="employees_import_template.csv"'
    return resp


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

    if not _employee_accessible(db, org_id, emp):
        return jsonify({"error": "forbidden"}), 403

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

    if not _employee_accessible(db, org_id, emp):
        return jsonify({"error": "forbidden"}), 403

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

    # reports_to — nullable ObjectId referencing a manager employee in the same org
    if "reports_to" in data:
        reports_to = data.get("reports_to")
        if reports_to is not None and reports_to != "" and reports_to != "null":
            try:
                reports_to_oid = ObjectId(reports_to)
            except (InvalidId, TypeError):
                return jsonify({"error": "invalid_reports_to"}), 400
            if not db.employees.find_one({"_id": reports_to_oid, "org_id": ObjectId(org_id)}):
                return jsonify({"error": "reports_to_not_found"}), 400
            set_fields["reports_to"] = reports_to_oid
        else:
            set_fields["reports_to"] = None

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

    log_audit_event(
        db, org_id, session.get("user_id"), session.get("user_name") or "",
        ACTION_EMPLOYEE_UPDATE,
        target_type="employee", target_id=emp_id,
        target_label=emp.get("employee_id") or emp_id,
        meta={"employee_id": emp.get("employee_id")},
    )

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

    if not _employee_accessible(db, org_id, emp):
        return jsonify({"error": "forbidden"}), 403

    try:
        db.sessions.delete_many({"employee_id": emp_oid, "org_id": ObjectId(org_id)})
        db.notifications.delete_many({"employee_id": emp_oid, "org_id": ObjectId(org_id)})
        db.employees.delete_one({"_id": emp_oid, "org_id": ObjectId(org_id)})
    except Exception:
        logger.exception("Employee deletion failed (employee=%s)", emp_id)
        return jsonify({"error": "deletion_failed"}), 500

    log_audit_event(
        db, org_id, session.get("user_id"), session.get("user_name") or "",
        ACTION_EMPLOYEE_DELETE,
        target_type="employee", target_id=emp_id,
        target_label=emp.get("employee_id") or emp_id,
        meta={"employee_id": emp.get("employee_id"), "delete_related": True},
    )

    return jsonify({"ok": True})


def _require_admin():
    """Admin-only session check for manager-invite routes.

    Returns the authenticated admin's org_id (str) on success, None when
    unauthenticated / not an admin.  Reuses the same active-session + TOTP
    validation as _require_auth() but also enforces role == "admin".
    """
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
    db = get_db()
    user = db.users.find_one({"_id": ObjectId(user_id)}, {"role": 1})
    if not user or user.get("role") != "admin":
        return None
    return org_id


# ── Manager-role data scoping ─────────────────────────────────────────
# Managers may only ever see the employees who report to them.  Every
# employee-data query for a manager session is scoped to
# `{ "reports_to": <linked_employee_id> }`.  These helpers are fail-closed:
# a manager whose linked_employee_id is missing/malformed gets a filter that
# matches nothing, never the org-wide roster.  Admin sessions always return
# an empty extra filter (full org, unaffected).

_NEVER_MATCH = {"_id": None}  # matches no employee documents


def _employee_scope_filter(db, org_id: str):
    """Return an extra MongoDB filter dict to AND into employee queries.

    ``{}``            → admin (or otherwise unscoped): full org access.
    {"reports_to": oid} → manager: reports_to scoped to their team.
    _NEVER_MATCH      → fail-closed: no data for a malformed/unknown scope.
    """
    user_id = session.get("user_id")
    try:
        uid = ObjectId(user_id)
    except (InvalidId, TypeError):
        return _NEVER_MATCH

    user = db.users.find_one(
        {"_id": uid},
        {"role": 1, "org_id": 1, "linked_employee_id": 1},
    )
    if not user or str(user.get("org_id")) != org_id:
        return _NEVER_MATCH

    role = user.get("role")
    if role == "admin":
        return {}
    if role == "manager":
        linked = user.get("linked_employee_id")
        if isinstance(linked, ObjectId):
            return {"reports_to": linked}
        if linked:
            try:
                return {"reports_to": ObjectId(linked)}
            except (InvalidId, TypeError):
                return _NEVER_MATCH
        return _NEVER_MATCH
    # Unknown role — never fail open to org-wide data.
    return _NEVER_MATCH


def _employee_accessible(db, org_id: str, emp) -> bool:
    """Whether the current session may access a single employee document.

    Admin sees everything; a manager may only see employees reporting to
    them (their own document or a direct report).  Always fails closed on
    missing/malformed scope.
    """
    scope = _employee_scope_filter(db, org_id)
    if scope == _NEVER_MATCH:
        return False
    if not scope:
        return True  # admin
    linked = scope.get("reports_to")
    return emp.get("reports_to") == linked or emp.get("_id") == linked


# ── Reporting-manager helpers ─────────────────────────────────────────
# Managers are referenced by their employee ObjectId (reports_to), never by
# plaintext name, consistent with how org_id is handled elsewhere.

@employees_bp.route("/employees/<emp_id>/manager-status")
def manager_status(emp_id: str):
    """Return whether the employee already has VooVr login access as a
    manager/admin, plus the current invite state, so the UI can offer or
    hold an invite appropriately.

    Response:
      {"has_access": bool, "role": "manager"|"admin"|null,
       "invite_status": "pending"|null}

    Only a still-``pending`` invite is reported as "Invite pending".  A
    ``superseded`` (or expired/accepted) invite reads the same as no invite
    having been sent — the admin can freely click "Send Invite" again.
    """
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

    user = db.users.find_one(
        {"linked_employee_id": emp_oid},
        {"role": 1},
    )

    pending_invite = db.invites.find_one(
        {
            "org_id": ObjectId(org_id),
            "linked_employee_id": emp_oid,
            "status": "pending",
        },
        {"_id": 1},
    )

    return jsonify({
        "has_access": bool(user),
        "role": user.get("role") if user else None,
        "invite_status": "pending" if pending_invite else None,
    })


@employees_bp.route("/employees/<emp_id>/invite-manager", methods=["POST"])
def invite_manager(emp_id: str):
    """Send a manager-invite email for the given employee (admin-only).

    Looks up the employee's stored (decrypted) email, creates an ``invites``
    document with a 7-day expiry token, and emails the invite link via Brevo.
    Never stores the manager's plaintext name anywhere new — always references
    the employee by its ObjectId and the invite by token/email_hash.
    """
    org_id = _require_admin()
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
    email = pii.get("email")
    if not email:
        return jsonify({"error": "no_email"}), 400

    org = db.organizations.find_one({"_id": ObjectId(org_id)}, {"name": 1})
    org_name = (org or {}).get("name") or "your organization"

    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)

    # Void any still-pending invites for this employee before issuing a new
    # one. This guarantees at most one valid `pending` invite per employee at
    # any time, so the most recently sent email link is always the one that
    # works — an older, still-unused email link (if clicked) fails with a
    # clear `superseded` reason instead of a confusing already_used/
    # mismatched result on what looks like the first attempt.
    superseded = db.invites.update_many(
        {
            "org_id": ObjectId(org_id),
            "linked_employee_id": emp_oid,
            "status": "pending",
        },
        {"$set": {"status": "superseded", "superseded_at": now}},
    )
    if superseded.modified_count > 0:
        log_audit_event(
            db, org_id, session.get("user_id"), session.get("user_name") or "",
            ACTION_MANAGER_INVITE_SUPERSEDED,
            target_type="employee", target_id=emp_id,
            target_label=emp.get("employee_id") or emp_id,
            meta={
                "employee_id": emp.get("employee_id"),
                "superseded_count": superseded.modified_count,
            },
        )

    invite_doc = {
        "org_id": ObjectId(org_id),
        "email": email,
        "email_hash": blind_index(email),
        "role": "manager",
        "linked_employee_id": emp_oid,
        "token": token,
        "created_by": session.get("user_id"),
        "status": "pending",
        "expires_at": now + timedelta(days=7),
        "created_at": now,
    }
    db.invites.insert_one(invite_doc)

    invite_link = url_for("auth.invite_accept", token=token, _external=True)
    sent = send_manager_invite_email(email, org_name, invite_link)

    log_audit_event(
        db, org_id, session.get("user_id"), session.get("user_name") or "",
        ACTION_MANAGER_INVITE_SENT,
        target_type="employee", target_id=emp_id,
        target_label=emp.get("employee_id") or emp_id,
        meta={"employee_id": emp.get("employee_id"), "email_sent": sent},
    )

    if not sent:
        return jsonify({"error": "email_send_failed"}), 502

    return jsonify({"ok": True, "email": email})


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

    if not _employee_accessible(db, org_id, emp):
        return jsonify({"error": "forbidden"}), 403

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

    log_audit_event(
        db, org_id, session.get("user_id"), session.get("user_name") or "",
        ACTION_EMPLOYEE_EXPORT,
        target_type="employee", target_id=emp_id,
        target_label=emp.get("employee_id") or emp_id,
        meta={"employee_id": emp.get("employee_id")},
    )

    return resp


@employees_bp.route("/manager-roles/overview")
def manager_roles_overview():
    """Admin-only roster for the "Manager Roles" settings section.

    Returns every employee with the info needed to bulk-invite managers:
      id             — employee _id (ObjectId as string)
      name / email   — decrypted PII (same pattern as _employee_to_json)
      position       — display position
      reports_count  — number of employees whose reports_to == this _id
      has_email      — whether a stored (invitable) email exists
      access_status  — "admin" | "manager" | "invite_pending" | "no_account"
      manager_since  — created_at ISO date of the linked users doc (manager only)

    Admin-only: sending invites must stay with role == "admin".
    """
    org_id = _require_admin()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    org_oid = ObjectId(org_id)

    employees = list(db.employees.find({"org_id": org_oid}))

    # Per-employee direct-report counts (reports_to -> count).
    report_counts = {
        (row["_id"]): row["cnt"]
        for row in db.employees.aggregate([
            {"$match": {"org_id": org_oid, "reports_to": {"$ne": None}}},
            {"$group": {"_id": "$reports_to", "cnt": {"$sum": 1}}},
        ])
    }

    # users joined by linked_employee_id (manager/admin accounts), one per employee.
    users_by_linked = {}
    for u in db.users.find(
        {"org_id": org_oid, "linked_employee_id": {"$ne": None}},
        {"role": 1, "linked_employee_id": 1, "created_at": 1},
    ):
        users_by_linked[u.get("linked_employee_id")] = u

    # pending invites joined by linked_employee_id.
    pending_linked = set()
    for inv in db.invites.find(
        {"org_id": org_oid, "status": "pending", "linked_employee_id": {"$ne": None}},
        {"linked_employee_id": 1},
    ):
        pending_linked.add(inv.get("linked_employee_id"))

    rows = []
    for emp in employees:
        pii = decrypt_fields(emp.get("encrypted"), emp.get("wrapped_dek", ""))
        email = (pii.get("email") or "").strip()
        eid = emp["_id"]

        user = users_by_linked.get(eid)
        access_status = "no_account"
        manager_since = None
        if user:
            if user.get("role") == "admin":
                access_status = "admin"
            elif user.get("role") == "manager":
                access_status = "manager"
                created = user.get("created_at")
                if created:
                    try:
                        manager_since = created.strftime("%Y-%m-%d")
                    except Exception:
                        manager_since = None
            else:
                access_status = user.get("role") or "no_account"
        elif eid in pending_linked:
            access_status = "invite_pending"

        rows.append({
            "id": str(eid),
            "employee_id": emp.get("employee_id"),
            "name": pii.get("name", ""),
            "email": email,
            "has_email": bool(email),
            "position": emp.get("position", ""),
            "reports_count": report_counts.get(eid, 0),
            "access_status": access_status,
            "manager_since": manager_since,
        })

    rows.sort(key=lambda r: (r["name"] or "").lower())

    return jsonify({"employees": rows})

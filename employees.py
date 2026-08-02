from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, jsonify, request, session

from blind_index import blind_index
from employee_scoring import score_employee
from extensions import get_db
from field_encryption import decrypt_fields, encrypt_fields

employees_bp = Blueprint("employees", __name__)


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
    return org_id


def _next_employee_id(db, org_id: str) -> str:
    last = db.employees.find_one(
        {"org_id": ObjectId(org_id)},
        sort=[("employee_id", -1)],
    )
    if last and last.get("employee_id"):
        num = int(last["employee_id"].replace("EMP", "")) + 1
    else:
        num = 1
    return f"EMP{num:03d}"


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
        "joining_date": emp.get("joining_date", ""),
        "status": emp.get("status", "active"),
        "wellness_score": wellness_score,
        "wellness_status": wellness_status,
        "attrition_risk_pct": attrition_risk_pct,
        "burnout_index": (ai or {}).get("burnout_index"),
        "wellness_source": wellness_source,
        "reasons": reasons,
        "signals": signals,
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
    joining_date = (data.get("joining_date") or "").strip()

    if not name:
        return jsonify({"error": "name_required"}), 400

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

    emp_doc = {
        "org_id": ObjectId(org_id),
        "employee_id": employee_id,
        "department": department,
        "position": position,
        "joining_date": joining_date,
        "status": "active",
        "email_hash": blind_index(email) if email else None,
        "encrypted": encrypted_fields,
        "wrapped_dek": wrapped_dek,
        "signals": signals,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    result = db.employees.insert_one(emp_doc)
    emp_doc["_id"] = result.inserted_id

    return jsonify(_employee_to_json(emp_doc)), 201


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

    if dept:
        query["department"] = dept
    if emp_status:
        query["status"] = emp_status

    cursor = db.employees.find(query).sort("created_at", -1)
    employees = list(cursor)

    # Client-side name/email search (fields are encrypted, can't query directly)
    result = []
    for emp in employees:
        item = _employee_to_json(emp)
        if search:
            if search not in item["name"].lower() and search not in item["email"].lower() and search not in item.get("department", "").lower():
                continue
        result.append(item)

    return jsonify({
        "employees": result,
        "total": len(result),
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

    # Plain fields
    for field in ("department", "position", "joining_date", "status"):
        if field in data:
            set_fields[field] = (data[field] or "").strip()

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
    for field in ("name", "email", "phone"):
        if field in data:
            val = (data[field] or "").strip()
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
        result = db.employees.delete_one({"_id": ObjectId(emp_id), "org_id": ObjectId(org_id)})
    except InvalidId:
        return jsonify({"error": "invalid_id"}), 400

    if result.deleted_count == 0:
        return jsonify({"error": "not_found"}), 404

    return jsonify({"ok": True})

import logging
from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, jsonify, request

from employees import _require_auth
from extensions import get_db

logger = logging.getLogger(__name__)

conversation_memory_bp = Blueprint("conversation_memory", __name__)

MEMORY_TYPES = {"OPENER", "QUESTION", "COMMITMENT", "FOLLOW_UP", "NOTE"}

# Stored statuses are the *base* factual states.  OVERDUE is derived at
# read time for PENDING commitments/follow-ups whose due_at has passed,
# so we never silently mutate PENDING back and forth as time flows.
BASE_STATUSES = {
    "OPENER": {"SAVED", "USED"},
    "QUESTION": {"SAVED", "USED"},
    "COMMITMENT": {"PENDING", "COMPLETED"},
    "FOLLOW_UP": {"PENDING", "COMPLETED"},
    "NOTE": {"SAVED"},
}

MAX_MEMORY_CONTENT_LEN = 4000


def _effective_status(m, now):
    status = m.get("status", "SAVED")
    due_at = m.get("due_at")
    if (
        m.get("type") in ("COMMITMENT", "FOLLOW_UP")
        and status == "PENDING"
        and due_at is not None
        and due_at < now
    ):
        return "OVERDUE"
    return status


def _memory_to_json(m):
    now = datetime.now(timezone.utc)
    due_at = m.get("due_at")
    if due_at is not None and due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=timezone.utc)
    return {
        "id": str(m["_id"]),
        "employee_id": str(m["employee_id"]),
        "session_id": str(m["session_id"]) if m.get("session_id") else None,
        "type": m.get("type"),
        "content": m.get("content", ""),
        "status": _effective_status(m, now),
        "due_at": due_at.isoformat() if due_at else None,
        "used_at": m.get("used_at").isoformat() if m.get("used_at") else None,
        "completed_at": m.get("completed_at").isoformat() if m.get("completed_at") else None,
        "created_at": m["created_at"].isoformat() if m.get("created_at") else None,
        "updated_at": m["updated_at"].isoformat() if m.get("updated_at") else None,
    }


@conversation_memory_bp.route("/conversation-memory", methods=["POST"])
def create_memory():
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    data = request.get_json(silent=True) or {}
    employee_id = data.get("employee_id")
    mtype = (data.get("type") or "").strip().upper()
    content = (data.get("content") or "").strip()
    session_id = data.get("session_id")
    due_at_raw = data.get("due_at")

    if not employee_id:
        return jsonify({"error": "employee_id_required"}), 400
    if mtype not in MEMORY_TYPES:
        return jsonify({"error": "invalid_type"}), 400
    if not content:
        return jsonify({"error": "content_required"}), 400
    if len(content) > MAX_MEMORY_CONTENT_LEN:
        return jsonify({"error": "content_too_long"}), 400

    try:
        emp_oid = ObjectId(employee_id)
    except InvalidId:
        return jsonify({"error": "invalid_employee_id"}), 400

    session_oid = None
    if session_id:
        try:
            session_oid = ObjectId(session_id)
        except InvalidId:
            return jsonify({"error": "invalid_session_id"}), 400

    db = get_db()
    emp = db.employees.find_one({"_id": emp_oid, "org_id": ObjectId(org_id)})
    if not emp:
        return jsonify({"error": "employee_not_found"}), 404

    if session_oid is not None:
        sess = db.sessions.find_one(
            {"_id": session_oid, "org_id": ObjectId(org_id), "employee_id": emp_oid}
        )
        if not sess:
            return jsonify({"error": "session_not_found"}), 404

    due_at = None
    if due_at_raw:
        try:
            due_at = datetime.fromisoformat(due_at_raw)
            if due_at.tzinfo is None:
                due_at = due_at.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return jsonify({"error": "invalid_due_at"}), 400

    base_status = "SAVED"
    if mtype in ("COMMITMENT", "FOLLOW_UP"):
        # A newly captured commitment/follow-up is pending until explicitly
        # completed.  Nothing is auto-completed — only explicit action changes it.
        base_status = "PENDING"

    now = datetime.now(timezone.utc)
    doc = {
        "org_id": ObjectId(org_id),
        "employee_id": emp_oid,
        "session_id": session_oid,
        "type": mtype,
        "content": content,
        "status": base_status,
        "due_at": due_at,
        "used_at": None,
        "completed_at": None,
        "created_at": now,
        "updated_at": now,
    }
    result = db.conversation_memory.insert_one(doc)
    doc["_id"] = result.inserted_id
    return jsonify(_memory_to_json(doc)), 201


@conversation_memory_bp.route("/conversation-memory")
def list_memory():
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    query = {"org_id": ObjectId(org_id)}

    emp_id = (request.args.get("employee_id") or "").strip()
    session_id = (request.args.get("session_id") or "").strip()
    mtype = (request.args.get("type") or "").strip().upper()
    status = (request.args.get("status") or "").strip().upper()

    if emp_id:
        try:
            query["employee_id"] = ObjectId(emp_id)
        except InvalidId:
            return jsonify({"error": "invalid_employee_id"}), 400
    if session_id:
        try:
            query["session_id"] = ObjectId(session_id)
        except InvalidId:
            return jsonify({"error": "invalid_session_id"}), 400
    if mtype:
        query["type"] = mtype
    if status:
        query["status"] = status

    items = list(db.conversation_memory.find(query).sort("created_at", 1))
    result = [_memory_to_json(m) for m in items]

    if status == "OVERDUE":
        now = datetime.now(timezone.utc)
        result = [m for m in result if m.get("status") == "OVERDUE"]

    return jsonify({
        "items": result,
        "total": len(result),
    })


@conversation_memory_bp.route("/conversation-memory/<memory_id>", methods=["PATCH"])
def update_memory(memory_id: str):
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    try:
        m = db.conversation_memory.find_one({"_id": ObjectId(memory_id), "org_id": ObjectId(org_id)})
    except InvalidId:
        return jsonify({"error": "invalid_id"}), 400
    if not m:
        return jsonify({"error": "not_found"}), 404

    data = request.get_json(silent=True) or {}
    set_fields: dict = {}
    mtype = m.get("type")

    if "content" in data:
        content = (data["content"] or "").strip()
        if not content:
            return jsonify({"error": "content_required"}), 400
        if len(content) > MAX_MEMORY_CONTENT_LEN:
            return jsonify({"error": "content_too_long"}), 400
        set_fields["content"] = content

    if "due_at" in data:
        due_at_raw = data.get("due_at")
        if due_at_raw:
            try:
                due_at = datetime.fromisoformat(due_at_raw)
                if due_at.tzinfo is None:
                    due_at = due_at.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                return jsonify({"error": "invalid_due_at"}), 400
            set_fields["due_at"] = due_at
        else:
            set_fields["due_at"] = None

    if "status" in data:
        status = (data["status"] or "").strip().upper()
        allowed = BASE_STATUSES.get(mtype, set())
        if status not in ("USED", "COMPLETED") and status not in allowed:
            return jsonify({"error": "invalid_status"}), 400
        if status == "COMPLETED" and mtype not in ("COMMITMENT", "FOLLOW_UP"):
            return jsonify({"error": "invalid_status"}), 400
        if status == "USED" and mtype not in ("OPENER", "QUESTION"):
            return jsonify({"error": "invalid_status"}), 400
        set_fields["status"] = status
        now = datetime.now(timezone.utc)
        if status == "USED":
            set_fields["used_at"] = now
        if status == "COMPLETED":
            set_fields["completed_at"] = now

    if not set_fields:
        return jsonify({"error": "no_fields_to_update"}), 400

    set_fields["updated_at"] = datetime.now(timezone.utc)
    db.conversation_memory.update_one({"_id": ObjectId(memory_id)}, {"$set": set_fields})
    m = db.conversation_memory.find_one({"_id": ObjectId(memory_id)})
    return jsonify(_memory_to_json(m))


@conversation_memory_bp.route("/conversation-memory/<memory_id>", methods=["DELETE"])
def delete_memory(memory_id: str):
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    try:
        oid = ObjectId(memory_id)
    except InvalidId:
        return jsonify({"error": "invalid_id"}), 400

    result = db.conversation_memory.delete_one({"_id": oid, "org_id": ObjectId(org_id)})
    if not result.deleted_count:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"ok": True})

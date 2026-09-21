import logging
from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, jsonify, request, session

from employees import _require_auth
from employees import _NEVER_MATCH, _employee_scope_filter, _employee_accessible
from audit_log import (
    ACTION_MEMORY_CREATE,
    ACTION_MEMORY_UPDATE,
    ACTION_MEMORY_DELETE,
    ACTION_MEMORY_USE,
    log_audit_event,
)
from extensions import get_db

logger = logging.getLogger(__name__)

conversation_memory_bp = Blueprint("conversation_memory", __name__)

MEMORY_TYPES = {"OPENER", "QUESTION", "COMMITMENT", "FOLLOW_UP", "NOTE"}

# Stored statuses are the *base* factual states.  OVERDUE is derived at
# read time for PENDING/IN_PROGRESS commitments/follow-ups whose due_at has
# passed, so we never silently mutate PENDING back and forth as time flows.
BASE_STATUSES = {
    "OPENER": {"SAVED", "USED"},
    "QUESTION": {"SAVED", "USED"},
    "COMMITMENT": {"PENDING", "IN_PROGRESS", "COMPLETED", "CANCELLED"},
    "FOLLOW_UP": {"PENDING", "IN_PROGRESS", "COMPLETED", "CANCELLED"},
    "NOTE": {"SAVED"},
}

# Provenance: whether a record is an unverified AI suggestion (never written
# by the current pipeline — reserved for future producers) or an HR-confirmed
# fact.  Creation is the confirmation point: HR writes → "confirmed".
CONFIRMATION_STATUSES = {"suggested", "confirmed"}

# Follow-up / commitment priority levels (optional, additive).
PRIORITY_LEVELS = {"low", "medium", "high"}

MAX_MEMORY_CONTENT_LEN = 4000
MAX_METADATA_KEYS = 60


def _effective_status(m, now):
    status = m.get("status", "SAVED")
    due_at = m.get("due_at")
    if (
        m.get("type") in ("COMMITMENT", "FOLLOW_UP")
        and status in ("PENDING", "IN_PROGRESS")
        and due_at is not None
        and (due_at.replace(tzinfo=timezone.utc) if due_at.tzinfo is None else due_at) < now
    ):
        return "OVERDUE"
    return status


def _body_meta(m) -> dict:
    """Additive provenance / lifecycle fields (all backward-compatible)."""
    return {
        "confirmation_status": m.get("confirmation_status", "confirmed"),
        "archive": bool(m.get("archive", False)),
        "created_by": str(m["created_by"]) if m.get("created_by") else None,
        "metadata": m.get("metadata") or {},
        "priority": m.get("priority") or "medium",
        "owner_user_id": str(m["owner_user_id"]) if m.get("owner_user_id") else None,
        "related_follow_up_id": str(m["related_follow_up_id"]) if m.get("related_follow_up_id") else None,
        "status_history": [
            {
                "status": h.get("status"),
                "changed_at": h["changed_at"].isoformat() if h.get("changed_at") else None,
                "changed_by": str(h["changed_by"]) if h.get("changed_by") else None,
            }
            for h in m.get("status_history") or []
        ],
    }


def _memory_to_json(m):
    now = datetime.now(timezone.utc)
    due_at = m.get("due_at")
    if due_at is not None and due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=timezone.utc)
    usage = []
    for u in m.get("usage") or []:
        usage.append({
            "used_at": u["used_at"].isoformat() if u.get("used_at") else None,
            "meeting_id": str(u["meeting_id"]) if u.get("meeting_id") else None,
            "session_id": str(u["session_id"]) if u.get("session_id") else None,
        })
    base = {
        "id": str(m["_id"]),
        "employee_id": str(m["employee_id"]),
        "session_id": str(m["session_id"]) if m.get("session_id") else None,
        "type": m.get("type"),
        "content": m.get("content", ""),
        "status": _effective_status(m, now),
        "due_at": due_at.isoformat() if due_at else None,
        "used_at": m.get("used_at").isoformat() if m.get("used_at") else None,
        "started_at": m.get("started_at").isoformat() if m.get("started_at") else None,
        "completed_at": m.get("completed_at").isoformat() if m.get("completed_at") else None,
        "cancelled_at": m.get("cancelled_at").isoformat() if m.get("cancelled_at") else None,
        "usage_count": m.get("usage_count", len(usage)),
        "usage": usage,
        "created_at": m["created_at"].isoformat() if m.get("created_at") else None,
        "updated_at": m["updated_at"].isoformat() if m.get("updated_at") else None,
    }
    base.update(_body_meta(m))
    return base


def _memory_scope_employee_ids(db, org_id: str):
    """Mirror of meetings._meeting_scope_employee_ids: resolve the employee
    ObjectIds a manager may read memory for.

    None → admin (full org). list → manager's reachable employee ids. [] →
    fail-closed (malformed/unknown scope matches nothing).
    """
    scope = _employee_scope_filter(db, org_id)
    if scope == _NEVER_MATCH:
        return []
    if not scope:
        return None
    reports_to = scope.get("reports_to")
    docs = db.employees.find(
        {
            "org_id": ObjectId(org_id),
            "$or": [{"reports_to": reports_to}, {"_id": reports_to}],
        },
        {"_id": 1},
    )
    return [d["_id"] for d in docs]


def _memory_emp_denied(db, org_id: str, m) -> bool:
    """True if the current session may not access a memory record's employee.

    Admin → False (full org). Manager → False only when the record's employee
    resolves and is within the manager's team.  Fails closed otherwise.
    """
    if _employee_scope_filter(db, org_id) == {}:
        return False
    emp = db.employees.find_one(
        {"_id": m.get("employee_id"), "org_id": ObjectId(org_id)}
    )
    if not emp or not _employee_accessible(db, org_id, emp):
        return True
    return False


def _actor_user(db, org_id: str):
    """Return the current user doc (or None) for created_by / owner storage."""
    uid = session.get("user_id")
    if not uid:
        return None
    try:
        uid = ObjectId(uid)
    except (InvalidId, TypeError):
        return None
    return db.users.find_one({"_id": uid, "org_id": ObjectId(org_id)})


def _parse_metadata(raw):
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        return None
    if len(raw) > MAX_METADATA_KEYS:
        return None
    for k, v in raw.items():
        if not isinstance(k, str):
            return None
        if v is not None and not isinstance(v, (str, int, float, bool)):
            return None
    return raw


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
    confirmation_status = (data.get("confirmation_status") or "confirmed").strip().lower()
    priority_raw = (data.get("priority") or "").strip().lower()
    owner_user_id = data.get("owner_user_id")
    related_follow_up_raw = data.get("related_follow_up_id")
    metadata = _parse_metadata(data.get("metadata"))

    if not employee_id:
        return jsonify({"error": "employee_id_required"}), 400
    if mtype not in MEMORY_TYPES:
        return jsonify({"error": "invalid_type"}), 400
    if not content:
        return jsonify({"error": "content_required"}), 400
    if len(content) > MAX_MEMORY_CONTENT_LEN:
        return jsonify({"error": "content_too_long"}), 400
    if confirmation_status not in CONFIRMATION_STATUSES:
        return jsonify({"error": "invalid_confirmation_status"}), 400
    if metadata is None:
        return jsonify({"error": "invalid_metadata"}), 400

    trackable = mtype in ("COMMITMENT", "FOLLOW_UP")
    priority = priority_raw or "medium"
    if priority not in PRIORITY_LEVELS:
        return jsonify({"error": "invalid_priority"}), 400
    if priority_raw and not trackable:
        return jsonify({"error": "priority_not_supported_for_type"}), 400

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

    # A manager may only record memory against employees in their team.
    if not _employee_accessible(db, org_id, emp):
        return jsonify({"error": "forbidden"}), 403

    if session_oid is not None:
        sess = db.sessions.find_one(
            {"_id": session_oid, "org_id": ObjectId(org_id), "employee_id": emp_oid}
        )
        if not sess:
            return jsonify({"error": "session_not_found"}), 404

    owner_oid = None
    if owner_user_id:
        if not trackable:
            return jsonify({"error": "owner_not_supported_for_type"}), 400
        try:
            owner_oid = ObjectId(owner_user_id)
        except InvalidId:
            return jsonify({"error": "invalid_owner_user_id"}), 400
        owner = db.users.find_one({"_id": owner_oid, "org_id": ObjectId(org_id)})
        if not owner:
            return jsonify({"error": "owner_not_found"}), 404

    related_follow_up_oid = None
    if related_follow_up_raw:
        try:
            related_follow_up_oid = ObjectId(related_follow_up_raw)
        except InvalidId:
            return jsonify({"error": "invalid_related_follow_up_id"}), 400
        rel = db.conversation_memory.find_one({
            "_id": related_follow_up_oid,
            "org_id": ObjectId(org_id),
            "employee_id": emp_oid,
            "type": "FOLLOW_UP",
        })
        if not rel:
            return jsonify({"error": "related_follow_up_not_found"}), 404

    due_at = None
    if due_at_raw:
        try:
            due_at = datetime.fromisoformat(due_at_raw)
            if due_at.tzinfo is None:
                due_at = due_at.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return jsonify({"error": "invalid_due_at"}), 400

    base_status = "SAVED"
    if trackable:
        # A newly captured commitment/follow-up is pending until explicitly
        # completed.  Nothing is auto-completed — only explicit action changes it.
        base_status = "PENDING"

    now = datetime.now(timezone.utc)
    actor = _actor_user(db, org_id)
    actor_oid = actor["_id"] if actor else None

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
        "cancelled_at": None,
        "usage_count": 0,
        "usage": [],
        "created_by": actor_oid,
        "confirmation_status": confirmation_status,
        "archive": False,
        "metadata": metadata,
        "status_history": [{
            "status": base_status,
            "changed_at": now,
            "changed_by": actor_oid,
        }],
        "created_at": now,
        "updated_at": now,
    }
    if trackable:
        doc["priority"] = priority
        doc["owner_user_id"] = owner_oid
    if related_follow_up_oid is not None:
        doc["related_follow_up_id"] = related_follow_up_oid

    result = db.conversation_memory.insert_one(doc)
    doc["_id"] = result.inserted_id

    try:
        log_audit_event(
            db, org_id, str(actor_oid) if actor_oid else None,
            session.get("user_name") or "", ACTION_MEMORY_CREATE,
            target_type="conversation_memory", target_id=str(doc["_id"]),
            target_label=content[:120],
            meta={"type": mtype, "confirmation_status": confirmation_status},
        )
    except Exception:
        logger.exception("audit log memory.create failed")

    return jsonify(_memory_to_json(doc)), 201


@conversation_memory_bp.route("/conversation-memory")
def list_memory():
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    query = {"org_id": ObjectId(org_id)}

    allowed_ids = _memory_scope_employee_ids(db, org_id)
    if allowed_ids == []:
        # Fail-closed manager scope: never fall through to org-wide reads.
        return jsonify({"items": [], "total": 0})
    if allowed_ids is not None:
        query["employee_id"] = {"$in": allowed_ids}

    emp_id = (request.args.get("employee_id") or "").strip()
    session_id = (request.args.get("session_id") or "").strip()
    mtype = (request.args.get("type") or "").strip().upper()
    status = (request.args.get("status") or "").strip().upper()
    confirmation = (request.args.get("confirmation_status") or "").strip().lower()
    archived = (request.args.get("archived") or "").strip().lower()

    if emp_id:
        try:
            emp_oid = ObjectId(emp_id)
        except InvalidId:
            return jsonify({"error": "invalid_employee_id"}), 400
        if allowed_ids is not None:
            if emp_oid not in allowed_ids:
                return jsonify({"items": [], "total": 0})
            query["employee_id"] = emp_oid
        else:
            query["employee_id"] = emp_oid
    if session_id:
        try:
            query["session_id"] = ObjectId(session_id)
        except InvalidId:
            return jsonify({"error": "invalid_session_id"}), 400
    if mtype:
        query["type"] = mtype
    if status:
        query["status"] = status
    if confirmation:
        query["confirmation_status"] = confirmation
    if archived == "true":
        query["archive"] = True
    elif archived == "false":
        query["archive"] = {"$ne": True}

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

    # Manager-role isolation: a manager must not edit another team's memory.
    if _memory_emp_denied(db, org_id, m):
        return jsonify({"error": "forbidden"}), 403

    data = request.get_json(silent=True) or {}
    set_fields: dict = {}
    mtype = m.get("type")
    actor = _actor_user(db, org_id)
    actor_oid = actor["_id"] if actor else None
    trackable = mtype in ("COMMITMENT", "FOLLOW_UP")

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

    if "confirmation_status" in data:
        confirmation = (data["confirmation_status"] or "").strip().lower()
        if confirmation not in CONFIRMATION_STATUSES:
            return jsonify({"error": "invalid_confirmation_status"}), 400
        set_fields["confirmation_status"] = confirmation

    if "archive" in data:
        archive = data["archive"]
        if not isinstance(archive, bool):
            return jsonify({"error": "invalid_archive"}), 400
        set_fields["archive"] = archive

    if "metadata" in data:
        metadata = _parse_metadata(data.get("metadata"))
        if metadata is None:
            return jsonify({"error": "invalid_metadata"}), 400
        set_fields["metadata"] = metadata

    if trackable:
        if "priority" in data:
            priority = (data["priority"] or "").strip().lower()
            if priority not in PRIORITY_LEVELS:
                return jsonify({"error": "invalid_priority"}), 400
            set_fields["priority"] = priority

        if "owner_user_id" in data:
            owner_raw = data.get("owner_user_id")
            if owner_raw:
                try:
                    owner_oid = ObjectId(owner_raw)
                except InvalidId:
                    return jsonify({"error": "invalid_owner_user_id"}), 400
                owner = db.users.find_one({"_id": owner_oid, "org_id": ObjectId(org_id)})
                if not owner:
                    return jsonify({"error": "owner_not_found"}), 404
                set_fields["owner_user_id"] = owner_oid
            else:
                set_fields["owner_user_id"] = None

    if "related_follow_up_id" in data:
        rel_raw = data.get("related_follow_up_id")
        if rel_raw:
            try:
                rel_oid = ObjectId(rel_raw)
            except InvalidId:
                return jsonify({"error": "invalid_related_follow_up_id"}), 400
            rel = db.conversation_memory.find_one({
                "_id": rel_oid,
                "org_id": ObjectId(org_id),
                "employee_id": m.get("employee_id"),
                "type": "FOLLOW_UP",
            })
            if not rel:
                return jsonify({"error": "related_follow_up_not_found"}), 404
            set_fields["related_follow_up_id"] = rel_oid
        else:
            set_fields["related_follow_up_id"] = None

    if "status" in data:
        status = (data["status"] or "").strip().upper()
        allowed = BASE_STATUSES.get(mtype, set())
        if status not in allowed:
            return jsonify({"error": "invalid_status"}), 400
        if status == "CANCELLED" and not trackable:
            return jsonify({"error": "invalid_status"}), 400
        if status == "IN_PROGRESS" and not trackable:
            return jsonify({"error": "invalid_status"}), 400
        if status == "COMPLETED" and not trackable:
            return jsonify({"error": "invalid_status"}), 400
        if status == "USED" and mtype not in ("OPENER", "QUESTION"):
            return jsonify({"error": "invalid_status"}), 400
        now = datetime.now(timezone.utc)
        set_fields["status"] = status
        if status == "USED":
            set_fields["used_at"] = now
        if status == "COMPLETED":
            set_fields["completed_at"] = now
        if status == "CANCELLED":
            set_fields["cancelled_at"] = now
        if status == "IN_PROGRESS" and not m.get("started_at"):
            set_fields["started_at"] = now
        # Append-only status history (preserves the original record trail).
        history = list(m.get("status_history") or [])
        history.append({
            "status": status,
            "changed_at": now,
            "changed_by": actor_oid,
        })
        set_fields["status_history"] = history

    if not set_fields:
        return jsonify({"error": "no_fields_to_update"}), 400

    set_fields["updated_at"] = datetime.now(timezone.utc)
    db.conversation_memory.update_one(
        {"_id": ObjectId(memory_id), "org_id": ObjectId(org_id)},
        {"$set": set_fields},
    )
    m = db.conversation_memory.find_one(
        {"_id": ObjectId(memory_id), "org_id": ObjectId(org_id)}
    )

    try:
        log_audit_event(
            db, org_id, str(actor_oid) if actor_oid else None,
            session.get("user_name") or "", ACTION_MEMORY_UPDATE,
            target_type="conversation_memory", target_id=str(m["_id"]),
            target_label=(m.get("content") or "")[:120],
            meta={"changed_fields": sorted(set_fields.keys())},
        )
    except Exception:
        logger.exception("audit log memory.update failed")

    return jsonify(_memory_to_json(m))


@conversation_memory_bp.route("/conversation-memory/<memory_id>/usage", methods=["POST"])
def record_usage(memory_id: str):
    """Explicitly record that an opener/question was actually used.

    This is the confirmation point for surfacing — nothing marks an opener as
    "used" on its own. Only OPENER/QUESTION items can be recorded as used.

    Appends to a per-item usage history {used_at, meeting_id, session_id},
    increments usage_count, sets last used_at, and marks the item USED.
    The underlying memory status change (SAVED -> USED) is explicit-only and
    never auto-completes anything.
    """
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    try:
        oid = ObjectId(memory_id)
    except InvalidId:
        return jsonify({"error": "invalid_id"}), 400

    m = db.conversation_memory.find_one({"_id": oid, "org_id": ObjectId(org_id)})
    if not m:
        return jsonify({"error": "not_found"}), 404

    # Manager-role isolation: only teams the manager may access.
    if _memory_emp_denied(db, org_id, m):
        return jsonify({"error": "forbidden"}), 403

    if m.get("type") not in ("OPENER", "QUESTION"):
        return jsonify({"error": "not_usable"}), 400

    data = request.get_json(silent=True) or {}
    meeting_oid = None
    meeting_id_raw = data.get("meeting_id")
    if meeting_id_raw:
        try:
            meeting_oid = ObjectId(meeting_id_raw)
        except InvalidId:
            return jsonify({"error": "invalid_meeting_id"}), 400
        meeting = db.meetings.find_one(
            {"_id": meeting_oid, "org_id": ObjectId(org_id), "employee_id": m["employee_id"]}
        )
        if not meeting:
            return jsonify({"error": "meeting_not_found"}), 404

    session_oid = None
    session_id_raw = data.get("session_id")
    if session_id_raw:
        try:
            session_oid = ObjectId(session_id_raw)
        except InvalidId:
            session_oid = None

    now = datetime.now(timezone.utc)
    actor = _actor_user(db, org_id)
    actor_oid = actor["_id"] if actor else None

    usage_entry = {
        "used_at": now,
        "meeting_id": meeting_oid,
        "session_id": session_oid,
    }
    current_usage = list(m.get("usage") or [])
    current_usage.append(usage_entry)
    history = list(m.get("status_history") or [])
    history.append({
        "status": "USED",
        "changed_at": now,
        "changed_by": actor_oid,
    })

    db.conversation_memory.update_one(
        {"_id": oid, "org_id": ObjectId(org_id)},
        {"$set": {
            "status": "USED",
            "used_at": now,
            "usage_count": len(current_usage),
            "usage": current_usage,
            "status_history": history,
            "updated_at": now,
        }},
    )
    m = db.conversation_memory.find_one({"_id": oid, "org_id": ObjectId(org_id)})

    try:
        log_audit_event(
            db, org_id, str(actor_oid) if actor_oid else None,
            session.get("user_name") or "", ACTION_MEMORY_USE,
            target_type="conversation_memory", target_id=str(oid),
            target_label=(m.get("content") or "")[:120],
            meta={"usage_count": len(current_usage)},
        )
    except Exception:
        logger.exception("audit log memory.usage failed")

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

    m = db.conversation_memory.find_one({"_id": oid, "org_id": ObjectId(org_id)})
    if not m:
        return jsonify({"error": "not_found"}), 404

    # Manager-role isolation: a manager must not delete another team's memory.
    if _memory_emp_denied(db, org_id, m):
        return jsonify({"error": "forbidden"}), 403

    result = db.conversation_memory.delete_one({"_id": oid, "org_id": ObjectId(org_id)})
    if not result.deleted_count:
        return jsonify({"error": "not_found"}), 404

    actor = _actor_user(db, org_id)
    try:
        log_audit_event(
            db, org_id, str(actor["_id"]) if actor else None,
            session.get("user_name") or "", ACTION_MEMORY_DELETE,
            target_type="conversation_memory", target_id=str(oid),
            target_label=(m.get("content") or "")[:120],
        )
    except Exception:
        logger.exception("audit log memory.delete failed")

    return jsonify({"ok": True})
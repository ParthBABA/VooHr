import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, jsonify, request, session

from employees import _require_auth
from employees import _employee_to_json
from employees import _NEVER_MATCH
from employees import _employee_scope_filter
from employees import _employee_accessible
from extensions import get_db
from reminders import surface_items, ensure_reminder_notifications

logger = logging.getLogger(__name__)

meetings_bp = Blueprint("meetings", __name__)

MEETING_STATUSES = {"scheduled", "completed", "cancelled"}

MAX_MEETING_TITLE_LEN = 200


def _meeting_to_json(m, emp=None) -> dict:
    return {
        "id": str(m["_id"]),
        "employee_id": str(m["employee_id"]),
        "title": m.get("title", ""),
        "scheduled_at": m["scheduled_at"].isoformat() if m.get("scheduled_at") else None,
        "status": m.get("status", "scheduled"),
        "session_id": str(m["session_id"]) if m.get("session_id") else None,
        "created_at": m["created_at"].isoformat() if m.get("created_at") else None,
        "updated_at": m["updated_at"].isoformat() if m.get("updated_at") else None,
        "employee": _employee_to_json(emp) if emp else None,
    }


def _lookup_employee(db, org_id, employee_id):
    try:
        oid = ObjectId(employee_id)
    except InvalidId:
        return None
    return db.employees.find_one({"_id": oid, "org_id": ObjectId(org_id)})


def _meeting_scope_employee_ids(db, org_id: str):
    """Resolve the employee ObjectIds a manager may see meetings for.

    Reuses ``employees._employee_scope_filter`` (the same fail-closed helper
    the employee routes use) rather than inventing a parallel implementation.

    Returns:
        None  → admin / unscoped: no employee filter, full org (unchanged).
        list  → manager's reachable employee ObjectIds (direct reports plus
                their own record, matching ``_employee_accessible``).
        []    → fail-closed: malformed/unknown scope matches nothing.
    """
    scope = _employee_scope_filter(db, org_id)
    if scope == _NEVER_MATCH:
        return []
    if not scope:
        return None  # admin
    reports_to = scope.get("reports_to")
    docs = db.employees.find(
        {
            "org_id": ObjectId(org_id),
            "$or": [{"reports_to": reports_to}, {"_id": reports_to}],
        },
        {"_id": 1},
    )
    return [d["_id"] for d in docs]


def _meeting_emp_denied(db, org_id: str, m) -> bool:
    """True if the current session may not access a single meeting's employee.

    Admin → always False (full org, unchanged).  Manager/fail-closed → False
    only when the meeting's employee resolves and is within the manager's
    team (via ``_employee_accessible``); otherwise True (fail closed).
    """
    if _employee_scope_filter(db, org_id) == {}:
        return False  # admin
    emp = db.employees.find_one({"_id": m.get("employee_id"), "org_id": ObjectId(org_id)})
    if not emp or not _employee_accessible(db, org_id, emp):
        return True
    return False


@meetings_bp.route("/meetings", methods=["POST"])
def create_meeting():
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    data = request.get_json(silent=True) or {}
    employee_id = data.get("employee_id")
    scheduled_at = data.get("scheduled_at")
    title = (data.get("title") or "").strip()
    session_id = data.get("session_id")

    if not employee_id:
        return jsonify({"error": "employee_id_required"}), 400
    if not scheduled_at:
        return jsonify({"error": "scheduled_at_required"}), 400
    if len(title) > MAX_MEETING_TITLE_LEN:
        return jsonify({"error": "title_too_long"}), 400

    try:
        scheduled_dt = datetime.fromisoformat(scheduled_at)
        if scheduled_dt.tzinfo is None:
            scheduled_dt = scheduled_dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return jsonify({"error": "invalid_scheduled_at"}), 400

    db = get_db()
    emp = _lookup_employee(db, org_id, employee_id)
    if not emp:
        return jsonify({"error": "employee_not_found"}), 404

    # A manager must not schedule a meeting against another team's employee.
    if not _employee_accessible(db, org_id, emp):
        return jsonify({"error": "forbidden"}), 403

    session_oid = None
    if session_id:
        try:
            session_oid = ObjectId(session_id)
        except InvalidId:
            pass
        if session_oid is not None:
            sess = db.sessions.find_one(
                {"_id": session_oid, "org_id": ObjectId(org_id), "employee_id": ObjectId(employee_id)}
            )
            if not sess:
                session_oid = None

    now = datetime.now(timezone.utc)
    doc = {
        "org_id": ObjectId(org_id),
        "employee_id": ObjectId(employee_id),
        "title": title,
        "scheduled_at": scheduled_dt,
        "status": "scheduled",
        "session_id": session_oid,
        "created_by": ObjectId(session.get("user_id")) if session.get("user_id") else None,
        "created_at": now,
        "updated_at": now,
    }
    result = db.meetings.insert_one(doc)
    doc["_id"] = result.inserted_id
    return jsonify(_meeting_to_json(doc, emp)), 201


@meetings_bp.route("/meetings")
def list_meetings():
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    query = {"org_id": ObjectId(org_id)}

    # Manager-role scoping: a manager only sees meetings for employees who
    # report to them. Hands off to the same fail-closed helper the employee
    # routes use. Admin sessions get None (no filter → unchanged behavior).
    allowed_ids = _meeting_scope_employee_ids(db, org_id)
    if allowed_ids == []:
        # Fail-closed manager scope (malformed linked_employee_id, or no
        # reports yet): nothing to show — never fall through to org-wide.
        return jsonify({"meetings": [], "total": 0})
    if allowed_ids is not None:
        query["employee_id"] = {"$in": allowed_ids}

    emp_id = (request.args.get("employee_id") or "").strip()
    status = (request.args.get("status") or "").strip()
    if emp_id:
        try:
            emp_oid = ObjectId(emp_id)
        except InvalidId:
            return jsonify({"error": "invalid_employee_id"}), 400
        if allowed_ids is not None:
            # Targeting a specific employee must not bypass the manager's
            # scope: if that employee isn't in the team, return nothing.
            if emp_oid not in allowed_ids:
                return jsonify({"meetings": [], "total": 0})
            query["employee_id"] = emp_oid
        else:
            query["employee_id"] = emp_oid
    if status:
        query["status"] = status

    meetings = list(db.meetings.find(query).sort("scheduled_at", 1))

    emp_cache: dict = {}
    result = []
    for m in meetings:
        eid = m["employee_id"]
        if eid not in emp_cache:
            emp_cache[eid] = db.employees.find_one({"_id": eid, "org_id": ObjectId(org_id)})
        result.append(_meeting_to_json(m, emp_cache.get(eid)))

    return jsonify({
        "meetings": result,
        "total": len(result),
    })


@meetings_bp.route("/meetings/dashboard")
def meetings_dashboard():
    """One-shot aggregate feed for the Meeting Tracker board.

    Real-world, org-scoped: each active employee becomes one card carrying
    their latest scheduled meeting, latest completed session (for previous
    context), and factual conversation-memory counters + the actual open
    (PENDING/OVERDUE) commitment & follow-up items.

    Returned in a single call to avoid N+1; the detailed per-item history is
    still loaded on demand by the detail views via the dedicated endpoints.
    """
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    org_oid = ObjectId(org_id)
    now = datetime.now(timezone.utc)

    # Manager-role scoping: the whole board is limited to the manager's own
    # team. Admin gets None (no extra employee filter → unchanged behavior);
    # a fail-closed manager scope returns [] and short-circuits to an empty
    # board without querying org-wide data or firing reminder notifications.
    allowed_ids = _meeting_scope_employee_ids(db, org_id)
    if allowed_ids == []:
        return jsonify({
            "employees": [],
            "people": [],
            "counters": {"today": 0, "this_week": 0, "pending_followups": 0, "pending_commitments": 0},
            "followup_employee_ids": [],
            "overdue_employee_ids": [],
        })

    emp_filter = {"org_id": org_oid, "status": "active"}
    if allowed_ids is not None:
        emp_filter["_id"] = {"$in": allowed_ids}
    employees = list(db.employees.find(emp_filter).sort("created_at", 1))

    meeting_filter = {"org_id": org_oid, "status": {"$in": ["scheduled", "completed"]}}
    if allowed_ids is not None:
        meeting_filter["employee_id"] = {"$in": allowed_ids}
    meetings = list(db.meetings.find(meeting_filter).sort("scheduled_at", 1))

    session_filter = {"org_id": org_oid}
    if allowed_ids is not None:
        session_filter["employee_id"] = {"$in": allowed_ids}
    sessions = list(db.sessions.find(session_filter).sort("created_at", -1))
    latest_completed: dict = {}
    for s in sessions:
        if s.get("status") == "completed":
            eid = str(s.get("employee_id"))
            if eid not in latest_completed:
                latest_completed[eid] = s

    # For surfacing/reminders we only need the actionable subset: PENDING
    # (commitments/follow-ups; OVERDUE is derived at read time) and SAVED
    # (not-yet-used openers/questions/notes). Full histories are loaded on
    # demand by the detail views.
    memory_filter = {"org_id": org_oid, "status": {"$in": ["PENDING", "SAVED"]}}
    if allowed_ids is not None:
        memory_filter["employee_id"] = {"$in": allowed_ids}
    memory = list(db.conversation_memory.find(memory_filter))
    by_emp: dict = {}
    for m in memory:
        eid = str(m.get("employee_id"))
        agg = by_emp.setdefault(eid, {
            "pending_commitments": 0, "pending_followups": 0,
            "overdue_followups": 0, "openers_used": 0, "openers_saved": 0,
            "notes": 0, "questions_used": 0, "open_items": [],
        })
        mt = m.get("type")
        status = m.get("status")
        due_at = m.get("due_at")
        effective = status
        if (
            mt in ("COMMITMENT", "FOLLOW_UP")
            and status == "PENDING"
            and due_at is not None
            and due_at < now
        ):
            effective = "OVERDUE"
        if mt == "COMMITMENT" and effective in ("PENDING", "OVERDUE"):
            agg["pending_commitments"] += 1
        if mt == "FOLLOW_UP" and effective in ("PENDING", "OVERDUE"):
            agg["pending_followups"] += 1
            if effective == "OVERDUE":
                agg["overdue_followups"] += 1
        if mt == "OPENER" and status == "USED":
            agg["openers_used"] += 1
        if mt == "OPENER" and status == "SAVED":
            agg["openers_saved"] += 1
        if mt == "NOTE":
            agg["notes"] += 1
        if mt == "QUESTION" and status == "USED":
            agg["questions_used"] += 1
        # Collect the actual open items for the Follow section + detail
        if mt in ("COMMITMENT", "FOLLOW_UP") and effective in ("PENDING", "OVERDUE"):
            agg["open_items"].append({
                "id": str(m["_id"]),
                "type": mt,
                "content": m.get("content", ""),
                "due_at": due_at.isoformat() if due_at else None,
                "status": effective,
            })
    for agg in by_emp.values():
        agg["open_items"].sort(key=lambda o: o["due_at"] or "9999-12-31T00:00:00")

    people = []
    for e in employees:
        eid = str(e["_id"])
        agg = by_emp.get(eid, {
            "pending_commitments": 0, "pending_followups": 0,
            "overdue_followups": 0, "openers_used": 0, "openers_saved": 0,
            "notes": 0, "questions_used": 0, "open_items": [],
        })
        next_meeting = next(
            (mk for mk in meetings if str(mk.get("employee_id")) == eid and mk.get("status") == "scheduled"),
            None,
        )
        prev = latest_completed.get(eid)
        counts = {k: agg[k] for k in (
            "pending_commitments", "pending_followups", "overdue_followups",
            "openers_used", "openers_saved", "notes", "questions_used",
        )}
        person = {
            "id": eid,
            "employee": _employee_to_json(e),
            "next_meeting": _meeting_to_json(next_meeting) if next_meeting else None,
            "previous_session": {
                "session_id": str(prev["_id"]) if prev else None,
                "created_at": prev["created_at"].isoformat() if prev else None,
            } if prev else None,
            "open_items": agg["open_items"],
            "counts": counts,
            # Populated after the loop once surfaced items are computed.
            "surfaced": [],
        }
        people.append(person)

    # A card only makes sense when there is something to surface (a pending
    # meeting or an unresolved commitment/follow-up). Skip pure-lurkers.
    def has_followup(p):
        return p["counts"]["pending_followups"] > 0

    def has_meeting(p):
        m = p["next_meeting"]
        return m is not None and m["scheduled_at"] is not None

    people = [p for p in people if p["next_meeting"] or has_followup(p)]
    people.sort(key=lambda p: (p["employee"]["name"] or "").lower())

    # Deterministic memory surfacing: for each employee with an upcoming
    # meeting, attach their prioritized actionable surfaced items
    # (PENDING/OVERDUE commitments & follow-ups, saved openers, etc.).
    # The full prioritized list feeds the "BEFORE YOU START" area on cards;
    # nothing here invents facts — only stored conversation_memory records.
    employees_with_upcoming = {
        p["id"]
        for p in people
        if p["next_meeting"] and p["next_meeting"].get("scheduled_at")
    }
    surfaces = surface_items(memory, employees_with_upcoming, now)
    for p in people:
        p["surfaced"] = surfaces.get(p["id"], [])

    counters = {
        "today": sum(
            1 for p in people
            if p["next_meeting"]
            and datetime.fromisoformat(p["next_meeting"]["scheduled_at"]).date() == now.date()
        ),
        "this_week": sum(
            1 for p in people
            if p["next_meeting"]
            and now.date()
            <= datetime.fromisoformat(p["next_meeting"]["scheduled_at"]).date()
            <= (now + timedelta(days=6 - now.weekday())).date()
        ),
        "pending_followups": sum(p["counts"]["pending_followups"] for p in people),
        "pending_commitments": sum(p["counts"]["pending_commitments"] for p in people),
        # No reminder entity exists — no reminders metric.
    }

    followup_employee_ids = [p["id"] for p in people if p["counts"]["pending_followups"] > 0]
    overdue_employee_ids = [p["id"] for p in people if p["counts"]["overdue_followups"] > 0]

    # Idempotent: surfaces reachable upcoming-meeting reminders into the org's
    # notification bell without ever duplicating a (meeting, memory, stage) key.
    try:
        ensure_reminder_notifications(db, org_id, now)
    except Exception:  # never break the board because notification writes fail
        logger.exception("ensure_reminder_notifications failed during dashboard")

    return jsonify({
        "employees": [_employee_to_json(e) for e in employees],
        "people": people,
        "counters": counters,
        "followup_employee_ids": followup_employee_ids,
        "overdue_employee_ids": overdue_employee_ids,
    })


@meetings_bp.route("/meetings/<meeting_id>")
def get_meeting(meeting_id: str):
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    try:
        m = db.meetings.find_one({"_id": ObjectId(meeting_id), "org_id": ObjectId(org_id)})
    except InvalidId:
        return jsonify({"error": "invalid_id"}), 400
    if not m:
        return jsonify({"error": "not_found"}), 404

    # IDOR guard: a manager must not view another team's meeting.
    if _meeting_emp_denied(db, org_id, m):
        return jsonify({"error": "forbidden"}), 403

    emp = db.employees.find_one({"_id": m["employee_id"], "org_id": ObjectId(org_id)})
    return jsonify(_meeting_to_json(m, emp))


@meetings_bp.route("/meetings/<meeting_id>", methods=["PATCH"])
def update_meeting(meeting_id: str):
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    try:
        m = db.meetings.find_one({"_id": ObjectId(meeting_id), "org_id": ObjectId(org_id)})
    except InvalidId:
        return jsonify({"error": "invalid_id"}), 400
    if not m:
        return jsonify({"error": "not_found"}), 404

    # IDOR guard: a manager must not modify another team's meeting.
    if _meeting_emp_denied(db, org_id, m):
        return jsonify({"error": "forbidden"}), 403

    data = request.get_json(silent=True) or {}
    set_fields: dict = {}

    if "title" in data:
        title = (data["title"] or "").strip()
        if len(title) > MAX_MEETING_TITLE_LEN:
            return jsonify({"error": "title_too_long"}), 400
        if title:
            set_fields["title"] = title

    if "scheduled_at" in data:
        try:
            scheduled_dt = datetime.fromisoformat(data["scheduled_at"])
            if scheduled_dt.tzinfo is None:
                scheduled_dt = scheduled_dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return jsonify({"error": "invalid_scheduled_at"}), 400
        set_fields["scheduled_at"] = scheduled_dt

    if "status" in data:
        status = (data["status"] or "").strip()
        if status not in MEETING_STATUSES:
            return jsonify({"error": "invalid_status"}), 400
        set_fields["status"] = status

    if "session_id" in data:
        session_id = data.get("session_id")
        if session_id:
            try:
                session_oid = ObjectId(session_id)
            except InvalidId:
                return jsonify({"error": "invalid_session_id"}), 400
            sess = db.sessions.find_one(
                {"_id": session_oid, "org_id": ObjectId(org_id), "employee_id": ObjectId(m["employee_id"])}
            )
            if not sess:
                return jsonify({"error": "session_not_found"}), 404
            set_fields["session_id"] = session_oid
        else:
            set_fields["session_id"] = None

    if not set_fields:
        return jsonify({"error": "no_fields_to_update"}), 400

    set_fields["updated_at"] = datetime.now(timezone.utc)
    db.meetings.update_one({"_id": ObjectId(meeting_id)}, {"$set": set_fields})
    m = db.meetings.find_one({"_id": ObjectId(meeting_id)})
    emp = db.employees.find_one({"_id": m["employee_id"], "org_id": ObjectId(org_id)})
    return jsonify(_meeting_to_json(m, emp))


@meetings_bp.route("/meetings/<meeting_id>", methods=["DELETE"])
def delete_meeting(meeting_id: str):
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    try:
        oid = ObjectId(meeting_id)
    except InvalidId:
        return jsonify({"error": "invalid_id"}), 400

    m = db.meetings.find_one({"_id": oid, "org_id": ObjectId(org_id)})
    if not m:
        return jsonify({"error": "not_found"}), 404

    # IDOR guard: a manager must not delete another team's meeting.
    if _meeting_emp_denied(db, org_id, m):
        return jsonify({"error": "forbidden"}), 403

    db.meetings.delete_one({"_id": oid, "org_id": ObjectId(org_id)})
    return jsonify({"ok": True})

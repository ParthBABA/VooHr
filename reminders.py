"""Phase 3 — deterministic reminder & memory-surfacing layer for meetings.

Hard rules (they are enforced here, not just intended):
  * We never invent facts. Every surfaced item is an actual record that already
    exists in ``conversation_memory`` — we only pick which stored items to show.
  * No AI, no behavioral conclusions, no auto-completion, no generic advice.
  * Reminder uniqueness is ``meeting_id + memory_id + stage`` (NOT the text).
  * Surfaced set is computed deterministically and scoped to the org.

A surfaced item is one of:
  - PENDING / OVERDUE commitment
  - PENDING / OVERDUE follow-up
  - SAVED (not-yet-used) opener
  - SAVED question
  - other explicitly SAVED item (e.g. NOTE)

An item stops being surfaced the moment it is explicitly COMPLETED (or, for
openers/questions, explicitly USED) — nothing here changes those states.
"""
import logging
from datetime import datetime, timezone, timedelta

from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, jsonify, request

from employees import _require_auth
from extensions import get_db

logger = logging.getLogger(__name__)

reminders_bp = Blueprint("reminders", __name__)

_RELEVANT_STATUSES = {"PENDING", "SAVED"}

# Factual labels only — no interpretation, no advice.
_TYPE_LABEL = {
    "OPENER": "opener",
    "QUESTION": "question",
    "COMMITMENT": "commitment",
    "FOLLOW_UP": "follow-up",
    "NOTE": "note",
}


def _prio_key(it) -> tuple:
    """Stable, explicit ordering for the HR board.

    overdue commitment > overdue follow-up > pending commitment >
    pending follow-up > saved opener > saved question > other saved.
    """
    t = it["type"]
    s = it["status"]
    if t == "COMMITMENT" and s == "OVERDUE":
        return (0,)
    if t == "FOLLOW_UP" and s == "OVERDUE":
        return (1,)
    if t == "COMMITMENT" and s == "PENDING":
        return (2,)
    if t == "FOLLOW_UP" and s == "PENDING":
        return (3,)
    if t == "OPENER":
        return (4,)
    if t == "QUESTION":
        return (5,)
    return (6,)


def surface_items(memory, upcoming_emp_ids, now):
    """Return ``{employee_id: [surface_item, ...]}`` for employees who have an
    upcoming meeting. Sorted by ``_prio_key``.

    ``memory`` is a list of conversation_memory docs already scoped to the org
    (typically status in PENDING/SAVED). ``upcoming_emp_ids`` is the set of
    employee ObjectId strings that have a reachable upcoming meeting — only
    those employees get anything surfaced.
    """
    surfaces: dict = {}
    for m in memory:
        eid = str(m.get("employee_id"))
        if eid not in upcoming_emp_ids:
            continue
        mt = m.get("type")
        status = m.get("status") or "SAVED"
        due = m.get("due_at")
        effective = status
        if (
            mt in ("COMMITMENT", "FOLLOW_UP")
            and status == "PENDING"
            and due is not None
            and due < now
        ):
            effective = "OVERDUE"

        if mt in ("COMMITMENT", "FOLLOW_UP"):
            # Only actionable open commitments/follow-ups are surfaced.
            if effective not in ("PENDING", "OVERDUE"):
                continue
        elif mt in ("OPENER", "QUESTION"):
            # Only not-yet-used openers/questions are actionable to surface.
            if status == "USED":
                continue
            effective = "SAVED"
        elif mt == "NOTE":
            if status != "SAVED":
                continue
            effective = "SAVED"
        else:
            continue

        surfaces.setdefault(eid, []).append({
            "id": str(m["_id"]),
            "type": mt,
            "content": m.get("content", ""),
            "status": effective,
            "due_at": due.isoformat() if due else None,
            "session_id": str(m["session_id"]) if m.get("session_id") else None,
            "created_at": m["created_at"].isoformat() if m.get("created_at") else None,
        })

    for items in surfaces.values():
        items.sort(key=_prio_key)
    return surfaces


def _now():
    return datetime.now(timezone.utc)


def stage_for(meeting_time, now):
    """Compute the reminder stage for a meeting, or None if it is too far out.

    soon_1h  -> within the next hour
    day_of   -> meeting is scheduled for today (calendar day)
    upcoming_24h -> within the next 24 hours (but not today)
    """
    if meeting_time.tzinfo is None:
        meeting_time = meeting_time.replace(tzinfo=timezone.utc)
    delta = meeting_time - now
    if delta <= timedelta(hours=1):
        return "soon_1h"
    if meeting_time.date() == now.date():
        return "day_of"
    if delta <= timedelta(hours=24):
        return "upcoming_24h"
    return None


def _reminder_summary(it) -> str:
    label = _TYPE_LABEL.get(it["type"], it["type"])
    suffix = f" · due {it['due_at']}" if it.get("due_at") else ""
    return f"{label} {it['status'].lower()}: {it['content']}{suffix}"


def ensure_reminder_notifications(db, org_id, now=None) -> int:
    """Idempotently create reminder notifications for reachable upcoming
    meetings.

    One notification per (org, meeting_id, memory_id, stage).  Re-running is a
    no-op for existing keys, so loading the dashboard repeatedly never
    duplicates reminders.      ``now`` is injectable for tests.
    """
    now = now or _now()
    org_oid = ObjectId(org_id)

    meetings = list(db.meetings.find({"org_id": org_oid, "status": "scheduled"}))
    memory = list(db.conversation_memory.find(
        {"org_id": org_oid, "status": {"$in": list(_RELEVANT_STATUSES)}}
    ))

    upcoming_by_emp: dict = {}
    for m in meetings:
        if m.get("scheduled_at") and stage_for(m["scheduled_at"], now) is not None:
            upcoming_by_emp[str(m.get("employee_id"))] = m

    surfaces = surface_items(memory, set(upcoming_by_emp.keys()), now)

    created = 0
    for eid, items in surfaces.items():
        meeting = upcoming_by_emp.get(eid)
        if not meeting:
            continue
        stage = stage_for(meeting["scheduled_at"], now)
        if stage is None:
            continue
        for it in items:
            memory_oid = ObjectId(it["id"])
            existing = db.notifications.find_one({
                "org_id": org_oid,
                "meeting_id": meeting["_id"],
                "memory_id": memory_oid,
                "stage": stage,
            })
            if existing:
                continue
            db.notifications.insert_one({
                "org_id": org_oid,
                "type": "meeting_reminder",
                "headline": f"Before {'this' if stage == 'day_of' else 'your next'} meeting",
                "summary": _reminder_summary(it),
                "confidence": 0,
                "employee_id": meeting["employee_id"],
                "source_session_id": None,
                "meeting_id": meeting["_id"],
                "memory_id": memory_oid,
                "stage": stage,
                "read": False,
                "dismissed": False,
                "created_at": now,
            })
            created += 1

    if created:
        logger.debug("ensure_reminder_notifications: created=%d org=%s", created, org_id)
    return created


def _reminder_to_json(r):
    return {
        "id": str(r["_id"]),
        "type": r.get("type"),
        "headline": r.get("headline", ""),
        "summary": r.get("summary", ""),
        "employee_id": str(r["employee_id"]) if r.get("employee_id") else None,
        "meeting_id": str(r["meeting_id"]) if r.get("meeting_id") else None,
        "memory_id": str(r["memory_id"]) if r.get("memory_id") else None,
        "stage": r.get("stage"),
        "read": r.get("read", False),
        "dismissed": r.get("dismissed", False),
        "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
    }


@reminders_bp.route("/reminders/generate", methods=["POST"])
def generate_reminders():
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401
    db = get_db()
    created = ensure_reminder_notifications(db, org_id, _now())
    return jsonify({"ok": True, "created": created})


@reminders_bp.route("/reminders")
def list_reminders():
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    query = {"org_id": ObjectId(org_id), "type": "meeting_reminder"}
    meeting_id = (request.args.get("meeting_id") or "").strip()
    if meeting_id:
        try:
            query["meeting_id"] = ObjectId(meeting_id)
        except InvalidId:
            return jsonify({"error": "invalid_meeting_id"}), 400

    docs = list(db.notifications.find(query).sort("created_at", -1))
    return jsonify({
        "reminders": [_reminder_to_json(r) for r in docs],
        "total": len(docs),
    })

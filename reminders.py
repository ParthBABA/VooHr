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
import os
from datetime import datetime, timezone, timedelta

from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, jsonify, request

import email_service
from employees import _require_auth
from extensions import get_db
from field_encryption import decrypt_fields

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


# ── Delivery channels (email + WhatsApp-on-top of in-app notifications) ───


def _meeting_owner(db, org_id, meeting):
    """The HR/manager user who scheduled a meeting (stored as ``created_by``
    since create_meeting). Returns None when absent so legacy meetings simply
    skip out-of-app delivery instead of erroring."""
    created_by = meeting.get("created_by")
    if not created_by:
        return None
    try:
        created_by = ObjectId(created_by)
    except (InvalidId, TypeError):
        return None
    return db.users.find_one({"_id": created_by, "org_id": ObjectId(org_id)})


def _user_email(user) -> str | None:
    """Best-effort decrypted email for a user (user PII is envelope-encrypted,
    with a plain ``email`` fallback for fixtures/legacy docs)."""
    if not user:
        return None
    email = user.get("email") or ""
    if not email:
        try:
            pii = decrypt_fields(user.get("encrypted"), user.get("wrapped_dek", ""))
            email = pii.get("email") or ""
        except Exception:
            email = ""
    return email.strip() or None


def _meeting_reminders_enabled(user) -> bool:
    """User-level opt-out for email/WhatsApp meeting reminders. Defaults to
    True — in-app notifications are never gated by this."""
    if not user:
        return True
    prefs = user.get("notification_prefs") or {}
    return bool(prefs.get("meeting_reminders", True))


def _reminder_phone_number(user) -> str | None:
    """Verified phone number for WhatsApp delivery (from the WhatsApp intake
    feature's settings field). Returns None when the intake integration has not
    stored one yet."""
    if not user:
        return None
    number = (user.get("phone_number") or "").strip()
    if not number:
        wa = user.get("whatsapp") or {}
        number = (wa.get("phone_number") or "").strip()
    return number or None


def _meeting_page_url() -> str:
    """Deep link to the Meeting Tracker board (the page that surfaces meetings
    and their pending follow-ups)."""
    base = (
        os.environ.get("CLIENT_URL") or os.environ.get("SITE_URL") or ""
    ).strip().rstrip("/")
    return f"{base}/meeting-tracker" if base else "/meeting-tracker"


def _whatsapp_reminder_text(employee_name, meeting_time, reminder_summary, stage) -> str:
    """Terse WhatsApp-style reminder body (not the full email body)."""
    when = meeting_time.strftime("%A %d %b · %I:%M %p") if meeting_time else "soon"
    return (
        f"VooVr · {employee_name or 'Your'} meeting : {when} ({stage}). "
        f"Open follow-up: {reminder_summary}. "
        f"Details: {_meeting_page_url()}"
    )


def _send_reminder_whatsapp(to_phone: str, text: str) -> bool:
    """TODO(whatsapp): WhatsApp Cloud API outbound is not wired up yet.

    The earlier WhatsApp intake integration never landed — there is no
    whatsapp.py module and no WHATSAPP_ACCESS_TOKEN env var. This is an
    intentional no-op stub so reminder generation keeps working today; wire it
    to `whatsapp.send_message(to_phone, text)` once that helper exists.
    """
    if not to_phone:
        return False
    if not os.environ.get("WHATSAPP_ACCESS_TOKEN"):
        logger.debug(
            "reminder_whatsapp=skipped reason=no_whatsapp_token phone_set=%s",
            bool(to_phone),
        )
        return False
    # TODO(whatsapp): call the Cloud API helper here — this line is unreachable
    # until WHATSAPP_ACCESS_TOKEN is configured.
    logger.info("reminder_whatsapp=sent phone=%s", to_phone)
    return True


def _deliver_reminder_channels(db, org_id, meeting, it, stage):
    """Send email + WhatsApp for one reminder. Every failure is logged and
    swallowed — a bad address or a dead provider must never block the in-app
    notification or crash reminder generation."""
    user = _meeting_owner(db, org_id, meeting)
    if not user:
        return
    if not _meeting_reminders_enabled(user):
        logger.debug(
            "reminder_email=skipped reason=opt_out user=%s meeting=%s stage=%s",
            user.get("_id"), meeting.get("_id"), stage,
        )
        return

    emp = None
    try:
        eid = meeting.get("employee_id")
        if eid:
            emp = db.employees.find_one({"_id": eid, "org_id": ObjectId(org_id)})
    except Exception:
        emp = None
    employee_name = ""
    if emp:
        try:
            pii = decrypt_fields(emp.get("encrypted"), emp.get("wrapped_dek", ""))
            employee_name = pii.get("name") or ""
        except Exception:
            employee_name = ""
    if not employee_name:
        employee_name = emp.get("name") if emp else "your colleague"

    summary = _reminder_summary(it)
    meeting_time = meeting.get("scheduled_at")

    owner_email = _user_email(user)
    if owner_email:
        try:
            email_service.send_reminder_email(
                owner_email, employee_name, meeting_time, summary, stage
            )
        except Exception:
            logger.exception(
                "reminder_email=failed meeting=%s stage=%s recipient=%s",
                meeting.get("_id"), stage, owner_email,
            )

    phone = _reminder_phone_number(user)
    if phone:
        try:
            _send_reminder_whatsapp(
                phone, _whatsapp_reminder_text(employee_name, meeting_time, summary, stage)
            )
        except Exception:
            logger.exception(
                "reminder_whatsapp=failed meeting=%s stage=%s phone_set=True",
                meeting.get("_id"), stage,
            )


def _deliver_reminder(db, org_id, meeting, it, stage):
    """Wraps the per-reminder delivery so an unexpected failure anywhere can
    never escape the generation loop."""
    try:
        _deliver_reminder_channels(db, org_id, meeting, it, stage)
    except Exception:
        logger.exception(
            "reminder_delivery=failed meeting=%s stage=%s",
            meeting.get("_id"), stage,
        )


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
            _deliver_reminder(db, org_id, meeting, it, stage)

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

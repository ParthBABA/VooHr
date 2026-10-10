"""Phase 3 — deterministic reminder & memory-surfacing layer for meetings.

Hard rules (they are enforced here, not just intended):
  * We never invent facts. Every surfaced item is an actual record that already
    exists in ``conversation_memory`` — we only pick which stored items to show.
  * No AI, no behavioral conclusions, no auto-completion, no generic advice.
  * Reminder uniqueness is ``meeting_id + memory_id + stage`` (NOT the text).
  * Surfaced set is computed deterministically and scoped to the org.

A surfaced item is one of:
  - PENDING / IN_PROGRESS / OVERDUE commitment
  - PENDING / IN_PROGRESS / OVERDUE follow-up
  - SAVED (not-yet-used) opener
  - SAVED question
  - other explicitly SAVED item (e.g. NOTE)
  - archived records are never surfaced

An item stops being surfaced the moment it is explicitly COMPLETED or
CANCELLED (or, for openers/questions, explicitly USED) — nothing here changes
those states.
"""
import logging
import os
import threading
import time
from datetime import datetime, timezone, timedelta

from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, jsonify, request
from pymongo.errors import DuplicateKeyError

import email_service
import gmail_send
import whatsapp
from employees import _require_auth
from conversation_memory import is_ai_suggestion
from extensions import get_db
from field_encryption import decrypt_fields

logger = logging.getLogger(__name__)

reminders_bp = Blueprint("reminders", __name__)

_RELEVANT_STATUSES = {"PENDING", "SAVED", "IN_PROGRESS"}

# Stages for which Gmail (if connected) is attempted first; other stages fall back to Brevo.
GMAIL_REMINDER_STAGES = {"soon_1h", "day_of", "upcoming_24h"}

# A meeting that is still "scheduled" this far past its scheduled time is
# treated as missed (swept to "missed" by meetings_dashboard) and generates no
# reminder stage. Shared by reminders.stage_for (past cutoff) and meetings.py
# (sweep threshold) so the two stay consistent.
MEETING_MISSED_GRACE = timedelta(hours=2)

# Out-of-app delivery retry policy (email/WhatsApp).  In-app notifications are
# the record itself and always "delivered"; only external channels are retried.
# Each failure backs off exponentially from REMINDER_BACKOFF_BASE, capped at
# REMINDER_BACKOFF_CAP, and stops after REMINDER_MAX_ATTEMPTS — at which point a
# single "delivery failed" notification is recorded for the owner.
REMINDER_MAX_ATTEMPTS = int(os.environ.get("REMINDER_MAX_ATTEMPTS", "5"))
REMINDER_BACKOFF_BASE = timedelta(minutes=5)
REMINDER_BACKOFF_CAP = timedelta(hours=6)


def _aware(dt):
    """Mongo returns naive UTC datetimes; normalize before comparing."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

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
    if t == "COMMITMENT" and s in ("PENDING", "IN_PROGRESS"):
        return (2,)
    if t == "FOLLOW_UP" and s in ("PENDING", "IN_PROGRESS"):
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
        if m.get("archive"):
            continue
        # An unconfirmed AI suggestion is never surfaced: nobody has vouched
        # for it yet, so it must not appear as a fact in front of HR or reach
        # the employee over email/WhatsApp. It stays visible only in the
        # Meeting Tracker's "AI suggestions" block until it is confirmed.
        if is_ai_suggestion(m):
            continue
        eid = str(m.get("employee_id"))
        if eid not in upcoming_emp_ids:
            continue
        mt = m.get("type")
        status = m.get("status") or "SAVED"
        due = m.get("due_at")
        effective = status
        if (
            mt in ("COMMITMENT", "FOLLOW_UP")
            and status in ("PENDING", "IN_PROGRESS")
            and due is not None
            and _aware(due) < now
        ):
            effective = "OVERDUE"

        if mt in ("COMMITMENT", "FOLLOW_UP"):
            # Only actionable open commitments/follow-ups are surfaced.
            if effective not in ("PENDING", "IN_PROGRESS", "OVERDUE"):
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

    Returns None for a meeting already well in the past (more than
    ``MEETING_MISSED_GRACE`` before ``now``): a "meeting starts in an hour"
    reminder for something that happened weeks ago is misleading and never
    generated.
    """
    if meeting_time is None:
        return None
    meeting_time = _aware(meeting_time)
    delta = meeting_time - now
    if delta < -MEETING_MISSED_GRACE:
        return None
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
    email = (user.get("email") or "").strip()
    if email:
        return email or None
    try:
        pii = decrypt_fields(user.get("encrypted"), user.get("wrapped_dek", ""))
        email = (pii.get("email") or "").strip()
    except Exception:
        logger.warning(
            "meeting reminder email skipped: owner email could not be resolved "
            "(decryption failed) user=%s",
            user.get("_id"),
        )
        return None
    return email or None


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


def _build_reminder_email(employee_name: str, meeting_time, items_summaries: list, stage: str) -> tuple[str, str, str]:
    """Build subject, HTML, and plain text for a meeting reminder email.

    This mirrors the content generated by email_service._reminder_html and
    _REMINDER_STAGE_SUBJECT/INTRO but returns the parts separately so we can
    send via Gmail API (which needs subject + html + text) or Brevo.

    items_summaries is a list of strings (may be empty). Each string is an
    already-formatted summary like "commitment pending: ship the handoff notes".
    """
    from email_service import _REMINDER_STAGE_SUBJECT, _REMINDER_STAGE_INTRO, _format_meeting_time, _email_footer, _escape_html, _site_base_url

    subject = _REMINDER_STAGE_SUBJECT.get(
        stage, _REMINDER_STAGE_SUBJECT["day_of"]
    ).format(employee=employee_name or "your colleague")

    intro = _REMINDER_STAGE_INTRO.get(
        stage, _REMINDER_STAGE_INTRO["day_of"]
    ).format(employee=_escape_html(employee_name or "your colleague"))
    when = _format_meeting_time(meeting_time)
    base = _site_base_url()
    meeting_url = f"{base}/meeting-tracker" if base else "/meeting-tracker"

    items_html = ""
    items_text = ""
    if items_summaries:
        items_html = "<p><b>Open commitments & follow-ups:</b></p><ul>" + "".join(
            f"<li>{_escape_html(s)}</li>" for s in items_summaries
        ) + "</ul>"
        items_text = "\n".join(f"- {s}" for s in items_summaries)

    html = (
        "<p>" + intro + "</p>"
        f"<p><b>Scheduled:</b> {_escape_html(when)}</p>"
        + items_html
        + f"<p style=\"margin:24px 0;\"><a href=\"{meeting_url}\" "
        "style=\"background:#f5b301;color:#121212;text-decoration:none;"
        "padding:12px 22px;border-radius:8px;font-weight:600;display:inline-block;\">"
        "Open Meeting Tracker</a></p>"
        "<p>You're receiving this because you scheduled this meeting in VooVr. "
        "You can turn these emails off anytime in Settings &rarr; Notifications.</p>"
        + _email_footer()
    )

    text_parts = [
        intro,
        f"Scheduled: {when}",
    ]
    if items_text:
        text_parts.append("Open commitments & follow-ups:")
        text_parts.append(items_text)
    text_parts.extend([
        f"Details: {meeting_url}",
        "You're receiving this because you scheduled this meeting in VooVr. "
        "You can turn these emails off anytime in Settings -> Notifications.",
    ])
    text_parts.append("View our Privacy Policy: {}/privacy | Terms of Service: {}/terms".format(base, base))

    text = "\n".join(text_parts)

    return subject, html, text


def _meeting_page_url() -> str:
    """Deep link to the Meeting Tracker board (the page that surfaces meetings
    and their pending follow-ups)."""
    base = (
        os.environ.get("CLIENT_URL") or os.environ.get("SITE_URL") or ""
    ).strip().rstrip("/")
    return f"{base}/meeting-tracker" if base else "/meeting-tracker"


def _whatsapp_reminder_text(employee_name, meeting_time, items: list, stage) -> str:
    """Terse WhatsApp-style reminder body (not the full email body).

    items is a list of surfaced item dicts (may be empty).
    """
    when = meeting_time.strftime("%A %d %b · %I:%M %p") if meeting_time else "soon"
    base = f"VooVr · {employee_name or 'Your'} meeting : {when} ({stage})."
    if items:
        return f"{base} Open items: {len(items)}. Details: {_meeting_page_url()}"
    return f"{base} Details: {_meeting_page_url()}"


def _send_reminder_whatsapp(to_phone: str, text: str) -> bool:
    """Send a reminder text via the WhatsApp Cloud API helper.

    Returns True/False; every failure is logged and swallowed by the caller
    (a bad number or dead provider must never block reminder generation).
    """
    if not to_phone:
        return False
    if not os.environ.get("WHATSAPP_ACCESS_TOKEN"):
        logger.debug(
            "reminder_whatsapp=skipped reason=no_whatsapp_token phone_set=%s",
            bool(to_phone),
        )
        return False
    return whatsapp.send_message(to_phone, text)


def _deliver_reminder_channels(db, org_id, meeting, items: list, stage):
    """Send email + WhatsApp for one meeting reminder and report per-channel status.

    items is a list of surfaced item dicts (may be empty). The reminder is
    always created in-app; external channels are best-effort.

    Every failure is logged and swallowed — a bad address or a dead provider
    must never block the in-app notification or crash reminder generation.
    The caller records ``delivery_status`` on the notification so the sweep
    can retry failed external delivery with bounded backoff.

    Returns ``{"ok", "wanted", "email_sent", "whatsapp_sent", "errors"}``:
      ``wanted``   — how many external channels had a destination (0 → ok).
      ``ok``       — all wanted channels succeeded (or none were wanted).
      ``errors``   — short human reasons for what failed (for notifications).
    """
    user = _meeting_owner(db, org_id, meeting)
    if not user:
        logger.info(
            "meeting_reminder_email status=skipped reason=owner_not_found "
            "meeting_id=%s org_id=%s stage=%s",
            meeting.get("_id"), org_id, stage,
        )
        return {"ok": True, "wanted": 0, "email_sent": False,
                "whatsapp_sent": False, "errors": []}
    if not _meeting_reminders_enabled(user):
        logger.info(
            "meeting_reminder_email status=skipped reason=opted_out "
            "meeting_id=%s org_id=%s stage=%s",
            meeting.get("_id"), org_id, stage,
        )
        return {"ok": True, "wanted": 0, "email_sent": False,
                "whatsapp_sent": False, "errors": []}

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

    meeting_time = meeting.get("scheduled_at")

    # Build item summaries for email/WhatsApp
    items_summaries = [_reminder_summary(it) for it in items] if items else []

    email_sent = False
    email_channel = "brevo"
    whatsapp_sent = False
    errors = []

    owner_email = _user_email(user)
    if owner_email:
        subject, html, text = _build_reminder_email(employee_name, meeting_time, items_summaries, stage)

        use_gmail = (
            stage in GMAIL_REMINDER_STAGES
            and user.get("gmail", {}).get("status") == "connected"
        )

        if use_gmail:
            try:
                email_sent = bool(gmail_send.send_html(user, subject, html, text))
                email_channel = "gmail"
            except Exception:
                logger.exception(
                    "meeting_reminder_gmail status=failed stage=%s meeting_id=%s org_id=%s",
                    stage, meeting.get("_id"), org_id,
                )
                email_sent = False

        if not email_sent:
            try:
                email_sent = bool(email_service.send_reminder_email(
                    owner_email, employee_name, meeting_time, items_summaries, stage
                ))
                email_channel = "brevo"
            except Exception:
                logger.exception(
                    "meeting_reminder_email status=failed stage=%s meeting_id=%s org_id=%s",
                    stage, meeting.get("_id"), org_id,
                )
                email_sent = False

        if email_sent:
            logger.info(
                "meeting_reminder_email status=sent stage=%s meeting_id=%s org_id=%s channel=%s",
                stage, meeting.get("_id"), org_id, email_channel,
            )
        else:
            errors.append("email")
    else:
        logger.info(
            "meeting_reminder_email status=skipped reason=email_unavailable "
            "stage=%s meeting_id=%s org_id=%s",
            stage, meeting.get("_id"), org_id,
        )

    phone = _reminder_phone_number(user)
    if phone:
        # Build when_phrase per stage
        if stage == "soon_1h":
            when_phrase = "in about an hour"
        elif stage == "day_of":
            if meeting_time:
                when_phrase = "today at " + meeting_time.strftime("%I:%M %p").lstrip('0')
            else:
                when_phrase = "today"
        elif stage == "upcoming_24h":
            if meeting_time:
                from datetime import datetime, timezone, timedelta
                now_local = datetime.now(timezone.utc).astimezone()
                if meeting_time.date() == (now_local + timedelta(days=1)).date():
                    when_phrase = "tomorrow at " + meeting_time.strftime("%I:%M %p").lstrip('0')
                else:
                    when_phrase = "on " + meeting_time.strftime("%a %d %b at %I:%M %p").lstrip('0')
            else:
                when_phrase = "soon"
        else:
            when_phrase = "soon"

        # Build items_line
        if items:
            items_line = f"You have {len(items)} open commitment(s) or follow-up(s) to review."
        else:
            items_line = "No open items right now."

        try:
            whatsapp_sent = bool(whatsapp.send_reminder_template(
                phone, employee_name, when_phrase, items_line
            ))
        except Exception:
            logger.exception(
                "reminder_whatsapp=failed meeting=%s stage=%s phone_set=True",
                meeting.get("_id"), stage,
            )
            errors.append("whatsapp")
        if not whatsapp_sent:
            errors.append("whatsapp")

    wanted = (1 if owner_email else 0) + (1 if phone else 0)
    if wanted == 0:
        ok = True
    else:
        ok = (email_sent or not owner_email) and (whatsapp_sent or not phone)
    return {"ok": ok, "wanted": wanted, "email_sent": email_sent,
            "whatsapp_sent": whatsapp_sent, "errors": list(dict.fromkeys(errors))}


def _deliver_reminder(db, org_id, meeting, items: list, stage):
    """Deprecated-thin wrapper retained for callers that only want the side
    effect: deliver on a best-effort basis and swallow all failures."""
    try:
        return _deliver_reminder_channels(db, org_id, meeting, items, stage)
    except Exception:
        logger.exception(
            "reminder_delivery=failed meeting=%s stage=%s",
            meeting.get("_id"), stage,
        )
        return {"ok": False, "wanted": 1, "email_sent": False,
                "whatsapp_sent": False, "errors": ["unexpected"]}


def _backoff_for_attempt(attempts: int) -> timedelta:
    return min(REMINDER_BACKOFF_CAP, REMINDER_BACKOFF_BASE * (2 ** attempts))


def _modified_count(res):
    try:
        return getattr(res, "modified_count", None)
    except Exception:
        return None


def _note_delivery_failure(db, org_id, meeting, reminder_nid, stage, now):
    """Record one 'external delivery exhausted' notification per reminder so
    the owner learns an email/WhatsApp ping never made it out. Idempotent via
    the event_key dedup; the in-app notification itself is always delivered."""
    if db.notifications.find_one({
        "org_id": ObjectId(org_id),
        "type": "delivery_failed",
        "event_key": f"delivery_failed:{reminder_nid}",
    }):
        return
    db.notifications.insert_one({
        "org_id": ObjectId(org_id),
        "type": "delivery_failed",
        "headline": "A reminder could not be delivered",
        "summary": "Email/WhatsApp delivery failed repeatedly for a meeting reminder.",
        "confidence": 0,
        "employee_id": meeting.get("employee_id"),
        "source_session_id": None,
        "meeting_id": meeting["_id"],
        "memory_id": None,
        "stage": stage,
        "event_key": f"delivery_failed:{reminder_nid}",
        "recipient_user_id": None,
        "read": False,
        "dismissed": False,
        "created_at": now,
    })


def _deliver_and_record(db, org_id, meeting, stage, now):
    """First delivery attempt + delivery_status bookkeeping for a fresh
    meeting-level reminder notification."""
    notification = db.notifications.find_one({
        "org_id": ObjectId(org_id),
        "meeting_id": meeting["_id"],
        "memory_id": None,
        "stage": stage,
        "type": "meeting_reminder",
    })
    if not notification:
        return

    # Load items from item_ids stored on the notification
    item_ids = notification.get("item_ids", [])
    items = []
    if item_ids:
        items = list(db.conversation_memory.find({
            "_id": {"$in": item_ids},
            "org_id": ObjectId(org_id),
        }))

    status = _deliver_reminder_channels(db, org_id, meeting, items, stage)
    fields = {
        "delivery_status": "delivered" if status["ok"] else "failed",
        "last_attempt_at": now,
        "delivery_errors": status["errors"],
    }
    if status["ok"]:
        fields["next_attempt_at"] = None
    else:
        attempts = notification.get("attempts") or 0
        fields["next_attempt_at"] = now + _backoff_for_attempt(attempts)
        if (attempts + 1) >= REMINDER_MAX_ATTEMPTS:
            fields["next_attempt_at"] = None
            _note_delivery_failure(db, org_id, meeting, notification["_id"], stage, now)
    db.notifications.update_one({"_id": notification["_id"]}, {"$set": fields})


def retry_pending_deliveries(db, org_id, now=None) -> int:
    """Retry failed/pending out-of-app reminder delivery.

    Attempts are claimed with a compare-and-swap on ``attempts`` so concurrent
    workers cannot double-deliver the same notification: exactly one worker
    wins each claim; the others skip to the next document.  Delivery never
    blocks — `next_attempt_at` is pushed out before sending so a crashed
    worker still gets a bounded retry.

    For meeting-level reminders (memory_id=None), items are loaded from
    notification.item_ids. For per-item reminders (memory_id set), the single
    memory item is loaded. Neither case skips due to empty items.
    """
    now = now or _now()
    org_oid = ObjectId(org_id)
    pending = list(db.notifications.find({
        "org_id": org_oid,
        "type": "meeting_reminder",
        "delivery_status": {"$in": ["pending", "retrying", "failed"]},
    }))
    retried = 0
    for n in pending:
        attempts = n.get("attempts") or 0
        if attempts >= REMINDER_MAX_ATTEMPTS:
            continue
        nxt = _aware(n.get("next_attempt_at"))
        if nxt is not None and nxt > now:
            continue
        res = db.notifications.update_one(
            {"_id": n["_id"], "attempts": attempts},
            {"$set": {
                "attempts": attempts + 1,
                "delivery_status": "retrying",
                "next_attempt_at": now + _backoff_for_attempt(attempts),
                "last_attempt_at": now,
            }},
        )
        if _modified_count(res) != 1:
            continue

        meeting = db.meetings.find_one({"_id": n.get("meeting_id"), "org_id": org_oid})
        if not meeting:
            continue

        # Load items: for meeting-level reminders use item_ids, for per-item reminders use memory_id
        items = []
        if n.get("memory_id"):
            # Legacy per-item reminder
            memory = db.conversation_memory.find_one(
                {"_id": n["memory_id"], "org_id": org_oid}
            )
            if memory:
                items = [memory]
        else:
            # Meeting-level reminder
            item_ids = n.get("item_ids", [])
            if item_ids:
                items = list(db.conversation_memory.find({
                    "_id": {"$in": item_ids},
                    "org_id": org_oid,
                }))

        status = _deliver_reminder_channels(db, org_id, meeting, items, n.get("stage"))
        fields = {
            "delivery_status": "delivered" if status["ok"] else "failed",
            "delivery_errors": status["errors"],
        }
        if status["ok"]:
            fields["next_attempt_at"] = None
        elif (attempts + 1) >= REMINDER_MAX_ATTEMPTS:
            fields["next_attempt_at"] = None
            _note_delivery_failure(db, org_id, meeting, n["_id"], n.get("stage"), now)
        db.notifications.update_one({"_id": n["_id"]}, {"$set": fields})
        retried += 1
    return retried


def ensure_due_notifications(db, org_id, now=None) -> int:
    """Create at most ONE 'memory overdue' notification per commitment/follow-up
    the first time its due date passes unresolved. Idempotent (dedup by memory)
    and never auto-advances the record's own status — PENDING becoming OVERDUE
    is still a read-time derivation."""
    now = now or _now()
    org_oid = ObjectId(org_id)
    created = 0
    memory = list(db.conversation_memory.find({"org_id": org_oid}))
    for m in memory:
        if m.get("archive"):
            continue
        # Same rule as surfacing: a suggestion nobody confirmed can never
        # generate an "overdue" notification about a promise that may not exist.
        if is_ai_suggestion(m):
            continue
        mt = m.get("type")
        if mt not in ("COMMITMENT", "FOLLOW_UP"):
            continue
        status = m.get("status")
        if status not in ("PENDING", "IN_PROGRESS"):
            continue
        due = m.get("due_at")
        if due is None or _aware(due) >= now:
            continue
        if db.notifications.find_one({
            "org_id": org_oid,
            "type": "memory_overdue",
            "memory_id": m["_id"],
        }):
            continue
        try:
            db.notifications.insert_one({
                "org_id": org_oid,
                "type": "memory_overdue",
                "headline": "An item is overdue",
                "summary": f"{_TYPE_LABEL.get(mt, mt)}: {m.get('content', '')}",
                "confidence": 0,
                "employee_id": m.get("employee_id"),
                "source_session_id": None,
                "meeting_id": None,
                "memory_id": m["_id"],
                "stage": "overdue_due",
                "event_key": f"overdue:{m['_id']}",
                "recipient_user_id": None,
                "read": False,
                "dismissed": False,
                "created_at": now,
            })
        except DuplicateKeyError:
            continue
        created += 1
    return created


def sweep_all_orgs(db, now=None) -> dict:
    """One daemon-sweep pass over every org that needs reminders.

    Idempotent end-to-end (unique indexes + find_one dedup guards), so any
    number of workers may run it safely. Each per-org step is individually
    guarded so one org's failure never aborts the rest of the pass.
    """
    now = now or _now()
    org_ids: set = set()
    try:
        for m in db.meetings.find({"status": "scheduled"}):
            oid = m.get("org_id")
            if oid is not None:
                org_ids.add(str(oid))
    except Exception:
        logger.exception("reminder sweep: meeting org scan failed")
    try:
        for n in db.notifications.find({
            "type": "meeting_reminder",
            "delivery_status": {"$in": ["pending", "retrying", "failed"]},
        }):
            oid = n.get("org_id")
            if oid is not None:
                org_ids.add(str(oid))
    except Exception:
        logger.exception("reminder sweep: notification org scan failed")

    created = retried = due = 0
    for org_id in sorted(org_ids):
        try:
            created += ensure_reminder_notifications(db, org_id, now)
        except Exception:
            logger.exception("reminder sweep: generation failed org=%s", org_id)
        try:
            retried += retry_pending_deliveries(db, org_id, now)
        except Exception:
            logger.exception("reminder sweep: retry failed org=%s", org_id)
        try:
            due += ensure_due_notifications(db, org_id, now)
        except Exception:
            logger.exception("reminder sweep: due notifications failed org=%s", org_id)

    if created or retried or due:
        logger.info(
            "reminder sweep done orgs=%d created=%d retried=%d due=%d",
            len(org_ids), created, retried, due,
        )
    return {"orgs": len(org_ids), "created": created, "retried": retried, "due": due}


# ── Background daemon (env-gated, mirrors jobs.py's thread pattern) ──────

_SWEEP_LOCK = threading.Lock()
_SWEEP_THREAD = None


def _sweep_enabled() -> bool:
    return os.environ.get("REMINDER_SWEEP_ENABLED", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _sweep_interval_seconds() -> int:
    try:
        return max(int(os.environ.get("REMINDER_SWEEP_INTERVAL_SECONDS", "300")), 5)
    except ValueError:
        return 300


def start_reminder_sweep(app):
    """Start the daemon reminder sweep (single thread per process).

    Multi-worker safe: reminder uniqueness is enforced by a partial unique
    index and retry attempts are claimed via compare-and-swap, so at most one
    worker ever delivers a given reminder.  Exceptions are logged and the loop
    keeps going — a failing org or dead provider must not take the sweep down.
    """
    if not _sweep_enabled():
        logger.info("reminder sweep disabled via REMINDER_SWEEP_ENABLED")
        return
    with _SWEEP_LOCK:
        global _SWEEP_THREAD
        if _SWEEP_THREAD is not None and _SWEEP_THREAD.is_alive():
            return

        def _run():
            interval = _sweep_interval_seconds()
            while True:
                started = time.monotonic()
                try:
                    with app.app_context():
                        sweep_all_orgs(get_db())
                except Exception:
                    logger.exception("reminder sweep: iteration failed")
                elapsed = time.monotonic() - started
                time.sleep(max(interval - elapsed, 1))

        _SWEEP_THREAD = threading.Thread(
            target=_run, name="reminder-sweep", daemon=True
        )
        _SWEEP_THREAD.start()
        logger.info("reminder sweep started interval=%ds", _sweep_interval_seconds())


def ensure_reminder_notifications(db, org_id, now=None) -> int:
    """Idempotently create reminder notifications for reachable upcoming
    meetings.

    One notification per (org, meeting_id, stage) with memory_id=None.
    Items are optional detail — a meeting with no items still gets a reminder.
    Re-running is a no-op for existing keys. ``now`` is injectable for tests.
    """
    now = now or _now()
    org_oid = ObjectId(org_id)

    meetings = list(db.meetings.find({"org_id": org_oid, "status": "scheduled"}))
    memory = list(db.conversation_memory.find(
        {"org_id": org_oid, "status": {"$in": list(_RELEVANT_STATUSES)}}
    ))

    # Build upcoming meetings map by employee_id
    upcoming_by_emp: dict = {}
    for m in meetings:
        scheduled = _aware(m.get("scheduled_at"))
        if scheduled and stage_for(scheduled, now) is not None:
            upcoming_by_emp[str(m.get("employee_id"))] = m

    surfaces = surface_items(memory, set(upcoming_by_emp.keys()), now)

    created = 0
    for meeting in meetings:
        scheduled = _aware(meeting.get("scheduled_at"))
        stage = stage_for(scheduled, now)
        if stage is None:
            continue

        # Check if meeting-level reminder already exists
        existing = db.notifications.find_one({
            "org_id": org_oid,
            "meeting_id": meeting["_id"],
            "memory_id": None,
            "stage": stage,
            "type": "meeting_reminder",
        })
        if existing:
            continue

        # Get items for this employee (may be empty)
        eid = str(meeting.get("employee_id"))
        items = surfaces.get(eid, [])
        item_ids = [ObjectId(it["id"]) for it in items]

        # Build summary from items
        summary = ""
        if items:
            summary = _reminder_summary(items[0])
            if len(items) > 1:
                summary += f" + {len(items) - 1} more"
        else:
            summary = "No open commitments or follow-ups."

        try:
            db.notifications.insert_one({
                "org_id": org_oid,
                "type": "meeting_reminder",
                "headline": f"Before {'this' if stage == 'day_of' else 'your next'} meeting",
                "summary": summary,
                "confidence": 0,
                "employee_id": meeting["employee_id"],
                "source_session_id": None,
                "meeting_id": meeting["_id"],
                "memory_id": None,
                "stage": stage,
                "event_key": f"reminder:{meeting['_id']}:{stage}",
                "recipient_user_id": None,
                "delivery_status": "pending",
                "delivery_channel": ["in_app", "email", "whatsapp"],
                "delivery_errors": [],
                "attempts": 0,
                "next_attempt_at": None,
                "last_attempt_at": None,
                "read": False,
                "dismissed": False,
                "created_at": now,
                "item_ids": item_ids,
            })
        except DuplicateKeyError:
            # Another worker created the same (org, meeting, stage) reminder
            continue

        created += 1
        _deliver_and_record(db, org_id, meeting, stage, now)

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

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
import whatsapp
from employees import _require_auth
from extensions import get_db
from field_encryption import decrypt_fields

logger = logging.getLogger(__name__)

reminders_bp = Blueprint("reminders", __name__)

_RELEVANT_STATUSES = {"PENDING", "SAVED", "IN_PROGRESS"}

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


def _deliver_reminder_channels(db, org_id, meeting, it, stage):
    """Send email + WhatsApp for one reminder and report per-channel status.

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
        return {"ok": True, "wanted": 0, "email_sent": False,
                "whatsapp_sent": False, "errors": []}
    if not _meeting_reminders_enabled(user):
        logger.debug(
            "reminder_email=skipped reason=opt_out user=%s meeting=%s stage=%s",
            user.get("_id"), meeting.get("_id"), stage,
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

    summary = _reminder_summary(it)
    meeting_time = meeting.get("scheduled_at")

    email_sent = False
    whatsapp_sent = False
    errors = []

    owner_email = _user_email(user)
    if owner_email:
        try:
            email_service.send_reminder_email(
                owner_email, employee_name, meeting_time, summary, stage
            )
            email_sent = True
        except Exception:
            logger.exception(
                "reminder_email=failed meeting=%s stage=%s recipient=%s",
                meeting.get("_id"), stage, owner_email,
            )
            errors.append("email")

    phone = _reminder_phone_number(user)
    if phone:
        try:
            whatsapp_sent = bool(_send_reminder_whatsapp(
                phone, _whatsapp_reminder_text(employee_name, meeting_time, summary, stage)
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


def _deliver_reminder(db, org_id, meeting, it, stage):
    """Deprecated-thin wrapper retained for callers that only want the side
    effect: deliver on a best-effort basis and swallow all failures."""
    try:
        return _deliver_reminder_channels(db, org_id, meeting, it, stage)
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


def _deliver_and_record(db, org_id, meeting, it, stage, now):
    """First delivery attempt + delivery_status bookkeeping for a fresh
    reminder notification."""
    notification = db.notifications.find_one({
        "org_id": ObjectId(org_id),
        "meeting_id": meeting["_id"],
        "memory_id": ObjectId(it["id"]),
        "stage": stage,
    })
    if not notification:
        return
    status = _deliver_reminder_channels(db, org_id, meeting, it, stage)
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
        memory = None
        if n.get("memory_id"):
            memory = db.conversation_memory.find_one(
                {"_id": n["memory_id"], "org_id": org_oid}
            )
        if not meeting or not memory:
            continue

        status = _deliver_reminder_channels(db, org_id, meeting, memory, n.get("stage"))
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
        scheduled = _aware(m.get("scheduled_at"))
        if scheduled and stage_for(scheduled, now) is not None:
            upcoming_by_emp[str(m.get("employee_id"))] = m

    surfaces = surface_items(memory, set(upcoming_by_emp.keys()), now)

    created = 0
    for eid, items in surfaces.items():
        meeting = upcoming_by_emp.get(eid)
        if not meeting:
            continue
        stage = stage_for(_aware(meeting["scheduled_at"]), now)
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
            try:
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
                    "event_key": f"reminder:{meeting['_id']}:{memory_oid}:{stage}",
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
                })
            except DuplicateKeyError:
                # Another worker created the same (org, meeting, memory, stage)
                # reminder between our find_one and insert — treat as success.
                continue
            created += 1
            _deliver_and_record(db, org_id, meeting, it, stage, now)

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

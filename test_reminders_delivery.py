"""
Reminder delivery channels — email + (stubbed) WhatsApp on top of the
in-app reminder system.

Verifies (hand-rolled in-memory Mongo facade + patched outbound channels):
  - one email send per (meeting, stage) at every stage, with the
    right stage, recipient, employee name and reminder summary
  - WhatsApp is skipped when the owning user has no phone_number
  - WhatsApp is attempted when a phone_number is present (stub is invoked)
  - the user-level opt-out (notification_prefs.meeting_reminders=False)
    suppresses email/WhatsApp but STILL creates the in-app notification
  - the send_reminder_email() return value is honored: a False result is
    recorded as delivery_status=failed (not delivered) and is retried on a
    later sweep; a skip/owner_unavailable is terminal and logged with reason
  - meeting with NO items still creates 1 notification and sends 1 email
  - meeting with multiple items creates 1 notification with all items in email body
"""
import logging
from datetime import datetime, timedelta, timezone
from unittest import mock

from bson import ObjectId

# reminders -> employees -> config requires SECRET_KEY at import time.
import os
os.environ.setdefault("SECRET_KEY", "test-secret-key")

import reminders as rm_mod
import email_service as email_mod

ORG_A = "aaaaaaaaaaaaaaaaaaaaaaaa"
EMP_1 = "111111111111111111111111"
SESSION_1 = "333333333333333333333333"
OWNER = "999999999999999999999999"

NOW = datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc)  # a Wednesday


class FakeCollection:
    def __init__(self):
        self._docs = []

    def _match(self, doc, filt):
        for k, v in filt.items():
            if isinstance(v, dict) and "$ne" in v:
                if doc.get(k) == v["$ne"]:
                    return False
            elif isinstance(v, dict) and "$in" in v:
                if doc.get(k) not in v["$in"]:
                    return False
            elif doc.get(k) != v:
                return False
        return True

    def _strip(self, v):
        if isinstance(v, dict):
            return {k: self._strip(x) for k, x in v.items()}
        if isinstance(v, list):
            return [self._strip(x) for x in v]
        if isinstance(v, datetime):
            return v if v.tzinfo is None else v.astimezone(timezone.utc).replace(tzinfo=None)
        return v

    def find_one(self, filt, *args, **kw):
        for d in self._docs:
            if self._match(d, filt):
                return dict(d)
        return None

    def find(self, filt=None, **kw):
        filt = filt or {}
        return [d for d in self._docs if self._match(d, filt)]

    def insert_one(self, doc):
        d = self._strip(dict(doc))
        d["_id"] = d.get("_id") or ObjectId()
        self._docs.append(d)
        return type("R", (), {"inserted_id": d["_id"]})()

    def update_one(self, filt, update):
        for d in self._docs:
            if self._match(d, filt):
                if "$set" in update:
                    d.update(self._strip(update["$set"]))
                return type("R", (), {"matched_count": 1, "modified_count": 1})()
        return type("R", (), {"matched_count": 0, "modified_count": 0})()

    def count_documents(self, filt):
        return sum(1 for d in self._docs if self._match(d, filt))


class FakeDB:
    def __init__(self):
        self.meetings = FakeCollection()
        self.conversation_memory = FakeCollection()
        self.employees = FakeCollection()
        self.sessions = FakeCollection()
        self.notifications = FakeCollection()
        self.users = FakeCollection()


def _seed(db, owner_email="hr@voovr.com", phone=None, prefs=None):
    db.employees.insert_one({
        "_id": ObjectId(EMP_1), "employee_id": "EMP001", "name": "Harshit Rana",
        "position": "Product Designer", "department": "Design",
        "org_id": ObjectId(ORG_A), "status": "active",
    })
    db.sessions.insert_one({
        "_id": ObjectId(SESSION_1), "org_id": ObjectId(ORG_A),
        "employee_id": ObjectId(EMP_1), "status": "completed",
        "created_at": datetime(2026, 8, 29, tzinfo=timezone.utc),
    })
    owner = {
        "_id": ObjectId(OWNER), "role": "admin", "org_id": ObjectId(ORG_A),
        "email": owner_email,
    }
    if phone:
        owner["phone_number"] = phone
    if prefs is not None:
        owner["notification_prefs"] = prefs
    db.users.insert_one(owner)


def _add_meeting(db, scheduled_at):
    st = datetime.fromisoformat(scheduled_at)
    if st.tzinfo is None:
        st = st.replace(tzinfo=timezone.utc)
    r = db.meetings.insert_one({
        "org_id": ObjectId(ORG_A), "employee_id": ObjectId(EMP_1),
        "title": "1:1", "scheduled_at": st, "status": "scheduled",
        "session_id": None, "created_by": ObjectId(OWNER),
        "created_at": datetime(2026, 8, 28, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 8, 28, tzinfo=timezone.utc),
    })
    return r.inserted_id


def _add_memory(db, content="ship the handoff notes"):
    r = db.conversation_memory.insert_one({
        "org_id": ObjectId(ORG_A), "employee_id": ObjectId(EMP_1),
        "session_id": ObjectId(SESSION_1), "type": "COMMITMENT",
        "content": content, "status": "PENDING",
        "due_at": datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc),
        "used_at": None, "completed_at": None, "usage_count": 0, "usage": [],
        "created_at": datetime(2026, 8, 29, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 8, 29, tzinfo=timezone.utc),
    })
    return r.inserted_id


def _generate(db, now=NOW):
    return rm_mod.ensure_reminder_notifications(db, ORG_A, now)


# ── One email per EXTERNAL stage ────────────────────────────────────────

def test_email_sent_once_per_external_stage(monkeypatch):
    """External delivery (email/WhatsApp) only for upcoming_24h and soon_1h.
    day_of creates in-app notification only: no email, no WhatsApp."""
    cases = {
        "soon_1h": "2026-08-30T09:30:00",        # within the hour
        "upcoming_24h": "2026-08-31T05:00:00",   # next day, < 24h out
    }
    for stage, meeting_at in cases.items():
        db = FakeDB()
        _seed(db)
        _add_memory(db)
        _add_meeting(db, meeting_at)

        send = mock.Mock(return_value=True)
        wa = mock.Mock(return_value=False)
        monkeypatch.setattr(email_mod, "send_reminder_email", send)
        monkeypatch.setattr(rm_mod, "_send_reminder_whatsapp", wa)

        created = _generate(db)

        assert created == 1, stage
        assert send.call_count == 1, stage
        email, emp_name, meeting_time, summaries, sent_stage, sent_tz = send.call_args.args
        assert email == "hr@voovr.com", stage
        assert emp_name == "Harshit Rana", stage
        assert sent_stage == stage, stage
        assert len(summaries) == 1, stage
        assert "ship the handoff notes" in summaries[0], stage
        # In-app notification still created alongside the email.
        assert db.notifications.count_documents(
            {"type": "meeting_reminder", "stage": stage}
        ) == 1, stage
        n = db.notifications.find_one({"type": "meeting_reminder", "stage": stage})
        assert n["delivery_status"] == "delivered", stage
        assert n["delivery_errors"] == [], stage
        # No phone number set → WhatsApp skipped.
        assert wa.call_count == 0, stage


def test_day_of_creates_notification_no_external_delivery(monkeypatch):
    """day_of stage creates in-app notification only, no email/WhatsApp."""
    db = FakeDB()
    _seed(db)
    _add_memory(db)
    _add_meeting(db, "2026-08-30T15:00:00")  # Same day as NOW (09:00)

    send = mock.Mock(return_value=True)
    wa = mock.Mock(return_value=False)
    monkeypatch.setattr(email_mod, "send_reminder_email", send)
    monkeypatch.setattr(rm_mod, "_send_reminder_whatsapp", wa)

    created = _generate(db)

    assert created == 1
    assert send.call_count == 0  # No email for day_of
    assert wa.call_count == 0    # No WhatsApp for day_of
    n = db.notifications.find_one({"type": "meeting_reminder", "stage": "day_of"})
    assert n is not None
    assert n["delivery_status"] == "delivered"
    assert n["delivery_channel"] == ["in_app"]
    assert n["delivery_errors"] == []


# ── WhatsApp gating ─────────────────────────────────────────────────────

def test_whatsapp_sent_when_phone_present(monkeypatch):
    db = FakeDB()
    _seed(db, phone="+919000000000")
    _add_memory(db)
    _add_meeting(db, "2026-08-30T09:30:00")  # soon_1h stage

    import whatsapp as wa_mod
    wa = mock.Mock(return_value=True)  # Return True to avoid error
    monkeypatch.setattr(email_mod, "send_reminder_email", mock.Mock(return_value=True))
    monkeypatch.setattr(wa_mod, "send_reminder_template", wa)

    _generate(db)

    assert wa.call_count == 1
    phone, employee_name, when_phrase, items_line = wa.call_args.args
    assert phone == "+919000000000"
    assert "open commitment(s) or follow-up(s)" in items_line
    assert when_phrase == "in about an hour"


# ── Opt-out preference ──────────────────────────────────────────────────

def test_opt_out_suppresses_email_but_keeps_in_app(monkeypatch):
    db = FakeDB()
    _seed(db, prefs={"meeting_reminders": False})
    _add_memory(db)
    _add_meeting(db, "2026-08-30T15:00:00")

    send = mock.Mock(return_value=True)
    wa = mock.Mock(return_value=False)
    monkeypatch.setattr(email_mod, "send_reminder_email", send)
    monkeypatch.setattr(rm_mod, "_send_reminder_whatsapp", wa)

    created = _generate(db)

    assert created == 1
    assert send.call_count == 0
    assert wa.call_count == 0
    assert db.notifications.count_documents({"type": "meeting_reminder"}) == 1


def test_opt_in_default_sends_email(monkeypatch):
    db = FakeDB()
    _seed(db)  # no notification_prefs at all → default opt-in
    _add_memory(db)
    _add_meeting(db, "2026-08-30T09:30:00")  # soon_1h stage

    send = mock.Mock(return_value=True)
    monkeypatch.setattr(email_mod, "send_reminder_email", send)
    monkeypatch.setattr(rm_mod, "_send_reminder_whatsapp", mock.Mock(return_value=False))

    _generate(db)

    assert send.call_count == 1
    email, emp_name, meeting_time, summaries, sent_stage, sent_tz = send.call_args.args
    assert len(summaries) == 1
    assert "ship the handoff notes" in summaries[0]


# ── Delivery never blocks generation ────────────────────────────────────

def test_email_failure_does_not_block_notification(monkeypatch):
    db = FakeDB()
    _seed(db)
    _add_memory(db)
    _add_meeting(db, "2026-08-30T15:00:00")

    def boom(*a, **k):
        raise RuntimeError("provider down")
    monkeypatch.setattr(email_mod, "send_reminder_email", boom)

    created = _generate(db)

    assert created == 1
    assert db.notifications.count_documents({"type": "meeting_reminder"}) == 1


def test_meeting_without_owner_skips_delivery(monkeypatch):
    db = FakeDB()
    _seed(db)
    _add_memory(db)
    r = _add_meeting(db, "2026-08-30T15:00:00")
    # Legacy pre-created_by meeting.
    db.meetings.update_one({"_id": r}, {"$set": {"created_by": None}})

    send = mock.Mock(return_value=True)
    monkeypatch.setattr(email_mod, "send_reminder_email", send)
    monkeypatch.setattr(rm_mod, "_send_reminder_whatsapp", mock.Mock(return_value=False))

    created = _generate(db)

    assert created == 1
    assert send.call_count == 0
    assert db.notifications.count_documents({"type": "meeting_reminder"}) == 1


def test_email_false_is_recorded_failed_with_retry_ready(monkeypatch):
    db = FakeDB()
    _seed(db)
    _add_memory(db)
    _add_meeting(db, "2026-08-30T09:30:00")  # soon_1h stage

    monkeypatch.setattr(email_mod, "send_reminder_email", mock.Mock(return_value=False))
    monkeypatch.setattr(rm_mod, "_send_reminder_whatsapp", mock.Mock(return_value=False))

    assert _generate(db) == 1
    n = db.notifications.find_one({"type": "meeting_reminder"})
    assert n is not None
    assert n["delivery_status"] == "failed"
    assert n["delivery_errors"] == ["email"]
    assert n["next_attempt_at"] is not None
    # The in-app notification itself is still created.
    assert db.notifications.count_documents({"type": "meeting_reminder"}) == 1


def test_failed_email_retried_and_delivered_on_next_sweep(monkeypatch):
    db = FakeDB()
    _seed(db)
    _add_memory(db)
    _add_meeting(db, "2026-08-30T09:30:00")  # soon_1h stage

    send = mock.Mock(side_effect=[False, True])
    monkeypatch.setattr(email_mod, "send_reminder_email", send)
    monkeypatch.setattr(rm_mod, "_send_reminder_whatsapp", mock.Mock(return_value=False))

    assert _generate(db) == 1
    n = db.notifications.find_one({"type": "meeting_reminder"})
    assert n["delivery_status"] == "failed"
    assert (n.get("attempts") or 0) == 0
    assert send.call_count == 1

    retried = rm_mod.retry_pending_deliveries(db, ORG_A, NOW + timedelta(minutes=6))
    assert retried == 1
    n = db.notifications.find_one({"type": "meeting_reminder"})
    assert n["delivery_status"] == "delivered"
    assert n["next_attempt_at"] is None
    assert n["attempts"] == 1
    assert n["delivery_errors"] == []
    assert send.call_count == 2


def test_send_reminder_email_missing_brevo_config_returns_false(monkeypatch):
    monkeypatch.delenv("BREVO_API_KEY", raising=False)
    monkeypatch.delenv("BREVO_SENDER_EMAIL", raising=False)
    monkeypatch.setattr(
        email_mod.requests,
        "post",
        mock.Mock(side_effect=AssertionError("network must not be called")),
    )
    ok = email_mod.send_reminder_email(
        "owner@example.com", "Harshit Rana", NOW, ["summary"], "day_of"
    )
    assert ok is False
    email_mod.requests.post.assert_not_called()


def test_meeting_with_no_items_creates_reminder_and_email(monkeypatch):
    """Test (a): meeting with NO items still creates 1 notification and sends 1 email."""
    db = FakeDB()
    _seed(db)
    # No memory items added
    _add_meeting(db, "2026-08-30T09:30:00")  # soon_1h stage

    send = mock.Mock(return_value=True)
    monkeypatch.setattr(email_mod, "send_reminder_email", send)
    monkeypatch.setattr(rm_mod, "_send_reminder_whatsapp", mock.Mock(return_value=False))

    created = _generate(db)

    assert created == 1
    assert send.call_count == 1
    email, emp_name, meeting_time, summaries, sent_stage, sent_tz = send.call_args.args
    assert summaries == []  # Empty list for no items
    assert db.notifications.count_documents({"type": "meeting_reminder"}) == 1


def test_meeting_with_multiple_items_creates_one_reminder_with_all_items(monkeypatch):
    """Test (b): meeting with 3 items creates 1 notification and 1 email with all 3 items."""
    db = FakeDB()
    _seed(db)
    # Add 3 memory items
    _add_memory(db, content="first commitment")
    _add_memory(db, content="second follow-up")
    _add_memory(db, content="third note")
    _add_meeting(db, "2026-08-30T09:30:00")  # soon_1h stage

    send = mock.Mock(return_value=True)
    monkeypatch.setattr(email_mod, "send_reminder_email", send)
    monkeypatch.setattr(rm_mod, "_send_reminder_whatsapp", mock.Mock(return_value=False))

    created = _generate(db)

    assert created == 1
    assert send.call_count == 1
    email, emp_name, meeting_time, summaries, sent_stage, sent_tz = send.call_args.args
    assert len(summaries) == 3
    assert any("first commitment" in s for s in summaries)
    assert any("second follow-up" in s for s in summaries)
    assert any("third note" in s for s in summaries)
    assert db.notifications.count_documents({"type": "meeting_reminder"}) == 1


def test_rerun_ensure_reminder_notifications_is_idempotent(monkeypatch):
    """Test (c): re-running ensure_reminder_notifications creates nothing new."""
    db = FakeDB()
    _seed(db)
    _add_memory(db)
    _add_meeting(db, "2026-08-30T09:30:00")  # soon_1h stage

    send = mock.Mock(return_value=True)
    monkeypatch.setattr(email_mod, "send_reminder_email", send)
    monkeypatch.setattr(rm_mod, "_send_reminder_whatsapp", mock.Mock(return_value=False))

    # First run
    created1 = _generate(db)
    assert created1 == 1
    assert send.call_count == 1
    email, emp_name, meeting_time, summaries, sent_stage, sent_tz = send.call_args.args

    # Second run
    created2 = _generate(db)
    assert created2 == 0
    assert send.call_count == 1  # No new email sent


def test_retry_failed_meeting_reminder_with_no_items_resends(monkeypatch):
    """Test (e): retry of a failed meeting-level reminder with no items re-sends."""
    db = FakeDB()
    _seed(db)
    # No memory items
    _add_meeting(db, "2026-08-30T09:30:00")  # soon_1h stage

    send = mock.Mock(side_effect=[False, True])
    monkeypatch.setattr(email_mod, "send_reminder_email", send)
    monkeypatch.setattr(rm_mod, "_send_reminder_whatsapp", mock.Mock(return_value=False))

    # Initial generation - email fails
    created = _generate(db)
    assert created == 1
    n = db.notifications.find_one({"type": "meeting_reminder"})
    assert n["delivery_status"] == "failed"
    assert n["delivery_errors"] == ["email"]
    assert n["memory_id"] is None  # Meeting-level reminder
    email, emp_name, meeting_time, summaries, sent_stage, sent_tz = send.call_args.args

    # Retry - should succeed
    retried = rm_mod.retry_pending_deliveries(db, ORG_A, NOW + timedelta(minutes=6))
    assert retried == 1
    n = db.notifications.find_one({"type": "meeting_reminder"})
    assert n["delivery_status"] == "delivered"
    assert n["delivery_errors"] == []
    assert send.call_count == 2  # Called twice (initial + retry)


def test_owner_not_found_logs_skip_reason(caplog, monkeypatch):
    db = FakeDB()
    _seed(db)
    _add_memory(db)
    r = _add_meeting(db, "2026-08-30T09:30:00")  # soon_1h stage
    db.meetings.update_one({"_id": r}, {"$set": {"created_by": None}})

    monkeypatch.setattr(email_mod, "send_reminder_email", mock.Mock(return_value=True))
    monkeypatch.setattr(rm_mod, "_send_reminder_whatsapp", mock.Mock(return_value=False))

    with caplog.at_level(logging.INFO, logger="reminders"):
        assert _generate(db) == 1
    assert any("reason=owner_not_found" in m for m in caplog.messages)


def test_owner_email_unavailable_logs_skip_reason(caplog, monkeypatch):
    db = FakeDB()
    _seed(db, owner_email="")
    _add_memory(db)
    _add_meeting(db, "2026-08-30T09:30:00")  # soon_1h stage

    monkeypatch.setattr(email_mod, "send_reminder_email", mock.Mock(return_value=True))
    monkeypatch.setattr(rm_mod, "_send_reminder_whatsapp", mock.Mock(return_value=False))

    with caplog.at_level(logging.INFO, logger="reminders"):
        assert _generate(db) == 1
    assert any("reason=email_unavailable" in m for m in caplog.messages)
    # No email was attempted for a recipient we could not resolve.
    assert email_mod.send_reminder_email.call_count == 0


def test_opted_out_logs_skip_reason(caplog, monkeypatch):
    db = FakeDB()
    _seed(db, prefs={"meeting_reminders": False})
    _add_memory(db)
    _add_meeting(db, "2026-08-30T09:30:00")  # soon_1h stage

    monkeypatch.setattr(email_mod, "send_reminder_email", mock.Mock(return_value=True))
    monkeypatch.setattr(rm_mod, "_send_reminder_whatsapp", mock.Mock(return_value=False))

    with caplog.at_level(logging.INFO, logger="reminders"):
        assert _generate(db) == 1
    assert any("reason=opted_out" in m for m in caplog.messages)
    assert db.notifications.count_documents({"type": "meeting_reminder"}) == 1


# ── New requirements tests ──────────────────────────────────────────────────

def test_email_ok_whatsapp_4xx_only_one_email_across_retries(monkeypatch):
    """Test (a): email ok + WhatsApp 4xx -> exactly 1 email sent total across 5 sweeps."""
    db = FakeDB()
    _seed(db, phone="+919000000000")
    _add_memory(db)
    _add_meeting(db, "2026-08-30T09:30:00")  # soon_1h stage

    import whatsapp as wa_mod
    
    # WhatsApp raises a 4xx error (permanent failure)
    class WhatsApp4xxError(Exception):
        pass
    
    wa_error = WhatsApp4xxError("HTTP 400: Meta error code 131047")
    wa = mock.Mock(side_effect=wa_error)
    send = mock.Mock(return_value=True)
    monkeypatch.setattr(email_mod, "send_reminder_email", send)
    monkeypatch.setattr(wa_mod, "send_reminder_template", wa)

    # Initial generation
    created = _generate(db)
    assert created == 1
    assert send.call_count == 1  # Email sent once
    assert wa.call_count == 1    # WhatsApp attempted once

    n = db.notifications.find_one({"type": "meeting_reminder"})
    assert n["delivery_status"] == "delivered"  # Email succeeded, WhatsApp permanent failure
    assert n["email_sent"] is True
    assert n["whatsapp_sent"] is False
    assert n.get("whatsapp_permanent_failure") is True

    # Retry 5 times - email should NOT be re-sent
    for i in range(5):
        retried = rm_mod.retry_pending_deliveries(db, ORG_A, NOW + timedelta(minutes=6*(i+1)))
        assert retried == 0  # No retries needed since email done and WhatsApp permanent failure

    assert send.call_count == 1  # Still only 1 email sent total


def test_whatsapp_5xx_then_success_on_retry(monkeypatch):
    """Test (b): WhatsApp 5xx then success on retry -> email sent once, WhatsApp sent once."""
    db = FakeDB()
    _seed(db, phone="+919000000000")
    _add_memory(db)
    _add_meeting(db, "2026-08-30T09:30:00")  # soon_1h stage

    import whatsapp as wa_mod
    
    # WhatsApp fails with 5xx first, then succeeds
    class WhatsApp5xxError(Exception):
        pass
    
    wa = mock.Mock(side_effect=[WhatsApp5xxError("HTTP 500"), True])
    send = mock.Mock(return_value=True)
    monkeypatch.setattr(email_mod, "send_reminder_email", send)
    monkeypatch.setattr(wa_mod, "send_reminder_template", wa)

    # Initial generation - WhatsApp fails with 5xx
    created = _generate(db)
    assert created == 1
    assert send.call_count == 1  # Email sent once
    assert wa.call_count == 1    # WhatsApp attempted once (failed)

    n = db.notifications.find_one({"type": "meeting_reminder"})
    assert n["delivery_status"] == "failed"  # WhatsApp failed
    assert n["email_sent"] is True
    assert n["whatsapp_sent"] is False
    assert n.get("whatsapp_permanent_failure") is False  # 5xx is retryable
    assert "whatsapp" in n["delivery_errors"]

    # Retry - WhatsApp should succeed
    retried = rm_mod.retry_pending_deliveries(db, ORG_A, NOW + timedelta(minutes=6))
    assert retried == 1
    assert send.call_count == 1  # Email NOT re-sent
    assert wa.call_count == 2    # WhatsApp retried once more

    n = db.notifications.find_one({"type": "meeting_reminder"})
    assert n["delivery_status"] == "delivered"
    assert n["email_sent"] is True
    assert n["whatsapp_sent"] is True
    assert n["delivery_errors"] == []


def test_email_fail_then_success_whatsapp_not_resent_if_succeeded(monkeypatch):
    """Test (c): email fail then success on retry -> WhatsApp not re-sent if it already succeeded."""
    db = FakeDB()
    _seed(db, phone="+919000000000")
    _add_memory(db)
    _add_meeting(db, "2026-08-30T09:30:00")  # soon_1h stage

    import whatsapp as wa_mod
    
    # Email fails first, then succeeds; WhatsApp succeeds on first try
    send = mock.Mock(side_effect=[False, True])
    wa = mock.Mock(return_value=True)
    monkeypatch.setattr(email_mod, "send_reminder_email", send)
    monkeypatch.setattr(wa_mod, "send_reminder_template", wa)

    # Initial generation - email fails, WhatsApp succeeds
    created = _generate(db)
    assert created == 1
    assert send.call_count == 1
    assert wa.call_count == 1

    n = db.notifications.find_one({"type": "meeting_reminder"})
    assert n["delivery_status"] == "failed"
    assert n["email_sent"] is False
    assert n["whatsapp_sent"] is True
    assert "email" in n["delivery_errors"]

    # Retry - email should succeed, WhatsApp should NOT be re-sent
    retried = rm_mod.retry_pending_deliveries(db, ORG_A, NOW + timedelta(minutes=6))
    assert retried == 1
    assert send.call_count == 2  # Email retried once
    assert wa.call_count == 1    # WhatsApp NOT re-sent (already succeeded)

    n = db.notifications.find_one({"type": "meeting_reminder"})
    assert n["delivery_status"] == "delivered"
    assert n["email_sent"] is True
    assert n["whatsapp_sent"] is True
    assert n["delivery_errors"] == []


# ── Timezone tests ────────────────────────────────────────────────────────

def test_format_local_kolkata(monkeypatch):
    """Test format_local with Asia/Kolkata timezone."""
    from email_service import format_local
    from datetime import datetime, timezone
    
    # 2026-01-15 14:30 UTC = 2026-01-15 20:00 IST (UTC+5:30)
    dt = datetime(2026, 1, 15, 14, 30, tzinfo=timezone.utc)
    result = format_local(dt, "Asia/Kolkata")
    assert "Thursday" in result or "Friday" in result  # Day name
    assert "15 Jan" in result or "Jan 15" in result
    assert "20:" in result or "8:" in result  # 8 PM in 12-hour format
    assert "IST" in result or "+0530" in result or "IST" in result


def test_format_local_new_york(monkeypatch):
    """Test format_local with America/New_York timezone."""
    from email_service import format_local
    from datetime import datetime, timezone
    
    # 2026-01-15 14:30 UTC = 2026-01-15 09:30 EST (UTC-5)
    dt = datetime(2026, 1, 15, 14, 30, tzinfo=timezone.utc)
    result = format_local(dt, "America/New_York")
    assert "Thursday" in result or "Friday" in result
    assert "15 Jan" in result or "Jan 15" in result
    assert "9:" in result or "09:" in result  # 9 AM
    assert "EST" in result or "EDT" in result or "EST" in result


def test_format_local_invalid_fallbacks_to_utc(monkeypatch):
    """Test format_local falls back to UTC for invalid timezone."""
    from email_service import format_local
    from datetime import datetime, timezone
    
    dt = datetime(2026, 1, 15, 14, 30, tzinfo=timezone.utc)
    result = format_local(dt, "Invalid/Timezone")
    assert "UTC" in result or "GMT" in result


def test_format_local_none_returns_default(monkeypatch):
    """Test format_local with None datetime."""
    from email_service import format_local
    
    result = format_local(None, "Asia/Kolkata")
    assert result == "time to be confirmed"


def test_stage_for_local_date_near_midnight_ist(monkeypatch):
    """Test stage_for uses owner's local calendar date near midnight IST.
    
    Scenario: Meeting at 2026-08-30 18:30 UTC (which is 2026-08-31 00:00 IST).
    In UTC: day_of (same day as NOW which is 2026-08-30 09:00 UTC)
    In IST: day_of should be 2026-08-31 (next day)
    """
    from reminders import stage_for
    from datetime import datetime, timezone, timedelta
    
    # NOW = 2026-08-30 09:00 UTC (Wednesday morning)
    NOW = datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc)
    
    # Meeting at 18:30 UTC = 2026-08-31 00:00 IST (midnight IST, next calendar day)
    meeting_utc = datetime(2026, 8, 30, 18, 30, tzinfo=timezone.utc)
    
    # With UTC timezone: same UTC date (Aug 30) -> day_of
    stage_utc = stage_for(meeting_utc, NOW, "UTC")
    assert stage_utc == "day_of"
    
    # With IST timezone: next calendar day (Aug 31) -> upcoming_24h (since delta < 24h but not same local date)
    assert stage_for(meeting_utc, NOW, "Asia/Kolkata") == "upcoming_24h"
    
    # Meeting at 2026-08-30 10:30 UTC = 2026-08-30 16:00 IST (same local date, future meeting)
    meeting_utc2 = datetime(2026, 8, 30, 10, 30, tzinfo=timezone.utc)
    stage_utc2 = stage_for(meeting_utc2, NOW, "UTC")
    stage_ist2 = stage_for(meeting_utc2, NOW, "Asia/Kolkata")
    assert stage_utc2 == "day_of"  # Same UTC date
    assert stage_ist2 == "day_of"  # Same IST date


def test_stage_for_midnight_boundary(monkeypatch):
    """Test stage_for correctly handles midnight boundary in owner's timezone.
    
    NOW = 2026-08-30 09:00 UTC
    Meeting = 2026-08-30 18:30 UTC = 2026-08-31 00:00 IST (midnight IST)
    In IST: next calendar day -> upcoming_24h (since delta < 24h but not same local date)
    In UTC: same calendar day -> day_of
    """
    from reminders import stage_for
    from datetime import datetime, timezone, timedelta
    
    NOW = datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc)
    
    # Meeting at 18:30 UTC = midnight IST (next day in IST)
    meeting = datetime(2026, 8, 30, 18, 30, tzinfo=timezone.utc)
    
    # UTC: same date (Aug 30) -> day_of
    assert stage_for(meeting, NOW, "UTC") == "day_of"
    
    # IST: next date (Aug 31) -> upcoming_24h (delta is 9.5h < 24h)
    assert stage_for(meeting, NOW, "Asia/Kolkata") == "upcoming_24h"


def test_timezone_manual_not_overwritten_by_auto(monkeypatch):
    """Test manual timezone choice is not overwritten by auto-detection."""
    db = FakeDB()
    _seed(db)
    _add_memory(db)
    _add_meeting(db, "2026-08-30T09:30:00")
    
    # Manually set timezone to America/New_York via API
    from bson import ObjectId
    db.users.update_one(
        {"_id": ObjectId("999999999999999999999999")},
        {"$set": {"timezone": "America/New_York", "timezone_source": "manual"}}
    )
    
    # Now trigger generation (which would try to auto-detect)
    from reminders import ensure_reminder_notifications
    from datetime import datetime, timezone
    NOW = datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc)
    created = ensure_reminder_notifications(db, "aaaaaaaaaaaaaaaaaaaaaaaa", NOW)
    
    # Verify user still has manual timezone
    user = db.users.find_one({"_id": ObjectId("999999999999999999999999")})
    assert user.get("timezone") == "America/New_York"
    assert user.get("timezone_source") == "manual"


def test_timezone_default_when_missing(monkeypatch):
    """Test default timezone (Asia/Kolkata) is used when user has no timezone set."""
    db = FakeDB()
    _seed(db)
    _add_memory(db)
    _add_meeting(db, "2026-08-30T09:30:00")
    
    # Remove timezone from user
    from bson import ObjectId
    db.users.update_one(
        {"_id": ObjectId("999999999999999999999999")},
        {"$unset": {"timezone": "", "timezone_source": ""}}
    )
    
    from reminders import ensure_reminder_notifications
    from datetime import datetime, timezone
    NOW = datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc)
    created = ensure_reminder_notifications(db, "aaaaaaaaaaaaaaaaaaaaaaaa", NOW)
    
    # Verify notification has owner_timezone set to default
    n = db.notifications.find_one({"type": "meeting_reminder"})
    assert n.get("owner_timezone") == "Asia/Kolkata"
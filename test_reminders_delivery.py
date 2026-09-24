"""
Reminder delivery channels — email + (stubbed) WhatsApp on top of the
in-app reminder system.

Verifies (hand-rolled in-memory Mongo facade + patched outbound channels):
  - one email send per (meeting, memory, stage) at every stage, with the
    right stage, recipient, employee name and reminder summary
  - WhatsApp is skipped when the owning user has no phone_number
  - WhatsApp is attempted when a phone_number is present (stub is invoked)
  - the user-level opt-out (notification_prefs.meeting_reminders=False)
    suppresses email/WhatsApp but STILL creates the in-app notification
  - the send_reminder_email() return value is honored: a False result is
    recorded as delivery_status=failed (not delivered) and is retried on a
    later sweep; a skip/owner_unavailable is terminal and logged with reason
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


# ── One email per stage ─────────────────────────────────────────────────

def test_email_sent_once_per_stage(monkeypatch):
    cases = {
        "day_of": "2026-08-30T15:00:00",        # same calendar day
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
        email, emp_name, meeting_time, summary, sent_stage = send.call_args.args
        assert email == "hr@voovr.com", stage
        assert emp_name == "Harshit Rana", stage
        assert sent_stage == stage, stage
        assert "ship the handoff notes" in summary, stage
        # In-app notification still created alongside the email.
        assert db.notifications.count_documents(
            {"type": "meeting_reminder", "stage": stage}
        ) == 1, stage
        n = db.notifications.find_one({"type": "meeting_reminder", "stage": stage})
        assert n["delivery_status"] == "delivered", stage
        assert n["delivery_errors"] == [], stage
        # No phone number set → WhatsApp skipped.
        assert wa.call_count == 0, stage


# ── WhatsApp gating ─────────────────────────────────────────────────────

def test_whatsapp_sent_when_phone_present(monkeypatch):
    db = FakeDB()
    _seed(db, phone="+919000000000")
    _add_memory(db)
    _add_meeting(db, "2026-08-30T15:00:00")

    wa = mock.Mock(return_value=False)
    monkeypatch.setattr(email_mod, "send_reminder_email", mock.Mock(return_value=True))
    monkeypatch.setattr(rm_mod, "_send_reminder_whatsapp", wa)

    _generate(db)

    assert wa.call_count == 1
    phone, text = wa.call_args.args
    assert phone == "+919000000000"
    assert "ship the handoff notes" in text
    assert "/meeting-tracker" in text


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
    _add_meeting(db, "2026-08-30T15:00:00")

    send = mock.Mock(return_value=True)
    monkeypatch.setattr(email_mod, "send_reminder_email", send)
    monkeypatch.setattr(rm_mod, "_send_reminder_whatsapp", mock.Mock(return_value=False))

    _generate(db)

    assert send.call_count == 1


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
    _add_meeting(db, "2026-08-30T15:00:00")

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
    _add_meeting(db, "2026-08-30T15:00:00")

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
        "owner@example.com", "Harshit Rana", NOW, "summary", "day_of"
    )
    assert ok is False
    email_mod.requests.post.assert_not_called()


def test_owner_not_found_logs_skip_reason(caplog, monkeypatch):
    db = FakeDB()
    _seed(db)
    _add_memory(db)
    r = _add_meeting(db, "2026-08-30T15:00:00")
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
    _add_meeting(db, "2026-08-30T15:00:00")

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
    _add_meeting(db, "2026-08-30T15:00:00")

    monkeypatch.setattr(email_mod, "send_reminder_email", mock.Mock(return_value=True))
    monkeypatch.setattr(rm_mod, "_send_reminder_whatsapp", mock.Mock(return_value=False))

    with caplog.at_level(logging.INFO, logger="reminders"):
        assert _generate(db) == 1
    assert any("reason=opted_out" in m for m in caplog.messages)
    assert db.notifications.count_documents({"type": "meeting_reminder"}) == 1
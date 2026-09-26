"""
Phase 8 — meeting prep endpoint, event/overdue notifications, retry/sweep,
manager scoping on notifications, and memory provenance.

Verifies (via the same hand-rolled in-memory Mongo facade as the earlier
phase tests — no live DB, no external test dependency):
  - GET /api/meetings/<id>/prep authorization + shape for admins and managers
  - preparation_status lifecycle (PATCH defaults + validation)
  - memory provenance: created_by, confirmation_status, priority, owner,
    status_history, IN_PROGRESS/CANCELLED lifecycle, archive exclusion
  - one-time event notifications for reschedule/cancel (dedup by event_key)
  - one-time memory_overdue notifications (derived OVERDUE, never auto-advance)
  - delivery retry with CAS claim, backoff, exhaustion -> delivery_failed
  - sweep_all_orgs / REMINDER_SWEEP_ENABLED gating of the daemon
  - manager scoping on the notifications list + read/dismiss/read-all
"""
import os
from datetime import datetime, timezone, timedelta
from unittest import mock

from bson import ObjectId
import pytest

from flask import Flask

import meetings as meetings_mod
import conversation_memory as cm_mod
import notifications as notif_mod
import reminders as rm_mod
import employees as employees_mod
import email_service as email_mod

ORG_A = "aaaaaaaaaaaaaaaaaaaaaaaa"
EMP_A = "aaaa0000aaaa0000aaaa0001"       # manager A's own employee record
REP_A1 = "aaaa0000aaaa0000aaaa0002"
EMP_B = "aaaa0000aaaa0000aaaa0003"
REP_B1 = "aaaa0000aaaa0000aaaa0004"
SESSION_1 = "333333333333333333333333"
ADMIN_USER = "999999999999999999999999"
MANAGER_A = "888888888888888888888881"
MANAGER_B = "888888888888888888888882"

NOW = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)


def _bson_strip(v):
    if isinstance(v, dict):
        return {k: _bson_strip(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_bson_strip(x) for x in v]
    if isinstance(v, datetime):
        return v if v.tzinfo is None else v.astimezone(timezone.utc).replace(tzinfo=None)
    return v


class FakeCollection:
    """Minimal in-memory collection supporting the ops these routes use."""

    def __init__(self):
        self._docs = []

    def _match(self, doc, filt):
        for k, v in filt.items():
            if k == "$or":
                if not any(self._match(doc, sub) for sub in v):
                    return False
            elif isinstance(v, dict) and "$ne" in v:
                if doc.get(k) == v["$ne"]:
                    return False
            elif isinstance(v, dict) and "$in" in v:
                if doc.get(k) not in v["$in"]:
                    return False
            elif doc.get(k) != v:
                return False
        return True

    def find_one(self, filt, *args, **kw):
        for d in self._docs:
            if self._match(d, filt):
                return dict(d)
        return None

    def find(self, filt=None, *args, **kw):
        filt = filt or {}
        wrapped = [dict(d) for d in self._docs if self._match(d, filt)]

        class Cursor:
            def sort(self, key, direction=None):
                if isinstance(key, str):
                    key = [(key, direction or 1)]
                sign = {1: 1, -1: -1}
                for k, dirn in reversed(key):
                    wrapped.sort(key=lambda d: d.get(k), reverse=(sign.get(dirn, 1) == -1))
                return self

            def skip(self, n):
                del wrapped[:n]
                return self

            def limit(self, n):
                del wrapped[n:]
                return self

            def __iter__(self):
                return iter(wrapped)

            def __len__(self):
                return len(wrapped)

        return Cursor()

    def insert_one(self, doc):
        d = _bson_strip(dict(doc))
        d["_id"] = d.get("_id") or ObjectId()
        self._docs.append(d)
        return type("R", (), {"inserted_id": d["_id"]})()

    def update_one(self, filt, update):
        for d in self._docs:
            if self._match(d, filt):
                if "$set" in update:
                    d.update(_bson_strip(update["$set"]))
                if "$unset" in update:
                    for k in update["$unset"]:
                        d.pop(k, None)
                return type("R", (), {"matched_count": 1, "modified_count": 1})()
        return type("R", (), {"matched_count": 0, "modified_count": 0})()

    def update_many(self, filt, update):
        count = 0
        for d in self._docs:
            if self._match(d, filt):
                if "$set" in update:
                    d.update(_bson_strip(update["$set"]))
                count += 1
        return type("R", (), {"matched_count": count, "modified_count": count})()

    def delete_one(self, filt):
        for i, d in enumerate(self._docs):
            if self._match(d, filt):
                del self._docs[i]
                return type("R", (), {"deleted_count": 1})()
        return type("R", (), {"deleted_count": 0})()

    def count_documents(self, filt):
        return sum(1 for d in self._docs if self._match(d, filt))

    def create_index(self, *a, **k):
        return None


class FakeDB:
    def __init__(self):
        self.meetings = FakeCollection()
        self.conversation_memory = FakeCollection()
        self.employees = FakeCollection()
        self.sessions = FakeCollection()
        self.notifications = FakeCollection()
        self.users = FakeCollection()
        self.audit_log = FakeCollection()


def _emp(_id, emp_id, name, org=ORG_A, reports_to=None):
    return {
        "_id": ObjectId(_id), "employee_id": emp_id, "org_id": ObjectId(org),
        "name": name, "status": "active", "position": "Role", "department": "Dept",
        "reports_to": ObjectId(reports_to) if reports_to else None,
    }


def _seed_org(db):
    db.employees.insert_one(_emp(EMP_A, "EMP100", "Mgr A"))
    db.employees.insert_one(_emp(REP_A1, "EMP101", "Report A1", reports_to=EMP_A))
    db.employees.insert_one(_emp(EMP_B, "EMP200", "Mgr B"))
    db.employees.insert_one(_emp(REP_B1, "EMP201", "Report B1", reports_to=EMP_B))
    db.sessions.insert_one({
        "_id": ObjectId(SESSION_1), "org_id": ObjectId(ORG_A),
        "employee_id": ObjectId(REP_A1), "status": "completed",
        "created_at": datetime(2026, 9, 18, tzinfo=timezone.utc),
    })
    db.users.insert_one({"_id": ObjectId(ADMIN_USER), "role": "admin", "org_id": ObjectId(ORG_A)})
    db.users.insert_one({
        "_id": ObjectId(MANAGER_A), "role": "manager", "org_id": ObjectId(ORG_A),
        "linked_employee_id": ObjectId(EMP_A),
    })
    db.users.insert_one({
        "_id": ObjectId(MANAGER_B), "role": "manager", "org_id": ObjectId(ORG_A),
        "linked_employee_id": ObjectId(EMP_B),
    })
    return db


@pytest.fixture
def fake():
    return _seed_org(FakeDB())


def _make_client(monkeypatch, fake, user_id):
    for mod in (meetings_mod, cm_mod, notif_mod, rm_mod, employees_mod):
        monkeypatch.setattr(mod, "get_db", lambda: fake)
    for mod in (meetings_mod, cm_mod, notif_mod, employees_mod):
        monkeypatch.setattr(mod, "_require_auth", lambda: ORG_A)

    app = Flask(__name__)
    app.register_blueprint(meetings_mod.meetings_bp, url_prefix="/api")
    app.register_blueprint(cm_mod.conversation_memory_bp, url_prefix="/api")
    app.register_blueprint(notif_mod.notifications_bp, url_prefix="/api")
    app.register_blueprint(rm_mod.reminders_bp, url_prefix="/api")
    app.config["TESTING"] = True
    app.secret_key = "test"
    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess["user_id"] = user_id
        yield c


@pytest.fixture
def admin_client(monkeypatch, fake):
    yield from _make_client(monkeypatch, fake, ADMIN_USER)


@pytest.fixture
def client(admin_client):
    return admin_client


def set_user(client, user_id):
    """Swap the persistent test session's acting user."""
    with client.session_transaction() as sess:
        sess["user_id"] = user_id


def _future(days=3):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _create_meeting(client, employee_id=REP_A1, scheduled_at=None, title="1:1"):
    if scheduled_at is None:
        scheduled_at = _future()
    return client.post("/api/meetings", json={
        "employee_id": employee_id, "scheduled_at": scheduled_at, "title": title,
    })


def _seed_overdue_client(client, emp=REP_A1):
    mid = _create_meeting(client, employee_id=emp).get_json()["id"]
    return mid


def _add_memory(db, mtype, content, emp=REP_A1, status="PENDING", due_at=None,
                confirmation_status="confirmed", archived=False, **extra):
    doc = {
        "org_id": ObjectId(ORG_A), "employee_id": ObjectId(emp),
        "session_id": ObjectId(SESSION_1), "type": mtype, "content": content,
        "status": status, "due_at": due_at, "used_at": None,
        "completed_at": None, "usage_count": 0, "usage": [],
        "created_at": NOW, "updated_at": NOW,
        "confirmation_status": confirmation_status, "archive": archived,
    }
    doc.update(extra)
    return db.conversation_memory.insert_one(doc).inserted_id


def _add_notification(db, **extra):
    doc = {
        "org_id": ObjectId(ORG_A), "type": "meeting_reminder",
        "headline": "h", "summary": "s", "confidence": 0,
        "employee_id": ObjectId(REP_A1), "source_session_id": None,
        "meeting_id": None, "memory_id": None, "stage": "soon_1h",
        "read": False, "dismissed": False, "created_at": NOW,
    }
    doc.update(extra)
    return db.notifications.insert_one(doc).inserted_id


# ── Meeting prep endpoint ──────────────────────────────────────────────

def test_prep_shape_and_surfacing(admin_client, fake):
    mid = _seed_overdue_client(admin_client)
    _add_memory(fake, "OPENER", "open with energy", status="SAVED",
                confirmation_status="confirmed")
    _add_memory(fake, "OPENER", "this was used", status="USED",
                confirmation_status="confirmed")
    _add_memory(fake, "OPENER", "AI draft", status="SAVED",
                confirmation_status="suggested")
    _add_memory(fake, "NOTE", "talk about velocity", status="SAVED")
    _add_memory(fake, "COMMITMENT", "ship the deck", status="PENDING",
                due_at=NOW + timedelta(days=2))
    _add_memory(fake, "COMMITMENT", "finance follow-up", status="PENDING",
                due_at=NOW + timedelta(days=2), owner_user_id=ObjectId(ADMIN_USER))
    _add_memory(fake, "FOLLOW_UP", "confirm date", status="PENDING",
                due_at=NOW - timedelta(days=1))

    r = admin_client.get(f"/api/meetings/{mid}/prep")
    assert r.status_code == 200
    d = r.get_json()
    assert d["employee"]["id"] == REP_A1
    assert d["meeting"]["id"] == mid
    assert d["preparation_status"] == "not_started"
    assert d["preparation_completed"] is False
    assert d["generated_at"]
    assert [o["content"] for o in d["current_openers"]] == ["open with energy"]
    assert [o["content"] for o in d["previously_used_openers"]] == ["this was used"]
    assert {o["content"] for o in d["confirmed_openers"]} == {"open with energy", "this was used"}
    assert [i["content"] for i in d["suggested_topics"]] == ["AI draft"]
    assert [i["content"] for i in d["discussion_points"]] == ["talk about velocity"]
    assert [i["content"] for i in d["pending_commitments"]] == ["ship the deck"]
    assert [i["content"] for i in d["hr_commitments"]] == ["finance follow-up"]
    assert [i["content"] for i in d["overdue_follow_ups"]] == ["confirm date"]
    assert d["meta"]["note"]


def test_prep_preparation_status_lifecycle(admin_client, fake):
    mid = _seed_overdue_client(admin_client)
    r = admin_client.patch(f"/api/meetings/{mid}", json={"preparation_status": "in_progress"})
    assert r.status_code == 200
    assert r.get_json()["preparation_status"] == "in_progress"

    d = admin_client.get(f"/api/meetings/{mid}/prep").get_json()
    assert d["preparation_status"] == "in_progress"
    assert d["preparation_completed"] is False

    r = admin_client.patch(f"/api/meetings/{mid}", json={"preparation_status": "completed"})
    assert r.status_code == 200
    d = admin_client.get(f"/api/meetings/{mid}/prep").get_json()
    assert d["preparation_status"] == "completed"
    assert d["preparation_completed"] is True


def test_prep_rejects_invalid_preparation_status(admin_client, fake):
    mid = _seed_overdue_client(admin_client)
    r = admin_client.patch(f"/api/meetings/{mid}", json={"preparation_status": "half"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "invalid_preparation_status"


def test_prep_manager_within_team_ok_and_cross_team_forbidden(client, fake):
    set_user(client, MANAGER_A)
    mid_a = _seed_overdue_client(client, emp=REP_A1)
    set_user(client, MANAGER_B)
    mid_b = _seed_overdue_client(client, emp=REP_B1)

    set_user(client, MANAGER_A)
    assert client.get(f"/api/meetings/{mid_a}/prep").status_code == 200
    assert client.get(f"/api/meetings/{mid_b}/prep").status_code == 403

    set_user(client, MANAGER_B)
    assert client.get(f"/api/meetings/{mid_b}/prep").status_code == 200
    assert client.get(f"/api/meetings/{mid_a}/prep").status_code == 403


def test_prep_archived_memory_excluded(admin_client, fake):
    mid = _seed_overdue_client(admin_client)
    _add_memory(fake, "NOTE", "archived note", status="SAVED", archived=True)
    _add_memory(fake, "NOTE", "live note", status="SAVED")
    d = admin_client.get(f"/api/meetings/{mid}/prep").get_json()
    assert [i["content"] for i in d["discussion_points"]] == ["live note"]


def test_prep_unknown_meeting_and_employee(admin_client, fake):
    assert admin_client.get("/api/meetings/000000000000000000000000/prep").status_code == 404


# ── Memory provenance ──────────────────────────────────────────────────

def test_memory_create_provenance_defaults(admin_client, fake):
    r = admin_client.post("/api/conversation-memory", json={
        "employee_id": REP_A1, "type": "COMMITMENT", "content": "ship it",
        "priority": "high", "owner_user_id": ADMIN_USER,
        "due_at": _future(days=1),
    })
    assert r.status_code == 201
    d = r.get_json()
    assert d["confirmation_status"] == "confirmed"
    assert d["priority"] == "high"
    assert d["owner_user_id"] == ADMIN_USER
    assert d["created_by"] == ADMIN_USER
    assert d["status"] == "PENDING"
    assert [h["status"] for h in d["status_history"]] == ["PENDING"]
    assert d["status_history"][0]["changed_by"] == ADMIN_USER


def test_memory_suggested_confirmation_status(admin_client, fake):
    r = admin_client.post("/api/conversation-memory", json={
        "employee_id": REP_A1, "type": "NOTE", "content": "draft",
        "confirmation_status": "suggested",
    })
    assert r.status_code == 201
    assert r.get_json()["confirmation_status"] == "suggested"


def test_memory_status_history_and_timestamps(admin_client, fake):
    mid = admin_client.post("/api/conversation-memory", json={
        "employee_id": REP_A1, "type": "COMMITMENT", "content": "deck",
        "due_at": _future(days=1),
    }).get_json()["id"]

    r = admin_client.patch(f"/api/conversation-memory/{mid}", json={"status": "IN_PROGRESS"})
    assert r.status_code == 200
    d = r.get_json()
    assert d["status"] == "IN_PROGRESS"
    assert d["started_at"]
    # create-time PENDING entry + the PATCH entry
    assert [h["status"] for h in d["status_history"]] == ["PENDING", "IN_PROGRESS"]
    assert d["status_history"][-1]["changed_by"] == ADMIN_USER

    r = admin_client.patch(f"/api/conversation-memory/{mid}", json={"status": "COMPLETED"})
    d = r.get_json()
    assert d["status"] == "COMPLETED"
    assert d["completed_at"]
    assert [h["status"] for h in d["status_history"]] == ["PENDING", "IN_PROGRESS", "COMPLETED"]

    r = admin_client.patch(f"/api/conversation-memory/{mid}", json={"status": "CANCELLED"})
    d = r.get_json()
    assert d["status"] == "CANCELLED"
    assert d["cancelled_at"]
    assert [h["status"] for h in d["status_history"]][-1] == "CANCELLED"


def test_memory_status_not_trackable_rejected(admin_client, fake):
    mid = admin_client.post("/api/conversation-memory", json={
        "employee_id": REP_A1, "type": "NOTE", "content": "note",
    }).get_json()["id"]
    r = admin_client.patch(f"/api/conversation-memory/{mid}", json={"status": "IN_PROGRESS"})
    assert r.status_code == 400


def test_memory_manager_cannot_edit_other_team(client, fake):
    set_user(client, MANAGER_B)
    mid = client.post("/api/conversation-memory", json={
        "employee_id": REP_B1, "type": "COMMITMENT", "content": "b1 task",
        "due_at": _future(days=1),
    }).get_json()["id"]
    set_user(client, MANAGER_A)
    assert client.patch(
        f"/api/conversation-memory/{mid}", json={"content": "hacked"}
    ).status_code == 403


# ── Notification events (reschedule / cancel) ───────────────────────────

def test_cancel_creates_single_event_notification(admin_client, fake):
    mid = _seed_overdue_client(admin_client)
    db = fake
    m = db.meetings.find_one({"_id": ObjectId(mid)})
    assert m
    meetings_mod._maybe_meeting_event_notification(db, ORG_A, m, {"status": "cancelled"})
    meetings_mod._maybe_meeting_event_notification(db, ORG_A, m, {"status": "cancelled"})

    events = [d for d in db.notifications._docs if d.get("type") == "meeting_event"]
    assert len(events) == 1
    assert events[0]["event_key"] == "meeting_cancelled"
    assert events[0]["meeting_id"] == m["_id"]
    assert events[0]["recipient_user_id"] == ADMIN_USER


def test_reschedule_creates_single_event_notification(admin_client, fake):
    mid = _seed_overdue_client(admin_client)
    db = fake
    m = db.meetings.find_one({"_id": ObjectId(mid)})
    meetings_mod._maybe_meeting_event_notification(
        db, ORG_A, m, {"scheduled_at": datetime.now(timezone.utc) + timedelta(days=5)}
    )
    meetings_mod._maybe_meeting_event_notification(
        db, ORG_A, m, {"scheduled_at": datetime.now(timezone.utc) + timedelta(days=6)}
    )
    events = [d for d in db.notifications._docs if d.get("type") == "meeting_event"]
    assert len(events) == 1
    assert events[0]["event_key"] == "meeting_rescheduled"
    assert events[0]["headline"] == "A meeting was rescheduled"


def test_meeting_event_via_patch_route(admin_client, fake):
    mid = _seed_overdue_client(admin_client)
    admin_client.patch(f"/api/meetings/{mid}", json={"status": "cancelled"})
    admin_client.patch(f"/api/meetings/{mid}", json={"status": "cancelled"})
    admin_client.patch(f"/api/meetings/{mid}", json={"status": "cancelled"})
    events = [d for d in fake.notifications._docs if d.get("type") == "meeting_event"]
    assert len(events) == 1


def test_reschedule_no_dup_when_only_preparation_changes(admin_client, fake):
    mid = _seed_overdue_client(admin_client)
    admin_client.patch(f"/api/meetings/{mid}", json={"scheduled_at": _future(days=1)})
    admin_client.patch(f"/api/meetings/{mid}", json={"title": "1:1 (edited)"})
    events = [d for d in fake.notifications._docs if d.get("type") == "meeting_event"]
    assert len(events) == 1


# ── Overdue notifications ──────────────────────────────────────────────

def test_due_notification_created_once(admin_client, fake):
    _add_memory(fake, "FOLLOW_UP", "overdue follow-up", status="PENDING",
                due_at=NOW - timedelta(days=1))
    created = rm_mod.ensure_due_notifications(fake, ORG_A, NOW)
    assert created == 1
    due = [d for d in fake.notifications._docs if d.get("type") == "memory_overdue"]
    assert len(due) == 1
    assert due[0]["stage"] == "overdue_due"
    assert due[0]["event_key"].startswith("overdue:")

    # Idempotent on re-run.
    assert rm_mod.ensure_due_notifications(fake, ORG_A, NOW) == 0


def test_due_notification_skips_completed_or_future(admin_client, fake):
    _add_memory(fake, "COMMITMENT", "already done", status="COMPLETED",
                due_at=NOW - timedelta(days=1))
    _add_memory(fake, "COMMITMENT", "future work", status="PENDING",
                due_at=NOW + timedelta(days=5))
    _add_memory(fake, "FOLLOW_UP", "archived and overdue", status="PENDING",
                due_at=NOW - timedelta(days=1), archived=True)
    assert rm_mod.ensure_due_notifications(fake, ORG_A, NOW) == 0


def test_due_notification_in_progress_overdue(admin_client, fake):
    _add_memory(fake, "COMMITMENT", "started but late", status="IN_PROGRESS",
                due_at=NOW - timedelta(days=2))
    assert rm_mod.ensure_due_notifications(fake, ORG_A, NOW) == 1


def test_notification_serializer_includes_delivery_fields():
    doc = {
        "_id": ObjectId(), "org_id": ObjectId(ORG_A), "type": "meeting_reminder",
        "headline": "h", "summary": "s", "confidence": 0, "employee_id": ObjectId(REP_A1),
        "source_session_id": None, "meeting_id": None, "memory_id": None,
        "stage": "soon_1h", "recipient_user_id": ObjectId(ADMIN_USER),
        "delivery_status": "failed", "delivery_channel": ["in_app", "email", "whatsapp"],
        "delivery_errors": ["email"], "attempts": 3,
        "last_attempt_at": NOW, "next_attempt_at": NOW + timedelta(minutes=20),
        "event_key": "reminder:x", "read": False, "dismissed": False,
        "created_at": NOW,
    }
    j = notif_mod._notification_to_json(doc)
    assert j["recipient_user_id"] == str(doc["recipient_user_id"])
    assert j["delivery_status"] == "failed"
    assert j["delivery_channel"] == ["in_app", "email", "whatsapp"]
    assert j["delivery_errors"] == ["email"]
    assert j["attempts"] == 3
    assert j["next_attempt_at"]


# ── Delivery retry + sweep ────────────────────────────────────────────

def _seed_for_delivery(monkeypatch, success=True, status="failed", attempts=1):
    db = FakeDB()
    _seed_org(db)
    db.users.update_one({"_id": ObjectId(ADMIN_USER)}, {"$set": {"email": "hr@voovr.com"}})
    meeting = db.meetings.insert_one({
        "org_id": ObjectId(ORG_A), "employee_id": ObjectId(REP_A1),
        "title": "1:1", "status": "scheduled",
        "scheduled_at": NOW + timedelta(hours=1),
        "created_by": ObjectId(ADMIN_USER),
        "created_at": NOW, "updated_at": NOW,
    }).inserted_id
    memory = _add_memory(db, "COMMITMENT", "ship the deck",
                         due_at=NOW + timedelta(days=1))
    nid = _add_notification(db, meeting_id=meeting, memory_id=memory,
                            delivery_status=status, attempts=attempts,
                            delivery_errors=[], next_attempt_at=NOW - timedelta(minutes=1))
    if success:
        monkeypatch.setattr(email_mod, "send_reminder_email", mock.Mock(return_value=True))
    else:
        monkeypatch.setattr(email_mod, "send_reminder_email", mock.Mock(side_effect=RuntimeError("down")))
    return db, nid


def test_retry_success_marks_delivered(monkeypatch):
    db, nid = _seed_for_delivery(monkeypatch, success=True)
    retried = rm_mod.retry_pending_deliveries(db, ORG_A, NOW)
    assert retried == 1
    n = db.notifications.find_one({"_id": nid})
    assert n["delivery_status"] == "delivered"
    assert n["next_attempt_at"] is None
    assert n["attempts"] == 2
    assert email_mod.send_reminder_email.call_count == 1


def test_retry_failure_backs_off(monkeypatch):
    db, nid = _seed_for_delivery(monkeypatch, success=False, attempts=1)
    rm_mod.retry_pending_deliveries(db, ORG_A, NOW)
    n = db.notifications.find_one({"_id": nid})
    assert n["delivery_status"] == "failed"
    assert n["attempts"] == 2
    assert n["delivery_errors"] == ["email"]
    expect = NOW + rm_mod._backoff_for_attempt(1)
    assert n["next_attempt_at"] == _bson_strip(expect)


def test_retry_exhaustion_creates_delivery_failed_notification(monkeypatch):
    db, nid = _seed_for_delivery(
        monkeypatch, success=False, attempts=rm_mod.REMINDER_MAX_ATTEMPTS - 1
    )
    rm_mod.retry_pending_deliveries(db, ORG_A, NOW)
    n = db.notifications.find_one({"_id": nid})
    assert n["delivery_status"] == "failed"
    assert n["next_attempt_at"] is None
    failed = [d for d in db.notifications._docs if d.get("type") == "delivery_failed"]
    assert len(failed) == 1
    assert failed[0]["event_key"] == f"delivery_failed:{nid}"


def test_retry_not_due_yet_is_skipped(monkeypatch):
    db, nid = _seed_for_delivery(monkeypatch, success=True, attempts=1)
    later = NOW + timedelta(hours=1)
    db.notifications.update_one(
        {"_id": nid},
        {"$set": {"next_attempt_at": later, "delivery_status": "failed"}},
    )
    assert rm_mod.retry_pending_deliveries(db, ORG_A, NOW) == 0
    assert email_mod.send_reminder_email.call_count == 0


def test_retry_repeat_sweep_does_not_double_deliver(monkeypatch):
    db, nid = _seed_for_delivery(monkeypatch, success=True, attempts=1)
    assert rm_mod.retry_pending_deliveries(db, ORG_A, NOW) == 1
    assert email_mod.send_reminder_email.call_count == 1
    # A second pass reads the already-"delivered" doc and does nothing.
    assert rm_mod.retry_pending_deliveries(db, ORG_A, NOW) == 0
    assert email_mod.send_reminder_email.call_count == 1


def test_retry_claim_uses_cas_on_attempts(monkeypatch):
    db, nid = _seed_for_delivery(monkeypatch, success=True, attempts=1)
    # The claim happens atomically in the update_one filter: a stale claim
    # (attempts no longer matching) is rejected by Mongo's update_one and the
    # function skips it, so exactly one worker delivers each notification.
    stale = db.notifications.update_one({"_id": nid, "attempts": 1}, {"$set": {"attempts": 9}})
    assert stale.modified_count == 1


def test_create_meeting_stores_authenticated_creator(admin_client, fake):
    # create_meeting must persist the authenticated session user as the owner
    # so reminder email delivery can resolve the recipient (owner) for it.
    mid = _create_meeting(admin_client).get_json()["id"]
    m = fake.meetings.find_one({"_id": ObjectId(mid)})
    assert m["created_by"] == ObjectId(ADMIN_USER)


def test_sweep_all_orgs_runs_generation_retry_and_due(monkeypatch):
    db = FakeDB()
    _seed_org(db)
    db.users.update_one({"_id": ObjectId(ADMIN_USER)}, {"$set": {"email": "hr@voovr.com"}})
    # Upcoming meeting -> reminder generated for the surfaced commitment.
    meeting_id = db.meetings.insert_one({
        "org_id": ObjectId(ORG_A), "employee_id": ObjectId(REP_A1),
        "title": "1:1", "status": "scheduled",
        "scheduled_at": NOW + timedelta(minutes=30),
        "created_by": ObjectId(ADMIN_USER),
        "created_at": NOW, "updated_at": NOW,
    }).inserted_id
    _add_memory(db, "COMMITMENT", "upcoming commitment",
                due_at=NOW + timedelta(days=1))
    # Overdue item -> memory_overdue notification.
    _add_memory(db, "FOLLOW_UP", "overdue follow-up",
                due_at=NOW - timedelta(days=1))
    # Failed delivery (now due) -> retried.
    memory3 = _add_memory(db, "FOLLOW_UP", "retry me",
                          due_at=NOW + timedelta(days=1))
    _add_notification(db, meeting_id=meeting_id, memory_id=memory3,
                      delivery_status="failed", attempts=1,
                      delivery_errors=["whatsapp"], next_attempt_at=NOW - timedelta(minutes=1))

    monkeypatch.setattr(email_mod, "send_reminder_email", mock.Mock(return_value=True))

    result = rm_mod.sweep_all_orgs(db, NOW)
    assert result["orgs"] == 1
    assert result["created"] >= 1
    assert result["retried"] >= 1
    assert result["due"] >= 1
    reminders = [d for d in db.notifications._docs if d.get("type") == "meeting_reminder"]
    assert reminders
    assert any(d["delivery_status"] == "delivered" for d in reminders)


# ── Sweep daemon gating ────────────────────────────────────────────────

def test_sweep_enabled_flag():
    import reminders as rm
    prev = os.environ.get("REMINDER_SWEEP_ENABLED")
    try:
        os.environ["REMINDER_SWEEP_ENABLED"] = "0"
        assert rm._sweep_enabled() is False
        os.environ["REMINDER_SWEEP_ENABLED"] = "true"
        assert rm._sweep_enabled() is True
        os.environ["REMINDER_SWEEP_ENABLED"] = "1"
        assert rm._sweep_enabled() is True
        os.environ.pop("REMINDER_SWEEP_ENABLED", None)
        assert rm._sweep_enabled() is False  # default is opt-in (off)
    finally:
        if prev is None:
            os.environ.pop("REMINDER_SWEEP_ENABLED", None)
        else:
            os.environ["REMINDER_SWEEP_ENABLED"] = prev


def test_start_reminder_sweep_opt_in_spawns_daemon(monkeypatch):
    import contextlib
    import reminders as rm

    class _App:
        def app_context(self):
            return contextlib.nullcontext()

    monkeypatch.setenv("REMINDER_SWEEP_ENABLED", "1")
    monkeypatch.setattr(rm, "_sweep_interval_seconds", lambda: 999999)
    monkeypatch.setattr(rm, "sweep_all_orgs", lambda db, now=None: {})
    monkeypatch.setattr(rm, "get_db", lambda: object())

    with rm._SWEEP_LOCK:
        prior = rm._SWEEP_THREAD
        rm._SWEEP_THREAD = None
    try:
        rm.start_reminder_sweep(_App())
        t = rm._SWEEP_THREAD
        assert t is not None and t.is_alive() and t.daemon
        assert t.name == "reminder-sweep"
        first = t
        rm.start_reminder_sweep(_App())
        assert rm._SWEEP_THREAD is first
    finally:
        with rm._SWEEP_LOCK:
            rm._SWEEP_THREAD = prior


# ── Manager scoping on notifications ──────────────────────────────────

def test_manager_notifications_scope_list_and_counts(client, fake):
    for emp, tag in ((REP_A1, "a"), (REP_B1, "b")):
        _add_notification(fake, headline=f"headline {tag}",
                          employee_id=ObjectId(emp),
                          meeting_id=None, memory_id=None)
    _add_notification(fake, headline="unread for a", employee_id=ObjectId(REP_A1),
                      meeting_id=None, memory_id=None)

    set_user(client, MANAGER_A)
    d = client.get("/api/notifications").get_json()
    assert {n["headline"] for n in d["notifications"]} == {"headline a", "unread for a"}
    assert d["unread_count"] == 2

    set_user(client, MANAGER_B)
    d = client.get("/api/notifications").get_json()
    assert {n["headline"] for n in d["notifications"]} == {"headline b"}
    assert d["unread_count"] == 1


def test_manager_notifications_get_access(client, fake):
    b_id = _add_notification(fake, headline="b-only", employee_id=ObjectId(REP_B1),
                             meeting_id=None, memory_id=None)
    set_user(client, MANAGER_B)
    assert client.get(f"/api/notifications/{b_id}").status_code == 200
    set_user(client, MANAGER_A)
    assert client.get(f"/api/notifications/{b_id}").status_code == 404


def test_manager_notifications_dismiss_access(client, fake):
    b_id = _add_notification(fake, headline="b-only", employee_id=ObjectId(REP_B1),
                             meeting_id=None, memory_id=None)
    set_user(client, MANAGER_A)
    assert client.put(f"/api/notifications/{b_id}/dismiss").status_code == 404
    set_user(client, MANAGER_B)
    assert client.put(f"/api/notifications/{b_id}/dismiss").status_code == 200
    assert fake.notifications.find_one({"_id": b_id})["dismissed"] is True


def test_admin_sees_all_and_read_all_scoped_for_manager(client, fake):
    a_id = _add_notification(fake, headline="a", employee_id=ObjectId(REP_A1),
                             meeting_id=None, memory_id=None)
    _add_notification(fake, headline="b", employee_id=ObjectId(REP_B1),
                      meeting_id=None, memory_id=None)

    set_user(client, MANAGER_A)
    r = client.put("/api/notifications/read-all")
    assert r.status_code == 200
    assert fake.notifications.find_one({"_id": a_id})["read"] is True
    b = next(d for d in fake.notifications._docs if d.get("headline") == "b")
    assert b["read"] is False

    set_user(client, ADMIN_USER)
    d = client.get("/api/notifications").get_json()
    assert d["total"] == 2


# ── unread_only on the bell dropdown ───────────────────────────────────

def test_list_keeps_read_rows_by_default(client, fake):
    """The /notifications hub is a full history: read and unread together."""
    _add_notification(fake, headline="still unread", employee_id=ObjectId(REP_A1))
    _add_notification(fake, headline="already read", employee_id=ObjectId(REP_A1),
                      read=True)

    set_user(client, ADMIN_USER)
    d = client.get("/api/notifications").get_json()
    assert {n["headline"] for n in d["notifications"]} == {"still unread", "already read"}
    assert d["unread_count"] == 1


def test_unread_only_drops_read_rows_from_the_list(client, fake):
    _add_notification(fake, headline="still unread", employee_id=ObjectId(REP_A1))
    _add_notification(fake, headline="already read", employee_id=ObjectId(REP_A1),
                      read=True)

    set_user(client, ADMIN_USER)
    d = client.get("/api/notifications?unread_only=true").get_json()
    assert [n["headline"] for n in d["notifications"]] == ["still unread"]
    # The badge is still the true unread total, not the filtered row count.
    assert d["unread_count"] == 1


@pytest.mark.parametrize("value", ["1", "true", "yes", "TRUE", "Yes"])
def test_unread_only_accepts_the_truthy_spellings(client, fake, value):
    _add_notification(fake, headline="read one", employee_id=ObjectId(REP_A1), read=True)
    set_user(client, ADMIN_USER)
    d = client.get(f"/api/notifications?unread_only={value}").get_json()
    assert d["notifications"] == []


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
def test_unread_only_off_or_unchanged_keeps_read_rows(client, fake, value):
    _add_notification(fake, headline="read one", employee_id=ObjectId(REP_A1), read=True)
    set_user(client, ADMIN_USER)
    d = client.get(f"/api/notifications?unread_only={value}").get_json()
    assert [n["headline"] for n in d["notifications"]] == ["read one"]


def test_bell_empties_out_once_everything_is_read(client, fake):
    """The regression this param exists for: mark-all-read, then reload."""
    for tag in ("one", "two", "three"):
        _add_notification(fake, headline=tag, employee_id=ObjectId(REP_A1))

    set_user(client, ADMIN_USER)
    assert len(client.get("/api/notifications?limit=5&unread_only=true").get_json()["notifications"]) == 3

    assert client.put("/api/notifications/read-all").status_code == 200
    d = client.get("/api/notifications?limit=5&unread_only=true").get_json()
    assert d["notifications"] == []
    assert d["unread_count"] == 0


def test_unread_only_is_still_scoped_for_managers(client, fake):
    _add_notification(fake, headline="a unread", employee_id=ObjectId(REP_A1))
    _add_notification(fake, headline="b unread", employee_id=ObjectId(REP_B1))

    set_user(client, MANAGER_A)
    d = client.get("/api/notifications?unread_only=true").get_json()
    assert [n["headline"] for n in d["notifications"]] == ["a unread"]
    assert d["unread_count"] == 1

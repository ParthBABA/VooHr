"""
Phase 3 — Reminder & memory-surfacing layer.

Verifies (hand-rolled in-memory Mongo facade + Flask test client, no live DB):
  - deterministic surfacing: only actual stored PENDING/OVERDUE/SAVED records
    are surfaced; never invented, never AI-generated
  - priority ordering: overdue commitment > overdue follow-up > pending
    commitment > pending follow-up > saved opener > saved question > other
  - OVERDUE derived at read time, never stored
  - reminder uniqueness by (meeting_id + memory_id + stage)
  - idempotent generation (no duplicates on re-run)
  - org isolation: no cross-org leakage, no cross-org notification
  - dismiss is separate from the underlying memory (never completes it)
  - explicit usage recording via POST .../usage (count/history, OPENER only)
  - explicit-only status transitions (nothing auto-completes)
"""
from datetime import datetime, timezone, timedelta
from bson import ObjectId

import pytest
from flask import Flask

import employees as employees_mod
import meetings as meetings_mod
import conversation_memory as cm_mod
import reminders as rm_mod
import notifications as notif_mod

ORG_A = "aaaaaaaaaaaaaaaaaaaaaaaa"
ORG_B = "bbbbbbbbbbbbbbbbbbbbbbbb"
EMP_1 = "111111111111111111111111"
EMP_B = "222222222222222222222222"
SESSION_1 = "333333333333333333333333"
MEET_A = "444444444444444444444444"

# Fixed "now" for deterministic generation/stage tests (UTC), a Wednesday.
NOW = datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc)


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

    def find_one(self, filt):
        for d in self._docs:
            if self._match(d, filt):
                return dict(d)
        return None

    def find(self, filt=None, **kw):
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

            def __iter__(self):
                return iter(wrapped)

        return Cursor()

    def insert_one(self, doc):
        d = dict(doc)
        d["_id"] = d.get("_id") or ObjectId()
        self._docs.append(d)
        return type("R", (), {"inserted_id": d["_id"]})()

    def update_one(self, filt, update):
        for d in self._docs:
            if self._match(d, filt):
                if "$set" in update:
                    d.update(update["$set"])
                return type("R", (), {"matched_count": 1, "modified_count": 1})()
        return type("R", (), {"matched_count": 0, "modified_count": 0})()

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


@pytest.fixture
def fake():
    db = FakeDB()
    db.employees.insert_one({
        "_id": ObjectId(EMP_1), "employee_id": "EMP001", "name": "Harshit Rana",
        "position": "Product Designer", "department": "Design", "org_id": ObjectId(ORG_A), "status": "active",
    })
    db.employees.insert_one({
        "_id": ObjectId(EMP_B), "employee_id": "EMP002", "name": "Other Org Emp",
        "position": "Engineer", "department": "Eng", "org_id": ObjectId(ORG_B), "status": "active",
    })
    db.sessions.insert_one({
        "_id": ObjectId(SESSION_1), "org_id": ObjectId(ORG_A), "employee_id": ObjectId(EMP_1),
        "status": "completed", "created_at": datetime(2026, 8, 29, tzinfo=timezone.utc),
    })
    return db


@pytest.fixture
def client(monkeypatch, fake):
    monkeypatch.setattr(meetings_mod, "get_db", lambda: fake)
    monkeypatch.setattr(cm_mod, "get_db", lambda: fake)
    monkeypatch.setattr(rm_mod, "get_db", lambda: fake)
    monkeypatch.setattr(notif_mod, "get_db", lambda: fake)
    for mod in (meetings_mod, cm_mod, rm_mod, notif_mod, employees_mod):
        monkeypatch.setattr(mod, "_require_auth", lambda: ORG_A)

    def emp_json(emp):
        return {
            "id": str(emp["_id"]),
            "employee_id": emp.get("employee_id"),
            "name": emp.get("name", ""),
            "position": emp.get("position", ""),
            "department": emp.get("department", ""),
        }
    monkeypatch.setattr(meetings_mod, "_employee_to_json", emp_json)

    app = Flask(__name__)
    app.register_blueprint(meetings_mod.meetings_bp, url_prefix="/api")
    app.register_blueprint(cm_mod.conversation_memory_bp, url_prefix="/api")
    app.register_blueprint(rm_mod.reminders_bp, url_prefix="/api")
    app.register_blueprint(notif_mod.notifications_bp, url_prefix="/api")
    app.config["TESTING"] = True
    app.secret_key = "test"
    with app.test_client() as c:
        yield c


# ── Helpers (mutate the fixture DB) ─────────────────────────────────────

def add_meeting(fake, title="1:1", scheduled_at="2026-08-30T15:00:00",
                employee=EMP_1, org=ORG_A):
    st = datetime.fromisoformat(scheduled_at)
    if st.tzinfo is None:
        st = st.replace(tzinfo=timezone.utc)
    r = fake.meetings.insert_one({
        "org_id": ObjectId(org), "employee_id": ObjectId(employee),
        "title": title, "scheduled_at": st,
        "status": "scheduled", "session_id": None,
        "created_at": datetime(2026, 8, 28, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 8, 28, tzinfo=timezone.utc),
    })
    return r.inserted_id


def add_memory(fake, mtype="COMMITMENT", content="ctx", status="PENDING",
               due_at=None, employee=EMP_1, org=ORG_A):
    if due_at:
        due_at = datetime.fromisoformat(due_at)
        if due_at.tzinfo is None:
            due_at = due_at.replace(tzinfo=timezone.utc)
    r = fake.conversation_memory.insert_one({
        "org_id": ObjectId(org), "employee_id": ObjectId(employee),
        "session_id": ObjectId(SESSION_1), "type": mtype, "content": content,
        "status": status, "due_at": due_at, "used_at": None, "completed_at": None,
        "usage_count": 0, "usage": [],
        "created_at": datetime(2026, 8, 29, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 8, 29, tzinfo=timezone.utc),
    })
    return r.inserted_id


# ── Surfacing (pure) ────────────────────────────────────────────────────

def test_surface_pending_and_saved_but_not_completed(fake):
    c_id = add_memory(fake, "COMMITMENT", content="ship", status="PENDING",
                      due_at="2026-09-05T10:00:00")
    done_id = add_memory(fake, "COMMITMENT", content="already done",
                         status="COMPLETED", due_at="2026-08-20T10:00:00")
    opener_id = add_memory(fake, "OPENER", content="Remember the anniversary",
                           status="SAVED", due_at=None)
    memory = list(fake.conversation_memory.find({"org_id": ObjectId(ORG_A)}))
    surfaces = rm_mod.surface_items(memory, {EMP_1}, NOW)
    items = surfaces.get(EMP_1, [])
    ids = {i["id"] for i in items}
    assert str(c_id) in ids
    assert str(opener_id) in ids
    assert str(done_id) not in ids  # completed never resurfaces


def test_surface_priority_order(fake):
    c_overdue = add_memory(fake, "COMMITMENT", content="overdue commit",
                           status="PENDING", due_at="2026-08-28T10:00:00")
    c_pending = add_memory(fake, "COMMITMENT", content="pending commit",
                           status="PENDING", due_at="2026-09-05T10:00:00")
    opener_id = add_memory(fake, "OPENER", content="opener", status="SAVED",
                           due_at=None)
    memory = list(fake.conversation_memory.find({"org_id": ObjectId(ORG_A)}))
    items = rm_mod.surface_items(memory, {EMP_1}, NOW)[EMP_1]
    ordered = [i["id"] for i in items]
    assert ordered[0] == str(c_overdue)
    assert ordered[1] == str(c_pending)
    assert ordered[2] == str(opener_id)


def test_surface_overdue_derived_not_stored(fake):
    c_id = add_memory(fake, "COMMITMENT", content="past due", status="PENDING",
                      due_at="2026-08-28T10:00:00")
    memory = list(fake.conversation_memory.find({"org_id": ObjectId(ORG_A)}))
    items = rm_mod.surface_items(memory, {EMP_1}, NOW)[EMP_1]
    it = next(i for i in items if i["id"] == str(c_id))
    assert it["status"] == "OVERDUE"  # derived at read time
    doc = fake.conversation_memory.find_one({"_id": ObjectId(c_id)})
    assert doc["status"] == "PENDING"  # still stored as PENDING


def test_surface_only_for_upcoming_meeting_employees(fake):
    add_memory(fake, "COMMITMENT", content="has meeting", status="PENDING",
               due_at="2026-09-05T10:00:00")
    b_id = add_memory(fake, "COMMITMENT", content="other", status="PENDING",
                      due_at="2026-09-05T10:00:00", employee=EMP_B)
    memory = list(fake.conversation_memory.find({"org_id": ObjectId(ORG_A)}))
    surfaces = rm_mod.surface_items(memory, {EMP_1}, NOW)
    assert EMP_B not in surfaces
    assert str(b_id) not in {i["id"] for i in surfaces[EMP_1]}


def test_surface_no_new_facts(fake):
    # a NOT_USED opener surfaces, and we never fabricate a second item
    add_memory(fake, "OPENER", content="only real item", status="SAVED", due_at=None)
    memory = list(fake.conversation_memory.find({"org_id": ObjectId(ORG_A)}))
    items = rm_mod.surface_items(memory, {EMP_1}, NOW)[EMP_1]
    assert len(items) == 1
    assert items[0]["content"] == "only real item"


# ── Stage ───────────────────────────────────────────────────────────────

def test_stage_for():
    assert rm_mod.stage_for(NOW + timedelta(minutes=30), NOW) == "soon_1h"
    assert rm_mod.stage_for(NOW + timedelta(hours=6), NOW) == "day_of"
    assert rm_mod.stage_for(NOW + timedelta(hours=20), NOW) == "upcoming_24h"
    assert rm_mod.stage_for(NOW + timedelta(hours=50), NOW) is None


# ── Surfacing in dashboard ──────────────────────────────────────────────

def test_dashboard_exposes_surfaced(client, fake):
    add_meeting(fake, scheduled_at="2026-08-30T15:00:00")
    add_memory(fake, "COMMITMENT", content="ship", status="PENDING",
               due_at="2026-09-05T10:00:00")
    r = client.get("/api/meetings/dashboard")
    assert r.status_code == 200
    people = r.get_json()["people"]
    p = next(x for x in people if x["id"] == EMP_1)
    assert "surfaced" in p
    assert any(i["type"] == "COMMITMENT" for i in p["surfaced"])


# ── Notification generation (idempotent, dedup, org isolation) ──────────

def test_generate_dedup_by_meeting_memory_stage(client, fake):
    add_meeting(fake, scheduled_at="2026-08-30T15:00:00")
    add_memory(fake, "COMMITMENT", content="ship", status="PENDING",
               due_at="2026-09-05T10:00:00")
    r1 = client.post("/api/reminders/generate")
    assert r1.status_code == 200
    created1 = r1.get_json()["created"]
    assert created1 >= 1
    r2 = client.post("/api/reminders/generate")
    assert r2.get_json()["created"] == 0
    assert fake.notifications.count_documents({"type": "meeting_reminder"}) == created1


def test_generate_org_isolation(fake):
    add_meeting(fake, title="OtherOrg", scheduled_at="2026-08-30T15:00:00",
                employee=EMP_B, org=ORG_B)
    add_memory(fake, "COMMITMENT", content="orgB pending", status="PENDING",
               due_at="2026-09-05T10:00:00", employee=EMP_B, org=ORG_B)
    rm_mod.ensure_reminder_notifications(fake, ORG_A, NOW)
    assert fake.notifications.count_documents({"type": "meeting_reminder"}) == 0


def test_generate_notifications_in_bell(client, fake):
    add_meeting(fake, scheduled_at="2026-08-30T15:00:00")
    add_memory(fake, "COMMITMENT", content="ship", status="PENDING",
               due_at="2026-09-05T10:00:00")
    client.post("/api/reminders/generate")
    n = fake.notifications.find_one({"type": "meeting_reminder"})
    assert n is not None
    assert n["meeting_id"] and n["memory_id"] and n["stage"] == "day_of"


# ── Dismiss separate from memory ────────────────────────────────────────

def test_dismiss_does_not_complete_memory(client, fake):
    add_meeting(fake, scheduled_at="2026-08-30T15:00:00")
    c_id = add_memory(fake, "COMMITMENT", content="ship", status="PENDING",
                      due_at="2026-09-05T10:00:00")
    client.post("/api/reminders/generate")
    n = fake.notifications.find_one({"type": "meeting_reminder"})
    r = client.put(f"/api/notifications/{n['_id']}/dismiss")
    assert r.status_code == 200
    assert r.get_json()["dismissed"] is True
    doc = fake.conversation_memory.find_one({"_id": c_id})
    assert doc["status"] == "PENDING"


def test_dismiss_only_sets_dismissed(client, fake):
    add_meeting(fake, scheduled_at="2026-08-30T15:00:00")
    add_memory(fake, "COMMITMENT", content="ship", status="PENDING",
               due_at="2026-09-05T10:00:00")
    client.post("/api/reminders/generate")
    n = fake.notifications.find_one({"type": "meeting_reminder"})
    client.put(f"/api/notifications/{n['_id']}/dismiss")
    doc = fake.notifications.find_one({"_id": n["_id"]})
    assert doc["dismissed"] is True
    assert doc["read"] is True


# ── Explicit usage recording ────────────────────────────────────────────

def test_record_usage_opener(client, fake):
    meeting_id = add_meeting(fake, scheduled_at="2026-08-30T15:00:00")
    opener_id = add_memory(fake, "OPENER", content="Remember the anniversary",
                           status="SAVED", due_at=None)
    r = client.post(f"/api/conversation-memory/{opener_id}/usage",
                    json={"meeting_id": str(meeting_id), "session_id": SESSION_1})
    assert r.status_code == 200
    d = r.get_json()
    assert d["status"] == "USED"
    assert d["usage_count"] == 1
    assert d["usage"][0]["meeting_id"] == str(meeting_id)
    doc = fake.conversation_memory.find_one({"_id": ObjectId(opener_id)})
    assert doc["usage_count"] == 1
    assert len(doc["usage"]) == 1


def test_record_usage_commitment_rejected(client, fake):
    c_id = add_memory(fake, "COMMITMENT", content="ship", status="PENDING",
                      due_at="2026-09-05T10:00:00")
    r = client.post(f"/api/conversation-memory/{c_id}/usage", json={})
    assert r.status_code == 400
    assert r.get_json()["error"] == "not_usable"
    doc = fake.conversation_memory.find_one({"_id": c_id})
    assert doc["status"] == "PENDING"


def test_record_usage_second_time_accumulates(client, fake):
    add_meeting(fake, scheduled_at="2026-08-30T15:00:00")
    opener_id = add_memory(fake, "OPENER", content="opener", status="SAVED", due_at=None)
    client.post(f"/api/conversation-memory/{opener_id}/usage", json={})
    client.post(f"/api/conversation-memory/{opener_id}/usage", json={})
    d = client.post(f"/api/conversation-memory/{opener_id}/usage", json={}).get_json()
    assert d["usage_count"] == 3


def test_explicit_only_no_auto_complete(client, fake):
    add_meeting(fake, scheduled_at="2026-08-30T03:00:00")
    c_id = add_memory(fake, "COMMITMENT", content="still open", status="PENDING",
                      due_at="2026-08-30T11:00:00")
    client.post("/api/reminders/generate")
    doc = fake.conversation_memory.find_one({"_id": c_id})
    assert doc["status"] == "PENDING"

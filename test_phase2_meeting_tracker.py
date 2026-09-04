"""
Phase 2 — Meeting Tracker backend: meetings + conversation_memory.

Verifies (via a hand-rolled in-memory Mongo facade + Flask test client,
no live DB and no external test dependency):
  - org-level isolation
  - schema/field validation (types, lengths, invalid statuses)
  - OPENER SAVED->USED, COMMITMENT/FOLLOW_UP PENDING->COMPLETED,
    and derived OVERDUE for past-due pending items
  - explicit-only state changes (nothing auto-completed)
  - employee + session relationship checks
"""
import pytest
from datetime import datetime, timezone, timedelta
from bson import ObjectId

from flask import Flask

from employees import _employee_to_json as real_employee_to_json  # noqa: F401
import employees as employees_mod
import meetings as meetings_mod
import conversation_memory as cm_mod

ORG_A = "aaaaaaaaaaaaaaaaaaaaaaaa"
ORG_B = "bbbbbbbbbbbbbbbbbbbbbbbb"
EMP_1 = "111111111111111111111111"
EMP_B = "222222222222222222222222"
SESSION_1 = "333333333333333333333333"
ADMIN_USER = "999999999999999999999999"
EMP_2 = "444444444444444444444444"
EMP_OTHER = "555555555555555555555555"
EMP_MGR_OTHER = "666666666666666666666666"
MANAGER_USER = "888888888888888888888888"


class FakeCollection:
    """Minimal in-memory collection supporting the ops the blueprints use."""

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
            def __iter__(self):
                return iter(wrapped)
            def __len__(self):
                return len(wrapped)

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
                if "$unset" in update:
                    for k in update["$unset"]:
                        d.pop(k, None)
                return type("R", (), {"modified_count": 1})()
        return type("R", (), {"modified_count": 0})()

    def delete_one(self, filt):
        for i, d in enumerate(self._docs):
            if self._match(d, filt):
                del self._docs[i]
                return type("R", (), {"deleted_count": 1})()
        return type("R", (), {"deleted_count": 0})()

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
    # The test session acts as an org admin: the role-scoping helpers
    # (_employee_scope_filter / _employee_accessible) treat admins as full-org
    # (no filter), preserving the org-wide behavior these tests exercise.
    db.users.insert_one({
        "_id": ObjectId(ADMIN_USER), "role": "admin", "org_id": ObjectId(ORG_A),
    })
    return db


@pytest.fixture
def client(monkeypatch, fake):
    monkeypatch.setattr(meetings_mod, "get_db", lambda: fake)
    monkeypatch.setattr(cm_mod, "get_db", lambda: fake)
    monkeypatch.setattr(meetings_mod, "_require_auth", lambda: ORG_A)
    monkeypatch.setattr(cm_mod, "_require_auth", lambda: ORG_A)
    monkeypatch.setattr(employees_mod, "_require_auth", lambda: ORG_A)

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
    app.config["TESTING"] = True
    app.secret_key = "test"
    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess["user_id"] = ADMIN_USER
        yield c


# ── Meetings ─────────────────────────────────────────────────────────────

def _create_meeting(client, employee_id=EMP_1, scheduled_at="2026-09-05T10:00:00",
                    title="1:1", session_id=None):
    body = {"employee_id": employee_id, "scheduled_at": scheduled_at, "title": title}
    if session_id:
        body["session_id"] = session_id
    return client.post("/api/meetings", json=body)


def test_create_meeting_ok(client):
    r = _create_meeting(client)
    assert r.status_code == 201
    d = r.get_json()
    assert d["id"]
    assert d["employee_id"] == EMP_1
    assert d["status"] == "scheduled"
    assert d["scheduled_at"].startswith("2026-09-05")


def test_create_meeting_rejects_wrong_org_employee(client):
    r = _create_meeting(client, employee_id=EMP_B)
    assert r.status_code == 404
    assert r.get_json()["error"] == "employee_not_found"


def test_create_meeting_requires_scheduled_at(client):
    r = client.post("/api/meetings", json={"employee_id": EMP_1})
    assert r.status_code == 400
    assert r.get_json()["error"] == "scheduled_at_required"


def test_create_meeting_invalid_scheduled_at(client):
    r = _create_meeting(client, scheduled_at="not-a-date")
    assert r.status_code == 400


def test_create_meeting_links_existing_session(client):
    r = _create_meeting(client, session_id=SESSION_1)
    assert r.status_code == 201
    assert r.get_json()["session_id"] == SESSION_1


def test_list_meetings_includes_employee(client):
    _create_meeting(client)
    d = client.get("/api/meetings").get_json()
    assert d["total"] == 1
    assert d["meetings"][0]["employee"]["name"] == "Harshit Rana"


def test_update_meeting_status(client):
    mid = _create_meeting(client).get_json()["id"]
    r = client.patch(f"/api/meetings/{mid}", json={"status": "cancelled"})
    assert r.status_code == 200
    assert r.get_json()["status"] == "cancelled"


def test_update_meeting_invalid_status(client):
    mid = _create_meeting(client).get_json()["id"]
    r = client.patch(f"/api/meetings/{mid}", json={"status": "banana"})
    assert r.status_code == 400


def test_delete_meeting(client):
    mid = _create_meeting(client).get_json()["id"]
    assert client.delete(f"/api/meetings/{mid}").status_code == 200
    assert client.get(f"/api/meetings/{mid}").status_code == 404


# ── Conversation memory ───────────────────────────────────────────────────

def _post(client, mtype, content, session_id=SESSION_1, employee_id=EMP_1, **kw):
    body = {
        "employee_id": employee_id,
        "session_id": session_id,
        "type": mtype,
        "content": content,
    }
    body.update(kw)
    return client.post("/api/conversation-memory", json=body)


def _get_one(client, mid):
    items = client.get(f"/api/conversation-memory?employee_id={EMP_1}").get_json()["items"]
    return [m for m in items if m["id"] == mid][0]


def test_create_commitment_pending(client):
    d = _post(client, "COMMITMENT", "Review workload priorities").get_json()
    assert d["status"] == "PENDING"
    assert d["session_id"] == SESSION_1


def test_create_opener_saved(client):
    d = _post(client, "OPENER", "How are you feeling about your workload?").get_json()
    assert d["status"] == "SAVED"


def test_invalid_type_rejected(client):
    r = _post(client, "BANANA", "x")
    assert r.status_code == 400


def test_content_required(client):
    assert _post(client, "NOTE", "   ").status_code == 400


def test_opener_used_transition(client):
    mid = _post(client, "OPENER", "opener text").get_json()["id"]
    d = client.patch(f"/api/conversation-memory/{mid}", json={"status": "USED"}).get_json()
    assert d["status"] == "USED"
    assert d["used_at"] is not None


def test_commitment_completed(client):
    mid = _post(client, "COMMITMENT", "Review workload").get_json()["id"]
    d = client.patch(f"/api/conversation-memory/{mid}", json={"status": "COMPLETED"}).get_json()
    assert d["status"] == "COMPLETED"
    assert d["completed_at"] is not None


def test_commitment_not_auto_completed(client):
    assert _post(client, "COMMITMENT", "Do the thing").get_json()["status"] == "PENDING"


def test_cannot_use_a_commitment(client):
    mid = _post(client, "COMMITMENT", "x").get_json()["id"]
    assert client.patch(f"/api/conversation-memory/{mid}", json={"status": "USED"}).status_code == 400


def test_cannot_complete_an_opener(client):
    mid = _post(client, "OPENER", "opener").get_json()["id"]
    assert client.patch(f"/api/conversation-memory/{mid}", json={"status": "COMPLETED"}).status_code == 400


def test_overdue_derived_for_past_due(client):
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    mid = _post(client, "COMMITMENT", "overdue thing", due_at=past).get_json()["id"]
    assert _get_one(client, mid)["status"] == "OVERDUE"


def test_future_due_not_overdue(client):
    future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    mid = _post(client, "COMMITMENT", "future thing", due_at=future).get_json()["id"]
    assert _get_one(client, mid)["status"] == "PENDING"


def test_memory_org_scoped(client):
    _post(client, "NOTE", "note for A")
    r2 = client.get("/api/conversation-memory?employee_id=" + EMP_B)
    assert r2.get_json()["total"] == 0


def test_memory_rejects_other_org_employee(client):
    r = _post(client, "NOTE", "x", employee_id=EMP_B)
    assert r.status_code == 404


# ── Meetings dashboard (aggregate feed for Meeting Tracker) ──────────────

def test_dashboard_returns_employee_and_meeting(client):
    _create_meeting(client, scheduled_at="2026-09-05T10:00:00")
    d = client.get("/api/meetings/dashboard").get_json()
    assert any(e["id"] == EMP_1 for e in d["employees"])
    assert "reminders" not in d["counters"]  # no fake reminder engine
    people = d["people"]
    assert len(people) == 1
    row = people[0]
    assert row["employee"]["name"] == "Harshit Rana"
    assert row["next_meeting"]["id"]


def test_dashboard_memory_counters(client):
    _post(client, "COMMITMENT", "c1")
    _post(client, "COMMITMENT", "c2")
    _post(client, "FOLLOW_UP", "f1")
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    _post(client, "FOLLOW_UP", "f-overdue", due_at=past)
    d = client.get("/api/meetings/dashboard").get_json()
    assert d["counters"]["pending_commitments"] == 2
    assert d["counters"]["pending_followups"] == 2  # pending + overdue
    assert EMP_1 in d["followup_employee_ids"]
    assert EMP_1 in d["overdue_employee_ids"]
    # Open items carry real id/type/content/status for the Follow section
    p = next(x for x in d["people"] if x["id"] == EMP_1)
    assert len(p["open_items"]) == 4
    types = sorted({o["type"] for o in p["open_items"]})
    assert types == ["COMMITMENT", "FOLLOW_UP"]


def test_dashboard_previous_session_linked(client):
    _create_meeting(client, session_id=SESSION_1)
    d = client.get("/api/meetings/dashboard").get_json()
    row = d["people"][0]
    # SESSION_1 is the latest completed session for EMP_1
    assert row["previous_session"]["session_id"] == SESSION_1


# ── Manager-role scoping ─────────────────────────────────────────────────

@pytest.fixture
def mgr_client(monkeypatch):
    """Authenticates as a manager whose ``linked_employee_id`` is EMP_1 and
    yields ``(db, client)``.

    Employees in ORG_A: EMP_1 (the manager's own record), EMP_2 (direct
    report of EMP_1), and EMP_OTHER (reports to a different manager
    EMP_MGR_OTHER).  A manager must only ever see EMP_1 / EMP_2.  Already
    contains one meeting for EMP_2 (in-team, id=``my_meeting``) and one for
    EMP_OTHER (out-of-team, id=``other_meeting``), pre-inserted directly
    because creating an out-of-team meeting via the API is itself forbidden.
    """
    from flask import Flask
    now = datetime.now(timezone.utc)
    db = FakeDB()
    db.employees.insert_one({
        "_id": ObjectId(EMP_1), "employee_id": "EMP001", "name": "Manager Rana",
        "position": "Design Lead", "department": "Design", "org_id": ObjectId(ORG_A), "status": "active",
        "created_at": now,
    })
    db.employees.insert_one({
        "_id": ObjectId(EMP_2), "employee_id": "EMP002", "name": "Direct Report",
        "position": "Designer", "department": "Design", "org_id": ObjectId(ORG_A), "status": "active",
        "reports_to": ObjectId(EMP_1), "created_at": now,
    })
    db.employees.insert_one({
        "_id": ObjectId(EMP_OTHER), "employee_id": "EMP003", "name": "Other Team",
        "position": "Engineer", "department": "Eng", "org_id": ObjectId(ORG_A), "status": "active",
        "reports_to": ObjectId(EMP_MGR_OTHER), "created_at": now,
    })
    db.users.insert_one({
        "_id": ObjectId(MANAGER_USER), "role": "manager",
        "org_id": ObjectId(ORG_A), "linked_employee_id": ObjectId(EMP_1),
    })
    my_meeting = db.meetings.insert_one({
        "org_id": ObjectId(ORG_A), "employee_id": ObjectId(EMP_2),
        "title": "my team", "status": "scheduled",
        "scheduled_at": datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc),
        "created_at": now, "updated_at": now,
    })
    other_meeting = db.meetings.insert_one({
        "org_id": ObjectId(ORG_A), "employee_id": ObjectId(EMP_OTHER),
        "title": "other team", "status": "scheduled",
        "scheduled_at": datetime(2026, 9, 6, 10, 0, tzinfo=timezone.utc),
        "created_at": now, "updated_at": now,
    })
    db._manager_meeting_ids = {"my_meeting": my_meeting.inserted_id, "other_meeting": other_meeting.inserted_id}

    monkeypatch.setattr(meetings_mod, "get_db", lambda: db)
    monkeypatch.setattr(meetings_mod, "_require_auth", lambda: ORG_A)

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
    app.config["TESTING"] = True
    app.secret_key = "test"
    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess["user_id"] = MANAGER_USER
        yield db, c


def _create_meeting_for(client, employee_id, scheduled_at="2026-09-05T10:00:00", title="1:1"):
    return client.post("/api/meetings", json={
        "employee_id": employee_id, "scheduled_at": scheduled_at, "title": title,
    })


def test_manager_list_meetings_scoped(mgr_client):
    db, c = mgr_client
    d = c.get("/api/meetings").get_json()
    assert d["total"] == 1
    assert [m["employee_id"] for m in d["meetings"]] == [EMP_2]


def test_manager_list_meetings_respects_employee_param_scope(mgr_client):
    db, c = mgr_client
    # Asking for an out-of-team employee must not leak that team's meeting.
    d = c.get("/api/meetings?employee_id=" + EMP_OTHER).get_json()
    assert d["total"] == 0


def test_manager_get_other_team_meeting_denied(mgr_client):
    db, c = mgr_client
    mid = str(db._manager_meeting_ids["other_meeting"])
    r = c.get(f"/api/meetings/{mid}")
    assert r.status_code == 403
    assert r.get_json()["error"] == "forbidden"


def test_manager_can_get_own_team_meeting(mgr_client):
    db, c = mgr_client
    mid = str(db._manager_meeting_ids["my_meeting"])
    assert c.get(f"/api/meetings/{mid}").status_code == 200


def test_manager_update_other_team_meeting_denied(mgr_client):
    db, c = mgr_client
    mid = str(db._manager_meeting_ids["other_meeting"])
    r = c.patch(f"/api/meetings/{mid}", json={"status": "cancelled"})
    assert r.status_code == 403


def test_manager_delete_other_team_meeting_denied(mgr_client):
    db, c = mgr_client
    mid = str(db._manager_meeting_ids["other_meeting"])
    r = c.delete(f"/api/meetings/{mid}")
    assert r.status_code == 403
    # Still present for the owning team.
    assert c.get(f"/api/meetings/{mid}").status_code == 403


def test_manager_create_for_other_team_denied(mgr_client):
    db, c = mgr_client
    r = _create_meeting_for(c, EMP_OTHER)
    assert r.status_code == 403
    assert r.get_json()["error"] == "forbidden"


def test_manager_create_for_own_team_ok(mgr_client):
    db, c = mgr_client
    r = _create_meeting_for(c, EMP_2)
    assert r.status_code == 201


def test_manager_dashboard_scoped(mgr_client):
    db, c = mgr_client
    d = c.get("/api/meetings/dashboard").get_json()
    emp_ids = {e["id"] for e in d["employees"]}
    assert EMP_1 in emp_ids
    assert EMP_2 in emp_ids
    assert EMP_OTHER not in emp_ids
    # EMP_OTHER's meeting must never surface on the manager's board.
    assert all(p["id"] != EMP_OTHER for p in d["people"])




"""
Manager / admin permission scoping — regression safety net.

Covers the whole manager-role rollout as automated tests so future changes
can't silently reopen the permission gaps that were (until now) only caught
by manual clicking:

  1. `_employee_scope_filter` / `_employee_accessible` unit behaviour
  2. Per-route GET / PUT / DELETE permission matrix (two-manager isolation)
  3. Meetings scoping (manager team-only; admin unscoped)
  4. Manager-invite flow (supersede, expire, email-mismatch, atomic accept,
     no-email guard)
  5. CSV `reports_to_email` resolution (same-file manager, unresolved
     warning, blank column)

All network boundaries (Google OAuth, Brevo email) are stubbed — no real
external calls.  Mongo is an in-memory fake; never a real deployment DB.
"""
import os
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId
from flask import Flask, redirect, session

from blind_index import blind_index
import employees as employees_mod
import meetings as meetings_mod
import auth as auth_mod
import login_flow
import field_encryption as field_encryption_mod

os.environ.setdefault("HASH_INDEX_SECRET", "test-secret")

ORG_A = "aaaaaaaaaaaaaaaaaaaaaaaa"
ORG_B = "bbbbbbbbbbbbbbbbbbbbbbbb"
ADMIN_USER = "999999999999999999999999"
MANAGER_A = "888888888888888888888888"
MANAGER_B = "777777777777777777777777"
EMP_A = "111111111111111111111111"
REP_A1 = "aaaa11111111111111111111"
EMP_B = "222222222222222222222222"
REP_B1 = "bbbb22222222222222222222"
SESS_A1 = "333333333333333333333333"
SESS_B1 = "444444444444444444444444"


# ── In-memory Mongo facade ──────────────────────────────────────────────

class FakeCollection:
    def __init__(self):
        self._docs = []

    def _match(self, doc, filt):
        for k, v in filt.items():
            if k == "$or":
                if not any(self._match(doc, sub) for sub in v):
                    return False
            elif k == "$and":
                if not all(self._match(doc, sub) for sub in v):
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
            def __init__(self, items):
                self._items = items
            def sort(self, key, direction=None):
                return self
            def skip(self, n):
                return Cursor(self._items[n:])
            def limit(self, n):
                return Cursor(self._items[:n])
            def __iter__(self):
                return iter(self._items)
            def __len__(self):
                return len(self._items)

        return Cursor(wrapped)

    def insert_one(self, doc):
        d = dict(doc)
        d["_id"] = d.get("_id") or ObjectId()
        self._docs.append(d)
        return type("R", (), {"inserted_id": d["_id"]})()

    def update_one(self, filt, update):
        return self._apply(filt, update, many=False)

    def update_many(self, filt, update):
        return self._apply(filt, update, many=True)

    def _apply(self, filt, update, many):
        count = 0
        for d in self._docs:
            if not self._match(d, filt):
                continue
            if "$set" in update:
                d.update(update["$set"])
            if "$unset" in update:
                for k in update["$unset"]:
                    d.pop(k, None)
            count += 1
            if not many:
                break
        return type("R", (), {"modified_count": count})()

    def find_one_and_update(self, filt, update, *args, **kw):
        for d in self._docs:
            if not self._match(d, filt):
                continue
            prev = dict(d)
            if "$set" in update:
                d.update(update["$set"])
            return prev
        return None

    def delete_one(self, filt):
        for i, d in enumerate(self._docs):
            if self._match(d, filt):
                del self._docs[i]
                return type("R", (), {"deleted_count": 1})()
        return type("R", (), {"deleted_count": 0})()

    def delete_many(self, filt):
        before = len(self._docs)
        self._docs = [d for d in self._docs if not self._match(d, filt)]
        return type("R", (), {"deleted_count": before - len(self._docs)})()

    def count(self, filt=None):
        return len([d for d in self._docs if self._match(d, filt or {})])

    def count_documents(self, filt=None):
        return self.count(filt)


class FakeDB:
    def __init__(self):
        self.employees = FakeCollection()
        self.users = FakeCollection()
        self.sessions = FakeCollection()
        self.notifications = FakeCollection()
        self.meetings = FakeCollection()
        self.conversation_memory = FakeCollection()
        self.invites = FakeCollection()
        self.organizations = FakeCollection()
        self.audit_logs = FakeCollection()


def _seed_db():
    db = FakeDB()
    now = datetime.now(timezone.utc)

    def emp(_id, emp_id, name, reports_to=None):
        return {
            "_id": ObjectId(_id), "employee_id": emp_id, "org_id": ObjectId(ORG_A),
            "status": "active", "department": "Initial Dept", "position": "Role",
            "reports_to": ObjectId(reports_to) if reports_to else None,
            "encrypted": {"name": name, "email": f"{emp_id.lower()}@corp.com"},
            "wrapped_dek": "dek", "signals": {}, "created_at": now,
        }

    db.employees.insert_one(emp(EMP_A, "EMP100", "Mgr A"))
    db.employees.insert_one(emp(REP_A1, "EMP101", "Report A1", reports_to=EMP_A))
    db.employees.insert_one(emp(EMP_B, "EMP200", "Mgr B"))
    db.employees.insert_one(emp(REP_B1, "EMP201", "Report B1", reports_to=EMP_B))

    db.users.insert_one({"_id": ObjectId(ADMIN_USER), "role": "admin", "org_id": ObjectId(ORG_A)})
    db.users.insert_one({
        "_id": ObjectId(MANAGER_A), "role": "manager", "org_id": ObjectId(ORG_A),
        "linked_employee_id": ObjectId(EMP_A),
    })
    db.users.insert_one({
        "_id": ObjectId(MANAGER_B), "role": "manager", "org_id": ObjectId(ORG_A),
        "linked_employee_id": ObjectId(EMP_B),
    })

    db.meetings.insert_one({
        "org_id": ObjectId(ORG_A), "employee_id": ObjectId(REP_A1),
        "title": "A1 sync", "status": "scheduled",
        "scheduled_at": datetime.now(timezone.utc) + timedelta(days=1),
        "created_at": now,
    })
    db.meetings.insert_one({
        "org_id": ObjectId(ORG_A), "employee_id": ObjectId(REP_B1),
        "title": "B1 sync", "status": "scheduled",
        "scheduled_at": datetime.now(timezone.utc) + timedelta(days=1),
        "created_at": now,
    })
    db.sessions.insert_one({
        "_id": ObjectId(SESS_A1), "org_id": ObjectId(ORG_A),
        "employee_id": ObjectId(REP_A1), "user_id": ObjectId(ADMIN_USER),
        "status": "completed", "created_at": now,
    })
    db.sessions.insert_one({
        "_id": ObjectId(SESS_B1), "org_id": ObjectId(ORG_A),
        "employee_id": ObjectId(REP_B1), "user_id": ObjectId(ADMIN_USER),
        "status": "completed", "created_at": now,
    })
    db.organizations.insert_one({"_id": ObjectId(ORG_A), "name": "Acme"})
    return db


# ── Shared patched environment (one DB shared by every role fixture) ─────

def _dump_emp(emp):
    pii = emp.get("encrypted") or {}
    return {
        "id": str(emp["_id"]),
        "employee_id": emp.get("employee_id"),
        "name": pii.get("name", emp.get("employee_id", "")),
        "email": pii.get("email", ""),
        "department": emp.get("department", ""),
        "position": emp.get("position", ""),
        "reports_to": str(emp["reports_to"]) if emp.get("reports_to") else None,
    }


def _make_admin_gate(db):
    def gate():
        uid = session.get("user_id")
        if not uid:
            return None
        try:
            u = db.users.find_one({"_id": ObjectId(uid)})
        except Exception:
            return None
        if not u or u.get("role") != "admin":
            return None
        return ORG_A
    return gate


@pytest.fixture
def env(monkeypatch):
    db = _seed_db()
    monkeypatch.setattr(employees_mod, "get_db", lambda: db)
    monkeypatch.setattr(meetings_mod, "get_db", lambda: db)
    monkeypatch.setattr(auth_mod, "get_db", lambda: db)
    monkeypatch.setattr(employees_mod, "_require_auth", lambda: ORG_A)
    monkeypatch.setattr(meetings_mod, "_require_auth", lambda: ORG_A)
    monkeypatch.setattr(employees_mod, "_require_admin", _make_admin_gate(db))
    monkeypatch.setattr(employees_mod, "_employee_to_json", _dump_emp)
    monkeypatch.setattr(meetings_mod, "_employee_to_json", _dump_emp)
    monkeypatch.setattr(employees_mod, "decrypt_fields", lambda enc, dek: enc or {})
    monkeypatch.setattr(employees_mod, "log_audit_event", lambda *a, **k: None)
    monkeypatch.setattr(employees_mod, "send_manager_invite_email", lambda *a, **k: True)
    return db


def _make_app_client(app, user_id):
    from auth import auth_bp  # needed so employees.url_for("auth.invite_accept") resolves
    app.register_blueprint(auth_bp)
    app.secret_key = "test"
    app.config["TESTING"] = True
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["org_id"] = ORG_A
        sess["session_token"] = "tok"
    return c


@pytest.fixture
def admin_client(env):
    app = Flask(__name__)
    app.register_blueprint(employees_mod.employees_bp, url_prefix="/api")
    app.register_blueprint(meetings_mod.meetings_bp, url_prefix="/api")
    return _make_app_client(app, ADMIN_USER)


@pytest.fixture
def mgr_a_client(env):
    app = Flask(__name__)
    app.register_blueprint(employees_mod.employees_bp, url_prefix="/api")
    app.register_blueprint(meetings_mod.meetings_bp, url_prefix="/api")
    return _make_app_client(app, MANAGER_A)


@pytest.fixture
def mgr_b_client(env):
    app = Flask(__name__)
    app.register_blueprint(employees_mod.employees_bp, url_prefix="/api")
    app.register_blueprint(meetings_mod.meetings_bp, url_prefix="/api")
    return _make_app_client(app, MANAGER_B)


def _emp_id(db, employee_id):
    return db.employees.find_one({"employee_id": employee_id})["_id"]


# ── 1. Employee-scoping helpers (unit) ──────────────────────────────────

def test_admin_scope_filter_is_empty(env, admin_client):
    with admin_client.application.test_request_context("/"):
        session["user_id"] = ADMIN_USER
        assert employees_mod._employee_scope_filter(env, ORG_A) == {}


def test_manager_scope_filter_is_reports_only(env, mgr_a_client):
    with mgr_a_client.application.test_request_context("/"):
        session["user_id"] = MANAGER_A
        assert employees_mod._employee_scope_filter(env, ORG_A) == {"reports_to": ObjectId(EMP_A)}


def test_manager_scope_filter_malformed_link_fails_closed(env, mgr_a_client):
    env.users.insert_one({
        "_id": ObjectId("555555555555555555555555"), "role": "manager",
        "org_id": ObjectId(ORG_A), "linked_employee_id": "not-an-objectid?!",
    })
    with mgr_a_client.application.test_request_context("/"):
        session["user_id"] = "555555555555555555555555"
        assert employees_mod._employee_scope_filter(env, ORG_A) == employees_mod._NEVER_MATCH


def test_manager_scope_filter_missing_link_fails_closed(env, mgr_a_client):
    env.users.insert_one({
        "_id": ObjectId("555555555555555555555555"), "role": "manager",
        "org_id": ObjectId(ORG_A), "linked_employee_id": None,
    })
    with mgr_a_client.application.test_request_context("/"):
        session["user_id"] = "555555555555555555555555"
        assert employees_mod._employee_scope_filter(env, ORG_A) == employees_mod._NEVER_MATCH


def test_manager_accessible_only_own_team(env, mgr_a_client):
    rep_a = env.employees.find_one({"employee_id": "EMP101"})          # reports to EMP_A
    rep_b = env.employees.find_one({"employee_id": "EMP201"})          # reports to EMP_B
    mgr_a = env.employees.find_one({"employee_id": "EMP100"})          # own record
    mgr_b = env.employees.find_one({"employee_id": "EMP200"})
    with mgr_a_client.application.test_request_context("/"):
        session["user_id"] = MANAGER_A
        assert employees_mod._employee_accessible(env, ORG_A, rep_a) is True
        assert employees_mod._employee_accessible(env, ORG_A, mgr_a) is True  # own doc
        assert employees_mod._employee_accessible(env, ORG_A, rep_b) is False
        assert employees_mod._employee_accessible(env, ORG_A, mgr_b) is False


# ── 2. Per-route GET / PUT / DELETE permission matrix ───────────────────

def test_manager_can_get_own_report(mgr_a_client, env):
    rid = str(_emp_id(env, "EMP101"))
    assert mgr_a_client.get(f"/api/employees/{rid}").status_code == 200


def test_manager_get_other_team_member_forbidden(mgr_a_client, env):
    rid = str(_emp_id(env, "EMP201"))
    assert mgr_a_client.get(f"/api/employees/{rid}").status_code == 403


def test_manager_put_edits_own_report_fields(mgr_a_client, env):
    rid = str(_emp_id(env, "EMP101"))
    r = mgr_a_client.put(f"/api/employees/{rid}", json={"department": "New Dept", "position": "Senior"})
    assert r.status_code == 200
    emp = env.employees.find_one({"employee_id": "EMP101"})
    assert emp["department"] == "New Dept"
    assert emp["position"] == "Senior"


def test_manager_put_reports_to_denied_and_unchanged(mgr_a_client, env):
    rid = str(_emp_id(env, "EMP101"))
    r = mgr_a_client.put(f"/api/employees/{rid}", json={"reports_to": str(_emp_id(env, "EMP200"))})
    assert r.status_code == 403
    assert r.get_json()["error"] == "admin_required_for_reports_to"
    emp = env.employees.find_one({"employee_id": "EMP101"})
    assert emp["reports_to"] == ObjectId(EMP_A)  # unchanged


def test_manager_delete_own_report_forbidden(mgr_a_client, env):
    rid = str(_emp_id(env, "EMP101"))
    r = mgr_a_client.delete(f"/api/employees/{rid}")
    assert r.status_code == 403
    assert r.get_json()["error"] == "admin_required"
    assert env.employees.find_one({"_id": ObjectId(rid)}) is not None  # still exists


def test_admin_get_any_employee(admin_client, env):
    for emp_id in ("EMP100", "EMP101", "EMP200", "EMP201"):
        assert admin_client.get(f"/api/employees/{str(_emp_id(env, emp_id))}").status_code == 200


def test_admin_put_reports_to_allowed(admin_client, env):
    rid = str(_emp_id(env, "EMP101"))
    r = admin_client.put(f"/api/employees/{rid}", json={"reports_to": str(_emp_id(env, "EMP200"))})
    assert r.status_code == 200
    emp = env.employees.find_one({"employee_id": "EMP101"})
    assert emp["reports_to"] == ObjectId(EMP_B)


def test_admin_delete_allowed_and_cascades(admin_client, env):
    rid = str(_emp_id(env, "EMP101"))
    r = admin_client.delete(f"/api/employees/{rid}")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    assert env.employees.find_one({"_id": ObjectId(rid)}) is None
    assert env.sessions.count({"employee_id": ObjectId(rid)}) == 0
    assert env.notifications.count({}) == 0


def test_two_manager_list_isolation(mgr_a_client, mgr_b_client, env):
    # Manager A's /employees must never contain any of Manager B's team.
    # (The scoped list is reports_to == linked, so the managers' own records
    # aren't listed — only their direct reports.)
    a_ids = [e["employee_id"] for e in mgr_a_client.get("/api/employees?limit=200").get_json()["employees"]]
    b_ids = [e["employee_id"] for e in mgr_b_client.get("/api/employees?limit=200").get_json()["employees"]]
    assert "EMP101" in a_ids          # A's report
    assert "EMP201" not in a_ids      # B's report hidden from A
    assert "EMP201" in b_ids          # B's report
    assert "EMP101" not in b_ids      # A's report hidden from B


# ── 3. Meetings scoping ─────────────────────────────────────────────────

def test_manager_meetings_only_own_team(mgr_a_client, env):
    d = mgr_a_client.get("/api/meetings").get_json()
    ids = [m["employee_id"] for m in d["meetings"]]
    assert ids == [REP_A1]          # only A1, never B1
    assert REP_B1 not in ids


def test_manager_get_other_team_meeting_forbidden(mgr_a_client, env):
    b1 = env.meetings.find_one({"employee_id": ObjectId(REP_B1)})
    r = mgr_a_client.get(f"/api/meetings/{b1['_id']}")
    assert r.status_code == 403


def test_manager_dashboard_nonempty_and_scoped(mgr_a_client, env):
    d = mgr_a_client.get("/api/meetings/dashboard").get_json()
    emp_ids = {e["id"] for e in d["employees"]}
    assert REP_A1 in emp_ids
    assert EMP_A in emp_ids
    assert REP_B1 not in emp_ids   # other team never on the board
    assert REP_B1 not in {p["id"] for p in d["people"]}


def test_admin_meetings_unscoped(admin_client, env):
    d = admin_client.get("/api/meetings").get_json()
    ids = {m["employee_id"] for m in d["meetings"]}
    assert REP_A1 in ids and REP_B1 in ids


# ── 4. Manager-invite flow ──────────────────────────────────────────────

class _FakeGoogle:
    def __init__(self, state):
        self._state = state
    def authorize_redirect(self, uri):
        return redirect("https://accounts.google.com/authorize?fake=1")
    def authorize_access_token(self):
        return {"userinfo": dict(self._state)}


class _FakeOAuth:
    def __init__(self, state):
        self.google = _FakeGoogle(state)


@pytest.fixture
def auth_client(monkeypatch):
    db = FakeDB()
    _seed_invite_org(db)
    state = {}
    monkeypatch.setattr(auth_mod, "get_db", lambda: db)
    monkeypatch.setattr(auth_mod, "log_audit_event", lambda *a, **k: None)
    monkeypatch.setattr(auth_mod, "encrypt_fields", lambda pii: (dict(pii), "dek"))
    monkeypatch.setattr(auth_mod, "decrypt_fields", lambda enc, dek: enc or {})
    monkeypatch.setattr(auth_mod, "_record_active_session", lambda *a, **k: None)
    monkeypatch.setattr(login_flow, "_record_active_session", lambda *a, **k: None)
    monkeypatch.setattr(auth_mod, "oauth", _FakeOAuth(state))

    def set_userinfo(ui):
        state.clear()
        state.update(ui or {})

    app = Flask(__name__)
    app.register_blueprint(auth_mod.auth_bp)
    app.secret_key = "test"
    app.config["TESTING"] = True
    app.config["GOOGLE_REDIRECT_URI"] = "https://app.test/callback"
    c = app.test_client()
    c._db = db
    c._set_userinfo = set_userinfo
    return c


def _seed_invite_org(db):
    db.organizations.insert_one({"_id": ObjectId(ORG_A), "name": "Acme"})
    db.employees.insert_one({
        "_id": ObjectId(EMP_A), "employee_id": "EMP100", "org_id": ObjectId(ORG_A),
        "status": "active", "email_hash": blind_index("mgr-a@corp.com"),
        "encrypted": {"name": "Mgr A", "email": "mgr-a@corp.com"}, "wrapped_dek": "dek",
    })
    db.employees.insert_one({
        "_id": ObjectId(EMP_B), "employee_id": "EMP200", "org_id": ObjectId(ORG_A),
        "status": "active", "email_hash": None,
        "encrypted": {"name": "No Email", "email": None}, "wrapped_dek": "dek",
    })


def _invite_doc(emp_oid, token, email, status="pending", expires_in=7):
    now = datetime.now(timezone.utc)
    return {
        "org_id": ObjectId(ORG_A), "email": email,
        "email_hash": blind_index(email), "role": "manager",
        "linked_employee_id": emp_oid, "token": token, "status": status,
        "expires_at": now + timedelta(days=expires_in), "created_at": now,
    }


def test_invite_accept_invalid_token(auth_client):
    r = auth_client.get("/invite/accept?token=nope")
    assert r.status_code == 302
    assert "reason=invalid" in r.headers["Location"]


def test_invite_accept_already_used_single_user(auth_client):
    db = auth_client._db
    token = "tok-already"
    db.invites.insert_one(_invite_doc(ObjectId(EMP_A), token, "mgr-a@corp.com", status="accepted"))
    r = auth_client.get("/invite/accept?token=" + token)
    assert "reason=already_used" in r.headers["Location"]
    assert db.users.count({}) == 0


def test_invite_accept_superseded(auth_client):
    db = auth_client._db
    db.invites.insert_one(_invite_doc(ObjectId(EMP_A), "old-token", "mgr-a@corp.com", status="superseded", expires_in=-7))
    r = auth_client.get("/invite/accept?token=old-token")
    assert "reason=superseded" in r.headers["Location"]


def test_invite_accept_expired(auth_client):
    db = auth_client._db
    db.invites.insert_one(_invite_doc(ObjectId(EMP_A), "exp-token", "mgr-a@corp.com", expires_in=-1))
    r = auth_client.get("/invite/accept?token=exp-token")
    assert "reason=expired" in r.headers["Location"]


def test_google_callback_accept_creates_exactly_one_manager(auth_client):
    db = auth_client._db
    token = "fresh-token"
    db.invites.insert_one(_invite_doc(ObjectId(EMP_A), token, "mgr-a@corp.com"))
    auth_client._set_userinfo({
        "email": "mgr-a@corp.com", "name": "Mgr A", "sub": "g1", "picture": "p",
    })

    # Phase 1: /invite/accept stashes the token and kicks off OAuth.
    r1 = auth_client.get("/invite/accept?token=" + token)
    assert r1.status_code == 302
    assert "accounts.google.com" in r1.headers["Location"]

    # Phase 2: Google callback finalizes.
    r2 = auth_client.get("/google/callback")
    assert r2.status_code == 302
    assert "/dashboard.html?role=manager&welcome=1" in r2.headers["Location"]

    users = list(db.users.find({}))
    assert len(users) == 1
    u = users[0]
    assert u["role"] == "manager"
    assert u["linked_employee_id"] == ObjectId(EMP_A)
    invite = db.invites.find_one({"token": token})
    assert invite["status"] == "accepted"


def test_google_callback_double_accept_creates_no_second_user(auth_client):
    db = auth_client._db
    token = "fresh-token-2"
    db.invites.insert_one(_invite_doc(ObjectId(EMP_A), token, "mgr-a@corp.com"))
    auth_client._set_userinfo({
        "email": "mgr-a@corp.com", "name": "Mgr A", "sub": "g1",
    })
    auth_client.get("/invite/accept?token=" + token)
    r2 = auth_client.get("/google/callback")   # first success
    assert r2.status_code == 302

    # A second near-concurrent attempt on the same token must not insert
    # another user document (the CAS on status:pending rejects it).
    before = len(db.users.find({}))
    auth_client.get("/invite/accept?token=" + token)  # already accepted
    auth_client._set_userinfo({
        "email": "mgr-a@corp.com", "name": "Mgr A", "sub": "g1",
    })
    r3 = auth_client.get("/google/callback")
    assert r3.status_code == 302
    assert len(db.users.find({})) == before == 1


def test_google_callback_email_mismatch_no_user(auth_client):
    db = auth_client._db
    token = "mismatch-token"
    db.invites.insert_one(_invite_doc(ObjectId(EMP_A), token, "mgr-a@corp.com"))
    auth_client._set_userinfo({
        "email": "someone-else@corp.com", "name": "Nope", "sub": "g9",
    })
    auth_client.get("/invite/accept?token=" + token)
    r = auth_client.get("/google/callback")
    assert "reason=email_mismatch" in r.headers["Location"]
    assert db.users.count({}) == 0


def test_invite_manager_no_email_guard(admin_client, env):
    rid = str(_emp_id(env, "EMP200"))  # seeded with no email in the auth-side seed
    env.employees.update_one({"_id": ObjectId(rid)}, {"$set": {
        "encrypted": {"name": "No Email", "email": None}, "email_hash": None,
    }})
    r = admin_client.post(f"/api/employees/{rid}/invite-manager")
    assert r.status_code == 400
    assert r.get_json()["error"] == "no_email"
    assert env.invites.count({}) == 0


def test_invite_manager_supersedes_previous_pending(admin_client, env):
    rid = str(_emp_id(env, "EMP101"))
    env.employees.update_one({"_id": ObjectId(rid)}, {"$set": {
        "encrypted": {"name": "Report A1", "email": "rep-a1@corp.com"},
        "email_hash": blind_index("rep-a1@corp.com"),
    }})
    r1 = admin_client.post(f"/api/employees/{rid}/invite-manager").get_json()
    assert r1["ok"] is True

    # Capture the OLD pending invite before issuing the second one, so we can
    # assert on it deterministically (created_at can tie between two inserts).
    old_invite = env.invites.find_one({"status": "pending"})
    assert old_invite is not None

    r2 = admin_client.post(f"/api/employees/{rid}/invite-manager").get_json()
    assert r2["ok"] is True

    invites = list(env.invites.find({}))
    assert len(invites) == 2
    # The old token is superseded; exactly one invite remains pending.
    assert env.invites.find_one({"token": old_invite["token"]})["status"] == "superseded"
    assert sum(1 for i in invites if i["status"] == "pending") == 1

    # Old token now reads clearly as superseded, not the misleading already_used.
    r_old = admin_client.get("/invite/accept?token=" + old_invite["token"])
    assert "reason=superseded" in r_old.headers["Location"]


# ── 5. CSV reports_to_email resolution ──────────────────────────────────

@pytest.fixture
def csv_client(monkeypatch):
    import io as _io
    db = FakeDB()
    db.users.insert_one({"_id": ObjectId(ADMIN_USER), "role": "admin", "org_id": ObjectId(ORG_A)})
    emp_seq = {"n": 0}

    def fake_next(db_, org_id):
        emp_seq["n"] += 1
        return f"EMP{emp_seq['n']:03d}"
    monkeypatch.setattr(employees_mod, "_next_employee_id", fake_next)
    monkeypatch.setattr(employees_mod, "_require_auth", lambda: ORG_A)
    monkeypatch.setattr(employees_mod, "_require_admin", lambda: ORG_A)
    monkeypatch.setattr(employees_mod, "get_db", lambda: db)
    monkeypatch.setattr(employees_mod, "encrypt_fields", lambda pii: (dict(pii), "dek"))
    monkeypatch.setattr(employees_mod, "decrypt_fields", lambda enc, dek: enc or {})
    monkeypatch.setattr(employees_mod, "log_audit_event", lambda *a, **k: None)

    app = Flask(__name__)
    app.register_blueprint(employees_mod.employees_bp, url_prefix="/api")
    app.secret_key = "test"
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["user_id"] = ADMIN_USER
    c._db = db
    db._emp_seq = emp_seq
    c._io = _io
    return c


def _post_csv(client, rows):
    header = (
        "name,email,phone,department,position,employment_type,work_mode,"
        "joining_date,reports_to_email"
    )
    data = ("\n".join([header] + rows) + "\n").encode("utf-8")
    return client.post(
        "/api/employees/import",
        data={"file": (client._io.BytesIO(data), "employees.csv")},
        content_type="multipart/form-data",
    )


def test_csv_same_file_manager_and_report_linked(csv_client):
    db = csv_client._db
    r = _post_csv(csv_client, [
        "Report,rep@corp.com,555,Design,Designer,FT,Office,2026-01-01,mgr@corp.com",
        "Manager,mgr@corp.com,555,HR,Manager,FT,Office,2025-01-01,",
    ])
    data = r.get_json()
    assert data["created"] == 2
    assert data["reports_to_resolved"] == 1
    assert data["warnings"] == []
    report = db.employees.find_one({"employee_id": "EMP001"})
    manager = db.employees.find_one({"employee_id": "EMP002"})
    assert report["reports_to"] == manager["_id"]


def test_csv_unresolved_reports_to_warns_but_creates(csv_client):
    db = csv_client._db
    r = _post_csv(csv_client, [
        "Solo,solo@corp.com,555,Design,Designer,FT,Office,2026-01-01,ghost@corp.com",
    ])
    data = r.get_json()
    assert data["created"] == 1
    assert data["reports_to_unresolved"] == 1
    assert data["warnings"][0]["warning"] == "reports_to_email_not_found"
    emp = db.employees.find_one({"employee_id": "EMP001"})
    assert "reports_to" not in emp


def test_csv_blank_reports_to_no_warning(csv_client):
    r = _post_csv(csv_client, [
        "Solo,solo@corp.com,555,Design,Designer,FT,Office,2026-01-01,",
    ])
    data = r.get_json()
    assert data["created"] == 1
    assert data["warnings"] == []
    assert data["reports_to_unresolved"] == 0

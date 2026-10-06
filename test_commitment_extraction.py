"""
Phase extra — AI commitment extraction & resolution.

Verifies (via the same hand-rolled in-memory Mongo facade pattern as
test_phase2, no live DB):
  - new suggested items are stored as PENDING + confirmation_status="suggested"
    and never count as open promises / reminders / delete blockers
  - re-running extraction is idempotent (content-normalized dedupe, incl. a
    CANCELLED item from the same session)
  - verdicts are stored as metadata only, never touched on status, and obey
    confidence/verdict/overwrite rules
  - only a human can act: /ai-resolution accept/dismiss with error paths and
    manager-role isolation
  - confirm (PATCH) / reject (DELETE) turn a suggestion into a real item or
    remove it, and the dashboard/meeting-trader surfacing reflects all of that
"""
from datetime import datetime, timezone, timedelta
from bson import ObjectId

from flask import Flask
from pytest import fixture as _fixture

import commitment_extraction as ce
import conversation_memory as cm_mod
import meetings as meetings_mod
import reminders as reminders_mod

from audit_log import ACTION_MEMORY_AI_SUGGEST, ACTION_MEMORY_UPDATE, ACTION_MEETING_DELETE

ORG_A = "aaaaaaaaaaaaaaaaaaaaaaaa"
EMP_1 = "111111111111111111111111"
EMP_2 = "222222222222222222222222"
EMP_OTHER = "555555555555555555555555"
EMP_MGR_OTHER = "666666666666666666666666"
SESSION_1 = "333333333333333333333333"
OTHER_SESSION = "aaaaaaaaaaaaaaaaaaaaaaab"
ADMIN_USER = "999999999999999999999999"
MANAGER_USER = "888888888888888888888888"

NOW = datetime.now(timezone.utc)


def _bson_strip(v):
    """Mirror real BSON: datetimes are stored naive UTC, like Mongo does."""
    if isinstance(v, dict):
        return {k: _bson_strip(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_bson_strip(x) for x in v]
    if isinstance(v, datetime):
        return v if v.tzinfo is None else v.astimezone(timezone.utc).replace(tzinfo=None)
    return v


class FakeCollection:
    """Minimal in-memory collection supporting the ops the modules use."""

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

    def count_documents(self, filt, *a, **k):
        return sum(1 for d in self._docs if self._match(d, filt))

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
        self.conversation_memory = FakeCollection()
        self.meetings = FakeCollection()
        self.employees = FakeCollection()
        self.sessions = FakeCollection()
        self.notifications = FakeCollection()
        self.users = FakeCollection()
        self.audit_log = FakeCollection()


def _seed_user(db, role="admin", uid=ADMIN_USER, linked=None):
    doc = {"_id": ObjectId(uid), "role": role, "org_id": ObjectId(ORG_A)}
    if linked:
        doc["linked_employee_id"] = ObjectId(linked)
    db.users.insert_one(doc)


def _base_db():
    db = FakeDB()
    db.employees.insert_one({
        "_id": ObjectId(EMP_1), "employee_id": "EMP001", "name": "Harshit Rana",
        "position": "Product Designer", "department": "Design",
        "org_id": ObjectId(ORG_A), "status": "active",
    })
    db.sessions.insert_one({
        "_id": ObjectId(SESSION_1), "org_id": ObjectId(ORG_A),
        "employee_id": ObjectId(EMP_1), "status": "completed",
        "created_at": NOW - timedelta(hours=1),
        "transcript": {"edited": ""},
    })
    _seed_user(db)
    return db


def _session_doc(employee_id=EMP_1, session_id=SESSION_1, created_at=None):
    return {
        "_id": ObjectId(session_id),
        "org_id": ObjectId(ORG_A),
        "employee_id": ObjectId(employee_id),
        "status": "completed",
        "created_at": created_at or NOW - timedelta(hours=1),
        "transcript": {"edited": "The employee said they will fix the login flow next week."},
    }


class MockLLM:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def extract_commitments(self, transcript, prompt_items, session_date_iso, language="en"):
        self.calls.append({
            "transcript": transcript,
            "prompt_items": prompt_items,
            "session_date_iso": session_date_iso,
            "language": language,
        })
        return self.result


def _enabled(monkeypatch):
    monkeypatch.setenv("COMMITMENT_AI_ENABLED", "true")


def _disabled(monkeypatch):
    monkeypatch.delenv("COMMITMENT_AI_ENABLED", raising=False)


def _seed_open_confirmed(db, content="fix the login flow", status="PENDING",
                         session_id=OTHER_SESSION, due_at=None, with_resolution=False,
                         mtype="COMMITMENT"):
    doc = {
        "org_id": ObjectId(ORG_A),
        "employee_id": ObjectId(EMP_1),
        "session_id": ObjectId(session_id),
        "type": mtype,
        "content": content,
        "status": status,
        "due_at": due_at or (NOW + timedelta(days=2)),
        "archive": False,
        "priority": "medium",
        "created_at": NOW - timedelta(days=3),
        "status_history": [{"status": status, "changed_at": NOW, "changed_by": None}],
    }
    if with_resolution:
        doc["metadata"] = {"ai_resolution": {
            "session_id": str(OTHER_SESSION), "verdict": "done", "confidence": 0.9,
            "evidence": "they confirmed it's shipped", "suggested_at": NOW.isoformat(),
            "dismissed": False,
        }}
    return db.conversation_memory.insert_one(doc).inserted_id


def _seed_suggested(db, mtype="COMMITMENT", content="write the onboarding doc",
                    session_id=SESSION_1, due_at=None, employee_id=EMP_1):
    return db.conversation_memory.insert_one({
        "org_id": ObjectId(ORG_A),
        "employee_id": ObjectId(employee_id),
        "session_id": ObjectId(session_id),
        "type": mtype,
        "content": content,
        "status": "PENDING",
        "confirmation_status": "suggested",
        "due_at": due_at,
        "archive": False,
        "priority": "medium",
        "created_at": NOW,
        "metadata": {"source": "ai_transcript", "source_session_id": str(session_id)},
    }).inserted_id


# ── run_for_session: gating & extraction ────────────────────────────────


def test_disabled_flag_returns_skipped(fake, monkeypatch):
    _disabled(monkeypatch)
    db = fake
    db.conversation_memory  # ensure attribute exists
    r = ce.run_for_session(db, ORG_A, _session_doc(), MockLLM({}))
    assert r == {"skipped": "disabled"}
    assert db.conversation_memory.count_documents({}) == 0


def test_missing_transcript_returns_skipped(fake, monkeypatch):
    _enabled(monkeypatch)
    doc = _session_doc()
    doc["transcript"] = {}
    r = ce.run_for_session(fake, ORG_A, doc, MockLLM({"new_items": []}))
    assert r == {"skipped": "no_transcript"}


def test_suggested_new_items_stored_provisionally(fake, monkeypatch):
    _enabled(monkeypatch)
    evidence = "quote \u2014 " + ("x" * 300)
    llm = MockLLM({"new_items": [
        {"type": "COMMITMENT", "content": "  Fix   the login flow ", "owner": "Manager", "evidence": evidence},
        {"type": "FOLLOW_UP", "content": "Send the Q3 report by Friday",
         "due_at_iso": (NOW + timedelta(days=7)).date().isoformat()},
        {"type": "IDEA", "content": "not a trackable"},
        {"type": "COMMITMENT", "content": "   "},
    ], "resolutions": []})

    r = ce.run_for_session(fake, ORG_A, _session_doc(), llm)

    assert r["new_items_created"] == 2
    assert r["resolutions_stored"] == 0
    rows = list(fake.conversation_memory.find())
    assert len(rows) == 2
    by_content = {row["content"]: row for row in rows}

    fix = by_content["Fix the login flow"]
    assert fix["confirmation_status"] == "suggested"
    assert fix["status"] == "PENDING"
    assert fix["session_id"] == ObjectId(SESSION_1)
    assert fix["metadata"]["source"] == "ai_transcript"
    assert fix["metadata"]["source_session_id"] == str(SESSION_1)
    assert fix["metadata"]["owner_hint"] == "manager"
    assert fix["metadata"]["evidence"] == evidence[:200] and len(fix["metadata"]["evidence"]) == 200
    assert fix["created_by"] is None
    assert fix["status_history"][0]["status"] == "PENDING"

    follow = by_content["Send the Q3 report by Friday"]
    assert follow["type"] == "FOLLOW_UP"
    assert follow["status"] == "PENDING"
    assert follow["confirmation_status"] == "suggested"

    audit = list(fake.audit_log.find({"action": ACTION_MEMORY_AI_SUGGEST}))
    assert len(audit) == 1
    assert audit[0]["meta"] == {"new_items_created": 2, "resolutions_stored": 0}
    assert audit[0]["target_id"] == str(SESSION_1)

    assert len(llm.calls) == 1
    assert not llm.calls[0]["prompt_items"]
    assert llm.calls[0]["language"] == "en"


def test_new_items_deduped_on_rerun(fake, monkeypatch):
    _enabled(monkeypatch)
    payload = {"new_items": [
        {"type": "COMMITMENT", "content": "Send the Q3 report"},
        {"type": "COMMITMENT", "content": "send the   Q3 REPORT"},
    ], "resolutions": []}
    ce.run_for_session(fake, ORG_A, _session_doc(), MockLLM(payload))
    assert fake.conversation_memory.count_documents({}) == 1
    r = ce.run_for_session(fake, ORG_A, _session_doc(), MockLLM(payload))
    assert r["new_items_created"] == 0
    assert fake.conversation_memory.count_documents({}) == 1


def test_new_items_deduped_against_existing_confirmed_item(fake, monkeypatch):
    _enabled(monkeypatch)
    fake.conversation_memory.insert_one({
        "org_id": ObjectId(ORG_A), "employee_id": ObjectId(EMP_1),
        "session_id": ObjectId(OTHER_SESSION), "type": "COMMITMENT",
        "content": "fix the login flow", "status": "PENDING",
        "due_at": NOW + timedelta(days=1), "archive": False,
    })
    r = ce.run_for_session(fake, ORG_A, _session_doc(), MockLLM({
        "new_items": [{"type": "COMMITMENT", "content": "FIX THE LOGIN FLOW"}], "resolutions": [],
    }))
    assert r["new_items_created"] == 0
    assert fake.conversation_memory.count_documents({}) == 1


def test_cancelled_duplicate_blocks_same_session_only(fake, monkeypatch):
    _enabled(monkeypatch)
    fake.conversation_memory.insert_one({
        "org_id": ObjectId(ORG_A), "employee_id": ObjectId(EMP_1),
        "session_id": ObjectId(SESSION_1), "type": "COMMITMENT",
        "content": "migrate the infra", "status": "CANCELLED", "due_at": None,
        "archive": False,
        "metadata": {"source_session_id": str(SESSION_1)},
    })
    fake.conversation_memory.insert_one({
        "org_id": ObjectId(ORG_A), "employee_id": ObjectId(EMP_1),
        "session_id": ObjectId(OTHER_SESSION), "type": "COMMITMENT",
        "content": "archive the docs", "status": "CANCELLED", "due_at": None,
        "archive": False,
        "metadata": {"source_session_id": str(OTHER_SESSION)},
    })
    payload = {"new_items": [
        {"type": "COMMITMENT", "content": "migrate the infra"},   # same session cancelled -> blocked
        {"type": "COMMITMENT", "content": "archive the docs"},    # other session cancelled -> allowed
    ], "resolutions": []}
    ce.run_for_session(fake, ORG_A, _session_doc(), MockLLM(payload))
    legit = [m for m in fake.conversation_memory.find({"confirmation_status": "suggested"})]
    assert [m["content"] for m in legit] == ["archive the docs"]


def test_resolution_stored_never_changes_status(fake, monkeypatch):
    _enabled(monkeypatch)
    i1 = _seed_open_confirmed(fake, "fix the login flow")
    i2 = _seed_open_confirmed(fake, "get the report", mtype="FOLLOW_UP")
    llm = MockLLM({"new_items": [], "resolutions": [
        {"ref": "i1", "verdict": "Done", "confidence": 0.9, "evidence": "live in prod now"},
        {"ref": "i2", "verdict": "in_progress", "confidence": 0.7},
        {"ref": "i3", "verdict": "done", "confidence": 0.9},        # unknown ref
        {"ref": "i1", "verdict": "done", "confidence": 0.4},        # too low
        {"ref": "i2", "verdict": "unclear", "confidence": 0.9},     # not actionable
    ]})

    r = ce.run_for_session(fake, ORG_A, _session_doc(), llm)

    assert r["resolutions_stored"] == 2
    m1 = fake.conversation_memory.find_one({"_id": i1})
    m2 = fake.conversation_memory.find_one({"_id": i2})
    assert m1["status"] == "PENDING"
    assert m1["metadata"]["ai_resolution"]["verdict"] == "done"
    assert m1["metadata"]["ai_resolution"]["confidence"] == 0.9
    assert m1["metadata"]["ai_resolution"]["evidence"] == "live in prod now"
    assert "accepted" not in m1["metadata"]["ai_resolution"]
    assert m2["status"] == "PENDING"
    assert m2["metadata"]["ai_resolution"]["verdict"] == "in_progress"
    assert m2["metadata"]["ai_resolution"]["session_id"] == str(SESSION_1)
    assert "accepted" not in m1["metadata"]["ai_resolution"]
    # The model saw the open items as short refs, never ids.
    prompt_refs = [p["ref"] for p in llm.calls[0]["prompt_items"]]
    assert prompt_refs == ["i1", "i2"]
    assert all("_id" not in p for p in llm.calls[0]["prompt_items"])


def test_same_session_rerun_keeps_existing_resolution(fake, monkeypatch):
    _enabled(monkeypatch)
    i1 = _seed_open_confirmed(fake, "fix the login flow", with_resolution=True)
    # A real first run stamps the resolution with THIS session's id; re-running
    # the same session must keep the verdict instead of churning it.
    m = fake.conversation_memory.find_one({"_id": i1})
    m["metadata"]["ai_resolution"]["session_id"] = str(SESSION_1)
    fake.conversation_memory.update_one({"_id": i1}, {"$set": {"metadata": m["metadata"]}})
    payload = {"new_items": [], "resolutions": [
        {"ref": "i1", "verdict": "in_progress", "confidence": 0.9},
    ]}
    r = ce.run_for_session(fake, ORG_A, _session_doc(), MockLLM(payload))
    assert r["resolutions_stored"] == 0
    m = fake.conversation_memory.find_one({"_id": i1})
    assert m["metadata"]["ai_resolution"]["verdict"] == "done"


def test_accepted_resolution_never_reshown(fake, monkeypatch):
    _enabled(monkeypatch)
    _seed_open_confirmed(fake, "fix the login flow", with_resolution=True)
    m = fake.conversation_memory.find_one({"content": "fix the login flow"})
    m["metadata"]["ai_resolution"]["accepted"] = True
    fake.conversation_memory.update_one({"_id": m["_id"]}, {"$set": {"metadata": m["metadata"]}})
    r = ce.run_for_session(fake, ORG_A, _session_doc(), MockLLM({
        "new_items": [], "resolutions": [{"ref": "i1", "verdict": "done", "confidence": 0.9}],
    }))
    assert r["resolutions_stored"] == 0
    m = fake.conversation_memory.find_one({"_id": m["_id"]})
    assert m["metadata"]["ai_resolution"]["accepted"] is True


def test_fallback_payload_writes_nothing(fake, monkeypatch):
    _enabled(monkeypatch)
    r = ce.run_for_session(fake, ORG_A, _session_doc(), MockLLM({
        "is_fallback": True, "new_items": [{"type": "COMMITMENT", "content": "should not persist"}],
    }))
    assert r == {"skipped": "fallback"}
    assert fake.conversation_memory.count_documents({}) == 0


# ── /conversation-memory/<id>/ai-resolution endpoint ────────────────────


def _app(monkeypatch, db, uid=ADMIN_USER, name="Admin User"):
    monkeypatch.setattr(cm_mod, "get_db", lambda: db)
    monkeypatch.setattr(meetings_mod, "get_db", lambda: db)
    monkeypatch.setattr(cm_mod, "_require_auth", lambda: ORG_A)
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
    app.register_blueprint(cm_mod.conversation_memory_bp, url_prefix="/api")
    app.register_blueprint(meetings_mod.meetings_bp, url_prefix="/api")
    app.config["TESTING"] = True
    app.secret_key = "test"
    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess["user_id"] = uid
            sess["user_name"] = name
        yield c


@_fixture
def fake(monkeypatch):
    return _base_db()


@_fixture
def admin_client(monkeypatch, fake):
    yield from _app(monkeypatch, fake)


def _ai_resolution_item(db, mid=None, confirmed=True):
    doc = {
        "org_id": ObjectId(ORG_A), "employee_id": ObjectId(EMP_1),
        "session_id": ObjectId(OTHER_SESSION), "type": "COMMITMENT",
        "content": "fix the login flow", "status": "PENDING",
        "due_at": NOW + timedelta(days=2), "archive": False,
        "priority": "medium", "created_at": NOW,
        "confirmation_status": "confirmed" if confirmed else "suggested",
        "metadata": {"ai_resolution": {
            "session_id": str(SESSION_1), "verdict": "done", "confidence": 0.9,
            "evidence": "live in prod", "suggested_at": NOW.isoformat(), "dismissed": False,
        }},
        "status_history": [{"status": "PENDING", "changed_at": NOW, "changed_by": None}],
    }
    if mid:
        doc["_id"] = ObjectId(mid)
    return db.conversation_memory.insert_one(doc).inserted_id


def test_ai_resolution_accept_completes_item(admin_client, fake):
    mid = _ai_resolution_item(fake)
    r = admin_client.post(f"/api/conversation-memory/{mid}/ai-resolution", json={"action": "accept"})
    assert r.status_code == 200
    m = fake.conversation_memory.find_one({"_id": mid})
    assert m["status"] == "COMPLETED"
    assert m["completed_at"] is not None
    assert m["metadata"]["ai_resolution"]["accepted"] is True
    assert m["metadata"]["ai_resolution"]["dismissed"] is False
    assert m["status_history"][-1]["status"] == "COMPLETED"
    assert m["status_history"][-1]["changed_by"] == ObjectId(ADMIN_USER)
    assert m["confirmation_status"] == "confirmed"
    audit = list(fake.audit_log.find({"action": ACTION_MEMORY_UPDATE}))
    assert len(audit) == 1
    assert audit[0]["meta"]["ai_resolution"] == "accept"


def test_ai_resolution_dismiss_keeps_status(admin_client, fake):
    mid = _ai_resolution_item(fake)
    r = admin_client.post(f"/api/conversation-memory/{mid}/ai-resolution", json={"action": "dismiss"})
    assert r.status_code == 200
    m = fake.conversation_memory.find_one({"_id": mid})
    assert m["status"] == "PENDING"
    assert m.get("completed_at") is None
    assert m["metadata"]["ai_resolution"]["dismissed"] is True
    assert m["metadata"]["ai_resolution"]["dismissed_at"]


def test_ai_resolution_invalid_action(admin_client, fake):
    mid = _ai_resolution_item(fake)
    r = admin_client.post(f"/api/conversation-memory/{mid}/ai-resolution", json={"action": "nuke"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "invalid_action"


def test_ai_resolution_accept_non_trackable_400(admin_client, fake):
    mid = fake.conversation_memory.insert_one({
        "org_id": ObjectId(ORG_A), "employee_id": ObjectId(EMP_1), "type": "NOTE",
        "content": "a note", "status": "SAVED", "archive": False,
        "metadata": {"ai_resolution": {"verdict": "done", "confidence": 0.9, "dismissed": False}},
    }).inserted_id
    r = admin_client.post(f"/api/conversation-memory/{mid}/ai-resolution", json={"action": "accept"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "not_trackable"


def test_ai_resolution_dismiss_without_suggestion_400(admin_client, fake):
    mid = fake.conversation_memory.insert_one({
        "org_id": ObjectId(ORG_A), "employee_id": ObjectId(EMP_1), "type": "COMMITMENT",
        "content": "no suggestion here", "status": "PENDING", "archive": False, "metadata": {},
    }).inserted_id
    r = admin_client.post(f"/api/conversation-memory/{mid}/ai-resolution", json={"action": "dismiss"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "no_ai_resolution"


@_fixture
def mgr_client(monkeypatch):
    db = FakeDB()
    db.employees.insert_one({
        "_id": ObjectId(EMP_1), "employee_id": "EMP001", "name": "Manager",
        "department": "Design", "org_id": ObjectId(ORG_A), "status": "active",
    })
    db.employees.insert_one({
        "_id": ObjectId(EMP_2), "employee_id": "EMP002", "name": "Direct Report",
        "department": "Design", "org_id": ObjectId(ORG_A), "status": "active",
        "reports_to": ObjectId(EMP_1),
    })
    db.employees.insert_one({
        "_id": ObjectId(EMP_OTHER), "employee_id": "EMP003", "name": "Other Team",
        "department": "Eng", "org_id": ObjectId(ORG_A), "status": "active",
        "reports_to": ObjectId(EMP_MGR_OTHER),
    })
    _seed_user(db, role="manager", uid=MANAGER_USER, linked=EMP_1)
    yield from _app(monkeypatch, db, uid=MANAGER_USER, name="Manager")


def test_ai_resolution_cross_team_denied(mgr_client):
    db = cm_mod.get_db()
    mid = db.conversation_memory.insert_one({
        "org_id": ObjectId(ORG_A), "employee_id": ObjectId(EMP_OTHER),
        "type": "COMMITMENT", "content": "other team", "status": "PENDING",
        "archive": False,
        "metadata": {"ai_resolution": {"verdict": "done", "confidence": 0.9, "dismissed": False}},
    }).inserted_id
    r = mgr_client.post(f"/api/conversation-memory/{mid}/ai-resolution", json={"action": "accept"})
    assert r.status_code == 403
    assert r.get_json()["error"] == "forbidden"
    m = db.conversation_memory.find_one({"_id": mid})
    assert m["status"] == "PENDING"


# ── confirmed flows for suggestions (PATCH confirm, DELETE reject) ──────


def test_confirm_and_reject_suggested_item(admin_client, fake):
    sid_a = _seed_suggested(fake)
    sid_b = _seed_suggested(fake, content="reject this one")

    r = admin_client.patch(f"/api/conversation-memory/{sid_a}",
                           json={"confirmation_status": "confirmed"})
    assert r.status_code == 200
    m = fake.conversation_memory.find_one({"_id": sid_a})
    assert m["confirmation_status"] == "confirmed"

    r = admin_client.delete(f"/api/conversation-memory/{sid_b}")
    assert r.status_code == 200
    assert fake.conversation_memory.find_one({"_id": sid_b}) is None


# ── delete guard + dashboard counting ───────────────────────────────────


def _seed_meeting(db, employee_id=EMP_1, status="scheduled"):
    return db.meetings.insert_one({
        "org_id": ObjectId(ORG_A), "employee_id": ObjectId(employee_id),
        "title": "1:1", "status": status,
        "scheduled_at": NOW + timedelta(days=1),
        "created_at": NOW, "updated_at": NOW,
    }).inserted_id


def test_delete_meeting_not_blocked_by_suggested_item(admin_client, fake):
    mid = _seed_meeting(fake)
    _seed_suggested(fake)  # unconfirmed suggestion for EMP_1
    r = admin_client.delete(f"/api/meetings/{mid}")
    assert r.status_code == 200
    assert fake.meetings.find_one({"_id": mid}) is None


def test_delete_meeting_blocked_by_confirmed_item_but_force_ok(admin_client, fake):
    mid = _seed_meeting(fake)
    _seed_open_confirmed(fake, "a real open promise")
    r = admin_client.delete(f"/api/meetings/{mid}")
    assert r.status_code == 409
    assert r.get_json() == {"error": "open_promises", "open_promises": 1}
    assert fake.meetings.find_one({"_id": mid}) is not None

    r = admin_client.delete(f"/api/meetings/{mid}?force=true")
    assert r.status_code == 200
    assert fake.meetings.find_one({"_id": mid}) is None
    audit = list(fake.audit_log.find({"action": ACTION_MEETING_DELETE}))
    assert any(a["meta"].get("forced") is True and a["meta"].get("open_promises") == 1
               for a in audit)


def test_dashboard_excludes_suggestions_and_exposes_ai_suggestions(admin_client, fake):
    _seed_meeting(fake)
    confirmed_with_res = _seed_open_confirmed(fake, "fix the login flow", with_resolution=True)
    _seed_open_confirmed(fake, "second open promise")
    _seed_suggested(fake)

    r = admin_client.get("/api/meetings/dashboard")
    assert r.status_code == 200
    d = r.get_json()
    person = next(p for p in d["people"] if p["id"] == EMP_1)

    assert person["counts"]["pending_commitments"] == 2
    assert person["open_items"] and all(i["content"] != "write the onboarding doc"
                                       for i in person["open_items"])
    assert [n["content"] for n in person["ai_suggestions"]["new_items"]] == ["write the onboarding doc"]
    assert [res["item_id"] for res in person["ai_suggestions"]["resolutions"]] == [str(confirmed_with_res)]
    assert person["surfaced"] and all(i["content"] != "write the onboarding doc"
                                      for i in person["surfaced"])
    assert d["counters"]["pending_commitments"] == 2

    # Accept the resolution: it leaves the suggestions and the counts drop.
    r = admin_client.post(
        f"/api/conversation-memory/{confirmed_with_res}/ai-resolution", json={"action": "accept"}
    )
    assert r.status_code == 200
    r = admin_client.get("/api/meetings/dashboard")
    person = next(p for p in r.get_json()["people"] if p["id"] == EMP_1)
    assert person["ai_suggestions"]["resolutions"] == []
    assert person["counts"]["pending_commitments"] == 1


def test_reminders_surface_and_overdue_skip_suggestions(fake, monkeypatch):
    _enabled(monkeypatch)
    confirmed_id = fake.conversation_memory.insert_one({
        "org_id": ObjectId(ORG_A), "employee_id": ObjectId(EMP_1),
        "session_id": ObjectId(OTHER_SESSION), "type": "COMMITMENT",
        "content": "real overdue promise", "status": "PENDING",
        "due_at": NOW - timedelta(days=1), "archive": False,
    }).inserted_id
    _seed_suggested(fake, content="suggested overdue 1", due_at=NOW - timedelta(days=1))
    _seed_suggested(fake, content="suggested overdue 2", mtype="FOLLOW_UP",
                    due_at=NOW - timedelta(days=1))

    memory = list(fake.conversation_memory.find())
    surfaces = reminders_mod.surface_items(memory, {str(ObjectId(EMP_1))}, NOW)
    surfaced_content = [i["content"] for i in surfaces.get(str(ObjectId(EMP_1)), [])]
    assert surfaced_content == ["real overdue promise"]

    created = reminders_mod.ensure_due_notifications(fake, ORG_A, NOW)
    assert created == 1
    notes = list(fake.notifications.find({"type": "memory_overdue"}))
    assert len(notes) == 1
    assert notes[0]["memory_id"] == confirmed_id
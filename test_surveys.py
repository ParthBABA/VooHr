"""Tests for the surveys module (survey templates + survey responses).

Uses an in-memory Mongo facade + Flask test client (no live DB), matching
the approach of the Phase 2 meeting tracker suite.  Verifies:
  - org-level isolation for templates and responses
  - template CRUD + question normalization + "in use" guard
  - response validation per question type (Likert / numeric / text)
  - engagement_score normalization companies
  - employee signals.engagement_survey_score sync on create/update/delete
"""
import os
import pytest
from datetime import datetime, timezone
from bson import ObjectId

from flask import Flask

# Config requires SECRET_KEY at import time, and the route blueprints pull in
# config transitively at collection — pin a test value first.
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-survey-tests")

import surveys as surveys_mod

ORG_A = "aaaaaaaaaaaaaaaaaaaaaaaa"
ORG_B = "bbbbbbbbbbbbbbbbbbbbbbbb"
EMP_1 = "111111111111111111111111"
EMP_B = "222222222222222222222222"
ADMIN_USER = "999999999999999999999999"


class FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, key, direction=None):
        if isinstance(key, str):
            key = [(key, direction or 1)]
        sign = {1: 1, -1: -1}
        for k, dirn in reversed(key):
            self._docs.sort(key=lambda d: d.get(k), reverse=(sign.get(dirn, 1) == -1))
        return self

    def skip(self, n):
        self._docs = self._docs[n:]
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    def __iter__(self):
        return iter(self._docs)

    def __len__(self):
        return len(self._docs)


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

    def find(self, filt=None, *args, **kw):
        return FakeCursor([dict(d) for d in self._docs if self._match(d, filt or {})])

    def find_one(self, filt=None, *args, **kw):
        matches = [d for d in self._docs if self._match(d, filt or {})]
        sort = kw.get("sort")
        if sort:
            sign = {1: 1, -1: -1}
            for k, dirn in reversed(sort):
                matches.sort(key=lambda d: d.get(k), reverse=(sign.get(dirn, 1) == -1))
        return dict(matches[0]) if matches else None

    def count_documents(self, filt):
        return sum(1 for d in self._docs if self._match(d, filt))

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

    def aggregate(self, pipeline):
        matches = self._docs
        for stage in pipeline:
            if "$match" in stage:
                matches = [d for d in matches if self._match(d, stage["$match"])]
            elif "$group" in stage:
                group = stage["$group"]
                key_field = group["_id"][1:] if group["_id"].startswith("$") else group["_id"]
                grouped = {}
                for d in matches:
                    k = d.get(key_field)
                    entry = grouped.setdefault(
                        k, {"_id": k, "n": 0, "_counted": set()}
                    )
                    bucket = entry["_counted"]
                    if id(d) not in bucket:
                        bucket.add(id(d))
                        entry["n"] += 1
                matches = [
                    {"_id": k, "n": v["n"]} for k, v in grouped.items()
                ]
        return matches

    def create_index(self, *a, **k):
        return None


class FakeDB:
    def __init__(self):
        self.survey_templates = FakeCollection()
        self.survey_responses = FakeCollection()
        self.employees = FakeCollection()
        self.users = FakeCollection()
        self.audit_log = FakeCollection()


@pytest.fixture
def fake():
    db = FakeDB()
    db.employees.insert_one({
        "_id": ObjectId(EMP_1), "employee_id": "EMP001", "name": "Harshit Rana",
        "position": "Product Designer", "department": "Design",
        "org_id": ObjectId(ORG_A), "status": "active",
    })
    db.employees.insert_one({
        "_id": ObjectId(EMP_B), "employee_id": "EMP099", "name": "Other Org Emp",
        "position": "Engineer", "department": "Eng",
        "org_id": ObjectId(ORG_B), "status": "active",
    })
    return db


@pytest.fixture
def client(monkeypatch, fake):
    monkeypatch.setattr(surveys_mod, "get_db", lambda: fake)
    monkeypatch.setattr(surveys_mod, "_require_auth", lambda: ORG_A)

    app = Flask(__name__)
    app.register_blueprint(surveys_mod.surveys_bp, url_prefix="/api")
    app.config["TESTING"] = True
    app.secret_key = "test"
    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess["user_id"] = ADMIN_USER
        yield c


# ── Helpers ───────────────────────────────────────────────────────────

def _create_template(client, questions=None, title="Pulse Survey", **extra):
    body = {"title": title, "questions": questions or [
        {"text": "How energized do you feel?", "type": "likert_5"},
        {"text": "Would you recommend this team?", "type": "likert_10"},
    ]}
    body.update(extra)
    return client.post("/api/survey-templates", json=body)


def _submit_response(client, template_id, employee_id=EMP_1, answers=None, **extra):
    body = {
        "template_id": template_id,
        "employee_id": employee_id,
        "answers": answers or [
            {"question_id": "q1", "value": 5},
            {"question_id": "q2", "value": 10},
        ],
    }
    body.update(extra)
    return client.post("/api/survey-responses", json=body)


# ── Templates ─────────────────────────────────────────────────────────

class TestTemplates:
    def test_create_template_ok(self, client):
        r = _create_template(client)
        assert r.status_code == 201
        d = r.get_json()
        assert d["id"]
        assert d["title"] == "Pulse Survey"
        assert d["status"] == "draft"
        assert d["questions"][0]["id"] == "q1"
        assert d["questions"][0]["type"] == "likert_5"
        assert d["questions"][1]["id"] == "q2"
        assert d["questions"][1]["type"] == "likert_10"

    def test_create_template_requires_title(self, client):
        r = client.post("/api/survey-templates", json={"questions": [{"text": "x", "type": "text"}]})
        assert r.status_code == 400
        assert r.get_json()["error"] == "title_required"

    def test_create_template_invalid_question_type(self, client):
        r = _create_template(client, questions=[{"text": "x", "type": "bogus"}])
        assert r.status_code == 400
        assert r.get_json()["error"] == "invalid_question_type"

    def test_create_template_rejects_duplicate_client_ids(self, client):
        # Ids are regenerated positionally, so list ordering is authoritative.
        r = _create_template(client, questions=[
            {"id": "q1", "text": "A", "type": "text"},
            {"id": "q1", "text": "B", "type": "text"},
        ])
        assert r.status_code == 201
        d = r.get_json()
        assert [q["id"] for q in d["questions"]] == ["q1", "q2"]

    def test_list_templates_paginated_and_status_filter(self, client):
        _create_template(client, title="Weekly")
        _create_template(client, title="Quarterly")
        _create_template(client, title="Archived", status="archived")

        r = client.get("/api/survey-templates?status=archived")
        data = r.get_json()
        assert data["total"] == 1
        assert data["items"][0]["title"] == "Archived"

        r = client.get("/api/survey-templates?limit=1")
        data = r.get_json()
        assert data["total"] == 3
        assert len(data["items"]) == 1
        assert data["has_more"] is True

    def test_get_template(self, client):
        tid = _create_template(client).get_json()["id"]
        r = client.get(f"/api/survey-templates/{tid}")
        assert r.status_code == 200
        assert r.get_json()["id"] == tid

    def test_get_template_not_found(self, client):
        r = client.get("/api/survey-templates/%s" % ("0" * 24))
        assert r.status_code == 404

    def test_patch_template_edits_title_and_status(self, client):
        tid = _create_template(client).get_json()["id"]
        r = client.patch(f"/api/survey-templates/{tid}", json={"title": "Renamed", "status": "published"})
        assert r.status_code == 200
        d = r.get_json()
        assert d["title"] == "Renamed"
        assert d["status"] == "published"

    def test_patch_template_questions_blocked_when_in_use(self, client):
        tid = _create_template(client).get_json()["id"]
        _submit_response(client, tid)
        r = client.patch(f"/api/survey-templates/{tid}", json={"questions": [{"text": "Z", "type": "text"}]})
        assert r.status_code == 409
        assert r.get_json()["error"] == "template_in_use"

    def test_delete_template_in_use_blocked(self, client):
        tid = _create_template(client).get_json()["id"]
        _submit_response(client, tid)
        r = client.delete(f"/api/survey-templates/{tid}")
        assert r.status_code == 409
        assert r.get_json()["error"] == "template_in_use"

    def test_delete_template_unused_ok(self, client):
        tid = _create_template(client).get_json()["id"]
        r = client.delete(f"/api/survey-templates/{tid}")
        assert r.status_code == 200
        assert r.get_json()["ok"] is True


# ── Responses ─────────────────────────────────────────────────────────

class TestResponses:
    def test_submit_response_ok_and_score_normalized(self, client):
        tid = _create_template(client).get_json()["id"]
        r = _submit_response(client, tid)
        assert r.status_code == 201
        d = r.get_json()
        assert d["template_id"] == tid
        assert d["employee_id"] == EMP_1
        assert d["employee_name"] == ""
        # likert_5 (5→100) + likert_10 (10→100) both max → 100
        assert d["engagement_score"] == 100

    def test_submit_likert_low_score(self, client):
        tid = _create_template(client).get_json()["id"]
        r = _submit_response(client, tid, answers=[
            {"question_id": "q1", "value": 1},
            {"question_id": "q2", "value": 1},
        ])
        assert r.status_code == 201
        # likert_5 (1→0) + likert_10 (1→0) → 0
        assert r.get_json()["engagement_score"] == 0

    def test_submit_mixed_score_averages(self, client):
        tid = _create_template(client).get_json()["id"]
        r = _submit_response(client, tid, answers=[
            {"question_id": "q1", "value": 3},   # 50
            {"question_id": "q2", "value": 6},   # 55.6
        ])
        assert r.status_code == 201
        assert r.get_json()["engagement_score"] == 53

    def test_submit_text_only_response_has_no_score(self, client):
        tid = _create_template(client, questions=[
            {"text": "Anything else?", "type": "text"},
        ]).get_json()["id"]
        r = _submit_response(client, tid, answers=[
            {"question_id": "q1", "value": "Feeling okay"},
        ])
        assert r.status_code == 201
        assert r.get_json()["engagement_score"] is None

    def test_submit_unknown_template(self, client):
        r = _submit_response(client, "0" * 24)
        assert r.status_code == 404
        assert r.get_json()["error"] == "template_not_found"

    def test_submit_requires_template_id(self, client):
        r = _submit_response(client, None)
        assert r.status_code == 400
        assert r.get_json()["error"] == "template_id_required"

    def test_submit_requires_employee_id(self, client):
        tid = _create_template(client).get_json()["id"]
        r = _submit_response(client, tid, employee_id=None)
        assert r.status_code == 400
        assert r.get_json()["error"] == "employee_id_required"

    def test_submit_archived_template_rejected(self, client):
        tid = _create_template(client, status="archived").get_json()["id"]
        r = _submit_response(client, tid)
        assert r.status_code == 400
        assert r.get_json()["error"] == "template_archived"

    def test_submit_other_org_employee_rejected(self, client):
        tid = _create_template(client).get_json()["id"]
        r = _submit_response(client, tid, employee_id=EMP_B)
        assert r.status_code == 404
        assert r.get_json()["error"] == "employee_not_found"

    def test_submit_unknown_question_rejected(self, client):
        tid = _create_template(client).get_json()["id"]
        r = _submit_response(client, tid, answers=[{"question_id": "q9", "value": 3}])
        assert r.status_code == 400
        assert r.get_json()["error"] == "unknown_question"

    def test_submit_likert_out_of_range_rejected(self, client):
        tid = _create_template(client).get_json()["id"]
        r = _submit_response(client, tid, answers=[{"question_id": "q1", "value": 9}])
        assert r.status_code == 400
        assert r.get_json()["error"] == "answer_out_of_range"

    def test_submit_duplicate_question_rejected(self, client):
        tid = _create_template(client).get_json()["id"]
        r = _submit_response(client, tid, answers=[
            {"question_id": "q1", "value": 3},
            {"question_id": "q1", "value": 2},
        ])
        assert r.status_code == 400
        assert r.get_json()["error"] == "duplicate_question"

    def test_submit_syncs_employee_engagement_signal(self, client, fake):
        tid = _create_template(client).get_json()["id"]
        _submit_response(client, tid)
        emp = fake.employees.find_one({"_id": ObjectId(EMP_1)})
        assert emp["signals"]["engagement_survey_score"] == 100

    def test_update_response_resyncs_signal(self, client, fake):
        tid = _create_template(client).get_json()["id"]
        rid = _submit_response(client, tid).get_json()["id"]
        r = client.patch(f"/api/survey-responses/{rid}", json={"answers": [
            {"question_id": "q1", "value": 1},
            {"question_id": "q2", "value": 1},
        ]})
        assert r.status_code == 200
        assert r.get_json()["engagement_score"] == 0
        emp = fake.employees.find_one({"_id": ObjectId(EMP_1)})
        assert emp["signals"]["engagement_survey_score"] == 0

    def test_delete_response_resyncs_from_previous(self, client, fake):
        tid = _create_template(client).get_json()["id"]
        # First response: score 0; second response: score 100 (most recent).
        rid_old = _submit_response(client, tid, answers=[
            {"question_id": "q1", "value": 1}, {"question_id": "q2", "value": 1},
        ]).get_json()["id"]
        rid_new = _submit_response(client, tid).get_json()["id"]
        assert fake.employees.find_one({"_id": ObjectId(EMP_1)})["signals"]["engagement_survey_score"] == 100

        r = client.delete(f"/api/survey-responses/{rid_new}")
        assert r.status_code == 200
        assert fake.employees.find_one({"_id": ObjectId(EMP_1)})["signals"]["engagement_survey_score"] == 0

        client.delete(f"/api/survey-responses/{rid_old}")
        emp = fake.employees.find_one({"_id": ObjectId(EMP_1)})
        assert "engagement_survey_score" not in emp.get("signals", {})

    def test_list_responses_filters_by_template_and_employee(self, client):
        tid = _create_template(client).get_json()["id"]
        _submit_response(client, tid)
        _submit_response(client, tid)

        data = client.get(f"/api/survey-responses?template_id={tid}").get_json()
        assert data["total"] == 2

        data = client.get(f"/api/survey-responses?employee_id={EMP_1}").get_json()
        assert data["total"] == 2

    def test_get_response_not_found(self, client):
        r = client.get("/api/survey-responses/%s" % ("0" * 24))
        assert r.status_code == 404

    def test_delete_response_not_found(self, client):
        r = client.delete("/api/survey-responses/%s" % ("0" * 24))
        assert r.status_code == 404


# ── Org isolation ─────────────────────────────────────────────────────

class TestOrgIsolation:
    def test_org_a_cannot_see_org_b_template(self, client, fake):
        fake.survey_templates.insert_one({
            "_id": ObjectId(), "org_id": ObjectId(ORG_B),
            "title": "Foreign", "questions": [],
            "status": "published",
            "created_at": datetime.now(timezone.utc),
        })
        r = client.get("/api/survey-templates")
        assert r.get_json()["total"] == 0

    def test_org_a_cannot_read_org_b_response(self, client, fake):
        fake.survey_responses.insert_one({
            "_id": ObjectId(), "org_id": ObjectId(ORG_B),
            "template_id": ObjectId(), "employee_id": ObjectId(EMP_B),
            "answers": [], "submitted_by": None,
            "created_at": datetime.now(timezone.utc),
        })
        r = client.get("/api/survey-responses")
        assert r.get_json()["total"] == 0
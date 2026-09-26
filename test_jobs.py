"""Tests for the async translation / TTS background jobs (jobs.py).

The job endpoints are exercised against an isolated Flask app with the jobs
blueprint registered. Providers and the database are mocked — no real APIs or
MongoDB are contacted. The daemon `threading.Thread` is replaced with a
synchronous shim so each job runs to completion inside the request, making the
full "POST -> worker -> done -> notification" chain deterministic.
"""

import os
from datetime import datetime, timezone
from unittest import mock

# jobs -> sessions -> employees -> config requires SECRET_KEY at import time.
os.environ.setdefault("SECRET_KEY", "test-secret-key")

import bson
from bson import ObjectId
from flask import Flask, session as flask_session

import jobs as jobs_mod
from providers.storage import LocalStorage
from providers.tts_languages import UnsupportedTTSLanguageError

_ORG = "64b000000000000000000001"
_USER = "64b000000000000000000002"
_EMP = "64b000000000000000000003"
_SESSION = "64b000000000000000000004"


# ── Hermetic in-memory MongoDB ────────────────────────────────────────────


class _FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, key, direction):
        self._docs.sort(key=lambda d: d.get(key), reverse=(direction == -1))
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    def __iter__(self):
        return iter(self._docs)


class _FakeCollection:
    def __init__(self):
        self.docs = []

    def _match(self, doc, q):
        for k, v in q.items():
            if isinstance(v, dict) and "$ne" in v:
                if doc.get(k) == v["$ne"]:
                    return False
            elif doc.get(k) != v:
                return False
        return True

    def find_one(self, q, projection=None, sort=None):
        matches = [d for d in self.docs if self._match(d, q)]
        if sort:
            key, direction = sort[0]
            matches.sort(key=lambda d: d.get(key), reverse=(direction == -1))
        if not matches:
            return None
        doc = matches[0]
        if projection:
            return {k: doc[k] for k in doc if k in projection or k == "_id"}
        return dict(doc)

    def find(self, q):
        return _FakeCursor([d for d in self.docs if self._match(d, q)])

    def insert_one(self, doc):
        doc = dict(doc)
        doc.setdefault("_id", bson.ObjectId())
        self.docs.append(doc)
        return mock.Mock(inserted_id=doc["_id"])

    def _set_path(self, doc, path, value):
        """Apply a (possibly dotted) $set path the way MongoDB does.

        Sessions store per-language analyses under ``analyses.<language>``, so a
        flat ``doc.update()`` would create a literal ``"analyses.japanese"`` key
        and the tests would pass while production stored a nested document.
        """
        parts = path.split(".")
        node = doc
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        node[parts[-1]] = value

    def update_one(self, q, update, upsert=False):
        matches = [d for d in self.docs if self._match(d, q)]
        if not matches or "$set" not in update:
            return mock.Mock(matched_count=len(matches))
        matched = matches[0]
        for path, value in update["$set"].items():
            self._set_path(matched, path, value)
        for path in update.get("$unset") or {}:
            parts = path.split(".")
            node = matched
            for part in parts[:-1]:
                node = node.get(part)
                if not isinstance(node, dict):
                    node = None
                    break
            if isinstance(node, dict):
                node.pop(parts[-1], None)
        return mock.Mock(matched_count=1)

    def create_index(self, *a, **k):
        pass

    def clear(self):
        self.docs = []


class _FakeDB:
    def __init__(self):
        for name in (
            "rate_limits",
            "users",
            "sessions",
            "employees",
            "meetings",
            "notifications",
            "translation_jobs",
            "tts_jobs",
        ):
            setattr(self, name, _FakeCollection())

    def fresh(self):
        for coll in vars(self).values():
            coll.clear()
        return self

    def __getitem__(self, name):
        coll = getattr(self, name, None)
        if coll is None:
            coll = _FakeCollection()
            setattr(self, name, coll)
        return coll


_THREAD_LOCK = mock.Mock()


class _SyncThread:
    """Runs the job synchronously when .start() is called."""

    def __init__(self, target=None, args=(), daemon=False):
        self._target, self._args = target, args

    def start(self):
        if self._target:
            self._target(*self._args)


def _make_app():
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="test-secret-key")
    if "MONGO_CLIENT" in app.extensions:
        pass
    app.register_blueprint(jobs_mod.jobs_bp, url_prefix="/api")
    return app


def _make_client(db, monkeypatch, storage=None):
    import jobs
    import tts as tts_routes  # noqa: F401 - ensure session import parity

    def __require_auth():
        return _ORG

    monkeypatch.setattr(jobs, "_require_auth", __require_auth)
    monkeypatch.setattr(jobs, "get_db", lambda: db)
    monkeypatch.setattr(jobs, "check_rate_limit", lambda *a, **k: (True, 0))
    monkeypatch.setattr(jobs, "record_rate_limit_event", lambda *a, **k: None)

    class _FakeLLM:
        model = "fake-model"

        def analyze(self, text, language="en"):
            return {"summary": f"Summary in {language}", "risks": {"burnout_index": 20, "attrition_risk_pct": 10}}

        def translate(self, text, language_code):
            return "Traducción: " + text

    class _FakeTTS:
        content_type = "audio/mpeg"

        def synthesize(self, text, language_code, voice_name=None, voice_tier=None):
            return b"FAKEAUDIO"

    storage = storage or LocalStorage(".")
    monkeypatch.setattr(jobs, "get_llm_provider", lambda: _FakeLLM())
    monkeypatch.setattr(jobs, "get_tts_provider_for", lambda language_code=None: _FakeTTS())
    monkeypatch.setattr(jobs, "get_storage_provider", lambda: storage)
    monkeypatch.setattr(jobs.threading, "Thread", _SyncThread)

    app = _make_app()

    def _seed():
        db.sessions.insert_one(
            {
                "_id": ObjectId(_SESSION),
                "org_id": ObjectId(_ORG),
                "employee_id": ObjectId(_EMP),
                "status": "completed",
                "transcript": {"raw": "Hello, how are you feeling today?", "edited": "", "word_count": 6},
                "analysis": {"summary": "Old summary"},
                "analysis_language": "en",
                "analysis_version": 1,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
        )
        db.employees.insert_one(
            {"_id": ObjectId(_EMP), "org_id": ObjectId(_ORG), "name": "Ananya", "email_hash": "eh"}
        )
        db.users.insert_one({"_id": ObjectId(_USER), "org_id": ObjectId(_ORG)})

    _seed()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = _USER
    return client, db, storage


# ── Translation jobs ──────────────────────────────────────────────────────


class TestTranslationJobs:
    def test_create_runs_worker_and_updates_session(self, tmp_path, monkeypatch):
        db = _FakeDB()
        storage = LocalStorage(str(tmp_path))
        client, db, _ = _make_client(db.fresh(), monkeypatch, storage)

        r = client.post("/api/translate-jobs", json={"session_id": _SESSION, "language": "japanese"})
        assert r.status_code == 201
        body = r.get_json()
        assert body["status"] == "queued"
        job_id = body["id"]

        job = db.translation_jobs.find_one({"_id": ObjectId(job_id)})
        assert job["status"] == "done"
        s = db.sessions.find_one({"_id": ObjectId(_SESSION)})
        assert s["analysis_language"] == "japanese"
        # Result lives under analyses.<language>, not a flat top-level field.
        assert "Summary in japanese" in s["analyses"]["japanese"]["summary"]
        assert s["analyses"]["japanese"]["generated_at"] is not None
        # The legacy flat field is dropped so there is only one source of truth.
        assert "analysis" not in s
        assert s["status"] == "completed"

        n = db.notifications.find_one({"type": "translation_ready"})
        assert n is not None
        assert n["source_session_id"] == ObjectId(_SESSION)
        assert n["employee_id"] == ObjectId(_EMP)
        assert n["detail_key"] == "translate:japanese"
        assert n["headline"] == "Translation ready"

    def test_notification_deduplicated(self, tmp_path, monkeypatch):
        db = _FakeDB()
        storage = LocalStorage(str(tmp_path))
        client, db, _ = _make_client(db.fresh(), monkeypatch, storage)

        for _ in range(2):
            client.post("/api/translate-jobs", json={"session_id": _SESSION, "language": "japanese"})

        matching = list(db.notifications.find({"type": "translation_ready"}))
        assert len(matching) == 1

    def test_rejects_unsupported_language(self, tmp_path, monkeypatch):
        db = _FakeDB()
        client, _, _ = _make_client(db.fresh(), monkeypatch, LocalStorage(str(tmp_path)))
        r = client.post("/api/translate-jobs", json={"session_id": _SESSION, "language": "klingon"})
        assert r.status_code == 400
        assert r.get_json() == {"error": "unsupported_language"}

    def test_rejects_unknown_session(self, tmp_path, monkeypatch):
        db = _FakeDB()
        client, _, _ = _make_client(db.fresh(), monkeypatch, LocalStorage(str(tmp_path)))
        r = client.post("/api/translate-jobs", json={"session_id": "5" * 24, "language": "japanese"})
        assert r.status_code == 404

    def test_get_job_and_list_filtering(self, tmp_path, monkeypatch):
        db = _FakeDB()
        client, db, _ = _make_client(db.fresh(), monkeypatch, LocalStorage(str(tmp_path)))
        body = client.post("/api/translate-jobs", json={"session_id": _SESSION, "language": "japanese"}).get_json()
        job_id = body["id"]

        got = client.get("/api/translate-jobs/" + job_id)
        assert got.status_code == 200
        assert got.get_json()["input_ref"] == {"language": "japanese"}

        listed = client.get(f"/api/translate-jobs?session_id={_SESSION}&status=done")
        assert listed.status_code == 200
        assert len(listed.get_json()["jobs"]) == 1

        # Foreign org must not see the job.
        with mock.patch.object(jobs_mod, "_require_auth", return_value="aa" * 12):
            hidden = client.get("/api/translate-jobs/" + job_id)
            assert hidden.status_code == 404


# ── TTS jobs ─────────────────────────────────────────────────────────────


class TestTTSJobs:
    def test_create_runs_worker_and_stores_audio(self, tmp_path, monkeypatch):
        db = _FakeDB()
        storage = LocalStorage(str(tmp_path))
        client, db, _ = _make_client(db.fresh(), monkeypatch, storage)

        r = client.post(
            "/api/tts-jobs",
            json={
                "text": "Hello there",
                "language_code": "ja-JP",
                "translate": True,
                "session_id": _SESSION,
                "block": "wsMatters",
            },
        )
        assert r.status_code == 201
        job_id = r.get_json()["id"]

        job = db.tts_jobs.find_one({"_id": ObjectId(job_id)})
        assert job["status"] == "done"
        result = job["result"]
        assert result["content_type"] == "audio/mpeg"
        assert result["audio_key"].startswith("audio/sessions/")

        audio_resp = client.get(f"/api/tts-jobs/{job_id}/audio")
        assert audio_resp.status_code == 200
        assert audio_resp.data == b"FAKEAUDIO"
        assert audio_resp.mimetype == "audio/mpeg"

        n = db.notifications.find_one({"type": "audio_ready"})
        assert n is not None
        assert n["detail_key"] == "tts:wsMatters:ja-JP"
        assert n["source_session_id"] == ObjectId(_SESSION)

    def test_tts_notification_uses_human_block_label(self, tmp_path, monkeypatch):
        """The audio-ready summary must show a readable section name, never the
        raw `data-narr-target` selector sent from the frontend."""
        db = _FakeDB()
        client, db, _ = _make_client(db.fresh(), monkeypatch, LocalStorage(str(tmp_path)))

        client.post(
            "/api/tts-jobs",
            json={
                "text": "Hello there",
                "language_code": "ja-JP",
                "translate": True,
                "session_id": _SESSION,
                "block": ".ws-hero",
            },
        )
        n = db.notifications.find_one({"type": "audio_ready"})
        assert n is not None
        summary = n["summary"]
        assert "Live Conversation Score" in summary
        assert ".ws-hero" not in summary
        assert ".ws" not in summary

        # Unknown or missing blocks must fall back to a neutral phrase and can
        # never leak selector syntax either.
        db.notifications.clear()
        client.post(
            "/api/tts-jobs",
            json={
                "text": "Hello there",
                "language_code": "ja-JP",
                "translate": True,
                "session_id": _SESSION,
            },
        )
        n = db.notifications.find_one({"type": "audio_ready"})
        assert n is not None
        assert n["summary"] == "Audio for this section is ready to play."
        db = _FakeDB()
        client, _, _ = _make_client(db.fresh(), monkeypatch, LocalStorage(str(tmp_path)))
        assert client.post("/api/tts-jobs", json={"language_code": "en-US"}).status_code == 400
        assert client.post("/api/tts-jobs", json={"text": "hi"}).status_code == 400

    def test_tts_job_rejects_unsupported_language(self, tmp_path, monkeypatch):
        """A language no configured TTS provider can voice returns the real
        unsupported_tts_language error instead of synthesizing with a default
        English voice."""
        db = _FakeDB()
        client, _, _ = _make_client(db.fresh(), monkeypatch, LocalStorage(str(tmp_path)))
        monkeypatch.setattr(
            jobs_mod,
            "get_tts_provider_for",
            lambda language_code=None: (_ for _ in ()).throw(
                UnsupportedTTSLanguageError(language_code)
            ),
        )
        r = client.post("/api/tts-jobs", json={"text": "hello", "language_code": "xx-XX"})
        assert r.status_code == 400
        assert r.get_json() == {"error": "unsupported_tts_language", "language": "xx-XX"}

    def test_audio_not_served_before_done(self, tmp_path, monkeypatch):
        db = _FakeDB()
        client, db, _ = _make_client(db.fresh(), monkeypatch, LocalStorage(str(tmp_path)))
        body = client.post(
            "/api/tts-jobs", json={"text": "hi", "language_code": "en-US"}
        ).get_json()
        stored = next(d for d in db.tts_jobs.docs if str(d["_id"]) == body["id"])
        stored["status"] = "processing"
        r = client.get(f"/api/tts-jobs/{body['id']}/audio")
        assert r.status_code == 409

    def test_audio_404_for_other_org(self, tmp_path, monkeypatch):
        db = _FakeDB()
        client, _, _ = _make_client(db.fresh(), monkeypatch, LocalStorage(str(tmp_path)))
        body = client.post(
            "/api/tts-jobs", json={"text": "hi", "language_code": "en-US"}
        ).get_json()
        with mock.patch.object(jobs_mod, "_require_auth", return_value="aa" * 12):
            r = client.get(f"/api/tts-jobs/{body['id']}/audio")
        assert r.status_code == 404
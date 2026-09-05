"""Tests for the LLM request timeout.

The shared ``_call_and_parse`` helper must (a) pass an explicit ``timeout``
to every chat.completions.create call and (b) turn the SDK's APITimeoutError
into the app's own catchable ``LLMTimeoutError`` instead of letting a hung
upstream request outlive the platform worker timeout (whose generic raw-HTML
500 bypasses this app's JSON error handler).
"""

import importlib
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from openai import APITimeoutError

os.environ.setdefault("SECRET_KEY", "ci-test-secret")


def _fake_response(content):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


class _ChatResource:
    def __init__(self, completions):
        self.completions = completions


class _CapturingClient:
    """Mimics the openai client surface: .chat.completions.create(**kwargs)."""

    def __init__(self, content):
        self.content = content
        self.captured = {}
        self.chat = _ChatResource(self._completions())

    def _completions(self):
        class _Completions:
            def create(_self, **kwargs):
                self.captured.update(kwargs)
                return _fake_response(self.content)
        return _Completions()


class _RaisingClient:
    def __init__(self, exc):
        self._exc = exc
        self.captured = {}
        self.chat = _ChatResource(self._completions())

    def _completions(self):
        class _Completions:
            def create(_self, **kwargs):
                self.captured.update(kwargs)
                raise self._exc
        return _Completions()


@pytest.fixture
def llm_mod():
    return importlib.import_module("providers.llm")


def _call(llm_mod, client):
    return llm_mod._call_and_parse(
        client,
        model="model-x",
        system_prompt="sys",
        user_content="user",
        validator=lambda parsed: parsed,
        fallback={"summary": "fallback"},
        supports_json_mode=True,
        log_label="test",
    )


class TestCallAndParseTimeout:
    def test_timeout_kwarg_defaults_to_twenty_seconds(self, llm_mod):
        client = _CapturingClient('{"ok": true}')
        result = _call(llm_mod, client)
        assert result == {"ok": True}
        assert client.captured["timeout"] == 20.0

    def test_timeout_is_env_configurable(self, llm_mod, monkeypatch):
        monkeypatch.setenv("LLM_REQUEST_TIMEOUT_SECONDS", "7")
        assert llm_mod._llm_timeout_seconds() == 7.0

    def test_invalid_timeout_env_falls_back_to_default(self, llm_mod, monkeypatch):
        monkeypatch.setenv("LLM_REQUEST_TIMEOUT_SECONDS", "not-a-number")
        assert llm_mod._llm_timeout_seconds() == 20.0

    def test_timeout_applies_to_non_json_mode_too(self, llm_mod):
        client = _CapturingClient("```json\n{\"ok\": true}\n```")
        result = llm_mod._call_and_parse(
            client,
            model="model-x",
            system_prompt="sys",
            user_content="user",
            validator=lambda parsed: parsed,
            fallback={"summary": "fallback"},
            supports_json_mode=False,
            log_label="test",
        )
        assert result == {"ok": True}
        assert client.captured["timeout"] == 20.0

    def test_json_parse_failure_still_returns_fallback(self, llm_mod):
        client = _CapturingClient("not json at all")
        result = _call(llm_mod, client)
        assert result == {"summary": "fallback"}
        assert client.captured["timeout"] == 20.0

    def test_api_timeout_raises_llm_timeout_error(self, llm_mod):
        client = _RaisingClient(APITimeoutError("timed out"))
        with pytest.raises(llm_mod.LLMTimeoutError):
            _call(llm_mod, client)
        assert client.captured["timeout"] == 20.0

    def test_llm_timeout_error_is_importable_from_sessions(self):
        # sessions.py must be able to catch it distinctly from the generic
        # Exception handler.
        import sessions
        assert sessions.LLMTimeoutError is importlib.import_module("providers.llm").LLMTimeoutError


class TestTranslateTimeout:
    def _read_source(self, llm_mod):
        return open(os.path.join(os.path.dirname(__file__), "providers", "llm.py"), encoding="utf-8").read()

    def test_shared_helper_sets_timeout_kwarg(self, llm_mod):
        src = self._read_source(llm_mod)
        helper = src[src.find("def _call_and_parse"):src.find("class BaseLLM")]
        assert 'kwargs["timeout"] = timeout if timeout is not None else _llm_timeout_seconds()' in helper

    def test_both_translate_calls_bind_request_duration(self, llm_mod):
        # translate() bypasses the shared helper (single user message), so it
        # must carry the same per-request timeout explicitly — once for each
        # provider class.
        src = self._read_source(llm_mod)
        assert src.count("timeout=_llm_timeout_seconds(),") == 2

    def test_drift_uses_tighter_default_cap(self, llm_mod):
        # explain_drift runs inside the SAME /analyze request as the main
        # analysis, so its budget must not stack the full 20s on top of the
        # main call's 20s (analyze + drift would then exceed the platform's
        # ~30s worker timeout and the platform would serve raw HTML).
        assert llm_mod._llm_drift_timeout_seconds() == 8.0

    def test_drift_cap_is_env_overridable(self, llm_mod, monkeypatch):
        monkeypatch.setenv("LLM_DRIFT_TIMEOUT_SECONDS", "12")
        assert llm_mod._llm_drift_timeout_seconds() == 12.0

    def test_invalid_drift_env_falls_back_to_default(self, llm_mod, monkeypatch):
        monkeypatch.setenv("LLM_DRIFT_TIMEOUT_SECONDS", "not-a-number")
        assert llm_mod._llm_drift_timeout_seconds() == 8.0

    def test_both_explain_drift_calls_bind_the_tighter_cap(self, llm_mod):
        src = self._read_source(llm_mod)
        assert src.count("timeout=_llm_drift_timeout_seconds(),") == 2

    def test_call_and_parse_accepts_timeout_override(self, llm_mod):
        client = _CapturingClient('{"ok": true}')
        result = llm_mod._call_and_parse(
            client,
            model="model-x",
            system_prompt="sys",
            user_content="user",
            validator=lambda parsed: parsed,
            fallback={"summary": "fallback"},
            supports_json_mode=True,
            log_label="test",
            timeout=5,
        )
        assert result == {"ok": True}
        assert client.captured["timeout"] == 5



from bson import ObjectId
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# /sessions/<id>/analyze timeout behavior (regression: a slow/hung upstream
# must NOT be stored as a fallback analysis, which would zero out the employee
# wellness score as if it were a real reading).
# ---------------------------------------------------------------------------

def _dget(doc, path):
    node = doc
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _match(doc, filt):
    for k, v in filt.items():
        dv = _dget(doc, k)
        if isinstance(v, dict):
            for op, arg in v.items():
                if op == "$ne" and dv == arg:
                    return False
                if op == "$in" and (dv not in arg):
                    return False
                if op == "$nin" and (dv in arg):
                    return False
                if op not in ("$ne", "$in", "$nin") and dv != arg:
                    return False
        elif dv != v:
            return False
    return True


class _FakeCursor:
    def __init__(self, docs, filt):
        self._docs = [d for d in docs if _match(d, filt)]

    def sort(self, *a, **k):
        return self

    def limit(self, n):
        return self

    def skip(self, n):
        return self

    def __iter__(self):
        return iter(self._docs)


class _FakeCollection:
    def __init__(self, docs=None):
        self._docs = list(docs or [])

    def find_one(self, filt=None, *a, **kw):
        matches = [d for d in self._docs if _match(d, filt or {})]
        return dict(matches[0]) if matches else None

    def find(self, filt=None, *a, **kw):
        return _FakeCursor(self._docs, filt or {})

    def insert_one(self, doc):
        d = dict(doc)
        self._docs.append(d)
        return SimpleNamespace(inserted_id=d.get("_id"))

    def update_one(self, filt, update):
        for d in self._docs:
            if _match(d, filt):
                for op, fields in update.items():
                    if op == "$set":
                        d.update(fields)
                return SimpleNamespace(matched_count=1)
        return SimpleNamespace(matched_count=0)

    def count_documents(self, filt=None):
        return sum(1 for d in self._docs if _match(d, filt or {}))


@pytest.fixture
def _fake_sessions_db():
    return SimpleNamespace(
        sessions=_FakeCollection(),
        employees=_FakeCollection(),
        organizations=_FakeCollection(),
        notifications=_FakeCollection(),
    )


def _seed_session(fake_db, transcript="Manager: How are you doing?"):
    now = datetime.now(timezone.utc)
    doc = {
        "_id": ObjectId("5" * 24),
        "org_id": ObjectId("a" * 24),
        "employee_id": ObjectId("1" * 24),
        "source": "voice_dictation",
        "status": "transcribed",
        "language": "en",
        "recording_device": "browser",
        "recording_duration": 0,
        "recording_type": "webm",
        "audio": None,
        "transcript": {"raw": transcript, "edited": transcript, "word_count": 4},
        "analysis": None,
        "analysis_version": 0,
        "last_transcript_update": now,
        "last_analyzed_at": None,
        "created_at": now,
        "updated_at": now,
    }
    fake_db.sessions.insert_one(doc)
    return doc


@pytest.fixture
def _sessions_client(monkeypatch, _fake_sessions_db):
    import sessions as sessions_mod
    from flask import Flask

    monkeypatch.setattr(sessions_mod, "get_db", lambda: _fake_sessions_db)
    monkeypatch.setattr(sessions_mod, "check_rate_limit", lambda *a, **k: (True, 0))
    monkeypatch.setattr(sessions_mod, "record_rate_limit_event", lambda *a, **k: None)
    monkeypatch.setattr(sessions_mod, "_require_auth", lambda: "a" * 24)

    app = Flask(__name__)
    app.config.update(SECRET_KEY="x")
    app.register_blueprint(sessions_mod.sessions_bp, url_prefix="/api")
    return app.test_client()


class TestAnalyzeEndpointTimeout:
    def test_timeout_returns_retryable_message_and_stores_nothing(
        self, monkeypatch, _sessions_client, _fake_sessions_db
    ):
        import sessions as sessions_mod
        from providers.llm import LLMTimeoutError

        class _TimeoutLLM:
            model = "fake-model"

            def analyze(self, transcript):
                raise LLMTimeoutError()

        monkeypatch.setattr(sessions_mod, "get_llm_provider", lambda: _TimeoutLLM())
        _seed_session(_fake_sessions_db)

        r = _sessions_client.post("/api/sessions/" + "5" * 24 + "/analyze")
        assert r.status_code == 500
        assert r.get_json()["error"] == "Analysis is taking longer than expected. Please try again."

        stored = _fake_sessions_db.sessions.find_one({"_id": ObjectId("5" * 24)})
        assert stored["status"] == "failed"
        assert stored["analysis"] is None



class TestStaleProcessingRecovery:
    """Sessions orphaned in "processing" (the /analyze request died before
    writing a terminal status) must be demoted to "failed" on read so the UI
    can offer a retry instead of a permanently disabled "Analyzing" button."""

    @staticmethod
    def _set_status(fake_db, status, updated_at):
        fake_db.sessions.update_one(
            {"_id": ObjectId("5" * 24)},
            {"$set": {"status": status, "updated_at": updated_at}},
        )

    def test_stale_processing_session_demoted_to_failed(self, _sessions_client, _fake_sessions_db):
        _seed_session(_fake_sessions_db)
        self._set_status(
            _fake_sessions_db,
            "processing",
            datetime.now(timezone.utc) - timedelta(minutes=10),
        )

        r = _sessions_client.get("/api/sessions/" + "5" * 24)
        assert r.status_code == 200
        assert r.get_json()["status"] == "failed"

        stored = _fake_sessions_db.sessions.find_one({"_id": ObjectId("5" * 24)})
        assert stored["status"] == "failed"

    def test_fresh_processing_session_left_untouched(self, _sessions_client, _fake_sessions_db):
        _seed_session(_fake_sessions_db)
        self._set_status(_fake_sessions_db, "processing", datetime.now(timezone.utc))

        r = _sessions_client.get("/api/sessions/" + "5" * 24)
        assert r.status_code == 200
        assert r.get_json()["status"] == "processing"

    def test_list_demotes_stale_processing(self, _sessions_client, _fake_sessions_db):
        _seed_session(_fake_sessions_db)
        self._set_status(
            _fake_sessions_db,
            "processing",
            datetime.now(timezone.utc) - timedelta(minutes=10),
        )

        r = _sessions_client.get("/api/sessions")
        assert r.status_code == 200
        body = r.get_json()
        assert body["sessions"][0]["status"] == "failed"

    def test_recently_touched_processing_stays(self, _sessions_client, _fake_sessions_db):
        _seed_session(_fake_sessions_db)
        self._set_status(
            _fake_sessions_db,
            "processing",
            datetime.now(timezone.utc) - timedelta(seconds=30),
        )

        r = _sessions_client.get("/api/sessions/" + "5" * 24)
        assert r.status_code == 200
        assert r.get_json()["status"] == "processing"

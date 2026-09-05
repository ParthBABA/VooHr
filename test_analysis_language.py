"""Tests for the per-language analysis feature.

Covers the LLM prompt-side language directive and the
``POST /sessions/<id>/analyze`` language plumbing (body -> llm.analyze ->
persisted ``analysis_language`` -> session JSON).
"""

import importlib
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from bson import ObjectId
from providers.llm import LLMTimeoutError

os.environ.setdefault("SECRET_KEY", "ci-test-secret")

ORG_A = "aaaaaaaaaaaaaaaaaaaaaaaa"
EMP_1 = "111111111111111111111111"


# ---------------------------------------------------------------------------
# In-memory Mongo facade (deliberately small but faithful enough for the
# analyze_session happy path, including the drift-detection query).
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
        if k == "$or" and isinstance(v, list):
            if not any(_match(doc, sub) for sub in v):
                return False
            continue
        dv = _dget(doc, k)
        if isinstance(v, dict):
            for op, arg in v.items():
                if op == "$ne":
                    if dv == arg:
                        return False
                elif op == "$in":
                    if dv not in arg:
                        return False
                elif op == "$nin":
                    if dv in arg:
                        return False
                else:
                    if dv != arg:
                        return False
        elif dv != v:
            return False
    return True


class FakeCursor:
    def __init__(self, docs, filt):
        self._docs = [d for d in docs if _match(d, filt)]

    def sort(self, *args, **kwargs):
        return self

    def limit(self, n):
        return self

    def skip(self, n):
        return self

    def __iter__(self):
        return iter(self._docs)


class FakeCollection:
    def __init__(self, docs=None):
        self._docs = list(docs or [])

    def find_one(self, filt=None, *args, **kw):
        matches = [d for d in self._docs if _match(d, filt or {})]
        sort = kw.get("sort")
        if sort:
            sign = {1: 1, -1: -1}
            for k, dirn in reversed(sort):
                matches.sort(key=lambda d: _dget(d, k), reverse=(sign.get(dirn, 1) == -1))
        return dict(matches[0]) if matches else None

    def find(self, filt=None, *args, **kw):
        return FakeCursor(self._docs, filt or {})

    def insert_one(self, doc):
        d = dict(doc)
        self._docs.append(d)
        return SimpleNamespace(inserted_id=d.get("_id"))

    def update_one(self, filt, update):
        for d in self._docs:
            if _match(d, filt):
                for op, fields in update.items():
                    if op == "$set":
                        for k, v in fields.items():
                            d[k] = v
                return SimpleNamespace(matched_count=1)
        return SimpleNamespace(matched_count=0)

    def count_documents(self, filt=None):
        return sum(1 for d in self._docs if _match(d, filt or {}))

    def aggregate(self, pipeline):
        return []

    def delete_one(self, filt=None):
        for i, d in enumerate(list(self._docs)):
            if _match(d, filt or {}):
                del self._docs[i]
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)


@pytest.fixture
def fake_db():
    return SimpleNamespace(
        sessions=FakeCollection(),
        employees=FakeCollection(),
        organizations=FakeCollection(),
        notifications=FakeCollection(),
    )


@pytest.fixture
def llm_spy():
    calls = []

    class _FakeLLM:
        model = "fake-model"

        def analyze(self, transcript, language="en"):
            calls.append((transcript, language))
            return {
                "summary": "A measured, largely comfortable conversation.",
                "title": "Feeling stretched but open",
                "risks": {"burnout_index": 40, "attrition_risk_pct": 20},
                "psychological_safety": {"safety_score": 70, "statement": "Spoke openly."},
                "conversation_coach": [],
            }

    return SimpleNamespace(calls=calls, instance=_FakeLLM())


def _seed_session(fake_db, transcript="Manager: How are you doing?"):
    now = datetime.now(timezone.utc)
    doc = {
        "_id": ObjectId("5" * 24),
        "org_id": ObjectId(ORG_A),
        "employee_id": ObjectId(EMP_1),
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
def client(monkeypatch, fake_db, llm_spy):
    s = importlib.import_module("sessions")
    monkeypatch.setattr(s, "get_db", lambda: fake_db)
    monkeypatch.setattr(s, "check_rate_limit", lambda *a, **k: (True, 0))
    monkeypatch.setattr(s, "record_rate_limit_event", lambda *a, **k: None)
    monkeypatch.setattr(s, "_require_auth", lambda: ORG_A)
    monkeypatch.setattr(s, "get_llm_provider", lambda: llm_spy.instance)

    from flask import Flask

    app = Flask(__name__)
    app.config.update(SECRET_KEY="x")
    app.register_blueprint(s.sessions_bp, url_prefix="/api")
    return app.test_client()


# ---------------------------------------------------------------------------
# LLM prompt directive
# ---------------------------------------------------------------------------

class TestLanguageInstruction:
    def _read_llm(self):
        return importlib.import_module("providers.llm")

    def test_english_and_empty_produce_no_directive(self):
        llm = self._read_llm()
        assert llm._language_instruction("en") == ""
        assert llm._language_instruction("en ") == ""
        assert llm._language_instruction("EN") == ""
        assert llm._language_instruction(None) == ""
        assert llm._language_instruction("") == ""

    def test_unknown_language_falls_back_to_default(self):
        llm = self._read_llm()
        assert llm._language_instruction("french") == ""

    def test_hinglish_requests_latin_script_output(self):
        llm = self._read_llm()
        directive = llm._language_instruction("hinglish")
        assert "Hinglish" in directive
        assert "Latin script" in directive
        # Schema/key-preservation rule must still be present.
        assert "JSON keys" in directive

    def test_hinglish_instruction_contains_concrete_examples(self):
        """The Hinglish instruction must show example sentences, not just name
        the language — a bare label reliably produces plain English with the
        model defaulting to 'simple vocabulary' instead of real code-switching."""
        llm = self._read_llm()
        instruction = llm._language_instruction("hinglish")
        # Must contain actual Devanagari-free Hindi function words in example
        # sentences, proving concrete examples are present, not just the label.
        assert "hai" in instruction or "Hai" in instruction
        assert "example" in instruction.lower() or "wrong" in instruction.lower()
        # Must explicitly warn against the failure mode we just saw in production.
        assert "simple" in instruction.lower() or "simplified" in instruction.lower()

    def test_analyze_signature_accepts_language(self):
        llm = self._read_llm()
        import inspect

        for cls in (llm.BaseLLM, llm.OpenAILLM, llm.DeepSeekLLM):
            sig = inspect.signature(cls.analyze)
            assert "language" in sig.parameters
            assert sig.parameters["language"].default == "en"

    def test_signature_stays_transcript_prefix_compatible(self):
        # Existing privacy tests assert `def analyze(self, transcript: str`
        # as a prefix across providers — the language param must not disturb that.
        src = open(
            os.path.join(os.path.dirname(__file__), "providers", "llm.py"), encoding="utf-8"
        ).read()
        assert src.count("def analyze(self, transcript: str") >= 3


# ---------------------------------------------------------------------------
# /sessions/<id>/analyze endpoint
# ---------------------------------------------------------------------------

class TestAnalyzeLanguageEndpoint:
    def test_default_language_is_english(self, client, fake_db, llm_spy):
        _seed_session(fake_db)
        r = client.post("/api/sessions/" + "5" * 24 + "/analyze", json={})
        assert r.status_code == 200
        body = r.get_json()
        assert body["analysis_language"] == "en"
        assert llm_spy.calls[0][1] == "en"

    def test_hinglish_body_passed_to_llm_and_persisted(self, client, fake_db, llm_spy):
        _seed_session(fake_db)
        r = client.post(
            "/api/sessions/" + "5" * 24 + "/analyze",
            json={"language": "hinglish"},
        )
        assert r.status_code == 200
        body = r.get_json()
        assert body["analysis_language"] == "hinglish"
        assert llm_spy.calls[0][1] == "hinglish"
        stored = fake_db.sessions.find_one({"_id": ObjectId(body["id"])})
        assert stored["analysis_language"] == "hinglish"

    def test_language_is_case_and_whitespace_insensitive(self, client, fake_db, llm_spy):
        _seed_session(fake_db)
        r = client.post(
            "/api/sessions/" + "5" * 24 + "/analyze",
            json={"language": "  Hinglish  "},
        )
        assert r.status_code == 200
        assert r.get_json()["analysis_language"] == "hinglish"

    def test_non_string_language_ignored(self, client, fake_db, llm_spy):
        _seed_session(fake_db)
        r = client.post("/api/sessions/" + "5" * 24 + "/analyze", json={"language": 42})
        assert r.status_code == 200
        body = r.get_json()
        assert body["analysis_language"] == "en"
        assert llm_spy.calls[0][1] == "en"

    def test_analyze_returns_language_in_session_json(self, client, fake_db, llm_spy):
        doc = _seed_session(fake_db)
        client.post("/api/sessions/" + "5" * 24 + "/analyze", json={"language": "hinglish"})
        r = client.get("/api/sessions/" + str(doc["_id"]))
        assert r.status_code == 200
        assert r.get_json()["analysis_language"] == "hinglish"

    def test_timeout_returns_retryable_message_and_stores_nothing(
        self, client, fake_db, monkeypatch
    ):
        # A slow/hung upstream request must surface a retry-friendly message,
        # NOT a stored fallback analysis (which would zero out the employee
        # wellness score as if it were a real reading).
        import sessions as sessions_mod

        class _TimeoutLLM:
            model = "fake-model"

            def analyze(self, transcript, language="en"):
                raise LLMTimeoutError()

        monkeypatch.setattr(sessions_mod, "get_llm_provider", lambda: _TimeoutLLM())
        _seed_session(fake_db)

        r = client.post("/api/sessions/" + "5" * 24 + "/analyze", json={"language": "hinglish"})
        assert r.status_code == 500
        assert r.get_json()["error"] == "Analysis is taking longer than expected. Please try again."

        stored = fake_db.sessions.find_one({"_id": ObjectId("5" * 24)})
        assert stored["status"] == "failed"
        assert stored["analysis"] is None
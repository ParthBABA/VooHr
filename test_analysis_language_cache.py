"""Per-language session-analysis caching.

A session keeps one analysis per output language under ``analyses``, keyed by
language, with ``analysis_language`` acting only as a pointer to the entry
currently being viewed. Covered here:

- Switching language A -> B -> A calls the LLM exactly once for A and once for
  B; the second A is served from the stored result with no LLM call at all.
- Generating B never destroys A's stored analysis.
- Documents written before this shape (a single flat ``analysis`` plus
  ``analysis_language``) still read back correctly through the shim.
- The serialized session reports the viewed analysis and advertises which
  languages are already available, so the workspace can switch instantly.
"""

import os
from datetime import datetime, timezone

# jobs -> sessions -> employees -> config requires SECRET_KEY at import time.
os.environ.setdefault("SECRET_KEY", "test-secret-key")

import bson
import pytest
from bson import ObjectId
from types import SimpleNamespace

import jobs as jobs_mod
from sessions import (
    _session_to_json,
    analysis_key,
    resolve_analysis_language,
    session_analysis,
    session_analyses,
    session_risks,
)

_ORG = "64b0000000000000000000a1"
_EMP = "64b0000000000000000000a2"
_SESSION = "64b0000000000000000000a3"


# ── Minimal in-memory MongoDB that models dotted $set / $unset ───────────


def _set_dotted(doc, path, value):
    parts = path.split(".")
    node = doc
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def _match(doc, q):
    """Equality / ``$ne`` / ``$in`` matching, enough for these tests."""
    for k, v in (q or {}).items():
        if isinstance(v, dict) and "$ne" in v:
            if doc.get(k) == v["$ne"]:
                return False
        elif doc.get(k) != v:
            return False
    return True


class _Collection:
    def __init__(self):
        self.docs = []

    def find_one(self, q=None, *a, **kw):
        for d in self.docs:
            if _match(d, q or {}):
                return d
        return None

    def insert_one(self, doc):
        doc = dict(doc)
        doc.setdefault("_id", bson.ObjectId())
        self.docs.append(doc)
        return SimpleNamespace(inserted_id=doc["_id"])

    def update_one(self, q, update, upsert=False):
        for d in self.docs:
            if _match(d, q):
                for path, value in (update.get("$set") or {}).items():
                    _set_dotted(d, path, value)
                for path in (update.get("$unset") or {}):
                    parts = path.split(".")
                    node = d
                    for part in parts[:-1]:
                        node = node.get(part)
                        if not isinstance(node, dict):
                            node = None
                            break
                    if isinstance(node, dict):
                        node.pop(parts[-1], None)
                return SimpleNamespace(matched_count=1)
        return SimpleNamespace(matched_count=0)


class _CountingLLM:
    """Records every analyze() call so caching can be asserted precisely."""

    model = "fake-model"

    def __init__(self):
        self.calls = []

    def analyze(self, text, language="en"):
        self.calls.append(language)
        return {
            "summary": f"Summary in {language}",
            "risks": {"burnout_index": 20, "attrition_risk_pct": 10},
        }


class _DB:
    """Namespaced collections, subscriptable the way PyMongo's Database is."""

    def __init__(self, *names):
        for name in names:
            setattr(self, name, _Collection())

    def __getitem__(self, name):
        coll = getattr(self, name, None)
        if coll is None:
            coll = _Collection()
            setattr(self, name, coll)
        return coll


@pytest.fixture
def db():
    fake = _DB(
        "sessions",
        "employees",
        "translation_jobs",
        "tts_jobs",
        "notifications",
    )
    now = datetime.now(timezone.utc)
    fake.sessions.insert_one(
        {
            "_id": ObjectId(_SESSION),
            "org_id": ObjectId(_ORG),
            "employee_id": ObjectId(_EMP),
            "status": "completed",
            "transcript": {"raw": "Manager: How are you doing?", "edited": "", "word_count": 5},
            "analyses": {},
            "analysis_version": 0,
            "created_at": now,
            "updated_at": now,
        }
    )
    fake.employees.insert_one(
        {"_id": ObjectId(_EMP), "org_id": ObjectId(_ORG), "name": "Ananya", "email_hash": "eh"}
    )
    return fake


def _run(db, llm, language):
    """Drive one translation job synchronously and return the finished job."""
    job = db.translation_jobs.insert_one(
        {
            "_id": bson.ObjectId(),
            "org_id": ObjectId(_ORG),
            "session_id": ObjectId(_SESSION),
            "employee_id": ObjectId(_EMP),
            "language": language,
            "status": "queued",
            "created_at": datetime.now(timezone.utc),
        }
    )
    jobs_mod._run_translation_job(db, job.inserted_id, llm, ObjectId(_ORG), language)
    return db.translation_jobs.find_one({"_id": job.inserted_id})


# ── The core bug: A -> B -> A must not re-run the LLM ────────────────────


class TestLanguageSwitchCaching:
    def test_a_b_a_calls_llm_once_per_language(self, db):
        llm = _CountingLLM()

        _run(db, llm, "english")
        assert llm.calls == ["english"]

        _run(db, llm, "japanese")
        assert llm.calls == ["english", "japanese"]

        # Switching back to the first language must be free.
        _run(db, llm, "english")
        assert llm.calls == ["english", "japanese"], "second 'english' re-ran the LLM"

    def test_second_lookup_is_marked_cached(self, db):
        llm = _CountingLLM()
        _run(db, llm, "japanese")
        job = _run(db, llm, "japanese")
        assert job["status"] == "done"
        assert job["result"]["cached"] is True

    def test_first_generation_is_not_marked_cached(self, db):
        llm = _CountingLLM()
        job = _run(db, llm, "japanese")
        assert job["result"]["cached"] is False

    def test_generating_b_does_not_destroy_a(self, db):
        llm = _CountingLLM()
        _run(db, llm, "english")
        _run(db, llm, "japanese")

        s = db.sessions.find_one({"_id": ObjectId(_SESSION)})
        assert s["analyses"]["english"]["summary"] == "Summary in english"
        assert s["analyses"]["japanese"]["summary"] == "Summary in japanese"
        # The pointer moved to the language just generated, but both survive.
        assert s["analysis_language"] == "japanese"

    def test_switching_back_restores_a_and_leaves_b_intact(self, db):
        llm = _CountingLLM()
        _run(db, llm, "english")
        _run(db, llm, "japanese")
        _run(db, llm, "english")

        s = db.sessions.find_one({"_id": ObjectId(_SESSION)})
        assert s["analysis_language"] == "english"
        assert s["analyses"]["japanese"]["summary"] == "Summary in japanese"
        assert s["analyses"]["english"]["summary"] == "Summary in english"

    def test_analysis_version_counts_generations_not_switches(self, db):
        llm = _CountingLLM()
        _run(db, llm, "english")
        _run(db, llm, "japanese")
        _run(db, llm, "english")

        s = db.sessions.find_one({"_id": ObjectId(_SESSION)})
        # Two real generations; the cached third selection must not bump it.
        assert s["analysis_version"] == 2

    def test_wellness_rollup_still_runs_on_cached_lookup(self, db):
        llm = _CountingLLM()
        _run(db, llm, "japanese")
        emp_before = db.employees.find_one({"_id": ObjectId(_EMP)})
        assert emp_before["ai_wellness"]["score"] == 85

        _run(db, llm, "japanese")
        emp_after = db.employees.find_one({"_id": ObjectId(_EMP)})
        assert emp_after["ai_wellness"]["score"] == 85

    def test_no_transcript_still_fails_when_nothing_cached(self, db):
        llm = _CountingLLM()
        s = db.sessions.find_one({"_id": ObjectId(_SESSION)})
        _set_dotted(s, "transcript", {"raw": "", "edited": "", "word_count": 0})

        job = _run(db, llm, "japanese")
        assert job["status"] == "failed"
        assert job["error"] == "no_transcript_to_analyze"
        assert llm.calls == []


# ── Backward compatibility with the old flat shape ───────────────────────


class TestLegacyDocumentCompat:
    def test_legacy_flat_analysis_is_readable(self):
        s = {"analysis": {"summary": "Legacy"}, "analysis_language": "en"}
        assert session_analyses(s) == {"en": {"summary": "Legacy"}}
        assert session_analysis(s)["summary"] == "Legacy"
        assert resolve_analysis_language(s) == "en"

    def test_legacy_flat_analysis_uses_its_own_language_pointer(self):
        s = {"analysis": {"summary": "Legacy"}, "analysis_language": "japanese"}
        assert list(session_analyses(s)) == ["japanese"]
        assert session_analysis(s)["summary"] == "Legacy"

    def test_legacy_document_defaults_to_english_without_pointer(self):
        s = {"analysis": {"summary": "Legacy"}}
        assert resolve_analysis_language(s) == "en"

    def test_map_wins_over_stale_flat_field(self):
        # A partially migrated doc: the map is authoritative.
        s = {
            "analysis": {"summary": "Stale"},
            "analysis_language": "en",
            "analyses": {"japanese": {"summary": "Fresh"}},
        }
        assert session_analyses(s) == {"japanese": {"summary": "Fresh"}}
        assert resolve_analysis_language(s) == "japanese"
        assert session_analysis(s)["summary"] == "Fresh"

    def test_empty_map_falls_back_to_legacy_field(self):
        s = {"analysis": {"summary": "Legacy"}, "analyses": {}}
        assert session_analyses(s) == {"en": {"summary": "Legacy"}}

    def test_legacy_translation_job_hits_cache(self, db):
        # Seed a pre-migration document, then select its existing language.
        s = db.sessions.find_one({"_id": ObjectId(_SESSION)})
        s["analysis"] = {"summary": "Pre-existing"}
        s["analysis_language"] = "japanese"
        s.pop("analyses", None)

        llm = _CountingLLM()
        job = _run(db, llm, "japanese")
        assert job["status"] == "done"
        assert job["result"]["cached"] is True
        assert llm.calls == []


# ── Pointer resolution edge cases ───────────────────────────────────────


class TestAnalysisLanguageResolution:
    def test_pointer_to_missing_key_falls_back_to_english(self):
        s = {"analyses": {"en": {"summary": "E"}, "thai": {"summary": "T"}}, "analysis_language": "klingon"}
        assert resolve_analysis_language(s) == "en"

    def test_pointer_to_missing_key_falls_back_to_first_available(self):
        s = {"analyses": {"thai": {"summary": "T"}}, "analysis_language": "klingon"}
        assert resolve_analysis_language(s) == "thai"

    def test_no_analyses_resolves_to_none(self):
        assert resolve_analysis_language({"analyses": {}}) is None
        assert session_analysis({"analyses": {}}) is None
        assert session_analyses({}) == {}

    def test_non_dict_entries_are_ignored(self):
        s = {"analyses": {"en": {"summary": "E"}, "bad": None, "worse": "nope"}}
        assert session_analyses(s) == {"en": {"summary": "E"}}

    def test_risks_helper_tolerates_list_and_missing(self):
        assert session_risks({"analyses": {"en": {"risks": ["a"]}}}) == {}
        assert session_risks({"analyses": {"en": {}}}) == {}
        assert session_risks({}) == {}
        assert session_risks({"analyses": {"en": {"risks": {"burnout_index": 3}}}}) == {"burnout_index": 3}


class TestAnalysisKey:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("japanese", "japanese"),
            ("  Japanese  ", "japanese"),
            ("en", "en"),
            (None, "en"),
            ("", "en"),
            (123, "en"),
            # Values MongoDB would reject as field names must never reach a write.
            ("bad.key", "en"),
            ("$inject", "en"),
        ],
    )
    def test_normalizes_to_a_safe_key(self, raw, expected):
        assert analysis_key(raw) == expected


# ── Serialization ───────────────────────────────────────────────────────


class TestSessionSerialization:
    def test_reports_viewed_analysis_and_available_languages(self, db):
        llm = _CountingLLM()
        _run(db, llm, "english")
        _run(db, llm, "japanese")

        out = _session_to_json(db.sessions.find_one({"_id": ObjectId(_SESSION)}))
        assert out["analysis_language"] == "japanese"
        assert out["analysis"]["summary"] == "Summary in japanese"
        assert out["available_analysis_languages"] == ["english", "japanese"]

    def test_legacy_document_still_serializes(self):
        out = _session_to_json(
            {"_id": ObjectId(), "analysis": {"summary": "Legacy"}, "analysis_language": "en"}
        )
        assert out["analysis"]["summary"] == "Legacy"
        assert out["analysis_language"] == "en"
        assert out["available_analysis_languages"] == ["en"]

    def test_session_without_analysis_serializes_as_none(self):
        out = _session_to_json({"_id": ObjectId(), "analyses": {}})
        assert out["analysis"] is None
        assert out["analysis_language"] == "en"
        assert out["available_analysis_languages"] == []

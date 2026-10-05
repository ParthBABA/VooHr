"""Trust-boundary tests for the analysis pipeline.

Covers the failures that would make VooHr show something untrue:

  * a provider FALLBACK (unparseable LLM reply, burnout 0 / attrition 0) must
    never be stored as a completed session nor turned into a wellness score;
  * fallback / failed sessions must not pollute the drift-detection window;
  * numeric risk fields must be coerced so string values can never raise (or
    default a missing reading to "0 risk") in the wellness roll-up;
  * the output validator's quality checks must be enforceable and reachable.
"""

import importlib
import os
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

os.environ.setdefault("SECRET_KEY", "ci-test-secret")

from _mongo_facade import (  # noqa: F401 - imports _fake_sessions_db fixture
    EMPLOYEE_ID,
    ORG_ID,
    SESSION_ID,
    _seed_session,
    _fake_sessions_db,
)
from flask import Flask


# â”€â”€ helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@pytest.fixture
def llm_mod():
    return importlib.import_module("providers.llm")


@pytest.fixture
def sessions_mod():
    return importlib.import_module("sessions")


def _make_client(sessions_mod, monkeypatch, fake_db):
    monkeypatch.setattr(sessions_mod, "get_db", lambda: fake_db)
    monkeypatch.setattr(sessions_mod, "check_rate_limit", lambda *a, **k: (True, 0))
    monkeypatch.setattr(sessions_mod, "record_rate_limit_event", lambda *a, **k: None)
    monkeypatch.setattr(sessions_mod, "_require_auth", lambda: ORG_ID)

    app = Flask(__name__)
    app.config.update(SECRET_KEY="x")
    app.register_blueprint(sessions_mod.sessions_bp, url_prefix="/api")
    return app.test_client()


@pytest.fixture
def client(sessions_mod, monkeypatch, _fake_sessions_db):
    return _make_client(sessions_mod, monkeypatch, _fake_sessions_db)


class _StubLLM:
    """Minimal provider stub returning a canned analysis."""

    model = "stub-model"

    def __init__(self, analysis):
        self._analysis = analysis
        self.calls = 0

    def analyze(self, transcript, language="en"):
        self.calls += 1
        return self._analysis

    def explain_drift(self, sessions):
        self.drift_payload = sessions
        return {"is_genuine_pattern": False, "headline": "", "summary": ""}


def _install_llm(sessions_mod, monkeypatch, analysis):
    stub = _StubLLM(analysis)
    monkeypatch.setattr(sessions_mod, "get_llm_provider", lambda: stub)
    return stub


def _valid_analysis(**risk_overrides):
    risks = {"burnout_index": 40, "attrition_risk_pct": 20, "risk_factors": ["load"]}
    risks.update(risk_overrides)
    return {"summary": "A real analysis.", "risks": risks}


def _seed_employee(fake_db, **overrides):
    doc = {"_id": ObjectId(EMPLOYEE_ID), "org_id": ObjectId(ORG_ID), "name": "Test"}
    doc.update(overrides)
    fake_db.employees.insert_one(doc)
    return doc


def _post_analyze(client):
    return client.post(f"/api/sessions/{SESSION_ID}/analyze")


# â”€â”€ 0.1 fallback is never a real reading â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class TestFallbackAnalysisIsNotAReading:
    def test_fallback_payload_is_marked(self, llm_mod):
        assert llm_mod.FALLBACK_ANALYSIS["is_fallback"] is True

    def test_fallback_has_zero_placeholder_risks(self, llm_mod):
        """The dangerous part: these zeros become a wellness score of 100."""
        risks = llm_mod.FALLBACK_ANALYSIS["risks"]
        assert risks["burnout_index"] == 0
        assert risks["attrition_risk_pct"] == 0

    def test_parse_failure_marks_session_failed_and_stores_no_analysis(
        self, sessions_mod, monkeypatch, client, _fake_sessions_db
    ):
        llm_mod = importlib.import_module("providers.llm")
        _seed_session(_fake_sessions_db)
        _seed_employee(_fake_sessions_db, ai_wellness={"score": 42})
        _install_llm(sessions_mod, monkeypatch, llm_mod.FALLBACK_ANALYSIS)

        r = _post_analyze(client)
        assert r.status_code == 500
        assert r.get_json()["error"] == "Analysis could not be completed. Please try again."

        stored = _fake_sessions_db.sessions.find_one({"_id": ObjectId(SESSION_ID)})
        assert stored["status"] == "failed"
        assert not stored["analyses"]
        assert stored["analysis_version"] == 0

    def test_parse_failure_leaves_wellness_untouched(
        self, sessions_mod, monkeypatch, client, _fake_sessions_db
    ):
        _seed_session(_fake_sessions_db)
        _seed_employee(_fake_sessions_db, ai_wellness={"score": 42})
        _install_llm(
            sessions_mod, monkeypatch,
            importlib.import_module("providers.llm").FALLBACK_ANALYSIS,
        )

        _post_analyze(client)

        emp = _fake_sessions_db.employees.find_one({"_id": ObjectId(EMPLOYEE_ID)})
        assert emp["ai_wellness"] == {"score": 42}

    def test_fallback_module_dict_is_not_mutated_by_a_session(
        self, sessions_mod, monkeypatch, client, _fake_sessions_db
    ):
        """The provider hands back a shared module-level dict; nothing written
        by one session may leak into the next."""
        llm_mod = importlib.import_module("providers.llm")
        before = dict(llm_mod.FALLBACK_ANALYSIS)
        _seed_session(_fake_sessions_db)
        _install_llm(sessions_mod, monkeypatch, llm_mod.FALLBACK_ANALYSIS)

        _post_analyze(client)

        assert llm_mod.FALLBACK_ANALYSIS == before

    def test_timeout_path_unchanged(
        self, sessions_mod, monkeypatch, client, _fake_sessions_db
    ):
        from providers.llm import LLMTimeoutError

        class _TimeoutLLM:
            model = "stub"

            def analyze(self, transcript, language="en"):
                raise LLMTimeoutError()

        _seed_session(_fake_sessions_db)
        _seed_employee(_fake_sessions_db, ai_wellness={"score": 42})
        monkeypatch.setattr(sessions_mod, "get_llm_provider", lambda: _TimeoutLLM())

        r = _post_analyze(client)
        assert r.status_code == 500
        assert r.get_json()["error"] == "Analysis is taking longer than expected. Please try again."
        stored = _fake_sessions_db.sessions.find_one({"_id": ObjectId(SESSION_ID)})
        assert stored["status"] == "failed"
        emp = _fake_sessions_db.employees.find_one({"_id": ObjectId(EMPLOYEE_ID)})
        assert emp["ai_wellness"] == {"score": 42}


# â”€â”€ 0.2 drift window excludes fallbacks â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _seed_completed(fake_db, sid, created_at, risks=None, fallback=False, analysis=None):
    doc = {
        "_id": ObjectId(sid),
        "org_id": ObjectId(ORG_ID),
        "employee_id": ObjectId(EMPLOYEE_ID),
        "status": "completed",
        "created_at": created_at,
        "updated_at": created_at,
        "transcript": {"raw": "text", "edited": "text"},
    }
    if fallback:
        doc["analyses"] = {"en": {"is_fallback": True, "risks": {"burnout_index": 0,
                                                                "attrition_risk_pct": 0}}}
    elif analysis is not None:
        doc["analyses"] = {"en": analysis}
    elif risks is not None:
        doc["analyses"] = {"en": {"risks": risks}}
    fake_db.sessions.insert_one(doc)
    return doc


class TestDriftWindowExcludesFallbacks:
    """The just-analyzed session always joins its own window, so each case
    seeds it alongside deliberately-aged real and fallback sessions."""

    def _seed_window(self, fake_db, n_real, n_fallback):
        base = datetime.now(timezone.utc)
        day = 0
        sid = 1
        for _ in range(n_real):
            day += 1
            _seed_completed(fake_db, str(sid) * 24, base - timedelta(days=20 - day),
                            risks={"burnout_index": 30 + sid, "attrition_risk_pct": 20 + sid})
            sid += 1
        for _ in range(n_fallback):
            day += 1
            _seed_completed(fake_db, str(sid) * 24, base - timedelta(days=20 - day),
                            fallback=True)
            sid += 1

    def _capture_drift(self, sessions_mod, monkeypatch, client, fake_db):
        seen = []

        class _LLM(_StubLLM):
            def explain_drift(self, sessions):
                seen.append(sessions)
                return {"is_genuine_pattern": False, "headline": "", "summary": ""}

        monkeypatch.setattr(sessions_mod, "get_llm_provider", lambda: _LLM(_valid_analysis()))
        _seed_employee(fake_db)
        _seed_session(fake_db)
        _post_analyze(client)
        return seen

    def test_fallbacks_do_not_fill_an_otherwise_empty_window(
        self, sessions_mod, monkeypatch, client, _fake_sessions_db
    ):
        # Only the new session is real: with fallbacks counted the window would
        # reach DRIFT_WINDOW_SIZE and the drift LLM would fire on placeholder
        # zeros, reporting a "healthy, stable" trend that was never analysed.
        self._seed_window(_fake_sessions_db, n_real=0, n_fallback=5)
        seen = self._capture_drift(sessions_mod, monkeypatch, client, _fake_sessions_db)

        assert seen == [], "drift must not run on a window padded by fallbacks"

    def test_fallbacks_are_dropped_from_the_window_payload(
        self, sessions_mod, monkeypatch, client, _fake_sessions_db
    ):
        self._seed_window(_fake_sessions_db, n_real=2, n_fallback=5)
        seen = self._capture_drift(sessions_mod, monkeypatch, client, _fake_sessions_db)

        assert len(seen) == 1
        assert len(seen[0]) == 3  # 2 real + the session just analyzed
        assert all(not s.get("is_fallback") for s in seen[0])
        # No placeholder (0, 0) pair anywhere in the payload.
        assert not any(
            s["burnout_index"] == 0 and s["attrition_risk_pct"] == 0 for s in seen[0]
        )

    def test_real_sessions_still_fill_window_without_fallbacks(
        self, sessions_mod, monkeypatch, client, _fake_sessions_db
    ):
        self._seed_window(_fake_sessions_db, n_real=4, n_fallback=0)
        seen = self._capture_drift(sessions_mod, monkeypatch, client, _fake_sessions_db)

        assert len(seen) == 1
        assert len(seen[0]) == 3

    def test_legacy_document_without_marker_still_qualifies(self, sessions_mod):
        legacy = {"analyses": {"en": {"risks": {"burnout_index": 10, "attrition_risk_pct": 10}}}}
        assert sessions_mod.session_has_fallback_analysis(legacy) is False

    def test_session_risks_hides_fallback_values(self, sessions_mod):
        s = {"analyses": {"en": {"is_fallback": True,
                                  "risks": {"burnout_index": 0, "attrition_risk_pct": 0}}}}
        assert sessions_mod.session_risks(s) == {}

    def test_session_risks_unaffected_by_real_analysis(self, sessions_mod):
        s = {"analyses": {"en": {"risks": {"burnout_index": 33, "attrition_risk_pct": 44}}}}
        assert sessions_mod.session_risks(s) == {"burnout_index": 33, "attrition_risk_pct": 44}

    def test_fallback_in_any_language_disqualifies(self, sessions_mod):
        s = {"analyses": {"en": {"risks": {}}, "hi": {"is_fallback": True}}}
        assert sessions_mod.session_has_fallback_analysis(s) is True


# â”€â”€ 0.3 numeric coercion â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class TestRiskScoreCoercion:
    @pytest.mark.parametrize("raw,expected", [
        ("70", 70.0), ("70%", 70.0), (" 70 ", 70.0), (70, 70.0),
        (70.5, 70.5), ("70.5", 70.5), (150, 100.0), (-5, 0.0),
        ("150%", 100.0), ("-5", 0.0), (0, 0.0), (100, 100.0),
    ])
    def test_coerces_to_clamped_number(self, llm_mod, raw, expected):
        assert llm_mod._coerce_risk_score(raw) == expected

    @pytest.mark.parametrize("raw", [
        None, "", "   ", "abc", "high", [], {}, ["70"], True, False,
        float("nan"), float("inf"),
    ])
    def test_non_numeric_becomes_none_not_zero(self, llm_mod, raw):
        assert llm_mod._coerce_risk_score(raw) is None

    def test_validate_analysis_coerces_string_risks(self, llm_mod):
        out = llm_mod.validate_analysis({"risks": {"burnout_index": "70",
                                                   "attrition_risk_pct": "60%"}})
        assert out["risks"]["burnout_index"] == 70.0
        assert out["risks"]["attrition_risk_pct"] == 60.0

    def test_validate_analysis_nulls_unusable_risks(self, llm_mod):
        out = llm_mod.validate_analysis({"risks": {"burnout_index": "high",
                                                   "attrition_risk_pct": None}})
        assert out["risks"]["burnout_index"] is None
        assert out["risks"]["attrition_risk_pct"] is None

    def test_validate_analysis_clamps_out_of_range(self, llm_mod):
        out = llm_mod.validate_analysis({"risks": {"burnout_index": 150,
                                                   "attrition_risk_pct": -5}})
        assert out["risks"]["burnout_index"] == 100.0
        assert out["risks"]["attrition_risk_pct"] == 0.0

    def test_validate_analysis_coerces_safety_score(self, llm_mod):
        out = llm_mod.validate_analysis({
            "psychological_safety": {"safety_score": "82%"},
        })
        assert out["psychological_safety"]["safety_score"] == 82.0

    def test_validate_analysis_handles_list_risks(self, llm_mod):
        out = llm_mod.validate_analysis({"risks": [{"burnout_index": 10}]})
        assert isinstance(out["risks"], dict)
        assert out["risks"]["burnout_index"] == 10.0

    def test_validate_analysis_keeps_floats(self, llm_mod):
        out = llm_mod.validate_analysis({"risks": {"burnout_index": 33.7,
                                                   "attrition_risk_pct": 12.2}})
        assert out["risks"]["burnout_index"] == 33.7
        assert out["risks"]["attrition_risk_pct"] == 12.2


class TestWellnessRollup:
    def _analyze_with_risks(self, sessions_mod, monkeypatch, client, fake_db, risks):
        _seed_session(fake_db)
        _seed_employee(fake_db, ai_wellness={"score": 42})
        _install_llm(sessions_mod, monkeypatch,
                     {"summary": "s", "risks": risks} if risks is not None
                     else {"summary": "s"})
        return _post_analyze(client)

    def test_both_numeric_strings_produce_a_real_score(
        self, sessions_mod, monkeypatch, client, _fake_sessions_db
    ):
        # Would previously be "70"+"60" -> "7060" -> TypeError.
        r = self._analyze_with_risks(sessions_mod, monkeypatch, client, _fake_sessions_db,
                                     {"burnout_index": "70", "attrition_risk_pct": "60%"})
        assert r.status_code == 200
        emp = _fake_sessions_db.employees.find_one({"_id": ObjectId(EMPLOYEE_ID)})
        assert emp["ai_wellness"]["score"] == 35
        assert emp["ai_wellness"]["burnout_index"] == 70.0
        assert emp["ai_wellness"]["attrition_risk_pct"] == 60.0

    def test_missing_both_values_leaves_wellness_untouched(
        self, sessions_mod, monkeypatch, client, _fake_sessions_db
    ):
        r = self._analyze_with_risks(sessions_mod, monkeypatch, client, _fake_sessions_db,
                                     {"burnout_index": None, "attrition_risk_pct": None})
        assert r.status_code == 200
        emp = _fake_sessions_db.employees.find_one({"_id": ObjectId(EMPLOYEE_ID)})
        assert emp["ai_wellness"] == {"score": 42}

    def test_one_missing_value_does_not_default_to_zero(
        self, sessions_mod, monkeypatch, client, _fake_sessions_db
    ):
        # Defaulting attrition to 0 would report 65 instead of skipping.
        r = self._analyze_with_risks(sessions_mod, monkeypatch, client, _fake_sessions_db,
                                     {"burnout_index": 80, "attrition_risk_pct": None})
        assert r.status_code == 200
        emp = _fake_sessions_db.employees.find_one({"_id": ObjectId(EMPLOYEE_ID)})
        assert emp["ai_wellness"] == {"score": 42}

    def test_out_of_range_values_are_clamped_not_crashing(
        self, sessions_mod, monkeypatch, client, _fake_sessions_db
    ):
        r = self._analyze_with_risks(sessions_mod, monkeypatch, client, _fake_sessions_db,
                                     {"burnout_index": 150, "attrition_risk_pct": -5})
        assert r.status_code == 200
        emp = _fake_sessions_db.employees.find_one({"_id": ObjectId(EMPLOYEE_ID)})
        assert emp["ai_wellness"]["score"] == 50  # 100 - (100 + 0)/2

    def test_list_risks_do_not_crash_wellness(
        self, sessions_mod, monkeypatch, client, _fake_sessions_db
    ):
        r = self._analyze_with_risks(sessions_mod, monkeypatch, client, _fake_sessions_db,
                                     [{"burnout_index": 50}])
        assert r.status_code == 200
        emp = _fake_sessions_db.employees.find_one({"_id": ObjectId(EMPLOYEE_ID)})
        assert emp["ai_wellness"] == {"score": 42}

    def test_uncoercible_string_does_not_crash(
        self, sessions_mod, monkeypatch, client, _fake_sessions_db
    ):
        r = self._analyze_with_risks(sessions_mod, monkeypatch, client, _fake_sessions_db,
                                     {"burnout_index": "severe", "attrition_risk_pct": 30})
        assert r.status_code == 200
        emp = _fake_sessions_db.employees.find_one({"_id": ObjectId(EMPLOYEE_ID)})
        assert emp["ai_wellness"] == {"score": 42}

    def test_analysis_still_completed_when_wellness_write_raises(
        self, sessions_mod, monkeypatch, client, _fake_sessions_db
    ):
        """The analysis is saved first, so a broken wellness write must not
        leave a 'failed' session that still holds an analysis."""
        _seed_session(_fake_sessions_db)
        _seed_employee(_fake_sessions_db)

        class _ExplodingEmployees:
            def update_one(self, *a, **k):
                raise RuntimeError("db down")

        _fake_sessions_db.employees = _ExplodingEmployees()
        _install_llm(sessions_mod, monkeypatch, _valid_analysis())

        r = _post_analyze(client)
        assert r.status_code == 200
        stored = _fake_sessions_db.sessions.find_one({"_id": ObjectId(SESSION_ID)})
        assert stored["status"] == "completed"
        assert stored["analyses"]["en"]["summary"] == "A real analysis."


# â”€â”€ 0.4 output-quality enforcement â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class TestQualityWarnings:
    def test_clean_output_has_no_warnings(self, llm_mod):
        out = llm_mod.validate_analysis({
            "summary": "Harshit described a heavy week and asked for clearer priorities.",
            "psychological_safety": {"interpretation": "Evidence suggests he held back on scope."},
        })
        assert out["quality_warnings"] == []

    def test_ordinary_sentence_no_longer_trips_a_keyword(self, llm_mod):
        # "you are" used to fire on normal prose.
        out = llm_mod.validate_analysis({
            "summary": "When you are unclear about priorities, the team guesses.",
            "psychology": {"behavioural_interpretation": [
                {"interpretation": "He said you are overloaded, but hedged the claim."}
            ]},
        })
        assert out["quality_warnings"] == []

    @pytest.mark.parametrize("text", [
        "She appears depressed after the reorg.",
        "This looks like an anxiety disorder pattern.",
        "He shows narcissistic tendencies in review meetings.",
        "The employee suffers from burnout syndrome.",
    ])
    def test_clinical_labels_are_flagged(self, llm_mod, text):
        out = llm_mod.validate_analysis({"summary": text})
        assert out["quality_warnings"]
        assert any("clinical" in w for w in out["quality_warnings"])

    @pytest.mark.parametrize("text", [
        "She deliberately withheld the deadline.",
        "He intentionally hid the scope change.",
        "The manager was on purpose testing him.",
        "She is pretending the workload is fine.",
    ])
    def test_motive_attribution_is_flagged(self, llm_mod, text):
        out = llm_mod.validate_analysis({"summary": text})
        assert out["quality_warnings"]
        assert any("motive" in w for w in out["quality_warnings"])

    def test_warnings_are_nested_inside_list_fields_too(self, llm_mod):
        out = llm_mod.validate_analysis({
            "topics_to_avoid": [{"topic": "Health", "reason": "He mentioned being depressed."}],
        })
        assert out["quality_warnings"]

    def test_warnings_are_also_in_validation_errors(self, llm_mod):
        out = llm_mod.validate_analysis({"summary": "He is hiding something."})
        assert any("motive" in e for e in out["_validation_errors"])

    def test_quality_warnings_always_present(self, llm_mod):
        out = llm_mod.validate_analysis({"summary": "Fine."})
        assert "quality_warnings" in out

    def test_quality_warnings_is_a_list_not_a_string(self, llm_mod):
        out = llm_mod.validate_analysis({"summary": "Fine."})
        assert isinstance(out["quality_warnings"], list)

    def test_fallback_payload_has_no_quality_warnings_field(self, llm_mod):
        # The fallback is replaced wholesale by sessions.py, so it must not
        # claim a clean bill of health.
        assert "quality_warnings" not in llm_mod.FALLBACK_ANALYSIS


# â”€â”€ 0.5 prompt honesty â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class TestPromptHonesty:
    def test_v2_prompt_no_longer_claims_it_does_not_score(self, llm_mod):
        prompt = llm_mod._build_v2_prompt()
        assert "it does not score, rank, or rate the employee" not in prompt

    def test_v1_prompt_no_longer_claims_it_does_not_score(self, llm_mod):
        prompt = llm_mod._build_v1_prompt()
        assert "it does not score, rank, or rate the employee" not in prompt

    @pytest.mark.parametrize("builder", ["_build_v2_prompt", "_build_v1_prompt"])
    def test_prompt_still_requires_the_numeric_fields(self, llm_mod, builder):
        prompt = getattr(llm_mod, builder)()
        assert "burnout_index" in prompt
        assert "attrition_risk_pct" in prompt
        assert "safety_score" in prompt

    @pytest.mark.parametrize("builder", ["_build_v2_prompt", "_build_v1_prompt"])
    def test_prompt_explains_what_the_numbers_are(self, llm_mod, builder):
        prompt = getattr(llm_mod, builder)()
        assert "probabilistic indicators" in prompt


# â”€â”€ 0.6 hardening â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class TestSessionStatusHardening:
    def test_put_cannot_mark_completed_without_analysis(
        self, sessions_mod, monkeypatch, client, _fake_sessions_db
    ):
        _seed_session(_fake_sessions_db)
        r = client.put(f"/api/sessions/{SESSION_ID}", json={"status": "completed"})
        assert r.status_code == 400
        assert r.get_json()["error"] == "analysis_required_to_complete_session"
        stored = _fake_sessions_db.sessions.find_one({"_id": ObjectId(SESSION_ID)})
        assert stored["status"] == "transcribed"

    def test_put_allows_completed_when_analysis_exists(
        self, sessions_mod, monkeypatch, client, _fake_sessions_db
    ):
        _seed_session(_fake_sessions_db, analyses={"en": {"risks": {}}})
        r = client.put(f"/api/sessions/{SESSION_ID}", json={"status": "completed"})
        assert r.status_code == 200

    def test_put_still_allows_failed_and_draft(self, client, _fake_sessions_db):
        _seed_session(_fake_sessions_db)
        r = client.put(f"/api/sessions/{SESSION_ID}", json={"status": "failed"})
        assert r.status_code == 200
        r = client.put(f"/api/sessions/{SESSION_ID}", json={"status": "draft"})
        assert r.status_code == 200

    def test_second_concurrent_analyze_is_rejected(
        self, sessions_mod, monkeypatch, client, _fake_sessions_db
    ):
        _seed_session(_fake_sessions_db)
        _seed_employee(_fake_sessions_db)
        _install_llm(sessions_mod, monkeypatch, _valid_analysis())

        assert _post_analyze(client).status_code == 200
        # Reset to "processing" to simulate an in-flight first request.
        _fake_sessions_db.sessions.update_one(
            {"_id": ObjectId(SESSION_ID)}, {"$set": {"status": "processing"}}
        )
        r = _post_analyze(client)
        assert r.status_code == 409
        assert "already running" in r.get_json()["error"]

    def test_guard_does_not_block_a_fresh_session(
        self, sessions_mod, monkeypatch, client, _fake_sessions_db
    ):
        _seed_session(_fake_sessions_db)
        _seed_employee(_fake_sessions_db)
        _install_llm(sessions_mod, monkeypatch, _valid_analysis())
        assert _post_analyze(client).status_code == 200


class TestRiskNumberHelper:
    @pytest.mark.parametrize("raw,expected", [
        (70, 70.0), ("70", 70.0), ("70%", 70.0), (150, 100.0), (-5, 0.0),
        (None, None), ("abc", None), ([], None), (True, None),
    ])
    def test_as_risk_number(self, sessions_mod, raw, expected):
        assert sessions_mod._as_risk_number(raw) == expected

    def test_non_dict_analysis_is_not_a_fallback(self, sessions_mod):
        assert sessions_mod.is_fallback_analysis(None) is False
        assert sessions_mod.is_fallback_analysis("x") is False

    def test_real_analysis_is_not_a_fallback(self, sessions_mod):
        assert sessions_mod.is_fallback_analysis({"summary": "hi"}) is False
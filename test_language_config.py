"""Tests for the centralised analysis/translation-language config.

Language enablement is controlled by the ENABLED_ANALYSIS_LANGUAGES env var
instead of being scattered across backend and frontend:

* providers.llm.SUPPORTED_ANALYSIS_LANGUAGES is the intersection of the
  implemented instruction catalogue and the env-enabled set.
* _language_instruction() returns "" for a disabled (yet still implemented)
  language, exactly like an unrecognized one.
* GET /api/config/languages exposes the enabled set to the workspace UI so
  the frontend never hardcodes the list.

Spanish and French stay implemented but OFF by default until there is
validated demand for them. Hindi/Hinglish were also removed from the default
set per the product decision to pivot to low-English-proficiency markets.
"""

import os
import sys
import unittest.mock as _mock

os.environ.setdefault("SECRET_KEY", "test-secret-key")

# Other test files may swap sys.modules entries for MagicMocks at collection
# time; evict ONLY mock entries so the real packages load here.
for _name in ("requests", "flask", "openai"):
    if isinstance(sys.modules.get(_name), _mock.MagicMock):
        del sys.modules[_name]

import pytest


# The 10 low-English-proficiency markets enabled by default (English is
# implicit and never appears in the list).
DEFAULT_ENABLED = [
    "japanese", "thai", "arabic", "mandarin", "indonesian",
    "vietnamese", "korean", "bengali", "turkish", "portuguese",
]
DEFAULT_ENABLED_SET = set(DEFAULT_ENABLED)


def _load_llm():
    """Import providers.llm fresh so its module-level env read re-runs."""
    sys.modules.pop("providers.llm", None)
    import providers.llm as llm
    return llm


@pytest.fixture(autouse=True)
def _restore_modules():
    # Env-driven module state is read at import time, so these tests reload
    # providers.llm (and api, for the endpoint) with a controlled env.  That
    # must not leak into other test modules: restoring the ORIGINAL module
    # objects preserves class identity (e.g. sessions.py's `LLMTimeoutError`
    # binding) that earlier imports in the suite depend on.
    saved_llm = sys.modules.get("providers.llm")
    saved_api = sys.modules.get("api")
    yield
    if saved_llm is not None:
        sys.modules["providers.llm"] = saved_llm
    else:
        sys.modules.pop("providers.llm", None)
    if saved_api is not None:
        sys.modules["api"] = saved_api
    else:
        sys.modules.pop("api", None)


class TestSupportedLanguageEnforcement:
    def test_default_environment_enables_market_languages(self, monkeypatch):
        monkeypatch.delenv("ENABLED_ANALYSIS_LANGUAGES", raising=False)
        llm = _load_llm()
        assert llm.SUPPORTED_ANALYSIS_LANGUAGES == DEFAULT_ENABLED_SET
        # Implemented, but disabled by default -> behaves like unknown.
        for disabled in ("hinglish", "hindi", "spanish", "french"):
            assert llm._language_instruction(disabled) == ""
        # Validated languages produce their real instruction text.
        assert "Japanese" in llm._language_instruction("japanese")
        assert "Modern Standard Arabic" in llm._language_instruction("arabic")
        assert "Mandarin" in llm._language_instruction("mandarin")
        assert "Portuguese" in llm._language_instruction("portuguese")
        # English is the implicit default and never produces a suffix.
        assert llm._language_instruction("en") == ""
        assert llm._language_instruction(None) == ""
        assert llm._language_instruction("") == ""

    def test_spanish_enabled_via_env_returns_real_instruction(self, monkeypatch):
        monkeypatch.setenv(
            "ENABLED_ANALYSIS_LANGUAGES",
            ",".join(["english"] + DEFAULT_ENABLED + ["spanish"]),
        )
        llm = _load_llm()
        assert llm.SUPPORTED_ANALYSIS_LANGUAGES == DEFAULT_ENABLED_SET | {"spanish"}
        assert "Spanish" in llm._language_instruction("spanish")
        assert "Japanese" in llm._language_instruction("japanese")
        # Hindi is implemented but not enabled.
        assert llm._language_instruction("hindi") == ""

    def test_all_languages_enabled(self, monkeypatch):
        monkeypatch.setenv(
            "ENABLED_ANALYSIS_LANGUAGES",
            "hinglish,hindi,spanish,french," + ",".join(DEFAULT_ENABLED),
        )
        llm = _load_llm()
        assert llm.SUPPORTED_ANALYSIS_LANGUAGES == (
            {"hinglish", "hindi", "spanish", "french"} | DEFAULT_ENABLED_SET
        )
        assert "French" in llm._language_instruction("french")
        assert "Bengali" in llm._language_instruction("bengali")

    def test_keys_are_normalised_and_unknown_ignored(self, monkeypatch):
        monkeypatch.setenv("ENABLED_ANALYSIS_LANGUAGES", " Japanese ,  THAI ,german,unknown,,")
        llm = _load_llm()
        # Case and whitespace are stripped; "german"/"unknown" are not
        # implemented so the intersection with the catalogue drops them.
        assert llm.SUPPORTED_ANALYSIS_LANGUAGES == {"japanese", "thai"}
        assert llm._language_instruction("GERMAN") == ""


class TestConfigLanguagesEndpoint:
    def _make_client(self):
        sys.modules.pop("api", None)
        import api
        from flask import Flask

        app = Flask(__name__)
        app.config.update(TESTING=True, SECRET_KEY="unit-test-secret")
        app.register_blueprint(api.api_bp, url_prefix="/api")
        return app.test_client()

    def test_returns_only_enabled_languages(self, monkeypatch):
        monkeypatch.delenv("ENABLED_ANALYSIS_LANGUAGES", raising=False)
        _load_llm()
        client = self._make_client()
        resp = client.get("/api/config/languages")
        assert resp.status_code == 200
        assert resp.get_json() == {"languages": DEFAULT_ENABLED}

    def test_returns_spanish_when_enabled(self, monkeypatch):
        monkeypatch.setenv(
            "ENABLED_ANALYSIS_LANGUAGES",
            ",".join(["english"] + DEFAULT_ENABLED + ["spanish"]),
        )
        _load_llm()
        client = self._make_client()
        resp = client.get("/api/config/languages")
        assert resp.status_code == 200
        assert resp.get_json() == {"languages": ["spanish"] + DEFAULT_ENABLED}
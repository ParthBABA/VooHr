"""Tests for the LLM request timeout.

The shared ``_call_and_parse`` helper must (a) pass an explicit ``timeout``
to every chat.completions.create call and (b) turn the SDK's APITimeoutError
into the app's own catchable ``LLMTimeoutError`` instead of letting a hung
upstream request outlive the platform worker timeout (whose generic raw-HTML
500 bypasses this app's JSON error handler).
"""

import importlib
import os
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
        assert 'kwargs["timeout"] = _llm_timeout_seconds()' in helper

    def test_both_translate_calls_bind_request_duration(self, llm_mod):
        # translate() bypasses the shared helper (single user message), so it
        # must carry the same per-request timeout explicitly — once for each
        # provider class.
        src = self._read_source(llm_mod)
        assert src.count("timeout=_llm_timeout_seconds(),") == 2
"""Tests for language-aware TTS provider selection (get_tts_provider_for).

Regressions covered:
- A language is routed to the configured provider only when that provider can
  actually voice it (e.g. ja-JP + Deepgram must go to Deepgram).
- A language the configured provider cannot voice falls back to a provider
  that CAN (e.g. th-TH + Deepgram -> Google), never to the provider's default
  English voice.
- When NEITHER Google nor Deepgram can voice the language, the selector
  raises UnsupportedTTSLanguageError and the /api/tts/synthesize route
  surfaces it as a clean 400 {"error": "unsupported_tts_language", "language": ...}.
- Gemini is intentionally NOT part of this routing — scoped to Google +
  Deepgram only.
"""

import os

os.environ.setdefault("SECRET_KEY", "test-secret-key")

import pytest
from flask import Flask

import tts as tts_mod
from providers import get_tts_provider_for
from providers.deepgram_tts import DeepgramTTS
from providers.google_tts import GoogleNeural2TTS
from providers.tts_languages import UnsupportedTTSLanguageError


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.secret_key = "test"
    return app


# ── Provider selection unit tests ────────────────────────────────────────


def test_japanese_routes_to_deepgram_when_configured_deepgram(app):
    app.config["TTS_PROVIDER"] = "deepgram"
    with app.app_context():
        assert isinstance(get_tts_provider_for("ja-JP"), DeepgramTTS)


def test_japanese_routes_to_google_when_configured_google(app):
    app.config["TTS_PROVIDER"] = "google"
    with app.app_context():
        assert isinstance(get_tts_provider_for("ja-JP"), GoogleNeural2TTS)


def test_japanese_defaults_to_google_when_no_provider_configured(app):
    # TTS_PROVIDER defaults to "google"; Japanese is supported by both
    # providers, so the default must select Google.
    with app.app_context():
        assert isinstance(get_tts_provider_for("ja-JP"), GoogleNeural2TTS)


def test_thai_never_routes_to_deepgram(app):
    app.config["TTS_PROVIDER"] = "deepgram"
    with app.app_context():
        # Thai is outside Deepgram's Aura-2 capability set, so the selector
        # must fall back to Google instead of synthesizing with the default
        # English voice.
        provider = get_tts_provider_for("th-TH")
        assert isinstance(provider, GoogleNeural2TTS)


def test_arabic_mandarin_and_portuguese_never_route_to_deepgram(app):
    app.config["TTS_PROVIDER"] = "deepgram"
    with app.app_context():
        for bcp47 in ("ar-SA", "zh-CN", "pt-BR", "id-ID", "ko-KR"):
            assert isinstance(get_tts_provider_for(bcp47), GoogleNeural2TTS)


def test_english_uses_configured_provider(app):
    app.config["TTS_PROVIDER"] = "deepgram"
    with app.app_context():
        assert isinstance(get_tts_provider_for("en-US"), DeepgramTTS)


def test_dutch_uses_configured_deepgram(app):
    app.config["TTS_PROVIDER"] = "deepgram"
    with app.app_context():
        assert isinstance(get_tts_provider_for("nl-NL"), DeepgramTTS)


def test_missing_language_uses_configured_provider(app):
    app.config["TTS_PROVIDER"] = "deepgram"
    with app.app_context():
        assert isinstance(get_tts_provider_for(None), DeepgramTTS)
        assert isinstance(get_tts_provider_for(""), DeepgramTTS)


def test_unsupported_language_raises_with_language_code(app):
    # "hi" is an implemented analysis language but is NOT in either Google's
    # curated market set nor Deepgram's Aura-2 set, so routing must raise
    # rather than hand it to a default English voice.
    app.config["TTS_PROVIDER"] = "deepgram"
    with app.app_context():
        with pytest.raises(UnsupportedTTSLanguageError) as exc:
            get_tts_provider_for("hi-IN")
        assert exc.value.language_code == "hi-IN"

    app.config["TTS_PROVIDER"] = "google"
    with app.app_context():
        with pytest.raises(UnsupportedTTSLanguageError) as exc:
            get_tts_provider_for("xx-XX")
        assert exc.value.language_code == "xx-XX"


# ── Endpoint behaviour (real routing, no provider patch) ─────────────────


def _make_routing_app(app, monkeypatch):
    monkeypatch.setattr(tts_mod, "_require_auth", lambda: "org-1")
    app.register_blueprint(tts_mod.tts_bp, url_prefix="/api")
    return app


def test_endpoint_returns_400_for_unsupported_language(app, monkeypatch):
    app.config["TTS_PROVIDER"] = "google"
    _make_routing_app(app, monkeypatch)
    with app.test_client() as c:
        r = c.post(
            "/api/tts/synthesize",
            json={"text": "hello", "language_code": "xx-XX"},
        )
    assert r.status_code == 400
    assert r.get_json() == {"error": "unsupported_tts_language", "language": "xx-XX"}


def test_endpoint_routes_supported_language_to_google_real_selector(app, monkeypatch):
    app.config["TTS_PROVIDER"] = "google"
    _make_routing_app(app, monkeypatch)
    # Keep real selector routing; only neutralize the actual HTTP synthesis.
    monkeypatch.setattr(
        GoogleNeural2TTS, "synthesize", lambda self, *a, **k: b"ok"
    )
    with app.test_client() as c:
        r = c.post(
            "/api/tts/synthesize",
            json={"text": "konnichiwa", "language_code": "ja-JP"},
        )
    assert r.status_code == 200
    assert r.data == b"ok"
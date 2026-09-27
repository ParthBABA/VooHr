"""Tests for language-aware TTS provider selection (get_tts_provider_for).

Regressions covered:
- English and Japanese always route to Deepgram (_DEEPGRAM_PREFERRED_LANGUAGES),
  overriding TTS_PROVIDER, because Aura-2 is the better model for them.
- A language the configured provider cannot voice falls back to a provider
  that CAN (e.g. th-TH + Deepgram -> Google), never to the provider's default
  English voice.
- A language the configured provider CAN voice still uses it (e.g. nl-NL +
  Deepgram -> Deepgram) — the preferred-language override is deliberately
  narrow and must not swallow the other Aura-2 languages.
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


def test_japanese_routes_to_deepgram_even_when_google_configured(app):
    # Japanese is voiced by both providers, so TTS_PROVIDER=google would
    # normally win. _DEEPGRAM_PREFERRED_LANGUAGES overrides that: en/ja always
    # go to Deepgram because it is the better model for them.
    app.config["TTS_PROVIDER"] = "google"
    with app.app_context():
        assert isinstance(get_tts_provider_for("ja-JP"), DeepgramTTS)


def test_japanese_defaults_to_deepgram_when_no_provider_configured(app):
    # TTS_PROVIDER defaults to "google" and Japanese is supported by both
    # providers, so without the preferred-language override the default would
    # select Google. It must still be Deepgram.
    with app.app_context():
        assert isinstance(get_tts_provider_for("ja-JP"), DeepgramTTS)
        assert not isinstance(get_tts_provider_for("ja-JP"), GoogleNeural2TTS)


def test_english_routes_to_deepgram_even_when_google_configured(app):
    # The mirror of the Japanese case: "en" is in _DEEPGRAM_PREFERRED_LANGUAGES,
    # so it must not be handed to Google just because TTS_PROVIDER says so.
    app.config["TTS_PROVIDER"] = "google"
    with app.app_context():
        assert isinstance(get_tts_provider_for("en-US"), DeepgramTTS)


def test_deepgram_preferred_languages_ignore_every_provider_config(app):
    for configured in ("google", "deepgram", "gemini", None):
        app.config["TTS_PROVIDER"] = configured
        with app.app_context():
            for bcp47 in ("en-US", "en", "ja-JP", "ja"):
                assert isinstance(get_tts_provider_for(bcp47), DeepgramTTS), (
                    "%s with TTS_PROVIDER=%r" % (bcp47, configured)
                )


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


def test_endpoint_routes_google_only_language_to_google_real_selector(app, monkeypatch):
    app.config["TTS_PROVIDER"] = "google"
    _make_routing_app(app, monkeypatch)
    # Keep real selector routing; only neutralize the actual HTTP synthesis.
    # Thai is outside Deepgram's Aura-2 set, so this still resolves to Google.
    # Both providers are stubbed so a future routing change cannot silently
    # turn this into a live, billed API call.
    monkeypatch.setattr(
        GoogleNeural2TTS, "synthesize", lambda self, *a, **k: b"ok"
    )
    monkeypatch.setattr(
        DeepgramTTS, "synthesize", lambda self, *a, **k: pytest.fail(
            "Thai must not be routed to Deepgram"
        )
    )
    with app.test_client() as c:
        r = c.post(
            "/api/tts/synthesize",
            json={"text": "sawasdee", "language_code": "th-TH"},
        )
    assert r.status_code == 200
    assert r.data == b"ok"


def test_endpoint_routes_english_to_deepgram_real_selector(app, monkeypatch):
    """End-to-end counterpart of the preferred-language override: en must reach
    Deepgram even with TTS_PROVIDER=google, with no live HTTP either way."""
    app.config["TTS_PROVIDER"] = "google"
    _make_routing_app(app, monkeypatch)
    monkeypatch.setattr(
        DeepgramTTS, "synthesize", lambda self, *a, **k: b"deepgram-ok"
    )
    monkeypatch.setattr(
        GoogleNeural2TTS, "synthesize", lambda self, *a, **k: pytest.fail(
            "English must not be routed to Google"
        )
    )
    with app.test_client() as c:
        r = c.post(
            "/api/tts/synthesize",
            json={"text": "hello", "language_code": "en-US"},
        )
    assert r.status_code == 200
    assert r.data == b"deepgram-ok"

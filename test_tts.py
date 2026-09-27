"""Tests for the TTS pipeline: parallel chunking, streaming route, caching,
content-type selection, and request validation.

Provider HTTP calls (Google/Deepgram) and SDK calls (Gemini) are all mocked —
no real APIs are contacted.
"""

import base64
import io
import os
import sys
import threading
import types
import wave

# tts -> employees -> config requires SECRET_KEY at import time. Set it here
# before importing the modules under test.
os.environ.setdefault("SECRET_KEY", "test-secret-key")

import pytest
from flask import Flask
from unittest.mock import patch

import tts as tts_mod
from providers.deepgram_tts import DeepgramTTS
from providers.google_tts import GoogleNeural2TTS, _resolve_google_locale
from providers.storage import LocalStorage
from providers.tts import BaseTTS
from providers.tts_cache import TTSCache

_ORG = "org-1"


def _install_fake_genai():
    """Provide a minimal `google.genai` shim so providers/gemini_tts imports
    without the (uninstalled) google-genai SDK."""
    if "google.genai" in sys.modules:
        return
    genai = types.ModuleType("google.genai")
    genai.Client = lambda *a, **k: None
    genai.types = types.SimpleNamespace(
        GenerateContentConfig=lambda **k: None,
        SpeechConfig=lambda **k: None,
        VoiceConfig=lambda **k: None,
        PrebuiltVoiceConfig=lambda **k: None,
    )
    sys.modules["google.genai"] = genai
    if "google" in sys.modules:
        sys.modules["google"].genai = genai
    else:
        google_mod = types.ModuleType("google")
        google_mod.genai = genai
        sys.modules["google"] = google_mod


def _cached(provider_cls, namespace, tmp_path):
    provider = provider_cls()
    provider._tts_cache = TTSCache(namespace, LocalStorage(str(tmp_path)))
    return provider


def _make_wav(frames):
    """Build a minimal valid mono 24 kHz 16-bit WAV from raw PCM frames."""
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(frames)
    return out.getvalue()


def _capture_google_post(tts, monkeypatch):
    """Replace requests.post in providers.google_tts with a recorder.

    Returns the list that collects each outgoing request's JSON payload. The
    fake response satisfies the provider's success path (HTTP 200 + base64
    audioContent) so synthesize() runs to completion.
    """
    sent = []

    def fake_post(url, params=None, json=None, timeout=None):
        sent.append(json)
        return types.SimpleNamespace(
            status_code=200,
            json=lambda: {"audioContent": base64.b64encode(b"google-audio").decode()},
            text="",
        )

    monkeypatch.setattr("providers.google_tts.requests.post", fake_post)
    return sent


def _capture_deepgram_post(tts, monkeypatch):
    """Replace requests.post in providers.deepgram_tts with a recorder.

    Returns the list that collects each outgoing request's query params (where
    the chosen model lives). The fake response carries a valid WAV so the
    provider's success path completes.
    """
    sent = []
    frames = b"\x01\x02" * 50

    def fake_post(url, params=None, headers=None, json=None, timeout=None):
        sent.append(params)
        return types.SimpleNamespace(status_code=200, content=_make_wav(frames), text="")

    monkeypatch.setattr("providers.deepgram_tts.requests.post", fake_post)
    return sent


# ── content_type selection ───────────────────────────────────────────────


def test_content_type_defaults_and_overrides():
    assert BaseTTS.content_type == "audio/wav"
    assert GoogleNeural2TTS().content_type == "audio/mpeg"
    assert DeepgramTTS().content_type == "audio/wav"

    _install_fake_genai()
    from providers.gemini_tts import GeminiTTS

    assert GeminiTTS().content_type == "audio/mpeg"


# ── Google TTS: parallel chunk synthesis / errors / cache ────────────────


def test_google_synthesize_preserves_chunk_order():
    tts = GoogleNeural2TTS()
    tts.api_key = "fake-key"
    tts._split_chunks = lambda text: ["a", "b", "c", "d"]

    def fake_chunk(text, language_code, voice_name):
        return ("[%s]" % text).encode()

    tts._synthesize_chunk = fake_chunk

    out = tts.synthesize("abcd", "en-US", voice_name="en-US-Neural2-A")
    assert out == b"[a][b][c][d]"


def test_google_synthesize_runs_chunks_concurrently(monkeypatch):
    tts = GoogleNeural2TTS()
    tts.api_key = "fake-key"

    seen = []
    lock = threading.Lock()

    def fake_chunk(text, language_code, voice_name):
        with lock:
            seen.append(text)
            n = len(seen)
        if n == 1:
            # If synthesis is sequential, no second chunk ever starts while we
            # block, and this times out — proving parallelism is required.
            if not _second_chunk_starts(seen, lock, 3):
                raise AssertionError("chunks did not run concurrently")
        return ("[%s]" % text).encode()

    monkeypatch.setattr(tts, "_synthesize_chunk", fake_chunk)
    monkeypatch.setattr(tts, "_split_chunks", lambda text: ["a", "b", "c", "d"])

    out = tts.synthesize("abcd", "en-US", voice_name="en-US-Neural2-A")
    assert out == b"[a][b][c][d]"


def _second_chunk_starts(seen, lock, timeout):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with lock:
            if len(seen) >= 2:
                return True
        time.sleep(0.01)
    return False


def test_google_synthesize_raises_runtime_error_on_chunk_failure(monkeypatch):
    tts = GoogleNeural2TTS()
    tts.api_key = "fake-key"
    monkeypatch.setattr(tts, "_split_chunks", lambda text: ["first", "second"])

    def bad_chunk(text, language_code, voice_name):
        raise RuntimeError("boom in %r" % text)

    monkeypatch.setattr(tts, "_synthesize_chunk", bad_chunk)

    with pytest.raises(RuntimeError) as excinfo:
        tts.synthesize("whole text", "en-US", voice_name="en-US-Neural2-A")

    assert "Google TTS chunk synthesis failed" in str(excinfo.value)
    assert "boom in" in str(excinfo.value)


def test_google_synthesize_skips_api_on_cache_hit(tmp_path, monkeypatch):
    tts = _cached(GoogleNeural2TTS, "google", tmp_path)
    tts.api_key = "fake-key"

    calls = []

    def fake_chunk(text, language_code, voice_name):
        calls.append(text)
        return ("<%s>" % text).encode()

    monkeypatch.setattr(tts, "_synthesize_chunk", fake_chunk)

    first = tts.synthesize("hello world", "en-US")
    second = tts.synthesize("hello world", "en-US")

    assert first == b"<hello world>"
    assert second == first
    assert len(calls) == 1  # second call served from cache, no API hit


def test_google_cache_key_distinguishes_voice_tier(tmp_path, monkeypatch):
    tts = _cached(GoogleNeural2TTS, "google", tmp_path)
    tts.api_key = "fake-key"

    calls = []
    monkeypatch.setattr(
        tts,
        "_synthesize_chunk",
        lambda text, language_code, voice_name: (
            calls.append(voice_name) or b"audio"
        ),
    )

    tts.synthesize("same text", "en-US", voice_name="en-US-Standard-A")
    tts.synthesize("same text", "en-US", voice_name="en-US-Wavenet-A")

    assert len(calls) == 2  # keys differ, no false cache hit


# ── Google TTS: locale aliases (Google's catalog != incoming BCP-47) ─────


@pytest.mark.parametrize(
    "incoming,expected_locale",
    [
        ("zh-CN", "cmn-CN"),  # Google has no zh-* voices, only cmn-CN/cmn-TW
        ("ar-SA", "ar-XA"),   # Google has no ar-SA voice
        ("bn-BD", "bn-IN"),   # Google only ships Bengali (India)
    ],
)
def test_google_sends_real_locale_for_aliased_languages(
    incoming, expected_locale, tmp_path, monkeypatch
):
    tts = _cached(GoogleNeural2TTS, "google", tmp_path)
    tts.api_key = "fake-key"
    sent = _capture_google_post(tts, monkeypatch)

    tts.synthesize("hello", incoming)

    assert len(sent) == 1
    voice = sent[0]["voice"]
    assert voice["languageCode"] == expected_locale
    assert voice["name"].startswith(expected_locale + "-")


def test_google_aliased_locale_drives_constructed_voice_name(tmp_path, monkeypatch):
    """The generated voice name must use Google's locale, not the client's."""
    tts = _cached(GoogleNeural2TTS, "google", tmp_path)
    tts.api_key = "fake-key"
    sent = _capture_google_post(tts, monkeypatch)

    tts.synthesize("hello again", "zh-CN")

    assert sent[0]["voice"]["name"] == "cmn-CN-%s-%s" % (
        tts.default_tier,
        tts.default_variant,
    )


def test_google_aliased_locale_honours_explicit_voice_tier(tmp_path, monkeypatch):
    tts = _cached(GoogleNeural2TTS, "google", tmp_path)
    tts.api_key = "fake-key"
    sent = _capture_google_post(tts, monkeypatch)

    tts.synthesize("tiered", "bn-BD", voice_tier="Studio")

    assert sent[0]["voice"]["languageCode"] == "bn-IN"
    assert sent[0]["voice"]["name"] == "bn-IN-Studio-%s" % tts.default_variant


def test_google_passes_through_unaliased_language_unchanged(tmp_path, monkeypatch):
    """A language with no alias (here Japanese) is sent exactly as received."""
    tts = _cached(GoogleNeural2TTS, "google", tmp_path)
    tts.api_key = "fake-key"
    sent = _capture_google_post(tts, monkeypatch)

    tts.synthesize("konnichiwa", "ja-JP")

    assert sent[0]["voice"]["languageCode"] == "ja-JP"
    assert sent[0]["voice"]["name"] == "ja-JP-%s-%s" % (
        tts.default_tier,
        tts.default_variant,
    )


def test_google_explicit_voice_name_keeps_resolved_language_code(tmp_path, monkeypatch):
    tts = _cached(GoogleNeural2TTS, "google", tmp_path)
    tts.api_key = "fake-key"
    sent = _capture_google_post(tts, monkeypatch)

    tts.synthesize("explicit", "ar-SA", voice_name="ar-XA-Standard-C")

    assert sent[0]["voice"] == {
        "languageCode": "ar-XA",
        "name": "ar-XA-Standard-C",
    }


def test_google_cache_key_uses_resolved_locale(tmp_path, monkeypatch):
    """zh-CN and cmn-CN describe the same audio, so they must share one cache
    entry rather than synthesizing (and storing) the same voice twice."""
    tts = _cached(GoogleNeural2TTS, "google", tmp_path)
    tts.api_key = "fake-key"
    sent = _capture_google_post(tts, monkeypatch)

    tts.synthesize("shared text", "zh-CN")
    tts.synthesize("shared text", "cmn-CN")

    assert len(sent) == 1  # second call resolved to the same key -> cache hit


def test_resolve_google_locale_is_case_and_region_insensitive():
    assert _resolve_google_locale("zh-CN") == "cmn-CN"
    assert _resolve_google_locale("zh-TW") == "cmn-CN"
    assert _resolve_google_locale("ZH-cn") == "cmn-CN"
    assert _resolve_google_locale("ar") == "ar-XA"
    assert _resolve_google_locale("bn-BD") == "bn-IN"
    # Unaliased codes come back byte-for-byte unchanged.
    assert _resolve_google_locale("en-US") == "en-US"
    assert _resolve_google_locale("ja-JP") == "ja-JP"
    assert _resolve_google_locale("pt-BR") == "pt-BR"
    assert _resolve_google_locale("") == ""


def test_google_aliases_do_not_change_supported_languages():
    """Routing must keep matching the original base codes, unchanged."""
    assert {"zh", "ar", "bn"} <= GoogleNeural2TTS.SUPPORTED_LANGUAGES
    assert "cmn" not in GoogleNeural2TTS.SUPPORTED_LANGUAGES
    assert "bn-IN" not in GoogleNeural2TTS.SUPPORTED_LANGUAGES


# ── Gemini TTS: parallel chunk synthesis + content type ──────────────────


@pytest.fixture
def gemini_cls():
    _install_fake_genai()
    from providers.gemini_tts import GeminiTTS

    return GeminiTTS


def test_gemini_synthesize_parallel_preserves_order(gemini_cls, tmp_path, monkeypatch):
    tts = _cached(gemini_cls, "gemini", tmp_path)
    monkeypatch.setattr(tts, "_split_chunks", lambda text: ["x", "y", "z"])

    def fake_chunk(text, voice):
        return ("[%s]" % text).encode()

    monkeypatch.setattr(tts, "_synthesize_chunk", fake_chunk)

    out = tts.synthesize("xyz", "en-US", voice_name="Kore")
    assert out == b"[x][y][z]"


def test_gemini_synthesize_raises_on_chunk_failure(gemini_cls, tmp_path, monkeypatch):
    tts = _cached(gemini_cls, "gemini", tmp_path)
    monkeypatch.setattr(tts, "_split_chunks", lambda text: ["x", "y"])

    def bad_chunk(text, voice):
        raise RuntimeError("gemini boom")

    monkeypatch.setattr(tts, "_synthesize_chunk", bad_chunk)

    with pytest.raises(RuntimeError) as excinfo:
        tts.synthesize("xy", "en-US", voice_name="Kore")

    assert "Gemini-TTS chunk synthesis failed" in str(excinfo.value)


def test_gemini_synthesize_caches(gemini_cls, tmp_path, monkeypatch):
    tts = _cached(gemini_cls, "gemini", tmp_path)
    calls = []

    def fake_chunk(text, voice):
        calls.append(text)
        return b"audio"

    monkeypatch.setattr(tts, "_synthesize_chunk", fake_chunk)

    assert tts.synthesize("repeated", "en-US", voice_name="Kore") == b"audio"
    assert tts.synthesize("repeated", "en-US", voice_name="Kore") == b"audio"
    assert len(calls) == 1


# ── Deepgram TTS: cache + streaming cache hit ────────────────────────────


def test_deepgram_synthesize_and_stream_use_cache(tmp_path, monkeypatch):
    tts = _cached(DeepgramTTS, "deepgram", tmp_path)
    calls = []
    frames = b"\x01\x02" * 50
    monkeypatch.setattr(
        tts,
        "_synthesize_chunk_wav",
        lambda text, model: calls.append(text) or _make_wav(frames),
    )
    monkeypatch.setattr(tts, "_split_chunks", lambda text: ["one", "two"])
    monkeypatch.setattr(tts, "_normalize_text", lambda text, lang: text)

    full = tts.synthesize("one two", "en-US")
    expected = _make_wav(frames * 2)
    assert full == expected  # both chunks synthesized + concatenated
    assert len(calls) == 2

    # Streaming the same text/voice now serves the cached WAV without touching
    # the Deepgram websocket at all.
    with patch("providers.deepgram_tts.websockets.connect") as m_connect:
        streamed = list(tts.synthesize_stream("one two", "en-US"))

    assert streamed == [expected]
    m_connect.assert_not_called()


# ── Deepgram TTS: language-appropriate default voice ─────────────────────


def test_deepgram_uses_language_specific_default_voice(tmp_path, monkeypatch):
    """With no voice_name from the UI, Japanese must not be read by an English
    Aura-2 voice."""
    tts = _cached(DeepgramTTS, "deepgram", tmp_path)
    tts.api_key = "fake-key"
    sent = _capture_deepgram_post(tts, monkeypatch)

    tts.synthesize("こんにちは", "ja-JP")

    assert len(sent) == 1
    assert sent[0]["model"] == "aura-2-izanami-ja"
    assert sent[0]["model"] != tts.default_model


def test_deepgram_english_default_voice_is_unchanged(tmp_path, monkeypatch):
    tts = _cached(DeepgramTTS, "deepgram", tmp_path)
    tts.api_key = "fake-key"
    sent = _capture_deepgram_post(tts, monkeypatch)

    tts.synthesize("hello there", "en-US")

    assert sent[0]["model"] == "aura-2-thalia-en"
    assert sent[0]["model"] == tts.default_model


@pytest.mark.parametrize("language_code", sorted(DeepgramTTS._DEFAULT_VOICE_BY_LANGUAGE))
def test_deepgram_every_supported_language_has_a_matching_voice(
    language_code, tmp_path, monkeypatch
):
    """Each supported base language resolves to a voice whose model ID ends in
    that language code — i.e. the voice can actually speak it."""
    tts = _cached(DeepgramTTS, "deepgram", tmp_path)
    tts.api_key = "fake-key"
    sent = _capture_deepgram_post(tts, monkeypatch)

    tts.synthesize("sample", language_code + "-XX")

    model = sent[0]["model"]
    assert model == DeepgramTTS._DEFAULT_VOICE_BY_LANGUAGE[language_code]
    assert model.endswith("-" + language_code)
    assert model.startswith("aura-2-")


def test_deepgram_voice_map_covers_all_supported_languages():
    """No supported language may fall through to the English default, which is
    the exact bug this map fixes."""
    assert DeepgramTTS.SUPPORTED_LANGUAGES <= set(DeepgramTTS._DEFAULT_VOICE_BY_LANGUAGE)


def test_deepgram_explicit_voice_name_overrides_language_default(tmp_path, monkeypatch):
    tts = _cached(DeepgramTTS, "deepgram", tmp_path)
    tts.api_key = "fake-key"
    sent = _capture_deepgram_post(tts, monkeypatch)

    tts.synthesize("override", "ja-JP", voice_name="aura-2-fujin-ja")

    assert sent[0]["model"] == "aura-2-fujin-ja"
    assert sent[0]["model"] != DeepgramTTS._DEFAULT_VOICE_BY_LANGUAGE["ja"]


def test_deepgram_explicit_voice_name_wins_for_english_too(tmp_path, monkeypatch):
    tts = _cached(DeepgramTTS, "deepgram", tmp_path)
    tts.api_key = "fake-key"
    sent = _capture_deepgram_post(tts, monkeypatch)

    tts.synthesize("override", "en-US", voice_name="aura-2-apollo-en")

    assert sent[0]["model"] == "aura-2-apollo-en"


def test_deepgram_unmapped_language_falls_back_to_default_model(tmp_path, monkeypatch):
    """Behaviour outside the map is unchanged: self.default_model is used, which
    is still env-overridable via DEEPGRAM_TTS_MODEL."""
    tts = _cached(DeepgramTTS, "deepgram", tmp_path)
    tts.api_key = "fake-key"
    tts.default_model = "aura-2-odysseus-en"  # stands in for the env override
    sent = _capture_deepgram_post(tts, monkeypatch)

    tts.synthesize("outside the map", "pt-BR")

    assert "pt" not in DeepgramTTS._DEFAULT_VOICE_BY_LANGUAGE
    assert sent[0]["model"] == "aura-2-odysseus-en"


def test_deepgram_cache_key_distinguishes_voice_per_language(tmp_path, monkeypatch):
    """Same text in two languages must not collide on one cache entry."""
    tts = _cached(DeepgramTTS, "deepgram", tmp_path)
    tts.api_key = "fake-key"
    sent = _capture_deepgram_post(tts, monkeypatch)

    tts.synthesize("identical text", "en-US")
    tts.synthesize("identical text", "ja-JP")

    assert [p["model"] for p in sent] == ["aura-2-thalia-en", "aura-2-izanami-ja"]


# ── Route: streaming response, fallback, mimetype, validation ────────────


class _StreamingTTS(BaseTTS):
    content_type = "audio/mpeg"

    def synthesize(self, text, language_code, voice_name=None, voice_tier=None):
        return b"FULL-AUDIO"

    def synthesize_stream(self, text, language_code, voice_name=None, voice_tier=None):
        for i in range(3):
            yield b"CHUNK%d" % i


class _WholeFallbackTTS(BaseTTS):
    content_type = "audio/wav"

    def synthesize(self, text, language_code, voice_name=None, voice_tier=None):
        return b"WHOLE-WAV"


def _make_tts_app(monkeypatch, provider):
    monkeypatch.setattr(tts_mod, "_require_auth", lambda: _ORG)
    monkeypatch.setattr(tts_mod, "get_tts_provider_for", lambda language_code=None: provider)

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.secret_key = "test"
    app.register_blueprint(tts_mod.tts_bp, url_prefix="/api")
    return app


def test_route_streams_chunks_as_they_arrive(monkeypatch):
    app = _make_tts_app(monkeypatch, _StreamingTTS())
    with app.test_client() as c:
        r = c.post(
            "/api/tts/synthesize",
            json={"text": "hello", "language_code": "en-US"},
        )
    assert r.status_code == 200
    assert r.mimetype == "audio/mpeg"
    assert r.data == b"CHUNK0CHUNK1CHUNK2"


def test_route_fallback_provider_yields_whole_audio(monkeypatch):
    app = _make_tts_app(monkeypatch, _WholeFallbackTTS())
    with app.test_client() as c:
        r = c.post(
            "/api/tts/synthesize",
            json={"text": "hello", "language_code": "en-US"},
        )
    assert r.status_code == 200
    assert r.mimetype == "audio/wav"
    assert r.data == b"WHOLE-WAV"


def test_route_returns_json_error_when_synthesis_fails(monkeypatch):
    class _FailingTTS(BaseTTS):
        def synthesize(self, text, language_code, voice_name=None, voice_tier=None):
            raise RuntimeError("provider exploded")

    app = _make_tts_app(monkeypatch, _FailingTTS())
    with app.test_client() as c:
        r = c.post(
            "/api/tts/synthesize",
            json={"text": "hello", "language_code": "en-US"},
        )
    assert r.status_code == 500
    assert r.get_json() == {"error": "internal_server_error"}


def test_route_applies_streaming_provider_with_caching(tmp_path, monkeypatch):
    """Real Google provider behind the route: first request synthesizes,
    second request streams straight from the on-disk cache."""
    tts = _cached(GoogleNeural2TTS, "google", tmp_path)
    tts.api_key = "fake-key"
    calls = []
    monkeypatch.setattr(
        tts,
        "_synthesize_chunk",
        lambda text, language_code, voice_name: calls.append(1) or b"CACHED-AUDIO",
    )
    app = _make_tts_app(monkeypatch, tts)

    with app.test_client() as c:
        r1 = c.post(
            "/api/tts/synthesize",
            json={"text": "hello", "language_code": "en-US"},
        )
        r2 = c.post(
            "/api/tts/synthesize",
            json={"text": "hello", "language_code": "en-US"},
        )

    assert r1.status_code == 200
    assert r1.mimetype == "audio/mpeg"
    assert r1.data == b"CACHED-AUDIO"
    assert r2.data == r1.data
    assert len(calls) == 1


# ── Route: input validation (contract unchanged) ─────────────────────────


def test_route_requires_text(monkeypatch):
    app = _make_tts_app(monkeypatch, _StreamingTTS())
    with app.test_client() as c:
        r = c.post(
            "/api/tts/synthesize",
            json={"text": "   ", "language_code": "en-US"},
        )
    assert r.status_code == 400
    assert r.get_json() == {"error": "text_required"}


def test_route_rejects_oversized_text(monkeypatch):
    app = _make_tts_app(monkeypatch, _StreamingTTS())
    with app.test_client() as c:
        r = c.post(
            "/api/tts/synthesize",
            json={"text": "x" * (tts_mod._MAX_TEXT_CHARS + 1), "language_code": "en-US"},
        )
    assert r.status_code == 400
    assert r.get_json() == {"error": "text_too_long"}


def test_route_requires_language_code(monkeypatch):
    app = _make_tts_app(monkeypatch, _StreamingTTS())
    with app.test_client() as c:
        r = c.post("/api/tts/synthesize", json={"text": "hello"})
    assert r.status_code == 400
    assert r.get_json() == {"error": "language_code_required"}


def test_route_keeps_request_contract(monkeypatch):
    """The exact request fields (text, language_code, translate, voice_name,
    voice_tier) are still accepted unchanged."""
    received = {}

    class _CaptureTTS(BaseTTS):
        content_type = "audio/mpeg"

        def synthesize(self, text, language_code, voice_name=None, voice_tier=None):
            received.update(
                text=text,
                language_code=language_code,
                voice_name=voice_name,
                voice_tier=voice_tier,
            )
            return b"ok"

    class _FakeLLM:
        def translate(self, text, language_code):
            return text

    monkeypatch.setattr(tts_mod, "_require_auth", lambda: _ORG)
    monkeypatch.setattr(tts_mod, "get_tts_provider_for", lambda language_code=None: _CaptureTTS())
    monkeypatch.setattr(tts_mod, "get_llm_provider", lambda: _FakeLLM())

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.secret_key = "test"
    app.register_blueprint(tts_mod.tts_bp, url_prefix="/api")
    with app.test_client() as c:
        r = c.post(
            "/api/tts/synthesize",
            json={
                "text": "Hola",
                "language_code": "es-ES",
                "translate": True,
                "voice_name": "kv-es-ES",
                "voice_tier": "Standard",
            },
        )

    assert r.status_code == 200
    assert r.data == b"ok"
    assert received["text"] == "Hola"
    assert received["language_code"] == "es-ES"
    assert received["voice_name"] == "kv-es-ES"
    assert received["voice_tier"] == "Standard"
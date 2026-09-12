"""Tests for the TTS pipeline: parallel chunking, streaming route, caching,
content-type selection, and request validation.

Provider HTTP calls (Google/Deepgram) and SDK calls (Gemini) are all mocked —
no real APIs are contacted.
"""

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
from providers.google_tts import GoogleNeural2TTS
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
    monkeypatch.setattr(tts_mod, "get_tts_provider", lambda: provider)

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
    monkeypatch.setattr(tts_mod, "get_tts_provider", lambda: _CaptureTTS())
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
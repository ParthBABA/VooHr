"""Tests for the Deepgram speech-to-text provider.

Covers:
1. transcribe() posts to Deepgram's prerecorded /listen endpoint and parses
   results.channels[0].alternatives[0].transcript correctly.
2. transcribe() returns the "[STT not configured ...]" string when no
   DEEPGRAM_API_KEY is set (matching openai_stt.py's convention).
3. Non-200 responses raise a clear, debuggable exception.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from providers.deepgram_stt import DeepgramSTT


def _fake_response(status=200, payload=None):
    resp = MagicMock()
    resp.status_code = status
    if payload is not None:
        resp.json.return_value = payload
    else:
        resp.json.side_effect = RuntimeError("json was not read")
    return resp


TRANSCRIPT_PAYLOAD = {
    "results": {
        "channels": [
            {
                "alternatives": [
                    {"transcript": "  Hello there, this is a test.  "}
                ]
            }
        ]
    }
}


class TestDeepgramSTTParse:
    def test_transcribe_posts_raw_audio_and_parses_transcript(self):
        stt = DeepgramSTT()
        stt.api_key = "fake-key"

        resp = _fake_response(200, TRANSCRIPT_PAYLOAD)

        with patch("providers.deepgram_stt.requests.post", return_value=resp) as m_post:
            out = stt.transcribe(b"FAKE-AUDIO-BYTES", "audio/webm")

        assert out == "Hello there, this is a test."
        args, kwargs = m_post.call_args
        assert args[0] == "https://api.deepgram.com/v1/listen"
        assert kwargs["data"] == b"FAKE-AUDIO-BYTES"
        assert kwargs["headers"]["Authorization"] == "Token fake-key"
        assert kwargs["headers"]["Content-Type"] == "audio/webm"
        # Default language is English, which maps to nova-3 + language=en.
        assert kwargs["params"]["model"] == "nova-3"
        assert kwargs["params"]["language"] == "en"
        assert kwargs["params"]["smart_format"] == "true"
        assert kwargs["params"]["punctuate"] == "true"

    def test_transcribe_hinglish_uses_nova2_hi_latn(self, monkeypatch):
        """Hinglish maps to Deepgram hi-Latn which is only on nova-2."""
        monkeypatch.delenv("DEEPGRAM_STT_MODEL", raising=False)
        stt = DeepgramSTT()
        stt.api_key = "fake-key"

        resp = _fake_response(200, TRANSCRIPT_PAYLOAD)

        with patch("providers.deepgram_stt.requests.post", return_value=resp) as m_post:
            stt.transcribe(b"data", "audio/webm", "hinglish")

        _, kwargs = m_post.call_args
        assert kwargs["params"]["language"] == "hi-Latn"
        assert kwargs["params"]["model"] == "nova-2"

    def test_transcribe_language_is_case_insensitive(self, monkeypatch):
        monkeypatch.delenv("DEEPGRAM_STT_MODEL", raising=False)
        stt = DeepgramSTT()
        stt.api_key = "fake-key"

        resp = _fake_response(200, TRANSCRIPT_PAYLOAD)

        with patch("providers.deepgram_stt.requests.post", return_value=resp) as m_post:
            stt.transcribe(b"data", "audio/webm", "HINGLISH")

        _, kwargs = m_post.call_args
        assert kwargs["params"]["language"] == "hi-Latn"
        assert kwargs["params"]["model"] == "nova-2"

    def test_transcribe_hi_uses_devanagari_nova3(self, monkeypatch):
        monkeypatch.delenv("DEEPGRAM_STT_MODEL", raising=False)
        stt = DeepgramSTT()
        stt.api_key = "fake-key"

        resp = _fake_response(200, TRANSCRIPT_PAYLOAD)

        with patch("providers.deepgram_stt.requests.post", return_value=resp) as m_post:
            stt.transcribe(b"data", "audio/webm", "hi")

        _, kwargs = m_post.call_args
        assert kwargs["params"]["language"] == "hi"
        assert kwargs["params"]["model"] == "nova-3"

    def test_transcribe_auto_uses_multi_nova3(self, monkeypatch):
        monkeypatch.delenv("DEEPGRAM_STT_MODEL", raising=False)
        stt = DeepgramSTT()
        stt.api_key = "fake-key"

        resp = _fake_response(200, TRANSCRIPT_PAYLOAD)

        with patch("providers.deepgram_stt.requests.post", return_value=resp) as m_post:
            stt.transcribe(b"data", "audio/webm", "auto")

        _, kwargs = m_post.call_args
        assert kwargs["params"]["language"] == "multi"
        assert kwargs["params"]["model"] == "nova-3"

    def test_transcribe_region_code_maps_to_deepgram_region(self, monkeypatch):
        monkeypatch.delenv("DEEPGRAM_STT_MODEL", raising=False)
        stt = DeepgramSTT()
        stt.api_key = "fake-key"

        resp = _fake_response(200, TRANSCRIPT_PAYLOAD)

        with patch("providers.deepgram_stt.requests.post", return_value=resp) as m_post:
            stt.transcribe(b"data", "audio/webm", "en-in")

        _, kwargs = m_post.call_args
        assert kwargs["params"]["language"] == "en-IN"
        assert kwargs["params"]["model"] == "nova-3"

    def test_transcribe_unknown_language_falls_back_to_english(self, monkeypatch):
        """An unrecognized language must never hard-fail the request."""
        monkeypatch.delenv("DEEPGRAM_STT_MODEL", raising=False)
        stt = DeepgramSTT()
        stt.api_key = "fake-key"

        resp = _fake_response(200, TRANSCRIPT_PAYLOAD)

        with patch("providers.deepgram_stt.requests.post", return_value=resp) as m_post:
            stt.transcribe(b"data", "audio/webm", "klingon")

        _, kwargs = m_post.call_args
        assert kwargs["params"]["language"] == "en"
        assert kwargs["params"]["model"] == "nova-3"

    def test_transcribe_respects_deepgram_stt_model_env(self, monkeypatch):
        monkeypatch.setenv("DEEPGRAM_STT_MODEL", "nova-3")
        stt = DeepgramSTT()
        stt.api_key = "fake-key"

        resp = _fake_response(200, TRANSCRIPT_PAYLOAD)

        with patch("providers.deepgram_stt.requests.post", return_value=resp) as m_post:
            stt.transcribe(b"data", "audio/mpeg")

        _, kwargs = m_post.call_args
        assert kwargs["params"]["model"] == "nova-3"

    def test_transcribe_default_model_is_nova_2(self, monkeypatch):
        monkeypatch.delenv("DEEPGRAM_STT_MODEL", raising=False)
        assert DeepgramSTT().model == "nova-2"

    def test_transcribe_strips_whitespace_from_empty_transcript(self):
        stt = DeepgramSTT()
        stt.api_key = "fake-key"

        empty_payload = {
            "results": {
                "channels": [{"alternatives": [{"transcript": "   "}]}]
            }
        }
        resp = _fake_response(200, empty_payload)

        with patch("providers.deepgram_stt.requests.post", return_value=resp):
            assert stt.transcribe(b"data") == ""

    def test_transcribe_raises_on_non_200(self):
        stt = DeepgramSTT()
        stt.api_key = "fake-key"

        resp = _fake_response(400, {"err_code": "bad_request"})
        resp.text = "some error body"

        with patch("providers.deepgram_stt.requests.post", return_value=resp):
            with pytest.raises(RuntimeError) as excinfo:
                stt.transcribe(b"data")

        assert "400" in str(excinfo.value)
        assert "some error body" in str(excinfo.value)


class TestDeepgramSTTNotConfigured:
    def test_no_api_key_returns_not_configured_string(self, monkeypatch):
        monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
        monkeypatch.delenv("DEEPGRAM", raising=False)

        stt = DeepgramSTT()
        out = stt.transcribe(b"data")

        assert out == "[STT not configured — set DEEPGRAM_API_KEY in .env]"


class TestDeepgramSTTEnv:
    def test_api_key_reads_primary_env_var(self, monkeypatch):
        monkeypatch.setenv("DEEPGRAM_API_KEY", "primary-key")
        monkeypatch.delenv("DEEPGRAM", raising=False)
        assert DeepgramSTT().api_key == "primary-key"

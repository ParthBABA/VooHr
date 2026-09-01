import base64
import logging
import os

import requests

from providers.tts import BaseTTS

logger = logging.getLogger(__name__)

_TTS_ENDPOINT = "https://texttospeech.googleapis.com/v1/text:synthesize"

_MODEL_NAME = "gemini-3.1-flash-tts-preview"

# Cloud Text-to-Speech Gemini-TTS hard limit is 4000 bytes of input text per
# request. We use a conservative margin well below that. This matters a lot
# for non-Latin scripts (e.g. Hindi/Devanagari) which run ~2.5-3 bytes per
# character.
_MAX_CHUNK_BYTES = 3500

# Sentence boundary characters used to split long text into aligned chunks.
# Includes the Devanagari danda ('।') used by Hindi/Nepali/etc.
_SENTENCE_BOUNDARIES = ".!?\u0964"

# Gemini-TTS prebuilt voice names, e.g. "Kore", "Leda".
_DEFAULT_VOICE = "Kore"


class GeminiTTS(BaseTTS):
    """Google Cloud Text-to-Speech Gemini-TTS provider using the REST API directly.

    Uses the ``gemini-3.1-flash-tts-preview`` model. The voice is selected via
    *voice_name* (a Gemini-TTS prebuilt voice such as "Kore" or "Leda") and
    defaults to the ``GEMINI_TTS_VOICE`` env var (or "Kore").
    """

    def __init__(self):
        self.api_key = (
            os.environ.get("GEMINI_TTS_API_KEY")
            or os.environ.get("GOOGLE_TTS_API_KEY")
            or os.environ.get("GOOGLE_TTS", "")
        )
        self.default_voice = os.environ.get("GEMINI_TTS_VOICE", _DEFAULT_VOICE)
        self.endpoint = _TTS_ENDPOINT
        self.model_name = _MODEL_NAME

    def _ensure_api_key(self) -> str:
        if not self.api_key:
            raise RuntimeError(
                "Gemini-TTS API key is not configured. Set GEMINI_TTS_API_KEY "
                "(or GOOGLE_TTS_API_KEY / GOOGLE_TTS) in the environment."
            )
        return self.api_key

    def _split_chunks(self, text: str) -> list:
        """Split text into byte-safe chunks at sentence boundaries.

        Each chunk stays under _MAX_CHUNK_BYTES when UTF-8 encoded. When no
        sentence boundary fits within the byte limit, the chunk is hard-split
        at the furthest byte-safe character and splitting resumes after it.
        """
        if len(text.encode("utf-8")) <= _MAX_CHUNK_BYTES:
            return [text]

        chunks = []
        n = len(text)
        start = 0
        while start < n:
            if len(text[start:].encode("utf-8")) <= _MAX_CHUNK_BYTES:
                chunks.append(text[start:])
                break

            byte_len = 0
            i = start
            last_boundary = None
            while i < n:
                char = text[i]
                char_bytes = len(char.encode("utf-8"))
                if byte_len + char_bytes > _MAX_CHUNK_BYTES:
                    break
                byte_len += char_bytes
                i += 1
                if char in _SENTENCE_BOUNDARIES:
                    last_boundary = i

            end = last_boundary if last_boundary is not None else i
            chunks.append(text[start:end])
            start = end

        return chunks

    def _synthesize_chunk(self, text: str, language_code: str, voice_name: str) -> bytes:
        api_key = self._ensure_api_key()
        payload = {
            "input": {"text": text},
            "voice": {
                "languageCode": language_code,
                "name": voice_name,
                "modelName": self.model_name,
            },
            "audioConfig": {"audioEncoding": "MP3"},
        }
        try:
            resp = requests.post(
                self.endpoint,
                params={"key": api_key},
                json=payload,
                timeout=60,
            )
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Gemini-TTS request failed: {exc}"
            ) from exc

        if resp.status_code != 200:
            raise RuntimeError(
                f"Gemini-TTS returned HTTP {resp.status_code}: {resp.text}"
            )

        data = resp.json()
        audio_b64 = data.get("audioContent")
        if not audio_b64:
            raise RuntimeError("Gemini-TTS response contained no audio content")

        return base64.b64decode(audio_b64)

    def synthesize(self, text: str, language_code: str, voice_name: str = None, voice_tier: str = None) -> bytes:
        text = (text or "").strip()
        if not text:
            return b""

        voice = voice_name or self.default_voice

        chunks = self._split_chunks(text)
        logger.debug(
            "Gemini-TTS synthesize: language=%s voice=%s chunks=%d bytes=%d",
            language_code, voice, len(chunks), len(text.encode("utf-8")),
        )

        parts = []
        for chunk in chunks:
            parts.append(self._synthesize_chunk(chunk, language_code, voice))

        return b"".join(parts)

import logging
import os

import requests

from providers.tts import BaseTTS

logger = logging.getLogger(__name__)

_TTS_ENDPOINT = "https://api.deepgram.com/v1/speak"
_DEFAULT_MODEL = "aura-2-thalia-en"

# Deepgram Aura (Aura-2 / Aura-1) caps each request at 2000 characters.
# We use a conservative margin below that. This matters for non-Latin
# scripts (e.g. Hindi/Devanagari) which run ~2.5-3 bytes per character.
_MAX_CHUNK_CHARS = 1900

# Sentence boundary characters used to split long text into aligned chunks.
# Includes the Devanagari danda ('।') used by Hindi/Nepali/etc.
_SENTENCE_BOUNDARIES = ".!?\u0964"


class DeepgramTTS(BaseTTS):
    """Deepgram Aura Text-to-Speech provider using the REST API directly.

    Uses the ``aura-2-thalia-en`` voice/model by default (a clear, confident,
    feminine American English voice). The model may be overridden via the
    ``DEEPGRAM_TTS_MODEL`` env var. The API key is read from the
    ``DEEPGRAM_API_KEY`` (or ``DEEPGRAM``) env var and is never exposed to
    the client.
    """

    def __init__(self):
        self.api_key = (
            os.environ.get("DEEPGRAM_API_KEY") or os.environ.get("DEEPGRAM", "")
        )
        self.default_model = os.environ.get("DEEPGRAM_TTS_MODEL", _DEFAULT_MODEL)
        self.endpoint = _TTS_ENDPOINT

    def _ensure_api_key(self) -> str:
        if not self.api_key:
            raise RuntimeError(
                "Deepgram Text-to-Speech API key is not configured. Set "
                "DEEPGRAM_API_KEY (or DEEPGRAM) in the environment."
            )
        return self.api_key

    def _split_chunks(self, text: str) -> list:
        """Split text into chunks at sentence boundaries under the char limit."""
        if len(text) <= _MAX_CHUNK_CHARS:
            return [text]

        chunks = []
        n = len(text)
        start = 0
        while start < n:
            if len(text[start:]) <= _MAX_CHUNK_CHARS:
                chunks.append(text[start:])
                break

            # Extend the chunk to just under the char limit, remembering the
            # furthest sentence boundary that still fits so we can split on a
            # sentence end when possible.
            i = start
            last_boundary = None
            while i < n and i - start < _MAX_CHUNK_CHARS:
                if text[i] in _SENTENCE_BOUNDARIES:
                    last_boundary = i + 1
                i += 1

            # Prefer the furthest sentence boundary; otherwise hard-split at
            # the furthest character that still fits.
            end = last_boundary if last_boundary is not None else i
            chunks.append(text[start:end])
            start = end

        return chunks

    def _synthesize_chunk(self, text: str, model: str) -> bytes:
        api_key = self._ensure_api_key()
        headers = {
            "Authorization": f"Token {api_key}",
            "Content-Type": "application/json",
        }
        params = {"model": model}
        payload = {"text": text}
        try:
            resp = requests.post(
                self.endpoint,
                params=params,
                headers=headers,
                json=payload,
                timeout=60,
            )
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Deepgram TTS request failed: {exc}"
            ) from exc

        if resp.status_code != 200:
            raise RuntimeError(
                f"Deepgram TTS returned HTTP {resp.status_code}: {resp.text}"
            )

        if not resp.content:
            raise RuntimeError("Deepgram TTS response contained no audio content")

        return resp.content

    def synthesize(self, text: str, language_code: str, voice_name: str = None, voice_tier: str = None) -> bytes:
        text = (text or "").strip()
        if not text:
            return b""

        model = voice_name or self.default_model

        chunks = self._split_chunks(text)
        logger.debug(
            "Deepgram TTS synthesize: model=%s chunks=%d chars=%d",
            model, len(chunks), len(text),
        )

        parts = []
        for chunk in chunks:
            parts.append(self._synthesize_chunk(chunk, model))

        return b"".join(parts)

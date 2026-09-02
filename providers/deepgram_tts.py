import asyncio
import json
import logging
import os
import queue
import threading

import requests
import websockets

from providers.tts import BaseTTS
from providers.text_normalize import humanize_numbers

logger = logging.getLogger(__name__)

_TTS_WS_ENDPOINT = "wss://api.deepgram.com/v1/speak"
_TTS_ENDPOINT = "https://api.deepgram.com/v1/speak"
_DEFAULT_MODEL = "aura-2-thalia-en"

# Deepgram Aura (Aura-2 / Aura-1) caps each request at 2000 characters.
# We use a conservative margin below that. This matters for non-Latin
# scripts (e.g. Hindi/Devanagari) which run ~2.5-3 bytes per character.
_MAX_CHUNK_CHARS = 1900

# Sentence boundary characters used to split long text into aligned chunks.
# Includes the Devanagari danda ('।') used by Hindi/Nepali/etc.
_SENTENCE_BOUNDARIES = ".!?\u0964"

# WebSocket streaming emits raw audio only. linear16 is 16-bit signed little-
# endian PCM; the default streaming-compatible encoding. 24 kHz is the
# Deepgram streaming default sample rate.
_ENCODING = "linear16"
_SAMPLE_RATE = 24000


class _DoneSentinel:
    pass


# Thread-queue sentinels used to hand control messages from the WebSocket
# producer thread back to the streaming generator.
_DONE = _DoneSentinel()


def _ERROR_SENTINEL(exc):
    return exc


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
        self.ws_endpoint = _TTS_WS_ENDPOINT

    def _ensure_api_key(self) -> str:
        if not self.api_key:
            raise RuntimeError(
                "Deepgram Text-to-Speech API key is not configured. Set "
                "DEEPGRAM_API_KEY (or DEEPGRAM) in the environment."
            )
        return self.api_key

    def _normalize_text(self, text: str, language_code: str) -> str:
        """Rewrite numbers/quantities into words, falling back to raw text."""
        try:
            return humanize_numbers(text, language_code)
        except Exception:
            logger.warning(
                "Deepgram TTS number normalization failed; using raw text "
                "(language_code=%s)", language_code,
                exc_info=True,
            )
            return text

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

        text = self._normalize_text(text, language_code)
        chunks = self._split_chunks(text)
        logger.debug(
            "Deepgram TTS synthesize: model=%s chunks=%d chars=%d",
            model, len(chunks), len(text),
        )

        parts = []
        for chunk in chunks:
            parts.append(self._synthesize_chunk(chunk, model))

        return b"".join(parts)

    def synthesize_stream(self, text: str, language_code: str, voice_name: str = None, voice_tier: str = None):
        """Synthesize text and yield raw linear16 PCM audio chunks as they arrive.

        Uses Deepgram's WebSocket streaming endpoint so audio playback can
        begin as soon as the first chunk is available, rather than waiting
        for the complete response.

        Yields:
            bytes: Raw linear16 PCM audio chunks (16-bit signed little-endian,
            24 kHz mono — intended for incremental streaming playback).
        """
        text = (text or "").strip()
        if not text:
            return

        model = voice_name or self.default_model
        api_key = self._ensure_api_key()
        text = self._normalize_text(text, language_code)
        chunks = self._split_chunks(text)

        params = (
            f"model={model}&encoding={_ENCODING}&sample_rate={_SAMPLE_RATE}"
        )
        ws_url = f"{self.ws_endpoint}?{params}"

        # Stream chunk-by-chunk over a single websocket. Each chunk stays well
        # under Deepgram's 2000-character cap; sending + flushing one chunk at
        # a time keeps the server's pending text buffer bounded. Audio for
        # each chunk streams back incrementally as it is synthesized, so the
        # browser can begin playback from the very first chunk.
        logger.debug(
            "Deepgram TTS synthesize_stream: model=%s chunks=%d chars=%d",
            model, len(chunks), len(text),
        )

        # The WebSocket I/O runs on a background thread (its own event loop);
        # each incoming audio chunk is handed back to this generator through a
        # thread-safe queue, and yielded to the caller as soon as it arrives.
        chunks_queue = queue.Queue()

        def _producer():
            async def _run():
                async with websockets.connect(
                    ws_url,
                    additional_headers={"Authorization": f"Token {api_key}"},
                    open_timeout=30,
                ) as ws:
                    for chunk in chunks:
                        # Send one chunk of text, then flush to signal end of
                        # input and collect the audio it produced.
                        await ws.send(json.dumps({"type": "Speak", "text": chunk}))
                        await ws.send(json.dumps({"type": "Flush"}))

                        while True:
                            try:
                                message = await ws.recv()
                            except websockets.ConnectionClosed:
                                return
                            if isinstance(message, bytes):
                                if message:
                                    chunks_queue.put(message)
                                continue
                            # Text control messages (metadata, flushed, etc.)
                            # are ignored; audio is carried in binary frames.
                            try:
                                msg = json.loads(message)
                            except (TypeError, ValueError):
                                continue
                            if msg.get("type") == "Flushed":
                                # All audio for this chunk has been delivered.
                                break
                            if msg.get("type") == "Warning":
                                logger.warning(
                                    "Deepgram TTS warning: %s", msg.get("message")
                                )

            try:
                asyncio.run(_run())
            except Exception as exc:
                chunks_queue.put(_ERROR_SENTINEL(exc))
            finally:
                chunks_queue.put(_DONE)

        threading.Thread(target=_producer, daemon=True).start()

        while True:
            item = chunks_queue.get()
            if item is _DONE:
                break
            if isinstance(item, Exception):
                raise RuntimeError(f"Deepgram TTS streaming failed: {item}") from item
            if item:
                yield item

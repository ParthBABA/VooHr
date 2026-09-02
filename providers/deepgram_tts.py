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

# REST (non-streaming) synthesis encoding. Deepgram's "Audio Format
# Combinations" table allows exactly two bitrates for mp3: 32000 and 48000
# (48000 is Deepgram's default). Sending any other value is invalid and would
# be rejected/coerced by the API, so we validate locally instead of letting an
# invalid value through silently.
_REST_ENCODING = "mp3"
_MP3_BIT_RATES = (32000, 48000)
_REST_BIT_RATE = 48000


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

    def _synthesize_chunk(self, text: str, model: str, bit_rate: int = _REST_BIT_RATE) -> bytes:
        api_key = self._ensure_api_key()
        # mp3 only accepts 32000 or 48000 bps per Deepgram's Audio Format
        # Combinations table. Reject anything else early with a clear error
        # instead of silently sending an invalid bitrate to the API.
        if _REST_ENCODING == "mp3" and bit_rate not in _MP3_BIT_RATES:
            raise ValueError(
                f"Invalid mp3 bit_rate {bit_rate!r}: Deepgram only supports "
                f"bitrates {sorted(_MP3_BIT_RATES)} for mp3 encoding"
            )
        headers = {
            "Authorization": f"Token {api_key}",
            "Content-Type": "application/json",
        }
        # MP3 is Deepgram's default encoding and 48000 its default bitrate, but
        # we send them explicitly (rather than relying on the implicit default)
        # so the response format is pinned and predictable.
        params = {
            "model": model,
            "encoding": _REST_ENCODING,
            "bit_rate": bit_rate,
        }
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

    def _strip_mp3_prelude(self, data: bytes) -> bytes:
        """Remove an MP3 stream's leading header/metadata so it can be spliced.

        Deepgram wraps each REST mp3 response with its own ID3v2 tag and an
        initial Xing/Info/LAME header frame. Naively concatenating several
        independent mp3 streams leaves those per-chunk headers embedded in the
        joined output; decoders may emit a click/pop or skip audio where they
        suddenly reappear mid-stream.

        This strips the ID3v2 tag and the initial Xing/Info/LAME header frame
        (a metadata-only first MPEG frame) from a chunk sent to the splicer. If
        anything looks malformed the bytes are returned unchanged so we never
        corrupt audio.
        """
        body = self._skip_id3v2(data)
        frame = self._find_mp3_frame(body)
        if frame is None:
            return body
        head = frame[4:40]
        if head.find(b"Xing") >= 0 or head.find(b"Info") >= 0 or head.find(b"LAME") >= 0:
            length = self._mp3_frame_len(frame)
            start = body.find(frame)
            return body[start + length:]
        return body

    @staticmethod
    def _skip_id3v2(data: bytes) -> bytes:
        """Return *data* with any leading ID3v2 tag removed."""
        if data[:3] == b"ID3" and len(data) >= 10:
            size = (
                ((data[6] & 0x7F) << 21)
                | ((data[7] & 0x7F) << 14)
                | ((data[8] & 0x7F) << 7)
                | (data[9] & 0x7F)
            )
            end = 10 + size
            if end <= len(data):
                return data[end:]
        return data

    @staticmethod
    def _find_mp3_frame(data: bytes) -> bytes:
        """Return the MPEG audio frame header bytes if a frame sync is found."""
        for i in range(min(len(data) - 3, 1024 * 4)):
            if data[i] == 0xFF and (data[i + 1] & 0xE0) == 0xE0:
                return data[i:i + 4]
        return None

    @staticmethod
    def _mp3_frame_len(header: bytes) -> int:
        """Best-effort MPEG frame length from a 4-byte frame header."""
        if len(header) < 4:
            return 4
        bitrate_idx = (header[2] >> 4) & 0x0F
        srate_idx = (header[2] >> 2) & 0x03
        padding = (header[2] >> 1) & 0x01
        version = (header[1] >> 3) & 0x03  # 3=MPEG1, 2=MPEG2/2.5
        layer = (header[1] >> 1) & 0x03    # 1=LayerIII, 2=LayerII, 3=LayerI

        bitrates = {
            (1, 1): [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
            (1, 2): [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384],
            (1, 3): [0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448],
            (2, 1): [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256],
            (2, 2): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
            (2, 3): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
        }
        if bitrate_idx == 0 or bitrate_idx >= 15:
            return 4
        if layer == 0:  # reserved
            return 4
        srates = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}
        srate = srates.get(version, srates[3])[srate_idx] if srate_idx < 3 else 0
        br = bitrates.get((version, layer), [0] * 16).__getitem__(bitrate_idx) * 1000
        if br <= 0 or srate <= 0:
            return 4
        if layer == 3:  # Layer I
            return int((12 * br / srate + padding) * 4)
        if layer == 2:  # Layer II
            return int(144 * br / srate + padding)
        # Layer III: MPEG1 uses 144 slots/frame, MPEG2/2.5 uses 72.
        if version == 1:
            return int(144 * br / srate + padding)
        return int(72 * br / srate + padding)

    def _join_mp3_parts(self, parts: list) -> bytes:
        """Concatenate independent mp3 byte streams with clean splices.

        Each Deepgram response is a self-contained mp3 stream carrying its own
        ID3v2 tag and Xing/Info/LAME header frame. Naively ``b"".join``ing
        them leaves a foreign header frame mid-stream at every boundary, which
        decoders can render as a click/pop/static. We keep the first chunk
        intact (it carries the tag) and strip the header prelude from every
        subsequent chunk before joining, so the output is one continuous mp3
        stream.
        """
        if len(parts) == 1:
            return parts[0]
        joined = [parts[0]]
        for part in parts[1:]:
            joined.append(self._strip_mp3_prelude(part))
        return b"".join(joined)

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

        # For text split across multiple requests we join the independent mp3
        # streams, stripping each trailing chunk's ID3/Xing header so no
        # mid-stream header frames cause boundary clicks/pops.
        return self._join_mp3_parts(parts)

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

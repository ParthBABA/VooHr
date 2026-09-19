import io
import logging
import os
import wave

import requests
import websockets  # kept for test patch target compatibility

from providers.tts import BaseTTS
from providers.text_normalize import prepare_text_for_speech
from providers.tts_cache import TTSCache

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

# Synthesis encoding is linear16 (16-bit signed little-endian PCM) at 24 kHz,
# Deepgram's default sample rate. linear16 is streaming-native, so sample rate
# is explicit. Chunks concatenate sample-for-sample without per-chunk encoder
# state or the clicks/pops that splicing independent mp3 streams produces.
_SAMPLE_RATE = 24000


class DeepgramTTS(BaseTTS):
    """Deepgram Aura Text-to-Speech provider using the REST API directly.

    Uses the ``aura-2-thalia-en`` voice/model by default (a clear, confident,
    feminine American English voice). The model may be overridden via the
    ``DEEPGRAM_TTS_MODEL`` env var. The API key is read from the
    ``DEEPGRAM_API_KEY`` (or ``DEEPGRAM``) env var and is never exposed to
    the client.
    """

    # Base language codes Deepgram Aura-2 can actually voice. Anything outside
    # this set MUST be routed elsewhere — forcing it through the default English
    # model produces garbled, mispronounced audio.
    SUPPORTED_LANGUAGES = frozenset({"en", "es", "nl", "de", "fr", "it", "ja"})

    def __init__(self):
        self.api_key = (
            os.environ.get("DEEPGRAM_API_KEY") or os.environ.get("DEEPGRAM", "")
        )
        self.default_model = os.environ.get("DEEPGRAM_TTS_MODEL", _DEFAULT_MODEL)
        self.endpoint = _TTS_ENDPOINT
        self._tts_cache = TTSCache("deepgram")

    def _ensure_api_key(self) -> str:
        if not self.api_key:
            raise RuntimeError(
                "Deepgram Text-to-Speech API key is not configured. Set "
                "DEEPGRAM_API_KEY (or DEEPGRAM) in the environment."
            )
        return self.api_key

    def _normalize_text(self, text: str, language_code: str) -> str:
        """Strip symbols then humanize numbers, falling back to raw text."""
        try:
            return prepare_text_for_speech(text, language_code)
        except Exception:
            logger.warning(
                "Deepgram TTS text normalization failed; using raw text "
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

    @staticmethod
    def _normalize_wav(audio: bytes) -> bytes:
        """Re-read and re-write a WAV to fix malformed RIFF/data size headers.

        Deepgram's REST ``container=wav`` response sometimes carries
        placeholder sizes (``0x7FFF0000``) instead of the true byte counts.
        Most browser ``<audio>`` decoders reject such headers, but Python's
        ``wave`` module tolerates them by reading to EOF — so we re-emit
        through ``wave`` to produce correct size fields.
        """
        try:
            with wave.open(io.BytesIO(audio), "rb") as src:
                nchannels = src.getnchannels()
                sampwidth = src.getsampwidth()
                framerate = src.getframerate()
                frames = src.readframes(src.getnframes())
        except (wave.Error, EOFError, OSError):
            return audio
        out = io.BytesIO()
        with wave.open(out, "wb") as dst:
            dst.setnchannels(nchannels)
            dst.setsampwidth(sampwidth)
            dst.setframerate(framerate)
            dst.writeframes(frames)
        return out.getvalue()

    def _synthesize_chunk_wav(self, text: str, model: str) -> bytes:
        """Synthesize one chunk to a fully-formed WAV file via the REST API.

        Uses ``linear16`` PCM wrapped in a WAV container at 24 kHz (raw PCM
        carries no per-chunk framing, so independent chunks can be
        concatenated sample-for-sample without the clicks/pops that splicing
        independent mp3 streams produces). ``linear16`` is a streaming-native
        encoding, so sample rate is explicit (Deepgram default is 24 kHz).

        The returned WAV is normalized so that RIFF and data chunk sizes match
        the actual payload length (Deepgram sometimes returns placeholder
        sizes that browser decoders cannot handle).
        """
        api_key = self._ensure_api_key()
        headers = {
            "Authorization": f"Token {api_key}",
            "Content-Type": "application/json",
        }
        params = {
            "model": model,
            "encoding": "linear16",
            "container": "wav",
            "sample_rate": _SAMPLE_RATE,
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

        return self._normalize_wav(resp.content)

    @staticmethod
    def _concat_wav_chunks(wav_parts) -> bytes:
        """Concatenate multiple WAV files into one continuous WAV.

        Raw PCM concatenates cleanly (no per-chunk encoder state or framing),
        so we read the samples out of each WAV and write them all into one
        new WAV. All chunks are synthesized with the same sample rate /
        channels, so this is a trivial sample-for-sample copy.

        A list of length 1 is passed through unchanged (single-chunk no-op).
        """
        if len(wav_parts) == 1:
            return wav_parts[0]

        sample_width = None
        sample_rate = None
        channels = None
        frames = []
        for part in wav_parts:
            with wave.open(io.BytesIO(part), "rb") as w:
                sample_rate = w.getframerate()
                sample_width = w.getsampwidth()
                channels = w.getnchannels()
                frames.append(w.readframes(w.getnframes()))

        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setnchannels(channels)
            w.setsampwidth(sample_width)
            w.setframerate(sample_rate)
            w.writeframes(b"".join(frames))
        return out.getvalue()

    def synthesize(self, text: str, language_code: str, voice_name: str = None, voice_tier: str = None) -> bytes:
        text = (text or "").strip()
        if not text:
            return b""

        model = voice_name or self.default_model

        cache_key = self._tts_cache.build_key(
            text, language_code, model, voice_tier
        )
        cached = self._tts_cache.get(cache_key)
        if cached is not None:
            logger.debug(
                "Deepgram TTS cache hit: model=%s", model,
            )
            return cached

        text = self._normalize_text(text, language_code)
        chunks = self._split_chunks(text)
        logger.debug(
            "Deepgram TTS synthesize: model=%s chunks=%d chars=%d",
            model, len(chunks), len(text),
        )

        # Always return ONE complete WAV file so the browser can decode the
        # whole narration as a single contiguous AudioBuffer (no per-chunk
        # seams). Each chunk is synthesized independently as linear16 WAV and
        # concatenated sample-for-sample; raw PCM splices cleanly with no
        # clicks/pops (unlike splicing independent mp3 streams).
        if len(chunks) == 1:
            audio = self._synthesize_chunk_wav(chunks[0], model)
        else:
            parts = [self._synthesize_chunk_wav(chunk, model) for chunk in chunks]
            audio = self._concat_wav_chunks(parts)
        self._tts_cache.set(cache_key, audio)
        return audio

    def synthesize_stream(self, text: str, language_code: str, voice_name: str = None, voice_tier: str = None):
        """Yield the complete, standards-compliant WAV as a single chunk.

        The narration mini-player (static/narration-stream.js) loads the
        response body as a single ``Blob`` into an ``<audio>`` element and
        expects **one complete WAV** it can decode natively. Raw linear16 PCM
        chunks from the WebSocket endpoint cannot be decoded by ``<audio>``,
        so we delegate to :meth:`synthesize` (which returns a single,
        cache-aware, normalized WAV) and yield it as one chunk.

        Providers that support incremental decoding may override this to yield
        smaller chunks; for Deepgram, the only consumer requires a single
        complete file.
        """
        text = (text or "").strip()
        if not text:
            return

        audio = self.synthesize(
            text, language_code, voice_name=voice_name, voice_tier=voice_tier
        )
        if audio:
            yield audio

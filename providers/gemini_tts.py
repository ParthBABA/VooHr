import base64
import logging
import os
from concurrent.futures import ThreadPoolExecutor

from google import genai

from providers.tts import BaseTTS
from providers.tts_cache import TTSCache

logger = logging.getLogger(__name__)

_MODEL_NAME = "gemini-3.1-flash-tts-preview"

# Gemini-TTS input is limited to 4000 bytes per request. We use a
# conservative margin well below that. This matters a lot for non-Latin
# scripts (e.g. Hindi/Devanagari) which run ~2.5-3 bytes per character.
_MAX_CHUNK_BYTES = 3500

# Sentence boundary characters used to split long text into aligned chunks.
# Includes the Devanagari danda ('।') used by Hindi/Nepali/etc.
_SENTENCE_BOUNDARIES = ".!?\u0964"

# Gemini-TTS prebuilt voice names, e.g. "Kore", "Leda".
_DEFAULT_VOICE = "Kore"


class GeminiTTS(BaseTTS):
    """Google Gemini-TTS provider using the google-genai SDK.

    Uses the ``gemini-3.1-flash-tts-preview`` model via the Gemini API. The
    voice is selected via *voice_name* (a Gemini-TTS prebuilt voice such as
    "Kore" or "Leda") and defaults to the ``GEMINI_TTS_VOICE`` env var (or
    "Kore").
    """

    content_type = "audio/mpeg"

    def __init__(self):
        api_key = (
            os.environ.get("GEMINI_TTS_API_KEY")
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_TTS_API_KEY")
            or os.environ.get("GOOGLE_TTS", "")
        )
        self.default_voice = os.environ.get("GEMINI_TTS_VOICE", _DEFAULT_VOICE)
        self.model_name = _MODEL_NAME
        self._client = genai.Client(api_key=api_key or None) if api_key else genai.Client()
        self._tts_cache = TTSCache("gemini")

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

    def _synthesize_chunk(self, text: str, voice_name: str) -> bytes:
        response = self._client.models.generate_content(
            model=self.model_name,
            contents=text,
            config=genai.types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=genai.types.SpeechConfig(
                    voice_config=genai.types.VoiceConfig(
                        prebuilt_voice_config=genai.types.PrebuiltVoiceConfig(
                            voice_name=voice_name
                        )
                    )
                ),
            ),
        )

        audio = None
        if response.candidates:
            for part in response.candidates[0].content.parts:
                if part.inline_data:
                    audio = part.inline_data.data
                    break
        if not audio:
            raise RuntimeError("Gemini-TTS response contained no audio content")

        return base64.b64decode(audio)

    def synthesize(self, text: str, language_code: str, voice_name: str = None, voice_tier: str = None) -> bytes:
        text = (text or "").strip()
        if not text:
            return b""

        voice = voice_name or self.default_voice

        cache_key = self._tts_cache.build_key(
            text, language_code, voice, voice_tier
        )
        cached = self._tts_cache.get(cache_key)
        if cached is not None:
            logger.debug(
                "Gemini-TTS cache hit: voice=%s", voice,
            )
            return cached

        chunks = self._split_chunks(text)
        logger.debug(
            "Gemini-TTS synthesize: voice=%s chunks=%d bytes=%d",
            voice, len(chunks), len(text.encode("utf-8")),
        )

        parts = []
        worker_count = min(len(chunks), 8)
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [
                executor.submit(self._synthesize_chunk, chunk, voice)
                for chunk in chunks
            ]
            for future in futures:
                try:
                    parts.append(future.result())
                except Exception as exc:
                    raise RuntimeError(
                        f"Gemini-TTS chunk synthesis failed: {exc}"
                    ) from exc

        audio = b"".join(parts)
        self._tts_cache.set(cache_key, audio)
        return audio

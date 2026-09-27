import base64
import logging
import os
from concurrent.futures import ThreadPoolExecutor

import requests

from providers.tts import BaseTTS
from providers.tts_cache import TTSCache

logger = logging.getLogger(__name__)

_TTS_ENDPOINT = "https://texttospeech.googleapis.com/v1/text:synthesize"

# Google Cloud TTS hard limit is 5000 bytes per request of input text. We use
# a conservative margin well below that. This matters a lot for non-Latin
# scripts (e.g. Hindi/Devanagari) which run ~2.5-3 bytes per character.
_MAX_CHUNK_BYTES = 4500

# Sentence boundary characters used to split long text into aligned chunks.
# Includes the Devanagari danda ('।') used by Hindi/Nepali/etc.
_SENTENCE_BOUNDARIES = ".!?\u0964"


_VALID_VOICE_TIERS = frozenset({"Neural2", "Studio", "Wavenet", "Standard"})

# Base language codes where Google Cloud TTS uses a different locale
# code than the BCP-47 code this app receives from the client. Applied
# once at the top of synthesize() so every downstream use (the API
# payload's languageCode, the constructed voice name, and the cache
# key) is consistent and always a real Google locale.
_GOOGLE_LOCALE_ALIASES = {
    "zh": "cmn-CN",   # Mandarin — Google has no "zh-*" voices, only cmn-CN/cmn-TW
    "ar": "ar-XA",    # Modern Standard Arabic — Google has no "ar-SA" voice
    "bn": "bn-IN",    # Google only ships Bengali (India), no "bn-BD"
}


def _resolve_google_locale(language_code: str) -> str:
    """Map an incoming BCP-47 code to the locale Google Cloud TTS
    actually serves, when they differ. Falls back to the original code
    unchanged for every language not in the alias map."""
    base = (language_code or "").split("-")[0].strip().lower()
    return _GOOGLE_LOCALE_ALIASES.get(base, language_code)


class GoogleNeural2TTS(BaseTTS):
    """Google Cloud Text-to-Speech provider using the REST API directly.

    Builds the voice name as ``f"{language_code}-{tier}-{variant}"`` where
    *tier* defaults to the ``GOOGLE_TTS_VOICE_TIER`` env var (or "Neural2")
    and *variant* defaults to ``GOOGLE_TTS_VOICE_VARIANT`` (or "A").  An
    explicit *voice_name* may be supplied to override the generated name
    entirely.
    """

    content_type = "audio/mpeg"

    # Base language codes this provider can voice. Curated to the enabled
    # market set (English + the 10 low-English-proficiency markets);
    # Google Cloud TTS genuinely voices far more — extend as markets enable.
    SUPPORTED_LANGUAGES = frozenset(
        {"en", "ja", "th", "ar", "zh", "id", "vi", "ko", "bn", "tr", "pt"}
    )

    def __init__(self):
        self.api_key = (
            os.environ.get("GOOGLE_TTS_API_KEY") or os.environ.get("GOOGLE_TTS", "")
        )
        tier = os.environ.get("GOOGLE_TTS_VOICE_TIER", "Neural2")
        if tier not in _VALID_VOICE_TIERS:
            logger.warning(
                "Invalid GOOGLE_TTS_VOICE_TIER=%r – falling back to 'Neural2'. "
                "Valid tiers: %s", tier, ", ".join(sorted(_VALID_VOICE_TIERS)),
            )
            tier = "Neural2"
        self.default_tier = tier
        self.default_variant = os.environ.get("GOOGLE_TTS_VOICE_VARIANT", "A")
        self.endpoint = _TTS_ENDPOINT
        self._tts_cache = TTSCache("google")

    def _ensure_api_key(self) -> str:
        if not self.api_key:
            raise RuntimeError(
                "Google Text-to-Speech API key is not configured. Set "
                "GOOGLE_TTS_API_KEY (or GOOGLE_TTS) in the environment."
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

            # Extend the chunk until just under the byte limit, remembering
            # the furthest sentence boundary that still fits so we can split
            # on a sentence end when possible.
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

            # Prefer the furthest sentence boundary; otherwise hard-split at
            # the furthest byte-safe character.
            end = last_boundary if last_boundary is not None else i
            chunks.append(text[start:end])
            start = end

        return chunks

    def _synthesize_chunk(self, text: str, language_code: str, voice_name: str) -> bytes:
        api_key = self._ensure_api_key()
        payload = {
            "input": {"text": text},
            "voice": {"languageCode": language_code, "name": voice_name},
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
                f"Google TTS request failed: {exc}"
            ) from exc

        if resp.status_code != 200:
            raise RuntimeError(
                f"Google TTS returned HTTP {resp.status_code}: {resp.text}"
            )

        data = resp.json()
        audio_b64 = data.get("audioContent")
        if not audio_b64:
            raise RuntimeError("Google TTS response contained no audio content")

        return base64.b64decode(audio_b64)

    def synthesize(self, text: str, language_code: str, voice_name: str = None, voice_tier: str = None) -> bytes:
        text = (text or "").strip()
        if not text:
            return b""

        # Google serves Mandarin, Arabic and Bengali under locale codes that
        # differ from the BCP-47 code the client sent. Resolved once, up front,
        # so the cache key, the constructed voice name and the API payload all
        # agree on a locale that actually exists in Google's catalog. Routing
        # (SUPPORTED_LANGUAGES) still matches the original base codes.
        language_code = _resolve_google_locale(language_code)

        if not voice_name:
            tier = voice_tier if (voice_tier and voice_tier in _VALID_VOICE_TIERS) else self.default_tier
            if voice_tier and voice_tier not in _VALID_VOICE_TIERS:
                logger.warning(
                    "Invalid voice_tier=%r requested – falling back to default '%s'.",
                    voice_tier, self.default_tier,
                )
            voice_name = f"{language_code}-{tier}-{self.default_variant}"

        # Repeated text/voice combos skip the API entirely and return the
        # cached audio (keyed on the resolved voice name, language, and tier).
        cache_key = self._tts_cache.build_key(
            text, language_code, voice_name, voice_tier
        )
        cached = self._tts_cache.get(cache_key)
        if cached is not None:
            logger.debug(
                "Google TTS cache hit: language=%s voice=%s",
                language_code, voice_name,
            )
            return cached

        chunks = self._split_chunks(text)
        logger.debug(
            "Google TTS synthesize: language=%s voice=%s chunks=%d bytes=%d",
            language_code, voice_name, len(chunks), len(text.encode("utf-8")),
        )

        # Synthesize all chunks in parallel (network-bound API calls), then
        # rejoin in the original order so the output is byte-identical to the
        # sequential path. If any single chunk fails we surface a clear error
        # rather than returning a silently-truncated/partial audio file.
        parts = []
        worker_count = min(len(chunks), 8)
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [
                executor.submit(self._synthesize_chunk, chunk, language_code, voice_name)
                for chunk in chunks
            ]
            for future in futures:
                try:
                    parts.append(future.result())
                except Exception as exc:
                    raise RuntimeError(
                        f"Google TTS chunk synthesis failed: {exc}"
                    ) from exc

        audio = b"".join(parts)
        self._tts_cache.set(cache_key, audio)
        return audio

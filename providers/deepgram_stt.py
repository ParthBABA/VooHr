import logging
import os

import requests

from providers.stt import BaseSTT

_STT_ENDPOINT = "https://api.deepgram.com/v1/listen"
_DEFAULT_MODEL = "nova-2"

logger = logging.getLogger(__name__)


class DeepgramSTT(BaseSTT):
    """Speech-to-text using Deepgram's prerecorded REST API.

    Reuses DEEPGRAM_API_KEY (already used for Deepgram TTS), so one key covers
    both speech and narration. The model defaults to ``nova-2``, Deepgram's
    general-purpose prerecorded model, and can be overridden via the
    ``DEEPGRAM_STT_MODEL`` env var.

    The ``language`` arg selects both the Deepgram ``language`` param and the
    model, because not every language (notably ``hi-Latn`` for Hinglish) is
    supported on every model generation. The ``multi`` ("auto-detect") mode is
    supported but is *not* the default, since Deepgram documents accuracy
    trade-offs for it (especially around Hindi). Unknown/missing languages
    silently fall back to ``_DEFAULT_LANGUAGE_KEY`` rather than hard-failing.
    """

    # language_code -> (deepgram_language_param, deepgram_model)
    _LANGUAGE_MODEL_MAP = {
        "en":        ("en",      "nova-3"),
        "en-in":     ("en-IN",   "nova-3"),
        "hi":        ("hi",      "nova-3"),      # Hindi, Devanagari script
        "hinglish":  ("hi-Latn", "nova-2"),      # Hindi-English, Roman script
        "es":        ("es",      "nova-3"),
        "fr":        ("fr",      "nova-3"),
        "de":        ("de",      "nova-3"),
        "pt":        ("pt",      "nova-3"),
        "ru":        ("ru",      "nova-3"),
        "ja":        ("ja",      "nova-3"),
        "ko":        ("ko",      "nova-3"),
        "zh":        ("zh",      "nova-3"),
        "nl":        ("nl",      "nova-3"),
        "it":        ("it",      "nova-3"),
        "auto":      ("multi",   "nova-3"),      # auto-detect / code-switch, best-effort
    }
    _DEFAULT_LANGUAGE_KEY = "en"

    def __init__(self):
        self.api_key = (
            os.environ.get("DEEPGRAM_API_KEY") or os.environ.get("DEEPGRAM", "")
        )
        self.model = os.environ.get("DEEPGRAM_STT_MODEL", _DEFAULT_MODEL)

    def transcribe(
        self,
        audio_bytes: bytes,
        content_type: str = "audio/webm",
        language: str = None,
    ) -> str:
        if not self.api_key:
            return "[STT not configured — set DEEPGRAM_API_KEY in .env]"

        lang_key = (language or self._DEFAULT_LANGUAGE_KEY).lower()
        if lang_key not in self._LANGUAGE_MODEL_MAP and language is not None:
            logger.warning(
                "Unknown STT language %r — falling back to %r",
                language,
                self._DEFAULT_LANGUAGE_KEY,
            )
        dg_language, dg_model = self._LANGUAGE_MODEL_MAP.get(
            lang_key, self._LANGUAGE_MODEL_MAP[self._DEFAULT_LANGUAGE_KEY]
        )

        headers = {
            "Authorization": f"Token {self.api_key}",
            "Content-Type": content_type,
        }
        params = {
            # DEEPGRAM_STT_MODEL, when explicitly set, overrides the per-language
            # default so model choice can be tuned manually without a code change.
            "model": os.environ.get("DEEPGRAM_STT_MODEL") or dg_model,
            "language": dg_language,
            "smart_format": "true",
            "punctuate": "true",
        }
        try:
            resp = requests.post(
                _STT_ENDPOINT,
                headers=headers,
                params=params,
                data=audio_bytes,
                timeout=60,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Deepgram STT request failed: {exc}") from exc

        if resp.status_code != 200:
            raise RuntimeError(
                f"Deepgram STT returned HTTP {resp.status_code}: {resp.text}"
            )

        data = resp.json()
        alternatives = data["results"]["channels"][0]["alternatives"]
        return (alternatives[0].get("transcript") or "").strip()

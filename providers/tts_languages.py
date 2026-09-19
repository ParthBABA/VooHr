"""Shared TTS language-support helpers.

Each provider class declares ``SUPPORTED_LANGUAGES`` — the exact base language
codes (ISO-639-1, the part before ``-``) it can actually voice. The
language-aware selector ``providers.get_tts_provider_for()`` uses those sets to
route a ``(text, language_code)`` request to a provider that supports the
language instead of silently synthesizing every language with a provider's
default English voice (which is what garbles non-English / non-Latin text).

Why this exists:
* Deepgram Aura-2 can voice exactly 7 languages; anything else forced through
  its default English model produces mispronounced audio.
* Google Cloud TTS is far broader, but no provider is universal, so the
  selector must check support explicitly rather than assume.
* When no configured provider can voice a language, ``UnsupportedTTSLanguageError``
  is raised so callers return a real client-facing error instead of wrong audio.
"""


class UnsupportedTTSLanguageError(ValueError):
    """Raised when no configured TTS provider can voice a language.

    Carries the original BCP-47 code so callers can echo it back to the
    client (e.g. ``{"error": "unsupported_tts_language", "language": ...}``).
    """

    def __init__(self, language_code: str = None):
        self.language_code = language_code
        super().__init__(
            f"No configured TTS provider supports language: {language_code!r}"
        )


# Base-code aliases normalise providers that advertise the same language under
# a different code.
_BASE_ALIASES = {}


def base_language(language_code: str) -> str:
    """Normalise a BCP-47 code to its canonical base language code.

    ``"th-TH"`` -> ``"th"``, ``"zh-CN"`` -> ``"zh"``. Empty/whitespace input
    -> ``""``.
    """
    base = (language_code or "").split("-")[0].strip().lower()
    return _BASE_ALIASES.get(base, base)


def language_supported(language_code: str, supported_codes) -> bool:
    """True when *language_code*'s base language is in *supported_codes*."""
    return bool(base_language(language_code) in supported_codes)
from flask import current_app


def get_stt_provider():
    name = current_app.config.get("STT_PROVIDER", "openai")
    if name == "openai":
        from providers.openai_stt import OpenAIWhisperSTT
        return OpenAIWhisperSTT()
    if name == "deepgram":
        from providers.deepgram_stt import DeepgramSTT
        return DeepgramSTT()
    raise ValueError(f"Unknown STT provider: {name}")


def get_vision_provider():
    name = current_app.config.get("VISION_PROVIDER", "openai")
    if name == "openai":
        from providers.vision_ocr import OpenAIVisionOCR
        return OpenAIVisionOCR()
    raise ValueError(f"Unknown vision provider: {name}")


def get_llm_provider():
    name = current_app.config.get("LLM_PROVIDER", "deepseek")
    if name == "deepseek":
        from providers.llm import DeepSeekLLM
        return DeepSeekLLM()
    if name == "openai":
        from providers.llm import OpenAILLM
        return OpenAILLM()
    raise ValueError(f"Unknown LLM provider: {name}")


def _tts_provider_class(name):
    if name == "google":
        from providers.google_tts import GoogleNeural2TTS
        return GoogleNeural2TTS
    if name == "gemini":
        from providers.gemini_tts import GeminiTTS
        return GeminiTTS
    if name == "deepgram":
        from providers.deepgram_tts import DeepgramTTS
        return DeepgramTTS
    raise ValueError(f"Unknown TTS provider: {name}")


def get_tts_provider():
    name = current_app.config.get("TTS_PROVIDER", "google")
    return _tts_provider_class(name)()


# Provider names consulted when the configured/default provider cannot voice
# the requested language. Google has the broadest coverage of the enabled
# market set, so it is tried first.
_TTS_LANGUAGE_FALLBACK_ORDER = ("google", "deepgram")


def get_tts_provider_for(language_code: str = None):
    """Return a TTS provider able to voice *language_code*.

    The configured provider (``TTS_PROVIDER``) is used whenever its
    ``SUPPORTED_LANGUAGES`` includes the request's base language. Otherwise we
    route to the first provider in ``_TTS_LANGUAGE_FALLBACK_ORDER`` that DOES
    support it — the core anti-garbe: a language is never silently forced
    through a provider's default English voice.

    Raises:
        UnsupportedTTSLanguageError: when neither Google nor Deepgram can
            voice the language. Callers must surface a real error to the
            client instead of playing mispronounced English audio.
    """
    from providers.tts_languages import UnsupportedTTSLanguageError, base_language

    name = current_app.config.get("TTS_PROVIDER", "google")

    # De-duplicated candidate order: configured provider first, then the
    # fallback chain (Google-first when the configured provider isn't Google).
    seen = set()
    candidates = []
    for candidate in (name,) + _TTS_LANGUAGE_FALLBACK_ORDER:
        if candidate and candidate not in seen:
            seen.add(candidate)
            candidates.append(candidate)

    # A missing/empty language means the call site has nothing to route on:
    # fall back to the configured provider (same behaviour as get_tts_provider).
    base = base_language(language_code)
    if not base:
        return _tts_provider_class(candidates[0])()

    for candidate in candidates:
        cls = _tts_provider_class(candidate)
        if base in getattr(cls, "SUPPORTED_LANGUAGES", frozenset()):
            return cls()

    raise UnsupportedTTSLanguageError(language_code)


def get_storage_provider():
    name = current_app.config.get("STORAGE_PROVIDER", "local")
    if name == "local":
        from providers.storage import LocalStorage
        return LocalStorage()
    raise ValueError(f"Unknown storage provider: {name}")

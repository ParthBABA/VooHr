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


def get_tts_provider():
    name = current_app.config.get("TTS_PROVIDER", "google")
    if name == "google":
        from providers.google_tts import GoogleNeural2TTS
        return GoogleNeural2TTS()
    if name == "gemini":
        from providers.gemini_tts import GeminiTTS
        return GeminiTTS()
    if name == "deepgram":
        from providers.deepgram_tts import DeepgramTTS
        return DeepgramTTS()
    raise ValueError(f"Unknown TTS provider: {name}")


def get_storage_provider():
    name = current_app.config.get("STORAGE_PROVIDER", "local")
    if name == "local":
        from providers.storage import LocalStorage
        return LocalStorage()
    raise ValueError(f"Unknown storage provider: {name}")

from flask import current_app


def get_stt_provider():
    name = current_app.config.get("STT_PROVIDER", "deepseek")
    if name == "deepseek":
        from providers.deepseek_stt import DeepSeekSTT
        return DeepSeekSTT()
    raise ValueError(f"Unknown STT provider: {name}")


def get_llm_provider():
    name = current_app.config.get("LLM_PROVIDER", "openai")
    if name == "openai":
        from providers.llm import OpenAILLM
        return OpenAILLM()
    raise ValueError(f"Unknown LLM provider: {name}")


def get_storage_provider():
    name = current_app.config.get("STORAGE_PROVIDER", "local")
    if name == "local":
        from providers.storage import LocalStorage
        return LocalStorage()
    raise ValueError(f"Unknown storage provider: {name}")

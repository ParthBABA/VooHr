from abc import ABC, abstractmethod


class BaseSTT(ABC):
    @abstractmethod
    def transcribe(
        self,
        audio_bytes: bytes,
        content_type: str = "audio/webm",
        language: str = None,
    ) -> str:
        ...

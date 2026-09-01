from abc import ABC, abstractmethod


class BaseTTS(ABC):
    @abstractmethod
    def synthesize(self, text: str, language_code: str, voice_name: str = None) -> bytes:
        """Synthesize the given text into raw MP3 audio bytes.

        Args:
            text: The text to speak.
            language_code: BCP-47 language code, e.g. "en-US", "hi-IN".
            voice_name: Optional explicit Google Cloud TTS voice name override.

        Returns:
            Raw MP3 audio bytes.
        """
        ...

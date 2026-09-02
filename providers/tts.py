from abc import ABC, abstractmethod


class BaseTTS(ABC):
    @abstractmethod
    def synthesize(self, text: str, language_code: str, voice_name: str = None, voice_tier: str = None) -> bytes:
        """Synthesize the given text into raw MP3 audio bytes.

        Args:
            text: The text to speak.
            language_code: BCP-47 language code, e.g. "en-US", "hi-IN".
            voice_name: Optional explicit Google Cloud TTS voice name override.
            voice_tier: Optional voice tier override (e.g. "Neural2", "Studio",
                "Wavenet", "Standard").  Used when *voice_name* is not provided.

        Returns:
            Raw MP3 audio bytes.
        """
        ...

    def synthesize_stream(self, text: str, language_code: str, voice_name: str = None, voice_tier: str = None):
        """Synthesize the given text, yielding raw audio chunks incrementally.

        Streaming-capable providers may override this to return audio in
        small chunks (e.g. linear16 PCM) as soon as each chunk is ready,
        allowing the client to begin playback before the full response
        arrives. Providers that only support whole-response synthesis may
        keep the default implementation, which yields the single complete
        result from :meth:`synthesize`.

        Args:
            text: The text to speak.
            language_code: BCP-47 language code, e.g. "en-US", "hi-IN".
            voice_name: Optional explicit voice name override.
            voice_tier: Optional voice tier override.

        Yields:
            bytes: Raw audio chunks to be played incrementally.
        """
        yield self.synthesize(text, language_code, voice_name=voice_name, voice_tier=voice_tier)

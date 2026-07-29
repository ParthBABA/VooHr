import io
import os

from providers.stt import BaseSTT


class OpenAIWhisperSTT(BaseSTT):
    """Speech-to-text using OpenAI's Whisper API.

    Reuses OPENAI_API_KEY (already used for LLM analysis).
    """

    def __init__(self):
        self.api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_KEY", "")
        self.model = os.environ.get("OPENAI_STT_MODEL", "whisper-1")

    def transcribe(self, audio_bytes: bytes, content_type: str = "audio/webm") -> str:
        if not self.api_key:
            return "[STT not configured — set OPENAI_API_KEY in .env]"

        from openai import OpenAI

        client = OpenAI(api_key=self.api_key)

        # Whisper needs a filename with a real extension to infer the format.
        ext = "webm"
        if "wav" in content_type:
            ext = "wav"
        elif "mp3" in content_type or "mpeg" in content_type:
            ext = "mp3"
        elif "ogg" in content_type:
            ext = "ogg"
        elif "m4a" in content_type or "mp4" in content_type:
            ext = "m4a"

        audio_file = io.BytesIO(audio_bytes)
        audio_file.name = f"recording.{ext}"

        result = client.audio.transcriptions.create(
            model=self.model,
            file=audio_file,
            response_format="json",
        )
        return (result.text or "").strip()

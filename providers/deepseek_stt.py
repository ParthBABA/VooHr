import os

import requests

from providers.stt import BaseSTT


class DeepSeekSTT(BaseSTT):
    def __init__(self):
        self.api_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("DEEPSSEK_API", "")
        self.api_url = os.environ.get(
            "DEEPSEEK_STT_URL",
            "https://api.deepseek.com/v1/audio/transcriptions",
        )

    def transcribe(self, audio_bytes: bytes, content_type: str = "audio/webm") -> str:
        if not self.api_key:
            return "[STT not configured — set DEEPSEEK_API_KEY in .env]"

        files = {"file": ("audio.webm", audio_bytes, content_type)}
        headers = {"Authorization": f"Bearer {self.api_key}"}
        data = {"model": "whisper-1", "response_format": "json"}

        resp = requests.post(self.api_url, headers=headers, files=files, data=data, timeout=120)
        resp.raise_for_status()
        result = resp.json()
        return result.get("text", "")

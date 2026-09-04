import os

import requests

from providers.stt import BaseSTT

_STT_ENDPOINT = "https://api.deepgram.com/v1/listen"
_DEFAULT_MODEL = "nova-2"


class DeepgramSTT(BaseSTT):
    """Speech-to-text using Deepgram's prerecorded REST API.

    Reuses DEEPGRAM_API_KEY (already used for Deepgram TTS), so one key covers
    both speech and narration. The model defaults to ``nova-2``, Deepgram's
    general-purpose prerecorded model, and can be overridden via the
    ``DEEPGRAM_STT_MODEL`` env var.
    """

    def __init__(self):
        self.api_key = (
            os.environ.get("DEEPGRAM_API_KEY") or os.environ.get("DEEPGRAM", "")
        )
        self.model = os.environ.get("DEEPGRAM_STT_MODEL", _DEFAULT_MODEL)

    def transcribe(self, audio_bytes: bytes, content_type: str = "audio/webm") -> str:
        if not self.api_key:
            return "[STT not configured — set DEEPGRAM_API_KEY in .env]"

        headers = {
            "Authorization": f"Token {self.api_key}",
            "Content-Type": content_type,
        }
        params = {
            "model": self.model,
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

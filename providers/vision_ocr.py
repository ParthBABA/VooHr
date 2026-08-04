import base64
import os
from abc import ABC, abstractmethod


class BaseVisionOCR(ABC):
    @abstractmethod
    def extract_text(self, image_bytes: bytes, content_type: str = "image/png") -> str:
        ...


class OpenAIVisionOCR(BaseVisionOCR):
    """OCR using OpenAI's vision-capable chat completions.

    Reuses OPENAI_API_KEY (already used for Whisper transcription and LLM
    analysis) and sends the image as a base64 data URL, exactly like the
    STT provider reuses the shared key for audio.
    """

    SYSTEM_PROMPT = (
        "Transcribe all readable text from this image verbatim, preserving line breaks. "
        "Return only the transcribed text, nothing else."
    )

    def __init__(self):
        self.api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_KEY", "")
        self.model = os.environ.get("OPENAI_VISION_MODEL", "gpt-4o-mini")

    def extract_text(self, image_bytes: bytes, content_type: str = "image/png") -> str:
        if not self.api_key:
            return "[OCR not configured — set OPENAI_API_KEY in .env]"

        from openai import OpenAI

        client = OpenAI(api_key=self.api_key)

        encoded = base64.b64encode(image_bytes).decode("ascii")
        data_url = f"data:{content_type};base64,{encoded}"

        resp = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url},
                        }
                    ],
                },
            ],
            temperature=0.0,
            max_tokens=4096,
        )

        content = resp.choices[0].message.content
        return (content or "").strip()

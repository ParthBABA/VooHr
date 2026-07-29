import os
from abc import ABC, abstractmethod


class BaseLLM(ABC):
    @abstractmethod
    def analyze(self, transcript: str) -> dict:
        ...


class OpenAILLM(BaseLLM):
    def __init__(self):
        self.api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_KEY", "")
        self.model = os.environ.get("OPENAI_ANALYSIS_MODEL", "gpt-4o")

    def analyze(self, transcript: str) -> dict:
        from openai import OpenAI

        client = OpenAI(api_key=self.api_key)

        system_prompt = """You are an HR analysis assistant. Analyze the following transcript of an HR-employee conversation.

Return a JSON object with these fields:
- summary: A 2-3 sentence summary of the conversation
- psychology: { "notes": "psychological observations", "sentiment": "positive|neutral|anxious|frustrated|engaged|disengaged", "sentiment_score": 0.0-1.0 }
- follow_ups: [ { "item": "action description", "priority": "high|medium|low" } ]
- action_items: [ { "item": "action description", "assigned_to": "manager|employee|hr" } ]
- risks: { "burnout_index": 0-100, "attrition_risk_pct": 0-100, "risk_factors": ["list of risk factors"] }

Return ONLY valid JSON, no markdown formatting."""

        resp = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": transcript},
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
        )

        import json

        try:
            return json.loads(resp.choices[0].message.content)
        except (json.JSONDecodeError, AttributeError, IndexError):
            return {
                "summary": "Analysis failed — could not parse AI response.",
                "psychology": {"notes": "", "sentiment": "unknown", "sentiment_score": 0.0},
                "follow_ups": [],
                "action_items": [],
                "risks": {"burnout_index": 0, "attrition_risk_pct": 0, "risk_factors": []},
            }

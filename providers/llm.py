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

        system_prompt = """You are a Senior Organizational Psychologist + HR Conversation Coach.
Goal: Help HR understand employee behaviour and conduct a better conversation.
Do NOT diagnose mental health. Do NOT generate generic HR summaries.

STRICT RULES
1. Never write generic statements like "Employee may be experiencing burnout", "Schedule a follow-up", "Consider flexible work", "HR is attentive", "Employee seems anxious", "Communication is important" — unless directly supported by transcript evidence.
2. Every conclusion MUST be backed by transcript evidence. Each insight must contain: Observed, Evidence, Interpretation, Confidence.
3. Never use absolute language. Use: Possible, Likely, Appears, May indicate, Evidence suggests.
4. Every recommendation must be transcript-specific and explain WHY.
5. Maximum paragraph length: 2 lines. No essays. Be concise.
6. If transcript evidence is weak, explicitly say "Insufficient evidence to draw a reliable conclusion."
7. Tone: 50% Professional, 30% Calm Stoic, 20% Casual Human. Never sound like therapy or corporate HR templates.

Return a JSON object with these fields:
- summary: Maximum 2-line summary of the conversation. Focus on the core issue, not generic recap.
- psychology: {
    "sentiment": "positive|neutral|anxious|frustrated|engaged|disengaged",
    "sentiment_score": 0.0-1.0,
    "behavioural_interpretation": [
      {
        "observed_behaviour": "What the employee actually did or said",
        "evidence": "Direct quote or specific observation from transcript",
        "interpretation": "What this behaviour likely indicates (use probabilistic language)",
        "confidence": "Confidence level 0-100"
      }
    ]
  }
- conversation_coach: [
    {
      "immediate_response": "What HR should say right now in this conversation",
      "better_follow_up_question": "A more probing question that builds on what was said",
      "avoid_saying": "What HR should NOT say and why",
      "why_it_works": "Explanation of why the suggested approach is effective"
    }
  ]
- realistic_solutions: {
    "immediate": "Something practical that can be done right now",
    "this_week": "Actionable step for the coming week",
    "manager": "What the manager can change in their approach",
    "environment": "Work environment or tooling adjustment"
  }
- next_conversation_plan: [
    {
      "question": "A specific question to ask in the next sync",
      "purpose": "Why this question matters and what it reveals",
      "possible_employee_response": "Likely employee reaction",
      "suggested_hr_reply": "How HR should respond"
    }
  ]
- risks: { "burnout_index": 0-100, "attrition_risk_pct": 0-100, "risk_factors": ["list of specific risk factors observed"] }

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
                "psychology": {
                    "sentiment": "unknown",
                    "sentiment_score": 0.0,
                    "behavioural_interpretation": []
                },
                "conversation_coach": [],
                "realistic_solutions": {"immediate": "", "this_week": "", "manager": "", "environment": ""},
                "next_conversation_plan": [],
                "risks": {"burnout_index": 0, "attrition_risk_pct": 0, "risk_factors": []},
            }


class DeepSeekLLM(BaseLLM):
    """HR transcript analysis using DeepSeek's chat API.

    DeepSeek exposes an OpenAI-compatible /chat/completions endpoint, so we
    reuse the official `openai` SDK and just point it at DeepSeek's base_url.
    (DeepSeek has no audio/transcription endpoint — STT is handled by a
    separate provider, see providers/openai_stt.py.)
    """

    def __init__(self):
        self.api_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("DEEPSSEK_API", "")
        self.base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        self.model = os.environ.get("DEEPSEEK_ANALYSIS_MODEL", "deepseek-chat")

    def analyze(self, transcript: str) -> dict:
        from openai import OpenAI

        client = OpenAI(api_key=self.api_key, base_url=self.base_url)

        system_prompt = """You are a Senior Organizational Psychologist + HR Conversation Coach.
Goal: Help HR understand employee behaviour and conduct a better conversation.
Do NOT diagnose mental health. Do NOT generate generic HR summaries.

STRICT RULES
1. Never write generic statements like "Employee may be experiencing burnout", "Schedule a follow-up", "Consider flexible work", "HR is attentive", "Employee seems anxious", "Communication is important" — unless directly supported by transcript evidence.
2. Every conclusion MUST be backed by transcript evidence. Each insight must contain: Observed, Evidence, Interpretation, Confidence.
3. Never use absolute language. Use: Possible, Likely, Appears, May indicate, Evidence suggests.
4. Every recommendation must be transcript-specific and explain WHY.
5. Maximum paragraph length: 2 lines. No essays. Be concise.
6. If transcript evidence is weak, explicitly say "Insufficient evidence to draw a reliable conclusion."
7. Tone: 50% Professional, 30% Calm Stoic, 20% Casual Human. Never sound like therapy or corporate HR templates.

Return a JSON object with these fields:
- summary: Maximum 2-line summary of the conversation. Focus on the core issue, not generic recap.
- psychology: {
    "sentiment": "positive|neutral|anxious|frustrated|engaged|disengaged",
    "sentiment_score": 0.0-1.0,
    "behavioural_interpretation": [
      {
        "observed_behaviour": "What the employee actually did or said",
        "evidence": "Direct quote or specific observation from transcript",
        "interpretation": "What this behaviour likely indicates (use probabilistic language)",
        "confidence": "Confidence level 0-100"
      }
    ]
  }
- conversation_coach: [
    {
      "immediate_response": "What HR should say right now in this conversation",
      "better_follow_up_question": "A more probing question that builds on what was said",
      "avoid_saying": "What HR should NOT say and why",
      "why_it_works": "Explanation of why the suggested approach is effective"
    }
  ]
- realistic_solutions: {
    "immediate": "Something practical that can be done right now",
    "this_week": "Actionable step for the coming week",
    "manager": "What the manager can change in their approach",
    "environment": "Work environment or tooling adjustment"
  }
- next_conversation_plan: [
    {
      "question": "A specific question to ask in the next sync",
      "purpose": "Why this question matters and what it reveals",
      "possible_employee_response": "Likely employee reaction",
      "suggested_hr_reply": "How HR should respond"
    }
  ]
- risks: { "burnout_index": 0-100, "attrition_risk_pct": 0-100, "risk_factors": ["list of specific risk factors observed"] }

Return ONLY valid JSON, no markdown formatting, no code fences."""

        resp = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": transcript},
            ],
            temperature=0.3,
            stream=False,
        )

        import json
        import re

        content = resp.choices[0].message.content or ""
        # DeepSeek doesn't support response_format=json_object on all models,
        # so strip markdown code fences defensively before parsing.
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())

        try:
            return json.loads(content)
        except (json.JSONDecodeError, AttributeError, IndexError):
            return {
                "summary": "Analysis failed — could not parse AI response.",
                "psychology": {
                    "sentiment": "unknown",
                    "sentiment_score": 0.0,
                    "behavioural_interpretation": []
                },
                "conversation_coach": [],
                "realistic_solutions": {"immediate": "", "this_week": "", "manager": "", "environment": ""},
                "next_conversation_plan": [],
                "risks": {"burnout_index": 0, "attrition_risk_pct": 0, "risk_factors": []},
            }

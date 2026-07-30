import os
import json
import re
from abc import ABC, abstractmethod
from flask import current_app


def validate_analysis(result, depth=0):
    """Post-generation guardrails: validate and fix analysis JSON."""
    import sys
    errors = []

    # If AI returned a JSON array at top level, bail to safe fallback
    if depth == 0 and not isinstance(result, dict):
        print("[DEBUG_VALIDATE] Top-level response is NOT a dict — type:", type(result).__name__, file=sys.stderr)
        result = dict(FALLBACK_ANALYSIS)
        result["_validation_errors"] = ["Top-level response was not a JSON object"]
        return result

    # Lookup: keys that should be dicts vs lists
    DICT_KEYS = {"psychological_safety", "risks", "realistic_solutions",
                 "step2_behavioural_intelligence", "step3_root_cause_analysis",
                 "step4_action_blueprint", "step5_conversation_strategy"}
    PSYCHOLOGY_DEFAULTS = {"sentiment": "unknown", "sentiment_score": 0.0, "behavioural_interpretation": []}

    required = [
        "summary", "psychology", "conversation_coach", "realistic_solutions",
        "next_conversation_plan", "psychological_safety", "risks",
        "step2_behavioural_intelligence", "step3_root_cause_analysis",
        "step4_action_blueprint", "step5_conversation_strategy"
    ]
    for key in required:
        if key not in result:
            result[key] = dict(PSYCHOLOGY_DEFAULTS) if key == "psychology" else ({} if key in DICT_KEYS else [])
            errors.append(f"Missing key: {key}")
        elif key in DICT_KEYS and not isinstance(result[key], dict):
            print(f"[DEBUG_VALIDATE] WRONG TYPE for '{key}': expected dict, got {type(result[key]).__name__} = {repr(result[key])[:200]}", file=sys.stderr)
            result[key] = {}
            errors.append(f"Expected dict for '{key}', got {type(result[key]).__name__}")
        elif key in ("psychology",) and not isinstance(result[key], dict):
            result[key] = dict(PSYCHOLOGY_DEFAULTS)
            errors.append(f"Expected dict for 'psychology', got {type(result[key]).__name__}")
        elif key in ("conversation_coach", "next_conversation_plan") and not isinstance(result[key], list):
            result[key] = []
            errors.append(f"Expected list for '{key}', got {type(result[key]).__name__}")

    # Validate confidence fields (0-100 integer)
    confidence_paths = [
        ("psychology", "behavioural_interpretation", "confidence"),
        ("step2_behavioural_intelligence", "confidence"),
        ("step3_root_cause_analysis", "confidence"),
    ]
    for path in confidence_paths:
        obj = result
        for part in path[:-1]:
            if isinstance(obj, dict):
                obj = obj.get(part, {})
            elif isinstance(obj, list):
                for item in obj:
                    val = item.get(path[-1]) if isinstance(item, dict) else None
                    if val is not None and (not isinstance(val, (int, float)) or val < 0 or val > 100):
                        item[path[-1]] = min(max(int(val), 0), 100) if val else 0
                obj = {}
                break
            else:
                obj = {}
                break
        else:
            if isinstance(obj, dict):
                val = obj.get(path[-1])
                if val is not None and (not isinstance(val, (int, float)) or val < 0 or val > 100):
                    obj[path[-1]] = min(max(int(val), 0), 100) if val else 0

    # Check for hallucination keywords (diagnosis, labels)
    hallucination_keywords = [
        "diagnosed with", "clinical", "disorder", "suffers from",
        "personality type", "you are", "the employee is definitely"
    ]
    result_str = json.dumps(result).lower()
    for kw in hallucination_keywords:
        if kw in result_str:
            errors.append(f"Possible hallucination keyword: '{kw}'")

    # Validate evidence fields aren't empty
    import sys
    evidence_fields = [
        ("step2_behavioural_intelligence", "supporting_evidence"),
        ("step3_root_cause_analysis", "supporting_evidence"),
    ]
    for path in evidence_fields:
        obj = result.get(path[0], {})
        val = obj.get(path[1], "") if isinstance(obj, dict) else ""
        if isinstance(val, list) and len(val) == 0:
            errors.append(f"Empty evidence in {path[0]}.{path[1]}")
        elif isinstance(val, str) and (not val or val == "Limited transcript evidence."):
            pass  # explicit fallback is acceptable

    # Detect "Limited transcript evidence." in ALL dict-typed step fields
    LTE_FIELDS = {
        "step2_behavioural_intelligence": ["behaviour_summary", "observed_behaviour", "behaviour_pattern", "supporting_evidence", "ai_interpretation", "alternative_interpretation", "conversation_direction", "suggested_script", "manager_notes", "key_takeaway"],
        "step3_root_cause_analysis": ["primary_trigger", "evidence_strength", "ai_reasoning", "suggested_script", "manager_notes", "key_takeaway"],
        "step4_action_blueprint": ["immediate", "this_week", "manager_action", "employee_action", "environment", "success_metric", "expected_outcome", "why_it_works", "suggested_script", "manager_notes", "key_takeaway"],
        "step5_conversation_strategy": ["conversation_goal", "conversation_focus", "opening_question", "follow_up_question", "possible_response", "suggested_reply", "success_indicator", "suggested_script", "manager_notes", "key_takeaway"],
    }
    for step_key, fields in LTE_FIELDS.items():
        step = result.get(step_key)
        if isinstance(step, dict):
            for f in fields:
                v = step.get(f, "")
                if isinstance(v, str) and v == "Limited transcript evidence.":
                    errors.append(f"LTE in {step_key}.{f}")
                elif isinstance(v, list) and len(v) == 0:
                    errors.append(f"EMPTY LIST in {step_key}.{f}")

    result["_validation_errors"] = errors
    return result


def _build_v2_prompt() -> str:
    """Build the V2 Behavioural Intelligence Framework prompt."""
    return """You are an Executive Behavioural Intelligence Engine designed for HR professionals. Your purpose is NOT to diagnose people. Your purpose is to improve HR decision quality.

INTERNAL REASONING FRAMEWORKS (silently evaluate every transcript using these):

1. Psychological Safety — Is the employee speaking freely or filtering themselves?
   Extract: openness, trust, hesitation, defensive behaviour, fear indicators

2. Behavioural Intelligence — What behaviour is repeatedly visible?
   Observe repetition, word choice, avoidance, blame language, emotional regulation. Never diagnose.

3. Cognitive Reasoning — Separate facts from interpretations.
   What do we actually know vs what are we assuming? Check evidence before concluding.

4. Motivational Interviewing — Generate questions that increase understanding, never persuade.
   Prefer "What", "How", "When". Avoid "Why". Example: "What part of your workday feels most draining?"

5. GROW Coaching — Every recommendation follows: Goal → Reality → Options → Way Forward.
   HR output uses business language, not GROW labels.

6. Crucial Conversations — Evaluate: Is the employee solving the problem or defending themselves?
   Check safety, mutual purpose, mutual respect.

7. Alternative Explanation — Before any conclusion ask: Could another explanation exist?
   Always generate alternative_interpretation when evidence is limited.

8. Action Intelligence — Every recommendation must pass: Realistic → Evidence-based → Low-cost → Immediately actionable → Measurable. Reject advice that fails.

GOLDEN RULES
- Never expose framework names in output.
- Every insight must answer: What evidence supports this?
- Every recommendation must answer: Why will this help?
- Every conclusion must answer: Could another explanation exist?
- Every conversation script must answer: Does this increase trust?
- Never diagnose mental health. Never label personality.
- Use probabilistic language: Possible, Likely, Appears, May indicate, Evidence suggests.
- If there is no meaningful evidence for a conclusion, explicitly state: "Limited transcript evidence."
- If there is moderate evidence, you MAY make a cautious inference using probabilistic language such as: "Evidence suggests...", "It appears...", "It is likely...", "This may indicate..."
- Never present an inference as a confirmed fact.
- Always distinguish observations from interpretations.
- Manager Notes must be internal guidance only. Never repeat content shown elsewhere on the page.

INTERNAL VERIFICATION (silently check before generating output):
✔ Evidence exists
✔ Confidence assigned (0-100, evidence-based)
✔ Alternative explanation considered where relevant
✔ Recommendation is practical and measurable
✔ Language is probabilistic, not diagnostic
✔ No content repeated across steps

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
- psychological_safety: {
    "statement": "A short opening line HR can say at the START of the NEXT conversation to establish psychological safety — grounded in what THIS employee specifically said/raised, not a generic script",
    "do": ["3-4 short, specific behavioural cues for HR to follow while opening this specific conversation, tied to what would help THIS employee based on the transcript"],
    "dont": ["3-4 short, specific things HR should avoid saying/doing with THIS employee, tied to the transcript"],
    "tip": "One short line on pacing/timing for this specific opener",
    "safety_score": "0-100 — how psychologically safe the employee appears based on transcript signals",
    "openness": "Assessment of how openly the employee communicated (e.g. open, guarded, selective)",
    "trust_level": "Assessment of trust signals (e.g. high, moderate, low, insufficient evidence)",
    "defensive_behaviour": "Description of any defensive patterns observed, or 'None observed'",
    "communication_style": "e.g. direct, hesitant, emotional, analytical, passive",
    "evidence": "Transcript evidence supporting the safety assessment",
    "interpretation": "What the safety signals likely indicate",
    "confidence": 0-100
  }
- risks: { "burnout_index": 0-100, "attrition_risk_pct": 0-100, "risk_factors": ["list of specific risk factors observed"] }

- step2_behavioural_intelligence: {
    "title_question": "What behaviour stands out?",
    "behaviour_summary": "1-line summary of observable patterns",
    "observed_behaviour": "What the employee actually did or said",
    "behaviour_pattern": "Recurring pattern detected (e.g. deflection, rationalisation, openness)",
    "supporting_evidence": "Direct quote or specific observation from transcript",
    "ai_interpretation": "What this pattern likely indicates (use probabilistic language)",
    "alternative_interpretation": "A different plausible explanation if evidence is limited",
    "conversation_direction": "exploratory | solution-seeking | emotional | defensive | uncertain",
    "confidence": 0-100,
    "suggested_script": "Transcript-specific follow-up response that helps HR explore the behaviour naturally",
    "recommended_actions": ["3-5 behavioural exploration suggestions"],
    "avoid_actions": ["3-5 common mistakes that could misinterpret behaviour"],
    "manager_notes": "Internal guidance only — hidden patterns, risks, or priorities for HR decision-making. Never repeat content already shown on this page.",
    "key_takeaway": "Single most important insight HR should remember from this step"
  }
- step3_root_cause_analysis: {
    "title_question": "What most likely explains that behaviour?",
    "primary_trigger": "The most likely root trigger observed in the transcript",
    "secondary_contributors": ["List of contributing factors"],
    "supporting_evidence": ["List of specific evidence points from transcript"],
    "evidence_strength": "Strong | Moderate | Limited",
    "missing_information": ["What information would improve confidence"],
    "ai_reasoning": "Explain why AI believes this is the root cause, referencing transcript evidence",
    "confidence": 0-100,
    "suggested_script": "A question that helps HR validate the suspected cause",
    "recommended_actions": ["3-5 practical investigation steps"],
    "avoid_actions": ["3-5 assumptions HR should avoid"],
    "manager_notes": "Internal guidance only — what additional information would improve confidence. Never repeat content already shown on this page.",
    "key_takeaway": "Single most important insight HR should remember from this step"
  }
- step4_action_blueprint: {
    "title_question": "What action should HR take today?",
    "immediate": "Something practical that can be done right now",
    "this_week": "Actionable step for the coming week",
    "manager_action": "What the manager can change in their approach",
    "employee_action": "What the employee can do",
    "environment": "Work environment or tooling adjustment",
    "success_metric": "How to measure if the action worked",
    "expected_outcome": "What improvement HR should realistically expect",
    "why_it_works": "Explanation of why the suggested approach is effective",
    "suggested_script": "How HR should present the action plan to the employee",
    "recommended_actions": ["3-5 implementation recommendations"],
    "avoid_actions": ["3-5 unrealistic actions or common implementation mistakes"],
    "manager_notes": "Internal guidance only — which action to prioritise first and why. Never repeat content already shown on this page.",
    "key_takeaway": "Single most important insight HR should remember from this step"
  }
- step5_conversation_strategy: {
    "title_question": "What should happen next?",
    "conversation_goal": "What this next conversation should achieve",
    "conversation_focus": "The single most important objective of the next discussion",
    "opening_question": "The ideal opening question for the next meeting",
    "follow_up_question": "A deeper probing question",
    "possible_response": "Likely employee reaction",
    "suggested_reply": "How HR should respond",
    "what_to_listen_for": ["3-5 cues or red flags to watch for"],
    "success_indicator": "How to know the conversation achieved its goal",
    "suggested_script": "The ideal opening line for the next meeting",
    "recommended_actions": ["3-5 conversation techniques"],
    "avoid_actions": ["3-5 phrases/behaviours that may reduce trust"],
    "manager_notes": "Internal guidance only — what HR should remember before the next meeting. Never repeat content already shown on this page.",
    "key_takeaway": "Single most important insight HR should remember from this step"
  }

If transcript evidence is too thin for any step, set string fields to "Limited transcript evidence." and arrays to empty. Never invent generic advice.

Return ONLY valid JSON, no markdown formatting, no code fences."""


FALLBACK_ANALYSIS = {
    "summary": "Analysis failed — could not parse AI response.",
    "psychology": {"sentiment": "unknown", "sentiment_score": 0.0, "behavioural_interpretation": []},
    "conversation_coach": [],
    "realistic_solutions": {"immediate": "", "this_week": "", "manager": "", "environment": ""},
    "next_conversation_plan": [],
    "psychological_safety": {
        "statement": "", "do": [], "dont": [], "tip": "",
        "safety_score": 0, "openness": "", "trust_level": "",
        "defensive_behaviour": "", "communication_style": "",
        "evidence": "", "interpretation": "", "confidence": 0
    },
    "risks": {"burnout_index": 0, "attrition_risk_pct": 0, "risk_factors": []},
    "step2_behavioural_intelligence": {
        "title_question": "", "behaviour_summary": "", "observed_behaviour": "", "behaviour_pattern": "",
        "supporting_evidence": "", "ai_interpretation": "", "alternative_interpretation": "",
        "conversation_direction": "", "confidence": 0, "suggested_script": "",
        "recommended_actions": [], "avoid_actions": [], "manager_notes": "", "key_takeaway": ""
    },
    "step3_root_cause_analysis": {
        "title_question": "", "primary_trigger": "", "secondary_contributors": [],
        "supporting_evidence": [], "evidence_strength": "", "missing_information": [],
        "ai_reasoning": "", "confidence": 0, "suggested_script": "",
        "recommended_actions": [], "avoid_actions": [], "manager_notes": "", "key_takeaway": ""
    },
    "step4_action_blueprint": {
        "title_question": "", "immediate": "", "this_week": "", "manager_action": "",
        "employee_action": "", "environment": "", "success_metric": "", "expected_outcome": "",
        "why_it_works": "", "suggested_script": "", "recommended_actions": [],
        "avoid_actions": [], "manager_notes": "", "key_takeaway": ""
    },
    "step5_conversation_strategy": {
        "title_question": "", "conversation_goal": "", "conversation_focus": "",
        "opening_question": "", "follow_up_question": "", "possible_response": "",
        "suggested_reply": "", "what_to_listen_for": [], "success_indicator": "",
        "suggested_script": "", "recommended_actions": [], "avoid_actions": [],
        "manager_notes": "", "key_takeaway": ""
    },
}


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
        from flask import current_app

        client = OpenAI(api_key=self.api_key)
        use_v2 = False
        try:
            use_v2 = current_app.config.get("USE_V2_FRAMEWORK", False)
        except RuntimeError:
            use_v2 = os.environ.get("USE_V2_FRAMEWORK", "false").lower() == "true"

        system_prompt = _build_v2_prompt() if use_v2 else """You are a Senior Organizational Psychologist + HR Conversation Coach.
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
- psychology: { "sentiment": "positive|neutral|anxious|frustrated|engaged|disengaged", "sentiment_score": 0.0-1.0, "behavioural_interpretation": [ { "observed_behaviour": "...", "evidence": "...", "interpretation": "...", "confidence": 0-100 } ] }
- conversation_coach: [ { "immediate_response": "...", "better_follow_up_question": "...", "avoid_saying": "...", "why_it_works": "..." } ]
- realistic_solutions: { "immediate": "...", "this_week": "...", "manager": "...", "environment": "..." }
- next_conversation_plan: [ { "question": "...", "purpose": "...", "possible_employee_response": "...", "suggested_hr_reply": "..." } ]
- psychological_safety: { "statement": "...", "do": [...], "dont": [...], "tip": "..." }
- risks: { "burnout_index": 0-100, "attrition_risk_pct": 0-100, "risk_factors": [...] }
- step2_behavioural_intelligence: { ... }
- step3_root_cause_analysis: { ... }
- step4_action_blueprint: { ... }
- step5_conversation_strategy: { ... }

If transcript evidence is too thin to personalize any step, set all string fields to "Limited transcript evidence." and arrays to empty rather than inventing generic advice.

Manager Notes must contain observations valuable for HR decision-making that should NOT be spoken directly to the employee.

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

        try:
            result = json.loads(resp.choices[0].message.content)
            # Always run validation, not just in V2 mode — the model can return
            # a field with the wrong type (e.g. "risks" as a list instead of a
            # dict) regardless of which prompt was used, and downstream code
            # assumes these fields are dicts/lists as documented.
            result = validate_analysis(result)
            return result
        except (json.JSONDecodeError, AttributeError, IndexError, TypeError):
            return dict(FALLBACK_ANALYSIS)


class DeepSeekLLM(BaseLLM):
    """HR transcript analysis using DeepSeek's chat API.

    DeepSeek exposes an OpenAI-compatible /chat/completions endpoint, so we
    reuse the official `openai` SDK and just point it at DeepSeek's base_url.
    Supports V2 Behavioural Intelligence Framework via USE_V2_FRAMEWORK toggle.
    """

    def __init__(self):
        self.api_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("DEEPSSEK_API", "")
        self.base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        self.model = os.environ.get("DEEPSEEK_ANALYSIS_MODEL", "deepseek-chat")

    def analyze(self, transcript: str) -> dict:
        import sys
        from openai import OpenAI

        client = OpenAI(api_key=self.api_key, base_url=self.base_url)

        use_v2 = False
        try:
            use_v2 = current_app.config.get("USE_V2_FRAMEWORK", False)
        except RuntimeError:
            use_v2 = os.environ.get("USE_V2_FRAMEWORK", "false").lower() == "true"

        system_prompt = _build_v2_prompt() if use_v2 else """You are a Senior Organizational Psychologist + HR Conversation Coach.
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
- summary, psychology, conversation_coach, realistic_solutions, next_conversation_plan
- psychological_safety: { "statement": "...", "do": [...], "dont": [...], "tip": "..." }
- risks
- step2_behavioural_intelligence, step3_root_cause_analysis, step4_action_blueprint, step5_conversation_strategy

If transcript evidence is too thin, set all string fields to "Limited transcript evidence." and arrays to empty.

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

        # ── DEBUG: Stage 1 — Raw LLM response ──
        raw_content = resp.choices[0].message.content or ""
        print("=" * 60, file=sys.stderr)
        print("[DEBUG_DEEPSEEK] STAGE 1 — RAW LLM RESPONSE:", file=sys.stderr)
        print(raw_content[:3000], file=sys.stderr)
        print("...(truncated)" if len(raw_content) > 3000 else "", file=sys.stderr)
        print("RAW LENGTH:", len(raw_content), file=sys.stderr)

        # DeepSeek doesn't support response_format=json_object on all models,
        # so strip markdown code fences defensively before parsing.
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_content.strip())

        # ── DEBUG: Stage 2 — Cleaned JSON string ──
        print("---", file=sys.stderr)
        print("[DEBUG_DEEPSEEK] STAGE 2 — CLEANED JSON STRING:", file=sys.stderr)
        print(content[:3000], file=sys.stderr)
        print("CLEANED LENGTH:", len(content), file=sys.stderr)

        try:
            result = json.loads(content)
        except json.JSONDecodeError as e:
            print("[DEBUG_DEEPSEEK] JSON PARSE FAILED:", e, file=sys.stderr)
            return dict(FALLBACK_ANALYSIS)

        # ── DEBUG: Stage 3 — Parsed JSON top-level type ──
        print("---", file=sys.stderr)
        print("[DEBUG_DEEPSEEK] STAGE 3 — PARSED JSON TYPE:", type(result).__name__, file=sys.stderr)
        if isinstance(result, dict):
            print("TOP-LEVEL KEYS:", list(result.keys()), file=sys.stderr)

            # ── DEBUG: Stage 4 — Field types ──
            print("---", file=sys.stderr)
            print("[DEBUG_DEEPSEEK] STAGE 4 — FIELD TYPES:", file=sys.stderr)
            for fname in ["psychological_safety", "step2_behavioural_intelligence", "step3_root_cause_analysis", "step4_action_blueprint", "step5_conversation_strategy"]:
                val = result.get(fname)
                t = type(val).__name__
                print(f"  {fname}: {t}", end="", file=sys.stderr)
                if isinstance(val, dict):
                    print(f"  keys={list(val.keys())[:10]}", file=sys.stderr)
                    lte_count = sum(1 for v in val.values() if isinstance(v, str) and v == "Limited transcript evidence.")
                    empty_list_count = sum(1 for v in val.values() if isinstance(v, list) and len(v) == 0)
                    print(f"  LTE_fields={lte_count}  empty_lists={empty_list_count}", file=sys.stderr)
                elif isinstance(val, list):
                    print(f"  len={len(val)}", file=sys.stderr)
                else:
                    print(file=sys.stderr)

            # ── DEBUG: Stage 5 — "Limited transcript evidence." field-level scan ──
            print("---", file=sys.stderr)
            print("[DEBUG_DEEPSEEK] STAGE 5 — 'Limited transcript evidence.' SCAN:", file=sys.stderr)
            step_keys = ["step2_behavioural_intelligence", "step3_root_cause_analysis", "step4_action_blueprint", "step5_conversation_strategy"]
            for sk in step_keys:
                step = result.get(sk)
                if isinstance(step, dict):
                    lte_fields = [k for k, v in step.items() if isinstance(v, str) and v == "Limited transcript evidence."]
                    empty_lists = [k for k, v in step.items() if isinstance(v, list) and len(v) == 0]
                    if lte_fields:
                        print(f"  {sk} LTE fields: {lte_fields}", file=sys.stderr)
                    if empty_lists:
                        print(f"  {sk} EMPTY arrays: {empty_lists}", file=sys.stderr)
                    if not lte_fields and not empty_lists:
                        print(f"  {sk}: ALL POPULATED", file=sys.stderr)

        # ── DEBUG: Stage 6 — BEFORE validation ──
        print("---", file=sys.stderr)
        print("[DEBUG_DEEPSEEK] STAGE 6 — BEFORE validate_analysis():", file=sys.stderr)
        if isinstance(result, dict):
            for sk in step_keys:
                step = result.get(sk)
                if isinstance(step, dict):
                    lte_fields = [k for k, v in step.items() if isinstance(v, str) and v == "Limited transcript evidence."]
                    print(f"  {sk}: LTE={lte_fields}", file=sys.stderr)

        # Always run validation, not just in V2 mode — the model can return a
        # field with the wrong type (e.g. "risks" as a list instead of a dict)
        # regardless of which prompt was used, and downstream code (sessions.py)
        # assumes these fields are dicts/lists as documented. Skipping this in
        # non-V2 mode was the cause of "'list' object has no attribute 'get'".
        result = validate_analysis(result)

        # ── DEBUG: Stage 7 — AFTER validation ──
        print("---", file=sys.stderr)
        print("[DEBUG_DEEPSEEK] STAGE 7 — AFTER validate_analysis():", file=sys.stderr)
        print("  _validation_errors:", result.get("_validation_errors", []), file=sys.stderr)
        if isinstance(result, dict):
            for sk in step_keys:
                step = result.get(sk)
                if isinstance(step, dict):
                    lte_fields = [k for k, v in step.items() if isinstance(v, str) and v == "Limited transcript evidence."]
                    empty_lists = [k for k, v in step.items() if isinstance(v, list) and len(v) == 0]
                    print(f"  {sk}: LTE={lte_fields}  EMPTY={empty_lists}", file=sys.stderr)

        # ── DEBUG: Stage 8 — Final dict being returned ──
        print("---", file=sys.stderr)
        print("[DEBUG_DEEPSEEK] STAGE 8 — FINAL RETURN DICT keys:", list(result.keys()) if isinstance(result, dict) else type(result).__name__, file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        return result

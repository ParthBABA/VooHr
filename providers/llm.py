import logging
import os
import json
import re
from abc import ABC, abstractmethod
from flask import current_app

logger = logging.getLogger(__name__)


# ── Canonical field names per step section (what the frontend expects) ──
SECTION_FIELDS = {
    "step2_behavioural_intelligence": {
        "string": ["title_question", "behaviour_summary", "observed_behaviour", "behaviour_pattern",
                    "supporting_evidence", "ai_interpretation", "alternative_interpretation",
                    "conversation_direction", "suggested_script", "manager_notes", "key_takeaway"],
        "list": ["recommended_actions", "avoid_actions"],
        "numeric": ["confidence"],
    },
    "step3_root_cause_analysis": {
        "string": ["title_question", "primary_trigger", "evidence_strength", "ai_reasoning",
                    "suggested_script", "manager_notes", "key_takeaway"],
        "list": ["secondary_contributors", "supporting_evidence", "missing_information",
                  "recommended_actions", "avoid_actions"],
        "numeric": ["confidence"],
    },
    "step4_action_blueprint": {
        "string": ["title_question", "immediate", "this_week", "manager_action", "employee_action",
                    "environment", "success_metric", "expected_outcome", "why_it_works",
                    "suggested_script", "manager_notes", "key_takeaway"],
        "list": ["recommended_actions", "avoid_actions"],
        "numeric": ["confidence"],
    },
    "step5_conversation_strategy": {
        "string": ["title_question", "conversation_goal", "conversation_focus", "opening_question",
                    "follow_up_question", "possible_response", "suggested_reply", "success_indicator",
                    "suggested_script", "manager_notes", "key_takeaway"],
        "list": ["what_to_listen_for", "recommended_actions", "avoid_actions"],
        "numeric": ["confidence"],
    },
}

# ── Field name aliases the LLM might produce instead of canonical names ──
FIELD_ALIASES = {
    "behaviour_summary": ["behavior_summary", "summary"],
    "observed_behaviour": ["observed_behavior", "behaviour", "behavior", "observed"],
    "behaviour_pattern": ["behavior_pattern", "pattern"],
    "supporting_evidence": ["evidence", "supporting_evidence_"],
    "ai_interpretation": ["interpretation", "ai_analysis"],
    "alternative_interpretation": ["alternative_explanation", "alt_interpretation"],
    "conversation_direction": ["direction", "conversation_style"],
    "secondary_contributors": ["secondary_contributor", "contributing_factors", "secondary_factors"],
    "evidence_strength": ["strength", "evidence_quality"],
    "missing_information": ["missing_info", "information_gaps", "gaps"],
    "ai_reasoning": ["ai_reason", "reasoning"],
    "primary_trigger": ["trigger", "root_trigger", "primary_cause"],
    "immediate": ["immediate_action"],
    "this_week": ["this_week_action"],
    "manager_action": ["manager"],
    "employee_action": ["employee"],
    "success_metric": ["metric", "kpi"],
    "expected_outcome": ["outcome", "expected_result"],
    "why_it_works": ["rationale"],
    "conversation_goal": ["goal", "objective"],
    "conversation_focus": ["focus"],
    "opening_question": ["opening_question", "opening"],
    "follow_up_question": ["follow_up", "followup_question", "followup"],
    "possible_response": ["possible_employee_response", "employee_response", "expected_response"],
    "suggested_reply": ["suggested_hr_reply", "hr_reply"],
    "what_to_listen_for": ["listen_for", "things_to_listen_for", "cues"],
    "success_indicator": ["indicator"],
    "title_question": ["title_question", "title"],
    "key_takeaway": ["takeaway", "key_insight", "main_takeaway"],
    "suggested_script": ["script", "conversation_script", "suggested_response"],
    "recommended_actions": ["recommendations", "actions", "recommended"],
    "avoid_actions": ["actions_to_avoid", "avoid", "avoid_list"],
    "manager_notes": ["notes_for_manager", "internal_notes", "notes"],
}

# ── Build reverse alias → canonical mapping ──
ALIAS_TO_CANONICAL = {}
for canonical, aliases in FIELD_ALIASES.items():
    for alias in aliases:
        ALIAS_TO_CANONICAL[alias] = canonical


def _has_meaningful_content(val):
    """Check if a value has real content (not empty, not LTE)."""
    if isinstance(val, str):
        return bool(val.strip()) and val != "Limited transcript evidence."
    if isinstance(val, list):
        return len(val) > 0
    if isinstance(val, (int, float)):
        return True
    return False


def _normalize_step_fields(result, step_key):
    """Map aliased field names to canonical names within a step section."""
    step = result.get(step_key)
    if not isinstance(step, dict):
        return
    canonical = SECTION_FIELDS.get(step_key)
    if not canonical:
        return

    all_canonical = set(canonical["string"] + canonical["list"] + canonical["numeric"])
    renamed = {}
    for key in list(step.keys()):
        if key in all_canonical:
            continue
        if key in ALIAS_TO_CANONICAL:
            target = ALIAS_TO_CANONICAL[key]
            if target not in step or not _has_meaningful_content(step.get(target)):
                step[target] = step[key]
            renamed[key] = target
    for old_key in renamed:
        del step[old_key]
    if renamed:
        logger.debug("normalize %s: renamed %s", step_key, renamed)


CONFIDENCE_MAP = {
    "low": 25,
    "medium": 50,
    "moderate": 50,
    "high": 85,
}

def _parse_confidence(val):
    """Safely convert any confidence value to an integer 0-100."""
    if val is None:
        return 0
    if isinstance(val, (int, float)):
        return min(max(int(round(val)), 0), 100)
    if isinstance(val, str):
        stripped = val.strip()
        try:
            num = float(stripped)
            return min(max(int(round(num)), 0), 100)
        except (ValueError, TypeError):
            pass
        return CONFIDENCE_MAP.get(stripped.lower(), 0)
    return 0


def _coerce_step_section(key, val):
    """Coerce a wrongly-typed step section into a dict, preserving content."""
    if isinstance(val, str) and val.strip():
        result = {"raw_analysis": val}
        return result
    if isinstance(val, list):
        if len(val) > 0 and isinstance(val[0], dict):
            return val[0]
        return {"raw_items": list(val)}
    return None


def validate_analysis(result, depth=0):
    """Post-generation guardrails: validate and fix analysis JSON."""
    errors = []

    # If AI returned a JSON array at top level, bail to safe fallback
    if depth == 0 and not isinstance(result, dict):
        logger.warning("Top-level response is not a dict: %s", type(result).__name__)
        result = dict(FALLBACK_ANALYSIS)
        result["_validation_errors"] = ["Top-level response was not a JSON object"]
        return result

    # ── Step 0: Normalize field names before any validation ──
    for step_key in SECTION_FIELDS:
        _normalize_step_fields(result, step_key)

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
            val = result[key]
            coerced = _coerce_step_section(key, val)
            if coerced is not None:
                result[key] = coerced
                orig_type = type(val).__name__
                new_type = "dict"
                reason = "list_extracted_first_element" if (isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict)) else \
                         "string_wrapped_to_raw_analysis" if isinstance(val, str) else \
                         "list_wrapped_to_raw_items"
                logger.debug("type_coercion section=%s orig=%s dest=dict reason=%s", key, orig_type, reason)
                errors.append(f"Coerced {orig_type}→dict for '{key}' (reason={reason})")
            else:
                logger.debug("type_coercion section=%s orig=%s dest=dict reason=unexpected_type_replaced_empty", key, type(val).__name__)
                result[key] = {}
                errors.append(f"Expected dict for '{key}', got {type(val).__name__}")
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
                    if isinstance(item, dict) and path[-1] in item:
                        item[path[-1]] = _parse_confidence(item.get(path[-1]))
                obj = {}
                break
            else:
                obj = {}
                break
        else:
            if isinstance(obj, dict) and path[-1] in obj:
                obj[path[-1]] = _parse_confidence(obj.get(path[-1]))

    # Check for hallucination keywords (diagnosis, labels)
    hallucination_keywords = [
        "diagnosed with", "clinical", "disorder", "suffers from",
        "personality type", "you are", "the employee is definitely"
    ]
    result_str = json.dumps(result).lower()
    for kw in hallucination_keywords:
        if kw in result_str:
            errors.append(f"Possible hallucination keyword: '{kw}'")

    # ── Section-level content check for steps 2-5 ──
    # Only trigger fallback when a section has ZERO meaningful content across
    # all its string/list fields. Never replace individual non-empty fields.
    for step_key, spec in SECTION_FIELDS.items():
        step = result.get(step_key)
        if not isinstance(step, dict):
            continue

        string_fields = spec["string"]
        list_fields = spec["list"]

        # Check if this section has ANY meaningful content
        has_content = False
        for f in string_fields:
            v = step.get(f, "")
            if isinstance(v, str) and v.strip() and v != "Limited transcript evidence.":
                has_content = True
                break
        if not has_content:
            for f in list_fields:
                v = step.get(f, [])
                if isinstance(v, list) and len(v) > 0:
                    has_content = True
                    break

        if has_content:
            continue

        # ── Section has NO meaningful content → log fallback details ──
        missing_info = []
        for f in string_fields:
            v = step.get(f, "")
            if not isinstance(v, str) or not v.strip() or v == "Limited transcript evidence.":
                missing_info.append(f"{f}='{v}'" if v else f"{f}=<empty>")
        for f in list_fields:
            v = step.get(f, [])
            if not isinstance(v, list) or len(v) == 0:
                missing_info.append(f"{f}=[]")

        logger.debug("fallback section=%s reason=no_meaningful_content missing=%s", step_key, missing_info)

    # Check for individual LTE/empty fields (informational, not replacing)
    for step_key, spec in SECTION_FIELDS.items():
        step = result.get(step_key)
        if not isinstance(step, dict):
            continue
        for f in spec["string"]:
            v = step.get(f, "")
            if isinstance(v, str) and v == "Limited transcript evidence.":
                errors.append(f"LTE in {step_key}.{f}")
        for f in spec["list"]:
            v = step.get(f, [])
            if isinstance(v, list) and len(v) == 0:
                errors.append(f"EMPTY LIST in {step_key}.{f}")

    result["_validation_errors"] = errors
    return result


DRIFT_STRING_FIELDS = [
    "headline",
    "summary",
    "root_cause_thread",
    "tone_shift",
    "trigger_point",
    "trajectory",
    "reversibility",
    "suggested_opening_script",
]


def validate_drift_explanation(result):
    """Post-generation guardrails: validate and fix drift-explanation JSON."""
    errors = []

    if not isinstance(result, dict):
        logger.warning("Drift explanation is not a dict: %s", type(result).__name__)
        result = dict(FALLBACK_DRIFT_EXPLANATION)
        result["_validation_errors"] = ["Drift explanation was not a JSON object"]
        return result

    is_genuine = result.get("is_genuine_pattern", False)
    if not isinstance(is_genuine, bool):
        if isinstance(is_genuine, str):
            result["is_genuine_pattern"] = is_genuine.strip().lower() in ("true", "yes", "1")
            reason = f"string_parsed ('{is_genuine}')"
        else:
            result["is_genuine_pattern"] = bool(is_genuine)
            reason = f"{type(is_genuine).__name__}_coerced"
        logger.debug("type_coercion field=is_genuine_pattern orig=%s dest=bool reason=%s", type(is_genuine).__name__, reason)
        errors.append("Coerced non-bool for 'is_genuine_pattern'")

    for field in DRIFT_STRING_FIELDS:
        val = result.get(field, "")
        if not isinstance(val, str):
            result[field] = str(val) if val is not None else ""
            logger.debug("type_coercion field=%s orig=%s dest=str reason=coerced", field, type(val).__name__)
            errors.append(f"Coerced {type(val).__name__}→str for '{field}'")

    ev = result.get("escalation_evidence", [])
    if not isinstance(ev, list):
        result["escalation_evidence"] = [str(ev)] if ev is not None else []
        logger.debug("type_coercion field=escalation_evidence orig=%s dest=list reason=coerced", type(ev).__name__)
        errors.append(f"Coerced {type(ev).__name__}→list for 'escalation_evidence'")
    else:
        result["escalation_evidence"] = [str(x) for x in ev]

    result["confidence"] = _parse_confidence(result.get("confidence"))

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
- Apply "Limited transcript evidence." PER FIELD, never per step: only mark an individual field this way if the transcript has literally zero relevant signal for that specific field. A weak or missing field must NOT cause you to blank out the rest of that step — populate every other field that has at least some supporting signal.
- Short, casual, or code-mixed (e.g. Hindi-English) transcripts still count as evidence. Do not treat brevity, informal tone, or mixed language alone as "insufficient evidence" — judge only whether the content is relevant to the field.
- Default to a cautious inference using probabilistic language ("Evidence suggests...", "It appears...", "It is likely...", "This may indicate...") whenever there is ANY relevant signal, however small. Reserve "Limited transcript evidence." for fields with genuinely no relevant signal at all.
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

Judge every field independently. Only set a field to "Limited transcript evidence." (string fields) or an empty array (list fields) if that specific field truly has no relevant signal in the transcript. Do not blank out a whole step just because one field in it is weak — a typical transcript should leave most fields populated with a cautious, evidence-based answer. Never invent generic advice not grounded in the transcript.

Return ONLY valid JSON, no markdown formatting, no code fences."""


def _build_drift_prompt(sessions) -> str:
    """Format multiple sync sessions as labeled transcript blocks.

    Each block shows the sync number, date, and risk scores followed by the
    FULL transcript so the model can cross-reference actual conversation
    content rather than just the trend lines.
    """
    blocks = []
    for idx, sess in enumerate(sessions, start=1):
        date = sess.get("date") or "?"
        attrition = sess.get("attrition_risk_pct")
        burnout = sess.get("burnout_index")
        transcript = sess.get("transcript") or ""
        blocks.append(
            f"--- Sync {idx} ({date}) — attrition_risk_pct={attrition}, burnout_index={burnout} ---\n{transcript}"
        )
    return "\n\n".join(blocks)


def _build_drift_system_prompt() -> str:
    """Build the Risk Drift Detection system prompt."""
    return """You are a Senior Organizational Psychologist advising HR. Your task is Risk Drift Detection: determine whether an employee's rising attrition/burnout risk across MULTIPLE recent syncs reflects a genuine, compounding multi-session pattern — or a single bad day / isolated context that does not yet signal a trend.

STRICT RULES
1. CROSS-REFERENCE the full transcripts against each other. Do NOT restate the risk percentages — they only tell you where to look; the transcripts tell you why.
2. A genuine pattern must be visible in the CONTENT of at least two sessions: a recurring trigger, escalating wording, worsening self-report, growing withdrawal or resignation, repeated mention of the same stressor. Never infer a pattern from the trend line alone.
3. Actively consider the alternative: one bad day, a one-off context (project crunch, a sick week, a data artifact), or a course correction mid-window. If the transcripts support that, set is_genuine_pattern = false.
4. Never diagnose mental health. Use probabilistic language ONLY: Possible, Likely, Appears, May indicate, Evidence suggests.
5. Be concise and evidence-grounded. Every item in escalation_evidence must be traceable to a specific session's content.

Return ONLY valid JSON with EXACTLY these fields:
- is_genuine_pattern: boolean — whether the cross-session evidence supports a genuine multi-session risk drift
- headline: str — one crisp line HR can grasp in seconds
- summary: str — 2-3 sentence overview of the drift
- root_cause_thread: str — the single most likely thread connecting the sessions
- escalation_evidence: list of str — specific, session-referenced evidence points supporting the pattern
- tone_shift: str — how the employee's tone changed across sessions, or "No meaningful shift"
- trigger_point: str — which session/event appears to be the inflection point, if any
- trajectory: str — the possible/likely course if nothing changes
- reversibility: str — how reversible this appears and what would change it
- suggested_opening_script: str — one opening line for the next sync, grounded in THIS employee's own words
- confidence: int 0-100 — overall confidence in this read

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


FALLBACK_PHRASING_ANALYSIS = {
    "overall_tone_score": 0,
    "hr_phrasing_flags": [],
    "employee_signals": [],
    "positive_moments": [],
}


FALLBACK_DRIFT_EXPLANATION = {
    "is_genuine_pattern": False,
    "headline": "",
    "summary": "",
    "root_cause_thread": "",
    "escalation_evidence": [],
    "tone_shift": "",
    "trigger_point": "",
    "trajectory": "",
    "reversibility": "",
    "suggested_opening_script": "",
    "confidence": 0,
}


def _build_phrasing_prompt() -> str:
    """Build the Phrasing & Psychological-Safety Review prompt.

    Unlike _build_v2_prompt() (deep post-conversation analysis), this scans
    the transcript line-by-line for two things:
      1. HR phrasing that could reduce psychological safety/trust — with a
         verbatim quote (for UI highlighting) and a better rephrasing.
      2. Employee language signals (nervousness, hesitation, concealment,
         defensiveness) grounded in observable evidence, never a diagnosis.
    """
    return """You are a Communication & Psychological-Safety Reviewer for HR conversation transcripts. You draw on established, peer-reviewed frameworks — psychological safety (Edmondson), Nonviolent Communication (Rosenberg), active listening, and motivational interviewing — but you NEVER name these frameworks in your output. You write like a skilled human coach, not an academic.

YOUR TWO JOBS

JOB 1 — HR Phrasing Review
Read every line spoken by HR/manager. Flag lines that could reduce psychological safety, sound judgmental, invalidate the employee's experience, put them on the defensive, or ask a leading/closed question instead of an open one. For each flagged line, extract the EXACT quote (verbatim, character-for-character substring copied from the transcript — this is used to highlight the text in the UI, so it must match exactly, including punctuation and casing) and propose a warmer, safer rephrasing that says the same thing without the problem.

JOB 2 — Employee Signal Review
Read every line spoken by the employee. Identify moments where the language itself carries a signal worth HR's attention — hesitation, hedging ("I guess", "maybe it's fine"), short/deflective answers after a direct question, sudden topic changes, minimizing language, or conversely genuine openness. For each, extract the EXACT quote (verbatim substring, same rule as above) and name the signal using probabilistic, non-diagnostic language. Never diagnose a mental health condition or label personality. Ground every signal in a specific observable pattern in the text, not a vibe.

GOLDEN RULES
- "quote" fields MUST be an exact, verbatim, contiguous substring of the transcript as given — do not paraphrase, trim mid-word, translate, or fix typos. If you cannot find an exact substring, do not include that flag.
- Keep each quote short and specific: one sentence or clause (roughly 3–25 words), not a whole paragraph.
- Only flag genuine issues/signals with real evidence. Do not force a minimum count — a clean, healthy conversation can have very few or zero HR flags.
- Cap output at a maximum of 8 hr_phrasing_flags, 8 employee_signals, and 4 positive_moments — pick the most important ones.
- Use probabilistic language for employee_signals ("appears", "may indicate", "possible") — never "the employee is anxious" as a fact.
- Never diagnose mental health conditions or assign personality labels.
- Also capture positive_moments: HR lines that were done well (specific, empathetic, safety-building) so HR sees what to keep doing, not just what to fix.
- Short, casual, or Hindi-English code-mixed transcripts are still valid evidence — do not skip analysis just because the language is informal or mixed.

Return a JSON object with exactly these fields:
- overall_tone_score: 0-100, how psychologically safe and empathetic the HR side of this conversation felt overall
- hr_phrasing_flags: [
    {
      "quote": "exact verbatim substring of the HR line being flagged",
      "category": "judgmental_tone | invalidating | leading_question | blame_language | dismissive | interrupting_defensive | clarity",
      "severity": "low | medium | high",
      "issue": "One short sentence: what's wrong with this phrasing and why it could reduce trust",
      "better_rephrasing": "A warmer, psychologically-safer way to say the same thing",
      "why_it_works": "One short sentence explaining why the rewrite is better, grounded in evidence/communication principles, without naming any framework by name"
    }
  ]
- employee_signals: [
    {
      "quote": "exact verbatim substring of the employee line being flagged",
      "signal": "nervous_hesitant | possible_concealment | defensive_guarded | minimizing | frustrated | genuinely_open",
      "confidence": 0-100,
      "evidence_basis": "The specific observable pattern that suggests this (e.g. hedging words, short answer after a direct question, topic deflection, tone shift) — not a generic statement",
      "hr_suggestion": "One short, specific thing HR could say or do next to create more safety for this employee, grounded in what was actually said"
    }
  ]
- positive_moments: [
    { "quote": "exact verbatim substring of a well-handled HR line", "why_it_works": "One short sentence on why this built trust or safety" }
  ]

Return ONLY valid JSON, no markdown formatting, no code fences."""


def validate_phrasing_analysis(result: dict) -> dict:
    """Defensively normalize the phrasing-review LLM output so the frontend
    can always rely on the expected shape, even if the model returns a
    slightly different structure (e.g. a flag list as a single dict)."""
    if not isinstance(result, dict):
        return dict(FALLBACK_PHRASING_ANALYSIS)

    out = dict(FALLBACK_PHRASING_ANALYSIS)

    score = result.get("overall_tone_score", 0)
    try:
        out["overall_tone_score"] = max(0, min(100, int(score)))
    except (TypeError, ValueError):
        out["overall_tone_score"] = 0

    def _clean_list(key, required_fields):
        val = result.get(key)
        if isinstance(val, dict):
            val = [val]
        if not isinstance(val, list):
            return []
        cleaned = []
        for item in val:
            if not isinstance(item, dict):
                continue
            quote = item.get("quote")
            if not quote or not isinstance(quote, str) or not quote.strip():
                continue
            row = {f: item.get(f, "") for f in required_fields}
            row["quote"] = quote
            if "confidence" in row:
                try:
                    row["confidence"] = max(0, min(100, int(row["confidence"])))
                except (TypeError, ValueError):
                    row["confidence"] = 0
            cleaned.append(row)
        return cleaned[:8]

    out["hr_phrasing_flags"] = _clean_list(
        "hr_phrasing_flags",
        ["quote", "category", "severity", "issue", "better_rephrasing", "why_it_works"],
    )
    out["employee_signals"] = _clean_list(
        "employee_signals",
        ["quote", "signal", "confidence", "evidence_basis", "hr_suggestion"],
    )
    out["positive_moments"] = _clean_list("positive_moments", ["quote", "why_it_works"])[:4]

    return out


class BaseLLM(ABC):
    @abstractmethod
    def analyze(self, transcript: str) -> dict:
        ...

    @abstractmethod
    def analyze_phrasing(self, transcript: str) -> dict:
        """Line-level phrasing/psych-safety review: flags HR lines that could
        reduce trust (with a rephrasing suggestion) and employee lines that
        carry a communication signal (hesitation, concealment, openness)."""
        ...

    @abstractmethod
    def explain_drift(self, sessions: list) -> dict:
        """Explain whether risk across multiple syncs forms a genuine pattern.

        sessions: list of {date, attrition_risk_pct, burnout_index, transcript},
        oldest first, minimum 3 entries (caller's responsibility to enforce).
        Sends FULL transcripts together (not just the risk % numbers) so the
        model can cross-reference actual conversation content, not just trend lines.
        """
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
2. Every conclusion MUST be backed by transcript evidence.
3. Never use absolute language. Use: Possible, Likely, Appears, May indicate, Evidence suggests.
4. Every recommendation must be transcript-specific and explain WHY.
5. Maximum paragraph length: 2 lines. No essays. Be concise.
6. If transcript evidence is weak, explicitly say "Insufficient evidence to draw a reliable conclusion."
7. Tone: 50% Professional, 30% Calm Stoic, 20% Casual Human. Never sound like therapy or corporate HR templates.

INTERNAL VERIFICATION (silently check before generating output):
- Evidence exists
- Confidence assigned (0-100, evidence-based)
- Alternative explanation considered where relevant
- Recommendation is practical and measurable
- Language is probabilistic, not diagnostic
- No content repeated across steps

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
    "employee": "What the employee can do",
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
    "statement": "A short opening line HR can say at the START of the NEXT conversation to establish psychological safety",
    "do": ["3-4 short, specific behavioural cues for HR to follow"],
    "dont": ["3-4 short, specific things HR should avoid saying/doing"],
    "tip": "One short line on pacing/timing for this specific opener",
    "safety_score": "0-100",
    "openness": "Assessment of how openly the employee communicated",
    "trust_level": "Assessment of trust signals",
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
    "manager_notes": "Internal guidance only. Never repeat content already shown on this page.",
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
    "manager_notes": "Internal guidance only. Never repeat content already shown on this page.",
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
    "manager_notes": "Internal guidance only. Never repeat content already shown on this page.",
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
    "manager_notes": "Internal guidance only. Never repeat content already shown on this page.",
    "key_takeaway": "Single most important insight HR should remember from this step"
  }

Judge each field on its own: only set an individual string field to "Limited transcript evidence." (or a list field to empty) if that specific field has no relevant signal in the transcript. Do not blank an entire step just because one field in it is weak — most fields should stay populated with a cautious, evidence-based answer. Short or code-mixed (e.g. Hindi-English) transcripts still count as evidence; never invent generic advice not grounded in the transcript.

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

    def explain_drift(self, sessions: list) -> dict:
        """
        sessions: list of {date, attrition_risk_pct, burnout_index, transcript},
        oldest first, minimum 3 entries (caller's responsibility to enforce).
        Sends FULL transcripts together (not just the risk % numbers) so the
        model can cross-reference actual conversation content, not just trend lines.
        """
        from openai import OpenAI

        client = OpenAI(api_key=self.api_key)

        resp = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": _build_drift_system_prompt()},
                {"role": "user", "content": _build_drift_prompt(sessions)},
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
        )

        try:
            result = json.loads(resp.choices[0].message.content)
            result = validate_drift_explanation(result)
            return result
        except (json.JSONDecodeError, AttributeError, IndexError, TypeError):
            return dict(FALLBACK_DRIFT_EXPLANATION)

    def analyze_phrasing(self, transcript: str) -> dict:
        from openai import OpenAI

        client = OpenAI(api_key=self.api_key)

        resp = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": _build_phrasing_prompt()},
                {"role": "user", "content": transcript},
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
        )

        try:
            result = json.loads(resp.choices[0].message.content)
            return validate_phrasing_analysis(result)
        except (json.JSONDecodeError, AttributeError, IndexError, TypeError):
            return dict(FALLBACK_PHRASING_ANALYSIS)


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
2. Every conclusion MUST be backed by transcript evidence.
3. Never use absolute language. Use: Possible, Likely, Appears, May indicate, Evidence suggests.
4. Every recommendation must be transcript-specific and explain WHY.
5. Maximum paragraph length: 2 lines. No essays. Be concise.
6. If transcript evidence is weak, explicitly say "Insufficient evidence to draw a reliable conclusion."
7. Tone: 50% Professional, 30% Calm Stoic, 20% Casual Human. Never sound like therapy or corporate HR templates.

INTERNAL VERIFICATION (silently check before generating output):
- Evidence exists
- Confidence assigned (0-100, evidence-based)
- Alternative explanation considered where relevant
- Recommendation is practical and measurable
- Language is probabilistic, not diagnostic
- No content repeated across steps

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
    "employee": "What the employee can do",
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
    "statement": "A short opening line HR can say at the START of the NEXT conversation to establish psychological safety",
    "do": ["3-4 short, specific behavioural cues for HR to follow"],
    "dont": ["3-4 short, specific things HR should avoid saying/doing"],
    "tip": "One short line on pacing/timing for this specific opener",
    "safety_score": "0-100",
    "openness": "Assessment of how openly the employee communicated",
    "trust_level": "Assessment of trust signals",
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
    "manager_notes": "Internal guidance only. Never repeat content already shown on this page.",
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
    "manager_notes": "Internal guidance only. Never repeat content already shown on this page.",
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
    "manager_notes": "Internal guidance only. Never repeat content already shown on this page.",
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
    "manager_notes": "Internal guidance only. Never repeat content already shown on this page.",
    "key_takeaway": "Single most important insight HR should remember from this step"
  }

Judge each field on its own: only set an individual string field to "Limited transcript evidence." (or a list field to empty) if that specific field has no relevant signal in the transcript. Do not blank an entire step just because one field in it is weak — most fields should stay populated with a cautious, evidence-based answer. Short or code-mixed (e.g. Hindi-English) transcripts still count as evidence; never invent generic advice not grounded in the transcript.

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

        raw_content = resp.choices[0].message.content or ""

        # DeepSeek doesn't support response_format=json_object on all models,
        # so strip markdown code fences defensively before parsing.
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_content.strip())

        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            logger.warning("DeepSeek analyze: JSON parse failed")
            return dict(FALLBACK_ANALYSIS)

        result = validate_analysis(result)
        return result

    def explain_drift(self, sessions: list) -> dict:
        """
        sessions: list of {date, attrition_risk_pct, burnout_index, transcript},
        oldest first, minimum 3 entries (caller's responsibility to enforce).
        Sends FULL transcripts together (not just the risk % numbers) so the
        model can cross-reference actual conversation content, not just trend lines.
        """
        from openai import OpenAI

        client = OpenAI(api_key=self.api_key, base_url=self.base_url)

        resp = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": _build_drift_system_prompt()},
                {"role": "user", "content": _build_drift_prompt(sessions)},
            ],
            temperature=0.3,
            stream=False,
        )

        # DeepSeek doesn't reliably support response_format=json_object, so
        # strip markdown code fences defensively before parsing.
        raw_content = resp.choices[0].message.content or ""
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_content.strip())

        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            logger.warning("DeepSeek explain_drift: JSON parse failed")
            return dict(FALLBACK_DRIFT_EXPLANATION)

        result = validate_drift_explanation(result)
        return result

    def analyze_phrasing(self, transcript: str) -> dict:
        from openai import OpenAI

        client = OpenAI(api_key=self.api_key, base_url=self.base_url)

        resp = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": _build_phrasing_prompt()},
                {"role": "user", "content": transcript},
            ],
            temperature=0.2,
            stream=False,
        )

        raw_content = resp.choices[0].message.content or ""
        # DeepSeek doesn't reliably support response_format=json_object, so
        # strip markdown code fences defensively before parsing.
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_content.strip())

        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            logger.warning("DeepSeek analyze_phrasing: JSON parse failed")
            return dict(FALLBACK_PHRASING_ANALYSIS)

        return validate_phrasing_analysis(result)

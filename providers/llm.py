import logging
import os
import json
import re
from abc import ABC, abstractmethod
from flask import current_app
from openai import APITimeoutError

logger = logging.getLogger(__name__)


class LLMTimeoutError(Exception):
    """Raised when an LLM request exceeds its configured timeout.

    Distinct from JSON-parse failures: this means the upstream provider was
    slow or hung for a single request. Callers can treat it as retryable —
    the request's content is fine, the provider was just too slow.
    """


def _llm_timeout_seconds() -> float:
    """Per-request timeout for chat.completions.create (seconds).

    Default 20s keeps each call well under a typical ~30s platform worker
    timeout (so the platform can't kill the worker and serve its own raw
    HTML error page) while still being generous for a longer prompt such as
    for a long transcript. Env-configurable so it can be tuned against real
    call-duration data without a code change.
    """
    try:
        return float(os.environ.get("LLM_REQUEST_TIMEOUT_SECONDS", "20"))
    except (TypeError, ValueError):
        return 20.0


def _llm_drift_timeout_seconds() -> float:
    """Tighter per-request budget for the best-effort drift check.

    explain_drift runs inside the SAME /analyze request as the main analysis
    (which is already allowed up to LLM_REQUEST_TIMEOUT_SECONDS), so the two
    calls share the platform worker's budget. A separate, shorter cap keeps
    analyze + drift inside that budget instead of letting the pair exceed it
    (which makes the platform kill the worker and serve raw HTML). Drift is
    best-effort and never allowed to fail /analyze, so bounding it tighter is
    always safe.
    """
    try:
        return float(os.environ.get("LLM_DRIFT_TIMEOUT_SECONDS", "8"))
    except (TypeError, ValueError):
        return 8.0

# ── Canonical field names per step section (what the frontend expects) ──
SECTION_FIELDS = {
    "step2_behavioural_intelligence": {
        "string": ["title_question", "behaviour_summary", "observed_behaviour", "behaviour_pattern",
                    "supporting_evidence", "ai_interpretation", "alternative_interpretation",
                    "conversation_direction", "suggested_script", "manager_notes", "key_takeaway"],
        "list": ["recommended_actions", "avoid_actions", "underlying_drivers"],
        "confidence_label": ["confidence_label"],
    },
    "step3_root_cause_analysis": {
        "string": ["title_question", "primary_trigger", "evidence_strength", "ai_reasoning",
                    "suggested_script", "manager_notes", "key_takeaway"],
        "list": ["secondary_contributors", "supporting_evidence", "missing_information",
                  "recommended_actions", "avoid_actions"],
        "confidence_label": ["confidence_label"],
    },
    "step4_action_blueprint": {
        "string": ["title_question", "immediate", "this_week", "manager_action", "employee_action",
                    "environment", "success_metric", "expected_outcome", "why_it_works",
                    "suggested_script", "manager_notes", "key_takeaway"],
        "list": ["recommended_actions", "avoid_actions"],
        "confidence_label": ["confidence_label"],
    },
    "step5_conversation_strategy": {
        "string": ["title_question", "conversation_goal", "conversation_focus", "opening_question",
                    "follow_up_question", "possible_response", "suggested_reply", "success_indicator",
                    "suggested_script", "manager_notes", "key_takeaway"],
        "list": ["what_to_listen_for", "recommended_actions", "avoid_actions"],
        "confidence_label": ["confidence_label"],
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
    "confidence_label": ["confidence", "signal_strength", "confidence_level"],
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

    all_canonical = set(canonical["string"] + canonical["list"] + canonical["confidence_label"])
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

# ── Qualitative signal labels replace numeric 0-100 confidence everywhere in
# the analysis schema. See _parse_confidence_label for coercion rules. ──
CONFIDENCE_LABELS = ["Strong Signal", "Moderate Signal", "Light Signal"]
CONFIDENCE_KEYWORD = {label.split()[0].lower(): label for label in CONFIDENCE_LABELS}


def _confidence_label_from_score(score):
    """Map a numeric 0-100 confidence to the qualitative label enum."""
    try:
        score = int(round(float(score)))
    except (TypeError, ValueError):
        return "Light Signal"
    if score >= 70:
        return "Strong Signal"
    if score >= 40:
        return "Moderate Signal"
    return "Light Signal"


def _parse_confidence_label(val):
    """Coerce any confidence value to the signal-label enum, defaulting to
    'Light Signal' on anything unparseable. Handles the label itself, a bare
    keyword (e.g. 'Strong'), or a legacy numeric score (0-100)."""
    if val is None:
        return "Light Signal"
    if isinstance(val, (int, float)):
        return _confidence_label_from_score(val)
    if isinstance(val, str):
        stripped = val.strip().lower()
        for label in CONFIDENCE_LABELS:
            if label.lower() == stripped:
                return label
        if stripped in CONFIDENCE_KEYWORD:
            return CONFIDENCE_KEYWORD[stripped]
    try:
        return _confidence_label_from_score(stripped)
    except (TypeError, ValueError):
        pass
    return "Light Signal"


# ── Qualitative emotional-tone label replaces the old numeric sentiment
# score. See _parse_sentiment_label for coercion rules. ──
SENTIMENT_LABELS = ["Positive", "Reflective", "Anxious", "Strained", "Mixed"]
_SENTIMENT_LEGACY = {
    "positive": "Positive",
    "engaged": "Positive",
    "neutral": "Reflective",
    "mixed": "Mixed",
    "anxious": "Anxious",
    "frustrated": "Strained",
    "disengaged": "Strained",
}


def _parse_sentiment_label(val):
    """Coerce the emotion tone to the qualitative label enum, returning an
    empty string when nothing usable is present. Handles the label, a legacy
    keyword, or (as a last resort) a numeric 0-100 legacy sentiment score —
    never a number in the output."""
    if val is None:
        return ""
    if isinstance(val, (int, float)):
        try:
            num = float(val)
        except (TypeError, ValueError):
            return ""
        score = max(0, min(100, num * 100 if num <= 1.0 else num))
        return "Positive" if score >= 70 else ("Reflective" if score >= 40 else "Mixed")
    if isinstance(val, str):
        stripped = val.strip().lower()
        if not stripped:
            return ""
        for label in SENTIMENT_LABELS:
            if label.lower() == stripped:
                return label
        if stripped in _SENTIMENT_LEGACY:
            return _SENTIMENT_LEGACY[stripped]
        try:
            num = float(stripped.replace("%", "").strip())
            score = max(0, min(100, num * 100 if num <= 1.0 else num))
            return "Positive" if score >= 70 else ("Reflective" if score >= 40 else "Mixed")
        except (TypeError, ValueError):
            return ""
    return ""


def _parse_confidence_score(val):
    """Safely convert any numeric confidence value to an integer 0-100.

    Used only by the risk-drift schema (explain_drift), which keeps a numeric
    confidence field for the drift hero/notification pills.
    """
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
    PSYCHOLOGY_DEFAULTS = {"sentiment_label": "", "behavioural_interpretation": []}

    required = [
        "summary", "psychology", "conversation_coach", "realistic_solutions",
        "next_conversation_plan", "topics_to_avoid", "psychological_safety", "risks",
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
        elif key in ("conversation_coach", "next_conversation_plan", "topics_to_avoid") and not isinstance(result[key], list):
            result[key] = []
            errors.append(f"Expected list for '{key}', got {type(result[key]).__name__}")

    # Validate signal-strength labels (string enum, never a number). Nested
    # list-of-dicts paths (e.g. psychology.behavioural_interpretation) are
    # coerced item-by-item.
    confidence_label_paths = [
        ("psychology", "behavioural_interpretation", "confidence_label"),
        ("psychological_safety", "confidence_label"),
        ("step2_behavioural_intelligence", "confidence_label"),
        ("step3_root_cause_analysis", "confidence_label"),
        ("step5_conversation_strategy", "confidence_label"),
    ]
    for step_path in confidence_label_paths:
        obj = result
        for part in step_path[:-1]:
            if isinstance(obj, dict):
                obj = obj.get(part, {})
            else:
                obj = {}
                break
        field = step_path[-1]
        targets = obj if isinstance(obj, list) else [obj]
        for node in targets:
            if isinstance(node, dict) and (field in node or "confidence" in node):
                node[field] = _parse_confidence_label(node.get(field) or node.get("confidence"))
                if "confidence" in node:
                    del node["confidence"]

    # Normalize the emotional-tone field. The old `psychology.sentiment`
    # (numeric 0-100 or legacy keyword) and `psychology.sentiment_score` are
    # folded into the qualitative `sentiment_label` and then removed so the
    # UI never receives a numeric sentiment value.
    psych = result.get("psychology")
    if isinstance(psych, dict):
        legacy_val = (
            psych.get("sentiment_label")
            if psych.get("sentiment_label") not in (None, "", False)
            else psych.get("sentiment")
        )
        if legacy_val is not None and legacy_val != "":
            psych["sentiment_label"] = _parse_sentiment_label(legacy_val)
        else:
            psych["sentiment_label"] = ""
        for legacy_key in ("sentiment", "sentiment_score"):
            if legacy_key in psych:
                del psych[legacy_key]

    # Clean topics_to_avoid: keep only well-formed object entries.
    topics_avoid = result.get("topics_to_avoid")
    if isinstance(topics_avoid, list):
        cleaned = []
        for item in topics_avoid:
            if not isinstance(item, dict):
                continue
            cleaned.append({
                "topic": str(item.get("topic") or ""),
                "reason": str(item.get("reason") or ""),
            })
        result["topics_to_avoid"] = cleaned

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

    result["confidence"] = _parse_confidence_score(result.get("confidence"))

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
- This tool exists to help HR remember what to follow up on — it does not score, rank, or rate the employee. Never phrase output as a judgment of the employee (e.g. avoid framing like "Harshit scored 75% positive sentiment"). Frame output as guidance for HR's next action (e.g. "These are the things worth following up on next time").
- Never attach a clinical or trait label to the employee (e.g. "perfectionist tendency", "self-critical inner dialogue", "anxiety pattern"). Instead, describe the specific behaviour in plain, descriptive language anchored to what they actually said — e.g. "Harshit named a pattern of blaming himself after small mistakes" rather than "perfectionist tendency".
- Do not speculate about the employee's underlying motives, fears, or psychological needs beyond what they explicitly said. Avoid phrases like "possibly because...", "this suggests he may be...", "indicating a need for..." — these are unstated inferences, not observations. Stick to describing what was said and what pattern it forms in behavior, not why it exists internally. If a reasonable behavioral observation is useful, phrase it as a description of the pattern itself, not a theory about its psychological cause — e.g. write "Harshit shared this without being asked" rather than "this suggests he may feel isolated and needs to unload."
- Never emit a bare status word for a signal or flag field (e.g. "None observed", "N/A", "Not observed"). If nothing is flagged, write a brief natural sentence such as "Nothing flagged — the conversation felt open and low-stress."
- Every confidence_label field must use exactly one value: Strong Signal | Moderate Signal | Light Signal. Strong Signal = multiple clear, direct pieces of transcript evidence. Moderate Signal = some supporting evidence but with a plausible alternative reading. Light Signal = a single weak or indirect cue. Never output a number.
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
✔ Signal label assigned (Strong/Moderate/Light, evidence-based)
✔ Alternative explanation considered where relevant
✔ Recommendation is practical and measurable
✔ Language is probabilistic, not diagnostic
✔ No content repeated across steps

TITLE GENERATION (highest priority — do this FIRST before writing any other field):
After reading the ENTIRE conversation, write a single "title" field. This must be a 4-7 word title in title case that captures the CORE CONCLUSION or MAIN MOTIVE of the conversation — what was the employee really trying to say, what was the real issue underneath, or what was the single most important outcome. Ask yourself: "If HR could only remember ONE thing from this session, what would it be?" That is the title.
GOOD examples: "Burnout Risk From Unclear Expectations", "Employee Considering Exit Over Pay Gap", "Genuine Engagement Despite Role Misalignment", "Team Conflict Escalating Without Manager Support", "Micromanagement Driving Disengagement".
BAD examples (NEVER write these): "HR Sync Session", "Employee Conversation", "Discussed Work Concerns", "General Check-in", "Team Discussion", "Monthly Review".
The title must prove you understood the CONVERSATION DEEPLY — its real meaning, not just its surface topic.

Return a JSON object with these fields:
- title: 4-7 word title in title case — the CORE CONCLUSION of this conversation (as instructed above)
- summary: Maximum 2-line summary of the conversation. Focus on the core issue, not generic recap.
- psychology: {
    "sentiment_label": "Positive | Reflective | Anxious | Strained | Mixed",
    "behavioural_interpretation": [
      {
        "observed_behaviour": "What the employee actually did or said",
        "evidence": "Direct quote or specific observation from transcript",
        "interpretation": "What this behaviour likely indicates (use probabilistic language) — never attach a clinical or trait label; describe the specific behaviour in plain, descriptive language anchored to what they actually said; never speculate about unstated motives, fears, or needs (no 'possibly because...', no 'this suggests he may be...')",
        "confidence_label": "Strong Signal | Moderate Signal | Light Signal"
      }
    ]
  }
  For sentiment_label only: Never output a numeric sentiment score. Describe the emotional tone in one or two words, grounded in what was actually said. Pick the single best descriptor that reflects the tone of the exchange itself — never a number.
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
- topics_to_avoid: [
    {
      "topic": "Short label for the sensitive topic already covered",
      "reason": "Why HR should not re-raise this proactively (e.g. employee volunteered it themselves; re-probing could feel invasive)"
    }
  ]
  Populate this ONLY for topics the employee voluntarily disclosed in this transcript without being pushed. The purpose is to stop HR from digging back into something the employee shared once, unprompted — re-raising it proactively next time could make them feel probed rather than supported. Leave empty if nothing qualifies.
- psychological_safety: {
    "statement": "A short opening line HR can say at the START of the NEXT conversation to establish psychological safety — grounded in what THIS employee specifically said/raised, not a generic script",
    "do": ["3-4 short, specific behavioural cues for HR to follow while opening this specific conversation, tied to what would help THIS employee based on the transcript"],
    "dont": ["3-4 short, specific things HR should avoid saying/doing with THIS employee, tied to the transcript"],
    "tip": "One short line on pacing/timing for this specific opener",
    "safety_score": "0-100 — how much follow-up attention this conversation warrants from HR (higher = the employee was open and at ease; treat a low score as a reason to re-check safety next time, never as a score of the employee)",
    "openness": "Assessment of how openly the employee communicated (e.g. open, guarded, selective)",
    "trust_level": "Assessment of trust signals (e.g. high, moderate, low, insufficient evidence)",
    "defensive_behaviour": "A short natural sentence describing any defensive behaviour observed. If none is present, write a brief natural sentence such as 'Nothing flagged — the conversation felt open and low-stress.' Never return a bare status label like 'None observed'.",
    "communication_style": "e.g. direct, hesitant, emotional, analytical, passive",
    "evidence": "Transcript evidence supporting the safety assessment",
    "interpretation": "What the safety signals likely indicate — ground this in observable behaviour from the transcript; never speculate about unstated motives, fears, or needs (no 'possibly because...', no 'this suggests he may be...')",
    "confidence_label": "Strong Signal | Moderate Signal | Light Signal"
  }
- risks: { "burnout_index": 0-100, "attrition_risk_pct": 0-100, "risk_factors": ["list of specific risk factors observed"] }

- step2_behavioural_intelligence: {
    "title_question": "What behaviour stands out?",
    "behaviour_summary": "1-line summary of observable patterns",
    "observed_behaviour": "What the employee actually did or said",
    "behaviour_pattern": "Recurring pattern detected (e.g. deflection, rationalisation, openness)",
    "supporting_evidence": "Direct quote or specific observation from transcript",
    "ai_interpretation": "What this pattern likely indicates (use probabilistic language) — never attach a clinical or trait label; describe the specific behaviour in plain, descriptive language anchored to what they said; never speculate about unstated motives, fears, or needs (no 'possibly because...', no 'this suggests he may be...')",
    "alternative_interpretation": "A different plausible explanation if evidence is limited",
    "underlying_drivers": [
      {
        "name": "One of: Workload Pressure | Lack of Clarity | Resource Constraints | Recognition Gap | Growth Stagnation",
        "confidence": "0-100 — how strongly the transcript evidence supports this as an underlying driver for the observed behaviour"
      }
      // Include only the drivers that have real transcript support — omit axes with no evidence rather than guessing a value.
      // Do not include a driver unless there is genuine transcript evidence for it — omitting an axis is correct when the evidence isn't there.
    ],
    "conversation_direction": "exploratory | solution-seeking | emotional | defensive | uncertain",
    "confidence_label": "Strong Signal | Moderate Signal | Light Signal",
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
    "ai_reasoning": "Explain why this is the most likely cause, referencing transcript evidence — describe the specific behaviour in plain, descriptive language, never a clinical or trait label; never speculate about unstated motives, fears, or needs (no 'possibly because...', no 'indicating a need for...', no 'this suggests he may feel...')",
    "confidence_label": "Strong Signal | Moderate Signal | Light Signal",
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
    "success_metric": "What HR should notice or check on next time — do NOT frame this as a target or milestone the employee needs to reach (avoid 'Success looks like: [employee] does X'). Phrase it as an observation for HR's attention, e.g. 'Next check-in: notice whether Harshit has found it easier to ask for support, without treating it as a milestone he needs to hit.'",
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
    "confidence_label": "Strong Signal | Moderate Signal | Light Signal",
    "suggested_script": "The ideal opening line for the next meeting",
    "recommended_actions": ["3-5 conversation techniques"],
    "avoid_actions": ["3-5 phrases/behaviours that may reduce trust"],
    "manager_notes": "Internal guidance only — what HR should remember before the next meeting. Never repeat content already shown on this page.",
    "key_takeaway": "Single most important insight HR should remember from this step"
  }

Judge every field independently. Only set a field to "Limited transcript evidence." (string fields) or an empty array (list fields) if that specific field truly has no relevant signal in the transcript. Do not blank out a whole step just because one field in it is weak — a typical transcript should leave most fields populated with a cautious, evidence-based answer. Never invent generic advice not grounded in the transcript.

Return ONLY valid JSON, no markdown formatting, no code fences."""


def _build_v1_prompt() -> str:
    """Build the classic (non-V2) analysis prompt.

    Shared by OpenAILLM.analyze and DeepSeekLLM.analyze so the two providers
    stay in sync by construction instead of carrying two near-identical copies
    of the same string.
    """
    return """You are a Senior Organizational Psychologist + HR Conversation Coach.
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
8. This tool exists to help HR remember what to follow up on — it does not score, rank, or rate the employee. Never phrase output as a judgment of the employee (e.g. avoid framing like "Harshit scored 75% positive sentiment"). Frame output as guidance for HR's next action (e.g. "These are the things worth following up on next time").
9. Never attach a clinical or trait label to the employee (e.g. "perfectionist tendency", "self-critical inner dialogue", "anxiety pattern"). Instead, describe the specific behaviour in plain, descriptive language anchored to what they actually said — e.g. "Harshit named a pattern of blaming himself after small mistakes" rather than "perfectionist tendency".
10. Do not speculate about the employee's underlying motives, fears, or psychological needs beyond what they explicitly said. Avoid phrases like "possibly because...", "this suggests he may be...", "indicating a need for..." — these are unstated inferences, not observations. Stick to describing what was said and what pattern it forms in behavior, not why it exists internally. If a reasonable behavioral observation is useful, phrase it as a description of the pattern itself, not a theory about its psychological cause — e.g. write "Harshit shared this without being asked" rather than "this suggests he may feel isolated and needs to unload."
11. Never emit a bare status word for a signal or flag field (e.g. "None observed", "N/A", "Not observed"). If nothing is flagged, write a brief natural sentence such as "Nothing flagged — the conversation felt open and low-stress."
12. Every confidence_label field must use exactly one value: Strong Signal | Moderate Signal | Light Signal. Strong Signal = multiple clear, direct pieces of transcript evidence. Moderate Signal = some supporting evidence but with a plausible alternative reading. Light Signal = a single weak or indirect cue. Never output a number.

INTERNAL VERIFICATION (silently check before generating output):
- Evidence exists
- Signal label assigned (Strong/Moderate/Light, evidence-based)
- Alternative explanation considered where relevant
- Recommendation is practical and measurable
- Language is probabilistic, not diagnostic
- No content repeated across steps

TITLE GENERATION (highest priority — do this FIRST before writing any other field):
After reading the ENTIRE conversation, write a single "title" field. This must be a 4-7 word title in title case that captures the CORE CONCLUSION or MAIN MOTIVE of the conversation — what was the employee really trying to say, what was the real issue underneath, or what was the single most important outcome. Ask yourself: "If HR could only remember ONE thing from this session, what would it be?" That is the title.
GOOD examples: "Burnout Risk From Unclear Expectations", "Employee Considering Exit Over Pay Gap", "Genuine Engagement Despite Role Misalignment", "Team Conflict Escalating Without Manager Support", "Micromanagement Driving Disengagement".
BAD examples (NEVER write these): "HR Sync Session", "Employee Conversation", "Discussed Work Concerns", "General Check-in", "Team Discussion", "Monthly Review".
The title must prove you understood the CONVERSATION DEEPLY — its real meaning, not just its surface topic.

Return a JSON object with these fields:
- title: 4-7 word title in title case — the CORE CONCLUSION of this conversation (as instructed above)
- summary: Maximum 2-line summary of the conversation. Focus on the core issue, not generic recap.
- psychology: {
    "sentiment_label": "Positive | Reflective | Anxious | Strained | Mixed",
    "behavioural_interpretation": [
      {
        "observed_behaviour": "What the employee actually did or said",
        "evidence": "Direct quote or specific observation from transcript",
        "interpretation": "What this behaviour likely indicates (use probabilistic language) — never attach a clinical or trait label; describe the specific behaviour in plain, descriptive language anchored to what they actually said; never speculate about unstated motives, fears, or needs (no 'possibly because...', no 'this suggests he may be...')",
        "confidence_label": "Strong Signal | Moderate Signal | Light Signal"
      }
    ]
  }
  For sentiment_label only: Never output a numeric sentiment score. Describe the emotional tone in one or two words, grounded in what was actually said. Pick the single best descriptor that reflects the tone of the exchange itself — never a number.
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
- topics_to_avoid: [
    {
      "topic": "Short label for the sensitive topic already covered",
      "reason": "Why HR should not re-raise this proactively (e.g. employee volunteered it themselves; re-probing could feel invasive)"
    }
  ]
  Populate this ONLY for topics the employee voluntarily disclosed in this transcript without being pushed. The purpose is to stop HR from digging back into something the employee shared once, unprompted — re-raising it proactively next time could make them feel probed rather than supported. Leave empty if nothing qualifies.
- psychological_safety: {
    "statement": "A short opening line HR can say at the START of the NEXT conversation to establish psychological safety, grounded in what THIS employee specifically said/raised, not a generic script",
    "do": ["3-4 short, specific behavioural cues for HR to follow while opening this specific conversation"],
    "dont": ["3-4 short, specific things HR should avoid saying/doing with THIS employee"],
    "tip": "One short line on pacing/timing for this specific opener",
    "safety_score": "0-100 — how much follow-up attention this conversation warrants from HR (higher = the employee was open and at ease; treat a low score as a reason to re-check safety next time, never as a score of the employee)",
    "openness": "Assessment of how openly the employee communicated (e.g. open, guarded, selective)",
    "trust_level": "Assessment of trust signals (e.g. high, moderate, low, insufficient evidence)",
    "defensive_behaviour": "A short natural sentence describing any defensive behaviour observed. If none is present, write a brief natural sentence such as 'Nothing flagged — the conversation felt open and low-stress.' Never return a bare status label like 'None observed'.",
    "communication_style": "e.g. direct, hesitant, emotional, analytical, passive",
    "evidence": "Transcript evidence supporting the safety assessment",
    "interpretation": "What the safety signals likely indicate — ground this in observable behaviour from the transcript; never speculate about unstated motives, fears, or needs (no 'possibly because...', no 'this suggests he may be...')",
    "confidence_label": "Strong Signal | Moderate Signal | Light Signal"
  }
- risks: { "burnout_index": 0-100, "attrition_risk_pct": 0-100, "risk_factors": ["list of specific risk factors observed"] }

- step2_behavioural_intelligence: {
    "title_question": "What behaviour stands out?",
    "behaviour_summary": "1-line summary of observable patterns",
    "observed_behaviour": "What the employee actually did or said",
    "behaviour_pattern": "Recurring pattern detected (e.g. deflection, rationalisation, openness)",
    "supporting_evidence": "Direct quote or specific observation from transcript",
    "ai_interpretation": "What this pattern likely indicates (use probabilistic language) — never attach a clinical or trait label; describe the specific behaviour in plain, descriptive language anchored to what they said; never speculate about unstated motives, fears, or needs (no 'possibly because...', no 'this suggests he may be...')",
    "alternative_interpretation": "A different plausible explanation if evidence is limited",
    "underlying_drivers": [
      {
        "name": "One of: Workload Pressure | Lack of Clarity | Resource Constraints | Recognition Gap | Growth Stagnation",
        "confidence": "0-100 — how strongly the transcript evidence supports this as an underlying driver for the observed behaviour"
      }
      // Include only the drivers that have real transcript support — omit axes with no evidence rather than guessing a value.
      // Do not include a driver unless there is genuine transcript evidence for it — omitting an axis is correct when the evidence isn't there.
    ],
    "conversation_direction": "exploratory | solution-seeking | emotional | defensive | uncertain",
    "confidence_label": "Strong Signal | Moderate Signal | Light Signal",
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
    "ai_reasoning": "Explain why this is the most likely cause, referencing transcript evidence — describe the specific behaviour in plain, descriptive language, never a clinical or trait label; never speculate about unstated motives, fears, or needs (no 'possibly because...', no 'indicating a need for...', no 'this suggests he may feel...')",
    "confidence_label": "Strong Signal | Moderate Signal | Light Signal",
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
    "success_metric": "What HR should notice or check on next time — do NOT frame this as a target or milestone the employee needs to reach (avoid 'Success looks like: [employee] does X'). Phrase it as an observation for HR's attention, e.g. 'Next check-in: notice whether Harshit has found it easier to ask for support, without treating it as a milestone he needs to hit.'",
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
    "confidence_label": "Strong Signal | Moderate Signal | Light Signal",
    "suggested_script": "The ideal opening line for the next meeting",
    "recommended_actions": ["3-5 conversation techniques"],
    "avoid_actions": ["3-5 phrases/behaviours that may reduce trust"],
    "manager_notes": "Internal guidance only. Never repeat content already shown on this page.",
    "key_takeaway": "Single most important insight HR should remember from this step"
  }

Judge each field on its own: only set an individual string field to "Limited transcript evidence." (or a list field to empty) if that specific field has no relevant signal in the transcript. Do not blank an entire step just because one field in it is weak — most fields should stay populated with a cautious, evidence-based answer. Short or code-mixed (e.g. Hindi-English) transcripts still count as evidence; never invent generic advice not grounded in the transcript.

Return ONLY valid JSON, no markdown formatting."""


_HINGLISH_INSTRUCTION = """\n\nOUTPUT LANGUAGE: Hinglish\n\nRender every human-readable text value in the response below (title, summary, behavioural interpretations, conversation coach notes, follow-up plans, psychological-safety statement, do/don't guidance, suggested scripts, suggested questions and HR replies, risk factors, topics to avoid, recommendations and manager notes) in Hinglish written in Latin script — casual Hindi-English code-mixing, the way Hindi-speaking professionals actually talk. For example: "Follow-up karna zaroori hai kyunki usne kaam ka load clearly share kiya tha." and "Agli baar pehle expectations set karna behtar rahega taaki confusion na ho."\n\nKeep every JSON key, id and the enumerated status/value labels (e.g. Strong Signal | Moderate Signal | Light Signal, Positive | Reflective | Anxious | Strained | Mixed, openness, trust_level, communication_style values) in English exactly as specified by the schema. Do NOT translate verbatim quotes from the transcript — reproduce them word-for-word as spoken."""


def _language_instruction(language: str | None) -> str:
    """Return an output-language suffix for the analysis system prompt.

    Only "hinglish" produces a suffix; English / missing / unknown values
    return "" so the default prompt stays byte-for-byte identical.
    """
    if (language or "").strip().lower() == "hinglish":
        return _HINGLISH_INSTRUCTION
    return ""


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
    "psychology": {"sentiment_label": "", "behavioural_interpretation": []},
    "conversation_coach": [],
    "realistic_solutions": {"immediate": "", "this_week": "", "manager": "", "environment": ""},
    "next_conversation_plan": [],
    "topics_to_avoid": [],
    "psychological_safety": {
        "statement": "", "do": [], "dont": [], "tip": "",
        "safety_score": 0, "openness": "", "trust_level": "",
        "defensive_behaviour": "", "communication_style": "",
        "evidence": "", "interpretation": "", "confidence_label": ""
    },
    "risks": {"burnout_index": 0, "attrition_risk_pct": 0, "risk_factors": []},
    "step2_behavioural_intelligence": {
        "title_question": "", "behaviour_summary": "", "observed_behaviour": "", "behaviour_pattern": "",
        "supporting_evidence": "", "ai_interpretation": "", "alternative_interpretation": "",
        "conversation_direction": "", "confidence_label": "", "suggested_script": "",
        "recommended_actions": [], "avoid_actions": [], "manager_notes": "", "key_takeaway": "",
        "underlying_drivers": []
    },
    "step3_root_cause_analysis": {
        "title_question": "", "primary_trigger": "", "secondary_contributors": [],
        "supporting_evidence": [], "evidence_strength": "", "missing_information": [],
        "ai_reasoning": "", "confidence_label": "", "suggested_script": "",
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
        "suggested_reply": "", "what_to_listen_for": [], "success_indicator": "", "confidence_label": "",
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
      "confidence_label": "Strong Signal | Moderate Signal | Light Signal",
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
            if "confidence_label" in row:
                row["confidence_label"] = _parse_confidence_label(
                    row["confidence_label"] or item.get("confidence")
                )
            cleaned.append(row)
        return cleaned[:8]

    out["hr_phrasing_flags"] = _clean_list(
        "hr_phrasing_flags",
        ["quote", "category", "severity", "issue", "better_rephrasing", "why_it_works"],
    )
    out["employee_signals"] = _clean_list(
        "employee_signals",
        ["quote", "signal", "confidence_label", "evidence_basis", "hr_suggestion"],
    )
    out["positive_moments"] = _clean_list("positive_moments", ["quote", "why_it_works"])[:4]

    return out


def _call_and_parse(
    client,
    model: str,
    system_prompt: str,
    user_content: str,
    validator,
    fallback: dict,
    temperature: float = 0.3,
    supports_json_mode: bool = True,
    log_label: str = "",
    timeout: float | None = None,
) -> dict:
    """Shared call/parse/fallback logic for both OpenAI-compatible providers.

    OpenAILLM and DeepSeekLLM each repeat the same sequence — build a client,
    call chat.completions.create, parse the response, validate, and fall back
    to a default dict on parse failure. This helper extracts that boilerplate
    so behavior can't silently diverge between the two providers.

    supports_json_mode=True uses response_format={"type": "json_object"}
    (OpenAI). supports_json_mode=False (DeepSeek) instead strips markdown code
    fences defensively from the raw response before parsing, since DeepSeek
    doesn't reliably support response_format=json_object.

    timeout overrides the per-call budget (default _llm_timeout_seconds, 20s).
    Best-effort secondary calls such as drift detection pass their own tighter
    cap so a request that chains several calls still fits inside the platform
    worker timeout.
    """
    kwargs = dict(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        temperature=temperature,
    )
    # Bound request duration. Without an explicit timeout the openai SDK
    # waits indefinitely, and a slow/hung DeepSeek request then outlives the
    # platform worker timeout (~30s), which kills the worker BEFORE Flask's
    # own error handler can run — so the user sees the platform's raw HTML
    # error page instead of this app's JSON. 20s leaves headroom under that.
    kwargs["timeout"] = timeout if timeout is not None else _llm_timeout_seconds()
    if supports_json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    else:
        kwargs["stream"] = False

    try:
        resp = client.chat.completions.create(**kwargs)
    except APITimeoutError:
        if log_label:
            logger.warning(f"{log_label}: LLM request timed out after {kwargs['timeout']}s")
        raise LLMTimeoutError from None
    raw_content = resp.choices[0].message.content or ""

    if not supports_json_mode:
        raw_content = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_content.strip())

    try:
        result = json.loads(raw_content)
    except (json.JSONDecodeError, AttributeError, IndexError, TypeError):
        if log_label:
            logger.warning(f"{log_label}: JSON parse failed")
        return dict(fallback)

    # Always run validation, not just in V2 mode — the model can return a field
    # with the wrong type regardless of which prompt was used, and downstream
    # code assumes these fields are dicts/lists as documented.
    return validator(result)


class BaseLLM(ABC):
    @abstractmethod
    def analyze(self, transcript: str, language: str = "en") -> dict:
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

    @abstractmethod
    def translate(self, text: str, target_language: str) -> str:
        """Translate text into the target language (a human-readable name,
        e.g. "Hindi"). Returns ONLY the translated text."""
        ...


class OpenAILLM(BaseLLM):
    def __init__(self):
        self.api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_KEY", "")
        self.model = os.environ.get("OPENAI_ANALYSIS_MODEL", "gpt-4o")

    def analyze(self, transcript: str, language: str = "en") -> dict:
        from openai import OpenAI
        from flask import current_app

        client = OpenAI(api_key=self.api_key)
        use_v2 = False
        try:
            use_v2 = current_app.config.get("USE_V2_FRAMEWORK", False)
        except RuntimeError:
            use_v2 = os.environ.get("USE_V2_FRAMEWORK", "false").lower() == "true"

        system_prompt = (_build_v2_prompt() if use_v2 else _build_v1_prompt()) + _language_instruction(language)

        return _call_and_parse(
            client, self.model, system_prompt, transcript,
            validator=validate_analysis, fallback=FALLBACK_ANALYSIS,
            temperature=0.3, supports_json_mode=True,
        )

    def explain_drift(self, sessions: list) -> dict:
        """
        sessions: list of {date, attrition_risk_pct, burnout_index, transcript},
        oldest first, minimum 3 entries (caller's responsibility to enforce).
        Sends FULL transcripts together (not just the risk % numbers) so the
        model can cross-reference actual conversation content, not just trend lines.
        """
        from openai import OpenAI

        client = OpenAI(api_key=self.api_key)

        return _call_and_parse(
            client, self.model, _build_drift_system_prompt(), _build_drift_prompt(sessions),
            validator=validate_drift_explanation, fallback=FALLBACK_DRIFT_EXPLANATION,
            temperature=0.3, supports_json_mode=True,
            timeout=_llm_drift_timeout_seconds(),
        )

    def analyze_phrasing(self, transcript: str) -> dict:
        from openai import OpenAI

        client = OpenAI(api_key=self.api_key)

        return _call_and_parse(
            client, self.model, _build_phrasing_prompt(), transcript,
            validator=validate_phrasing_analysis, fallback=FALLBACK_PHRASING_ANALYSIS,
            temperature=0.2, supports_json_mode=True,
        )

    def translate(self, text: str, target_language: str) -> str:
        from openai import OpenAI

        client = OpenAI(api_key=self.api_key)
        prompt = (
            f"Translate the following text into {target_language}. "
            f"Return ONLY the translated text, no preamble, no quotes, no explanation:\n\n{text}"
        )
        resp = client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            timeout=_llm_timeout_seconds(),
        )
        return (resp.choices[0].message.content or "").strip()


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

    def analyze(self, transcript: str, language: str = "en") -> dict:
        from openai import OpenAI

        client = OpenAI(api_key=self.api_key, base_url=self.base_url)

        use_v2 = False
        try:
            use_v2 = current_app.config.get("USE_V2_FRAMEWORK", False)
        except RuntimeError:
            use_v2 = os.environ.get("USE_V2_FRAMEWORK", "false").lower() == "true"

        system_prompt = (_build_v2_prompt() if use_v2 else _build_v1_prompt()) + _language_instruction(language)

        return _call_and_parse(
            client, self.model, system_prompt, transcript,
            validator=validate_analysis, fallback=FALLBACK_ANALYSIS,
            temperature=0.3, supports_json_mode=False, log_label="DeepSeek analyze",
        )

    def explain_drift(self, sessions: list) -> dict:
        """
        sessions: list of {date, attrition_risk_pct, burnout_index, transcript},
        oldest first, minimum 3 entries (caller's responsibility to enforce).
        Sends FULL transcripts together (not just the risk % numbers) so the
        model can cross-reference actual conversation content, not just trend lines.
        """
        from openai import OpenAI

        client = OpenAI(api_key=self.api_key, base_url=self.base_url)

        return _call_and_parse(
            client, self.model, _build_drift_system_prompt(), _build_drift_prompt(sessions),
            validator=validate_drift_explanation, fallback=FALLBACK_DRIFT_EXPLANATION,
            temperature=0.3, supports_json_mode=False, log_label="DeepSeek explain_drift",
            timeout=_llm_drift_timeout_seconds(),
        )

    def analyze_phrasing(self, transcript: str) -> dict:
        from openai import OpenAI

        client = OpenAI(api_key=self.api_key, base_url=self.base_url)

        return _call_and_parse(
            client, self.model, _build_phrasing_prompt(), transcript,
            validator=validate_phrasing_analysis, fallback=FALLBACK_PHRASING_ANALYSIS,
            temperature=0.2, supports_json_mode=False, log_label="DeepSeek analyze_phrasing",
        )

    def translate(self, text: str, target_language: str) -> str:
        from openai import OpenAI

        client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        prompt = (
            f"Translate the following text into {target_language}. "
            f"Return ONLY the translated text, no preamble, no quotes, no explanation:\n\n{text}"
        )
        resp = client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            timeout=_llm_timeout_seconds(),
        )
        return (resp.choices[0].message.content or "").strip()

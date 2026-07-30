"""
Debug script: Traces the full pipeline from DeepSeek response -> JSON parsing -> validation.
Runs independently of Flask (loads env vars directly).
"""
import os
import sys
import json
import re
from pathlib import Path

# Load .env manually
dotenv_path = Path(".env")
if dotenv_path.exists():
    for line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

# ── Configuration ──
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("DEEPSSEK_API", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_ANALYSIS_MODEL", "deepseek-chat")
USE_V2 = os.environ.get("USE_V2_FRAMEWORK", "false").lower() == "true"

print(f"[CONFIG] USE_V2_FRAMEWORK={USE_V2}", file=sys.stderr)
print(f"[CONFIG] DEEPSEEK_API_KEY={'set' if DEEPSEEK_API_KEY else 'MISSING'}", file=sys.stderr)
print(f"[CONFIG] DEEPSEEK_MODEL={DEEPSEEK_MODEL}", file=sys.stderr)

# ── The exact prompt used by DeepSeekLLM (from providers/llm.py) ──
NON_V2_PROMPT = """You are a Senior Organizational Psychologist + HR Conversation Coach.
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

# ── A realistic bilingual transcript (from the failed sessions) ──
SAMPLE_TRANSCRIPT = """Manager: Hi Harshit, kaise ho? Let's start our weekly check-in.
Harshit: Hi sir, main theek hoon. Bas kaam ka pressure thoda zyada hai.
Manager: Haan, I understand. Can you tell me more about what's been challenging?
Harshit: Actually sir, pichle kuch hafton se mujhe laga ki mera workload bahut badh gaya hai. Main roz 10-11 ghante kaam kar raha hoon. Kuch projects ki deadlines ek saath aa gayi hain.
Manager: That sounds tough. Have you spoken to anyone about this?
Harshit: Nahi sir, main sochta hoon ki yeh sab karna hi hai. Everyone is working hard. Main complain nahi karna chahta.
Manager: I appreciate that, but your wellbeing matters. Do you feel comfortable in team meetings?
Harshit: Haan, comfortable hoon. But kabhi kabhi I feel ki meri baat ko utna importance nahi milta. Especially jab main koi suggestion deta hoon, toh woh ignore ho jata hai.
Manager: That's concerning. Can you give me an example?
Harshit: Last week, maine ek improvement suggest kiya tha for the reporting process, but my team lead said "we'll see" and then nothing happened. Aisa pehle bhi ho chuka hai.
Manager: I see. How does that make you feel?
Harshit: Honestly, demotivated. Ab main suggestions dena hi band kar diya hoon. Why bother if no one listens?
Manager: I'm glad you shared this. What would help you feel more engaged?
Harshit: Maybe if someone actually follows up on my suggestions. Or at least tells me why it can't be done. Bas ignore karna accha nahi lagta.
Manager: Fair point. I'll look into this. Anything else on your mind?
Harshit: Actually, mai soch raha hoon ki kya mujhe koi training le leni chahiye. I feel stuck in my current role. Growth nahi dikh raha hai.
Manager: That's a good initiative. Let's discuss this in our next 1:1.
Harshit: Okay sir. Thank you for listening."""

# ── Import the same validate_analysis and FALLBACK_ANALYSIS ──
sys.path.insert(0, str(Path(".").resolve()))
# We'll inline the relevant parts to avoid Flask dependency
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

# ── STAGE 1: Call DeepSeek API ──
print("=" * 60, file=sys.stderr)
print("[DEBUG] STAGE 1: Calling DeepSeek API...", file=sys.stderr)

from openai import OpenAI
client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

prompt = FALLBACK_ANALYSIS  # dummy, just to have var
# Use the non-V2 prompt (as is currently live)
system_prompt = NON_V2_PROMPT

resp = client.chat.completions.create(
    model=DEEPSEEK_MODEL,
    messages=[
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": SAMPLE_TRANSCRIPT},
    ],
    temperature=0.3,
    stream=False,
)

raw_content = resp.choices[0].message.content or ""
print("[DEBUG] STAGE 1 — RAW LLM RESPONSE:", file=sys.stderr)
print(raw_content[:4000], file=sys.stderr)
print("...(truncated)" if len(raw_content) > 4000 else "", file=sys.stderr)
print("RAW LENGTH:", len(raw_content), file=sys.stderr)

# ── STAGE 2: Clean JSON string ──
print("---", file=sys.stderr)
print("[DEBUG] STAGE 2 — CLEANED:", file=sys.stderr)
cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_content.strip())
print(cleaned[:4000], file=sys.stderr)

# ── STAGE 3: Parse JSON ──
print("---", file=sys.stderr)
print("[DEBUG] STAGE 3 — PARSING JSON...", file=sys.stderr)
try:
    result = json.loads(cleaned)
except json.JSONDecodeError as e:
    print(f"[DEBUG] JSON PARSE FAILED: {e}", file=sys.stderr)
    result = dict(FALLBACK_ANALYSIS)
    print("[DEBUG] FALLBACK TRIGGERED — reason: JSON parse error", file=sys.stderr)

print(f"[DEBUG] PARSED TYPE: {type(result).__name__}", file=sys.stderr)
if isinstance(result, dict):
    print(f"TOP-LEVEL KEYS: {list(result.keys())}", file=sys.stderr)

# ── STAGE 4: Field types ──
print("---", file=sys.stderr)
print("[DEBUG] STAGE 4 — FIELD TYPES & LTE SCAN:", file=sys.stderr)
step_keys = ["step2_behavioural_intelligence", "step3_root_cause_analysis", "step4_action_blueprint", "step5_conversation_strategy"]
if isinstance(result, dict):
    for fname in ["psychological_safety"] + step_keys:
        val = result.get(fname)
        t = type(val).__name__
        print(f"  {fname}: {t}", end="", file=sys.stderr)
        if isinstance(val, dict):
            lte_count = sum(1 for v in val.values() if isinstance(v, str) and v == "Limited transcript evidence.")
            empty_list_count = sum(1 for v in val.values() if isinstance(v, list) and len(v) == 0)
            print(f"  keys={list(val.keys())[:15]}  LTE={lte_count}  empty_lists={empty_list_count}", file=sys.stderr)
        elif isinstance(val, list):
            print(f"  len={len(val)}", file=sys.stderr)
        else:
            print(f"  repr={repr(val)[:100]}", file=sys.stderr)
else:
    print(f"  (top-level is a list, not a dict)", file=sys.stderr)

# ── STAGE 5: Detailed LTE scan ──
print("---", file=sys.stderr)
print("[DEBUG] STAGE 5 — DETAILED LTE SCAN:", file=sys.stderr)
if isinstance(result, dict):
    for sk in step_keys:
        step = result.get(sk)
        if isinstance(step, dict):
            lte_fields = [k for k, v in step.items() if isinstance(v, str) and v == "Limited transcript evidence."]
            empty_lists = [k for k, v in step.items() if isinstance(v, list) and len(v) == 0]
            missing_keys = [k for k in FALLBACK_ANALYSIS.get(sk, {}).keys() if k not in step]
            if lte_fields:
                print(f"  {sk} LTE fields: {lte_fields}", file=sys.stderr)
            if empty_lists:
                print(f"  {sk} EMPTY arrays: {empty_lists}", file=sys.stderr)
            if missing_keys:
                print(f"  {sk} MISSING keys: {missing_keys}", file=sys.stderr)
            if not lte_fields and not empty_lists and not missing_keys:
                print(f"  {sk}: ALL POPULATED", file=sys.stderr)
        elif step is None:
            print(f"  {sk}: KEY NOT PRESENT IN RESPONSE", file=sys.stderr)
        else:
            print(f"  {sk}: WRONG TYPE ({type(step).__name__})", file=sys.stderr)

# ── STAGE 6: validate_analysis (only relevant fields) ──
if USE_V2 and isinstance(result, dict):
    print("---", file=sys.stderr)
    print("[DEBUG] STAGE 6 — Running validate_analysis()...", file=sys.stderr)
    # Inline a simple validate_analysis check
    DICT_KEYS = {"psychological_safety", "risks", "realistic_solutions",
                 "step2_behavioural_intelligence", "step3_root_cause_analysis",
                 "step4_action_blueprint", "step5_conversation_strategy"}
    for key in DICT_KEYS:
        if key in result and not isinstance(result[key], dict):
            print(f"  [VALIDATE] WRONG TYPE for '{key}': {type(result[key]).__name__} -> replaced with {{}}", file=sys.stderr)
            result[key] = {}
    print("[DEBUG] STAGE 6 — Validation complete", file=sys.stderr)
else:
    print("---", file=sys.stderr)
    print(f"[DEBUG] STAGE 6 — validate_analysis() SKIPPED (USE_V2={USE_V2})", file=sys.stderr)

# ── Final summary ──
print("=" * 60, file=sys.stderr)
print("[DEBUG] FINAL SUMMARY:", file=sys.stderr)
all_lte = True
for sk in step_keys:
    step = result.get(sk) if isinstance(result, dict) else None
    if isinstance(step, dict):
        lte = [k for k, v in step.items() if isinstance(v, str) and v == "Limited transcript evidence."]
        print(f"  {sk}: {len(lte)}/{len(step)} fields are LTE", file=sys.stderr)
        if len(lte) < len(step):
            all_lte = False
    else:
        print(f"  {sk}: {type(step).__name__}", file=sys.stderr)

print(f"\n[DEBUG] ALL STEPS ALL-LTE? {all_lte}", file=sys.stderr)
print(f"\n[DEBUG] ROOT CAUSE HYPOTHESIS:", file=sys.stderr)
if not isinstance(result, dict):
    print("  Top-level response is NOT a JSON object (it's a list)", file=sys.stderr)
elif all_lte:
    print("  DeepSeek is explicitly writing 'Limited transcript evidence.' for all step2-5 fields", file=sys.stderr)
    print("  -> Prompt says 'If transcript evidence is too thin...set to Limited transcript evidence.'", file=sys.stderr)
    print("  -> AI interprets moderate evidence as 'too thin' for step2-5 (over-generalization)", file=sys.stderr)
elif "step2_behavioural_intelligence" not in result:
    print("  step2-5 keys are MISSING entirely from the AI response", file=sys.stderr)
else:
    print("  Partially populated — specific fields have real data", file=sys.stderr)

print("=" * 60, file=sys.stderr)

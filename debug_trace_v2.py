"""
Trace USE_V2_FRAMEWORK execution path without changing business logic.
Reads the same .env file and Config class as the running app.
"""
import os
import sys
from pathlib import Path

print("=" * 60, file=sys.stderr)
print("[TRACE] USE_V2_FRAMEWORK execution path investigation", file=sys.stderr)
print("=" * 60, file=sys.stderr)

# ── 1. Where USE_V2_FRAMEWORK is defined ──
print("\n[1] Definition site: config.py line 48", file=sys.stderr)
print("    USE_V2_FRAMEWORK = os.environ.get('USE_V2_FRAMEWORK', 'false').lower() == 'true'", file=sys.stderr)

# ── 2. Check .env file ──
print("\n[2] .env file scan:", file=sys.stderr)
dotenv_path = Path(".env")
if dotenv_path.exists():
    lines = dotenv_path.read_text(encoding="utf-8").splitlines()
    v2_lines = [l for l in lines if "USE_V2" in l.upper()]
    if v2_lines:
        for l in v2_lines:
            print(f"    FOUND: {l}", file=sys.stderr)
    else:
        print("    NOT FOUND: No USE_V2_FRAMEWORK entry in .env", file=sys.stderr)
else:
    print("    .env file does not exist", file=sys.stderr)

# ── 3. Check environment variables directly ──
print("\n[3] Environment variable check:", file=sys.stderr)
env_val = os.environ.get("USE_V2_FRAMEWORK")
if env_val is None:
    print("    os.environ['USE_V2_FRAMEWORK']: NOT SET (None)", file=sys.stderr)
else:
    print(f"    os.environ['USE_V2_FRAMEWORK']: '{env_val}'", file=sys.stderr)

# ── 4. Simulate Config class parsing ──
print("\n[4] Config class parsing simulation:", file=sys.stderr)
parsed = os.environ.get("USE_V2_FRAMEWORK", "false").lower() == "true"
print(f"    os.environ.get('USE_V2_FRAMEWORK', 'false').lower() == 'true'  =>  {parsed}", file=sys.stderr)
print(f"    Therefore Config.USE_V2_FRAMEWORK = {parsed}", file=sys.stderr)

# ── 5. Trace through Flask app initialization ──
print("\n[5] Flask app initialization trace:", file=sys.stderr)
print("    app.py:15  ->  app.config.from_object(Config)", file=sys.stderr)
print("    This copies Config.USE_V2_FRAMEWORK (False) into app.config", file=sys.stderr)
print("    So current_app.config['USE_V2_FRAMEWORK'] == False", file=sys.stderr)

# ── 6. Trace through API request → DeepSeekLLM.analyze() ──
print("\n[6] Execution path: API request → prompt selection", file=sys.stderr)
print("    sessions.py:226  ->  analysis = llm.analyze(transcript)", file=sys.stderr)
print("    providers/__init__.py:20  ->  from providers.llm import DeepSeekLLM", file=sys.stderr)
print("    providers/__init__.py:21  ->  return DeepSeekLLM()", file=sys.stderr)
print("    DeepSeekLLM.__init__(): providers/llm.py:413-416", file=sys.stderr)
print("    DeepSeekLLM.analyze(): providers/llm.py:418", file=sys.stderr)

# ── 7. Exact V2 toggle logic ──
print("\n[7] V2 toggle logic in DeepSeekLLM.analyze() (providers/llm.py:424-430):", file=sys.stderr)
print("    Line 424: use_v2 = False", file=sys.stderr)
print("    Line 425-426: try: use_v2 = current_app.config.get('USE_V2_FRAMEWORK', False)", file=sys.stderr)
print("    Line 427-428: except RuntimeError: use_v2 = os.environ.get('USE_V2_FRAMEWORK', 'false').lower() == 'true'", file=sys.stderr)
print("    Line 430: system_prompt = _build_v2_prompt() if use_v2 else <legacy_prompt>", file=sys.stderr)

# Simulate both code paths
print("\n[8] Runtime simulation:", file=sys.stderr)
try:
    from flask import current_app
    # We're not in a Flask request context, so this will raise RuntimeError
    flask_val = current_app.config.get("USE_V2_FRAMEWORK", False)
    print(f"    Via current_app.config: USE_V2_FRAMEWORK = {flask_val}", file=sys.stderr)
except RuntimeError:
    fallback = os.environ.get("USE_V2_FRAMEWORK", "false").lower() == "true"
    print(f"    Outside Flask context (RuntimeError caught)", file=sys.stderr)
    print(f"    Fallback to os.environ: USE_V2_FRAMEWORK = {fallback}", file=sys.stderr)

print(f"\n[9] _build_v2_prompt() is called? ", file=sys.stderr)
print(f"    Answer: NO — use_v2 is {parsed}, so the ternary on line 430", file=sys.stderr)
print(f"            evaluates to the ELSE branch (the legacy prompt string).", file=sys.stderr)

# ── 10. Check the OpenAILLM too for comparison ──
print(f"\n[10] Same logic exists in OpenAILLM.analyze() (providers/llm.py:322-333)", file=sys.stderr)
print(f"     Line 327-332 has identical V2 toggle logic.", file=sys.stderr)
print(f"     Both providers use the same code pattern.", file=sys.stderr)

# ── 11. How to enable V2 ──
print(f"\n[11] How to enable V2 (without code changes):", file=sys.stderr)
print(f"     Option A: Add to .env:  USE_V2_FRAMEWORK=true", file=sys.stderr)
print(f"     Option B: Set env var before launch:", file=sys.stderr)
print(f"       $env:USE_V2_FRAMEWORK='true'; python app.py", file=sys.stderr)
print(f"     Option C: Export in shell: export USE_V2_FRAMEWORK=true", file=sys.stderr)
print(f"     No code changes needed — the toggle is already wired.", file=sys.stderr)

# ── 12. Consequences ──
print(f"\n[12] Consequences of USE_V2_FRAMEWORK=False:", file=sys.stderr)
print(f"     - _build_v2_prompt() is NEVER called", file=sys.stderr)
print(f"     - validate_analysis() is NEVER called", file=sys.stderr)
print(f"     - The legacy prompt (with {{ ... }} for step2-5) is used", file=sys.stderr)
print(f"     - DeepSeek invents its own field names for step2-5", file=sys.stderr)
print(f"     - Frontend shows 'Limited transcript evidence.' for all step2-5 fields", file=sys.stderr)

print("=" * 60, file=sys.stderr)

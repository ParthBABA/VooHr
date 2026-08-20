# LLM Data Privacy — Data Flow Record

This document records every path where VooHr employee/user data is sent to
an external or local LLM provider, and whether PII minimization is applied.

---

## 1. Transcript Analysis (`analyze`)

**Trigger:** `POST /api/sessions/<session_id>/analyze`
**Source file:** `sessions.py:211-408`
**LLM classes:** `DeepSeekLLM.analyze()` (`providers/llm.py:873-1049`),
                `OpenAILLM.analyze()` (`providers/llm.py:657-829`)

### Data Sent

| Field | Included | Notes |
|-------|----------|-------|
| Transcript text (edited or raw) | Yes | Core analysis input — the conversation content |
| Employee name | No | Never included in prompt |
| Employee email | No | Never included in prompt |
| Employee phone | No | Never included in prompt |
| Employee department/position | No | Never included in prompt |
| Session metadata (source, device) | No | Never included in prompt |
| Employee ID / Org ID | No | Never included in prompt |
| API keys / tokens / secrets | No | Never included in prompt |
| User-Agent / IP | No | Never included in prompt |

### Prompt Structure

- **System prompt:** Generic HR conversation coach instructions (identical
  for all employees). Contains no employee-specific identifiers.
- **User message:** Raw transcript text only. No preamble with employee
  name, department, or other identifiers.

### PII Minimization Status

**Applied.** The LLM receives only the transcript text — a minimal data set
required for behavioral analysis. No employee PII (name, email, phone,
department, ID) is included. No authentication credentials, session tokens,
or encryption keys are included.

### Storage / Retention

- The LLM provider (DeepSeek or OpenAI) receives the transcript as part of
  a chat completion request.
- Provider retention policies are governed by the respective provider's
  terms of service. VooHr does not control provider-side data retention.
- The analysis result (structured JSON) is stored in the `sessions` collection
  under `analysis` and propagated to `employees.ai_wellness`.

---

## 2. Risk Drift Detection (`explain_drift`)

**Trigger:** Automatically after `POST /api/sessions/<session_id>/analyze`
             when ≥3 completed sessions exist for an employee.
**Source file:** `sessions.py:293-387`
**LLM classes:** `DeepSeekLLM.explain_drift()` (`providers/llm.py:1051-1084`),
                `OpenAILLM.explain_drift()` (`providers/llm.py:831-857`)

### Data Sent

| Field | Included | Notes |
|-------|----------|-------|
| Transcript text (up to 3 sessions) | Yes | Full transcripts for cross-reference |
| Risk scores per session (attrition_risk_pct, burnout_index) | Yes | Trend context for drift analysis |
| Session dates | Yes | Timeline context |
| Employee name | No | Never included in prompt |
| Employee email | No | Never included in prompt |
| Employee ID / Org ID | No | Never included in prompt |
| API keys / tokens / secrets | No | Never included in prompt |

### Prompt Structure

- **System prompt:** Generic senior organizational psychologist instructions.
  No employee-specific identifiers.
- **User message:** Labeled transcript blocks in the format:
  ```
  --- Sync 1 (2024-01-15) — attrition_risk_pct=45, burnout_index=60 ---
  [transcript text]
  ```
  No employee name, email, or organizational identifiers.

### PII Minimization Status

**Applied.** The drift detection prompt contains only transcript text and
derived risk scores — the minimum data needed for cross-session pattern
analysis. No employee PII is included.

### Storage / Retention

- Provider retention governed by DeepSeek/OpenAI terms of service.
- The drift explanation result is stored in `employees.drift_explanation`
  and may trigger a notification in `notifications`.

---

## 3. Audio Transcription (STT)

**Trigger:** `POST /api/transcribe`
**Source file:** `sessions.py:411-433`
**Provider:** `OpenAIWhisperSTT` (`providers/openai_stt.py`)

### Data Sent

| Field | Included | Notes |
|-------|----------|-------|
| Audio bytes | Yes | The raw audio recording |
| Content type | Yes | For format detection |

### PII Considerations

The audio recording may contain the employee's voice and conversation
content. This is the core functionality — STT is required to convert
recorded HR conversations into text for analysis. No additional PII
(name, email, ID) is sent beyond the audio itself.

### Storage / Retention

- OpenAI Whisper API retention governed by OpenAI's data usage policies.
- The resulting text transcript is stored in `sessions.transcript`.

---

## 4. Image OCR (Vision)

**Trigger:** `POST /api/transcribe-image`
**Source file:** `sessions.py:436-464`
**Provider:** `OpenAIVisionOCR` (`providers/vision_ocr.py`)

### Data Sent

| Field | Included | Notes |
|-------|----------|-------|
| Image bytes (base64) | Yes | The uploaded image |
| Content type | Yes | For format detection |

### PII Considerations

Images may contain visible text with employee or organizational data.
This is the core OCR functionality. No additional PII is sent beyond
the image itself.

### Storage / Retention

- OpenAI Vision API retention governed by OpenAI's data usage policies.
- The resulting text is returned to the client and optionally stored
  as session transcript content.

---

## Summary of PII Minimization

| Data Flow | Employee PII Sent | Auth Secrets Sent | Minimization Applied |
|-----------|-------------------|-------------------|---------------------|
| Transcript analysis | No | No | Yes |
| Drift detection | No | No | Yes |
| Audio transcription | Voice/content only | No | N/A (core function) |
| Image OCR | Image content only | No | N/A (core function) |

### Key Design Decisions

1. **No employee identifiers in LLM prompts.** The system prompt is generic
   and identical for all employees. The user message contains only transcript
   text — no name, email, department, or ID.

2. **No authentication secrets anywhere near LLM calls.** API keys are
   loaded from environment variables and passed directly to the provider
   SDK. They are never included in prompts or logged.

3. **Transcript text is the minimum required data.** For behavioral
   analysis, the conversation content is the essential input. Adding
   employee metadata would increase PII exposure without improving
   analysis quality.

4. **Drift detection uses the same minimization.** Cross-session analysis
   includes transcripts and risk scores but no employee identifiers.

### Logging Safeguards

- Raw LLM payloads are never logged.
- Raw LLM responses are never logged (JSON parse failures log only a
  warning message, not the response content).
- LLM provider API keys are never logged.
- Transcript text is never logged.
- Only operational metadata (session IDs, error categories, timing) is
  logged for debugging.

### Remaining Considerations

- Provider-side data retention is outside VooHr's control. Organizations
  using VooHr should be aware that transcript text is processed by
  third-party LLM providers (DeepSeek or OpenAI).
- If transcript data contains highly sensitive employee information,
  the organization should consider their LLM provider's data processing
  agreements.

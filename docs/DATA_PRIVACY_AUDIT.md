# Data Privacy Audit — VooHR / HR Copilot MVP

> Generated: 2026-08-20
> Scope: Employee/user data flow through the complete application stack
> Method: Static code analysis of all Python routes, models, services, and frontend integrations

---

## 1. Data Categories & Flow Summary

### 1.1 Employee PII (employees collection)

| Data Field | Source | Storage | Encrypted | Blind Index | API Return | LLM Exposure | Logging | Browser |
|---|---|---|---|---|---|---|---|---|
| `name` | Frontend form | `encrypted.name` (AES-256-GCM) | Yes | No | Yes (decrypted) | No | No | Yes (decrypted) |
| `email` | Frontend form | `encrypted.email` (AES-256-GCM) | Yes | Yes (`email_hash` HMAC-SHA256) | Yes (decrypted) | No | No | Yes (decrypted) |
| `phone` | Frontend form | `encrypted.phone` (AES-256-GCM) | Yes | No | Yes (decrypted) | No | No | Yes (decrypted) |
| `photo` | Frontend (base64 data-URL) | `photo` (plain) | No | No | Yes | No | No | Yes |

**Risk assessment**: PII fields are properly envelope-encrypted with per-document DEKs wrapped by Cloud KMS. Blind index on email enables login lookups without decryption. Photo stored as base64 data-URL is unencrypted — acceptable for a profile image but should not contain sensitive content. No PII is sent to LLM providers.

### 1.2 Employee Business Data (employees collection)

| Data Field | Source | Storage | Encrypted | API Return | LLM Exposure | Logging | Browser |
|---|---|---|---|---|---|---|---|
| `employee_id` | Auto-generated (EMP001) | Plain | No | Yes | No | No | Yes |
| `department` | Frontend form | Plain | No | Yes | No | No | Yes |
| `position` | Frontend form | Plain | No | Yes | No | No | Yes |
| `employment_type` | Frontend form | Plain | No | Yes | No | No | Yes |
| `work_mode` | Frontend form | Plain | No | Yes | No | No | Yes |
| `joining_date` | Frontend form | Plain | No | Yes | No | No | Yes |
| `status` | Frontend form | Plain | No | Yes | No | No | Yes |
| `signals` | Frontend form | Plain | No | Yes | No | No | Yes |

**Risk assessment**: Business/role data is not PII in the traditional sense and is stored in plain — acceptable for HR operational data. Not sent to LLM providers.

### 1.3 Employee AI-Generated Data (employees collection)

| Data Field | Source | Storage | API Return | LLM Exposure | Logging | Browser |
|---|---|---|---|---|---|---|
| `ai_wellness.score` | LLM analysis | Plain | Yes | Derived | No | Yes |
| `ai_wellness.status` | LLM analysis | Plain | Yes | Derived | No | Yes |
| `ai_wellness.attrition_risk_pct` | LLM analysis | Plain | Yes | Derived | No | Yes |
| `ai_wellness.burnout_index` | LLM analysis | Plain | Yes | Derived | No | Yes |
| `ai_wellness.risk_factors` | LLM analysis | Plain | Yes | Derived | No | Yes |
| `ai_wellness.source_session_id` | Internal ref | Plain | Yes | No | No | Yes |
| `drift_explanation` | LLM drift analysis | Plain | Yes (detail) | Derived | No | Yes |

**Risk assessment**: AI scores are derived from transcript analysis. The raw transcript is sent to the LLM (see sessions), but no PII identifiers are included in the LLM prompt. The LLM receives only the transcript text, not employee names, emails, or org IDs.

### 1.4 User Account Data (users collection)

| Data Field | Source | Storage | Encrypted | Blind Index | API Return | LLM Exposure | Logging | Browser |
|---|---|---|---|---|---|---|---|---|
| `name` | Frontend/Google OAuth | `encrypted.name` (AES-256-GCM) | Yes | No | Yes (`/api/me`) | No | No | Yes (`/api/me`) |
| `email` | Frontend/Google OAuth | `encrypted.email` (AES-256-GCM) | Yes | Yes (`email_hash` HMAC-SHA256) | Yes (`/api/me`) | No | No | Yes (`/api/me`) |
| `google_id` | Google OAuth | Plain | No | No | No | No | No | No |
| `picture` | Google OAuth | Plain | No | No | Yes (`/api/me`) | No | No | Yes (`/api/me`) |
| `password_hash` | Frontend | Argon2id + pepper | No | No | No | No | No | No |
| `totp_secret` | TOTP setup | Plain (base32) | No | No | No | No | No | No |
| `totp_backup_codes` | TOTP setup | SHA-256 hashed | No | No | No | No | No | No |
| `org_id` | Session | Plain (ObjectId) | No | No | Yes (`/api/me`) | No | No | Yes (`/api/me`) |
| `role` | Assignment | Plain | No | No | Yes (`/api/me`) | No | No | Yes (`/api/me`) |

**Risk assessment**: User PII (name, email) is properly encrypted. TOTP secrets and backup codes are never exposed through any API. Password hashes use Argon2id with server pepper. Google OAuth tokens are not stored.

### 1.5 Session/Transcript Data (sessions collection)

| Data Field | Source | Storage | Encrypted | API Return | LLM Exposure | Logging | Browser |
|---|---|---|---|---|---|---|---|
| `transcript.raw` | Frontend audio/text | Plain | No | Yes | **Yes** (DeepSeek/OpenAI) | No | Yes |
| `transcript.edited` | Frontend editor | Plain | No | Yes | **Yes** (DeepSeek/OpenAI) | No | Yes |
| `audio` | Frontend upload | Plain (base64) | No | Yes | No | No | Yes |
| `analysis` | LLM response | Plain | No | Yes | Derived | No | Yes |

**Risk assessment**: Transcripts contain the most sensitive employee data — they are HR conversation content. Transcripts are sent to external LLM providers (DeepSeek or OpenAI) for analysis. No employee PII (name, email) is included in the LLM prompt — only the raw transcript text. The LLM prompt does not contain org_id or employee identifiers. This is the primary data exposure point to third parties.

**LLM provider data handling**:
- **DeepSeek**: Transcripts sent to `api.deepseek.com` via OpenAI-compatible SDK
- **OpenAI (GPT-4o)**: Transcripts sent to OpenAI API for analysis or Whisper STT
- Neither provider receives employee names, emails, or org IDs
- LLM responses are stored as `analysis` field in sessions collection

### 1.6 Active Sessions (active_sessions collection)

| Data Field | Source | Storage | API Return | Logging | Browser |
|---|---|---|---|---|---|
| `session_token` | Login | SHA-256 hash only | No (hash only for comparison) | No | No |
| `user_agent` | Browser header | Plain | Yes (parsed device info) | No | Yes (parsed) |
| `ip` | Request | Plain | Yes | No | Yes |
| `location` | Geo-IP lookup | Plain | Yes | No | Yes |

**Risk assessment**: Session tokens are properly hashed before storage. IP addresses and location data are exposed to the authenticated user in session management UI — acceptable for security visibility.

### 1.7 Notifications (notifications collection)

| Data Field | Source | Storage | API Return | LLM Exposure | Logging | Browser |
|---|---|---|---|---|---|---|
| `headline` | Drift detection | Plain | Yes | No | No | Yes |
| `summary` | Drift detection | Plain | Yes | No | No | Yes |
| `drift_explanation` | LLM drift analysis | Plain | Yes (detail) | Derived | No | Yes |
| `sessions_window` | Drift detection | Plain | Yes (detail) | No | No | Yes |

**Risk assessment**: Notification content is derived from LLM drift analysis. Contains behavioral observations but no direct PII.

### 1.8 Organizations (organizations collection)

| Data Field | Source | Storage | API Return | LLM Exposure | Logging | Browser |
|---|---|---|---|---|---|---|
| `name` | Frontend form | Plain | Yes | No | No | Yes |
| `industry` | Frontend form | Plain | Yes | No | No | Yes |
| `company_size` | Frontend form | Plain | Yes | No | No | Yes |
| `notification_prefs` | Settings | Plain | Yes | No | No | Yes |

**Risk assessment**: Organization metadata is not sensitive. Properly scoped to authenticated users.

---

## 2. Data Flow Diagram

```
Frontend (browser)
  │
  ├── employee form data (name, email, phone, department, etc.)
  │     │
  │     ▼
  ├── POST /api/employees
  │     │
  │     ▼
  ├── employees.py create_employee()
  │     ├── encrypt_fields() → AES-256-GCM (name, email, phone)
  │     ├── blind_index() → HMAC-SHA256 (email → email_hash)
  │     └── MongoDB insert: employees collection
  │
  ├── session data (transcript text)
  │     │
  │     ▼
  ├── POST /api/sessions/<id>/analyze
  │     │
  │     ▼
  ├── sessions.py analyze_session()
  │     ├── decrypt transcript (stored plain)
  │     ├── llm.analyze(transcript) ──────────► DeepSeek/OpenAI API
  │     │                                        (transcript text only, no PII)
  │     ├── analysis result stored in sessions collection
  │     └── ai_wellness scores written to employees collection
  │
  ├── GET /api/employees
  │     │
  │     ▼
  ├── employees.py list_employees()
  │     ├── decrypt_fields() for each employee
  │     └── JSON response with decrypted PII
  │
  └── GET /api/me
        │
        ▼
      api.py me()
        ├── decrypt user PII
        └── JSON response with name, email, org info
```

---

## 3. Sensitive Fields Summary

### Fields Never Exposed Through Any API
- `password_hash` (Argon2id + pepper)
- `totp_secret` (base32 TOTP seed)
- `totp_backup_codes[].code_hash` (SHA-256)
- `pending_totp_secret` / `pending_totp_secret_expires`
- `wrapped_dek` (KMS-wrapped DEK)
- `encrypted` (raw ciphertext blob)
- `email_hash` (blind index, query-only)
- `session_token` (only SHA-256 hash stored)
- `otp_hash` (OTP codes)
- `rate_limits.key` / `rate_limits.ts`
- `counters.seq` (employee ID counter)

### Fields Sent to LLM Providers
- `transcript.raw` / `transcript.edited` — HR conversation text only
- No PII identifiers included in LLM prompts
- LLM prompt does not reference employee name, email, org_id, or employee_id

### Fields in Browser localStorage/sessionStorage
- **None** — no application data stored in browser storage
- `cookieConsent` flag only (non-sensitive preference)

### Fields in URL Query Parameters
- None containing sensitive data
- Employee IDs appear in URL paths (`/api/employees/<id>`) — these are opaque ObjectIds, not PII

### Fields in Logs/Errors
- `logger.exception()` calls log stack traces for debugging
- No PII, session tokens, or sensitive data included in log messages
- Client-facing errors use generic messages (e.g., "Analysis failed. Please try again.")
- Validation errors log field names and types, not values

---

## 4. Deletion Behavior

### Current Employee Deletion
- **DELETE /api/employees/<id>**: Hard-deletes the employee document from MongoDB
- **Scope**: Organization-scoped (query includes `org_id`)
- **Related data NOT deleted**: sessions, notifications, drift_explanation, ai_wellness
- **Risk**: Orphaned sessions and notifications remain in the database after employee deletion

### Current Session Deletion
- **DELETE /api/sessions/<id>**: Hard-deletes the session document
- **Scope**: Organization-scoped

### Current User/Account Deletion
- **Not implemented** — no user deletion endpoint exists

---

## 5. Retention Behavior

| Collection | TTL/Index | Auto-Cleanup |
|---|---|---|
| `rate_limits.expire_at` | TTL index | Yes (MongoDB auto-delete) |
| `otp_verifications.expires_at` | Manual cleanup at startup | Yes (`_cleanup_expired_pending_totp`) |
| `active_sessions` | No TTL | No — relies on app-level revocation |
| `employees` | No TTL | No — manual deletion only |
| `sessions` | No TTL | No — manual deletion only |
| `notifications` | No TTL | No — manual deletion only |

---

## 6. Encryption Architecture

### Field-Level Envelope Encryption
1. Each document gets a fresh 32-byte AES DEK (`os.urandom(32)`)
2. Each PII field encrypted with AES-256-GCM using unique 12-byte nonce
3. DEK wrapped by Google Cloud KMS (`wrap_data_key`)
4. Stored as: `{field: base64(iv + authTag + ciphertext)}` + `wrapped_dek`

### Blind Index (HMAC-SHA256)
- Used for email lookups (login, signup, duplicate detection)
- `email_hash = HMAC-SHA256(lowercased_email, HASH_INDEX_SECRET)`
- Enables MongoDB queries without decryption

### Password Hashing
- Argon2id with server-side `PASSWORD_PEPPER`
- Strength check: >=8 chars, must mix letters and digits

---

## 7. Tenant Isolation

All data queries enforce organization scoping:
- `employees`: `{"org_id": ObjectId(org_id)}`
- `sessions`: `{"org_id": ObjectId(org_id)}`
- `notifications`: `{"org_id": ObjectId(org_id)}`
- `active_sessions`: `{"user_id": ObjectId(user_id)}` (user-level, not org-level)

Authentication middleware (`_require_auth`) validates user_id + org_id from session before any data access.

---

## 8. Recommendations

### Critical (Phase 1)
1. **Cascade employee deletion**: When an employee is deleted, related sessions and notifications should be cleaned up to prevent orphaned data.

### Important (Phase 2)
2. **User account deletion**: Implement account deletion endpoint that cleans up user data, active sessions, and handles employee ownership transfer.
3. **Session data retention**: Consider auto-expiring old session transcripts after a configurable retention period.
4. **Active session cleanup**: Add TTL or periodic cleanup for stale active_sessions records.

### Low Priority
5. **Photo encryption**: Employee photos (base64 data-URLs) are stored unencrypted — acceptable for profile images but consider encryption if photos contain sensitive content.
6. **TOTP secret rotation**: Consider periodic TOTP secret rotation for long-lived accounts.

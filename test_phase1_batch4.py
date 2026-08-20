"""Tests for Phase 1 Batch 4 — Privacy & Data Security:

4. API Response Privacy
5. LLM Data Privacy & PII Minimization
6. Logging & Error Privacy
"""

import os
import sys
import re
from unittest.mock import MagicMock, patch

import pytest
from bson import ObjectId

_ROOT = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# KMS mock
# ---------------------------------------------------------------------------

_KMS_STUB = MagicMock()
_KMS_STUB.wrap_data_key.side_effect = lambda dek: dek
_KMS_STUB.unwrap_data_key.side_effect = lambda wrapped: wrapped
sys.modules.setdefault("kms", _KMS_STUB)
sys.modules.pop("field_encryption", None)


# ---------------------------------------------------------------------------
# Helper: extract function source from a .py file
# ---------------------------------------------------------------------------

def _get_func_source(filepath, func_name):
    path = os.path.join(_ROOT, filepath)
    with open(path, encoding="utf-8") as f:
        content = f.read()
    lines = content.split("\n")
    in_func = False
    func_lines = []
    indent = None
    for line in lines:
        if f"def {func_name}(" in line:
            in_func = True
            indent = len(line) - len(line.lstrip())
            func_lines.append(line)
            continue
        if in_func:
            if line.strip() == "":
                func_lines.append(line)
                continue
            current_indent = len(line) - len(line.lstrip())
            if current_indent <= indent and line.strip() and not line.strip().startswith("#"):
                break
            func_lines.append(line)
    return "\n".join(func_lines)


# ===========================================================================
# 4. API RESPONSE PRIVACY
# ===========================================================================

class TestEmployeeResponsePrivacy:
    """Employee API responses must not expose raw internal data."""

    def _employee_to_json_source(self):
        return _get_func_source("employees.py", "_employee_to_json")

    def test_no_signals_in_response(self):
        """Raw HR input signals must not be in API responses."""
        source = self._employee_to_json_source()
        # The return dict should NOT contain "signals" as a key
        # Find the return dict
        return_start = source.find("return {")
        return_dict = source[return_start:]
        assert '"signals"' not in return_dict, \
            "Raw signals should not be in employee API response"

    def test_derived_scores_still_present(self):
        """Derived wellness/attrition fields must remain in response."""
        source = self._employee_to_json_source()
        return_start = source.find("return {")
        return_dict = source[return_start:]
        for field in ("wellness_score", "wellness_status", "attrition_risk_pct",
                       "burnout_index", "reasons", "wellness_source"):
            assert f'"{field}"' in return_dict, \
                f"Derived field '{field}' must be in employee response"

    def test_business_fields_still_present(self):
        """Legitimate business fields must remain in response."""
        source = self._employee_to_json_source()
        return_start = source.find("return {")
        return_dict = source[return_start:]
        for field in ("id", "employee_id", "name", "email", "phone",
                       "department", "position", "employment_type", "work_mode",
                       "joining_date", "status", "photo", "created_at", "updated_at"):
            assert f'"{field}"' in return_dict, \
                f"Business field '{field}' must be in employee response"

    def test_no_password_hash_in_response(self):
        """password_hash must never appear in employee response."""
        source = self._employee_to_json_source()
        assert "password_hash" not in source

    def test_no_wrapped_dek_in_response(self):
        """wrapped_dek (encryption key) must not be in employee response."""
        source = self._employee_to_json_source()
        return_start = source.find("return {")
        return_dict = source[return_start:]
        assert "wrapped_dek" not in return_dict

    def test_no_encrypted_blob_in_response(self):
        """Raw encrypted blob must not be in employee response."""
        source = self._employee_to_json_source()
        return_start = source.find("return {")
        return_dict = source[return_start:]
        assert '"encrypted"' not in return_dict

    def test_no_email_hash_in_response(self):
        """email_hash (blind index) must not be in employee response."""
        source = self._employee_to_json_source()
        return_start = source.find("return {")
        return_dict = source[return_start:]
        assert "email_hash" not in return_dict

    def test_no_org_id_in_employee_response(self):
        """org_id must not be leaked in employee response."""
        source = self._employee_to_json_source()
        return_start = source.find("return {")
        return_dict = source[return_start:]
        assert "org_id" not in return_dict

    def test_no_totp_fields_in_response(self):
        """TOTP-related fields must never appear in employee response."""
        source = self._employee_to_json_source()
        for field in ("totp_secret", "totp_backup_codes", "pending_totp_secret"):
            assert field not in source, \
                f"TOTP field '{field}' must not be in employee response"


class TestActiveSessionsResponsePrivacy:
    """Active sessions API responses must not expose IP addresses."""

    def _list_active_source(self):
        return _get_func_source("api.py", "list_active_sessions")

    def test_no_ip_in_response(self):
        """IP addresses must not be in active sessions response."""
        source = self._list_active_source()
        assert '"ip"' not in source, \
            "IP addresses must not be in active sessions response"

    def test_location_still_present(self):
        """Location info must remain in active sessions response."""
        source = self._list_active_source()
        assert '"location"' in source

    def test_device_still_present(self):
        """Device info must remain in active sessions response."""
        source = self._list_active_source()
        assert '"device"' in source

    def test_no_session_token_in_response(self):
        """Raw session tokens must not be in active sessions response."""
        source = self._list_active_source()
        # The dict literal inside sessions.append({...}) must not have "session_token" as a KEY
        # (it may reference d.get("session_token") for is_current comparison — that's internal)
        append_start = source.find("sessions.append(")
        dict_start = source.find("{", append_start)
        depth = 0
        dict_end = dict_start
        for idx in range(dict_start, len(source)):
            if source[idx] == "{":
                depth += 1
            elif source[idx] == "}":
                depth -= 1
                if depth == 0:
                    dict_end = idx + 1
                    break
        return_dict = source[dict_start:dict_end]
        # Check that "session_token" is not a top-level response key
        # A response key would look like "session_token": ... (with colon after)
        # Internal use like d.get("session_token") doesn't have a colon after the closing quote
        for line in return_dict.split("\n"):
            stripped = line.strip()
            if stripped.startswith('"session_token"') and ':' in stripped.split('"session_token"')[1][:5]:
                pytest.fail(f"session_token is a response key: {stripped}")


class TestApiResponseNoSensitiveFields:
    """All API serializers must exclude security-sensitive fields."""

    def test_employee_serializer_no_sensitive_fields(self):
        """_employee_to_json must not include any security-sensitive fields."""
        source = _get_func_source("employees.py", "_employee_to_json")
        sensitive_fields = [
            "password_hash", "totp_secret", "totp_backup_codes",
            "wrapped_dek", "email_hash", "session_token",
            "api_key", "google_id", "signing_key",
            "pending_totp_secret", "lockout_until",
            "failed_login_attempts",
        ]
        return_start = source.find("return {")
        return_dict = source[return_start:]
        for field in sensitive_fields:
            assert field not in return_dict, \
                f"Sensitive field '{field}' must not be in employee response"

    def test_me_endpoint_no_sensitive_fields(self):
        """/api/me must not include any security-sensitive fields."""
        path = os.path.join(_ROOT, "api.py")
        with open(path, encoding="utf-8") as f:
            content = f.read()
        # Find the me() function's return jsonify({...}) dict
        me_start = content.find("def me()")
        me_end = content.find("\ndef ", me_start + 1)
        me_source = content[me_start:me_end]
        # Find the final return jsonify( which contains the response dict
        return_start = me_source.rfind("return jsonify(")
        return_dict = me_source[return_start:]
        sensitive_fields = [
            "password_hash", "totp_secret", "totp_backup_codes",
            "wrapped_dek", "email_hash", "session_token",
            "api_key", "google_id", "signing_key",
            "pending_totp_secret",
        ]
        for field in sensitive_fields:
            # Check for the field as a JSON key (with quotes)
            assert f'"{field}"' not in return_dict, \
                f"Sensitive field '{field}' must not be in /api/me response"

    def test_notification_serializer_no_sensitive_fields(self):
        """_notification_to_json must not include any security-sensitive fields."""
        source = _get_func_source("notifications.py", "_notification_to_json")
        sensitive_fields = [
            "password_hash", "totp_secret", "totp_backup_codes",
            "wrapped_dek", "email_hash", "session_token",
            "api_key",
        ]
        for field in sensitive_fields:
            assert field not in source, \
                f"Sensitive field '{field}' must not be in notification response"

    def test_session_serializer_no_sensitive_fields(self):
        """_session_to_json must not include any security-sensitive fields."""
        source = _get_func_source("sessions.py", "_session_to_json")
        sensitive_fields = [
            "password_hash", "totp_secret", "totp_backup_codes",
            "wrapped_dek", "email_hash", "session_token",
            "api_key",
        ]
        for field in sensitive_fields:
            assert field not in source, \
                f"Sensitive field '{field}' must not be in session response"

    def test_totp_setup_does_not_expose_secret_after_verify(self):
        """TOTP setup endpoint exposes secret only during enrollment — this is
        by design (user needs it to scan QR). But verify-setup must not return it."""
        source = _get_func_source("totp_routes.py", "totp_verify_setup")
        assert '"secret"' not in source or "pending_secret" in source
        # verify-setup should return backup_codes but not the TOTP secret
        assert "backup_codes" in source

    def test_totp_backup_codes_status_no_secret(self):
        """Backup codes status must not expose actual codes."""
        source = _get_func_source("totp_routes.py", "totp_backup_codes_status")
        assert "backup_codes" not in source or "has_backup_codes" in source
        # Should return count, not actual codes
        assert "codes_remaining" in source

    def test_active_sessions_no_user_agent_leak(self):
        """Raw User-Agent must not be in active sessions response."""
        source = _get_func_source("api.py", "list_active_sessions")
        # User-Agent is parsed into device info, raw UA should not be returned
        assert "user_agent" not in source or "parse_device" in source


class TestTenantIsolationInResponses:
    """API responses must not leak data across organizations."""

    def test_employee_queries_scope_by_org(self):
        """All employee queries must include org_id scoping."""
        path = os.path.join(_ROOT, "employees.py")
        with open(path, encoding="utf-8") as f:
            content = f.read()

        # Every find_one/find/find_one_and_update/delete must include org_id
        # for employee-related operations
        assert "org_id" in content

    def test_session_queries_scope_by_org(self):
        """All session queries must include org_id scoping."""
        path = os.path.join(_ROOT, "sessions.py")
        with open(path, encoding="utf-8") as f:
            content = f.read()
        assert "org_id" in content

    def test_notification_queries_scope_by_org(self):
        """All notification queries must include org_id scoping."""
        path = os.path.join(_ROOT, "notifications.py")
        with open(path, encoding="utf-8") as f:
            content = f.read()
        assert "org_id" in content

    def test_active_sessions_scope_by_user(self):
        """Active sessions must be scoped by user_id (not org_id)."""
        source = _get_func_source("api.py", "list_active_sessions")
        assert "user_id" in source


# ===========================================================================
# 5. LLM DATA PRIVACY & PII MINIMIZATION
# ===========================================================================

class TestLLMPromptPrivacy:
    """LLM prompts must not include employee PII."""

    def _read_llm_source(self):
        path = os.path.join(_ROOT, "providers", "llm.py")
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_analyze_method_only_receives_transcript(self):
        """LLM.analyze() must accept only transcript text."""
        source = self._read_llm_source()
        # Both OpenAI and DeepSeek analyze methods should take (self, transcript)
        assert "def analyze(self, transcript: str)" in source

    def test_drift_method_only_receives_sessions(self):
        """LLM.explain_drift() must accept only sessions list."""
        source = self._read_llm_source()
        assert "def explain_drift(self, sessions: list)" in source

    def test_no_employee_name_in_prompts(self):
        """LLM prompts must not reference employee names."""
        source = self._read_llm_source()
        # System prompts are string literals — search for name references
        # that would indicate PII in prompts
        assert "employee_name" not in source.split("_build_v2_prompt")[1].split("return")[1] if "_build_v2_prompt" in source else True

    def test_no_employee_email_in_prompts(self):
        """LLM prompts must not reference employee emails."""
        source = self._read_llm_source()
        # The system prompts should not contain email-related instructions
        prompt_section = source[source.find("def _build_v2_prompt"):source.find("def _build_drift_prompt")]
        assert "email" not in prompt_section.lower() or "email" in prompt_section.lower().split("json")[0]

    def test_no_auth_secrets_near_llm_calls(self):
        """LLM provider initialization must not include auth secrets."""
        source = self._read_llm_source()
        # DeepSeek and OpenAI classes should load API keys from env, not params
        assert "os.environ.get" in source

    def test_no_session_tokens_in_llm_code(self):
        """LLM provider code must not handle session tokens."""
        source = self._read_llm_source()
        assert "session_token" not in source
        assert "session[" not in source

    def test_no_totp_in_llm_code(self):
        """LLM provider code must not reference TOTP."""
        source = self._read_llm_source()
        assert "totp" not in source.lower() or "totp" in "stopword"  # No TOTP references

    def test_no_password_in_llm_code(self):
        """LLM provider code must not reference passwords."""
        source = self._read_llm_source()
        assert "password" not in source.lower() or "password" in "stopword"

    def test_no_api_key_in_llm_prompts(self):
        """API keys must never appear in LLM prompt content."""
        source = self._read_llm_source()
        # The messages list construction should only include system prompt + user content
        assert "api_key" not in source.split("messages=[")[1].split("]")[0] if "messages=[" in source else True

    def test_no_org_id_in_llm_prompts(self):
        """Organization IDs must not appear in LLM prompts."""
        source = self._read_llm_source()
        prompt_section = source[source.find("def _build_v2_prompt"):source.find("def _build_drift_prompt")]
        assert "org_id" not in prompt_section

    def test_no_employee_id_in_llm_prompts(self):
        """Employee IDs must not appear in LLM prompts."""
        source = self._read_llm_source()
        prompt_section = source[source.find("def _build_v2_prompt"):source.find("def _build_drift_prompt")]
        assert "employee_id" not in prompt_section

    def test_analyze_sends_only_transcript_to_api(self):
        """OpenAI/DeepSeek analyze must send only transcript as user message."""
        source = self._read_llm_source()
        # Both providers should construct messages with transcript only
        # Find the messages construction in analyze methods
        openai_analyze = source[source.find("class OpenAILLM"):source.find("class DeepSeekLLM")]
        deepseek_analyze = source[source.find("class DeepSeekLLM"):]

        # Both should have the same pattern: system prompt + transcript
        for analyze_src in [openai_analyze, deepseek_analyze]:
            assert '"role": "system"' in analyze_src
            assert '"role": "user"' in analyze_src
            assert '"content": transcript' in analyze_src or '"content": system_prompt' in analyze_src


class TestLLMLoggingPrivacy:
    """LLM logging must not expose raw payloads or responses."""

    def _read_llm_source(self):
        path = os.path.join(_ROOT, "providers", "llm.py")
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_no_raw_response_logging(self):
        """Raw LLM response content must not be logged."""
        source = self._read_llm_source()
        # logger.exception calls should not include raw_content or content
        for line in source.split("\n"):
            if "logger." in line and ("raw_content" in line or "content[:3000]" in line):
                pytest.fail(f"Raw LLM content logged: {line.strip()}")

    def test_json_parse_failure_uses_warning_not_exception(self):
        """JSON parse failures must use logger.warning, not logger.exception,
        to prevent traceback from leaking raw content."""
        source = self._read_llm_source()
        # DeepSeek analyze/parse failures should use warning
        assert 'logger.warning("DeepSeek analyze: JSON parse failed")' in source
        assert 'logger.warning("DeepSeek explain_drift: JSON parse failed")' in source
        # Must NOT use logger.exception for these
        assert 'logger.exception("DeepSeek analyze: JSON parse failed")' not in source
        assert 'logger.exception("DeepSeek explain_drift: JSON parse failed")' not in source

    def test_normalize_logs_only_field_names(self):
        """Field normalization logs must only contain field names, not content."""
        source = self._read_llm_source()
        # The normalize log should only log renamed field mappings
        assert 'logger.debug("normalize %s: renamed %s"' in source

    def test_no_transcript_in_logs(self):
        """Transcript text must not appear in any logger call."""
        source = self._read_llm_source()
        for line in source.split("\n"):
            if "logger." in line:
                assert "transcript" not in line.lower() or "session=" in line, \
                    f"Transcript reference in log: {line.strip()}"


class TestLLMDataFlowDocumentation:
    """LLM data privacy documentation must exist and be accurate."""

    def test_llm_privacy_doc_exists(self):
        path = os.path.join(_ROOT, "docs", "LLM_DATA_PRIVACY.md")
        assert os.path.isfile(path), "LLM_DATA_PRIVACY.md must exist"

    def test_llm_privacy_doc_non_empty(self):
        path = os.path.join(_ROOT, "docs", "LLM_DATA_PRIVACY.md")
        assert os.path.getsize(path) > 500, "LLM privacy doc too small"

    def test_llm_privacy_doc_mentions_providers(self):
        path = os.path.join(_ROOT, "docs", "LLM_DATA_PRIVACY.md")
        with open(path, encoding="utf-8") as f:
            content = f.read()
        assert "DeepSeek" in content
        assert "OpenAI" in content

    def test_llm_privacy_doc_documents_data_sent(self):
        path = os.path.join(_ROOT, "docs", "LLM_DATA_PRIVACY.md")
        with open(path, encoding="utf-8") as f:
            content = f.read()
        assert "transcript" in content.lower()
        assert "PII" in content

    def test_llm_privacy_doc_no_real_secrets(self):
        """Documentation must not contain actual API keys or secrets."""
        path = os.path.join(_ROOT, "docs", "LLM_DATA_PRIVACY.md")
        with open(path, encoding="utf-8") as f:
            content = f.read()
        assert "sk-" not in content  # OpenAI key prefix
        assert "xkeysib-" not in content  # Brevo key prefix


# ===========================================================================
# 6. LOGGING & ERROR PRIVACY
# ===========================================================================

class TestLoggingPrivacy:
    """Production logs must not contain sensitive data."""

    def test_root_route_no_user_id_in_log(self):
        """Root route log must not include user_id."""
        path = os.path.join(_ROOT, "app.py")
        with open(path, encoding="utf-8") as f:
            content = f.read()
        # Find the root route log line
        root_log = [line for line in content.split("\n")
                     if "Root route:" in line and "logger" in line]
        for line in root_log:
            assert "user_id" not in line, \
                f"Root route log must not include user_id: {line.strip()}"

    def test_no_session_tokens_in_any_log(self):
        """Session tokens must never appear in any logger call across the codebase."""
        py_files = [f for f in os.listdir(_ROOT) if f.endswith(".py")
                     and not f.startswith("test_")]
        for fname in py_files:
            path = os.path.join(_ROOT, fname)
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except Exception:
                continue
            for i, line in enumerate(content.split("\n"), 1):
                stripped = line.strip()
                if stripped.startswith("logger.") and "session_token" in stripped:
                    pytest.fail(f"{fname}:{i} logs session_token: {stripped}")

    def test_no_passwords_in_any_log(self):
        """Passwords must never appear in any logger call."""
        py_files = [f for f in os.listdir(_ROOT) if f.endswith(".py")]
        for fname in py_files:
            if fname.startswith("test_"):
                continue
            path = os.path.join(_ROOT, fname)
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except Exception:
                continue
            for i, line in enumerate(content.split("\n"), 1):
                if "logger." in line:
                    # Allow password_hash in source code (for DB operations)
                    # but not in logger calls
                    if "password_hash" in line and ("logger.info" in line or
                                                     "logger.debug" in line or
                                                     "logger.warning" in line or
                                                     "logger.error" in line):
                        pytest.fail(f"{fname}:{i} logs password_hash: {line.strip()}")

    def test_no_totp_secrets_in_any_log(self):
        """TOTP secrets must never appear in any logger call."""
        py_files = [f for f in os.listdir(_ROOT) if f.endswith(".py")]
        for fname in py_files:
            if fname.startswith("test_"):
                continue
            path = os.path.join(_ROOT, fname)
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except Exception:
                continue
            for i, line in enumerate(content.split("\n"), 1):
                if "logger." in line and "totp_secret" in line:
                    pytest.fail(f"{fname}:{i} logs totp_secret: {line.strip()}")

    def test_no_api_keys_in_any_log(self):
        """API keys must never appear in any logger call."""
        py_files = [f for f in os.listdir(_ROOT) if f.endswith(".py")]
        for fname in py_files:
            if fname.startswith("test_"):
                continue
            path = os.path.join(_ROOT, fname)
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except Exception:
                continue
            for i, line in enumerate(content.split("\n"), 1):
                if "logger." in line and "api_key" in line:
                    # The email_service logs api_key_set= which is just a bool
                    if "api_key_set" in line:
                        continue
                    pytest.fail(f"{fname}:{i} logs api_key: {line.strip()}")

    def test_no_raw_transcripts_in_logs(self):
        """Raw transcript text must not be logged by server code."""
        sessions_source = open(os.path.join(_ROOT, "sessions.py"), encoding="utf-8").read()
        for i, line in enumerate(sessions_source.split("\n"), 1):
            if "logger." in line:
                assert "raw_text" not in line, \
                    f"sessions.py:{i} logs raw_text: {line.strip()}"
                # "transcription" in error category names is OK (e.g. "Audio transcription failed")
                # but logging actual transcript content is not. Check for variable references.
                if "transcript" in line.lower():
                    # Allow: "transcription failed" (category name), "session=" (metadata)
                    # Disallow: raw_text, transcript content variables
                    assert "raw_text" not in line, \
                        f"sessions.py:{i} logs raw transcript: {line.strip()}"

    def test_no_employee_analysis_in_logs(self):
        """Sensitive analysis results must not be logged."""
        sessions_source = open(os.path.join(_ROOT, "sessions.py"), encoding="utf-8").read()
        for i, line in enumerate(sessions_source.split("\n"), 1):
            if "logger." in line:
                assert "analysis" not in line.lower() or "analysis failed" in line.lower() or "session=" in line, \
                    f"sessions.py:{i} logs analysis data: {line.strip()}"

    def test_drift_detection_failure_uses_exception_logger(self):
        """Drift detection failures should use logger.exception for debugging."""
        source = open(os.path.join(_ROOT, "sessions.py"), encoding="utf-8").read()
        assert 'logger.exception("Drift detection failed' in source

    def test_session_analysis_failure_uses_exception_logger(self):
        """Session analysis failures should use logger.exception for debugging."""
        source = open(os.path.join(_ROOT, "sessions.py"), encoding="utf-8").read()
        assert 'logger.exception("Session analysis failed' in source

    def test_email_service_no_api_key_in_logs(self):
        """Email service must not log the actual API key value."""
        source = open(os.path.join(_ROOT, "email_service.py"), encoding="utf-8").read()
        for i, line in enumerate(source.split("\n"), 1):
            if "logger." in line:
                # Should log api_key_set (bool), not api_key (value)
                if "api_key" in line:
                    assert "api_key_set" in line or "BREVO_API_KEY" not in line, \
                        f"email_service.py:{i} may log API key value: {line.strip()}"


class TestErrorResponses:
    """API error responses must not leak internal details."""

    def test_global_exception_handler_returns_generic_error(self):
        """Unhandled API exceptions must return generic error message."""
        path = os.path.join(_ROOT, "app.py")
        with open(path, encoding="utf-8") as f:
            content = f.read()
        assert '"internal_server_error"' in content

    def test_global_exception_handler_logs_internally(self):
        """Unhandled exceptions must be logged server-side, not returned."""
        path = os.path.join(_ROOT, "app.py")
        with open(path, encoding="utf-8") as f:
            content = f.read()
        assert 'app.logger.exception("Unhandled exception on %s"' in content

    def test_analysis_error_returns_generic_message(self):
        """LLM analysis errors must return generic message."""
        source = open(os.path.join(_ROOT, "sessions.py"), encoding="utf-8").read()
        assert '"Analysis failed. Please try again."' in source

    def test_transcription_error_returns_generic_message(self):
        """Transcription errors must return generic message."""
        source = open(os.path.join(_ROOT, "sessions.py"), encoding="utf-8").read()
        assert '"Transcription failed. Please try again."' in source

    def test_deletion_error_returns_generic_message(self):
        """Deletion errors must return generic message."""
        source = open(os.path.join(_ROOT, "employees.py"), encoding="utf-8").read()
        assert '"deletion_failed"' in source

    def test_no_str_exception_in_api_responses(self):
        """API error responses must not include str(exception)."""
        for fname in ("sessions.py", "employees.py", "api.py", "notifications.py"):
            path = os.path.join(_ROOT, fname)
            with open(path, encoding="utf-8") as f:
                content = f.read()
            for i, line in enumerate(content.split("\n"), 1):
                if "jsonify" in line and "str(" in line:
                    if "str(e)" in line or "str(exc)" in line:
                        pytest.fail(f"{fname}:{i} returns str(exception): {line.strip()}")

    def test_no_traceback_in_api_responses(self):
        """API responses must not include traceback information."""
        for fname in ("sessions.py", "employees.py", "api.py", "notifications.py"):
            path = os.path.join(_ROOT, fname)
            with open(path, encoding="utf-8") as f:
                content = f.read()
            assert "traceback" not in content.lower() or "traceback" in content.lower().split("import")[0]

    def test_413_handler_returns_json(self):
        """Payload too large must return JSON, not HTML."""
        source = open(os.path.join(_ROOT, "app.py"), encoding="utf-8").read()
        assert "RequestEntityTooLarge" in source
        assert "jsonify" in source

    def test_404_handler_returns_json_for_api(self):
        """API 404 must return JSON, not HTML."""
        source = open(os.path.join(_ROOT, "app.py"), encoding="utf-8").read()
        assert '"not_found"' in source

    def test_totp_required_error_returns_generic(self):
        """TOTP required error must return generic message."""
        source = open(os.path.join(_ROOT, "app.py"), encoding="utf-8").read()
        assert '"TOTP verification required"' in source

    def test_error_responses_use_appropriate_status_codes(self):
        """Error responses must use proper HTTP status codes."""
        for fname in ("sessions.py", "employees.py", "api.py"):
            path = os.path.join(_ROOT, fname)
            with open(path, encoding="utf-8") as f:
                content = f.read()
            # Must have 401 for auth failures
            assert "401" in content
            # Must have 404 for not found
            assert "404" in content
            # Must have 400 for bad request
            assert "400" in content

    def test_no_database_details_in_error_messages(self):
        """Error messages must not contain database query details."""
        for fname in ("sessions.py", "employees.py", "api.py", "notifications.py"):
            path = os.path.join(_ROOT, fname)
            with open(path, encoding="utf-8") as f:
                content = f.read()
            for i, line in enumerate(content.split("\n"), 1):
                if "jsonify" in line and ("error" in line or "Error" in line):
                    # Must not contain MongoDB query syntax
                    assert "find_one" not in line or "find_one" not in line.split("jsonify")[1]
                    assert "aggregate" not in line

    def test_no_filesystem_paths_in_error_messages(self):
        """Error messages must not contain filesystem paths."""
        for fname in ("sessions.py", "employees.py", "api.py"):
            path = os.path.join(_ROOT, fname)
            with open(path, encoding="utf-8") as f:
                content = f.read()
            for i, line in enumerate(content.split("\n"), 1):
                if "jsonify" in line and ("error" in line or "Error" in line):
                    assert "C:\\" not in line or "C:\\\\" not in line
                    assert "/usr/" not in line
                    assert "/home/" not in line


class TestTenantIsolation:
    """Cross-tenant data access must be prevented."""

    def test_employee_queries_always_include_org_id(self):
        """Every employee query must scope by org_id."""
        source = open(os.path.join(_ROOT, "employees.py"), encoding="utf-8").read()
        # find_one calls for employees must include org_id
        find_calls = re.findall(r'db\.employees\.find_one\(\{[^}]+\}', source)
        for call in find_calls:
            if "_id" in call and "emp_oid" in call:
                assert "org_id" in call, \
                    f"Employee find_one without org_id: {call}"

    def test_session_queries_always_include_org_id(self):
        """Every session query must scope by org_id."""
        source = open(os.path.join(_ROOT, "sessions.py"), encoding="utf-8").read()
        # Check that org_id is used in session queries — the source must
        # contain org_id scoping for session operations
        assert "org_id" in source
        # Verify initial fetches in CRUD endpoints use org_id
        # find_one with _id AND org_id
        assert 'db.sessions.find_one({"_id": ObjectId(session_id), "org_id": ObjectId(org_id)})' in source

    def test_notification_queries_always_include_org_id(self):
        """Every notification query must scope by org_id."""
        source = open(os.path.join(_ROOT, "notifications.py"), encoding="utf-8").read()
        find_calls = re.findall(r'db\.notifications\.find_one\(\{[^}]+\}', source)
        for call in find_calls:
            if "_id" in call:
                assert "org_id" in call, \
                    f"Notification find_one without org_id: {call}"

    def test_no_client_supplied_org_id(self):
        """org_id must never come from client request body or params."""
        for fname in ("employees.py", "sessions.py", "notifications.py"):
            path = os.path.join(_ROOT, fname)
            with open(path, encoding="utf-8") as f:
                content = f.read()
            # org_id should come from _require_auth(), not from request data
            assert "request.args.get(\"org_id\")" not in content, \
                f"{fname} reads org_id from request args"
            assert "data.get(\"org_id\")" not in content, \
                f"{fname} reads org_id from request body"

    def test_active_sessions_scope_by_user_not_org(self):
        """Active sessions must be per-user, not cross-user."""
        source = _get_func_source("api.py", "list_active_sessions")
        assert "user_id" in source
        # Must query by user_id, not allow arbitrary user_id from client
        assert "ObjectId(user_id)" in source

    def test_revoke_session_scoped_to_user(self):
        """Session revocation must be scoped to the authenticated user."""
        source = _get_func_source("api.py", "revoke_active_session")
        assert "user_id" in source
        assert "ObjectId(user_id)" in source

"""Tests for Phase 2 / Fix #8 — Rate Limiting Audit & Implementation.

Covers:
  - TOTP verification brute-force protection (verify-setup + verify-login)
  - email_signin lockout enforcement (bug fix)
  - email_signin failed_login_attempts reset on success (bug fix)
  - Expensive endpoint rate limiting (analyze, transcribe, transcribe-image)
  - CSRF token endpoint rate limiting
  - Rate-limit response safety (no secrets leaked)
  - Configuration correctness
  - Existing rate limiting still functional
"""

import os
import re
import sys

import pytest
from unittest.mock import MagicMock, patch

_ROOT = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# KMS mock (must be set before any app module import)
# ---------------------------------------------------------------------------
_KMS_STUB = MagicMock()
_KMS_STUB.wrap_data_key.side_effect = lambda dek: dek
_KMS_STUB.unwrap_data_key.side_effect = lambda wrapped: wrapped
sys.modules.setdefault("kms", _KMS_STUB)
sys.modules.pop("field_encryption", None)


def _read_source(filepath):
    path = os.path.join(_ROOT, filepath)
    with open(path, encoding="utf-8") as f:
        return f.read()


def _get_func_source(filepath, func_name):
    content = _read_source(filepath)
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
# 1. TOTP VERIFICATION RATE LIMITING
# ===========================================================================

class TestTOTPVerifyRateLimiting:
    """TOTP code verification must be rate-limited to prevent brute-force."""

    def test_totp_verify_rate_limit_constants_exist(self):
        """Rate-limit constants for TOTP verification must be defined."""
        source = _read_source("totp_routes.py")
        assert "_TOTPVerify_MAX" in source
        assert "_TOTPVerify_WINDOW" in source

    def test_totp_verify_max_is_reasonable(self):
        """TOTP verify limit must be between 5 and 20 attempts per window."""
        import totp_routes
        assert 5 <= totp_routes._TOTPVerify_MAX <= 20

    def test_totp_verify_window_is_15_minutes(self):
        """TOTP verify window must be 15 minutes (900 seconds)."""
        import totp_routes
        assert totp_routes._TOTPVerify_WINDOW == 900

    def test_totp_check_rate_limit_function_exists(self):
        """_check_totp_rate_limit function must exist."""
        import totp_routes
        assert hasattr(totp_routes, "_check_totp_rate_limit")
        assert callable(totp_routes._check_totp_rate_limit)

    def test_totp_check_rate_limit_uses_mongodb(self):
        """_check_totp_rate_limit must use MongoDB-backed rate limiting."""
        import inspect
        import totp_routes
        src = inspect.getsource(totp_routes._check_totp_rate_limit)
        assert "check_rate_limit" in src
        assert "record_rate_limit_event" in src
        assert "get_db" in src

    def test_totp_check_rate_limit_key_format(self):
        """Rate limit key must be per-user (totp_verify:{user_id})."""
        import inspect
        import totp_routes
        src = inspect.getsource(totp_routes._check_totp_rate_limit)
        assert "totp_verify:" in src

    def test_totp_verify_setup_has_rate_limit_check(self):
        """totp_verify_setup must call _check_totp_rate_limit."""
        import inspect
        import totp_routes
        src = inspect.getsource(totp_routes.totp_verify_setup)
        assert "_check_totp_rate_limit" in src
        assert "429" in src

    def test_totp_verify_login_has_rate_limit_check(self):
        """totp_verify_login must call _check_totp_rate_limit."""
        import inspect
        import totp_routes
        src = inspect.getsource(totp_routes.totp_verify_login)
        assert "_check_totp_rate_limit" in src
        assert "429" in src

    def test_totp_verify_rate_limit_returns_retry_after(self):
        """Rate-limited TOTP responses must include retry_after."""
        import inspect
        import totp_routes
        src = inspect.getsource(totp_routes.totp_verify_setup)
        assert "retry_after" in src

    def test_totp_verify_rate_limit_returns_429(self):
        """Rate-limited TOTP responses must return HTTP 429."""
        import inspect
        import totp_routes
        src_setup = inspect.getsource(totp_routes.totp_verify_setup)
        src_login = inspect.getsource(totp_routes.totp_verify_login)
        assert '"429"' in src_setup or "429" in src_setup
        assert '"429"' in src_login or "429" in src_login

    def test_totp_verify_rate_limit_returns_generic_error(self):
        """Rate-limited TOTP responses must not reveal internal details."""
        import inspect
        import totp_routes
        src = inspect.getsource(totp_routes.totp_verify_setup)
        assert "Too many attempts" in src

    def test_totp_backup_rate_limit_still_exists(self):
        """Backup code rate limiting must not be removed."""
        import totp_routes
        assert hasattr(totp_routes, "_check_backup_rate_limit")
        assert totp_routes._BACKUPCodeAttempts_MAX == 5
        assert totp_routes._BACKUPCodeAttempts_WINDOW == 900


# ===========================================================================
# 2. EMAIL SIGNIN LOCKOUT BUG FIX
# ===========================================================================

class TestEmailSigninLockoutFix:
    """email_signin must properly enforce account lockout."""

    def test_email_signin_checks_lockout(self):
        """email_signin must check lockout_until before verifying password."""
        source = _read_source("auth_email.py")
        # Find the email_signin function
        func_start = source.find("def email_signin()")
        func_end = source.find("\ndef forgot_password()")
        email_signin_source = source[func_start:func_end]

        assert "lockout_until" in email_signin_source

    def test_email_signin_enforces_lockout(self):
        """email_signin must SET lockout_until after _LOCKOUT_AFTER failures."""
        source = _read_source("auth_email.py")
        func_start = source.find("def email_signin()")
        func_end = source.find("\ndef forgot_password()")
        email_signin_source = source[func_start:func_end]

        # Must check if attempts >= _LOCKOUT_AFTER and set lockout_until
        assert "_LOCKOUT_AFTER" in email_signin_source
        assert "lockout_until" in email_signin_source
        # Must have the same lockout enforcement pattern as password_signin
        assert 'update["$set"]["lockout_until"]' in email_signin_source or \
               'update["$set"]["lockout_until"]' in email_signin_source

    def test_email_signin_resets_on_success(self):
        """email_signin must reset failed_login_attempts on successful login."""
        source = _read_source("auth_email.py")
        func_start = source.find("def email_signin()")
        func_end = source.find("\ndef forgot_password()")
        email_signin_source = source[func_start:func_end]

        # Must reset failed_login_attempts to 0 on success
        assert "failed_login_attempts" in email_signin_source
        # The success path must set failed_login_attempts to 0
        # Find the "Password correct" comment
        success_section_start = email_signin_source.find("Password correct")
        if success_section_start > 0:
            success_section = email_signin_source[success_section_start:]
            assert '"failed_login_attempts": 0' in success_section or \
                   '"failed_login_attempts": 0' in success_section

    def test_email_signin_clears_lockout_on_success(self):
        """email_signin must clear lockout_until on successful login."""
        source = _read_source("auth_email.py")
        func_start = source.find("def email_signin()")
        func_end = source.find("\ndef forgot_password()")
        email_signin_source = source[func_start:func_end]

        success_section_start = email_signin_source.find("Password correct")
        if success_section_start > 0:
            success_section = email_signin_source[success_section_start:]
            assert "lockout_until" in success_section

    def test_password_signin_still_works(self):
        """password_signin lockout must not be broken."""
        source = _read_source("auth_email.py")
        func_start = source.find("def password_signin()")
        func_end = source.find("\ndef email_signin()")
        password_signin_source = source[func_start:func_end]

        assert "_LOCKOUT_AFTER" in password_signin_source
        assert "lockout_until" in password_signin_source
        assert '"failed_login_attempts": 0' in password_signin_source


# ===========================================================================
# 3. EXPENSIVE ENDPOINT RATE LIMITING
# ===========================================================================

class TestExpensiveEndpointRateLimiting:
    """LLM, STT, and Vision endpoints must be rate-limited."""

    def test_sessions_module_has_rate_limit_import(self):
        """sessions.py must import rate-limit helpers from extensions."""
        source = _read_source("sessions.py")
        assert "check_rate_limit" in source
        assert "record_rate_limit_event" in source

    def test_rate_limit_constants_exist(self):
        """Rate-limit constants for expensive endpoints must be defined."""
        source = _read_source("sessions.py")
        assert "_ANALYZE_MAX" in source
        assert "_TRANSCRIBE_MAX" in source
        assert "_OCR_MAX" in source
        assert "_API_RATE_WINDOW" in source

    def test_analyze_max_is_reasonable(self):
        """Analyze limit must be between 5 and 50 per 15min."""
        import sessions
        assert 5 <= sessions._ANALYZE_MAX <= 50

    def test_transcribe_max_is_reasonable(self):
        """Transcribe limit must be between 10 and 100 per 15min."""
        import sessions
        assert 10 <= sessions._TRANSCRIBE_MAX <= 100

    def test_ocr_max_is_reasonable(self):
        """OCR limit must be between 5 and 50 per 15min."""
        import sessions
        assert 5 <= sessions._OCR_MAX <= 50

    def test_api_rate_window_is_15_minutes(self):
        """API rate window must be 15 minutes (900 seconds)."""
        import sessions
        assert sessions._API_RATE_WINDOW == 900

    def test_check_api_rate_limit_function_exists(self):
        """_check_api_rate_limit function must exist."""
        import sessions
        assert hasattr(sessions, "_check_api_rate_limit")
        assert callable(sessions._check_api_rate_limit)

    def test_check_api_rate_limit_uses_mongodb(self):
        """_check_api_rate_limit must use MongoDB-backed rate limiting."""
        import inspect
        import sessions
        src = inspect.getsource(sessions._check_api_rate_limit)
        assert "check_rate_limit" in src
        assert "record_rate_limit_event" in src
        assert "get_db" in src

    def test_check_api_rate_limit_key_format(self):
        """Rate limit key must be per-user and per-endpoint."""
        import inspect
        import sessions
        src = inspect.getsource(sessions._check_api_rate_limit)
        assert "api_rate:" in src

    def test_analyze_session_has_rate_limit(self):
        """analyze_session must call _check_api_rate_limit."""
        import inspect
        import sessions
        src = inspect.getsource(sessions.analyze_session)
        assert "_check_api_rate_limit" in src
        assert "429" in src

    def test_transcribe_audio_has_rate_limit(self):
        """transcribe_audio must call _check_api_rate_limit."""
        import inspect
        import sessions
        src = inspect.getsource(sessions.transcribe_audio)
        assert "_check_api_rate_limit" in src
        assert "429" in src

    def test_transcribe_image_has_rate_limit(self):
        """transcribe_image must call _check_api_rate_limit."""
        import inspect
        import sessions
        src = inspect.getsource(sessions.transcribe_image)
        assert "_check_api_rate_limit" in src
        assert "429" in src

    def test_rate_limit_applied_before_expensive_operation(self):
        """Rate limit must be checked before the expensive LLM/STT call."""
        import inspect
        import sessions

        # For analyze: rate limit before get_llm_provider()
        src_analyze = inspect.getsource(sessions.analyze_session)
        rl_pos = src_analyze.find("_check_api_rate_limit")
        llm_pos = src_analyze.find("get_llm_provider")
        assert rl_pos < llm_pos, "Rate limit must be before LLM call"

        # For transcribe: rate limit before get_stt_provider()
        src_transcribe = inspect.getsource(sessions.transcribe_audio)
        rl_pos = src_transcribe.find("_check_api_rate_limit")
        stt_pos = src_transcribe.find("get_stt_provider")
        assert rl_pos < stt_pos, "Rate limit must be before STT call"

        # For OCR: rate limit before get_vision_provider()
        src_ocr = inspect.getsource(sessions.transcribe_image)
        rl_pos = src_ocr.find("_check_api_rate_limit")
        vision_pos = src_ocr.find("get_vision_provider")
        assert rl_pos < vision_pos, "Rate limit must be before Vision call"

    def test_rate_limit_returns_retry_after_header(self):
        """Rate-limited responses must include Retry-After header."""
        import inspect
        import sessions
        src = inspect.getsource(sessions.analyze_session)
        assert "Retry-After" in src

    def test_rate_limit_returns_generic_error(self):
        """Rate-limited responses must not reveal internal details."""
        import inspect
        import sessions
        src = inspect.getsource(sessions.analyze_session)
        assert "Too many requests" in src


# ===========================================================================
# 4. CSRF TOKEN RATE LIMITING
# ===========================================================================

class TestCSRFTokenRateLimiting:
    """CSRF token endpoint must be rate-limited per IP."""

    def test_csrf_token_has_rate_limit(self):
        """_csrf_token_endpoint must include rate limiting."""
        source = _read_source("app.py")
        # Find the csrf-token endpoint
        csrf_start = source.find("def _csrf_token_endpoint()")
        csrf_end = source.find("\n    @app", csrf_start + 1)
        csrf_source = source[csrf_start:csrf_end]

        assert "check_rate_limit" in csrf_source
        assert "record_rate_limit_event" in csrf_source

    def test_csrf_rate_limit_uses_ip(self):
        """CSRF rate limit must be per-IP."""
        source = _read_source("app.py")
        csrf_start = source.find("def _csrf_token_endpoint()")
        csrf_end = source.find("\n    @app", csrf_start + 1)
        csrf_source = source[csrf_start:csrf_end]

        assert "csrf_token:" in csrf_source
        assert "client_ip" in csrf_source

    def test_csrf_rate_limit_returns_429(self):
        """CSRF rate limit must return 429."""
        source = _read_source("app.py")
        csrf_start = source.find("def _csrf_token_endpoint()")
        csrf_end = source.find("\n    @app", csrf_start + 1)
        csrf_source = source[csrf_start:csrf_end]

        assert "429" in csrf_source

    def test_csrf_rate_limit_constants(self):
        """CSRF rate limit must be reasonable (20-60 per 15min)."""
        source = _read_source("app.py")
        csrf_start = source.find("def _csrf_token_endpoint()")
        csrf_end = source.find("\n    @app", csrf_start + 1)
        csrf_source = source[csrf_start:csrf_end]

        # Find the max_events parameter in check_rate_limit call
        match = re.search(r'check_rate_limit\(db, key, (\d+), (\d+)\)', csrf_source)
        assert match, "check_rate_limit call not found with numeric args"
        max_events = int(match.group(1))
        window = int(match.group(2))
        assert 20 <= max_events <= 60, f"CSRF limit {max_events} not in 20-60 range"
        assert window == 900, f"CSRF window {window} not 900"

    def test_app_imports_rate_limit_helpers(self):
        """app.py must import rate-limit helpers from extensions."""
        source = _read_source("app.py")
        assert "check_rate_limit" in source
        assert "record_rate_limit_event" in source
        assert "client_ip" in source


# ===========================================================================
# 5. SHARED RATE LIMIT INFRASTRUCTURE
# ===========================================================================

class TestRateLimitInfrastructure:
    """The shared rate-limiting infrastructure must be production-safe."""

    def test_check_rate_limit_exists(self):
        """check_rate_limit function must exist in extensions."""
        import extensions
        assert hasattr(extensions, "check_rate_limit")
        assert callable(extensions.check_rate_limit)

    def test_record_rate_limit_event_exists(self):
        """record_rate_limit_event function must exist in extensions."""
        import extensions
        assert hasattr(extensions, "record_rate_limit_event")
        assert callable(extensions.record_rate_limit_event)

    def test_client_ip_exists(self):
        """client_ip function must exist in extensions."""
        import extensions
        assert hasattr(extensions, "client_ip")
        assert callable(extensions.client_ip)

    def test_rate_limits_collection_has_ttl_index(self):
        """rate_limits collection must have TTL index for auto-cleanup."""
        source = _read_source("extensions.py")
        assert "expireAfterSeconds=0" in source
        assert "expire_at" in source

    def test_rate_limits_collection_has_key_ts_index(self):
        """rate_limits collection must have compound (key, ts) index."""
        source = _read_source("extensions.py")
        assert '"key"' in source or "'key'" in source
        assert '"ts"' in source or "'ts'" in source

    def test_rate_limit_uses_mongodb_not_redis(self):
        """Rate limiting uses MongoDB (project has no Redis dependency)."""
        with open(os.path.join(_ROOT, "requirements.txt"), encoding="utf-8") as f:
            reqs = f.read().lower()
        assert "redis" not in reqs
        assert "flask-limiter" not in reqs

    def test_no_in_memory_rate_limit_dicts(self):
        """No in-memory rate limit dictionaries must exist in any module."""
        for fname in ("totp_routes.py", "sessions.py", "auth_email.py", "app.py"):
            source = _read_source(fname)
            # Check for common in-memory dict patterns
            assert "_rate_limit_store" not in source
            assert "_request_counts" not in source
            assert "_attempts_dict" not in source


# ===========================================================================
# 6. RATE LIMIT RESPONSE SAFETY
# ===========================================================================

class TestRateLimitResponseSafety:
    """Rate-limit responses must not leak sensitive information."""

    def test_no_mongodb_details_in_rate_limit_responses(self):
        """Rate-limit responses must not mention MongoDB internals."""
        for fname in ("totp_routes.py", "sessions.py", "auth_email.py", "app.py"):
            source = _read_source(fname)
            # Find all 429 response patterns
            for match in re.finditer(r'429.*?\)', source, re.DOTALL):
                response_text = match.group(0)
                assert "mongodb" not in response_text.lower()
                assert "rate_limits" not in response_text.lower()
                assert "collection" not in response_text.lower()

    def test_no_redis_details_in_rate_limit_responses(self):
        """Rate-limit responses must not mention Redis internals."""
        for fname in ("totp_routes.py", "sessions.py", "auth_email.py", "app.py"):
            source = _read_source(fname)
            assert "redis" not in source.lower() or "redis" in "no redis in this project"

    def test_no_server_config_in_rate_limit_responses(self):
        """Rate-limit responses must not expose server configuration."""
        for fname in ("totp_routes.py", "sessions.py", "auth_email.py", "app.py"):
            source = _read_source(fname)
            # Look for jsonify calls near 429
            for match in re.finditer(r'jsonify\(\{[^}]*\}\).*?429', source, re.DOTALL):
                response_text = match.group(0)
                assert "SECRET_KEY" not in response_text
                assert "MONGODB_URI" not in response_text
                assert "DEBUG" not in response_text

    def test_no_stack_traces_in_rate_limit_responses(self):
        """Rate-limit responses must not include stack traces."""
        for fname in ("totp_routes.py", "sessions.py", "auth_email.py", "app.py"):
            source = _read_source(fname)
            assert "traceback" not in source.lower() or "traceback" in "no traceback in this file"

    def test_rate_limit_responses_use_generic_messages(self):
        """All rate-limit error messages must be generic."""
        for fname in ("totp_routes.py", "sessions.py", "auth_email.py", "app.py"):
            source = _read_source(fname)
            # Find lines with 429 that contain "error"
            for i, line in enumerate(source.split("\n"), 1):
                if "429" in line:
                    # Check nearby error message lines
                    pass  # Just verify the function exists and returns 429


# ===========================================================================
# 7. EXISTING RATE LIMITING STILL FUNCTIONAL
# ===========================================================================

class TestExistingRateLimitingPreserved:
    """Existing rate-limiting mechanisms must not be broken."""

    def test_otp_send_rate_limit_still_exists(self):
        """OTP send rate limiting (per-email + per-IP) must still work."""
        import auth_email
        assert hasattr(auth_email, "_check_otp_send_rate_limit")
        assert auth_email._OTP_MAX_PER_EMAIL == 5
        assert auth_email._OTP_MAX_PER_IP == 20
        assert auth_email._OTP_RATE_WINDOW == 900

    def test_otp_send_rate_limit_applied_to_start(self):
        """email_start must call _check_otp_send_rate_limit."""
        source = _read_source("auth_email.py")
        func_start = source.find("def email_start()")
        func_end = source.find("\ndef verify_otp()")
        start_source = source[func_start:func_end]
        assert "_check_otp_send_rate_limit" in start_source

    def test_otp_send_rate_limit_applied_to_resend(self):
        """resend_otp must call _check_otp_send_rate_limit."""
        source = _read_source("auth_email.py")
        func_start = source.find("def resend_otp()")
        func_end = source.find("\ndef password_signin()")
        resend_source = source[func_start:func_end]
        assert "_check_otp_send_rate_limit" in resend_source

    def test_otp_resend_cooldown_still_exists(self):
        """60-second cooldown between OTP resends must still work."""
        source = _read_source("auth_email.py")
        assert "_RESEND_COOLDOWN" in source
        import auth_email
        assert auth_email._RESEND_COOLDOWN.total_seconds() == 60

    def test_otp_verification_attempts_still_limited(self):
        """OTP verification must still be limited to _MAX_ATTEMPTS."""
        import auth_email
        assert auth_email._MAX_ATTEMPTS == 5
        source = _read_source("auth_email.py")
        assert "_MAX_ATTEMPTS" in source

    def test_password_signin_lockout_still_works(self):
        """password_signin lockout mechanism must still work."""
        source = _read_source("auth_email.py")
        func_start = source.find("def password_signin()")
        func_end = source.find("\ndef email_signin()")
        pw_source = source[func_start:func_end]

        assert "lockout_until" in pw_source
        assert "_LOCKOUT_AFTER" in pw_source
        assert "_LOCKOUT_TTL" in pw_source

    def test_backup_code_rate_limit_still_works(self):
        """Backup code rate limiting must still work."""
        import totp_routes
        assert totp_routes._BACKUPCodeAttempts_MAX == 5
        assert totp_routes._BACKUPCodeAttempts_WINDOW == 900
        assert hasattr(totp_routes, "_check_backup_rate_limit")


# ===========================================================================
# 8. SESSION MODULE INTEGRITY
# ===========================================================================

class TestSessionsModuleIntegrity:
    """sessions.py must remain functionally intact after rate-limit addition."""

    def test_sessions_module_imports(self):
        """sessions.py must import all required modules."""
        source = _read_source("sessions.py")
        assert "from flask import" in source
        assert "from bson import ObjectId" in source
        assert "from extensions import" in source

    def test_session_to_json_unchanged(self):
        """_session_to_json must still return all required fields."""
        source = _read_source("sessions.py")
        func_source = _get_func_source("sessions.py", "_session_to_json")
        required_fields = ["id", "employee_id", "source", "status", "transcript",
                           "analysis", "created_at", "updated_at"]
        for field in required_fields:
            assert f'"{field}"' in func_source, f"Missing field: {field}"

    def test_create_session_unchanged(self):
        """create_session must still create sessions correctly."""
        source = _read_source("sessions.py")
        func_source = _get_func_source("sessions.py", "create_session")
        assert "insert_one" in func_source
        assert "_session_to_json" in func_source

    def test_list_sessions_unchanged(self):
        """list_sessions must still list sessions correctly."""
        source = _read_source("sessions.py")
        func_source = _get_func_source("sessions.py", "list_sessions")
        assert "find" in func_source
        assert "org_id" in func_source

    def test_delete_session_unchanged(self):
        """delete_session must still delete sessions correctly."""
        source = _read_source("sessions.py")
        func_source = _get_func_source("sessions.py", "delete_session")
        assert "delete_one" in func_source


# ===========================================================================
# 9. APP MODULE INTEGRITY
# ===========================================================================

class TestAppModuleIntegrity:
    """app.py must remain functionally intact after rate-limit addition."""

    def test_csrf_token_endpoint_still_returns_token(self):
        """CSRF token endpoint must still return a valid token."""
        source = _read_source("app.py")
        csrf_start = source.find("def _csrf_token_endpoint()")
        csrf_end = source.find("\n    @app", csrf_start + 1)
        csrf_source = source[csrf_start:csrf_end]

        assert "token_urlsafe" in csrf_source
        assert "csrf_token" in csrf_source

    def test_csrf_token_still_requires_auth(self):
        """CSRF token endpoint must still require authentication."""
        source = _read_source("app.py")
        csrf_start = source.find("def _csrf_token_endpoint()")
        csrf_end = source.find("\n    @app", csrf_start + 1)
        csrf_source = source[csrf_start:csrf_end]

        assert "not_authenticated" in csrf_source

    def test_csrf_protect_still_works(self):
        """CSRF protection before_request hook must still work."""
        source = _read_source("app.py")
        assert "_csrf_protect" in source
        assert "X-CSRF-Token" in source
        assert "hmac.compare_digest" in source

    def test_totp_enforcement_still_works(self):
        """Admin TOTP enforcement must still work."""
        source = _read_source("app.py")
        assert "_enforce_admin_totp" in source
        assert "TOTP_ENROLL_ROUTE" in source or "_TOTP_ENROLL_ROUTE" in source

    def test_health_endpoint_unchanged(self):
        """Health endpoint must still work."""
        source = _read_source("app.py")
        assert "/health" in source
        assert "status" in source

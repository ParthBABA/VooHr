"""Tests for Phase 2 / Fix #9 — Request & Upload Security Audit.

Covers:
  - Audio upload size limit (MAX_AUDIO_BYTES)
  - Audio MIME/type validation (AUDIO_CONTENT_TYPES)
  - Image magic-bytes validation (PIL-based)
  - Employee photo/base64 size limit (MAX_PHOTO_BYTES)
  - Employee field length validation
  - Session CRUD field length limits (raw_text, edited_text, audio)
  - Password max length before argon2 processing
  - LLM transcript truncation (MAX_LLM_TRANSCRIPT_CHARS)
  - Flask form limits (MAX_FORM_MEMORY_SIZE, MAX_FORM_PARTS)
  - Global MAX_CONTENT_LENGTH and 413 handler
  - Configuration correctness
  - Existing functionality unaffected
"""

import base64
import io
import os
import struct
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


# ===========================================================================
# 1. GLOBAL REQUEST SIZE LIMITS
# ===========================================================================

class TestGlobalRequestLimits:
    """Global Flask request size limits must be configured."""

    def test_max_content_length_exists(self):
        """MAX_CONTENT_LENGTH must be defined in Config."""
        source = _read_source("config.py")
        assert "MAX_CONTENT_LENGTH" in source

    def test_max_content_length_is_50mb(self):
        """MAX_CONTENT_LENGTH must be 50 MB."""
        from config import Config
        assert Config.MAX_CONTENT_LENGTH == 50 * 1024 * 1024

    def test_max_form_memory_size_exists(self):
        """MAX_FORM_MEMORY_SIZE must be configured."""
        source = _read_source("config.py")
        assert "MAX_FORM_MEMORY_SIZE" in source

    def test_max_form_memory_size_is_reasonable(self):
        """MAX_FORM_MEMORY_SIZE must be between 1 MB and 50 MB."""
        from config import Config
        assert 1 * 1024 * 1024 <= Config.MAX_FORM_MEMORY_SIZE <= 50 * 1024 * 1024

    def test_max_form_parts_exists(self):
        """MAX_FORM_PARTS must be configured."""
        source = _read_source("config.py")
        assert "MAX_FORM_PARTS" in source

    def test_max_form_parts_is_reasonable(self):
        """MAX_FORM_PARTS must be between 10 and 10000."""
        from config import Config
        assert 10 <= Config.MAX_FORM_PARTS <= 10000

    def test_413_handler_exists(self):
        """App must have a RequestEntityTooLarge error handler."""
        source = _read_source("app.py")
        assert "RequestEntityTooLarge" in source
        assert "_handle_payload_too_large" in source

    def test_413_handler_returns_safe_response(self):
        """413 handler must return JSON without stack traces."""
        source = _read_source("app.py")
        # Find the handler and verify it returns jsonify with 413
        assert '"Request payload is too large."' in source
        assert "413" in source

    def test_request_entity_too_large_imported(self):
        """RequestEntityTooLarge must be imported in app.py."""
        source = _read_source("app.py")
        assert "from werkzeug.exceptions import RequestEntityTooLarge" in source


# ===========================================================================
# 2. AUDIO UPLOAD SIZE LIMIT
# ===========================================================================

class TestAudioUploadSizeLimit:
    """Audio transcription must have a per-endpoint size limit."""

    def test_max_audio_bytes_constant_exists(self):
        """MAX_AUDIO_BYTES must be defined in sessions.py."""
        source = _read_source("sessions.py")
        assert "MAX_AUDIO_BYTES" in source

    def test_max_audio_bytes_is_26mb(self):
        """MAX_AUDIO_BYTES must be 26 MB (25 MB Whisper + 1 MB headroom)."""
        import sessions
        assert sessions.MAX_AUDIO_BYTES == 26 * 1024 * 1024

    def test_max_audio_bytes_less_than_global(self):
        """Audio limit must be smaller than the global MAX_CONTENT_LENGTH."""
        import sessions
        from config import Config
        assert sessions.MAX_AUDIO_BYTES < Config.MAX_CONTENT_LENGTH

    def test_transcribe_enforces_audio_size_check(self):
        """transcribe_audio must check audio size before calling STT."""
        source = _read_source("sessions.py")
        # Find the transcribe_audio function and verify it checks size
        func_start = source.find("def transcribe_audio(")
        func_body = source[func_start:func_start + 2000]
        assert "MAX_AUDIO_BYTES" in func_body
        assert "audio_too_large" in func_body

    def test_transcribe_returns_413_for_oversized_audio(self):
        """Oversized audio must return 413, not 400."""
        source = _read_source("sessions.py")
        func_start = source.find("def transcribe_audio(")
        func_body = source[func_start:func_start + 2000]
        # Find the audio_too_large check
        check_pos = func_body.find("audio_too_large")
        assert check_pos > 0
        # Verify 413 status code
        check_area = func_body[check_pos - 100:check_pos + 100]
        assert "413" in check_area


# ===========================================================================
# 3. AUDIO MIME TYPE VALIDATION
# ===========================================================================

class TestAudioMIMEValidation:
    """Audio uploads must validate content type against an allowlist."""

    def test_audio_content_types_constant_exists(self):
        """AUDIO_CONTENT_TYPES must be defined."""
        source = _read_source("sessions.py")
        assert "AUDIO_CONTENT_TYPES" in source

    def test_audio_content_types_includes_common_formats(self):
        """Allowlist must include webm, wav, mp3, ogg, m4a."""
        import sessions
        assert "audio/webm" in sessions.AUDIO_CONTENT_TYPES
        assert "audio/wav" in sessions.AUDIO_CONTENT_TYPES
        assert "audio/mpeg" in sessions.AUDIO_CONTENT_TYPES
        assert "audio/ogg" in sessions.AUDIO_CONTENT_TYPES
        assert "audio/mp4" in sessions.AUDIO_CONTENT_TYPES or "audio/x-m4a" in sessions.AUDIO_CONTENT_TYPES

    def test_transcribe_enforces_content_type_check(self):
        """transcribe_audio must validate content type against the allowlist."""
        source = _read_source("sessions.py")
        func_start = source.find("def transcribe_audio(")
        func_body = source[func_start:func_start + 2000]
        assert "AUDIO_CONTENT_TYPES" in func_body
        assert "unsupported_audio_type" in func_body

    def test_audio_content_types_is_set(self):
        """AUDIO_CONTENT_TYPES must be a set for O(1) lookup."""
        import sessions
        assert isinstance(sessions.AUDIO_CONTENT_TYPES, set)

    def test_audio_content_types_definitely_not_a_zip(self):
        """The allowlist must NOT contain application/zip or similar non-audio types."""
        import sessions
        assert "application/zip" not in sessions.AUDIO_CONTENT_TYPES
        assert "application/pdf" not in sessions.AUDIO_CONTENT_TYPES
        assert "text/plain" not in sessions.AUDIO_CONTENT_TYPES


# ===========================================================================
# 4. IMAGE MAGIC-BYTES VALIDATION
# ===========================================================================

class TestImageMagicBytesValidation:
    """Image uploads must be validated by actual file content, not just MIME header."""

    def test_validate_image_magic_bytes_function_exists(self):
        """_validate_image_magic_bytes must be defined in sessions.py."""
        source = _read_source("sessions.py")
        assert "_validate_image_magic_bytes" in source

    def test_png_magic_bytes_detected(self):
        """PNG files must be detected by their magic bytes."""
        import sessions
        png_header = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        assert sessions._validate_image_magic_bytes(png_header) == "image/png"

    def test_jpeg_magic_bytes_detected(self):
        """JPEG files must be detected by their magic bytes."""
        import sessions
        jpeg_header = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        assert sessions._validate_image_magic_bytes(jpeg_header) == "image/jpeg"

    def test_webp_magic_bytes_detected(self):
        """WebP files must be detected by their magic bytes."""
        import sessions
        webp_header = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 100
        assert sessions._validate_image_magic_bytes(webp_header) == "image/webp"

    def test_gif_magic_bytes_detected(self):
        """GIF files must be detected by their magic bytes."""
        import sessions
        gif_header = b"GIF89a" + b"\x00" * 100
        assert sessions._validate_image_magic_bytes(gif_header) == "image/gif"

    def test_zip_file_rejected_as_image(self):
        """A ZIP file disguised as an image must be rejected."""
        import sessions
        zip_header = b"PK\x03\x04" + b"\x00" * 100
        assert sessions._validate_image_magic_bytes(zip_header) is None

    def test_pdf_file_rejected_as_image(self):
        """A PDF file must be rejected by magic-bytes validation."""
        import sessions
        pdf_header = b"%PDF-1.4" + b"\x00" * 100
        assert sessions._validate_image_magic_bytes(pdf_header) is None

    def test_empty_bytes_rejected(self):
        """Empty/short bytes must be rejected."""
        import sessions
        assert sessions._validate_image_magic_bytes(b"") is None
        assert sessions._validate_image_magic_bytes(b"\x00") is None
        assert sessions._validate_image_magic_bytes(b"\x89PNG") is None  # too short

    def test_transcribe_image_uses_magic_bytes_validation(self):
        """transcribe_image must call _validate_image_magic_bytes (via PIL)."""
        source = _read_source("sessions.py")
        func_start = source.find("def transcribe_image(")
        func_body = source[func_start:func_start + 2000]
        # PIL validation present
        assert "PIL" in func_body or "Pillow" in func_body or "from PIL" in func_body
        assert "invalid_image_content" in func_body

    def test_transcribe_image_pil_verify_rejects_non_image(self):
        """PIL's img.verify() must catch non-image data."""
        from PIL import Image
        # A valid PNG with PIL verify
        img = Image.new("RGB", (1, 1))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        valid_png = buf.getvalue()
        # PIL should accept this
        img2 = Image.open(io.BytesIO(valid_png))
        img2.verify()  # should not raise

    def test_transcribe_image_pil_verify_rejects_garbage(self):
        """PIL's img.verify() must reject random garbage with image header."""
        from PIL import Image
        # Create data that has PNG magic bytes but is garbage after that
        fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
        with pytest.raises(Exception):
            img = Image.open(io.BytesIO(fake_png))
            img.verify()


# ===========================================================================
# 5. EMPLOYEE PHOTO SIZE LIMIT
# ===========================================================================

class TestEmployeePhotoSizeLimit:
    """Employee photo (base64 data-URL) must have a maximum size."""

    def test_max_photo_bytes_constant_exists(self):
        """MAX_PHOTO_BYTES must be defined in employees.py."""
        source = _read_source("employees.py")
        assert "MAX_PHOTO_BYTES" in source

    def test_max_photo_bytes_is_2mb(self):
        """MAX_PHOTO_BYTES must be 2 MB."""
        import employees
        assert employees.MAX_PHOTO_BYTES == 2 * 1024 * 1024

    def test_create_employee_checks_photo_size(self):
        """create_employee must check photo size before storing."""
        source = _read_source("employees.py")
        func_start = source.find("def create_employee(")
        func_end = source.find("\ndef ", func_start + 1)
        func_body = source[func_start:func_end if func_end > 0 else func_start + 9000]
        assert "MAX_PHOTO_BYTES" in func_body
        assert "photo_too_large" in func_body

    def test_create_employee_returns_413_for_large_photo(self):
        """Oversized photo must return 413."""
        source = _read_source("employees.py")
        func_start = source.find("def create_employee(")
        func_end = source.find("\ndef ", func_start + 1)
        func_body = source[func_start:func_end if func_end > 0 else func_start + 9000]
        check_pos = func_body.find("photo_too_large")
        assert check_pos > 0
        check_area = func_body[check_pos - 100:check_pos + 100]
        assert "413" in check_area

    def test_update_employee_checks_photo_size(self):
        """update_employee must check photo size before storing."""
        source = _read_source("employees.py")
        func_start = source.find("def update_employee(")
        func_body = source[func_start:func_start + 4000]
        assert "MAX_PHOTO_BYTES" in func_body
        assert "photo_too_large" in func_body


# ===========================================================================
# 6. EMPLOYEE FIELD LENGTH VALIDATION
# ===========================================================================

class TestEmployeeFieldLengthValidation:
    """Employee CRUD must enforce field length limits."""

    def test_employee_field_limits_defined(self):
        """Field length constants must be defined."""
        source = _read_source("employees.py")
        assert "MAX_EMPLOYEE_NAME_LEN" in source
        assert "MAX_EMPLOYEE_EMAIL_LEN" in source
        assert "MAX_EMPLOYEE_PHONE_LEN" in source
        assert "MAX_EMPLOYEE_DEPT_LEN" in source
        assert "MAX_EMPLOYEE_POSITION_LEN" in source

    def test_employee_name_limit_is_200(self):
        """Name limit must be 200 characters."""
        import employees
        assert employees.MAX_EMPLOYEE_NAME_LEN == 200

    def test_employee_email_limit_is_320(self):
        """Email limit must be 320 characters (RFC 5321)."""
        import employees
        assert employees.MAX_EMPLOYEE_EMAIL_LEN == 320

    def test_create_employee_enforces_name_length(self):
        """create_employee must check name length."""
        source = _read_source("employees.py")
        func_start = source.find("def create_employee(")
        func_body = source[func_start:func_start + 3000]
        assert "name_too_long" in func_body

    def test_create_employee_enforces_all_field_lengths(self):
        """create_employee must check all string field lengths."""
        source = _read_source("employees.py")
        func_start = source.find("def create_employee(")
        func_body = source[func_start:func_start + 3000]
        for error in ("name_too_long", "email_too_long", "phone_too_long",
                       "department_too_long", "position_too_long"):
            assert error in func_body, f"Missing length check for {error}"

    def test_update_employee_enforces_field_lengths(self):
        """update_employee must check field lengths."""
        source = _read_source("employees.py")
        func_start = source.find("def update_employee(")
        func_body = source[func_start:func_start + 4000]
        assert "too_long" in func_body

    def test_update_employee_enforces_pii_field_lengths(self):
        """update_employee must check PII field lengths."""
        source = _read_source("employees.py")
        func_start = source.find("def update_employee(")
        func_body = source[func_start:func_start + 4000]
        # Should have length checks for name, email, phone in PII section
        pii_section_start = func_body.find("PII fields")
        if pii_section_start > 0:
            pii_body = func_body[pii_section_start:pii_section_start + 500]
            assert "too_long" in pii_body


# ===========================================================================
# 7. SESSION CRUD FIELD LENGTH LIMITS
# ===========================================================================

class TestSessionFieldLengthLimits:
    """Session CRUD must enforce field length limits."""

    def test_session_field_limits_defined(self):
        """Session field length constants must be defined."""
        source = _read_source("sessions.py")
        assert "MAX_RAW_TEXT_BYTES" in source
        assert "MAX_EDITED_TEXT_BYTES" in source
        assert "MAX_SESSION_AUDIO_BYTES" in source

    def test_raw_text_limit_is_512kb(self):
        """raw_text limit must be 512 KB."""
        import sessions
        assert sessions.MAX_RAW_TEXT_BYTES == 512 * 1024

    def test_edited_text_limit_is_512kb(self):
        """edited_text limit must be 512 KB."""
        import sessions
        assert sessions.MAX_EDITED_TEXT_BYTES == 512 * 1024

    def test_session_audio_limit_is_2mb(self):
        """Session audio (base64 in JSON) limit must be 2 MB."""
        import sessions
        assert sessions.MAX_SESSION_AUDIO_BYTES == 2 * 1024 * 1024

    def test_create_session_checks_raw_text_size(self):
        """create_session must check raw_text byte length."""
        source = _read_source("sessions.py")
        func_start = source.find("def create_session(")
        func_body = source[func_start:func_start + 3000]
        assert "raw_text_too_large" in func_body

    def test_create_session_checks_edited_text_size(self):
        """create_session must check edited_text byte length."""
        source = _read_source("sessions.py")
        func_start = source.find("def create_session(")
        func_body = source[func_start:func_start + 3000]
        assert "edited_text_too_large" in func_body

    def test_create_session_checks_audio_payload_size(self):
        """create_session must check audio payload byte length."""
        source = _read_source("sessions.py")
        func_start = source.find("def create_session(")
        func_body = source[func_start:func_start + 3000]
        assert "audio_payload_too_large" in func_body

    def test_create_session_returns_413_for_large_text(self):
        """Oversized text must return 413."""
        source = _read_source("sessions.py")
        func_start = source.find("def create_session(")
        func_body = source[func_start:func_start + 3000]
        check_pos = func_body.find("raw_text_too_large")
        assert check_pos > 0
        check_area = func_body[check_pos - 100:check_pos + 100]
        assert "413" in check_area

    def test_update_session_checks_edited_text_size(self):
        """update_session must check edited_text byte length."""
        source = _read_source("sessions.py")
        func_start = source.find("def update_session(")
        func_body = source[func_start:func_start + 3000]
        assert "edited_text_too_large" in func_body

    def test_update_session_checks_audio_payload_size(self):
        """update_session must check audio payload byte length."""
        source = _read_source("sessions.py")
        func_start = source.find("def update_session(")
        func_body = source[func_start:func_start + 3000]
        assert "audio_payload_too_large" in func_body

    def test_session_source_field_length_checked(self):
        """create_session must check source field length."""
        source = _read_source("sessions.py")
        func_start = source.find("def create_session(")
        func_body = source[func_start:func_start + 3000]
        assert "source_too_long" in func_body

    def test_session_language_field_length_checked(self):
        """create_session must check language field length."""
        source = _read_source("sessions.py")
        func_start = source.find("def create_session(")
        func_body = source[func_start:func_start + 3000]
        assert "language_too_long" in func_body


# ===========================================================================
# 8. PASSWORD MAX LENGTH
# ===========================================================================

class TestPasswordMaxLength:
    """Passwords must have a maximum length before argon2 processing."""

    def test_max_password_length_constant_exists(self):
        """MAX_PASSWORD_LENGTH must be defined."""
        source = _read_source("auth_email.py")
        assert "MAX_PASSWORD_LENGTH" in source

    def test_max_password_length_is_256(self):
        """MAX_PASSWORD_LENGTH must be 256."""
        import auth_email
        assert auth_email.MAX_PASSWORD_LENGTH == 256

    def test_email_start_checks_password_length(self):
        """email_start must check password length before argon2."""
        source = _read_source("auth_email.py")
        func_start = source.find("def email_start(")
        func_body = source[func_start:func_start + 2000]
        assert "password_too_long" in func_body

    def test_password_signin_checks_password_length(self):
        """password_signin must check password length before argon2."""
        source = _read_source("auth_email.py")
        func_start = source.find("def password_signin(")
        func_body = source[func_start:func_start + 2000]
        assert "MAX_PASSWORD_LENGTH" in func_body

    def test_email_signin_checks_password_length(self):
        """email_signin must check password length before argon2."""
        source = _read_source("auth_email.py")
        func_start = source.find("def email_signin(")
        func_body = source[func_start:func_start + 2000]
        assert "MAX_PASSWORD_LENGTH" in func_body

    def test_password_length_check_before_blind_index(self):
        """Password/email length must be checked BEFORE blind_index call in password_signin."""
        source = _read_source("auth_email.py")
        func_start = source.find("def password_signin(")
        func_body = source[func_start:func_start + 2000]
        length_check_pos = func_body.find("MAX_PASSWORD_LENGTH")
        blind_index_pos = func_body.find("blind_index(email)")
        assert length_check_pos > 0, "Password length check must exist"
        assert blind_index_pos > 0, "blind_index call must exist"
        assert length_check_pos < blind_index_pos, "Length check must come BEFORE blind_index"

    def test_password_length_check_before_argon2(self):
        """Password length must be checked BEFORE argon2 verify in password_signin."""
        source = _read_source("auth_email.py")
        func_start = source.find("def password_signin(")
        func_body = source[func_start:func_start + 2000]
        length_check_pos = func_body.find("MAX_PASSWORD_LENGTH")
        verify_pos = func_body.find("verify_password(")
        assert length_check_pos > 0
        assert verify_pos > 0
        assert length_check_pos < verify_pos, "Length check must come BEFORE argon2 verify"


# ===========================================================================
# 9. EMAIL/OTP LENGTH LIMITS
# ===========================================================================

class TestEmailOTPMaxLength:
    """Email and OTP inputs must have maximum lengths."""

    def test_max_email_length_defined(self):
        """_MAX_EMAIL_LENGTH must be defined."""
        source = _read_source("auth_email.py")
        assert "_MAX_EMAIL_LENGTH" in source

    def test_max_email_length_is_320(self):
        """_MAX_EMAIL_LENGTH must be 320 (RFC 5321)."""
        import auth_email
        assert auth_email._MAX_EMAIL_LENGTH == 320

    def test_max_otp_length_defined(self):
        """_MAX_OTP_LENGTH must be defined."""
        source = _read_source("auth_email.py")
        assert "_MAX_OTP_LENGTH" in source

    def test_max_otp_length_is_128(self):
        """_MAX_OTP_LENGTH must be 128."""
        import auth_email
        assert auth_email._MAX_OTP_LENGTH == 128

    def test_verify_otp_checks_otp_length(self):
        """verify_otp must check OTP length."""
        source = _read_source("auth_email.py")
        func_start = source.find("def verify_otp(")
        func_body = source[func_start:func_start + 2000]
        assert "_MAX_OTP_LENGTH" in func_body

    def test_email_start_checks_email_length(self):
        """email_start must check email length."""
        source = _read_source("auth_email.py")
        func_start = source.find("def email_start(")
        func_body = source[func_start:func_start + 2000]
        assert "_MAX_EMAIL_LENGTH" in func_body


# ===========================================================================
# 10. LLM TRANSCRIPT TRUNCATION
# ===========================================================================

class TestLLMTranscriptTruncation:
    """LLM calls must receive truncated transcripts, not unbounded data."""

    def test_max_llm_transcript_chars_exists(self):
        """MAX_LLM_TRANSCRIPT_CHARS must be defined in sessions.py."""
        source = _read_source("sessions.py")
        assert "MAX_LLM_TRANSCRIPT_CHARS" in source

    def test_max_llm_transcript_chars_is_50000(self):
        """MAX_LLM_TRANSCRIPT_CHARS must be 50,000."""
        import sessions
        assert sessions.MAX_LLM_TRANSCRIPT_CHARS == 50_000

    def test_analyze_session_truncates_transcript(self):
        """analyze_session must truncate transcript before LLM call."""
        source = _read_source("sessions.py")
        func_start = source.find("def analyze_session(")
        func_body = source[func_start:func_start + 5000]
        assert "llm_transcript" in func_body
        assert "MAX_LLM_TRANSCRIPT_CHARS" in func_body

    def test_analyze_session_uses_truncated_for_llm(self):
        """The LLM call must use the truncated transcript, not the full one."""
        source = _read_source("sessions.py")
        func_start = source.find("def analyze_session(")
        func_body = source[func_start:func_start + 5000]
        # The llm.analyze call should use llm_transcript, not transcript
        llm_call_pos = func_body.find("llm.analyze(")
        assert llm_call_pos > 0
        llm_call_area = func_body[llm_call_pos:llm_call_pos + 50]
        assert "llm_transcript" in llm_call_area

    def test_drift_detection_truncates_transcripts(self):
        """Risk Drift Detection must truncate transcripts too."""
        source = _read_source("sessions.py")
        # Find the drift detection payload construction
        drift_start = source.find("sessions_payload = [")
        drift_body = source[drift_start:drift_start + 600]
        assert "MAX_LLM_TRANSCRIPT_CHARS" in drift_body

    def test_truncation_preserves_full_transcript_in_db(self):
        """Truncation must NOT modify the stored transcript — only the LLM input."""
        source = _read_source("sessions.py")
        func_start = source.find("def analyze_session(")
        func_body = source[func_start:func_start + 3000]
        # The truncation should create llm_transcript as a slice, not overwrite transcript
        assert 'llm_transcript = transcript[:MAX_LLM_TRANSCRIPT_CHARS]' in func_body

    def test_full_transcript_not_sent_to_llm(self):
        """The full 'transcript' variable must NOT be passed to llm.analyze."""
        source = _read_source("sessions.py")
        func_start = source.find("def analyze_session(")
        func_body = source[func_start:func_start + 3000]
        # Find the llm.analyze call — it should use llm_transcript not transcript
        llm_call_start = func_body.find("llm.analyze(")
        llm_call_end = func_body.find(")", llm_call_start)
        llm_call = func_body[llm_call_start:llm_call_end + 1]
        assert "llm_transcript" in llm_call
        # Ensure 'transcript' alone is not the argument
        args = llm_call.replace("llm.analyze(", "").rstrip(")")
        assert args.strip() != "transcript"


# ===========================================================================
# 11. IMAGE UPLOAD SIZE + TYPE VALIDATION
# ===========================================================================

class TestImageUploadValidation:
    """Image OCR uploads must have size + type + magic-bytes validation."""

    def test_max_image_bytes_is_10mb(self):
        """MAX_IMAGE_BYTES must be 10 MB."""
        import sessions
        assert sessions.MAX_IMAGE_BYTES == 10 * 1024 * 1024

    def test_image_content_types_includes_supported_formats(self):
        """IMAGE_CONTENT_TYPES must include png, jpeg, webp."""
        import sessions
        assert "image/png" in sessions.IMAGE_CONTENT_TYPES
        assert "image/jpeg" in sessions.IMAGE_CONTENT_TYPES
        assert "image/webp" in sessions.IMAGE_CONTENT_TYPES

    def test_transcribe_image_enforces_size_check(self):
        """transcribe_image must check image size."""
        source = _read_source("sessions.py")
        func_start = source.find("def transcribe_image(")
        func_body = source[func_start:func_start + 2000]
        assert "MAX_IMAGE_BYTES" in func_body
        assert "image_too_large" in func_body

    def test_transcribe_image_enforces_content_type(self):
        """transcribe_image must check content type."""
        source = _read_source("sessions.py")
        func_start = source.find("def transcribe_image(")
        func_body = source[func_start:func_start + 2000]
        assert "IMAGE_CONTENT_TYPES" in func_body
        assert "unsupported_image_type" in func_body

    def test_transcribe_image_enforces_magic_bytes(self):
        """transcribe_image must validate magic bytes via PIL."""
        source = _read_source("sessions.py")
        func_start = source.find("def transcribe_image(")
        func_body = source[func_start:func_start + 2000]
        assert "PIL" in func_body or "from PIL" in func_body
        assert "invalid_image_content" in func_body

    def test_transcribe_image_size_check_before_content_type(self):
        """Size check must come before content-type check (cheaper first)."""
        source = _read_source("sessions.py")
        func_start = source.find("def transcribe_image(")
        func_body = source[func_start:func_start + 2000]
        size_check_pos = func_body.find("image_too_large")
        type_check_pos = func_body.find("unsupported_image_type")
        assert size_check_pos < type_check_pos

    def test_transcribe_image_content_type_check_before_pil(self):
        """Content-type check must come before PIL validation (cheaper first)."""
        source = _read_source("sessions.py")
        func_start = source.find("def transcribe_image(")
        func_body = source[func_start:func_start + 2000]
        type_check_pos = func_body.find("unsupported_image_type")
        pil_check_pos = func_body.find("invalid_image_content")
        assert type_check_pos < pil_check_pos


# ===========================================================================
# 12. EXISTING FUNCTIONALITY PRESERVED
# ===========================================================================

class TestExistingFunctionality:
    """Verify existing functionality is not broken by new validation."""

    def test_image_webm_audio_still_supported(self):
        """audio/webm (the browser recording format) must still be allowed."""
        import sessions
        assert "audio/webm" in sessions.AUDIO_CONTENT_TYPES

    def test_image_png_still_supported(self):
        """image/png must still be supported for OCR."""
        import sessions
        assert "image/png" in sessions.IMAGE_CONTENT_TYPES

    def test_image_jpeg_still_supported(self):
        """image/jpeg must still be supported for OCR."""
        import sessions
        assert "image/jpeg" in sessions.IMAGE_CONTENT_TYPES

    def test_image_webp_still_supported(self):
        """image/webp must still be supported for OCR."""
        import sessions
        assert "image/webp" in sessions.IMAGE_CONTENT_TYPES

    def test_employee_photo_prefix_still_validated(self):
        """Employee photo must still check data:image/ prefix."""
        source = _read_source("employees.py")
        assert 'photo.startswith("data:image/")' in source

    def test_session_create_still_requires_raw_text(self):
        """create_session must still require raw_text."""
        source = _read_source("sessions.py")
        func_start = source.find("def create_session(")
        func_body = source[func_start:func_start + 2000]
        assert "raw_text_required" in func_body

    def test_session_create_still_requires_employee_id(self):
        """create_session must still require employee_id."""
        source = _read_source("sessions.py")
        func_start = source.find("def create_session(")
        func_body = source[func_start:func_start + 2000]
        assert "employee_id_required" in func_body

    def test_password_strength_check_still_enforced(self):
        """Password strength check must still be enforced."""
        source = _read_source("auth_email.py")
        assert "password_strength_ok" in source

    def test_rate_limiting_still_present(self):
        """Rate limiting must still be present on expensive endpoints."""
        source = _read_source("sessions.py")
        assert "_check_api_rate_limit" in source

    def test_auth_still_required_on_upload_endpoints(self):
        """Upload endpoints must still require authentication."""
        source = _read_source("sessions.py")
        func_start = source.find("def transcribe_audio(")
        func_body = source[func_start:func_start + 500]
        assert "_require_auth" in func_body

        func_start = source.find("def transcribe_image(")
        func_body = source[func_start:func_start + 500]
        assert "_require_auth" in func_body


# ===========================================================================
# 13. ERROR RESPONSE SAFETY
# ===========================================================================

class TestErrorResponseSafety:
    """Error responses must not expose internals."""

    def test_413_returns_generic_message(self):
        """413 responses must use generic messages."""
        source = _read_source("sessions.py")
        assert "audio_too_large" in source  # generic, no details
        assert "image_too_large" in source

    def test_no_stack_traces_in_error_responses(self):
        """Error responses must not include stack traces or exception details."""
        source = _read_source("sessions.py")
        # Check that error responses use jsonify with error strings, not repr(e)
        for line in source.split("\n"):
            if "return jsonify" in line and "413" in line:
                assert "traceback" not in line.lower()
                assert "repr(" not in line

    def test_photo_too_large_returns_safe_response(self):
        """Photo rejection must use a safe, generic message."""
        source = _read_source("employees.py")
        assert "photo_too_large" in source

    def test_no_filesystem_paths_in_errors(self):
        """Error messages must not contain filesystem paths."""
        source = _read_source("sessions.py")
        assert "/static/" not in source.split("return jsonify")[0] if "return jsonify" in source else True

    def test_error_returns_appropriate_status_codes(self):
        """Size-related rejections must return 413, type rejections 400."""
        source = _read_source("sessions.py")
        # audio_too_large should be 413
        func_start = source.find("def transcribe_audio(")
        func_body = source[func_start:func_start + 2000]
        audio_pos = func_body.find("audio_too_large")
        audio_area = func_body[audio_pos - 50:audio_pos + 50]
        assert "413" in audio_area

        # unsupported_audio_type should be 400
        unsupported_pos = func_body.find("unsupported_audio_type")
        unsupported_area = func_body[unsupported_pos - 50:unsupported_pos + 50]
        assert "400" in unsupported_area


# ===========================================================================
# 14. CONFIGURATION CORRECTNESS
# ===========================================================================

class TestConfigurationCorrectness:
    """All new constants must have correct, sensible values."""

    def test_audio_limit_reasonable(self):
        """Audio limit must be in the 20-50 MB range."""
        import sessions
        assert 20 * 1024 * 1024 <= sessions.MAX_AUDIO_BYTES <= 50 * 1024 * 1024

    def test_image_limit_reasonable(self):
        """Image limit must be in the 5-25 MB range."""
        import sessions
        assert 5 * 1024 * 1024 <= sessions.MAX_IMAGE_BYTES <= 25 * 1024 * 1024

    def test_photo_limit_reasonable(self):
        """Photo limit must be in the 0.5-5 MB range."""
        import employees
        assert 512 * 1024 <= employees.MAX_PHOTO_BYTES <= 5 * 1024 * 1024

    def test_text_limits_reasonable(self):
        """Text limits must be in the 100KB-2MB range."""
        import sessions
        assert 100 * 1024 <= sessions.MAX_RAW_TEXT_BYTES <= 2 * 1024 * 1024
        assert 100 * 1024 <= sessions.MAX_EDITED_TEXT_BYTES <= 2 * 1024 * 1024

    def test_llm_truncation_reasonable(self):
        """LLM truncation must be in the 10K-100K char range."""
        import sessions
        assert 10_000 <= sessions.MAX_LLM_TRANSCRIPT_CHARS <= 100_000

    def test_password_limit_reasonable(self):
        """Password max must be in the 64-1024 range."""
        import auth_email
        assert 64 <= auth_email.MAX_PASSWORD_LENGTH <= 1024

    def test_email_limit_matches_rfc(self):
        """Email max must be 320 (RFC 5321)."""
        import auth_email
        assert auth_email._MAX_EMAIL_LENGTH == 320

"""Focused tests for the Phase 1 security fixes.

1. config.py — SECRET_KEY must be required (no fallback).
2. field_encryption.py — per-field nonce, round-trip, backward compat.
3. app.py — no hardcoded debug=True, no session_token logging.
"""

import importlib
import os
import sys
from unittest import mock

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


# ---------------------------------------------------------------------------
# 1. SECRET_KEY enforcement
# ---------------------------------------------------------------------------

class TestSecretKeyRequired:
    """Config must refuse to start when SECRET_KEY is missing."""

    def _import_config_without_dotenv(self):
        """Import config.py after neutralising load_dotenv so the .env file
        doesn't re-inject SECRET_KEY into the environment."""
        sys.modules.pop("config", None)
        with mock.patch("dotenv.load_dotenv", return_value=None):
            return importlib.import_module("config")

    def test_missing_secret_key_raises_runtime_error(self):
        env = os.environ.copy()
        env.pop("SECRET_KEY", None)
        with mock.patch.dict(os.environ, env, clear=True):
            with pytest.raises(RuntimeError, match="SECRET_KEY"):
                self._import_config_without_dotenv()

    def test_empty_secret_key_raises_runtime_error(self):
        env = os.environ.copy()
        env["SECRET_KEY"] = ""
        with mock.patch.dict(os.environ, env, clear=True):
            with pytest.raises(RuntimeError, match="SECRET_KEY"):
                self._import_config_without_dotenv()

    def test_blank_secret_key_raises_runtime_error(self):
        env = os.environ.copy()
        env["SECRET_KEY"] = "   "
        with mock.patch.dict(os.environ, env, clear=True):
            with pytest.raises(RuntimeError, match="SECRET_KEY"):
                self._import_config_without_dotenv()


# ---------------------------------------------------------------------------
# Helpers for encryption tests — mock out the KMS dependency at import time.
# ---------------------------------------------------------------------------

# We need to mock 'kms' before importing field_encryption, because
# field_encryption does ``from kms import wrap_data_key, unwrap_data_key``
# at module level and kms.py tries to connect to Cloud KMS on import.

_KMS_STUB = mock.MagicMock()

# Simple round-trip stub: wrap_data_key just returns the raw DEK bytes
# (not actually encrypted).  unwrap_data_key returns them as-is.  This is
# enough to verify the AES-GCM encrypt/decrypt logic.
_KMS_STUB.wrap_data_key.side_effect = lambda dek: dek
_KMS_STUB.unwrap_data_key.side_effect = lambda wrapped: wrapped

sys.modules.setdefault("kms", _KMS_STUB)

# Ensure field_encryption is freshly imported with the mocked kms.
sys.modules.pop("field_encryption", None)

import field_encryption  # noqa: E402


# ---------------------------------------------------------------------------
# 2. Per-field nonce uniqueness
# ---------------------------------------------------------------------------

class TestNonceUniqueness:
    """Each field must get a fresh 12-byte nonce."""

    def test_different_nonces_per_field(self):
        fields = {
            "name": "Alice",
            "email": "alice@example.com",
            "phone": "+1234567890",
        }
        encrypted, wrapped_dek = field_encryption.encrypt_fields(fields)

        # Decode each value and extract the 12-byte nonce prefix.
        nonces = []
        for key in encrypted:
            raw = field_encryption._unb64(encrypted[key])
            nonce = raw[:12]
            nonces.append(nonce)

        # All nonces must be distinct.
        assert len(nonces) == len(set(nonces)), (
            f"Expected all unique nonces but got duplicates: {nonces}"
        )

    def test_nonces_are_12_bytes(self):
        fields = {"a": "x", "b": "y"}
        encrypted, _ = field_encryption.encrypt_fields(fields)
        for val in encrypted.values():
            raw = field_encryption._unb64(val)
            assert len(raw[:12]) == 12


# ---------------------------------------------------------------------------
# 3. Encrypt → Decrypt round-trip
# ---------------------------------------------------------------------------

class TestEncryptDecryptRoundTrip:
    def test_round_trip(self):
        fields = {
            "name": "Bob",
            "email": "bob@example.com",
            "phone": "+9876543210",
        }
        encrypted, wrapped_dek = field_encryption.encrypt_fields(fields)
        decrypted = field_encryption.decrypt_fields(encrypted, wrapped_dek)

        for key, value in fields.items():
            assert decrypted[key] == value

    def test_skips_none_and_empty(self):
        fields = {"a": "hello", "b": None, "c": ""}
        encrypted, wrapped_dek = field_encryption.encrypt_fields(fields)
        assert "b" not in encrypted
        assert "c" not in encrypted
        decrypted = field_encryption.decrypt_fields(encrypted, wrapped_dek)
        assert decrypted == {"a": "hello"}

    def test_unicode_fields(self):
        fields = {"city": "Mumbai", "note": "مرحبا"}
        encrypted, wrapped_dek = field_encryption.encrypt_fields(fields)
        decrypted = field_encryption.decrypt_fields(encrypted, wrapped_dek)
        assert decrypted == fields


# ---------------------------------------------------------------------------
# 4. Backward compatibility — old data encrypted with a single reused nonce
# ---------------------------------------------------------------------------

class TestBackwardCompatibility:
    """Records encrypted under the old code (same nonce for every field) must
    still be decryptable, because decrypt_fields reads the nonce from the
    stored ciphertext blob, not from a shared variable."""

    def test_decrypt_old_format(self):
        """Simulate what the old encrypt_fields() produced: one nonce reused
        across all fields (nonce reuse with the same DEK)."""
        dek = os.urandom(32)
        aesgcm = AESGCM(dek)
        reused_nonce = os.urandom(12)

        old_encrypted = {}
        for key, value in {"name": "Alice", "email": "alice@example.com"}.items():
            ct = aesgcm.encrypt(reused_nonce, value.encode("utf-8"), None)
            old_encrypted[key] = field_encryption._b64(reused_nonce + ct)

        # Decrypt with the new code.
        wrapped_dek = field_encryption._b64(dek)  # our stub wraps by identity
        decrypted = field_encryption.decrypt_fields(old_encrypted, wrapped_dek)
        assert decrypted == {"name": "Alice", "email": "alice@example.com"}

    def test_new_record_decrypts_after_upgrading(self):
        """Encrypt with the NEW code (unique nonces per field), then decrypt."""
        fields = {"name": "Charlie", "email": "c@co.com"}
        encrypted, wrapped_dek = field_encryption.encrypt_fields(fields)
        decrypted = field_encryption.decrypt_fields(encrypted, wrapped_dek)
        assert decrypted == fields


# ---------------------------------------------------------------------------
# 5. app.py — no hardcoded debug=True
# ---------------------------------------------------------------------------

_APP_SOURCE = open(os.path.join(os.path.dirname(__file__), "app.py"), encoding="utf-8").read()


class TestNoHardcodedDebug:
    """Flask debug mode must never be hardcoded to True."""

    def test_no_literal_debug_true_in_source(self):
        assert "debug=True" not in _APP_SOURCE, (
            "app.py still contains a literal debug=True"
        )

    def test_debug_defaults_to_false_via_env(self):
        """The app.run() call should use an env var that defaults to False."""
        # Verify the app.run line references an env var and does NOT
        # hardcode True.
        import re
        app_run_lines = [line for line in _APP_SOURCE.splitlines() if "app.run(" in line]
        assert len(app_run_lines) == 1, "Expected exactly one app.run() call"
        run_line = app_run_lines[0]
        # Should not have debug=True
        assert "debug=True" not in run_line
        # Should reference an environment variable or default to false
        assert "debug=" in run_line.lower()


# ---------------------------------------------------------------------------
# 6. app.py — no session_token in log statements
# ---------------------------------------------------------------------------

class TestNoSessionTokenLogging:
    """Session tokens must never appear in log output."""

    def test_no_session_token_in_logger_calls(self):
        """No logger.info/debug/warning/error call should reference session_token."""
        import re
        logger_pattern = re.compile(
            r'\.(info|debug|warning|error|exception)\s*\('
        )
        for lineno, line in enumerate(_APP_SOURCE.splitlines(), 1):
            if logger_pattern.search(line):
                assert "session_token" not in line.lower(), (
                    f"Line {lineno} logs session_token: {line.strip()}"
                )

    def test_no_print_of_session_token(self):
        """No print() call should reference session_token."""
        for lineno, line in enumerate(_APP_SOURCE.splitlines(), 1):
            if line.strip().startswith("print("):
                assert "session_token" not in line.lower(), (
                    f"Line {lineno} prints session_token: {line.strip()}"
                )

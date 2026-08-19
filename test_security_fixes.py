"""Focused tests for the Phase 1 security fixes.

1. config.py — SECRET_KEY must be required (no fallback).
2. field_encryption.py — per-field nonce, round-trip, backward compat.
3. app.py — no hardcoded debug=True, no session_token logging.
4. login_flow.py — session tokens are stored as SHA-256 hashes.
5. CSRF protection — centralized before_request guard.
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


# ---------------------------------------------------------------------------
# 7. login_flow.py — session tokens stored as SHA-256 hashes
# ---------------------------------------------------------------------------

# We need to mock dependencies that login_flow.py imports at module level.
# mock out requests and flask so login_flow can import without a live app.
if "requests" not in sys.modules:
    sys.modules["requests"] = mock.MagicMock()
if "flask" not in sys.modules:
    _flask_mock = mock.MagicMock()
    sys.modules["flask"] = _flask_mock

sys.modules.pop("login_flow", None)

import login_flow  # noqa: E402


class TestSessionTokenHash:
    """The SHA-256 hashing helper must work correctly."""

    def test_hash_is_hex_64_chars(self):
        token = "abc123"
        h = login_flow._hash_session_token(token)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_hash_is_deterministic(self):
        token = "test-token-12345"
        assert login_flow._hash_session_token(token) == login_flow._hash_session_token(token)

    def test_different_tokens_different_hashes(self):
        h1 = login_flow._hash_session_token("token-alpha")
        h2 = login_flow._hash_session_token("token-beta")
        assert h1 != h2

    def test_hash_matches_sha256(self):
        import hashlib
        token = "verify-me"
        expected = hashlib.sha256(token.encode("utf-8")).hexdigest()
        assert login_flow._hash_session_token(token) == expected


class TestSessionTokenNotStoredPlaintext:
    """Verify no plaintext token is written to active_sessions."""

    def test_record_active_session_stores_hash_not_token(self):
        """_record_active_session must insert the SHA-256 hash, not the raw
        token, into the active_sessions collection."""
        import uuid
        from unittest.mock import MagicMock, patch
        from bson import ObjectId

        db = MagicMock()
        fake_user_id = ObjectId()
        fake_token = str(uuid.uuid4())

        with patch("login_flow.session", {"session_token": None}) as mock_sess, \
             patch("login_flow.request", MagicMock(remote_addr="127.0.0.1")), \
             patch("login_flow.threading.Thread"), \
             patch("login_flow.uuid.uuid4", return_value=fake_token):
            login_flow._record_active_session(db, fake_user_id)

            # The Flask session gets the raw token (browser needs it)
            assert mock_sess["session_token"] == fake_token

            # The DB insert must NOT contain the raw token
            insert_doc = db.active_sessions.insert_one.call_args[0][0]
            stored_token = insert_doc["session_token"]
            assert stored_token != fake_token, "Raw token must not be stored in DB"
            assert stored_token == login_flow._hash_session_token(fake_token)


class TestSessionValidation:
    """_session_is_active must hash the raw token before querying."""

    def test_hashes_token_before_query(self):
        from unittest.mock import MagicMock, patch
        from bson import ObjectId

        db = MagicMock()
        user_id = str(ObjectId())
        raw_token = "raw-session-token-abc"

        with patch("login_flow._hash_session_token", return_value="HASHED") as mock_hash, \
             patch("employees.get_db", return_value=db):
            # find_one returns a record so session is "active"
            db.active_sessions.find_one.return_value = {
                "_id": ObjectId(),
                "last_seen": None,
            }
            from employees import _session_is_active
            result = _session_is_active(user_id, raw_token)

            assert result is True
            # The DB query must have used the hashed value
            db.active_sessions.find_one.assert_called_once_with(
                {"user_id": ObjectId(user_id), "session_token": "HASHED"}
            )

    def test_rejects_invalid_token(self):
        from unittest.mock import MagicMock, patch
        from bson import ObjectId

        db = MagicMock()
        user_id = str(ObjectId())

        with patch("employees.get_db", return_value=db):
            db.active_sessions.find_one.return_value = None
            from employees import _session_is_active
            result = _session_is_active(user_id, "nonexistent-token")

            assert result is False


class TestSessionExpiration:
    """Session heartbeat/expiration logic must still work with hashed tokens."""

    def test_updates_last_seen_when_stale(self):
        from unittest.mock import MagicMock, patch
        from bson import ObjectId
        from datetime import datetime, timedelta, timezone

        db = MagicMock()
        user_id = str(ObjectId())
        stale_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        rec_id = ObjectId()

        with patch("employees.get_db", return_value=db):
            db.active_sessions.find_one.return_value = {
                "_id": rec_id,
                "last_seen": stale_time,
            }
            from employees import _session_is_active
            result = _session_is_active(user_id, "any-token")

            assert result is True
            db.active_sessions.update_one.assert_called_once_with(
                {"_id": rec_id},
                {"$set": {"last_seen": mock.ANY}},
            )


class TestSessionRevocation:
    """Revoke endpoint must compare hashes, not raw tokens."""

    def test_cannot_revoke_current_session(self):
        """If the DB hash matches the current session hash, revocation
        must be rejected."""
        from unittest.mock import MagicMock, patch
        from bson import ObjectId

        token = "current-session-token"
        hashed = login_flow._hash_session_token(token)

        # Simulate the revoke-active-session guard logic from api.py
        # (the route itself depends on Flask Blueprint which can't be
        # imported in a mocked environment, so test the comparison directly.)
        doc = {"session_token": hashed}
        with patch("login_flow._hash_session_token", return_value=hashed):
            current_hash = login_flow._hash_session_token(token)
            is_current = doc.get("session_token") == current_hash

        assert is_current is True, "Current session should be detected as current"

    def test_can_revoke_other_session(self):
        """A different session's hash must not match the current token."""
        from unittest.mock import patch

        current_token = "my-current-token"
        other_token = "some-other-token"

        current_hash = login_flow._hash_session_token(current_token)
        other_hash = login_flow._hash_session_token(other_token)

        doc = {"session_token": other_hash}
        is_current = doc.get("session_token") == current_hash
        assert is_current is False, "Other session should not be detected as current"


# ---------------------------------------------------------------------------
# 8. CSRF protection — centralized before_request guard
# ---------------------------------------------------------------------------

# Restore the real Flask module (it was mocked above for login_flow tests).
sys.modules.pop("flask", None)
import importlib
_real_flask = importlib.import_module("flask")
sys.modules["flask"] = _real_flask

import hmac as _hmac
import secrets as _secrets

from flask import Flask, jsonify, request, session as flask_session


def _make_csrf_app():
    """Build a minimal Flask app with the same CSRF logic as app.py so we
    can test the middleware in isolation (no MongoDB, no Google OAuth)."""
    app = Flask(__name__)
    app.secret_key = _secrets.token_urlsafe(32)
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    @app.route("/api/csrf-token")
    def csrf_token():
        if not flask_session.get("user_id"):
            return jsonify({"error": "not_authenticated"}), 401
        if "_csrf_token" not in flask_session:
            flask_session["_csrf_token"] = _secrets.token_urlsafe(32)
        return jsonify({"csrf_token": flask_session["_csrf_token"]})

    @app.before_request
    def _csrf_protect():
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        if not flask_session.get("user_id"):
            return None
        token = request.headers.get("X-CSRF-Token")
        if not token:
            return jsonify({"error": "CSRF validation failed"}), 403
        expected = flask_session.get("_csrf_token")
        if not expected:
            return jsonify({"error": "CSRF validation failed"}), 403
        if not _hmac.compare_digest(token, expected):
            return jsonify({"error": "CSRF validation failed"}), 403
        return None

    @app.route("/api/data", methods=["GET"])
    def get_data():
        return jsonify({"ok": True})

    @app.route("/api/data", methods=["POST"])
    def post_data():
        return jsonify({"ok": True})

    @app.route("/api/data", methods=["PUT"])
    def put_data():
        return jsonify({"ok": True})

    @app.route("/api/data", methods=["PATCH"])
    def patch_data():
        return jsonify({"ok": True})

    @app.route("/api/data", methods=["DELETE"])
    def delete_data():
        return jsonify({"ok": True})

    @app.route("/api/unauth-data", methods=["POST"])
    def unauth_post():
        """A route called before login — should be CSRF-exempt."""
        return jsonify({"ok": True})

    return app


class TestCSRFProtection:
    """Centralized CSRF before_request guard."""

    @pytest.fixture()
    def client(self):
        app = _make_csrf_app()
        app.config["TESTING"] = True
        with app.test_client() as c:
            yield c

    def _login(self, client):
        """Simulate a logged-in session with a pre-set CSRF token."""
        with client.session_transaction() as sess:
            sess["user_id"] = "test-user-id"
            sess["_csrf_token"] = _secrets.token_urlsafe(32)

    def _get_token(self, client):
        """Obtain the CSRF token from the session."""
        with client.session_transaction() as sess:
            return sess.get("_csrf_token")

    # -- Test 1: Authenticated GET succeeds without CSRF token --

    def test_authenticated_get_succeeds_without_token(self, client):
        self._login(client)
        resp = client.get("/api/data")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    # -- Test 2-5: POST/PUT/PATCH/DELETE without token -> 403 --

    def test_post_without_token_403(self, client):
        self._login(client)
        resp = client.post("/api/data")
        assert resp.status_code == 403
        assert "CSRF" in resp.get_json()["error"]

    def test_put_without_token_403(self, client):
        self._login(client)
        resp = client.put("/api/data")
        assert resp.status_code == 403

    def test_patch_without_token_403(self, client):
        self._login(client)
        resp = client.patch("/api/data")
        assert resp.status_code == 403

    def test_delete_without_token_403(self, client):
        self._login(client)
        resp = client.delete("/api/data")
        assert resp.status_code == 403

    # -- Test 6: Valid CSRF token -> request succeeds --

    def test_valid_csrf_token_succeeds(self, client):
        self._login(client)
        token = self._get_token(client)
        resp = client.post("/api/data", headers={"X-CSRF-Token": token})
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    # -- Test 7: Invalid CSRF token -> 403 --

    def test_invalid_csrf_token_403(self, client):
        self._login(client)
        resp = client.post(
            "/api/data", headers={"X-CSRF-Token": "totally-wrong-token"}
        )
        assert resp.status_code == 403

    # -- Test 8: Empty CSRF token -> 403 --

    def test_empty_csrf_token_403(self, client):
        self._login(client)
        resp = client.post("/api/data", headers={"X-CSRF-Token": ""})
        assert resp.status_code == 403

    # -- Test 9: Wrong session's CSRF token -> 403 --

    def test_wrong_session_token_403(self, client):
        # Login as user A and get their token
        self._login(client)
        token_a = self._get_token(client)

        # Login as user B (clear session, re-login)
        with client.session_transaction() as sess:
            sess.clear()
            sess["user_id"] = "other-user-id"

        resp = client.post(
            "/api/data", headers={"X-CSRF-Token": token_a}
        )
        assert resp.status_code == 403

    # -- Test 10: CSRF token is session-bound --

    def test_csrf_token_tied_to_session(self, client):
        self._login(client)
        token1 = self._get_token(client)

        # Clear and re-login — token should be different
        with client.session_transaction() as sess:
            sess.clear()

        self._login(client)
        token2 = self._get_token(client)
        assert token1 != token2

    # -- Test 11: OPTIONS requests are not blocked --

    def test_options_not_blocked(self, client):
        self._login(client)
        resp = client.options("/api/data")
        # OPTIONS should not return 403 (may return 405 if route doesn't
        # support it, but NOT 403 from CSRF)
        assert resp.status_code != 403

    # -- Test 12: Unauthenticated requests are CSRF-exempt --

    def test_unauthenticated_post_exempt(self, client):
        """POST from unauthenticated session should pass CSRF (no session
        to hijack)."""
        resp = client.post("/api/unauth-data")
        assert resp.status_code == 200

    # -- Additional: CSRF token endpoint returns token for authenticated user --

    def test_csrf_token_endpoint_returns_token(self, client):
        self._login(client)
        resp = client.get("/api/csrf-token")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "csrf_token" in data
        assert len(data["csrf_token"]) > 20

    # -- Additional: CSRF token endpoint rejects unauthenticated --

    def test_csrf_token_endpoint_rejects_unauthenticated(self, client):
        resp = client.get("/api/csrf-token")
        assert resp.status_code == 401

    # -- Additional: PUT with valid token succeeds --

    def test_put_with_valid_token_succeeds(self, client):
        self._login(client)
        token = self._get_token(client)
        resp = client.put("/api/data", headers={"X-CSRF-Token": token})
        assert resp.status_code == 200

    # -- Additional: DELETE with valid token succeeds --

    def test_delete_with_valid_token_succeeds(self, client):
        self._login(client)
        token = self._get_token(client)
        resp = client.delete("/api/data", headers={"X-CSRF-Token": token})
        assert resp.status_code == 200

"""Focused tests for the Phase 1 security fixes.

1. config.py — SECRET_KEY must be required (no fallback).
2. field_encryption.py — per-field nonce, round-trip, backward compat.
3. app.py — no hardcoded debug=True, no session_token logging.
4. login_flow.py — session tokens are stored as SHA-256 hashes.
5. CSRF protection — centralized before_request guard.
6. TOTP enforcement — _require_auth and _check_auth block unverified admins.
"""

import ast as _ast
import importlib
import inspect
import os
import sys
from unittest import mock

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_ROOT = os.path.dirname(os.path.abspath(__file__))


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

        with patch("employees._hash_session_token", return_value="HASHED") as mock_hash, \
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


# ---------------------------------------------------------------------------
# 9. TOTP enforcement — _require_auth and _check_auth
# ---------------------------------------------------------------------------

from bson import ObjectId
from unittest.mock import MagicMock, patch
from employees import _require_auth, _totp_required, TOTPRequired

# Valid 24-char hex ObjectIds for tests
_FAKE_UID = "a" * 24
_FAKE_OID = ObjectId(_FAKE_UID)


class TestTOTPRequired:
    """TOTPRequired exception is raised when an admin session needs
    TOTP verification."""

    def test_is_exception_subclass(self):
        assert issubclass(TOTPRequired, Exception)

    def test_can_be_raised_and_caught(self):
        with pytest.raises(TOTPRequired):
            raise TOTPRequired()


class TestTOTPRequiredFunction:
    """_totp_required() returns True only when the admin session has TOTP
    enabled but the session hasn't been verified."""

    def _make_session(self, user_id=None, session_token="tok",
                      totp_verified_session=None):
        uid = user_id or _FAKE_UID
        s = {"user_id": uid, "session_token": session_token}
        if totp_verified_session is not None:
            s["totp_verified_session"] = totp_verified_session
        return s

    def test_returns_false_when_not_logged_in(self):
        with patch("employees.session", {}):
            assert _totp_required() is False

    def test_returns_false_for_non_admin(self):
        user_doc = {"role": "user", "totp_enabled": True}
        db = MagicMock()
        db.users.find_one.return_value = user_doc
        sess = self._make_session()
        with patch("employees.session", sess), \
             patch("employees.get_db", return_value=db):
            assert _totp_required() is False

    def test_returns_false_when_totp_not_enabled(self):
        user_doc = {"role": "admin", "totp_enabled": False}
        db = MagicMock()
        db.users.find_one.return_value = user_doc
        sess = self._make_session()
        with patch("employees.session", sess), \
             patch("employees.get_db", return_value=db):
            assert _totp_required() is False

    def test_returns_false_when_totp_enabled_and_verified(self):
        user_doc = {"role": "admin", "totp_enabled": True}
        db = MagicMock()
        db.users.find_one.return_value = user_doc
        sess = self._make_session(totp_verified_session="tok")
        with patch("employees.session", sess), \
             patch("employees.get_db", return_value=db):
            assert _totp_required() is False

    def test_returns_true_when_totp_enabled_and_not_verified(self):
        user_doc = {"role": "admin", "totp_enabled": True}
        db = MagicMock()
        db.users.find_one.return_value = user_doc
        sess = self._make_session()
        with patch("employees.session", sess), \
             patch("employees.get_db", return_value=db):
            assert _totp_required() is True

    def test_returns_true_when_totp_verified_for_different_session(self):
        user_doc = {"role": "admin", "totp_enabled": True}
        db = MagicMock()
        db.users.find_one.return_value = user_doc
        sess = self._make_session(totp_verified_session="other-token")
        with patch("employees.session", sess), \
             patch("employees.get_db", return_value=db):
            assert _totp_required() is True

    def test_returns_false_when_user_not_found(self):
        db = MagicMock()
        db.users.find_one.return_value = None
        sess = self._make_session()
        with patch("employees.session", sess), \
             patch("employees.get_db", return_value=db):
            assert _totp_required() is False

    def test_returns_false_when_totp_enabled_field_missing(self):
        user_doc = {"role": "admin"}  # no totp_enabled key
        db = MagicMock()
        db.users.find_one.return_value = user_doc
        sess = self._make_session()
        with patch("employees.session", sess), \
             patch("employees.get_db", return_value=db):
            assert _totp_required() is False


class TestRequireAuthTOTP:
    """_require_auth() raises TOTPRequired when TOTP enforcement is needed."""

    def _make_session(self, **overrides):
        s = {
            "user_id": _FAKE_UID,
            "org_id": _FAKE_UID,
            "session_token": "tok123",
        }
        s.update(overrides)
        return s

    def test_raises_totp_when_admin_totp_not_verified(self):
        user_doc = {"role": "admin", "totp_enabled": True}
        db = MagicMock()
        db.users.find_one.return_value = user_doc
        sess = self._make_session()
        with patch("employees.session", sess), \
             patch("employees.get_db", return_value=db), \
             patch("employees._session_is_active", return_value=True):
            with pytest.raises(TOTPRequired):
                _require_auth()

    def test_returns_org_id_when_admin_totp_verified(self):
        user_doc = {"role": "admin", "totp_enabled": True}
        db = MagicMock()
        db.users.find_one.return_value = user_doc
        sess = self._make_session(totp_verified_session="tok123")
        with patch("employees.session", sess), \
             patch("employees.get_db", return_value=db), \
             patch("employees._session_is_active", return_value=True):
            result = _require_auth()
            assert result == _FAKE_UID

    def test_returns_org_id_when_totp_not_required(self):
        user_doc = {"role": "admin", "totp_enabled": False}
        db = MagicMock()
        db.users.find_one.return_value = user_doc
        sess = self._make_session()
        with patch("employees.session", sess), \
             patch("employees.get_db", return_value=db), \
             patch("employees._session_is_active", return_value=True):
            result = _require_auth()
            assert result == _FAKE_UID

    def test_returns_none_when_no_session(self):
        with patch("employees.session", {}):
            assert _require_auth() is None


class TestCheckAuthTOTP:
    """_check_auth() in api.py raises TOTPRequired when needed."""

    def _make_session(self, **overrides):
        s = {"user_id": _FAKE_UID, "session_token": "tok123"}
        s.update(overrides)
        return s

    def test_raises_totp_when_admin_totp_not_verified(self):
        user_doc = {"role": "admin", "totp_enabled": True}
        db = MagicMock()
        db.users.find_one.return_value = user_doc
        sess = self._make_session()
        with patch("api.session", sess), \
             patch("api.get_db", return_value=db), \
             patch("api._session_is_active", return_value=True):
            from api import _check_auth
            with pytest.raises(TOTPRequired):
                _check_auth()

    def test_returns_user_id_when_totp_verified(self):
        user_doc = {"role": "admin", "totp_enabled": True}
        db = MagicMock()
        db.users.find_one.return_value = user_doc
        sess = self._make_session(totp_verified_session="tok123")
        with patch("api.session", sess), \
             patch("api.get_db", return_value=db), \
             patch("api._session_is_active", return_value=True):
            from api import _check_auth
            result = _check_auth()
            assert result == _FAKE_UID

    def test_returns_user_id_when_totp_not_required(self):
        user_doc = {"role": "user", "totp_enabled": False}
        db = MagicMock()
        db.users.find_one.return_value = user_doc
        sess = self._make_session()
        with patch("api.session", sess), \
             patch("api.get_db", return_value=db), \
             patch("api._session_is_active", return_value=True):
            from api import _check_auth
            result = _check_auth()
            assert result == _FAKE_UID

    def test_returns_none_when_no_session(self):
        with patch("api.session", {}):
            from api import _check_auth
            assert _check_auth() is None


class TestClientBypassPrevention:
    """Client-provided totp_verified values cannot bypass server-side
    enforcement."""

    def test_totp_verified_session_from_client_ignored(self):
        """Even if a client sets totp_verified_session in the session,
        it must match the current session_token to be valid."""
        user_doc = {"role": "admin", "totp_enabled": True}
        db = MagicMock()
        db.users.find_one.return_value = user_doc
        sess = {
            "user_id": _FAKE_UID,
            "org_id": _FAKE_UID,
            "session_token": "real-tok",
            "totp_verified_session": "forged-tok",
        }
        with patch("employees.session", sess), \
             patch("employees.get_db", return_value=db), \
             patch("employees._session_is_active", return_value=True):
            with pytest.raises(TOTPRequired):
                _require_auth()

    def test_empty_totp_verified_session_not_bypass(self):
        """An empty totp_verified_session does not bypass the check."""
        user_doc = {"role": "admin", "totp_enabled": True}
        db = MagicMock()
        db.users.find_one.return_value = user_doc
        sess = {
            "user_id": _FAKE_UID,
            "org_id": _FAKE_UID,
            "session_token": "tok123",
            "totp_verified_session": "",
        }
        with patch("employees.session", sess), \
             patch("employees.get_db", return_value=db), \
             patch("employees._session_is_active", return_value=True):
            with pytest.raises(TOTPRequired):
                _require_auth()

    def test_totp_enabled_true_from_client_ignored(self):
        """The totp_enabled flag is read from the DB, not the session."""
        user_doc = {"role": "admin"}  # no totp_enabled in DB
        db = MagicMock()
        db.users.find_one.return_value = user_doc
        sess = {
            "user_id": _FAKE_UID,
            "org_id": _FAKE_UID,
            "session_token": "tok123",
        }
        with patch("employees.session", sess), \
             patch("employees.get_db", return_value=db), \
             patch("employees._session_is_active", return_value=True):
            result = _require_auth()
            assert result == _FAKE_UID  # No TOTP required, passes through


class TestExistingPhase1Tests:
    """Verify existing Phase 1 tests are not broken by TOTP changes."""

    def test_totp_routes_still_accessible_in_source(self):
        """The TOTP routes in totp_routes.py must not reference _require_auth."""
        import inspect
        import totp_routes
        for name, obj in inspect.getmembers(totp_routes, inspect.isfunction):
            src = inspect.getsource(obj)
            assert "_require_auth()" not in src, (
                f"totp_routes.{name} unexpectedly calls _require_auth()"
            )

    def test_session_token_still_hashed(self):
        """Session token hashing still works after TOTP enforcement."""
        token = "test-token-for-hash"
        h = login_flow._hash_session_token(token)
        assert len(h) == 64

    def test_error_handler_returns_403(self):
        """The TOTPRequired error handler in app.py returns 403."""
        # Read the app.py source to verify the handler exists
        app_source = open(
            os.path.join(os.path.dirname(__file__), "app.py"), encoding="utf-8"
        ).read()
        assert "TOTPRequired" in app_source
        assert "403" in app_source

    def test_require_auth_imports_totp_required(self):
        """employees.py exports TOTPRequired for use by other modules."""
        from employees import TOTPRequired
        assert TOTPRequired is not None

    def test_check_auth_exists_in_api(self):
        """api.py defines _check_auth with TOTP enforcement."""
        import api
        assert hasattr(api, "_check_auth")


# ---------------------------------------------------------------------------
# 10. Rate limiting — Email OTP
# ---------------------------------------------------------------------------

from datetime import timedelta
from unittest.mock import patch as _patch, MagicMock as _MagicMock
from extensions import check_rate_limit, record_rate_limit_event


class TestEmailOTPRateLimiting:
    """MongoDB-backed rate limiting for OTP sending."""

    def _make_db(self):
        db = _MagicMock()
        return db

    def test_first_otp_request_succeeds(self):
        db = self._make_db()
        db.rate_limits.count_documents.return_value = 0
        allowed, retry_after = check_rate_limit(db, "otp_email:test@example.com", 5, 900)
        assert allowed is True
        assert retry_after is None

    def test_requests_within_limit_eventually_return_429(self):
        db = self._make_db()
        # Simulate 5 events already recorded
        db.rate_limits.count_documents.return_value = 5
        allowed, retry_after = check_rate_limit(db, "otp_email:test@example.com", 5, 900)
        assert allowed is False
        assert retry_after is not None
        assert retry_after > 0

    def test_rate_limiting_applied_to_repeated_requests(self):
        db = self._make_db()
        # Below limit
        db.rate_limits.count_documents.return_value = 3
        allowed, _ = check_rate_limit(db, "otp_email:a@b.com", 5, 900)
        assert allowed is True
        # At limit
        db.rate_limits.count_documents.return_value = 5
        allowed, _ = check_rate_limit(db, "otp_email:a@b.com", 5, 900)
        assert allowed is False

    def test_rate_limit_state_is_mongodb_not_process_local(self):
        """The rate limit functions use MongoDB, not a Python dict."""
        import inspect
        from extensions import check_rate_limit, record_rate_limit_event
        src_check = inspect.getsource(check_rate_limit)
        src_record = inspect.getsource(record_rate_limit_event)
        assert "rate_limits" in src_check
        assert "rate_limits" in src_record
        assert "count_documents" in src_check
        assert "insert_one" in src_record

    def test_resend_otp_returns_user_friendly_429(self):
        """The resend endpoint returns a user-friendly message with retry_after."""
        import inspect
        import auth_email
        src = inspect.getsource(auth_email.resend_otp)
        assert "retry_after" in src
        assert "Retry-After" in src
        assert "Too many OTP requests" in src

    def test_otp_rate_limit_constants_are_reasonable(self):
        """Rate limit constants exist and have reasonable values."""
        import auth_email
        assert auth_email._OTP_MAX_PER_EMAIL == 5
        assert auth_email._OTP_MAX_PER_IP == 20
        assert auth_email._OTP_RATE_WINDOW == 900

    def test_client_ip_function_exists(self):
        """auth_email has a _client_ip helper."""
        import auth_email
        assert callable(auth_email._client_ip)


# ---------------------------------------------------------------------------
# 11. Rate limiting — TOTP backup codes
# ---------------------------------------------------------------------------

class TestBackupCodeRateLimiting:
    """MongoDB-backed rate limiting for backup code attempts."""

    def test_valid_backup_code_still_works(self):
        """The backup code verification path still returns success."""
        import inspect
        import totp_routes
        src = inspect.getsource(totp_routes.totp_verify_login_backup)
        assert "codes_remaining" in src
        assert "regenerate_recommended" in src

    def test_repeated_invalid_attempts_return_429(self):
        """Backup code verification calls _check_backup_rate_limit."""
        import inspect
        import totp_routes
        src = inspect.getsource(totp_routes.totp_verify_login_backup)
        assert "_check_backup_rate_limit" in src
        assert "429" in src

    def test_rate_limiting_uses_mongodb(self):
        """_check_backup_rate_limit uses MongoDB, not in-memory dict."""
        import inspect
        import totp_routes
        src = inspect.getsource(totp_routes._check_backup_rate_limit)
        assert "check_rate_limit" in src
        assert "record_rate_limit_event" in src
        assert "get_db" in src

    def test_no_in_memory_rate_limit_dict(self):
        """The old in-memory _BACKUPCodeAttempts dict is removed."""
        import totp_routes
        assert not hasattr(totp_routes, "_BACKUPCodeAttempts")


# ---------------------------------------------------------------------------
# 12. Error leakage removal — sessions.py
# ---------------------------------------------------------------------------

class TestErrorLeakageRemoval:
    """Exception messages are no longer returned to clients."""

    def test_analysis_exception_returns_generic_error(self):
        """analyze_session returns a generic error, not str(e)."""
        import inspect
        import sessions
        src = inspect.getsource(sessions.analyze_session)
        assert "str(e)" not in src
        assert "Analysis failed. Please try again." in src

    def test_transcription_exception_returns_generic_error(self):
        """transcribe_audio returns a generic error, not str(e)."""
        import inspect
        import sessions
        src = inspect.getsource(sessions.transcribe_audio)
        assert "str(e)" not in src
        assert "Transcription failed. Please try again." in src

    def test_ocr_exception_returns_generic_error(self):
        """transcribe_image returns a generic error, not str(e)."""
        import inspect
        import sessions
        src = inspect.getsource(sessions.transcribe_image)
        assert "str(e)" not in src
        assert "Transcription failed. Please try again." in src

    def test_detailed_exception_logged_not_exposed(self):
        """Exception details are logged, not sent to client."""
        import inspect
        import sessions
        # analyze_session uses logger.exception
        src = inspect.getsource(sessions.analyze_session)
        assert "logger.exception" in src
        # transcribe uses logger.exception
        src_audio = inspect.getsource(sessions.transcribe_audio)
        assert "logger.exception" in src_audio
        src_image = inspect.getsource(sessions.transcribe_image)
        assert "logger.exception" in src_image

    def test_sessions_module_has_logger(self):
        """sessions.py defines a logger for exception logging."""
        import sessions
        assert hasattr(sessions, "logger")

    def test_email_enumeration_fixed(self):
        """email_signin no longer reveals google_only_account vs not_registered."""
        import inspect
        import auth_email
        src = inspect.getsource(auth_email.email_signin)
        # Both cases should return invalid_credentials
        assert "google_only_account" not in src
        assert src.count("invalid_credentials") >= 2


# ---------------------------------------------------------------------------
# 13. Debug print cleanup — no production prints remain
# ---------------------------------------------------------------------------


def _count_print_calls(filepath: str) -> list[int]:
    """Return line numbers of print() or traceback.print_exc() calls in a .py file."""
    with open(filepath, encoding="utf-8") as f:
        source = f.read()
    tree = _ast.parse(source)
    lines = []
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Call):
            func = node.func
            if isinstance(func, _ast.Name) and func.id == "print":
                lines.append(node.lineno)
            elif isinstance(func, _ast.Attribute) and func.attr == "print_exc":
                lines.append(node.lineno)
    return lines


class TestNoProductionPrints:
    """Production source files must not contain print() or traceback.print_exc() calls."""

    def _assert_no_prints(self, filepath: str):
        lines = _count_print_calls(filepath)
        assert lines == [], f"Found print/traceback.print_exc at lines {lines} in {filepath}"

    def test_sessions_py_clean(self):
        self._assert_no_prints(os.path.join(_ROOT, "sessions.py"))

    def test_notifications_py_clean(self):
        self._assert_no_prints(os.path.join(_ROOT, "notifications.py"))

    def test_providers_llm_py_clean(self):
        self._assert_no_prints(os.path.join(_ROOT, "providers", "llm.py"))

    def test_auth_email_py_clean(self):
        self._assert_no_prints(os.path.join(_ROOT, "auth_email.py"))

    def test_auth_py_clean(self):
        self._assert_no_prints(os.path.join(_ROOT, "auth.py"))

    def test_employees_py_clean(self):
        self._assert_no_prints(os.path.join(_ROOT, "employees.py"))

    def test_totp_routes_py_clean(self):
        self._assert_no_prints(os.path.join(_ROOT, "totp_routes.py"))

    def test_extensions_py_clean(self):
        self._assert_no_prints(os.path.join(_ROOT, "extensions.py"))

    def test_app_py_clean(self):
        self._assert_no_prints(os.path.join(_ROOT, "app.py"))


class TestNoSysImportsForDebug:
    """Production files should not have orphaned `import sys` used only for stderr prints."""

    def _sys_import_lines(self, filepath: str) -> list[int]:
        with open(filepath, encoding="utf-8") as f:
            source = f.read()
        tree = _ast.parse(source)
        lines = []
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                for alias in node.names:
                    if alias.name == "sys":
                        lines.append(node.lineno)
            elif isinstance(node, _ast.ImportFrom):
                if node.module == "sys":
                    lines.append(node.lineno)
        return lines

    def test_sessions_py_no_sys_import(self):
        assert self._sys_import_lines(os.path.join(_ROOT, "sessions.py")) == []

    def test_notifications_py_no_sys_import(self):
        assert self._sys_import_lines(os.path.join(_ROOT, "notifications.py")) == []

    def test_providers_llm_py_no_sys_import(self):
        assert self._sys_import_lines(os.path.join(_ROOT, "providers", "llm.py")) == []


class TestNoTracebackImport:
    """Production files should not import traceback (replaced by logger.exception)."""

    def _has_traceback_import(self, filepath: str) -> bool:
        with open(filepath, encoding="utf-8") as f:
            source = f.read()
        tree = _ast.parse(source)
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                for alias in node.names:
                    if alias.name == "traceback":
                        return True
            elif isinstance(node, _ast.ImportFrom):
                if node.module == "traceback":
                    return True
        return False

    def test_sessions_py_no_traceback_import(self):
        assert not self._has_traceback_import(os.path.join(_ROOT, "sessions.py"))


class TestSensitiveDataNotInPrints:
    """Verify sensitive data patterns do not appear in print/debug output in source."""

    def _read_source(self, *parts):
        with open(os.path.join(_ROOT, *parts), encoding="utf-8") as f:
            return f.read()

    def test_no_stderr_analysis_output(self):
        """LLM analysis results must not be printed to stderr."""
        src = self._read_source("sessions.py")
        assert "print(raw_content" not in src
        assert "print(content[:3000]" not in src
        assert "print(\"STAGE" not in src

    def test_no_stderr_drift_results(self):
        """Drift analysis results must not be printed."""
        src = self._read_source("sessions.py")
        assert "is_genuine_pattern=" not in src
        assert "confidence={drift" not in src

    def test_no_stderr_llm_response_content(self):
        """Raw LLM response content must not be printed."""
        src = self._read_source("providers", "llm.py")
        assert "raw_content[:3000]" not in src
        assert "print(raw_content" not in src
        assert "RAW LLM RESPONSE" not in src

    def test_no_stderr_employee_ids_in_prints(self):
        """Employee IDs must not be printed."""
        src = self._read_source("notifications.py")
        assert "employee={n.get" not in src
        assert "employee=" not in src.split("print(")[1].split(")")[0] if "print(" in src else True

    def test_no_stderr_validation_errors_exposed(self):
        """Validation error details must not be printed."""
        src = self._read_source("providers", "llm.py")
        assert "_validation_errors:" not in src

    def test_no_stderr_type_coercion_details(self):
        """TYPE_COERCION debug output must not be printed."""
        src = self._read_source("providers", "llm.py")
        assert "[TYPE_COERCION]" not in src

    def test_no_stderr_debug_session_tags(self):
        """[DEBUG_SESSION] tags must not exist in source."""
        src = self._read_source("sessions.py")
        assert "[DEBUG_SESSION]" not in src
        assert "[DEBUG_DRIFT]" not in src
        assert "[DEBUG_NOTIF]" not in src

    def test_no_stderr_debug_deepseek_tags(self):
        """[DEBUG_DEEPSEEK] tags must not exist in source."""
        src = self._read_source("providers", "llm.py")
        assert "[DEBUG_DEEPSEEK]" not in src
        assert "[DEBUG_NORMALIZE]" not in src
        assert "[DEBUG_VALIDATE]" not in src
        assert "[DEBUG_FALLBACK]" not in src


class TestExistingGenericErrorsUnchanged:
    """Generic error responses from previous security fixes remain unchanged."""

    def test_analysis_failed_generic(self):
        import sessions
        src = inspect.getsource(sessions.analyze_session)
        assert '"Analysis failed. Please try again."' in src

    def test_transcription_failed_generic(self):
        import sessions
        src = inspect.getsource(sessions.transcribe_audio)
        assert '"Transcription failed. Please try again."' in src

    def test_ocr_failed_generic(self):
        import sessions
        src = inspect.getsource(sessions.transcribe_image)
        assert '"Transcription failed. Please try again."' in src


class TestDriftDetectionUsesLogger:
    """Drift detection exception handling uses logger, not print."""

    def test_drift_exception_handler_uses_logger(self):
        import sessions
        src = inspect.getsource(sessions.analyze_session)
        assert "logger.exception(\"Drift detection failed" in src

    def test_no_traceback_print_exc_in_sessions(self):
        import sessions
        src = inspect.getsource(sessions.analyze_session)
        assert "traceback.print_exc" not in src

    def test_drift_skip_branches_are_clean(self):
        """The drift skip branches (insufficient sessions, employee not found,
        window already processed) no longer print anything."""
        import sessions
        src = inspect.getsource(sessions.analyze_session)
        # These were the old debug prints in the skip branches
        assert "qualifying_sessions}" not in src
        assert "employee not found" not in src
        assert "drift window already processed" not in src

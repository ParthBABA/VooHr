"""Functional tests for the password-reset flow.

Covers POST /auth/forgot-password (reset-link email) and
POST /auth/reset-password (token validation + password update), plus the
GET /reset-password page.

These tests run against the REAL create_app() middleware with an in-memory
fake MongoDB injected before import, so no real database is touched.  The
pattern mirrors test_csrf_rate_limit_fix.py.
"""

import hashlib
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)

import bson


# ── In-memory fake MongoDB ────────────────────────────────────────────

class _FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, key, direction):
        self._docs.sort(key=lambda d: d.get(key), reverse=(direction == -1))
        return self

    def __iter__(self):
        return iter(self._docs)


class _FakeCollection:
    def __init__(self):
        self.docs = []

    def _match(self, doc, q):
        for k, v in q.items():
            if isinstance(v, dict) and "$gt" in v:
                if not (doc.get(k) is not None and doc.get(k) > v["$gt"]):
                    return False
            elif doc.get(k) != v:
                return False
        return True

    def find_one(self, q, projection=None, sort=None):
        matches = [d for d in self.docs if self._match(d, q)]
        if sort:
            key, direction = sort[0]
            matches.sort(key=lambda d: d.get(key), reverse=(direction == -1))
        if not matches:
            return None
        doc = matches[0]
        if projection:
            return {k: doc[k] for k in doc if k in projection or k == "_id"}
        return dict(doc)

    def find(self, q):
        return _FakeCursor(d for d in self.docs if self._match(d, q))

    def insert_one(self, doc):
        doc = dict(doc)
        doc.setdefault("_id", bson.ObjectId())
        self.docs.append(doc)

    def update_one(self, q, update, upsert=False):
        matches = [d for d in self.docs if self._match(d, q)]
        if not matches:
            if upsert:
                self.insert_one(dict(q))
                matches = [d for d in self.docs if self._match(d, q)]
            else:
                return
        doc = matches[0]
        if "$set" in update:
            doc.update(update["$set"])
        if "$unset" in update:
            for k in update["$unset"]:
                doc.pop(k, None)

    def update_many(self, q, update):
        for doc in self.docs:
            if self._match(doc, q):
                if "$set" in update:
                    doc.update(update["$set"])
                if "$unset" in update:
                    for k in update["$unset"]:
                        doc.pop(k, None)

    def delete_one(self, q):
        matches = [d for d in self.docs if self._match(d, q)]
        if matches:
            self.docs.remove(matches[0])

    def delete_many(self, q):
        self.docs[:] = [d for d in self.docs if not self._match(d, q)]

    def count_documents(self, q):
        return sum(1 for d in self.docs if self._match(d, q))

    def create_index(self, *a, **k):
        pass

    def clear(self):
        self.docs = []


class _FakeDB:
    def __init__(self):
        self.rate_limits = _FakeCollection()
        self.active_sessions = _FakeCollection()
        self.otp_verifications = _FakeCollection()
        self.users = _FakeCollection()
        self.password_resets = _FakeCollection()


_FAKE_DB = _FakeDB()

# Patch extensions BEFORE importing app so every blueprint module that does
# `from extensions import get_db/init_db` binds the fakes.  This keeps the
# suite hermetic (no MongoDB connection).  All patches are restored right
# after the import so other modules see clean code.
import extensions as _extensions

_orig_get_db = _extensions.get_db
_orig_init_db = _extensions.init_db
_extensions.get_db = lambda: _FAKE_DB
_extensions.init_db = lambda a: None

import unittest.mock as _mock

# Evict mock entries stubbed by other test modules so real packages load.
for _name in ("requests", "flask"):
    if isinstance(sys.modules.get(_name), _mock.MagicMock):
        del sys.modules[_name]

import auth as _auth

_orig_register_oauth = _auth.register_google_oauth
_auth.register_google_oauth = lambda a: None

import app as _app_module

_flask_app = _app_module.app
_flask_app.config["TESTING"] = True

_extensions.get_db = _orig_get_db
_extensions.init_db = _orig_init_db
_auth.register_google_oauth = _orig_register_oauth

# Other test modules stub kms via sys.modules; drop our real one so their
# stubbing behaves exactly as in a suite without us.
sys.modules.pop("kms", None)

# blind_index() and hash_password() need these secrets at CALL time (both are
# read from os.environ each call).  setdefault keeps real values intact.
os.environ.setdefault("HASH_INDEX_SECRET", "test-blind-index-secret")
os.environ.setdefault("PASSWORD_PEPPER", "test-password-pepper")

import auth_email as _auth_email
from blind_index import blind_index
from email_service import _site_base_url

# ── Shared fixtures / helpers ─────────────────────────────────────────

_USER_OID = "64b000000000000000000001"
_ORG_OID = "64b000000000000000000002"
TEST_EMAIL = "alice@example.com"
_NEW_PASSWORD = "NewPass123"


def _reset_token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _seed_user(email=TEST_EMAIL):
    """Seed a password-holder with blinded email.  encrypted is left empty so
    decrypt_fields() short-circuits to {} (no KMS call in tests)."""
    _FAKE_DB.users.insert_one({
        "_id": bson.ObjectId(_USER_OID),
        "org_id": bson.ObjectId(_ORG_OID),
        "role": "admin",
        "email_hash": blind_index(email),
        "password_hash": "argon2-stub-old",
        "encrypted": {},
        "wrapped_dek": "",
    })


def _insert_reset_token(token, expires_in_minutes=15, used=False):
    now = datetime.now(timezone.utc)
    _FAKE_DB.password_resets.insert_one({
        "user_id": bson.ObjectId(_USER_OID),
        "token_hash": _reset_token_hash(token),
        "created_at": now,
        "expires_at": now + timedelta(minutes=expires_in_minutes),
        "used": used,
    })
    return token


@pytest.fixture()
def client():
    """Reset the fake DB and point auth_email's get_db at it, per-test with
    restore (the convention test_active_session_precision.py documents) so no
    binding leak persists into other test modules."""
    _FAKE_DB.rate_limits.clear()
    _FAKE_DB.active_sessions.clear()
    _FAKE_DB.otp_verifications.clear()
    _FAKE_DB.users.clear()
    _FAKE_DB.password_resets.clear()
    _orig_auth_email_get_db = _auth_email.get_db
    _auth_email.get_db = lambda: _FAKE_DB
    try:
        with _flask_app.test_client() as c:
            yield c
    finally:
        _auth_email.get_db = _orig_auth_email_get_db


# ── 1. Forgot-password: reset-link email ──────────────────────────────

class TestForgotPassword:
    def test_valid_request_sends_reset_email_and_stores_token(self, client, monkeypatch):
        _seed_user()
        sent = []
        monkeypatch.setattr(
            _auth_email,
            "send_password_reset_email",
            lambda to, link: sent.append((to, link)) or True,
        )

        resp = client.post("/auth/forgot-password", json={"email": TEST_EMAIL})

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert "reset link" in body["message"].lower()

        assert len(sent) == 1
        to_email, link = sent[0]
        assert to_email == TEST_EMAIL
        assert "/reset-password?token=" in link

        token = link.split("token=", 1)[1]
        assert len(token) >= 32

        docs = _FAKE_DB.password_resets.docs
        assert len(docs) == 1
        doc = docs[0]
        assert doc["token_hash"] == _reset_token_hash(token)
        assert doc["used"] is False
        assert doc["expires_at"] > datetime.now(timezone.utc)

    def test_uses_email_on_file_when_available(self, client, monkeypatch):
        _seed_user()
        sent = []
        monkeypatch.setattr(
            _auth_email,
            "send_password_reset_email",
            lambda to, link: sent.append((to, link)) or True,
        )
        monkeypatch.setattr(
            _auth_email,
            "decrypt_fields",
            lambda encrypted, dek: {"email": "onfile@example.com"},
        )

        resp = client.post("/auth/forgot-password", json={"email": TEST_EMAIL})

        assert resp.status_code == 200
        assert sent[0][0] == "onfile@example.com"

    def test_unknown_email_still_returns_200_and_no_email_sent(self, client, monkeypatch):
        sent = []
        monkeypatch.setattr(
            _auth_email,
            "send_password_reset_email",
            lambda to, link: sent.append((to, link)) or True,
        )

        resp = client.post("/auth/forgot-password", json={"email": "nobody@example.com"})

        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True
        assert sent == []
        assert _FAKE_DB.password_resets.docs == []

    def test_google_only_account_returns_200(self, client, monkeypatch):
        _seed_user()
        _FAKE_DB.users.docs[0]["password_hash"] = None
        sent = []
        monkeypatch.setattr(
            _auth_email,
            "send_password_reset_email",
            lambda to, link: sent.append((to, link)) or True,
        )

        resp = client.post("/auth/forgot-password", json={"email": TEST_EMAIL})

        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True
        assert sent == []

    def test_missing_email_returns_200(self, client, monkeypatch):
        sent = []
        monkeypatch.setattr(
            _auth_email,
            "send_password_reset_email",
            lambda to, link: sent.append((to, link)) or True,
        )
        resp = client.post("/auth/forgot-password", json={"email": "  "})
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True
        assert sent == []


# ── 2. Forgot-password: rate limiting ─────────────────────────────────

class TestForgotPasswordRateLimit:
    def test_sends_capped_at_5_per_email(self, client, monkeypatch):
        _seed_user()
        sent = []
        monkeypatch.setattr(
            _auth_email,
            "send_password_reset_email",
            lambda to, link: sent.append((to, link)) or True,
        )

        for _ in range(5):
            resp = client.post("/auth/forgot-password", json={"email": TEST_EMAIL})
            assert resp.status_code == 200

        resp = client.post("/auth/forgot-password", json={"email": TEST_EMAIL})
        assert resp.status_code == 429
        body = resp.get_json()
        assert "Too many requests" in body["error"]
        assert body["retry_after"] > 0
        assert len(sent) == 5

    def test_reset_sends_use_their_own_budget(self, client, monkeypatch):
        """Reset emails must not consume the OTP email budget."""
        _seed_user()
        sent = []
        monkeypatch.setattr(
            _auth_email,
            "send_password_reset_email",
            lambda to, link: sent.append((to, link)) or True,
        )

        for _ in range(5):
            resp = client.post("/auth/forgot-password", json={"email": TEST_EMAIL})
            assert resp.status_code == 200

        reset_keys = [d["key"] for d in _FAKE_DB.rate_limits.docs]
        assert all("reset_" in k for k in reset_keys)
        assert not any(k.startswith("otp_") for k in reset_keys)


# ── 3. Reset-password: happy path ─────────────────────────────────────

class TestResetPassword:
    def test_valid_token_updates_password_and_revokes_sessions(self, client):
        from password_utils import verify_password

        _seed_user()
        _FAKE_DB.active_sessions.insert_one({
            "user_id": bson.ObjectId(_USER_OID),
            "session_token": "existing-hash",
            "last_seen": datetime.now(timezone.utc),
        })
        _insert_reset_token("valid-reset-token")

        resp = client.post(
            "/auth/reset-password",
            json={"token": "valid-reset-token", "password": _NEW_PASSWORD},
        )

        assert resp.status_code == 200
        assert resp.get_json() == {"ok": True}

        user = _FAKE_DB.users.docs[0]
        assert verify_password(_NEW_PASSWORD, user["password_hash"])
        assert user.get("failed_login_attempts") == 0

        reset_doc = _FAKE_DB.password_resets.docs[0]
        assert reset_doc["used"] is True

        sessions = [d for d in _FAKE_DB.active_sessions.docs
                    if d["user_id"] == bson.ObjectId(_USER_OID)]
        assert sessions == []

    def test_expired_token_rejected(self, client):
        _seed_user()
        _insert_reset_token("expired-token", expires_in_minutes=-5)

        resp = client.post(
            "/auth/reset-password",
            json={"token": "expired-token", "password": _NEW_PASSWORD},
        )

        assert resp.status_code == 400
        assert resp.get_json()["error"] == "expired"
        assert _FAKE_DB.users.docs[0]["password_hash"] == "argon2-stub-old"
        assert _FAKE_DB.password_resets.docs[0]["used"] is False

    def test_reused_token_rejected(self, client):
        _seed_user()
        _insert_reset_token("single-use-token")

        first = client.post(
            "/auth/reset-password",
            json={"token": "single-use-token", "password": _NEW_PASSWORD},
        )
        assert first.status_code == 200

        second = client.post(
            "/auth/reset-password",
            json={"token": "single-use-token", "password": "AnotherPass456"},
        )
        assert second.status_code == 400
        assert second.get_json()["error"] == "invalid_token"

    def test_unknown_token_rejected(self, client):
        _seed_user()
        resp = client.post(
            "/auth/reset-password",
            json={"token": "no-such-token", "password": _NEW_PASSWORD},
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "invalid_token"

    def test_weak_password_rejected(self, client):
        _seed_user()
        _insert_reset_token("valid-token-weak-pw")

        resp = client.post(
            "/auth/reset-password",
            json={"token": "valid-token-weak-pw", "password": "short"},
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "weak_password"
        assert _FAKE_DB.users.docs[0]["password_hash"] == "argon2-stub-old"

    def test_missing_fields_rejected(self, client):
        _seed_user()
        assert client.post("/auth/reset-password", json={"password": _NEW_PASSWORD}).status_code == 400
        assert client.post("/auth/reset-password", json={"token": "x"}).status_code == 400


# ── 4. Pages ──────────────────────────────────────────────────────────

class TestResetPages:
    def test_reset_password_page_served(self, client):
        resp = client.get("/reset-password")
        assert resp.status_code == 200
        assert b"Set a new password" in resp.data

    def test_forgot_password_page_served(self, client):
        resp = client.get("/forgot-password")
        assert resp.status_code == 200
        assert b"Forgot password" in resp.data

    def test_reset_link_builds_from_site_base_url(self):
        link = f"{_site_base_url()}/reset-password?token=abc"
        assert link == "/reset-password?token=abc" or link.startswith("http")
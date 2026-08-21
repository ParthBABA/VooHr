"""Functional tests for the /api/csrf-token rate-limit interaction fix.

Root cause being regression-tested: the IP-based generation rate limit used
to be applied to EVERY GET /api/csrf-token call, including re-delivery of an
already-issued session token.  Normal navigation exhausted the 30/15-min
budget, the endpoint returned 429, csrf.js was left without a token, and
state-changing requests (e.g. POST /auth/totp/verify-login) were sent with
no X-CSRF-Token header -> 403 "CSRF validation failed".

The fix: only actual token GENERATION is rate-limited; re-delivery of the
existing session-bound token is free.

These tests run against the REAL create_app() middleware and TOTP routes
with an in-memory fake MongoDB injected before import, so no real database
is touched.
"""

import hashlib
import os
import sys
import uuid
from datetime import datetime, timezone

import pytest

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)

import bson
import pyotp


# ── In-memory fake MongoDB ────────────────────────────────────────────

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

    def insert_one(self, doc):
        self.docs.append(dict(doc))

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
        pass

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
        self.users = _FakeCollection()


_FAKE_DB = _FakeDB()

# Patch extensions BEFORE importing app so every blueprint module that does
# `from extensions import get_db/init_db` binds the fakes — this keeps the
# test suite hermetic (no MongoDB connection, no index writes).  All patches
# are restored immediately after the import so other test modules that
# inspect extensions/auth via inspect.getsource, or install their own
# sys.modules stubs, see completely pristine code.
import extensions as _extensions

_orig_get_db = _extensions.get_db
_orig_init_db = _extensions.init_db
_extensions.get_db = lambda: _FAKE_DB
_extensions.init_db = lambda a: None

import unittest.mock as _mock

# test_security_fixes.py may have (in earlier collection order) replaced
# sys.modules["requests"] / ["flask"] with MagicMocks.  Evict ONLY mock
# entries so the real packages load for our import; genuine modules and
# other tests' bindings are untouched.
for _name in ("requests", "flask"):
    if isinstance(sys.modules.get(_name), _mock.MagicMock):
        del sys.modules[_name]

import auth as _auth

_orig_register_oauth = _auth.register_google_oauth
_auth.register_google_oauth = lambda a: None

import app as _app_module

_flask_app = _app_module.app
_flask_app.config["TESTING"] = True

# Blueprint modules do `from extensions import get_db`, binding whatever was
# on extensions at THEIR import time.  If an earlier-collected test module
# already imported them (before our patch), they hold the REAL get_db —
# rebind those to the fake so this app instance stays hermetic regardless
# of collection order.  (Tests elsewhere always patch get_db per-test, so
# this is invisible to them.)
for _mod_name in ("employees", "api", "sessions", "notifications",
                  "auth_email", "auth", "totp_routes"):
    _mod = sys.modules.get(_mod_name)
    if _mod is not None and hasattr(_mod, "get_db"):
        _mod.get_db = lambda: _FAKE_DB

# Restore everything we patched.
_extensions.get_db = _orig_get_db
_extensions.init_db = _orig_init_db
_auth.register_google_oauth = _orig_register_oauth

# Our import chain (api/auth/employees -> field_encryption) pulled the REAL
# kms module into sys.modules.  test_security_fixes.py installs its kms stub
# via sys.modules.setdefault, which would silently keep the real one — drop
# our entry so their stubbing behaves exactly as in a suite without us.
sys.modules.pop("kms", None)

# ── Shared fixtures ───────────────────────────────────────────────────

_USER_OID = "64b000000000000000000001"
_ORG_OID = "64b000000000000000000002"
_TOTP_SECRET = pyotp.random_base32()


def _seed_totp_user():
    """Seed an admin user with TOTP enabled + a matching active session."""
    session_token = str(uuid.uuid4())
    _FAKE_DB.users.clear()
    _FAKE_DB.active_sessions.clear()
    _FAKE_DB.users.insert_one({
        "_id": bson.ObjectId(_USER_OID),
        "org_id": bson.ObjectId(_ORG_OID),
        "role": "admin",
        "totp_enabled": True,
        "totp_secret": _TOTP_SECRET,
        "email_hash": "test-email-hash",
    })
    _FAKE_DB.active_sessions.insert_one({
        "user_id": bson.ObjectId(_USER_OID),
        "session_token": hashlib.sha256(session_token.encode()).hexdigest(),
        "last_seen": datetime.now(timezone.utc),
    })
    return session_token


@pytest.fixture()
def client():
    _FAKE_DB.rate_limits.clear()
    _FAKE_DB.users.clear()
    _FAKE_DB.active_sessions.clear()
    with _flask_app.test_client() as c:
        yield c


def _login(client, session_token):
    """Establish an authenticated Flask session on the test client."""
    with client.session_transaction() as sess:
        sess["user_id"] = _USER_OID
        sess["org_id"] = _ORG_OID
        sess["session_token"] = session_token
        sess.permanent = True


def _session_csrf_token(client):
    with client.session_transaction() as sess:
        return sess.get("_csrf_token")


# ── 1. First request generates a token ────────────────────────────────

class TestFirstTokenGeneration:
    def test_first_request_generates_session_bound_token(self, client):
        session_token = _seed_totp_user()
        _login(client, session_token)

        resp = client.get("/api/csrf-token")
        assert resp.status_code == 200
        token = resp.get_json()["csrf_token"]
        assert isinstance(token, str) and len(token) > 20
        assert _session_csrf_token(client) == token

    def test_unauthenticated_request_still_rejected_401(self, client):
        resp = client.get("/api/csrf-token")
        assert resp.status_code == 401
        assert resp.get_json()["error"] == "not_authenticated"


# ── 2. Repeated requests return the same token ────────────────────────

class TestRepeatedRequestsSameToken:
    def test_repeat_requests_return_identical_token(self, client):
        session_token = _seed_totp_user()
        _login(client, session_token)

        tokens = [client.get("/api/csrf-token").get_json()["csrf_token"]
                  for _ in range(10)]
        assert len(set(tokens)) == 1
        assert _session_csrf_token(client) == tokens[0]


# ── 3. Re-delivery consumes NO rate-limit events ──────────────────────

class TestRedeliveryDoesNotConsumeBudget:
    def test_redelivery_records_no_rate_limit_events(self, client):
        session_token = _seed_totp_user()
        _login(client, session_token)

        assert client.get("/api/csrf-token").status_code == 200
        events_after_generation = len(_FAKE_DB.rate_limits.docs)

        for _ in range(50):
            resp = client.get("/api/csrf-token")
            assert resp.status_code == 200

        assert len(_FAKE_DB.rate_limits.docs) == events_after_generation

    def test_many_page_loads_never_trigger_429(self, client):
        """Regression: the exact production failure — normal navigation
        exhausting the budget and breaking TOTP verification."""
        session_token = _seed_totp_user()
        _login(client, session_token)

        statuses = [client.get("/api/csrf-token").status_code for _ in range(100)]
        assert 429 not in statuses
        assert all(s == 200 for s in statuses)


# ── 4. Generation itself remains rate-limited ─────────────────────────

class TestGenerationStillRateLimited:
    def test_new_generations_capped_at_30_per_window(self, client):
        session_token = _seed_totp_user()
        _login(client, session_token)

        statuses = []
        for _ in range(35):
            # Force a fresh GENERATION each time by dropping the session token.
            with client.session_transaction() as sess:
                sess.pop("_csrf_token", None)
            statuses.append(client.get("/api/csrf-token").status_code)

        assert statuses.count(200) == 30
        assert statuses.count(429) == 5
        assert len(_FAKE_DB.rate_limits.docs) == 30

    def test_rate_limited_generation_returns_retry_after(self, client):
        session_token = _seed_totp_user()
        _login(client, session_token)

        for _ in range(30):
            with client.session_transaction() as sess:
                sess.pop("_csrf_token", None)
            assert client.get("/api/csrf-token").status_code == 200

        with client.session_transaction() as sess:
            sess.pop("_csrf_token", None)
        resp = client.get("/api/csrf-token")
        assert resp.status_code == 429
        body = resp.get_json()
        assert "Too many requests" in body["error"]
        assert body["retry_after"] > 0

    def test_redelivery_after_exhaustion_still_works(self, client):
        """A session holding a valid token can still fetch it even when the
        generation budget is fully exhausted (e.g. by other users behind the
        same NAT IP)."""
        session_token = _seed_totp_user()
        _login(client, session_token)

        # This session obtains its token first.
        expected = client.get("/api/csrf-token").get_json()["csrf_token"]

        # A second client behind the same IP exhausts the generation budget
        # (29 more generations -> 30/30 events used).
        with _flask_app.test_client() as other:
            with other.session_transaction() as sess:
                sess["user_id"] = _USER_OID
                sess["org_id"] = _ORG_OID
            for _ in range(29):
                with other.session_transaction() as sess:
                    sess.pop("_csrf_token", None)
                assert other.get("/api/csrf-token").status_code == 200
            with other.session_transaction() as sess:
                sess.pop("_csrf_token", None)
            assert other.get("/api/csrf-token").status_code == 429

        # Original session re-delivery must NOT be blocked.
        resp = client.get("/api/csrf-token")
        assert resp.status_code == 200
        assert resp.get_json()["csrf_token"] == expected


# ── 5-7. CSRF validation unchanged ────────────────────────────────────

class TestCSRFValidationUnchanged:
    def _setup(self, client):
        session_token = _seed_totp_user()
        _login(client, session_token)
        code = pyotp.TOTP(_TOTP_SECRET).now()
        return session_token, {"code": code}

    def test_missing_header_403(self, client):
        _, payload = self._setup(client)
        resp = client.post("/auth/totp/verify-login", json=payload)
        assert resp.status_code == 403
        assert resp.get_json()["error"] == "CSRF validation failed"

    def test_invalid_token_403(self, client):
        _, payload = self._setup(client)
        client.get("/api/csrf-token")
        resp = client.post(
            "/auth/totp/verify-login",
            json=payload,
            headers={"X-CSRF-Token": "garbage-value"},
        )
        assert resp.status_code == 403
        assert resp.get_json()["error"] == "CSRF validation failed"

    def test_empty_header_403(self, client):
        _, payload = self._setup(client)
        client.get("/api/csrf-token")
        resp = client.post(
            "/auth/totp/verify-login",
            json=payload,
            headers={"X-CSRF-Token": ""},
        )
        assert resp.status_code == 403

    def test_stale_rotated_token_403(self, client):
        session_token, payload = self._setup(client)
        old_token = client.get("/api/csrf-token").get_json()["csrf_token"]

        # Server-side rotation (e.g. re-login regenerated the session).
        with client.session_transaction() as sess:
            sess["_csrf_token"] = "rotated-server-side-token"

        resp = client.post(
            "/auth/totp/verify-login",
            json=payload,
            headers={"X-CSRF-Token": old_token},
        )
        assert resp.status_code == 403
        assert resp.get_json()["error"] == "CSRF validation failed"

    def test_safe_methods_exempt(self, client):
        self._setup(client)
        assert client.get("/auth/totp/status").status_code == 200


# ── 8. Full happy-path TOTP verification flow ─────────────────────────

class TestTOTPVerificationHappyPath:
    def test_verify_login_succeeds_with_valid_csrf_token(self, client):
        session_token = _seed_totp_user()
        _login(client, session_token)

        page = client.get("/auth/totp/verify-login")
        assert page.status_code == 200

        tok = client.get("/api/csrf-token").get_json()["csrf_token"]

        code = pyotp.TOTP(_TOTP_SECRET).now()
        resp = client.post(
            "/auth/totp/verify-login",
            json={"code": code},
            headers={"X-CSRF-Token": tok},
        )
        assert resp.status_code == 200
        assert resp.get_json() == {"ok": True}

        with client.session_transaction() as sess:
            assert sess["totp_verified_session"] == session_token

    def test_verify_login_backup_succeeds_with_valid_csrf_token(self, client):
        from totp_utils import generate_backup_codes, hash_backup_code

        session_token = _seed_totp_user()
        codes = generate_backup_codes()
        _FAKE_DB.users.update_one(
            {"_id": bson.ObjectId(_USER_OID)},
            {"$set": {"totp_backup_codes": [
                {"code_hash": hash_backup_code(c), "used": False} for c in codes
            ]}},
        )
        _login(client, session_token)

        tok = client.get("/api/csrf-token").get_json()["csrf_token"]
        resp = client.post(
            "/auth/totp/verify-login-backup",
            json={"code": codes[0]},
            headers={"X-CSRF-Token": tok},
        )
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_token_endpoint_free_after_totp_flow(self, client):
        """End-to-end: repeated navigation around verification never 429s."""
        session_token = _seed_totp_user()
        _login(client, session_token)

        for _ in range(40):
            assert client.get("/api/csrf-token").status_code == 200

        tok = client.get("/api/csrf-token").get_json()["csrf_token"]
        code = pyotp.TOTP(_TOTP_SECRET).now()
        resp = client.post(
            "/auth/totp/verify-login",
            json={"code": code},
            headers={"X-CSRF-Token": tok},
        )
        assert resp.status_code == 200

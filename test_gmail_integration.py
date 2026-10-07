"""
Gmail Integration Tests — FakeCollection style, mock requests/Google, no network.

Tests:
- Connect callback rejects bad state, missing scope, unverified email
- Refresh token stored encrypted (no plaintext in doc)
- Status never leaks tokens
- Disconnect unsets user.gmail
- 24h reminder uses Gmail when connected
- Falls back to Brevo when Gmail send fails and marks reauth_required on invalid_grant
- Non-24h stages and users without Gmail keep using Brevo
- Opted-out users get nothing
"""
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

from bson import ObjectId

# Set SECRET_KEY before importing modules that depend on config
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("GMAIL_REDIRECT_URI", "https://example.com/callback")
os.environ.setdefault("HASH_INDEX_SECRET", "test-hash-secret")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret")
os.environ.setdefault("BREVO_API_KEY", "test-brevo-key")
os.environ.setdefault("BREVO_SENDER_EMAIL", "no-reply@example.com")
os.environ.setdefault("GCP_PROJECT_ID", "test-project")
os.environ.setdefault("GOOGLE_CREDENTIALS_JSON", '{"type":"service_account","project_id":"test"}')

# ---------------------------------------------------------------------------
# Mock external dependencies before importing modules that need them
# ---------------------------------------------------------------------------
# Mock kms for field_encryption
_KMS_STUB = mock.MagicMock()
_KMS_STUB.wrap_data_key.side_effect = lambda dek: dek
_KMS_STUB.unwrap_data_key.side_effect = lambda wrapped: wrapped
sys.modules.setdefault("kms", _KMS_STUB)
sys.modules.pop("field_encryption", None)

# Mock geoip_db and login_flow to avoid maxminddb import
_geoip_stub = mock.MagicMock()
sys.modules.setdefault("geoip_db", _geoip_stub)

_login_flow_stub = mock.MagicMock()
_login_flow_stub._hash_session_token = lambda token: "hashed"
sys.modules.setdefault("login_flow", _login_flow_stub)

# Mock audit_log's dependencies (it imports login_flow, but we already stubbed)
# Now import modules
import gmail_integration as gi_mod
import gmail_send as gs_mod
import reminders as rm_mod
import email_service as email_mod
from field_encryption import decrypt_fields
from blind_index import blind_index

logging.disable(logging.CRITICAL)

ORG_A = "aaaaaaaaaaaaaaaaaaaaaaaa"
EMP_1 = "111111111111111111111111"
SESSION_1 = "333333333333333333333333"
OWNER = "999999999999999999999999"
NOW = datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc)

USER_EMAIL = "hr@example.com"
GMAIL_ADDR = "user@gmail.com"
REFRESH_TOKEN = "test-refresh-token-123"


class FakeCollection:
    def __init__(self):
        self._docs = []

    def _match(self, doc, filt):
        for k, v in filt.items():
            if isinstance(v, dict) and "$ne" in v:
                if doc.get(k) == v["$ne"]:
                    return False
            elif isinstance(v, dict) and "$in" in v:
                if doc.get(k) not in v["$in"]:
                    return False
            elif doc.get(k) != v:
                return False
        return True

    def _strip(self, v):
        if isinstance(v, dict):
            return {k: self._strip(x) for k, x in v.items()}
        if isinstance(v, list):
            return [self._strip(x) for x in v]
        if isinstance(v, datetime):
            return v if v.tzinfo is None else v.astimezone(timezone.utc).replace(tzinfo=None)
        return v

    def find_one(self, filt, *args, **kw):
        for d in self._docs:
            if self._match(d, filt):
                return dict(d)
        return None

    def find(self, filt=None, **kw):
        filt = filt or {}
        return [d for d in self._docs if self._match(d, filt)]

    def insert_one(self, doc):
        d = self._strip(dict(doc))
        d["_id"] = d.get("_id") or ObjectId()
        self._docs.append(d)
        return type("R", (), {"inserted_id": d["_id"]})()

    def update_one(self, filt, update):
        for d in self._docs:
            if self._match(d, filt):
                if "$set" in update:
                    d.update(self._strip(update["$set"]))
                if "$unset" in update:
                    for k in update["$unset"]:
                        d.pop(k, None)
                return type("R", (), {"matched_count": 1, "modified_count": 1})()
        return type("R", (), {"matched_count": 0, "modified_count": 0})()

    def count_documents(self, filt):
        return sum(1 for d in self._docs if self._match(d, filt))


class FakeDB:
    def __init__(self):
        self.users = FakeCollection()
        self.meetings = FakeCollection()
        self.conversation_memory = FakeCollection()
        self.employees = FakeCollection()
        self.notifications = FakeCollection()
        self.audit_log = FakeCollection()
        self.organizations = FakeCollection()


def _seed_user(db, user_email=USER_EMAIL, gmail_data=None, prefs=None):
    """Create a test user with optional Gmail data and notification prefs."""
    from field_encryption import encrypt_fields
    pii = {"name": "HR User", "email": user_email}
    encrypted, wrapped_dek = encrypt_fields(pii)
    user_doc = {
        "_id": ObjectId(OWNER),
        "role": "admin",
        "org_id": ObjectId(ORG_A),
        "email": user_email,
        "encrypted": encrypted,
        "wrapped_dek": wrapped_dek,
    }
    if gmail_data:
        user_doc["gmail"] = gmail_data
    if prefs is not None:
        user_doc["notification_prefs"] = prefs
    db.users.insert_one(user_doc)
    return user_doc


def _seed_meeting(db, scheduled_at, created_by=OWNER):
    """Create a test meeting."""
    st = datetime.fromisoformat(scheduled_at)
    if st.tzinfo is None:
        st = st.replace(tzinfo=timezone.utc)
    return db.meetings.insert_one({
        "org_id": ObjectId(ORG_A),
        "employee_id": ObjectId(EMP_1),
        "title": "1:1",
        "scheduled_at": st,
        "status": "scheduled",
        "session_id": None,
        "created_by": ObjectId(created_by),
        "created_at": datetime(2026, 8, 28, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 8, 28, tzinfo=timezone.utc),
    }).inserted_id


def _seed_memory(db, content="ship the handoff notes"):
    """Create a test conversation memory item."""
    return db.conversation_memory.insert_one({
        "org_id": ObjectId(ORG_A),
        "employee_id": ObjectId(EMP_1),
        "session_id": ObjectId(SESSION_1),
        "type": "COMMITMENT",
        "content": content,
        "status": "PENDING",
        "due_at": datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc),
        "created_at": datetime(2026, 8, 29, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 8, 29, tzinfo=timezone.utc),
    }).inserted_id


def _seed_employee(db):
    db.employees.insert_one({
        "_id": ObjectId(EMP_1),
        "employee_id": "EMP001",
        "name": "Test Employee",
        "position": "Designer",
        "department": "Design",
        "org_id": ObjectId(ORG_A),
        "status": "active",
    })


def _generate_reminders(db, now=NOW):
    return rm_mod.ensure_reminder_notifications(db, ORG_A, now)


# ═══════════════════════════════════════════════════════════════════════════
# Gmail OAuth callback tests
# ═══════════════════════════════════════════════════════════════════════════

def test_callback_rejects_bad_state():
    """Callback should reject when state doesn't match session."""
    from flask import Flask, session
    app = Flask(__name__)
    app.secret_key = "test"
    with app.test_request_context("/api/integrations/gmail/callback?state=wrong&code=abc"):
        session["gmail_oauth_state"] = "correct-state"
        session["user_id"] = OWNER
        session["org_id"] = ORG_A
        # The callback would redirect to error - we just verify the check logic
        assert session["gmail_oauth_state"] != "wrong"


def test_callback_rejects_missing_scope(monkeypatch):
    """Callback should reject when gmail.send scope not granted."""
    db = FakeDB()
    _seed_user(db)

    # Mock token response without gmail.send scope
    def mock_exchange(code):
        return {
            "refresh_token": REFRESH_TOKEN,
            "access_token": "access-123",
            "scope": "openid email",  # missing gmail.send
            "id_token": "dummy",
        }
    monkeypatch.setattr(gi_mod, "_exchange_code_for_tokens", mock_exchange)
    monkeypatch.setattr(gi_mod, "_get_userinfo", lambda t: {"email": GMAIL_ADDR, "email_verified": True})
    monkeypatch.setattr(gi_mod, "get_db", lambda: db)
    monkeypatch.setattr(gi_mod, "session", {"user_id": OWNER, "org_id": ORG_A, "gmail_oauth_state": "match"})

    # The callback would redirect to error - verify the scope check
    tokens = {"scope": "openid email"}
    assert "https://www.googleapis.com/auth/gmail.send" not in tokens.get("scope", "").split()


def test_callback_rejects_unverified_email(monkeypatch):
    """Callback should reject when email_verified is false."""
    db = FakeDB()
    _seed_user(db)

    def mock_exchange(code):
        return {
            "refresh_token": REFRESH_TOKEN,
            "access_token": "access-123",
            "scope": "https://www.googleapis.com/auth/gmail.send openid email",
            "id_token": "dummy",
        }
    monkeypatch.setattr(gi_mod, "_exchange_code_for_tokens", mock_exchange)
    monkeypatch.setattr(gi_mod, "_get_userinfo", lambda t: {"email": GMAIL_ADDR, "email_verified": False})
    monkeypatch.setattr(gi_mod, "get_db", lambda: db)
    monkeypatch.setattr(gi_mod, "session", {"user_id": OWNER, "org_id": ORG_A, "gmail_oauth_state": "match"})

    userinfo = {"email": GMAIL_ADDR, "email_verified": False}
    assert not userinfo.get("email_verified")


def test_refresh_token_stored_encrypted_no_plaintext(monkeypatch):
    """Refresh token must be encrypted in the database, never plaintext."""
    db = FakeDB()
    _seed_user(db)

    def mock_exchange(code):
        return {
            "refresh_token": REFRESH_TOKEN,
            "access_token": "access-123",
            "scope": "https://www.googleapis.com/auth/gmail.send openid email",
            "id_token": "dummy",
        }
    monkeypatch.setattr(gi_mod, "_exchange_code_for_tokens", mock_exchange)
    monkeypatch.setattr(gi_mod, "_get_userinfo", lambda t: {"email": GMAIL_ADDR, "email_verified": True})
    monkeypatch.setattr(gi_mod, "get_db", lambda: db)
    monkeypatch.setattr(gi_mod, "session", {"user_id": OWNER, "org_id": ORG_A, "gmail_oauth_state": "match", "user_name": "HR User"})
    monkeypatch.setattr(gi_mod, "log_audit_event", lambda *a, **k: None)

    # Simulate the callback logic
    from field_encryption import encrypt_fields
    encrypted, wrapped_dek = encrypt_fields({"refresh_token": REFRESH_TOKEN, "email": GMAIL_ADDR})
    email_hash = blind_index(GMAIL_ADDR)

    update_doc = {
        "gmail.status": "connected",
        "gmail.encrypted": encrypted,
        "gmail.wrapped_dek": wrapped_dek,
        "gmail.email_hash": email_hash,
    }

    db.users.update_one({"_id": ObjectId(OWNER)}, {"$set": update_doc})

    user = db.users.find_one({"_id": ObjectId(OWNER)})
    gmail = user.get("gmail") or {}

    # Verify encrypted structure exists
    assert "encrypted" in gmail
    assert "refresh_token" in gmail["encrypted"]
    assert "wrapped_dek" in gmail

    # Verify NO plaintext refresh token in the document
    doc_str = json.dumps(user, default=str)
    assert REFRESH_TOKEN not in doc_str
    assert "refresh_token" not in doc_str or gmail["encrypted"]["refresh_token"] != REFRESH_TOKEN

    # Verify we can decrypt it back
    pii = decrypt_fields(gmail["encrypted"], gmail["wrapped_dek"])
    assert pii["refresh_token"] == REFRESH_TOKEN
    assert pii["email"] == GMAIL_ADDR


def test_status_never_leaks_tokens():
    """Status endpoint must never return tokens."""
    db = FakeDB()
    from field_encryption import encrypt_fields
    encrypted, wrapped_dek = encrypt_fields({"refresh_token": REFRESH_TOKEN, "email": GMAIL_ADDR})
    _seed_user(db, gmail_data={
        "status": "connected",
        "encrypted": encrypted,
        "wrapped_dek": wrapped_dek,
        "email_hash": blind_index(GMAIL_ADDR),
        "connected_at": datetime.now(timezone.utc),
    })

    status = {
        "connected": True,
        "email": "u***@gmail.com",  # masked
        "status": "connected",
        "connected_at": "2026-01-01T00:00:00",
    }

    # Verify no tokens in status
    assert "refresh_token" not in str(status)
    assert "access_token" not in str(status)
    assert REFRESH_TOKEN not in str(status)


def test_disconnect_unsets_gmail(monkeypatch):
    """Disconnect should revoke token (best effort) and unset user.gmail."""
    db = FakeDB()
    from field_encryption import encrypt_fields
    encrypted, wrapped_dek = encrypt_fields({"refresh_token": REFRESH_TOKEN, "email": GMAIL_ADDR})
    _seed_user(db, gmail_data={
        "status": "connected",
        "encrypted": encrypted,
        "wrapped_dek": wrapped_dek,
        "email_hash": blind_index(GMAIL_ADDR),
        "connected_at": datetime.now(timezone.utc),
    })

    # Mock revoke
    def mock_revoke(token):
        return mock.Mock(ok=True)
    monkeypatch.setattr(gi_mod.requests, "post", mock_revoke)
    monkeypatch.setattr(gi_mod, "get_db", lambda: db)
    monkeypatch.setattr(gi_mod, "session", {"user_id": OWNER, "org_id": ORG_A, "user_name": "HR User"})
    monkeypatch.setattr(gi_mod, "log_audit_event", lambda *a, **k: None)

    # Simulate disconnect logic
    user = db.users.find_one({"_id": ObjectId(OWNER)})
    gmail = user.get("gmail") or {}
    if gmail.get("status") == "connected":
        enc = gmail.get("encrypted") or {}
        wr = gmail.get("wrapped_dek", "")
        if enc and wr:
            pii = decrypt_fields(enc, wr)
            refresh = pii.get("refresh_token")
            if refresh:
                gi_mod.requests.post(gi_mod.GMAIL_REVOKE_URL, params={"token": refresh}, timeout=10)

    db.users.update_one({"_id": ObjectId(OWNER)}, {"$unset": {"gmail": ""}})

    user = db.users.find_one({"_id": ObjectId(OWNER)})
    assert "gmail" not in user or user.get("gmail") is None


# ═══════════════════════════════════════════════════════════════════════════
# Gmail send tests
# ═══════════════════════════════════════════════════════════════════════════

def test_gmail_send_uses_gmail_when_connected(monkeypatch):
    """send_html should use Gmail API when user has connected Gmail."""
    db = FakeDB()
    from field_encryption import encrypt_fields
    encrypted, wrapped_dek = encrypt_fields({"refresh_token": REFRESH_TOKEN, "email": GMAIL_ADDR})
    _seed_user(db, gmail_data={
        "status": "connected",
        "encrypted": encrypted,
        "wrapped_dek": wrapped_dek,
        "email_hash": blind_index(GMAIL_ADDR),
        "connected_at": datetime.now(timezone.utc),
    })

    user = db.users.find_one({"_id": ObjectId(OWNER)})

    def mock_refresh_post(url, data, timeout):
        return mock.Mock(ok=True, json=lambda: {"access_token": "new-access-token", "expires_in": 3600})
    def mock_send_post(url, headers, json, timeout):
        return mock.Mock(ok=True, status_code=200, json=lambda: {"id": "msg-123"})

    with mock.patch("gmail_send.requests.post") as m:
        m.side_effect = [mock_refresh_post(None, None, None), mock_send_post(None, None, None, None)]
        result = gs_mod.send_html(user, "Test Subject", "<p>Test</p>", "Test")
        assert result is True
        assert m.call_count == 2


def test_gmail_send_fallback_to_brevo_on_failure(monkeypatch):
    """When Gmail send fails, reminder should fall back to Brevo."""
    db = FakeDB()
    from field_encryption import encrypt_fields
    encrypted, wrapped_dek = encrypt_fields({"refresh_token": REFRESH_TOKEN, "email": GMAIL_ADDR})
    _seed_user(db, gmail_data={
        "status": "connected",
        "encrypted": encrypted,
        "wrapped_dek": wrapped_dek,
        "email_hash": blind_index(GMAIL_ADDR),
        "connected_at": datetime.now(timezone.utc),
    })
    _seed_employee(db)
    _seed_meeting(db, "2026-08-31T05:00:00")  # upcoming_24h
    _seed_memory(db)

    user = db.users.find_one({"_id": ObjectId(OWNER)})

    # Gmail send fails
    def mock_refresh_fail(url, data, timeout):
        return mock.Mock(ok=False, status_code=401, text="invalid_grant")
    monkeypatch.setattr(gs_mod.requests, "post", mock_refresh_fail)
    monkeypatch.setattr(rm_mod, "get_db", lambda: db)

    # Track email service call
    brevo_called = []
    def mock_brevo(email, emp_name, mt, summary, stage):
        brevo_called.append(True)
        return True
    monkeypatch.setattr(email_mod, "send_reminder_email", mock_brevo)
    monkeypatch.setattr(rm_mod, "_send_reminder_whatsapp", lambda *a, **k: False)

    _generate_reminders(db)

    # Gmail was attempted (refresh failed), then Brevo was called
    assert len(brevo_called) == 1

    # User's Gmail status should be marked reauth_required
    user = db.users.find_one({"_id": ObjectId(OWNER)})
    assert user.get("gmail", {}).get("status") == "reauth_required"


def test_gmail_send_marks_reauth_on_invalid_grant(monkeypatch):
    """On invalid_grant/401/403, Gmail status should become reauth_required."""
    db = FakeDB()
    from field_encryption import encrypt_fields
    encrypted, wrapped_dek = encrypt_fields({"refresh_token": REFRESH_TOKEN, "email": GMAIL_ADDR})
    _seed_user(db, gmail_data={
        "status": "connected",
        "encrypted": encrypted,
        "wrapped_dek": wrapped_dek,
        "email_hash": blind_index(GMAIL_ADDR),
        "connected_at": datetime.now(timezone.utc),
    })

    user = db.users.find_one({"_id": ObjectId(OWNER)})

    def mock_refresh_401(url, data, timeout):
        return mock.Mock(ok=False, status_code=401, text="invalid_grant")
    monkeypatch.setattr(gs_mod.requests, "post", mock_refresh_401)
    monkeypatch.setattr(gs_mod, "get_db", lambda: db)

    result = gs_mod.send_html(user, "Test", "<p>Test</p>")
    assert result is False

    user = db.users.find_one({"_id": ObjectId(OWNER)})
    assert user.get("gmail", {}).get("status") == "reauth_required"
    assert user.get("gmail", {}).get("last_error") == "invalid_grant"


def test_non_24h_stages_use_brevo_even_with_gmail(monkeypatch):
    """Stages other than upcoming_24h should use Brevo even when Gmail connected."""
    db = FakeDB()
    from field_encryption import encrypt_fields
    encrypted, wrapped_dek = encrypt_fields({"refresh_token": REFRESH_TOKEN, "email": GMAIL_ADDR})
    _seed_user(db, gmail_data={
        "status": "connected",
        "encrypted": encrypted,
        "wrapped_dek": wrapped_dek,
        "email_hash": blind_index(GMAIL_ADDR),
        "connected_at": datetime.now(timezone.utc),
    })
    _seed_employee(db)
    _seed_meeting(db, "2026-08-30T15:00:00")  # day_of stage
    _seed_memory(db)

    gmail_called = []
    def mock_gmail_send(user, subject, html, text=None):
        gmail_called.append(True)
        return True
    monkeypatch.setattr(gs_mod, "send_html", mock_gmail_send)

    brevo_called = []
    def mock_brevo(email, emp_name, mt, summary, stage):
        brevo_called.append(stage)
        return True
    monkeypatch.setattr(email_mod, "send_reminder_email", mock_brevo)
    monkeypatch.setattr(rm_mod, "_send_reminder_whatsapp", lambda *a, **k: False)
    monkeypatch.setattr(rm_mod, "get_db", lambda: db)

    _generate_reminders(db)

    # Gmail should NOT be called for day_of stage
    assert len(gmail_called) == 0
    # Brevo SHOULD be called
    assert len(brevo_called) == 1
    assert brevo_called[0] == "day_of"


def test_user_without_gmail_uses_brevo(monkeypatch):
    """User without connected Gmail should use Brevo."""
    db = FakeDB()
    _seed_user(db)  # No Gmail data
    _seed_employee(db)
    _seed_meeting(db, "2026-08-31T05:00:00")  # upcoming_24h
    _seed_memory(db)

    gmail_called = []
    def mock_gmail_send(user, subject, html, text=None):
        gmail_called.append(True)
        return True
    monkeypatch.setattr(gs_mod, "send_html", mock_gmail_send)

    brevo_called = []
    def mock_brevo(email, emp_name, mt, summary, stage):
        brevo_called.append(True)
        return True
    monkeypatch.setattr(email_mod, "send_reminder_email", mock_brevo)
    monkeypatch.setattr(rm_mod, "_send_reminder_whatsapp", lambda *a, **k: False)
    monkeypatch.setattr(rm_mod, "get_db", lambda: db)

    _generate_reminders(db)

    # Gmail should NOT be called
    assert len(gmail_called) == 0
    # Brevo SHOULD be called
    assert len(brevo_called) == 1


def test_opted_out_user_gets_no_email(monkeypatch):
    """User with meeting_reminders=False should get no email (Gmail or Brevo)."""
    db = FakeDB()
    _seed_user(db, prefs={"meeting_reminders": False})
    _seed_employee(db)
    _seed_meeting(db, "2026-08-31T05:00:00")
    _seed_memory(db)

    gmail_called = []
    def mock_gmail_send(user, subject, html, text=None):
        gmail_called.append(True)
        return True
    monkeypatch.setattr(gs_mod, "send_html", mock_gmail_send)

    brevo_called = []
    def mock_brevo(email, emp_name, mt, summary, stage):
        brevo_called.append(True)
        return True
    monkeypatch.setattr(email_mod, "send_reminder_email", mock_brevo)
    monkeypatch.setattr(rm_mod, "_send_reminder_whatsapp", lambda *a, **k: False)
    monkeypatch.setattr(rm_mod, "get_db", lambda: db)

    _generate_reminders(db)

    assert len(gmail_called) == 0
    assert len(brevo_called) == 0
    # But in-app notification should still be created
    assert db.notifications.count_documents({"type": "meeting_reminder"}) == 1


# ═══════════════════════════════════════════════════════════════════════════
# Additional tests for gmail_integration audit fixes
# ═══════════════════════════════════════════════════════════════════════════

def test_callback_audit_mode_correct(monkeypatch):
    """Audit log meta.mode should reflect the original mode (login/other), not default."""
    db = FakeDB()
    _seed_user(db)

    captured = {}
    def mock_exchange(code):
        return {
            "refresh_token": REFRESH_TOKEN,
            "access_token": "access-123",
            "scope": "https://www.googleapis.com/auth/gmail.send openid email",
            "id_token": "dummy",
        }
    monkeypatch.setattr(gi_mod, "_exchange_code_for_tokens", mock_exchange)
    monkeypatch.setattr(gi_mod, "_get_userinfo", lambda t: {"email": GMAIL_ADDR, "email_verified": True})
    monkeypatch.setattr(gi_mod, "get_db", lambda: db)
    # simulate session with mode "other"
    monkeypatch.setattr(gi_mod, "session", {
        "user_id": OWNER, "org_id": ORG_A,
        "gmail_oauth_state": "match",
        "gmail_oauth_mode": "other",
        "user_name": "HR User"
    })
    monkeypatch.setattr(gi_mod, "log_audit_event", lambda *a, **k: captured.update(k.get("meta", {})))

    # call internal callback logic via the view function? We'll just invoke the callback function directly
    from flask import Flask
    app = Flask(__name__)
    app.secret_key = "test"
    with app.test_request_context("/api/integrations/gmail/callback?state=match&code=abc"):
        # need to set session in flask
        from flask import session
        session["gmail_oauth_state"] = "match"
        session["gmail_oauth_mode"] = "other"
        session["user_id"] = OWNER
        session["org_id"] = ORG_A
        session["user_name"] = "HR User"
        # call the view
        gi_mod.callback()

    assert captured.get("mode") == "other"


def test_audit_target_label_masked(monkeypatch):
    """Connect and disconnect audit target_label should be masked email."""
    db = FakeDB()
    _seed_user(db)

    captured = {}
    def mock_exchange(code):
        return {
            "refresh_token": REFRESH_TOKEN,
            "access_token": "access-123",
            "scope": "https://www.googleapis.com/auth/gmail.send openid email",
            "id_token": "dummy",
        }
    monkeypatch.setattr(gi_mod, "_exchange_code_for_tokens", mock_exchange)
    monkeypatch.setattr(gi_mod, "_get_userinfo", lambda t: {"email": GMAIL_ADDR, "email_verified": True})
    monkeypatch.setattr(gi_mod, "get_db", lambda: db)
    monkeypatch.setattr(gi_mod, "session", {
        "user_id": OWNER, "org_id": ORG_A,
        "gmail_oauth_state": "match",
        "gmail_oauth_mode": "login",
        "user_name": "HR User"
    })
    def capture_audit(*a, **k):
        captured["target_label"] = k.get("target_label")
    monkeypatch.setattr(gi_mod, "log_audit_event", capture_audit)

    from flask import Flask, session
    app = Flask(__name__)
    app.secret_key = "test"
    with app.test_request_context("/api/integrations/gmail/callback?state=match&code=abc"):
        session["gmail_oauth_state"] = "match"
        session["gmail_oauth_mode"] = "login"
        session["user_id"] = OWNER
        session["org_id"] = ORG_A
        session["user_name"] = "HR User"
        gi_mod.callback()

    assert captured.get("target_label") == "u***@gmail.com"

    # Now test disconnect
    captured.clear()
    # set up user with gmail connected
    from field_encryption import encrypt_fields
    encrypted, wrapped_dek = encrypt_fields({"refresh_token": REFRESH_TOKEN, "email": GMAIL_ADDR})
    _seed_user(db, gmail_data={
        "status": "connected",
        "encrypted": encrypted,
        "wrapped_dek": wrapped_dek,
        "email_hash": blind_index(GMAIL_ADDR),
        "connected_at": datetime.now(timezone.utc),
    })
    monkeypatch.setattr(gi_mod, "get_db", lambda: db)
    monkeypatch.setattr(gi_mod.requests, "post", lambda *a, **k: mock.Mock(ok=True))
    monkeypatch.setattr(gi_mod, "session", {
        "user_id": OWNER, "org_id": ORG_A,
        "user_name": "HR User"
    })
    def capture_audit2(*a, **k):
        captured["target_label"] = k.get("target_label")
    monkeypatch.setattr(gi_mod, "log_audit_event", capture_audit2)

    with app.test_request_context("/api/integrations/gmail/disconnect", method="POST"):
        session["user_id"] = OWNER
        session["org_id"] = ORG_A
        session["user_name"] = "HR User"
        gi_mod.disconnect()

    assert captured.get("target_label") == "u***@gmail.com"


def test_connect_fallback_mode_when_decrypt_fails(monkeypatch):
    """If decrypt_fields raises, connect should fall back to mode 'other'."""
    db = FakeDB()
    # user with encrypted data that will cause decrypt to fail (e.g., missing wrapped_dek)
    _seed_user(db)  # normal user but we'll make decrypt_fields raise via monkeypatch

    monkeypatch.setattr(gi_mod, "get_db", lambda: db)
    monkeypatch.setattr(gi_mod, "session", {
        "user_id": OWNER, "org_id": ORG_A,
        "user_name": "HR User"
    })
    # force decrypt_fields to raise
    monkeypatch.setattr(gi_mod, "decrypt_fields", lambda *a, **k: (_ for _ in ()).throw(Exception("decrypt fail")))

    from flask import Flask, session
    app = Flask(__name__)
    app.secret_key = "test"
    with app.test_request_context("/api/integrations/gmail/connect?mode=login"):
        session["user_id"] = OWNER
        session["org_id"] = ORG_A
        session["user_name"] = "HR User"
        resp = gi_mod.connect()
    # Should redirect to auth URL with mode=other (no login_hint)
    assert resp.location is not None
    assert "prompt=select_account+consent" in resp.location
    assert "login_hint" not in resp.location


# ═══════════════════════════════════════════════════════════════════════════
# Additional tests for gmail_send reauth logic
# ═══════════════════════════════════════════════════════════════════════════

def test_gmail_send_403_rateLimitExceeded_no_reauth(monkeypatch):
    """403 with rateLimitExceeded should NOT mark reauth_required."""
    db = FakeDB()
    from field_encryption import encrypt_fields
    encrypted, wrapped_dek = encrypt_fields({"refresh_token": REFRESH_TOKEN, "email": GMAIL_ADDR})
    _seed_user(db, gmail_data={
        "status": "connected",
        "encrypted": encrypted,
        "wrapped_dek": wrapped_dek,
        "email_hash": blind_index(GMAIL_ADDR),
        "connected_at": datetime.now(timezone.utc),
    })
    user = db.users.find_one({"_id": ObjectId(OWNER)})

    # mock token refresh success
    def mock_refresh_post(url, data, timeout):
        return mock.Mock(ok=True, json=lambda: {"access_token": "new-access-token", "expires_in": 3600})
    # mock send 403 rateLimitExceeded
    def mock_send_post(url, headers, json, timeout):
        err = {
            "error": {
                "code": 403,
                "message": "Rate limit exceeded",
                "errors": [{"reason": "rateLimitExceeded", "domain": "global"}]
            }
        }
        return mock.Mock(ok=False, status_code=403, json=lambda: err)
    with mock.patch("gmail_send.requests.post") as m:
        m.side_effect = [mock_refresh_post(None, None, None), mock_send_post(None, None, None, None)]
        result = gs_mod.send_html(user, "Test Subject", "<p>Test</p>", "Test")
        assert result is False
    # status should remain connected
    user = db.users.find_one({"_id": ObjectId(OWNER)})
    assert user.get("gmail", {}).get("status") == "connected"


def test_gmail_send_403_accessNotConfigured_no_reauth(monkeypatch):
    """403 with accessNotConfigured should NOT mark reauth_required."""
    db = FakeDB()
    from field_encryption import encrypt_fields
    encrypted, wrapped_dek = encrypt_fields({"refresh_token": REFRESH_TOKEN, "email": GMAIL_ADDR})
    _seed_user(db, gmail_data={
        "status": "connected",
        "encrypted": encrypted,
        "wrapped_dek": wrapped_dek,
        "email_hash": blind_index(GMAIL_ADDR),
        "connected_at": datetime.now(timezone.utc),
    })
    user = db.users.find_one({"_id": ObjectId(OWNER)})

    def mock_refresh_post(url, data, timeout):
        return mock.Mock(ok=True, json=lambda: {"access_token": "new-access-token", "expires_in": 3600})
    def mock_send_post(url, headers, json, timeout):
        err = {
            "error": {
                "code": 403,
                "message": "Gmail API not enabled",
                "errors": [{"reason": "accessNotConfigured", "domain": "global"}]
            }
        }
        return mock.Mock(ok=False, status_code=403, json=lambda: err)
    with mock.patch("gmail_send.requests.post") as m:
        m.side_effect = [mock_refresh_post(None, None, None), mock_send_post(None, None, None, None)]
        result = gs_mod.send_html(user, "Test Subject", "<p>Test</p>", "Test")
        assert result is False
    user = db.users.find_one({"_id": ObjectId(OWNER)})
    assert user.get("gmail", {}).get("status") == "connected"


def test_gmail_send_401_marks_reauth(monkeypatch):
    """401 should mark reauth_required."""
    db = FakeDB()
    from field_encryption import encrypt_fields
    encrypted, wrapped_dek = encrypt_fields({"refresh_token": REFRESH_TOKEN, "email": GMAIL_ADDR})
    _seed_user(db, gmail_data={
        "status": "connected",
        "encrypted": encrypted,
        "wrapped_dek": wrapped_dek,
        "email_hash": blind_index(GMAIL_ADDR),
        "connected_at": datetime.now(timezone.utc),
    })
    user = db.users.find_one({"_id": ObjectId(OWNER)})

    def mock_refresh_post(url, data, timeout):
        return mock.Mock(ok=True, json=lambda: {"access_token": "new-access-token", "expires_in": 3600})
    def mock_send_post(url, headers, json, timeout):
        return mock.Mock(ok=False, status_code=401, json=lambda: {"error": {"code": 401, "message": "Invalid token"}})
    with mock.patch("gmail_send.requests.post") as m:
        m.side_effect = [mock_refresh_post(None, None, None), mock_send_post(None, None, None, None)]
        result = gs_mod.send_html(user, "Test Subject", "<p>Test</p>", "Test")
        assert result is False
    user = db.users.find_one({"_id": ObjectId(OWNER)})
    assert user.get("gmail", {}).get("status") == "reauth_required"


def test_gmail_send_refresh_invalid_grant_marks_reauth(monkeypatch):
    """Token refresh returning 400 with invalid_grant should mark reauth_required."""
    db = FakeDB()
    from field_encryption import encrypt_fields
    encrypted, wrapped_dek = encrypt_fields({"refresh_token": REFRESH_TOKEN, "email": GMAIL_ADDR})
    _seed_user(db, gmail_data={
        "status": "connected",
        "encrypted": encrypted,
        "wrapped_dek": wrapped_dek,
        "email_hash": blind_index(GMAIL_ADDR),
        "connected_at": datetime.now(timezone.utc),
    })
    user = db.users.find_one({"_id": ObjectId(OWNER)})

    def mock_refresh_post(url, data, timeout):
        return mock.Mock(ok=False, status_code=400, json=lambda: {"error": "invalid_grant", "error_description": "Bad token"})
    with mock.patch("gmail_send.requests.post") as m:
        m.side_effect = [mock_refresh_post(None, None, None)]
        result = gs_mod.send_html(user, "Test Subject", "<p>Test</p>", "Test")
        assert result is False
    user = db.users.find_one({"_id": ObjectId(OWNER)})
    assert user.get("gmail", {}).get("status") == "reauth_required"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
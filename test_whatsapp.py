"""Tests for the WhatsApp dictation intake channel (whatsapp.py +
whatsapp_routes.py).

Covers the Cloud API helpers (signature verification, phone normalization, the
"configured" gate, and outbound ``send_message`` / ``send_otp_message`` built on
``_post_message``) and the webhook end-to-end: Meta handshake, signature
rejection, inbound text/audio intake into dictation sessions, the
"session_ready" notification, unmatched-number replies, and per-phone rate
limiting. Like test_jobs.py, the daemon thread is swapped for a synchronous
shim so every message is fully processed before the request returns.
"""

import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from unittest import mock

# whatsapp_routes -> sessions -> employees -> config requires SECRET_KEY at
# import time.
os.environ.setdefault("SECRET_KEY", "test-secret-key")

import bson
from bson import ObjectId
from flask import Flask

import whatsapp as whatsapp_mod
import whatsapp_routes as wr

_ORG = "64b000000000000000000001"
_USER = "64b000000000000000000002"
_PHONE = "919999999999"


# ── Hermetic in-memory MongoDB ────────────────────────────────────────────


class _FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, key, direction):
        self._docs.sort(key=lambda d: d.get(key), reverse=(direction == -1))
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    def __iter__(self):
        return iter(self._docs)


class _FakeCollection:
    def __init__(self):
        self.docs = []

    def _match(self, doc, q):
        for k, v in q.items():
            if isinstance(v, dict) and "$ne" in v:
                if doc.get(k) == v["$ne"]:
                    return False
            elif isinstance(v, dict) and "$gt" in v:
                if not (doc.get(k) is not None and doc.get(k) > v["$gt"]):
                    return False
            elif doc.get(k) != v:
                return False
        return True

    def count_documents(self, q):
        return sum(1 for d in self.docs if self._match(d, q))

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
        return _FakeCursor([d for d in self.docs if self._match(d, q)])

    def insert_one(self, doc):
        doc = dict(doc)
        doc.setdefault("_id", bson.ObjectId())
        self.docs.append(doc)
        return mock.Mock(inserted_id=doc["_id"])

    def update_one(self, q, update, upsert=False):
        matches = [d for d in self.docs if self._match(d, q)]
        if not matches:
            return mock.Mock(matched_count=0)
        matched = matches[0]
        for op, values in (update or {}).items():
            if op == "$set":
                matched.update(values)
            elif op == "$inc":
                for key, delta in values.items():
                    matched[key] = matched.get(key, 0) + delta
        return mock.Mock(matched_count=1)

    def create_index(self, *a, **k):
        pass

    def clear(self):
        self.docs = []


class _FakeDB:
    def __init__(self):
        for name in ("rate_limits", "users", "sessions", "employees", "notifications", "phone_otps"):
            setattr(self, name, _FakeCollection())

    def fresh(self):
        for coll in vars(self).values():
            coll.clear()
        return self

    def __getitem__(self, name):
        coll = getattr(self, name, None)
        if coll is None:
            coll = _FakeCollection()
            setattr(self, name, coll)
        return coll


class _SyncThread:
    """Runs the background worker synchronously when .start() is called."""

    def __init__(self, target=None, args=(), daemon=False):
        self._target, self._args = target, args

    def start(self):
        if self._target:
            self._target(*self._args)


class _FakeSTT:
    content_type = "audio"

    def __init__(self, text="Transcribed from voice note"):
        self.text = text
        self.calls = []

    def transcribe(self, audio_bytes, content_type="audio/webm", language=None):
        self.calls.append({"audio_bytes": audio_bytes, "content_type": content_type, "language": language})
        return self.text


def _text_payload(phone, body):
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "wa_biz_1",
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "contacts": [{"profile": {"name": "Sender"}, "wa_id": phone}],
                    "messages": [{
                        "from": phone,
                        "id": "wamid.TESTTEXT",
                        "timestamp": "1700000000",
                        "type": "text",
                        "text": {"body": body},
                    }],
                },
            }],
        }],
    }


def _audio_payload(phone, media_id="MEDIA123"):
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "wa_biz_1",
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "contacts": [{"profile": {"name": "Sender"}, "wa_id": phone}],
                    "messages": [{
                        "from": phone,
                        "id": "wamid.TESTAUDIO",
                        "timestamp": "1700000000",
                        "type": "audio",
                        "audio": {"mime_type": "audio/ogg", "id": media_id, "voice": True},
                    }],
                },
            }],
        }],
    }


def _document_payload(phone, media_id="MEDIADOC", mime_type="text/plain", filename="notes.txt"):
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "wa_biz_1",
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "contacts": [{"profile": {"name": "Sender"}, "wa_id": phone}],
                    "messages": [{
                        "from": phone,
                        "id": "wamid.TESTDOC",
                        "timestamp": "1700000000",
                        "type": "document",
                        "document": {
                            "id": media_id,
                            "mime_type": mime_type,
                            "filename": filename,
                        },
                    }],
                },
            }],
        }],
    }


def _make_client(monkeypatch, db=None):
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="test-secret-key")
    app.register_blueprint(wr.whatsapp_bp, url_prefix="/api")
    db = db or _FakeDB()
    monkeypatch.setattr(wr, "get_db", lambda: db)
    monkeypatch.setattr(wr.threading, "Thread", _SyncThread)
    monkeypatch.setattr(wr, "verify_webhook_signature", lambda *a, **k: True)
    return app.test_client(), db


def _seed_user(db, phone=_PHONE):
    db.users.insert_one({
        "_id": ObjectId(_USER),
        "org_id": ObjectId(_ORG),
        "phone_number": phone,
    })


def _seeded_store(monkeypatch, db=None):
    db = db or _FakeDB()
    _seed_user(db)
    return _make_client(monkeypatch, db)


class TestWhatsAppHelpers:
    def test_normalize_phone_strips_formatting(self):
        assert whatsapp_mod.normalize_phone("+1 (555) 123-4567") == "15551234567"
        assert whatsapp_mod.normalize_phone("919999999999") == "919999999999"
        assert whatsapp_mod.normalize_phone(None) == ""
        assert whatsapp_mod.normalize_phone("") == ""

    def test_verify_webhook_signature_accepts_real_hmac(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_APP_SECRET", "super-secret")
        payload = b'{"object": "whatsapp_business_account"}'
        digest = hmac.new(b"super-secret", payload, hashlib.sha256).hexdigest()
        assert whatsapp_mod.verify_webhook_signature(payload, f"sha256={digest}") is True

    def test_verify_webhook_signature_rejects_tampered_body(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_APP_SECRET", "super-secret")
        payload = b'{"object": "whatsapp_business_account"}'
        digest = hmac.new(b"super-secret", payload, hashlib.sha256).hexdigest()
        assert whatsapp_mod.verify_webhook_signature(b"tampered", f"sha256={digest}") is False

    def test_verify_webhook_signature_fails_closed(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_APP_SECRET", "super-secret")
        assert whatsapp_mod.verify_webhook_signature(b"x", "") is False
        assert whatsapp_mod.verify_webhook_signature(b"x", "sha256=nothex") is False
        assert whatsapp_mod.verify_webhook_signature(b"x", "md5=abc") is False
        monkeypatch.delenv("WHATSAPP_APP_SECRET")
        assert whatsapp_mod.verify_webhook_signature(b"x", "sha256=abc") is False

    def test_is_configured_gate(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "token")
        monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123")
        assert whatsapp_mod.is_configured() is True
        monkeypatch.delenv("WHATSAPP_PHONE_NUMBER_ID")
        assert whatsapp_mod.is_configured() is False


class _FakeResp:
    def __init__(self, status_code=200, text='{"messages":[{"id":"wamid.X"}]}'):
        self.status_code = status_code
        self.text = text


class TestSendOtpMessage:
    """send_otp_message uses an approved template when configured, with a
    free-form text fallback otherwise."""

    _BASE_ENV = {
        "WHATSAPP_ACCESS_TOKEN": "tok",
        "WHATSAPP_PHONE_NUMBER_ID": "123",
        "WHATSAPP_OTP_TEMPLATE_NAME": "voovr_otp",
        "WHATSAPP_OTP_LANG": "en",
    }

    def _configure(self, monkeypatch, **overrides):
        env = dict(self._BASE_ENV)
        env.update(overrides)
        for k, v in env.items():
            if v is None:
                monkeypatch.delenv(k, raising=False)
            else:
                monkeypatch.setenv(k, v)

    def _capture(self, monkeypatch, status_code=200):
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            captured["timeout"] = timeout
            return _FakeResp(status_code=status_code)

        monkeypatch.setattr(whatsapp_mod.requests, "post", fake_post)
        return captured

    def test_template_payload_when_configured(self, monkeypatch):
        self._configure(monkeypatch)
        captured = self._capture(monkeypatch)
        assert whatsapp_mod.send_otp_message("917983582371", "123456") is True
        assert captured["url"].endswith("/v20.0/123/messages")
        assert captured["headers"]["Authorization"] == "Bearer tok"
        body = captured["json"]
        assert body["to"] == "917983582371"
        assert body["type"] == "template"
        assert body["template"]["name"] == "voovr_otp"
        assert body["template"]["language"] == {"code": "en"}
        component = body["template"]["components"][0]
        assert component["type"] == "body"
        assert component["parameters"] == [{"type": "text", "text": "123456"}]

    def test_template_language_defaults_to_en(self, monkeypatch):
        self._configure(monkeypatch, WHATSAPP_OTP_LANG=None)
        captured = self._capture(monkeypatch)
        assert whatsapp_mod.send_otp_message("917983582371", "123456") is True
        assert captured["json"]["template"]["language"] == {"code": "en"}

    def test_falls_back_to_plain_text_when_no_template(self, monkeypatch):
        self._configure(monkeypatch, WHATSAPP_OTP_TEMPLATE_NAME=None)
        captured = self._capture(monkeypatch)
        assert whatsapp_mod.send_otp_message("917983582371", "123456") is True
        body = captured["json"]
        assert body["type"] == "text"
        assert "123456" in body["text"]["body"]

    def test_non_2xx_returns_false(self, monkeypatch):
        self._configure(monkeypatch)
        self._capture(monkeypatch, status_code=400)
        assert whatsapp_mod.send_otp_message("917983582371", "123456") is False

    def test_missing_fields_make_no_request(self, monkeypatch):
        self._configure(monkeypatch)
        sent = []
        monkeypatch.setattr(
            whatsapp_mod.requests, "post",
            lambda *a, **k: sent.append(1) or _FakeResp(),
        )
        assert whatsapp_mod.send_otp_message("", "123456") is False
        assert whatsapp_mod.send_otp_message("917983582371", "") is False
        assert sent == []

    def test_not_configured_returns_false(self, monkeypatch):
        self._configure(
            monkeypatch,
            WHATSAPP_ACCESS_TOKEN=None,
            WHATSAPP_PHONE_NUMBER_ID=None,
        )
        sent = []
        monkeypatch.setattr(
            whatsapp_mod.requests, "post",
            lambda *a, **k: sent.append(1) or _FakeResp(),
        )
        assert whatsapp_mod.send_otp_message("917983582371", "123456") is False
        assert sent == []


class TestPhoneOtpEndpointUsesTemplate:
    """request_phone_otp delivers the code through send_otp_message (template
    path when configured), not a free-form send_message.

    Source-inspection on purpose: importing api.py triggers config.py, which
    loads .env into the shared test process and flips field-encryption's KMS
    gate. The repo's batch tests use the same style for rate-limit wiring."""

    def _api_source(self):
        path = os.path.join(os.path.dirname(__file__), "api.py")
        return open(path, encoding="utf-8").read()

    def _route_body(self, name):
        source = self._api_source()
        start = source.find(f"def {name}(")
        assert start != -1, f"{name} not found in api.py"
        end = source.find("\n@", start + 1)
        return source[start:end if end != -1 else None]

    def test_request_phone_otp_uses_send_otp_message(self):
        body = self._route_body("request_phone_otp")
        assert "send_otp_message(phone, otp)" in body
        assert "send_message(" not in body

    def test_request_phone_otp_still_returns_send_failed_on_failure(self):
        body = self._route_body("request_phone_otp")
        assert "send_failed" in body
        assert "502" in body

    def test_verify_phone_otp_still_intact(self):
        body = self._route_body("verify_phone_otp")
        assert "phone_otps" in body
        assert "invalid_otp" in body
        assert "phone_in_use" in body


class TestWhatsAppWebhookHandshake:
    def test_valid_token_returns_challenge(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "verify-me")
        client, _ = _make_client(monkeypatch)
        r = client.get(
            "/api/whatsapp/webhook",
            query_string={
                "hub.mode": "subscribe",
                "hub.verify_token": "verify-me",
                "hub.challenge": "CHALLENGE123",
            },
        )
        assert r.status_code == 200
        assert r.data.decode() == "CHALLENGE123"

    def test_wrong_token_rejected(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "verify-me")
        client, _ = _make_client(monkeypatch)
        r = client.get(
            "/api/whatsapp/webhook",
            query_string={
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong",
                "hub.challenge": "CHALLENGE123",
            },
        )
        assert r.status_code == 403


class TestWhatsAppWebhookIntake:
    def test_bad_signature_rejected(self, monkeypatch):
        client, db = _make_client(monkeypatch)
        monkeypatch.setattr(wr, "verify_webhook_signature", lambda *a, **k: False)
        r = client.post("/api/whatsapp/webhook", data=b"{}", headers={"X-Hub-Signature-256": "sha256=aa"})
        assert r.status_code == 403
        assert db.sessions.docs == []

    def test_wrong_object_rejected(self, monkeypatch):
        client, _ = _make_client(monkeypatch)
        r = client.post("/api/whatsapp/webhook", json={"object": "not_whatsapp"})
        assert r.status_code == 400

    def test_unmatched_number_gets_link_reply_no_session(self, monkeypatch):
        client, db = _make_client(monkeypatch)
        sent = []
        monkeypatch.setattr(wr, "send_message", lambda phone, text: sent.append((phone, text)) or True)

        r = client.post("/api/whatsapp/webhook", json=_text_payload("911111111111", "Dictate this"))
        assert r.status_code == 200
        assert db.sessions.docs == []
        assert db.notifications.docs == []
        assert len(sent) == 1
        assert sent[0][0] == "911111111111"
        assert "isn't linked" in sent[0][1]

    def test_text_message_creates_session_and_notification(self, monkeypatch):
        client, db = _seeded_store(monkeypatch)
        sent = []
        monkeypatch.setattr(wr, "send_message", lambda phone, text: sent.append((phone, text)) or True)

        r = client.post("/api/whatsapp/webhook", json=_text_payload(_PHONE, "Hi, this is a dictation."))
        assert r.status_code == 200

        sessions = db.sessions.docs
        assert len(sessions) == 1
        s = sessions[0]
        assert s["source"] == "whatsapp_dictation"
        assert s["recording_device"] == "whatsapp_text"
        assert s["recording_type"] == "text"
        assert s["employee_id"] is None
        assert s["transcript"]["raw"] == "Hi, this is a dictation."
        assert s["transcript"]["edited"] == "Hi, this is a dictation."

        n = db.notifications.find_one({"type": "session_ready"})
        assert n is not None
        assert n["source_session_id"] == s["_id"]
        assert n["employee_id"] is None
        assert "WhatsApp dictation" in n["headline"]

        # Immediate ack first, then the "Done!" completion message.
        assert sent[0][1] == wr._ACK_TEXT
        assert "Done!" in sent[-1][1]
        assert all(phone == _PHONE for phone, _ in sent)

    def test_audio_message_transcribes_and_creates_session(self, monkeypatch):
        client, db = _seeded_store(monkeypatch)
        sent = []
        stt = _FakeSTT("Hello from the voice note")
        monkeypatch.setattr(wr, "send_message", lambda phone, text: sent.append((phone, text)) or True)
        monkeypatch.setattr(wr, "get_stt_provider", lambda: stt)
        monkeypatch.setattr(wr, "download_media", lambda media_id: b"FAKEAUDIOBYTES")

        r = client.post("/api/whatsapp/webhook", json=_audio_payload(_PHONE))
        assert r.status_code == 200

        assert stt.calls == [{"audio_bytes": b"FAKEAUDIOBYTES", "content_type": "audio/ogg", "language": None}]

        sessions = db.sessions.docs
        assert len(sessions) == 1
        s = sessions[0]
        assert s["source"] == "whatsapp_dictation"
        assert s["recording_device"] == "whatsapp"
        assert s["recording_type"] == "audio/ogg"
        assert s["transcript"]["raw"] == "Hello from the voice note"
        assert s["employee_id"] is None

        assert db.notifications.find_one({"type": "session_ready"}) is not None
        assert sent[0][1] == wr._ACK_TEXT
        assert "Done!" in sent[-1][1]

    def test_audio_download_failure_does_not_create_session(self, monkeypatch):
        client, db = _seeded_store(monkeypatch)
        sent = []
        monkeypatch.setattr(wr, "send_message", lambda phone, text: sent.append((phone, text)) or True)
        monkeypatch.setattr(wr, "get_stt_provider", lambda: _FakeSTT())
        monkeypatch.setattr(wr, "download_media", lambda media_id: None)

        r = client.post("/api/whatsapp/webhook", json=_audio_payload(_PHONE))
        assert r.status_code == 200
        assert db.sessions.docs == []
        assert any("couldn't fetch" in text for _, text in sent)

    def test_txt_document_creates_session_from_decoded_text(self, monkeypatch):
        client, db = _seeded_store(monkeypatch)
        sent = []
        monkeypatch.setattr(wr, "send_message", lambda phone, text: sent.append((phone, text)) or True)
        monkeypatch.setattr(
            wr, "download_media",
            lambda media_id: "Meeting notes for tomorrow.\nSign the contract.".encode("utf-8"),
        )

        r = client.post("/api/whatsapp/webhook", json=_document_payload(_PHONE))
        assert r.status_code == 200

        sessions = db.sessions.docs
        assert len(sessions) == 1
        s = sessions[0]
        assert s["source"] == "whatsapp_dictation"
        assert s["recording_device"] == "whatsapp_document"
        assert s["recording_type"] == "text"
        assert s["employee_id"] is None
        assert s["transcript"]["raw"] == "Meeting notes for tomorrow.\nSign the contract."
        assert s["transcript"]["edited"] == "Meeting notes for tomorrow.\nSign the contract."

        n = db.notifications.find_one({"type": "session_ready"})
        assert n is not None
        assert n["source_session_id"] == s["_id"]

        assert sent[0][1] == wr._ACK_TEXT
        assert "Done!" in sent[-1][1]
        assert all(phone == _PHONE for phone, _ in sent)

    def test_txt_document_matches_by_filename_when_mime_type_is_generic(self, monkeypatch):
        client, db = _seeded_store(monkeypatch)
        monkeypatch.setattr(wr, "send_message", lambda phone, text: True)
        monkeypatch.setattr(
            wr, "download_media",
            lambda media_id: "Plain text served with a generic mime type.".encode("utf-8"),
        )

        r = client.post(
            "/api/whatsapp/webhook",
            json=_document_payload(_PHONE, mime_type="application/octet-stream", filename="draft.txt"),
        )
        assert r.status_code == 200
        assert len(db.sessions.docs) == 1
        assert db.sessions.docs[0]["recording_type"] == "text"
        assert db.sessions.docs[0]["transcript"]["raw"] == "Plain text served with a generic mime type."

    def test_txt_document_decodes_non_utf8_bytes(self, monkeypatch):
        client, db = _seeded_store(monkeypatch)
        monkeypatch.setattr(wr, "send_message", lambda phone, text: True)
        monkeypatch.setattr(wr, "download_media", lambda media_id: "caf\xe9 latin".encode("latin-1"))

        r = client.post("/api/whatsapp/webhook", json=_document_payload(_PHONE))
        assert r.status_code == 200
        assert len(db.sessions.docs) == 1
        assert "caf" in db.sessions.docs[0]["transcript"]["raw"]

    def test_audio_mime_document_goes_through_stt_and_creates_session(self, monkeypatch):
        client, db = _seeded_store(monkeypatch)
        sent = []
        stt = _FakeSTT("Transcribed from the attached file")
        monkeypatch.setattr(wr, "send_message", lambda phone, text: sent.append((phone, text)) or True)
        monkeypatch.setattr(wr, "get_stt_provider", lambda: stt)
        monkeypatch.setattr(wr, "download_media", lambda media_id: b"FAKEAUDIOBYTES")

        payload = _document_payload(_PHONE, media_id="MEDIAMP3", mime_type="audio/mpeg", filename="recording.mp3")
        r = client.post("/api/whatsapp/webhook", json=payload)
        assert r.status_code == 200

        assert stt.calls == [{"audio_bytes": b"FAKEAUDIOBYTES", "content_type": "audio/mpeg", "language": None}]

        sessions = db.sessions.docs
        assert len(sessions) == 1
        s = sessions[0]
        assert s["source"] == "whatsapp_dictation"
        assert s["recording_device"] == "whatsapp_document"
        assert s["recording_type"] == "audio/mpeg"
        assert s["transcript"]["raw"] == "Transcribed from the attached file"
        assert s["employee_id"] is None

        assert db.notifications.find_one({"type": "session_ready"}) is not None
        assert sent[0][1] == wr._ACK_TEXT
        assert "Done!" in sent[-1][1]

    def test_unsupported_document_gets_not_supported_reply_no_session(self, monkeypatch):
        client, db = _seeded_store(monkeypatch)
        sent = []
        monkeypatch.setattr(wr, "send_message", lambda phone, text: sent.append((phone, text)) or True)

        payload = _document_payload(_PHONE, media_id="MEDIAPDF", mime_type="application/pdf", filename="report.pdf")
        r = client.post("/api/whatsapp/webhook", json=payload)
        assert r.status_code == 200

        assert db.sessions.docs == []
        assert db.notifications.docs == []
        assert sent[0][1] == wr._ACK_TEXT
        assert any(".txt" in text and "aren't supported yet" in text for _, text in sent)

    def test_rate_limited_phone_gets_throttle_reply(self, monkeypatch):
        client, db = _seeded_store(monkeypatch)
        sent = []
        monkeypatch.setattr(wr, "send_message", lambda phone, text: sent.append((phone, text)) or True)

        now = datetime.now(timezone.utc)
        for _ in range(wr._WHATSAPP_INBOUND_MAX):
            db.rate_limits.insert_one({
                "key": f"whatsapp_inbound:{_PHONE}",
                "ts": now,
                "expire_at": now + timedelta(seconds=3600),
            })

        r = client.post("/api/whatsapp/webhook", json=_text_payload(_PHONE, "Too many"))
        assert r.status_code == 200
        assert db.sessions.docs == []
        assert len(sent) == 1
        assert "message limit" in sent[0][1]

    def test_just_below_rate_limit_still_processes(self, monkeypatch):
        client, db = _seeded_store(monkeypatch)
        sent = []
        monkeypatch.setattr(wr, "send_message", lambda phone, text: sent.append((phone, text)) or True)

        now = datetime.now(timezone.utc)
        for _ in range(wr._WHATSAPP_INBOUND_MAX - 1):
            db.rate_limits.insert_one({
                "key": f"whatsapp_inbound:{_PHONE}",
                "ts": now,
                "expire_at": now + timedelta(seconds=3600),
            })

        r = client.post("/api/whatsapp/webhook", json=_text_payload(_PHONE, "One more"))
        assert r.status_code == 200
        assert len(db.sessions.docs) == 1
        assert sent[0][1] == wr._ACK_TEXT
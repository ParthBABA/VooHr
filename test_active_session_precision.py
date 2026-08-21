"""Backend tests for active-session device + region precision.

Root cause being regression-tested: modern Chromium reports
"Windows NT 10.0" in the User-Agent for BOTH Windows 10 and Windows 11,
and api._parse_device mapped "10.0" -> "10", so Windows 11 machines were
labelled "Chrome ... on Windows 10".  The only reliable discriminator is
the User-Agent Client Hint Sec-CH-UA-Platform-Version (major >= 13 means
Windows 11), which login_flow now captures at login time.

Also covered: trusted client-IP resolution behind the Railway reverse
proxy (geo was computed from the proxy hop, yielding no location),
location normalisation to {city, region, country} with nulls, and the
privacy guarantee that raw IPs / Client Hints never reach the frontend.

Backend-only surface: login_flow.py, api.py, app.py (Accept-CH opt-in).
"""

import hashlib
import sys
import uuid
from datetime import datetime, timezone

import pytest

_ROOT = sys.path[0] if sys.path[0] else "."
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bson
from bson import ObjectId
from datetime import timedelta
import unittest.mock as _mock

# test_security_fixes.py may have replaced sys.modules["requests"] /
# ["flask"] with MagicMocks at collection time.  Evict ONLY mock entries so
# the real packages load for our import (same pattern as
# test_csrf_rate_limit_fix.py).
for _name in ("requests", "flask"):
    if isinstance(sys.modules.get(_name), _mock.MagicMock):
        del sys.modules[_name]

# ── In-memory fake MongoDB (hermetic: no real database anywhere) ─────
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
        return _FakeCursor([d for d in self.docs if self._match(d, q)])

    def insert_one(self, doc):
        doc = dict(doc)
        doc.setdefault("_id", bson.ObjectId())
        self.docs.append(doc)

    def update_one(self, q, update, upsert=False):
        matches = [d for d in self.docs if self._match(d, q)]
        if not matches:
            return
        doc = matches[0]
        if "$set" in update:
            doc.update(update["$set"])

    def delete_one(self, q):
        matches = [d for d in self.docs if self._match(d, q)]
        if matches:
            self.docs.remove(matches[0])

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

# Deliberately do NOT import the real `app` module here: app.py binds
# `get_db` at import time (from-import), so whoever imports it first pins
# that binding to their own fake and breaks every other test module's
# assumptions.  Instead we build an isolated Flask app around api_bp and
# patch get_db per-test with restore — the convention the rest of the
# suite uses ("tests elsewhere always patch get_db per-test").
import api as _api
import employees as _employees
import login_flow as _login_flow
from flask import Flask as _Flask

_test_app = _Flask(__name__)
_test_app.config.update(TESTING=True, SECRET_KEY="unit-test-secret")
_test_app.register_blueprint(_api.api_bp, url_prefix="/api")


# ── Shared fixtures / helpers ─────────────────────────────────────────

_USER_A = "64b00000000000000000000a"
_USER_B = "64b00000000000000000000b"

UA_WIN_CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
UA_WIN_EDGE = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36 Edg/141.0.0.0"
)
UA_WIN_FIREFOX = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) "
    "Gecko/20100101 Firefox/132.0"
)
UA_WIN7_CHROME = (
    "Mozilla/5.0 (Windows NT 6.1; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
UA_MAC_SAFARI = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.6 Safari/605.1.15"
)
UA_ANDROID = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Mobile Safari/537.36"
)
UA_IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 "
    "Safari/604.1"
)


def _seed_user(oid, email_hash="eh"):
    _FAKE_DB.users.insert_one({
        "_id": ObjectId(oid),
        "org_id": ObjectId("64b000000000000000000002"),
        "role": "admin",
        "totp_enabled": False,
        "email_hash": email_hash,
    })


def _insert_session(user_oid, ua=UA_WIN_CHROME, ip="", location=None,
                    ch_platform="", ch_platform_version="", token_suffix="",
                    age_seconds=0):
    raw_token = str(uuid.uuid4()) + token_suffix
    now = datetime.now(timezone.utc)
    seen = now - timedelta(seconds=age_seconds)
    _FAKE_DB.active_sessions.insert_one({
        "user_id": ObjectId(user_oid),
        "session_token": hashlib.sha256(raw_token.encode()).hexdigest(),
        "user_agent": ua,
        "ch_platform": ch_platform,
        "ch_platform_version": ch_platform_version,
        "ip": ip,
        "location": location,
        "created_at": seen,
        "last_seen": seen,
    })
    return raw_token


@pytest.fixture()
def db():
    _FAKE_DB.users.clear()
    _FAKE_DB.active_sessions.clear()
    _FAKE_DB.rate_limits.clear()
    return _FAKE_DB


_DB_PATCH_MODULES = ("api", "employees", "sessions", "notifications",
                     "auth_email", "auth", "totp_routes")


@pytest.fixture()
def client():
    _FAKE_DB.users.clear()
    _FAKE_DB.active_sessions.clear()
    _FAKE_DB.rate_limits.clear()
    originals = []
    for name in _DB_PATCH_MODULES:
        mod = sys.modules.get(name)
        if mod is not None and hasattr(mod, "get_db"):
            originals.append((mod, mod.get_db))
            mod.get_db = lambda: _FAKE_DB
    try:
        with _test_app.test_client() as c:
            yield c
    finally:
        for mod, fn in originals:
            mod.get_db = fn


def _login(client, user_oid, raw_session_token):
    with client.session_transaction() as sess:
        sess["user_id"] = user_oid
        sess["session_token"] = raw_session_token


# ── 1-3, 4-9: device parsing ──────────────────────────────────────────

class TestWindowsDetection:
    def test_windows11_via_client_hints(self, db):
        d = _api._parse_device(UA_WIN_CHROME, "Windows", "13.0.0")
        assert d["os"] == "Windows 11"
        assert d["browser"] == "Chrome 151"
        assert d["device_type"] == "Desktop"

    def test_windows10_via_client_hints(self, db):
        d = _api._parse_device(UA_WIN_CHROME, "Windows", "10.0.0")
        assert d["os"] == "Windows 10"

    def test_windows11_future_platform_version(self, db):
        # Chromium maps Win11 feature updates to 14.x, 15.x, ...
        d = _api._parse_device(UA_WIN_CHROME, "Windows", "14.2.1")
        assert d["os"] == "Windows 11"

    def test_ambiguous_windows_falls_back_to_generic(self, db):
        # No Client Hints stored (e.g. sessions recorded before this fix,
        # or browsers that never send hints such as Firefox) -> plain
        # "Windows", NEVER a guess of 10 or 11.
        d = _api._parse_device(UA_WIN_CHROME)
        assert d["os"] == "Windows"

    def test_firefox_on_windows_stays_generic(self, db):
        d = _api._parse_device(UA_WIN_FIREFOX)
        assert d["browser"] == "Firefox 132"
        assert d["os"] == "Windows"

    def test_contradicting_platform_hint_not_trusted(self, db):
        d = _api._parse_device(UA_WIN_CHROME, "macOS", "13.0.0")
        assert d["os"] == "Windows"

    def test_garbage_platform_version_not_trusted(self, db):
        d = _api._parse_device(UA_WIN_CHROME, "Windows", "banana")
        assert d["os"] == "Windows"

    def test_windows7_still_resolved_from_user_agent(self, db):
        d = _api._parse_device(UA_WIN7_CHROME)
        assert d["os"] == "Windows 7"

    def test_edge_browser_and_windows11(self, db):
        d = _api._parse_device(UA_WIN_EDGE, "Windows", "13.0.0")
        assert d["browser"] == "Edge 141"
        assert d["os"] == "Windows 11"


class TestBrowserAndOsParsing:
    def test_chrome_version_major_only(self, db):
        d = _api._parse_device(UA_WIN_CHROME, "Windows", "10.0.0")
        assert d["browser"] == "Chrome 151"

    def test_edge_not_misdetected_as_chrome(self, db):
        d = _api._parse_device(UA_WIN_EDGE)
        assert d["browser"].startswith("Edge ")

    def test_firefox_version(self, db):
        d = _api._parse_device(UA_WIN_FIREFOX)
        assert d["browser"] == "Firefox 132"

    def test_macos_detection(self, db):
        d = _api._parse_device(UA_MAC_SAFARI)
        assert d["device_type"] == "Desktop"
        assert d["browser"] == "Safari 17"
        assert d["os"] == "macOS 10.15.7"

    def test_android_phone_detection(self, db):
        d = _api._parse_device(UA_ANDROID)
        assert d["device_type"] == "Mobile"
        assert d["os"] == "Android 14"
        assert d["browser"] == "Chrome 151"

    def test_android_tablet_detection(self, db):
        ua = UA_ANDROID.replace("; Pixel 8)", "; Pixel Tablet)").replace("Mobile Safari", "Safari")
        d = _api._parse_device(ua)
        assert d["device_type"] == "Tablet"

    def test_ios_detection(self, db):
        d = _api._parse_device(UA_IPHONE)
        assert d["device_type"] == "Mobile"
        assert d["os"] == "iOS 17.0"

    def test_empty_user_agent_never_crashes(self, db):
        d = _api._parse_device("")
        assert d == {"device_type": "Desktop", "browser": "Unknown", "os": "Unknown"}


# ── 12-13: trusted client-IP resolution ───────────────────────────────

class TestClientIpResolution:
    def _ip(self, remote_addr, xff=None):
        headers = {"X-Forwarded-For": xff} if xff else {}
        with _test_app.test_request_context(
            "/", environ_base={"REMOTE_ADDR": remote_addr}, headers=headers,
        ):
            return _login_flow._client_ip()

    def test_public_peer_trusted_directly(self, db):
        assert self._ip("203.0.113.5") == "203.0.113.5"

    def test_public_peer_ignores_forwarded_header(self, db):
        # Direct public connection: XFF (client-forgeable) must be ignored.
        assert self._ip("203.0.113.5", "1.2.3.4") == "203.0.113.5"

    def test_proxy_hop_uses_rightmost_public_forwarded_ip(self, db):
        # Railway edge: private peer + proxy-appended chain.  Rightmost
        # public entry is the one added by the trusted edge.
        assert self._ip("10.1.2.3", "203.0.113.99, 198.51.100.9") == "198.51.100.9"

    def test_spoofed_leftmost_entry_ignored(self, db):
        # Client plants a bogus public IP at the front of the chain; the
        # trusted edge appends the real one.  Right-to-left walk skips it.
        assert self._ip("10.1.2.3", "6.6.6.6, 198.51.100.9") == "198.51.100.9"

    def test_all_private_chain_yields_no_ip(self, db):
        assert self._ip("172.20.0.5", "192.168.1.1, 10.0.0.5") == ""

    def test_private_peer_without_header_yields_no_ip(self, db):
        assert self._ip("127.0.0.1") == ""


# ── 10-12: location lookup + formatting ───────────────────────────────

class TestLocationLookup:
    def test_successful_lookup_formats_city_region_country(self, db):
        resp = _mock.MagicMock()
        resp.json.return_value = {
            "status": "success",
            "city": "Dehradun",
            "regionName": "Uttarakhand",
            "country": "India",
        }
        with _mock.patch.object(_login_flow.requests, "get", return_value=resp) as g:
            loc = _login_flow._lookup_location("203.0.113.7")
        assert loc == {"city": "Dehradun", "region": "Uttarakhand", "country": "India"}
        assert g.call_count == 1

    def test_missing_fields_become_null(self, db):
        resp = _mock.MagicMock()
        resp.json.return_value = {
            "status": "success", "city": "Dehradun", "regionName": "", "country": None,
        }
        with _mock.patch.object(_login_flow.requests, "get", return_value=resp):
            loc = _login_flow._lookup_location("203.0.113.7")
        assert loc == {"city": "Dehradun", "region": None, "country": None}

    def test_all_fields_empty_returns_none(self, db):
        resp = _mock.MagicMock()
        resp.json.return_value = {
            "status": "success", "city": "", "regionName": "", "country": "",
        }
        with _mock.patch.object(_login_flow.requests, "get", return_value=resp):
            assert _login_flow._lookup_location("203.0.113.7") is None

    def test_provider_failure_returns_none(self, db):
        resp = _mock.MagicMock()
        resp.json.return_value = {"status": "fail"}
        with _mock.patch.object(_login_flow.requests, "get", return_value=resp):
            assert _login_flow._lookup_location("203.0.113.7") is None

    def test_network_error_returns_none(self, db):
        with _mock.patch.object(_login_flow.requests, "get",
                                side_effect=RuntimeError("timeout")):
            assert _login_flow._lookup_location("203.0.113.7") is None

    def test_private_ip_short_circuits_without_network_call(self, db):
        with _mock.patch.object(_login_flow.requests, "get") as g:
            assert _login_flow._lookup_location("192.168.1.42") is None
            assert _login_flow._lookup_location("") is None
            g.assert_not_called()


# ── Session recording captures precision metadata ─────────────────────

class TestRecordActiveSession:
    def test_records_client_hints_and_resolved_public_ip(self, db):
        class _SyncThread:
            def __init__(self, target=None, args=(), daemon=False):
                self._target, self._args = target, args

            def start(self):
                pass  # geo thread intentionally not run in this test

        captured = {}
        with _mock.patch.object(_login_flow.threading, "Thread", _SyncThread):
            with _test_app.test_request_context(
                "/",
                method="POST",
                environ_base={"REMOTE_ADDR": "10.9.9.9"},
                headers={
                    "User-Agent": UA_WIN_CHROME,
                    "Sec-CH-UA-Platform": "Windows",
                    "Sec-CH-UA-Platform-Version": "13.0.0",
                    "X-Forwarded-For": "198.51.100.23",
                },
            ):
                _login_flow._record_active_session(db, ObjectId(_USER_A))

        assert len(db.active_sessions.docs) == 1
        doc = db.active_sessions.docs[0]
        captured.update(doc)
        assert doc["ch_platform"] == "Windows"
        assert doc["ch_platform_version"] == "13.0.0"
        assert doc["ip"] == "198.51.100.23"          # resolved public client
        assert doc["user_agent"] == UA_WIN_CHROME
        assert len(doc["session_token"]) == 64       # sha-256 hex, not raw
        assert doc["location"] is None               # filled async, later

    def test_parsed_label_roundtrip_for_recorded_session(self, db):
        """End-to-end: record with Win11 hints -> API parses 'Windows 11'."""
        doc = {
            "user_id": ObjectId(_USER_A),
            "session_token": "h" * 64,
            "user_agent": UA_WIN_CHROME,
            "ch_platform": "Windows",
            "ch_platform_version": "13.0.0",
            "ip": "198.51.100.23",
            "location": None,
        }
        d = _api._parse_device(doc["user_agent"], doc["ch_platform"],
                               doc["ch_platform_version"])
        assert d["os"] == "Windows 11"


# ── 11, 14, 15: /api/sessions/active contract ─────────────────────────

class TestActiveSessionsEndpoint:
    def test_authentication_required(self, client, db):
        resp = client.get("/api/sessions/active")
        assert resp.status_code == 401
        assert resp.get_json()["error"] == "not_authenticated"

    def test_windows11_label_and_location_shape(self, client, db):
        _seed_user(_USER_A)
        raw = _insert_session(
            _USER_A, ua=UA_WIN_CHROME, ip="203.0.113.7",
            location={"city": "Dehradun", "region": "Uttarakhand", "country": "India"},
            ch_platform="Windows", ch_platform_version="13.0.0",
        )
        _login(client, _USER_A, raw)

        resp = client.get("/api/sessions/active")
        assert resp.status_code == 200
        sessions = resp.get_json()["sessions"]
        assert len(sessions) == 1
        s = sessions[0]
        assert s["device"]["os"] == "Windows 11"
        assert s["device"]["browser"] == "Chrome 151"
        assert s["location"] == {
            "city": "Dehradun", "region": "Uttarakhand", "country": "India",
        }

    def test_missing_location_returns_null(self, client, db):
        _seed_user(_USER_A)
        raw = _insert_session(_USER_A, location=None)
        _login(client, _USER_A, raw)

        sessions = client.get("/api/sessions/active").get_json()["sessions"]
        assert sessions[0]["location"] is None

    def test_raw_ip_never_in_response(self, client, db):
        _seed_user(_USER_A)
        raw = _insert_session(
            _USER_A, ip="203.0.113.7",
            location={"city": "Dehradun", "region": "Uttarakhand", "country": "India"},
            ch_platform="Windows", ch_platform_version="13.0.0",
        )
        _login(client, _USER_A, raw)

        resp = client.get("/api/sessions/active")
        body = resp.get_data(as_text=True)
        assert '"ip"' not in body
        assert "203.0.113.7" not in body
        for s in resp.get_json()["sessions"]:
            assert "ip" not in s
            # Internal Client-Hint storage fields are not exposed either;
            # only their parsed effect (device.os) is visible.
            assert "ch_platform" not in s
            assert "ch_platform_version" not in s

    def test_location_dict_cannot_leak_extra_fields(self, client, db):
        _seed_user(_USER_A)
        raw = _insert_session(
            _USER_A,
            location={"city": "Dehradun", "region": "Uttarakhand",
                      "country": "India", "zip": "248001", "as": "AS9829"},
        )
        _login(client, _USER_A, raw)

        s = client.get("/api/sessions/active").get_json()["sessions"][0]
        assert set(s["location"].keys()) == {"city", "region", "country"}

    def test_user_isolation_only_own_sessions_listed(self, client, db):
        _seed_user(_USER_A)
        _seed_user(_USER_B)
        raw_a = _insert_session(_USER_A, ua=UA_WIN_CHROME)
        _insert_session(_USER_B, ua=UA_MAC_SAFARI)
        _login(client, _USER_A, raw_a)

        sessions = client.get("/api/sessions/active").get_json()["sessions"]
        assert len(sessions) == 1
        docs = [d for d in db.active_sessions.docs
                if d["user_id"] == ObjectId(_USER_A)]
        assert sessions[0]["id"] == str(docs[0]["_id"])

    def test_accept_ch_opt_in_present_in_app(self, client, db):
        # The opt-in that makes Sec-CH-UA-Platform-Version legitimately
        # available on the next login request.  Verified via source
        # inspection: instantiating the real create_app() here would pin
        # app.py's import-time get_db binding and pollute sibling test
        # modules (see module docstring / comment above).
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "app.py"), encoding="utf-8") as f:
            src = f.read()
        assert "Accept-CH" in src
        assert "Sec-CH-UA-Platform-Version" in src


# ── Read-path enrichment: stale rows reach the UI precisely ──────────
#
# Sessions recorded BEFORE Client-Hint capture / trusted-IP geo existed
# have no ch_* fields and location=None forever — the UI showed
# "Chrome 151 on Windows" / "Local network" for them regardless of the
# login-time fixes.  list_active_sessions now enriches such rows from the
# requesting browser itself.

_HINT_HEADERS = {
    "User-Agent": UA_WIN_CHROME,
    "Sec-CH-UA-Platform": "Windows",
    "Sec-CH-UA-Platform-Version": "13.0.0",
}


class TestReadPathEnrichment:
    def test_stale_current_session_gains_windows11_from_request_hints(
            self, client, db):
        _seed_user(_USER_A)
        raw = _insert_session(_USER_A, ua=UA_WIN_CHROME)  # no hints stored
        _login(client, _USER_A, raw)

        resp = client.get("/api/sessions/active", headers=_HINT_HEADERS)
        s = resp.get_json()["sessions"][0]
        assert s["device"]["os"] == "Windows 11"
        assert s["device"]["browser"] == "Chrome 151"

        # Persisted: the next visit needs no backfill.
        doc = db.active_sessions.docs[0]
        assert doc["ch_platform"] == "Windows"
        assert doc["ch_platform_version"] == "13.0.0"

    def test_hints_not_applied_when_user_agent_differs(self, client, db):
        _seed_user(_USER_A)
        raw = _insert_session(_USER_A, ua=UA_MAC_SAFARI)
        _login(client, _USER_A, raw)

        resp = client.get("/api/sessions/active", headers=_HINT_HEADERS)
        s = resp.get_json()["sessions"][0]
        assert s["device"]["os"] == "macOS 10.15.7"   # parsed from its own UA
        doc = db.active_sessions.docs[0]
        assert doc["ch_platform_version"] == ""       # untouched

    def test_location_backfilled_for_current_session(self, client, db):
        _seed_user(_USER_A)
        raw = _insert_session(_USER_A, ip="10.1.2.3", location=None)
        _login(client, _USER_A, raw)

        expected = {"city": "Dehradun", "region": "Uttarakhand", "country": "India"}
        with _mock.patch.object(_api, "_lookup_location",
                                return_value=expected) as lookup:
            resp = client.get("/api/sessions/active",
                              environ_base={"REMOTE_ADDR": "203.0.113.7"})
        s = resp.get_json()["sessions"][0]
        assert s["location"] == expected
        lookup.assert_called_once_with("203.0.113.7")  # request's public IP

        doc = db.active_sessions.docs[0]
        assert doc["location"] == expected             # persisted

    def test_known_location_never_relooked_up(self, client, db):
        _seed_user(_USER_A)
        raw = _insert_session(_USER_A, ip="203.0.113.7",
                              location={"city": "Dehradun",
                                        "region": "Uttarakhand",
                                        "country": "India"})
        _login(client, _USER_A, raw)

        with _mock.patch.object(_api, "_lookup_location") as lookup:
            resp = client.get("/api/sessions/active")
        assert resp.get_json()["sessions"][0]["location"] == {
            "city": "Dehradun", "region": "Uttarakhand", "country": "India",
        }
        lookup.assert_not_called()

    def test_at_most_one_synchronous_lookup_per_request(self, client, db):
        _seed_user(_USER_A)
        raw_current = _insert_session(_USER_A, ip="10.9.9.9", location=None,
                                      age_seconds=0)
        _insert_session(_USER_A, ip="198.51.100.5", location=None,
                        age_seconds=3600, token_suffix="-older")
        _login(client, _USER_A, raw_current)

        expected = {"city": "Dehradun", "region": "Uttarakhand", "country": "India"}

        class _DeferredThread:
            def __init__(self, target=None, args=(), daemon=False):
                self._target, self._args = target, args

            def start(self):
                pass  # deferred backfill must NOT run inside the request

        with _mock.patch.object(_api, "_lookup_location",
                                return_value=expected) as lookup, \
             _mock.patch.object(_api.threading, "Thread", _DeferredThread):
            resp = client.get("/api/sessions/active",
                              environ_base={"REMOTE_ADDR": "203.0.113.7"})
        lookup.assert_called_once()                    # bounded latency
        sessions = resp.get_json()["sessions"]
        by_current = [s for s in sessions if s["is_current"]]
        assert by_current and by_current[0]["location"] == expected

    def test_enrichment_exposes_no_raw_ip(self, client, db):
        _seed_user(_USER_A)
        raw = _insert_session(_USER_A, ip="203.0.113.7", location=None)
        _login(client, _USER_A, raw)

        with _mock.patch.object(_api, "_lookup_location",
                                return_value={"city": "X", "region": None,
                                              "country": None}):
            resp = client.get("/api/sessions/active",
                              environ_base={"REMOTE_ADDR": "203.0.113.7"})
        body = resp.get_data(as_text=True)
        assert "203.0.113.7" not in body
        assert '"ip"' not in body

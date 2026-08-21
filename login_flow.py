"""Shared login-flow helpers for VooHr.

Placed in a separate module to avoid circular imports between auth.py
(Google OAuth) and auth_email.py (email/password auth).  Both import
from here instead of from each other.

Provides:
  - _record_active_session  — track a new login in active_sessions
  - _login_result_for_user  — single source of truth for post-auth TOTP
    branching: does the user go straight to the dashboard, need TOTP
    verification, or is a new admin who must enrol in TOTP first?
"""

import hashlib
import threading
import uuid
from datetime import datetime, timezone

import requests
from bson import ObjectId
from flask import request, session


def _hash_session_token(token: str) -> str:
    """Deterministic SHA-256 hash of a session token for database storage.

    The raw token lives only in the Flask session cookie and server memory.
    Only the hex digest is persisted in ``active_sessions`` so a database
    compromise never leaks usable session tokens.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _is_private_ip(ip) -> bool:
    """True for loopback/private/local addresses that can never geolocate."""
    if not ip:
        return True
    if ip == "::1":
        return True
    if ":" in ip:
        return True
    try:
        parts = [int(p) for p in ip.split(".")]
    except ValueError:
        return True
    if len(parts) != 4:
        return True
    a, b, _c, _d = parts
    if a == 10:
        return True
    if a == 127:
        return True
    if a == 172 and 16 <= b <= 31:
        return True
    if a == 192 and b == 168:
        return True
    return False


def _client_ip() -> str:
    """Best-effort resolution of the actual client PUBLIC IP for geo
    attribution, safe behind the Railway/reverse-proxy deployment.

    Trust model (deliberately conservative — never blindly trusts XFF):
      1. If the direct peer (remote_addr) is a PUBLIC address, the TCP
         connection itself identifies the client — trust it outright.
      2. Otherwise the peer is a trusted reverse proxy (Railway's edge
         terminates TLS and forwards internally, so remote_addr is a
         private hop).  Proxies APPEND to X-Forwarded-For, while a client
         can plant arbitrary entries at the FRONT of the chain — so walk
         the chain RIGHT to LEFT and take the first public address.  A
         spoofed leftmost entry is therefore ignored.
      3. If nothing public can be determined, return "" — callers treat
         that as "no location", never guessing and never blocking login.
    """
    peer = request.remote_addr or ""
    if not _is_private_ip(peer):
        return peer
    forwarded = request.headers.get("X-Forwarded-For", "")
    for candidate in reversed([c.strip() for c in forwarded.split(",")]):
        if candidate and not _is_private_ip(candidate):
            return candidate
    return ""


def _lookup_location(ip) -> dict:
    """Best-effort geo lookup for a public IP. Returns
    {"city", "region", "country"} or None. Never raises — a geolocation
    failure or timeout must not block login (2s cap, runs once per login).

    Approximate network-level location only (IP registry data); fields the
    provider does not return are normalised to None rather than empty
    strings so the API never emits "undefined"-style values.
    """
    if not ip or _is_private_ip(ip):
        return None
    try:
        resp = requests.get(
            f"http://ip-api.com/json/{ip}?fields=city,regionName,country,status",
            timeout=2,
        )
        data = resp.json()
    except Exception:
        return None
    if data.get("status") != "success":
        return None

    def _clean(value):
        value = (value or "").strip()
        return value or None

    location = {
        "city": _clean(data.get("city")),
        "region": _clean(data.get("regionName")),
        "country": _clean(data.get("country")),
    }
    if not any(location.values()):
        return None
    return location


def _record_active_session(db, user_id: ObjectId):
    """Track this login as an active session so the settings page can list it
    and let the user revoke access.  Storing the token in the Flask session is
    what lets _require_auth validate later requests.

    The geo lookup is run in a background thread so it never blocks the login
    redirect (free-tier backends can add a 2s penalty otherwise).

    Also captures User-Agent Client Hints (Sec-CH-UA-Platform /
    Sec-CH-UA-Platform-Version) when the browser supplies them.  These are
    the only reliable signal distinguishing Windows 11 from Windows 10 —
    both report "Windows NT 10.0" in the plain User-Agent string.  The raw
    values are stored server-side; nothing here is exposed verbatim to the
    frontend (the sessions API returns parsed device/location metadata only).
    """
    now = datetime.now(timezone.utc)
    session_token = str(uuid.uuid4())
    ip = _client_ip()
    session["session_token"] = session_token
    db.active_sessions.insert_one(
        {
            "user_id": ObjectId(user_id),
            "session_token": _hash_session_token(session_token),
            "user_agent": request.headers.get("User-Agent", ""),
            "ch_platform": request.headers.get("Sec-CH-UA-Platform", ""),
            "ch_platform_version": request.headers.get("Sec-CH-UA-Platform-Version", ""),
            "ip": ip,
            "location": None,
            "created_at": now,
            "last_seen": now,
        }
    )
    threading.Thread(
        target=_attach_location_async,
        args=(db, session_token, ip),
        daemon=True,
    ).start()


def _attach_location_async(db, session_token: str, ip: str):
    """Best-effort background geo lookup. Never raises, never blocks login."""
    try:
        location = _lookup_location(ip)
        if location:
            db.active_sessions.update_one(
                {"session_token": _hash_session_token(session_token)},
                {"$set": {"location": location}},
            )
    except Exception:
        pass


def _login_result_for_user(db, user):
    """After authenticating a user, determine the appropriate post-login
    redirect based on TOTP status.

    This is the single source of truth for "does this user need TOTP
    verification, does this admin need forced setup, or do they go
    straight to the dashboard."  Both the Google OAuth callback
    (auth.py) and the email/password endpoints (auth_email.py) call
    this so the branching logic is never duplicated.

    Must be called AFTER _record_active_session so that session_token
    is available in the Flask session.

    Returns a dict:
        redirect       : str  — URL path to redirect the browser to
        requires_totp  : bool — True when the user has TOTP enabled and
                                must verify before accessing the dashboard
        totp_enroll    : bool — True when the user is an admin whose TOTP
                                is not yet enabled (forced setup)
    """
    role = user.get("role")
    totp_enabled = user.get("totp_enabled") is True
    session_token = session.get("session_token", "")

    # Brand-new admin (or admin who disabled TOTP) — force enrolment.
    if role == "admin" and not totp_enabled:
        return {
            "redirect": "/settings/security/setup-totp?forced=1",
            "requires_totp": False,
            "totp_enroll": True,
        }

    # TOTP enabled but this session hasn't presented a valid code yet.
    if totp_enabled and session.get("totp_verified_session") != session_token:
        return {
            "redirect": "/auth/totp/verify-login?next=/dashboard.html",
            "requires_totp": True,
            "totp_enroll": False,
        }

    # No TOTP gate — straight to dashboard.
    return {
        "redirect": "/dashboard.html",
        "requires_totp": False,
        "totp_enroll": False,
    }

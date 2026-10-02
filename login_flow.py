"""Shared login-flow helpers for VooVr.

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
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone

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
    """True for addresses that are not globally routable and therefore can
    never be geolocated: loopback, RFC1918 private ranges, RFC6598 CGNAT
    (100.64.0.0/10 — used by Tailscale and many Indian ISPs), IPv6 (incl.
    ::1) and link-local."""
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
    # RFC6598 shared address space (CGNAT / Tailscale) — not routable.
    if a == 100 and 64 <= b <= 127:
        return True
    # Link-local (APIPA) — never geolocatable.
    if a == 169 and b == 254:
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


def _clean_ch(value) -> str:
    """Normalise a User-Agent Client Hint header value.

    Chromium sends structured-field strings WITH their surrounding quotes —
    e.g. ``Sec-CH-UA-Platform: "Windows"`` arrives as ``'"Windows"'``.
    Downstream matching (api._windows_display_name) expects the bare value,
    so strip whitespace and one pair of surrounding double quotes."""
    v = (value or "").strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        v = v[1:-1].strip()
    return v


def _country_name(code):
    """Map 2-letter country code to full name using pycountry if available,
    falling back to the code itself.
    """
    if not code:
        return code
    try:
        import pycountry

        country = pycountry.countries.get(alpha_2=code.strip().upper())
        if country and country.name:
            return country.name
    except Exception:
        pass
    return code


def _lookup_location(ip) -> dict:
    """Best-effort approximate geo lookup (ipinfo.io). Never raises."""
    if not ip or _is_private_ip(ip):
        return None
    key = os.environ.get("IP_API_KEY")
    if not key:
        return None  # no key configured: skip location, never block login
    try:
        resp = requests.get(
            f"https://ipinfo.io/{ip}/json",
            params={"token": key},
            timeout=2,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not isinstance(data, dict) or data.get("bogon"):
            return None

        def _clean(v):
            v = (v or "").strip()
            return v or None

        location = {
            "city": _clean(data.get("city")),
            "region": _clean(data.get("region")),
            "country": _country_name(_clean(data.get("country"))),
        }
        return location if any(location.values()) else None
    except Exception:
        return None


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
            "ch_platform": _clean_ch(request.headers.get("Sec-CH-UA-Platform")),
            "ch_platform_version": _clean_ch(
                request.headers.get("Sec-CH-UA-Platform-Version")
            ),
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


def _device_fingerprint(device) -> str:
    return "|".join(
        (device.get(key) or "Unknown").strip().casefold()
        for key in ("device_type", "browser", "os")
    )


def _claim_signin_alert_rate_slot(db, user_id, now) -> bool:
    cutoff = now - timedelta(hours=24)
    result = db.users.update_one(
        {
            "_id": user_id,
            "$expr": {
                "$lt": [
                    {
                        "$size": {
                            "$filter": {
                                "input": {"$ifNull": ["$new_signin_alert_times", []]},
                                "as": "sent_at",
                                "cond": {"$gt": ["$$sent_at", cutoff]},
                            }
                        }
                    },
                    3,
                ]
            },
        },
        {
            "$push": {
                "new_signin_alert_times": {"$each": [now], "$slice": -3}
            }
        },
    )
    return getattr(result, "modified_count", 0) == 1


def _maybe_send_new_signin_alert(db, session_token: str, location):
    from api import _device_label, _parse_device
    from email_service import send_new_signin_alert
    from field_encryption import decrypt_fields

    session_hash = _hash_session_token(session_token)
    current = db.active_sessions.find_one({"session_token": session_hash})
    if not current:
        return

    user_id = current.get("user_id")
    user = db.users.find_one({"_id": user_id})
    if not user:
        return

    previous_sessions = list(
        db.active_sessions.find(
            {"user_id": user_id, "session_token": {"$ne": session_hash}}
        )
    )
    current_device = _parse_device(
        current.get("user_agent", ""),
        current.get("ch_platform"),
        current.get("ch_platform_version"),
    )
    fingerprint = _device_fingerprint(current_device)
    known_devices = list(user.get("known_devices") or [])
    known_countries = list(user.get("known_countries") or [])

    for previous in previous_sessions:
        device = _parse_device(
            previous.get("user_agent", ""),
            previous.get("ch_platform"),
            previous.get("ch_platform_version"),
        )
        known_devices.append(_device_fingerprint(device))
        previous_country = (previous.get("location") or {}).get("country")
        if previous_country:
            known_countries.append(previous_country)

    country = location.get("country")
    has_history = bool(known_devices or known_countries or previous_sessions)
    known_device_keys = {value.casefold() for value in known_devices if value}
    known_country_keys = {value.casefold() for value in known_countries if value}
    is_new_device = fingerprint not in known_device_keys
    is_new_country = bool(country and country.casefold() not in known_country_keys)

    next_devices = list(dict.fromkeys(known_devices + [fingerprint]))[-20:]
    next_countries = list(dict.fromkeys(known_countries + ([country] if country else [])))[-20:]
    db.users.update_one(
        {"_id": user_id},
        {"$set": {"known_devices": next_devices, "known_countries": next_countries}},
    )

    if not has_history or not (is_new_device or is_new_country):
        return

    claim = db.active_sessions.update_one(
        {
            "session_token": session_hash,
            "new_signin_alert_claimed": {"$ne": True},
        },
        {"$set": {"new_signin_alert_claimed": True}},
    )
    if getattr(claim, "modified_count", 0) != 1:
        return

    now = datetime.now(timezone.utc)
    if not _claim_signin_alert_rate_slot(db, user_id, now):
        return

    pii = decrypt_fields(user.get("encrypted"), user.get("wrapped_dek", ""))
    email = pii.get("email")
    if not email:
        return
    name = (pii.get("name") or "").strip()
    first_name = name.split()[0] if name else "there"
    location_text = ", ".join(
        value for value in (location.get("city"), location.get("region"), country)
        if value
    )
    if not send_new_signin_alert(
        email, first_name, _device_label(current_device), location_text, now
    ):
        return

    from audit_log import ACTION_SESSION_NEW_SIGNIN_ALERT_SENT, log_audit_event

    log_audit_event(
        db,
        user.get("org_id"),
        user_id,
        name,
        ACTION_SESSION_NEW_SIGNIN_ALERT_SENT,
        target_type="session",
        target_id=str(current.get("_id", "")),
        target_label=_device_label(current_device),
        meta={"country": country},
    )


def _attach_location_async(db, session_token: str, ip: str):
    """Best-effort background geo lookup. Never raises, never blocks login."""
    try:
        location = _lookup_location(ip)
        if location:
            db.active_sessions.update_one(
                {"session_token": _hash_session_token(session_token)},
                {"$set": {"location": location}},
            )
            _maybe_send_new_signin_alert(db, session_token, location)
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

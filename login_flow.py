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

import threading
import uuid
from datetime import datetime, timezone

import requests
from bson import ObjectId
from flask import request, session


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


def _lookup_location(ip) -> dict:
    """Best-effort geo lookup for a public IP. Returns
    {"city", "region", "country"} or None. Never raises — a geolocation
    failure or timeout must not block login (2s cap, runs once per login).
    """
    if _is_private_ip(ip):
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
    return {
        "city": data.get("city"),
        "region": data.get("regionName"),
        "country": data.get("country"),
    }


def _record_active_session(db, user_id: ObjectId):
    """Track this login as an active session so the settings page can list it
    and let the user revoke access.  Storing the token in the Flask session is
    what lets _require_auth validate later requests.

    The geo lookup is run in a background thread so it never blocks the login
    redirect (free-tier backends can add a 2s penalty otherwise).
    """
    now = datetime.now(timezone.utc)
    session_token = str(uuid.uuid4())
    ip = request.remote_addr or ""
    session["session_token"] = session_token
    db.active_sessions.insert_one(
        {
            "user_id": ObjectId(user_id),
            "session_token": session_token,
            "user_agent": request.headers.get("User-Agent", ""),
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
                {"session_token": session_token},
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

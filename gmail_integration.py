"""Gmail Integration — OAuth connect/disconnect and status for the current user/org."""

import json
import logging
import os
import secrets
import urllib.parse
from datetime import datetime, timezone

import requests
from bson import ObjectId
from flask import Blueprint, jsonify, redirect, request, session

from audit_log import ACTION_GMAIL_CONNECT, ACTION_GMAIL_DISCONNECT, log_audit_event
from blind_index import blind_index
from employees import _require_auth
from extensions import get_db
from field_encryption import decrypt_fields, encrypt_fields

logger = logging.getLogger(__name__)

gmail_bp = Blueprint("gmail", __name__)

GMAIL_SCOPES = "https://www.googleapis.com/auth/gmail.send openid email"
GMAIL_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GMAIL_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GMAIL_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


def _get_site_base() -> str:
    """Get the site base URL for redirect URIs."""
    base = (
        os.environ.get("CLIENT_URL")
        or os.environ.get("SITE_URL")
        or ""
    ).strip().rstrip("/")
    return base


def _get_redirect_uri() -> str:
    """Get the Gmail OAuth redirect URI from env or derive from site base."""
    redirect_uri = os.environ.get("GMAIL_REDIRECT_URI")
    if redirect_uri:
        return redirect_uri.strip()
    base = _get_site_base()
    if base:
        return f"{base}/api/integrations/gmail/callback"
    return ""


def _require_user_and_org():
    """Require authenticated user and return (user_id, org_id, db, user_doc)."""
    org_id = _require_auth()
    if not org_id:
        return None, None, None, None
    user_id = session.get("user_id")
    if not user_id:
        return None, None, None, None
    db = get_db()
    try:
        user = db.users.find_one({"_id": ObjectId(user_id), "org_id": ObjectId(org_id)})
    except Exception:
        return None, None, None, None
    if not user:
        return None, None, None, None
    return user_id, org_id, db, user


def _build_auth_url(mode: str, user_email: str, state: str) -> str:
    """Build the Google OAuth authorization URL."""
    redirect_uri = _get_redirect_uri()
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": GMAIL_SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }

    if mode == "login" and user_email:
        params["login_hint"] = user_email
        params["prompt"] = "consent"
    elif mode == "other":
        params["prompt"] = "select_account consent"

    return f"{GMAIL_AUTH_URL}?{urllib.parse.urlencode(params)}"


def _exchange_code_for_tokens(code: str) -> dict | None:
    """Exchange authorization code for tokens at Google's token endpoint."""
    redirect_uri = _get_redirect_uri()
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")

    if not client_id or not client_secret:
        logger.error("gmail_oauth=missing_credentials")
        return None

    data = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }

    try:
        resp = requests.post(GMAIL_TOKEN_URL, data=data, timeout=10)
    except requests.RequestException as exc:
        logger.error("gmail_oauth=token_request_failed error=%s", exc)
        return None

    if not resp.ok:
        logger.error(
            "gmail_oauth=token_exchange_failed status=%s body=%s",
            resp.status_code,
            resp.text[:300],
        )
        return None

    return resp.json()


def _get_userinfo(access_token: str) -> dict | None:
    """Fetch user info (email, email_verified) from Google."""
    try:
        resp = requests.get(
            GMAIL_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
    except requests.RequestException as exc:
        logger.error("gmail_oauth=userinfo_failed error=%s", exc)
        return None

    if not resp.ok:
        logger.error(
            "gmail_oauth=userinfo_failed status=%s body=%s",
            resp.status_code,
            resp.text[:300],
        )
        return None

    return resp.json()


def _get_connected_email(user) -> str | None:
    """Get the currently connected Gmail address for a user."""
    gmail = user.get("gmail") or {}
    if gmail.get("status") != "connected":
        return None
    encrypted = gmail.get("encrypted") or {}
    wrapped_dek = gmail.get("wrapped_dek", "")
    if not encrypted or not wrapped_dek:
        return None
    try:
        pii = decrypt_fields(encrypted, wrapped_dek)
        return pii.get("email")
    except Exception:
        return None


def _mask_email(email: str) -> str:
    """Mask email for display (e.g., john.doe@example.com -> j***@example.com)."""
    if not email or "@" not in email:
        return email or ""
    local, domain = email.split("@", 1)
    masked_local = (local[0] if local else "") + "***"
    return f"{masked_local}@{domain}"


@gmail_bp.route("/connect")
def connect():
    """Initiate Gmail OAuth flow."""
    user_id, org_id, db, user = _require_user_and_org()
    if not user_id:
        return jsonify({"error": "not_authenticated"}), 401

    mode = (request.args.get("mode") or "login").lower()
    if mode not in ("login", "other"):
        mode = "login"

    user_email = ""
    if mode == "login":
        try:
            pii = decrypt_fields(user.get("encrypted"), user.get("wrapped_dek", ""))
            user_email = pii.get("email", "")
        except Exception:
            # If decryption fails, fall back to "other" mode (no login_hint)
            mode = "other"
            user_email = ""

    state = secrets.token_urlsafe(32)
    session["gmail_oauth_state"] = state
    session["gmail_oauth_mode"] = mode

    auth_url = _build_auth_url(mode, user_email, state)
    return redirect(auth_url)


@gmail_bp.route("/callback")
def callback():
    """Handle Google OAuth callback."""
    user_id, org_id, db, user = _require_user_and_org()
    if not user_id:
        return redirect("/signin?redirect=/settings?tab=integrations")

    state = request.args.get("state", "")
    stored_state = session.pop("gmail_oauth_state", None)
    # read mode before popping
    oauth_mode = session.pop("gmail_oauth_mode", "login")

    if not stored_state or state != stored_state:
        logger.warning("gmail_oauth=invalid_state user_id=%s", user_id)
        return redirect("/settings?tab=integrations&gmail=error")

    code = request.args.get("code", "")
    if not code:
        logger.warning("gmail_oauth=no_code user_id=%s", user_id)
        return redirect("/settings?tab=integrations&gmail=error")

    tokens = _exchange_code_for_tokens(code)
    if not tokens:
        return redirect("/settings?tab=integrations&gmail=error")

    refresh_token = tokens.get("refresh_token")
    access_token = tokens.get("access_token")
    id_token = tokens.get("id_token")

    if not refresh_token:
        logger.warning("gmail_oauth=no_refresh_token user_id=%s", user_id)
        return redirect("/settings?tab=integrations&gmail=error")

    granted_scopes = tokens.get("scope", "").split()
    if "https://www.googleapis.com/auth/gmail.send" not in granted_scopes:
        logger.warning("gmail_oauth=missing_scope user_id=%s scopes=%s", user_id, granted_scopes)
        return redirect("/settings?tab=integrations&gmail=error")

    userinfo = _get_userinfo(access_token)
    if not userinfo:
        return redirect("/settings?tab=integrations&gmail=error")

    email = (userinfo.get("email") or "").strip().lower()
    email_verified = userinfo.get("email_verified", False)

    if not email or not email_verified:
        logger.warning("gmail_oauth=unverified_email user_id=%s email=%s verified=%s", user_id, email, email_verified)
        return redirect("/settings?tab=integrations&gmail=error")

    email_hash = blind_index(email)

    encrypted_fields, wrapped_dek = encrypt_fields({
        "refresh_token": refresh_token,
        "email": email,
    })

    now = datetime.now(timezone.utc)
    update_doc = {
        "gmail.status": "connected",
        "gmail.encrypted": encrypted_fields,
        "gmail.wrapped_dek": wrapped_dek,
        "gmail.email_hash": email_hash,
        "gmail.connected_at": now,
        "gmail.last_ok_at": now,
        "gmail.last_error": None,
    }

    db.users.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": update_doc}
    )

    log_audit_event(
        db, org_id, user_id, session.get("user_name") or "",
        ACTION_GMAIL_CONNECT,
        target_type="gmail_integration",
        target_id=user_id,
        target_label=_mask_email(email),
        meta={"mode": oauth_mode},
    )

    logger.info("gmail_oauth=connected user_id=%s email=%s", user_id, _mask_email(email))
    return redirect("/settings?tab=integrations&gmail=connected")


@gmail_bp.route("/status")
def status():
    """Return Gmail connection status for the current user."""
    user_id, org_id, db, user = _require_user_and_org()
    if not user_id:
        return jsonify({"error": "not_authenticated"}), 401

    gmail = user.get("gmail") or {}
    connected = gmail.get("status") == "connected"
    email = _get_connected_email(user) if connected else None

    return jsonify({
        "connected": connected,
        "email": _mask_email(email) if email else None,
        "status": gmail.get("status", "not_connected"),
        "connected_at": gmail.get("connected_at").isoformat() if gmail.get("connected_at") else None,
    })


@gmail_bp.route("/disconnect", methods=["POST"])
def disconnect():
    """Disconnect Gmail account (revoke token and clear stored data)."""
    user_id, org_id, db, user = _require_user_and_org()
    if not user_id:
        return jsonify({"error": "not_authenticated"}), 401

    gmail = user.get("gmail") or {}
    refresh_token = None
    email = None

    if gmail.get("status") == "connected":
        encrypted = gmail.get("encrypted") or {}
        wrapped_dek = gmail.get("wrapped_dek", "")
        if encrypted and wrapped_dek:
            try:
                pii = decrypt_fields(encrypted, wrapped_dek)
                refresh_token = pii.get("refresh_token")
                email = pii.get("email")
            except Exception:
                logger.warning("gmail_disconnect=decrypt_failed user_id=%s", user_id)

    if refresh_token:
        try:
            requests.post(
                GMAIL_REVOKE_URL,
                params={"token": refresh_token},
                timeout=10,
            )
        except Exception:
            logger.warning("gmail_disconnect=revoke_failed user_id=%s", user_id)

    db.users.update_one(
        {"_id": ObjectId(user_id)},
        {"$unset": {"gmail": ""}}
    )

    log_audit_event(
        db, org_id, user_id, session.get("user_name") or "",
        ACTION_GMAIL_DISCONNECT,
        target_type="gmail_integration",
        target_id=user_id,
        target_label=_mask_email(email) if email else "unknown",
    )

    logger.info("gmail_oauth=disconnected user_id=%s email=%s", user_id, _mask_email(email) if email else "unknown")
    return jsonify({"ok": True})
"""Gmail API send function for meeting reminders."""

import base64
import logging
import os
import time
from datetime import datetime, timezone
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

import requests

from field_encryption import decrypt_fields

logger = logging.getLogger(__name__)

GMAIL_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"

_access_token_cache = {}


def _get_access_token(user) -> str | None:
    """Get a valid access token, refreshing if needed. Cached in memory for ~50 min."""
    gmail = user.get("gmail") or {}
    if gmail.get("status") != "connected":
        return None

    encrypted = gmail.get("encrypted") or {}
    wrapped_dek = gmail.get("wrapped_dek", "")
    if not encrypted or not wrapped_dek:
        return None

    try:
        pii = decrypt_fields(encrypted, wrapped_dek)
    except Exception as exc:
        logger.warning("gmail_send=decrypt_failed user_id=%s error=%s", user.get("_id"), exc)
        return None

    refresh_token = pii.get("refresh_token")
    if not refresh_token:
        return None

    now = time.time()
    cache_key = str(user["_id"])
    cached = _access_token_cache.get(cache_key)
    if cached and cached["expires_at"] > now + 60:
        return cached["access_token"]

    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")

    if not client_id or not client_secret:
        logger.error("gmail_send=missing_credentials")
        return None

    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }

    try:
        resp = requests.post(GMAIL_TOKEN_URL, data=data, timeout=10)
    except requests.RequestException as exc:
        logger.error("gmail_send=refresh_failed user_id=%s error=%s", user.get("_id"), exc)
        return None

    if not resp.ok:
        logger.error(
            "gmail_send=refresh_failed user_id=%s status=%s body=%s",
            user.get("_id"), resp.status_code, resp.text[:300]
        )
        # Only mark reauth_required for invalid_grant errors
        try:
            err_json = resp.json()
            if err_json.get("error") == "invalid_grant":
                _mark_reauth_required(user)
        except Exception:
            pass
        return None

    tokens = resp.json()
    access_token = tokens.get("access_token")
    expires_in = tokens.get("expires_in", 3600)

    if not access_token:
        logger.warning("gmail_send=no_access_token_in_response user_id=%s", user.get("_id"))
        return None

    _access_token_cache[cache_key] = {
        "access_token": access_token,
        "expires_at": now + expires_in - 300,
    }

    return access_token


def _mark_reauth_required(user):
    """Mark the user's Gmail integration as needing re-authentication."""
    try:
        from extensions import get_db
        from bson import ObjectId
        db = get_db()
        db.users.update_one(
            {"_id": ObjectId(user["_id"])},
            {"$set": {
                "gmail.status": "reauth_required",
                "gmail.last_error": "invalid_grant",
                "gmail.last_error_at": datetime.now(timezone.utc),
            }}
        )
    except Exception as exc:
        logger.warning("gmail_send=mark_reauth_failed user_id=%s error=%s", user.get("_id"), exc)


def _build_mime_message(
    to_email: str,
    subject: str,
    html: str,
    text: str | None = None,
    from_email: str | None = None,
    from_name: str = "VooVr",
) -> str:
    """Build a MIME message and return base64url encoded raw message."""
    msg = MIMEMultipart("alternative")
    msg["To"] = to_email
    msg["Subject"] = subject
    if from_email is not None:
        display_name = from_name if from_name.isascii() else Header(from_name, "utf-8").encode()
        msg["From"] = formataddr((display_name, from_email))

    if text:
        msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    return raw


def send_html(user_doc, subject: str, html: str, text: str | None = None) -> bool:
    """Send an HTML email via the user's connected Gmail account.

    Returns True on success, False on failure.
    On invalid_grant/401/403, marks the user's Gmail as reauth_required.
    """
    access_token = _get_access_token(user_doc)
    if not access_token:
        return False

    gmail = user_doc.get("gmail") or {}
    encrypted = gmail.get("encrypted") or {}
    wrapped_dek = gmail.get("wrapped_dek", "")
    to_email = None
    from_email = None
    if encrypted and wrapped_dek:
        try:
            pii = decrypt_fields(encrypted, wrapped_dek)
            from_email = pii.get("email")
            to_email = from_email
        except Exception:
            pass

    if not to_email:
        logger.warning("gmail_send=no_recipient_email user_id=%s", user_doc.get("_id"))
        return False

    raw_message = _build_mime_message(
        to_email,
        subject,
        html,
        text,
        from_email=from_email,
        from_name=os.environ.get("GMAIL_FROM_NAME", "VooVr"),
    )

    try:
        resp = requests.post(
            GMAIL_SEND_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={"raw": raw_message},
            timeout=10,
        )
    except requests.RequestException as exc:
        logger.error("gmail_send=network_error user_id=%s error=%s", user_doc.get("_id"), exc)
        return False

    # Determine if we should mark reauth_required
    should_reauth = False
    if resp.status_code == 401:
        should_reauth = True
    elif resp.status_code == 403:
        try:
            err_json = resp.json()
            error_info = err_json.get("error", {})
            # Google API error format: {"error": {"code": 403, "message": "...", "errors": [{"reason": "rateLimitExceeded", ...}]}}
            reasons = set()
            for err in error_info.get("errors", []):
                reason = err.get("reason")
                if reason:
                    reasons.add(reason)
            # Reauth only for auth/permission errors, not quota/rate limits
            if not reasons:
                # No specific reason, treat as auth error
                should_reauth = True
            else:
                auth_reasons = {"insufficientPermissions", "authError", "forbidden", "invalidCredentials"}
                if reasons & auth_reasons:
                    should_reauth = True
                # else quota/rate limit etc -> do not reauth
        except Exception:
            # If cannot parse, be conservative and treat as auth error
            should_reauth = True
    if should_reauth:
        logger.warning("gmail_send=auth_error user_id=%s status=%s", user_doc.get("_id"), resp.status_code)
        _mark_reauth_required(user_doc)
        return False

    if not resp.ok:
        logger.error(
            "gmail_send=api_error user_id=%s status=%s body=%s",
            user_doc.get("_id"), resp.status_code, resp.text[:300]
        )
        return False

    logger.info("gmail_send=sent user_id=%s subject_len=%d", user_doc.get("_id"), len(subject))
    return True
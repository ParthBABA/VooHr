"""Outbound email for OTP delivery via the Brevo Transactional Email API.

Configuration is read from the environment:

  BREVO_API_KEY      — Brevo v3 API key (required; starts with "xkeysib-")
  BREVO_SENDER_EMAIL — verified Brevo sender address (required)
  BREVO_SENDER_NAME  — display name used as the email sender (default "VooHr")

Emails are sent with the existing `requests` dependency to
POST https://api.brevo.com/v3/smtp/email (no extra SDK).

send_otp_email never raises — it logs and returns False so route handlers can
respond cleanly. Every failure is logged server-side with a `email_failed=`
category and the Brevo HTTP status + response body so the root cause is
identifiable in the logs. The API key is never logged.
"""

import logging
import os
import re

import requests

logger = logging.getLogger(__name__)

BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"
OTP_SUBJECT = "Your VooHr verification code"

# Bare-address check (rejects formats like "Name<email@example.com>").
_SENDER_RE = re.compile(r"^[^<>\s]+@[^<>\s]+\.[^<>\s]+$")


def _email_footer() -> str:
    return (
        "<hr style=\"border:none;border-top:1px solid #333;margin:24px 0;\">"
        "<p style=\"font-size:0.75rem;color:#888;\">"
        "This email was sent because you have an account with HR Copilot. "
        "<a href=\"https://voovrhr.com/privacy-policy\" style=\"color:#aaa;\">View our Privacy Policy</a> | "
        "<a href=\"https://voovrhr.com/terms-of-service\" style=\"color:#aaa;\">Terms of Service</a>"
        "</p>"
        "<p style=\"font-size:0.75rem;color:#888;\">"
        "Questions? Contact us at <a href=\"mailto:voovrhr@gmail.com\" style=\"color:#aaa;\">voovrhr@gmail.com</a>"
        "</p>"
    )

def _otp_html(otp: str) -> str:
    return (
        "<p>Use the following code to verify your email:</p>"
        f"<h2 style=\"letter-spacing:4px\">{otp}</h2>"
        "<p>This code expires in 10 minutes. If you didn't request it, "
        "you can safely ignore this email.</p>"
        + _email_footer()
    )


def _brevo_error_message(resp) -> str:
    """Best-effort extract of Brevo's human-readable error detail. Brevo never
    echoes the API key in the response body, so this is safe to log."""
    try:
        data = resp.json()
        message = data.get("message")
        code = data.get("code")
        if message:
            return f"message={message} code={code}"
    except Exception:
        pass
    return (resp.text or "")[:300]


def _send_via_brevo(to_email: str, otp: str) -> bool:
    api_key = os.environ.get("BREVO_API_KEY", "")
    sender_email = os.environ.get("BREVO_SENDER_EMAIL", "")
    sender_name = os.environ.get("BREVO_SENDER_NAME", "VooHr")

    if not api_key or not sender_email:
        logger.error(
            "email_failed=missing_config recipient=%s api_key_set=%s sender_email_set=%s",
            to_email,
            bool(api_key),
            bool(sender_email),
        )
        return False

    if not _SENDER_RE.match(sender_email):
        logger.error(
            "email_failed=invalid_sender_format recipient=%s sender=%s "
            "(expected a bare address like user@example.com)",
            to_email,
            sender_email,
        )
        return False

    payload = {
        "sender": {"email": sender_email, "name": sender_name},
        "to": [{"email": to_email}],
        "subject": OTP_SUBJECT,
        "htmlContent": _otp_html(otp),
    }

    try:
        resp = requests.post(
            BREVO_API_URL,
            headers={
                "api-key": api_key,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=15,
        )
    except requests.Timeout:
        logger.error(
            "email_failed=timeout recipient=%s url=%s timeout=15",
            to_email,
            BREVO_API_URL,
        )
        return False
    except requests.RequestException as exc:
        logger.error(
            "email_failed=network recipient=%s url=%s error=%s",
            to_email,
            BREVO_API_URL,
            exc,
        )
        return False

    if resp.status_code == 401:
        logger.error(
            "email_failed=invalid_api_key status=401 recipient=%s body=%s",
            to_email,
            _brevo_error_message(resp),
        )
        return False
    if resp.status_code == 403:
        logger.error(
            "email_failed=unauthorized_sender status=403 recipient=%s sender=%s body=%s",
            to_email,
            sender_email,
            _brevo_error_message(resp),
        )
        return False
    if resp.status_code == 429:
        logger.warning(
            "email_failed=rate_limited status=429 recipient=%s body=%s",
            to_email,
            _brevo_error_message(resp),
        )
        return False
    if resp.status_code >= 500:
        logger.error(
            "email_failed=brevo_server_error status=%s recipient=%s body=%s",
            resp.status_code,
            to_email,
            _brevo_error_message(resp),
        )
        return False
    if not resp.ok:
        logger.error(
            "email_failed=api_error status=%s recipient=%s body=%s",
            resp.status_code,
            to_email,
            _brevo_error_message(resp),
        )
        return False

    try:
        message_id = resp.json().get("messageId")
    except Exception:
        message_id = "n/a"
    logger.info(
        "email_sent provider=brevo status=%s recipient=%s message_id=%s",
        resp.status_code,
        to_email,
        message_id,
    )
    return True


def send_otp_email(to_email: str, otp: str) -> bool:
    """Send an OTP email. Returns True on success, False on any failure."""
    try:
        return _send_via_brevo(to_email, otp)
    except Exception:
        logger.exception("email_failed=unexpected_exception recipient=%s", to_email)
        return False

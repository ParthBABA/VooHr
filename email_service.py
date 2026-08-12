"""Outbound email for OTP delivery via the Brevo Transactional Email API.

Configuration is read from the environment:

  BREVO_API_KEY      — Brevo v3 API key (required; starts with "xkeysib-")
  BREVO_SENDER_EMAIL — verified Brevo sender address (required)
  BREVO_SENDER_NAME  — display name used as the email sender (default "VooHr")

Emails are sent with the existing `requests` dependency to
POST https://api.brevo.com/v3/smtp/email (no extra SDK).

send_otp_email never raises — it logs and returns False so route handlers can
respond cleanly.
"""

import logging
import os

import requests

logger = logging.getLogger(__name__)

BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"
OTP_SUBJECT = "Your VooHr verification code"


def _otp_html(otp: str) -> str:
    return (
        "<p>Use the following code to verify your email:</p>"
        f"<h2 style=\"letter-spacing:4px\">{otp}</h2>"
        "<p>This code expires in 10 minutes. If you didn't request it, "
        "you can safely ignore this email.</p>"
    )


def _send_via_brevo(to_email: str, otp: str) -> bool:
    api_key = os.environ.get("BREVO_API_KEY", "")
    sender_email = os.environ.get("BREVO_SENDER_EMAIL", "")
    sender_name = os.environ.get("BREVO_SENDER_NAME", "VooHr")

    if not api_key or not sender_email:
        logger.error(
            "Brevo not configured: missing BREVO_API_KEY or BREVO_SENDER_EMAIL "
            "in environment (to=%s)",
            to_email,
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
    except requests.RequestException:
        logger.exception("Brevo request failed for to=%s (timeout/network)", to_email)
        return False

    if resp.status_code == 401:
        logger.error("Brevo rejected API key (401) for to=%s", to_email)
        return False
    if resp.status_code == 403:
        logger.error(
            "Brevo sender %s not verified or account blocked (403) for to=%s",
            sender_email,
            to_email,
        )
        return False
    if resp.status_code == 429:
        logger.warning("Brevo rate limited (429) for to=%s", to_email)
        return False
    if resp.status_code >= 500:
        logger.error("Brevo API error (status %s) for to=%s", resp.status_code, to_email)
        return False
    if not resp.ok:
        logger.warning(
            "Brevo API returned status %s for to=%s: %s",
            resp.status_code,
            to_email,
            resp.text,
        )
        return False

    try:
        message_id = resp.json().get("messageId")
    except Exception:
        message_id = "n/a"
    logger.info("Brevo accepted OTP email to=%s messageId=%s", to_email, message_id)
    return True


def send_otp_email(to_email: str, otp: str) -> bool:
    """Send an OTP email. Returns True on success, False on any failure."""
    try:
        return _send_via_brevo(to_email, otp)
    except Exception:
        logger.exception("Failed to send OTP email to %s", to_email)
        return False

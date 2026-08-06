"""Outbound email for OTP delivery.

Configuration is read from the environment:

  EMAIL_HOST  — either a raw Resend API key (starts with "re_") or an SMTP
                hostname like smtp.resend.com
  EMAIL_PORT  — SMTP port (default 587); only used for the SMTP path
  EMAIL_USER  — SMTP user (default "resend"); only used for the SMTP path
  EMAIL_PASS  — SMTP password/API key; only used for the SMTP path
  EMAIL_FROM  — from address; default "VooHr <onboarding@resend.dev>"

When EMAIL_HOST is a Resend key we call Resend's REST API directly with the
existing `requests` dependency (no extra SDK). Otherwise we fall back to
Python's built-in smtplib over STARTTLS.

send_otp_email never raises — it logs and returns False so route handlers can
respond cleanly.
"""

import logging
import os
import smtplib
from email.mime.text import MIMEText

import requests

logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"
OTP_SUBJECT = "Your VooHr verification code"


def _otp_html(otp: str) -> str:
    return (
        "<p>Use the following code to verify your email:</p>"
        f"<h2 style=\"letter-spacing:4px\">{otp}</h2>"
        "<p>This code expires in 10 minutes. If you didn't request it, "
        "you can safely ignore this email.</p>"
    )


def _send_via_resend(to_email: str, otp: str) -> bool:
    resp = requests.post(
        RESEND_API_URL,
        headers={"Authorization": f"Bearer {os.environ['EMAIL_HOST']}"},
        json={
            "from": os.environ.get("EMAIL_FROM", "VooHr <onboarding@resend.dev>"),
            "to": [to_email],
            "subject": OTP_SUBJECT,
            "html": _otp_html(otp),
        },
        timeout=15,
    )
    if not resp.ok:
        logger.warning("Resend API returned status %s", resp.status_code)
    return resp.ok


def _send_via_smtp(to_email: str, otp: str, host: str) -> bool:
    port = int(os.environ.get("EMAIL_PORT", "587"))
    user = os.environ.get("EMAIL_USER", "resend")
    password = os.environ.get("EMAIL_PASS", "")
    sender = os.environ.get("EMAIL_FROM", "VooHr <onboarding@resend.dev>")

    msg = MIMEText(_otp_html(otp), "html", "utf-8")
    msg["Subject"] = OTP_SUBJECT
    msg["From"] = sender
    msg["To"] = to_email

    with smtplib.SMTP(host, port, timeout=15) as server:
        server.starttls()
        if user:
            server.login(user, password)
        server.send_message(msg)
    return True


def send_otp_email(to_email: str, otp: str) -> bool:
    """Send an OTP email. Returns True on success, False on any failure."""
    host = os.environ.get("EMAIL_HOST", "")
    try:
        if host.startswith("re_"):
            return _send_via_resend(to_email, otp)
        return _send_via_smtp(to_email, otp, host)
    except Exception:
        logger.exception("Failed to send OTP email to %s", to_email)
        return False

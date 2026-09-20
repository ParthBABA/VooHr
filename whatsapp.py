"""WhatsApp Cloud API client for VooVr's WhatsApp intake channel.

Small, dependency-light wrapper around the Meta WhatsApp Business Cloud API:

  * ``send_message``            — outbound plain-text messages (meeting
                                  reminders, intake acknowledgements, and the
                                  one-time verification codes used when a user
                                  links their number in Settings)
  * ``download_media``          — resolve a WhatsApp media ID to its temporary
                                  URL and fetch the raw audio bytes so an
                                  inbound voice note can be transcribed
  * ``verify_webhook_signature`` — HMAC-SHA256 validation of Meta's
                                  ``X-Hub-Signature-256`` header on every
                                  inbound webhook POST

Configuration (see ``.env.example``):
    WHATSAPP_ACCESS_TOKEN      — system-user token for the WhatsApp Business
                                 account
    WHATSAPP_PHONE_NUMBER_ID   — the app's registered phone-number ID (used as
                                 the ``from`` sender for outbound messages)
    WHATSAPP_APP_SECRET        — app secret, only needed for webhook signature
                                 checks
    WHATSAPP_VERIFY_TOKEN      — webhook handshake token, validated by the
                                 routes in ``whatsapp_routes.py``

Every entry point is best-effort: it never raises, logs failures, and returns a
bool/``None`` so callers (reminders.py, whatsapp_routes.py, api.py OTP flow)
can degrade gracefully instead of crashing reminder generation or the webhook
handler.
"""

import hashlib
import hmac
import logging
import os

import requests

logger = logging.getLogger(__name__)

_GRAPH_ENDPOINT = "https://graph.facebook.com"
_API_VERSION = "v20.0"
_REQUEST_TIMEOUT = 15
_MEDIA_TIMEOUT = 60


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def is_configured() -> bool:
    """True when the outbound WhatsApp credentials are present.

    A missing token or phone-number ID means ``send_message`` can only fail,
    so callers (e.g. the Settings OTP endpoint) can answer with a clear
    "not configured" response instead of a confusing send failure.
    """
    return bool(_env("WHATSAPP_ACCESS_TOKEN") and _env("WHATSAPP_PHONE_NUMBER_ID"))


def normalize_phone(number) -> str:
    """Reduce a phone number to its digits (E.164 without a leading ``+``).

    The Cloud API always reports a sender as bare digits (e.g. ``15551234567``)
    while users may store ``+91 90000 00000``. Comparing digit-only forms keeps
    the webhook's ``db.users`` lookup and the OTP flow immune to formatting
    differences.
    """
    if not number:
        return ""
    return "".join(ch for ch in str(number).strip() if ch.isdigit())


def _graph_headers() -> dict:
    return {
        "Authorization": f"Bearer {_env('WHATSAPP_ACCESS_TOKEN')}",
        "Content-Type": "application/json",
    }


def send_message(to_phone: str, text: str) -> bool:
    """Send a plain WhatsApp text message via the Cloud API.

    POSTs to ``graph.facebook.com/v20.0/{WHATSAPP_PHONE_NUMBER_ID}/messages``
    with ``WHATSAPP_ACCESS_TOKEN`` as bearer auth. Returns ``True``/``False``,
    logs every failure, and never raises.
    """
    if not text or not to_phone:
        logger.info("whatsapp_send=skipped reason=missing_fields phone_set=%s", bool(to_phone))
        return False
    if not is_configured():
        logger.info(
            "whatsapp_send=skipped reason=not_configured phone_set=%s",
            bool(to_phone),
        )
        return False

    number_id = _env("WHATSAPP_PHONE_NUMBER_ID")
    url = f"{_GRAPH_ENDPOINT}/{_API_VERSION}/{number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_phone,
        "type": "text",
        "text": {"body": text},
    }
    try:
        resp = requests.post(url, headers=_graph_headers(), json=payload, timeout=_REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        logger.warning("whatsapp_send=failed phone_set=%s error=%s", bool(to_phone), exc)
        return False

    if 200 <= resp.status_code < 300:
        logger.info("whatsapp_send=sent phone_set=%s", bool(to_phone))
        return True

    logger.warning(
        "whatsapp_send=failed phone_set=%s http=%s body=%s",
        bool(to_phone),
        resp.status_code,
        resp.text[:500],
    )
    return False


def download_media(media_id: str) -> bytes | None:
    """Resolve a WhatsApp media ID to its temporary URL and download the bytes.

    First GETs ``graph.facebook.com/v20.0/{media_id}`` (with
    ``WHATSAPP_ACCESS_TOKEN`` as bearer auth) to learn the media's ``url``, then
    GETs that URL with the same bearer token. Returns the raw bytes on success,
    ``None`` on any failure — never raises.
    """
    if not media_id or not _env("WHATSAPP_ACCESS_TOKEN"):
        logger.debug("whatsapp_media=skipped reason=missing_id_or_token")
        return None

    try:
        resp = requests.get(
            f"{_GRAPH_ENDPOINT}/{_API_VERSION}/{media_id}",
            headers=_graph_headers(),
            timeout=_REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("whatsapp_media=meta_failed media_id=%s error=%s", media_id, exc)
        return None

    if 200 <= resp.status_code < 300:
        url = (resp.json() or {}).get("url")
    else:
        logger.warning(
            "whatsapp_media=meta_failed media_id=%s http=%s",
            media_id,
            resp.status_code,
        )
        return None
    if not url:
        logger.warning("whatsapp_media=meta_failed media_id=%s reason=no_url", media_id)
        return None

    try:
        audio = requests.get(url, headers=_graph_headers(), timeout=_MEDIA_TIMEOUT)
    except requests.RequestException as exc:
        logger.warning("whatsapp_media=download_failed media_id=%s error=%s", media_id, exc)
        return None

    if 200 <= audio.status_code < 300:
        logger.info(
            "whatsapp_media=downloaded media_id=%s bytes=%d",
            media_id,
            len(audio.content),
        )
        return audio.content

    logger.warning(
        "whatsapp_media=download_failed media_id=%s http=%s",
        media_id,
        audio.status_code,
    )
    return None


def verify_webhook_signature(payload_bytes: bytes, signature_header: str) -> bool:
    """Validate Meta's ``X-Hub-Signature-256`` header.

    The header is ``sha256=<hex>`` — the HMAC-SHA256 of the raw request body
    keyed on ``WHATSAPP_APP_SECRET``. Fails closed (returns ``False``) for a
    missing secret/header, a malformed header, or a mismatched digest.
    """
    secret = _env("WHATSAPP_APP_SECRET")
    if not secret or not signature_header or payload_bytes is None:
        return False
    if isinstance(payload_bytes, str):
        payload_bytes = payload_bytes.encode()
    header = signature_header.strip()
    if not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header[len("sha256="):], expected)
"""WhatsApp Cloud API inbound webhook + dictation intake channel.

Meta sends an HTTP POST to ``/api/whatsapp/webhook`` whenever someone messages
the app's WhatsApp number. VooVr turns that message into a dictation session
for the linked user's organization:

  * text   -> used directly as the transcript (``recording_device
              'whatsapp_text'``, STT skipped)
  * audio  -> downloaded and transcribed via the configured STT provider
              (``recording_device 'whatsapp'``)

Flow (mirrors jobs.py's background translation/TTS jobs):

  1. verify the ``X-Hub-Signature-256`` header on the raw request body
  2. rate-limit per sender phone number (per hour, DB-backed)
  3. look up the sender in ``db.users`` by ``phone_number``
  4. acknowledge the sender immediately ("Got it — transcribing now...")
  5. do the heavy work (download + STT + session insert) on a daemon thread so
     the webhook answers Meta's POST quickly
  6. on completion, create a "session_ready" in-app notification (same shape
     as translation_ready / audio_ready) and a completion WhatsApp message

The GET endpoint handles Meta's subscription-verification handshake, comparing
``hub.verify_token`` against ``WHATSAPP_VERIFY_TOKEN``.
"""

import hmac
import logging
import os
import threading
from datetime import datetime, timezone

from flask import Blueprint, Response, jsonify, request

from extensions import check_rate_limit, get_db, record_rate_limit_event
from providers import get_stt_provider
from sessions import MAX_RAW_TEXT_BYTES, _insert_session_doc
from whatsapp import download_media, normalize_phone, send_message, verify_webhook_signature

whatsapp_bp = Blueprint("whatsapp", __name__)
logger = logging.getLogger(__name__)

# ── Rate-limit constants for the inbound webhook ───────────────────────
# Per-phone sliding window so a single borrowed/linked number cannot flood the
# STT provider or session store.
_WHATSAPP_INBOUND_MAX = 50      # messages per phone per window
_WHATSAPP_INBOUND_WINDOW = 3600 # 1 hour

_ACK_TEXT = "Got it — transcribing now..."

_VERIFY_TOKEN_ENV = "WHATSAPP_VERIFY_TOKEN"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _intake_page_url() -> str:
    """Deep link to the notifications page (Activities tab) where the
    completed session shows up."""
    base = (
        os.environ.get("CLIENT_URL") or os.environ.get("SITE_URL") or ""
    ).strip().rstrip("/")
    return f"{base}/notifications" if base else "/notifications"


@whatsapp_bp.route("/whatsapp/webhook", methods=["GET"])
def webhook_verify():
    """Meta's subscription handshake. Returns the challenge as plain text on
    a matching verify token; otherwise a 403."""
    mode = request.args.get("hub.mode", "")
    token = request.args.get("hub.verify_token", "")
    challenge = request.args.get("hub.challenge", "") or ""
    expected = os.environ.get(_VERIFY_TOKEN_ENV, "").strip()
    if mode == "subscribe" and expected and hmac.compare_digest(token, expected):
        return Response(challenge, status=200, mimetype="text/plain")
    logger.warning(
        "whatsapp_webhook=verify_failed mode=%s token_provided=%s expected_set=%s",
        mode or "missing",
        bool(token),
        bool(expected),
    )
    return Response("Verification failed", status=403, mimetype="text/plain")


@whatsapp_bp.route("/whatsapp/webhook", methods=["POST"])
def webhook_receive():
    """Receive inbound message events from Meta."""
    # Parse JSON first so Werkzeug caches the body; request.get_data() then
    # returns the exact raw bytes needed for the X-Hub-Signature-256 check.
    data = request.get_json(silent=True) or {}
    payload_bytes = request.get_data()
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not verify_webhook_signature(payload_bytes, signature):
        return jsonify({"error": "invalid_signature"}), 403

    if data.get("object") != "whatsapp_business_account":
        return jsonify({"error": "unsupported_object"}), 400

    try:
        db = get_db()
    except Exception:
        logger.exception("whatsapp_webhook=db_unavailable")
        return jsonify({"error": "db_unavailable"}), 503

    for entry in data.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            for msg in value.get("messages") or []:
                _handle_inbound_message(db, msg)

    return jsonify({"status": "ok"}), 200


def _handle_inbound_message(db, msg) -> None:
    """Route a single inbound message: rate-limit, resolve the user, ack, then
    dispatch the heavy work to a background thread."""
    sender = msg.get("from") or ""
    phone = normalize_phone(sender)
    if not phone:
        logger.debug("whatsapp_inbound=skipped reason=no_sender")
        return

    key = f"whatsapp_inbound:{phone}"
    allowed, retry_after = check_rate_limit(db, key, _WHATSAPP_INBOUND_MAX, _WHATSAPP_INBOUND_WINDOW)
    if not allowed:
        send_message(
            phone,
            "You've reached VooVr's message limit — please try again in a little while.",
        )
        logger.warning("whatsapp_inbound=rate_limited phone_set=True retry_after=%s", retry_after)
        return

    user = db.users.find_one({"phone_number": phone})
    if not user:
        send_message(
            phone,
            "Hi! This number isn't linked to a VooVr account yet. "
            "Open VooVr → Settings → Notifications, verify this WhatsApp "
            "number, and send your dictation again.",
        )
        return

    record_rate_limit_event(db, key, ttl_seconds=_WHATSAPP_INBOUND_WINDOW)

    mtype = msg.get("type")
    send_message(phone, _ACK_TEXT)

    if mtype == "audio":
        media_id = ((msg.get("audio") or {}).get("id") or "").strip()
        mime_type = ((msg.get("audio") or {}).get("mime_type") or "audio/ogg").strip()
        if not media_id:
            return
        stt = get_stt_provider()
        threading.Thread(
            target=_process_voice_note,
            args=(db, user, phone, media_id, mime_type, stt),
            daemon=True,
        ).start()
    elif mtype == "text":
        text = ((msg.get("text") or {}).get("body") or "").strip()
        if not text:
            return
        threading.Thread(
            target=_process_text,
            args=(db, user, phone, text),
            daemon=True,
        ).start()


def _process_voice_note(db, user, phone, media_id, mime_type, stt) -> None:
    """Background: download the voice note, transcribe it, and persist it as a
    dictation session."""
    try:
        audio = download_media(media_id)
        if not audio:
            send_message(phone, "Sorry, I couldn't fetch that voice note. Please try again.")
            return
        raw_text = (stt.transcribe(audio, content_type=mime_type) or "").strip()
        if not raw_text:
            send_message(phone, "I couldn't hear any speech in that voice note. Please try again.")
            return
        session = _insert_session_doc(
            db,
            user.get("org_id"),
            None,
            raw_text=raw_text,
            edited_text=raw_text,
            source="whatsapp_dictation",
            duration_seconds=0,
            recording_device="whatsapp",
            recording_type=mime_type or "audio",
            language="en",
        )
        _finish_intake(db, user, session, phone)
    except Exception:
        logger.exception("whatsapp_intake=audio_failed phone_set=True")
        send_message(phone, "Something went wrong while transcribing that note. Please try again.")


def _process_text(db, user, phone, text) -> None:
    """Background: persist an inbound text message directly as a dictation
    session transcript (no STT needed)."""
    try:
        if len(text.encode("utf-8")) > MAX_RAW_TEXT_BYTES:
            send_message(phone, "That message was too long to save as a dictation. Please send a shorter one.")
            return
        session = _insert_session_doc(
            db,
            user.get("org_id"),
            None,
            raw_text=text,
            edited_text=text,
            source="whatsapp_dictation",
            duration_seconds=0,
            recording_device="whatsapp_text",
            recording_type="text",
            language="en",
        )
        _finish_intake(db, user, session, phone)
    except Exception:
        logger.exception("whatsapp_intake=text_failed phone_set=True")
        send_message(phone, "Something went wrong while saving that message. Please try again.")


def _finish_intake(db, user, session, phone) -> None:
    """Create the session_ready in-app notification and tell the sender."""
    _notify_session_ready(db, user, session)
    send_message(
        phone,
        "Done! Your dictation session is ready on VooVr. "
        f"View it under Activities: {_intake_page_url()}",
    )


def _notify_session_ready(db, user, session) -> None:
    """Insert a "session_ready" notification with the same shape as the
    translation_ready / audio_ready notifications (jobs.py) so it renders as
    an Activity on /notifications. Best effort: a write failure is logged and
    swallowed and never surfaces on the WhatsApp reply the user already got."""
    try:
        org_id = user.get("org_id")
        session_id = session["_id"]
        existing = db.notifications.find_one(
            {
                "org_id": org_id,
                "type": "session_ready",
                "source_session_id": session_id,
            }
        )
        if existing:
            return
        raw = (session.get("transcript") or {}).get("raw") or ""
        preview = " ".join(raw.strip().split())
        if len(preview) > 100:
            preview = preview[:100].rstrip() + "…"
        db.notifications.insert_one(
            {
                "org_id": org_id,
                "type": "session_ready",
                "headline": "WhatsApp dictation ready",
                "summary": f"Your WhatsApp dictation is ready: {preview or 'new session'}.",
                "confidence": 0,
                "employee_id": None,
                "source_session_id": session_id,
                "meeting_id": None,
                "detail_key": f"whatsapp:{session_id}",
                "read": False,
                "created_at": _now(),
            }
        )
    except Exception:
        logger.exception("whatsapp_intake=notification_failed session_set=True")
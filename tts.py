import logging

from flask import Blueprint, Response, current_app, jsonify, request

from employees import _require_auth
from providers import get_llm_provider, get_tts_provider

tts_bp = Blueprint("tts", __name__)
logger = logging.getLogger(__name__)

_MAX_TEXT_CHARS = 50000


@tts_bp.route("/tts/synthesize", methods=["POST"])
def synthesize():
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    language_code = (data.get("language_code") or "").strip()
    translate_flag = bool(data.get("translate", False))
    voice_name = (data.get("voice_name") or "").strip() or None
    voice_tier = (data.get("voice_tier") or "").strip() or None

    # ── Input validation — reject oversized / empty requests before any
    # provider is hit. ──
    if not text:
        return jsonify({"error": "text_required"}), 400
    if len(text) > _MAX_TEXT_CHARS:
        return jsonify({"error": "text_too_long"}), 400
    if not language_code:
        return jsonify({"error": "language_code_required"}), 400

    try:
        # Optional translation before synthesis when the target isn't English.
        if translate_flag and language_code.split("-")[0].lower() != "en":
            llm = get_llm_provider()
            text = llm.translate(text, language_code)

        tts = get_tts_provider()

        # Stream raw linear16 PCM (24000 Hz) back chunk-by-chunk over the
        # WebSocket so the browser can begin playback as soon as the first
        # audio arrives. The frontend (narration-stream.js) wraps each chunk
        # in a WAV header itself and decodes it via Web Audio.
        def generate():
            for chunk in tts.synthesize_stream(
                text, language_code, voice_name=voice_name, voice_tier=voice_tier
            ):
                if chunk:
                    yield chunk

        return Response(generate(), mimetype="audio/wav")
    except Exception as e:
        logger.exception("TTS synthesize failed")
        if current_app.debug:
            return jsonify({"error": "internal_server_error", "detail": str(e)}), 500
        return jsonify({"error": "internal_server_error"}), 500

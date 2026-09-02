import logging

from flask import Blueprint, Response, current_app, jsonify, request

from employees import _require_auth
from providers import get_llm_provider, get_tts_provider

tts_bp = Blueprint("tts", __name__)
logger = logging.getLogger(__name__)

_MAX_TEXT_CHARS = 50000


def _sniff_audio_mimetype(audio: bytes) -> str:
    """Detect the audio container from the leading magic bytes.

    The TTS REST provider returns mp3 for the common single-chunk path but
    WAV (concatenated linear16 PCM) for the multi-chunk long-text path, so
    the response's Content-Type can no longer be hardcoded. The format is
    determined here instead:

    * RIFF..WAVE header           -> ``audio/wav``
    * ID3 tag (``ID3``)           -> ``audio/mpeg``
    * MPEG audio frame sync
      (0xFF 0xFB/0xF3/... 0xEx)   -> ``audio/mpeg``
    * unknown sentinel            -> ``audio/mpeg`` (mp3 is the default path)
    """
    if not audio:
        return "audio/mpeg"
    if audio[:4] == b"RIFF" and audio[8:12] == b"WAVE":
        return "audio/wav"
    if audio[:3] == b"ID3":
        return "audio/mpeg"
    if audio[0] == 0xFF and (audio[1] & 0xE0) == 0xE0:
        return "audio/mpeg"
    return "audio/mpeg"


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

        # The provider returns mp3 (short text) or WAV (long/multi-chunk
        # text), so sniff the actual container from the bytes rather than
        # hardcoding a format and return the correct Content-Type.
        audio = tts.synthesize(
            text, language_code, voice_name=voice_name, voice_tier=voice_tier
        )
        return Response(audio, mimetype=_sniff_audio_mimetype(audio))
    except Exception as e:
        logger.exception("TTS synthesize failed")
        if current_app.debug:
            return jsonify({"error": "internal_server_error", "detail": str(e)}), 500
        return jsonify({"error": "internal_server_error"}), 500

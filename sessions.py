from datetime import datetime, timezone
import io
import logging

from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, jsonify, request, session

from employee_scoring import _status_for
from employees import _require_auth
from extensions import get_db, check_rate_limit, record_rate_limit_event
from providers import get_llm_provider, get_storage_provider, get_stt_provider, get_vision_provider
from providers.llm import LLMTimeoutError

sessions_bp = Blueprint("sessions", __name__)
logger = logging.getLogger(__name__)

# ── Rate-limit constants for expensive authenticated endpoints ─────────
# Per-user sliding-window limits to prevent abuse of LLM / STT / Vision
# calls.  These are intentionally generous for legitimate HR workflows
# but prevent automated flooding.
_ANALYZE_MAX = 15            # max analyze requests per window
_PHRASING_MAX = 15          # max phrasing-review requests per window
_TRANSCRIBE_MAX = 30        # max transcription requests per window
_OCR_MAX = 20               # max image OCR requests per window
_API_RATE_WINDOW = 900      # 15-minute sliding window in seconds


def _check_api_rate_limit(user_id_str: str, endpoint: str, max_events: int) -> tuple[bool, int]:
    """Return (allowed, retry_after) for authenticated API rate limiting.

    Uses a per-user, per-endpoint key so different endpoints have
    independent budgets.
    """
    db = get_db()
    key = f"api_rate:{user_id_str}:{endpoint}"
    allowed, retry_after = check_rate_limit(db, key, max_events, _API_RATE_WINDOW)
    if not allowed:
        return False, retry_after or _API_RATE_WINDOW
    record_rate_limit_event(db, key, ttl_seconds=_API_RATE_WINDOW)
    return True, 0

# Number of recent completed sessions needed before the silent Risk Drift
# Detection check fires. New employees with fewer analyzed syncs are skipped.
DRIFT_WINDOW_SIZE = 3

# Image OCR: only raster formats the vision provider understands, and a
# ~10MB cap so a huge screenshot/photo can't blow up the request buffer.
IMAGE_CONTENT_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/webp"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024

# Audio transcription: Whisper accepts up to 25 MB.  The extra 1 MB headroom
# lets us reject *before* forwarding to the provider while staying safely
# under Whisper's own limit.
MAX_AUDIO_BYTES = 26 * 1024 * 1024  # 26 MB

# Audio MIME types the browser can realistically produce for recording.
# Content-Type on a multipart part is client-controlled, so this is a
# defence-in-depth check, not a security boundary on its own.
AUDIO_CONTENT_TYPES = {
    "audio/webm", "audio/wav", "audio/x-wav", "audio/mpeg",
    "audio/mp3", "audio/ogg", "audio/x-m4a", "audio/mp4",
    "audio/aac", "audio/flac",
}

# ── Session field length limits ────────────────────────────────────────
# Prevent uncontrolled growth of MongoDB documents via oversized text.
MAX_RAW_TEXT_BYTES = 512 * 1024       # 512 KB  (transcription text)
MAX_EDITED_TEXT_BYTES = 512 * 1024    # 512 KB  (user-edited transcript)
MAX_SESSION_AUDIO_BYTES = 2 * 1024 * 1024  # 2 MB  (base64 audio in JSON)
MAX_SESSION_SOURCE_LEN = 100          # characters
MAX_SESSION_LANGUAGE_LEN = 20         # characters

# ── LLM transcript truncation ──────────────────────────────────────────
# DeepSeek / OpenAI models accept large context windows, but sending
# unbounded transcripts wastes tokens and can hit rate-limit / cost
# boundaries.  50 000 chars ≈ 12 000 tokens — comfortably within model
# limits while covering even very long HR conversations.
MAX_LLM_TRANSCRIPT_CHARS = 50_000


def _validate_image_magic_bytes(image_bytes: bytes) -> str | None:
    """Check file magic bytes and return the detected MIME type, or None.

    Uses the first bytes of the file to identify the actual format,
    independent of what the client claims in the Content-Type header.
    Returns the MIME type string on success, or None if the bytes do not
    match any supported image format.
    """
    if len(image_bytes) < 8:
        return None

    # PNG: 89 50 4E 47 0D 0A 1A 0A
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"

    # JPEG: FF D8 FF
    if image_bytes[:3] == b"\xff\xd8\xff":
        return "image/jpeg"

    # WebP: RIFF....WEBP
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"

    # GIF: GIF87a or GIF89a
    if image_bytes[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"

    return None


def _session_to_json(s) -> dict:
    return {
        "id": str(s["_id"]),
        "employee_id": str(s["employee_id"]),
        "source": s.get("source", "voice_dictation"),
        "status": s.get("status", "draft"),
        "language": s.get("language", "en"),
        "recording_device": s.get("recording_device", "browser"),
        "recording_duration": s.get("recording_duration", 0),
        "recording_type": s.get("recording_type", "webm"),
        "audio": s.get("audio"),
        "transcript": s.get("transcript", {"raw": "", "edited": "", "word_count": 0}),
        "analysis": s.get("analysis"),
        "analysis_version": s.get("analysis_version", 0),
        "phrasing_analysis": s.get("phrasing_analysis"),
        "phrasing_analysis_version": s.get("phrasing_analysis_version", 0),
        "last_transcript_update": s["last_transcript_update"].isoformat() if s.get("last_transcript_update") else None,
        "last_analyzed_at": s["last_analyzed_at"].isoformat() if s.get("last_analyzed_at") else None,
        "last_phrasing_analyzed_at": s["last_phrasing_analyzed_at"].isoformat() if s.get("last_phrasing_analyzed_at") else None,
        "created_at": s["created_at"].isoformat() if s.get("created_at") else None,
        "updated_at": s["updated_at"].isoformat() if s.get("updated_at") else None,
    }


@sessions_bp.route("/sessions", methods=["POST"])
def create_session():
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    data = request.get_json(silent=True) or {}
    employee_id = data.get("employee_id")
    raw_text = (data.get("raw_text") or "").strip()
    source = data.get("source", "voice_dictation")
    duration = int(data.get("duration_seconds", 0))
    recording_device = data.get("recording_device", "browser")
    recording_type = data.get("recording_type", "webm")
    language = data.get("language", "en")

    if not employee_id:
        return jsonify({"error": "employee_id_required"}), 400
    if not raw_text:
        return jsonify({"error": "raw_text_required"}), 400

    # ── Field length guards ─────────────────────────────────────────
    if len(raw_text.encode("utf-8")) > MAX_RAW_TEXT_BYTES:
        return jsonify({"error": "raw_text_too_large"}), 413
    edited_raw = data.get("edited_text")
    if edited_raw and len(edited_raw.encode("utf-8")) > MAX_EDITED_TEXT_BYTES:
        return jsonify({"error": "edited_text_too_large"}), 413
    audio_val = data.get("audio")
    if audio_val and isinstance(audio_val, str) and len(audio_val) > MAX_SESSION_AUDIO_BYTES:
        return jsonify({"error": "audio_payload_too_large"}), 413
    if len(str(source)) > MAX_SESSION_SOURCE_LEN:
        return jsonify({"error": "source_too_long"}), 400
    if len(str(language)) > MAX_SESSION_LANGUAGE_LEN:
        return jsonify({"error": "language_too_long"}), 400

    try:
        ObjectId(employee_id)
    except InvalidId:
        return jsonify({"error": "invalid_employee_id"}), 400

    db = get_db()
    emp = db.employees.find_one({"_id": ObjectId(employee_id), "org_id": ObjectId(org_id)})
    if not emp:
        return jsonify({"error": "employee_not_found"}), 404

    now = datetime.now(timezone.utc)
    doc = {
        "org_id": ObjectId(org_id),
        "employee_id": ObjectId(employee_id),
        "source": source,
        "status": "transcribed",
        "language": language,
        "recording_device": recording_device,
        "recording_duration": duration,
        "recording_type": recording_type,
        "audio": data.get("audio"),
        "transcript": {
            "raw": raw_text,
            "edited": data.get("edited_text") or raw_text,
            "word_count": len(raw_text.split()),
        },
        "analysis": None,
        "analysis_version": 0,
        "last_transcript_update": now,
        "last_analyzed_at": None,
        "created_at": now,
        "updated_at": now,
    }

    result = db.sessions.insert_one(doc)
    doc["_id"] = result.inserted_id

    return jsonify(_session_to_json(doc)), 201


@sessions_bp.route("/sessions")
def list_sessions():
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    query = {"org_id": ObjectId(org_id)}

    emp_id = request.args.get("employee_id")
    if emp_id:
        try:
            query["employee_id"] = ObjectId(emp_id)
        except InvalidId:
            return jsonify({"error": "invalid_employee_id"}), 400

    status_filter = request.args.get("status")
    if status_filter:
        query["status"] = status_filter

    cursor = db.sessions.find(query).sort("created_at", -1)
    sessions_list = [_session_to_json(s) for s in cursor]

    return jsonify({"sessions": sessions_list, "total": len(sessions_list)})


@sessions_bp.route("/sessions/<session_id>")
def get_session(session_id: str):
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    try:
        s = db.sessions.find_one({"_id": ObjectId(session_id), "org_id": ObjectId(org_id)})
    except InvalidId:
        return jsonify({"error": "invalid_id"}), 400

    if not s:
        return jsonify({"error": "not_found"}), 404

    return jsonify(_session_to_json(s))


@sessions_bp.route("/sessions/<session_id>", methods=["PUT"])
def update_session(session_id: str):
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    try:
        s = db.sessions.find_one({"_id": ObjectId(session_id), "org_id": ObjectId(org_id)})
    except InvalidId:
        return jsonify({"error": "invalid_id"}), 400

    if not s:
        return jsonify({"error": "not_found"}), 404

    data = request.get_json(silent=True) or {}
    set_fields = {}

    if "edited_text" in data:
        edited = (data["edited_text"] or "").strip()
        if len(edited.encode("utf-8")) > MAX_EDITED_TEXT_BYTES:
            return jsonify({"error": "edited_text_too_large"}), 413
        set_fields["transcript.edited"] = edited
        set_fields["transcript.word_count"] = len(edited.split())
        set_fields["last_transcript_update"] = datetime.now(timezone.utc)

    if "status" in data:
        valid_statuses = {"draft", "transcribed", "processing", "completed", "failed"}
        if data["status"] in valid_statuses:
            set_fields["status"] = data["status"]

    if "audio" in data:
        audio_val = data["audio"]
        if audio_val and isinstance(audio_val, str) and len(audio_val) > MAX_SESSION_AUDIO_BYTES:
            return jsonify({"error": "audio_payload_too_large"}), 413
        set_fields["audio"] = audio_val

    if not set_fields:
        return jsonify({"error": "no_fields_to_update"}), 400

    set_fields["updated_at"] = datetime.now(timezone.utc)
    db.sessions.update_one({"_id": ObjectId(session_id)}, {"$set": set_fields})

    s = db.sessions.find_one({"_id": ObjectId(session_id)})
    return jsonify(_session_to_json(s))


@sessions_bp.route("/sessions/<session_id>", methods=["DELETE"])
def delete_session(session_id: str):
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    try:
        result = db.sessions.delete_one({"_id": ObjectId(session_id), "org_id": ObjectId(org_id)})
    except InvalidId:
        return jsonify({"error": "invalid_id"}), 400

    if result.deleted_count == 0:
        return jsonify({"error": "not_found"}), 404

    return jsonify({"ok": True})


@sessions_bp.route("/sessions/<session_id>/analyze", methods=["POST"])
def analyze_session(session_id: str):
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    user_id_str = session.get("user_id", "")
    allowed, retry_after = _check_api_rate_limit(user_id_str, "analyze", _ANALYZE_MAX)
    if not allowed:
        return jsonify({
            "error": "Too many requests. Please try again later.",
            "retry_after": retry_after,
        }), 429, {"Retry-After": str(retry_after)}

    db = get_db()
    try:
        s = db.sessions.find_one({"_id": ObjectId(session_id), "org_id": ObjectId(org_id)})
    except InvalidId:
        return jsonify({"error": "invalid_id"}), 400

    if not s:
        return jsonify({"error": "not_found"}), 404

    transcript = (s.get("transcript") or {}).get("edited") or (s.get("transcript") or {}).get("raw", "")
    if not transcript:
        return jsonify({"error": "no_transcript_to_analyze"}), 400

    # ── Truncate before sending to LLM ──────────────────────────────
    # Prevents unbounded token usage / cost when a transcript is
    # exceptionally long.  The truncation is applied *after* the full
    # transcript is stored (so nothing is lost from the DB) but *before*
    # the LLM call.
    llm_transcript = transcript[:MAX_LLM_TRANSCRIPT_CHARS]

    db.sessions.update_one(
        {"_id": ObjectId(session_id)},
        {"$set": {"status": "processing", "updated_at": datetime.now(timezone.utc)}},
    )

    try:
        llm = get_llm_provider()
        analysis = llm.analyze(llm_transcript)

        now = datetime.now(timezone.utc)
        db.sessions.update_one(
            {"_id": ObjectId(session_id)},
            {
                "$set": {
                    "status": "completed",
                    "analysis": {
                        "model_used": f"{llm.model}",
                        **analysis,
                        "approved": False,
                        "approved_at": None,
                    },
                    "analysis_version": (s.get("analysis_version", 0) + 1),
                    "last_analyzed_at": now,
                    "updated_at": now,
                }
            },
        )

        # Roll the AI's read of this transcript into the employee's
        # wellness score — this is what actually drives Directory/Dashboard
        # now, instead of the old static default.
        risks = analysis.get("risks") or {}
        if not isinstance(risks, dict):
            # Defensive fallback: the LLM occasionally returns "risks" as a
            # list instead of an object. Never trust the shape blindly.
            risks = {}
        burnout_index = risks.get("burnout_index")
        attrition_risk_pct = risks.get("attrition_risk_pct")

        if burnout_index is not None or attrition_risk_pct is not None:
            burnout_index = burnout_index if burnout_index is not None else 0
            attrition_risk_pct = attrition_risk_pct if attrition_risk_pct is not None else 0
            ai_wellness_score = round(100 - ((burnout_index + attrition_risk_pct) / 2))
            ai_wellness_score = max(0, min(100, ai_wellness_score))

            db.employees.update_one(
                {"_id": s["employee_id"]},
                {
                    "$set": {
                        "ai_wellness": {
                            "score": ai_wellness_score,
                            "status": _status_for(ai_wellness_score),
                            "attrition_risk_pct": attrition_risk_pct,
                            "burnout_index": burnout_index,
                            "risk_factors": risks.get("risk_factors", []),
                            "source_session_id": str(session_id),
                            "updated_at": now,
                        },
                        "updated_at": now,
                    }
                },
            )

        # ── Silent background: Risk Drift Detection ──
        # Fires automatically after the wellness update above, never on user
        # action. It is best-effort: any failure here is logged and swallowed
        # so it can't fail the /analyze response or roll back the ai_wellness
        # update that already succeeded.
        try:
            # 1. Last N completed sessions (oldest-first) with a real analysis
            #    and a non-empty transcript.
            qualifying_sessions = list(
                db.sessions.find(
                    {
                        "employee_id": s["employee_id"],
                        "org_id": ObjectId(org_id),
                        "status": "completed",
                        "analysis": {"$ne": None},
                        "$or": [
                            {"transcript.edited": {"$nin": [None, ""]}},
                            {"transcript.raw": {"$nin": [None, ""]}},
                        ],
                    }
                ).sort("created_at", -1).limit(DRIFT_WINDOW_SIZE)
            )
            qualifying_sessions.reverse()

            # 2. Not enough completed syncs yet — expected for new employees,
            #    not an error. Skip quietly.
            if len(qualifying_sessions) >= DRIFT_WINDOW_SIZE:
                emp = db.employees.find_one({"_id": s["employee_id"]})

                # 3. Skip if this window was already processed (retries / re-analysis).
                if emp and emp.get("last_drift_check_session_id") != s["_id"]:
                    # 4. Build the payload for explain_drift(): FULL transcripts,
                    #    oldest first, not just the risk % numbers.
                    #    Each transcript is truncated to prevent unbounded
                    #    token usage in the drift-detection LLM call.
                    sessions_payload = [
                        {
                            "date": sess["created_at"].isoformat() if sess.get("created_at") else None,
                            "attrition_risk_pct": (sess["analysis"] or {}).get("risks", {}).get("attrition_risk_pct"),
                            "burnout_index": (sess["analysis"] or {}).get("risks", {}).get("burnout_index"),
                            "transcript": ((sess.get("transcript") or {}).get("edited") or (sess.get("transcript") or {}).get("raw", ""))[:MAX_LLM_TRANSCRIPT_CHARS],
                        }
                        for sess in qualifying_sessions
                    ]

                    # 5. LLM call — a failure here must never fail the /analyze
                    #    response; the outer except swallows it.
                    llm = get_llm_provider()
                    drift = llm.explain_drift(sessions_payload)

                    # 6. Persist the drift read and mark this window processed
                    #    so re-runs don't duplicate the check.
                    db.employees.update_one(
                        {"_id": s["employee_id"]},
                        {"$set": {
                            "last_drift_check_session_id": s["_id"],
                            "last_drift_check_at": datetime.now(timezone.utc),
                            "drift_explanation": drift,
                        }},
                    )

                    # 7. Surface genuine drift as a notification. Best-effort like
                    #    everything else in this block: a write failure is logged
                    #    and swallowed, never allowed to fail /analyze. False
                    #    positives stay silent, exactly as before.
                    if drift.get("is_genuine_pattern"):
                        org_doc = db.organizations.find_one({"_id": ObjectId(org_id)})
                        risk_alerts = ((org_doc or {}).get("notification_prefs") or {}).get("risk_alerts", True)
                        if risk_alerts:
                            now = datetime.now(timezone.utc)
                            existing = db.notifications.find_one(
                                {"org_id": ObjectId(org_id), "source_session_id": s["_id"]}
                            )
                            if not existing:
                                db.notifications.insert_one(
                                    {
                                        "org_id": ObjectId(org_id),
                                        "employee_id": s["employee_id"],
                                        "type": "risk_drift",
                                        "headline": drift.get("headline", ""),
                                        "summary": drift.get("summary", ""),
                                        "confidence": drift.get("confidence", 0),
                                        "source_session_id": s["_id"],
                                        "drift_explanation": drift,
                                        "sessions_window": [
                                            {
                                                "date": sess["created_at"].isoformat() if sess.get("created_at") else None,
                                                "attrition_risk_pct": (sess["analysis"] or {}).get("risks", {}).get("attrition_risk_pct"),
                                                "burnout_index": (sess["analysis"] or {}).get("risks", {}).get("burnout_index"),
                                            }
                                            for sess in qualifying_sessions
                                        ],
                                        "read": False,
                                        "created_at": now,
                                    }
                                )
        except Exception:
            logger.exception("Drift detection failed (session=%s)", session_id)

        # Build the response INSIDE the same try block so that any failure
        # here (e.g. the session vanishing, or an unexpected shape) is caught
        # below and returned as clean JSON instead of leaking an unhandled
        # exception out to Flask's HTML debug page — which is what was
        # causing "Unexpected token '<', <!doctype ... is not valid JSON"
        # on the frontend.
        s = db.sessions.find_one({"_id": ObjectId(session_id)})
        if not s:
            return jsonify({"error": "session_disappeared_after_analysis"}), 500
        result_json = _session_to_json(s)
        return jsonify(result_json)

    except LLMTimeoutError:
        # Retryable: the upstream LLM was slow/hung, not permanently broken.
        # Do NOT store the fallback analysis here — writing a mostly-empty
        # fallback would zero out the employee's wellness score as if it were
        # a real reading. Surface a distinct message so the user knows a retry
        # is likely to work.
        logger.warning("Session analysis timed out (session=%s)", session_id)
        db.sessions.update_one(
            {"_id": ObjectId(session_id)},
            {"$set": {"status": "failed", "updated_at": datetime.now(timezone.utc)}},
        )
        return jsonify({"error": "Analysis is taking longer than expected. Please try again."}), 500

    except Exception as e:
        logger.exception("Session analysis failed (session=%s)", session_id)
        db.sessions.update_one(
            {"_id": ObjectId(session_id)},
            {"$set": {"status": "failed", "updated_at": datetime.now(timezone.utc)}},
        )
        return jsonify({"error": "Analysis failed. Please try again."}), 500


@sessions_bp.route("/sessions/<session_id>/phrasing", methods=["POST"])
def analyze_phrasing(session_id: str):
    """Line-level phrasing & psychological-safety review.

    Separate from /analyze (which produces the deep post-conversation
    report). This scans the transcript for specific HR lines that could
    reduce trust (with a rephrasing suggestion) and specific employee lines
    that carry a communication signal (hesitation, possible concealment,
    guardedness, openness). Each flagged item carries a verbatim quote so
    the frontend can highlight the exact text in the transcript view.
    """
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    user_id_str = session.get("user_id", "")
    allowed, retry_after = _check_api_rate_limit(user_id_str, "phrasing", _PHRASING_MAX)
    if not allowed:
        return jsonify({
            "error": "Too many requests. Please try again later.",
            "retry_after": retry_after,
        }), 429, {"Retry-After": str(retry_after)}

    db = get_db()
    try:
        s = db.sessions.find_one({"_id": ObjectId(session_id), "org_id": ObjectId(org_id)})
    except InvalidId:
        return jsonify({"error": "invalid_id"}), 400

    if not s:
        return jsonify({"error": "not_found"}), 404

    transcript = (s.get("transcript") or {}).get("edited") or (s.get("transcript") or {}).get("raw", "")
    if not transcript:
        return jsonify({"error": "no_transcript_to_analyze"}), 400

    # Same truncation strategy as /analyze: the full transcript stays in the
    # DB untouched, only the LLM call itself is capped.
    llm_transcript = transcript[:MAX_LLM_TRANSCRIPT_CHARS]

    try:
        llm = get_llm_provider()
        phrasing = llm.analyze_phrasing(llm_transcript)

        now = datetime.now(timezone.utc)
        db.sessions.update_one(
            {"_id": ObjectId(session_id)},
            {
                "$set": {
                    "phrasing_analysis": phrasing,
                    "phrasing_analysis_version": (s.get("phrasing_analysis_version", 0) + 1),
                    "last_phrasing_analyzed_at": now,
                    "updated_at": now,
                }
            },
        )

        s = db.sessions.find_one({"_id": ObjectId(session_id)})
        if not s:
            return jsonify({"error": "session_disappeared_after_analysis"}), 500
        return jsonify(_session_to_json(s))

    except LLMTimeoutError:
        logger.warning("Phrasing review timed out (session=%s)", session_id)
        return jsonify({"error": "Phrasing review is taking longer than expected. Please try again."}), 500

    except Exception:
        logger.exception("Phrasing review failed (session=%s)", session_id)
        return jsonify({"error": "Phrasing review failed. Please try again."}), 500


@sessions_bp.route("/transcribe", methods=["POST"])
def transcribe_audio():
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    user_id_str = session.get("user_id", "")
    allowed, retry_after = _check_api_rate_limit(user_id_str, "transcribe", _TRANSCRIBE_MAX)
    if not allowed:
        return jsonify({
            "error": "Too many requests. Please try again later.",
            "retry_after": retry_after,
        }), 429, {"Retry-After": str(retry_after)}

    if "audio" not in request.files:
        return jsonify({"error": "audio_file_required"}), 400

    audio_file = request.files["audio"]
    content_type = audio_file.content_type or "audio/webm"
    audio_bytes = audio_file.read()

    # Optional provider language hint (e.g. "en", "hi", "hinglish", "auto").
    # Forwarded as-is; each provider normalizes/validates it, falling back
    # to its default rather than failing on an unrecognized value.
    language = (request.form.get("language") or "").strip() or None

    if not audio_bytes:
        return jsonify({"error": "empty_audio"}), 400

    # ── Per-endpoint size check BEFORE expensive STT call ────────────
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        return jsonify({"error": "audio_too_large"}), 413

    # ── MIME-type validation (defence-in-depth) ─────────────────────
    # Client-controlled Content-Type header is checked against an
    # allowlist of formats the browser can produce for getUserMedia.
    if content_type not in AUDIO_CONTENT_TYPES:
        return jsonify({"error": "unsupported_audio_type"}), 400

    try:
        stt = get_stt_provider()
        text = stt.transcribe(audio_bytes, content_type, language)
        return jsonify({"text": text})
    except Exception as e:
        logger.exception("Audio transcription failed")
        return jsonify({"error": "Transcription failed. Please try again."}), 500


@sessions_bp.route("/transcribe-image", methods=["POST"])
def transcribe_image():
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    user_id_str = session.get("user_id", "")
    allowed, retry_after = _check_api_rate_limit(user_id_str, "ocr", _OCR_MAX)
    if not allowed:
        return jsonify({
            "error": "Too many requests. Please try again later.",
            "retry_after": retry_after,
        }), 429, {"Retry-After": str(retry_after)}

    if "image" not in request.files:
        return jsonify({"error": "image_file_required"}), 400

    image_file = request.files["image"]
    content_type = image_file.content_type or "image/png"
    image_bytes = image_file.read()

    if not image_bytes:
        return jsonify({"error": "empty_image"}), 400

    if len(image_bytes) > MAX_IMAGE_BYTES:
        return jsonify({"error": "image_too_large"}), 400

    if content_type not in IMAGE_CONTENT_TYPES:
        return jsonify({"error": "unsupported_image_type"}), 400

    # ── Server-side magic-bytes validation (PIL) ────────────────────
    # The client-supplied Content-Type is untrusted; verify the actual
    # file signature using Pillow so a non-image file disguised with a
    # legitimate MIME header is rejected before the OCR API call.
    try:
        from PIL import Image as _PILImage
        img = _PILImage.open(io.BytesIO(image_bytes))
        img.verify()  # raises if the file is corrupt / not an image
    except Exception:
        return jsonify({"error": "invalid_image_content"}), 400

    try:
        vision = get_vision_provider()
        text = vision.extract_text(image_bytes, content_type)
        return jsonify({"text": text})
    except Exception as e:
        logger.exception("Image transcription (OCR) failed")
        return jsonify({"error": "Transcription failed. Please try again."}), 500

"""Trackable background jobs for the conversation workspace.

Translate ("Translate" <select>) and TTS ("Listen" language) actions used to
be pure request/response: the page fired a fetch, and if the user navigated
to another page mid-request (this is a multi-page app — dashboard.html,
settings.html, etc.), the in-flight request was abandoned and the result was
lost.

These two actions are now first-class async jobs that survive navigation:

    translation_jobs / tts_jobs

    {
        _id, org_id, user_id, session_id, employee_id, meeting_id,
        status: "queued" | "processing" | "done" | "failed",
        input_ref:  what is being translated / synthesized
        result:     translated analysis result | audio storage key
        error, created_at, completed_at,
    }

POST   /api/translate-jobs   -> create + dispatch the job, return {id}
GET    /api/translate-jobs   -> list (filterable by session_id/status)
GET    /api/translate-jobs/<id>            -> current status
POST   /api/tts-jobs         -> create + dispatch the job, return {id}
GET    /api/tts-jobs         -> list (filterable by session_id/status)
GET    /api/tts-jobs/<id>                  -> current status
GET    /api/tts-jobs/<id>/audio            -> the produced audio (done only)

The actual work (re-running the analysis at an output language, or
synthesizing narration audio) happens on a daemon thread, following the same
`threading.Thread` dispatch the platform already uses for background geo
lookups (api.py, login_flow.py) — no new async framework.

Completion is surfaced to the user through the existing notification system
(type "translation_ready" / "audio_ready") so the result is visible no matter
which page they land on afterwards.
"""

import logging
import threading
from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, jsonify, request, session

from employees import _require_auth
from extensions import get_db, check_rate_limit, record_rate_limit_event
from providers import get_llm_provider, get_storage_provider, get_tts_provider
from providers.llm import SUPPORTED_ANALYSIS_LANGUAGES

jobs_bp = Blueprint("jobs", __name__)
logger = logging.getLogger(__name__)

# Reuse the same truncation the synchronous analysis path applies, so the
# background re-run feeds the LLM the exact same (bounded) input.
from sessions import MAX_LLM_TRANSCRIPT_CHARS  # noqa: E402

# Same ceiling the synchronous /tts/synthesize route enforces.
_MAX_TTS_TEXT_CHARS = 50_000

_TRANSLATION_COLLECTION = "translation_jobs"
_TTS_COLLECTION = "tts_jobs"

# ── Rate-limit constants for the background-job endpoints ────────────────
_TRANSLATE_JOB_MAX = 15
_TTS_JOB_MAX = 30
_JOB_RATE_WINDOW = 900  # 15-minute sliding window, matching sessions.py

# Human display names used in notification summaries. Analysis language keys
# map to the names the frontend already uses; TTS BCP-47 prefixes fall back to
# a small common-language table.
_ANALYSIS_LANG_NAMES = {
    "en": "English",
    "hinglish": "Hinglish",
    "hindi": "Hindi",
    "spanish": "Spanish",
    "french": "French",
}
_BCP47_LANG_NAMES = {
    "en": "English",
    "hi": "Hindi",
    "es": "Spanish",
    "fr": "French",
    "bn": "Bengali",
    "ta": "Tamil",
    "te": "Telugu",
    "mr": "Marathi",
}

# Provider content-type -> file extension used when storing synthesized audio.
_CONTENT_TYPE_TO_EXT = {
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/ogg": "ogg",
    "audio/x-m4a": "m4a",
}


def _check_api_rate_limit(user_id_str: str, endpoint: str, max_events: int) -> tuple[bool, int]:
    """Return (allowed, retry_after) using the same sliding-window scheme as
    sessions.py (per-user, per-endpoint, 15-minute budget)."""
    db = get_db()
    key = f"api_rate:{user_id_str}:{endpoint}"
    allowed, retry_after = check_rate_limit(db, key, max_events, _JOB_RATE_WINDOW)
    if not allowed:
        return False, retry_after or _JOB_RATE_WINDOW
    record_rate_limit_event(db, key, ttl_seconds=_JOB_RATE_WINDOW)
    return True, 0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _language_display(language: str) -> str:
    """Best-effort human name for an analysis language key or BCP-47 code."""
    if not language:
        return ""
    key = language.strip().lower()
    name = _ANALYSIS_LANG_NAMES.get(key) or _BCP47_LANG_NAMES.get(key.split("-")[0])
    return name or language


def _job_to_json(job: dict) -> dict:
    return {
        "id": str(job["_id"]),
        "status": job.get("status", "queued"),
        "input_ref": job.get("input_ref"),
        "result": job.get("result"),
        "error": job.get("error"),
        "session_id": str(job["session_id"]) if job.get("session_id") else None,
        "employee_id": str(job["employee_id"]) if job.get("employee_id") else None,
        "meeting_id": str(job["meeting_id"]) if job.get("meeting_id") else None,
        "created_at": job["created_at"].isoformat() if job.get("created_at") else None,
        "completed_at": job["completed_at"].isoformat() if job.get("completed_at") else None,
    }


def _set_status(db, collection: str, job_id, status: str, error=None, result=None) -> None:
    """Idempotently update a job's status + terminal fields."""
    update = {
        "status": status,
        "completed_at": _now() if status in ("done", "failed") else None,
        "updated_at": _now(),
    }
    if error is not None:
        update["error"] = error
    if result is not None:
        update["result"] = result
    db[collection].update_one({"_id": job_id}, {"$set": update})


def _fetch_owned_job(db, collection: str, org_id, job_id):
    """Fetch a job scoped to the caller's org; returns doc or None."""
    try:
        org_oid = ObjectId(org_id) if not isinstance(org_id, ObjectId) else org_id
        job_oid = ObjectId(job_id)
    except InvalidId:
        return None
    return db[collection].find_one({"_id": job_oid, "org_id": org_oid})


def _notify_ready(db, org_id, job, notif_type: str, headline: str, summary: str, dedup_key: str) -> None:
    """Create a done/failed notification through the existing notifications
    collection (same shape as risk_drift / meeting_reminder entries). Best
    effort: a write failure is logged and swallowed so it never surfaces on
    the job status itself.

    A per-(org, session, type, detail) dedup key prevents the same readiness
    from being announced every time the same job is retried.
    """
    try:
        existing = db.notifications.find_one(
            {
                "org_id": org_id,
                "type": notif_type,
                "source_session_id": job.get("session_id"),
                "detail_key": dedup_key,
            }
        )
        if existing:
            return
        db.notifications.insert_one(
            {
                "org_id": org_id,
                "type": notif_type,
                "headline": headline,
                "summary": summary,
                "confidence": 0,
                # Deliberately store None (not a fabricated ObjectId) when the
                # job isn't tied to a real employee — notification rendering
                # uses a neutral label for such types. A random non-existent id
                # here would make every employee-name lookup fail and the UI
                # fall back to a misleading "Employee" label.
                "employee_id": job.get("employee_id"),
                "source_session_id": job.get("session_id"),
                "meeting_id": job.get("meeting_id"),
                "detail_key": dedup_key,
                "read": False,
                "created_at": _now(),
            }
        )
    except Exception:
        logger.exception("Failed to create %s notification (job=%s)", notif_type, job.get("_id"))


# ── Background workers ───────────────────────────────────────────────────
# Each worker receives everything it needs up front (db handle + already
# constructed provider instances). Providers only read app.config at
# construction time, so resolving them in the request context first means the
# daemon thread never touches Flask proxies — the same pattern the platform
# uses for its background geo-lookup threads.


def _run_translation_job(db, job_id, llm, org_id, language: str) -> None:
    """Re-run the session analysis at the requested output language.

    The workspace "Translate" control re-analyzes the transcript with an
    output-language instruction; that is what this job reproduces in the
    background, reusing the provider's translate/analyze logic verbatim.
    """
    collection = _TRANSLATION_COLLECTION
    job = db[collection].find_one({"_id": job_id})
    if not job:
        return
    session_id = job.get("session_id")
    employee_id = job.get("employee_id")

    _set_status(db, collection, job_id, "processing")
    try:
        s = db.sessions.find_one({"_id": session_id, "org_id": org_id})
        if not s:
            _set_status(db, collection, job_id, "failed", error="session_not_found")
            return

        transcript = (s.get("transcript") or {}).get("edited") or (s.get("transcript") or {}).get("raw", "")
        if not transcript:
            _set_status(db, collection, job_id, "failed", error="no_transcript_to_analyze")
            return

        # Same truncation as the synchronous /analyze path.
        analysis = llm.analyze(transcript[:MAX_LLM_TRANSCRIPT_CHARS], language=language)

        now = _now()
        db.sessions.update_one(
            {"_id": session_id, "org_id": org_id},
            {
                "$set": {
                    "status": "completed",
                    "analysis": {
                        "model_used": f"{llm.model}",
                        **analysis,
                        "approved": False,
                        "approved_at": None,
                    },
                    "analysis_language": language,
                    "analysis_version": (s.get("analysis_version", 0) + 1),
                    "last_analyzed_at": now,
                    "updated_at": now,
                }
            },
        )

        # Mirror the wellness roll-up the synchronous analyze does, so an
        # output-language re-run leaves dashboard/directory scores consistent.
        risks = analysis.get("risks") or {}
        if not isinstance(risks, dict):
            risks = {}
        burnout_index = risks.get("burnout_index")
        attrition_risk_pct = risks.get("attrition_risk_pct")
        if (burnout_index is not None or attrition_risk_pct is not None) and employee_id:
            burnout_index = burnout_index if burnout_index is not None else 0
            attrition_risk_pct = attrition_risk_pct if attrition_risk_pct is not None else 0
            ai_wellness_score = max(0, min(100, round(100 - ((burnout_index + attrition_risk_pct) / 2))))
            db.employees.update_one(
                {"_id": employee_id, "org_id": org_id},
                {
                    "$set": {
                        "ai_wellness": {
                            "score": ai_wellness_score,
                            "status": _ai_wellness_status(ai_wellness_score),
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

        result = {
            "session_id": str(session_id),
            "analysis_language": language,
        }
        _set_status(db, collection, job_id, "done", result=result)
        _notify_ready(
            db,
            org_id,
            job,
            notif_type="translation_ready",
            headline="Translation ready",
            summary=f"Translation for {_language_display(language) or 'the selected language'} finished.",
            dedup_key=f"translate:{language}",
        )
    except Exception:
        logger.exception("Translation job failed (job=%s)", job_id)
        _set_status(db, collection, job_id, "failed", error="Translation failed. Please try again.")


def _ai_wellness_status(score: int) -> str:
    if score >= 70:
        return "good"
    if score >= 40:
        return "at-risk"
    return "critical"


def _run_tts_job(db, job_id, tts, llm, storage) -> None:
    """Synthesize narration audio for a Listen request, translating first if
    requested, then storing the bytes via the storage provider. Mirrors the
    logic of the synchronous /tts/synthesize route."""
    collection = _TTS_COLLECTION
    job = db[collection].find_one({"_id": job_id})
    if not job:
        return
    input_ref = job.get("input_ref") or {}
    session_id = job.get("session_id")
    org_id = job.get("org_id")

    _set_status(db, collection, job_id, "processing")
    try:
        text = (input_ref.get("text") or "").strip()
        language_code = (input_ref.get("language_code") or "").strip()
        if not text:
            _set_status(db, collection, job_id, "failed", error="text_required")
            return
        if len(text) > _MAX_TTS_TEXT_CHARS:
            _set_status(db, collection, job_id, "failed", error="text_too_long")
            return
        if not language_code:
            _set_status(db, collection, job_id, "failed", error="language_code_required")
            return

        # Optional translation before synthesis for non-English targets —
        # identical to the synchronous route.
        if input_ref.get("translate") and language_code.split("-")[0].lower() != "en":
            text = llm.translate(text, language_code)

        tts = tts or get_tts_provider()
        audio = tts.synthesize(
            text,
            language_code,
            voice_name=input_ref.get("voice_name"),
            voice_tier=input_ref.get("voice_tier"),
        )
        if not audio:
            _set_status(db, collection, job_id, "failed", error="empty_audio")
            return

        content_type = getattr(tts, "content_type", "audio/wav") or "audio/wav"
        ext = _CONTENT_TYPE_TO_EXT.get(content_type, "bin")
        storage = storage or get_storage_provider()
        target = str(session_id) if session_id else "tts"
        audio_key = storage.save(target, f"job_{job_id}.{ext}", audio)

        result = {
            "audio_key": audio_key,
            "content_type": content_type,
        }
        _set_status(db, collection, job_id, "done", result=result)

        block = input_ref.get("block") or ""
        _notify_ready(
            db,
            org_id,
            job,
            notif_type="audio_ready",
            headline="Audio ready",
            summary=f"Audio{(' for ' + block) if block else ''} finished.",
            dedup_key=f"tts:{block}:{language_code}",
        )
    except Exception:
        logger.exception("TTS job failed (job=%s)", job_id)
        _set_status(db, collection, job_id, "failed", error="Audio generation failed. Please try again.")


# ── Translation job endpoints ────────────────────────────────────────────


@jobs_bp.route("/translate-jobs", methods=["POST"])
def create_translation_job():
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    user_id_str = session.get("user_id", "")
    allowed, retry_after = _check_api_rate_limit(user_id_str, "translate_job", _TRANSLATE_JOB_MAX)
    if not allowed:
        return jsonify({
            "error": "Too many requests. Please try again later.",
            "retry_after": retry_after,
        }), 429, {"Retry-After": str(retry_after)}

    data = request.get_json(silent=True) or {}
    session_id_raw = (data.get("session_id") or "").strip()
    language = (data.get("language") or "en").strip().lower()

    try:
        session_id = ObjectId(session_id_raw)
    except InvalidId:
        return jsonify({"error": "invalid_session_id"}), 400

    if language != "en" and language not in SUPPORTED_ANALYSIS_LANGUAGES:
        return jsonify({"error": "unsupported_language"}), 400

    db = get_db()
    s = db.sessions.find_one({"_id": session_id, "org_id": ObjectId(org_id)})
    if not s:
        return jsonify({"error": "session_not_found"}), 404

    employee_id = s.get("employee_id")
    meeting_id = _meeting_id_for_session(db, org_id, session_id)

    job_id = db[_TRANSLATION_COLLECTION].insert_one(
        {
            "org_id": ObjectId(org_id),
            "user_id": ObjectId(session.get("user_id")),
            "session_id": session_id,
            "employee_id": employee_id,
            "meeting_id": meeting_id,
            "status": "queued",
            "input_ref": {"language": language},
            "result": None,
            "error": None,
            "created_at": _now(),
            "completed_at": None,
        }
    ).inserted_id

    llm = get_llm_provider()
    threading.Thread(
        target=_run_translation_job,
        args=(db, job_id, llm, ObjectId(org_id), language),
        daemon=True,
    ).start()

    return jsonify({"id": str(job_id), "status": "queued"}), 201


@jobs_bp.route("/translate-jobs/<job_id>")
def get_translation_job(job_id: str):
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    job = _fetch_owned_job(db, _TRANSLATION_COLLECTION, org_id, job_id)
    if not job:
        return jsonify({"error": "not_found"}), 404
    return jsonify(_job_to_json(job))


@jobs_bp.route("/translate-jobs")
def list_translation_jobs():
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    query = {"org_id": ObjectId(org_id)}

    session_id = request.args.get("session_id")
    if session_id:
        try:
            query["session_id"] = ObjectId(session_id)
        except InvalidId:
            return jsonify({"error": "invalid_session_id"}), 400

    status_filter = request.args.get("status")
    if status_filter:
        query["status"] = status_filter

    limit = request.args.get("limit", default=20, type=int)
    limit = min(max(limit, 1), 50)

    jobs = list(
        db[_TRANSLATION_COLLECTION].find(query).sort("created_at", -1).limit(limit)
    )
    return jsonify({"jobs": [_job_to_json(j) for j in jobs], "total": len(jobs)})


# ── TTS job endpoints ────────────────────────────────────────────────────


@jobs_bp.route("/tts-jobs", methods=["POST"])
def create_tts_job():
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    user_id_str = session.get("user_id", "")
    allowed, retry_after = _check_api_rate_limit(user_id_str, "tts_job", _TTS_JOB_MAX)
    if not allowed:
        return jsonify({
            "error": "Too many requests. Please try again later.",
            "retry_after": retry_after,
        }), 429, {"Retry-After": str(retry_after)}

    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    language_code = (data.get("language_code") or "").strip()
    if not text:
        return jsonify({"error": "text_required"}), 400
    if len(text) > _MAX_TTS_TEXT_CHARS:
        return jsonify({"error": "text_too_long"}), 400
    if not language_code:
        return jsonify({"error": "language_code_required"}), 400

    db = get_db()

    session_id = None
    employee_id = None
    meeting_id = None
    session_id_raw = (data.get("session_id") or "").strip()
    if session_id_raw:
        try:
            session_id = ObjectId(session_id_raw)
        except InvalidId:
            return jsonify({"error": "invalid_session_id"}), 400
        s = db.sessions.find_one({"_id": session_id, "org_id": ObjectId(org_id)})
        employee_id = (s or {}).get("employee_id")
        meeting_id = _meeting_id_for_session(db, org_id, session_id)

    meeting_id_raw = (data.get("meeting_id") or "").strip()
    if meeting_id_raw and not meeting_id:
        try:
            meeting_id = ObjectId(meeting_id_raw)
        except InvalidId:
            return jsonify({"error": "invalid_meeting_id"}), 400

    job_id = db[_TTS_COLLECTION].insert_one(
        {
            "org_id": ObjectId(org_id),
            "user_id": ObjectId(session.get("user_id")),
            "session_id": session_id,
            "employee_id": employee_id,
            "meeting_id": meeting_id,
            "status": "queued",
            "input_ref": {
                "text": text,
                "language_code": language_code,
                "translate": bool(data.get("translate", False)),
                "voice_name": (data.get("voice_name") or "").strip() or None,
                "voice_tier": (data.get("voice_tier") or "").strip() or None,
                "block": (data.get("block") or "").strip(),
            },
            "result": None,
            "error": None,
            "created_at": _now(),
            "completed_at": None,
        }
    ).inserted_id

    # Resolve providers inside the request context; the worker thread uses the
    # instances directly and never touches Flask proxies.
    tts = get_tts_provider()
    llm = get_llm_provider()
    storage = get_storage_provider()
    threading.Thread(
        target=_run_tts_job,
        args=(db, job_id, tts, llm, storage),
        daemon=True,
    ).start()

    return jsonify({"id": str(job_id), "status": "queued"}), 201


@jobs_bp.route("/tts-jobs/<job_id>")
def get_tts_job(job_id: str):
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    job = _fetch_owned_job(db, _TTS_COLLECTION, org_id, job_id)
    if not job:
        return jsonify({"error": "not_found"}), 404
    return jsonify(_job_to_json(job))


@jobs_bp.route("/tts-jobs")
def list_tts_jobs():
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    query = {"org_id": ObjectId(org_id)}

    session_id = request.args.get("session_id")
    if session_id:
        try:
            query["session_id"] = ObjectId(session_id)
        except InvalidId:
            return jsonify({"error": "invalid_session_id"}), 400

    status_filter = request.args.get("status")
    if status_filter:
        query["status"] = status_filter

    limit = request.args.get("limit", default=20, type=int)
    limit = min(max(limit, 1), 50)

    jobs = list(db[_TTS_COLLECTION].find(query).sort("created_at", -1).limit(limit))
    return jsonify({"jobs": [_job_to_json(j) for j in jobs], "total": len(jobs)})


@jobs_bp.route("/tts-jobs/<job_id>/audio")
def get_tts_job_audio(job_id: str):
    """Serve the audio produced by a completed TTS job.

    Only the owning org can fetch it. Streamed as a file download with the
    provider's content type so the workspace narration mini-player can play it
    after the user navigates back.
    """
    from flask import send_file

    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    job = _fetch_owned_job(db, _TTS_COLLECTION, org_id, job_id)
    if not job:
        return jsonify({"error": "not_found"}), 404
    if job.get("status") != "done":
        return jsonify({"error": "not_ready"}), 409

    result = job.get("result") or {}
    audio_key = result.get("audio_key")
    if not audio_key:
        return jsonify({"error": "no_audio"}), 404

    try:
        path = get_storage_provider().path_for(audio_key)
    except Exception:
        return jsonify({"error": "not_found"}), 404

    if not path.is_file():
        return jsonify({"error": "not_found"}), 404

    content_type = result.get("content_type") or "audio/wav"
    filename = f"tts-job-{job_id}.{_CONTENT_TYPE_TO_EXT.get(content_type, 'bin')}"
    return send_file(
        path,
        mimetype=content_type,
        download_name=filename,
        conditional=True,
    )


def _meeting_id_for_session(db, org_id, session_id):
    """Return the meeting referencing this session, if any — lets the ready
    notification deep-link with meeting context when one exists."""
    try:
        meeting = db.meetings.find_one(
            {"session_id": session_id, "org_id": ObjectId(org_id)}, {"_id": 1}
        )
        return meeting["_id"] if meeting else None
    except Exception:
        return None
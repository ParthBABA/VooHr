from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, jsonify, request, session

from employee_scoring import _status_for
from employees import _require_auth
from extensions import get_db
from providers import get_llm_provider, get_storage_provider, get_stt_provider

sessions_bp = Blueprint("sessions", __name__)

# Number of recent completed sessions needed before the silent Risk Drift
# Detection check fires. New employees with fewer analyzed syncs are skipped.
DRIFT_WINDOW_SIZE = 3


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
        "last_transcript_update": s["last_transcript_update"].isoformat() if s.get("last_transcript_update") else None,
        "last_analyzed_at": s["last_analyzed_at"].isoformat() if s.get("last_analyzed_at") else None,
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
        set_fields["transcript.edited"] = edited
        set_fields["transcript.word_count"] = len(edited.split())
        set_fields["last_transcript_update"] = datetime.now(timezone.utc)

    if "status" in data:
        valid_statuses = {"draft", "transcribed", "processing", "completed", "failed"}
        if data["status"] in valid_statuses:
            set_fields["status"] = data["status"]

    if "audio" in data:
        set_fields["audio"] = data["audio"]

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

    db.sessions.update_one(
        {"_id": ObjectId(session_id)},
        {"$set": {"status": "processing", "updated_at": datetime.now(timezone.utc)}},
    )

    try:
        llm = get_llm_provider()
        analysis = llm.analyze(transcript)

        # ── DEBUG: Stage 9 — Analysis result from LLM ──
        import sys
        print("---", file=sys.stderr)
        print("[DEBUG_SESSION] STAGE 9 — analysis dict from llm.analyze():", file=sys.stderr)
        print("  type:", type(analysis).__name__, file=sys.stderr)
        if isinstance(analysis, dict):
            print("  keys:", list(analysis.keys()), file=sys.stderr)
            for sk in ["step2_behavioural_intelligence", "step3_root_cause_analysis", "step4_action_blueprint", "step5_conversation_strategy"]:
                step = analysis.get(sk)
                if isinstance(step, dict):
                    lte = [k for k, v in step.items() if isinstance(v, str) and v == "Limited transcript evidence."]
                    empty = [k for k, v in step.items() if isinstance(v, list) and len(v) == 0]
                    print(f"    {sk}: LTE={lte}  EMPTY={empty}", file=sys.stderr)
                else:
                    print(f"    {sk}: type={type(step).__name__}", file=sys.stderr)

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
        # action. It is best-effort: any failure here is logged to stderr and
        # swallowed so it can't fail the /analyze response or roll back the
        # ai_wellness update that already succeeded.
        try:
            import sys

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
            if len(qualifying_sessions) < DRIFT_WINDOW_SIZE:
                print(f"[DEBUG_DRIFT] session={session_id} skipped: {len(qualifying_sessions)}/{DRIFT_WINDOW_SIZE} qualifying sessions", file=sys.stderr)
            else:
                emp = db.employees.find_one({"_id": s["employee_id"]})

                # 3. Skip if this window was already processed (retries / re-analysis).
                if not emp:
                    print(f"[DEBUG_DRIFT] session={session_id} skipped: employee not found", file=sys.stderr)
                elif emp.get("last_drift_check_session_id") == s["_id"]:
                    print(f"[DEBUG_DRIFT] session={session_id} skipped: drift window already processed", file=sys.stderr)
                else:
                    # 4. Build the payload for explain_drift(): FULL transcripts,
                    #    oldest first, not just the risk % numbers.
                    sessions_payload = [
                        {
                            "date": sess["created_at"].isoformat() if sess.get("created_at") else None,
                            "attrition_risk_pct": (sess["analysis"] or {}).get("risks", {}).get("attrition_risk_pct"),
                            "burnout_index": (sess["analysis"] or {}).get("risks", {}).get("burnout_index"),
                            "transcript": (sess.get("transcript") or {}).get("edited") or (sess.get("transcript") or {}).get("raw", ""),
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
                    print(f"[DEBUG_DRIFT] session={session_id} drift check complete — is_genuine_pattern={drift.get('is_genuine_pattern')} confidence={drift.get('confidence')}", file=sys.stderr)

                    # 7. Surface genuine drift as a notification. Best-effort like
                    #    everything else in this block: a write failure is logged
                    #    and swallowed, never allowed to fail /analyze. False
                    #    positives stay silent, exactly as before.
                    if drift.get("is_genuine_pattern"):
                        now = datetime.now(timezone.utc)
                        existing = db.notifications.find_one(
                            {"org_id": ObjectId(org_id), "source_session_id": s["_id"]}
                        )
                        if existing:
                            print(f"[DEBUG_NOTIF] session={session_id} notification already exists ({existing['_id']}) — skipping", file=sys.stderr)
                        else:
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
                            print(f"[DEBUG_NOTIF] session={session_id} notification created for employee={s['employee_id']}", file=sys.stderr)
                    else:
                        print(f"[DEBUG_NOTIF] session={session_id} is_genuine_pattern=False — no notification created", file=sys.stderr)
        except Exception as e:
            import traceback
            print(f"[DEBUG_DRIFT] session={session_id} drift check failed: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)

        # ── DEBUG: Stage 10 — Re-fetch from DB and check stored data ──
        s_check = db.sessions.find_one({"_id": ObjectId(session_id)})
        stored_analysis = (s_check or {}).get("analysis") or {}
        print("---", file=sys.stderr)
        print("[DEBUG_SESSION] STAGE 10 — analysis stored in MongoDB:", file=sys.stderr)
        if isinstance(stored_analysis, dict):
            for sk in ["step2_behavioural_intelligence", "step3_root_cause_analysis", "step4_action_blueprint", "step5_conversation_strategy"]:
                step = stored_analysis.get(sk)
                if isinstance(step, dict):
                    lte = [k for k, v in step.items() if isinstance(v, str) and v == "Limited transcript evidence."]
                    empty = [k for k, v in step.items() if isinstance(v, list) and len(v) == 0]
                    print(f"    {sk}: LTE={lte}  EMPTY={empty}", file=sys.stderr)
                else:
                    print(f"    {sk}: type={type(step).__name__}", file=sys.stderr)
        else:
            print(f"    stored_analysis type: {type(stored_analysis).__name__}", file=sys.stderr)

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

        # ── DEBUG: Stage 11 — JSON response sent to frontend ──
        print("---", file=sys.stderr)
        print("[DEBUG_SESSION] STAGE 11 — response JSON to frontend:", file=sys.stderr)
        resp_analysis = result_json.get("analysis") or {}
        if isinstance(resp_analysis, dict):
            for sk in ["step2_behavioural_intelligence", "step3_root_cause_analysis", "step4_action_blueprint", "step5_conversation_strategy"]:
                step = resp_analysis.get(sk)
                if isinstance(step, dict):
                    lte = [k for k, v in step.items() if isinstance(v, str) and v == "Limited transcript evidence."]
                    empty = [k for k, v in step.items() if isinstance(v, list) and len(v) == 0]
                    print(f"    {sk}: LTE={lte}  EMPTY={empty}", file=sys.stderr)
                    # Verify populated content — log fields with real values
                    populated = [k for k, v in step.items() if isinstance(v, str) and v.strip() and v != "Limited transcript evidence."]
                    non_empty_lists = [k for k, v in step.items() if isinstance(v, list) and len(v) > 0]
                    if populated or non_empty_lists:
                        print(f"    {sk}: POPULATED_fields={populated}  POPULATED_lists={non_empty_lists}", file=sys.stderr)
                else:
                    print(f"    {sk}: type={type(step).__name__}  value={repr(step)[:200]}", file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        return jsonify(result_json)

    except Exception as e:
        import traceback
        print("[DEBUG_SESSION] EXCEPTION:", e, file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        db.sessions.update_one(
            {"_id": ObjectId(session_id)},
            {"$set": {"status": "failed", "updated_at": datetime.now(timezone.utc)}},
        )
        return jsonify({"error": f"analysis_failed: {str(e)}"}), 500


@sessions_bp.route("/transcribe", methods=["POST"])
def transcribe_audio():
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    if "audio" not in request.files:
        return jsonify({"error": "audio_file_required"}), 400

    audio_file = request.files["audio"]
    content_type = audio_file.content_type or "audio/webm"
    audio_bytes = audio_file.read()

    if not audio_bytes:
        return jsonify({"error": "empty_audio"}), 400

    try:
        stt = get_stt_provider()
        text = stt.transcribe(audio_bytes, content_type)
        return jsonify({"text": text})
    except Exception as e:
        return jsonify({"error": f"transcription_failed: {str(e)}"}), 500

"""Survey templates and employee pulse-survey responses.

Managers/admins record structured Q&A (Likert + free-text) against an
existing employee record via the logged-in session ``_require_auth`` gate.
Numeric questions are normalized to a single 0-100 engagement score and
synced into ``employees.signals.engagement_survey_score`` (most recent
response wins), so the existing wellness-scoring pipeline stays the one
consumer of that signal and this module never invents its own scoring.

Tenant isolation mirrors the rest of the API: every query is scoped by the
caller's ``org_id``, and every write validates that referenced templates and
employees belong to the same org.
"""

import logging
from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, jsonify, request, session

from audit_log import (
    ACTION_SURVEY_RESPONSE_CREATE,
    ACTION_SURVEY_RESPONSE_DELETE,
    ACTION_SURVEY_RESPONSE_UPDATE,
    ACTION_SURVEY_TEMPLATE_CREATE,
    ACTION_SURVEY_TEMPLATE_DELETE,
    ACTION_SURVEY_TEMPLATE_UPDATE,
    log_audit_event,
)
from employees import _require_auth
from extensions import get_db
from field_encryption import decrypt_fields

logger = logging.getLogger(__name__)

surveys_bp = Blueprint("surveys", __name__)

# ── Question / template limits ────────────────────────────────────────
MAX_TITLE_LEN = 200            # template title
MAX_DESC_LEN = 2000            # template description
MAX_QUESTION_TEXT_LEN = 500    # per-question prompt
MAX_QUESTIONS = 50             # questions per template
MAX_ANSWER_TEXT_LEN = 2000     # free-text answer length

QUESTION_TYPES = {"likert_5", "likert_10", "numeric_100", "text"}
TEMPLATE_STATUSES = {"draft", "published", "archived"}

# Pagination constants (mirror audit_log / employees).
DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 200


# ── Serializers ───────────────────────────────────────────────────────

def _template_to_json(t):
    return {
        "id": str(t["_id"]),
        "title": t.get("title", ""),
        "description": t.get("description", ""),
        "questions": list(t.get("questions") or []),
        "status": t.get("status", "draft"),
        "response_count": t.get("response_count", 0),
        "created_by": t.get("created_by"),
        "created_at": t.get("created_at").isoformat() if t.get("created_at") else None,
        "updated_at": t.get("updated_at").isoformat() if t.get("updated_at") else None,
    }


def _response_to_json(r, employees_by_id=None):
    answers = [
        {"question_id": a.get("question_id"), "value": a.get("value")}
        for a in (r.get("answers") or [])
    ]
    name = ""
    email = ""
    emp = (employees_by_id or {}).get(r.get("employee_id"))
    if emp:
        pii = decrypt_fields(emp.get("encrypted"), emp.get("wrapped_dek", ""))
        name = pii.get("name", "")
        email = pii.get("email", "")
    return {
        "id": str(r["_id"]),
        "template_id": str(r["template_id"]) if r.get("template_id") else None,
        "employee_id": str(r["employee_id"]),
        "employee_name": name,
        "employee_email": email,
        "answers": answers,
        "engagement_score": r.get("engagement_score"),
        "submitted_by": r.get("submitted_by"),
        "created_at": r.get("created_at").isoformat() if r.get("created_at") else None,
        "updated_at": r.get("updated_at").isoformat() if r.get("updated_at") else None,
    }


def _page_args():
    try:
        page = max(1, int(request.args.get("page", 1) or 1))
    except ValueError:
        page = 1
    try:
        limit = int(request.args.get("limit", DEFAULT_PAGE_LIMIT) or DEFAULT_PAGE_LIMIT)
    except ValueError:
        limit = DEFAULT_PAGE_LIMIT
    return page, min(max(1, limit), MAX_PAGE_LIMIT)


# ── Template question parsing / validation ────────────────────────────

def _normalize_questions(raw) -> tuple[list, str | None]:
    """Validate and normalize a raw questions list into [{id, text, type}].

    Client-supplied ids are ignored; ids are regenerated positionally
    (q1, q2, ...) so a template is stable until its questions are replaced
    (which is blocked once responses exist). Returns (questions, error).
    """
    if not isinstance(raw, list):
        return [], "questions_required"
    if not raw:
        return [], "questions_required"
    if len(raw) > MAX_QUESTIONS:
        return [], "too_many_questions"

    questions = []
    for i, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            return [], "invalid_question"
        text = (item.get("text") or "").strip()
        qtype = (item.get("type") or "").strip()
        if not text:
            return [], "question_text_required"
        if len(text) > MAX_QUESTION_TEXT_LEN:
            return [], "question_text_too_long"
        if qtype not in QUESTION_TYPES:
            return [], "invalid_question_type"
        questions.append({"id": f"q{i}", "text": text, "type": qtype})
    return questions, None


def _validate_answers(template, raw_answers) -> tuple[list, str | None]:
    """Validate a raw answers list against a template's questions.

    Returns (answers, error) where answers is a list of
    ``{"question_id": ..., "value": ...}`` records.
    """
    if not isinstance(raw_answers, list) or not raw_answers:
        return [], "answers_required"

    questions_by_id = {q["id"]: q for q in (template.get("questions") or [])}
    answers = []
    seen = set()
    for item in raw_answers:
        if not isinstance(item, dict):
            return [], "invalid_answer"
        question_id = item.get("question_id")
        value = item.get("value")
        if not isinstance(question_id, str):
            return [], "invalid_answer_question_id"
        q = questions_by_id.get(question_id)
        if not q:
            return [], "unknown_question"
        if question_id in seen:
            return [], "duplicate_question"
        seen.add(question_id)

        qtype = q.get("type")
        if qtype == "text":
            if not isinstance(value, str):
                return [], "answer_not_text"
            value = value.strip()
            if not value:
                return [], "answer_required"
            if len(value) > MAX_ANSWER_TEXT_LEN:
                return [], "answer_too_long"
        else:
            if isinstance(value, bool) or not isinstance(value, int):
                return [], "answer_not_numeric"
            if qtype == "likert_5" and not (1 <= value <= 5):
                return [], "answer_out_of_range"
            if qtype == "likert_10" and not (1 <= value <= 10):
                return [], "answer_out_of_range"
            if qtype == "numeric_100" and not (0 <= value <= 100):
                return [], "answer_out_of_range"

        answers.append({"question_id": question_id, "value": value})
    return answers, None


def _engagement_score(template, answers) -> int | None:
    """Normalize numeric answers to a 0-100 engagement score.

    likert_5:  1..5   -> 0,25,50,75,100
    likert_10: 1..10  -> 0..100 (9 steps)
    numeric_100: 0..100 raw
    Free-text answers never contribute. Returns None when the response has
    no numeric answers, in which case the employee signal is left untouched.
    """
    questions_by_id = {q["id"]: q for q in (template.get("questions") or [])}
    total = 0.0
    count = 0
    for a in answers:
        q = questions_by_id.get(a["question_id"])
        value = a.get("value")
        if not q or isinstance(value, bool) or not isinstance(value, int):
            continue
        qtype = q.get("type")
        if qtype == "likert_5":
            total += (value - 1) / 4 * 100
            count += 1
        elif qtype == "likert_10":
            total += (value - 1) / 9 * 100
            count += 1
        elif qtype == "numeric_100":
            total += value
            count += 1
    if not count:
        return None
    return max(0, min(100, round(total / count)))


# ── Employee engagement-signal sync ───────────────────────────────────

def _sync_engagement_score(db, org_id, employee_oid):
    """Point ``employee.signals.engagement_survey_score`` at the most recent
    response that carries a numeric score (or clear it when none remains).

    Text-only responses carry ``engagement_score=None`` and are skipped so a
    free-text check-in never erases a previously recorded numeric score.
    """
    emp = db.employees.find_one({"_id": employee_oid, "org_id": ObjectId(org_id)})
    if not emp:
        return

    latest = db.survey_responses.find_one(
        {"org_id": ObjectId(org_id), "employee_id": employee_oid,
         "engagement_score": {"$ne": None}},
        sort=[("created_at", -1), ("_id", -1)],
    )

    signals = dict(emp.get("signals") or {})
    if latest and latest.get("engagement_score") is not None:
        signals["engagement_survey_score"] = latest["engagement_score"]
    else:
        signals.pop("engagement_survey_score", None)

    db.employees.update_one(
        {"_id": employee_oid},
        {"$set": {
            "signals": signals,
            "updated_at": datetime.now(timezone.utc),
        }},
    )


def _count_responses(db, org_id, template_oids):
    """Return {template_oid: response_count} for the given template ids."""
    if not template_oids:
        return {}
    oid = ObjectId(org_id)
    counts = {t: 0 for t in template_oids}
    for row in db.survey_responses.aggregate([
        {"$match": {"org_id": oid, "template_id": {"$in": template_oids}}},
        {"$group": {"_id": "$template_id", "n": {"$sum": 1}}},
    ]):
        counts[row["_id"]] = row["n"]
    return counts


def _get_template(db, org_id, template_id_raw):
    try:
        t = db.survey_templates.find_one(
            {"_id": ObjectId(template_id_raw), "org_id": ObjectId(org_id)}
        )
    except InvalidId:
        return None
    return t


# ── Template endpoints ────────────────────────────────────────────────

@surveys_bp.route("/survey-templates", methods=["POST"])
def create_survey_template():
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    description = (data.get("description") or "").strip()
    status = (data.get("status") or "").strip() or "draft"

    if not title:
        return jsonify({"error": "title_required"}), 400
    if len(title) > MAX_TITLE_LEN:
        return jsonify({"error": "title_too_long"}), 400
    if len(description) > MAX_DESC_LEN:
        return jsonify({"error": "description_too_long"}), 400
    if status not in TEMPLATE_STATUSES:
        return jsonify({"error": "invalid_status"}), 400

    questions, err = _normalize_questions(data.get("questions"))
    if err:
        return jsonify({"error": err}), 400

    now = datetime.now(timezone.utc)
    doc = {
        "org_id": ObjectId(org_id),
        "title": title,
        "description": description,
        "questions": questions,
        "status": status,
        "created_by": session.get("user_id"),
        "created_at": now,
        "updated_at": now,
    }
    result = get_db().survey_templates.insert_one(doc)
    doc["_id"] = result.inserted_id

    log_audit_event(
        get_db(), org_id, session.get("user_id"), session.get("user_name") or "",
        ACTION_SURVEY_TEMPLATE_CREATE,
        target_type="survey_template", target_id=str(doc["_id"]),
        target_label=title,
        meta={"questions": len(questions), "status": status},
    )
    return jsonify(_template_to_json(doc)), 201


@surveys_bp.route("/survey-templates")
def list_survey_templates():
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    page, limit = _page_args()
    db = get_db()
    query = {"org_id": ObjectId(org_id)}
    status = (request.args.get("status") or "").strip().lower()
    if status:
        if status not in TEMPLATE_STATUSES:
            return jsonify({"error": "invalid_status"}), 400
        query["status"] = status

    total = db.survey_templates.count_documents(query)
    docs = list(
        db.survey_templates.find(query)
        .sort("created_at", -1)
        .skip((page - 1) * limit)
        .limit(limit)
    )
    counts = _count_responses(db, org_id, [d["_id"] for d in docs])
    items = []
    for d in docs:
        d["response_count"] = counts.get(d["_id"], 0)
        items.append(_template_to_json(d))

    offset = (page - 1) * limit
    return jsonify({
        "items": items,
        "total": total,
        "page": page,
        "limit": limit,
        "has_more": total > (offset + len(items)),
    })


@surveys_bp.route("/survey-templates/<template_id>")
def get_survey_template(template_id: str):
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    t = _get_template(db, org_id, template_id)
    if not t:
        return jsonify({"error": "not_found"}), 404

    t["response_count"] = _count_responses(db, org_id, [t["_id"]]).get(t["_id"], 0)
    return jsonify(_template_to_json(t))


@surveys_bp.route("/survey-templates/<template_id>", methods=["PATCH"])
def update_survey_template(template_id: str):
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    t = _get_template(db, org_id, template_id)
    if not t:
        return jsonify({"error": "not_found"}), 404

    data = request.get_json(silent=True) or {}
    set_fields = {}

    if "title" in data:
        title = (data["title"] or "").strip()
        if not title:
            return jsonify({"error": "title_required"}), 400
        if len(title) > MAX_TITLE_LEN:
            return jsonify({"error": "title_too_long"}), 400
        set_fields["title"] = title

    if "description" in data:
        description = (data["description"] or "").strip()
        if len(description) > MAX_DESC_LEN:
            return jsonify({"error": "description_too_long"}), 400
        set_fields["description"] = description

    if "status" in data:
        status = (data["status"] or "").strip().lower()
        if status not in TEMPLATE_STATUSES:
            return jsonify({"error": "invalid_status"}), 400
        set_fields["status"] = status

    if "questions" in data:
        # Replacing questions would orphan already-recorded answers when the
        # question ids are regenerated, so block it once responses exist.
        in_use = db.survey_responses.count_documents(
            {"org_id": ObjectId(org_id), "template_id": t["_id"]}
        )
        if in_use:
            return jsonify({"error": "template_in_use"}), 409
        questions, err = _normalize_questions(data.get("questions"))
        if err:
            return jsonify({"error": err}), 400
        set_fields["questions"] = questions

    if not set_fields:
        return jsonify({"error": "no_fields_to_update"}), 400

    set_fields["updated_at"] = datetime.now(timezone.utc)
    db.survey_templates.update_one({"_id": t["_id"]}, {"$set": set_fields})
    t = db.survey_templates.find_one({"_id": t["_id"]})
    t["response_count"] = _count_responses(db, org_id, [t["_id"]]).get(t["_id"], 0)

    log_audit_event(
        db, org_id, session.get("user_id"), session.get("user_name") or "",
        ACTION_SURVEY_TEMPLATE_UPDATE,
        target_type="survey_template", target_id=str(t["_id"]),
        target_label=set_fields.get("title") or t.get("title", ""),
        meta={"fields": sorted(set_fields.keys())},
    )
    return jsonify(_template_to_json(t))


@surveys_bp.route("/survey-templates/<template_id>", methods=["DELETE"])
def delete_survey_template(template_id: str):
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    try:
        oid = ObjectId(template_id)
    except InvalidId:
        return jsonify({"error": "invalid_id"}), 400

    t = db.survey_templates.find_one({"_id": oid, "org_id": ObjectId(org_id)})
    if not t:
        return jsonify({"error": "not_found"}), 404

    in_use = db.survey_responses.count_documents(
        {"org_id": ObjectId(org_id), "template_id": oid}
    )
    if in_use:
        return jsonify({"error": "template_in_use"}), 409

    db.survey_templates.delete_one({"_id": oid})
    log_audit_event(
        db, org_id, session.get("user_id"), session.get("user_name") or "",
        ACTION_SURVEY_TEMPLATE_DELETE,
        target_type="survey_template", target_id=str(oid),
        target_label=t.get("title", ""),
    )
    return jsonify({"ok": True})


# ── Response endpoints ────────────────────────────────────────────────

@surveys_bp.route("/survey-responses", methods=["POST"])
def create_survey_response():
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    data = request.get_json(silent=True) or {}
    db = get_db()

    template_id_raw = (data.get("template_id") or "").strip()
    if not template_id_raw:
        return jsonify({"error": "template_id_required"}), 400
    t = _get_template(db, org_id, template_id_raw)
    if not t:
        return jsonify({"error": "template_not_found"}), 404
    if t.get("status") == "archived":
        return jsonify({"error": "template_archived"}), 400

    employee_id_raw = (data.get("employee_id") or "").strip()
    if not employee_id_raw:
        return jsonify({"error": "employee_id_required"}), 400
    try:
        emp_oid = ObjectId(employee_id_raw)
    except InvalidId:
        return jsonify({"error": "invalid_employee_id"}), 400
    emp = db.employees.find_one({"_id": emp_oid, "org_id": ObjectId(org_id)})
    if not emp:
        return jsonify({"error": "employee_not_found"}), 404

    answers, err = _validate_answers(t, data.get("answers"))
    if err:
        return jsonify({"error": err}), 400

    now = datetime.now(timezone.utc)
    doc = {
        "org_id": ObjectId(org_id),
        "template_id": t["_id"],
        "employee_id": emp_oid,
        "answers": answers,
        "engagement_score": _engagement_score(t, answers),
        "submitted_by": session.get("user_id"),
        "created_at": now,
        "updated_at": now,
    }
    result = db.survey_responses.insert_one(doc)
    doc["_id"] = result.inserted_id

    _sync_engagement_score(db, org_id, emp_oid)

    log_audit_event(
        db, org_id, session.get("user_id"), session.get("user_name") or "",
        ACTION_SURVEY_RESPONSE_CREATE,
        target_type="employee", target_id=str(emp_oid),
        target_label=emp.get("employee_id") or str(emp_oid),
        meta={"survey_template": str(t["_id"]), "engagement_score": doc["engagement_score"]},
    )
    return jsonify(_response_to_json(doc, {emp_oid: emp})), 201


@surveys_bp.route("/survey-responses")
def list_survey_responses():
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    page, limit = _page_args()
    db = get_db()
    oid = ObjectId(org_id)
    query = {"org_id": oid}

    template_id = (request.args.get("template_id") or "").strip()
    if template_id:
        try:
            query["template_id"] = ObjectId(template_id)
        except InvalidId:
            return jsonify({"error": "invalid_template_id"}), 400

    employee_id = (request.args.get("employee_id") or "").strip()
    if employee_id:
        try:
            query["employee_id"] = ObjectId(employee_id)
        except InvalidId:
            return jsonify({"error": "invalid_employee_id"}), 400

    total = db.survey_responses.count_documents(query)
    docs = list(
        db.survey_responses.find(query)
        .sort("created_at", -1)
        .skip((page - 1) * limit)
        .limit(limit)
    )

    emp_ids = list({d["employee_id"] for d in docs})
    employees = {
        e["_id"]: e
        for e in db.employees.find({"_id": {"$in": emp_ids}, "org_id": oid})
    } if emp_ids else {}

    items = [_response_to_json(d, employees) for d in docs]
    offset = (page - 1) * limit
    return jsonify({
        "items": items,
        "total": total,
        "page": page,
        "limit": limit,
        "has_more": total > (offset + len(items)),
    })


@surveys_bp.route("/survey-responses/<response_id>")
def get_survey_response(response_id: str):
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    try:
        r = db.survey_responses.find_one(
            {"_id": ObjectId(response_id), "org_id": ObjectId(org_id)}
        )
    except InvalidId:
        return jsonify({"error": "invalid_id"}), 400
    if not r:
        return jsonify({"error": "not_found"}), 404

    emp = db.employees.find_one({"_id": r["employee_id"], "org_id": ObjectId(org_id)})
    return jsonify(_response_to_json(r, {r["employee_id"]: emp} if emp else None))


@surveys_bp.route("/survey-responses/<response_id>", methods=["PATCH"])
def update_survey_response(response_id: str):
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    try:
        r = db.survey_responses.find_one(
            {"_id": ObjectId(response_id), "org_id": ObjectId(org_id)}
        )
    except InvalidId:
        return jsonify({"error": "invalid_id"}), 400
    if not r:
        return jsonify({"error": "not_found"}), 404

    data = request.get_json(silent=True) or {}
    if "answers" not in data:
        return jsonify({"error": "no_fields_to_update"}), 400

    t = _get_template(db, org_id, r["template_id"])
    if not t:
        return jsonify({"error": "template_not_found"}), 404

    answers, err = _validate_answers(t, data.get("answers"))
    if err:
        return jsonify({"error": err}), 400

    set_fields = {
        "answers": answers,
        "engagement_score": _engagement_score(t, answers),
        "updated_at": datetime.now(timezone.utc),
    }
    db.survey_responses.update_one({"_id": r["_id"]}, {"$set": set_fields})
    r = db.survey_responses.find_one({"_id": r["_id"]})

    _sync_engagement_score(db, org_id, r["employee_id"])

    emp = db.employees.find_one({"_id": r["employee_id"], "org_id": ObjectId(org_id)})
    log_audit_event(
        db, org_id, session.get("user_id"), session.get("user_name") or "",
        ACTION_SURVEY_RESPONSE_UPDATE,
        target_type="employee", target_id=str(r["employee_id"]),
        target_label=(emp or {}).get("employee_id") or str(r["employee_id"]),
        meta={"survey_response": str(r["_id"]), "engagement_score": set_fields["engagement_score"]},
    )
    return jsonify(_response_to_json(r, {r["employee_id"]: emp} if emp else None))


@surveys_bp.route("/survey-responses/<response_id>", methods=["DELETE"])
def delete_survey_response(response_id: str):
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    try:
        r = db.survey_responses.find_one(
            {"_id": ObjectId(response_id), "org_id": ObjectId(org_id)}
        )
    except InvalidId:
        return jsonify({"error": "invalid_id"}), 400
    if not r:
        return jsonify({"error": "not_found"}), 404

    db.survey_responses.delete_one({"_id": r["_id"]})

    # If the deleted response was the most recent one, the employee's
    # engagement signal must be re-derived from whatever remains.
    _sync_engagement_score(db, org_id, r["employee_id"])

    emp = db.employees.find_one({"_id": r["employee_id"], "org_id": ObjectId(org_id)})
    log_audit_event(
        db, org_id, session.get("user_id"), session.get("user_name") or "",
        ACTION_SURVEY_RESPONSE_DELETE,
        target_type="employee", target_id=str(r["employee_id"]),
        target_label=(emp or {}).get("employee_id") or str(r["employee_id"]),
        meta={"survey_response": str(r["_id"])},
    )
    return jsonify({"ok": True})
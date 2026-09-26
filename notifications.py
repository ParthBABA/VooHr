import logging
from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, jsonify, request

from employees import _require_auth, _employee_scope_filter, _NEVER_MATCH
from extensions import get_db
from field_encryption import decrypt_fields

notifications_bp = Blueprint("notifications", __name__)
logger = logging.getLogger(__name__)

# ── Notification categories ────────────────────────────────────────────
# The Notifications page groups rows into three tabs. The tab is decided by
# this explicit allowlist, never by "anything that isn't an activity", so a
# new notification type can't silently land under Risk Signals.
#   activity → background-job completions (translation / audio / dictation)
#   meeting  → meeting-tracker reminders, meeting changes, overdue items and
#              reminder-delivery problems
#   risk     → genuine risk-drift signals (also the fallback for unknown
#              types, which preserves the previous behaviour)
ACTIVITY_NOTIFICATION_TYPES = frozenset({
    "translation_ready", "audio_ready", "session_ready",
})
MEETING_NOTIFICATION_TYPES = frozenset({
    "meeting_reminder", "meeting_event", "memory_overdue", "delivery_failed",
})


def _notification_category(notif_type) -> str:
    """Map a notification ``type`` to its Notifications-page tab."""
    if notif_type in ACTIVITY_NOTIFICATION_TYPES:
        return "activity"
    if notif_type in MEETING_NOTIFICATION_TYPES:
        return "meeting"
    return "risk"


def _notification_scope_employee_ids(db, org_id: str):
    """Employee ObjectIds a manager may see notifications for.

    Mirrors ``meetings._meeting_scope_employee_ids`` — the same fail-closed
    helper the employee routes use. Returns:
      None → admin / unscoped: no employee filter, full org (unchanged).
      list → manager's reachable employee ObjectIds (own record + direct
             reports).
      []   → fail-closed: malformed/unknown scope matches nothing.
    """
    scope = _employee_scope_filter(db, org_id)
    if scope == _NEVER_MATCH:
        return []
    if not scope:
        return None
    reports_to = scope.get("reports_to")
    docs = db.employees.find(
        {
            "org_id": ObjectId(org_id),
            "$or": [{"reports_to": reports_to}, {"_id": reports_to}],
        },
        {"_id": 1},
    )
    return [d["_id"] for d in docs]


def _scoped_notification_filter(db, org_id, base):
    """AND an employee scope into a notification filter for manager sessions.

    Managers never see (or act on) notifications for employees outside their
    team; notifications without an employee are denied too (fail closed)."""
    allowed = _notification_scope_employee_ids(db, org_id)
    if allowed is None:
        return base
    if not allowed:
        base = dict(base)
        base["_id"] = None
        return base
    base = dict(base)
    base["employee_id"] = {"$in": allowed}
    return base


def _notification_to_json(n, employee_name="", employee_photo=None) -> dict:
    """Serialize a notifications doc for API responses.

    Includes everything a list row needs to render without a second call.
    `employee_name` is decrypted separately (PII fields can't be queried).

    `employee_photo` is the employee's base64 data-URL avatar, passed in only by
    callers that opt in (the bell panel, which renders at most a handful of
    rows). The list endpoint leaves it None unless `include_photo` is set, so
    the hub's 200-row pages don't carry megabytes of inline images.
    """
    return {
        "id": str(n["_id"]),
        "type": n.get("type", "risk_drift"),
        "category": _notification_category(n.get("type", "risk_drift")),
        "headline": n.get("headline", ""),
        "summary": n.get("summary", ""),
        "confidence": n.get("confidence", 0),
        "employee_id": str(n["employee_id"]) if n.get("employee_id") else None,
        "employee_name": employee_name or "",
        "employee_photo": employee_photo or None,
        "source_session_id": str(n["source_session_id"]) if n.get("source_session_id") else None,
        "meeting_id": str(n["meeting_id"]) if n.get("meeting_id") else None,
        "memory_id": str(n["memory_id"]) if n.get("memory_id") else None,
        "stage": n.get("stage"),
        "recipient_user_id": str(n["recipient_user_id"]) if n.get("recipient_user_id") else None,
        "delivery_status": n.get("delivery_status"),
        "delivery_channel": n.get("delivery_channel") or [],
        "delivery_errors": n.get("delivery_errors") or [],
        "attempts": n.get("attempts", 0),
        "last_attempt_at": n["last_attempt_at"].isoformat() if n.get("last_attempt_at") else None,
        "next_attempt_at": n["next_attempt_at"].isoformat() if n.get("next_attempt_at") else None,
        "event_key": n.get("event_key"),
        "dismissed": n.get("dismissed", False),
        "read": n.get("read", False),
        "created_at": n["created_at"].isoformat() if n.get("created_at") else None,
    }


def _employee_identity(db, org_id, employee_id) -> tuple:
    """Resolve an employee's display name and avatar photo in one lookup.

    The name lives in the encrypted PII blob; the photo is a plain field, so a
    single find_one covers both. Returns ("", None) when there is no employee
    (system notifications have no employee_id) or the row has gone away.
    """
    if not employee_id:
        return "", None
    emp = db.employees.find_one({"_id": employee_id, "org_id": ObjectId(org_id)})
    if not emp:
        return "", None
    pii = decrypt_fields(emp.get("encrypted"), emp.get("wrapped_dek", ""))
    return pii.get("name", ""), emp.get("photo")


@notifications_bp.route("/notifications")
def list_notifications():
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()

    limit = request.args.get("limit", default=20, type=int)
    limit = min(max(limit, 1), 50)
    page = request.args.get("page", default=1, type=int)
    page = max(page, 1)
    skip = (page - 1) * limit

    # Photos are inline base64 data-URLs (~20-40 KB each), so they are opt-in:
    # the bell panel asks for them to fill its avatars, while the hub pages
    # through up to 200 rows and would otherwise ship megabytes it never uses.
    include_photo = request.args.get("include_photo", "").lower() in ("1", "true", "yes")

    # unread_only narrows the returned rows to unread notifications. The bell
    # dropdown asks for it so a notification the user has already opened (or
    # cleared with "Mark all read") stops reappearing on every poll. The
    # /notifications hub deliberately omits it: that page is a full history and
    # must keep showing read rows too. Only the row filter changes — unread_count
    # below is always the org-wide unread total, either way.
    unread_only = request.args.get("unread_only", "").lower() in ("1", "true", "yes")

    base_filter = {"org_id": ObjectId(org_id)}
    if unread_only:
        base_filter["read"] = False
    query = _scoped_notification_filter(db, org_id, base_filter)
    notifications = list(
        db.notifications.find(query).sort("created_at", -1).skip(skip).limit(limit)
    )

    # Resolve employee names in a single pass — encrypted PII can't be queried
    # directly, so decrypt each matching employee once. Photos ride along on
    # the same documents, so this costs no extra round trip.
    emp_ids = {n.get("employee_id") for n in notifications if n.get("employee_id")}
    emp_names = {}
    emp_photos = {}
    if emp_ids:
        for emp in db.employees.find({"_id": {"$in": list(emp_ids)}, "org_id": ObjectId(org_id)}):
            pii = decrypt_fields(emp.get("encrypted"), emp.get("wrapped_dek", ""))
            emp_names[emp["_id"]] = pii.get("name", "")
            if include_photo:
                emp_photos[emp["_id"]] = emp.get("photo")

    result = [
        _notification_to_json(
            n,
            emp_names.get(n.get("employee_id"), ""),
            emp_photos.get(n.get("employee_id")),
        )
        for n in notifications
    ]

    unread_query = _scoped_notification_filter(
        db, org_id, {"org_id": ObjectId(org_id), "read": False}
    )
    unread_count = db.notifications.count_documents(unread_query)
    total = db.notifications.count_documents(query)

    logger.debug("notifications list: page=%d limit=%d total=%d unread=%d", page, limit, total, unread_count)

    return jsonify({
        "notifications": result,
        "total": total,
        "unread_count": unread_count,
        "page": page,
        "limit": limit,
    })


@notifications_bp.route("/notifications/<notification_id>")
def get_notification(notification_id: str):
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    try:
        n = db.notifications.find_one(
            _scoped_notification_filter(
                db, org_id, {"_id": ObjectId(notification_id), "org_id": ObjectId(org_id)}
            )
        )
    except InvalidId:
        return jsonify({"error": "invalid_id"}), 400

    if not n:
        return jsonify({"error": "not_found"}), 404

    employee_name, employee_photo = _employee_identity(db, org_id, n.get("employee_id"))

    data = _notification_to_json(n, employee_name, employee_photo)
    data["drift_explanation"] = n.get("drift_explanation", {})
    data["sessions_window"] = n.get("sessions_window", [])

    return jsonify(data)


@notifications_bp.route("/notifications/<notification_id>/read", methods=["PUT"])
def mark_read(notification_id: str):
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    try:
        nid = ObjectId(notification_id)
    except InvalidId:
        return jsonify({"error": "invalid_id"}), 400

    # Idempotent: updating an already-read notification still matches and
    # returns ok; only non-existent/foreign/out-of-scope ids 404.
    result = db.notifications.update_one(
        _scoped_notification_filter(
            db, org_id, {"_id": nid, "org_id": ObjectId(org_id)}
        ),
        {"$set": {"read": True, "read_at": datetime.now(timezone.utc)}},
    )
    if result.matched_count == 0:
        return jsonify({"error": "not_found"}), 404

    return jsonify({"ok": True})


@notifications_bp.route("/notifications/read-all", methods=["PUT"])
def mark_all_read():
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    result = db.notifications.update_many(
        _scoped_notification_filter(
            db, org_id, {"org_id": ObjectId(org_id), "read": False}
        ),
        {"$set": {"read": True, "read_at": datetime.now(timezone.utc)}},
    )

    logger.debug("mark_all_read: modified=%d", result.modified_count)

    return jsonify({"ok": True, "modified": result.modified_count})


@notifications_bp.route("/notifications/<notification_id>/dismiss", methods=["PUT"])
def dismiss_notification(notification_id: str):
    """Dismiss a reminder notification WITHOUT touching the underlying item.

    Dismissal is purely a notification-side read/dismissal state.  It never
    marks a commitment complete or an opener used — those are explicit,
    separate actions on the conversation_memory record.
    """
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    try:
        nid = ObjectId(notification_id)
    except InvalidId:
        return jsonify({"error": "invalid_id"}), 400

    result = db.notifications.update_one(
        _scoped_notification_filter(
            db, org_id, {"_id": nid, "org_id": ObjectId(org_id)}
        ),
        {"$set": {
            "dismissed": True,
            "read": True,
            "read_at": datetime.now(timezone.utc),
        }},
    )
    if result.matched_count == 0:
        return jsonify({"error": "not_found"}), 404

    # Confirm the underlying memory record was not altered.
    n = db.notifications.find_one(
        _scoped_notification_filter(db, org_id, {"_id": nid, "org_id": ObjectId(org_id)})
    )
    memory_id = str(n["memory_id"]) if n.get("memory_id") else None
    return jsonify({"ok": True, "dismissed": True, "memory_id": memory_id})

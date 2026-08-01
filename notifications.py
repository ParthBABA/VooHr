import sys
from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, jsonify, request

from employees import _require_auth
from extensions import get_db
from field_encryption import decrypt_fields

notifications_bp = Blueprint("notifications", __name__)


def _notification_to_json(n, employee_name="") -> dict:
    """Serialize a notifications doc for API responses.

    Includes everything a list row needs to render without a second call.
    `employee_name` is decrypted separately (PII fields can't be queried).
    """
    return {
        "id": str(n["_id"]),
        "type": n.get("type", "risk_drift"),
        "headline": n.get("headline", ""),
        "summary": n.get("summary", ""),
        "confidence": n.get("confidence", 0),
        "employee_id": str(n["employee_id"]) if n.get("employee_id") else None,
        "employee_name": employee_name or "",
        "source_session_id": str(n["source_session_id"]) if n.get("source_session_id") else None,
        "read": n.get("read", False),
        "created_at": n["created_at"].isoformat() if n.get("created_at") else None,
    }


def _employee_name(db, org_id, employee_id) -> str:
    """Decrypt an employee's name the same way _employee_to_json does."""
    if not employee_id:
        return ""
    emp = db.employees.find_one({"_id": employee_id, "org_id": ObjectId(org_id)})
    if not emp:
        return ""
    pii = decrypt_fields(emp.get("encrypted"), emp.get("wrapped_dek", ""))
    return pii.get("name", "")


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

    query = {"org_id": ObjectId(org_id)}
    notifications = list(
        db.notifications.find(query).sort("created_at", -1).skip(skip).limit(limit)
    )

    # Resolve employee names in a single pass — encrypted PII can't be queried
    # directly, so decrypt each matching employee once.
    emp_ids = {n.get("employee_id") for n in notifications if n.get("employee_id")}
    emp_names = {}
    if emp_ids:
        for emp in db.employees.find({"_id": {"$in": list(emp_ids)}, "org_id": ObjectId(org_id)}):
            pii = decrypt_fields(emp.get("encrypted"), emp.get("wrapped_dek", ""))
            emp_names[emp["_id"]] = pii.get("name", "")

    result = [_notification_to_json(n, emp_names.get(n.get("employee_id"), "")) for n in notifications]

    unread_count = db.notifications.count_documents(
        {"org_id": ObjectId(org_id), "read": False}
    )
    total = db.notifications.count_documents(query)

    print(
        f"[DEBUG_NOTIF] list org={org_id} page={page} limit={limit} "
        f"total={total} unread={unread_count}",
        file=sys.stderr,
    )

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
            {"_id": ObjectId(notification_id), "org_id": ObjectId(org_id)}
        )
    except InvalidId:
        return jsonify({"error": "invalid_id"}), 400

    if not n:
        return jsonify({"error": "not_found"}), 404

    employee_name = _employee_name(db, org_id, n.get("employee_id"))

    data = _notification_to_json(n, employee_name)
    data["drift_explanation"] = n.get("drift_explanation", {})
    data["sessions_window"] = n.get("sessions_window", [])

    print(f"[DEBUG_NOTIF] get id={notification_id} org={org_id} employee={n.get('employee_id')}", file=sys.stderr)

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
    # returns ok; only non-existent/foreign ids 404.
    result = db.notifications.update_one(
        {"_id": nid, "org_id": ObjectId(org_id)},
        {"$set": {"read": True, "read_at": datetime.now(timezone.utc)}},
    )
    if result.matched_count == 0:
        return jsonify({"error": "not_found"}), 404

    print(f"[DEBUG_NOTIF] mark_read id={notification_id} org={org_id}", file=sys.stderr)

    return jsonify({"ok": True})


@notifications_bp.route("/notifications/read-all", methods=["PUT"])
def mark_all_read():
    org_id = _require_auth()
    if not org_id:
        return jsonify({"error": "not_authenticated"}), 401

    db = get_db()
    result = db.notifications.update_many(
        {"org_id": ObjectId(org_id), "read": False},
        {"$set": {"read": True, "read_at": datetime.now(timezone.utc)}},
    )

    print(f"[DEBUG_NOTIF] mark_all_read org={org_id} modified={result.modified_count}", file=sys.stderr)

    return jsonify({"ok": True, "modified": result.modified_count})

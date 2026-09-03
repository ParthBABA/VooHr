"""Organization audit log — append-only record of sensitive admin actions.

Every entry is a row in the ``audit_log`` collection keyed by ``org_id`` so
any org only ever sees its own history (tenant isolation).  Audit writes are
fail-open: a logging failure must never block the request that triggered it,
so ``log_audit_event`` swallows and logs exceptions instead of raising.
"""

import logging
from datetime import datetime, timezone

from bson import ObjectId
from flask import Blueprint, jsonify, request, session

from extensions import get_db
from login_flow import _hash_session_token

log = logging.getLogger(__name__)

audit_bp = Blueprint("audit", __name__)

# ── Action constants ───────────────────────────────────────────────────
# Machine-readable action identifiers stored in each audit row.  Keep these
# stable — the frontend ACTION_LABELS map in static/settings.html translates
# these to human-readable text, and any change here must be mirrored there.
ACTION_EMPLOYEE_CREATE = "employee.create"
ACTION_EMPLOYEE_UPDATE = "employee.update"
ACTION_EMPLOYEE_DELETE = "employee.delete"
ACTION_EMPLOYEE_EXPORT = "employee.export"
ACTION_EMPLOYEE_BULK_IMPORT = "employee.bulk_import"
ACTION_EMPLOYEE_BULK_EXPORT = "employee.bulk_export"
ACTION_ORG_UPDATE = "organization.update"
ACTION_TOTP_ENABLE = "totp.enable"
ACTION_TOTP_DISABLE = "totp.disable"
ACTION_TOTP_BACKUP_CODES_REGENERATE = "totp.backup_codes_regenerate"
ACTION_SESSION_REVOKE = "session.revoke"
ACTION_ACCOUNT_EXPORT = "account.export"
ACTION_ACCOUNT_DELETE = "account.delete"
ACTION_MANAGER_INVITE_SENT = "manager.invite_sent"
ACTION_MANAGER_INVITE_ACCEPTED = "manager.invite_accepted"
ACTION_MANAGER_INVITE_SUPERSEDED = "manager.invite_superseded"

# Pagination for the audit-log endpoint.
DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 200


def log_audit_event(db, org_id, actor_user_id, actor_name, action,
                    target_type=None, target_id=None, target_label=None, meta=None):
    """Append one entry to the org's audit log.

    Never raises — audit logging is best-effort and must not break the
    request that triggered it.  Call after the underlying DB write has
    succeeded.

    Args:
        db: the Mongo database handle.
        org_id: owning organization (str ObjectId, ObjectId, or None for
            user-scoped actions where the org is resolved by the caller).
        actor_user_id: id of the actor performing the action (string or ObjectId).
        actor_name: display name of the actor.
        action: one of the ACTION_* constants above.
        target_type: kind of entity acted upon, e.g. "employee", "session".
        target_id: id of the target entity.
        target_label: human-readable label of the target, e.g. a device label.
        meta: dict of extra structured detail (e.g. self_revoke for sessions).
    """
    try:
        record = {
            "org_id": _to_oid(org_id),
            "actor_user_id": str(actor_user_id) if actor_user_id else None,
            "actor_name": actor_name or "",
            "action": action,
            "target_type": target_type,
            "target_id": str(target_id) if target_id else None,
            "target_label": target_label,
            "meta": meta or {},
            "created_at": datetime.now(timezone.utc),
        }
        db.audit_log.insert_one(record)
    except Exception:
        log.exception("Failed to write audit log entry (action=%s)", action)


def _to_oid(value):
    """Normalize an org/user id that may be a string or ObjectId to ObjectId
    when possible; otherwise return None.  Keeps storage types consistent so
    per-org filtering works regardless of how the caller passed the id."""
    if value is None:
        return None
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _require_admin():
    """Admin-only session check for the audit-log endpoint.

    Returns the authenticated user document (admin) on success, None when
    unauthenticated / not an admin.  Reuses the same active-session
    validation as the rest of the API but stays self-contained here to avoid
    a circular import with employees.py (which imports audit_log)."""
    user_id = session.get("user_id")
    if not user_id:
        return None
    try:
        ObjectId(user_id)
    except Exception:
        session.clear()
        return None
    db = get_db()
    user = db.users.find_one(
        {"_id": ObjectId(user_id)},
        {"role": 1, "org_id": 1},
    )
    if not user:
        session.clear()
        return None
    if not _session_is_active(db, user_id, session.get("session_token")):
        session.clear()
        return None
    if user.get("role") != "admin":
        return None
    return user


def _session_is_active(db, user_id, session_token):
    """Minimal active-session check (mirrors employees._session_is_active)
    so the audit endpoint doesn't have to import employees (circular)."""
    if not user_id or not session_token:
        return False
    try:
        rec = db.active_sessions.find_one(
            {"user_id": ObjectId(user_id), "session_token": _hash_session_token(session_token)}
        )
    except Exception:
        return False
    return rec is not None


@audit_bp.route("/audit-log")
def list_audit_log():
    """Return the org's audit log, admin-only and paginated.

    Query params: page (1-based, default 1), limit (default 50, max 200).
    Response: {"events": [...], "total", "page", "limit", "has_more"}.
    Entries are returned newest-first.
    """
    user = _require_admin()
    if not user:
        return jsonify({"error": "not_authenticated"}), 401

    org_id = user.get("org_id")
    if not org_id:
        return jsonify({"error": "forbidden"}), 403

    try:
        page = max(1, int(request.args.get("page", 1) or 1))
    except ValueError:
        page = 1
    try:
        limit = int(request.args.get("limit", DEFAULT_PAGE_LIMIT) or DEFAULT_PAGE_LIMIT)
    except ValueError:
        limit = DEFAULT_PAGE_LIMIT
    limit = min(max(1, limit), MAX_PAGE_LIMIT)

    db = get_db()
    query = {"org_id": _to_oid(org_id)}
    total = db.audit_log.count_documents(query)
    docs = (
        db.audit_log.find(query)
        .sort("created_at", -1)
        .skip((page - 1) * limit)
        .limit(limit)
    )

    events = []
    for d in docs:
        created = d.get("created_at")
        events.append({
            "id": str(d["_id"]),
            "action": d.get("action"),
            "actor_user_id": d.get("actor_user_id"),
            "actor_name": d.get("actor_name"),
            "target_type": d.get("target_type"),
            "target_id": d.get("target_id"),
            "target_label": d.get("target_label"),
            "meta": d.get("meta") or {},
            "created_at": created.isoformat() if created else None,
        })

    offset = (page - 1) * limit
    has_more = total > (offset + len(events))

    return jsonify({
        "events": events,
        "total": total,
        "page": page,
        "limit": limit,
        "has_more": has_more,
    })

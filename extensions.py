import logging
from datetime import datetime, timedelta, timezone

from flask import current_app, request
from pymongo import MongoClient, ASCENDING, DESCENDING, TEXT
from pymongo.errors import DuplicateKeyError

log = logging.getLogger(__name__)


def init_db(app):
    """Create a single MongoClient for the app's lifetime and stash the
    database handle on app.extensions so blueprints can look it up lazily
    via get_db(), without needing MONGODB_URI to be valid at import time.
    """
    client = MongoClient(app.config["MONGODB_URI"]) if app.config.get("MONGODB_URI") else None
    app.extensions["mongo_client"] = client
    app.extensions["mongo_db"] = client[app.config["MONGODB_DB"]] if client is not None else None

    if client is not None:
        db = client[app.config["MONGODB_DB"]]
        _cleanup_expired_pending_totp(db)
        _init_rate_limits(db)
        _init_indexes(db)


def _cleanup_expired_pending_totp(db):
    """Remove stale pending_totp_secret / pending_totp_secret_expires
    fields from user documents whose expiry is in the past.  Runs once
    at startup so abandoned enrollments don't linger forever.
    """
    db.users.update_many(
        {"pending_totp_secret_expires": {"$lt": datetime.now(timezone.utc)}},
        {"$unset": {
            "pending_totp_secret": "",
            "pending_totp_secret_expires": "",
        }},
    )


# ── Rate-limit helpers (shared by auth_email and totp_routes) ─────────

def _init_rate_limits(db):
    """Ensure the rate_limits collection has a TTL index so old events are
    automatically garbage-collected by MongoDB.
    """
    db.rate_limits.create_index(
        [("key", ASCENDING), ("ts", ASCENDING)],
        background=True,
    )
    db.rate_limits.create_index(
        "expire_at",
        expireAfterSeconds=0,
        background=True,
    )


# ── MongoDB indexes ───────────────────────────────────────────────────

def _init_indexes(db):
    """Create all application indexes.  Idempotent: safe to run on every
    startup.  ``create_index`` is a no-op when an equivalent index already
    exists.

    Indexes are derived from actual query patterns observed in the codebase:
      - users:           email_hash lookups (login, signup), _id default
      - employees:       org_id filtered lists, compound (org_id, employee_id)
      - sessions:        org_id lists, compound with employee_id + created_at
      - notifications:   org_id lists with read/unread filter, created_at sort
      - active_sessions: user_id lookup, session_token lookup, TTL cleanup
      - rate_limits:     (already created in _init_rate_limits)
      - counters:        _id-only for atomic employee ID generation
    """

    # ── users ──────────────────────────────────────────────────────────
    # Query: find_one({"email_hash": ...}) in auth.py, auth_email.py
    try:
        db.users.create_index("email_hash", unique=True, background=True)
    except DuplicateKeyError:
        log.critical(
            "FAILED to create unique index on users.email_hash because "
            "duplicate email_hash records already exist. "
            "Run 'python find_duplicates.py' to identify the duplicates. "
            "The application will continue to start, but signup/login "
            "may fail until duplicates are resolved. "
            "DO NOT weaken the index — resolve the duplicate data instead."
        )

    # ── employees ──────────────────────────────────────────────────────
    # Query: find({org_id, ...}).sort("created_at", -1)  in list_employees
    db.employees.create_index(
        [("org_id", ASCENDING), ("created_at", DESCENDING)],
        background=True,
    )
    # Query: _next_employee_id — sort by employee_id desc within org
    # (now also enforced as unique constraint via atomic counter)
    db.employees.create_index(
        [("org_id", ASCENDING), ("employee_id", ASCENDING)],
        unique=True,
        background=True,
    )

    # ── sessions ───────────────────────────────────────────────────────
    # Query: find({org_id}).sort("created_at", -1) in list_sessions
    db.sessions.create_index(
        [("org_id", ASCENDING), ("created_at", DESCENDING)],
        background=True,
    )
    # Query: find({org_id, employee_id, ...}) in list_sessions, analyze drift
    db.sessions.create_index(
        [("employee_id", ASCENDING), ("created_at", DESCENDING)],
        background=True,
    )

    # ── notifications ──────────────────────────────────────────────────
    # Query: find({org_id}).sort("created_at", -1) in list_notifications
    db.notifications.create_index(
        [("org_id", ASCENDING), ("created_at", DESCENDING)],
        background=True,
    )
    # Query: count_documents({org_id, read: False}) in list_notifications
    db.notifications.create_index(
        [("org_id", ASCENDING), ("read", ASCENDING)],
        background=True,
    )
    # Reminder dedup: unique per (org, meeting, memory, stage). Partial so the
    # index only covers reminder documents (meeting_reminder type).
    db.notifications.create_index(
        [
            ("org_id", ASCENDING),
            ("meeting_id", ASCENDING),
            ("memory_id", ASCENDING),
            ("stage", ASCENDING),
        ],
        unique=True,
        partialFilterExpression={"type": "meeting_reminder"},
        background=True,
    )

    # ── meetings ───────────────────────────────────────────────────────
    # Query: find({org_id}).sort("scheduled_at", 1) in list_meetings
    db.meetings.create_index(
        [("org_id", ASCENDING), ("scheduled_at", ASCENDING)],
        background=True,
    )
    # Query: find({org_id, employee_id}) for per-employee meeting lists
    db.meetings.create_index(
        [("org_id", ASCENDING), ("employee_id", ASCENDING)],
        background=True,
    )

    # ── conversation_memory ────────────────────────────────────────────
    # Query: find({org_id, employee_id}).sort("created_at", 1)
    db.conversation_memory.create_index(
        [("org_id", ASCENDING), ("employee_id", ASCENDING), ("created_at", ASCENDING)],
        background=True,
    )
    # Query: find({org_id, session_id}) for previous-session memory
    db.conversation_memory.create_index(
        [("org_id", ASCENDING), ("session_id", ASCENDING)],
        background=True,
    )

    # ── active_sessions ────────────────────────────────────────────────
    # Query: find_one({user_id, session_token}) in _session_is_active
    db.active_sessions.create_index(
        [("user_id", ASCENDING), ("session_token", ASCENDING)],
        background=True,
    )
    # Query: find({user_id}).sort("last_seen", -1) in list_active_sessions
    db.active_sessions.create_index(
        [("user_id", ASCENDING), ("last_seen", DESCENDING)],
        background=True,
    )

    # ── otp_verifications ──────────────────────────────────────────────
    # Query: find_one({email_hash}) in auth_email.py
    db.otp_verifications.create_index("email_hash", background=True)

    # ── audit_log ──────────────────────────────────────────────────────
    # Query: find({org_id}).sort("created_at", -1) in the /api/audit-log route
    db.audit_log.create_index(
        [("org_id", ASCENDING), ("created_at", DESCENDING)],
        background=True,
    )

    # ── invites ────────────────────────────────────────────────────────
    # Query: find_one({"token": ...}) in auth.invite_accept / google_callback
    db.invites.create_index("token", unique=True, background=True)
    # Query: admin lookup of pending invites per employee
    db.invites.create_index(
        [("org_id", ASCENDING), ("linked_employee_id", ASCENDING)],
        background=True,
    )


# ── Atomic employee ID generation ────────────────────────────────────

def next_employee_id(db, org_id: str) -> str:
    """Generate the next employee ID for an organization atomically.

    Uses a MongoDB ``counters`` collection with ``$inc`` + ``upsert`` to
    guarantee uniqueness even under concurrent requests.

    Counter document structure:
        { "_id": "employee:<org_id>", "seq": 1 }

    Returns the formatted employee ID (e.g. "EMP001").
    """
    counter_id = f"employee:{org_id}"
    result = db.counters.find_one_and_update(
        {"_id": counter_id},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=True,  # return the document AFTER the update
    )
    seq = result["seq"]
    return f"EMP{seq:03d}"


def check_rate_limit(db, key, max_events, window_seconds):
    """Return (allowed: bool, retry_after: float | None).

    Counts events for *key* within the sliding *window_seconds* window.
    If the count >= *max_events*, returns (False, seconds_until_window_expires).
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=window_seconds)
    count = db.rate_limits.count_documents(
        {"key": key, "ts": {"$gt": cutoff}},
    )
    if count >= max_events:
        oldest = db.rate_limits.find_one(
            {"key": key, "ts": {"$gt": cutoff}},
            sort=[("ts", 1)],
        )
        if oldest:
            remaining = (oldest["ts"] + timedelta(seconds=window_seconds) - now).total_seconds()
            return False, max(1, int(remaining) + 1)
        return False, window_seconds
    return True, None


def record_rate_limit_event(db, key, ttl_seconds=3600):
    """Insert a timestamped event for the given rate-limit key.  The
    ``expire_at`` field is indexed with a TTL so MongoDB auto-deletes it.
    """
    now = datetime.now(timezone.utc)
    db.rate_limits.insert_one({
        "key": key,
        "ts": now,
        "expire_at": now + timedelta(seconds=ttl_seconds),
    })


def _is_private_ip(ip) -> bool:
    """True for addresses that are never globally routable and therefore can
    only be a proxy/edge hop rather than a direct client: loopback, RFC1918
    private ranges, RFC6598 CGNAT, IPv6 and link-local."""
    if not ip:
        return True
    if ip == "::1" or ":" in ip:
        return True
    try:
        parts = [int(p) for p in ip.split(".")]
    except ValueError:
        return True
    if len(parts) != 4:
        return True
    a, b, _c, _d = parts
    if a == 10 or a == 127:
        return True
    if a == 172 and 16 <= b <= 31:
        return True
    if a == 192 and b == 168:
        return True
    # RFC6598 shared address space (CGNAT / Tailscale) — not routable.
    if a == 100 and 64 <= b <= 127:
        return True
    # Link-local (APIPA) — never routable.
    if a == 169 and b == 254:
        return True
    return False


def client_ip():
    """Return the real client IP for rate-limiting / CSRF/IP-keying.

    Trust boundary (do not regress): X-Forwarded-For is ONLY trusted when the
    request actually arrived through our known reverse proxy (Render/Railway
    terminate TLS and forward to us, so remote_addr is a private hop there).
    A proxy APPENDS the client IP to the XFF chain, while any client can plant
    arbitrary values at the FRONT of the header — so blindly trusting the
    leftmost entry lets an attacker rotate fake IPs to bypass rate limits on
    login, OTP, CSRF token generation, etc.  We therefore:
      1. If the direct peer (remote_addr) is a PUBLIC address, the TCP
         connection itself identifies the client — trust remote_addr only.
      2. Otherwise the peer is a trusted proxy — walk the XFF chain RIGHT to
         LEFT, skipping private/proxy hops, and return the first public
         address the proxy appended.  Leftmost (client-planted) entries are
         ignored.
      3. If no trusted-proxy signal yields an IP, fall back to remote_addr.
    """
    peer = request.remote_addr or ""
    if peer and not _is_private_ip(peer):
        # Direct, publicly-reachable client — no proxy involved. Trust the
        # TCP peer only; never read XFF from an arbitrary caller.
        return peer
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        for candidate in reversed([c.strip() for c in forwarded.split(",")]):
            if candidate and not _is_private_ip(candidate):
                return candidate
    # No trusted-proxy signal → use the direct peer.
    return peer or "unknown"


def get_db():
    db = current_app.extensions.get("mongo_db")
    if db is None:
        raise RuntimeError(
            "MongoDB is not configured. Set MONGODB_URI in your environment / .env file."
        )
    return db

from datetime import datetime, timedelta, timezone

from flask import current_app
from pymongo import MongoClient, ASCENDING


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


def get_db():
    db = current_app.extensions.get("mongo_db")
    if db is None:
        raise RuntimeError(
            "MongoDB is not configured. Set MONGODB_URI in your environment / .env file."
        )
    return db

"""Self-hosted, in-process scheduler for background reminder generation.

Runs inside the same web process (no external cron / cron-job.org service
needed) and is safe under gunicorn's ``--workers 2``: every worker process
starts its own APScheduler ``BackgroundScheduler`` on the same 15-minute
interval, but each sweep must first claim a short-lived distributed lock stored
as a document in the MongoDB ``scheduler_locks`` collection. Only the worker
that atomically claims/refreshes the lock does the work; the others log a skip
and wait for the next tick — so a given reminder is generated (and its email /
WhatsApp sent) exactly once per interval, never once per worker.

Crash safety: the lock is never explicitly released. It is just a timestamp
(``locked_until``) that goes stale after ``LOCK_TTL`` (10 minutes), so a dead
worker can never leave the lock stuck; the next tick reclaims it.
"""
import logging
import os
import threading
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

import reminders
from extensions import get_db

logger = logging.getLogger(__name__)

#: Value of scheduler_locks.job that identifies the reminder sweep lock.
JOB_RUN_KEY = "reminder_sweep"

#: How old a lock may be before another process may reclaim it. This is the
#: short-lived window: a crashed worker leaves a stale timestamp, but that
#: timestamp naturally expires and the next tick proceeds.
LOCK_TTL = timedelta(
    minutes=max(1, int(os.environ.get("REMINDER_SWEEP_LOCK_TTL_MINUTES", "10")))
)

#: How often the in-process sweep runs (default: every 15 minutes).
SWEEP_INTERVAL_MINUTES = max(
    1, int(os.environ.get("REMINDER_SWEEP_INTERVAL_MINUTES", "15"))
)

#: Module-level guard so the scheduler is registered at most once per worker
#: process, even if Flask's debug reloader or an import path executes
#: create_app() (and therefore start_scheduler()) more than once.
_scheduler_started = False
_scheduler_start_lock = threading.Lock()

#: Cache the (idempotent) lock-index creation to once per process.
_lock_index_ensured = False


def _now():
    return datetime.now(timezone.utc)


def _ensure_lock_index(db):
    """Create the unique index backing the distributed lock, once per process.

    The unique constraint on scheduler_locks.job is what makes the upsert in
    acquire_sweep_lock() atomic across workers: when a fresh lock already
    exists, a competing upsert cannot insert a second row and instead fails
    with DuplicateKeyError.
    """
    global _lock_index_ensured
    if _lock_index_ensured:
        return
    try:
        db.scheduler_locks.create_index("job", unique=True)
        _lock_index_ensured = True
    except Exception:
        logger.exception("reminder scheduler: could not ensure unique index on scheduler_locks.job")


def acquire_sweep_lock(db, now=None) -> bool:
    """Atomically claim (or refresh) the distributed reminder-sweep lock.

    findAndModify/upsert pattern: we only proceed when no lock document exists
    for ``JOB_RUN_KEY`` with a ``locked_until`` newer than ``now - LOCK_TTL``.
    A missing lock is inserted; a stale lock is atomically refreshed with the
    current timestamp. Both paths are atomic in MongoDB, so exactly one worker
    wins each interval. A fresh lock owned by another process leaves the upsert
    unable to insert (unique ``job`` index -> DuplicateKeyError) and we return
    False without doing any work.

    ``now`` is injectable for tests.
    """
    now = now or _now()
    cutoff = now - LOCK_TTL
    try:
        result = db.scheduler_locks.find_one_and_update(
            {"job": JOB_RUN_KEY, "locked_until": {"$lt": cutoff}},
            {
                "$set": {
                    "locked_until": now + LOCK_TTL,
                    "locked_at": now,
                    "holder_pid": os.getpid(),
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        logger.debug("reminder scheduler: lock race lost on upsert, skipping")
        return False
    return result is not None


def _enumerate_org_ids(db):
    """Every org that could need reminders, using the same sources the rest of
    the codebase reads org ids from:

      - every document in ``organizations`` (the canonical org registry used by
        auth/api/employees/sessions), and
      - every org_id with a still-scheduled meeting (mirrors the org scan in
        reminders.sweep_all_orgs) so data predating an org row is still swept.

    Returns a sorted list of org_id strings.
    """
    org_ids: set = set()
    try:
        for org in db.organizations.find({}, {"_id": 1}):
            oid = org.get("_id")
            if oid is not None:
                org_ids.add(str(oid))
    except Exception:
        logger.exception("reminder scheduler: organizations org scan failed")
    try:
        for m in db.meetings.find({"status": "scheduled"}, {"org_id": 1}):
            oid = m.get("org_id")
            if oid is not None:
                org_ids.add(str(oid))
    except Exception:
        logger.exception("reminder scheduler: meetings org scan failed")
    return sorted(org_ids)


def run_reminder_sweep(db=None, now=None) -> dict:
    """One reminder-generation sweep, guarded by the distributed lock.

    (a) Try to acquire the short-lived scheduler_locks document. If another
        worker/process already holds a fresh lock, log a skip and return
        without doing any work.
    (b) If acquired, enumerate every org_id and call
        reminders.ensure_reminder_notifications(db, org_id, now) for each,
        then reminders.retry_pending_deliveries(db, org_id, now) so earlier
        failed deliveries are retried with bounded backoff on this same tick,
        catching and logging per-org exceptions so one broken org never stops
        the rest.
    (c) Let the lock expire naturally (timestamp staleness) — there is no
        explicit release, so a crashed worker cannot leave the lock stuck.

    ``db``/``now`` are injectable for tests.
    """
    db = db or get_db()
    now = now or _now()
    _ensure_lock_index(db)

    if not acquire_sweep_lock(db, now):
        holder = None
        try:
            lock = db.scheduler_locks.find_one({"job": JOB_RUN_KEY})
            if lock:
                holder = lock.get("holder_pid")
        except Exception:
            pass
        logger.info(
            "reminder scheduler: skipped — lock held by pid=%s, waiting for next tick",
            holder or "another worker",
        )
        return {"acquired": False, "orgs": 0, "created": 0, "retried": 0}

    org_ids = _enumerate_org_ids(db)
    created = 0
    retried = 0
    for org_id in org_ids:
        try:
            created += reminders.ensure_reminder_notifications(db, org_id, now)
        except Exception:
            logger.exception("reminder scheduler: reminder generation failed org=%s", org_id)
        try:
            retried += reminders.retry_pending_deliveries(db, org_id, now)
        except Exception:
            logger.exception("reminder scheduler: retry_pending_deliveries failed org=%s", org_id)
    logger.info(
        "reminder scheduler: lock acquired pid=%s orgs=%d created=%d retried=%d",
        os.getpid(), len(org_ids), created, retried,
    )
    return {"acquired": True, "orgs": len(org_ids), "created": created, "retried": retried}


def start_scheduler(app):
    """Start the in-process APScheduler used for reminder sweeps.

    Called once from create_app(). A module-level flag guarantees it is
    registered at most once per worker process, so Flask's debug reloader or
    any import mechanism that re-executes create_app() cannot start a second
    scheduler in the same worker.
    """
    global _scheduler_started
    with _scheduler_start_lock:
        if _scheduler_started:
            logger.debug("reminder scheduler: already started in this process, skipping")
            return

        try:
            db = app.extensions.get("mongo_db")
            if db is not None:
                _ensure_lock_index(db)
        except Exception:
            logger.exception("reminder scheduler: lock index setup failed")

        def _timed_job():
            # APScheduler runs outside Flask's request/context machinery, so
            # push an app context so get_db() resolves for this worker.
            with app.app_context():
                run_reminder_sweep()

        scheduler = BackgroundScheduler(daemon=True, timezone=timezone.utc)
        scheduler.add_job(
            _timed_job,
            trigger=IntervalTrigger(minutes=SWEEP_INTERVAL_MINUTES),
            id="reminder_sweep",
            name="reminder_sweep",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        scheduler.start()

        _scheduler_started = True
        logger.info(
            "reminder scheduler: started pid=%d interval=%dmin (in-process, "
            "distributed lock via scheduler_locks)",
            os.getpid(), SWEEP_INTERVAL_MINUTES,
        )
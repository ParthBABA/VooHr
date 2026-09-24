"""
Self-hosted in-process scheduler (APScheduler) — distributed-lock unit tests.

Uses the same hand-rolled in-memory Mongo facade idiom as the rest of the
suite (no live DB). Verifies that run_reminder_sweep()

  - acquires the scheduler_locks lock and runs ensure_reminder_notifications()
    and retry_pending_deliveries() for every org when no lock exists yet,
  - skips cleanly (no reminder generation at all) when another process already
    holds a fresh lock, and
  - reclaims an expired/stale lock and runs the sweep again.
"""
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

import pytest

import scheduler as scheduler_mod

ORG_A = "aaaaaaaaaaaaaaaaaaaaaaaa"
ORG_B = "bbbbbbbbbbbbbbbbbbbbbbbb"

# Fixed "now" for deterministic lock-timing assertions (UTC).
NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)


class FakeCollection:
    def __init__(self):
        self._docs = []
        self._unique_keys = set()

    def _matches(self, doc, filt):
        for k, v in filt.items():
            if isinstance(v, dict):
                if "$lt" in v and not (doc.get(k) is not None and doc.get(k) < v["$lt"]):
                    return False
                if "$gt" in v and not (doc.get(k) is not None and doc.get(k) > v["$gt"]):
                    return False
                continue
            if doc.get(k) != v:
                return False
        return True

    def _insert(self, doc):
        d = dict(doc)
        for uk in self._unique_keys:
            if any(all(existing.get(k) == d.get(k) for k in uk) for existing in self._docs):
                raise DuplicateKeyError(f"duplicate key {uk}")
        d["_id"] = d.get("_id") or ObjectId()
        self._docs.append(d)
        return d

    def find_one(self, filt=None, *args, **kw):
        filt = filt or {}
        for d in self._docs:
            if self._matches(d, filt):
                return dict(d)
        return None

    def find(self, filt=None, *args, **kw):
        filt = filt or {}
        wrapped = [dict(d) for d in self._docs if self._matches(d, filt)]

        class Cursor:
            def __iter__(self):
                return iter(wrapped)

        return Cursor()

    def insert_one(self, doc):
        d = self._insert(doc)
        return type("R", (), {"inserted_id": d["_id"]})()

    def update_one(self, filt, update):
        for d in self._docs:
            if self._matches(d, filt):
                if "$set" in update:
                    d.update(update["$set"])
                return type("R", (), {"matched_count": 1})
        return type("R", (), {"matched_count": 0})()

    def find_one_and_update(self, filt, update, upsert=False, return_document=None):
        for d in self._docs:
            if self._matches(d, filt):
                if "$set" in update:
                    d.update(update["$set"])
                if "$setOnInsert" in update:
                    for k, v in update["$setOnInsert"].items():
                        d.setdefault(k, v)
                return dict(d)
        if upsert:
            new = {k: v for k, v in filt.items() if not isinstance(v, dict)}
            for k, v in (update.get("$set") or {}).items():
                new[k] = v
            for k, v in (update.get("$setOnInsert") or {}).items():
                new.setdefault(k, v)
            return dict(self._insert(new))
        return None

    def count_documents(self, filt):
        return sum(1 for d in self._docs if self._matches(d, filt))

    def create_index(self, key, unique=False, background=False, **kw):
        if unique:
            keys = (key,) if isinstance(key, str) else tuple(f[0] for f in key)
            self._unique_keys.add(keys)
        return None


class FakeDB:
    def __init__(self):
        self.organizations = FakeCollection()
        self.meetings = FakeCollection()
        self.sessions = FakeCollection()
        self.users = FakeCollection()
        self.employees = FakeCollection()
        self.conversation_memory = FakeCollection()
        self.notifications = FakeCollection()
        self.scheduler_locks = FakeCollection()


@pytest.fixture
def fake():
    # The module-level index flag is process-wide; force the fake index to be
    # re-registered on this per-test DB so insert-race behavior is exercised.
    scheduler_mod._lock_index_ensured = False
    db = FakeDB()
    db.organizations.insert_one({"_id": ObjectId(ORG_A), "name": "Acme"})
    db.organizations.insert_one({"_id": ObjectId(ORG_B), "name": "Beta"})
    return db


def _seed_lock(db, at, holder=9999):
    db.scheduler_locks.insert_one({
        "job": scheduler_mod.JOB_RUN_KEY,
        "locked_at": at,
        "locked_until": at + scheduler_mod.LOCK_TTL,
        "holder_pid": holder,
    })


def _spy_generation(monkeypatch):
    calls = []

    def fake_gen(db, org_id, now=None):
        calls.append(str(org_id))
        return 1

    monkeypatch.setattr(
        scheduler_mod.reminders, "ensure_reminder_notifications", fake_gen
    )
    monkeypatch.setattr(
        scheduler_mod.reminders, "retry_pending_deliveries",
        lambda db, org_id, now=None: 0,
    )
    return calls


def test_acquires_lock_and_sweeps_every_org(fake, monkeypatch):
    calls = _spy_generation(monkeypatch)

    result = scheduler_mod.run_reminder_sweep(fake, NOW)

    assert result == {"acquired": True, "orgs": 2, "created": 2, "retried": 0}
    assert calls == [ORG_A, ORG_B]

    lock = fake.scheduler_locks.find_one({"job": scheduler_mod.JOB_RUN_KEY})
    assert lock is not None
    assert lock["locked_at"] == NOW
    assert lock["locked_until"] == NOW + scheduler_mod.LOCK_TTL
    assert lock["holder_pid"] is not None


def test_skips_when_fresh_lock_held_by_another_process(fake, monkeypatch):
    # A "fresh" lock: still valid well past the 10-minute TTL at our run time.
    _seed_lock(fake, NOW, holder=9999)
    calls = _spy_generation(monkeypatch)

    result = scheduler_mod.run_reminder_sweep(fake, NOW + timedelta(minutes=1))

    assert result == {"acquired": False, "orgs": 0, "created": 0, "retried": 0}
    assert calls == []

    # The other worker's lock is untouched — we neither refreshed nor replaced it.
    lock = fake.scheduler_locks.find_one({"job": scheduler_mod.JOB_RUN_KEY})
    assert lock is not None
    assert lock["holder_pid"] == 9999
    assert lock["locked_at"] == NOW


def test_reclaims_expired_lock(fake, monkeypatch):
    # A stale lock: its locked_until (20+ min ago) is older than now - LOCK_TTL.
    _seed_lock(fake, NOW - 2 * scheduler_mod.LOCK_TTL - timedelta(minutes=1), holder=7777)
    calls = _spy_generation(monkeypatch)

    result = scheduler_mod.run_reminder_sweep(fake, NOW)

    assert result["acquired"] is True
    assert calls == [ORG_A, ORG_B]

    # The stale lock was atomically refreshed by this worker.
    lock = fake.scheduler_locks.find_one({"job": scheduler_mod.JOB_RUN_KEY})
    assert lock is not None
    assert lock["holder_pid"] != 7777
    assert lock["locked_at"] == NOW
    assert lock["locked_until"] == NOW + scheduler_mod.LOCK_TTL


def test_single_broken_org_does_not_stop_the_sweep(fake, monkeypatch):
    def fake_gen(db, org_id, now=None):
        if str(org_id) == ORG_B:
            raise RuntimeError("boom")
        return 1

    calls = []

    def fake_gen_both(db, org_id, now=None):
        calls.append(str(org_id))
        return fake_gen(db, org_id, now)

    monkeypatch.setattr(
        scheduler_mod.reminders, "ensure_reminder_notifications", fake_gen_both
    )

    result = scheduler_mod.run_reminder_sweep(fake, NOW)

    assert result["acquired"] is True
    assert result["orgs"] == 2
    assert result["created"] == 1
    assert calls == [ORG_A, ORG_B]


def test_run_reminder_sweep_also_retries_failed_deliveries(fake, monkeypatch):
    created = []

    def fake_gen(db, org_id, now=None):
        created.append(str(org_id))
        return 1

    retry_calls = []

    def fake_retry(db, org_id, now=None):
        retry_calls.append(str(org_id))
        return 1

    monkeypatch.setattr(
        scheduler_mod.reminders, "ensure_reminder_notifications", fake_gen
    )
    monkeypatch.setattr(
        scheduler_mod.reminders, "retry_pending_deliveries", fake_retry
    )

    result = scheduler_mod.run_reminder_sweep(fake, NOW)

    assert result["acquired"] is True
    assert result["orgs"] == 2
    assert result["created"] == 2
    assert result["retried"] == 2
    assert created == [ORG_A, ORG_B]
    assert retry_calls == [ORG_A, ORG_B]


def test_start_scheduler_is_guarded_against_double_registration(monkeypatch):
    from contextlib import nullcontext

    started = []

    class _FakeScheduler:
        def __init__(self, *a, **kw):
            pass

        def add_job(self, fn, **kw):
            pass

        def start(self):
            started.append(1)

    monkeypatch.setattr(
        scheduler_mod, "BackgroundScheduler", lambda *a, **kw: _FakeScheduler()
    )
    monkeypatch.setattr(scheduler_mod, "_ensure_lock_index", lambda db: None)

    class _App:
        def __init__(self):
            self.extensions = {"mongo_db": object()}

        def app_context(self):
            return nullcontext()

    scheduler_mod._scheduler_started = False
    app = _App()
    try:
        scheduler_mod.start_scheduler(app)
        scheduler_mod.start_scheduler(app)  # second call must be a no-op
        assert len(started) == 1
    finally:
        scheduler_mod._scheduler_started = False
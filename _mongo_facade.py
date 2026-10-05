"""In-memory MongoDB facade shared by the session-analysis tests.

Extracted from test_llm_timeout.py so the fallback / numeric-coercion /
drift-window tests can reuse exactly the same fake collection semantics that
were already relied on by the timeout regression tests:

  * ``$set`` / ``$unset`` honour DOTTED paths (analyses are stored per
    language under ``analyses.<language>``; a flat update would instead create
    a literal ``"analyses.<language>"`` key and hide the real shape).
  * ``find(...).sort(...).limit(...)`` actually sorts and limits, so window
    queries behave like the real driver.
  * ``find_one_and_update`` applies the update only when the filter matches,
    and returns None otherwise (used by the /analyze concurrency guard).
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from bson import ObjectId

SESSION_ID = "5" * 24
ORG_ID = "a" * 24
EMPLOYEE_ID = "1" * 24


def _dget(doc, path):
    node = doc
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _unset_path(doc, path):
    parts = path.split(".")
    node = doc
    for part in parts[:-1]:
        node = node.get(part)
        if not isinstance(node, dict):
            return
    node.pop(parts[-1], None)


def _match(doc, filt):
    for k, v in filt.items():
        # Logical operators carry a LIST of sub-filters, not a value.
        if k == "$and":
            if not all(_match(doc, sub) for sub in v):
                return False
            continue
        if k == "$or":
            if not any(_match(doc, sub) for sub in v):
                return False
            continue
        if k == "$nor":
            if any(_match(doc, sub) for sub in v):
                return False
            continue
        dv = _dget(doc, k)
        if isinstance(v, dict):
            for op, arg in v.items():
                if op == "$ne" and dv == arg:
                    return False
                if op == "$in" and dv not in arg:
                    return False
                if op == "$nin" and dv in arg:
                    return False
                if op == "$exists" and (dv is not None) != arg:
                    return False
                if op not in ("$ne", "$in", "$nin", "$exists") and dv != arg:
                    return False
        elif dv != v:
            return False
    return True


def _set_dotted(doc, path, value):
    """Write *value* at a dotted *path*, creating intermediate dicts."""
    parts = path.split(".")
    node = doc
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


class _FakeCursor:
    def __init__(self, docs, filt):
        self._docs = [d for d in docs if _match(d, filt)]
        self._sort_key = None
        self._sort_dir = 1
        self._limit = None

    def sort(self, key, direction=-1):
        self._sort_key = key
        self._sort_dir = direction
        return self

    def limit(self, n):
        self._limit = n
        return self

    def skip(self, n):
        self._docs = self._docs[n:]
        return self

    def __iter__(self):
        docs = list(self._docs)
        if self._sort_key is not None:
            docs.sort(
                key=lambda d: (_dget(d, self._sort_key) or datetime.min.replace(tzinfo=timezone.utc)),
                reverse=self._sort_dir < 0,
            )
        if self._limit is not None:
            docs = docs[: self._limit]
        return iter(docs)


class _FakeCollection:
    def __init__(self, docs=None):
        self._docs = list(docs or [])

    def find_one(self, filt=None, *a, **kw):
        matches = [d for d in self._docs if _match(d, filt or {})]
        return dict(matches[0]) if matches else None

    def find(self, filt=None, *a, **kw):
        return _FakeCursor(self._docs, filt or {})

    def find_one_and_update(self, filt, update, *a, **kw):
        for d in self._docs:
            if _match(d, filt):
                self._apply(d, update)
                return dict(d)
        return None

    def insert_one(self, doc):
        d = dict(doc)
        self._docs.append(d)
        return SimpleNamespace(inserted_id=d.get("_id"))

    @staticmethod
    def _apply(d, update):
        for op, fields in update.items():
            if op == "$set":
                for path, value in fields.items():
                    _set_dotted(d, path, value)
            elif op == "$unset":
                for path in fields:
                    _unset_path(d, path)

    def update_one(self, filt, update):
        for d in self._docs:
            if _match(d, filt):
                self._apply(d, update)
                return SimpleNamespace(matched_count=1)
        return SimpleNamespace(matched_count=0)

    def count_documents(self, filt=None):
        return sum(1 for d in self._docs if _match(d, filt or {}))


@pytest.fixture
def _fake_sessions_db():
    return SimpleNamespace(
        sessions=_FakeCollection(),
        employees=_FakeCollection(),
        organizations=_FakeCollection(),
        notifications=_FakeCollection(),
    )


def _seed_session(fake_db, transcript="Manager: How are you doing?", **overrides):
    now = datetime.now(timezone.utc)
    doc = {
        "_id": ObjectId(SESSION_ID),
        "org_id": ObjectId(ORG_ID),
        "employee_id": ObjectId(EMPLOYEE_ID),
        "source": "voice_dictation",
        "status": "transcribed",
        "language": "en",
        "recording_device": "browser",
        "recording_duration": 0,
        "recording_type": "webm",
        "audio": None,
        "transcript": {"raw": transcript, "edited": transcript, "word_count": 4},
        "analyses": {},
        # Legacy flat slot, kept so pre-migration assertions still resolve.
        "analysis": None,
        "analysis_version": 0,
        "last_transcript_update": now,
        "last_analyzed_at": None,
        "created_at": now,
        "updated_at": now,
    }
    doc.update(overrides)
    fake_db.sessions.insert_one(doc)
    return doc
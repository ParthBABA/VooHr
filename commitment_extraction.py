"""Post-analysis commitment extraction & resolution.

Runs automatically after a session's transcript has been analyzed and stored,
and does two things in a single LLM call:

  1. ``new_items`` — commitments / follow-ups that were explicitly promised in
     this transcript. Each is stored as ``confirmation_status="suggested"`` so
     it is visibly provisional: it does not count as an open promise, does not
     block a meeting delete, and never triggers a reminder until HR confirms it.
  2. ``resolutions`` — a verdict on the employee's *earlier* open commitments.
     A "done"/"in_progress" verdict with sufficient confidence is recorded as
     ``metadata.ai_resolution`` on that item and surfaced in the Meeting
     Tracker for a human to accept or dismiss.

Hard rules (enforced here, not merely intended):
  * The AI NEVER changes a status. Accepting a resolution goes through
    ``POST /conversation-memory/<id>/ai-resolution``, i.e. an explicit human
    action with an audit entry. Extraction itself writes no status at all.
  * Re-running is idempotent. New items are de-duplicated against every live
    item the employee already has, so re-analyzing a transcript cannot inflate
    the board with duplicates.
  * Everything is best-effort. This module never raises into its caller: a
    failure here must not change the session's status or lose the analysis that
    has already been stored.
  * Only conversation text and opaque short refs ("i1") ever reach the model —
    no name, email, department, or database id (see docs/LLM_DATA_PRIVACY.md).
"""

import logging
import os
from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId

from audit_log import ACTION_MEMORY_AI_SUGGEST, log_audit_event
from conversation_memory import MAX_MEMORY_CONTENT_LEN, is_ai_suggestion
from providers.llm import (
    MIN_RESOLUTION_CONFIDENCE,
    normalize_commitment_text,
)

logger = logging.getLogger(__name__)

# Opt-in. Off by default so the extra LLM call is never made unless an
# organization deliberately turns it on.
ENV_FLAG = "COMMITMENT_AI_ENABLED"

TRACKABLE_TYPES = ("COMMITMENT", "FOLLOW_UP")
OPEN_STATUSES = ("PENDING", "IN_PROGRESS")

# Only these verdicts propose that something happened; "not_mentioned" and
# "unclear" carry no information worth showing.
ACTIONABLE_VERDICTS = ("done", "in_progress")

# Caps on what may be sent / stored, so one verbose reply can't blow up the
# prompt or the database.
MAX_OPEN_ITEMS_FOR_PROMPT = 25
MAX_NEW_ITEMS = 10
MAX_EVIDENCE_LEN = 200


def _aware(dt):
    """Mongo returns naive UTC datetimes; normalize before comparing."""
    if dt is not None and getattr(dt, "tzinfo", None) is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _as_oid(value):
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(str(value))
    except (InvalidId, TypeError, ValueError):
        return None


def _enabled() -> bool:
    return os.environ.get(ENV_FLAG, "").strip().lower() == "true"


def _parse_due(raw):
    """Parse a model-supplied ISO due date into an aware datetime, or None.

    An unresolvable or malformed date is dropped rather than guessed: a
    fabricated due date would manufacture a real-looking deadline.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        due = datetime.fromisoformat(raw.strip())
    except (ValueError, TypeError):
        return None
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    return due


def _session_created_at(session_doc):
    return _aware((session_doc or {}).get("created_at"))


def _session_date_iso(session_doc) -> str:
    """The session's date, handed to the model so it can resolve relative
    dates ("next Friday") against a real anchor instead of guessing."""
    created = _session_created_at(session_doc)
    return created.date().isoformat() if created else ""


# ── Reading the employee's existing open promises ──────────────────────


def _load_open_items(db, org_oid, employee_oid, session_oid) -> list:
    """Open, HR-confirmed commitments/follow-ups this session may resolve.

    Excluded: this session's own items (they are not "earlier"), archived
    records, and AI suggestions nobody has confirmed yet — an unconfirmed
    suggestion is not a promise, so the model is never asked to close it.
    """
    query = {
        "org_id": org_oid,
        "employee_id": employee_oid,
        "type": {"$in": list(TRACKABLE_TYPES)},
        "status": {"$in": list(OPEN_STATUSES)},
        "session_id": {"$ne": session_oid},
    }
    open_items = []
    for m in db.conversation_memory.find(query):
        if m.get("archive"):
            continue
        if is_ai_suggestion(m):
            continue
        open_items.append(m)
        if len(open_items) >= MAX_OPEN_ITEMS_FOR_PROMPT:
            break
    return open_items


def _build_ref_map(open_items) -> tuple:
    """Pair each open item with a short opaque ref for the prompt.

    The refs are positional, so the model sees "i1", "i2", … — never a Mongo id
    and never anything that identifies the employee.
    """
    prompt_items = []
    by_ref = {}
    for idx, m in enumerate(open_items, start=1):
        ref = f"i{idx}"
        due = _aware(m.get("due_at"))
        prompt_items.append({
            "ref": ref,
            "type": m.get("type"),
            "content": m.get("content", ""),
            "due_at": due.isoformat() if due else None,
        })
        by_ref[ref] = m
    return prompt_items, by_ref


# ── Writing the suggestions ─────────────────────────────────────────────


def _is_duplicate(existing, norm: str, session_oid) -> bool:
    """True if this exact promise already exists for the employee.

    Any live item (any status except CANCELLED) with the same normalized text
    blocks the insert, so re-analyzing a transcript is idempotent. A CANCELLED
    item only blocks when it came from this very session — a cancelled promise
    is no longer a live one, but re-running extraction over the same session
    must not resurrect a decision that session already produced.
    """
    if not norm:
        return False
    if normalize_commitment_text(existing.get("content") or "") != norm:
        return False
    if existing.get("status") != "CANCELLED":
        return True
    metadata = existing.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    return str(metadata.get("source_session_id") or "") == str(session_oid)


def _store_new_items(db, org_oid, employee_oid, session_oid, new_items) -> int:
    """Insert the suggested commitments/follow-ups as unconfirmed records.

    Each row mirrors what the POST /conversation-memory handler writes for a
    human-entered item, with two differences that are the whole point of this
    feature: status is PENDING (never COMPLETED — nothing is auto-completed)
    and confirmation_status is "suggested" (nobody has vouched for it yet).
    """
    incoming = [it for it in (new_items or []) if isinstance(it, dict)][:MAX_NEW_ITEMS]
    if not incoming:
        return 0

    now = datetime.now(timezone.utc)
    existing = list(db.conversation_memory.find(
        {"org_id": org_oid, "employee_id": employee_oid}
    ))

    created = 0
    for it in incoming:
        mtype = str(it.get("type") or "").strip().upper()
        if mtype not in TRACKABLE_TYPES:
            continue
        content = " ".join(str(it.get("content") or "").split())[:MAX_MEMORY_CONTENT_LEN]
        if not content:
            continue
        norm = normalize_commitment_text(content)
        if any(_is_duplicate(m, norm, session_oid) for m in existing):
            continue

        owner = str(it.get("owner") or "").strip().lower()
        if owner not in ("employee", "manager", "unknown"):
            owner = "unknown"
        metadata = {
            "source": "ai_transcript",
            "source_session_id": str(session_oid),
            "evidence": str(it.get("evidence") or "")[:MAX_EVIDENCE_LEN],
            "owner_hint": owner,
        }
        due_at = _parse_due(it.get("due_at_iso"))

        doc = {
            "org_id": org_oid,
            "employee_id": employee_oid,
            "session_id": session_oid,
            "type": mtype,
            "content": content,
            # PENDING until a human closes it — the model cannot complete it.
            "status": "PENDING",
            "due_at": due_at,
            "used_at": None,
            "completed_at": None,
            "cancelled_at": None,
            "usage_count": 0,
            "usage": [],
            # No human created this row, so there is no author to record.
            "created_by": None,
            "confirmation_status": "suggested",
            "archive": False,
            "metadata": metadata,
            "priority": "medium",
            "owner_user_id": None,
            "status_history": [{
                "status": "PENDING",
                "changed_at": now,
                "changed_by": None,
            }],
            "created_at": now,
            "updated_at": now,
        }
        result = db.conversation_memory.insert_one(doc)
        existing.append(dict(doc, _id=result.inserted_id))
        created += 1

    return created


def _is_newer_session(db, session_doc, old_session_ref) -> bool:
    """True when this run's session is newer than the one that produced the
    suggestion already stored on an item.

    Re-analyzing an older session must not replace the verdict a more recent
    conversation gave. Same session → keep what is already there (stable, no
    churn across repeated runs). Unknown timestamps → treat this run as newer,
    since it is the most recent one processed.
    """
    old_id = _as_oid(old_session_ref)
    if old_id is None:
        return True
    new_session_oid = session_doc.get("_id")
    if old_id == new_session_oid:
        return False
    new_at = _session_created_at(session_doc)
    old_doc = db.sessions.find_one({"_id": old_id}) if hasattr(db, "sessions") else None
    old_at = _session_created_at(old_doc)
    if new_at is None or old_at is None:
        return True
    return new_at > old_at


def _store_resolutions(db, session_doc, by_ref, resolutions) -> int:
    """Attach an actionable AI verdict to the matching open item as metadata.

    Deliberately does NOT touch ``status``: the record stays PENDING /
    IN_PROGRESS and the verdict waits for a human to accept or dismiss it via
    the ai-resolution endpoint.
    """
    stored = 0
    for r in resolutions or []:
        if not isinstance(r, dict):
            continue
        verdict = str(r.get("verdict") or "").strip().lower()
        if verdict not in ACTIONABLE_VERDICTS:
            continue
        try:
            confidence = float(r.get("confidence"))
        except (TypeError, ValueError):
            continue
        if confidence < MIN_RESOLUTION_CONFIDENCE:
            continue

        item = by_ref.get(str(r.get("ref") or "").strip())
        if not item:
            continue

        metadata = dict(item.get("metadata") or {})
        previous = metadata.get("ai_resolution")
        if isinstance(previous, dict):
            # A human already ruled on this suggestion — never re-raise it.
            if previous.get("dismissed") or previous.get("accepted"):
                continue
            if not _is_newer_session(db, session_doc, previous.get("session_id")):
                continue

        metadata["ai_resolution"] = {
            "session_id": str(session_doc.get("_id")),
            "verdict": verdict,
            "confidence": max(0.0, min(1.0, confidence)),
            "evidence": str(r.get("evidence") or "")[:MAX_EVIDENCE_LEN],
            "suggested_at": datetime.now(timezone.utc).isoformat(),
            "dismissed": False,
        }
        db.conversation_memory.update_one(
            {"_id": item["_id"], "org_id": item.get("org_id")},
            {"$set": {"metadata": metadata, "updated_at": datetime.now(timezone.utc)}},
        )
        stored += 1
    return stored


# ── Entry point ─────────────────────────────────────────────────────────


def run_for_session(db, org_id, session_doc, llm, language="en") -> dict:
    """Extract new commitments and suggest resolutions for one analyzed session.

    Returns a small summary dict (never raises). Callers are expected to wrap
    this in their own try/except too, so nothing here can affect the session's
    stored analysis or status.
    """
    if not _enabled():
        return {"skipped": "disabled"}
    try:
        return _run(db, org_id, session_doc, llm, language)
    except Exception:
        # Best-effort by design: the analysis is already stored, so a failure
        # here must stay invisible to the user and never flip a completed
        # session to "failed".
        logger.exception(
            "Commitment extraction failed (session=%s)",
            (session_doc or {}).get("_id"),
        )
        return {"error": True}


def _run(db, org_id, session_doc, llm, language) -> dict:
    session_doc = session_doc if isinstance(session_doc, dict) else {}
    org_oid = _as_oid(org_id)
    session_oid = session_doc.get("_id")
    employee_oid = session_doc.get("employee_id")
    if org_oid is None or session_oid is None or employee_oid is None:
        return {"skipped": "incomplete_session"}

    transcript_doc = session_doc.get("transcript") or {}
    transcript = transcript_doc.get("edited") or transcript_doc.get("raw") or ""
    if not transcript.strip():
        return {"skipped": "no_transcript"}

    # Same truncation budget as the analysis call. Imported lazily because
    # sessions.py imports this module, so a module-level import would be
    # circular.
    from sessions import MAX_LLM_TRANSCRIPT_CHARS

    open_items = _load_open_items(db, org_oid, employee_oid, session_oid)
    prompt_items, by_ref = _build_ref_map(open_items)

    result = llm.extract_commitments(
        transcript[:MAX_LLM_TRANSCRIPT_CHARS],
        prompt_items,
        _session_date_iso(session_doc),
        language=language,
    )

    # An unparseable reply means "we learned nothing", not "there were no
    # promises" — it must never be written as a factual empty result.
    if not isinstance(result, dict) or result.get("is_fallback"):
        logger.warning(
            "Commitment extraction returned a fallback payload (session=%s)", session_oid
        )
        return {"skipped": "fallback"}

    created = _store_new_items(
        db, org_oid, employee_oid, session_oid, result.get("new_items")
    )
    resolved = _store_resolutions(db, session_doc, by_ref, result.get("resolutions"))

    try:
        log_audit_event(
            db, org_id, None, "AI suggestion", ACTION_MEMORY_AI_SUGGEST,
            target_type="session", target_id=str(session_oid),
            # Counts only: never item text, so the audit trail stays free of
            # conversation content.
            meta={"new_items_created": created, "resolutions_stored": resolved},
        )
    except Exception:
        logger.exception("audit log memory.ai_suggest failed")

    return {"new_items_created": created, "resolutions_stored": resolved}

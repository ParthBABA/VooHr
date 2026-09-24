"""Notification categories — which Notifications-page tab a row belongs to.

Meeting-tracker reminders used to fall into "Risk Signals" because the page
treated everything that wasn't a job completion as a risk. The backend now
tags each notification with an explicit ``category`` (activity / meeting /
risk) and the page groups rows by it.
"""
from datetime import datetime, timezone
from pathlib import Path

import pytest
from bson import ObjectId

import notifications as notif_mod

ROOT = Path(__file__).parent


@pytest.mark.parametrize("notif_type", ["translation_ready", "audio_ready", "session_ready"])
def test_job_completions_are_activities(notif_type):
    assert notif_mod._notification_category(notif_type) == "activity"


@pytest.mark.parametrize(
    "notif_type",
    ["meeting_reminder", "meeting_event", "memory_overdue", "delivery_failed"],
)
def test_meeting_notifications_are_meetings(notif_type):
    assert notif_mod._notification_category(notif_type) == "meeting"


def test_risk_drift_is_a_risk():
    assert notif_mod._notification_category("risk_drift") == "risk"


def test_unknown_or_missing_type_falls_back_to_risk():
    # Preserves the historical default so nothing silently disappears.
    assert notif_mod._notification_category("brand_new_type") == "risk"
    assert notif_mod._notification_category(None) == "risk"


def test_categories_do_not_overlap():
    assert not (
        notif_mod.ACTIVITY_NOTIFICATION_TYPES & notif_mod.MEETING_NOTIFICATION_TYPES
    )


def test_every_notification_type_created_by_the_app_is_categorised_on_purpose():
    """Every literal ``"type": "..."`` written into db.notifications must be
    in one of the allowlists (or be risk_drift) — a new type has to be a
    deliberate choice, not an accident of the fallback."""
    import re

    known = (
        notif_mod.ACTIVITY_NOTIFICATION_TYPES
        | notif_mod.MEETING_NOTIFICATION_TYPES
        | {"risk_drift"}
    )
    found = set()
    for name in ("reminders.py", "meetings.py", "sessions.py", "whatsapp_routes.py"):
        found |= set(re.findall(r'"type":\s*"([a-z_]+)"', (ROOT / name).read_text()))
    # jobs.py passes the type as notif_type=...
    found |= set(re.findall(r'notif_type="([a-z_]+)"', (ROOT / "jobs.py").read_text()))
    # only look at values that look like notification types
    candidates = {t for t in found if t.endswith(("_ready", "_reminder", "_event", "_overdue", "_failed", "_drift"))}
    assert candidates, "expected to find notification types in the source"
    assert candidates <= known, f"uncategorised notification types: {candidates - known}"


def test_serializer_includes_category():
    doc = {
        "_id": ObjectId(),
        "type": "meeting_reminder",
        "headline": "Before your next meeting",
        "summary": "commitment overdue: will talk",
        "created_at": datetime(2026, 9, 22, tzinfo=timezone.utc),
    }
    out = notif_mod._notification_to_json(doc, "harshit rana")
    assert out["category"] == "meeting"
    assert out["type"] == "meeting_reminder"


def test_serializer_category_for_legacy_doc_without_type():
    out = notif_mod._notification_to_json({"_id": ObjectId()})
    assert out["category"] == "risk"  # type defaults to risk_drift


# ── Frontend wiring (static checks; the behaviour itself is exercised in the
#    browser audit) ──────────────────────────────────────────────────────────

def _page():
    return (ROOT / "static" / "notifications.html").read_text(encoding="utf-8")


def test_notifications_page_has_a_meetings_tab():
    html = _page()
    assert 'id="tabMeetings"' in html
    assert 'id="panelMeetings"' in html
    assert 'id="meetingList"' in html


def test_notifications_page_no_longer_treats_non_activity_as_risk():
    html = _page()
    assert "!isActivity" not in html


def test_meeting_notifications_route_to_the_meeting_tracker():
    html = _page()
    assert "'/meeting-tracker'" in html

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


# ── Employee photo on notification rows ────────────────────────────────────
# The bell panel shows the employee's real avatar. Photos are inline base64
# data-URLs, so the list endpoint only ships them when the caller opts in —
# otherwise the hub's 200-row pages would carry megabytes they never render.

PHOTO = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD"


def test_serializer_carries_the_employee_photo():
    doc = {"_id": ObjectId(), "employee_id": ObjectId()}
    out = notif_mod._notification_to_json(doc, "harshit rana", PHOTO)
    assert out["employee_photo"] == PHOTO


def test_serializer_photo_defaults_to_none_so_callers_are_unchanged():
    """The two-argument call sites keep working and get no photo."""
    out = notif_mod._notification_to_json({"_id": ObjectId()}, "harshit rana")
    assert out["employee_photo"] is None


def test_employee_identity_returns_name_and_photo_together(monkeypatch):
    class _Employees:
        def find_one(self, query):
            return {"encrypted": b"blob", "wrapped_dek": "dek", "photo": PHOTO}

    class _DB:
        employees = _Employees()

    monkeypatch.setattr(notif_mod, "decrypt_fields",
                        lambda blob, dek: {"name": "harshit rana"})
    name, photo = notif_mod._employee_identity(_DB(), str(ObjectId()), ObjectId())
    assert name == "harshit rana"
    assert photo == PHOTO


def test_employee_identity_is_empty_without_an_employee():
    """System notifications carry no employee_id — no lookup, no crash."""
    assert notif_mod._employee_identity(None, str(ObjectId()), None) == ("", None)


BELL_PAGES = (
    "dashboard.html",
    "notifications.html",
    "conversation-workspace.html",
    "risk-drift.html",
)


@pytest.mark.parametrize("page", BELL_PAGES)
def test_bell_requests_photos_from_the_notifications_endpoint(page):
    html = (ROOT / "static" / page).read_text(encoding="utf-8")
    assert "/api/notifications?limit=5&include_photo=1" in html


@pytest.mark.parametrize("page", BELL_PAGES)
def test_bell_uses_the_shared_panel_renderer(page):
    html = (ROOT / "static" / page).read_text(encoding="utf-8")
    assert "VooNotif.renderNotifRows(" in html


def test_panel_renderer_builds_an_img_with_an_initials_fallback():
    js = (ROOT / "static" / "notification-routing.js").read_text(encoding="utf-8")
    # An <img> fills the avatar slot, and the initials path is still there for
    # employees with no photo (and for an image that fails to decode).
    assert "'notif-row__photo'" in js
    assert "showInitials(" in js
    assert "addEventListener('error'" in js


def test_panel_avatar_slot_is_styled_for_a_photo():
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    assert ".notif-row__photo{" in css
    assert "object-fit:cover" in css
    # The unread dot is positioned outside the 34px box, so the avatar must
    # never get overflow:hidden or it would clip the badge.
    avatar_rule = css.split(".notif-row__avatar{")[1].split("}")[0]
    assert "overflow:hidden" not in avatar_rule

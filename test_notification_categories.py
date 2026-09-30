"""Notification categories — which Notifications-page tab a row belongs to.

Meeting-tracker reminders used to fall into "Risk Signals" because the page
treated everything that wasn't a job completion as a risk. The backend now
tags each notification with an explicit ``category`` (activity / meeting /
risk) and the page groups rows by it.
"""
from datetime import datetime, timezone
from pathlib import Path
import os

import pytest
from bson import ObjectId

# notifications -> employees -> config requires SECRET_KEY at import time.
os.environ.setdefault("SECRET_KEY", "test-secret-key")

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
def test_bell_only_asks_for_unread_notifications(page):
    """The dropdown must not re-show read rows on every poll — that was the
    'seen notifications keep coming back' bug. unread_only makes the endpoint
    filter, so the panel empties out once everything has been viewed."""
    html = (ROOT / "static" / page).read_text(encoding="utf-8")
    assert "/api/notifications?limit=5&include_photo=1&unread_only=true" in html


def test_notifications_hub_keeps_showing_read_and_unread():
    """The hub's paginated list is an intentional full history — it must not
    gain unread_only, or read rows would vanish from the page itself."""
    html = (ROOT / "static" / "notifications.html").read_text(encoding="utf-8")
    hub_fetch = "/api/notifications?limit=' + limit + '&page=' + page"
    assert hub_fetch in html
    assert f"{hub_fetch}&unread_only" not in html


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


def test_mark_read_never_blocks_navigation():
    """window.VooNotif.markRead must PUT the read and then resolve *always*.

    The bell navigates inside .then(), so a rejected promise would strand the
    user on the page they just clicked. Three things guarantee resolution:
    the request's own catch, a 1500ms cap raced against it, and a no-op
    resolve for a missing id. keepalive additionally keeps the PUT alive
    across the navigation that follows it."""
    js = (ROOT / "static" / "notification-routing.js").read_text(encoding="utf-8")
    assert "'/api/notifications/' + encodeURIComponent(id) + '/read'" in js
    assert "method: 'PUT'" in js
    assert "keepalive: true" in js
    # No id -> nothing to send, but the caller still gets a promise to chain.
    assert "if (!id) return Promise.resolve();" in js
    # A failed request is swallowed, and the race caps how long the caller waits.
    assert ".catch(function () {})" in js
    assert "Promise.race([req, cap])" in js
    assert "setTimeout(resolve, 1500)" in js


def _routing_js():
    return (ROOT / "static" / "notification-routing.js").read_text(encoding="utf-8")


def test_bulk_mark_read_helpers_always_resolve():
    """markManyRead / markAllRead must resolve true|false and never reject.

    The hub chains a .then() onto these to decide whether to keep the
    optimistic UI or roll it back, and the bell chains one to clear its panel.
    A rejection would strand both — the badge stuck decremented and the row
    stuck painted read. So: no throw escapes, the CSRF wait is swallowed, the
    response is coerced to a boolean, and a 1500ms cap bounds the wait."""
    js = _routing_js()
    body = js[js.index("function putJson("):js.index("function markManyRead(")]
    # The CSRF wait is caught before the request is even attempted.
    assert "window.csrfTokenReady" in body
    assert ".catch(function () {})" in body
    # A non-2xx becomes false rather than an exception; a thrown error too.
    assert "return r.ok === true" in body
    assert ".catch(function () { return false; })" in body
    # Bounded, so a hung request can't freeze the page.
    assert "Promise.race([req, cap])" in body
    assert "setTimeout(function () { resolve(false); }, 1500)" in body
    # Both helpers go through it, so both inherit the never-reject contract.
    assert "return putJson('/api/notifications/read', { ids: list });" in js
    assert "return putJson('/api/notifications/read-all', {});" in js


def test_bulk_mark_read_sends_one_request_for_the_whole_batch():
    """A screenful of viewed rows must cost one request, not one per row."""
    js = _routing_js()
    many = js[js.index("function markManyRead("):js.index("function markAllRead(")]
    assert "return putJson('/api/notifications/read', { ids: list });" in many
    # Blank and duplicate ids are dropped rather than sent for the server to
    # de-duplicate; an empty list still resolves (true) without a request.
    assert "if (!list.length) return Promise.resolve(true);" in many
    # Content-Type matters: the body is JSON, not form-encoded.
    assert "'Content-Type': 'application/json'" in js
    assert "keepalive: true" in js


def test_bulk_mark_read_endpoints_match_the_backend_routes():
    js = _routing_js()
    routes = (ROOT / "notifications.py").read_text(encoding="utf-8")
    assert "putJson('/api/notifications/read'," in js
    assert '@notifications_bp.route("/notifications/read", methods=["PUT"])' in routes
    assert "putJson('/api/notifications/read-all'," in js
    assert '@notifications_bp.route("/notifications/read-all", methods=["PUT"])' in routes


def test_shared_badge_helper_is_exported():
    """The hub and the bell are separate closures on notifications.html, so the
    badge writer has to be shared or the two would drift."""
    js = _routing_js()
    assert "window.VooNotif.setBadge = setBadge;" in js
    # 0 total hides both the dot and the count.
    set_badge = js[js.index("function setBadge("):js.index("window.VooNotif = ")]
    assert "getElementById('notifDot')" in set_badge
    assert "getElementById('notifCount')" in set_badge
    assert "unread > 0 ? '' : 'none'" in set_badge


# ── The hub's "seen means read" behaviour ───────────────────────────────

def _hub_script():
    """The voovrInitNotifications body, i.e. the hub half of the page."""
    html = (ROOT / "static" / "notifications.html").read_text(encoding="utf-8")
    return html[html.index("window.voovrInitNotifications = function()"):]


def test_hub_open_notification_marks_read_optimistically():
    """The reported bug: on the hub, the red highlight survived until a reload
    because clicking View only fired a request and never updated the row.

    openNotification must now repaint the row and the counters *before*
    navigating, and must still send the read first and navigate after — the
    order that keeps the red badge from coming back on the next page."""
    hub = _hub_script()
    open_notif = hub[hub.index("function openNotification("):hub.index("// Queue a row")]
    # Optimistic: the row itself, not just the server. paintRow is the single
    # place that writes the flag and repaints the dot.
    assert "paintRow(n, row, true);" in open_notif
    paint = hub[hub.index("function paintRow("):hub.index("// Counters and badge")]
    assert "n.read = read;" in paint
    # Tab counters (and so 'has-unread') plus the header badge both move.
    assert "afterReadChange(-1);" in open_notif
    assert "function afterReadChange(" in hub
    assert "refreshCounts();" in hub
    # Read is still sent BEFORE navigating, and via the shared helper.
    assert "V.markRead(n.id).then(function() { window.location.href = url; })" in open_notif
    # Must not regress to an inline fire-and-forget fetch.
    assert "fetch('/api/notifications/" not in open_notif


def test_hub_marks_a_row_read_after_it_has_been_on_screen():
    """A row that has actually been seen counts as read, with no click.

    IntersectionObserver with a 0.6 ratio so a row merely scrolled past does
    not count, and a ~1.5s dwell so a fast scroll through doesn't mark
    everything. Only unread rows in the tab on screen are observed."""
    hub = _hub_script()
    assert "new IntersectionObserver(" in hub
    assert "var DWELL_MS = 1500;" in hub
    assert "var DWELL_RATIO = 0.6;" in hub
    assert "{ threshold: DWELL_RATIO }" in hub
    assert "entry.intersectionRatio < DWELL_RATIO" in hub
    # Only the active tab's rows, and only the unread ones.
    assert "cfg[currentTab].items.forEach(function(n) {" in hub
    assert "if (row && !n.read) observer.observe(row);" in hub
    # Unobserved once handled, so a row can never be marked twice.
    assert "observer.unobserve(row);" in hub
    # Stale dwell timers are cleared when the tab (and the DOM) is replaced.
    assert "clearTimeout(dwellTimers[k])" in hub
    assert "observer.disconnect();" in hub


def test_hub_batches_viewed_rows_into_one_request():
    """Viewing a screenful must not cost a request per row."""
    hub = _hub_script()
    assert "pending.push({ id: String(n.id), row: row });" in hub
    assert "V.markManyRead(batch.map(function(p) { return p.id; }))" in hub
    # One in-flight flush, not one per row.
    assert "if (flushTimer) return;" in hub
    assert "setTimeout(flushPending, 400)" in hub


def test_hub_rolls_back_rows_when_the_read_request_fails():
    """A failed request leaves the server still thinking the row is unread, so
    the page must not keep claiming it is read: repaint the rows, restore the
    badge, and leave the counters alone-behind."""
    hub = _hub_script()
    flush = hub[hub.index("function flushPending("):hub.index("function buildRow(")]
    assert "if (ok) {" in flush
    assert "paintRow(p.row._notif, p.row, false);" in flush
    assert "afterReadChange(batch.length);" in flush
    # And a successful change tells the bell (and any other listener) to resync.
    assert "notifyChanged();" in flush
    assert "new Event('voo:notifications-changed')" in hub


def test_hub_mark_all_read_uses_the_csrf_safe_helper():
    """A raw PUT races csrf.js and is rejected when the token hasn't landed.
    Both mark-all handlers must go through the shared helper instead."""
    html = (ROOT / "static" / "notifications.html").read_text(encoding="utf-8")
    assert "fetch('/api/notifications/read-all', { method: 'PUT' })" not in html
    # The bell dropdown's handler and the hub's, both.
    assert html.count("VooNotif.markAllRead()") == 1
    assert html.count("V.markAllRead()") == 1
    # Failure must not leave the hub's button stuck disabled.
    hub = _hub_script()
    assert "pageMarkAll.disabled = false;" in hub
    assert "if (!ok) return;" in hub


def test_hub_read_repaint_is_a_class_toggle_with_a_transition():
    """Rebuilding the list would snap the highlight away and lose scroll
    position; only the classes on that one row change, and CSS fades them."""
    hub = _hub_script()
    paint = hub[hub.index("function paintRow("):hub.index("// Counters and badge")]
    assert "row.classList.toggle('is-read', read);" in paint
    assert "sev.className = V.severityClass(n);" in paint

    html = (ROOT / "static" / "notifications.html").read_text(encoding="utf-8")
    assert "transition:background .3s ease,box-shadow .3s ease;" in html
    # And the fade is opt-out for anyone who asked for less motion.
    reduced = html[html.index("@media (prefers-reduced-motion: reduce)"):]
    reduced = reduced[:reduced.index("}")]
    assert ".alert-severity" in reduced
    assert "transition:none" in reduced


def test_panel_avatar_slot_is_styled_for_a_photo():
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    assert ".notif-row__photo{" in css
    assert "object-fit:cover" in css
    # The unread dot is positioned outside the 34px box, so the avatar must
    # never get overflow:hidden or it would clip the badge.
    avatar_rule = css.split(".notif-row__avatar{")[1].split("}")[0]
    assert "overflow:hidden" not in avatar_rule

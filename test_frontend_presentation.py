"""Presentation regressions, without database or external services."""
from html.parser import HTMLParser
from pathlib import Path
import re
import unittest

from flask import Flask
from page_rendering import metadata, render_page


ROOT = Path(__file__).parent


class HeadParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.ids = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        if attrs.get("id"):
            self.ids.append(attrs["id"])


class PresentationTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, static_folder=str(ROOT / "static"),
                         template_folder=str(ROOT / "templates"))

    def test_every_page_has_crawler_readable_metadata_and_unique_ids(self):
        for path in (ROOT / "static").glob("*.html"):
            with self.subTest(page=path.name), self.app.test_request_context(
                    "/nested/page?token=private", base_url="https://example.test"):
                response = render_page(path.name)
                html = response.get_data(as_text=True)
                parser = HeadParser()
                parser.feed(html)
                self.assertEqual(len(parser.ids), len(set(parser.ids)))
                for key, value in [("name", "description"), ("property", "og:title"),
                                   ("property", "og:image"), ("name", "twitter:card")]:
                    matches = [a for tag, a in parser.tags if tag == "meta" and a.get(key) == value]
                    self.assertEqual(len(matches), 1, (path.name, value))
                    self.assertTrue(matches[0]["content"])
                self.assertTrue(any(tag == "link" and a.get("rel") == "icon" for tag, a in parser.tags))
                self.assertNotIn("<!-- shared-head -->", html)
                self.assertNotIn("token=private", html)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                for tag, attrs in parser.tags:
                    if tag in {"script", "link", "img"}:
                        url = attrs.get("src") or attrs.get("href") or ""
                        if url and not re.match(r"^(?:/|#|https?:|data:)", url):
                            self.fail(f"Relative asset on {path.name}: {url}")

    def test_auth_metadata_is_not_indexable_and_escapes_title(self):
        from flask import render_template
        with self.app.test_request_context("/login?redirect=secret"):
            html = render_template("shared-head.html", **metadata('VooVr " <script>'))
            self.assertIn('content="noindex, nofollow"', html)
            self.assertIn("&lt;script&gt;", html)
            self.assertNotIn("redirect=secret", html)

    def test_asset_normalization_preserves_dynamic_javascript(self):
        from page_rendering import _RootAssetParser
        source = '<img src="logo.png"><script>var html = \'<img src="\' + photo + \'">\';</script>'
        result = _RootAssetParser(source).normalized()
        self.assertTrue(result.startswith('<img src="/logo.png">'))
        self.assertEqual(source[source.index('<script>'):], result[result.index('<script>'):])

    def test_no_native_dialogs_debug_logs_or_corrupt_css(self):
        for path in (ROOT / "static").glob("*.html"):
            source = path.read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"\b(?:alert|confirm)\s*\(|console\.log\s*\(", source), path.name)
            self.assertNotIn("HR Copilot", source)
        self.assertNotIn(b"\x00", (ROOT / "static/style.css").read_bytes())

    def test_meeting_tracker_delete_controls_on_card_and_detail(self):
        source = (ROOT / "static/meeting_tracker.html").read_text(encoding="utf-8")
        # ui-feedback must be loaded so the confirm dialog uses VooVrUI.ask.
        self.assertIn('<script src="/ui-feedback.js"></script>', source)
        # Every meeting_history row gets a delete control with the trash icon and
        # an accessible label; titles/dates are escaped into data attributes.
        self.assertIn('mtDelTrigger(m.id, \'Delete\', \'\', m.title || \'\', mtWhenTxt(m.scheduled_at))', source)
        self.assertIn('aria-label="Delete meeting"', source)
        self.assertRegex(source, r'data-title="\' \+ escHtml\(title \|\| \'\'\)')
        # The person card renders upcoming / history / missed entries with deletes.
        self.assertIn("mtCardMeetings(p)", source)
        self.assertIn("mtCardMeetings", source)
        self.assertIn("meeting_records", source)
        self.assertIn("recordLabel", source)
        # The detail popup's Danger Zone falls back to meeting_records too, so a
        # record that only lives there (e.g. a legacy/unknown status no longer
        # occupying the next/last/history slots) is still deletable.
        self.assertRegex(source, r"var delMeet = meet[\s\S]*?p\.meeting_records")
        # Cards are re-wired after every render.
        self.assertIn("mtWireDeleteMeeting(document);", source)
        # The confirm dialog gates the DELETE request: the ask must be awaited
        # before fetch, and a cancel must not issue the request.
        del_fn = source[source.index("function mtDeleteMeeting"):]
        ask_pos = del_fn.find("VooVrUI.ask(")
        fetch_pos = del_fn.find("fetch('/api/meetings/")
        self.assertTrue(ask_pos != -1 and fetch_pos != -1 and ask_pos < fetch_pos)
        self.assertIn("if (!confirmResult) return;", del_fn)
        # The DELETE follows the repo CSRF pattern (headers + same-origin creds;
        # csrf.js adds the X-CSRF-Token header automatically).
        self.assertIn("method: 'DELETE'", del_fn)
        self.assertIn("'Content-Type': 'application/json'", del_fn)
        self.assertIn("credentials: 'same-origin'", del_fn)
        # Mapped server feedback: 403 and 404 wording from the task spec.
        self.assertIn("You don\\u2019t have permission to delete this meeting", del_fn)
        self.assertIn("Meeting already deleted.", del_fn)
        # The board is re-fetched (not a full page reload) after a successful delete.
        self.assertTrue(re.search(r"closeModal\(\).*VooVrUI\.show\('Meeting deleted\..*loadData\(\)",
                                  del_fn, re.S), "success path must close the modal, toast, and re-loadData")

    # ── Signed-in identity actually reaches the sidebar footer ────────────
    #
    # Every page ships the same #userName / #userRole / #userAvatar markup
    # pre-filled with placeholder text. Those values are only real if a script
    # overwrites them from /api/me. meeting_tracker.html shipped the markup
    # with no such script, so its footer read "U / User / Admin" for every
    # logged-in user. These tests check the *write*, not just the presence of
    # the id, so the markup can't quietly revert to dead placeholders.

    IDENTITY_IDS = ("userName", "userRole", "userAvatar")

    def _is_written(self, source, el_id):
        """True when something assigns to the element's text/HTML/value.

        Handles the two real shapes in this codebase: a direct chained write
        (`getElementById('x').textContent = ...`) and the far more common
        cached-variable form (`var x = getElementById('x'); x.textContent =`).
        """
        chained = re.search(
            r"getElementById\(['\"]" + el_id + r"['\"]\)\s*\.\s*"
            r"(?:textContent|innerHTML|value)\s*=[^=]", source)
        if chained:
            return True
        for var in re.findall(
                r"(?:var|let|const)\s+([A-Za-z_$][\w$]*)\s*=\s*"
                r"(?:document\.)?getElementById\(\s*['\"]" + el_id + r"['\"]\s*\)", source):
            if re.search(r"\b" + re.escape(var) + r"\s*\.\s*"
                         r"(?:textContent|innerHTML|value)\s*=[^=]", source):
                return True
        return bool(re.search(
            r"querySelector\(\s*['\"]#" + el_id + r"['\"]\s*\)\s*\.\s*"
            r"(?:textContent|innerHTML|value)\s*=[^=]", source))

    def test_every_page_with_identity_markup_overwrites_it_from_the_api(self):
        """Audited against the RENDERED page, not the static source.

        The identity markup (#userName / #userRole / #userAvatar) now reaches
        dashboard.html and settings.html through the shared sidebar partial, so
        scanning static/*.html alone would miss exactly the pages most likely to
        regress. Rendering first keeps the audit honest — and stricter, since it
        now sees the markup each page actually ships.
        """
        pages = []
        for path in sorted((ROOT / "static").glob("*.html")):
            with self.app.test_request_context("/page", base_url="https://example.test"):
                source = render_page(path.name).get_data(as_text=True)
            if not all(f'id="{el}"' in source for el in self.IDENTITY_IDS):
                continue
            pages.append(path.name)
            with self.subTest(page=path.name):
                self.assertIn("fetch('/api/me')", source,
                              f"{path.name} ships identity markup but never fetches /api/me")
                for el in self.IDENTITY_IDS:
                    self.assertTrue(
                        self._is_written(source, el),
                        f"{path.name}: #{el} is never written — the hardcoded "
                        f"placeholder would stay on screen")
        # The audit is only meaningful if it actually found the known pages.
        self.assertIn("meeting_tracker.html", pages)
        # dashboard.html and settings.html must still be covered even though the
        # markup now arrives via {% include 'partials/sidebar.html' %}.
        for expected in ("dashboard.html", "settings.html"):
            self.assertIn(expected, pages,
                          f"{expected} renders the sidebar identity markup but "
                          f"was not audited")
        self.assertGreaterEqual(len(pages), 9)

    def test_meeting_tracker_shows_the_real_logged_in_user(self):
        """Specific regression: the tracker had the markup but no script."""
        source = (ROOT / "static" / "meeting_tracker.html").read_text(encoding="utf-8")
        self.assertIn("fetch('/api/me')", source)
        for el in self.IDENTITY_IDS:
            self.assertTrue(self._is_written(source, el), f"#{el} never written")
        # Role label matches the rest of the app, and a failed profile call
        # degrades silently instead of blocking or bouncing the user.
        self.assertIn("me.role === 'admin' ? 'HR Admin'", source)
        self.assertIn(".catch(function() { return null; })", source)

    # ── No dead buttons, no title-attribute wiring ────────────────────────
    #
    # The directory header shipped a "Filters" button with no id and no
    # handler anywhere in the file — a control that looked live and did
    # nothing. "Add Employee" worked, but only because it was found via
    # querySelector('[title="Add a new employee"]'), so a copy tweak would
    # have broken it silently. These guard both halves.

    def test_dashboard_header_buttons_are_all_id_addressable(self):
        source = (ROOT / "static" / "dashboard.html").read_text(encoding="utf-8")
        block = source.split('<div class="card-header-actions">', 1)[1]
        block = block.split("</div>", 1)[0]
        buttons = re.findall(r"<button\b[^>]*>", block)
        self.assertTrue(buttons, "card-header-actions should still hold its buttons")
        for tag in buttons:
            with self.subTest(button=tag[:70]):
                self.assertIn('id="', tag,
                              "header buttons must carry an id so JS can reach them")
                # And the id must actually be used.
                el_id = re.search(r'id="([^"]+)"', tag).group(1)
                self.assertIn(f"getElementById('{el_id}')", source,
                              f"#{el_id} is declared but never read — dead UI")

    def test_no_element_is_located_by_its_title_attribute(self):
        """Title text is a tooltip, not an API — a copy or i18n change would
        break the lookup with no error anywhere."""
        pattern = re.compile(r"querySelector(?:All)?\(\s*['\"]\[title")
        for path in sorted((ROOT / "static").glob("*.html")):
            source = path.read_text(encoding="utf-8")
            with self.subTest(page=path.name):
                self.assertIsNone(pattern.search(source),
                                  f"{path.name} locates an element by its title attribute")

    def test_add_employee_panel_is_opened_by_id(self):
        source = (ROOT / "static" / "dashboard.html").read_text(encoding="utf-8")
        panel = source.split("// \u2500\u2500 ADD EMPLOYEE PANEL", 1)[1]
        self.assertIn("getElementById('dashAddEmployeeBtn')", panel)
        self.assertNotIn('[title="Add a new employee"]', panel)
        # The button still exists in the markup, so the id has a target.
        self.assertIn('id="dashAddEmployeeBtn"', source)
        self.assertIn("if (openBtn) openBtn.addEventListener('click', openPanel)", panel)

    def test_directory_photo_is_sent_on_create(self):
        """Employee photos are a real, wired feature: the upload is resized to
        a 256px JPEG data-URL client-side and posted to /api/employees, which
        validates and stores it. Guards against anyone "fixing" a phantom
        preview-only bug by stripping the field from the request."""
        source = (ROOT / "static" / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("photo: currentPhotoData", source)
        self.assertIn("canvas.toDataURL('image/jpeg'", source)
        py = (ROOT / "employees.py").read_text(encoding="utf-8")
        self.assertIn("MAX_PHOTO_BYTES", py)
        self.assertIn('"photo": photo or None', py)

    def test_dashboard_does_not_recursively_fetch_every_employee(self):
        """The directory must page server-side.

        It used to walk /api/employees page by page until has_more was false,
        downloading the whole org, and then filter/paginate the DOM. That made
        the browser hold every employee just to draw 20 rows, and it silently
        truncated large orgs. Search and the derived wellness status now run on
        the server, so no page-walking loop may come back.
        """
        source = (ROOT / "static" / "dashboard.html").read_text(encoding="utf-8")
        self.assertNotIn("fetchEmployeeRoster", source)
        self.assertNotIn("has_more", source)
        self.assertIsNone(re.search(r"function\s+loadPage\s*\(", source),
                          "a recursive page loader is back")

    def test_dashboard_sends_filters_to_the_server(self):
        """Filtering must reach the API, not just hide rows already in the DOM.

        search/name is the one that matters most: it is matched against
        encrypted name/email, so it cannot work unless the server does it.
        """
        source = (ROOT / "static" / "dashboard.html").read_text(encoding="utf-8")
        for param in ("search", "department", "wellness_status", "page", "limit"):
            with self.subTest(param=param):
                self.assertIn(f"params.set('{param}'", source,
                              f"#{param} is never sent to /api/employees")
        # And the server must actually accept the derived-status filter.
        py = (ROOT / "employees.py").read_text(encoding="utf-8")
        self.assertIn('request.args.get("wellness_status")', py)
        self.assertIn('request.args.get("search")', py)

    def test_dashboard_stats_and_lookups_hit_dedicated_endpoints(self):
        """Stat cards, the department filter and the manager dropdown each need
        the full scoped set, which is why the recursion existed. They must read
        it from the server-side aggregate/lookup endpoints instead."""
        source = (ROOT / "static" / "dashboard.html").read_text(encoding="utf-8")
        for endpoint in ("/api/employees/stats",
                         "/api/employees/departments",
                         "/api/employees/manager-options"):
            with self.subTest(endpoint=endpoint):
                self.assertIn(endpoint, source)
        py = (ROOT / "employees.py").read_text(encoding="utf-8")
        for route in ('@employees_bp.route("/employees/stats")',
                      '@employees_bp.route("/employees/departments")',
                      '@employees_bp.route("/employees/manager-options")'):
            with self.subTest(route=route):
                self.assertIn(route, py)

    def test_static_routes_still_precede_employee_id_route(self):
        """The literal sub-paths must be registered before /employees/<id>,
        whose string converter would otherwise swallow them."""
        py = (ROOT / "employees.py").read_text(encoding="utf-8")
        for literal in ("/employees/stats", "/employees/departments",
                        "/employees/manager-options"):
            with self.subTest(literal=literal):
                self.assertLess(py.index(f'route("{literal}")'),
                                py.index('route("/employees/<emp_id>")'),
                                f"{literal} is shadowed by the <emp_id> route")

    # ── Shared Jinja partials ─────────────────────────────────────────────
    #
    # The sidebar / notification bell / theme-toggle markup used to be copied
    # into each page, so a fix had to be applied N times and drifted. These pages
    # are served only through render_page(), so they double as Jinja templates
    # and pull the shared parts from templates/partials/. The tests below pin
    # both halves: the includes resolve, and the duplication does not return.

    PARTIALS = ("sidebar.html", "notification_bell.html",
                "notification_bell_js.html", "theme_toggle_js.html")

    def _render(self, page):
        with self.app.test_request_context("/page", base_url="https://example.test"):
            return render_page(page).get_data(as_text=True)

    def test_every_partial_exists_and_is_referenced(self):
        for name in self.PARTIALS:
            with self.subTest(partial=name):
                self.assertTrue((ROOT / "templates" / "partials" / name).is_file(),
                                f"templates/partials/{name} is missing")
                needle = f"{{% include 'partials/{name}' %}}"
                users = [p.name for p in sorted((ROOT / "static").glob("*.html"))
                         if needle in p.read_text(encoding="utf-8")]
                self.assertTrue(users, f"no page includes {name}")

    def test_render_page_expands_includes(self):
        """render_page() must render pages as templates, or every {% include %}
        would ship to the browser as literal text."""
        html = self._render("dashboard.html")
        self.assertNotIn("{% include", html)
        self.assertNotIn("{{", html)
        # The sidebar only reaches dashboard.html through the partial.
        self.assertIn('<nav class="sidebar">', html)

    def test_partial_markup_is_normalized_like_page_markup(self):
        """The sidebar's logo is authored relative to the static root, so it must
        be rewritten to /voovr-logo-full.png even though it now comes from a
        template in templates/."""
        for page in ("dashboard.html", "settings.html"):
            with self.subTest(page=page):
                self.assertIn('src="/voovr-logo-full.png"', self._render(page))
                self.assertNotIn('src="voovr-logo-full.png"', self._render(page))

    def test_sidebar_marks_exactly_its_own_nav_item_active(self):
        """nav_active drives the highlight; getting it wrong silently breaks
        "where am I" on two pages.

        The partial matches on the authored (relative) href, but the rendered
        page has been normalized to root-absolute by _RootAssetParser, so the
        expected hrefs here are the normalized ones. Asserting them doubles as a
        check that normalization still reaches markup that came from a partial.
        """
        for page, active_href in (("dashboard.html", "/dashboard.html"),
                                  ("settings.html", "/settings.html")):
            with self.subTest(page=page):
                nav = re.search(r'<nav class="sidebar">.*?</nav>',
                                self._render(page), re.S).group(0)
                actives = re.findall(r'<a href="([^"]+)" class="nav-item active"', nav)
                self.assertEqual(actives, [active_href],
                                 f"{page} highlights {actives}, expected [{active_href}]")
                # Every other item must be plain — no second highlight.
                self.assertEqual(nav.count("nav-item active"), 1)

    def test_notification_bell_markup_is_not_duplicated(self):
        """The three bell pages must share one copy of the markup."""
        pages = ("dashboard.html", "conversation-workspace.html", "risk-drift.html")
        for page in pages:
            with self.subTest(page=page):
                source = (ROOT / "static" / page).read_text(encoding="utf-8")
                self.assertIn("{% include 'partials/notification_bell.html' %}", source)
                self.assertNotIn('id="notifPanel"', source,
                                 f"{page} still carries its own bell markup")
        for page in pages:
            with self.subTest(rendered=page):
                self.assertEqual(self._render(page).count('id="notifPanel"'), 1)

    def test_pages_without_a_bell_did_not_gain_one(self):
        """Only the three original bell pages have a bell. A partial include
        must not be added to the others as a side effect."""
        for page in ("meeting_tracker.html", "settings.html",
                     "sync.html", "sync_room.html"):
            with self.subTest(page=page):
                self.assertNotIn("id='notifBtn'", (ROOT / "static" / page).read_text(encoding="utf-8"))
                self.assertNotIn('id="notifBtn"', self._render(page))

    def test_pages_without_a_sidebar_did_not_gain_one(self):
        for page in ("conversation-workspace.html", "risk-drift.html",
                     "meeting_tracker.html", "sync.html", "sync_room.html"):
            with self.subTest(page=page):
                self.assertNotIn("partials/sidebar.html",
                                 (ROOT / "static" / page).read_text(encoding="utf-8"))
                self.assertNotIn('<nav class="sidebar">', self._render(page))

    def test_bell_routing_behavior_is_preserved(self):
        """Meeting-family notifications must still deep-link to the meeting
        tracker, and everything else must still fall through to the shared
        router. notifications.html is intentionally not on this partial."""
        partial = (ROOT / "templates" / "partials" / "notification_bell_js.html").read_text(
            encoding="utf-8")
        self.assertIn("function openNotification(n)", partial)
        for notification_type in ("meeting_reminder", "meeting_event",
                                  "memory_overdue", "delivery_failed"):
            with self.subTest(type=notification_type):
                self.assertIn(f"n.type === '{notification_type}'", partial)
        self.assertIn("'/meeting-tracker?employee_id=' + encodeURIComponent(n.employee_id)",
                      partial)
        self.assertIn("window.VooNotif.targetUrl(n)", partial)
        # Mark-as-read must stay fire-and-forget.
        self.assertIn("fetch('/api/notifications/' + n.id + '/read', { method: 'PUT' })",
                      partial)
        for page in ("dashboard.html", "conversation-workspace.html", "risk-drift.html"):
            with self.subTest(page=page):
                html = self._render(page)
                self.assertIn("'/meeting-tracker?employee_id='", html)

    def test_theme_toggle_handler_is_deduplicated_where_it_is_safe(self):
        """There are two different theme handlers in this app.

        Six pages read/write localStorage['voovr-theme'] themselves; two
        (settings.html, activity-log.html) instead delegate to the shared
        window.voovrGetTheme/voovrSetTheme API. The partial carries the
        localStorage variant, so only pages using THAT variant and whose IIFE
        contains nothing else may be collapsed onto it — otherwise a page's
        other initialisation would move or change order.
        """
        partial = (ROOT / "templates" / "partials" / "theme_toggle_js.html").read_text(
            encoding="utf-8")
        self.assertIn("localStorage.setItem('voovr-theme', next)", partial)
        self.assertIn("document.documentElement.setAttribute('data-theme', 'light')", partial)

        # localStorage variant, standalone IIFE -> use the partial.
        standalone = ("dashboard.html", "conversation-workspace.html", "risk-drift.html",
                      "meeting_tracker.html", "sync.html")
        for page in standalone:
            with self.subTest(page=page):
                source = (ROOT / "static" / page).read_text(encoding="utf-8")
                self.assertIn("{% include 'partials/theme_toggle_js.html' %}", source)
                self.assertNotIn("localStorage.setItem('voovr-theme'", source,
                                 f"{page} still has an inline copy")
                self.assertIn("localStorage.setItem('voovr-theme'", self._render(page))

        # localStorage variant, but the IIFE also wires syncAnalysisLang, so
        # splitting it would reorder initialisation. Left inline on purpose.
        with self.subTest(page="sync_room.html", note="shared IIFE, left inline"):
            source = (ROOT / "static" / "sync_room.html").read_text(encoding="utf-8")
            self.assertNotIn("partials/theme_toggle_js.html", source)
            self.assertIn("localStorage.setItem('voovr-theme'", self._render("sync_room.html"))

        # A different handler entirely: must be left alone.
        for page in ("settings.html", "activity-log.html"):
            with self.subTest(page=page, note="delegating handler, not the partial's"):
                source = (ROOT / "static" / page).read_text(encoding="utf-8")
                self.assertNotIn("partials/theme_toggle_js.html", source)
                html = self._render(page)
                self.assertIn("voovrSetTheme", html)
                self.assertNotIn("localStorage.setItem('voovr-theme'", html)

    def test_every_theme_toggle_button_is_wired_to_a_handler(self):
        """A #themeToggle button with no handler is a dead control. The handler
        may be either of the two styles above, so accept both."""
        for path in sorted((ROOT / "static").glob("*.html")):
            if 'id="themeToggle"' not in path.read_text(encoding="utf-8"):
                continue
            with self.subTest(page=path.name):
                html = self._render(path.name)
                self.assertTrue(
                    "voovr-theme" in html or "voovrSetTheme" in html,
                    f"{path.name} renders a #themeToggle button but no theme handler")


if __name__ == "__main__":
    unittest.main()

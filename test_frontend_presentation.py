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


if __name__ == "__main__":
    unittest.main()

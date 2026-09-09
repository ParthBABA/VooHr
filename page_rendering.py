"""Shared, crawler-readable metadata for the HTML pages served by Flask."""

from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path
import re

from flask import current_app, make_response, render_template, request


DESCRIPTION = (
    "VooVr brings employee records, conversation insights, meeting tracking, "
    "and thoughtful follow-ups together for modern HR teams."
)


def metadata(title, public=False):
    # Do not put query strings (which can contain invite tokens) in previews.
    base = (current_app.config.get("SITE_URL") or request.url_root).rstrip("/")
    return dict(page_title=title, page_description=DESCRIPTION,
                page_url=base + request.path, site_base=base, public_page=public)


@lru_cache(maxsize=64)
def _read_page(path, modified):
    return Path(path).read_text(encoding="utf-8")


class _RootAssetParser(HTMLParser):
    """Normalize actual markup, never JavaScript template strings or CSS."""
    def __init__(self, source):
        super().__init__(convert_charrefs=False)
        self.source = source
        self.offsets = [0]
        for line in source.splitlines(keepends=True):
            self.offsets.append(self.offsets[-1] + len(line))
        self.edits = []

    def handle_starttag(self, tag, attrs):
        original = self.get_starttag_text()
        replacement = re.sub(
            r'\b(src|href)="(?!/|#|[a-zA-Z][a-zA-Z0-9+.-]*:)([^"{}]+)"',
            r'\1="/\2"', original,
        )
        if replacement != original:
            line, column = self.getpos()
            offset = self.offsets[line - 1] + column
            self.edits.append((offset, len(original), replacement))

    def normalized(self):
        self.feed(self.source)
        result = self.source
        for offset, length, replacement in reversed(self.edits):
            result = result[:offset] + replacement + result[offset + length:]
        return result


def render_page(filename):
    path = Path(current_app.static_folder) / filename
    source = _read_page(str(path), path.stat().st_mtime_ns)
    title = re.search(r"<title>(.*?)</title>", source, re.S).group(1)
    head = render_template("shared-head.html", **metadata(
        title, public=filename in {"privacy-policy.html", "terms-of-service.html"}
    ))
    # Clean routes can be nested (/sync/room). Local assets and page links
    # were authored relative to the static root, not the current URL folder.
    response = make_response(_RootAssetParser(
        source.replace("<!-- shared-head -->", head, 1)).normalized())
    response.headers["Cache-Control"] = "no-store"
    return response

"""Shared, crawler-readable metadata for the HTML pages served by Flask."""

from functools import lru_cache
from html.parser import HTMLParser
import json
from pathlib import Path
import re

from flask import current_app, make_response, render_template, request


DESCRIPTION = (
    "VooVr brings employee records, conversation insights, meeting tracking, "
    "and thoughtful follow-ups together for modern HR teams."
)
SUPPORT_CONTENT_PATH = Path(__file__).parent / "data" / "support.json"


def metadata(title, public=False, description=None):
    # Do not put query strings (which can contain invite tokens) in previews.
    base = (current_app.config.get("SITE_URL") or request.url_root).rstrip("/")
    return dict(page_title=title, page_description=description or DESCRIPTION,
                page_url=base + request.path, site_base=base, public_page=public)


@lru_cache(maxsize=64)
def _read_page(path, modified):
    return Path(path).read_text(encoding="utf-8")


@lru_cache(maxsize=64)
def _compile_page(path, modified):
    """Compile a static page as a Jinja template, cached by mtime.

    The pages in static/ are served only through render_page() (never as raw
    static files), so they double as templates. That is what lets them pull in
    shared markup with {% include 'partials/...' %} instead of each carrying its
    own copy of the sidebar / notification bell / theme-toggle handler.
    """
    source = Path(path).read_text(encoding="utf-8")
    return current_app.jinja_env.from_string(source)


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
    modified = path.stat().st_mtime_ns
    source = _read_page(str(path), modified)
    title = re.search(r"<title>(.*?)</title>", source, re.S).group(1)
    head = render_template("shared-head.html", **metadata(
        title, public=filename in {
            "privacy-policy.html", "terms-of-service.html",
            "about.html", "careers.html", "cookie-policy.html",
            "status.html", "subprocessors.html"
        }
    ))
    # Expand {% include %} first, then substitute the head, so the injected
    # markup is never re-parsed as template syntax.
    page = _compile_page(str(path), modified).render(**metadata(title))
    page = page.replace("<!-- shared-head -->", head, 1)
    # Clean routes can be nested (/sync/room). Local assets and page links
    # were authored relative to the static root, not the current URL folder.
    # This runs after rendering so partials get the same treatment.
    response = make_response(_RootAssetParser(page).normalized())
    response.headers["Cache-Control"] = "no-store"
    return response


@lru_cache(maxsize=1)
def _support_content():
    return json.loads(SUPPORT_CONTENT_PATH.read_text(encoding="utf-8"))


def support_page_context(slug):
    """Build the shared support-page context, or return None for an unknown URL."""
    content = _support_content()
    page = content["articles"].get(slug)
    if page is None:
        return None

    ordered_pages = []

    def collect(items):
        for item in items:
            ordered_pages.append(item)
            collect(item.get("children", []))

    for group in content["groups"]:
        collect(group["items"])
    article_paths = {article["path"]: article for article in content["articles"].values()}
    article_tails = {
        article["path"].rsplit("/", 1)[-1]: article
        for article in content["articles"].values() if article["path"]
    }

    page_order = [item["path"] for item in ordered_pages]
    if "faq" not in page_order:
        page_order.append("faq")
    if "contact" not in page_order:
        page_order.append("contact")
    
    current_path = page["path"]
    current_index = page_order.index(current_path) if current_path in page_order else -1
    previous_path = page_order[current_index - 1] if current_index > 0 else None
    next_index = current_index + 1
    next_path = page_order[next_index] if next_index < len(page_order) else None

    title = "VooHr Support | Overview" if slug == "" else f"{page['title']} | VooHr Support"
    description = page["description"]
    search_items = [
        {"label": article["title"], "url": "/support" + ("/" + key if key else "")}
        for key, article in content["articles"].items() if key
    ]
    search_items.extend(
        {"label": item["question"], "url": "/support/faq#" + item["id"]}
        for item in content["faq"]
    )
    return {
        **metadata(title, public=True, description=description),
        "page_description": description,
        "support_page": page,
        "support_slug": slug,
        "support_groups": content["groups"],
        "support_articles": content["articles"],
        "support_related": {
            item: article_paths.get(item) or article_tails.get(item)
            for item in page.get("related", [])
        },
        "support_faq": content["faq"],
        "support_search_items": search_items,
        "support_previous": content["articles"].get(previous_path) if previous_path else None,
        "support_next": content["articles"].get(next_path) if next_path else None,
        "support_previous_path": previous_path,
        "support_next_path": next_path,
        "support_overview": content["articles"][""],
    }


def render_support_page(slug):
    context = support_page_context(slug)
    if context is None:
        return None
    page = render_template("support.html", **context)
    response = make_response(_RootAssetParser(page).normalized())
    response.headers["Cache-Control"] = "no-store"
    return response

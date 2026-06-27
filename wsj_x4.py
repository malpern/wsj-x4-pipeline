#!/usr/bin/env python3
"""
wsj_x4.py — Build a fresh EPUB of the latest Wall Street Journal articles,
formatted for the Xteink X4 (4.3" e-ink, CrossPoint firmware), and deliver it
to the device over WebDAV.

Trigger:  python wsj_x4.py   (i.e. "Build WSJ for X4")

Auth model: a Playwright *persistent* Chromium profile. You log into WSJ once
(`python wsj_x4.py --login`, handles 2FA), and the session is reused silently on
every later run. Your password and cookies live ONLY in that profile dir on disk
(~/.config/wsj_x4/chrome-profile) and are never committed to git.

This does NOT bypass the paywall. It reads subscriber-accessible article text
using your own authenticated session.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse

# ---------------------------------------------------------------------------
# Config (edit these freely)
# ---------------------------------------------------------------------------

# Ordered list of (section name, RSS url). Dead feeds are skipped, not fatal.
FEEDS: list[tuple[str, str]] = [
    ("World",      "https://feeds.a.dj.com/rss/RSSWorldNews.xml"),
    ("Markets",    "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"),
    ("Business",   "https://feeds.a.dj.com/rss/WSJcomUSBusiness.xml"),
    ("Technology", "https://feeds.a.dj.com/rss/RSSWSJD.xml"),
    ("Opinion",    "https://feeds.a.dj.com/rss/RSSOpinion.xml"),
]

# Delivery: HTTP WebDAV PUT to the device (NOT a write to the read-only
# /Volumes/crosspoint.local mount).
X4_WEBDAV_BASE = "http://crosspoint.local"
X4_WEBDAV_SUBDIR = "WSJ"          # collection folder on the device; "" = root

LOCAL_STAGING_DIR = Path("~/Documents/X4").expanduser()
PROFILE_DIR = Path("~/.config/wsj_x4/chrome-profile").expanduser()
SEEN_PATH = Path("~/.config/wsj_x4/seen.json").expanduser()

DEFAULT_LIMIT = 30
MIN_TEXT_CHARS = 800              # below this => treat as paywalled / not extractable
FETCH_TIMEOUT_MS = 35000
DELAY_RANGE = (1.0, 3.0)          # polite randomized delay between article fetches

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# Markers that indicate we hit a wall instead of the article.
PAYWALL_MARKERS = (
    "continue reading your article with",
    "subscribe to continue",
    "sign in to continue",
    "to read the full story",
    "become a member",
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Article:
    section: str
    title: str
    url: str
    published: datetime | None = None
    author: str = ""
    body_html: str = ""          # cleaned, e-ink-ready HTML
    skip_reason: str = ""        # set if the article was dropped

    @property
    def included(self) -> bool:
        return not self.skip_reason and bool(self.body_html)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print(msg, flush=True)


def normalize_url(url: str) -> str:
    """Strip query/fragment so the same story across feeds dedups to one key."""
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, p.path.rstrip("/"), "", "", "")).lower()


def load_seen() -> set[str]:
    try:
        return set(json.loads(SEEN_PATH.read_text()))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_seen(seen: set[str]) -> None:
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(sorted(seen), indent=0))


# ---------------------------------------------------------------------------
# 1. RSS discovery
# ---------------------------------------------------------------------------

def discover(feeds: list[tuple[str, str]]) -> list[Article]:
    import feedparser

    found: dict[str, Article] = {}     # keyed by normalized url (dedup)
    for section, feed_url in feeds:
        parsed = feedparser.parse(feed_url)
        if parsed.bozo and not parsed.entries:
            log(f"  ! feed skipped (unreadable): {section} <{feed_url}>")
            continue
        for e in parsed.entries:
            link = getattr(e, "link", "")
            if not link:
                continue
            key = normalize_url(link)
            if key in found:
                continue           # first feed wins; preserves section order
            pub = None
            if getattr(e, "published_parsed", None):
                pub = datetime.fromtimestamp(time.mktime(e.published_parsed), tz=timezone.utc)
            author = getattr(e, "author", "") or ""
            found[key] = Article(
                section=section,
                title=getattr(e, "title", "(untitled)").strip(),
                url=link,
                published=pub,
                author=author,
            )
        log(f"  + {section}: {sum(1 for a in found.values() if a.section == section)} unique so far")
    # newest first; undated items sort last
    return sorted(
        found.values(),
        key=lambda a: a.published or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )


# ---------------------------------------------------------------------------
# 2. Fetch + extract (authenticated)
# ---------------------------------------------------------------------------

def looks_like_wall(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in PAYWALL_MARKERS)


def extract_body(raw_html: str, keep_images: bool) -> tuple[str, str, str]:
    """Return (title, author, cleaned_html). cleaned_html='' if extraction failed."""
    from readability import Document
    from bs4 import BeautifulSoup

    doc = Document(raw_html)
    title = (doc.short_title() or "").strip()
    summary_html = doc.summary(html_partial=True)

    soup = BeautifulSoup(summary_html, "html.parser")

    # readability sometimes under-extracts WSJ; fall back to <article> paragraphs.
    text_len = len(soup.get_text(strip=True))
    if text_len < MIN_TEXT_CHARS:
        full = BeautifulSoup(raw_html, "html.parser")
        container = (
            full.select_one("article")
            or full.select_one('section[subscriptions-section="content"]')
            or full.select_one("div.article-content")
        )
        if container:
            paras = container.find_all("p")
            if paras:
                soup = BeautifulSoup("".join(str(p) for p in paras), "html.parser")

    # author (best-effort) from the original page meta
    author = ""
    meta_soup = BeautifulSoup(raw_html, "html.parser")
    for sel in ('meta[name="author"]', 'meta[property="article:author"]'):
        m = meta_soup.select_one(sel)
        if m and m.get("content"):
            author = m["content"].strip()
            break

    # Strip clutter and (by default) images for e-ink.
    for tag in soup(["script", "style", "noscript", "iframe", "form", "aside", "nav", "button"]):
        tag.decompose()
    if not keep_images:
        for tag in soup(["img", "figure", "picture", "svg", "video"]):
            tag.decompose()
    # collapse empty paragraphs
    for p in soup.find_all("p"):
        if not p.get_text(strip=True) and not p.find("img"):
            p.decompose()

    cleaned = str(soup).strip()
    if len(BeautifulSoup(cleaned, "html.parser").get_text(strip=True)) < MIN_TEXT_CHARS:
        return title, author, ""
    return title, author, cleaned


def fetch_all(articles: list[Article], keep_images: bool, headed: bool) -> None:
    from playwright.sync_api import sync_playwright

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=not headed,
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.new_page()
        for i, art in enumerate(articles, 1):
            label = art.title[:60]
            try:
                page.goto(art.url, wait_until="domcontentloaded", timeout=FETCH_TIMEOUT_MS)
                page.wait_for_timeout(1200)  # let late body content settle
                raw = page.content()
            except Exception as e:  # noqa: BLE001 - per-article soft fail
                art.skip_reason = f"fetch error: {type(e).__name__}"
                log(f"  [{i}/{len(articles)}] SKIP {label} -- {art.skip_reason}")
                continue

            title, author, body = extract_body(raw, keep_images)
            if title:
                art.title = title
            if author and not art.author:
                art.author = author

            if not body or looks_like_wall(body):
                art.skip_reason = "paywalled / insufficient text"
                log(f"  [{i}/{len(articles)}] SKIP {label} -- {art.skip_reason}")
            else:
                art.body_html = body
                log(f"  [{i}/{len(articles)}] ok   {label}")

            time.sleep(random.uniform(*DELAY_RANGE))
        ctx.close()


# ---------------------------------------------------------------------------
# Login helper
# ---------------------------------------------------------------------------

def do_login() -> None:
    from playwright.sync_api import sync_playwright

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    log("Opening a browser. Log into WSJ (2FA is fine), then CLOSE the window.")
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=False, user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.new_page()
        page.goto("https://www.wsj.com/", wait_until="domcontentloaded")
        # Wait until you finish logging in and close the window/tab. The
        # persistent profile saves your session to disk automatically.
        try:
            page.wait_for_event("close", timeout=0)
        except Exception:  # noqa: BLE001 - context torn down on window close
            pass
        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass
    log("Session saved. Future runs reuse it automatically.")


# ---------------------------------------------------------------------------
# 3. Build EPUB
# ---------------------------------------------------------------------------

def build_epub(articles: list[Article], out_path: Path) -> None:
    from ebooklib import epub

    today = datetime.now().strftime("%Y-%m-%d")
    book = epub.EpubBook()
    book.set_identifier(f"wsj-x4-{datetime.now().strftime('%Y%m%d-%H%M')}")
    book.set_title(f"WSJ Latest - {today}")
    book.set_language("en")
    book.add_author("The Wall Street Journal")

    chapters_by_section: dict[str, list] = {}
    spine: list = ["nav"]
    seen_sections: list[str] = []

    for idx, art in enumerate(articles):
        if art.section not in seen_sections:
            seen_sections.append(art.section)
        byline = f"<p><em>{art.author}</em></p>" if art.author else ""
        date_str = art.published.astimezone().strftime("%b %-d, %Y") if art.published else ""
        meta_line = f"<p><small>{art.section} &middot; {date_str}</small></p>" if date_str else \
                    f"<p><small>{art.section}</small></p>"
        source = f'<hr/><p><small>Source: <a href="{art.url}">{art.url}</a></small></p>'
        ch = epub.EpubHtml(
            title=art.title,
            file_name=f"art_{idx:03d}.xhtml",
            lang="en",
        )
        ch.content = (
            f"<html><head></head><body>"
            f"<h1>{art.title}</h1>{byline}{meta_line}"
            f"{art.body_html}{source}"
            f"</body></html>"
        )
        book.add_item(ch)
        chapters_by_section.setdefault(art.section, []).append(ch)
        spine.append(ch)

    # TOC grouped by section, in feed order
    book.toc = [
        (epub.Section(section), tuple(chapters_by_section[section]))
        for section in seen_sections
        if section in chapters_by_section
    ]
    book.add_item(epub.EpubNcx())   # EPUB2 ncx (CrossPoint compatibility)
    book.add_item(epub.EpubNav())   # EPUB3 nav
    book.spine = spine

    out_path.parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(str(out_path), book)


# ---------------------------------------------------------------------------
# 4. Deliver over WebDAV
# ---------------------------------------------------------------------------

def device_reachable() -> bool:
    import requests
    try:
        r = requests.request("PROPFIND", X4_WEBDAV_BASE + "/", timeout=5,
                             headers={"Depth": "0"})
        return r.status_code < 500
    except requests.RequestException:
        try:
            return requests.get(X4_WEBDAV_BASE + "/", timeout=5).status_code < 500
        except requests.RequestException:
            return False


def deliver(epub_path: Path) -> str:
    """PUT the EPUB to the device. Returns the remote URL on success."""
    import requests

    base = X4_WEBDAV_BASE.rstrip("/")
    if X4_WEBDAV_SUBDIR:
        coll = f"{base}/{X4_WEBDAV_SUBDIR}"
        # MKCOL is idempotent enough: 201 created, 405 already exists.
        requests.request("MKCOL", coll, timeout=10)
        remote = f"{coll}/{epub_path.name}"
    else:
        remote = f"{base}/{epub_path.name}"

    with open(epub_path, "rb") as fh:
        r = requests.put(remote, data=fh, timeout=120,
                        headers={"Content-Type": "application/epub+zip"})
    if r.status_code not in (200, 201, 204):
        raise RuntimeError(f"WebDAV PUT failed: HTTP {r.status_code}")
    return remote


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Build WSJ EPUB for the Xteink X4.")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="max articles")
    ap.add_argument("--images", action="store_true", help="keep images (default: drop)")
    ap.add_argument("--include-seen", action="store_true", help="don't skip already-delivered articles")
    ap.add_argument("--no-sync", action="store_true", help="build only; don't push to device")
    ap.add_argument("--headed", action="store_true", help="show the browser while fetching")
    ap.add_argument("--login", action="store_true", help="(re)authenticate WSJ, then exit")
    args = ap.parse_args()

    if args.login:
        do_login()
        return 0

    log("== Build WSJ for X4 ==")
    log("1. Discovering articles from RSS...")
    articles = discover(FEEDS)

    seen = load_seen()
    if not args.include_seen:
        before = len(articles)
        articles = [a for a in articles if normalize_url(a.url) not in seen]
        log(f"   filtered {before - len(articles)} already-delivered articles")

    articles = articles[: args.limit]
    if not articles:
        log("Nothing new to build. (Try --include-seen.)")
        return 0

    log(f"2. Fetching {len(articles)} articles in your WSJ session...")
    fetch_all(articles, keep_images=args.images, headed=args.headed)

    included = [a for a in articles if a.included]
    skipped = [a for a in articles if not a.included]
    if not included:
        log("No articles could be extracted. If everything was paywalled, "
            "your session may have expired -- run:  python wsj_x4.py --login")
        return 1

    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    fname = f"WSJ Latest - {stamp}.epub"
    staged = LOCAL_STAGING_DIR / fname
    log(f"3. Building EPUB ({len(included)} articles)...")
    build_epub(included, staged)

    delivered_to = str(staged)
    synced = False
    if args.no_sync:
        log("4. --no-sync: left EPUB in local staging.")
    elif device_reachable():
        log("4. Delivering to device over WebDAV...")
        try:
            delivered_to = deliver(staged)
            synced = True
        except Exception as e:  # noqa: BLE001
            log(f"   ! delivery failed ({e}); kept local copy.")
    else:
        log("4. Device offline (crosspoint.local unreachable) -- kept local copy.")

    if synced:
        for a in included:
            seen.add(normalize_url(a.url))
        save_seen(seen)

    # ---- Report ----
    by_section: dict[str, int] = {}
    for a in included:
        by_section[a.section] = by_section.get(a.section, 0) + 1

    log("\n===== Report =====")
    log(f"Included: {len(included)} articles")
    for s, n in by_section.items():
        log(f"    {s}: {n}")
    if skipped:
        log(f"Skipped: {len(skipped)}")
        for a in skipped:
            log(f"    - {a.title[:60]}  ({a.skip_reason})")
    log(f"Sync: {'delivered to device' if synced else 'NOT synced (local only)'}")
    log(f"Output: {delivered_to}")
    if not synced:
        log(f"Local file: {staged}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

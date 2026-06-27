# WSJ → Xteink X4

Build one fresh EPUB of the latest Wall Street Journal articles, formatted for a
**Xteink X4** (4.3" e-ink, 480×800, no touch, ESP32-C3, running **CrossPoint**
firmware), and deliver it to the device over **WebDAV**.

Run it with one command:

```bash
python wsj_x4.py        # "Build WSJ for X4"
```

## How it works

1. Discovers article URLs from WSJ RSS feeds (link discovery only — RSS has no
   bodies). Overlapping feeds are **deduplicated by canonical URL**.
2. Sorts newest-first, takes the newest 30 (`--limit N` to change).
3. Fetches full text **in your own authenticated WSJ session** (a persistent
   Playwright Chromium profile). It does **not** bypass the paywall.
4. Cleans each article for e-ink: strips ads/nav/promos/scripts, and **drops
   images by default** (the X4 chokes on GIF/progressive JPEG and is slow on big
   images). Use `--images` to keep them.
5. Builds a valid EPUB (EPUB3 `nav` + EPUB2 `ncx` TOC) grouped by section.
6. Delivers it to the device with an HTTP **WebDAV `PUT`** to
   `http://crosspoint.local/WSJ/`.

It also keeps a small **seen-cache** so repeat runs don't re-deliver articles you
already got (`--include-seen` to override).

## Security — your subscription stays private

Your WSJ login lives **only** in the Playwright profile on disk at
`~/.config/wsj_x4/chrome-profile/` (outside this repo). Cookies, tokens, and
login state are **never committed** — `.gitignore` blocks profile dirs, cookie
files, `.env`, and `*.epub`. There is no password stored anywhere; auth is the
browser profile.

## Setup (macOS)

```bash
# 1. virtualenv
python3 -m venv .venv
source .venv/bin/activate

# 2. dependencies
pip install -r requirements.txt

# 3. Playwright's browser (one-time)
playwright install chromium

# 4. Log into WSJ once (opens a real browser; 2FA is fine).
#    The session persists for all later runs.
python wsj_x4.py --login
```

When the session eventually expires (everything comes back paywalled), just run
`python wsj_x4.py --login` again, or delete `~/.config/wsj_x4/chrome-profile` to
start fresh.

## Delivery / WebDAV

The X4 mounts read-only in Finder, so the script does **not** write to
`/Volumes/crosspoint.local`. Instead it talks WebDAV over HTTP directly:

- `MKCOL http://crosspoint.local/WSJ/` (creates the collection if missing)
- `PUT  http://crosspoint.local/WSJ/WSJ Latest - YYYY-MM-DD-HHMM.epub`

Before building, it checks the device is reachable. If the X4 is **off / off-WiFi**,
the EPUB is left in `~/Documents/X4/` and the run reports it was not synced.

Change the target in the config block at the top of `wsj_x4.py`
(`X4_WEBDAV_BASE`, `X4_WEBDAV_SUBDIR`).

## Options

| flag | effect |
|------|--------|
| `--limit N` | max articles (default 30) |
| `--images` | keep images (default: dropped for e-ink) |
| `--include-seen` | don't skip already-delivered articles |
| `--no-sync` | build only; leave EPUB in local staging |
| `--headed` | show the browser while fetching (use if WSJ blocks headless) |
| `--login` | (re)authenticate WSJ, then exit |

## Feeds

Edit the `FEEDS` list at the top of `wsj_x4.py`. Dead feeds are skipped, not
fatal. Verified-working WSJ feeds are seeded (World, Markets, Business,
Technology, Opinion).

## One-keystroke run

**Raycast** — Script Command:

```bash
#!/bin/bash
# @raycast.title Build WSJ for X4
# @raycast.mode fullOutput
cd /Users/malpern/local-code/x4-pipeline && ./.venv/bin/python wsj_x4.py
```

**Keyboard Maestro** — single *Execute Shell Script* action:

```bash
cd /Users/malpern/local-code/x4-pipeline && ./.venv/bin/python wsj_x4.py
```

## A note on terms

This reads paywalled content you pay for, for personal offline reading on your own
device — it does not bypass the paywall. Automated extraction is nonetheless a gray
area under WSJ's terms of service; use it for personal use only.

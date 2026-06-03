#!/usr/bin/env python3
"""
scrape_prospects.py
-------------------
Parses sahl-top-100-prospects.html, extracts all player page URLs,
fetches each one, and saves the HTML locally to ./player_pages/

Usage:
    python scrape_prospects.py

Output:
    ./player_pages/player_XXXXX.html  (one file per player)
    ./player_pages/fetch_log.txt      (success/failure log)
"""

# --- tools/ -> repo-root bootstrap (added during tools/ move) ---
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
# --- end bootstrap ---

import os
import re
import time
from pathlib import Path
import requests
from html.parser import HTMLParser

# ── Config ────────────────────────────────────────────────────────────────────
_SCRIPT_DIR = Path(__file__).resolve().parent.parent
SOURCE_FILE = str(_SCRIPT_DIR / "sahl" / "sahl-top-100-prospects.html")
OUTPUT_DIR  = str(_SCRIPT_DIR / "sahl" / "player_pages")
DELAY       = 1.0   # seconds between requests — be polite to the server
# ──────────────────────────────────────────────────────────────────────────────


class PlayerLinkParser(HTMLParser):
    """Pulls every href that matches the player page pattern."""

    def __init__(self):
        super().__init__()
        self.links = []  # list of (name, url) tuples

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            attrs_dict = dict(attrs)
            href = attrs_dict.get("href", "")
            if re.search(r"player_\d+\.html", href):
                self._pending_href = href
            else:
                self._pending_href = None

    def handle_data(self, data):
        if getattr(self, "_pending_href", None):
            self.links.append((data.strip(), self._pending_href))
            self._pending_href = None

    def handle_endtag(self, tag):
        if tag == "a":
            self._pending_href = None


def fetch_html(url: str, timeout: int = 30) -> str:
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    if response.encoding:
        response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def main():
    # 1. Read the source file
    if not os.path.exists(SOURCE_FILE):
        print(f"ERROR: '{SOURCE_FILE}' not found. Run this script from the "
              "same directory as that file.")
        return

    with open(SOURCE_FILE, "r", encoding="utf-8", errors="replace") as f:
        html = f.read()

    # 2. Parse player links
    parser = PlayerLinkParser()
    parser.feed(html)

    # Deduplicate while preserving order
    seen = set()
    players = []
    for name, url in parser.links:
        if url not in seen:
            seen.add(url)
            players.append((name, url))

    print(f"Found {len(players)} unique player links.\n")

    # 3. Create output directory
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    log_path = os.path.join(OUTPUT_DIR, "fetch_log.txt")
    log_lines = []

    # 4. Fetch each player page
    for i, (name, url) in enumerate(players, 1):
        # Derive filename from URL
        match = re.search(r"(player_\d+\.html)", url)
        filename = match.group(1) if match else f"player_{i}.html"
        out_path = os.path.join(OUTPUT_DIR, filename)

        # Skip if already downloaded
        if os.path.exists(out_path):
            msg = f"[{i:>3}/{len(players)}] SKIP (already exists)  {name} → {filename}"
            print(msg)
            log_lines.append(msg)
            continue

        try:
            html_content = fetch_html(url)
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(html_content)
            size_kb = len(html_content) / 1024
            msg = f"[{i:>3}/{len(players)}] OK    {name} → {filename}  ({size_kb:.1f} KB)"
            print(msg)
            log_lines.append(msg)
        except Exception as e:
            msg = f"[{i:>3}/{len(players)}] FAIL  {name} → {url}\n        Error: {e}"
            print(msg)
            log_lines.append(msg)

        # Polite delay
        if i < len(players):
            time.sleep(DELAY)

    # 5. Write log
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(log_lines))

    successes = sum(1 for l in log_lines if "OK" in l or "SKIP" in l)
    failures  = sum(1 for l in log_lines if "FAIL" in l)
    print(f"\nDone. {successes} saved, {failures} failed.")
    print(f"Files are in: ./{OUTPUT_DIR}/")
    print(f"Log written to: {log_path}")


if __name__ == "__main__":
    main()

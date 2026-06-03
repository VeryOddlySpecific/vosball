#!/usr/bin/env python3
"""
StatsPlus Paper News

Fetches league home page, top 5 news articles, and N days of one-liners from
StatsPlus; assembles each into a paper-style HTML (newspaper look); saves
HTML under a single dated directory. Use --png to also render each HTML to PNG.

  {league}_news_{YYYYMMDD}

With --magazine, builds a combined multi-page newspaper/magazine (league_###_magazine.html):
home, then articles, then one-liners, laid out on A4-sized
pages (--page-size, default a4) with content flowing to the next page when full.

Dependencies: requests, beautifulsoup4. For PNG and magazine PDF: playwright
  (then run: playwright install chromium).

Usage:
  python statsplus_paper_news.py --league uba --league-id 106
  python statsplus_paper_news.py --league uba --league-id 106 --days 14 --output ./out
  python statsplus_paper_news.py --league uba --league-id 106 --magazine --page-size letter
"""

# --- repo-root + core/ path bootstrap ---
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _p in (_ROOT, _os.path.join(_ROOT, "core")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
# --- end bootstrap ---

import argparse
import re
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None  # type: ignore

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_LEAGUE_URL_CONFIG = _SCRIPT_DIR / "config" / "league_url.json"


def load_league_web_urls(config_path: Path) -> Dict[str, str]:
    """Load league base web URLs from config/league_url.json.
    Config stores API URLs (ending in /api); this strips that suffix to get
    the web base URL used for scraping HTML pages."""
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    result: Dict[str, str] = {}
    for k, v in raw.items():
        key = str(k).strip().lower()
        val = str(v).strip().rstrip("/")
        if val.endswith("/api"):
            val = val[:-4]  # strip /api suffix → web base URL
        if key and val:
            result[key] = val
    return result

# Page dimensions for magazine PDF (width, height) with units for CSS/Playwright
PAGE_SIZES: Dict[str, Tuple[str, str]] = {
    "letter": ("8.5in", "11in"),
    "a4": ("210mm", "297mm"),
}


def parse_page_size(page_size_arg: str) -> Tuple[str, str]:
    """Parse --page-size: 'letter', 'a4', or 'WxH' / 'WxHin' / 'WxHmm'. Return (width, height)."""
    if page_size_arg.lower() in PAGE_SIZES:
        return PAGE_SIZES[page_size_arg.lower()]
    m = re.match(r"^([\d.]+)\s*[xX×]\s*([\d.]+)\s*(in|mm)?$", page_size_arg.strip())
    if not m:
        raise ValueError(
            f"Invalid --page-size '{page_size_arg}'. Use 'letter', 'a4', or WxH (e.g. 8.5x11 or 210x297mm)."
        )
    w, h, unit = m.group(1), m.group(2), (m.group(3) or "in").lower()
    return (f"{w}{unit}", f"{h}{unit}")


def construct_content_url(base_url: str, content_filename: str) -> str:
    base_url = base_url.rstrip("/")
    return f"{base_url}/reports/news/html/leagues/{content_filename}"


def construct_box_score_url(base_url: str, game_id: int) -> str:
    base_url = base_url.rstrip("/")
    return f"{base_url}/reports/news/html/box_scores/game_box_{game_id}.html"


def content_base_url(base_url: str) -> str:
    """Base URL for news HTML content (teams, players, leagues, etc.). Relative hrefs like ../teams/... resolve here."""
    return f"{base_url.rstrip('/')}/reports/news/html"


def rewrite_relative_hrefs(html: str, content_base: str) -> str:
    """Replace relative href=\"../... and href='../... with absolute URLs using content_base."""
    html = html.replace('href="../', f'href="{content_base}/')
    html = html.replace("href='../", f"href='{content_base}/")
    return html


def dates_for_scores(date_yyyymmdd: str, num_days: int) -> List[date]:
    """Return the last num_days calendar days ending on the given date (YYYYMMDD)."""
    try:
        y, m, d = int(date_yyyymmdd[:4]), int(date_yyyymmdd[4:6]), int(date_yyyymmdd[6:8])
        end = date(y, m, d)
    except (ValueError, IndexError):
        end = date.today()
    return [end - timedelta(days=(num_days - 1 - i)) for i in range(num_days)]


def fetch_html(url: str, timeout: int = 30) -> str:
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    if response.encoding:
        response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def extract_message_body(html_content: str) -> str:
    """Extract article body from second <td> in table.databg; return markdown-like text."""
    if BeautifulSoup is None:
        raise ImportError("BeautifulSoup4 required. pip install beautifulsoup4")
    soup = BeautifulSoup(html_content, "html.parser")
    content_table = soup.find("table", class_="databg") or soup.find(
        "table", class_=re.compile(r"databg", re.I)
    )
    if not content_table:
        return ""
    first_row = content_table.find("tr")
    if not first_row:
        return ""
    td_tags = first_row.find_all("td")
    if len(td_tags) < 2:
        return ""
    message_td = td_tags[1]
    markdown_parts = []

    def process(el):
        if isinstance(el, str):
            t = re.sub(r"[\n\t\r]+", " ", el)
            t = re.sub(r" +", " ", t)
            if t.strip():
                markdown_parts.append(("text", t))
        elif hasattr(el, "name"):
            if el.name == "br":
                markdown_parts.append(("newline", "\n"))
            elif el.name == "a":
                href = el.get("href", "")
                link_text = el.get_text().strip()
                if link_text:
                    if href and (href.startswith("http://") or href.startswith("https://")):
                        markdown_parts.append(("text", f"[{link_text}]({href})"))
                    else:
                        markdown_parts.append(("text", link_text))
            elif el.name in ["strong", "b"]:
                text = el.get_text().strip()
                if text:
                    markdown_parts.append(("text", f"**{text}**"))
            elif el.name == "span":
                style = str(el.get("style", "")).lower()
                if "font-weight:bold" in style or "font-weight: bold" in style:
                    text = el.get_text().strip()
                    if text:
                        markdown_parts.append(("text", f"**{text}**"))
                else:
                    for child in el.children:
                        process(child)
            else:
                for child in el.children:
                    process(child)

    for child in message_td.children:
        process(child)

    markdown_text = ""
    for part_type, part_content in markdown_parts:
        if part_type == "newline":
            markdown_text += "\n"
        else:
            part_content = part_content.strip()
            if not part_content:
                continue
            if markdown_text:
                lc = markdown_text[-1] if markdown_text else ""
                fc = part_content[0] if part_content else ""
                needs_space = (
                    lc not in ("\n", " ", ".", ",", "!", "?", ":", ";", "-", "'", '"', "(")
                    and fc not in (".", ",", "!", "?", ":", ";", "-", "'", '"', ")", "]")
                )
                if needs_space and not part_content.startswith(" "):
                    markdown_text += " "
            markdown_text += part_content

    placeholders = {}
    n = 0
    for m in re.finditer(r"\[([^\]]+)\]\(([^)]+)\)", markdown_text):
        ph = f"__L{n}__"
        placeholders[ph] = m.group(0)
        markdown_text = markdown_text.replace(m.group(0), ph, 1)
        n += 1
    for m in re.finditer(r"\*\*([^*]+)\*\*", markdown_text):
        ph = f"__B{n}__"
        placeholders[ph] = m.group(0)
        markdown_text = markdown_text.replace(m.group(0), ph, 1)
        n += 1
    markdown_text = re.sub(r"([,;:!?.])([A-Za-z])", r"\1 \2", markdown_text)
    for ph, orig in placeholders.items():
        markdown_text = markdown_text.replace(ph, orig)
    lines = [line.strip() for line in markdown_text.split("\n")]
    result = "\n".join(lines)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


def markdown_like_to_html(text: str) -> str:
    """Convert markdown-like (**, [t](url), newlines) to HTML for paper body."""
    if not text:
        return ""
    # Protect links and bold
    placeholders = {}
    n = 0
    for m in re.finditer(r"\[([^\]]+)\]\(([^)]+)\)", text):
        ph = f"__L{n}__"
        placeholders[ph] = ('a', m.group(1), m.group(2))
        text = text.replace(m.group(0), ph, 1)
        n += 1
    for m in re.finditer(r"\*\*([^*]+)\*\*", text):
        ph = f"__B{n}__"
        placeholders[ph] = ('b', m.group(1))
        text = text.replace(m.group(0), ph, 1)
        n += 1
    parts = []
    for para in re.split(r"\n\n+", text):
        para = para.strip()
        if not para:
            continue
        line = re.sub(r"\n", " ", para)
        for ph, val in placeholders.items():
            if ph in line:
                if val[0] == "a":
                    line = line.replace(ph, f'<a href="{val[2]}">{val[1]}</a>')
                else:
                    line = line.replace(ph, f"<strong>{val[1]}</strong>")
        parts.append(f"<p>{line}</p>")
    return "\n    ".join(parts)


# Inner section fragment (placeholders: PAPER_NAME, DATELINE, SECTION_TAG, H1, DECK, BYLINE, BODY_HTML)
PAPER_SECTION_TEMPLATE = """  <div class="masthead">
    <div class="paper-name">{{PAPER_NAME}}</div>
    <div class="dateline">{{DATELINE}}</div>
  </div>
  <div class="section-tag">{{SECTION_TAG}}</div>
  <h1>{{H1}}</h1>
  <div class="deck">{{DECK}}</div>
  <div class="byline">{{BYLINE}}</div>
  <div class="columns">
    {{BODY_HTML}}
  </div>"""

# Full single-page document (placeholders: TITLE, SECTION_HTML)
PAPER_TEMPLATE = """
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #f2ead8; background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='4' height='4'%3E%3Crect width='4' height='4' fill='%23f2ead8'/%3E%3Ccircle cx='1' cy='1' r='0.6' fill='%23d9cdb0' opacity='0.4'/%3E%3C/svg%3E"); font-family: 'Source Serif 4', Georgia, serif; color: #1a1208; padding: 40px 20px; }
  .paper { max-width: 1200px; margin: 0 auto; background: #faf5e8; border: 1px solid #c8b98a; box-shadow: 4px 4px 20px rgba(0,0,0,0.18), inset 0 0 80px rgba(200,180,120,0.1); padding: 48px 56px 60px; }
  .masthead { text-align: center; border-bottom: 3px double #1a1208; padding-bottom: 12px; margin-bottom: 8px; }
  .paper-name { font-family: 'UnifrakturMaguntia', cursive; font-size: 52px; line-height: 1; letter-spacing: 1px; color: #0f0800; }
  .dateline { font-size: 11px; letter-spacing: 2px; text-transform: uppercase; color: #5a4a2a; margin-top: 6px; border-top: 1px solid #1a1208; border-bottom: 1px solid #1a1208; padding: 3px 0; }
  .section-tag { text-align: center; font-family: 'Playfair Display', serif; font-size: 11px; letter-spacing: 3px; text-transform: uppercase; color: #8b3a1a; margin: 14px 0 10px; }
  h1 { font-family: 'Playfair Display', serif; font-size: 42px; font-weight: 700; line-height: 1.1; text-align: center; margin-bottom: 6px; color: #0f0800; }
  .deck { font-family: 'Playfair Display', serif; font-style: italic; font-size: 17px; text-align: center; color: #3d2d10; margin-bottom: 16px; line-height: 1.4; }
  .byline { text-align: center; font-size: 11px; letter-spacing: 2px; text-transform: uppercase; color: #5a4a2a; border-top: 1px solid #c8b98a; border-bottom: 1px solid #c8b98a; padding: 5px 0; margin-bottom: 22px; }
  .columns { column-count: 2; column-gap: 36px; column-rule: 1px solid #c8b98a; }
  p { font-size: 15px; line-height: 1.75; margin-bottom: 14px; text-align: justify; hyphens: auto; font-weight: 300; }
  .wp-block-post-content p:first-child::first-letter { font-family: 'Playfair Display', serif; font-size: 62px; font-weight: 700; float: left; line-height: 0.78; margin-right: 6px; margin-top: 8px; color: #0f0800; }
  .day-head { font-family: 'Playfair Display', serif; font-weight: 700; font-size: 16px; margin-top: 18px; margin-bottom: 4px; color: #8b3a1a; }
  .stats-box { border: 1px solid #c8b98a; background: #f0e8d0; padding: 14px 18px; margin: 0 0 14px 0; break-inside: avoid; }
  .stats-box.compact { font-size: 12px; padding: 8px 10px; }
  .stats-box table { width: 100%; }
  .stats-box td, .stats-box th { padding: 2px 4px; }
  .linescore-table { font-size: 13px; margin: 8px 0; width: 100%; }
  .linescore-table td, .linescore-table th { padding: 2px 4px; }
  .standings-block { break-inside: avoid; margin-bottom: 18px; }
  .standings-block table { width: 100%; font-size: 13px; }
  .standings-block td, .standings-block th { padding: 3px 6px; }
  .standings-block th { text-align: left; }
  .standings-block .boxtitle { font-family: 'Playfair Display', serif; font-size: 14px; color: #8b3a1a; }
</style>

<div class="paper">
{{SECTION_HTML}}
</div>
"""

# Magazine: multi-page document with @page size and break-after between sections
# Placeholders: PAGE_WIDTH, PAGE_HEIGHT, SECTIONS
MAGAZINE_TEMPLATE = """
<style>
  @page { size: {{PAGE_WIDTH}} {{PAGE_HEIGHT}}; }
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #e0d8c4; font-family: 'Source Serif 4', Georgia, serif; color: #1a1208; }
  .magazine-page { break-after: page; }
  .paper { width: 90%; max-width: 90%; aspect-ratio: 22/34; margin: 0 auto 2em; background: #ebe3d2; border: none; box-shadow: none; padding: 40px 3% 48px; break-inside: auto; }
  .masthead { text-align: center; border-bottom: 3px double #1a1208; padding-bottom: 12px; margin-bottom: 8px; }
  section > .masthead ~ .masthead,
  section > .section-tag ~ .section-tag,
  section ~ section > h1 ~ h1,
  section ~ .deck ~ .deck { display: none; }
  section > .byline ~ .byline { margin: 15px 0 15px 0; }
  .paper-name { font-family: 'UnifrakturMaguntia', cursive; font-size: 52px; line-height: 1; letter-spacing: 1px; color: #0f0800; }
  .dateline { font-size: 11px; letter-spacing: 2px; text-transform: uppercase; color: #5a4a2a; margin-top: 6px; border-top: 1px solid #1a1208; border-bottom: 1px solid #1a1208; padding: 3px 0; }
  .section-tag { text-align: center; font-family: 'Playfair Display', serif; font-size: 11px; letter-spacing: 3px; text-transform: uppercase; color: #8b3a1a; margin: 14px 0 10px; }
  h1 { font-family: 'Playfair Display', serif; font-size: 42px; font-weight: 700; line-height: 1.1; text-align: center; margin-bottom: 6px; color: #0f0800; }
  .deck { font-family: 'Playfair Display', serif; font-style: italic; font-size: 17px; text-align: center; color: #3d2d10; margin-bottom: 16px; line-height: 1.4; }
  .byline { text-align: center; font-size: 11px; letter-spacing: 2px; text-transform: uppercase; color: #5a4a2a; border-top: 1px solid #c8b98a; border-bottom: 1px solid #c8b98a; padding: 5px 0; margin-bottom: 22px; }
  .columns { column-count: 2; column-gap: 36px; column-rule: 1px solid #c8b98a; }
  p { font-size: 15px; line-height: 1.75; margin-bottom: 14px; text-align: justify; hyphens: auto; font-weight: 300; }
  .wp-block-post-contentp:first-child::first-letter { font-family: 'Playfair Display', serif; font-size: 62px; font-weight: 700; float: left; line-height: 0.78; margin-right: 6px; margin-top: 8px; color: #0f0800; }
  .day-head { font-family: 'Playfair Display', serif; font-weight: 700; font-size: 16px; margin-top: 18px; margin-bottom: 4px; color: #8b3a1a; }
  .stats-box { border: 1px solid #c8b98a; background: #f0e8d0; padding: 14px 18px; margin: 0 0 14px 0; break-inside: avoid; }
  .stats-box.compact { font-size: 12px; padding: 8px 10px; }
  .stats-box table { width: 100%; }
  .stats-box td, .stats-box th { padding: 2px 4px; }
  .linescore-table { font-size: 13px; margin: 8px 0; width: 100%; }
  .linescore-table td, .linescore-table th { padding: 2px 4px; }
  .standings-block { break-inside: avoid; margin-bottom: 18px; }
  .standings-block table { width: 100%; font-size: 13px; }
  .standings-block td, .standings-block th { padding: 3px 6px; }
  .standings-block th { text-align: left; }
  .standings-block .boxtitle { font-family: 'Playfair Display', serif; font-size: 14px; color: #8b3a1a; }
</style>
{{SECTIONS}}"""


def build_magazine_html(
    sections: List[Tuple[str, str]],
    page_width: str,
    page_height: str,
) -> str:
    """Build a single HTML document. sections is a list of (section_type, html).
    Each type (home, standings, article, scores_roundup, daily_summary) gets one
    <section class="paper {type}"> containing all that type's content. Home and
    standings are wrapped in magazine-page for a page break."""
    # Group by type, preserving order of first occurrence
    grouped: Dict[str, List[str]] = {}
    order: List[str] = []
    for section_type, html in sections:
        if section_type not in grouped:
            order.append(section_type)
            grouped[section_type] = []
        grouped[section_type].append(html)
    parts = []
    for section_type in order:
        inner = "\n".join(grouped[section_type])
        if section_type in ("home", "standings"):
            parts.append(
                f'<div class="magazine-page"><section class="paper {section_type}">\n{inner}\n</section></div>\n'
            )
        else:
            parts.append(f'<section class="paper {section_type}">\n{inner}\n</section>\n')
    wrapped = "".join(parts)
    return (
        MAGAZINE_TEMPLATE.replace("{{PAGE_WIDTH}}", page_width)
        .replace("{{PAGE_HEIGHT}}", page_height)
        .replace("{{SECTIONS}}", wrapped)
    )


@dataclass
class NewsLink:
    article_id: int
    title: str
    filename: str


@dataclass
class FeaturedBlock:
    headline: str
    date_str: str
    body_html: str


@dataclass
class HomePageData:
    date_yyyymmdd: str
    date_display: str
    news_links: List[NewsLink]
    featured: List[FeaturedBlock]


@dataclass
class BoxScoreData:
    matchup_str: str
    final_score: str  # e.g. "5-3"
    recap_headline: str
    recap_html: str
    linescore_rows: List[Tuple[str, List[str], str, str, str]]  # (team_label, runs_per_inning, R, H, E)
    batting_tables_html: str
    pitching_tables_html: str


def parse_box_score(html: str) -> Optional[BoxScoreData]:
    """Parse a box score page; return None if structure is unrecognized."""
    if BeautifulSoup is None:
        return None
    soup = BeautifulSoup(html, "html.parser")

    recap_headline = ""
    recap_html = ""
    recap_subj = re.search(r"<!--RECAP_SUBJECT_START-->(.*?)<!--RECAP_SUBJECT_END-->", html, re.DOTALL)
    if recap_subj:
        recap_headline = recap_subj.group(1).strip()
    recap_text = re.search(r"<!--RECAP_TEXT_START-->(.*?)<!--RECAP_TEXT_END-->", html, re.DOTALL)
    if recap_text:
        recap_html = recap_text.group(1).strip()

    matchup_str = ""
    div = soup.find("div", class_="repsubtitle")
    if div:
        matchup_str = div.get_text(separator=" ", strip=True)
    if not matchup_str and soup.title:
        title = soup.title.string or ""
        if " at " in title:
            matchup_str = re.sub(r"^[^,]+,?\s*", "", title).strip()
            matchup_str = re.sub(r",\s*\d{2}/\d{2}/\d{4}.*$", "", matchup_str).strip()
    if not matchup_str:
        matchup_str = "Game"

    linescore_rows: List[Tuple[str, List[str], str, str, str]] = []
    for table in soup.find_all("table", class_="data"):
        header_cells = table.find_all("th", class_="dc")
        if not header_cells:
            continue
        header_text = " ".join(t.get_text(strip=True) for t in header_cells)
        if "R" not in header_text or "H" not in header_text:
            continue
        data_rows = table.find_all("tr")[1:3]
        if len(data_rows) < 2:
            continue
        for tr in data_rows:
            tds = tr.find_all("td")
            if len(tds) < 4:
                continue
            team_label = (tds[0].get_text(strip=True) or "").strip()
            runs = [td.get_text(strip=True) for td in tds[1:-3]]
            r = tds[-3].get_text(strip=True) if len(tds) >= 3 else ""
            h = tds[-2].get_text(strip=True) if len(tds) >= 2 else ""
            e = tds[-1].get_text(strip=True) if len(tds) >= 1 else ""
            if team_label:
                linescore_rows.append((team_label, runs, r, h, e))
        if linescore_rows:
            break

    final_score = ""
    if len(linescore_rows) >= 2:
        final_score = f"{linescore_rows[0][2]}-{linescore_rows[1][2]}"

    batting_tables_html = ""
    pitching_tables_html = ""
    for boxtitle in soup.find_all("th", class_="boxtitle"):
        label = (boxtitle.get_text(strip=True) or "").upper()
        if "BATTING LINESCORE" in label:
            parent = boxtitle.find_parent("tr")
            if not parent:
                continue
            next_row = parent.find_next_sibling("tr")
            if not next_row:
                continue
            for td in next_row.find_all("td", class_="databg"):
                tbl = td.find("table", class_=re.compile(r"sortable"))
                if tbl:
                    batting_tables_html += '<div class="stats-box compact">' + str(tbl) + "</div>"
        elif "PITCHING LINESCORE" in label:
            parent = boxtitle.find_parent("tr")
            if not parent:
                continue
            next_row = parent.find_next_sibling("tr")
            if not next_row:
                continue
            for td in next_row.find_all("td", class_="databg"):
                tbl = td.find("table", class_=re.compile(r"sortable"))
                if tbl:
                    pitching_tables_html += '<div class="stats-box compact">' + str(tbl) + "</div>"

    return BoxScoreData(
        matchup_str=matchup_str,
        final_score=final_score,
        recap_headline=recap_headline,
        recap_html=recap_html,
        linescore_rows=linescore_rows,
        batting_tables_html=batting_tables_html,
        pitching_tables_html=pitching_tables_html,
    )


def build_scores_roundup_body(
    days_games: List[Tuple[str, List[BoxScoreData]]],
) -> str:
    """Build HTML body for the scores roundup paper: grouped by date, then by game."""
    parts: List[str] = []
    for date_str, games in days_games:
        if not games:
            continue
        parts.append(f'<div class="day-head">{date_str}</div>')
        for box in games:
            subhead = box.matchup_str
            if box.final_score:
                subhead += f" — {box.final_score}"
            parts.append(f'<div class="day-head" style="font-size:14px; margin-top:12px;">{subhead}</div>')
            if box.recap_headline:
                parts.append(f"<p><strong>{box.recap_headline}</strong></p>")
            if box.recap_html:
                parts.append(f"<p>{box.recap_html}</p>")
            if box.linescore_rows:
                inning_count = len(box.linescore_rows[0][1])
                header_cells = "".join(f"<th class=\"dc\">{i+1}</th>" for i in range(inning_count))
                parts.append(
                    '<table class="linescore-table data">'
                    "<tr><th class=\"dl\">&nbsp;</th>"
                    f"{header_cells}"
                    '<th class="dc"><b>R</b></th><th class="dc"><b>H</b></th><th class="dc"><b>E</b></th></tr>'
                )
                for team_label, runs, r, h, e in box.linescore_rows:
                    run_cells = "".join(f'<td class="dc">{x}</td>' for x in runs)
                    parts.append(
                        f"<tr><td class=\"dl\">{team_label}</td>{run_cells}"
                        f'<td class="dc"><b>{r}</b></td><td class="dc"><b>{h}</b></td><td class="dc"><b>{e}</b></td></tr>'
                    )
                parts.append("</table>")
            if box.batting_tables_html:
                parts.append('<div class="day-head" style="font-size:12px;">Batting</div>')
                parts.append(box.batting_tables_html)
            if box.pitching_tables_html:
                parts.append('<div class="day-head" style="font-size:12px;">Pitching</div>')
                parts.append(box.pitching_tables_html)
    return "\n    ".join(parts) if parts else "<p>No games in this period.</p>"


def parse_home_page(html: str, league_id: int) -> HomePageData:
    if BeautifulSoup is None:
        raise ImportError("BeautifulSoup4 required. pip install beautifulsoup4")
    soup = BeautifulSoup(html, "html.parser")

    date_yyyymmdd = datetime.now().strftime("%Y%m%d")
    date_display = datetime.now().strftime("%A, %B %d, %Y")
    for div in soup.find_all("div", style=True):
        style = div.get("style", "")
        if "color:#FFFFFF" in style or "color: #FFFFFF" in style:
            text = div.get_text().strip()
            if re.match(r"\d{2}/\d{2}/\d{4}", text):
                parts = text.split("/")
                if len(parts) == 3:
                    month, day, year = parts[0], parts[1], parts[2]
                    date_yyyymmdd = f"{year}{month}{day}"
                    try:
                        dt = datetime(int(year), int(month), int(day))
                        date_display = dt.strftime("%A, %B %d, %Y")
                    except ValueError:
                        pass
                break

    news_links: List[NewsLink] = []
    for td in soup.find_all("td", class_="boxtitle"):
        if td.get_text().strip() != "NEWS":
            continue
        parent = td.find_parent("table")
        if not parent:
            continue
        ul = parent.find("ul")
        if not ul:
            continue
        for li in ul.find_all("li", limit=5):
            a = li.find("a", href=True)
            if not a:
                continue
            href = a.get("href", "")
            match = re.search(r"league_\d+_news_(\d+)\.html", href)
            if match:
                news_links.append(
                    NewsLink(
                        article_id=int(match.group(1)),
                        title=a.get_text().strip(),
                        filename=f"league_{league_id}_news_{match.group(1)}.html",
                    )
                )
        break

    featured: List[FeaturedBlock] = []
    for table in soup.find_all("table", class_="databg"):
        if table.find("td", class_="boxtitle"):
            continue
        tr = table.find("tr")
        if not tr:
            continue
        tds = tr.find_all("td")
        if len(tds) < 2:
            continue
        second = tds[1]
        spans = second.find_all("span", style=True)
        headline = ""
        date_str = ""
        for s in spans:
            st = str(s.get("style", "")).lower()
            if "font-weight:bold" in st and "15px" in st:
                headline = s.get_text().strip()
            elif "11px" in st:
                date_str = s.get_text().strip()
        if not headline:
            continue
        inner = second.decode_contents()
        for _ in range(5):
            inner = re.sub(r"^<span[^>]*>.*?</span>\s*", "", inner, count=1, flags=re.I | re.DOTALL)
            inner = re.sub(r"^<br\s*/?>\s*", "", inner, flags=re.I)
        body_html = inner.strip()
        featured.append(
            FeaturedBlock(headline=headline, date_str=date_str, body_html=body_html)
        )
    return HomePageData(
        date_yyyymmdd=date_yyyymmdd,
        date_display=date_display,
        news_links=news_links,
        featured=featured,
    )


def parse_scoreboard_box_links(html: str) -> List[int]:
    """Extract game IDs from box score links on a scoreboard page. Skips duplicates, preserves order."""
    seen: set = set()
    ids: List[int] = []
    for m in re.finditer(r"box_scores/game_box_(\d+)\.html", html):
        gid = int(m.group(1))
        if gid not in seen:
            seen.add(gid)
            ids.append(gid)
    return ids


# Standings default columns: Team, W, L, PCT, GB (first 5 columns)
STANDINGS_DEFAULT_COLUMNS = 5


def _standings_table_trim_columns(table) -> str:
    """Return table HTML with only the first STANDINGS_DEFAULT_COLUMNS columns (Team, W, L, PCT, GB)."""
    if BeautifulSoup is None:
        return str(table)
    rows_html = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        rows_html.append("<tr>" + "".join(str(c) for c in cells[:STANDINGS_DEFAULT_COLUMNS]) + "</tr>")
    if not rows_html:
        return str(table)
    attrs = getattr(table, "attrs", {})
    attr_parts = []
    for k, v in attrs.items():
        val = " ".join(v) if isinstance(v, list) else v
        attr_parts.append(f'{k}="{val}"')
    open_tag = "<table " + " ".join(attr_parts) + ">" if attr_parts else "<table>"
    return open_tag + "".join(rows_html) + "</table>"


def parse_standings_page(
    html: str, *, full_standings: bool = False
) -> Tuple[Optional[str], Optional[str]]:
    """Extract National League and American League standings from a StatsPlus standings page HTML.
    The standings page has multiple sections per league: divisions and wildcard. Each section is a
    row with td.boxtitle (or th.boxtitle) followed by a row containing a table.data.
    When full_standings is False (default), only Team, W, L, PCT, GB columns are kept.
    Returns (nl_html, al_html); each is the concatenation of all that league's sections (header + table per section).
    """
    if BeautifulSoup is None:
        return (None, None)
    soup = BeautifulSoup(html, "html.parser")
    nl_parts: List[str] = []
    al_parts: List[str] = []
    # Standings page uses td.boxtitle; home page snippet used th.boxtitle — support both
    for cell in soup.find_all(["td", "th"], class_="boxtitle"):
        text = (cell.get_text() or "").strip().upper()
        if "LEAGUE" not in text:
            continue
        is_nl = "NATIONAL" in text and "AMERICAN" not in text
        is_al = "AMERICAN" in text
        if not is_nl and not is_al:
            continue
        parent_tr = cell.find_parent("tr")
        if not parent_tr:
            continue
        next_tr = parent_tr.find_next_sibling("tr")
        if not next_tr:
            continue
        data_table = next_tr.find("table", class_=re.compile(r"data", re.I))
        if not data_table:
            continue
        title_text = (cell.get_text() or "").strip()
        header_html = (
            '<table class="databg" cellspacing="0" cellpadding="0">'
            f'<tr><td class="boxtitle">{title_text}</td></tr></table>'
        )
        table_html = str(data_table) if full_standings else _standings_table_trim_columns(data_table)
        section_block = header_html + "\n" + table_html
        if is_nl:
            nl_parts.append(section_block)
        else:
            al_parts.append(section_block)
    nl_html = "\n".join(nl_parts) if nl_parts else None
    al_html = "\n".join(al_parts) if al_parts else None
    return (nl_html, al_html)


def extract_paper_section_html(html: str) -> Optional[str]:
    """Extract the inner HTML of the .paper div from a full paper HTML document (for magazine reuse)."""
    if BeautifulSoup is None:
        return None
    soup = BeautifulSoup(html, "html.parser")
    paper = soup.find("div", class_="paper")
    if not paper:
        return None
    return paper.decode_contents()


def parse_news_oneliners(html: str, num_days: int) -> List[Tuple[str, List[str]]]:
    """Return list of (date_string, list of one-liner HTML strings) for the first num_days."""
    if BeautifulSoup is None:
        return []
    soup = BeautifulSoup(html, "html.parser")
    days: List[Tuple[str, List[str]]] = []
    current_date: Optional[str] = None
    current_lines: List[str] = []

    for tr in soup.find_all("tr"):
        th = tr.find("th", class_="dl")
        td = tr.find("td", class_=re.compile(r"dl.*wrap|wrap.*dl"))
        if th:
            if current_date is not None and current_lines:
                days.append((current_date, current_lines))
                if len(days) >= num_days:
                    return days
            current_date = th.get_text().strip()
            current_lines = []
        elif td and current_date is not None:
            current_lines.append(str(td.decode_contents()))
    if current_date and current_lines:
        days.append((current_date, current_lines))
    return days


def build_paper_section(
    paper_name: str,
    dateline: str,
    section_tag: str,
    h1: str,
    deck: str,
    byline: str,
    body_html: str,
) -> str:
    """Build the inner paper section HTML (masthead + section + body). Used for single pages and magazine."""
    return (
        PAPER_SECTION_TEMPLATE.replace("{{PAPER_NAME}}", paper_name)
        .replace("{{DATELINE}}", dateline)
        .replace("{{SECTION_TAG}}", section_tag)
        .replace("{{H1}}", h1)
        .replace("{{DECK}}", deck)
        .replace("{{BYLINE}}", byline)
        .replace("{{BODY_HTML}}", body_html)
    )


def build_paper_html(
    paper_name: str,
    dateline: str,
    section_tag: str,
    h1: str,
    deck: str,
    byline: str,
    body_html: str,
    title: Optional[str] = None,
) -> str:
    t = title or h1
    section_html = build_paper_section(
        paper_name=paper_name,
        dateline=dateline,
        section_tag=section_tag,
        h1=h1,
        deck=deck,
        byline=byline,
        body_html=body_html,
    )
    return PAPER_TEMPLATE.replace("{{SECTION_HTML}}", section_html).replace("{{TITLE}}", t)


def render_html_to_png(html_path: Path, png_path: Path) -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[WARNING] playwright not installed; skipping PNG. pip install playwright && playwright install chromium", file=sys.stderr)
        return False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1300, "height": 1400})
            file_url = "file:///" + str(html_path.resolve()).replace("\\", "/")
            page.goto(file_url, wait_until="networkidle")
            page.wait_for_timeout(500)
            paper = page.query_selector(".paper")
            if paper:
                paper.screenshot(path=str(png_path))
            else:
                page.screenshot(path=str(png_path))
            browser.close()
        return True
    except Exception as e:
        print(f"[WARNING] PNG render failed for {html_path}: {e}", file=sys.stderr)
        return False


def render_magazine_to_pdf(
    html_path: Path, pdf_path: Path, page_width: str, page_height: str
) -> bool:
    """Render magazine HTML to a multi-page PDF with the given page dimensions."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "[WARNING] playwright not installed; skipping PDF. pip install playwright && playwright install chromium",
            file=sys.stderr,
        )
        return False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            file_url = "file:///" + str(html_path.resolve()).replace("\\", "/")
            page.goto(file_url, wait_until="networkidle")
            page.wait_for_timeout(500)
            page.pdf(
                path=str(pdf_path),
                width=page_width,
                height=page_height,
                margin={"top": "0.5in", "bottom": "0.5in", "left": "0.5in", "right": "0.5in"},
            )
            browser.close()
        return True
    except Exception as e:
        print(f"[WARNING] Magazine PDF render failed for {html_path}: {e}", file=sys.stderr)
        return False


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Fetch StatsPlus league news and generate paper-style HTML + PNG.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--league", "-l", type=str, help="League tag (e.g. uba, woba)")
    p.add_argument("--league-id", type=int, required=True, help="League ID (e.g. 106)")
    p.add_argument("--days", type=int, default=7, help="Days of one-liners for daily summary (default: 7)")
    p.add_argument("--base-url", type=str, help="Override base URL (overrides config lookup)")
    p.add_argument(
        "--league-url-config",
        type=Path,
        default=DEFAULT_LEAGUE_URL_CONFIG,
        help="JSON file with league->api_url mappings (default: config/league_url.json)",
    )
    p.add_argument("--timeout", type=int, default=30, help="Request timeout (seconds)")
    p.add_argument("--output", "-o", type=Path, help="Parent directory for output (default: cwd)")
    p.add_argument("--magazine", action="store_true", help="Build a combined multi-page magazine PDF")
    p.add_argument(
        "--page-size",
        type=str,
        default="a4",
        help="Page size for combined magazine: 'a4', 'letter', or WxH e.g. 210x297mm (default: a4)",
    )
    p.add_argument(
        "--png",
        action="store_true",
        dest="render_png",
        help="Render each HTML file to PNG (default: off; requires playwright)",
    )
    p.add_argument(
        "--full-standings",
        action="store_true",
        help="Include all standings columns (default: only Team, W, L, PCT, GB)",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if not args.league and not args.base_url:
        print("[ERROR] Provide --league or --base-url", file=sys.stderr)
        sys.exit(1)
    base_url = args.base_url
    if not base_url:
        league_web_urls = load_league_web_urls(args.league_url_config)
        if args.league not in league_web_urls:
            print(
                f"[ERROR] League '{args.league}' not found in {args.league_url_config}. "
                "Add it to config/league_url.json or use --base-url.",
                file=sys.stderr,
            )
            sys.exit(1)
        base_url = league_web_urls[args.league]
    league_tag = args.league or "league"

    home_url = construct_content_url(base_url, f"league_{args.league_id}_home.html")
    print(f"[INFO] Fetching home: {home_url}")
    home_html = fetch_html(home_url, args.timeout)
    home_data = parse_home_page(home_html, args.league_id)
    out_dir_name = f"{league_tag}_news_{home_data.date_yyyymmdd}"
    _script_dir = Path(__file__).resolve().parent.parent
    parent = args.output or (_script_dir / league_tag / "news")
    out_dir = parent / out_dir_name
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[INFO] Output directory: {out_dir.resolve()}")

    file_prefix = f"league_{args.league_id}_"

    paper_name = f"The {league_tag.upper()} Chronicle" if league_tag != "league" else "League Chronicle"
    dateline_base = f"{home_data.date_display}  ·  League News  ·  Baseball"

    magazine_sections: List[Tuple[str, str]] = []  # (section_type, html)
    magazine_daily_summary: Optional[str] = None

    home_path = out_dir / f"{file_prefix}home.html"
    body_parts = []
    if home_data.featured:
        for fb in home_data.featured:
            body_parts.append(f'<div class="day-head">{fb.headline}</div>')
            body_parts.append(f'<p style="font-size:11px; color:#5a4a2a;">{fb.date_str}</p>')
            body_parts.append(f"<p>{fb.body_html}</p>")
    if home_data.news_links:
        body_parts.append('<div class="day-head">Today\'s Headlines</div>')
        for link in home_data.news_links:
            body_parts.append(f"<p>· {link.title}</p>")
    home_body = "\n    ".join(body_parts) if body_parts else "<p>No content.</p>"
    if args.magazine:
        magazine_sections.append(
            (
                "home",
                build_paper_section(
                    paper_name=paper_name,
                    dateline=dateline_base,
                    section_tag="★ League News ★",
                    h1="Today's Headlines",
                    deck="Latest from the league",
                    byline="StatsPlus League News",
                    body_html=home_body,
                ),
            )
        )
    if not home_path.exists():
        home_html_out = build_paper_html(
            paper_name=paper_name,
            dateline=dateline_base,
            section_tag="★ League News ★",
            h1="Today's Headlines",
            deck="Latest from the league",
            byline="StatsPlus League News",
            body_html=home_body,
            title=f"League News {home_data.date_yyyymmdd}",
        )
        home_path.write_text(home_html_out, encoding="utf-8")
        print(f"[INFO] Wrote {home_path}")
    else:
        print(f"[INFO] {home_path.name} exists, skipping")

    # League standings article: fetch league_###_standings.html, parse NL/AL, NL in first column, AL in second
    standings_path = out_dir / f"{file_prefix}standings.html"
    nl_standings: Optional[str] = None
    al_standings: Optional[str] = None
    need_standings_data = not standings_path.exists() or args.magazine
    if need_standings_data:
        standings_url = construct_content_url(base_url, f"league_{args.league_id}_standings.html")
        try:
            print(f"[INFO] Fetching standings: {standings_url}")
            standings_html = fetch_html(standings_url, args.timeout)
            nl_standings, al_standings = parse_standings_page(
                standings_html, full_standings=getattr(args, "full_standings", False)
            )
        except requests.RequestException as e:
            print(f"[WARNING] Could not fetch standings page: {e}", file=sys.stderr)
    if nl_standings or al_standings:
        parts = []
        if nl_standings:
            parts.append(f'<div class="standings-block stats-box">{nl_standings}</div>')
        if al_standings:
            parts.append(f'<div class="standings-block stats-box">{al_standings}</div>')
        standings_body = "\n    ".join(parts)
        if not standings_path.exists():
            standings_html_out = build_paper_html(
                paper_name=paper_name,
                dateline=dateline_base,
                section_tag="League Standings",
                h1="League Standings",
                deck="National League and American League",
                byline="StatsPlus",
                body_html=standings_body,
                title=f"League Standings {home_data.date_yyyymmdd}",
            )
            standings_path.write_text(standings_html_out, encoding="utf-8")
            print(f"[INFO] Wrote {standings_path}")
        if args.magazine:
            magazine_sections.append(
                (
                    "standings",
                    build_paper_section(
                        paper_name=paper_name,
                        dateline=dateline_base,
                        section_tag="League Standings",
                        h1="League Standings",
                        deck="National League and American League",
                        byline="StatsPlus",
                        body_html=standings_body,
                    ),
                )
            )
    elif standings_path.exists():
        if args.magazine:
            try:
                section_html = extract_paper_section_html(standings_path.read_text(encoding="utf-8"))
                if section_html:
                    magazine_sections.append(("standings", section_html))
            except Exception as e:
                print(f"[WARNING] Could not read standings for magazine: {e}", file=sys.stderr)
        print(f"[INFO] {standings_path.name} exists, skipping")

    for link in home_data.news_links:
        art_path = out_dir / f"{file_prefix}article_{link.article_id}.html"
        if art_path.exists():
            if args.magazine:
                try:
                    section_html = extract_paper_section_html(art_path.read_text(encoding="utf-8"))
                    if section_html:
                        magazine_sections.append(("article", section_html))
                except Exception as e:
                    print(f"[WARNING] Could not read {art_path.name} for magazine: {e}", file=sys.stderr)
            print(f"[INFO] {art_path.name} exists, skipping")
            continue
        try:
            url = construct_content_url(base_url, link.filename)
            print(f"[INFO] Fetching article {link.article_id}: {link.title[:50]}...")
            article_html = fetch_html(url, args.timeout)
            body_text = extract_message_body(article_html)
            body_html = markdown_like_to_html(body_text)
            art_html_out = build_paper_html(
                paper_name=paper_name,
                dateline=dateline_base,
                section_tag="League News",
                h1=link.title,
                deck="",
                byline="StatsPlus",
                body_html=body_html or "<p>No content.</p>",
                title=link.title,
            )
            art_path.write_text(art_html_out, encoding="utf-8")
            if args.magazine:
                magazine_sections.append(
                    (
                        "article",
                        build_paper_section(
                            paper_name=paper_name,
                            dateline=dateline_base,
                            section_tag="League News",
                            h1=link.title,
                            deck="",
                            byline="StatsPlus",
                            body_html=body_html or "<p>No content.</p>",
                        ),
                    )
                )
            print(f"[INFO] Wrote {art_path}")
        except Exception as e:
            print(f"[WARNING] Failed article {link.article_id}: {e}", file=sys.stderr)

    summary_path = out_dir / f"{file_prefix}daily_summary.html"
    if not summary_path.exists():
        news_url = construct_content_url(base_url, f"league_{args.league_id}_news.html")
        print(f"[INFO] Fetching one-liners: {news_url}")
        news_html = fetch_html(news_url, args.timeout)
        day_groups = parse_news_oneliners(news_html, args.days)
        summary_parts = []
        for date_str, lines in day_groups:
            summary_parts.append(f'<div class="day-head">{date_str}</div>')
            for line in lines:
                summary_parts.append(f"<p>{line}</p>")
        summary_body = "\n    ".join(summary_parts) if summary_parts else "<p>No one-liners.</p>"
        summary_html_out = build_paper_html(
            paper_name=paper_name,
            dateline=dateline_base,
            section_tag="Daily Summary",
            h1=f"Past {args.days} Days",
            deck=f"One-liners from the last {len(day_groups)} day(s)",
            byline="StatsPlus",
            body_html=summary_body,
            title=f"Daily Summary {home_data.date_yyyymmdd}",
        )
        summary_path.write_text(summary_html_out, encoding="utf-8")
        if args.magazine:
            magazine_daily_summary = build_paper_section(
                paper_name=paper_name,
                dateline=dateline_base,
                section_tag="Daily Summary",
                h1=f"Past {args.days} Days",
                deck=f"One-liners from the last {len(day_groups)} day(s)",
                byline="StatsPlus",
                body_html=summary_body,
            )
        print(f"[INFO] Wrote {summary_path}")
    else:
        if args.magazine:
            try:
                section_html = extract_paper_section_html(summary_path.read_text(encoding="utf-8"))
                if section_html:
                    magazine_daily_summary = section_html
            except Exception as e:
                print(f"[WARNING] Could not read daily summary for magazine: {e}", file=sys.stderr)
        print(f"[INFO] {summary_path.name} exists, skipping")

    # Scores & box scores: one roundup file per day (skip days with no games)
    print(f"[INFO] Fetching scoreboards and box scores for past {args.days} days...")
    score_dates = dates_for_scores(home_data.date_yyyymmdd, args.days)
    for d in score_dates:
        date_yyyymmdd = d.strftime("%Y%m%d")
        roundup_path = out_dir / f"{file_prefix}scores_roundup_{date_yyyymmdd}.html"
        if roundup_path.exists():
            if args.magazine:
                try:
                    section_html = extract_paper_section_html(roundup_path.read_text(encoding="utf-8"))
                    if section_html:
                        magazine_sections.append(("scores_roundup", section_html))
                except Exception as e:
                    print(f"[WARNING] Could not read {roundup_path.name} for magazine: {e}", file=sys.stderr)
            print(f"[INFO] {roundup_path.name} exists, skipping")
            continue
        scoreboard_filename = f"league_{args.league_id}_scores_{d.year}_{d.month:02d}_{d.day:02d}.html"
        scoreboard_url = construct_content_url(base_url, scoreboard_filename)
        try:
            scoreboard_html = fetch_html(scoreboard_url, args.timeout)
        except requests.RequestException:
            continue
        game_ids = parse_scoreboard_box_links(scoreboard_html)
        if not game_ids:
            continue
        date_display = d.strftime("%A, %B %d, %Y")
        games: List[BoxScoreData] = []
        for gid in game_ids:
            try:
                box_url = construct_box_score_url(base_url, gid)
                box_html = fetch_html(box_url, args.timeout)
            except requests.RequestException as e:
                print(f"[WARNING] Failed to fetch box score {gid}: {e}", file=sys.stderr)
                continue
            box = parse_box_score(box_html)
            if box:
                games.append(box)
        if not games:
            continue
        roundup_body = build_scores_roundup_body([(date_display, games)])
        roundup_html_out = build_paper_html(
            paper_name=paper_name,
            dateline=dateline_base,
            section_tag="★ Box Scores ★",
            h1=f"Scores — {date_display}",
            deck="Game summaries and linescores.",
            byline="StatsPlus",
            body_html=roundup_body,
            title=f"Scores & Box Scores {date_yyyymmdd}",
        )
        roundup_path.write_text(roundup_html_out, encoding="utf-8")
        if args.magazine:
            magazine_sections.append(
                (
                    "scores_roundup",
                    build_paper_section(
                        paper_name=paper_name,
                        dateline=dateline_base,
                        section_tag="★ Box Scores ★",
                        h1=f"Scores — {date_display}",
                        deck="Game summaries and linescores.",
                        byline="StatsPlus",
                        body_html=roundup_body,
                    ),
                )
            )
        print(f"[INFO] Wrote {roundup_path}")

    if args.magazine and magazine_daily_summary is not None:
        magazine_sections.append(("daily_summary", magazine_daily_summary))

    # PNG conversion: skipped by default; use --png to enable
    if getattr(args, "render_png", False):
        html_files = list(out_dir.glob("*.html"))
        for hp in html_files:
            png_path = hp.with_suffix(".png")
            if png_path.exists():
                print(f"[INFO] {png_path.name} exists, skipping")
                continue
            if render_html_to_png(hp, png_path):
                print(f"[INFO] Rendered {png_path}")

    if args.magazine and magazine_sections:
        try:
            page_width, page_height = parse_page_size(args.page_size)
        except ValueError as e:
            print(f"[ERROR] {e}", file=sys.stderr)
            sys.exit(1)
        magazine_html = build_magazine_html(magazine_sections, page_width, page_height)
        content_base = content_base_url(base_url)
        magazine_html = rewrite_relative_hrefs(magazine_html, content_base)
        magazine_path = out_dir / f"{file_prefix}magazine.html"
        magazine_path.write_text(magazine_html, encoding="utf-8")
        print(f"[INFO] Wrote {magazine_path}")

    print(f"[SUCCESS] Done. Output in {out_dir.resolve()}")


if __name__ == "__main__":
    main()

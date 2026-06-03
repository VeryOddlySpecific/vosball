# StatsPlus Paper News — Usage Guide

> Part of [VOSBall](../README.md). Most VOSBall work happens in the Streamlit web
> app (`py -m streamlit run webapp/app.py`), but this is a **standalone CLI
> utility** — it has no app page. It scrapes StatsPlus league pages and turns
> them into newspaper-style HTML (and optionally PNG/PDF).

Code: [`../tools/statsplus_paper_news.py`](../tools/statsplus_paper_news.py)
Extra deps: [`../requirements_statsplus_paper.txt`](../requirements_statsplus_paper.txt)

## Overview

`tools/statsplus_paper_news.py` fetches content from StatsPlus league news pages
and assembles it into **newspaper-style** HTML files. All output for a run is
written into a single dated directory.

**What it produces:**

- **Front page** — League home page as a paper (featured stories + "Today's Headlines").
- **Standings** — National League / American League standings (trimmed to Team, W, L, PCT, GB by default).
- **Top 5 articles** — Each of the five main news articles from the home page as its own paper.
- **Daily summary** — One paper compiling the last N days of one-liner news into a single "Past N Days" article.
- **Box scores** — One roundup paper per day (for the last N days) with game recaps, linescores, and batting/pitching tables. Days with no games are skipped.
- **Magazine** (optional, `--magazine`) — One combined multi-page document (`league_{id}_magazine.html`) laying all sections out on page-sized sheets.

**Skip-if-exists:** If an output HTML file already exists in the run directory,
the script skips fetching and writing it. With `--png`, an existing PNG is also
skipped. So you can safely re-run to fill in only missing files.

---

## Dependencies

| Package          | Purpose                                  |
|------------------|------------------------------------------|
| `requests`       | Fetching HTML from StatsPlus.            |
| `beautifulsoup4` | Parsing HTML.                            |
| `playwright`     | Rendering HTML→PNG and magazine→PDF (optional). |

**Setup:**

```powershell
py -m pip install -r requirements_statsplus_paper.txt
py -m playwright install chromium
```

Without Playwright, the script still generates all HTML; it only skips PNG/PDF
rendering and prints a warning.

---

## Arguments

| Argument              | Short | Required | Default | Description |
|-----------------------|-------|----------|---------|-------------|
| `--league`            | `-l`  | No*      | —       | League tag (e.g. `uba`, `sky`). Must exist in the league-URL config unless `--base-url` is set. |
| `--league-id`         | —     | **Yes**  | —       | StatsPlus league ID (e.g. `106`). Find it in the league's StatsPlus URLs. |
| `--days`              | —     | No       | `7`     | Number of days for the daily one-liner summary and for box scores. |
| `--base-url`          | —     | No*      | —       | Override base URL (e.g. `https://statsplus.net/myleague`). Bypasses the config lookup. |
| `--league-url-config` | —     | No       | `config/league_url.json` | JSON file mapping league tag → API URL. |
| `--timeout`           | —     | No       | `30`    | Request timeout in seconds. |
| `--output`            | `-o`  | No       | `<repo>/{league}/news/` | Parent directory for the run. |
| `--magazine`          | —     | No       | off     | Also build a combined multi-page magazine (HTML; PDF if Playwright is present). |
| `--page-size`         | —     | No       | `a4`    | Magazine page size: `a4`, `letter`, or `WxH` (e.g. `210x297mm`, `8.5x11in`). |
| `--png`               | —     | No       | off     | Render each HTML file to PNG (requires Playwright). |
| `--full-standings`    | —     | No       | off     | Include all standings columns instead of just Team/W/L/PCT/GB. |

\* You must provide either `--league` or `--base-url`.

---

## League URLs

Leagues are resolved from [`config/league_url.json`](../config/league_url.json),
which maps a league tag to its StatsPlus **API** URL (ending in `/api`). The
script strips the `/api` suffix to get the web base URL it scrapes.

To add a league, add an entry to `config/league_url.json`, or use `--base-url`
for a one-off run. Use a different mapping file with `--league-url-config`.

---

## Output Structure

**Parent directory:** defaults to `<repo>/{league}/news/`; override with `--output`.
**Run directory name:** `{league}_news_{YYYYMMDD}` — the date comes from the
league home page (e.g. `uba_news_20410125`).

**Files written (when they don't already exist):** all are prefixed
`league_{id}_`.

| File                                     | Description |
|------------------------------------------|-------------|
| `league_{id}_home.html`                  | Front page (featured stories + headlines). |
| `league_{id}_standings.html`             | NL / AL standings. |
| `league_{id}_article_{id}.html`          | One per top-5 article (e.g. `league_106_article_672.html`). |
| `league_{id}_daily_summary.html`         | One-liners for the last N days. |
| `league_{id}_scores_roundup_{YYYYMMDD}.html` | Box scores for one day (one file per day that had games). |
| `league_{id}_magazine.html`              | Combined multi-page magazine (only with `--magazine`). |

With `--png`, a matching `.png` is rendered next to each HTML file. With
`--magazine` and Playwright present, a magazine PDF is also produced.

Example for a 2-day run with games on both days:

```
uba/news/uba_news_20410125/
  league_106_home.html
  league_106_standings.html
  league_106_article_669.html
  ...
  league_106_daily_summary.html
  league_106_scores_roundup_20410124.html
  league_106_scores_roundup_20410125.html
```

---

## Examples

**Basic run (default 7 days, output under `<repo>/uba/news/`):**

```powershell
py tools\statsplus_paper_news.py --league uba --league-id 106
```

**14 days of one-liners and box scores, custom output dir:**

```powershell
py tools\statsplus_paper_news.py --league uba --league-id 106 --days 14 --output .\out
```

**League not in the config (custom base URL):**

```powershell
py tools\statsplus_paper_news.py --base-url https://statsplus.net/myleague --league myleague --league-id 42
```

Pass `--league myleague` alongside `--base-url` so the output folder is named
`myleague_news_YYYYMMDD`; otherwise it falls back to `league_news_YYYYMMDD`.

**Combined magazine on US Letter pages:**

```powershell
py tools\statsplus_paper_news.py --league uba --league-id 106 --magazine --page-size letter
```

**Render PNGs too, shorter timeout:**

```powershell
py tools\statsplus_paper_news.py --league sky --league-id 100 --png --timeout 15
```

---

## Re-runs and Skipping

- The script **always** fetches the league home page (needed for the date and article list).
- For every section (home, standings, each article, daily summary, each day's scores roundup), if the corresponding **HTML file** already exists, the script skips fetching and writing it.
- With `--png`, an existing **PNG** is skipped.

Re-run the same command to fill in only missing files (e.g. after a run that
failed partway through).

---

## Finding Your League ID

The league ID is the number in StatsPlus URLs, e.g.:

- `league_106_home.html` → league ID is **106**
- `league_100_scores_2071_07_25.html` → league ID is **100**

Open your league's news or scoreboard page and check the URL for the
`league_XX_` pattern.

---

## Troubleshooting

- **"League 'xyz' not found in config/league_url.json"** — Add it to `config/league_url.json` or use `--base-url https://...`.
- **PNG/PDF warnings or none produced** — Install Playwright (`py -m playwright install chromium`). PNG/PDF are optional; HTML is always generated.
- **404 or empty scoreboard** — The script skips that day. Normal for off-days.
- **Missing or partial output** — Re-run the same command; existing files are skipped and only missing ones are created.

---

> The current VOS engine is **VOS v10** in the `vosball/` package, driven by
> [`../run_vos.py`](../run_vos.py). The retired v2 engine lives at
> [`../tools/archive/vos_v2.py`](../tools/archive/vos_v2.py).

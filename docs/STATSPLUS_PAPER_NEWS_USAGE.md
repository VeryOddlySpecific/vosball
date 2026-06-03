# StatsPlus Paper News — Usage Guide

## Overview

`statsplus_paper_news.py` fetches content from StatsPlus league news pages and turns it into **newspaper-style** HTML and PNG files. All output is written into a single dated directory per run.

**What it produces:**

- **Front page** — League home page as a paper (featured stories + “Today’s Headlines”).
- **Top 5 articles** — Each of the five main news articles from the home page as its own paper (headline, body, paper layout).
- **Daily summary** — One paper that compiles the last N days of one-liner news into a single “Past N Days” article.
- **Box scores** — One paper per day (for the last N days) with game recaps, linescores, and batting/pitching details. Days with no games are skipped.

**Skip-if-exists:** If an output file (HTML or PNG) already exists in the run directory, the script skips fetching and writing that file (and skips PNG rendering when the PNG already exists).

---

## Dependencies

| Package        | Purpose                    |
|----------------|----------------------------|
| `requests`     | Fetching HTML from StatsPlus. |
| `beautifulsoup4` | Parsing HTML.            |
| `playwright`   | Rendering HTML to PNG (optional). |

**Setup:**

```bash
pip install requests beautifulsoup4 playwright
playwright install chromium
```

Without Playwright, the script still generates all HTML files; it will only skip creating the PNGs and print a warning.

---

## Arguments

| Argument        | Short | Required | Default | Description |
|-----------------|-------|----------|---------|-------------|
| `--league`     | `-l`  | No*      | —       | League tag (e.g. `uba`, `sky`). Must exist in the script’s league map unless `--base-url` is set. |
| `--league-id`  | —     | **Yes**  | —       | StatsPlus league ID (e.g. `106`, `100`). Find it in the league’s StatsPlus URLs. |
| `--days`       | —     | No       | `7`     | Number of days for the daily one-liner summary and for box scores (e.g. `7` = last 7 days). |
| `--base-url`   | —     | No*      | —       | Override base URL (e.g. `https://statsplus.net/myleague`). Use for leagues not in the built-in list. |
| `--timeout`    | —     | No       | `30`    | Request timeout in seconds. |
| `--output`     | `-o`  | No       | current directory | Parent directory for the run. The script creates `{league}_news_{YYYYMMDD}` inside it. |

\* You must provide either `--league` or `--base-url`.

---

## Output Structure

**Directory name:** `{league}_news_{YYYYMMDD}`  
The date (`YYYYMMDD`) comes from the league home page (e.g. `uba_news_20410125`).

**Files written (when they don’t already exist):**

| File                     | Description |
|--------------------------|-------------|
| `home.html` / `home.png` | Front page (featured stories + headlines). |
| `article_{id}.html` / `.png` | One pair per top-5 article (e.g. `article_672.html`). |
| `daily_summary.html` / `.png` | One-liners for the last N days. |
| `scores_roundup_YYYYMMDD.html` / `.png` | Box scores for one day (one file per day that had games). |

Example for a 2-day run with games on both days:

```
uba_news_20410125/
  home.html
  home.png
  article_669.html
  article_669.png
  ...
  daily_summary.html
  daily_summary.png
  scores_roundup_20410124.html
  scores_roundup_20410124.png
  scores_roundup_20410125.html
  scores_roundup_20410125.png
```

---

## Built-in Leagues

The script knows these league tags and their base URLs:

| Tag   | Base URL |
|-------|----------|
| `uba` | https://statsplus.net/uba |
| `woba` | https://atl-01.statsplus.net/woba |
| `wwoba` | https://atl-01.statsplus.net/wwoba |
| `sahl` | https://statsplus.net/sahl |
| `sky` | https://atl-01.statsplus.net/skylinebaseball |
| `smdb` | https://statsplus.net/sdmbootp |
| `tlg` | https://atl-02.statsplus.net/tlg |
| `sol` | https://atl-02.statsplus.net/sol |

To add more leagues, edit `LEAGUE_BASE_URLS` in `statsplus_paper_news.py`, or use `--base-url` for a one-off run.

---

## Examples

**Basic run (default 7 days, output in current directory):**

```bash
python statsplus_paper_news.py --league uba --league-id 106
```

**14 days of one-liners and box scores, output under `./out`:**

```bash
python statsplus_paper_news.py --league uba --league-id 106 --days 14 --output ./out
```

**League not in the list (custom base URL):**

```bash
python statsplus_paper_news.py --base-url https://statsplus.net/myleague --league myleague --league-id 42
```

Use `--league myleague` (or any tag) with `--base-url` if you want the output folder to be named `myleague_news_YYYYMMDD`; otherwise the folder name will be `league_news_YYYYMMDD`.

**Shorter timeout:**

```bash
python statsplus_paper_news.py --league sky --league-id 100 --timeout 15
```

---

## Re-runs and Skipping

- The script **always** fetches the league home page (needed to get the date and article list).
- For **home**, **each article**, **daily summary**, and **each day’s scores roundup**, if the corresponding **HTML file** already exists in the output directory, the script skips fetching and writing that file.
- For **PNG** generation, if the **PNG file** already exists, the script skips rendering that file.

So you can re-run the same command to fill in only missing HTML/PNG files (e.g. after adding a new league to the script or after a previous run that failed partway through).

---

## Finding Your League ID

The league ID is the number used in StatsPlus URLs, e.g. in:

- `league_106_home.html` → league ID is **106**
- `league_100_scores_2071_07_25.html` → league ID is **100**

Open your league’s news or scoreboard page in a browser and check the URL for the `league_XX_` pattern.

---

## Troubleshooting

- **"League 'xyz' not in LEAGUE_BASE_URLS"** — Use `--base-url https://...` for that league, or add the league to `LEAGUE_BASE_URLS` in the script.
- **PNG warnings / no PNGs** — Install Playwright and run `playwright install chromium`. PNGs are optional; HTML is still generated.
- **404 or empty scoreboard** — The script skips that day (no scores file for that date). Normal for off-days or invalid dates.
- **Missing or partial output** — Re-run the same command; existing HTML/PNG files will be skipped and only missing ones will be created.

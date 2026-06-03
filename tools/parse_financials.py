"""Parse an OOTP league financial report HTML into a per-team CSV.

The OOTP "League Financial Report" HTML (exported from the in-game BNN view)
contains a section per team with three sub-tables: GENERAL INFORMATION,
CURRENT FINANCIAL OVERVIEW, and LAST SEASON OVERVIEW. This script flattens
all 16 team sections (or however many your league has) into one CSV row each.

Team names are joined to canonical form via `config/teams-<league>.json` (same
file used by vos_v2.py, depth_chart.py, etc.) — keyed on the numeric team_id
embedded in the HTML (`team_6.html`). If a team_id isn't present in the
config, we fall back to the raw HTML text title-cased.

Usage:
    python parse_financials.py --league sdmb \
        --input  sdmb/contract_audit/sdmb_league_financials.html \
        --output sdmb/contract_audit/sdmb_team_financials.csv

If --league is omitted, the title-case fallback is used for every row.
"""
from __future__ import annotations
# --- repo-root + core/ path bootstrap ---
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _p in (_ROOT, _os.path.join(_ROOT, "core")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
# --- end bootstrap ---

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple


# ---------------------------------------------------------------------------
# Teams config loader (mirrors vos_v2.load_teams)
# ---------------------------------------------------------------------------

def load_teams(config_dir: Path, league: str) -> Dict[int, str]:
    """Build {team_id: 'Name Nickname'} from config/teams-<league>.json."""
    path = config_dir / f"teams-{league}.json"
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return {}
    result: Dict[int, str] = {}
    for tid_str, info in raw.items():
        if tid_str.startswith("_") or not isinstance(info, dict):
            continue
        try:
            tid = int(tid_str)
        except (TypeError, ValueError):
            continue
        name = info.get("Name") or ""
        nick = info.get("Nickname") or ""
        result[tid] = f"{name} {nick}".strip() or f"Team {tid}"
    return result


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------

# Each team section starts at:  <a href="../teams/team_NN.html" class="boxlink">TEAM NAME</a>
TEAM_HEADER_RE = re.compile(
    r'<a[^>]+team_(\d+)\.html"[^>]*class="boxlink"[^>]*>\s*([^<]+?)\s*</a>',
    re.IGNORECASE,
)

# Within a team block, each metric row looks like:
#   <td class="dl">LABEL</td><td class="dr">VALUE</td>
ROW_RE = re.compile(
    r'<td[^>]*class="dl"[^>]*>([^<]+)</td>\s*<td[^>]*class="dr"[^>]*>([^<]*)</td>'
)


METRIC_FIELDS = {
    # general info
    "staff payroll": "staff_payroll",
    "player payroll": "player_payroll",
    "current budget": "current_budget",
    "projected balance": "projected_balance",
    "average player salary": "avg_player_salary",
    "league average salary": "league_avg_salary",
    # current season
    "starting balance": "starting_balance",
    "gate revenue": "gate_revenue",
    "season ticket revenue": "season_ticket_revenue",
    "playoff revenue": "playoff_revenue",
    "media revenue": "media_revenue",
    "merchandising revenue": "merch_revenue",
    "other revenue": "other_revenue",
    "player expenses": "player_expenses",
    "staff expenses": "staff_expenses",
    "other expenses": "other_expenses",
    "misc expenses": "misc_expenses",
    "total revenue": "total_revenue",
    "total expenses": "total_expenses",
    "balance": "current_balance",
    "attendance": "attendance",
    "attendance per game": "attendance_per_game",
}


def parse_money(s: str) -> int:
    """`$12,345,678` or `-$1,200,000` → int. Empty / non-money → 0."""
    if not s:
        return 0
    s = s.strip()
    neg = s.startswith("-")
    digits = re.sub(r"[^\d]", "", s)
    if not digits:
        return 0
    v = int(digits)
    return -v if neg else v


def strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).replace("&#160;", " ").strip()


def parse_team_sections(html: str) -> Iterable[Tuple[int, str, str]]:
    """Yield (team_id, raw_html_name, block_html) for each team section."""
    matches = list(TEAM_HEADER_RE.finditer(html))
    for i, m in enumerate(matches):
        team_id = int(m.group(1))
        raw_name = m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(html)
        block = html[start:end]
        yield team_id, raw_name, block


def parse_block(block: str) -> dict:
    """Extract metrics from a team block, prefixed by sub-section."""
    out: dict = {}

    def section(name: str) -> str:
        m = re.search(rf"{re.escape(name)}\s*</th>", block)
        if not m:
            return ""
        start = m.end()
        end_m = re.search(r"</table>", block[start:])
        end = start + (end_m.end() if end_m else len(block) - start)
        return block[start:end]

    general = section("GENERAL INFORMATION")
    current = section("CURRENT FINANCIAL OVERVIEW")
    last = section("LAST SEASON OVERVIEW")

    def extract(sub: str, prefix: str = ""):
        for label_html, value_html in ROW_RE.findall(sub):
            label = strip_html(label_html).lower().rstrip(":").strip()
            value_raw = strip_html(value_html)
            key = METRIC_FIELDS.get(label)
            if not key:
                continue
            if label in ("attendance", "attendance per game"):
                num = re.sub(r"[^\d]", "", value_raw)
                out[prefix + key] = int(num) if num else 0
            else:
                out[prefix + key] = parse_money(value_raw)

    extract(general, "")
    extract(current, "cur_")
    extract(last, "last_")
    return out


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

BASE_COLS = [
    "team", "team_id",
    "current_budget", "player_payroll", "staff_payroll", "projected_balance",
    "avg_player_salary", "league_avg_salary",
    "cur_total_revenue", "cur_media_revenue", "cur_gate_revenue", "cur_season_ticket_revenue",
    "cur_merch_revenue", "cur_other_revenue", "cur_playoff_revenue",
    "cur_total_expenses", "cur_player_expenses", "cur_staff_expenses", "cur_other_expenses",
    "cur_starting_balance", "cur_current_balance",
    "cur_attendance", "cur_attendance_per_game",
    "last_total_revenue", "last_media_revenue", "last_gate_revenue",
    "last_total_expenses", "last_player_expenses",
    "last_current_balance", "last_attendance",
]


def write_csv(rows: list, out_path: Path) -> None:
    extras = sorted({k for r in rows for k in r} - set(BASE_COLS) - {"team_raw"})
    cols = BASE_COLS + extras
    with out_path.open("w", encoding="utf-8", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(cols)
        for r in sorted(rows, key=lambda r: -r.get("current_budget", 0)):
            wr.writerow([r.get(c, "") for c in cols])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Parse OOTP league financial HTML into per-team CSV."
    )
    ap.add_argument("--input", "-i", required=True, type=Path,
                    help="Path to <league>_league_financials.html (BNN financial report)")
    ap.add_argument("--output", "-o", required=True, type=Path,
                    help="Path to output CSV (one row per team)")
    ap.add_argument("--league", default=None,
                    help="League slug (e.g. sdmb, sahl, tlg). If provided, team names are "
                         "canonicalized via config/teams-<league>.json (preferred). "
                         "If omitted, the script title-cases the raw HTML team names.")
    ap.add_argument("--config-dir", type=Path, default=Path(__file__).parent / "config",
                    help="Directory containing teams-<league>.json (default: ./config)")
    args = ap.parse_args()

    if not args.input.exists():
        sys.exit(f"Input HTML not found: {args.input}")

    teams_map: Dict[int, str] = {}
    if args.league:
        teams_map = load_teams(args.config_dir, args.league)
        if not teams_map:
            print(f"WARN: teams-{args.league}.json not found or empty in {args.config_dir}. "
                  f"Falling back to title-case from HTML.", file=sys.stderr)

    html = args.input.read_text(encoding="utf-8", errors="replace")

    rows = []
    for team_id, raw_name, block in parse_team_sections(html):
        data = parse_block(block)
        data["team_id"] = team_id
        # Prefer canonical name from teams-<league>.json; fall back to title-case
        canonical = teams_map.get(team_id) if teams_map else None
        data["team"] = canonical if canonical else raw_name.title()
        data["team_raw"] = raw_name
        rows.append(data)

    if not rows:
        sys.exit("ERROR: no team sections matched in the HTML. "
                 "Confirm this is an OOTP League Financial Report.")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_csv(rows, args.output)
    print(f"Wrote {args.output}  ({len(rows)} teams)")

    if args.league and teams_map:
        missing_ids = [r["team_id"] for r in rows if r["team_id"] not in teams_map]
        if missing_ids:
            print(f"WARN: team_ids not in teams-{args.league}.json: {missing_ids}", file=sys.stderr)


if __name__ == "__main__":
    main()

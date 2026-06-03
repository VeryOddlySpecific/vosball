#!/usr/bin/env python3
"""
spring_training_invites.py — Build a non-roster spring training invite list
for one organization based on the latest evaluation summary.

Pool
----
All players in the target org at minor-league levels (AAA, AA, A+, A, A-, R,
R-ACL, R-DSL, INT). Major-league (ML) players are excluded since this list is
meant for non-roster invites. Players with no League_Level set are skipped.

Scoring
-------
Composite-heavy (current value) with a small upside bump:

    composite = 0.80 * VOS_Score + 0.20 * VOS_Potential

Slot allocation
---------------
Total cap from --max N. Slots are split:
- Hitters : Pitchers = roughly 50/50 (hitter_slots = max // 2)
- Pitchers: 2:1 RP-to-SP
- Hitters: distributed evenly across the eight fielding positions
  (C, 1B, 2B, 3B, SS, LF, CF, RF). DH-primary players are not given a
  dedicated bucket; they can still appear via flex slots unless
  --include-dh is passed.

Unfilled position quotas roll into a same-side flex pool which picks the best
remaining candidates by composite score.

Output
------
- {league}/spring_training/{org_slug}_{ts}.md  (position-bucket and per-level views)
- {league}/spring_training/{org_slug}_{ts}.csv

Usage
-----
    python spring_training_invites.py --league bwb  --org-code chh --max 30
    python spring_training_invites.py --league sahl --org-code hou --max 30

The full org name is resolved from config/{league}-park-factors.json by matching
team_info.team_code (case-insensitive).
"""

from __future__ import annotations
# --- tools/ -> repo-root bootstrap (added during tools/ move) ---
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
# --- end bootstrap ---


import argparse
import csv
import json
import logging
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)
SCRIPT_DIR = Path(__file__).resolve().parent.parent

MINOR_LEVELS = {"AAA", "AA", "A+", "A", "A-", "R", "R-ACL", "R-DSL", "INT"}
LEVEL_DISPLAY_ORDER = ["AAA", "AA", "A+", "A", "A-", "R", "R-ACL", "R-DSL", "INT"]
HITTER_BUCKETS = ["C", "1B", "2B", "3B", "SS", "LF", "CF", "RF"]
PITCHER_BUCKETS = ["SP", "RP"]

W_ABILITY = 0.80
W_POTENTIAL = 0.20


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Build a non-roster spring training invite list for one org."
    )
    p.add_argument("--league", required=True, help="League slug (e.g. sahl, bwb).")
    p.add_argument("--org-code", type=str, required=True,
                   help="Team code (e.g. 'chh'). Used to (1) locate the per-org eval under "
                        "{league}/eval/{org-code}/ and (2) resolve the org name via "
                        "config/{league}-park-factors.json team_info.team_code.")
    p.add_argument("--max", dest="max_total", type=int, required=True,
                   help="Maximum total number of invites. Split ~50/50 between hitters and pitchers.")
    p.add_argument("--input", type=Path, default=None,
                   help="Override path to evaluation_summary CSV. Default: latest in "
                        "{league}/eval/{org-code}/ else {league}/eval/.")
    p.add_argument("--output-dir", type=Path, default=None,
                   help="Output directory. Default: {league}/spring_training/.")
    p.add_argument("--include-dh", action="store_true",
                   help="Add DH as a dedicated hitter bucket (default: DH-primary players "
                        "only enter via flex slots).")
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args()


def resolve_org_name(league: str, org_code: str) -> str:
    path = SCRIPT_DIR / "config" / f"{league}-park-factors.json"
    if not path.exists():
        raise FileNotFoundError(
            f"Cannot resolve org-code '{org_code}': missing {path}. "
            f"Add a park-factors JSON with teams[*].team_info.team_code entries."
        )
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    teams = data.get("teams", {})
    code_map: Dict[str, str] = {}
    for org_name, blk in teams.items():
        code = (blk.get("team_info", {}) or {}).get("team_code")
        if code:
            code_map[code.upper()] = org_name
    target = org_code.upper()
    if target not in code_map:
        available = ", ".join(f"{c}={n}" for c, n in sorted(code_map.items()))
        raise ValueError(f"org-code '{org_code}' not found. Available: {available}")
    return code_map[target]


def find_latest_eval(league: str, org_code: Optional[str] = None) -> Path:
    base = SCRIPT_DIR / league / "eval"
    if not base.exists():
        raise FileNotFoundError(f"No eval directory found at {base}")
    pattern = f"evaluation_summary_{league}_*.csv"
    if org_code:
        org_dir = base / org_code
        if org_dir.is_dir():
            cands = sorted(org_dir.glob(pattern))
            if cands:
                return cands[-1]
            logger.warning("No evals in %s; falling back to %s", org_dir, base)
        else:
            logger.warning("Org-code dir %s missing; falling back to %s", org_dir, base)
    cands = sorted(base.glob(pattern))
    if not cands:
        raise FileNotFoundError(f"No {pattern} in {base}")
    return cands[-1]


def load_eval(path: Path) -> List[Dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def to_float(v: str) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def build_candidates(rows: Iterable[Dict[str, str]], org: str) -> List[Dict[str, object]]:
    out: List[Dict[str, object]] = []
    for row in rows:
        if row.get("Org") != org:
            continue
        level = row.get("League_Level") or ""
        if level not in MINOR_LEVELS:
            continue
        vos = to_float(row.get("VOS_Score", ""))
        pot = to_float(row.get("VOS_Potential", ""))
        if vos is None and pot is None:
            continue
        ability = vos if vos is not None else pot
        potential = pot if pot is not None else vos
        composite = W_ABILITY * ability + W_POTENTIAL * potential
        out.append({
            "id": row.get("ID", ""),
            "name": row.get("Name", ""),
            "age": to_float(row.get("Age", "")),
            "team": row.get("Team", ""),
            "level": level,
            "current_pos": row.get("Current_Position", "") or row.get("Pos", ""),
            "projected_pos": row.get("Projected_Position", "") or row.get("Pos", ""),
            "vos_score": ability,
            "vos_potential": potential,
            "composite": composite,
        })
    out.sort(key=lambda r: r["composite"], reverse=True)
    return out


def bucket_for(player: Dict[str, object], hitter_buckets: List[str]) -> str:
    pos = str(player.get("projected_pos") or "").upper()
    if pos == "SP":
        return "SP"
    if pos in ("RP", "CL", "SU", "MR", "LR", "P"):
        return "RP"
    if pos in hitter_buckets:
        return pos
    return "FLEX"


def split_slots(max_total: int, hitter_buckets: List[str]) -> Tuple[Dict[str, int], Dict[str, int]]:
    hitter_total = max_total // 2
    pitcher_total = max_total - hitter_total
    n_buckets = len(hitter_buckets)
    base, extra = divmod(hitter_total, n_buckets)
    priority = ["SS", "CF", "C", "2B", "3B", "1B", "RF", "LF", "DH"]
    ordered = [b for b in priority if b in hitter_buckets] + [b for b in hitter_buckets if b not in priority]
    hitter_quota = {b: base for b in hitter_buckets}
    for b in ordered[:extra]:
        hitter_quota[b] += 1
    sp = pitcher_total // 3
    rp = pitcher_total - sp
    return hitter_quota, {"SP": sp, "RP": rp}


def allocate(
    candidates: List[Dict[str, object]],
    hitter_quota: Dict[str, int],
    pitcher_quota: Dict[str, int],
    hitter_buckets: List[str],
) -> Dict[str, List[Dict[str, object]]]:
    invited: Dict[str, List[Dict[str, object]]] = defaultdict(list)
    hitter_overflow: List[Dict[str, object]] = []
    pitcher_overflow: List[Dict[str, object]] = []

    for p in candidates:
        b = bucket_for(p, hitter_buckets)
        if b == "FLEX":
            hitter_overflow.append(p)
            continue
        if b in hitter_buckets:
            if len(invited[b]) < hitter_quota.get(b, 0):
                invited[b].append(p)
            else:
                hitter_overflow.append(p)
        elif b in PITCHER_BUCKETS:
            if len(invited[b]) < pitcher_quota.get(b, 0):
                invited[b].append(p)
            else:
                pitcher_overflow.append(p)

    hitter_flex = sum(max(0, hitter_quota.get(b, 0) - len(invited[b])) for b in hitter_buckets)
    pitcher_flex = sum(max(0, pitcher_quota.get(b, 0) - len(invited[b])) for b in PITCHER_BUCKETS)
    for p in hitter_overflow[:hitter_flex]:
        invited["HITTER_FLEX"].append(p)
    for p in pitcher_overflow[:pitcher_flex]:
        invited["PITCHER_FLEX"].append(p)
    return invited


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def write_csv(path: Path, invited: Dict[str, List[Dict[str, object]]]) -> None:
    fieldnames = [
        "Bucket", "Name", "Age", "Level", "Team",
        "Current_Position", "Projected_Position",
        "VOS_Score", "VOS_Potential", "Composite", "ID",
    ]
    rows: List[Dict[str, object]] = []
    bucket_order = HITTER_BUCKETS + ["HITTER_FLEX", "SP", "RP", "PITCHER_FLEX"]
    for b in bucket_order:
        for p in invited.get(b, []):
            rows.append({
                "Bucket": b,
                "Name": p["name"],
                "Age": p["age"],
                "Level": p["level"],
                "Team": p["team"],
                "Current_Position": p["current_pos"],
                "Projected_Position": p["projected_pos"],
                "VOS_Score": round(p["vos_score"], 2) if p["vos_score"] is not None else "",
                "VOS_Potential": round(p["vos_potential"], 2) if p["vos_potential"] is not None else "",
                "Composite": round(p["composite"], 2),
                "ID": p["id"],
            })
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def _row_cells(p: Dict[str, object], show_level: bool) -> List[str]:
    age = f"{p['age']:.0f}" if p["age"] is not None else "—"
    vos = f"{p['vos_score']:.2f}" if p["vos_score"] is not None else "—"
    pot = f"{p['vos_potential']:.2f}" if p["vos_potential"] is not None else "—"
    comp = f"{p['composite']:.2f}"
    cells = [str(p["name"]), age]
    if show_level:
        cells.append(str(p["level"]))
    cells.extend([str(p["team"]), str(p["current_pos"]), str(p["projected_pos"]), vos, pot, comp])
    return cells


def _section(title: str, players: List[Dict[str, object]], note: str = "",
             show_level: bool = True) -> List[str]:
    if not players:
        return [f"### {title}", "", "_(no candidates)_", ""]
    lines = [f"### {title}"]
    if note:
        lines.append("")
        lines.append(f"_{note}_")
    lines.append("")
    if show_level:
        header = ["Name", "Age", "Lvl", "Team", "Cur", "Proj", "Ability", "Potential", "Composite"]
    else:
        header = ["Name", "Age", "Team", "Cur", "Proj", "Ability", "Potential", "Composite"]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join(["---"] * len(header)) + " |")
    for p in players:
        lines.append("| " + " | ".join(_row_cells(p, show_level)) + " |")
    lines.append("")
    return lines


def write_md(
    path: Path,
    org: str,
    league: str,
    eval_path: Path,
    invited: Dict[str, List[Dict[str, object]]],
    hitter_quota: Dict[str, int],
    pitcher_quota: Dict[str, int],
    max_total: int,
) -> None:
    total_invited = sum(len(v) for v in invited.values())
    hitter_buckets_all = HITTER_BUCKETS + ["HITTER_FLEX"]
    pitcher_buckets_all = PITCHER_BUCKETS + ["PITCHER_FLEX"]

    all_hitters: List[Dict[str, object]] = []
    for b in hitter_buckets_all:
        all_hitters.extend(invited.get(b, []))
    all_pitchers: List[Dict[str, object]] = []
    for b in pitcher_buckets_all:
        all_pitchers.extend(invited.get(b, []))

    lines: List[str] = []
    lines.append(f"# Spring Training Invite List — {org}")
    lines.append("")
    lines.append(f"_League: **{league}** · Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}_  ")
    lines.append(f"_Source: `{eval_path.name}`_")
    lines.append("")
    lines.append(f"**Total invites:** {total_invited} of {max_total} max  ")
    lines.append(f"**Split:** {len(all_hitters)} hitters / {len(all_pitchers)} pitchers")
    lines.append("")
    lines.append(f"**Composite formula:** `{W_ABILITY:.2f} × VOS_Score + {W_POTENTIAL:.2f} × VOS_Potential`")
    lines.append("")

    # ---- Position slot plan ----
    lines.append("## Position Slot Plan")
    lines.append("")
    lines.append("| Side | Slot | Quota | Filled |")
    lines.append("| --- | --- | --- | --- |")
    for b in HITTER_BUCKETS:
        lines.append(f"| Hitter | {b} | {hitter_quota.get(b, 0)} | {len(invited.get(b, []))} |")
    lines.append(f"| Hitter | FLEX | — | {len(invited.get('HITTER_FLEX', []))} |")
    for b in PITCHER_BUCKETS:
        lines.append(f"| Pitcher | {b} | {pitcher_quota.get(b, 0)} | {len(invited.get(b, []))} |")
    lines.append(f"| Pitcher | FLEX | — | {len(invited.get('PITCHER_FLEX', []))} |")
    lines.append("")

    # ---- By level ----
    lines.append("## By Level")
    lines.append("")
    lines.append("_Quick reference: invitees grouped by minor-league level, sorted by composite._")
    lines.append("")

    by_level_hitters: Dict[str, List[Dict[str, object]]] = defaultdict(list)
    by_level_pitchers: Dict[str, List[Dict[str, object]]] = defaultdict(list)
    for p in all_hitters:
        by_level_hitters[str(p["level"])].append(p)
    for p in all_pitchers:
        by_level_pitchers[str(p["level"])].append(p)

    lines.append("| Level | Hitters | Pitchers | Total |")
    lines.append("| --- | --- | --- | --- |")
    for lvl in LEVEL_DISPLAY_ORDER:
        h = len(by_level_hitters.get(lvl, []))
        pi = len(by_level_pitchers.get(lvl, []))
        if h + pi == 0:
            continue
        lines.append(f"| {lvl} | {h} | {pi} | {h + pi} |")
    lines.append(f"| **Total** | **{len(all_hitters)}** | **{len(all_pitchers)}** | **{total_invited}** |")
    lines.append("")

    for lvl in LEVEL_DISPLAY_ORDER:
        h_players = sorted(by_level_hitters.get(lvl, []), key=lambda r: r["composite"], reverse=True)
        p_players = sorted(by_level_pitchers.get(lvl, []), key=lambda r: r["composite"], reverse=True)
        if not h_players and not p_players:
            continue
        lines.append(f"### {lvl}")
        lines.append("")
        lines.extend(_section(f"{lvl} Hitters", h_players, show_level=False))
        lines.extend(_section(f"{lvl} Pitchers", p_players, show_level=False))

    # ---- Hitters by position ----
    lines.append("## Hitters (by Position)")
    lines.append("")
    for b in HITTER_BUCKETS:
        lines.extend(_section(b, invited.get(b, [])))
    lines.extend(_section(
        "Hitter Flex",
        invited.get("HITTER_FLEX", []),
        note="DH-primary players and overflow from positions that couldn't fill their quota.",
    ))

    # ---- Pitchers by role ----
    lines.append("## Pitchers (by Role)")
    lines.append("")
    lines.extend(_section("Starting Pitchers (SP)", invited.get("SP", [])))
    lines.extend(_section("Relievers (RP / CL)", invited.get("RP", [])))
    lines.extend(_section(
        "Pitcher Flex",
        invited.get("PITCHER_FLEX", []),
        note="Overflow from SP/RP buckets that couldn't fill their quota.",
    ))

    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    if args.max_total <= 0:
        logger.error("--max must be a positive integer.")
        return 2

    try:
        org_name = resolve_org_name(args.league, args.org_code)
    except (FileNotFoundError, ValueError) as e:
        logger.error("%s", e)
        return 2
    logger.info("Resolved --org-code %s -> %s", args.org_code, org_name)

    eval_path = args.input or find_latest_eval(args.league, args.org_code)
    logger.info("Reading eval: %s", eval_path)
    rows = load_eval(eval_path)

    candidates = build_candidates(rows, org_name)
    logger.info("Found %d minor-league candidates in %s.", len(candidates), org_name)
    if not candidates:
        logger.error("No candidates found for %s in %s.", org_name, eval_path.name)
        return 1

    hitter_buckets = list(HITTER_BUCKETS)
    if args.include_dh:
        hitter_buckets.append("DH")

    hitter_quota, pitcher_quota = split_slots(args.max_total, hitter_buckets)
    invited = allocate(candidates, hitter_quota, pitcher_quota, hitter_buckets)

    out_dir = args.output_dir or (SCRIPT_DIR / args.league / "spring_training")
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    org_slug = slug(org_name)
    md_path = out_dir / f"{org_slug}_{ts}.md"
    csv_path = out_dir / f"{org_slug}_{ts}.csv"

    write_csv(csv_path, invited)
    write_md(md_path, org_name, args.league, eval_path, invited,
             hitter_quota, pitcher_quota, args.max_total)

    total_invited = sum(len(v) for v in invited.values())
    logger.info("Wrote %s (%d invites)", md_path, total_invited)
    logger.info("Wrote %s", csv_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

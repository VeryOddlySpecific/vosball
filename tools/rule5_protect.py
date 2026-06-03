#!/usr/bin/env python3
"""
rule5_protect.py — identify Rule 5 protection candidates for an org.

Queries the StatsPlus /players endpoint for current secondary-roster status,
joins with the latest evaluation_summary CSV for VOS/Potential scores, then
filters for players who are:
  - in the specified org
  - NOT currently on the secondary roster (is_on_secondary != 1)
  - at least the minimum age (default 23)

Usage:
  python rule5_protect.py --league uba --org-id 507
  python rule5_protect.py --league uba --org-id 507 --min-age 23 --top 30
  python rule5_protect.py --league uba --org-name "Atlanta Bandits"

Requires stats.py (sapi) and config/league_url.json to be present in the
script directory.
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
import glob
import json
import logging
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import stats as sapi

SCRIPT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CACHE = SCRIPT_DIR / "cache"
LEAGUE_URL_CONFIG = SCRIPT_DIR / "config" / "league_url.json"
TEAMS_CONFIG = SCRIPT_DIR / "config" / "teams-{league}.json"

logger = logging.getLogger("rule5_protect")


# --------------------------------------------------------------------------
# Config / lookups
# --------------------------------------------------------------------------

def load_team_lookup(league: str) -> Dict[str, Dict[str, str]]:
    """Return {team_id_str: {'Name':..., 'Nickname':..., 'Parent':...}}."""
    path = Path(str(TEAMS_CONFIG).format(league=league.lower()))
    if not path.exists():
        logger.warning("Teams config not found: %s", path)
        return {}
    with path.open() as f:
        return json.load(f) or {}


def resolve_org_id(team_lookup: Dict[str, Dict[str, str]],
                   org_id: Optional[int],
                   org_name: Optional[str]) -> Tuple[int, str]:
    """Resolve --org-id / --org-name into (org_id, display_name)."""
    if org_id is not None:
        info = team_lookup.get(str(org_id))
        if not info:
            raise SystemExit(f"Org ID {org_id} not in teams config.")
        return org_id, f"{info.get('Name','')} {info.get('Nickname','')}".strip()
    if org_name:
        for tid, info in team_lookup.items():
            full = f"{info.get('Name','')} {info.get('Nickname','')}".strip()
            if full.lower() == org_name.lower() and int(info.get("Parent", 0)) == 0:
                return int(tid), full
        raise SystemExit(f"Org name '{org_name}' not found as a top-level org.")
    raise SystemExit("Must provide --org-id or --org-name.")


# --------------------------------------------------------------------------
# Eval CSV
# --------------------------------------------------------------------------

def latest_eval_csv(league: str) -> Optional[Path]:
    pattern = str(SCRIPT_DIR / league.lower() / "eval" / f"evaluation_summary_{league.lower()}_*.csv")
    matches = sorted(glob.glob(pattern))
    return Path(matches[-1]) if matches else None


def load_eval(path: Path) -> Dict[str, Dict[str, str]]:
    """Return {pid: row} from an evaluation_summary CSV."""
    out: Dict[str, Dict[str, str]] = {}
    with path.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            pid = (row.get("ID") or "").strip()
            if pid:
                out[pid] = row
    return out


# --------------------------------------------------------------------------
# Filter / rank
# --------------------------------------------------------------------------

def _bool_truthy(v: str) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "t", "y"}


def _to_float(v) -> float:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return 0.0


# Level ordering for tie-break / display
LEVEL_ORDER = {"ML": 0, "AAA": 1, "AA": 2, "A+": 3, "A": 4, "A-": 5, "R": 6, "INT": 7}


def find_candidates(
    players: Dict[str, Dict[str, str]],
    eval_rows: Dict[str, Dict[str, str]],
    org_id: int,
    min_age: int,
) -> List[Dict[str, object]]:
    """Join /players + eval. Return list of candidate dicts."""
    candidates: List[Dict[str, object]] = []
    for pid, p in players.items():
        # Active org filter
        try:
            p_org = int(p.get("organization_id") or 0)
        except ValueError:
            p_org = 0
        if p_org != org_id:
            continue

        # Status filters
        if _bool_truthy(p.get("retired", "")):
            continue
        if not _bool_truthy(p.get("is_active", "1")):
            continue

        # Age filter (use /players age — current)
        age = _to_float(p.get("age"))
        if age < min_age:
            continue

        # Secondary-roster filter — the whole point of this script
        on_secondary = _bool_truthy(p.get("is_on_secondary", ""))
        if on_secondary:
            continue

        ev = eval_rows.get(pid, {})
        vos = _to_float(ev.get("VOS_Score"))
        vos_pot = _to_float(ev.get("VOS_Potential"))
        if vos == 0.0 and vos_pot == 0.0 and not ev:
            # No eval row for this player — keep but flag
            best = 0.0
            level = ""
        else:
            best = max(vos, vos_pot)
            level = ev.get("League_Level", "")

        name = (ev.get("Name") or f"{p.get('first_name','')} {p.get('last_name','')}").strip()
        pos = ev.get("Pos") or p.get("pos") or p.get("role") or ""

        candidates.append({
            "pid": pid,
            "name": name,
            "pos": pos,
            "age": int(age) if age else "",
            "level": level,
            "vos": vos,
            "vos_pot": vos_pot,
            "best": best,
            "tier": ev.get("VOS_Tier", ""),
            "pot_tier": ev.get("VOS_Potential_Tier", ""),
            "in_eval": bool(ev),
            "dl60": _bool_truthy(p.get("is_on_dl60", "")),
            "waivers": _bool_truthy(p.get("is_on_waivers", "")),
            "dfa": _bool_truthy(p.get("designated_for_assignment", "")),
        })

    candidates.sort(key=lambda c: (-c["best"], LEVEL_ORDER.get(c["level"], 99), c["name"]))
    return candidates


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--league", required=True, help="League key (e.g., uba).")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--org-id", type=int, help="Org/parent team ID (e.g., 507 for Atlanta).")
    g.add_argument("--org-name", help='Top-level org display name (e.g., "Atlanta Bandits").')
    p.add_argument("--min-age", type=int, default=23, help="Minimum age (default: 23).")
    p.add_argument("--top", type=int, default=40, help="Number of rows to print (default: 40).")
    p.add_argument("--eval-csv", help="Override eval CSV path. Default: latest under <league>/eval/.")
    p.add_argument("--base-url", help="Override /players base URL.")
    p.add_argument("--no-cache", action="store_true", help="Bypass the /players disk cache.")
    p.add_argument("--out-csv", help="Optional CSV output path for the full candidate list.")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

    # Resolve org
    team_lookup = load_team_lookup(args.league)
    org_id, org_name = resolve_org_id(team_lookup, args.org_id, args.org_name)

    # Resolve eval CSV
    eval_path = Path(args.eval_csv) if args.eval_csv else latest_eval_csv(args.league)
    if not eval_path or not eval_path.exists():
        print(f"ERROR: eval CSV not found for league={args.league}", file=sys.stderr)
        return 2
    print(f"Eval CSV: {eval_path.name}")
    eval_rows = load_eval(eval_path)

    # Fetch /players
    base_url = sapi.resolve_base_url(args.league, args.base_url, LEAGUE_URL_CONFIG)
    if not base_url:
        print(f"ERROR: could not resolve base URL for league={args.league}", file=sys.stderr)
        return 2
    cache_dir = None if args.no_cache else DEFAULT_CACHE
    players = sapi.build_players_lookup(base_url, cache_dir=cache_dir)
    if not players:
        print(f"ERROR: /players returned no rows (url base: {base_url})", file=sys.stderr)
        return 2
    print(f"/players rows: {len(players)}")

    # Candidate selection
    cands = find_candidates(players, eval_rows, org_id, args.min_age)
    print(f"\nOrg: {org_name} (id={org_id})  | min age: {args.min_age}")
    print(f"R5-eligible (not on secondary, age >= {args.min_age}): {len(cands)}\n")

    # Print top
    header = f"{'Name':<24} {'Pos':<5} {'Age':<4} {'Lvl':<4} {'VOS':>6} {'Pot':>6} {'Tier':<22} {'PotTier':<22}"
    print(header)
    print("-" * len(header))
    for c in cands[:args.top]:
        flags = []
        if c["dl60"]: flags.append("DL60")
        if c["waivers"]: flags.append("WVR")
        if c["dfa"]: flags.append("DFA")
        if not c["in_eval"]: flags.append("no-eval")
        flag_str = (" " + ",".join(flags)) if flags else ""
        print(f"{c['name']:<24} {c['pos']:<5} {str(c['age']):<4} {c['level']:<4} "
              f"{c['vos']:>6.1f} {c['vos_pot']:>6.1f} {c['tier']:<22.22} {c['pot_tier']:<22.22}{flag_str}")

    # Optional CSV dump
    if args.out_csv:
        out_path = Path(args.out_csv)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["pid", "name", "pos", "age", "level", "vos", "vos_pot",
                        "tier", "pot_tier", "in_eval", "dl60", "waivers", "dfa"])
            for c in cands:
                w.writerow([c["pid"], c["name"], c["pos"], c["age"], c["level"],
                            f"{c['vos']:.2f}", f"{c['vos_pot']:.2f}",
                            c["tier"], c["pot_tier"],
                            int(c["in_eval"]), int(c["dl60"]),
                            int(c["waivers"]), int(c["dfa"])])
        print(f"\nWrote {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

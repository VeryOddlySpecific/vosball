#!/usr/bin/env python3
"""
rule5_draft.py — Rule 5 draft recommendations matched to your org's ML needs.

Reads your org's latest ML depth chart to identify positional holes and weak
slots, then scans the rest of the league for Rule 5 eligible players (not on
the 40-man / secondary roster) who can fill those gaps.

Usage:
  python rule5_draft.py --league uba --org atb
  python rule5_draft.py --league uba --org atb --min-age 23 --max-age 27
  python rule5_draft.py --league uba --org atb --top 5
  python rule5_draft.py --league uba --org atb --show-all
  python rule5_draft.py --league uba --org atb --out-csv r5_targets.csv

Age qualifier flags:
  --min-age   Minimum age to include (default: 23)
  --max-age   Maximum age to include (default: no cap)

By default only players whose position matches a HOLE or WEAK ML slot are
shown.  Pass --show-all to see every eligible player league-wide.

Requires stats.py (sapi) and config/league_url.json in the script directory.
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
import logging
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import stats as sapi

SCRIPT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CACHE = SCRIPT_DIR / "cache"
LEAGUE_URL_CONFIG = SCRIPT_DIR / "config" / "league_url.json"
TEAMS_CONFIG     = SCRIPT_DIR / "config" / "teams-{league}.json"
DEPTH_CONFIG     = SCRIPT_DIR / "config" / "depth_config.json"

logger = logging.getLogger("rule5_draft")

# Sentinel: slot exists in config but no player is slotted there.
_HOLE = -999.0

_DEPTH_CSV_RE = re.compile(
    r"^(?P<org>[a-z0-9_]+?)_(?P<level>ML|AAA|AA|A\+|A-|A|R|R-[A-Z]{3})_"
    r"(?P<ts>\d{8}_\d{6})\.csv$"
)

LEVEL_ORDER = {"ML": 0, "AAA": 1, "AA": 2, "A+": 3, "A": 4, "A-": 5, "R": 6, "INT": 7}


# ---------------------------------------------------------------------------
# Org resolution  (slug → full name → org_id)
# ---------------------------------------------------------------------------

def _load_park_factors(league: str) -> Dict[str, str]:
    """Return {team_code_lower: full_team_name} from the league's park-factors file."""
    pf_path = SCRIPT_DIR / "config" / f"{league.lower()}-park-factors.json"
    if not pf_path.exists():
        return {}
    with pf_path.open() as f:
        data = json.load(f)
    out: Dict[str, str] = {}
    for team_name, info in data.get("teams", {}).items():
        code = (info.get("team_info", {}).get("team_code") or "").strip().lower()
        if code:
            out[code] = team_name
    return out


def _load_team_lookup(league: str) -> Dict[str, Dict[str, str]]:
    """Return {team_id_str: {Name, Nickname, Parent}}."""
    path = Path(str(TEAMS_CONFIG).format(league=league.lower()))
    if not path.exists():
        return {}
    with path.open() as f:
        return json.load(f) or {}


def _org_id_from_name(team_lookup: Dict[str, Dict[str, str]], full_name: str) -> Optional[int]:
    """Find the top-level org_id for a given full name ('Atlanta Bandits')."""
    for tid, info in team_lookup.items():
        if int(info.get("Parent", 1)) != 0:
            continue
        display = f"{info.get('Name', '')} {info.get('Nickname', '')}".strip()
        if display.lower() == full_name.lower():
            return int(tid)
    return None


def _build_org_name_lookup(team_lookup: Dict[str, Dict[str, str]]) -> Dict[int, str]:
    """Return {org_id: 'Name Nickname'} for top-level orgs."""
    out: Dict[int, str] = {}
    for tid, info in team_lookup.items():
        if int(info.get("Parent", 1)) == 0:
            out[int(tid)] = f"{info.get('Name', '')} {info.get('Nickname', '')}".strip()
    return out


def resolve_org(league: str, slug: str) -> Tuple[int, str, str]:
    """Resolve a slug (e.g. 'atb') to (org_id, full_name, slug).

    Raises SystemExit if resolution fails.
    """
    pf_map = _load_park_factors(league)          # {slug: full_name}
    team_lookup = _load_team_lookup(league)

    full_name = pf_map.get(slug.lower())
    if not full_name:
        # Fall back: scan team_lookup for a display-name match.
        for info in team_lookup.values():
            display = f"{info.get('Name', '')} {info.get('Nickname', '')}".strip()
            if display.lower().replace(" ", "") == slug.lower().replace(" ", ""):
                full_name = display
                break
    if not full_name:
        raise SystemExit(
            f"Cannot resolve org slug '{slug}' for league '{league}'. "
            f"Known slugs: {sorted(pf_map)}"
        )

    org_id = _org_id_from_name(team_lookup, full_name)
    if org_id is None:
        raise SystemExit(
            f"Found org name '{full_name}' but no top-level org_id in "
            f"teams-{league}.json. Check Parent=0 entries."
        )

    return org_id, full_name, slug.lower()


# ---------------------------------------------------------------------------
# Depth chart discovery + needs parsing
# ---------------------------------------------------------------------------

def _discover_latest_depth_csv(depth_dir: Path, org_slug: str,
                                level: str = "ML") -> Optional[Path]:
    best_ts = ""
    best: Optional[Path] = None
    for path in depth_dir.glob(f"{org_slug}_{level}_*.csv"):
        m = _DEPTH_CSV_RE.match(path.name)
        if m and m.group("org") == org_slug and m.group("level") == level:
            if m.group("ts") > best_ts:
                best_ts = m.group("ts")
                best = path
    return best


def load_ml_needs(csv_path: Path) -> Dict[str, float]:
    """Parse a depth chart CSV into a positional needs map.

    Keys: hitter positions (C, 1B, 2B, 3B, SS, LF, CF, RF, DH)
          pitcher roles  (SP, CL, SU, MR, LR)

    Values:
      _HOLE  = slot expected but no player assigned
      < 0    = starter composite below min_comp_threshold (weak)
      >= 0   = starter meets/exceeds threshold (filled)
    """
    # Pull expected slots from depth_config if available.
    default_hitter_mins = {"C": 2, "1B": 1, "2B": 1, "SS": 1, "3B": 1,
                           "LF": 1, "CF": 1, "RF": 1, "DH": 0}
    default_role_counts = {"SP": 5, "CL": 1, "SU": 2, "MR": 4, "LR": 1}
    if DEPTH_CONFIG.exists():
        with DEPTH_CONFIG.open() as f:
            dcfg = json.load(f)
        ml_cfg = dcfg.get("levels", {}).get("ML", {})
        default_hitter_mins = ml_cfg.get("hitter_position_min", default_hitter_mins)
        default_role_counts = ml_cfg.get("pitcher_role_count", default_role_counts)

    needs: Dict[str, float] = {}
    for pos in default_hitter_mins:
        needs[pos] = _HOLE
    for role in default_role_counts:
        needs[role] = _HOLE

    with csv_path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            tier = (row.get("tier") or "").strip()
            if not tier:
                continue
            is_p = (row.get("is_pitcher") or "").strip().lower() == "true"
            gap_raw = (row.get("starter_gap") or "").strip()
            gap: Optional[float] = None
            if gap_raw:
                try:
                    gap = float(gap_raw)
                except ValueError:
                    pass

            if is_p:
                m = re.match(r"^(SP)(\d+)$", tier)
                if m and int(m.group(2)) == 1:
                    needs["SP"] = gap if gap is not None else 0.0
                    continue
                m = re.match(r"^(CL|SU|MR|LR)-(\d+)$", tier)
                if m and int(m.group(2)) == 1:
                    needs[m.group(1)] = gap if gap is not None else 0.0
            else:
                m = re.match(r"^([A-Z0-9]+)-(\d+)$", tier)
                if m and int(m.group(2)) == 1 and m.group(1) in needs:
                    needs[m.group(1)] = gap if gap is not None else 0.0

    return needs


def need_priority(gap: float) -> int:
    """Lower = more urgent. HOLE=0, WEAK=1, OK=2."""
    if gap <= _HOLE / 2:
        return 0
    if gap < 0:
        return 1
    return 2


def need_label(gap: float) -> str:
    if gap <= _HOLE / 2:
        return "HOLE"
    if gap < 0:
        return f"WEAK({gap:+.1f})"
    return f"ok({gap:+.1f})"


def _pos_to_need_keys(pos: str) -> List[str]:
    p = pos.upper().strip()
    if p == "SP":
        return ["SP"]
    if p in {"RP", "CL", "SU", "MR", "LR", "P"}:
        return ["CL", "SU", "MR", "LR"]
    if p in {"C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "DH"}:
        return [p]
    if p == "OF":
        return ["LF", "CF", "RF"]
    return []


def best_need_for_pos(pos: str, needs: Dict[str, float]) -> Tuple[Optional[float], str]:
    """Return (worst_gap, label) for pos against the needs map.
    For bullpen targets checks all pen roles; surfaces the most urgent.
    """
    keys = _pos_to_need_keys(pos)
    present = [(needs[k], k) for k in keys if k in needs]
    if not present:
        return None, ""
    worst_gap, worst_key = min(present, key=lambda x: x[0])
    label = need_label(worst_gap)
    if len(keys) > 1:
        label = f"{worst_key}:{label}"
    return worst_gap, label


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bool_truthy(v: str) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "t", "y"}


def _to_float(v) -> float:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return 0.0


def _latest_eval_csv(league: str) -> Optional[Path]:
    import glob as _glob
    pattern = str(SCRIPT_DIR / league.lower() / "eval" /
                  f"evaluation_summary_{league.lower()}_*.csv")
    matches = sorted(_glob.glob(pattern))
    return Path(matches[-1]) if matches else None


def _load_eval(path: Path) -> Dict[str, Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {}
    with path.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            pid = (row.get("ID") or "").strip()
            if pid:
                out[pid] = row
    return out


# ---------------------------------------------------------------------------
# Core: find Rule 5 targets
# ---------------------------------------------------------------------------

def find_targets(
    players: Dict[str, Dict[str, str]],
    eval_rows: Dict[str, Dict[str, str]],
    org_name_lookup: Dict[int, str],
    exclude_org_id: int,
    min_age: int,
    max_age: Optional[int],
    ml_needs: Optional[Dict[str, float]],
) -> List[Dict]:
    targets = []
    for pid, p in players.items():
        try:
            p_org = int(p.get("organization_id") or 0)
        except ValueError:
            p_org = 0
        if p_org == 0 or p_org == exclude_org_id:
            continue
        if _bool_truthy(p.get("retired", "")):
            continue
        if not _bool_truthy(p.get("is_active", "1")):
            continue

        age = _to_float(p.get("age"))
        if age < min_age:
            continue
        if max_age is not None and age > max_age:
            continue

        # Rule 5 eligible = NOT on 40-man (secondary roster)
        if _bool_truthy(p.get("is_on_secondary", "")):
            continue

        ev = eval_rows.get(pid, {})
        vos = _to_float(ev.get("VOS_Score"))
        vos_pot = _to_float(ev.get("VOS_Potential"))
        best = max(vos, vos_pot)
        pos = ev.get("Pos") or p.get("pos") or p.get("role") or ""
        level = ev.get("League_Level", "")
        name = (ev.get("Name") or
                f"{p.get('first_name', '')} {p.get('last_name', '')}").strip()
        org_display = org_name_lookup.get(p_org, f"OrgID:{p_org}")

        need_gap: Optional[float] = None
        need_lbl = ""
        if ml_needs is not None:
            need_gap, need_lbl = best_need_for_pos(pos, ml_needs)

        targets.append({
            "pid": pid,
            "name": name,
            "pos": pos,
            "age": int(age) if age else "",
            "org": org_display,
            "org_id": p_org,
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
            "need_gap": need_gap,
            "need_lbl": need_lbl,
        })

    # Sort: need urgency first (HOLE > WEAK > ok > no-match),
    # then best VOS descending within each need group.
    def _sort_key(t):
        gap = t["need_gap"]
        pri = need_priority(gap) if gap is not None else 3
        return (pri, -t["best"], LEVEL_ORDER.get(t["level"], 99))

    targets.sort(key=_sort_key)
    return targets


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--league", required=True,
                   help="League key (e.g. uba).")
    p.add_argument("--org", required=True,
                   help="Your org slug (e.g. atb). Used to find depth charts "
                        "and exclude your own players from the pool.")

    # Age qualifiers
    p.add_argument("--min-age", type=int, default=23,
                   help="Minimum player age (default: 23).")
    p.add_argument("--max-age", type=int, default=None,
                   help="Maximum player age, inclusive (default: no cap).")

    # Output
    p.add_argument("--top", type=int, default=5,
                   help="Max candidates to show per need slot (default: 5).")
    p.add_argument("--show-all", action="store_true",
                   help="Show all Rule 5 eligible players, not just those "
                        "matching a need. Sorted by need urgency then VOS.")
    p.add_argument("--depth-dir", type=Path, default=None,
                   help="Override depth chart directory "
                        "(default: {league}/depth/).")
    p.add_argument("--eval-csv", help="Override eval CSV path.")
    p.add_argument("--base-url", help="Override /players base URL.")
    p.add_argument("--no-cache", action="store_true",
                   help="Bypass the /players disk cache.")
    p.add_argument("--out-csv", help="Write full target list to this CSV.")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    # --- Resolve org ---
    org_id, org_name, org_slug = resolve_org(args.league, args.org)
    print(f"Org       : {org_name}  (id={org_id}, slug={org_slug})")

    # --- Load ML depth chart → needs ---
    depth_dir = args.depth_dir or (SCRIPT_DIR / args.league.lower() / "depth")
    depth_csv = _discover_latest_depth_csv(depth_dir, org_slug, level="ML")
    ml_needs: Optional[Dict[str, float]] = None

    if depth_csv is None:
        print(f"WARNING   : no ML depth chart found in {depth_dir} for slug "
              f"'{org_slug}'. Run depth_chart.py --level ML first.",
              file=sys.stderr)
    else:
        ml_needs = load_ml_needs(depth_csv)
        print(f"Depth CSV : {depth_csv.name}")

        holes  = sorted(pos for pos, g in ml_needs.items() if g <= _HOLE / 2)
        weak   = sorted(pos for pos, g in ml_needs.items()
                        if _HOLE / 2 < g < 0)
        filled = sorted(pos for pos, g in ml_needs.items() if g >= 0)

        if holes:
            print(f"  HOLES   : {', '.join(holes)}")
        if weak:
            print(f"  WEAK    : {', '.join(f'{p}({ml_needs[p]:+.1f})' for p in weak)}")
        if filled and not holes and not weak:
            print(f"  (all positions filled above threshold)")
        print()

    # --- Eval CSV ---
    eval_path = Path(args.eval_csv) if args.eval_csv else _latest_eval_csv(args.league)
    if not eval_path or not eval_path.exists():
        print(f"ERROR: eval CSV not found for league={args.league}", file=sys.stderr)
        return 2
    print(f"Eval CSV  : {eval_path.name}")
    eval_rows = _load_eval(eval_path)

    # --- /players ---
    base_url = sapi.resolve_base_url(args.league, args.base_url, LEAGUE_URL_CONFIG)
    if not base_url:
        print(f"ERROR: could not resolve base URL for league={args.league}",
              file=sys.stderr)
        return 2
    cache_dir = None if args.no_cache else DEFAULT_CACHE
    players = sapi.build_players_lookup(base_url, cache_dir=cache_dir)
    if not players:
        print(f"ERROR: /players returned no rows", file=sys.stderr)
        return 2
    print(f"/players  : {len(players)} rows")

    team_lookup    = _load_team_lookup(args.league)
    org_name_lookup = _build_org_name_lookup(team_lookup)

    # --- Build target list ---
    all_targets = find_targets(
        players=players,
        eval_rows=eval_rows,
        org_name_lookup=org_name_lookup,
        exclude_org_id=org_id,
        min_age=args.min_age,
        max_age=args.max_age,
        ml_needs=ml_needs,
    )

    # Filter to need-matching unless --show-all
    if args.show_all or ml_needs is None:
        display_targets = all_targets
    else:
        display_targets = [t for t in all_targets
                           if t["need_gap"] is not None and t["need_gap"] < 0]

    age_range = f">= {args.min_age}" + (f", <= {args.max_age}" if args.max_age else "")
    pool_label = "all eligible" if args.show_all else "need-matched"
    print(f"\nAge       : {age_range}")
    print(f"Pool      : {len(display_targets)} {pool_label} Rule 5 targets\n")

    # --- Display ---
    if not display_targets:
        print("No targets found.")
        return 0

    show_needs = ml_needs is not None
    need_hdr = f" {'Need':<18}" if show_needs else ""
    header = (f"{'Name':<24} {'Pos':<5} {'Age':<4} {'Lvl':<4} "
              f"{'VOS':>6} {'Pot':>6} {'Tier':<20} {'PotTier':<20}"
              f"{need_hdr} Org")
    sep = "-" * (len(header) + 20)

    if args.show_all or ml_needs is None:
        # Flat list up to args.top * sensible cap
        print(header)
        print(sep)
        for t in display_targets[:args.top * 10]:
            _print_row(t, show_needs)
    else:
        # Group by need slot, show top-N per slot.
        _print_by_need(display_targets, ml_needs, args.top, header, sep, show_needs)

    # --- Optional CSV ---
    if args.out_csv:
        out_path = Path(args.out_csv)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["pid", "name", "pos", "age", "org", "org_id", "level",
                        "vos", "vos_pot", "tier", "pot_tier",
                        "in_eval", "dl60", "waivers", "dfa",
                        "need_gap", "need_lbl"])
            for t in all_targets:
                w.writerow([
                    t["pid"], t["name"], t["pos"], t["age"],
                    t["org"], t["org_id"], t["level"],
                    f"{t['vos']:.2f}", f"{t['vos_pot']:.2f}",
                    t["tier"], t["pot_tier"],
                    int(t["in_eval"]), int(t["dl60"]),
                    int(t["waivers"]), int(t["dfa"]),
                    (f"{t['need_gap']:.2f}" if t["need_gap"] is not None else ""),
                    t["need_lbl"],
                ])
        print(f"\nWrote {out_path}")

    return 0


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _flag_str(t: Dict) -> str:
    flags = []
    if t["dl60"]:        flags.append("DL60")
    if t["waivers"]:     flags.append("WVR")
    if t["dfa"]:         flags.append("DFA")
    if not t["in_eval"]: flags.append("no-eval")
    return (" [" + ",".join(flags) + "]") if flags else ""


def _print_row(t: Dict, show_needs: bool) -> None:
    need_col = f" {str(t['need_lbl']):<18}" if show_needs else ""
    print(
        f"{t['name']:<24} {t['pos']:<5} {str(t['age']):<4} {t['level']:<4} "
        f"{t['vos']:>6.1f} {t['vos_pot']:>6.1f} "
        f"{t['tier']:<20.20} {t['pot_tier']:<20.20}"
        f"{need_col} {t['org']}{_flag_str(t)}"
    )


def _print_by_need(
    targets: List[Dict],
    ml_needs: Dict[str, float],
    top_n: int,
    header: str,
    sep: str,
    show_needs: bool,
) -> None:
    """Print targets grouped by need slot, most urgent first."""
    # Build ordered slot list: HOLE first, then WEAK by gap ascending.
    need_slots = [
        (pos, gap) for pos, gap in ml_needs.items() if gap < 0
    ]
    need_slots.sort(key=lambda x: (need_priority(x[1]), x[1]))

    if not need_slots:
        print("No HOLE or WEAK slots detected — nothing to fill.")
        return

    for pos, gap in need_slots:
        label = need_label(gap)
        # Collect targets whose best need key matches this slot.
        slot_targets = [
            t for t in targets
            if pos in _pos_to_need_keys(t["pos"])
        ]
        print(f"=== {pos}  ({label}) ===")
        if not slot_targets:
            print("  (no eligible Rule 5 targets found at this position)\n")
            continue
        print(header)
        print(sep)
        for t in slot_targets[:top_n]:
            _print_row(t, show_needs)
        print()


if __name__ == "__main__":
    sys.exit(main())

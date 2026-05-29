#!/usr/bin/env python3
"""
What-If — Interactive single-player VOS rating sandbox.

Load one player by ID from PlayerData-{league}.csv and their last-computed scores
from the most recent evaluation_summary_{league}_*.csv. Display base ratings and
component/total VOS. Then accept interactive rating overrides (e.g. PotCtrl=55)
and recompute scores on the fly to show the impact.

Usage:
    python what_if.py --league sahl --id 12345

Interactive commands (after loading a player):
    Rating=Value[,Rating=Value,...]   apply overrides (e.g. PotCtrl=55, Pow=45)
    show                               reprint current state
    diff                               show what has been changed from base
    reset                              clear all overrides
    role SP|RP                         force pitcher role (default: auto)
    help                               list commands
    quit / exit / q                    exit
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Reuse all scoring + loading from run_vos (v6 engine; alias kept as `v2`
# so the rest of this module — and player_card.py which `import what_if as wi`
# — continues to work unchanged).
import run_vos as v2


SCRIPT_DIR = Path(__file__).resolve().parent


# -----------------------------------------------------------------------------
# Loading helpers
# -----------------------------------------------------------------------------

def find_latest_eval_csv(league: str) -> Optional[Path]:
    """Find the most recent evaluation_summary_{league}_*.csv.

    Checks {league}/eval/ first, then the script directory as a fallback.
    """
    pattern = f"evaluation_summary_{league}_*.csv"
    candidates: List[Path] = []
    for search_dir in (SCRIPT_DIR / league / "eval", SCRIPT_DIR):
        if search_dir.exists():
            candidates.extend(search_dir.glob(pattern))
    if not candidates:
        return None
    # filename timestamps sort correctly lexicographically
    candidates.sort(key=lambda p: p.name)
    return candidates[-1]


def find_latest_org_eval_csv(league: str, org_abbrev: str) -> Optional[Path]:
    """Find the most recent evaluation_summary CSV under {league}/eval/{org_abbrev}/."""
    if not org_abbrev:
        return None
    search_dir = SCRIPT_DIR / league / "eval" / org_abbrev.lower()
    if not search_dir.exists():
        return None
    candidates = list(search_dir.glob(f"evaluation_summary_{league}_*.csv"))
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.name)
    return candidates[-1]


def load_latest_eval_row(eval_path: Path, player_id: str) -> Optional[Dict[str, str]]:
    """Return the row for player_id from a prior evaluation_summary CSV, or None."""
    if not eval_path.exists():
        return None
    with eval_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if (row.get("ID") or "").strip() == str(player_id).strip():
                return row
    return None


def load_player_row(league: str, player_id: str) -> Optional[Dict[str, str]]:
    """Return the row for player_id from PlayerData-{league}.csv, or None."""
    path = SCRIPT_DIR / "data" / v2.PLAYER_DATA_FILENAME_TEMPLATE.format(league=league)
    if not path.exists():
        print(f"ERROR: {path} not found", file=sys.stderr)
        return None
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if (row.get("ID") or "").strip() == str(player_id).strip():
                return row
    return None


# -----------------------------------------------------------------------------
# Scoring wrapper — builds an eval row (hitter or pitcher) from a player row
# -----------------------------------------------------------------------------

def score_player(
    row: Dict[str, str],
    cfg: Dict[str, Any],
    league_lookup: Dict[int, str],
    teams: Dict[int, str],
    role_override: Optional[str] = None,
    draft_mode: bool = False,
) -> Optional[Dict[str, Any]]:
    """Build the VOS eval output row for one player using vos_v2 logic."""
    if v2.is_pitcher(row):
        pos = (row.get("Pos") or "").strip().upper()
        role = role_override or ("RP" if pos in ("RP", "CL") else "SP")
        return v2.build_pitcher_row(row, cfg, league_lookup, teams, role=role, draft_mode=draft_mode)
    return v2.build_hitter_row(row, cfg, league_lookup, teams, draft_mode=draft_mode)


# -----------------------------------------------------------------------------
# Display helpers
# -----------------------------------------------------------------------------

# Fields we surface in the "base ratings" block.
HITTER_RATING_FIELDS = [
    # (label, current_col, potential_col_or_None)
    ("Cntct",   "Cntct", "PotCntct"),
    ("Gap",     "Gap",   "PotGap"),
    ("Pow",     "Pow",   "PotPow"),
    ("Eye",     "Eye",   "PotEye"),
    ("Ks",      "Ks",    "PotKs"),
    ("BABIP",   "BABIP", "PotBABIP"),
]
HITTER_DEFENSE_FIELDS = [
    ("IFR", "IFR"), ("IFE", "IFE"), ("IFA", "IFA"), ("TDP", "TDP"),
    ("OFR", "OFR"), ("OFE", "OFE"), ("OFA", "OFA"),
    ("CBlk", "CBlk"), ("CArm", "CArm"), ("CFrm", "CFrm"),
]
HITTER_BASERUNNING_FIELDS = [
    ("Speed", "Speed"), ("Run", "Run"), ("StlRt", "StlRt"), ("Steal", "Steal"),
]
POS_RATING_COLS = [
    ("C", "C", "PotC"), ("1B", "1B", "Pot1B"), ("2B", "2B", "Pot2B"),
    ("3B", "3B", "Pot3B"), ("SS", "SS", "PotSS"), ("LF", "LF", "PotLF"),
    ("CF", "CF", "PotCF"), ("RF", "RF", "PotRF"),
]

PITCHER_ABILITY_FIELDS = [
    ("Stf",  "Stf",  "PotStf"),
    ("Mov",  "Mov",  "PotMov"),
    ("Ctrl", "Ctrl", "PotCtrl"),   # Ctrl may be blank; Ctrl_R/Ctrl_L shown separately
    ("HRA",  "HRA",  "PotHRA"),
    ("PBABIP", "PBABIP", "PotPBABIP"),
    ("Vel",  "Vel",  None),
    ("Stm",  "Stm",  None),
    ("Hold", "Hold", None),
    ("GB",   "GB",   None),
]
PITCHER_SPLIT_FIELDS = [
    ("Ctrl_R", "Ctrl_R"), ("Ctrl_L", "Ctrl_L"),
    ("Stf_R",  "Stf_R"),  ("Stf_L",  "Stf_L"),
    ("Mov_R",  "Mov_R"),  ("Mov_L",  "Mov_L"),
    ("HRA_R",  "HRA_R"),  ("HRA_L",  "HRA_L"),
]
PITCH_FIELDS = [
    ("Fst",    "Fst",    "PotFst"),
    ("Snk",    "Snk",    "PotSnk"),
    ("Cutt",   "Cutt",   "PotCutt"),
    ("Crv",    "Crv",    "PotCrv"),
    ("Sld",    "Sld",    "PotSld"),
    ("Chg",    "Chg",    "PotChg"),
    ("Splt",   "Splt",   "PotSplt"),
    ("Frk",    "Frk",    "PotFrk"),
    ("CirChg", "CirChg", "PotCirChg"),
    ("Scr",    "Scr",    "PotScr"),
    ("Kncrv",  "Kncrv",  "PotKncrv"),
    ("Knbl",   "Knbl",   "PotKnbl"),
]
PERSONALITY_FIELDS = [("Int", "Int"), ("WrkEthic", "WrkEthic"), ("Greed", "Greed"), ("Loy", "Loy"), ("Lead", "Lead")]


def _fmt_val(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, str):
        s = v.strip()
        return s if s else "—"
    return str(v)


def _fmt_num(v: Any, prec: int = 2) -> str:
    try:
        return f"{float(v):.{prec}f}"
    except (TypeError, ValueError):
        return "—"


def _delta(new: Any, old: Any) -> str:
    try:
        d = float(new) - float(old)
    except (TypeError, ValueError):
        return ""
    if abs(d) < 0.005:
        return "  (·)"
    sign = "+" if d > 0 else ""
    return f"  ({sign}{d:.2f})"


def print_header(row: Dict[str, str], eval_row: Optional[Dict[str, str]]) -> None:
    print()
    print("=" * 72)
    print(f"  {row.get('Name','?')}  (ID {row.get('ID','?')})")
    kv = [
        ("Pos", row.get("Pos", "")),
        ("Age", row.get("Age", "")),
        ("Bats", row.get("Bats", "")),
        ("Throws", row.get("Throws", "")),
        ("Team", (eval_row or {}).get("Team", "")),
        ("Org",  (eval_row or {}).get("Org",  "")),
        ("Level",(eval_row or {}).get("League_Level", "")),
    ]
    print("  " + "  ".join(f"{k}: {_fmt_val(v)}" for k, v in kv))
    print("=" * 72)


def _print_field_block(title: str, fields: List[Tuple], row: Dict[str, str], with_pot: bool) -> None:
    """Print a block of ratings. `fields` is (label, col) or (label, col, pot_col)."""
    any_val = False
    for entry in fields:
        col = entry[1]
        v = row.get(col, "")
        if v not in (None, "",):
            any_val = True
            break
    if not any_val:
        return
    print(f"\n  [{title}]")
    if with_pot:
        print(f"    {'Rating':<10}{'Curr':>6}{'Pot':>7}")
        for label, cur_col, pot_col in (e if len(e) == 3 else (e[0], e[1], None) for e in fields):
            cur = row.get(cur_col, "")
            pot = row.get(pot_col, "") if pot_col else ""
            if (cur in (None, "")) and (pot in (None, "")):
                continue
            print(f"    {label:<10}{_fmt_val(cur):>6}{_fmt_val(pot):>7}")
    else:
        # two-column flat list
        pairs = []
        for entry in fields:
            label, col = entry[0], entry[1]
            val = row.get(col, "")
            if val not in (None, ""):
                pairs.append((label, val))
        for i in range(0, len(pairs), 2):
            chunk = pairs[i:i + 2]
            line = "    " + "   ".join(f"{lbl:<8} {_fmt_val(val):>3}" for lbl, val in chunk)
            print(line)


def print_ratings(row: Dict[str, str], is_pit: bool) -> None:
    if is_pit:
        _print_field_block("Pitcher Ability", PITCHER_ABILITY_FIELDS, row, with_pot=True)
        _print_field_block("Pitches", PITCH_FIELDS, row, with_pot=True)
        _print_field_block("Splits", PITCHER_SPLIT_FIELDS, row, with_pot=False)
    else:
        _print_field_block("Batting", HITTER_RATING_FIELDS, row, with_pot=True)
        _print_field_block("Defense", HITTER_DEFENSE_FIELDS, row, with_pot=False)
        _print_field_block("Positions", POS_RATING_COLS, row, with_pot=True)
        _print_field_block("Baserunning", HITTER_BASERUNNING_FIELDS, row, with_pot=False)
    _print_field_block("Personality", PERSONALITY_FIELDS, row, with_pot=False)


# Keys to compare in the scores table (hitter vs pitcher subsets show blanks fine).
# VOS_Reach/Career/Blended are the v6 names; VOS_Score/VOS_Potential are kept as
# legacy aliases (VOS_Score == VOS_Career, VOS_Potential == VOS_Reach) so
# downstream code that still reads the old names doesn't break.
SCORE_KEYS_HITTER = [
    "VOS_Reach", "VOS_Career", "VOS_Blended", "VOS_Ceiling", "Ceiling_Tier",
    "Arch_Career_WAR", "Arch_Career_WAR_Hi",
    "VOS_Score", "VOS_Potential",
    "Batting_Score", "Batting_Potential",
    "Defense_Score", "Baserunning_Score",
    "Development_Adj", "Age_Adj", "Personality_Adj",
    "Current_Position", "Projected_Position", "Ideal_Value",
]
SCORE_KEYS_PITCHER = [
    "VOS_Reach", "VOS_Career", "VOS_Blended",
    "VOS_Score", "VOS_Potential",
    "Pitching_Ability_Score", "Pitching_Ability_Potential",
    "Pitching_Arsenal_Score",
    "Development_Adj", "Age_Adj", "Personality_Adj",
    "Current_Position", "Projected_Position", "Ideal_Value",
]
# Extra rows shown only when draft_mode is active (values are blank on non-draft rows).
DRAFT_EXTRA_KEYS = ["Readiness_Adj", "Draft_Age_Adj", "Draft_RP_Penalty"]


def print_scores_table(
    base_eval: Optional[Dict[str, Any]],
    current_eval: Optional[Dict[str, Any]],
    modified_eval: Optional[Dict[str, Any]],
    is_pit: bool,
    draft_mode: bool = False,
) -> None:
    """Print side-by-side scores: Last Saved | Recomputed Base | Modified (+delta from recomputed base)."""
    keys = list(SCORE_KEYS_PITCHER if is_pit else SCORE_KEYS_HITTER)
    if draft_mode:
        # Insert Readiness + draft-specific adjustments right after Development_Adj
        dev_idx = keys.index("Development_Adj") + 1 if "Development_Adj" in keys else len(keys)
        for i, extra in enumerate(DRAFT_EXTRA_KEYS):
            keys.insert(dev_idx + i, extra)
    print()
    print(f"  {'Metric':<28}{'Last Saved':>12}{'Base':>12}{'Modified':>12}   Δ")
    print("  " + "-" * 70)
    for k in keys:
        saved = (base_eval or {}).get(k, "")
        base = (current_eval or {}).get(k, "")
        mod = (modified_eval or {}).get(k, "")
        is_numeric = not isinstance(base, str) and not isinstance(mod, str)
        # Some keys are strings (Current_Position / Projected_Position)
        if k in ("Current_Position", "Projected_Position"):
            line = f"  {k:<28}{_fmt_val(saved):>12}{_fmt_val(base):>12}{_fmt_val(mod):>12}"
        else:
            def fnum(x):
                try:
                    return f"{float(x):.2f}"
                except (TypeError, ValueError):
                    return "—"
            line = f"  {k:<28}{fnum(saved):>12}{fnum(base):>12}{fnum(mod):>12}{_delta(mod, base)}"
        print(line)


def _print_override_hint(is_pit: bool) -> None:
    """Print a compact list of common override-able columns for this player type."""
    if is_pit:
        print("\n  Override options (suffix any with Pot* for potential; _R / _L for splits):")
        print("    Ability:   Stf, Mov, Ctrl, HRA, PBABIP   |   Stamina/Other: Stm, Vel, Hold, GB")
        print("    Pitches:   Fst, Snk, Cutt, Crv, Sld, Chg, Splt, Frk, CirChg, Scr, Kncrv, Knbl")
        print("    Personality: Int, WrkEthic, Greed, Loy, Lead (use H/N/L — not numeric)")
    else:
        print("\n  Override options (prefix any batting tool with Pot* for potential):")
        print("    Batting:   Cntct, Gap, Pow, Eye, Ks, BABIP")
        print("    Defense:   IFR, IFE, IFA, TDP, OFR, OFE, OFA, CBlk, CArm, CFrm")
        print("    Positions: C, 1B, 2B, 3B, SS, LF, CF, RF  (+ Pot* variants)")
        print("    Baserun:   Speed, Run, StlRt, Steal")
        print("    Personality: Int, WrkEthic, Greed, Loy, Lead (use H/N/L — not numeric)")


# -----------------------------------------------------------------------------
# Override parsing
# -----------------------------------------------------------------------------

OVERRIDE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([-+]?\d+(?:\.\d+)?|[A-Za-z]+)\s*$")


def parse_overrides(text: str) -> List[Tuple[str, str]]:
    """Parse 'Col=Val, Col2=Val2' into list of (col, value_str). Raises ValueError on bad input."""
    parts = [p for p in re.split(r"[,;]", text) if p.strip()]
    out: List[Tuple[str, str]] = []
    for p in parts:
        m = OVERRIDE_RE.match(p)
        if not m:
            raise ValueError(f"bad override token: {p!r}  (expected Col=Number)")
        out.append((m.group(1), m.group(2)))
    return out


def apply_overrides(base_row: Dict[str, str], overrides: Dict[str, str]) -> Dict[str, str]:
    """Return a shallow copy of base_row with overrides applied. Warns on unknown columns."""
    new = dict(base_row)
    for col, val in overrides.items():
        if col not in base_row:
            print(f"  note: '{col}' is not a column in PlayerData — setting anyway.")
        new[col] = val
    return new


# -----------------------------------------------------------------------------
# REPL
# -----------------------------------------------------------------------------

HELP_TEXT = """
Commands:
  <Col>=<Val>[, <Col>=<Val>...]  Apply rating override(s). Example: PotCtrl=55, Pow=45
  show                            Reprint ratings + score comparison
  diff                            Show only the overridden columns and score deltas
  reset                           Clear all overrides, revert to baseline
  role SP|RP                      For pitchers: evaluate as SP or RP (default: auto)
  ratings                         Reprint the full ratings block for the current state
  help                            Show this help
  quit / exit / q                 Leave
"""


def run_repl(
    league: str,
    player_id: str,
    cfg: Dict[str, Any],
    league_lookup: Dict[int, str],
    teams: Dict[int, str],
    base_row: Dict[str, str],
    saved_eval_row: Optional[Dict[str, str]],
    draft_mode: bool = False,
) -> int:
    overrides: Dict[str, str] = {}
    role_override: Optional[str] = None
    is_pit = v2.is_pitcher(base_row)

    # initial recompute (no overrides) — proves consistency vs last saved
    base_eval = score_player(base_row, cfg, league_lookup, teams, role_override, draft_mode)

    print_header(base_row, saved_eval_row)
    mode_label = "DRAFT" if draft_mode else "standard"
    print(f"  Scoring mode: {mode_label}")
    print_ratings(base_row, is_pit)
    print_scores_table(saved_eval_row, base_eval, base_eval, is_pit, draft_mode)
    print("\n  Type overrides like `PotCtrl=55` or `help` for commands.")
    _print_override_hint(is_pit)
    print()

    while True:
        try:
            raw = input("what-if> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not raw:
            continue

        low = raw.lower()
        if low in ("quit", "exit", "q"):
            return 0
        if low == "help":
            print(HELP_TEXT)
            continue
        if low == "reset":
            overrides.clear()
            print("  Overrides cleared.")
            modified_row = base_row
        elif low == "ratings":
            cur_row = apply_overrides(base_row, overrides) if overrides else base_row
            print_ratings(cur_row, is_pit)
            continue
        elif low == "show":
            modified_row = apply_overrides(base_row, overrides) if overrides else base_row
        elif low == "diff":
            if not overrides:
                print("  No overrides applied.")
                continue
            print("  Overrides:")
            for k, v in overrides.items():
                print(f"    {k}: {base_row.get(k,'—')} -> {v}")
            modified_row = apply_overrides(base_row, overrides)
        elif low.startswith("role"):
            parts = raw.split()
            if len(parts) != 2 or parts[1].upper() not in ("SP", "RP"):
                print("  Usage: role SP|RP")
                continue
            role_override = parts[1].upper()
            print(f"  Role set to {role_override}")
            modified_row = apply_overrides(base_row, overrides) if overrides else base_row
        else:
            try:
                pairs = parse_overrides(raw)
            except ValueError as e:
                print(f"  Error: {e}")
                continue
            for col, val in pairs:
                overrides[col] = val
            modified_row = apply_overrides(base_row, overrides)

        modified_eval = score_player(modified_row, cfg, league_lookup, teams, role_override, draft_mode)
        # Recompute base with current role choice for consistent comparison
        base_eval = score_player(base_row, cfg, league_lookup, teams, role_override, draft_mode)
        print_scores_table(saved_eval_row, base_eval, modified_eval, is_pit, draft_mode)
        if overrides:
            print(f"  Active overrides: {', '.join(f'{k}={v}' for k, v in overrides.items())}")


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="Interactive single-player VOS what-if tool.")
    p.add_argument("--league", required=True, help="League slug (e.g. sahl, woba)")
    p.add_argument("--id", required=True, help="Player ID to load")
    p.add_argument("--eval-file", default=None, type=Path,
                   help="Specific evaluation_summary CSV (default: latest for the league)")
    p.add_argument("--config-dir", type=Path, default=v2.DEFAULT_CONFIG_DIR, help="Config directory")
    p.add_argument("--draft", dest="draft", action="store_true", default=None,
                   help="Force draft-mode scoring (readiness + dampened dev_adj + draft_age modifier)")
    p.add_argument("--no-draft", dest="draft", action="store_false",
                   help="Force non-draft scoring (disables auto-detect from eval file)")
    args = p.parse_args()

    league = args.league.strip()
    pid = str(args.id).strip()

    cfg = v2.load_weights(args.config_dir)
    if not cfg:
        print("ERROR: weights config missing/invalid", file=sys.stderr)
        return 1
    league_lookup = v2.load_id_maps(args.config_dir)
    teams = v2.load_teams(args.config_dir, league)

    row = load_player_row(league, pid)
    if row is None:
        print(f"ERROR: player ID {pid} not found in PlayerData-{league}.csv", file=sys.stderr)
        return 2

    eval_path = args.eval_file if args.eval_file else find_latest_eval_csv(league)
    saved_eval_row: Optional[Dict[str, str]] = None
    eval_has_draft_cols = False
    if eval_path and eval_path.exists():
        saved_eval_row = load_latest_eval_row(eval_path, pid)
        print(f"Loaded latest eval: {eval_path.name}")
        if saved_eval_row is None:
            print(f"  (player {pid} not present in that file)")
        else:
            # Detect if the eval was run in draft mode (readiness/draft age cols populated)
            eval_has_draft_cols = any(
                str(saved_eval_row.get(c, "")).strip() not in ("", "")
                for c in ("Readiness_Adj", "Draft_Age_Adj", "Draft_RP_Penalty")
            )
    else:
        print(f"No prior evaluation file found for league '{league}'.")

    # Resolve draft_mode: explicit flag wins; otherwise auto-detect from eval file.
    if args.draft is None:
        draft_mode = eval_has_draft_cols
        if draft_mode:
            print("  (draft-mode auto-detected from eval file — pass --no-draft to override)")
    else:
        draft_mode = bool(args.draft)

    return run_repl(league, pid, cfg, league_lookup, teams, row, saved_eval_row, draft_mode)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Player Card — Read-only profile view for a single player.

Loads one player from PlayerData-{league}.csv and prints a profile card:
  - Name, ID, declared position, bio (age/bats/throws/team/org/level)
  - VOS / Potential and component scores (from a fresh recompute)
  - Full ratings block (current + potential where applicable)
  - All positional scores (hitters: every position incl. DH)
                          (pitchers: scored as SP and as RP, side-by-side)

Usage:
    python player_card.py --league sahl --id 12345
    python player_card.py --league sahl --id "Chin-yau Sen"          # by name
    python player_card.py --league sahl --id 12345 --no-saved        # skip last-eval lookup
    python player_card.py --league sahl --id 12345 --compare 67890,11111      # side-by-side
    python player_card.py --league sahl --id "Sen" --compare "van Aert"       # names too
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
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from vosball.engine import (
    HITTER_POSITIONS, build_pitcher_row, classify_vos_tier, is_pitcher,
    resolve_int,
)
from vosball.data import (
    PLAYER_DATA_FILENAME_TEMPLATE, load_id_maps, load_teams, load_weights,
)

# Reuse what_if's display field definitions and helpers — single source of truth.
import what_if as wi

# AAVContext + dollar/year formatting live in free_agent_market — reuse so the
# VPC calibration math stays in one place (no duplication of the contract.py
# wiring between scripts).
import free_agent_market as fam
import depth_chart as dc

from org_depth_analysis import get_org_abbreviation


SCRIPT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_LEAGUE_SETTINGS = SCRIPT_DIR / "config" / "league_settings.json"


# -----------------------------------------------------------------------------
# Loading (mirrors what_if's helpers, kept local so this script stands alone)
# -----------------------------------------------------------------------------

def load_all_player_rows(league: str) -> Optional[List[Dict[str, str]]]:
    path = SCRIPT_DIR / "data" / PLAYER_DATA_FILENAME_TEMPLATE.format(league=league)
    if not path.exists():
        print(f"ERROR: {path} not found", file=sys.stderr)
        return None
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def resolve_player_row(
    rows: List[Dict[str, str]], token: str, league: str
) -> Optional[Dict[str, str]]:
    """Resolve a token to a single player row. A numeric token is matched on ID;
    anything else is matched on Name (case-insensitive: exact match first, then
    substring). On no match or an ambiguous name, prints a message to stderr and
    returns None so the caller can bail."""
    token = str(token).strip()
    if token.isdigit():
        for r in rows:
            if (r.get("ID") or "").strip() == token:
                return r
        print(f"ERROR: player ID {token} not found in PlayerData-{league}.csv", file=sys.stderr)
        return None

    tl = token.lower()
    exact = [r for r in rows if (r.get("Name") or "").strip().lower() == tl]
    matches = exact if exact else [r for r in rows if tl in (r.get("Name") or "").strip().lower()]
    if not matches:
        print(f"ERROR: no player matching '{token}' in PlayerData-{league}.csv", file=sys.stderr)
        return None
    if len(matches) > 1:
        print(f"ERROR: '{token}' matches {len(matches)} players — narrow the name or use an ID:",
              file=sys.stderr)
        for r in matches[:20]:
            print(f"    {(r.get('ID') or '').strip():>8}  {(r.get('Name') or '').strip():<28}"
                  f"{(r.get('Pos') or '').strip():<4} age {(r.get('Age') or '').strip()}",
                  file=sys.stderr)
        if len(matches) > 20:
            print(f"    ... and {len(matches) - 20} more", file=sys.stderr)
        return None
    return matches[0]


def league_org(league: str, settings_path: Path) -> Optional[str]:
    """Read the user's org name for `league` from league_settings.json. Returns
    None when the file/entry/org is absent — same source run_vos_all and
    trade_targets read, so --org-compare needs no extra config."""
    if not settings_path.exists():
        return None
    try:
        with settings_path.open(encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    entry = data.get(league) if isinstance(data, dict) else None
    if isinstance(entry, dict):
        org = entry.get("org")
        if isinstance(org, str) and org.strip():
            return org.strip()
    return None


# -----------------------------------------------------------------------------
# Display
# -----------------------------------------------------------------------------

ALL_HITTER_POSITIONS = HITTER_POSITIONS  # ["C","1B","2B","3B","SS","LF","CF","RF","DH"]


def _fnum(v: Any, prec: int = 2) -> str:
    try:
        return f"{float(v):.{prec}f}"
    except (TypeError, ValueError):
        return "—"


def print_summary_scores(
    eval_row: Dict[str, Any],
    saved_eval_row: Optional[Dict[str, str]],
    is_pit: bool,
    draft_mode: bool,
    org_saved_eval_row: Optional[Dict[str, str]] = None,
    org_abbrev: Optional[str] = None,
) -> None:
    """Top-of-card score summary (VOS, components, adjustments)."""
    keys = list(wi.SCORE_KEYS_PITCHER if is_pit else wi.SCORE_KEYS_HITTER)
    if draft_mode:
        dev_idx = keys.index("Development_Adj") + 1 if "Development_Adj" in keys else len(keys)
        for i, extra in enumerate(wi.DRAFT_EXTRA_KEYS):
            keys.insert(dev_idx + i, extra)

    has_saved = saved_eval_row is not None
    has_org_saved = org_saved_eval_row is not None
    org_label = f"({(org_abbrev or 'ORG').upper()} Saved)"
    print("\n  [Scores]")
    header = f"    {'Metric':<28}{'Recomputed':>12}"
    width = 28 + 12
    if has_saved:
        header += f"{'Last Saved':>14}"
        width += 14
    if has_org_saved:
        header += f"{org_label:>16}"
        width += 16
    print(header)
    print("    " + "-" * width)
    for k in keys:
        new = eval_row.get(k, "")
        saved = (saved_eval_row or {}).get(k, "")
        org_saved = (org_saved_eval_row or {}).get(k, "")
        if k in ("Current_Position", "Projected_Position"):
            new_s = wi._fmt_val(new)
            saved_s = wi._fmt_val(saved)
            org_s = wi._fmt_val(org_saved)
        else:
            new_s = _fnum(new)
            saved_s = _fnum(saved)
            org_s = _fnum(org_saved)
        line = f"    {k:<28}{new_s:>12}"
        if has_saved:
            line += f"{saved_s:>14}"
        if has_org_saved:
            org_disp = f"({org_s})" if org_s and org_s != "—" else ""
            line += f"{org_disp:>16}"
        print(line)


def _as_int(v: Any) -> str:
    try:
        return str(int(round(float(v))))
    except (TypeError, ValueError):
        return "-"


def _approx(v: Any) -> str:
    """Rough central estimate: '~15' (nearest whole)."""
    try:
        return f"~{float(v):.0f}"
    except (TypeError, ValueError):
        return "-"


def _to5plus(v: Any) -> str:
    """Generalized upside: nearest 5 with a '+', e.g. 42.1 -> '40+'. Falls back
    to '~N' for values too small to round to a meaningful 5-bucket."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "-"
    r = int(round(x / 5.0) * 5)
    return f"{r}+" if r >= 5 else _approx(x)


def print_proj_war(eval_row: Dict[str, Any]) -> None:
    """Archetype 'ballpark' career-WAR projection for hitters. Shows the
    AVERAGE career arc for a player of this profile (not a per-player forecast):
    average career WAR if he reaches MLB, the age-adjusted remaining WAR, and a
    projected debut age for prospects. Only printed when the weights file
    carries a war_archetype table (Arch_Career_WAR populated)."""
    arch = eval_row.get("Arch_Career_WAR", "")
    if arch == "" or arch is None:
        return
    hi = eval_row.get("Arch_Career_WAR_Hi", "")
    rem = eval_row.get("Remaining_WAR", "")
    rem_hi = eval_row.get("Remaining_WAR_Hi", "")
    debut = eval_row.get("Proj_Debut_Age", "")
    ceil = eval_row.get("VOS_Ceiling", "")
    age = eval_row.get("Age", "")
    lvl = str(eval_row.get("League_Level") or "").strip().upper()
    tier = str(eval_row.get("Ceiling_Tier") or "").strip()
    tier_str = f" - {tier}" if tier else ""
    print("\n  [Projected Career WAR - archetype]  (if he becomes a real MLBer, not a flameout)")
    print(f"    Ceiling {_fnum(ceil)}{tier_str}")
    print(f"    typical {_approx(arch)} WAR   |   top-tier {_to5plus(hi)} WAR")
    if lvl == "ML":
        print(f"    Remaining from age {_as_int(age)}:  typical {_approx(rem)} WAR   |   top-tier {_to5plus(rem_hi)} WAR")
    elif debut not in ("", None):
        print(f"    Projected MLB debut: age {_as_int(debut)}  (full career still ahead)")
    print("    typical = median, top-tier = top ~10% of this profile; NOT a per-player forecast")


def print_hitter_positional_scores(eval_row: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> None:
    """All-positions table for hitters: Current and Potential side-by-side, with VOS tier labels."""
    print("\n  [Positional Scores]")
    print(f"    {'Pos':<6}{'Current':>10}  {'Tier':<20}{'Potential':>12}  {'Tier':<20}")
    print("    " + "-" * 74)
    ideal_cur = (eval_row.get("Current_Position") or "").strip()
    ideal_pot = (eval_row.get("Projected_Position") or "").strip()
    for pos in ALL_HITTER_POSITIONS:
        cur = eval_row.get(f"{pos}_Score", "")
        pot = eval_row.get(f"{pos}_Potential", "")
        cur_tier = classify_vos_tier(cur, "hitter", cfg)
        pot_tier = classify_vos_tier(pot, "hitter", cfg)
        marker = ""
        if pos == ideal_cur and pos == ideal_pot:
            marker = "  <- current & projected"
        elif pos == ideal_cur:
            marker = "  <- current"
        elif pos == ideal_pot:
            marker = "  <- projected"
        print(
            f"    {pos:<6}{_fnum(cur):>10}  {cur_tier:<20}{_fnum(pot):>12}  {pot_tier:<20}{marker}"
        )


def print_pitcher_role_scores(
    row: Dict[str, str],
    cfg: Dict[str, Any],
    league_lookup: Dict[int, str],
    teams: Dict[int, str],
    draft_mode: bool,
) -> None:
    """Show both SP and RP evaluation for the pitcher."""
    sp_eval = build_pitcher_row(row, cfg, league_lookup, teams, role="SP", draft_mode=draft_mode)
    rp_eval = build_pitcher_row(row, cfg, league_lookup, teams, role="RP", draft_mode=draft_mode)
    rows_for_table = [
        ("VOS_Reach",                "VOS Reach",         True),
        ("VOS_Career",               "VOS Career",        True),
        ("VOS_Blended",              "VOS Blended",       True),
        ("VOS_Score",                "VOS (legacy=Career)",   True),
        ("VOS_Potential",            "VOS Pot (legacy=Reach)", True),
        ("Pitching_Ability_Score",   "Ability",           False),
        ("Pitching_Ability_Potential","Ability Potential",False),
        ("Pitching_Arsenal_Score",   "Arsenal",           False),
        ("Ideal_Value",              "Ideal Value",       False),
    ]
    print("\n  [Role Scores]")
    print(f"    {'Metric':<22}{'as SP':>10}  {'Tier':<20}{'as RP':>10}  {'Tier':<20}")
    print("    " + "-" * 86)
    for key, label, show_tier in rows_for_table:
        sp_v = (sp_eval or {}).get(key, "")
        rp_v = (rp_eval or {}).get(key, "")
        sp_tier = classify_vos_tier(sp_v, "pitcher", cfg) if show_tier else ""
        rp_tier = classify_vos_tier(rp_v, "pitcher", cfg) if show_tier else ""
        print(
            f"    {label:<22}{_fnum(sp_v):>10}  {sp_tier:<20}{_fnum(rp_v):>10}  {rp_tier:<20}"
        )


# -----------------------------------------------------------------------------
# Compare mode
# -----------------------------------------------------------------------------

COMPARE_COL_W = 13     # per-player column width
COMPARE_LABEL_W = 28   # left label column width (fits longest score key)
COMPARE_NAME_MAX = COMPARE_COL_W - 1  # leave a space


def _clip(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: max(1, n - 1)] + "…"


def _compare_row(label: str, cells: List[str]) -> str:
    body = "".join(f"{c:>{COMPARE_COL_W}}" for c in cells)
    return f"    {label:<{COMPARE_LABEL_W}}{body}"


def _highlight_winners(cells: List[str]) -> List[str]:
    """Wrap the max numeric cell(s) in [brackets] to flag the 'winner' per row.

    Non-numeric cells (e.g. "—", position strings) are left untouched. Ties are
    all bracketed. If fewer than 2 numeric values are present, nothing is changed.
    """
    parsed: List[Optional[float]] = []
    for c in cells:
        try:
            parsed.append(float((c or "").strip()))
        except (TypeError, ValueError):
            parsed.append(None)
    nums = [v for v in parsed if v is not None]
    if len(nums) < 2:
        return cells
    top = max(nums)
    # Guard against all-equal rows being "highlighted" — meaningless visual noise.
    if all(v == top for v in nums):
        return cells
    return [f"[{(c or '').strip()}]" if v == top else c for c, v in zip(cells, parsed)]


def print_compare_header(players: List[Dict[str, Any]]) -> None:
    """players: list of {row, eval_row, saved_eval_row, is_pit}."""
    print()
    print("=" * (4 + COMPARE_LABEL_W + COMPARE_COL_W * len(players)))
    print(_compare_row("", [_clip(p["row"].get("Name", "?"), COMPARE_NAME_MAX) for p in players]))
    print(_compare_row("ID", [str(p["row"].get("ID", "")) for p in players]))
    print(_compare_row("Pos", [(p["row"].get("Pos") or "").strip() for p in players]))
    print(_compare_row("Age", [str(p["row"].get("Age", "")) for p in players]))
    print(_compare_row("Bats/Throws", [
        f"{(p['row'].get('Bats') or '').strip()}/{(p['row'].get('Throws') or '').strip()}" for p in players
    ]))
    print(_compare_row("Team", [_clip((p["eval_row"] or {}).get("Team", ""), COMPARE_NAME_MAX) for p in players]))
    print(_compare_row("Org", [_clip((p["eval_row"] or {}).get("Org", ""), COMPARE_NAME_MAX) for p in players]))
    print(_compare_row("Level", [str((p["eval_row"] or {}).get("League_Level", "")) for p in players]))
    print("=" * (4 + COMPARE_LABEL_W + COMPARE_COL_W * len(players)))


def print_compare_scores(players: List[Dict[str, Any]], all_pit: bool, draft_mode: bool) -> None:
    keys = list(wi.SCORE_KEYS_PITCHER if all_pit else wi.SCORE_KEYS_HITTER)
    if draft_mode:
        dev_idx = keys.index("Development_Adj") + 1 if "Development_Adj" in keys else len(keys)
        for i, extra in enumerate(wi.DRAFT_EXTRA_KEYS):
            keys.insert(dev_idx + i, extra)
    print("\n  [Scores]")
    for k in keys:
        cells = []
        for p in players:
            v = (p["eval_row"] or {}).get(k, "")
            cells.append(wi._fmt_val(v) if k in ("Current_Position", "Projected_Position") else _fnum(v))
        print(_compare_row(k, _highlight_winners(cells)))


def print_compare_positional_scores(players: List[Dict[str, Any]]) -> None:
    """Hitters only — all-positions table, players as columns."""
    print("\n  [Positional Scores — Current]")
    for pos in ALL_HITTER_POSITIONS:
        cells = [_fnum((p["eval_row"] or {}).get(f"{pos}_Score", "")) for p in players]
        print(_compare_row(pos, _highlight_winners(cells)))
    print("\n  [Positional Scores — Potential]")
    for pos in ALL_HITTER_POSITIONS:
        cells = [_fnum((p["eval_row"] or {}).get(f"{pos}_Potential", "")) for p in players]
        print(_compare_row(pos, _highlight_winners(cells)))


def print_compare_pitcher_role_scores(
    players: List[Dict[str, Any]],
    cfg: Dict[str, Any],
    league_lookup: Dict[int, str],
    teams: Dict[int, str],
    draft_mode: bool,
) -> None:
    """Pitchers only — SP/RP eval per player."""
    sp_evals = [build_pitcher_row(p["row"], cfg, league_lookup, teams, role="SP", draft_mode=draft_mode) for p in players]
    rp_evals = [build_pitcher_row(p["row"], cfg, league_lookup, teams, role="RP", draft_mode=draft_mode) for p in players]
    rows = [
        ("VOS_Reach",                "VOS Reach"),
        ("VOS_Career",               "VOS Career"),
        ("VOS_Blended",              "VOS Blended"),
        ("VOS_Score",                "VOS (legacy)"),
        ("VOS_Potential",            "VOS Pot (legacy)"),
        ("Pitching_Ability_Score",   "Ability"),
        ("Pitching_Ability_Potential","Ability Pot."),
        ("Pitching_Arsenal_Score",   "Arsenal"),
        ("Ideal_Value",              "Ideal Value"),
    ]
    print("\n  [Role Scores — as SP]")
    for key, label in rows:
        print(_compare_row(label, _highlight_winners([_fnum((e or {}).get(key, "")) for e in sp_evals])))
    print("\n  [Role Scores — as RP]")
    for key, label in rows:
        print(_compare_row(label, _highlight_winners([_fnum((e or {}).get(key, "")) for e in rp_evals])))


def print_compare_ratings(players: List[Dict[str, Any]], all_pit: bool) -> None:
    """Side-by-side ratings using what_if's field definitions."""
    def block(title: str, fields: List[Tuple], with_pot: bool) -> None:
        # Filter to rows where at least one player has a value
        rows_out: List[Tuple[str, List[str]]] = []
        for entry in fields:
            if with_pot:
                label, cur_col, pot_col = (entry if len(entry) == 3 else (entry[0], entry[1], None))
                cur_cells = [wi._fmt_val(p["row"].get(cur_col, "")) for p in players]
                pot_cells = [wi._fmt_val(p["row"].get(pot_col, "") if pot_col else "") for p in players]
                if any(c != "—" for c in cur_cells):
                    rows_out.append((label, cur_cells))
                if pot_col and any(c != "—" for c in pot_cells):
                    rows_out.append((f"{label} (Pot)", pot_cells))
            else:
                label, col = entry[0], entry[1]
                cells = [wi._fmt_val(p["row"].get(col, "")) for p in players]
                if any(c != "—" for c in cells):
                    rows_out.append((label, cells))
        if not rows_out:
            return
        print(f"\n  [{title}]")
        for label, cells in rows_out:
            print(_compare_row(label, _highlight_winners(cells)))

    if all_pit:
        block("Pitcher Ability", wi.PITCHER_ABILITY_FIELDS, with_pot=True)
        block("Pitches", wi.PITCH_FIELDS, with_pot=True)
        block("Splits", wi.PITCHER_SPLIT_FIELDS, with_pot=False)
    else:
        block("Batting", wi.HITTER_RATING_FIELDS, with_pot=True)
        block("Defense", wi.HITTER_DEFENSE_FIELDS, with_pot=False)
        block("Position Ratings", wi.POS_RATING_COLS, with_pot=True)
        block("Baserunning", wi.HITTER_BASERUNNING_FIELDS, with_pot=False)
    block("Personality", wi.PERSONALITY_FIELDS, with_pot=False)


def run_compare(
    league: str,
    ids: List[str],
    cfg: Dict[str, Any],
    league_lookup: Dict[int, str],
    teams: Dict[int, str],
    eval_path: Optional[Path],
    skip_saved: bool,
    draft_arg: Optional[bool],
    role_override: Optional[str],
) -> int:
    players: List[Dict[str, Any]] = []
    eval_has_draft_cols = False
    all_rows = load_all_player_rows(league)
    if all_rows is None:
        return 2
    for token in ids:
        row = resolve_player_row(all_rows, token, league)
        if row is None:
            return 2
        rid = (row.get("ID") or "").strip()
        saved = None
        if not skip_saved and eval_path and eval_path.exists():
            saved = wi.load_latest_eval_row(eval_path, rid)
            if saved is not None:
                eval_has_draft_cols = eval_has_draft_cols or any(
                    str(saved.get(c, "")).strip() != ""
                    for c in ("Readiness_Adj", "Draft_Age_Adj", "Draft_RP_Penalty")
                )
        players.append({"row": row, "saved_eval_row": saved, "is_pit": is_pitcher(row)})

    draft_mode = bool(draft_arg) if draft_arg is not None else eval_has_draft_cols

    for p in players:
        p["eval_row"] = wi.score_player(p["row"], cfg, league_lookup, teams, role_override, draft_mode)
        if p["eval_row"] is None:
            print(f"ERROR: failed to score player ID {p['row'].get('ID')}.", file=sys.stderr)
            return 3

    pit_flags = [p["is_pit"] for p in players]
    all_pit = all(pit_flags)
    all_hit = not any(pit_flags)
    mixed = not (all_pit or all_hit)

    print_compare_header(players)
    print(f"  Scoring mode: {'DRAFT' if draft_mode else 'standard'}"
          + (f"  |  role: {role_override}" if (all_pit and role_override) else ""))

    print_compare_scores(players, all_pit, draft_mode)

    if mixed:
        print("\n  (mixed hitters/pitchers — skipping positional & role-score blocks)")
    elif all_hit:
        print_compare_positional_scores(players)
    else:
        print_compare_pitcher_role_scores(players, cfg, league_lookup, teams, draft_mode)

    if mixed:
        print("\n  (mixed hitters/pitchers — skipping ratings block)")
    else:
        print_compare_ratings(players, all_pit)

    print()
    return 0


# -----------------------------------------------------------------------------
# Contract details + fair value (VPC)
# -----------------------------------------------------------------------------

def _ceval_float(row: Dict[str, str], col: str) -> float:
    try:
        return float((row.get(col) or "").strip())
    except (TypeError, ValueError):
        return 0.0


def _ceval_int(row: Dict[str, str], col: str) -> int:
    return int(_ceval_float(row, col))


def _contract_present(row: Dict[str, str], prefix: str) -> bool:
    """A real contract row has years >= 1 AND a non-empty player_id (the eval
    CSV writes zeros for every field on players without a contract/extension)."""
    if not row:
        return False
    if not (row.get(f"{prefix}_player_id") or "").strip():
        return False
    return _ceval_int(row, f"{prefix}_years") >= 1


def _print_contract_block(row: Dict[str, str], prefix: str, label: str) -> None:
    """Render one contract or extension block from the eval-CSV passthrough."""
    years = _ceval_int(row, f"{prefix}_years")
    cur_yr = _ceval_int(row, f"{prefix}_current_year")
    signed = _ceval_int(row, f"{prefix}_season_year")
    is_major = _ceval_int(row, f"{prefix}_is_major") == 1
    tier = "major league" if is_major else "minor league"

    salaries: List[int] = []
    for i in range(min(years, 15)):  # CSV exposes salary0..salary14
        salaries.append(int(_ceval_float(row, f"{prefix}_salary{i}")))
    total = sum(salaries)
    aav = total // years if years > 0 else 0

    print(f"\n  [{label}]")
    yr_descr = f"Year {cur_yr} of {years}" if cur_yr else f"{years} yr(s)"
    signed_descr = f" (signed {signed})" if signed else ""
    print(f"    Status:     Active {tier} deal — {yr_descr}{signed_descr}")
    if salaries:
        per_yr = " / ".join(fam._fmt_aav(s) for s in salaries)
        print(f"    Salaries:   {per_yr}")
        print(f"    Total/AAV:  {fam._fmt_aav(total)} total  |  {fam._fmt_aav(aav)} AAV")

    no_trade = _ceval_int(row, f"{prefix}_no_trade") == 1
    print(f"    No-trade:   {'Yes' if no_trade else 'No'}")

    # Options on the final year and the year before final. Show only the flags
    # that are set so a vanilla deal renders as 'Options: none'.
    opt_bits: List[str] = []
    opt_specs = [
        ("last_year", "last yr"),
        ("next_last_year", "next-to-last yr"),
    ]
    flag_kinds = [
        ("team_option", "team option"),
        ("player_option", "player option"),
        ("vesting_option", "vesting option"),
    ]
    for opt_prefix, opt_label in opt_specs:
        for flag_col, flag_label in flag_kinds:
            if _ceval_int(row, f"{prefix}_{opt_prefix}_{flag_col}") == 1:
                opt_bits.append(f"{opt_label} {flag_label}")
        buyout = int(_ceval_float(row, f"{prefix}_{opt_prefix}_option_buyout"))
        if buyout > 0:
            opt_bits.append(f"{opt_label} buyout {fam._fmt_aav(buyout)}")
    print(f"    Options:    {', '.join(opt_bits) if opt_bits else 'none'}")

    # Incentives: thresholds + bonus pairs, plus the standalone award bonuses.
    inc_bits: List[str] = []
    min_pa = _ceval_int(row, f"{prefix}_minimum_pa")
    min_pa_bonus = int(_ceval_float(row, f"{prefix}_minimum_pa_bonus"))
    if min_pa > 0 or min_pa_bonus > 0:
        inc_bits.append(f"{min_pa} PA → {fam._fmt_aav(min_pa_bonus)}")
    min_ip = _ceval_int(row, f"{prefix}_minimum_ip")
    min_ip_bonus = int(_ceval_float(row, f"{prefix}_minimum_ip_bonus"))
    if min_ip > 0 or min_ip_bonus > 0:
        inc_bits.append(f"{min_ip} IP → {fam._fmt_aav(min_ip_bonus)}")
    for col, lbl in (("mvp_bonus", "MVP"), ("cyyoung_bonus", "Cy Young"), ("allstar_bonus", "All-Star")):
        amt = int(_ceval_float(row, f"{prefix}_{col}"))
        if amt > 0:
            inc_bits.append(f"{lbl} {fam._fmt_aav(amt)}")
    print(f"    Incentives: {', '.join(inc_bits) if inc_bits else 'none'}")


def print_contract_details(saved_eval_row: Optional[Dict[str, str]]) -> Tuple[Optional[int], Optional[int]]:
    """Print Contract + ContractExtension blocks. Returns (current AAV, total)
    of the active contract so the fair-value block can compare against it."""
    if not saved_eval_row:
        print("\n  [Contract]")
        print("    (no saved eval row — re-run run_vos.py to pull contract data)")
        return None, None

    if not _contract_present(saved_eval_row, "Contract"):
        print("\n  [Contract]")
        print("    No active contract (free agent / unsigned)")
        cur_aav: Optional[int] = None
        cur_total: Optional[int] = None
    else:
        _print_contract_block(saved_eval_row, "Contract", "Contract")
        years = _ceval_int(saved_eval_row, "Contract_years")
        cur_total = sum(int(_ceval_float(saved_eval_row, f"Contract_salary{i}")) for i in range(min(years, 15)))
        cur_aav = cur_total // years if years > 0 else None

    if _contract_present(saved_eval_row, "ContractExtension"):
        _print_contract_block(saved_eval_row, "ContractExtension", "Extension (signed)")

    return cur_aav, cur_total


def _load_all_eval_rows(eval_path: Path) -> List[Dict[str, str]]:
    """Slurp the league eval CSV — needed for VPC calibration via AAVContext."""
    rows: List[Dict[str, str]] = []
    with eval_path.open("r", encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    return rows


def find_org_position_leader(
    eval_path: Optional[Path],
    my_org: str,
    position: str,
    exclude_id: str,
    level: str = "ML",
) -> Optional[Dict[str, str]]:
    """Return the eval row of the highest-VOS player in `my_org` at `position`
    and `level` — i.e. your incumbent/best man at that spot. Org is matched by
    abbreviation so `my_org` may be a full name ('Houston Astros') or a code
    ('HOU'). Returns None when there's no eval CSV or no qualifying player."""
    if eval_path is None or not eval_path.exists():
        return None
    want_org = get_org_abbreviation(my_org).upper()
    want_pos = position.strip().upper()
    want_lvl = level.strip().upper()
    best: Optional[Dict[str, str]] = None
    best_vos = float("-inf")
    for r in _load_all_eval_rows(eval_path):
        org = (r.get("Org") or "").strip()
        if not org or get_org_abbreviation(org).upper() != want_org:
            continue
        if (r.get("League_Level") or "").strip().upper() != want_lvl:
            continue
        if (r.get("Current_Position") or "").strip().upper() != want_pos:
            continue
        if (r.get("ID") or "").strip() == str(exclude_id).strip():
            continue
        try:
            vos = float((r.get("VOS_Score") or "").strip())
        except (TypeError, ValueError):
            continue
        if vos > best_vos:
            best_vos = vos
            best = r
    return best


def _resolve_org_code(my_org: str, league: str) -> str:
    """Map an org name/abbreviation to the lowercase team code used in depth-chart
    filenames ({league}/depth/{code}_{level}_*.csv). Resolved the same way
    depth_chart.py names its files, so the codes line up. Falls back to the
    generic abbreviation when park-factors carries no entry."""
    name_to_code = dc._name_to_code_map(dc._default_park_factors_path(league))
    if my_org in name_to_code:
        return name_to_code[my_org]
    low = my_org.strip().lower()
    for name, code in name_to_code.items():
        if name.lower() == low:
            return code
    if low in set(name_to_code.values()):  # already a team code
        return low
    return get_org_abbreviation(my_org).lower()


def _find_latest_depth_csv(league: str, org_code: str, level: str) -> Optional[Path]:
    depth_dir = SCRIPT_DIR / league / "depth"
    if not depth_dir.exists():
        return None
    # Timestamps are YYYYMMDD_HHMMSS, so lexicographic sort == chronological.
    matches = sorted(depth_dir.glob(f"{org_code}_{level}_*.csv"))
    return matches[-1] if matches else None


def _parse_depth_tier(tier: str) -> Tuple[Optional[str], Optional[int]]:
    """Split a depth-chart tier label into (position, slot rank). Handles both
    hitter form 'CF-1' / '1B-2' and pitcher form 'SP1' / 'RP3'."""
    t = (tier or "").strip().upper()
    if not t:
        return None, None
    if "-" in t:
        pos, _, rank = t.rpartition("-")
    else:
        i = len(t)
        while i > 0 and t[i - 1].isdigit():
            i -= 1
        pos, rank = t[:i], t[i:]
    try:
        rank_i = int(rank)
    except (TypeError, ValueError):
        rank_i = None
    return (pos.strip() or None), rank_i


def find_org_depth_starter(
    league: str,
    my_org: str,
    position: str,
    exclude_id: str,
    level: str = "ML",
) -> Tuple[Optional[Dict[str, str]], Optional[Dict[str, str]], Optional[Path]]:
    """Return (starter row at `position`, the target's own depth row if present,
    depth CSV path). The 'starter' is the lowest slot rank (tie-broken by
    composite) once the target is excluded. The target's own row is returned too
    so the caller can compare composites. The path is returned even when no
    player qualifies so the caller can distinguish 'no depth chart on disk'
    (path None) from 'depth chart has nobody at this position' (path set)."""
    csv_path = _find_latest_depth_csv(league, _resolve_org_code(my_org, league), level)
    if csv_path is None:
        return None, None, None
    want = position.strip().upper()
    exclude = str(exclude_id).strip()
    self_row: Optional[Dict[str, str]] = None
    candidates: List[Tuple[float, float, Dict[str, str]]] = []
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            if (r.get("pid") or "").strip() == exclude:
                self_row = r  # captured regardless of which slot he occupies
                continue
            pos, rank = _parse_depth_tier(r.get("tier", ""))
            if pos != want:
                continue
            try:
                comp = float((r.get("composite") or "").strip())
            except (TypeError, ValueError):
                comp = float("-inf")
            # rank None sorts last; within a rank, higher composite wins.
            rank_key = float(rank) if rank is not None else float("inf")
            candidates.append((rank_key, comp, r))
    if not candidates:
        return None, self_row, csv_path
    candidates.sort(key=lambda t: (t[0], -t[1]))
    return candidates[0][2], self_row, csv_path


def _note_slots_ahead(
    row: Dict[str, str],
    starter_row: Dict[str, str],
    viewed_depth_row: Optional[Dict[str, str]],
    position: str,
    score_fn: Any,
    saved_eval_row: Optional[Dict[str, str]],
    league: str,
    level: str,
    eval_path: Optional[Path],
    use_composite: bool,
) -> None:
    """Print a one-line note when the viewed player would outrank the slotted
    starter. Three accuracy tiers:
      1. Viewed player already in the depth chart  -> exact composites, both
         read straight from the depth CSV.
      2. Outside player + --org-compare-composite  -> compute his real composite
         via depth_chart.CompositeContext (and re-score the starter the same way
         for an apples-to-apples compare).
      3. Outside player, default                   -> quick VOS estimate against
         the starter's composite, flagged as ratings-only.
    Prints nothing unless the player comes out ahead."""
    def _f(x: Any) -> Optional[float]:
        try:
            return float(str(x).strip())
        except (TypeError, ValueError):
            return None

    name = (row.get("Name") or "").strip()
    starter_name = (starter_row.get("name") or "").strip()
    cs_comp = _f(starter_row.get("composite"))

    # Tier 1 — viewed player is in this depth chart: exact composites.
    if viewed_depth_row is not None and cs_comp is not None:
        vt_comp = _f(viewed_depth_row.get("composite"))
        if vt_comp is not None and vt_comp > cs_comp:
            print(f"  Note: {name} ranks ahead of {starter_name} at {position} on the "
                  f"depth chart (composite {vt_comp:.1f} vs {cs_comp:.1f}).")
        return

    # Tier 2 — outside player, exact composite requested.
    if use_composite and saved_eval_row is not None:
        try:
            ctx = dc.CompositeContext(league, level)
        except Exception as exc:  # noqa: BLE001 — best-effort; degrade to Tier 3.
            print(f"  (--org-compare-composite: composite unavailable ({exc}); "
                  f"using VOS estimate)")
        else:
            vrec = ctx.score(saved_eval_row)
            vt = _f(vrec.get("composite"))
            spid = (starter_row.get("pid") or "").strip()
            srow = (wi.load_latest_eval_row(eval_path, spid)
                    if (eval_path and eval_path.exists() and spid) else None)
            cs = _f(ctx.score(srow).get("composite")) if srow else cs_comp
            if vt is not None and cs is not None:
                if vt > cs:
                    degraded = not _f(vrec.get("sample_weight"))
                    extra = (" — stats didn't join, so composites fell back to ratings"
                             if degraded else "")
                    print(f"  Note: {name} would slot ahead of {starter_name} at {position} — "
                          f"depth composite {vt:.1f} vs {cs:.1f}{extra}.")
                return

    # Tier 3 — quick VOS estimate. The depth chart slots by composite, so use the
    # starter's composite (not his VOS) as the bar: only flag when the target's
    # raw VOS already clears the starter's full composite. Conservative on
    # purpose — avoids a false "slots ahead" when the starter's in-season bat has
    # lifted his composite well above his rating.
    bar = cs_comp if cs_comp is not None else _f(starter_row.get("vos"))
    vt_vos = _f((saved_eval_row or {}).get("VOS_Score"))
    if vt_vos is None:
        vt_vos = _f((score_fn() or {}).get("VOS_Score"))
    if bar is not None and vt_vos is not None and vt_vos > bar:
        print(f"  Note: {name} would likely slot ahead of {starter_name} at {position} — "
              f"his VOS {vt_vos:.1f} clears the starter's depth-chart composite {bar:.1f} "
              f"(his own in-season stats aren't factored in here).")


def _build_aav_context(
    eval_path: Optional[Path],
    league: str,
    contract_config: Path,
    aav_years: int,
    diagnostic: bool = False,
) -> Optional["fam.AAVContext"]:
    """Construct AAVContext or return None with a printed reason. Failures are
    soft — the rest of the card still renders."""
    if eval_path is None or not eval_path.exists():
        print("  [Fair Value (VPC)] skipped: no eval CSV found for VPC calibration.")
        return None
    if not contract_config.exists():
        print(f"  [Fair Value (VPC)] skipped: contract config missing at {contract_config}.")
        return None
    try:
        eval_rows = _load_all_eval_rows(eval_path)
        cache_dir = SCRIPT_DIR / league / "cache" / "stats"
        return fam.AAVContext(
            eval_rows=eval_rows,
            contract_config_path=contract_config,
            league=league,
            league_url_config=dc.DEFAULT_LEAGUE_URL,
            base_url=None,
            cache_dir=cache_dir if cache_dir.exists() else None,
            aav_years=aav_years,
            diagnostic=diagnostic,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  [Fair Value (VPC)] skipped: AAVContext build failed ({exc}).")
        return None


def _make_player_rec(
    row: Dict[str, str],
    eval_row: Dict[str, Any],
    is_pit: bool,
    role_override: Optional[str],
) -> Dict[str, Any]:
    """Shape the dict that AAVContext.compute / suggest_contract expect.

    Mirrors the keys free_agent_market sets on FA records — keeps the contract
    pricing path identical to what the FA tool uses.
    """
    proj_role = ""
    if is_pit:
        proj_role = (role_override or (eval_row.get("Projected_Position") or "")).strip()
    return {
        "pid": str(row.get("ID", "")),
        "name": str(row.get("Name", "")),
        "age": row.get("Age", ""),
        "is_pitcher": is_pit,
        "primary_pos": (eval_row.get("Current_Position") or row.get("Pos") or "").strip(),
        "proj_role": proj_role,
        "vos": eval_row.get("VOS_Score", 0.0),
        "vos_potential": eval_row.get("VOS_Potential", 0.0),
    }


def print_fair_value(
    aav_ctx: Optional["fam.AAVContext"],
    rec: Dict[str, Any],
    current_aav: Optional[int],
    suggest_length: bool,
    suggest_max_years: int,
    suggest_floor_ratio: float,
) -> None:
    """Fair-AAV block + optional suggested-length deal, with vs-current delta."""
    print("\n  [Fair Value (VPC)]")
    if aav_ctx is None:
        print("    (skipped — see note above)")
        return

    fair_aav = aav_ctx.compute(rec)
    term = aav_ctx.aav_years
    if fair_aav is None or fair_aav <= 0:
        print(f"    Fair AAV ({term}-yr term): —  (could not compute)")
    else:
        line = f"    Fair AAV ({term}-yr term): {fam._fmt_aav(fair_aav)}"
        if current_aav and current_aav > 0:
            delta = fair_aav - current_aav
            sign = "+" if delta >= 0 else "-"
            line += (
                f"   |  Current AAV: {fam._fmt_aav(current_aav)}"
                f"  ->  surplus {sign}{fam._fmt_aav(abs(delta))}/yr"
            )
        print(line)

    if suggest_length:
        yrs, aav, total = aav_ctx.suggest_contract(
            rec, max_years=suggest_max_years, floor_ratio=suggest_floor_ratio,
        )
        if yrs and aav and total:
            print(
                f"    Suggested deal: {yrs} yr × {fam._fmt_aav(aav)}"
                f"  =  {fam._fmt_aav(total)} total"
            )
        else:
            print("    Suggested deal: —  (decline cutoff couldn't price a deal)")


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="View a player profile card.")
    p.add_argument("--league", required=True, help="League slug (e.g. sahl, woba)")
    p.add_argument("--id", required=True,
                   help="Player to view — a numeric ID or a player name "
                        "(name match is case-insensitive; ambiguous names list candidates)")
    p.add_argument("--eval-file", default=None, type=Path,
                   help="Specific evaluation_summary CSV (default: latest for the league)")
    p.add_argument("--no-saved", action="store_true",
                   help="Skip loading last-saved eval row (recompute only)")
    p.add_argument("--config-dir", type=Path, default=SCRIPT_DIR / "config", help="Config directory")
    p.add_argument("--draft", dest="draft", action="store_true", default=None,
                   help="Force draft-mode scoring")
    p.add_argument("--no-draft", dest="draft", action="store_false",
                   help="Force non-draft scoring")
    p.add_argument("--role", choices=["SP", "RP"], default=None,
                   help="For pitchers: force the role used in the [Scores] block (default: auto from Pos)")
    p.add_argument("--compare", default=None,
                   help="Additional player ID(s) or name(s) for side-by-side comparison "
                        "(comma-separated). The --id player is column 1; --compare players "
                        "follow in order.")
    p.add_argument("--my-org", dest="my_org", default=None,
                   help="Your own org name or abbreviation (e.g. 'STL' or 'St. Louis Cardinals'). "
                        "When set, the player's own org eval is shown only if it differs from yours. "
                        "Also names the org used by --org-compare (else read from league_settings.json).")
    p.add_argument("--no-org-saved", action="store_true",
                   help="Skip showing the player's own-org saved eval column.")
    p.add_argument("--org-compare", action="store_true",
                   help="Compare the player side-by-side against the starter slotted at the same "
                        "position in your org's depth chart ({league}/depth/). Falls back to the "
                        "top-VOS player in the eval CSV if no depth chart exists. The org is "
                        "--my-org, or (if unset) the 'org' configured for this league in "
                        "league_settings.json. Combines with --compare.")
    p.add_argument("--org-compare-level", default="ML",
                   help="Depth-chart level to pull your comparison starter from (default ML — your "
                        "big-league starter). E.g. AAA to compare against your top farmhand.")
    p.add_argument("--org-compare-composite", action="store_true",
                   help="For the 'would slot ahead' note on an outside player, compute his real "
                        "depth-chart composite (VOS blended with in-season stats) via "
                        "depth_chart.CompositeContext instead of the quick VOS estimate. Triggers "
                        "a one-time stat fetch for the level (cached per day).")
    p.add_argument("--league-settings", type=Path, default=DEFAULT_LEAGUE_SETTINGS,
                   help="Path to league_settings.json (used to resolve your org for --org-compare).")
    # Contract + fair-value block (parallels free_agent_market.py flags).
    p.add_argument("--no-contract", action="store_true",
                   help="Skip the [Contract] / [Fair Value (VPC)] blocks entirely.")
    p.add_argument("--no-aav", action="store_true",
                   help="Skip the VPC fair-value block but still show contract details.")
    p.add_argument("--aav-years", type=int, default=3,
                   help="Fixed term used for the Fair AAV column (default 3). "
                        "Matches free_agent_market.py's default.")
    p.add_argument("--contract-config", type=Path, default=fam.DEFAULT_CONTRACT_CONFIG,
                   help="Path to contract_config.json (default config/contract_config.json).")
    p.add_argument("--no-suggest-length", action="store_true",
                   help="Suppress the suggested length / AAV / total row.")
    p.add_argument("--suggest-max-years", type=int, default=7,
                   help="Upper bound for the suggested-length heuristic. Default 7.")
    p.add_argument("--suggest-floor-ratio", type=float, default=0.85,
                   help="Decline-cutoff floor for suggested length. Default 0.85.")
    args = p.parse_args()

    league = args.league.strip()
    pid = str(args.id).strip()

    cfg = load_weights(args.config_dir)
    if not cfg:
        print("ERROR: weights config missing/invalid", file=sys.stderr)
        return 1
    league_lookup = load_id_maps(args.config_dir)
    teams = load_teams(args.config_dir, league)

    eval_path = args.eval_file if args.eval_file else wi.find_latest_eval_csv(league)
    if eval_path and eval_path.exists() and not args.no_saved:
        print(f"Loaded latest eval: {eval_path.name}")

    all_rows = load_all_player_rows(league)
    if all_rows is None:
        return 2
    row = resolve_player_row(all_rows, pid, league)
    if row is None:
        return 2
    # If --id was a name, switch to the resolved ID for all downstream eval lookups.
    pid = (row.get("ID") or "").strip()

    # ---- Compare mode (explicit --compare and/or --org-compare) ----
    compare_ids: List[str] = []
    if args.compare:
        compare_ids += [s.strip() for s in args.compare.split(",") if s.strip()]

    if args.org_compare:
        my_org = args.my_org or league_org(league, args.league_settings)
        if not my_org:
            print(f"ERROR: --org-compare needs an org — pass --my-org, or add an 'org' entry "
                  f"for '{league}' to {args.league_settings.name}.", file=sys.stderr)
            return 2
        # Position to match on: prefer the saved eval (same source as the candidate
        # pool), fall back to a fresh score, then the raw declared Pos.
        tpos = ""
        tsaved = (wi.load_latest_eval_row(eval_path, pid)
                  if (eval_path and eval_path.exists()) else None)
        if tsaved:
            tpos = (tsaved.get("Current_Position") or "").strip()
        if not tpos:
            tscored = wi.score_player(row, cfg, league_lookup, teams, args.role, bool(args.draft))
            tpos = ((tscored or {}).get("Current_Position") or "").strip()
        if not tpos:
            tpos = (row.get("Pos") or "").strip()
        if not tpos:
            print("ERROR: --org-compare could not determine the player's position.", file=sys.stderr)
            return 2
        lvl = args.org_compare_level.upper()
        # Primary source: the depth chart's slotted starter at this position.
        # Falls back to the eval CSV's top-VOS player when no depth chart is on
        # disk for this org/level (not every league runs depth charts).
        depth_row, viewed_depth_row, depth_csv = find_org_depth_starter(
            league, my_org, tpos, pid, lvl)
        if depth_row is not None:
            lid = (depth_row.get("pid") or "").strip()
            print(f"  --org-compare: vs {my_org}'s depth-chart {lvl} {tpos} starter — "
                  f"{(depth_row.get('name') or '').strip()} (ID {lid}, "
                  f"slot {(depth_row.get('tier') or '').strip()}, "
                  f"from {depth_csv.name})")
            compare_ids.append(lid)
            _note_slots_ahead(
                row, depth_row, viewed_depth_row, tpos,
                lambda: wi.score_player(row, cfg, league_lookup, teams, args.role, bool(args.draft)),
                tsaved, league, lvl, eval_path, args.org_compare_composite,
            )
        elif depth_csv is not None:
            print(f"  (--org-compare: depth chart {depth_csv.name} has no {tpos} slotted — "
                  f"showing single card)")
        else:
            leader = find_org_position_leader(eval_path, my_org, tpos, pid, lvl)
            if leader is None:
                print(f"  (--org-compare: no depth chart for {my_org} and no {lvl} {tpos} in "
                      f"the eval CSV — showing single card)")
            else:
                lid = (leader.get("ID") or "").strip()
                print(f"  --org-compare: no depth chart found; vs {my_org}'s top-VOS {lvl} {tpos} "
                      f"— {(leader.get('Name') or '').strip()} (ID {lid}, "
                      f"VOS {(leader.get('VOS_Score') or '').strip()})")
                compare_ids.append(lid)

    if compare_ids:
        ids = [pid] + compare_ids
        return run_compare(
            league, ids, cfg, league_lookup, teams,
            eval_path, args.no_saved, args.draft, args.role,
        )

    # Find saved eval (for the side-by-side "Last Saved" column).
    saved_eval_row: Optional[Dict[str, str]] = None
    eval_has_draft_cols = False
    if not args.no_saved and eval_path and eval_path.exists():
        saved_eval_row = wi.load_latest_eval_row(eval_path, pid)
        if saved_eval_row is None:
            print(f"  (player {pid} not present in that file)")
        else:
            eval_has_draft_cols = any(
                str(saved_eval_row.get(c, "")).strip() != ""
                for c in ("Readiness_Adj", "Draft_Age_Adj", "Draft_RP_Penalty")
            )

    # Player's own org eval (e.g. {league}/eval/bos/) — secondary "(BOS Saved)" col.
    org_saved_eval_row: Optional[Dict[str, str]] = None
    org_abbrev: Optional[str] = None
    if not args.no_saved and not args.no_org_saved:
        org_id = resolve_int(row, "Org")
        org_name = teams.get(org_id, "") if org_id else ""
        if org_name:
            org_abbrev = get_org_abbreviation(org_name)
            my_abbrev = (
                get_org_abbreviation(args.my_org).upper() if args.my_org else None
            )
            if my_abbrev is None or my_abbrev != org_abbrev.upper():
                org_eval_path = wi.find_latest_org_eval_csv(league, org_abbrev)
                if org_eval_path and org_eval_path.exists():
                    print(f"Loaded {org_abbrev} org eval: {org_eval_path.name}")
                    org_saved_eval_row = wi.load_latest_eval_row(org_eval_path, pid)
                    if org_saved_eval_row is None:
                        print(f"  (player {pid} not present in {org_abbrev} eval)")

    if args.draft is None:
        draft_mode = eval_has_draft_cols
    else:
        draft_mode = bool(args.draft)

    is_pit = is_pitcher(row)
    role_override = args.role
    eval_row = wi.score_player(row, cfg, league_lookup, teams, role_override, draft_mode)
    if eval_row is None:
        print("ERROR: failed to score player (insufficient data).", file=sys.stderr)
        return 3

    # ---- Card output ----
    wi.print_header(row, saved_eval_row)
    print(f"  Scoring mode: {'DRAFT' if draft_mode else 'standard'}"
          + (f"  |  role: {role_override}" if (is_pit and role_override) else ""))

    print_summary_scores(
        eval_row, saved_eval_row, is_pit, draft_mode,
        org_saved_eval_row=org_saved_eval_row, org_abbrev=org_abbrev,
    )

    if is_pit:
        print_pitcher_role_scores(row, cfg, league_lookup, teams, draft_mode)
    else:
        print_proj_war(eval_row)
        print_hitter_positional_scores(eval_row, cfg)

    wi.print_ratings(row, is_pit)

    # Contract details + VPC fair value. Contract block draws from the saved
    # eval row (PlayerData CSV doesn't carry contract columns); fair value
    # requires the full eval CSV for VPC calibration.
    if not args.no_contract:
        cur_aav, _ = print_contract_details(saved_eval_row)
        if not args.no_aav:
            aav_ctx = _build_aav_context(
                eval_path, league, args.contract_config, args.aav_years,
            )
            rec = _make_player_rec(row, eval_row, is_pit, role_override)
            print_fair_value(
                aav_ctx, rec, cur_aav,
                suggest_length=not args.no_suggest_length,
                suggest_max_years=args.suggest_max_years,
                suggest_floor_ratio=args.suggest_floor_ratio,
            )

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

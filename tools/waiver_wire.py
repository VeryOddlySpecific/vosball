#!/usr/bin/env python3
"""
waiver_wire.py — Pull the current waiver wire from the /players endpoint and
grade every available player against your org's depth needs.

Why this lives next to trade_targets.py
---------------------------------------
trade_targets.py grades the league-wide /tradeblock through your VOS lens.
waiver_wire.py does the same thing for the *waiver* pool: the players any
org has dropped to the wire and which you can claim for free (subject to
waiver priority). The two scripts share the same scoring spine — candidate
record build, need matching, fit scoring, MD/CSV output — they just pull
from different sources.

Inputs
------
- /players API payload (the same one depth_chart's roster-override layer
  consumes). Players where ``is_on_waivers`` is truthy are the candidate set.
- Latest evaluation_summary_{league}_*.csv (league-wide; same as trade_targets).
- StatsPlus stat endpoints — shared cache via stats.build_player_stats.
- config/depth_config.json (per-level roster sizes, weights).
- config/league_settings.json (auto-resolve --org / --year).

Outputs
-------
- {league}/waiver_wire/{org}_waiver_wire_{ts}.md   — tiered claim list
- {league}/waiver_wire/{org}_waiver_wire_{ts}.csv  — flat candidates

Usage
-----
    python waiver_wire.py --league sahl                       # org auto-resolved
    python waiver_wire.py --league sahl --org "Houston Astros"

When --org / --year are omitted, the script reads them from
``config/league_settings.json`` keyed by --league (same pattern as
trade_targets.py and the bulk runners). Pass them explicitly to override.

A note on cost
--------------
Waivers are free in terms of trade chips — the cost is a roster spot plus
waiver priority. That changes the threshold calculus relative to a trade:
a 47-composite RP off the wire is genuinely tempting depth, even though
trade_targets would correctly filter the same composite as "Pass". This
script uses lower composite floors and a separate "Stash" category for
upside fliers that no trade-shopping checklist would justify.
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
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import depth_chart as dc
import stats as sapi
import trade_block as tb
import trade_targets as tt

logger = logging.getLogger(__name__)
SCRIPT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = SCRIPT_DIR / "config" / "depth_config.json"
DEFAULT_LEAGUE_URL = SCRIPT_DIR / "config" / "league_url.json"
DEFAULT_LEAGUE_IDS = SCRIPT_DIR / "config" / "league_ids.json"
DEFAULT_LEAGUE_SETTINGS = SCRIPT_DIR / "config" / "league_settings.json"

HITTER_POSITIONS = dc.HITTER_POSITIONS
BLOCKING_LEVELS = tb.BLOCKING_LEVELS  # ML + AAA — immediate-roster pool

# Composite floor for waivers — lower than trade_targets' floor (42.0) because
# claiming is free; the cost is a 40-man / roster spot, not a trade asset.
MIN_WAIVER_COMPOSITE = 38.0
# Anything above this in raw VOS_Pot is worth stashing even with no immediate
# need — claims are cheap, prospect upside outlasts roster crunch.
WAIVER_STASH_VOS_POT = 50.0
WAIVER_STASH_AGE_CEILING = 25
# Premium pickup floor — a 52+ composite on the wire is rare and shouldn't be
# allowed to slip through "no immediate need" filtering.
WAIVER_PREMIUM_COMPOSITE = 52.0


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Grade the current waiver wire (players with is_on_waivers set in "
            "/players) against your org's depth needs and produce a ranked "
            "claim list."
        ),
    )
    p.add_argument("--league", required=True, help="League slug (e.g. sahl).")
    p.add_argument("--org", default=None,
                   help="Your organization display name (must match Org column in eval). "
                        "When omitted, resolved from config/league_settings.json keyed by --league.")
    p.add_argument("--org-code", type=str, default=None,
                   help="Subdirectory under {league}/eval/ to look in first for per-org evals.")
    p.add_argument("--year", type=int, default=None,
                   help="Latest year for stats window. When omitted, resolved from "
                        "config/league_settings.json keyed by --league; falls back to "
                        "the current calendar year if neither is set.")
    p.add_argument("--league-settings", type=Path, default=DEFAULT_LEAGUE_SETTINGS,
                   help="Path to league_settings.json (used to auto-resolve --org and --year).")
    p.add_argument("--input", type=Path, default=None,
                   help="Override evaluation_summary CSV.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                   help="depth_config.json path.")
    p.add_argument("--league-url-config", type=Path, default=DEFAULT_LEAGUE_URL)
    p.add_argument("--league-ids-config", type=Path, default=DEFAULT_LEAGUE_IDS)
    p.add_argument("--base-url", type=str, default=None,
                   help="Override league API base URL.")
    p.add_argument("--output-dir", type=Path, default=None,
                   help="Output directory (default: {league}/waiver_wire/).")
    p.add_argument("--no-archive", action="store_true",
                   help="Skip auto-archive of prior runs in the output directory.")
    p.add_argument("--no-stats", action="store_true",
                   help="Skip stat fetch; composite uses VOS only (debugging).")
    p.add_argument("--no-cache", action="store_true",
                   help="Skip disk cache; force fresh API fetches.")
    p.add_argument("--cache-dir", type=Path, default=None,
                   help="Override cache directory (default: {league}/cache/stats/).")
    p.add_argument("--levels", type=str, default=None,
                   help="Comma-separated subset of levels for the *own-org* needs analysis "
                        "(default: every level in depth_config). Waiver candidates are always "
                        "ML+AAA-scoped since that's what's tradeable today.")
    p.add_argument("--include-prospects", action="store_true",
                   help="Include waiver candidates rostered below AAA. Off by default — "
                        "waivers are typically a 40-man mechanism, but minor-league waivers "
                        "do exist and this lets you see them.")
    p.add_argument("--min-composite", type=float, default=MIN_WAIVER_COMPOSITE,
                   help=f"Composite floor for candidates (default {MIN_WAIVER_COMPOSITE}).")
    p.add_argument("--include-no-need", action="store_true",
                   help="Include premium claims even when the position grades as 'Set' for the org.")
    p.add_argument("--include-retired", action="store_true",
                   help="Don't filter retired players from the waiver pool. The /players "
                        "payload occasionally still flags a retiree as is_on_waivers due "
                        "to ordering; off by default to keep noise out of the report.")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args()


# -----------------------------------------------------------------------------
# Waiver-pid extraction — the only thing that differs from trade_targets.
# -----------------------------------------------------------------------------

def extract_waiver_pids(
    players_lookup: Dict[str, Dict[str, str]],
    include_retired: bool = False,
) -> List[str]:
    """Pull the set of player IDs flagged is_on_waivers in the /players payload.

    DFA + DL60 overlap with the waiver flag in practice — a DFA'd player IS
    on waivers — so we surface those rather than filter them. Retirees
    flagged on the wire are almost always API ordering noise; drop them by
    default. ``include_retired`` keeps them for debugging.
    """
    pids: List[str] = []
    for pid, meta in players_lookup.items():
        if not dc._bool_from_value(meta.get("is_on_waivers")):
            continue
        if not include_retired and dc._bool_from_value(meta.get("retired")):
            continue
        pids.append(pid)
    return pids


# -----------------------------------------------------------------------------
# Categorize — waiver-specific buckets. Different from trade_targets because:
#   - claiming is free, so a "Premium (no need)" candidate is genuinely
#     interesting on the wire (you can flip them or stash them) rather than
#     just curiosity-trackable
#   - "Stash" replaces "Lottery" — same idea (young upside) but framed for
#     the wire workflow
#   - the "Pass" floor is lower since the cost calculus is different
# -----------------------------------------------------------------------------

def categorize_waiver(
    cand: Dict[str, Any],
    need_entry: Optional[Dict[str, Any]],
) -> str:
    tier = need_entry["tier"] if need_entry else "Set"
    comp = float(cand.get("composite") or 0)
    vos_pot = float(cand.get("vos_potential") or 0)
    age = tb.parse_age(cand) or 28.0

    if tier in ("Critical", "Major") and comp >= tb.PREMIUM_COMPOSITE:
        return "Priority Claim"
    if tier in ("Critical", "Major"):
        return "Need Claim"
    if tier == "Depth" and comp >= MIN_WAIVER_COMPOSITE:
        return "Depth Claim"
    # No immediate need but premium composite — claim and stash / flip.
    if comp >= WAIVER_PREMIUM_COMPOSITE:
        return "Premium Stash"
    # Young upside flier — same Lottery shape as trade_targets but renamed.
    if vos_pot >= WAIVER_STASH_VOS_POT and age <= WAIVER_STASH_AGE_CEILING:
        return "Stash"
    return "Pass"


# -----------------------------------------------------------------------------
# Report rendering — mirrors trade_targets but with waiver-flavor language.
# -----------------------------------------------------------------------------

def fmt(x: Any, digits: int = 1) -> str:
    try:
        return f"{float(x):.{digits}f}"
    except (TypeError, ValueError):
        return str(x) if x not in (None, "") else "—"


def render_md(
    league: str,
    org: str,
    year: int,
    candidates: List[Dict[str, Any]],
    targets: List[Dict[str, Any]],
    waiver_total: int,
    own_org_on_waivers: int,
) -> str:
    out: List[str] = []
    out.append(f"# Waiver Wire — {org}  ·  {league.upper()}  ·  {year}")
    out.append("")
    out.append(
        "_Players currently on waivers (is_on_waivers = true in /players), scored "
        "against your org's Acquisition Targets. Fit Score = candidate composite × "
        "need-tier weight + age curve. Higher is a better claim. Waivers are free; "
        "the floor is lower here than trade_targets, but the same VOS lens applies._"
    )
    out.append("")
    out.append(
        f"_Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}. "
        f"{waiver_total} players on waivers league-wide · "
        f"{own_org_on_waivers} of your own (filtered) · "
        f"{len(candidates)} evaluated as actionable._"
    )
    out.append("")

    by_cat: Dict[str, List[Dict[str, Any]]] = {}
    for c in candidates:
        by_cat.setdefault(c["_category"], []).append(c)

    out.append("## Summary")
    out.append("")
    out.append("| Bucket | Count | Top Pickup |")
    out.append("| --- | --- | --- |")
    section_order = (
        "Priority Claim", "Need Claim", "Depth Claim",
        "Premium Stash", "Stash", "Pass",
    )
    for label in section_order:
        rows = by_cat.get(label) or []
        if not rows:
            out.append(f"| {label} | 0 | — |")
            continue
        rows.sort(key=lambda p: -p["_fit_score"])
        top = rows[0]
        out.append(
            f"| {label} | {len(rows)} | "
            f"{top['name']} ({top.get('_current_org','?')}, fit {top['_fit_score']:.1f}) |"
        )
    out.append("")

    # Org needs snapshot — same shape trade_block/trade_targets emits.
    tier_order = {"Critical": 0, "Major": 1, "Depth": 2, "Set": 3}
    ordered_targets = sorted(targets, key=lambda t: (tier_order.get(t["tier"], 9), t["pos"]))
    out.append("## Your Needs (for cross-reference)")
    out.append("")
    out.append("| Tier | Pos/Role | Current State | Archetype | Reasoning |")
    out.append("| --- | --- | --- | --- | --- |")
    for t in ordered_targets:
        out.append(
            f"| **{t['tier']}** | {t['pos']} | {t['summary']} | "
            f"{t['archetype']} | {t['reasoning']} |"
        )
    out.append("")

    def _table(rows: List[Dict[str, Any]], title: str, blurb: str) -> None:
        if not rows:
            return
        out.append(f"## {title}")
        out.append("")
        out.append(f"_{blurb}_")
        out.append("")
        out.append(
            "| Name | Current Org | Lvl | Age | Pos/Role | Fit Need | "
            "Need Tier | Career | Reach | Comp | Fit | Flags |"
        )
        out.append(
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
        )
        rows = sorted(rows, key=lambda p: -p["_fit_score"])
        for p in rows:
            pos_label = p.get("primary_pos", "") or p.get("proj_role", "")
            fit_pos = p.get("_fit_pos") or "—"
            need_tier = (p.get("_need_entry") or {}).get("tier", "—")
            flags = p.get("_status_flags") or "—"
            out.append(
                f"| {p['name']} | {p.get('_current_org','')} | "
                f"{p.get('_level','')} | {fmt(p.get('age'), 0)} | "
                f"{pos_label} | {fit_pos} | {need_tier} | "
                f"{fmt(p.get('vos'))} | {fmt(p.get('vos_potential'))} | "
                f"{fmt(p.get('composite'))} | **{p['_fit_score']:.1f}** | {flags} |"
            )
        out.append("")

    _table(
        by_cat.get("Priority Claim") or [],
        "Priority Claims",
        "Premium-composite players who fill a Critical or Major hole. Submit "
        "a claim immediately — these are why the script exists.",
    )
    _table(
        by_cat.get("Need Claim") or [],
        "Need-Filling Claims",
        "Solid pieces that address a Critical or Major hole. Not stars, but "
        "claim spots are cheap and the upgrade is real.",
    )
    _table(
        by_cat.get("Depth Claim") or [],
        "Depth Claims",
        "Players who fit a Depth-tier need — useful as cost-controlled AAA "
        "insurance behind a current starter.",
    )
    _table(
        by_cat.get("Premium Stash") or [],
        "Premium Stash",
        "High-composite players available on the wire even though the position "
        "grades as Set. Worth claiming to flip or stash; you rarely see this "
        "talent unclaimed.",
    )
    _table(
        by_cat.get("Stash") or [],
        "Stash (Upside Fliers)",
        "Young, available, high VOS_Pot. Claim and stash in the farm — these "
        "are the lottery tickets the wire occasionally produces.",
    )
    _table(
        by_cat.get("Pass") or [],
        "Pass",
        "Below the composite floor and not filling any need — listed for "
        "completeness so you can spot anyone the auto-tiering missed.",
    )

    return "\n".join(out)


def write_csv(path: Path, candidates: List[Dict[str, Any]]) -> None:
    fields = [
        "pid", "name", "age", "current_org", "current_team", "level",
        "primary_pos", "proj_role",
        "career", "reach", "composite",
        "fit_pos", "need_tier", "need_archetype",
        "fit_score", "category", "status_flags",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for c in sorted(candidates, key=lambda p: -p["_fit_score"]):
            entry = c.get("_need_entry") or {}
            writer.writerow({
                "pid": c.get("pid", ""),
                "name": c.get("name", ""),
                "age": c.get("age", ""),
                "current_org": c.get("_current_org", ""),
                "current_team": c.get("_current_team", ""),
                "level": c.get("_level", ""),
                "primary_pos": c.get("primary_pos", ""),
                "proj_role": c.get("proj_role", ""),
                "career": f"{float(c.get('vos') or 0):.2f}",
                "reach":  f"{float(c.get('vos_potential') or 0):.2f}",
                "composite": f"{float(c.get('composite') or 0):.2f}",
                "fit_pos": c.get("_fit_pos", ""),
                "need_tier": entry.get("tier", ""),
                "need_archetype": entry.get("archetype", ""),
                "fit_score": f"{c['_fit_score']:.2f}",
                "category": c.get("_category", ""),
                "status_flags": c.get("_status_flags", ""),
            })


# -----------------------------------------------------------------------------
# main()
# -----------------------------------------------------------------------------

def main() -> int:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")

    cfg = dc.load_config(args.config)

    # Auto-resolve --org / --year from league_settings.json — same helper
    # trade_targets uses, so the two scripts stay behaviorally aligned.
    resolved_org, resolved_year = tt.resolve_org_year(
        args.league, args.org, args.year, args.league_settings,
    )
    args.org = resolved_org
    args.year = resolved_year
    if not args.org:
        logger.error(
            "No --org provided and no 'org' entry for league %r in %s. "
            "Either pass --org explicitly or add the league to league_settings.json.",
            args.league, args.league_settings,
        )
        return 2
    target_year = args.year or datetime.now().year

    if args.levels:
        levels_to_run = [lvl.strip().upper() for lvl in args.levels.split(",") if lvl.strip()]
        for lvl in levels_to_run:
            if lvl not in cfg["levels"]:
                logger.error("Level '%s' not in depth_config.json", lvl)
                return 2
    else:
        levels_to_run = list(cfg["levels"].keys())

    out_dir = args.output_dir or (SCRIPT_DIR / args.league / "waiver_wire")
    out_dir.mkdir(parents=True, exist_ok=True)
    if not args.no_archive:
        moved, archive_dir = dc.archive_previous_runs(out_dir)
        if moved:
            logger.info("Archived %d prior waiver_wire file(s) to %s", moved, archive_dir)

    # --- Load eval + apply /players override --------------------------------
    try:
        eval_path = dc.find_latest_eval(args.league, args.input, args.org_code)
    except FileNotFoundError as e:
        logger.error("%s", e)
        return 2
    logger.info("Using eval file: %s", eval_path)
    eval_rows = dc.read_eval(eval_path)

    base_url = sapi.resolve_base_url(args.league, args.base_url, args.league_url_config)
    if not base_url:
        logger.error(
            "No base URL for league '%s' — /players is required to read the waiver flag.",
            args.league,
        )
        return 2

    cache_dir = None
    if not args.no_cache:
        cache_dir = args.cache_dir or (SCRIPT_DIR / args.league / "cache" / "stats")

    players_lookup = sapi.build_players_lookup(base_url, cache_dir=cache_dir)
    if not players_lookup:
        logger.error("/players returned no rows — cannot determine waiver wire.")
        return 2
    logger.info("Loaded /players (%d entries)", len(players_lookup))

    level_id_to_label = dc.load_level_id_to_label()
    team_id_to_name = dc.load_team_id_to_name(args.league)

    # Apply override with include_inactive=True. The default behavior of
    # apply_players_override strips ANY inactive flag (retired / DFA /
    # waivers / DL60) — but waiver-flagged players are the entire point of
    # this script, so dropping them at the override layer would empty the
    # pool. We keep them in eval_rows and then post-filter retired (only)
    # at the waiver-pid extraction step. DFA + DL60 are surfaced via
    # _Status_Flags so the report can show them next to fit scores.
    counts = dc.apply_players_override(
        eval_rows, players_lookup, level_id_to_label, team_id_to_name,
        include_inactive=True,
    )
    logger.info(
        "Players override: %d eval rows | %d level overrides | %d org overrides "
        "(inactive players kept — waiver pool needs them visible)",
        counts["total"], counts["level_overrides"], counts["org_overrides"],
    )

    # --- Identify the waiver pool -------------------------------------------
    waiver_pids = extract_waiver_pids(
        players_lookup, include_retired=args.include_retired,
    )
    waiver_total = len(waiver_pids)
    if waiver_total == 0:
        logger.info("No players currently on waivers — nothing to evaluate.")
        # Still write an empty report so the user sees the script ran.
    logger.info("Waiver pool: %d player(s) flagged is_on_waivers.", waiver_total)

    # Count own-org waivers separately for the header — they're filtered out
    # of the recommendation list but it's useful to know how many you've cut.
    own_l = args.org.strip().lower()
    own_org_on_waivers = 0
    for pid in waiver_pids:
        meta = players_lookup.get(pid) or {}
        org_id_raw = (meta.get("organization_id") or meta.get("parent_team_id") or "").strip()
        if not org_id_raw:
            continue
        try:
            org_id = int(org_id_raw)
        except ValueError:
            continue
        org_name = team_id_to_name.get(org_id, "")
        if org_name and org_name.strip().lower() == own_l:
            own_org_on_waivers += 1

    # --- Stats pipeline (shared with depth_chart / trade_targets) -----------
    hitter_stats: Dict[str, Dict[str, Any]] = {}
    pitcher_stats: Dict[str, Dict[str, Any]] = {}
    if not args.no_stats:
        league_ids_map = dc.load_league_ids(args.league_ids_config)
        all_lids: List[int] = []
        seen: set = set()
        for level_ids in league_ids_map.get(args.league.lower(), {}).values():
            for lid in level_ids:
                if lid not in seen:
                    seen.add(lid)
                    all_lids.append(lid)
        logger.info("Fetching stats for %d lids", len(all_lids))
        hitter_stats, pitcher_stats, _, _ = sapi.build_player_stats(
            base_url, target_year,
            cfg.get("year_weights", [0.55, 0.35, 0.10]),
            cfg.get("woba_weights", {}),
            lids=all_lids or None,
            target_lids=None,
            cache_dir=cache_dir,
        )

    # --- Build own-org context for needs assessment -------------------------
    all_org_hitters: List[Dict[str, Any]] = []
    all_org_pitchers: List[Dict[str, Any]] = []
    for level in levels_to_run:
        h, p = tb.analyze_level(
            level, args, cfg, eval_rows, target_year, hitter_stats, pitcher_stats,
        )
        all_org_hitters.extend(h)
        all_org_pitchers.extend(p)

    if not all_org_hitters and not all_org_pitchers:
        logger.error(
            "No players found for org '%s' at levels %s — cannot grade needs.",
            args.org, levels_to_run,
        )
        return 2

    targets = tb.build_acquisition_targets(all_org_hitters, all_org_pitchers)
    need_lookup = tt._build_need_lookup(targets)

    # --- Evaluate waiver candidates -----------------------------------------
    # trade_targets.build_candidate_records does exactly what we want: take a
    # pid set, intersect with eval rows, drop own-org, scope to ML/AAA (unless
    # --include-prospects flips the scope), build player records.
    if args.include_prospects:
        # Build a custom candidate loop that doesn't enforce BLOCKING_LEVELS.
        candidates = _build_candidate_records_all_levels(
            waiver_pids, args.org, eval_rows, cfg, hitter_stats, pitcher_stats,
        )
    else:
        candidates = tt.build_candidate_records(
            waiver_pids, args.org, eval_rows, cfg, hitter_stats, pitcher_stats,
        )

    scored: List[Dict[str, Any]] = []
    for cand in candidates:
        need_entry, fit_pos = tt.match_candidate_to_need(cand, need_lookup, all_org_pitchers)
        cand["_need_entry"] = need_entry
        cand["_fit_pos"] = fit_pos
        cand["_fit_score"] = tt.compute_fit_score(cand, need_entry)
        cand["_category"] = categorize_waiver(cand, need_entry)

        comp = float(cand.get("composite") or 0)
        vos_pot = float(cand.get("vos_potential") or 0)
        # Floors. Drop anything below the composite floor unless it's a stash-
        # worthy upside flier.
        if comp < args.min_composite and vos_pot < WAIVER_STASH_VOS_POT:
            continue
        # Filter "Pass" by default unless --include-no-need also keeps the
        # bottom of the list visible.
        if cand["_category"] == "Pass" and not args.include_no_need:
            continue
        scored.append(cand)

    logger.info(
        "Waiver targets: %d candidates after filtering (%d hitters, %d pitchers).",
        len(scored),
        sum(1 for c in scored if not c.get("is_pitcher")),
        sum(1 for c in scored if c.get("is_pitcher")),
    )

    # --- Write outputs ------------------------------------------------------
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    org_slug = args.org.lower().replace(" ", "_")
    md_path = out_dir / f"{org_slug}_waiver_wire_{ts}.md"
    csv_path = out_dir / f"{org_slug}_waiver_wire_{ts}.csv"

    md = render_md(
        args.league, args.org, target_year,
        scored, targets, waiver_total, own_org_on_waivers,
    )
    md_path.write_text(md, encoding="utf-8")
    logger.info("Wrote %s", md_path)

    write_csv(csv_path, scored)
    logger.info("Wrote %s", csv_path)

    return 0


def _build_candidate_records_all_levels(
    waiver_pids: List[str],
    own_org: str,
    eval_rows: List[Dict[str, str]],
    cfg: Dict[str, Any],
    hitter_stats: Dict[str, Dict[str, Any]],
    pitcher_stats: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Variant of trade_targets.build_candidate_records that doesn't enforce
    the ML+AAA scope. Used when --include-prospects is set so the report also
    surfaces minor-league waivers.

    Identical scoring spine — the only difference is the level filter.
    """
    pid_set = set(waiver_pids)
    if not pid_set:
        return []
    floors = cfg.get("stat_floors", {})
    h_means, h_stds = (
        dc.compute_means_stds(hitter_stats, dc.HITTER_COMPONENTS, "overall")
        if hitter_stats else ({}, {})
    )
    h_means_l, h_stds_l = (
        dc.compute_means_stds(hitter_stats, dc.HITTER_COMPONENTS, "vs_l")
        if hitter_stats else ({}, {})
    )
    h_means_r, h_stds_r = (
        dc.compute_means_stds(hitter_stats, dc.HITTER_COMPONENTS, "vs_r")
        if hitter_stats else ({}, {})
    )
    p_means, p_stds = (
        dc.compute_means_stds(pitcher_stats, dc.PITCHER_COMPONENTS, "overall")
        if pitcher_stats else ({}, {})
    )

    own_l = own_org.strip().lower()
    candidates: List[Dict[str, Any]] = []
    seen_pids: set = set()
    for row in eval_rows:
        pid = (row.get("ID") or "").strip()
        if not pid or pid not in pid_set or pid in seen_pids:
            continue
        seen_pids.add(pid)
        if (row.get("Org") or "").strip().lower() == own_l:
            continue
        level = (row.get("League_Level") or "").strip().upper()
        if not level:
            continue
        # Pick the closest level cfg — when the player's level is below the
        # blocking pool, fall back to AAA cfg so the stat-weighting block
        # makes sense (a R-ball wOBA blended through ML weights would be
        # nonsense).
        level_cfg = (
            cfg["levels"].get(level)
            or cfg["levels"].get("AAA")
            or cfg["levels"].get("ML")
            or next(iter(cfg["levels"].values()), {})
        )
        rec = dc.build_player_record(
            row, pitcher_stats, hitter_stats, level_cfg, floors,
            p_means, p_stds, h_means, h_stds,
            h_means_l, h_stds_l, h_means_r, h_stds_r,
        )
        rec["_level"] = level
        rec["_current_org"] = (row.get("Org") or "").strip()
        rec["_current_team"] = (row.get("Team") or "").strip()
        rec["_status_flags"] = (row.get("_Status_Flags") or "").strip()
        candidates.append(rec)

    return candidates


if __name__ == "__main__":
    raise SystemExit(main())

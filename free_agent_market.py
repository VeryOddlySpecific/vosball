#!/usr/bin/env python3
"""
free_agent_market.py — Free agent analysis ranked by fit against your depth chart.

Filters the eval CSV to rows with no Org (free agents), runs the same
ratings+stats composite math as depth_chart.py, and then compares each FA
against the equivalent slot on your org's depth chart at the chosen level.

Output (one of each, alongside depth_chart outputs):
  {league}/depth/free_agents_{org_slug}_{level}_{ts}.csv
  {league}/depth/free_agents_{org_slug}_{level}_{ts}.md

The MD has four sections:
  1. Free Agent Hitters (ranked by composite)
  2. Free Agent Pitchers (ranked by composite)
  3. Hitter Upgrade Targets — FAs whose composite beats your starter at that pos
  4. Pitcher Upgrade Targets — FAs whose composite beats your weakest in role

Usage:
  py free_agent_market.py --league sahl --org "Houston Astros" --level ML --year 2061
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import stats as sapi
import depth_chart as dc
import contract as ct
import farm_value_old as fv

logger = logging.getLogger(__name__)
SCRIPT_DIR = Path(__file__).resolve().parent

DEFAULT_CONTRACT_CONFIG = SCRIPT_DIR / "config" / "contract_config.json"

HITTER_POSITIONS = dc.HITTER_POSITIONS
PITCHER_ROLES = ("SP", "CL", "SU", "MR", "LR")

# Multi-level FA scan: ordering for last-level filter and quick-hits sort.
# Lower number = higher level. R-ACL/R-DSL collapse to R for ranking.
LEVEL_RANK = {
    "ML": 0, "AAA": 1, "AA": 2, "A+": 3, "A": 4, "A-": 5,
    "R": 6, "R-ACL": 6, "R-DSL": 6,
}

# Per-level age caps for FA recommendations. Older FAs aren't useful for dev
# levels — filtering them out keeps the recommendations relevant. Override
# globally with --age-cap-override.
DEFAULT_AGE_CAPS: Dict[str, Optional[int]] = {
    "ML": None,
    "AAA": None,
    "AA": 30,
    "A+": 27,
    "A": 27,
    "A-": 24,
    "R": 24,
    "R-ACL": 24,
    "R-DSL": 24,
}

# Filename pattern for depth chart CSVs. Matches both vanilla levels (R, AAA)
# and affiliate-suffixed sub-levels (R-ACL, R-DSL).
DEPTH_CHART_FILE_RE = re.compile(
    r"^(?P<org>[a-z0-9_]+?)_(?P<level>ML|AAA|AA|A\+|A-|A|R|R-[A-Z]{3})_(?P<ts>\d{8}_\d{6})\.csv$"
)


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Rank free agents by fit against your depth chart.")
    p.add_argument("--league", required=True)
    p.add_argument("--org", required=True, help="Your org name as it appears in the eval CSV.")
    p.add_argument("--level", default=None, help="Level whose weights to evaluate FAs at (typically ML). Required unless --scan-depth-dir is set.")
    p.add_argument("--scan-depth-dir", action="store_true",
                   help="Multi-level mode. Reads the latest existing depth chart batch in {league}/depth/ for the org "
                        "and produces FA upgrade recommendations per (level, position) slot in one combined report. "
                        "Ignores --level when set.")
    p.add_argument("--no-auto-refresh", action="store_true",
                   help="In --scan-depth-dir mode, don't regenerate depth charts even when the "
                        "on-disk batch is older than the latest eval (or has no provenance). "
                        "By default FA re-runs depth_chart.py against the latest eval first so "
                        "recommendations always reflect current evaluations.")
    p.add_argument("--age-cap-override", type=int, default=None,
                   help="Override the per-level age cap in --scan-depth-dir mode. Defaults shipped per level (AA cap 30, A+/A cap 27, A-/R cap 24).")
    p.add_argument("--ignore-last-level", action="store_true",
                   help="In --scan-depth-dir mode, don't filter FAs by their last-played level. By default, only FAs whose last level is at most 1 below the target slot are surfaced.")
    p.add_argument("--min-pro-service-days", type=int, default=1,
                   help="In --scan-depth-dir mode, require FAs to have at least this many days of professional service per the /players endpoint. Filters out amateur draft-eligible players who appear as 'free agents' but haven't actually entered pro ball yet. Default 1 (any pro experience). Set 0 to include amateurs.")
    p.add_argument("--year", type=int, default=None, help="Stats window end year (defaults to calendar year — pass OOTP season explicitly).")
    p.add_argument("--input", type=Path, default=None, help="Override evaluation_summary CSV.")
    p.add_argument("--org-code", type=str, default=None,
                   help="Subdirectory under {league}/eval/ to look in first for per-org evals (e.g. 'hou'). Falls back to top-level eval/ if missing.")
    p.add_argument("--config", type=Path, default=dc.DEFAULT_CONFIG)
    p.add_argument("--league-url-config", type=Path, default=dc.DEFAULT_LEAGUE_URL)
    p.add_argument("--league-ids-config", type=Path, default=dc.DEFAULT_LEAGUE_IDS)
    p.add_argument("--park-factors", type=Path, default=None,
                   help="Path to combined-teams park-factors JSON. Used to resolve "
                        "a short --org code (e.g. 'stl') to the full team name that "
                        "matches the eval CSV's Org column. Defaults to "
                        "config/{league}-park-factors.json.")
    p.add_argument("--base-url", type=str, default=None)
    p.add_argument("--top-n-hitters", type=int, default=30)
    p.add_argument("--top-n-pitchers", type=int, default=30)
    p.add_argument("--min-pa", type=float, default=0.0,
                   help="Filter FA hitters with fewer than this many PA in the window. Default 0.")
    p.add_argument("--min-ip", type=float, default=0.0,
                   help="Filter FA pitchers with fewer than this many IP in the window. Default 0.")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--output-dir", type=Path, default=None)
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument("--min-comp", type=float, default=None,
                   help="Minimum composite (20-80 scale) for a FA to qualify as a starter at "
                        "a position. Used to score candidates in the 'Empty Starter Slots — "
                        "FA Candidates' section. If unset, the latest matching depth_chart "
                        "_starter_gaps.json sidecar is consulted automatically.")
    p.add_argument("--min-comp-pos", type=str, default=None,
                   help="Per-position min-comp overrides (e.g. 'C:65,SS:50'). Overrides the "
                        "sidecar values for the named positions; unlisted positions fall back "
                        "to --min-comp or the sidecar.")
    p.add_argument("--no-gap-fa-section", action="store_true",
                   help="Suppress the 'Empty Starter Slots — FA Candidates' section even when "
                        "thresholds are available.")
    p.add_argument("--gap-fa-top-n", type=int, default=3,
                   help="How many top FA candidates to list per empty starter slot. Default 3.")
    p.add_argument("--aav-years", type=int, default=3,
                   help="Contract term used to compute market fair AAV via contract.py "
                        "(AAV = total_fair_value / years). Fixed term keeps AAVs comparable "
                        "across players; the age curve dilutes longer terms for older players. "
                        "Default 3.")
    p.add_argument("--no-aav", action="store_true",
                   help="Skip fair-AAV computation. Useful when the league has no "
                        "contract_config.json or /players endpoint isn't reachable.")
    p.add_argument("--no-suggest-length", action="store_true",
                   help="Suppress the suggested contract length / AAV / total columns. "
                        "Use when you only want the fixed-term Fair AAV reference.")
    p.add_argument("--suggest-max-years", type=int, default=7,
                   help="Upper bound the suggestion can recommend. Default 7. The actual "
                        "suggestion is usually shorter because the decline cutoff trims "
                        "tail years where projected value drops below the floor.")
    p.add_argument("--suggest-floor-ratio", type=float, default=0.85,
                   help="Decline cutoff: stop suggesting years once projected year-N "
                        "fair value drops below this fraction of year-1. Default 0.85 "
                        "(85%%). Lower = longer suggested deals (more willing to pay "
                        "for the tail); higher = shorter, more conservative. The default "
                        "is tuned to the gentle age curves in this league's "
                        "contract_config — at 0.6 the cutoff almost never trips because "
                        "VOS only drops ~1%% per year post-peak.")
    p.add_argument("--contract-config", type=Path, default=DEFAULT_CONTRACT_CONFIG,
                   help="Path to contract_config.json (drives VPC params, age curve, "
                        "elite premiums, etc.). Default config/contract_config.json.")
    p.add_argument("--vpc-diagnostic", action="store_true",
                   help="Print VPC calibration diagnostics: sample size, "
                        "salary distribution, vos_potential distribution, and the "
                        "spread of implied $/vos-pt across the calibration rows. "
                        "Use to catch silently-stale eval CSVs after a weights bump.")
    return p.parse_args()


# -----------------------------------------------------------------------------
# FA filtering
# -----------------------------------------------------------------------------

def fa_pool(eval_rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Free agents = any row with a blank Org column."""
    return [r for r in eval_rows if not (r.get("Org") or "").strip()]


# Threshold for the Δ Stat-VOS ⚠ glyph. Tuned so a routine 5-point gap between
# stats and ratings (normal noise) doesn't trip the flag, but a 10+ point gap —
# the regression-candidate / overperformer band v10's BABIP features were
# designed to surface — does. Symmetric on both signs (under- and over-).
STAT_VOS_DELTA_FLAG = 10.0

# Sample-weight thresholds for the rating-only badge in FA upgrade tables.
# Below RATING_ONLY_THRESHOLD, the composite is essentially the rating VOS
# (stats contribute almost nothing) — flag so users don't read the composite
# as stat-confirmed. Above STAT_VOS_FLAG_MIN we trust the stat sample enough
# to fire the Δ ⚠ glyph; in between is the "some signal" middle band.
RATING_ONLY_THRESHOLD = 0.25
STAT_VOS_FLAG_MIN = 0.5

# Threshold for flagging FAs whose last-played level isn't where they
# accumulated most of their playing time — the "12 PA at ML, 400 at AAA"
# case from the audit. Only meaningful in single-level mode where the bundle
# has a target_lids-filtered view. The 0.3 floor: less than 30% of the
# player's recent PA at the target level → flagged.
TARGET_LEVEL_SHARE_FLAG = 0.30
TARGET_LEVEL_MIN_PA = 50.0  # below this we don't have enough signal to flag
TARGET_LEVEL_MIN_IP = 15.0

# Hitter & pitcher tier rank (lower = better). Mirrors the band order in
# run_vos.classify_vos_tier — duplicated here so we don't pull a run_vos
# import into the FA tool. If tier labels diverge from the defaults in
# weights config, the unknown label sorts to the bottom (worst).
HITTER_TIER_RANK: Dict[str, int] = {
    "Star": 0,
    "Above-Avg Regular": 1,
    "Reliable Starter": 2,
    "Fringe Regular": 3,
    "Bench": 4,
    "Replacement": 5,
    "Org Filler": 6,
}
PITCHER_TIER_RANK: Dict[str, int] = {
    "Ace": 0,
    "#2/#3 Starter": 1,
    "Mid-Rotation": 2,
    "Back-End / Setup": 3,
    "Long Relief / Swing": 4,
    "Replacement": 5,
    "Org Filler": 6,
}
TIER_UNKNOWN_RANK = 99

# A 2+ tier jump (e.g. Bench -> Above-Avg Regular) is "🔥-worthy" — the
# slot's quality is meaningfully poor relative to the FA's level.
TIER_JUMP_FIRE_THRESHOLD = 2


def _tier_rank(label: str, is_pitcher: bool) -> int:
    """Return the rank index for a tier label, or TIER_UNKNOWN_RANK when the
    label is empty / not in the standard set. Lower = better."""
    if not label:
        return TIER_UNKNOWN_RANK
    table = PITCHER_TIER_RANK if is_pitcher else HITTER_TIER_RANK
    return table.get(label, TIER_UNKNOWN_RANK)


def _tier_jump(fa_tier: str, slot_tier: str, is_pitcher: bool) -> int:
    """How many tier bands the FA outranks the slotted player by.

    Positive = FA is better. 0 = same tier. Negative = FA is worse (we don't
    surface these in upgrade tables but the helper supports both directions
    so callers can decide). Returns 0 when either tier is unknown — better
    to silently no-op than to falsely report a huge gap when the slot's
    tier didn't get computed.
    """
    if not fa_tier or not slot_tier:
        return 0
    fa_r = _tier_rank(fa_tier, is_pitcher)
    slot_r = _tier_rank(slot_tier, is_pitcher)
    if fa_r == TIER_UNKNOWN_RANK or slot_r == TIER_UNKNOWN_RANK:
        return 0
    return slot_r - fa_r  # FA rank lower => positive jump (better)


def _fmt_tier_jump(fa_tier: str, slot_tier: str, is_pitcher: bool) -> str:
    """Render a tier delta cell — '🔥 Bench → Star' when the jump is large,
    'Bench → Star' for modest jumps, '—' when no meaningful info. Slot tier
    is rendered first to read like the actual upgrade direction."""
    if not fa_tier and not slot_tier:
        return "—"
    if not slot_tier:
        slot_tier = "(none)"
    if not fa_tier:
        return f"{slot_tier} → ?"
    jump = _tier_jump(fa_tier, slot_tier, is_pitcher)
    marker = "🔥 " if jump >= TIER_JUMP_FIRE_THRESHOLD else ""
    arrow = "→" if jump > 0 else "≈" if jump == 0 else "↓"
    return f"{marker}{slot_tier} {arrow} {fa_tier}"


def _to_float_or_none(v: Any) -> Optional[float]:
    """Defensive float coercion for eval CSV passthrough fields. Empty strings
    and missing values return None so downstream display can show '—' instead
    of '0.0' (which would falsely look like a real 0)."""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _attach_v10_passthrough(rec: Dict[str, Any], eval_row: Dict[str, str]) -> None:
    """Pull v10-era diagnostic columns from the eval row onto the FA record.

    These columns aren't used by build_player_record's composite math, but
    surface in the FA tables / CSV so users can spot regression candidates
    (BABIP gaps), injury risks (Prone), and tier deltas at a glance without
    re-joining PlayerData.

    Also computes ``delta_stat_vos = stat_score - vos`` and a boolean
    ``stat_vos_flagged`` for the ⚠ glyph in MD tables. Positive delta = stats
    above ratings (overperformer / sell-high); negative = below (regression
    candidate / buy-low — the v10 BABIP wins are mostly in this band).
    """
    rec["vos_tier"] = (eval_row.get("VOS_Tier") or "").strip()
    rec["vos_potential_tier"] = (eval_row.get("VOS_Potential_Tier") or "").strip()
    rec["prone"] = (eval_row.get("Prone") or "").strip()
    rec["babip"] = _to_float_or_none(eval_row.get("BABIP"))
    rec["pot_babip"] = _to_float_or_none(eval_row.get("PotBABIP"))
    rec["pbabip"] = _to_float_or_none(eval_row.get("PBABIP"))
    rec["pot_pbabip"] = _to_float_or_none(eval_row.get("PotPBABIP"))

    stat = float(rec.get("stat_score") or 0.0)
    vos = float(rec.get("vos") or 0.0)
    sw = float(rec.get("sample_weight") or 0.0)
    delta = stat - vos
    rec["delta_stat_vos"] = delta
    # Only flag when there's enough stat sample to make the comparison
    # meaningful — a near-zero sample_weight means stat_score has been
    # blended toward neutral, and the gap is mostly an artifact of that.
    rec["stat_vos_flagged"] = (sw >= STAT_VOS_FLAG_MIN and abs(delta) >= STAT_VOS_DELTA_FLAG)
    # Rating-only FA: composite is essentially ratings, stats contributed
    # ~nothing. Surfaced in upgrade tables so a 'Star' tier with zero recent
    # PA reads as the rating-only projection it is, not a stat-confirmed star.
    rec["rating_only"] = (sw < RATING_ONLY_THRESHOLD)

    # Single-level mode: when build_player_stats was called with target_lids
    # set to one level, the bundle carries an `overall_target` view scoped to
    # that level. Compare total PA vs target-level PA to detect FAs whose
    # last_level recorded as the target but who actually played mostly
    # elsewhere — the "12 PA at ML, 400 at AAA" red flag from the audit.
    # Multi-level mode passes all_lids as target_lids so this share is always
    # 1.0; the flag stays unset and the badge doesn't render — correct
    # behavior because multi-level can't make this distinction.
    rec["target_level_share"] = None
    rec["target_level_mismatch"] = False
    bundle_key = "pitcher_bundle" if rec.get("is_pitcher") else "hitter_bundle"
    bundle = rec.get(bundle_key)
    if bundle:
        overall = bundle.get("overall") or {}
        target = bundle.get("overall_target")
        if target is not None:
            if rec.get("is_pitcher"):
                total = float(overall.get("IP", 0.0) or 0.0)
                at_target = float(target.get("IP", 0.0) or 0.0)
                min_sample = TARGET_LEVEL_MIN_IP
            else:
                total = float(overall.get("PA", 0.0) or 0.0)
                at_target = float(target.get("PA", 0.0) or 0.0)
                min_sample = TARGET_LEVEL_MIN_PA
            if total > 0:
                share = at_target / total
                rec["target_level_share"] = share
                rec["target_level_mismatch"] = (
                    total >= min_sample and share < TARGET_LEVEL_SHARE_FLAG
                )


# -----------------------------------------------------------------------------
# Market fair-AAV (via contract.py)
# -----------------------------------------------------------------------------


class AAVContext:
    """One-shot setup for market fair-AAV computation.

    Loads contract_config, calibrates VPC against established (6+ service-year)
    FAs in the eval CSV, and exposes a ``compute(rec) -> Optional[int]`` method
    that returns annualized dollars for any FA record (or None if the snapshot
    can't be built). Calibrating against true FAs gives a market-only VPC —
    arb deals stay out of the regression.
    """

    def __init__(
        self,
        eval_rows: List[Dict[str, str]],
        contract_config_path: Path,
        league: str,
        league_url_config: Path,
        base_url: Optional[str],
        cache_dir: Optional[Path],
        aav_years: int,
        diagnostic: bool = False,
    ) -> None:
        self.aav_years = max(1, int(aav_years))
        self.diagnostic = diagnostic
        self.cfg = ct.load_config(contract_config_path)
        self.rounding = int(self.cfg.get("contract_defaults", {}).get("rounding", 100000))
        vpc_cfg = self.cfg["vpc"]

        # /players for service-time filter. Falls back to ML-only calibration
        # when /players isn't reachable — still useful, just slightly noisier
        # because some arb deals leak into the calibration.
        players_lookup: Dict[str, Dict[str, str]] = {}
        url = base_url or sapi.resolve_base_url(league, None, league_url_config)
        if url:
            try:
                players_lookup = sapi.build_players_lookup(url, cache_dir=cache_dir)
            except Exception as exc:  # noqa: BLE001 — degrade gracefully
                logger.warning("AAV: /players fetch failed (%s); calibrating without service-time filter.", exc)

        calib_rows: List[Dict[str, str]] = eval_rows
        calib_mode = "all-ML"
        if players_lookup:
            calib_rows = ct._filter_rows_fa_only(eval_rows, players_lookup, min_service_years=6.0)
            calib_mode = "market-only (6+ service yrs)"

        self.vpc, self.vpc_n = fv.compute_vpc_base(
            rows=calib_rows,
            salary_col=vpc_cfg["salary_col"],
            calib_col=vpc_cfg["pot_col"],
            vos_floor=float(vpc_cfg["vos_floor"]),
            winsor_lower=float(vpc_cfg["winsor_lower"]),
            winsor_upper=float(vpc_cfg["winsor_upper"]),
            players_lookup=None,  # already filtered above
        )
        logger.info("AAV: VPC=%.0f (n=%d, calib=%s, term=%d yrs)",
                    self.vpc, self.vpc_n, calib_mode, self.aav_years)

        if self.diagnostic:
            self._print_vpc_diagnostic(
                calib_rows,
                salary_col=vpc_cfg["salary_col"],
                calib_col=vpc_cfg["pot_col"],
                vos_floor=float(vpc_cfg["vos_floor"]),
                calib_mode=calib_mode,
            )

    @staticmethod
    def _percentiles(vals: List[float], pcts: Tuple[float, ...] = (0.0, 0.25, 0.5, 0.75, 1.0)) -> List[float]:
        """Simple percentiles for diagnostic output. Linear interpolation
        between sorted values — matches numpy.percentile(interpolation='linear')
        for the percentiles we care about."""
        if not vals:
            return [0.0] * len(pcts)
        s = sorted(vals)
        n = len(s)
        out: List[float] = []
        for p in pcts:
            if n == 1:
                out.append(s[0])
                continue
            idx = p * (n - 1)
            lo = int(idx)
            hi = min(lo + 1, n - 1)
            frac = idx - lo
            out.append(s[lo] * (1 - frac) + s[hi] * frac)
        return out

    def _print_vpc_diagnostic(
        self,
        calib_rows: List[Dict[str, str]],
        salary_col: str,
        calib_col: str,
        vos_floor: float,
        calib_mode: str,
    ) -> None:
        """Dump the VPC calibration pool's salary/vos distribution so a stale
        eval CSV (e.g., one generated under v6 weights but consumed by v10
        contract math) is visible at a glance.

        The signal to watch: if the implied $/vos-pt spread is huge (max/min
        ratio > 5x) the calibration is noisy and the AAV column should be
        treated as a rough guide, not a quote. After a weights bump the mean
        vos shifts but the VPC re-fits — so VPC values that suddenly halve or
        double across runs are a 'did the eval re-run?' red flag.
        """
        pairs: List[Tuple[float, float]] = []
        for r in calib_rows:
            if (r.get("League_Level") or "").strip() != "ML":
                continue
            if fv.to_float(r.get("Contract_is_major"), 0.0) != 1.0:
                continue
            salary = fv.to_float(r.get(salary_col), 0.0)
            vos = fv.to_float(r.get(calib_col), 0.0)
            if salary <= 0 or vos < vos_floor:
                continue
            pairs.append((salary, vos))

        n = len(pairs)
        if n == 0:
            logger.info("VPC-DIAG: no rows survived the ML+major+salary+vos_floor filter — calibration empty")
            return

        salaries = [s for s, _ in pairs]
        voses = [v for _, v in pairs]
        ratios = [s / v for s, v in pairs if v > 0]

        s_pcts = self._percentiles(salaries)
        v_pcts = self._percentiles(voses)
        r_pcts = self._percentiles(ratios)

        def _fmt_m(v: float) -> str:
            return f"${v / 1_000_000:.2f}M" if v >= 1_000_000 else f"${v / 1_000:.0f}K"

        logger.info("VPC-DIAG (%s, n=%d, salary=%s, calib=%s, vos_floor=%g)",
                    calib_mode, n, salary_col, calib_col, vos_floor)
        logger.info(
            "VPC-DIAG salary       min=%s  p25=%s  p50=%s  p75=%s  max=%s",
            _fmt_m(s_pcts[0]), _fmt_m(s_pcts[1]), _fmt_m(s_pcts[2]),
            _fmt_m(s_pcts[3]), _fmt_m(s_pcts[4]),
        )
        logger.info(
            "VPC-DIAG vos_pot      min=%.1f  p25=%.1f  p50=%.1f  p75=%.1f  max=%.1f",
            v_pcts[0], v_pcts[1], v_pcts[2], v_pcts[3], v_pcts[4],
        )
        logger.info(
            "VPC-DIAG $/vos-pt     min=%s  p25=%s  p50=%s  p75=%s  max=%s",
            _fmt_m(r_pcts[0]), _fmt_m(r_pcts[1]), _fmt_m(r_pcts[2]),
            _fmt_m(r_pcts[3]), _fmt_m(r_pcts[4]),
        )
        spread = (r_pcts[4] / r_pcts[0]) if r_pcts[0] > 0 else float("inf")
        logger.info(
            "VPC-DIAG implied $/vos-pt spread (max/min) = %.1fx  [healthy <5x, noisy >10x]",
            spread,
        )
        # Quick stale-eval sniff: if winsorized VPC value drifts much from
        # row-wise median ratio, the eval is likely fine — VPC is a winsorized
        # ratio-of-sums. A big drift between p50 ratio and VPC means the tails
        # of the distribution are doing a lot of work; not a bug per se but
        # worth seeing.
        logger.info(
            "VPC-DIAG fitted VPC=%s  vs  row-wise median $/vos-pt=%s  (drift %+.1f%%)",
            _fmt_m(self.vpc), _fmt_m(r_pcts[2]),
            100.0 * (self.vpc - r_pcts[2]) / r_pcts[2] if r_pcts[2] > 0 else 0.0,
        )

    def _snapshot(self, rec: Dict[str, Any]) -> Optional["ct.PlayerSnapshot"]:
        """Build the contract.PlayerSnapshot used by run_valuation. Returns
        None when the record can't be coerced (missing age, etc.) — caller
        treats that as "skip this player's contract math"."""
        try:
            age_raw = rec.get("age", "")
            age = int(float(age_raw)) if age_raw not in ("", None) else 0
            is_pitcher = bool(rec.get("is_pitcher"))
            pos = rec.get("proj_role") if is_pitcher else rec.get("primary_pos", "")
            return ct.PlayerSnapshot(
                pid=str(rec.get("pid", "")),
                name=str(rec.get("name", "")),
                age=age,
                pos=(pos or "").strip(),
                vos_current=float(rec.get("vos", 0.0)),
                vos_potential=float(rec.get("vos_potential", 0.0)),
                is_pitcher=is_pitcher,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Snapshot build failed for %s: %s", rec.get("name"), exc)
            return None

    def compute(self, rec: Dict[str, Any]) -> Optional[int]:
        """Return annualized fair value in dollars for a FA record, or None."""
        snap = self._snapshot(rec)
        if snap is None:
            return None
        try:
            val = ct.run_valuation(
                snap=snap, vpc=self.vpc, vpc_sample=self.vpc_n,
                years=self.aav_years, contract_type="market",
                arb_years=0, pre_arb_years=0,
                cfg=self.cfg, rounding=self.rounding,
            )
            return val.total_fair_value // self.aav_years
        except Exception as exc:  # noqa: BLE001
            logger.debug("AAV compute failed for %s: %s", rec.get("name"), exc)
            return None

    def suggest_contract(
        self,
        rec: Dict[str, Any],
        max_years: int = 7,
        floor_ratio: float = 0.85,
    ) -> Tuple[Optional[int], Optional[int], Optional[int]]:
        """Pick a suggested contract length using a decline-cutoff heuristic.

        Algorithm:
          1. Project value out to ``max_years`` (one run_valuation call).
          2. The longest L in 1..max_years where year-L's projected fair value
             is at least ``floor_ratio`` × year-1's is the suggested length.
             ("Don't offer years where the player has clearly fallen off.")
          3. Re-run valuation at that L to get the actual market-priced AAV
             (the totals at length L differ from a slice of the max-year run
             because of risk discount rounding).

        Returns (years, aav, total). All None when the math couldn't run.
        Year-1 floor: if rec's age makes year-1 value already 0 or negative
        (deeply post-peak), we suggest 1 yr at whatever the model produces —
        never zero — since a 0-year contract isn't a meaningful output.
        """
        snap = self._snapshot(rec)
        if snap is None:
            return None, None, None
        try:
            max_years = max(1, int(max_years))
            # Long projection to read off the per-year curve cheaply.
            long_val = ct.run_valuation(
                snap=snap, vpc=self.vpc, vpc_sample=self.vpc_n,
                years=max_years, contract_type="market",
                arb_years=0, pre_arb_years=0,
                cfg=self.cfg, rounding=self.rounding,
            )
            rows = long_val.rows
            y1 = float(rows[0].fair_value) if rows else 0.0
            if y1 <= 0:
                suggested = 1  # degenerate case — see docstring
            else:
                threshold = y1 * float(floor_ratio)
                suggested = 1
                for i, r in enumerate(rows, start=1):
                    if float(r.fair_value) >= threshold:
                        suggested = i
                    else:
                        break  # decline is monotonic post-peak; stop scanning
            # Re-price at the actual suggested length for correct rounding/AAV.
            val = ct.run_valuation(
                snap=snap, vpc=self.vpc, vpc_sample=self.vpc_n,
                years=suggested, contract_type="market",
                arb_years=0, pre_arb_years=0,
                cfg=self.cfg, rounding=self.rounding,
            )
            total = int(val.total_fair_value)
            aav = total // suggested if suggested > 0 else None
            return suggested, aav, total
        except Exception as exc:  # noqa: BLE001
            logger.debug("Suggest-contract failed for %s: %s", rec.get("name"), exc)
            return None, None, None


def _build_aav_context_or_none(
    args: argparse.Namespace,
    eval_rows: List[Dict[str, str]],
    cache_dir: Optional[Path],
) -> Optional[AAVContext]:
    """Wrap AAVContext construction with the user's opt-out flag and a final
    safety net so a missing config or broken /players never crashes the run."""
    if args.no_aav:
        return None
    if not args.contract_config.exists():
        logger.warning("AAV: contract config not found at %s — skipping AAV column.", args.contract_config)
        return None
    try:
        return AAVContext(
            eval_rows=eval_rows,
            contract_config_path=args.contract_config,
            league=args.league,
            league_url_config=args.league_url_config,
            base_url=args.base_url,
            cache_dir=cache_dir,
            aav_years=args.aav_years,
            diagnostic=bool(getattr(args, "vpc_diagnostic", False)),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("AAV: failed to build context (%s); skipping AAV column.", exc)
        return None


def _attach_aav(
    records: List[Dict[str, Any]],
    aav_ctx: Optional[AAVContext],
    suggest_length: bool = True,
    suggest_max_years: int = 7,
    suggest_floor_ratio: float = 0.6,
) -> None:
    """Set ``fair_aav`` (and optionally ``suggested_years`` / ``suggested_aav``
    / ``suggested_total``) on each record in place. All None when ctx is
    unavailable so downstream renderers can safely call _fmt_aav / format
    helpers without special-casing.
    """
    if aav_ctx is None:
        for r in records:
            r["fair_aav"] = None
            r["suggested_years"] = None
            r["suggested_aav"] = None
            r["suggested_total"] = None
        return
    for r in records:
        r["fair_aav"] = aav_ctx.compute(r)
        if suggest_length:
            yrs, aav, total = aav_ctx.suggest_contract(
                r, max_years=suggest_max_years, floor_ratio=suggest_floor_ratio,
            )
            r["suggested_years"] = yrs
            r["suggested_aav"] = aav
            r["suggested_total"] = total
        else:
            r["suggested_years"] = None
            r["suggested_aav"] = None
            r["suggested_total"] = None


def _fmt_aav(v: Optional[int]) -> str:
    """Format dollars for table cells. Short form (e.g. '$8.4M') keeps rows tight."""
    if v is None or v <= 0:
        return "—"
    if v >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v / 1_000:.0f}K"
    return f"${v:,}"


def _fmt_years(v: Optional[int]) -> str:
    """Years cell for table output. Em-dash when suggestion couldn't be computed."""
    return "—" if v is None else str(int(v))


# -----------------------------------------------------------------------------
# Multi-level depth-chart discovery
# -----------------------------------------------------------------------------

def discover_starter_gaps_sidecar(
    depth_dir: Path, org_slug: str, level: str,
) -> Optional[Path]:
    """Return the newest ``{org}_{level}_{ts}_starter_gaps.json`` sidecar for
    this org/level, or None if no depth_chart run with --min-comp has been
    persisted yet. Picks the latest timestamp so re-runs are picked up
    automatically.
    """
    pattern = f"{org_slug}_{level}_*_starter_gaps.json"
    candidates = sorted(depth_dir.glob(pattern))
    if not candidates:
        return None
    return candidates[-1]


def load_starter_gaps_sidecar(path: Path) -> Dict[str, Any]:
    """Read a starter_gaps sidecar. Returns the parsed dict, or {} on error."""
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Couldn't read sidecar %s: %s", path, exc)
        return {}


def resolve_starter_thresholds(
    args: argparse.Namespace,
    depth_dir: Path,
    org_slug: str,
    level: str,
) -> Tuple[Dict[str, float], List[Dict[str, Any]], Optional[Path]]:
    """Merge sidecar + CLI overrides into a (thresholds, empty_slots, src_path)
    tuple. CLI flags win where set.

    - thresholds: ``{pos: float}`` for any position with a threshold.
    - empty_slots: list of ``{pos, would_be_*, threshold, gap}`` from sidecar
      (or empty when no sidecar exists — CLI-only mode can't know what your
      depth chart looks like without re-running it).
    - src_path: sidecar path if one was loaded, else None.
    """
    sidecar_path = discover_starter_gaps_sidecar(depth_dir, org_slug, level)
    sidecar_data: Dict[str, Any] = {}
    if sidecar_path:
        sidecar_data = load_starter_gaps_sidecar(sidecar_path)

    # Layered precedence (lowest → highest):
    #   sidecar.thresholds (already the merged result from depth_chart)
    #   CLI --min-comp (global, fills every position when set)
    #   CLI --min-comp-pos (per-position, highest)
    thresholds: Dict[str, float] = {
        pos: float(v) for pos, v in (sidecar_data.get("thresholds") or {}).items()
    }
    if args.min_comp is not None:
        for pos in HITTER_POSITIONS:
            thresholds[pos] = float(args.min_comp)
    try:
        per_pos_cli = dc.parse_min_comp_pos(args.min_comp_pos)
    except ValueError as exc:
        logger.error("%s", exc)
        per_pos_cli = {}
    for pos, val in per_pos_cli.items():
        thresholds[pos] = float(val)

    empty_slots = list(sidecar_data.get("empty_slots") or [])
    return thresholds, empty_slots, sidecar_path


def compute_empty_slots(
    starters_by_pos: Dict[str, Optional[Dict[str, Any]]],
    thresholds: Dict[str, float],
) -> List[Dict[str, Any]]:
    """Derive empty-slot entries from in-memory state. Used when no sidecar is
    available (e.g. user passed --min-comp on the FA CLI without re-running
    depth_chart) or when CLI overrides changed thresholds since the sidecar
    was written.
    """
    empty: List[Dict[str, Any]] = []
    for pos, starter in starters_by_pos.items():
        thr = thresholds.get(pos)
        if thr is None:
            continue
        comp = float(starter.get("composite", 0.0)) if starter else 0.0
        if not starter or comp < thr:
            empty.append({
                "pos": pos,
                "would_be_pid": (starter or {}).get("pid", ""),
                "would_be_name": (starter or {}).get("name", "(none)"),
                "would_be_composite": comp,
                "threshold": float(thr),
                "gap": float(thr) - comp,
            })
    return empty


def discover_latest_depth_batch(depth_dir: Path, org_slug: str) -> Optional[str]:
    """Find the most recent timestamp where this org has depth chart files."""
    timestamps: set = set()
    for path in depth_dir.glob(f"{org_slug}_*_*.csv"):
        m = DEPTH_CHART_FILE_RE.match(path.name)
        if m and m.group("org") == org_slug:
            timestamps.add(m.group("ts"))
    if not timestamps:
        return None
    return sorted(timestamps)[-1]


def read_depth_meta(depth_dir: Path, org_slug: str, ts: str) -> Optional[Dict[str, Any]]:
    """Read the ``{org}_{ts}_depth_meta.json`` provenance sidecar for a batch,
    or None if it doesn't exist / can't be parsed (legacy batches predating the
    sidecar)."""
    p = depth_dir / f"{org_slug}_{ts}_depth_meta.json"
    if not p.exists():
        return None
    try:
        with p.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Couldn't read depth meta %s: %s", p, exc)
        return None


def depth_batch_source_eval_ts(depth_dir: Path, org_slug: str, ts: str) -> Optional[str]:
    """Best-effort timestamp of the eval a depth batch was built from.

    Prefers the per-batch meta sidecar; falls back to any per-level
    starter_gaps sidecar from the same batch (depth_chart runs that had
    --min-comp but predate the meta sidecar). Returns None when no provenance
    exists at all — caller treats that as stale and regenerates.
    """
    meta = read_depth_meta(depth_dir, org_slug, ts)
    if meta and meta.get("source_eval_ts"):
        return meta["source_eval_ts"]
    for p in sorted(depth_dir.glob(f"{org_slug}_*_{ts}_starter_gaps.json")):
        data = dc.load_starter_gaps_sidecar(p)
        if data.get("source_eval_ts"):
            return data["source_eval_ts"]
    return None


def regenerate_depth_batch(
    args: argparse.Namespace,
    depth_dir: Path,
    org_slug: str,
    target_year: int,
    prior_meta: Optional[Dict[str, Any]],
) -> Optional[str]:
    """Re-run depth_chart.py (subprocess) against the latest eval and return the
    new batch timestamp, or None on failure.

    Mirrors run_depth_chart_all.py's subprocess pattern (no in-process coupling
    to depth_chart's batch state). Forces ``--org-code org_slug`` so the
    regenerated files carry the exact slug FA discovers by, and replays the
    starter min-comp thresholds from the prior batch's meta sidecar (falling
    back to this run's --min-comp/--min-comp-pos) so the gap analysis survives.
    """
    cmd = [
        sys.executable, str(SCRIPT_DIR / "depth_chart.py"),
        "--league", args.league,
        "--org", args.org,
        "--org-code", org_slug,
        "--year", str(target_year),
        "--all-level-charts",
        "--no-pdf",
    ]
    pf = args.park_factors or dc._default_park_factors_path(args.league)
    if pf and Path(pf).exists():
        cmd += ["--park-factors", str(pf)]

    # Replay thresholds: prior batch's recorded settings win; else this run's CLI.
    g_min = (prior_meta or {}).get("min_comp_global")
    pp_min = (prior_meta or {}).get("min_comp_per_pos") or {}
    if g_min is None and pp_min == {}:
        g_min = args.min_comp
        if args.min_comp_pos:
            try:
                pp_min = dc.parse_min_comp_pos(args.min_comp_pos)
            except ValueError:
                pp_min = {}
    if g_min is not None:
        cmd += ["--min-comp", str(g_min)]
    if pp_min:
        cmd += ["--min-comp-pos", ",".join(f"{k}:{v:g}" for k, v in pp_min.items())]

    logger.info("Regenerating depth charts from latest eval: $ %s", " ".join(cmd))
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        logger.error("depth_chart regeneration failed (rc=%d).", rc)
        return None
    return discover_latest_depth_batch(depth_dir, org_slug)


def ensure_fresh_depth_batch(
    args: argparse.Namespace,
    depth_dir: Path,
    org_slug: str,
    target_year: int,
) -> Optional[str]:
    """Return a depth-batch timestamp guaranteed to reflect the latest eval.

    Compares the latest depth batch's source-eval timestamp against the current
    latest eval. If the batch is older, has no provenance (legacy), or doesn't
    exist at all, regenerates via depth_chart.py first. ``--no-auto-refresh``
    skips regeneration and just warns. Returns None only when there's no batch
    and regeneration is disabled or fails.
    """
    try:
        latest_eval = dc.find_latest_eval(args.league, args.input, args.org_code)
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return discover_latest_depth_batch(depth_dir, org_slug)
    latest_eval_ts = dc.eval_ts_from_path(latest_eval)

    ts = discover_latest_depth_batch(depth_dir, org_slug)
    prior_meta = read_depth_meta(depth_dir, org_slug, ts) if ts else None

    if ts is None:
        reason = f"no depth batch on disk for '{org_slug}'"
    else:
        source_ts = depth_batch_source_eval_ts(depth_dir, org_slug, ts)
        if source_ts is None:
            reason = f"depth batch {ts} has no eval provenance (legacy)"
        elif latest_eval_ts and source_ts < latest_eval_ts:
            reason = (f"depth batch {ts} built from eval {source_ts}, "
                      f"but latest eval is {latest_eval_ts}")
        else:
            logger.info("Depth batch %s is current with eval %s.", ts, latest_eval_ts)
            return ts

    if getattr(args, "no_auto_refresh", False):
        logger.warning("Stale depth charts (%s) - --no-auto-refresh set, scanning as-is.", reason)
        return ts

    logger.info("Stale depth charts (%s) - regenerating from latest eval %s.",
                reason, latest_eval.name)
    new_ts = regenerate_depth_batch(args, depth_dir, org_slug, target_year, prior_meta)
    if new_ts is None:
        logger.error("Regeneration failed; falling back to existing batch (%s).", ts or "none")
        return ts
    return new_ts


def levels_in_batch(depth_dir: Path, org_slug: str, ts: str) -> List[Tuple[str, str]]:
    """Return [(display_level, base_level), ...] ordered top-down for the batch.

    ``display_level`` is what shows in filenames/output (e.g. 'R-ACL').
    ``base_level`` is the canonical depth_config key (e.g. 'R'). For
    affiliate-suffixed levels they differ; otherwise they match.
    """
    out: List[Tuple[str, str]] = []
    for path in depth_dir.glob(f"{org_slug}_*_{ts}.csv"):
        m = DEPTH_CHART_FILE_RE.match(path.name)
        if not m or m.group("org") != org_slug:
            continue
        display_level = m.group("level")
        base_level = display_level.split("-", 1)[0] if display_level.startswith("R-") else display_level
        out.append((display_level, base_level))
    out.sort(key=lambda x: LEVEL_RANK.get(x[0], 99))
    return out


def load_depth_chart_starters(
    csv_path: Path,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, List[Dict[str, Any]]]]:
    """Read a depth chart CSV. Returns (hitter_starters_by_pos, pitcher_slots_by_role).

    hitter_starters_by_pos: rank-1 player at each hitter position
        ``{C: {name, composite, age, ...}}``
    pitcher_slots_by_role: every slotted pitcher grouped by role
        ``{SP: [{...}, ...], CL: [...], SU: [...], MR: [...], LR: [...]}``
    """
    hitter_starters: Dict[str, Dict[str, Any]] = {}
    pitcher_slots: Dict[str, List[Dict[str, Any]]] = {role: [] for role in PITCHER_ROLES}
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            tier = (row.get("tier") or "").strip()
            if not tier:
                continue
            is_pitcher = (row.get("is_pitcher") or "").strip().lower() == "true"

            def _row_to_dict(r: Dict[str, str]) -> Dict[str, Any]:
                # stat_score is in the depth_chart CSV — pull it through so the
                # upgrade table can decompose total edge into rating vs stat
                # components. vos_tier is NOT in the CSV; the caller enriches
                # it from the in-memory org record via pid lookup (see
                # run_multilevel's starter-enrichment pass).
                return {
                    "pid": r.get("pid", ""),
                    "name": r.get("name", ""),
                    "age": r.get("age", ""),
                    "primary_pos": r.get("primary_pos", ""),
                    "tier": tier,
                    "vos": float(r.get("vos") or 0.0),
                    "stat_score": float(r.get("stat_score") or 0.0),
                    "composite": float(r.get("composite") or 0.0),
                }

            if is_pitcher:
                m = re.match(r"^(SP)(\d+)$", tier)
                if m:
                    pitcher_slots["SP"].append(_row_to_dict(row))
                    continue
                m = re.match(r"^(CL|SU|MR|LR)-(\d+)$", tier)
                if m:
                    pitcher_slots[m.group(1)].append(_row_to_dict(row))
            else:
                m = re.match(r"^([A-Z0-9]+)-(\d+)$", tier)
                if m and int(m.group(2)) == 1:
                    hitter_starters[m.group(1)] = _row_to_dict(row)
    return hitter_starters, pitcher_slots


# -----------------------------------------------------------------------------
# Output: Markdown rendering
# -----------------------------------------------------------------------------

def _fmt_delta(p: Dict[str, Any]) -> str:
    """Format the Δ Stat-VOS cell. Signed number plus ⚠ when flagged.

    Suppressed entirely (renders '—') when the FA has a near-zero sample
    weight, because in that case stat_score is the fallback 50.0 — the value
    isn't actually computed from anything, so showing 'Δ=-16.4' would falsely
    look like a regression candidate. The 0.25 floor is below the flag
    threshold of 0.5; lets borderline samples show their value without
    triggering the ⚠ flag.
    """
    d = p.get("delta_stat_vos")
    if d is None:
        return "—"
    sw = float(p.get("sample_weight") or 0.0)
    if sw < 0.25:
        return "—"
    marker = " ⚠" if p.get("stat_vos_flagged") else ""
    sign = "+" if d > 0 else ""
    return f"{sign}{d:.1f}{marker}"


def _fmt_babip(v: Optional[float]) -> str:
    """OOTP BABIP/PBABIP ratings are 1-100 (or 20-80 depending on scale).
    Pass through as an integer for visual scan; '—' when missing."""
    if v is None:
        return "—"
    return f"{int(round(v))}"


def _fmt_prone(s: str) -> str:
    """Prone passthrough. Empty -> '—'. Truncate long labels to keep tables
    readable (Wrecked / Fragile / Normal / Durable / Iron Man — all under 8 char)."""
    return s if s else "—"


def _fmt_last_level(p: Dict[str, Any]) -> str:
    """Last-level cell — shows the player's last-played level, plus a level
    mismatch indicator ('❗L') when most of their recent PA/IP came from a
    different level (single-level mode only; multi-level can't make this
    distinction so the flag never fires). Empty -> '-' for visual scan."""
    lvl = (p.get("last_level") or "").strip() or "-"
    if p.get("target_level_mismatch"):
        share = p.get("target_level_share")
        if share is not None:
            return f"{lvl} ❗{share * 100:.0f}%"
        return f"{lvl} ❗"
    return lvl


def _fmt_rating_only_badge(p: Dict[str, Any]) -> str:
    """Compact badge for rating-only FAs (sample_weight below threshold).
    Use in cells where space is tight; empty string when not rating-only so
    the cell stays clean."""
    return " 📊" if p.get("rating_only") else ""


def _hitter_row(p: Dict[str, Any]) -> str:
    hb = (p.get("hitter_bundle") or {}).get("overall") or {}
    pa = float(hb.get("PA", 0))
    woba = float(hb.get("wOBA", 0))
    ops = float(hb.get("OPS", 0))
    last_lvl = _fmt_last_level(p)
    aav = _fmt_aav(p.get("fair_aav"))
    sugg_yrs = _fmt_years(p.get("suggested_years"))
    sugg_aav = _fmt_aav(p.get("suggested_aav"))
    sugg_tot = _fmt_aav(p.get("suggested_total"))
    tier = p.get("vos_tier") or "—"
    delta = _fmt_delta(p)
    babip = _fmt_babip(p.get("babip"))
    pot_babip = _fmt_babip(p.get("pot_babip"))
    prone = _fmt_prone(p.get("prone", ""))
    name = f"{p['name']}{_fmt_rating_only_badge(p)}"
    return (
        f"| {name} | {p.get('age','')} | {p.get('primary_pos','')} | {last_lvl} | "
        f"{tier} | {p['vos']:.1f} | {p.get('stat_score', 50.0):.1f} | {delta} | "
        f"{p['composite']:.1f} | "
        f"{aav} | {sugg_yrs} | {sugg_aav} | {sugg_tot} | "
        f"{pa:.0f} | {woba:.3f} | {ops:.3f} | "
        f"{babip} | {pot_babip} | {prone} |"
    )


def _pitcher_row(p: Dict[str, Any]) -> str:
    pb = (p.get("pitcher_bundle") or {}).get("overall") or {}
    pb_cur = (p.get("pitcher_bundle") or {}).get("current") or {}
    ip = float(pb.get("IP", 0))
    fip = float(pb.get("FIP", 0))
    fip_cur = float(pb_cur.get("FIP", 0)) if pb_cur else 0.0
    k_bb = float(pb.get("K-BB%", 0))
    last_lvl = _fmt_last_level(p)
    aav = _fmt_aav(p.get("fair_aav"))
    sugg_yrs = _fmt_years(p.get("suggested_years"))
    sugg_aav = _fmt_aav(p.get("suggested_aav"))
    sugg_tot = _fmt_aav(p.get("suggested_total"))
    tier = p.get("vos_tier") or "—"
    delta = _fmt_delta(p)
    pbabip = _fmt_babip(p.get("pbabip"))
    pot_pbabip = _fmt_babip(p.get("pot_pbabip"))
    prone = _fmt_prone(p.get("prone", ""))
    name = f"{p['name']}{_fmt_rating_only_badge(p)}"
    return (
        f"| {name} | {p.get('age','')} | {p.get('proj_role','RP')} | {last_lvl} | "
        f"{tier} | {p['vos']:.1f} | {p.get('stat_score', 50.0):.1f} | {delta} | "
        f"{p['composite']:.1f} | "
        f"{aav} | {sugg_yrs} | {sugg_aav} | {sugg_tot} | "
        f"{ip:.0f} | {fip:.2f} | {fip_cur:.2f} | {k_bb*100:.1f}% | "
        f"{pbabip} | {pot_pbabip} | {prone} |"
    )


def _rank_fas_for_position(
    pos: str,
    fa_hitters: List[Dict[str, Any]],
    top_n: int,
) -> List[Tuple[float, Dict[str, Any]]]:
    """Top-N FA hitters viable at ``pos`` (raw pos_score > 0), ranked by
    blended pos score at that position. Returns (score_at_pos, player) tuples.
    """
    scored: List[Tuple[float, Dict[str, Any]]] = []
    for p in fa_hitters:
        raw = (p.get("pos_scores") or {}).get(pos, 0.0)
        if raw <= 0:
            continue
        blended = (p.get("pos_scores_blended") or {}).get(pos, 0.0)
        if blended <= 0:
            continue
        scored.append((blended, p))
    scored.sort(key=lambda t: -t[0])
    return scored[: max(1, int(top_n))]


def render_empty_slot_fa_section(
    empty_slots: List[Dict[str, Any]],
    fa_hitters: List[Dict[str, Any]],
    top_n: int,
    heading_level: int = 2,
    src_note: Optional[str] = None,
) -> List[str]:
    """Markdown lines for the 'Empty Starter Slots — FA Candidates' section.

    One sub-block per empty slot; top ``top_n`` FAs at the position ranked by
    blended pos score. A ✓ marks FAs whose pos score clears the threshold
    (i.e. would qualify as starter), so the user can tell at a glance whether
    a sign actually solves the gap or just shrinks it.

    Slot ordering: sorted by ``gap`` descending so the biggest holes (most
    pressing FA needs) appear at the top of the section. Pure gap rather than
    a fixability-weighted score — see the function's commit notes / discussion
    for the reasoning.
    """
    if not empty_slots:
        return []
    h = "#" * heading_level
    out: List[str] = []
    out.append(f"{h} Empty Starter Slots — FA Candidates")
    out.append("")
    note = ("_Top FA hitters at each empty starter slot, ranked by blended pos "
            "score. ✓ = FA's pos score clears the --min-comp threshold "
            "(qualifies as a starter). Slots ordered biggest-gap-first so the "
            "most pressing needs lead._")
    out.append(note)
    if src_note:
        out.append(f"_{src_note}_")
    out.append("")

    # Sort by gap descending — biggest hole first. Tie-break by position name
    # for stable output across runs when gaps are identical.
    ordered_slots = sorted(
        empty_slots,
        key=lambda s: (-float(s.get("gap", 0.0)), str(s.get("pos", ""))),
    )
    for slot in ordered_slots:
        pos = slot["pos"]
        threshold = float(slot["threshold"])
        wb_name = slot.get("would_be_name") or "(none)"
        wb_comp = float(slot.get("would_be_composite", 0.0))
        gap = float(slot.get("gap", threshold - wb_comp))
        out.append(
            f"**{pos}** — would-be starter: {wb_name} "
            f"(comp {wb_comp:.1f}, needs {threshold:g}, short by {gap:.1f})"
        )
        out.append("")

        ranked = _rank_fas_for_position(pos, fa_hitters, top_n)
        if not ranked:
            out.append("_No FA viable at this position in the current pool._")
            out.append("")
            continue

        out.append("| # | Name | Age | Primary | Last Lvl | Pos Score @ Pos | Composite | Clears? | Fair AAV | Sugg Yrs | Sugg AAV | Sugg Total |")
        out.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for i, (pos_score, p) in enumerate(ranked, start=1):
            clears = "✓" if pos_score >= threshold else ""
            out.append(
                f"| {i} | {p['name']} | {p.get('age','')} | "
                f"{p.get('primary_pos','')} | {p.get('last_level') or '-'} | "
                f"{pos_score:.1f} | {p.get('composite', 0.0):.1f} | "
                f"{clears} | {_fmt_aav(p.get('fair_aav'))} | "
                f"{_fmt_years(p.get('suggested_years'))} | "
                f"{_fmt_aav(p.get('suggested_aav'))} | "
                f"{_fmt_aav(p.get('suggested_total'))} |"
            )
        out.append("")
    return out


def render_md(
    league: str, org: str, level: str, year: int,
    fa_hitters_sorted: List[Dict[str, Any]],
    fa_pitchers_sorted: List[Dict[str, Any]],
    starters_by_pos: Dict[str, Optional[Dict[str, Any]]],
    pitcher_slots: Dict[str, List[Dict[str, Any]]],
    top_n_h: int, top_n_p: int,
    aav_years: int = 3,
    empty_slots: Optional[List[Dict[str, Any]]] = None,
    gap_fa_top_n: int = 3,
    threshold_source_note: Optional[str] = None,
) -> str:
    out: List[str] = []
    out.append(f"# Free Agent Market — {org} ({level})  ·  {league.upper()}  ·  {year}")
    out.append("")
    out.append("_Composites computed at the chosen level's weights from `depth_config.json`._")
    out.append(f"_Fair AAV: market-only VPC × age-projected VOS over a {aav_years}-yr term "
               "(see contract_config.json). Override with `--aav-years`._")
    out.append("")

    # Top section: empty starter slots (FA shopping list). Only renders when a
    # threshold was provided AND at least one slot didn't have a qualified
    # starter — the gap-shopping use case from depth_chart's --min-comp flow.
    out.extend(render_empty_slot_fa_section(
        empty_slots or [], fa_hitters_sorted, gap_fa_top_n,
        heading_level=2, src_note=threshold_source_note,
    ))

    # Section 1: hitters by composite
    out.append(f"## Free Agent Hitters (top {min(top_n_h, len(fa_hitters_sorted))} by composite)")
    out.append("")
    out.append("_Tier = v10 VOS_Tier (Career). Δ = stat_score − VOS; ⚠ when |Δ| ≥ 10 with usable sample "
               "(regression candidate when negative, overperformer when positive — v10's BABIP features "
               "are the underlying signal). 📊 = rating-only (no recent stats; composite is rating VOS). "
               "❗L% on Last Lvl = bulk of recent PA came from a different level. "
               "BABIP/Pot = current/potential BABIP ratings. Prone = injury proneness rating._")
    out.append("")
    out.append("| Name | Age | Pos | Last Lvl | Tier | VOS | Stat | Δ | Composite | Fair AAV | Sugg Yrs | Sugg AAV | Sugg Total | PA | wOBA | OPS | BABIP | Pot | Prone |")
    out.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for p in fa_hitters_sorted[:top_n_h]:
        out.append(_hitter_row(p))
    if not fa_hitters_sorted:
        out.append("| _(none)_ |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |")
    out.append("")

    # Section 2: pitchers by composite
    out.append(f"## Free Agent Pitchers (top {min(top_n_p, len(fa_pitchers_sorted))} by composite)")
    out.append("")
    out.append("_Same legend as hitters (Tier/Δ/⚠/📊/❗L%). PBABIP/Pot = current/potential pitcher BABIP-against "
               "rating (higher = worse). A low Δ with high PotPBABIP is a buy-low candidate when v10 says the "
               "underlying skill is fine._")
    out.append("")
    out.append("| Name | Age | Role | Last Lvl | Tier | VOS | Stat | Δ | Composite | Fair AAV | Sugg Yrs | Sugg AAV | Sugg Total | IP | FIP | FIP (cur) | K-BB% | PBABIP | Pot | Prone |")
    out.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for p in fa_pitchers_sorted[:top_n_p]:
        out.append(_pitcher_row(p))
    if not fa_pitchers_sorted:
        out.append("| _(none)_ |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |")
    out.append("")

    # Section 3: hitter upgrades vs starters — cross-position aware.
    # A FA can show up at any position where their pos_scores_blended is viable
    # (>0) AND beats the slotted starter's score at that same position. Means a
    # FA listed as primary 3B can surface as a 2B upgrade if their 2B blended
    # score beats your 2B starter's 2B score.
    #
    # Edge decomposition: total edge = rating edge + stat edge. Lets the user
    # see *why* the FA beats the slot — pure rating jump (depth fix) vs stat
    # surge (timing/luck/regression candidate). Tier delta column makes the
    # severity of the upgrade visible at a glance (Bench → Star is more
    # urgent than Above-Avg → Star).
    out.extend(_render_hitter_upgrade_table(
        starters_by_pos, fa_hitters_sorted, heading_level=2,
    ))

    # Section 4: pitcher upgrades vs weakest in role
    out.extend(_render_pitcher_upgrade_table(
        pitcher_slots, fa_pitchers_sorted, heading_level=2,
    ))
    return "\n".join(out)


# -----------------------------------------------------------------------------
# Upgrade-table renderers (shared between single-level and multi-level paths)
# -----------------------------------------------------------------------------


def _slot_pos_score(starter: Optional[Dict[str, Any]], pos: str) -> float:
    """Recover the starter's per-position blended score. Falls back to
    composite when the blended map isn't populated (older callers); returns
    0.0 for empty slots."""
    if not starter:
        return 0.0
    score = (starter.get("pos_scores_blended") or {}).get(pos)
    if score and score > 0:
        return float(score)
    return float(starter.get("composite", 0.0))


def pro_service_days_of(pid: str, players_lookup: Dict[str, Dict[str, Any]]) -> Optional[int]:
    """Pro service days for a player id from a /players lookup, or None when the
    player isn't in /players or the field is blank/unparseable. None means
    'unknown' — callers treat it as not-yet-a-pro for the service-time gate."""
    meta = players_lookup.get((pid or "").strip())
    if not meta:
        return None
    raw = (meta.get("pro_service_days") or "").strip()
    if not raw:
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def score_fa_records(
    eval_rows: List[Dict[str, str]],
    level_cfg: Dict[str, Any],
    floors: Dict[str, float],
    *,
    hitters_stats: Optional[Dict[str, Dict[str, Any]]] = None,
    pitchers_stats: Optional[Dict[str, Dict[str, Any]]] = None,
    means_stds: Optional[Dict[str, Any]] = None,
    players_lookup: Optional[Dict[str, Dict[str, Any]]] = None,
    min_pro_service_days: int = 0,
    exclude_retired: bool = True,
) -> List[Dict[str, Any]]:
    """Score the free-agent pool (eval rows with no Org) at one level, in-process.

    Ratings-only by default (empty stat bundles + empty z-score context) — fully
    offline, matching the depth UI page's default. Pass ``hitters_stats`` /
    ``pitchers_stats`` and a ``means_stds`` dict (keys: p_means, p_stds, h_means,
    h_stds, h_means_l, h_stds_l, h_means_r, h_stds_r) to blend in-season form.

    ``players_lookup`` (the /players payload) drives two eligibility filters that
    the eval CSV alone can't express, since unsigned amateurs and pro FAs both
    have no Org:
      • ``exclude_retired`` drops Retired=1 players.
      • ``min_pro_service_days`` drops anyone below that many days of pro service
        — including amateurs absent from /players (treated as 0). Matches the
        CLI scan's conservative semantics (unknown ⇒ excluded). Only applied when
        a non-empty ``players_lookup`` is supplied; with no /players data the
        gate is skipped (can't verify), so the caller should warn.

    Every record carries ``pro_service_days`` (int or None) for downstream use.
    Returns enriched player records. No file IO — reusable by the UI and CLI.
    """
    hs = hitters_stats or {}
    ps = pitchers_stats or {}
    ms = means_stds or {}
    pl = players_lookup or {}

    def _g(k: str) -> Dict[str, float]:
        return ms.get(k, {}) or {}

    out: List[Dict[str, Any]] = []
    for r in fa_pool(eval_rows):
        pid = (r.get("ID") or "").strip()
        if pl and exclude_retired and dc._bool_from_value(pl.get(pid, {}).get("retired")):
            continue
        rec = dc.build_player_record(
            r, ps, hs, level_cfg, floors,
            _g("p_means"), _g("p_stds"), _g("h_means"), _g("h_stds"),
            _g("h_means_l"), _g("h_stds_l"), _g("h_means_r"), _g("h_stds_r"),
        )
        rec["last_level"] = (r.get("League_Level") or "").strip()
        rec["pro_service_days"] = pro_service_days_of(pid, pl)
        _attach_v10_passthrough(rec, r)
        out.append(rec)

    # Service-time gate — only meaningful when we actually have /players data.
    if pl and min_pro_service_days > 0:
        out = [rec for rec in out
               if rec["pro_service_days"] is not None
               and rec["pro_service_days"] >= min_pro_service_days]
    return out


def compute_fa_fit(
    starters_by_pos: Dict[str, Optional[Dict[str, Any]]],
    pitcher_slots: Dict[str, List[Dict[str, Any]]],
    fa_hitters: List[Dict[str, Any]],
    fa_pitchers: List[Dict[str, Any]],
    thresholds: Dict[str, float],
    top_n: int = 3,
) -> Dict[str, List[Dict[str, Any]]]:
    """Biggest-holes-first FA targeting as plain data (no IO, no markdown).

    A hitter position / pitcher role is a *hole* when its starter (or weakest
    slotted pitcher) scores below the threshold for that slot; ``gap =
    threshold - slot_score``. Holes are returned sorted by gap descending — your
    biggest need first — each with the top-``top_n`` best-fit free agents.

    Hitter fit uses the per-position blended score (``pos_scores_blended[pos]``);
    pitcher fit uses ``composite`` within the SP/RP role bucket. Slots with no
    threshold entry are skipped. Shared by the CLI and the in-process UI page.
    """
    def _fa_entry(p: Dict[str, Any], fit_score: float, slot_score: float) -> Dict[str, Any]:
        return {
            "pid": p.get("pid", ""),
            "name": p.get("name", ""),
            "age": p.get("age", ""),
            "last_level": p.get("last_level", ""),
            "fit_score": float(fit_score),
            "vos": float(p.get("vos", 0.0) or 0.0),
            "vos_tier": p.get("vos_tier", "") or "",
            "edge": float(fit_score) - float(slot_score),
            "fair_aav": p.get("fair_aav"),
        }

    hitter_holes: List[Dict[str, Any]] = []
    for pos in HITTER_POSITIONS:
        thr = thresholds.get(pos)
        if thr is None:
            continue
        starter = starters_by_pos.get(pos)
        slot_score = _slot_pos_score(starter, pos)
        if slot_score >= float(thr):
            continue
        ranked = sorted(
            (p for p in fa_hitters if (p.get("pos_scores_blended") or {}).get(pos, 0.0) > 0),
            key=lambda p: -(p.get("pos_scores_blended") or {}).get(pos, 0.0),
        )[:top_n]
        hitter_holes.append({
            "pos": pos,
            "starter_name": starter["name"] if starter else "(empty)",
            "slot_score": float(slot_score),
            "threshold": float(thr),
            "gap": float(thr) - float(slot_score),
            "fas": [_fa_entry(p, (p.get("pos_scores_blended") or {}).get(pos, 0.0), slot_score)
                    for p in ranked],
        })

    pitcher_holes: List[Dict[str, Any]] = []
    for role in PITCHER_ROLES:
        thr = thresholds.get(role)
        if thr is None:
            continue
        slots = pitcher_slots.get(role, [])
        weakest = min(slots, key=lambda r: r["composite"]) if slots else None
        slot_comp = float(weakest["composite"]) if weakest else 0.0
        if weakest is not None and slot_comp >= float(thr):
            continue
        fa_role = "RP" if role in {"CL", "SU", "MR", "LR"} else "SP"
        ranked = sorted(
            (p for p in fa_pitchers if p.get("proj_role", "") == fa_role),
            key=lambda p: -float(p.get("composite", 0.0) or 0.0),
        )[:top_n]
        pitcher_holes.append({
            "role": role,
            "starter_name": weakest["name"] if weakest else "(empty)",
            "slot_score": slot_comp,
            "threshold": float(thr),
            "gap": float(thr) - slot_comp,
            "fas": [_fa_entry(p, float(p.get("composite", 0.0) or 0.0), slot_comp)
                    for p in ranked],
        })

    hitter_holes.sort(key=lambda hgap: -hgap["gap"])
    pitcher_holes.sort(key=lambda hgap: -hgap["gap"])
    return {"hitter_holes": hitter_holes, "pitcher_holes": pitcher_holes}


def _render_hitter_upgrade_table(
    starters_by_pos: Dict[str, Optional[Dict[str, Any]]],
    fa_hitters_sorted: List[Dict[str, Any]],
    heading_level: int = 2,
    level_label: Optional[str] = None,
) -> List[str]:
    """Hitter upgrade table — tier-aware with rating/stat edge decomposition.

    Output columns:
      Pos | Your Starter | Starter Tier | Best FA | FA Tier | Tier Jump |
      Slot Score | FA Score | Rating Edge | Stat Edge | Total Edge | Fair AAV
    """
    h = "#" * heading_level
    out: List[str] = []
    title_suffix = f" — {level_label}" if level_label else ""
    out.append(f"{h} Hitter Upgrade Targets{title_suffix} — FA score at position exceeds your starter")
    out.append("")
    out.append(
        "_Rating Edge = FA VOS − slot VOS (depth/scouting jump). Stat Edge = FA stat − slot stat "
        "(in-season form). 🔥 = 2+ tier jump (slot is structurally weak)._"
    )
    out.append("")
    out.append("| Pos | Your Starter | Starter Tier | Best FA | FA Tier | Tier Jump | Slot Score | FA Score | Rating Edge | Stat Edge | Total Edge | Fair AAV |")
    out.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    any_upgrade = False
    for pos in HITTER_POSITIONS:
        starter = starters_by_pos.get(pos)
        slot_score = _slot_pos_score(starter, pos)
        if starter:
            slot_name = starter["name"]
            slot_tier = starter.get("vos_tier", "") or ""
            slot_vos = float(starter.get("vos", 0.0) or 0.0)
            slot_stat = float(starter.get("stat_score", 0.0) or 0.0)
        else:
            slot_name = "(empty)"
            slot_tier = ""
            slot_vos = 0.0
            slot_stat = 0.0

        scored: List[Tuple[float, Dict[str, Any]]] = []
        for p in fa_hitters_sorted:
            fa_pos_score = (p.get("pos_scores_blended") or {}).get(pos, 0.0)
            if fa_pos_score > 0 and fa_pos_score > slot_score:
                scored.append((fa_pos_score, p))
        if not scored:
            continue
        scored.sort(key=lambda t: -t[0])
        best_score, best = scored[0]
        rating_edge = float(best.get("vos", 0.0) or 0.0) - slot_vos
        stat_edge = float(best.get("stat_score", 0.0) or 0.0) - slot_stat
        total_edge = best_score - slot_score
        fa_name = f"{best['name']}{_fmt_rating_only_badge(best)}"
        fa_tier = best.get("vos_tier", "") or "—"
        slot_tier_display = slot_tier or "—"
        out.append(
            f"| {pos} | {slot_name} | {slot_tier_display} | {fa_name} | {fa_tier} | "
            f"{_fmt_tier_jump(fa_tier, slot_tier, is_pitcher=False)} | "
            f"{slot_score:.1f} | {best_score:.1f} | "
            f"{_signed(rating_edge)} | {_signed(stat_edge)} | {_signed(total_edge)} | "
            f"{_fmt_aav(best.get('fair_aav'))} |"
        )
        any_upgrade = True
    if not any_upgrade:
        out.append("| _no FA hitters beat your current starters at any position_ |  |  |  |  |  |  |  |  |  |  |  |")
    out.append("")
    return out


def _render_pitcher_upgrade_table(
    pitcher_slots: Dict[str, List[Dict[str, Any]]],
    fa_pitchers_sorted: List[Dict[str, Any]],
    heading_level: int = 2,
    level_label: Optional[str] = None,
) -> List[str]:
    """Pitcher upgrade table — tier-aware. Compares each role's weakest
    slotted pitcher to the best available FA in that role bucket
    (SP/RP). Includes rating/stat edge decomposition + tier jump column.
    """
    h = "#" * heading_level
    out: List[str] = []
    title_suffix = f" — {level_label}" if level_label else ""
    out.append(f"{h} Pitcher Upgrade Targets{title_suffix} — FA outranks your weakest in role")
    out.append("")
    out.append(
        "_Tier comparison uses pitcher tier bands (Ace/#2-#3 Starter/Mid-Rotation/...). "
        "Edge columns same as hitter table._"
    )
    out.append("")
    out.append("| Role | Your Weakest | Slot Tier | Best FA | FA Tier | Tier Jump | Slot Comp | FA Comp | Rating Edge | Stat Edge | Total Edge | Fair AAV |")
    out.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    any_pup = False
    for role in PITCHER_ROLES:
        slots = pitcher_slots.get(role, [])
        if not slots:
            continue
        weakest = min(slots, key=lambda r: r["composite"])
        fa_role = "RP" if role in {"CL", "SU", "MR", "LR"} else "SP"
        candidates = [
            p for p in fa_pitchers_sorted
            if p.get("proj_role", "") == fa_role and p["composite"] > weakest["composite"]
        ]
        if not candidates:
            continue
        best = candidates[0]
        slot_vos = float(weakest.get("vos", 0.0) or 0.0)
        slot_stat = float(weakest.get("stat_score", 0.0) or 0.0)
        rating_edge = float(best.get("vos", 0.0) or 0.0) - slot_vos
        stat_edge = float(best.get("stat_score", 0.0) or 0.0) - slot_stat
        total_edge = float(best["composite"]) - float(weakest["composite"])
        slot_tier = weakest.get("vos_tier", "") or ""
        fa_tier = best.get("vos_tier", "") or ""
        fa_name = f"{best['name']}{_fmt_rating_only_badge(best)}"
        out.append(
            f"| {role} | {weakest['name']} | {slot_tier or '—'} | {fa_name} | "
            f"{fa_tier or '—'} | {_fmt_tier_jump(fa_tier, slot_tier, is_pitcher=True)} | "
            f"{weakest['composite']:.1f} | {best['composite']:.1f} | "
            f"{_signed(rating_edge)} | {_signed(stat_edge)} | {_signed(total_edge)} | "
            f"{_fmt_aav(best.get('fair_aav'))} |"
        )
        any_pup = True
    if not any_pup:
        out.append("| _no FA pitchers beat your current staff_ |  |  |  |  |  |  |  |  |  |  |  |")
    out.append("")
    return out


def _signed(v: float) -> str:
    """Render a number with explicit sign and one decimal. '0.0' stays
    unsigned (no '+0.0' noise). Used for edge-decomposition cells where
    direction matters as much as magnitude."""
    if abs(v) < 0.05:
        return "0.0"
    return f"{v:+.1f}"


def write_csv(path: Path, rows: List[Dict[str, Any]], fields: List[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            r2 = {k: (f"{r.get(k, ''):.4f}" if isinstance(r.get(k), float) else r.get(k, "")) for k in fields}
            writer.writerow(r2)


# -----------------------------------------------------------------------------
# Multi-level rendering
# -----------------------------------------------------------------------------

def _filter_fa_for_level(
    fa_records: List[Dict[str, Any]],
    display_level: str,
    age_cap: Optional[int],
    ignore_last_level: bool,
    min_pro_service_days: int = 0,
) -> List[Dict[str, Any]]:
    """Apply per-level age cap, last-level filter, and pro-service-time filter
    to FA candidates."""
    out = fa_records
    if min_pro_service_days > 0:
        def _service_ok(rec: Dict[str, Any]) -> bool:
            psd = rec.get("pro_service_days")
            if psd is None:
                # No /players record — be conservative: exclude. Most likely
                # the player is an amateur not yet in the pro system.
                return False
            return psd >= min_pro_service_days
        out = [r for r in out if _service_ok(r)]
    if age_cap is not None:
        def _age_ok(rec: Dict[str, Any]) -> bool:
            try:
                return float(rec.get("age", 0)) <= age_cap
            except (TypeError, ValueError):
                return True
        out = [r for r in out if _age_ok(r)]
    if not ignore_last_level:
        target_rank = LEVEL_RANK.get(display_level, 99)
        max_allowed = target_rank + 1  # at most 1 level below
        def _level_ok(rec: Dict[str, Any]) -> bool:
            ll = (rec.get("last_level") or "").strip()
            if not ll:
                return True  # unknown last level — let it through
            return LEVEL_RANK.get(ll, 99) <= max_allowed
        out = [r for r in out if _level_ok(r)]
    return out


def render_multilevel_md(
    league: str, org: str, year: int, ts: str,
    per_level_data: List[Dict[str, Any]],
    aav_years: int = 3,
    gap_fa_top_n: int = 3,
) -> str:
    out: List[str] = []
    out.append(f"# Free Agent Market — Multi-Level — {org}  ·  {league.upper()}  ·  {year}")
    out.append("")
    out.append(f"_Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} from depth-chart batch `{ts}`._")
    out.append("")
    out.append("Composites computed per-level using each level's weights from `depth_config.json`. "
               "Age caps and last-level filters applied to keep recommendations relevant — "
               "see `--age-cap-override` and `--ignore-last-level` to tune.")
    out.append(f"Fair AAV: market-only VPC × age-projected VOS over a {aav_years}-yr term "
               "(see contract_config.json). Override with `--aav-years`.")
    out.append("")

    # Quick Hits summary — best upgrade FA at each level/position pair.
    # Sorted by tier-jump descending so the structurally-weakest slots (where
    # the FA represents the biggest tier upgrade) lead the list. Within
    # equal-jump rows, sort by total edge descending so the biggest gap wins
    # the tie-break. This pulls the 'Bench → Star' urgency calls to the top
    # of a single scan instead of burying them under same-tier comp wins.
    out.append("## Quick Hits — Top FA Upgrade Per (Level, Position)")
    out.append("")
    out.append("_Sorted by tier jump severity (🔥 = 2+ tier jump). Only rows where the FA outranks the "
               "slotted starter at that position. Slot weakness ahead of comp-edge size._")
    out.append("")
    out.append("| Level | Pos | Tier Jump | Best FA | Age | FA Tier | Last Lvl | Your Slot | Slot Tier | Slot Score | FA Score | Total Edge | Fair AAV |")
    out.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    quickhit_rows: List[Tuple[int, float, str]] = []  # (-jump, -edge, row_text) for stable sort
    for d in per_level_data:
        for pos in HITTER_POSITIONS:
            starter = d["starters_by_pos"].get(pos)
            slot_score = _slot_pos_score(starter, pos)
            if starter:
                slot_name = starter["name"]
                slot_tier = starter.get("vos_tier", "") or ""
            else:
                slot_name = "(empty)"
                slot_tier = ""
            scored: List[Tuple[float, Dict[str, Any]]] = []
            for p in d["fa_hitters"]:
                fa_pos_score = (p.get("pos_scores_blended") or {}).get(pos, 0.0)
                if fa_pos_score > 0 and fa_pos_score > slot_score:
                    scored.append((fa_pos_score, p))
            if not scored:
                continue
            scored.sort(key=lambda t: -t[0])
            best_score, best = scored[0]
            edge = best_score - slot_score
            fa_tier = best.get("vos_tier", "") or ""
            jump = _tier_jump(fa_tier, slot_tier, is_pitcher=False)
            fa_name = f"{best['name']}{_fmt_rating_only_badge(best)}"
            row = (
                f"| {d['display_level']} | {pos} | "
                f"{_fmt_tier_jump(fa_tier, slot_tier, is_pitcher=False)} | "
                f"{fa_name} | {best.get('age','')} | {fa_tier or '—'} | "
                f"{_fmt_last_level(best)} | {slot_name} | {slot_tier or '—'} | "
                f"{slot_score:.1f} | {best_score:.1f} | {_signed(edge)} | "
                f"{_fmt_aav(best.get('fair_aav'))} |"
            )
            quickhit_rows.append((-jump, -edge, row))
    quickhit_rows.sort(key=lambda t: (t[0], t[1]))
    if not quickhit_rows:
        out.append("| _no clear hitter upgrades available across any level_ |  |  |  |  |  |  |  |  |  |  |  |  |")
    else:
        for _j, _e, row in quickhit_rows:
            out.append(row)
    out.append("")

    # Per-level detail
    for d in per_level_data:
        display_lvl = d["display_level"]
        out.append(f"## {display_lvl} — Detail")
        out.append("")
        out.append(
            f"_FA pool: {len(d['fa_hitters'])} hitters, {len(d['fa_pitchers'])} pitchers "
            f"after age cap ({d['age_cap'] if d['age_cap'] is not None else 'none'}) "
            f"and last-level filter._"
        )
        out.append("")

        # Per-level "empty starter slots — FA candidates" sub-section. One
        # heading deeper than the level header so it nests properly.
        out.extend(render_empty_slot_fa_section(
            d.get("empty_slots") or [], d.get("fa_hitters") or [],
            top_n=gap_fa_top_n,
            heading_level=3,
            src_note=d.get("threshold_source_note"),
        ))

        # Hitter and pitcher upgrade tables share the single-level helpers so
        # the column layout (tier jump, decomposed edge) stays consistent
        # across the single-level and multi-level reports.
        out.extend(_render_hitter_upgrade_table(
            d["starters_by_pos"], d["fa_hitters"],
            heading_level=3, level_label=display_lvl,
        ))
        out.extend(_render_pitcher_upgrade_table(
            d["pitcher_slots"], d["fa_pitchers"],
            heading_level=3, level_label=display_lvl,
        ))
    return "\n".join(out)


# -----------------------------------------------------------------------------
# Multi-level main flow
# -----------------------------------------------------------------------------

def run_multilevel(args: argparse.Namespace, cfg: Dict[str, Any]) -> int:
    """Read existing depth chart batch, compute per-level FA composites, and
    write one combined upgrade-recommendation MD."""
    floors = cfg.get("stat_floors", {})
    woba_w = cfg.get("woba_weights", {})
    year_weights = cfg.get("year_weights", [0.55, 0.35, 0.10])

    target_year = args.year or dc.league_default_year(args.league) or datetime.now().year

    # Discover the latest depth chart batch for the org
    depth_dir = args.output_dir or (SCRIPT_DIR / args.league / "depth")
    org_slug = (args.org_code or args.org).lower().replace(" ", "_")
    # Guarantee the depth charts we're about to scan reflect the latest eval —
    # regenerates them first if the batch is stale, legacy, or missing.
    ts = ensure_fresh_depth_batch(args, depth_dir, org_slug, target_year)
    if ts is None:
        logger.error(
            "No depth chart files found for org '%s' in %s, and auto-refresh "
            "did not produce one. Run depth_chart.py first.",
            args.org, depth_dir,
        )
        return 2
    levels = levels_in_batch(depth_dir, org_slug, ts)
    if not levels:
        logger.error("No levels resolved from depth chart batch %s", ts)
        return 2
    logger.info("Scanning batch %s — levels: %s", ts, [d for d, _ in levels])

    # Load eval and fetch stats once across the whole league pool
    eval_path = dc.find_latest_eval(args.league, args.input, args.org_code)
    logger.info("Eval file: %s", eval_path)
    eval_rows = dc.read_eval(eval_path)

    league_ids_map = dc.load_league_ids(args.league_ids_config)
    league_block = league_ids_map.get(args.league.lower(), {})
    all_lids: List[int] = []
    for _lvl, ids in league_block.items():
        for x in ids:
            if x not in all_lids:
                all_lids.append(x)

    base_url = sapi.resolve_base_url(args.league, args.base_url, args.league_url_config)
    if not base_url:
        logger.error("No base URL for league '%s'", args.league)
        return 2

    cache_dir: Optional[Path] = None
    if not args.no_cache:
        cache_dir = SCRIPT_DIR / args.league / "cache" / "stats"

    hitters, pitchers, _fielders, _lg = sapi.build_player_stats(
        base_url, target_year, year_weights, woba_w,
        lids=all_lids or None, target_lids=all_lids or None,
        cache_dir=cache_dir,
    )

    h_means, h_stds = dc.compute_means_stds(hitters, dc.HITTER_COMPONENTS, "overall") if hitters else ({}, {})
    h_means_l, h_stds_l = dc.compute_means_stds(hitters, dc.HITTER_COMPONENTS, "vs_l") if hitters else ({}, {})
    h_means_r, h_stds_r = dc.compute_means_stds(hitters, dc.HITTER_COMPONENTS, "vs_r") if hitters else ({}, {})
    p_means, p_stds = dc.compute_means_stds(pitchers, dc.PITCHER_COMPONENTS, "overall") if pitchers else ({}, {})

    fa_eval = fa_pool(eval_rows)

    # Build market fair-AAV context once (calibrated against league-wide FA
    # contracts) — reused across every level's FA record set.
    aav_ctx = _build_aav_context_or_none(args, eval_rows, cache_dir)

    # Fetch /players for service-time filtering and retired-player exclusion.
    # Amateur draft-eligible players show up in the FA pool because they have
    # no Org, but they have zero pro_service_days. Retired players also have no
    # Org but carry Retired=1. Both are excluded here.
    players_lookup: Dict[str, Dict[str, str]] = {}
    try:
        players_lookup = sapi.build_players_lookup(base_url, cache_dir=cache_dir)
        logger.info("Loaded /players (%d records).", len(players_lookup))
    except Exception as exc:
        logger.warning(
            "Failed to load /players (%s) — retired players and amateurs may appear in FA recommendations.",
            exc,
        )

    if players_lookup:
        _n0 = len(fa_eval)
        fa_eval = [
            r for r in fa_eval
            if not dc._bool_from_value(players_lookup.get((r.get("ID") or "").strip(), {}).get("retired"))
        ]
        if len(fa_eval) < _n0:
            logger.info("Removed %d retired player(s) from FA pool.", _n0 - len(fa_eval))
    logger.info("FA pool size: %d (pre-filter)", len(fa_eval))

    def _pro_service_days(pid: str) -> Optional[int]:
        """Return pro_service_days for a pid, or None if missing."""
        meta = players_lookup.get(pid)
        if not meta:
            return None
        raw = (meta.get("pro_service_days") or "").strip()
        if not raw:
            return None
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return None

    def make_records(rows: List[Dict[str, str]], lvl_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for r in rows:
            rec = dc.build_player_record(
                r, pitchers, hitters, lvl_cfg, floors,
                p_means, p_stds, h_means, h_stds,
                h_means_l, h_stds_l, h_means_r, h_stds_r,
            )
            rec["last_level"] = (r.get("League_Level") or "").strip()
            rec["pro_service_days"] = _pro_service_days(rec["pid"])
            _attach_v10_passthrough(rec, r)
            out.append(rec)
        return out

    # Per-level data — one entry per level in the batch
    per_level_data: List[Dict[str, Any]] = []
    for display_level, base_level in levels:
        if base_level not in cfg["levels"]:
            logger.warning(
                "Skipping level '%s' (base '%s' not in depth_config).", display_level, base_level,
            )
            continue
        lvl_cfg_local = cfg["levels"][base_level]

        csv_path = depth_dir / f"{org_slug}_{display_level}_{ts}.csv"
        starters_by_pos, pitcher_slots = load_depth_chart_starters(csv_path)

        # Attach per-position blended scores to each starter so the
        # cross-position FA comparison has both sides on the same footing.
        # The depth chart CSV doesn't persist pos_scores_blended, so rebuild
        # the org's records at this level's weights and join by pid.
        affiliate_suffix = (
            display_level.split("-", 1)[1]
            if display_level.startswith("R-") else None
        )
        org_eval_lvl = dc.org_pool(
            eval_rows, args.org, base_level, affiliate=affiliate_suffix,
        )
        org_records_lvl = make_records(org_eval_lvl, lvl_cfg_local)
        pid_to_pos_scores = {
            r["pid"]: (r.get("pos_scores_blended") or {})
            for r in org_records_lvl
        }
        # Pull vos_tier and vos_potential_tier from org records onto each
        # starter/pitcher slot — the depth_chart CSV doesn't persist these
        # but the upgrade tables need them for the tier-delta column.
        pid_to_org_meta = {
            r["pid"]: {
                "vos_tier": r.get("vos_tier", ""),
                "vos_potential_tier": r.get("vos_potential_tier", ""),
            }
            for r in org_records_lvl
        }
        for pos, starter in starters_by_pos.items():
            if starter:
                starter["pos_scores_blended"] = pid_to_pos_scores.get(starter["pid"], {})
                meta = pid_to_org_meta.get(starter["pid"], {})
                starter["vos_tier"] = meta.get("vos_tier", "")
                starter["vos_potential_tier"] = meta.get("vos_potential_tier", "")
        for _role, slot_list in pitcher_slots.items():
            for slot in slot_list:
                meta = pid_to_org_meta.get(slot["pid"], {})
                slot["vos_tier"] = meta.get("vos_tier", "")
                slot["vos_potential_tier"] = meta.get("vos_potential_tier", "")

        # Compute FA composites at THIS level's weights
        fa_records = make_records(fa_eval, lvl_cfg_local)

        age_cap = args.age_cap_override if args.age_cap_override is not None \
            else DEFAULT_AGE_CAPS.get(display_level)
        fa_filtered = _filter_fa_for_level(
            fa_records, display_level, age_cap, args.ignore_last_level,
            min_pro_service_days=args.min_pro_service_days,
        )

        _attach_aav(
            fa_filtered, aav_ctx,
            suggest_length=not args.no_suggest_length,
            suggest_max_years=args.suggest_max_years,
            suggest_floor_ratio=args.suggest_floor_ratio,
        )

        fa_hitters = sorted(
            [r for r in fa_filtered if not r["is_pitcher"]],
            key=lambda p: -p["composite"],
        )
        fa_pitchers = sorted(
            [r for r in fa_filtered if r["is_pitcher"]],
            key=lambda p: -p["composite"],
        )

        # Per-level threshold resolution mirrors the single-level path. The
        # multi-level batch's sidecars all live under depth_dir and are keyed
        # by display_level, so each level's --min-comp gating loads cleanly.
        thresholds_lvl, _sidecar_empty_lvl, sidecar_path_lvl = resolve_starter_thresholds(
            args, depth_dir, org_slug, display_level,
        )
        if thresholds_lvl and not args.no_gap_fa_section:
            empty_slots_lvl = compute_empty_slots(starters_by_pos, thresholds_lvl)
            src_bits_lvl: List[str] = []
            if sidecar_path_lvl is not None:
                src_bits_lvl.append(f"sidecar `{sidecar_path_lvl.name}`")
            if args.min_comp is not None:
                src_bits_lvl.append(f"--min-comp {args.min_comp:g}")
            if args.min_comp_pos:
                src_bits_lvl.append(f"--min-comp-pos `{args.min_comp_pos}`")
            threshold_source_note_lvl = (
                "Thresholds from " + ", ".join(src_bits_lvl) + "."
                if src_bits_lvl else None
            )
        else:
            empty_slots_lvl = []
            threshold_source_note_lvl = None

        per_level_data.append({
            "display_level": display_level,
            "base_level": base_level,
            "starters_by_pos": starters_by_pos,
            "pitcher_slots": pitcher_slots,
            "fa_hitters": fa_hitters,
            "fa_pitchers": fa_pitchers,
            "age_cap": age_cap,
            "empty_slots": empty_slots_lvl,
            "threshold_source_note": threshold_source_note_lvl,
        })

    if not per_level_data:
        logger.error("No usable levels — nothing to write.")
        return 2

    out_dir = depth_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    out_md = out_dir / f"free_agents_{org_slug}_multilevel_{ts}.md"
    md = render_multilevel_md(
        args.league, args.org, target_year, ts, per_level_data,
        aav_years=args.aav_years, gap_fa_top_n=args.gap_fa_top_n,
    )
    out_md.write_text(md, encoding="utf-8")
    logger.info("Wrote %s", out_md)
    return 0


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def _resolve_org_short_code(args: argparse.Namespace) -> None:
    """If ``--org`` is a short code (e.g. 'stl'), reverse-map it to the full team
    name from the park-factors mapping so ``org_pool`` filters by the value
    that actually appears in the eval CSV's ``Org`` column.

    Mirrors the behavior in depth_chart.py (search for ``code_lookup``). When
    the user passes ``--org stl`` and the park-factors file has a teams[] block
    mapping 'St. Louis Cardinals' -> 'stl', this rewrites ``args.org`` to
    'St. Louis Cardinals' and sets ``args.org_code`` to 'stl' if unset.
    Without this, ``org_pool`` would search for Org='stl', find zero rows,
    and every starter slot would be empty.
    """
    pf_path = args.park_factors or dc._default_park_factors_path(args.league)
    name_to_code = dc._name_to_code_map(pf_path)
    if not name_to_code or args.org in name_to_code:
        return  # Either no mapping available, or args.org is already a full name.
    code_lookup = {c: n for n, c in name_to_code.items()}
    maybe_code = (args.org or "").strip().lower()
    if maybe_code in code_lookup:
        logger.info(
            "Resolved --org %r to %r (code=%s).",
            args.org, code_lookup[maybe_code], maybe_code,
        )
        args.org = code_lookup[maybe_code]
        if not args.org_code:
            args.org_code = maybe_code


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")

    cfg = dc.load_config(args.config)
    _resolve_org_short_code(args)

    # Multi-level branch: read existing depth charts and produce one combined report.
    if args.scan_depth_dir:
        return run_multilevel(args, cfg)

    if not args.level:
        logger.error("--level is required unless --scan-depth-dir is set.")
        return 2
    level = args.level.strip().upper()
    if level not in cfg["levels"]:
        logger.error("Level '%s' not in depth_config.json", level)
        return 2
    level_cfg = cfg["levels"][level]
    floors = cfg.get("stat_floors", {})
    woba_w = cfg.get("woba_weights", {})
    year_weights = cfg.get("year_weights", [0.55, 0.35, 0.10])

    target_year = args.year or dc.league_default_year(args.league) or datetime.now().year

    # Load eval. Single-level mode rebuilds the depth chart live from this eval
    # below (org_pool → assign_positions), so it's inherently fresh — no on-disk
    # depth batch is consumed. Only the starter min-comp thresholds come from a
    # sidecar, and those aren't eval-dependent.
    eval_path = dc.find_latest_eval(args.league, args.input, args.org_code)
    logger.info("Eval file: %s (depth chart computed live - always current)", eval_path)
    eval_rows = dc.read_eval(eval_path)

    # Resolve lids — for FAs we want broad coverage; pull every level the league
    # has defined so a FA who last played in A+ still gets stats.
    league_ids_map = dc.load_league_ids(args.league_ids_config)
    league_block = league_ids_map.get(args.league.lower(), {})
    all_lids: List[int] = []
    for lvl, ids in league_block.items():
        for x in ids:
            if x not in all_lids:
                all_lids.append(x)
    target_lids = league_block.get(level, []) or all_lids

    base_url = sapi.resolve_base_url(args.league, args.base_url, args.league_url_config)
    if not base_url:
        logger.error("No base URL for league '%s'", args.league)
        return 2

    cache_dir: Optional[Path] = None
    if not args.no_cache:
        cache_dir = SCRIPT_DIR / args.league / "cache" / "stats"

    hitters, pitchers, _fielders, _lg = sapi.build_player_stats(
        base_url, target_year, year_weights, woba_w,
        lids=all_lids or None, target_lids=target_lids or None,
        cache_dir=cache_dir,
    )

    # Z-score reference computed across the full fetched pool.
    h_means, h_stds = dc.compute_means_stds(hitters, dc.HITTER_COMPONENTS, "overall") if hitters else ({}, {})
    h_means_l, h_stds_l = dc.compute_means_stds(hitters, dc.HITTER_COMPONENTS, "vs_l") if hitters else ({}, {})
    h_means_r, h_stds_r = dc.compute_means_stds(hitters, dc.HITTER_COMPONENTS, "vs_r") if hitters else ({}, {})
    p_means, p_stds = dc.compute_means_stds(pitchers, dc.PITCHER_COMPONENTS, "overall") if pitchers else ({}, {})

    def make_record(rows: List[Dict[str, str]]) -> List[Dict[str, Any]]:
        out = []
        for r in rows:
            rec = dc.build_player_record(
                r, pitchers, hitters, level_cfg, floors,
                p_means, p_stds, h_means, h_stds,
                h_means_l, h_stds_l, h_means_r, h_stds_r,
            )
            rec["last_level"] = (r.get("League_Level") or "").strip()
            _attach_v10_passthrough(rec, r)
            out.append(rec)
        return out

    # Org pool → user's depth chart at requested level
    org_eval = dc.org_pool(eval_rows, args.org, level)
    org_records = make_record(org_eval)
    org_hitters = [r for r in org_records if not r["is_pitcher"]]
    org_pitchers = [r for r in org_records if r["is_pitcher"]]

    placed = dc.assign_positions(org_hitters, level_cfg)
    starters_by_pos = {pos: (placed[pos][0] if placed[pos] else None) for pos in HITTER_POSITIONS}
    pitcher_slots = dc.assign_pitchers(org_pitchers, level_cfg)

    # Free agents from across the eval CSV (any level, any age)
    fa_eval = fa_pool(eval_rows)
    # Filter out retired players using /players data (Retired=1 in OOTP).
    players_lookup: Dict[str, Dict[str, str]] = {}
    try:
        players_lookup = sapi.build_players_lookup(base_url, cache_dir=cache_dir)
    except Exception as _exc:
        logger.warning("Failed to load /players for retired-player filter (%s).", _exc)
    if players_lookup:
        _n0 = len(fa_eval)
        fa_eval = [
            r for r in fa_eval
            if not dc._bool_from_value(players_lookup.get((r.get("ID") or "").strip(), {}).get("retired"))
        ]
        if len(fa_eval) < _n0:
            logger.info("Removed %d retired player(s) from FA pool.", _n0 - len(fa_eval))
    fa_records = make_record(fa_eval)

    # Apply optional sample-size filters
    def hitter_pa(p: Dict[str, Any]) -> float:
        return float(((p.get("hitter_bundle") or {}).get("overall") or {}).get("PA", 0.0))

    def pitcher_ip(p: Dict[str, Any]) -> float:
        return float(((p.get("pitcher_bundle") or {}).get("overall") or {}).get("IP", 0.0))

    fa_hitters = [p for p in fa_records if not p["is_pitcher"] and hitter_pa(p) >= args.min_pa]
    fa_pitchers = [p for p in fa_records if p["is_pitcher"] and pitcher_ip(p) >= args.min_ip]

    # Attach market fair-AAV to each FA record. Context calibrates VPC against
    # established (6+ service yrs) FA contracts in the eval CSV — same
    # methodology as contract_audit's --market-only mode.
    aav_ctx = _build_aav_context_or_none(args, eval_rows, cache_dir)
    _attach_aav(
        fa_hitters, aav_ctx,
        suggest_length=not args.no_suggest_length,
        suggest_max_years=args.suggest_max_years,
        suggest_floor_ratio=args.suggest_floor_ratio,
    )
    _attach_aav(
        fa_pitchers, aav_ctx,
        suggest_length=not args.no_suggest_length,
        suggest_max_years=args.suggest_max_years,
        suggest_floor_ratio=args.suggest_floor_ratio,
    )

    fa_hitters.sort(key=lambda p: -p["composite"])
    fa_pitchers.sort(key=lambda p: -p["composite"])

    logger.info("FA hitters: %d  |  FA pitchers: %d", len(fa_hitters), len(fa_pitchers))

    # Output
    out_dir = args.output_dir or (SCRIPT_DIR / args.league / "depth")
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    org_slug = (args.org_code or args.org).lower().replace(" ", "_")
    out_md = out_dir / f"free_agents_{org_slug}_{level}_{ts}.md"
    out_csv = out_dir / f"free_agents_{org_slug}_{level}_{ts}.csv"

    # Resolve --min-comp thresholds + empty starter slots for the new
    # "Empty Starter Slots — FA Candidates" section. Sidecar is consulted
    # first; CLI flags override per the precedence in resolve_starter_thresholds.
    thresholds, sidecar_empty_slots, sidecar_path = resolve_starter_thresholds(
        args, out_dir, org_slug, level,
    )
    # CLI overrides change which positions count as empty; always recompute
    # against the freshly-built starters_by_pos so the section is correct.
    if thresholds and not args.no_gap_fa_section:
        empty_slots_for_section = compute_empty_slots(starters_by_pos, thresholds)
        src_bits: List[str] = []
        if sidecar_path is not None:
            src_bits.append(f"sidecar `{sidecar_path.name}`")
        if args.min_comp is not None:
            src_bits.append(f"--min-comp {args.min_comp:g}")
        if args.min_comp_pos:
            src_bits.append(f"--min-comp-pos `{args.min_comp_pos}`")
        threshold_source_note = (
            "Thresholds from " + ", ".join(src_bits) + "."
            if src_bits else None
        )
        if empty_slots_for_section:
            logger.info(
                "Empty-slot FA section: %d position(s) — %s",
                len(empty_slots_for_section),
                ", ".join(s["pos"] for s in empty_slots_for_section),
            )
    else:
        empty_slots_for_section = []
        threshold_source_note = None

    md = render_md(
        args.league, args.org, level, target_year,
        fa_hitters, fa_pitchers, starters_by_pos, pitcher_slots,
        args.top_n_hitters, args.top_n_pitchers,
        aav_years=args.aav_years,
        empty_slots=empty_slots_for_section,
        gap_fa_top_n=args.gap_fa_top_n,
        threshold_source_note=threshold_source_note,
    )
    out_md.write_text(md, encoding="utf-8")
    logger.info("Wrote %s", out_md)

    # Combined CSV: all FAs (hitters + pitchers) ranked by composite within type.
    # v10 additions: vos_tier, vos_potential_tier, prone, babip/pbabip + Pot,
    # delta_stat_vos — lets the user slice the FA pool in Excel by tier,
    # injury risk, or stat-vs-skill gap without re-joining PlayerData.
    fields = [
        "pid", "name", "age", "primary_pos", "is_pitcher", "proj_role", "last_level",
        "vos", "vos_potential", "vos_tier", "vos_potential_tier",
        "stat_score", "delta_stat_vos", "sample_weight", "composite", "fair_aav",
        "PA", "wOBA", "OBP", "SLG", "wOBA_vs_L", "wOBA_vs_R",
        "IP", "FIP", "FIP_current", "K_pct", "BB_pct",
        "BABIP", "PotBABIP", "PBABIP", "PotPBABIP", "Prone",
    ]

    def flatten(p: Dict[str, Any]) -> Dict[str, Any]:
        hb = (p.get("hitter_bundle") or {})
        h_overall = hb.get("overall", {}) if hb else {}
        h_l = hb.get("vs_l", {}) if hb else {}
        h_r = hb.get("vs_r", {}) if hb else {}
        pb = (p.get("pitcher_bundle") or {}).get("overall", {}) if p.get("pitcher_bundle") else {}
        pb_cur = (p.get("pitcher_bundle") or {}).get("current", {}) if p.get("pitcher_bundle") else {}
        return {
            "pid": p.get("pid", ""),
            "name": p.get("name", ""),
            "age": p.get("age", ""),
            "primary_pos": p.get("primary_pos", ""),
            "is_pitcher": p.get("is_pitcher", False),
            "proj_role": p.get("proj_role", ""),
            "last_level": p.get("last_level", ""),
            "vos": p.get("vos", 0.0),
            "vos_potential": p.get("vos_potential", 0.0),
            "vos_tier": p.get("vos_tier", ""),
            "vos_potential_tier": p.get("vos_potential_tier", ""),
            "stat_score": p.get("stat_score", 0.0),
            "delta_stat_vos": p.get("delta_stat_vos", 0.0),
            "sample_weight": p.get("sample_weight", 0.0),
            "composite": p.get("composite", 0.0),
            "fair_aav": p.get("fair_aav") if p.get("fair_aav") is not None else "",
            "PA": h_overall.get("PA", "") if h_overall else "",
            "wOBA": h_overall.get("wOBA", "") if h_overall else "",
            "OBP": h_overall.get("OBP", "") if h_overall else "",
            "SLG": h_overall.get("SLG", "") if h_overall else "",
            "wOBA_vs_L": h_l.get("wOBA", "") if h_l else "",
            "wOBA_vs_R": h_r.get("wOBA", "") if h_r else "",
            "IP": pb.get("IP", "") if pb else "",
            "FIP": pb.get("FIP", "") if pb else "",
            "FIP_current": pb_cur.get("FIP", "") if pb_cur else "",
            "K_pct": pb.get("K%", "") if pb else "",
            "BB_pct": pb.get("BB%", "") if pb else "",
            "BABIP": p.get("babip") if p.get("babip") is not None else "",
            "PotBABIP": p.get("pot_babip") if p.get("pot_babip") is not None else "",
            "PBABIP": p.get("pbabip") if p.get("pbabip") is not None else "",
            "PotPBABIP": p.get("pot_pbabip") if p.get("pot_pbabip") is not None else "",
            "Prone": p.get("prone", ""),
        }

    rows_out = [flatten(p) for p in fa_hitters] + [flatten(p) for p in fa_pitchers]
    write_csv(out_csv, rows_out, fields)
    logger.info("Wrote %s", out_csv)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

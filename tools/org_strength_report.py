#!/usr/bin/env python3
"""
org_strength_report.py — Roll up an organization's per-level depth charts into a
positional strength report. Surfaces holes, weak spots, surplus, and players
the org is over-relying on (essential players whose removal collapses a position).

Inputs
------
- {league}/depth/{org_slug}_{level}_{ts}.csv
  Produced by depth_chart.py. One file per (org, level). Columns include
  composite, tier, primary_pos, vos, etc.

For league-relative percentile rankings, the script scans every org's CSVs
sharing the same timestamp.

Composite scale
---------------
depth_chart.py emits composites on a roughly 35-65 scale (20-80 clamped, but
real-world distribution is tighter). Composites are z-scored WITHIN each
level's stat pool, so a 60 at AAA and a 60 at A both mean "top of the level."
Absolute tier thresholds therefore work consistently across levels — and a
league-relative percentile is added on top to show how the org stacks up
against its peers at the same level.

Output
------
- {league}/org_depth/{org_slug}_strength_{ts}.md
- {league}/org_depth/{org_slug}_strength_{ts}_positions.csv
- {league}/org_depth/{org_slug}_strength_{ts}_player_details.csv

When run with --all-orgs, also emits:
- {league}/org_depth/league_strength_{ts}.md  (league-wide rollup)

Usage
-----
    python org_strength_report.py --league wwoba --org "Arizona Diamondbacks"
    python org_strength_report.py --league wwoba --all-orgs
    python org_strength_report.py --league wwoba --org-slug arizona_diamondbacks
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
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)
SCRIPT_DIR = Path(__file__).resolve().parent.parent

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

HITTER_POSITIONS = ["C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "DH"]
PITCHER_ROLES = ["SP", "CL", "SU", "MR", "LR"]
PITCHER_GROUPS = ["SP", "RP"]  # high-level grouping for analysis
RP_ROLES = {"CL", "SU", "MR", "LR"}

# Levels in promotion order (top to bottom)
LEVEL_ORDER = ["ML", "AAA", "AA", "A+", "A", "A-", "R"]

# Tier thresholds applied to the rank-1 (starter) composite at a position.
# Composites are 20-80 scaled but in practice cluster ~35-65.
TIER_THRESHOLDS = [
    ("Elite", 60.0),
    ("Strong", 55.0),
    ("Average", 50.0),
    ("Weak", 45.0),
    ("Hole", float("-inf")),
]

# Shorthand symbols for the matrix view in the MD report.
TIER_SYMBOL = {
    "Elite": "★★",
    "Strong": "★",
    "Average": "·",
    "Weak": "▽",
    "Hole": "✕",
    "Empty": "—",
}

# Filename pattern for depth_chart outputs. depth_chart writes the team code
# (e.g. 'stl') as the org segment, but we also accept the legacy slugified
# display name (e.g. 'st._louis_cardinals') so historical batches still load.
DEPTH_CSV_RE = re.compile(
    r"^(?P<org>[a-z0-9_.]+?)_(?P<level>ML|AAA|AA|A\+|A-|A|R-DSL|R-FCL|R)_(?P<ts>\d{8}_\d{6})\.csv$"
)

# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Build a positional strength report for one org (or every org) "
                    "from depth_chart.py outputs."
    )
    p.add_argument("--league", required=True, help="League slug (e.g. wwoba).")
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--org", default=None,
                     help="Organization display name as it appears in the depth chart "
                          "filename (e.g. 'Arizona Diamondbacks' or 'arizona_diamondbacks').")
    grp.add_argument("--org-slug", default=None,
                     help="Org filename slug (e.g. 'arizona_diamondbacks'). Skips the "
                          "name-to-slug resolution.")
    grp.add_argument("--all-orgs", action="store_true",
                     help="Build a report for every org in the latest batch.")
    p.add_argument("--timestamp", default=None,
                   help="Specific {YYYYMMDD_HHMMSS} batch to use. Default: latest "
                        "shared across orgs in {league}/depth/.")
    p.add_argument("--depth-dir", type=Path, default=None,
                   help="Override depth chart directory (default: {league}/depth/).")
    p.add_argument("--output-dir", type=Path, default=None,
                   help="Override output directory (default: {league}/org_depth/).")
    p.add_argument("--no-league-summary", action="store_true",
                   help="Skip the league-wide rollup MD when --all-orgs is set.")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args()


# -----------------------------------------------------------------------------
# Data classes
# -----------------------------------------------------------------------------

@dataclass
class SlottedPlayer:
    pid: str
    name: str
    age: float
    primary_pos: str
    is_pitcher: bool
    proj_role: str
    vos: float
    composite: float
    tier_label: str            # e.g. "C-1", "SP1", "MR-2"
    slot_pos: str              # parsed from tier_label: "C", "SP", "MR"
    slot_rank: int             # 1, 2, 3...
    sample_weight: float
    org_slug: str
    level: str

    @property
    def out_of_position(self) -> bool:
        if self.is_pitcher:
            return False
        return self.primary_pos != self.slot_pos


@dataclass
class PositionCell:
    """Strength of one position at one level for one org."""
    org_slug: str
    level: str
    position: str             # hitter pos or pitcher role group ("SP"/"RP")
    starter: Optional[SlottedPlayer]
    backup: Optional[SlottedPlayer]
    depth_count: int
    starter_comp: float       # 0.0 if no starter
    avg_top3_comp: float
    tier: str                 # Elite/Strong/.../Hole/Empty
    league_pctile: float = 0.0  # 0-100, 100 = best in league at this level/pos

    @property
    def is_hole(self) -> bool:
        return self.tier in ("Hole", "Empty")

    @property
    def is_weak(self) -> bool:
        return self.tier in ("Weak", "Hole", "Empty")

    @property
    def is_strong(self) -> bool:
        return self.tier in ("Strong", "Elite")


@dataclass
class EssentialFlag:
    """A player whose removal would drop their position's tier by 1+ levels."""
    player: SlottedPlayer
    cell_before: PositionCell
    tier_after: str
    starter_comp_after: float
    reason: str


@dataclass
class OrgReport:
    org_slug: str
    org_display: str
    league: str
    timestamp: str
    levels: List[str]
    cells: Dict[Tuple[str, str], PositionCell]   # (level, position) -> cell
    essentials: List[EssentialFlag]
    out_of_position_starters: List[SlottedPlayer]
    players_by_level: Dict[str, List[SlottedPlayer]]


# -----------------------------------------------------------------------------
# File discovery
# -----------------------------------------------------------------------------

def find_latest_timestamp(depth_dir: Path) -> str:
    """Return the most recent timestamp that has multiple orgs/levels. Falls
    back to the lexicographically-latest file timestamp."""
    timestamps: Dict[str, int] = defaultdict(int)
    for path in depth_dir.glob("*.csv"):
        m = DEPTH_CSV_RE.match(path.name)
        if not m:
            continue
        timestamps[m.group("ts")] += 1
    if not timestamps:
        raise FileNotFoundError(f"No depth chart CSVs found in {depth_dir}.")
    # Prefer timestamps with the most files (full-org runs); break ties by recency.
    return max(timestamps.items(), key=lambda kv: (kv[1], kv[0]))[0]


def discover_batch(depth_dir: Path, timestamp: str) -> Dict[str, Dict[str, Path]]:
    """Return {org_slug: {level: csv_path}} for the given timestamp."""
    out: Dict[str, Dict[str, Path]] = defaultdict(dict)
    for path in depth_dir.glob(f"*_{timestamp}.csv"):
        m = DEPTH_CSV_RE.match(path.name)
        if not m or m.group("ts") != timestamp:
            continue
        out[m.group("org")][m.group("level")] = path
    if not out:
        raise FileNotFoundError(
            f"No depth chart CSVs match timestamp {timestamp} in {depth_dir}."
        )
    return dict(out)


def _name_to_code_map(league: str) -> Dict[str, str]:
    """Load {team_display_name: team_code_lower} from the league's combined
    teams[] park-factors file. Empty dict if the file is missing or malformed.
    """
    candidates = [
        SCRIPT_DIR / "config" / f"{league}-park-factors.json",
        SCRIPT_DIR / "config" / f"park-factors-{league}.json",
    ]
    out: Dict[str, str] = {}
    for path in candidates:
        if not path.exists():
            continue
        try:
            with path.open("r", encoding="utf-8") as f:
                pf = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(pf, dict):
            continue
        tb = pf.get("teams")
        if isinstance(tb, dict):
            for team_name, block in tb.items():
                if team_name.startswith("_") or not isinstance(block, dict):
                    continue
                code = ((block.get("team_info") or {}).get("team_code") or "").strip().lower()
                if code:
                    out[team_name] = code
        # Single-team format fallback.
        if not out:
            info = pf.get("team_info")
            if isinstance(info, dict):
                code = (info.get("team_code") or "").strip().lower()
                name = (info.get("team_name") or "").strip()
                if code and name:
                    out[name] = code
        if out:
            return out
    return out


def _code_to_name_map(league: str) -> Dict[str, str]:
    return {code: name for name, code in _name_to_code_map(league).items()}


def resolve_org_slug(org_arg: Optional[str], org_slug_arg: Optional[str],
                     batch: Dict[str, Dict[str, Path]],
                     league: Optional[str] = None) -> str:
    if org_slug_arg:
        slug = org_slug_arg.lower().replace(" ", "_")
        if slug in batch:
            return slug
        raise ValueError(
            f"Org slug '{slug}' not in batch. Known: {sorted(batch)}"
        )
    if not org_arg:
        raise ValueError("Either --org or --org-slug must be provided.")

    # Preferred path: resolve the display name to its team code via the
    # league's park-factors file (this is what depth_chart writes nowadays).
    if league:
        name_to_code = _name_to_code_map(league)
        # Exact display-name match.
        if org_arg in name_to_code and name_to_code[org_arg] in batch:
            return name_to_code[org_arg]
        # Case-insensitive display-name match.
        lower_lookup = {k.lower(): v for k, v in name_to_code.items()}
        if org_arg.lower() in lower_lookup and lower_lookup[org_arg.lower()] in batch:
            return lower_lookup[org_arg.lower()]
        # The user may have passed the code itself (e.g. "stl").
        if org_arg.lower() in batch:
            return org_arg.lower()

    # Legacy path: slugified display name (e.g. 'st._louis_cardinals') for
    # historical batches that pre-date the team-code switch.
    candidate = org_arg.lower().replace(" ", "_")
    if candidate in batch:
        return candidate
    matches = [s for s in batch if candidate in s or s in candidate]
    if len(matches) == 1:
        return matches[0]
    raise ValueError(
        f"Could not resolve org '{org_arg}'. Candidates: {sorted(batch)}"
    )


def org_display_from_slug(slug: str, league: Optional[str] = None) -> str:
    """Pretty-print an org slug for report headings. If ``slug`` is a known
    team code in the league's park-factors, use the canonical display name;
    otherwise fall back to title-casing the slug parts."""
    if league:
        code_to_name = _code_to_name_map(league)
        if slug in code_to_name:
            return code_to_name[slug]
    return " ".join(part.capitalize() for part in slug.split("_"))


# -----------------------------------------------------------------------------
# CSV parsing
# -----------------------------------------------------------------------------

def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        s = str(v).strip()
        return float(s) if s else default
    except (TypeError, ValueError):
        return default


def parse_tier(tier: str, is_pitcher: bool) -> Tuple[str, int]:
    """Parse depth_chart tier label into (slot_pos, rank).

    Hitter tiers: 'C-1', '2B-2', 'SS-3'.
    Pitcher tiers: 'SP1'..'SP5', 'CL-1', 'SU-2', 'MR-3', 'LR-1'.
    """
    if not tier:
        return ("", 0)
    tier = tier.strip()
    if is_pitcher:
        # SP1..SP5 (no dash)
        m = re.match(r"^(SP)(\d+)$", tier)
        if m:
            return ("SP", int(m.group(2)))
        # CL-1, SU-2, MR-3, LR-1
        m = re.match(r"^(CL|SU|MR|LR)-(\d+)$", tier)
        if m:
            return (m.group(1), int(m.group(2)))
        return (tier, 0)
    # Hitter: <POS>-<rank>
    m = re.match(r"^([A-Z0-9]+)-(\d+)$", tier)
    if m:
        return (m.group(1), int(m.group(2)))
    return (tier, 0)


def load_slotted_players(csv_path: Path, org_slug: str, level: str) -> List[SlottedPlayer]:
    out: List[SlottedPlayer] = []
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            pid = (row.get("pid") or "").strip()
            if not pid:
                continue
            is_pitcher = (row.get("is_pitcher") or "").strip().lower() == "true"
            tier_label = (row.get("tier") or "").strip()
            slot_pos, slot_rank = parse_tier(tier_label, is_pitcher)
            out.append(SlottedPlayer(
                pid=pid,
                name=(row.get("name") or "").strip(),
                age=_to_float(row.get("age")),
                primary_pos=(row.get("primary_pos") or "").strip(),
                is_pitcher=is_pitcher,
                proj_role=(row.get("proj_role") or "").strip(),
                vos=_to_float(row.get("vos")),
                composite=_to_float(row.get("composite")),
                tier_label=tier_label,
                slot_pos=slot_pos,
                slot_rank=slot_rank,
                sample_weight=_to_float(row.get("sample_weight"), 1.0),
                org_slug=org_slug,
                level=level,
            ))
    return out


# -----------------------------------------------------------------------------
# Position grading
# -----------------------------------------------------------------------------

def tier_for_starter_comp(starter_comp: float, has_starter: bool) -> str:
    if not has_starter or starter_comp <= 0:
        return "Empty"
    for label, threshold in TIER_THRESHOLDS:
        if starter_comp >= threshold:
            return label
    return "Hole"


def players_at_position(players: List[SlottedPlayer], position: str) -> List[SlottedPlayer]:
    """Pull all players slotted at a given position group (handles SP / RP grouping)."""
    if position == "SP":
        return [p for p in players if p.is_pitcher and p.slot_pos == "SP"]
    if position == "RP":
        return [p for p in players if p.is_pitcher and p.slot_pos in RP_ROLES]
    return [p for p in players if not p.is_pitcher and p.slot_pos == position]


def build_cell(org_slug: str, level: str, position: str,
               players: List[SlottedPlayer]) -> PositionCell:
    pool = sorted(players_at_position(players, position),
                  key=lambda p: -p.composite)
    starter = pool[0] if pool else None
    backup = pool[1] if len(pool) > 1 else None
    starter_comp = starter.composite if starter else 0.0
    top3 = [p.composite for p in pool[:3]]
    avg_top3 = sum(top3) / len(top3) if top3 else 0.0
    tier = tier_for_starter_comp(starter_comp, starter is not None)
    return PositionCell(
        org_slug=org_slug,
        level=level,
        position=position,
        starter=starter,
        backup=backup,
        depth_count=len(pool),
        starter_comp=starter_comp,
        avg_top3_comp=avg_top3,
        tier=tier,
    )


def all_positions_for_grading() -> List[str]:
    return HITTER_POSITIONS + PITCHER_GROUPS


# -----------------------------------------------------------------------------
# League-relative percentile
# -----------------------------------------------------------------------------

def compute_league_percentiles(
    all_orgs_players: Dict[str, Dict[str, List[SlottedPlayer]]]
) -> Dict[Tuple[str, str], Dict[str, float]]:
    """Return {(level, position): {org_slug: percentile (0-100)}}.

    Percentile is computed from starter_comp at each (level, pos) cell.
    Higher = better. An empty cell gets percentile 0.
    """
    # Collect (level, pos) -> [(org_slug, starter_comp)] across the league.
    bucket: Dict[Tuple[str, str], List[Tuple[str, float]]] = defaultdict(list)
    for org_slug, by_level in all_orgs_players.items():
        for level, players in by_level.items():
            for pos in all_positions_for_grading():
                cell = build_cell(org_slug, level, pos, players)
                bucket[(level, pos)].append((org_slug, cell.starter_comp))

    out: Dict[Tuple[str, str], Dict[str, float]] = {}
    for key, entries in bucket.items():
        if not entries:
            continue
        # Sort ascending; percentile = (rank index / (n-1)) * 100.
        sorted_entries = sorted(entries, key=lambda x: x[1])
        n = len(sorted_entries)
        if n == 1:
            out[key] = {sorted_entries[0][0]: 50.0}
            continue
        org_pct: Dict[str, float] = {}
        # Rank with ties getting the average rank.
        i = 0
        while i < n:
            j = i
            while j + 1 < n and sorted_entries[j + 1][1] == sorted_entries[i][1]:
                j += 1
            avg_rank = (i + j) / 2.0
            pct = (avg_rank / (n - 1)) * 100.0
            for k in range(i, j + 1):
                org_pct[sorted_entries[k][0]] = pct
            i = j + 1
        out[key] = org_pct
    return out


# -----------------------------------------------------------------------------
# Essential player detection
# -----------------------------------------------------------------------------

TIER_RANK = {
    "Elite": 4,
    "Strong": 3,
    "Average": 2,
    "Weak": 1,
    "Hole": 0,
    "Empty": 0,
}


def detect_essentials(players_by_level: Dict[str, List[SlottedPlayer]],
                      org_slug: str) -> List[EssentialFlag]:
    """Flag rank-1 starters whose loss would meaningfully hurt the org.

    A drop is considered meaningful when EITHER:
      (a) The pre-removal tier was Strong/Elite — losing a quality starter
          always matters, even if there's no backup to replace them.
      (b) There was a real backup (depth >=2) and the tier still dropped
          by 1+. This catches cases where the #2 isn't good enough to
          sustain the prior grade.

    Singletons at Average/Weak tiers are NOT flagged — they were already
    not sustainable contributors, so their loss isn't news.
    """
    essentials: List[EssentialFlag] = []
    for level, players in players_by_level.items():
        for pos in all_positions_for_grading():
            cell_before = build_cell(org_slug, level, pos, players)
            if cell_before.starter is None:
                continue
            survivors = [p for p in players if p.pid != cell_before.starter.pid]
            cell_after = build_cell(org_slug, level, pos, survivors)
            tier_before_rank = TIER_RANK.get(cell_before.tier, 0)
            tier_after_rank = TIER_RANK.get(cell_after.tier, 0)
            drop = tier_before_rank - tier_after_rank
            if drop < 1:
                continue
            had_real_backup = cell_before.depth_count >= 2
            was_quality = cell_before.tier in ("Strong", "Elite")
            if not (was_quality or had_real_backup):
                continue
            comp_drop = cell_before.starter_comp - cell_after.starter_comp
            reason = (
                f"{cell_before.tier} → {cell_after.tier} "
                f"(comp {cell_before.starter_comp:.1f} → {cell_after.starter_comp:.1f}, "
                f"Δ {comp_drop:+.1f})"
            )
            essentials.append(EssentialFlag(
                player=cell_before.starter,
                cell_before=cell_before,
                tier_after=cell_after.tier,
                starter_comp_after=cell_after.starter_comp,
                reason=reason,
            ))
    # Sort by largest tier drop, then largest composite drop.
    essentials.sort(
        key=lambda e: (
            -(TIER_RANK.get(e.cell_before.tier, 0) - TIER_RANK.get(e.tier_after, 0)),
            -(e.cell_before.starter_comp - e.starter_comp_after),
        )
    )
    return essentials


# -----------------------------------------------------------------------------
# Out-of-position starter detection
# -----------------------------------------------------------------------------

def detect_out_of_position(players_by_level: Dict[str, List[SlottedPlayer]]
                           ) -> List[SlottedPlayer]:
    """Hitter starters (rank 1 or 2) whose primary_pos differs from slot_pos.
    These mark positions where the depth chart had to slide a player off-spec."""
    out: List[SlottedPlayer] = []
    for players in players_by_level.values():
        for p in players:
            if p.is_pitcher or p.slot_rank == 0 or p.slot_rank > 2:
                continue
            if p.out_of_position:
                out.append(p)
    return out


# -----------------------------------------------------------------------------
# Build report
# -----------------------------------------------------------------------------

def build_org_report(
    org_slug: str,
    league: str,
    timestamp: str,
    players_by_level: Dict[str, List[SlottedPlayer]],
    league_pctiles: Dict[Tuple[str, str], Dict[str, float]],
) -> OrgReport:
    cells: Dict[Tuple[str, str], PositionCell] = {}
    levels = sorted(players_by_level.keys(),
                    key=lambda l: LEVEL_ORDER.index(l) if l in LEVEL_ORDER else 99)
    for level in levels:
        players = players_by_level[level]
        for pos in all_positions_for_grading():
            cell = build_cell(org_slug, level, pos, players)
            pct = league_pctiles.get((level, pos), {}).get(org_slug)
            if pct is not None:
                cell.league_pctile = pct
            cells[(level, pos)] = cell

    essentials = detect_essentials(players_by_level, org_slug)
    oop = detect_out_of_position(players_by_level)

    return OrgReport(
        org_slug=org_slug,
        org_display=org_display_from_slug(org_slug, league),
        league=league,
        timestamp=timestamp,
        levels=levels,
        cells=cells,
        essentials=essentials,
        out_of_position_starters=oop,
        players_by_level=players_by_level,
    )


# -----------------------------------------------------------------------------
# Rendering — Markdown
# -----------------------------------------------------------------------------

def fmt_cell(cell: PositionCell) -> str:
    """One-line cell summary for the matrix view."""
    if cell.starter is None:
        return f"{TIER_SYMBOL['Empty']}"
    sym = TIER_SYMBOL.get(cell.tier, "·")
    return f"{sym} {cell.starter_comp:.0f}"


def fmt_cell_with_pct(cell: PositionCell) -> str:
    if cell.starter is None:
        return TIER_SYMBOL["Empty"]
    sym = TIER_SYMBOL.get(cell.tier, "·")
    pct = f"p{cell.league_pctile:.0f}" if cell.league_pctile else "—"
    return f"{sym} {cell.starter_comp:.0f} ({pct})"


def render_org_md(rpt: OrgReport) -> str:
    lines: List[str] = []
    lines.append(f"# {rpt.org_display} — Positional Strength Report  ·  {rpt.league.upper()}")
    lines.append("")
    lines.append(f"_Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} from depth-chart batch `{rpt.timestamp}`._")
    lines.append("")
    lines.append("Cells show: tier symbol, starter composite, league percentile (p100 = best in league at that level/position).")
    lines.append("Tiers: ★★ Elite (≥60) · ★ Strong (55–60) · · Average (50–55) · ▽ Weak (45–50) · ✕ Hole (<45) · — Empty (no slotted starter).")
    lines.append("")

    # --- Section 1: Position × Level matrix ----------------------------------
    lines.append("## Position Matrix")
    lines.append("")
    header = ["Pos"] + rpt.levels
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join(["---"] * len(header)) + " |")
    for pos in HITTER_POSITIONS + PITCHER_GROUPS:
        row = [pos]
        for level in rpt.levels:
            cell = rpt.cells.get((level, pos))
            row.append(fmt_cell_with_pct(cell) if cell else TIER_SYMBOL["Empty"])
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    # --- Section 2: Holes & Weak Spots --------------------------------------
    weak_cells = [c for c in rpt.cells.values() if c.is_weak]
    weak_cells.sort(key=lambda c: (LEVEL_ORDER.index(c.level) if c.level in LEVEL_ORDER else 99,
                                    c.position))
    lines.append("## Holes & Weak Spots")
    lines.append("")
    if not weak_cells:
        lines.append("_No weak/empty positions detected. Nice problem to have._")
    else:
        lines.append("| Level | Pos | Tier | Starter | Comp | Depth | League %ile |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for c in weak_cells:
            starter = c.starter.name if c.starter else "—"
            comp = f"{c.starter_comp:.1f}" if c.starter else "—"
            pct = f"p{c.league_pctile:.0f}" if c.starter else "—"
            lines.append(f"| {c.level} | {c.position} | {c.tier} | {starter} | {comp} | {c.depth_count} | {pct} |")
    lines.append("")

    # --- Section 3: Strengths -----------------------------------------------
    strong_cells = [c for c in rpt.cells.values() if c.is_strong]
    strong_cells.sort(key=lambda c: (-c.starter_comp,
                                      LEVEL_ORDER.index(c.level) if c.level in LEVEL_ORDER else 99))
    lines.append("## Strengths")
    lines.append("")
    if not strong_cells:
        lines.append("_No clearly strong positions._")
    else:
        lines.append("| Level | Pos | Tier | Starter | Comp | Backup | Backup Comp | Top3 Avg | League %ile |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for c in strong_cells:
            starter = c.starter.name if c.starter else "—"
            backup = c.backup.name if c.backup else "—"
            backup_c = f"{c.backup.composite:.1f}" if c.backup else "—"
            lines.append(
                f"| {c.level} | {c.position} | {c.tier} | {starter} | {c.starter_comp:.1f} "
                f"| {backup} | {backup_c} | {c.avg_top3_comp:.1f} | p{c.league_pctile:.0f} |"
            )
    lines.append("")

    # --- Section 4: Essential players --------------------------------------
    lines.append("## Essential Players (Tier Collapses Without Them)")
    lines.append("")
    lines.append("_Removing this player drops their position's tier at this level. The fewer of these you have, the more resilient the org._")
    lines.append("")
    if not rpt.essentials:
        lines.append("_None — every starter has a viable replacement at their position/level._")
    else:
        lines.append("| Level | Pos | Player | Composite | Without Them | After Comp | Note |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for e in rpt.essentials:
            lines.append(
                f"| {e.player.level} | {e.cell_before.position} | {e.player.name} "
                f"| {e.player.composite:.1f} | {e.tier_after} | {e.starter_comp_after:.1f} | {e.reason} |"
            )
    lines.append("")

    # --- Section 5: Out-of-position starters --------------------------------
    if rpt.out_of_position_starters:
        lines.append("## Out-of-Position Starters")
        lines.append("")
        lines.append("_Hitters slotted away from their primary position because nobody better was available — a softer signal that the slot is thin._")
        lines.append("")
        lines.append("| Level | Player | Primary | Slotted At | Composite |")
        lines.append("| --- | --- | --- | --- | --- |")
        for p in sorted(rpt.out_of_position_starters,
                        key=lambda x: (LEVEL_ORDER.index(x.level) if x.level in LEVEL_ORDER else 99,
                                       x.slot_pos)):
            lines.append(
                f"| {p.level} | {p.name} | {p.primary_pos} | {p.slot_pos} (rank {p.slot_rank}) | {p.composite:.1f} |"
            )
        lines.append("")

    # --- Section 6: League comparison summary -------------------------------
    lines.append("## League Comparison Summary")
    lines.append("")
    avg_pctile_by_pos: Dict[str, List[float]] = defaultdict(list)
    for (lvl, pos), cell in rpt.cells.items():
        if cell.starter is not None:
            avg_pctile_by_pos[pos].append(cell.league_pctile)
    if not avg_pctile_by_pos:
        lines.append("_League percentile data unavailable (only one org in batch?)._")
    else:
        lines.append("_Average percentile across all levels where the org has a starter slotted._")
        lines.append("")
        lines.append("| Position | Avg %ile | Levels w/ Starter |")
        lines.append("| --- | --- | --- |")
        rows = sorted(avg_pctile_by_pos.items(), key=lambda kv: -sum(kv[1]) / len(kv[1]))
        for pos, vals in rows:
            avg = sum(vals) / len(vals)
            lines.append(f"| {pos} | p{avg:.0f} | {len(vals)} |")
    lines.append("")

    # --- Section 7: Trade Targets / Surplus heuristics ----------------------
    lines.append("## Deal-From / Target Signals")
    lines.append("")
    deal_from = []
    targets = []
    for pos in HITTER_POSITIONS + PITCHER_GROUPS:
        levels_with_starter = [c for c in rpt.cells.values()
                                if c.position == pos and c.starter is not None]
        strong_levels = [c for c in levels_with_starter if c.is_strong]
        weak_levels = [(lvl, c) for (lvl, p), c in rpt.cells.items()
                       if p == pos and c.is_weak]
        # Surplus: 2+ levels strong AND ML or AAA is strong
        if len(strong_levels) >= 2 and any(c.level in ("ML", "AAA") for c in strong_levels):
            avg_pct = sum(c.league_pctile for c in strong_levels) / len(strong_levels)
            deal_from.append((pos, len(strong_levels), avg_pct,
                              ", ".join(f"{c.level} ({c.starter.name}, {c.starter_comp:.0f})"
                                        for c in strong_levels)))
        # Need: ML or AAA is weak/empty
        ml_aaa_weak = [c for c in levels_with_starter
                       if c.level in ("ML", "AAA") and c.is_weak]
        ml_aaa_missing = [(lvl, p) for (lvl, p), c in rpt.cells.items()
                          if p == pos and c.level in ("ML", "AAA") and c.starter is None]
        if ml_aaa_weak or ml_aaa_missing:
            targets.append((pos, ml_aaa_weak, ml_aaa_missing))

    lines.append("### Surplus — positions to deal from")
    lines.append("")
    if deal_from:
        lines.append("| Pos | Strong Levels | Avg %ile | Detail |")
        lines.append("| --- | --- | --- | --- |")
        for pos, n, avg_pct, detail in sorted(deal_from, key=lambda x: -x[1]):
            lines.append(f"| {pos} | {n} | p{avg_pct:.0f} | {detail} |")
    else:
        lines.append("_No clear surplus positions._")
    lines.append("")

    lines.append("### Need — positions to target in trade/FA/draft")
    lines.append("")
    if targets:
        lines.append("| Pos | Issue |")
        lines.append("| --- | --- |")
        for pos, weak, missing in targets:
            issues = []
            for c in weak:
                issues.append(f"{c.level} {c.tier} ({c.starter_comp:.0f})")
            for lvl, _ in missing:
                issues.append(f"{lvl} empty")
            lines.append(f"| {pos} | {'; '.join(issues)} |")
    else:
        lines.append("_No urgent needs at ML/AAA._")
    lines.append("")

    return "\n".join(lines) + "\n"


# -----------------------------------------------------------------------------
# Rendering — CSVs
# -----------------------------------------------------------------------------

def write_positions_csv(rpt: OrgReport, path: Path) -> None:
    fieldnames = [
        "org", "level", "position", "tier", "league_pctile",
        "starter_name", "starter_composite", "starter_vos",
        "backup_name", "backup_composite",
        "depth_count", "avg_top3_composite",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for level in rpt.levels:
            for pos in all_positions_for_grading():
                c = rpt.cells.get((level, pos))
                if c is None:
                    continue
                w.writerow({
                    "org": rpt.org_display,
                    "level": level,
                    "position": pos,
                    "tier": c.tier,
                    "league_pctile": f"{c.league_pctile:.1f}" if c.starter else "",
                    "starter_name": c.starter.name if c.starter else "",
                    "starter_composite": f"{c.starter_comp:.2f}" if c.starter else "",
                    "starter_vos": f"{c.starter.vos:.2f}" if c.starter else "",
                    "backup_name": c.backup.name if c.backup else "",
                    "backup_composite": f"{c.backup.composite:.2f}" if c.backup else "",
                    "depth_count": c.depth_count,
                    "avg_top3_composite": f"{c.avg_top3_comp:.2f}" if c.depth_count else "",
                })


def write_player_details_csv(rpt: OrgReport, path: Path) -> None:
    essential_pids = {(e.player.level, e.player.pid): e for e in rpt.essentials}
    fieldnames = [
        "org", "level", "pid", "name", "age",
        "primary_pos", "slot_pos", "slot_rank", "tier_label",
        "is_pitcher", "proj_role", "vos", "composite",
        "out_of_position", "essential", "essential_reason",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for level in rpt.levels:
            for p in sorted(rpt.players_by_level[level],
                            key=lambda x: (x.is_pitcher, x.slot_pos, x.slot_rank)):
                key = (p.level, p.pid)
                e = essential_pids.get(key)
                w.writerow({
                    "org": rpt.org_display,
                    "level": p.level,
                    "pid": p.pid,
                    "name": p.name,
                    "age": p.age,
                    "primary_pos": p.primary_pos,
                    "slot_pos": p.slot_pos,
                    "slot_rank": p.slot_rank,
                    "tier_label": p.tier_label,
                    "is_pitcher": p.is_pitcher,
                    "proj_role": p.proj_role,
                    "vos": f"{p.vos:.2f}",
                    "composite": f"{p.composite:.2f}",
                    "out_of_position": p.out_of_position,
                    "essential": e is not None,
                    "essential_reason": e.reason if e else "",
                })


# -----------------------------------------------------------------------------
# League-wide summary
# -----------------------------------------------------------------------------

def render_league_summary_md(reports: List[OrgReport], league: str, timestamp: str) -> str:
    lines: List[str] = []
    lines.append(f"# {league.upper()} League — Org Strength Rollup")
    lines.append("")
    lines.append(f"_Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} from batch `{timestamp}`._")
    lines.append("")
    lines.append("## Average %ile per Position (across all levels with a starter)")
    lines.append("")

    # Compute org × position avg percentile
    matrix: Dict[str, Dict[str, float]] = {}
    positions = HITTER_POSITIONS + PITCHER_GROUPS
    for r in reports:
        row: Dict[str, float] = {}
        for pos in positions:
            pcts = [c.league_pctile for c in r.cells.values()
                    if c.position == pos and c.starter is not None]
            row[pos] = sum(pcts) / len(pcts) if pcts else 0.0
        matrix[r.org_display] = row

    header = ["Org"] + positions + ["OVR"]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join(["---"] * len(header)) + " |")
    rows = []
    for org_name, row in matrix.items():
        ovr = sum(row.values()) / len(row) if row else 0.0
        rows.append((org_name, row, ovr))
    rows.sort(key=lambda x: -x[2])
    for org_name, row, ovr in rows:
        cells = [f"p{row[pos]:.0f}" for pos in positions]
        lines.append("| " + " | ".join([org_name] + cells + [f"p{ovr:.0f}"]) + " |")
    lines.append("")

    # Hole / surplus highlights per org
    lines.append("## Holes & Surpluses (ML + AAA only)")
    lines.append("")
    lines.append("| Org | Holes (ML/AAA) | Surplus Positions |")
    lines.append("| --- | --- | --- |")
    for r in reports:
        holes = []
        for (lvl, pos), c in r.cells.items():
            if lvl in ("ML", "AAA") and c.is_weak:
                holes.append(f"{lvl} {pos}")
        # Surplus = 2+ levels strong at the position
        surplus_positions = []
        for pos in HITTER_POSITIONS + PITCHER_GROUPS:
            strong_lvls = [c for c in r.cells.values()
                           if c.position == pos and c.is_strong]
            if len(strong_lvls) >= 2:
                surplus_positions.append(pos)
        lines.append(
            f"| {r.org_display} | "
            f"{', '.join(holes) if holes else '—'} | "
            f"{', '.join(surplus_positions) if surplus_positions else '—'} |"
        )
    lines.append("")

    return "\n".join(lines) + "\n"


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main() -> None:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(asctime)s [%(levelname)s] %(message)s")

    league = args.league.lower()
    depth_dir = args.depth_dir or (SCRIPT_DIR / league / "depth")
    output_dir = args.output_dir or (SCRIPT_DIR / league / "org_depth")
    output_dir.mkdir(parents=True, exist_ok=True)

    if not depth_dir.exists():
        raise FileNotFoundError(f"Depth dir not found: {depth_dir}")

    timestamp = args.timestamp or find_latest_timestamp(depth_dir)
    logger.info("Using batch timestamp: %s", timestamp)

    batch = discover_batch(depth_dir, timestamp)
    logger.info("Batch contains %d orgs.", len(batch))

    # Load all orgs' players (needed for league percentiles regardless of single-org run).
    all_orgs_players: Dict[str, Dict[str, List[SlottedPlayer]]] = {}
    for org_slug, levels_map in batch.items():
        by_level: Dict[str, List[SlottedPlayer]] = {}
        for level, csv_path in levels_map.items():
            try:
                by_level[level] = load_slotted_players(csv_path, org_slug, level)
            except Exception as exc:
                logger.warning("Failed to load %s: %s", csv_path, exc)
        if by_level:
            all_orgs_players[org_slug] = by_level

    if not all_orgs_players:
        raise RuntimeError("No orgs successfully loaded.")

    league_pctiles = compute_league_percentiles(all_orgs_players)

    # Decide which orgs to report on.
    if args.all_orgs:
        target_slugs = sorted(all_orgs_players.keys())
    else:
        target_slug = resolve_org_slug(args.org, args.org_slug, batch, league=league)
        target_slugs = [target_slug]

    reports: List[OrgReport] = []
    for slug in target_slugs:
        if slug not in all_orgs_players:
            logger.warning("Skipping %s — no data loaded.", slug)
            continue
        rpt = build_org_report(
            org_slug=slug,
            league=league,
            timestamp=timestamp,
            players_by_level=all_orgs_players[slug],
            league_pctiles=league_pctiles,
        )
        md_path = output_dir / f"{slug}_strength_{timestamp}.md"
        pos_csv = output_dir / f"{slug}_strength_{timestamp}_positions.csv"
        det_csv = output_dir / f"{slug}_strength_{timestamp}_player_details.csv"
        md_path.write_text(render_org_md(rpt), encoding="utf-8")
        write_positions_csv(rpt, pos_csv)
        write_player_details_csv(rpt, det_csv)
        logger.info("Wrote %s (+ 2 CSVs)", md_path.name)
        reports.append(rpt)

    if args.all_orgs and not args.no_league_summary and reports:
        league_md = output_dir / f"league_strength_{timestamp}.md"
        league_md.write_text(render_league_summary_md(reports, league, timestamp), encoding="utf-8")
        logger.info("Wrote %s", league_md.name)


if __name__ == "__main__":
    main()

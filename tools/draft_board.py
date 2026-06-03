#!/usr/bin/env python3
"""
draft_board.py — Generate a suggested draft board from:
  1. Latest org strength analysis at {league}/org_depth/{team}_strength_*_positions.csv
  2. A draft pool analysis at {league}/drafts/{draft}/05_draft_pool.md

Produces two boards in one MD file + a combined CSV:
  - Board A: Best Player Available, sorted by Ideal Value
  - Board B: Need-Adjusted, scored at best-fit position as pos_value + alpha * need(pos)

Usage:
  py draft_board.py --team seattle_whalers
  py draft_board.py --team seattle_whalers --league ndl --draft 2055_draft --need-alpha 1.0
"""

# --- repo-root + core/ path bootstrap ---
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _p in (_ROOT, _os.path.join(_ROOT, "core")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
# --- end bootstrap ---

import argparse
import csv
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Level weighting: higher = more urgent (closer to ML)
LEVEL_WEIGHTS = {"ML": 3.0, "AAA": 3.0, "AA": 2.0, "A+": 1.5, "A": 1.0, "A-": 0.5, "R": 0.3}

# Tier weighting: positive = need, negative = surplus
TIER_WEIGHTS = {
    "Hole": 1.0,
    "Empty": 1.0,
    "Weak": 0.5,
    "Average": 0.0,
    "Strong": -0.3,
    "Elite": -0.5,
}

# Extra boost when ML/AAA is Hole/Empty (the "explicit Need table" signal)
EXPLICIT_NEED_BOOST = 2.0

DEFAULT_NEED_ALPHA = 1.0
DEFAULT_TOP_N = 200


# ---------- Strength file resolution ----------

def find_latest_strength(org_depth_dir: Path, team: str) -> Tuple[Path, Path]:
    """Find the newest org_depth positions CSV for ``team``.

    Accepts two filename conventions:

      1. **Current** (org_depth_analysis.py output):
         ``org_depth_analysis_{team}_positions.csv`` — no timestamp.
      2. **Legacy** (older runs in some leagues):
         ``{team}_strength_{YYYYMMDD_HHMMSS}_positions.csv`` — with timestamp.

    When matches exist for both patterns, picks the most recently modified
    file (mtime) so a current re-run wins over stale legacy files.

    The companion MD path is derived from the chosen CSV by suffix swap
    (used only for the inputs footer in the report — never actually read).
    """
    patterns = [
        f"org_depth_analysis_{team}_positions.csv",  # current convention
        f"{team}_strength_*_positions.csv",          # legacy convention
    ]

    matches: List[Path] = []
    for pat in patterns:
        matches.extend(org_depth_dir.glob(pat))

    if not matches:
        raise FileNotFoundError(
            f"No strength file found for team '{team}' under {org_depth_dir}/. "
            f"Tried patterns: {', '.join(patterns)}. "
            f"Run `py org_depth_analysis.py --league {{league}} --org \"{{team name}}\" --csv` "
            f"to generate the positions CSV."
        )

    # Newest by mtime — handles mixed old+new files in the same dir.
    positions_csv = max(matches, key=lambda p: p.stat().st_mtime)
    md = positions_csv.with_name(positions_csv.name.replace("_positions.csv", ".md"))
    return positions_csv, md


def load_positions(positions_csv: Path) -> List[Dict[str, str]]:
    with open(positions_csv, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def compute_need_scores(
    position_rows: List[Dict[str, str]],
) -> Tuple[Dict[str, float], Dict[str, List[str]]]:
    """
    Tally per-position need score from the positions CSV.
    Each (level, pos) row contributes level_weight * tier_weight, plus an explicit-need
    boost when ML or AAA is Hole/Empty. Returns (scores, source_labels_for_display).
    """
    scores: Dict[str, float] = defaultdict(float)
    sources: Dict[str, List[str]] = defaultdict(list)

    for row in position_rows:
        lvl = (row.get("level") or "").strip()
        pos = (row.get("position") or "").strip()
        tier = (row.get("tier") or "").strip()
        if not (lvl and pos and tier):
            continue

        lvl_w = LEVEL_WEIGHTS.get(lvl, 0.0)
        tier_w = TIER_WEIGHTS.get(tier, 0.0)
        contribution = lvl_w * tier_w

        if lvl in ("ML", "AAA") and tier in ("Hole", "Empty"):
            contribution += EXPLICIT_NEED_BOOST

        if contribution != 0:
            scores[pos] += contribution
        # Show only "need" sources in the reference table, not surpluses
        if tier in ("Hole", "Empty", "Weak"):
            sources[pos].append(f"{lvl} {tier}")

    return dict(scores), dict(sources)


# ---------- Draft pool parsing (05_draft_pool.md) ----------

_VIABLE_RE = re.compile(r"^\s*([A-Z0-9+]+)\s*:\s*(-?[\d.]+)\s*$")


def parse_draft_pool_md(md_path: Path) -> List[Dict[str, Any]]:
    """Parse the 05_draft_pool.md table into a list of player dicts (one per row)."""
    text = md_path.read_text(encoding="utf-8")
    lines = text.splitlines()

    header_idx: Optional[int] = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith("| Rank |"):
            header_idx = i
            break
    if header_idx is None:
        raise ValueError(f"No table header found in {md_path}")

    header_cells = [c.strip() for c in lines[header_idx].strip().strip("|").split("|")]
    data_start = header_idx + 2  # skip the |---|---|... separator

    players: List[Dict[str, Any]] = []
    for line in lines[data_start:]:
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != len(header_cells):
            continue
        row = dict(zip(header_cells, cells))
        # Parse both axes. Under v10 the MD is sorted by Outlook; Ideal_Value
        # is preserved as a cross-reference column and as a fallback for
        # pre-v10 MDs that don't have an Outlook column.
        try:
            row["_ideal_value"] = float(row.get("Ideal Value", "") or 0.0)
        except ValueError:
            continue
        try:
            outlook_raw = row.get("Outlook", "").strip()
            row["_outlook"] = float(outlook_raw) if outlook_raw else None
        except ValueError:
            row["_outlook"] = None
        # Primary axis = Outlook when available, Ideal_Value otherwise.
        row["_primary_value"] = row["_outlook"] if row["_outlook"] is not None else row["_ideal_value"]
        row["_idx"] = len(players)  # unique key for cross-ranking
        players.append(row)

    if not players:
        raise ValueError(f"No player rows parsed from {md_path}")
    return players


def parse_viable_potentials(cell: str) -> Dict[str, float]:
    """Parse 'DH:85.90, CF:80.10' into {'DH': 85.90, 'CF': 80.10}."""
    out: Dict[str, float] = {}
    if not cell:
        return out
    for chunk in cell.split(","):
        m = _VIABLE_RE.match(chunk.strip())
        if m:
            try:
                out[m.group(1)] = float(m.group(2))
            except ValueError:
                pass
    return out


# ---------- Need-board scoring ----------

def score_need_board(
    players: List[Dict[str, Any]],
    need_scores: Dict[str, float],
    alpha: float,
) -> List[Dict[str, Any]]:
    """For each player, pick the position (projected or viable) that maximises
    pos_value + alpha * need_score(pos). Annotate in-place-style copies."""
    out: List[Dict[str, Any]] = []
    for p in players:
        projected = (p.get("Projected Position") or "").strip()
        ideal = p["_ideal_value"]

        candidates: List[Tuple[str, float]] = []
        if projected:
            candidates.append((projected, ideal))
        for pos, val in parse_viable_potentials(p.get("Viable Pos Potentials", "")).items():
            candidates.append((pos, val))
        if not candidates:
            candidates = [("", ideal)]

        best_pos, best_val, best_need = candidates[0][0], candidates[0][1], need_scores.get(candidates[0][0], 0.0)
        best_score = best_val + alpha * best_need
        for pos, val in candidates[1:]:
            need = need_scores.get(pos, 0.0)
            s = val + alpha * need
            if s > best_score:
                best_score, best_pos, best_val, best_need = s, pos, val, need

        ann = dict(p)
        ann["_best_fit_pos"] = best_pos
        ann["_best_pos_val"] = best_val
        ann["_need_bonus"] = alpha * best_need
        ann["_board_score"] = best_score
        out.append(ann)
    return out


# ---------- Output ----------

def write_outputs(
    team: str,
    league: str,
    draft_label: str,
    annotated: List[Dict[str, Any]],
    need_scores: Dict[str, float],
    need_sources: Dict[str, List[str]],
    output_dir: Path,
    top_n: int,
    alpha: float,
    strength_file: Path,
    draft_pool_file: Path,
) -> Tuple[Path, Path, Path, Path]:
    """Write the draft board outputs. Returns ``(md, csv, board_a_txt,
    board_b_txt)``. The two ``.txt`` files carry one player ID per line in
    rank order — Board A by Outlook (BPA), Board B by need-adjusted score
    — suitable for direct import into StatsPlus draft prep.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    md_path = output_dir / f"draft_board_{team}_{timestamp}.md"
    csv_path = output_dir / f"draft_board_{team}_{timestamp}.csv"
    board_a_txt_path = output_dir / f"draft_board_{team}_{timestamp}_board_a.txt"
    board_b_txt_path = output_dir / f"draft_board_{team}_{timestamp}_board_b.txt"

    # Board A sorts by the v10 primary axis (Outlook when populated;
    # Ideal_Value fallback). This matches draft_pool_analysis's MD ordering
    # so the board and the master pool MD agree on ranking.
    bpa_sorted = sorted(annotated, key=lambda p: -p["_primary_value"])
    bpa_rank = {p["_idx"]: i + 1 for i, p in enumerate(bpa_sorted)}

    need_sorted = sorted(annotated, key=lambda p: -p["_board_score"])
    need_rank = {p["_idx"]: i + 1 for i, p in enumerate(need_sorted)}

    team_title = team.replace("_", " ").title()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines: List[str] = [
        f"# Suggested Draft Board — {team_title}",
        f"_{league.upper()} · {draft_label} · generated {now_str}_",
        "",
        f"Inputs: `{strength_file.name}` · `{draft_pool_file.name}`",
        "",
        "## How to read",
        "",
        "- **Board A** is pure BPA. Under v10 it sorts by Outlook (Career-weights × Pot* composite; falls back to Ideal Value for pre-v10 MDs).",
        f"- **Board B** is need-adjusted. Each player is scored at their best-fit position as `pos_value + α × need_score(pos)`, α = {alpha}. `pos_value` comes from the heuristic Reach per-position composite (the same column run_vos has always emitted).",
        f"- **Need scores** come from the positions CSV: each (level, pos) row contributes `level_weight × tier_weight`, plus a +{EXPLICIT_NEED_BOOST} boost when ML/AAA is Hole/Empty.",
        "- **Δ** = BPA rank − Need rank. Positive Δ = player climbed because their position is a need.",
        "",
        "## Position Need Scores",
        "",
        "| Pos | Need Score | Sources |",
        "| --- | --- | --- |",
    ]
    pos_order = sorted(need_scores.keys(), key=lambda p: (-need_scores[p], p))
    for pos in pos_order:
        srcs = ", ".join(need_sources.get(pos, [])) or "—"
        lines.append(f"| {pos} | {need_scores[pos]:+.2f} | {srcs} |")

    lines.extend([
        "",
        f"## Board A — Best Player Available (Top {top_n})",
        "",
        "_**Sorted by Outlook** (v10 Career-weights × Pot* composite — \"if this prospect "
        "realizes their ceiling, how good will they be as an MLB player?\"). Ideal Value "
        "is the legacy heuristic Reach composite, preserved as a cross-reference. Reach "
        "is the logistic P(reach MLB) score; Career is the current-rating MLB projection. "
        "Pers and Prone surface the v10 personality and injury signals._",
        "",
        "| Rank | ID | Name | Pos | Age | Proj | Outlook | Ideal Value | Reach | Career | Pers | Prone | Tier |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ])
    for i, p in enumerate(bpa_sorted[:top_n], 1):
        # Columns flow through from 05_draft_pool.md by header name. Missing
        # columns (e.g. when running against a pre-v10 MD) render as empty.
        lines.append(
            f"| {i} | {p.get('ID', '')} | {p.get('Name', '')} | {p.get('Pos', '')} | {p.get('Age', '')} | "
            f"{p.get('Projected Position', '')} | {p.get('Outlook', '')} | {p['_ideal_value']:.2f} | "
            f"{p.get('Reach', '')} | {p.get('Career', '')} | "
            f"{p.get('Pers', '')} | {p.get('Prone', '')} | {p.get('Tier', '')} |"
        )

    lines.extend([
        "",
        f"## Board B — Need-Adjusted (Top {top_n})",
        "",
        "| Rank | Δ | ID | Name | Pos | Age | Best Fit | Pos Value | Need Bonus | Board Score | BPA Rank |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ])
    for i, p in enumerate(need_sorted[:top_n], 1):
        bpa_r = bpa_rank[p["_idx"]]
        delta = bpa_r - i
        delta_str = f"{delta:+d}" if delta else "·"
        lines.append(
            f"| {i} | {delta_str} | {p.get('ID', '')} | {p.get('Name', '')} | {p.get('Pos', '')} | {p.get('Age', '')} | "
            f"{p['_best_fit_pos']} | {p['_best_pos_val']:.2f} | {p['_need_bonus']:+.2f} | "
            f"{p['_board_score']:.2f} | {bpa_r} |"
        )

    md_path.write_text("\n".join(lines), encoding="utf-8")

    # ---- CSV: full pool, both ranks side by side ----
    # v10 additions (outlook, reach, career, blend, pers, prone, outlook_pos,
    # outlook_reason, ready) round out the columns downstream consumers can
    # filter or join on. Per project convention CSV column names are
    # lowercase; MD column labels are capitalized.
    fieldnames = [
        "bpa_rank", "need_rank", "delta",
        "id", "name", "pos", "age", "projected_pos", "tier",
        "ideal_value", "outlook", "reach", "career", "blend",
        "pers", "prone", "ready",
        "outlook_pos", "outlook_reason",
        "best_fit_pos", "best_pos_val", "need_bonus", "board_score",
    ]
    rows_for_csv = sorted(annotated, key=lambda p: bpa_rank[p["_idx"]])
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for p in rows_for_csv:
            bpa_r = bpa_rank[p["_idx"]]
            need_r = need_rank[p["_idx"]]
            w.writerow({
                "bpa_rank": bpa_r,
                "need_rank": need_r,
                "delta": bpa_r - need_r,
                "id": p.get("ID", ""),
                "name": p.get("Name", ""),
                "pos": p.get("Pos", ""),
                "age": p.get("Age", ""),
                "projected_pos": p.get("Projected Position", ""),
                "tier": p.get("Tier", ""),
                "ideal_value": f"{p['_ideal_value']:.2f}",
                "outlook": p.get("Outlook", ""),
                "reach": p.get("Reach", ""),
                "career": p.get("Career", ""),
                "blend": p.get("Blend", ""),
                "pers": p.get("Pers", ""),
                "prone": p.get("Prone", ""),
                "ready": p.get("Ready", ""),
                "outlook_pos": p.get("Outlook Pos", ""),
                "outlook_reason": p.get("Outlook Reason", ""),
                "best_fit_pos": p["_best_fit_pos"],
                "best_pos_val": f"{p['_best_pos_val']:.2f}",
                "need_bonus": f"{p['_need_bonus']:+.2f}",
                "board_score": f"{p['_board_score']:.2f}",
            })

    # ---- Plain ID-per-line txt files for StatsPlus import ----
    # Top-N in rank order, no header, no commentary. Honors the same
    # --top flag that limits the MD tables, so MD/CSV/txt stay in sync.
    # Players with no ID are skipped (defensive — shouldn't happen for
    # parsed-from-MD rows).
    def _write_id_list(path: Path, ordered: List[Dict[str, Any]], limit: int) -> int:
        ids: List[str] = []
        for p in ordered[:limit]:
            pid = str(p.get("ID") or "").strip()
            if pid:
                ids.append(pid)
        path.write_text("\n".join(ids) + ("\n" if ids else ""), encoding="utf-8")
        return len(ids)

    _write_id_list(board_a_txt_path, bpa_sorted, top_n)
    _write_id_list(board_b_txt_path, need_sorted, top_n)

    return md_path, csv_path, board_a_txt_path, board_b_txt_path


# ---------- CLI ----------

def main() -> None:
    script_dir = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="Generate a suggested draft board from org strength + draft pool analyses.",
    )
    parser.add_argument("--team", required=True, help="Team slug, e.g. seattle_whalers")
    parser.add_argument("--league", default="ndl", help="League folder (default: ndl)")
    parser.add_argument("--draft", default=None,
                        help="Draft folder name under {league}/drafts/ (default: newest)")
    parser.add_argument("--strength", default=None,
                        help="Override path to *_positions.csv")
    parser.add_argument("--draft-pool", default=None,
                        help="Override path to 05_draft_pool.md")
    parser.add_argument("--need-alpha", type=float, default=DEFAULT_NEED_ALPHA,
                        help=f"Weight on need_score (default {DEFAULT_NEED_ALPHA})")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP_N,
                        help=f"Players to show in each board (default {DEFAULT_TOP_N})")
    parser.add_argument("--output-dir", default=None,
                        help="Output directory (default: the draft folder)")
    args = parser.parse_args()

    league_dir = script_dir / args.league
    if not league_dir.exists():
        print(f"Error: league directory not found: {league_dir}", file=sys.stderr)
        sys.exit(1)

    # Resolve strength file
    if args.strength:
        positions_csv = Path(args.strength)
        if not positions_csv.exists():
            print(f"Error: strength file not found: {positions_csv}", file=sys.stderr)
            sys.exit(1)
    else:
        try:
            positions_csv, _ = find_latest_strength(league_dir / "org_depth", args.team)
        except FileNotFoundError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    # Resolve draft folder + pool file
    if args.draft_pool:
        draft_pool_md = Path(args.draft_pool)
        draft_folder = draft_pool_md.parent
    else:
        drafts_root = league_dir / "drafts"
        if args.draft:
            draft_folder = drafts_root / args.draft
        else:
            candidates = sorted(
                (p for p in drafts_root.iterdir() if p.is_dir()),
                key=lambda p: p.stat().st_mtime,
            )
            if not candidates:
                print(f"Error: no draft folders found in {drafts_root}", file=sys.stderr)
                sys.exit(1)
            draft_folder = candidates[-1]
        draft_pool_md = draft_folder / "05_draft_pool.md"

    if not draft_pool_md.exists():
        print(f"Error: draft pool md not found: {draft_pool_md}", file=sys.stderr)
        sys.exit(1)

    output_dir = Path(args.output_dir) if args.output_dir else draft_folder
    output_dir.mkdir(parents=True, exist_ok=True)

    draft_label = draft_folder.name

    print(f"Strength:   {positions_csv}")
    print(f"Draft pool: {draft_pool_md}")
    print(f"Output:     {output_dir}")
    print()

    print("Computing need scores...")
    need_scores, need_sources = compute_need_scores(load_positions(positions_csv))

    print("Parsing draft pool...")
    players = parse_draft_pool_md(draft_pool_md)
    print(f"  {len(players)} players loaded")

    print(f"Scoring need board (alpha={args.need_alpha})...")
    annotated = score_need_board(players, need_scores, args.need_alpha)

    print("Writing outputs...")
    md_path, csv_path, board_a_txt, board_b_txt = write_outputs(
        team=args.team,
        league=args.league,
        draft_label=draft_label,
        annotated=annotated,
        need_scores=need_scores,
        need_sources=need_sources,
        output_dir=output_dir,
        top_n=args.top,
        alpha=args.need_alpha,
        strength_file=positions_csv,
        draft_pool_file=draft_pool_md,
    )

    print()
    print(f"  [ok] {md_path}")
    print(f"  [ok] {csv_path}")
    print(f"  [ok] {board_a_txt}  (StatsPlus import: BPA / Outlook order)")
    print(f"  [ok] {board_b_txt}  (StatsPlus import: need-adjusted order)")


if __name__ == "__main__":
    main()

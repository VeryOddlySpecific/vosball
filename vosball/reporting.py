"""vosball.reporting — output writers for VOS evaluations.

write_output_csv emits the full evaluation CSV (fixed column order, optional
draft + contract columns); _write_eval_summary_md emits the Obsidian-friendly
Markdown summary. Both take an explicit output path. Lifted verbatim from
run_vos.py in the Phase 3 extraction — output unchanged (CSV guarded by
tests/test_golden.py).
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any, Dict, List

from vosball.engine.constants import HITTER_POSITIONS
from vosball.data.loaders import CONTRACT_FIELDS

logger = logging.getLogger(__name__)


def write_output_csv(
    rows: List[Dict[str, Any]],
    path: Path,
    draft_mode: bool = False,
    include_contracts: bool = False,
) -> None:
    if not rows:
        logger.warning("No rows to write")
        return
    cols = [
        "ID", "Name", "Pos", "Age", "Team", "Org", "League_Level",
        "VOS_Reach", "VOS_Career", "VOS_Blended", "VOS_Ceiling", "Ceiling_Tier",
        "Arch_Career_WAR", "Arch_Career_WAR_Hi",
        "Remaining_WAR", "Remaining_WAR_Hi", "Proj_Debut_Age",
        "VOS_Score", "VOS_Potential", "VOS_Tier", "VOS_Potential_Tier",
        "Batting_Score", "Batting_Potential", "Defense_Score", "Baserunning_Score",
        "Pitching_Ability_Score", "Pitching_Ability_Potential", "Pitching_Arsenal_Score",
        "Development_Adj", "Age_Adj", "Personality_Adj",
        "Park_Name", "Park_Applied", "Prone",
        "BABIP", "PotBABIP", "PBABIP", "PotPBABIP",
    ]
    if draft_mode:
        cols.insert(cols.index("Development_Adj") + 1, "Readiness_Adj")
        cols.insert(cols.index("Personality_Adj") + 1, "Draft_Age_Adj")
        cols.insert(cols.index("Draft_Age_Adj") + 1, "Draft_RP_Penalty")
    pos_cols = [f"{p}_Score" for p in HITTER_POSITIONS]
    pos_pot_cols = [f"{p}_Potential" for p in HITTER_POSITIONS]
    cols += pos_cols + pos_pot_cols
    cols += [
        "Projected_Top_Score", "Projected_Second_Score", "Projected_Margin",
        "Projected_Margin_Tier", "Projected_Viable_Positions", "Projected_Viable_Pos_List",
    ]
    cols += ["Current_Position", "Projected_Position", "Ideal_Value"]
    if include_contracts:
        cols += [f"Contract_{f}" for f in CONTRACT_FIELDS]
        cols += [f"ContractExtension_{f}" for f in CONTRACT_FIELDS]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _write_eval_summary_md(rows: List[Dict[str, Any]], path: Path, league: str) -> None:
    """Markdown summary for Obsidian. Shows all three v5 scores side by side."""
    md_cols = ["Name", "Pos", "Age", "Team", "Org", "League_Level",
               "VOS_Reach", "VOS_Career", "VOS_Blended"]

    def _row(r: Dict[str, Any]) -> str:
        cells = [str(r.get(c, "")) for c in md_cols]
        return "| " + " | ".join(cells) + " |"

    header = "| " + " | ".join(md_cols) + " |"
    sep = "| " + " | ".join("---" for _ in md_cols) + " |"

    mlb_rows = sorted(
        [r for r in rows if str(r.get("League_Level", "")).strip().upper() in ("MLB", "AAA")],
        key=lambda r: float(r.get("VOS_Career") or 0),
        reverse=True,
    )[:50]
    prospect_rows = sorted(
        [r for r in rows if str(r.get("League_Level", "")).strip().upper() not in ("MLB",)],
        key=lambda r: float(r.get("VOS_Reach") or 0),
        reverse=True,
    )[:75]

    lines: List[str] = [
        f"# Evaluation Summary — {league.upper()}  (v5)",
        "",
        f"_Generated from `{path.name.replace('.md', '.csv')}`._",
        "",
        "## Top MLB/AAA Players by VOS Career",
        "",
        header, sep,
    ]
    lines += [_row(r) for r in mlb_rows]
    lines += [
        "",
        "## Top Prospects by VOS Reach",
        "",
        header, sep,
    ]
    lines += [_row(r) for r in prospect_rows]
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Wrote evaluation summary MD: %s", path)

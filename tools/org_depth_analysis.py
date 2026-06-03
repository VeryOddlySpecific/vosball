"""
Organizational Depth Analysis Tool

Analyzes organizational depth across positions, skill sets, and levels to identify
weak spots, stockpiles, and strategic opportunities for draft/acquisition focus.
Reads VOS v2 evaluation_summary CSV output (with legacy column fallbacks).
"""

# --- tools/ -> repo-root bootstrap (added during tools/ move) ---
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
# --- end bootstrap ---

import argparse
import csv
import html as html_module
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent.parent


def _write_md_table(path: Path, headers: List[str], data_rows: List[List[Any]], title: str = "") -> None:
    """Write a simple Markdown table file for Obsidian quick-reference."""
    lines: List[str] = []
    if title:
        lines += [f"## {title}", ""]
    lines.append("| " + " | ".join(str(h) for h in headers) + " |")
    lines.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in data_rows:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


# Position groupings
INFIELD_POSITIONS = {"C", "1B", "2B", "3B", "SS"}
OUTFIELD_POSITIONS = {"LF", "CF", "RF"}
PITCHER_POSITIONS = {"SP", "RP"}
DH_POSITION = {"DH"}
ALL_HITTER_POSITIONS = INFIELD_POSITIONS | OUTFIELD_POSITIONS | DH_POSITION

# League level hierarchy (for depth analysis)
LEAGUE_LEVELS = ["ML", "AAA", "AA", "A", "A-", "R", "HS", "Unassigned"]
LEAGUE_LEVEL_WEIGHTS = {
    "ML": 1.0,
    "AAA": 0.8,
    "AA": 0.6,
    "A": 0.4,
    "A-": 0.3,
    "R": 0.2,
    "HS": 0.1,
    "Unassigned": 0.05,
}

# Expected depth per position (ideal number of players across all levels)
EXPECTED_DEPTH = {
    "C": 18,
    "1B": 15,
    "2B": 15,
    "3B": 15,
    "SS": 15,
    "LF": 15,
    "CF": 15,
    "RF": 15,
    "DH": 12,
    "SP": 40,
    "RP": 24,
}

# Quality thresholds for VOS v2 20-80 scale (50 = average, 54-56 = good, 62+ = elite)
QUALITY_THRESHOLDS = {
    "C": 52.0,
    "1B": 55.0,
    "2B": 52.0,
    "3B": 54.0,
    "SS": 52.0,
    "LF": 54.0,
    "CF": 54.0,
    "RF": 54.0,
    "DH": 55.0,
    "SP": 55.0,
    "RP": 52.0,
}


def get_column_value(row: Dict[str, Any], *possible_names: str) -> Optional[Any]:
    """Try multiple column names in order, return first found non-empty value."""
    for name in possible_names:
        if name in row:
            val = row[name]
            if val is not None and str(val).strip() != "":
                return val
    return None


def _normalize_player_row(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Normalize a raw CSV row to canonical internal format.
    Returns None if required fields (Ideal_Position, Ideal_Value) are missing.
    """
    # VOS output compatibility:
    # - legacy: Ideal_Position / Ideal Pos
    # - current (vos_v2): Projected_Position / Current_Position
    # Prefer projected role when available because Ideal_Value now reflects projected profile.
    ideal_pos = get_column_value(
        row,
        "Ideal_Position",
        "Ideal Pos",
        "Projected_Position",
        "Current_Position",
    )
    ideal_val = get_column_value(row, "Ideal_Value", "Ideal Value")
    if ideal_pos is None or ideal_val is None:
        return None

    try:
        value = float(ideal_val)
    except (TypeError, ValueError):
        return None

    pos = str(ideal_pos).strip().upper()
    if pos == "CL":
        pos = "RP"
    if pos not in (ALL_HITTER_POSITIONS | PITCHER_POSITIONS):
        return None

    level_raw = get_column_value(row, "League_Level", "League Level") or ""
    level = str(level_raw).strip() or "Unassigned"

    org_raw = get_column_value(row, "Org", "Organization") or ""
    org = str(org_raw).strip()

    archetype_raw = get_column_value(row, "Ideal Archetype", "Ideal_Archetype") or ""
    archetype = str(archetype_raw).strip() or "unknown"

    # Build normalized row with canonical keys for downstream use
    norm: Dict[str, Any] = dict(row)
    norm["_ideal_pos"] = pos
    norm["_ideal_value"] = value
    norm["_org"] = org
    norm["_level"] = level
    norm["_archetype"] = archetype

    # Coerce optional numerics
    for v2_name, legacy_name, key in [
        ("VOS_Score", "Current Value", "_current_value"),
        ("Batting_Score", "Bat Score", "_bat_score"),
        ("Defense_Score", "Defense Score", "_defense_score"),
        ("Baserunning_Score", "Baserunning Score", "_baserunning_score"),
        ("Pitching_Ability_Score", "Pitching Ability Score", "_pitch_ability"),
        ("Pitching_Arsenal_Score", "Pitching Arsenal Score", "_pitch_arsenal"),
    ]:
        raw = get_column_value(row, v2_name, legacy_name)
        if raw is not None:
            try:
                norm[key] = float(raw)
            except (TypeError, ValueError):
                norm[key] = None
        else:
            norm[key] = None

    return norm


def load_evaluation_summary(csv_path: Path) -> List[Dict[str, Any]]:
    """Load evaluation summary CSV with VOS v2 / legacy column mapping."""
    if not csv_path.exists():
        raise FileNotFoundError(f"Evaluation summary not found: {csv_path}")

    players: List[Dict[str, Any]] = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            norm = _normalize_player_row(row)
            if norm is not None:
                players.append(norm)
    return players


def filter_by_organization(players: List[Dict], org_name: Optional[str] = None) -> List[Dict]:
    """Filter players by organization name."""
    if not org_name:
        return players
    return [p for p in players if p.get("_org", "").strip() == org_name.strip()]


def analyze_positional_depth(players: List[Dict]) -> Dict[str, Dict[str, Any]]:
    """Analyze depth by position."""
    position_data: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "count": 0,
            "total_value": 0.0,
            "avg_value": 0.0,
            "quality_count": 0,
            "top_3_avg": 0.0,
            "by_level": defaultdict(int),
            "by_archetype": defaultdict(int),
            "players": [],
        }
    )

    for player in players:
        pos = player.get("_ideal_pos", "").strip()
        if not pos or pos not in (ALL_HITTER_POSITIONS | PITCHER_POSITIONS):
            continue

        value = player.get("_ideal_value", 0.0)
        level = player.get("_level", "Unassigned").strip()
        archetype = player.get("_archetype", "unknown").strip()

        position_data[pos]["count"] += 1
        position_data[pos]["total_value"] += value
        position_data[pos]["by_level"][level] += 1
        position_data[pos]["by_archetype"][archetype] += 1
        position_data[pos]["players"].append({
            "name": player.get("Name", ""),
            "value": value,
            "level": level,
            "archetype": archetype,
        })

        threshold = QUALITY_THRESHOLDS.get(pos, 52.0)
        if value >= threshold:
            position_data[pos]["quality_count"] += 1

    for pos, data in position_data.items():
        if data["count"] > 0:
            data["avg_value"] = data["total_value"] / data["count"]
        sorted_players = sorted(data["players"], key=lambda x: x["value"], reverse=True)
        top_3 = sorted_players[:3]
        if top_3:
            data["top_3_avg"] = sum(p["value"] for p in top_3) / len(top_3)

    return dict(position_data)


def calculate_position_strength_score(
    pos: str,
    data: Dict[str, Any],
    use_level_weights: bool = False,
) -> float:
    """Calculate a strength score (0-100) for a position."""
    expected = EXPECTED_DEPTH.get(pos, 15)
    threshold = QUALITY_THRESHOLDS.get(pos, 52.0)

    if use_level_weights:
        weighted_count = 0.0
        weighted_quality = 0.0
        for level, count in data["by_level"].items():
            weight = LEAGUE_LEVEL_WEIGHTS.get(level, 0.05)
            weighted_count += count * weight
            level_players = [p for p in data["players"] if p["level"] == level]
            quality_at_level = sum(1 for p in level_players if p["value"] >= threshold)
            weighted_quality += quality_at_level * weight
        actual = weighted_count
        quality = weighted_quality
    else:
        actual = data["count"]
        quality = data["quality_count"]

    top_3_avg = data["top_3_avg"]

    depth_ratio = min(actual / expected, 2.0)
    depth_score = (depth_ratio / 2.0) * 40.0

    quality_ratio = min(quality / expected, 2.0)
    quality_score = (quality_ratio / 2.0) * 40.0

    if threshold > 0:
        talent_ratio = min(top_3_avg / (threshold * 1.5), 1.5)
        talent_score = (talent_ratio / 1.5) * 20.0
    else:
        talent_score = 0.0

    total_score = depth_score + quality_score + talent_score
    return min(total_score, 100.0)


def analyze_skill_set_distribution(players: List[Dict]) -> Dict[str, Dict[str, Any]]:
    """Analyze distribution by skill set/archetype. Requires archetype data."""
    skill_data: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "count": 0,
            "total_value": 0.0,
            "avg_value": 0.0,
            "by_position": defaultdict(int),
            "by_level": defaultdict(int),
            "players": [],
        }
    )

    for player in players:
        archetype = player.get("_archetype", "").strip()
        if not archetype or archetype == "unknown":
            continue

        pos = player.get("_ideal_pos", "").strip()
        value = player.get("_ideal_value", 0.0)
        level = player.get("_level", "Unassigned").strip()

        skill_data[archetype]["count"] += 1
        skill_data[archetype]["total_value"] += value
        skill_data[archetype]["by_position"][pos] += 1
        skill_data[archetype]["by_level"][level] += 1
        skill_data[archetype]["players"].append({
            "name": player.get("Name", ""),
            "pos": pos,
            "value": value,
            "level": level,
        })

    for archetype, data in skill_data.items():
        if data["count"] > 0:
            data["avg_value"] = data["total_value"] / data["count"]

    return dict(skill_data)


def has_archetype_data(players: List[Dict]) -> bool:
    """True if any player has a non-empty, non-unknown archetype."""
    for p in players:
        a = p.get("_archetype", "").strip()
        if a and a != "unknown":
            return True
    return False


def analyze_position_group_depth(
    position_data: Dict[str, Dict],
    use_level_weights: bool = False,
) -> Dict[str, Dict[str, Any]]:
    """Analyze depth by position groups (IF, OF, Pitching)."""
    groups = {
        "Infield": INFIELD_POSITIONS,
        "Outfield": OUTFIELD_POSITIONS,
        "Pitching": PITCHER_POSITIONS,
    }

    group_data: Dict[str, Dict[str, Any]] = {}
    for group_name, positions in groups.items():
        total_count = 0
        total_value = 0.0
        quality_count = 0
        position_scores: List[float] = []

        for pos in positions:
            if pos in position_data:
                data = position_data[pos]
                total_count += data["count"]
                total_value += data["total_value"]
                quality_count += data["quality_count"]
                score = calculate_position_strength_score(pos, data, use_level_weights)
                position_scores.append(score)

        avg_score = sum(position_scores) / len(position_scores) if position_scores else 0.0
        avg_value = total_value / total_count if total_count > 0 else 0.0

        group_data[group_name] = {
            "total_count": total_count,
            "avg_value": avg_value,
            "quality_count": quality_count,
            "avg_strength_score": avg_score,
            "position_scores": {
                pos: calculate_position_strength_score(pos, position_data[pos], use_level_weights)
                for pos in positions
                if pos in position_data
            },
        }

    return group_data


def identify_weak_spots(
    position_data: Dict[str, Dict],
    use_level_weights: bool = False,
) -> List[Dict[str, Any]]:
    """Identify organizational weak spots."""
    weak_spots: List[Dict[str, Any]] = []

    for pos, data in position_data.items():
        expected = EXPECTED_DEPTH.get(pos, 15)
        strength = calculate_position_strength_score(pos, data, use_level_weights)
        threshold = QUALITY_THRESHOLDS.get(pos, 52.0)

        issues: List[str] = []
        if data["count"] < expected * 0.7:
            issues.append(f"depth {data['count']} < 70% of expected ({expected})")
        if data["quality_count"] < expected * 0.5:
            issues.append(
                f"quality {data['quality_count']} < 50% of expected ({expected})"
            )
        if data["top_3_avg"] < threshold:
            issues.append(
                f"top 3 avg ({data['top_3_avg']:.1f}) below threshold ({threshold:.0f})"
            )
        if strength < 50.0:
            issues.append(f"strength score {strength:.0f} < 50")

        if issues:
            weak_spots.append({
                "position": pos,
                "strength_score": strength,
                "count": data["count"],
                "expected": expected,
                "quality_count": data["quality_count"],
                "top_3_avg": data["top_3_avg"],
                "issues": issues,
            })

    return sorted(weak_spots, key=lambda x: x["strength_score"])


def identify_stockpiles(
    position_data: Dict[str, Dict],
    skill_data: Dict[str, Dict],
    has_archetype: bool,
) -> List[Dict[str, Any]]:
    """Identify positions or skill sets that are stockpiled."""
    stockpiles: List[Dict[str, Any]] = []

    for pos, data in position_data.items():
        expected = EXPECTED_DEPTH.get(pos, 15)
        if data["count"] > expected * 1.5:
            stockpiles.append({
                "type": "position",
                "name": pos,
                "count": data["count"],
                "expected": expected,
                "excess": data["count"] - expected,
                "excess_pct": ((data["count"] - expected) / expected) * 100,
            })

    if has_archetype:
        total_players = sum(d["count"] for d in position_data.values())
        if total_players > 0:
            for archetype, data in skill_data.items():
                pct_of_org = (data["count"] / total_players) * 100
                if pct_of_org > 15.0 and data["count"] > 5:
                    stockpiles.append({
                        "type": "skill_set",
                        "name": archetype,
                        "count": data["count"],
                        "pct_of_org": pct_of_org,
                        "avg_value": data["avg_value"],
                    })

    return sorted(
        stockpiles,
        key=lambda x: x.get("excess_pct", x.get("pct_of_org", 0)),
        reverse=True,
    )


def generate_recommendations(
    weak_spots: List[Dict],
    stockpiles: List[Dict],
    position_data: Dict[str, Dict],
) -> List[str]:
    """Generate strategic recommendations."""
    recommendations: List[str] = []

    if weak_spots:
        recommendations.append("Address weak spots:")
        for spot in weak_spots[:7]:
            issues = "; ".join(spot["issues"][:2])
            recommendations.append(
                f"  - {spot['position']}: {issues}"
            )
        recommendations.append("")

    if stockpiles:
        recommendations.append("Leverage stockpiles:")
        for stock in stockpiles[:5]:
            if stock["type"] == "position":
                recommendations.append(
                    f"  - {stock['name']}: position depth {stock['count']} > 150% of expected ({stock['expected']})"
                )
            else:
                recommendations.append(
                    f"  - {stock['name']} archetype: {stock['count']} players ({stock['pct_of_org']:.1f}% of org)"
                )
        recommendations.append("")

    infield_positions = [p for p in INFIELD_POSITIONS if p in position_data]
    outfield_positions = [p for p in OUTFIELD_POSITIONS if p in position_data]
    pitcher_positions = [p for p in PITCHER_POSITIONS if p in position_data]

    infield_total = sum(position_data[p]["count"] for p in infield_positions)
    outfield_total = sum(position_data[p]["count"] for p in outfield_positions)
    pitcher_total = sum(position_data[p]["count"] for p in pitcher_positions)
    total = infield_total + outfield_total + pitcher_total

    if total > 0:
        infield_score = (
            sum(
                calculate_position_strength_score(p, position_data[p])
                for p in infield_positions
            )
            / len(infield_positions)
            if infield_positions
            else 0
        )
        outfield_score = (
            sum(
                calculate_position_strength_score(p, position_data[p])
                for p in outfield_positions
            )
            / len(outfield_positions)
            if outfield_positions
            else 0
        )
        pitcher_score = (
            sum(
                calculate_position_strength_score(p, position_data[p])
                for p in pitcher_positions
            )
            / len(pitcher_positions)
            if pitcher_positions
            else 0
        )
        recommendations.append(
            f"Consider trades: Infield is {'weaker' if infield_score < pitcher_score else 'stronger'} "
            f"(IF strength {infield_score:.0f}, OF {outfield_score:.0f}, P {pitcher_score:.0f})."
        )

    return recommendations


def format_report(
    org_name: str,
    position_data: Dict[str, Dict],
    group_data: Dict[str, Dict],
    skill_data: Dict[str, Dict],
    weak_spots: List[Dict],
    stockpiles: List[Dict],
    recommendations: List[str],
    use_level_weights: bool = False,
    show_level_breakdown: bool = True,
    has_archetype: bool = False,
) -> str:
    """Format the analysis report."""
    lines: List[str] = []
    lines.append("=" * 80)
    lines.append(f"ORGANIZATIONAL DEPTH ANALYSIS: {org_name}")
    lines.append("")
    lines.append(
        "(VOS v2 20-80 scale; Ideal_Value used for position analysis. "
        "50 = average, 54-56 = good, 62+ = elite.)"
    )
    lines.append("")
    lines.append("POSITIONAL STRENGTH SCORES")
    lines.append("-" * 80)
    lines.append(
        f"{'Pos':<6} {'Count':<8} {'Quality':<8} {'Avg Val':<10} {'Top3 Avg':<10} {'Strength':<10}"
    )
    lines.append("-" * 80)

    all_positions = sorted(position_data.keys())
    for pos in all_positions:
        data = position_data[pos]
        strength = calculate_position_strength_score(pos, data, use_level_weights)
        lines.append(
            f"{pos:<6} {data['count']:<8} {data['quality_count']:<8} "
            f"{data['avg_value']:<10.1f} {data['top_3_avg']:<10.1f} {strength:<10.1f}"
        )
    lines.append("")

    if show_level_breakdown:
        lines.append("DEPTH BY LEVEL (Top 5 Positions by Count)")
        lines.append("-" * 80)
        sorted_positions = sorted(
            position_data.items(),
            key=lambda x: x[1]["count"],
            reverse=True,
        )[:5]
        for pos, data in sorted_positions:
            level_counts = sorted(
                data["by_level"].items(),
                key=lambda x: x[1],
                reverse=True,
            )
            level_str = ", ".join(
                f"{level}({count})" for level, count in level_counts if count > 0
            )
            lines.append(f"  {pos}: {level_str}")
        lines.append("")

    lines.append("POSITION GROUP SUMMARY")
    lines.append("-" * 80)
    for group_name, data in group_data.items():
        lines.append(
            f"  {group_name}: {data['total_count']} players, "
            f"{data['quality_count']} quality, avg value {data['avg_value']:.1f}, "
            f"avg strength {data['avg_strength_score']:.1f}"
        )
        for pos, score in data["position_scores"].items():
            lines.append(f"    {pos}: {score:.1f}")
    lines.append("")

    lines.append("WEAK SPOTS")
    lines.append("-" * 80)
    if weak_spots:
        for spot in weak_spots:
            lines.append(
                f"  - {spot['position']}: "
                + "; ".join(spot["issues"])
            )
    else:
        lines.append("  None identified.")
    lines.append("")

    lines.append("STOCKPILES")
    lines.append("-" * 80)
    if stockpiles:
        for stock in stockpiles:
            if stock["type"] == "position":
                lines.append(
                    f"  - {stock['name']}: position depth {stock['count']} > 150% of expected ({stock['expected']})"
                )
            else:
                lines.append(
                    f"  - {stock['name']} archetype: {stock['count']} players "
                    f"({stock['pct_of_org']:.1f}% of org, avg value: {stock['avg_value']:.1f})"
                )
    else:
        lines.append("  None identified.")
    lines.append("")

    if has_archetype:
        lines.append("SKILL SET DISTRIBUTION (Top 10 Archetypes)")
        lines.append("-" * 80)
        sorted_skills = sorted(
            skill_data.items(),
            key=lambda x: x[1]["count"],
            reverse=True,
        )
        lines.append(
            f"{'Archetype':<30} {'Count':<8} {'Avg Value':<12} {'Top Positions'}"
        )
        lines.append("-" * 80)
        for archetype, data in sorted_skills[:10]:
            top_positions = sorted(
                data["by_position"].items(),
                key=lambda x: x[1],
                reverse=True,
            )[:3]
            pos_str = ", ".join(f"{pos}({count})" for pos, count in top_positions)
            lines.append(
                f"{archetype:<30} {data['count']:<8} {data['avg_value']:<12.1f} {pos_str}"
            )
        lines.append("")
    else:
        lines.append("SKILL SET DISTRIBUTION")
        lines.append("-" * 80)
        lines.append("  Archetype analysis requires additional classification data.")
        lines.append("")

    if recommendations:
        lines.append("STRATEGIC RECOMMENDATIONS")
        lines.append("-" * 80)
        for rec in recommendations:
            lines.append(rec)
        lines.append("")

    return "\n".join(lines)


def export_csv_report(
    output_path: Path,
    position_data: Dict[str, Dict],
    group_data: Dict[str, Dict],
    skill_data: Dict[str, Dict],
    use_level_weights: bool = False,
    has_archetype: bool = False,
) -> None:
    """Export detailed CSV reports."""
    pos_output = output_path.parent / f"{output_path.stem}_positions.csv"
    with open(pos_output, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        level_headers = [f"Count_{level}" for level in LEAGUE_LEVELS]
        writer.writerow(
            [
                "Position",
                "Count",
                "Expected",
                "Quality Count",
                "Total Value",
                "Avg Value",
                "Top 3 Avg",
                "Strength Score",
            ]
            + level_headers
        )
        pos_headers = ["Position", "Count", "Expected", "Quality Count", "Total Value", "Avg Value", "Top 3 Avg", "Strength Score"] + level_headers
        pos_data_rows = []
        for pos in sorted(position_data.keys()):
            data = position_data[pos]
            strength = calculate_position_strength_score(pos, data, use_level_weights)
            level_counts = [data["by_level"].get(level, 0) for level in LEAGUE_LEVELS]
            row = [
                pos,
                data["count"],
                EXPECTED_DEPTH.get(pos, 15),
                data["quality_count"],
                f"{data['total_value']:.2f}",
                f"{data['avg_value']:.2f}",
                f"{data['top_3_avg']:.2f}",
                f"{strength:.2f}",
            ] + level_counts
            writer.writerow(row)
            pos_data_rows.append(row)
    print(f"Position strength report saved to {pos_output}")
    pos_md = pos_output.with_suffix(".md")
    _write_md_table(pos_md, pos_headers, pos_data_rows, title="Position Strength Report")
    print(f"Position strength MD saved to {pos_md}")

    if has_archetype:
        skill_output = output_path.parent / f"{output_path.stem}_skillsets.csv"
        with open(skill_output, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "Archetype",
                    "Count",
                    "Total Value",
                    "Avg Value",
                    "Top Position",
                    "Top Position Count",
                ]
            )
            for archetype, data in sorted(
                skill_data.items(),
                key=lambda x: x[1]["count"],
                reverse=True,
            ):
                top_pos = (
                    max(data["by_position"].items(), key=lambda x: x[1])
                    if data["by_position"]
                    else ("N/A", 0)
                )
                writer.writerow(
                    [
                        archetype,
                        data["count"],
                        f"{data['total_value']:.2f}",
                        f"{data['avg_value']:.2f}",
                        top_pos[0],
                        top_pos[1],
                    ]
                )
        print(f"Skill set report saved to {skill_output}")


def export_player_details_csv(
    output_path: Path,
    players: List[Dict[str, Any]],
    position_data: Dict[str, Dict],
) -> None:
    """Export detailed CSV with all players grouped by ideal position."""
    player_output = output_path.parent / f"{output_path.stem}_player_details.csv"

    players_by_position: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for player in players:
        ideal_pos = player.get("_ideal_pos", "").strip()
        if ideal_pos and ideal_pos in (ALL_HITTER_POSITIONS | PITCHER_POSITIONS):
            players_by_position[ideal_pos].append(player)

    for pos in players_by_position:
        players_by_position[pos].sort(
            key=lambda p: float(p.get("_ideal_value", 0) or 0),
            reverse=True,
        )

    headers = [
        "ID",
        "Name",
        "Pos",
        "Age",
        "Team",
        "Org",
        "League_Level",
        "VOS_Score",
        "VOS_Potential",
        "Batting_Score",
        "Batting_Potential",
        "Defense_Score",
        "Baserunning_Score",
        "Pitching_Ability_Score",
        "Pitching_Arsenal_Score",
        "Development_Adj",
        "Age_Adj",
        "Personality_Adj",
        "Park_Name",
        "Park_Applied",
        "C_Score",
        "1B_Score",
        "2B_Score",
        "3B_Score",
        "SS_Score",
        "LF_Score",
        "CF_Score",
        "RF_Score",
        "DH_Score",
        "Ideal_Position",
        "Ideal_Value",
    ]

    with open(player_output, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        all_detail_rows: List[List[Any]] = []
        for pos in sorted(players_by_position.keys()):
            for player in players_by_position[pos]:
                row = [
                    player.get("ID", ""),
                    player.get("Name", ""),
                    player.get("Pos", ""),
                    player.get("Age", ""),
                    get_column_value(player, "Team") or "",
                    get_column_value(player, "Org", "Organization") or "",
                    get_column_value(player, "League_Level", "League Level") or "",
                    get_column_value(player, "VOS_Score") or "",
                    get_column_value(player, "VOS_Potential", "VOS Potential") or "",
                    get_column_value(player, "Batting_Score", "Bat Score") or "",
                    get_column_value(player, "Batting_Potential", "Batting Potential") or "",
                    get_column_value(player, "Defense_Score", "Defense Score") or "",
                    get_column_value(player, "Baserunning_Score", "Baserunning Score") or "",
                    get_column_value(player, "Pitching_Ability_Score", "Pitching Ability Score") or "",
                    get_column_value(player, "Pitching_Arsenal_Score", "Pitching Arsenal Score") or "",
                    player.get("Development_Adj", ""),
                    player.get("Age_Adj", ""),
                    player.get("Personality_Adj", ""),
                    player.get("Park_Name", ""),
                    player.get("Park_Applied", ""),
                    player.get("C_Score", ""),
                    player.get("1B_Score", ""),
                    player.get("2B_Score", ""),
                    player.get("3B_Score", ""),
                    player.get("SS_Score", ""),
                    player.get("LF_Score", ""),
                    player.get("CF_Score", ""),
                    player.get("RF_Score", ""),
                    player.get("DH_Score", ""),
                    player.get("_ideal_pos", ""),
                    f"{player.get('_ideal_value', 0):.2f}",
                ]
                writer.writerow(row)
                all_detail_rows.append(row)

    print(f"Player details report saved to {player_output}")
    # MD: compact view — Name, Pos, Age, Team, Org, League_Level, VOS_Score, VOS_Potential, Ideal_Position, Ideal_Value
    _md_detail_headers = ["ID", "Name", "Pos", "Age", "Team", "Org", "League_Level", "VOS_Score", "VOS_Potential", "Ideal_Position", "Ideal_Value"]
    _idx = {h: i for i, h in enumerate(headers)}
    _md_idx = [_idx[h] for h in _md_detail_headers]
    _md_rows = [[r[i] for i in _md_idx] for r in all_detail_rows]
    player_md = player_output.with_suffix(".md")
    _write_md_table(player_md, _md_detail_headers, _md_rows, title="Player Details by Ideal Position")
    print(f"Player details MD saved to {player_md}")


def export_html_report(
    output_path: Path,
    org_name: str,
    players: List[Dict[str, Any]],
    position_data: Dict[str, Dict],
    group_data: Dict[str, Dict],
    skill_data: Dict[str, Dict],
    weak_spots: List[Dict],
    stockpiles: List[Dict],
    recommendations: List[str],
    use_level_weights: bool = False,
) -> None:
    """Export HTML report with collapsible position sections."""
    html_output = output_path.parent / f"{output_path.stem}.html"

    players_by_position: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for player in players:
        ideal_pos = player.get("_ideal_pos", "").strip()
        if ideal_pos and ideal_pos in (ALL_HITTER_POSITIONS | PITCHER_POSITIONS):
            players_by_position[ideal_pos].append(player)

    for pos in players_by_position:
        players_by_position[pos].sort(
            key=lambda p: float(p.get("_ideal_value", 0) or 0),
            reverse=True,
        )

    def _fmt_score(key: str, player: Dict) -> str:
        val = player.get(key)
        if val is None or val == "":
            return ""
        try:
            return f"{float(val):.2f}"
        except (TypeError, ValueError):
            return ""

    html_parts: List[str] = []
    html_parts.append(
        """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Organizational Depth Analysis: """
        + html_module.escape(org_name)
        + """</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #333; max-width: 1400px; margin: 0 auto; padding: 20px; background: #f5f5f5; }
        h1 { color: #2c3e50; border-bottom: 3px solid #3498db; padding-bottom: 10px; }
        h2 { color: #34495e; margin-top: 30px; border-bottom: 2px solid #ecf0f1; padding-bottom: 5px; }
        .position-block h2 { cursor: pointer; user-select: none; padding: 10px; background: #ecf0f1; border-radius: 5px; margin: 10px 0; }
        .position-block h2:hover { background: #d5dbdb; }
        .position-block .content { display: none; }
        .position-block.open .content { display: block; }
        .text-summary { background: white; padding: 20px; border-radius: 5px; margin-bottom: 30px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); white-space: pre-wrap; font-family: monospace; font-size: 0.9em; }
        table { width: 100%; border-collapse: collapse; background: white; }
        th { background: #3498db; color: white; padding: 10px; text-align: left; cursor: pointer; }
        th:hover { background: #2980b9; }
        td { padding: 8px; border-bottom: 1px solid #ecf0f1; }
        tr:hover { background: #f8f9fa; }
    </style>
    <script>
        function toggle(el) { el.parentElement.classList.toggle('open'); }
        function sortTable(th) {
            var table = th.closest('table');
            var col = th.getAttribute('data-col');
            var idx = Array.from(th.parentElement.children).indexOf(th);
            var tbody = table.querySelector('tbody');
            var rows = Array.from(tbody.querySelectorAll('tr'));
            var isNum = th.getAttribute('data-sort') === 'numeric';
            rows.sort(function(a, b) {
                var av = a.children[idx].textContent.trim();
                var bv = b.children[idx].textContent.trim();
                if (isNum) { var an = parseFloat(av) || 0; var bn = parseFloat(bv) || 0; return bn - an; }
                return av.localeCompare(bv);
            });
            rows.forEach(function(r) { tbody.appendChild(r); });
        }
    </script>
</head>
<body>
    <h1>Organizational Depth Analysis: """
        + html_module.escape(org_name)
        + """</h1>
"""
    )

    has_arch = has_archetype_data(players)
    report = format_report(
        org_name,
        position_data,
        group_data,
        skill_data,
        weak_spots,
        stockpiles,
        recommendations,
        use_level_weights=use_level_weights,
        show_level_breakdown=True,
        has_archetype=has_arch,
    )
    escaped = html_module.escape(report).replace("\n", "<br>")
    html_parts.append(f"    <div class='text-summary'><pre>{escaped}</pre></div>")

    position_scores = {}
    for pos in players_by_position:
        if pos in position_data:
            position_scores[pos] = calculate_position_strength_score(
                pos, position_data[pos], use_level_weights
            )
    sorted_positions = sorted(
        position_scores.items(),
        key=lambda x: x[1],
        reverse=True,
    )

    for pos, strength_score in sorted_positions:
        data = position_data.get(pos, {})
        players_list = players_by_position.get(pos, [])
        if not players_list:
            continue

        html_parts.append(
            f"<div class='position-block open'><h2 onclick='toggle(this)'>"
            f"{pos} — Count: {len(players_list)}, Quality: {data.get('quality_count', 0)}, "
            f"Avg: {data.get('avg_value', 0):.1f}, Strength: {strength_score:.1f}"
            f"</h2><div class='content'><table class='sortable'><thead><tr>"
            "<th data-col='ID' onclick='sortTable(this)' data-sort='text'>ID</th>"
            "<th data-col='Name' onclick='sortTable(this)'>Name</th>"
            "<th data-col='Pos' onclick='sortTable(this)'>Pos</th>"
            "<th data-col='Age' onclick='sortTable(this)' data-sort='numeric'>Age</th>"
            "<th data-col='Team' onclick='sortTable(this)'>Team</th>"
            "<th data-col='Org' onclick='sortTable(this)'>Org</th>"
            "<th data-col='League_Level' onclick='sortTable(this)'>League_Level</th>"
            "<th data-col='VOS_Score' onclick='sortTable(this)' data-sort='numeric'>VOS_Score</th>"
            "<th data-col='Ideal_Position' onclick='sortTable(this)'>Ideal_Position</th>"
            "<th data-col='Ideal_Value' onclick='sortTable(this)' data-sort='numeric'>Ideal_Value</th>"
            "<th data-col='Batting_Score' onclick='sortTable(this)' data-sort='numeric'>Batting_Score</th>"
            "<th data-col='Defense_Score' onclick='sortTable(this)' data-sort='numeric'>Defense_Score</th>"
            "<th data-col='Baserunning_Score' onclick='sortTable(this)' data-sort='numeric'>Baserunning_Score</th>"
            "<th data-col='Pitching_Ability_Score' onclick='sortTable(this)' data-sort='numeric'>Pitching_Ability_Score</th>"
            "<th data-col='Pitching_Arsenal_Score' onclick='sortTable(this)' data-sort='numeric'>Pitching_Arsenal_Score</th>"
            "</tr></thead><tbody>"
        )

        for player in players_list:
            name = html_module.escape(str(player.get("Name", "")))
            pos_val = html_module.escape(str(player.get("Pos", "")))
            age = player.get("Age", "")
            team = html_module.escape(str(get_column_value(player, "Team") or ""))
            org = html_module.escape(str(get_column_value(player, "Org", "Organization") or ""))
            level = html_module.escape(str(get_column_value(player, "League_Level", "League Level") or ""))
            vos = get_column_value(player, "VOS_Score") or ""
            vos_str = f"{float(vos):.2f}" if vos != "" else ""
            try:
                ideal_val = float(player.get("_ideal_value", 0))
            except (TypeError, ValueError):
                ideal_val = 0
            bat = _fmt_score("_bat_score", player) or _fmt_score("Batting_Score", player) or _fmt_score("Bat Score", player)
            def_ = _fmt_score("_defense_score", player) or _fmt_score("Defense_Score", player) or _fmt_score("Defense Score", player)
            br = _fmt_score("_baserunning_score", player) or _fmt_score("Baserunning_Score", player) or _fmt_score("Baserunning Score", player)
            pa = _fmt_score("_pitch_ability", player) or _fmt_score("Pitching_Ability_Score", player) or _fmt_score("Pitching Ability Score", player)
            par = _fmt_score("_pitch_arsenal", player) or _fmt_score("Pitching_Arsenal_Score", player) or _fmt_score("Pitching Arsenal Score", player)
            html_parts.append(
                f"<tr><td>{player.get('ID', '')}</td><td>{name}</td><td>{pos_val}</td><td>{age}</td>"
                f"<td>{team}</td><td>{org}</td><td>{level}</td><td>{vos_str}</td>"
                f"<td>{pos}</td><td>{ideal_val:.2f}</td>"
                f"<td>{bat}</td><td>{def_}</td><td>{br}</td><td>{pa}</td><td>{par}</td></tr>"
            )

        html_parts.append("</tbody></table></div></div>")

    html_parts.append("</body></html>")
    with open(html_output, "w", encoding="utf-8") as f:
        f.write("".join(html_parts))
    print(f"HTML report saved to {html_output}")


def get_org_abbreviation(org_name: str) -> str:
    """Get organization abbreviation from full name."""
    org_abbrev_map = {
        "Arizona Diamondbacks": "ARI",
        "Atlanta Braves": "ATL",
        "Baltimore Orioles": "BAL",
        "Boston Red Sox": "BOS",
        "Chicago Cubs": "CHC",
        "Chicago White Sox": "CWS",
        "Cincinnati Reds": "CIN",
        "Cleveland Guardians": "CLE",
        "Colorado Rockies": "COL",
        "Detroit Tigers": "DET",
        "Houston Astros": "HOU",
        "Kansas City Royals": "KC",
        "Los Angeles Angels": "LAA",
        "Los Angeles Dodgers": "LAD",
        "Miami Marlins": "MIA",
        "Milwaukee Brewers": "MIL",
        "Minnesota Twins": "MIN",
        "New York Mets": "NYM",
        "New York Yankees": "NYY",
        "Oakland Athletics": "OAK",
        "Philadelphia Phillies": "PHI",
        "Pittsburgh Pirates": "PIT",
        "San Diego Padres": "SD",
        "San Francisco Giants": "SF",
        "Seattle Mariners": "SEA",
        "St. Louis Cardinals": "STL",
        "Tampa Bay Rays": "TB",
        "Texas Rangers": "TEX",
        "Toronto Blue Jays": "TOR",
        "Washington Nationals": "WSH",
        "Anaheim Angels": "LAA",
        "California Angels": "LAA",
        "Montreal Expos": "MON",
        "Cleveland Indians": "CLE",
    }
    if org_name in org_abbrev_map:
        return org_abbrev_map[org_name]
    org_lower = org_name.lower()
    for key, abbrev in org_abbrev_map.items():
        if key.lower() == org_lower:
            return abbrev
    words = org_name.split()
    if len(words) == 1:
        abbrev = org_name.upper().replace(" ", "").replace(".", "")[:4]
    else:
        abbrev = "".join([w[0].upper() for w in words if w and w[0].isalpha()])[:4]
    abbrev = abbrev.replace(" ", "").replace(".", "").replace("-", "").replace("'", "")
    return abbrev if abbrev else "ORG"


def _resolve_eval_path(
    explicit_path: Optional[Path],
    league: Optional[str],
) -> Path:
    """Resolve evaluation CSV path from CLI args."""
    if explicit_path:
        p = Path(explicit_path) if not isinstance(explicit_path, Path) else explicit_path
        if p.exists():
            return p
        raise FileNotFoundError(f"Evaluation file not found: {p}")
    if league:
        league_slug = league.strip()
        eval_dir = SCRIPT_DIR / league_slug / "eval"
        pattern = f"evaluation_summary_{league_slug}_*.csv"
        matches = sorted(eval_dir.glob(pattern), key=lambda x: x.stat().st_mtime, reverse=True)
        if matches:
            return matches[0]
        # Fallback: search CWD for legacy runs
        cwd = Path.cwd()
        matches = sorted(cwd.glob(pattern), key=lambda x: x.stat().st_mtime, reverse=True)
        if matches:
            return matches[0]
        fallback = cwd / f"evaluation_summary_{league_slug}.csv"
        if fallback.exists():
            return fallback
        raise FileNotFoundError(
            f"No evaluation file found for league '{league}'. "
            "Expected evaluation_summary_{league}_*.csv or evaluation_summary_{league}.csv"
        )
    default = Path("evaluation_summary.csv")
    if default.exists():
        return default
    raise FileNotFoundError(
        "Evaluation summary not found. Specify a file path, --evaluation-file, or --league."
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze organizational depth across positions, skill sets, and levels"
    )
    parser.add_argument(
        "eval_path",
        nargs="?",
        type=str,
        default=None,
        help="Path to evaluation_summary CSV (or use --evaluation-file / --league)",
    )
    parser.add_argument(
        "-e",
        "--evaluation-file",
        type=Path,
        default=None,
        dest="evaluation_file",
        help="Path to evaluation_summary CSV",
    )
    parser.add_argument(
        "--league",
        type=str,
        default=None,
        help="League abbreviation; auto-detects latest evaluation_summary_{league}_*.csv",
    )
    parser.add_argument(
        "-o",
        "--org",
        type=str,
        default=None,
        help="Filter by organization name",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output path for text report",
    )
    parser.add_argument(
        "--csv",
        action="store_true",
        help="Also export CSV reports",
    )
    parser.add_argument(
        "--weight-by-level",
        action="store_true",
        help="Weight players by league level",
    )
    parser.add_argument(
        "--no-level-breakdown",
        action="store_true",
        help="Don't show level breakdown in report",
    )
    parser.add_argument(
        "--player-details",
        action="store_true",
        help="Export detailed CSV with all players by ideal position",
    )
    parser.add_argument(
        "--html",
        action="store_true",
        help="Export HTML report with collapsible position sections",
    )
    args = parser.parse_args()

    # Prefer explicit --evaluation-file, then positional eval_path
    explicit = args.evaluation_file
    if explicit is None and args.eval_path:
        explicit = Path(args.eval_path)

    try:
        eval_path = _resolve_eval_path(explicit, args.league)
    except FileNotFoundError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Loading evaluation summary from {eval_path}...")
    players = load_evaluation_summary(eval_path)
    print(f"Loaded {len(players)} players")

    if args.org:
        players = filter_by_organization(players, args.org)
        print(f"Filtered to {len(players)} players in {args.org}")
        org_name = args.org
    else:
        orgs = set(p.get("_org", "").strip() for p in players if p.get("_org"))
        if len(orgs) == 1:
            org_name = list(orgs)[0]
            print(f"Detected organization: {org_name}")
        else:
            org_name = "All Organizations" if len(orgs) > 1 else "Unassigned"

    if not players:
        print("[ERROR] No players to analyze")
        sys.exit(1)

    if args.output is None:
        stem = eval_path.stem  # e.g. evaluation_summary_woba_20260413_085618
        _league_slug = None
        if stem.startswith("evaluation_summary_"):
            _parts = stem[len("evaluation_summary_"):].split("_", 1)
            if _parts and _parts[0]:
                _league_slug = _parts[0].lower()
        if org_name and org_name not in ("All Organizations", "Unassigned"):
            _fname = f"org_depth_analysis_{get_org_abbreviation(org_name).lower()}.txt"
        else:
            _fname = "org_depth_analysis_all.txt"
        if _league_slug:
            args.output = SCRIPT_DIR / _league_slug / "org_depth" / _fname
        else:
            args.output = Path(_fname)
    else:
        args.output = Path(args.output)

    print("Analyzing positional depth...")
    position_data = analyze_positional_depth(players)

    has_arch = has_archetype_data(players)
    if has_arch:
        print("Analyzing skill set distribution...")
        skill_data = analyze_skill_set_distribution(players)
    else:
        skill_data = {}

    print("Analyzing position groups...")
    group_data = analyze_position_group_depth(position_data, args.weight_by_level)

    print("Identifying weak spots...")
    weak_spots = identify_weak_spots(position_data, args.weight_by_level)

    print("Identifying stockpiles...")
    stockpiles = identify_stockpiles(position_data, skill_data, has_arch)

    print("Generating recommendations...")
    recommendations = generate_recommendations(weak_spots, stockpiles, position_data)

    report = format_report(
        org_name,
        position_data,
        group_data,
        skill_data,
        weak_spots,
        stockpiles,
        recommendations,
        use_level_weights=args.weight_by_level,
        show_level_breakdown=not args.no_level_breakdown,
        has_archetype=has_arch,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\nReport saved to {args.output.resolve()}")

    if args.csv:
        export_csv_report(
            args.output,
            position_data,
            group_data,
            skill_data,
            args.weight_by_level,
            has_arch,
        )
    if args.player_details:
        print("Exporting player details CSV...")
        export_player_details_csv(args.output, players, position_data)
    if args.html:
        print("Generating HTML report...")
        export_html_report(
            args.output,
            org_name,
            players,
            position_data,
            group_data,
            skill_data,
            weak_spots,
            stockpiles,
            recommendations,
            args.weight_by_level,
        )

    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"Total Positions Analyzed: {len(position_data)}")
    print(f"Weak Spots Identified: {len(weak_spots)}")
    print(f"Stockpiles Identified: {len(stockpiles)}")
    if weak_spots:
        print(f"\nTop Weak Spot: {weak_spots[0]['position']} (Strength: {weak_spots[0]['strength_score']:.1f})")
    if stockpiles:
        print(f"\nLargest Stockpile: {stockpiles[0]['name']} ({stockpiles[0].get('count', 0)} players)")


if __name__ == "__main__":
    main()

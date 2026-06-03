#!/usr/bin/env python3
"""
org_summary_pdf.py — Renders the org-wide depth chart summary as a styled PDF.

Same inputs as `depth_chart.render_org_summary` (the level_data list collected
during a multi-level run). Produces a single PDF with a cover, a glossary
explaining VOS/Composite/Edge/Z, an org snapshot table, a per-level promotion
ladder, and a per-level detail block (lineups, bench, pitching, replacements,
demotions, mismatches).

Visual style mirrors `draft_grades_pdf.py` for consistency.
"""

from __future__ import annotations
# --- repo-root + core/ path bootstrap ---
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _p in (_ROOT, _os.path.join(_ROOT, "core")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
# --- end bootstrap ---


from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# Palette — matches draft_grades_pdf.py for visual consistency across reports.
DARK_BG = colors.HexColor("#0f1923")
HEADER_BG = colors.HexColor("#1e2d3d")
ACCENT = colors.HexColor("#2a6496")
WHITE = colors.white
LIGHT_GRAY = colors.HexColor("#f5f7fa")
MID_GRAY = colors.HexColor("#e0e5eb")
BAD_RED = colors.HexColor("#c0392b")
GOOD_GREEN = colors.HexColor("#1a7a4a")


# -----------------------------------------------------------------------------
# Style helpers
# -----------------------------------------------------------------------------

def _make_styles() -> Dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "Title", parent=base["Title"], fontName="Helvetica-Bold",
            fontSize=22, textColor=DARK_BG, alignment=TA_CENTER, spaceAfter=12,
        ),
        "subtitle": ParagraphStyle(
            "Subtitle", parent=base["Heading2"], fontName="Helvetica",
            fontSize=12, textColor=ACCENT, alignment=TA_CENTER, spaceAfter=24,
        ),
        "h1": ParagraphStyle(
            "H1", parent=base["Heading1"], fontName="Helvetica-Bold",
            fontSize=16, textColor=ACCENT, spaceBefore=12, spaceAfter=8,
        ),
        "h2": ParagraphStyle(
            "H2", parent=base["Heading2"], fontName="Helvetica-Bold",
            fontSize=13, textColor=DARK_BG, spaceBefore=8, spaceAfter=4,
        ),
        "h3": ParagraphStyle(
            "H3", parent=base["Heading3"], fontName="Helvetica-Bold",
            fontSize=11, textColor=DARK_BG, spaceBefore=6, spaceAfter=2,
        ),
        "body": ParagraphStyle(
            "Body", parent=base["Normal"], fontName="Helvetica",
            fontSize=10, textColor=DARK_BG, spaceAfter=4, leading=13,
        ),
        "muted": ParagraphStyle(
            "Muted", parent=base["Normal"], fontName="Helvetica-Oblique",
            fontSize=9, textColor=colors.HexColor("#5a6877"), spaceAfter=6,
        ),
        "glossary_term": ParagraphStyle(
            "GlossTerm", parent=base["Normal"], fontName="Helvetica-Bold",
            fontSize=10, textColor=ACCENT, spaceAfter=2,
        ),
        "glossary_def": ParagraphStyle(
            "GlossDef", parent=base["Normal"], fontName="Helvetica",
            fontSize=10, textColor=DARK_BG, spaceAfter=8, leading=13, leftIndent=12,
        ),
    }


def _table_style(header_rows: int = 1) -> TableStyle:
    """Standard table style: dark header, alternating rows, accent borders."""
    return TableStyle([
        # Header
        ("BACKGROUND", (0, 0), (-1, header_rows - 1), HEADER_BG),
        ("TEXTCOLOR", (0, 0), (-1, header_rows - 1), WHITE),
        ("FONTNAME", (0, 0), (-1, header_rows - 1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, header_rows - 1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, header_rows - 1), 6),
        ("TOPPADDING", (0, 0), (-1, header_rows - 1), 6),
        # Body
        ("FONTNAME", (0, header_rows), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, header_rows), (-1, -1), 9),
        ("TEXTCOLOR", (0, header_rows), (-1, -1), DARK_BG),
        ("ROWBACKGROUNDS", (0, header_rows), (-1, -1), [WHITE, LIGHT_GRAY]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.25, MID_GRAY),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, header_rows), (-1, -1), 4),
        ("BOTTOMPADDING", (0, header_rows), (-1, -1), 4),
    ])


def _build_table(headers: List[str], rows: List[List[Any]], col_widths: Optional[List[float]] = None) -> Table:
    data = [headers] + [[str(c) for c in r] for r in rows]
    t = Table(data, colWidths=col_widths, repeatRows=1)
    t.setStyle(_table_style())
    return t


# -----------------------------------------------------------------------------
# Section builders
# -----------------------------------------------------------------------------

def _cover(story: List[Any], styles: Dict[str, ParagraphStyle], league: str, org: str, year: int) -> None:
    story.append(Spacer(1, 1.5 * inch))
    story.append(Paragraph(f"{org}", styles["title"]))
    story.append(Paragraph(f"Org Depth Summary — {league.upper()} · {year}", styles["subtitle"]))
    story.append(Spacer(1, 0.3 * inch))
    story.append(Paragraph(
        f"Generated {datetime.now().strftime('%B %d, %Y · %H:%M')}",
        styles["muted"]
    ))


def _glossary(story: List[Any], styles: Dict[str, ParagraphStyle]) -> None:
    story.append(PageBreak())
    story.append(Paragraph("Methodology &amp; Glossary", styles["h1"]))
    story.append(Paragraph(
        "Quick reference for the metrics and concepts used throughout this report. "
        "All values blend physical-tool ratings (VOS) with actual on-field statistics.",
        styles["body"]
    ))
    story.append(Spacer(1, 0.15 * inch))

    entries = [
        ("VOS  (VOS Optimized Score)",
         "Proprietary 20–80-scale player rating that synthesizes physical tools, "
         "potential, age, position, park context, and personality into one number. "
         "50 is league average; 80 is elite. VOS is forward-looking — what a player's "
         "ratings imply they'd produce. It is the ratings half of every composite below."),
        ("Composite",
         "Per-level blend of VOS with the player's z-scored real-world stats: "
         "<i>composite = ratings_weight × VOS + stats_weight × stat_score</i>. "
         "Weights are level-specific (configured in <i>depth_config.json</i>): ratings "
         "dominate at A and below; stats dominate at AAA and ML. A small-sample stat "
         "score is automatically dampened back toward VOS so a guy with 30 PA isn't "
         "penalized by an unstable z-score. Composite drives every depth-chart decision."),
        ("Edge",
         "Composite difference between a promotion candidate and the player they'd "
         "displace. <b>vs Starter</b> measures lineup-spot impact (would this player "
         "actually start?). <b>vs Bench/Worst</b> measures roster impact (would they "
         "make the team at all?). Positive edge means the candidate is better — "
         "larger numbers mean a bigger upgrade."),
        ("Z (z-score)",
         "Statistical measure of how far a player's stat is from the level average, "
         "in standard deviations. A z of <b>+1.0</b> means the player is one standard "
         "deviation above league average; <b>-1.0</b> means one below. Used internally "
         "to convert raw stats (wOBA, FIP, K-BB%) into a 20–80 scale comparable to VOS. "
         "On the Demotion Candidates table, the Z column shows how far below average "
         "the player's full composite has drifted."),
    ]

    for term, definition in entries:
        story.append(Paragraph(term, styles["glossary_term"]))
        story.append(Paragraph(definition, styles["glossary_def"]))


def _org_snapshot(
    story: List[Any],
    styles: Dict[str, ParagraphStyle],
    level_data_list: List[Dict[str, Any]],
) -> None:
    story.append(PageBreak())
    story.append(Paragraph("Org Snapshot", styles["h1"]))
    story.append(Paragraph(
        "One row per level. Top players ranked by composite. "
        "Promotion-Cands counts AAA-or-below players flagged as ready to move up; "
        "Demotion counts players the model thinks are underperforming for their level.",
        styles["muted"]
    ))

    headers = ["Level", "Top Hitter", "Top SP", "Top RP", "Promo Cands", "Demos"]
    rows: List[List[Any]] = []
    for d in level_data_list:
        all_hitters = [p for slots in d["placed"].values() for p in slots]
        top_h = max(all_hitters, key=lambda p: p["composite"], default=None)
        sp_list = d["pitcher_slots"].get("SP", [])
        rp_list = (
            d["pitcher_slots"].get("CL", [])
            + d["pitcher_slots"].get("SU", [])
            + d["pitcher_slots"].get("MR", [])
            + d["pitcher_slots"].get("LR", [])
        )
        top_sp = max(sp_list, key=lambda p: p["composite"], default=None)
        top_rp = max(rp_list, key=lambda p: p["composite"], default=None)

        def _fmt(p: Optional[Dict[str, Any]]) -> str:
            return f"{p['name']} ({p['composite']:.1f})" if p else "—"

        rows.append([
            d["level"],
            _fmt(top_h), _fmt(top_sp), _fmt(top_rp),
            len(d["promotions"]), len(d["demotions"]),
        ])

    col_widths = [0.55 * inch, 1.85 * inch, 1.75 * inch, 1.75 * inch, 0.8 * inch, 0.65 * inch]
    story.append(_build_table(headers, rows, col_widths))


def _promotion_ladder(
    story: List[Any],
    styles: Dict[str, ParagraphStyle],
    level_data_list: List[Dict[str, Any]],
) -> None:
    relevant = [d for d in level_data_list if d["promotions"]]
    if not relevant:
        return
    story.append(PageBreak())
    story.append(Paragraph("Promotion Ladder", styles["h1"]))
    story.append(Paragraph(
        "Who's pushing into each level from below. Top 8 candidates per level "
        "by composite edge. \"vs starter\" means the candidate would replace "
        "the current first-team player at that position; \"vs bench\" means "
        "they'd at least beat your weakest comparable on the roster.",
        styles["muted"]
    ))
    story.append(Spacer(1, 0.1 * inch))

    for d in relevant:
        story.append(Paragraph(f"Pushing for {d['level']}", styles["h2"]))
        headers = ["Cand", "Pos/Role", "VOS", "Comp", "Replaces", "Their Comp", "Edge", "Type"]
        rows: List[List[Any]] = []
        for cand, weakest, starter in d["promotions"][:8]:
            cc = cand["composite"]
            comp = starter or weakest
            comp_name = comp["name"] if comp else "—"
            comp_score = f"{comp['composite']:.1f}" if comp else "—"
            edge = f"+{cc - comp['composite']:.1f}" if comp else "—"
            label = "starter" if starter else "bench"
            pos_label = cand.get("primary_pos") or cand.get("proj_role", "")
            rows.append([
                cand["name"], pos_label, f"{cand['vos']:.1f}",
                f"{cc:.1f}", comp_name, comp_score, edge, label,
            ])
        col_widths = [1.4 * inch, 0.7 * inch, 0.55 * inch, 0.6 * inch, 1.4 * inch, 0.85 * inch, 0.55 * inch, 0.6 * inch]
        story.append(_build_table(headers, rows, col_widths))
        story.append(Spacer(1, 0.15 * inch))


def _level_section(
    story: List[Any],
    styles: Dict[str, ParagraphStyle],
    d: Dict[str, Any],
) -> None:
    story.append(PageBreak())
    story.append(Paragraph(f"Level: {d['level']}", styles["h1"]))

    # Lineup vs RHP
    if d["lineup_r"]:
        story.append(Paragraph(f"{d['level']} — Lineup vs RHP", styles["h2"]))
        headers = ["#", "Name", "Pos", "wOBA (vs R)"]
        rows = []
        for slot, p in d["lineup_r"]:
            sb = (p.get("hitter_bundle") or {}).get("vs_r", {})
            rows.append([
                slot, p["name"],
                p.get("_assigned_pos", p.get("primary_pos", "")),
                f"{sb.get('wOBA', 0):.3f}",
            ])
        story.append(_build_table(headers, rows, [0.4 * inch, 2.2 * inch, 0.7 * inch, 1.0 * inch]))
        story.append(Spacer(1, 0.1 * inch))

    # Lineup vs LHP
    if d["lineup_l"]:
        story.append(Paragraph(f"{d['level']} — Lineup vs LHP", styles["h2"]))
        headers = ["#", "Name", "Pos", "wOBA (vs L)"]
        rows = []
        for slot, p in d["lineup_l"]:
            sb = (p.get("hitter_bundle") or {}).get("vs_l", {})
            rows.append([
                slot, p["name"],
                p.get("_assigned_pos", p.get("primary_pos", "")),
                f"{sb.get('wOBA', 0):.3f}",
            ])
        story.append(_build_table(headers, rows, [0.4 * inch, 2.2 * inch, 0.7 * inch, 1.0 * inch]))
        story.append(Spacer(1, 0.1 * inch))

    # Bench
    if d["bench"]:
        story.append(Paragraph(f"{d['level']} — Bench / Flex", styles["h2"]))
        headers = ["Name", "Pos", "VOS", "Composite"]
        rows = [
            [p["name"], p.get("primary_pos", ""),
             f"{p['vos']:.1f}", f"{p['composite']:.1f}"]
            for p in d["bench"]
        ]
        story.append(_build_table(headers, rows, [2.2 * inch, 0.8 * inch, 0.7 * inch, 1.0 * inch]))
        story.append(Spacer(1, 0.1 * inch))

    # Pitching staff
    ps = d["pitcher_slots"]
    if any(ps.values()):
        story.append(Paragraph(f"{d['level']} — Pitching Staff", styles["h2"]))
        headers = ["Role", "Name", "Composite", "FIP"]
        rows = []
        for i, p in enumerate(ps.get("SP", []), start=1):
            b = (p.get("pitcher_bundle") or {}).get("overall", {})
            rows.append([f"SP{i}", p["name"], f"{p['composite']:.1f}", f"{b.get('FIP', 0):.2f}"])
        for role in ("CL", "SU", "MR", "LR"):
            for p in ps.get(role, []):
                b = (p.get("pitcher_bundle") or {}).get("overall", {})
                rows.append([role, p["name"], f"{p['composite']:.1f}", f"{b.get('FIP', 0):.2f}"])
        story.append(_build_table(headers, rows, [0.7 * inch, 2.2 * inch, 1.0 * inch, 0.8 * inch]))
        story.append(Spacer(1, 0.1 * inch))

    # Replacements
    if d["replacements"]:
        story.append(Paragraph(f"{d['level']} — Replacement Candidates", styles["h2"]))
        story.append(Paragraph(
            "On-roster players who would be displaced by the promotion candidates above.",
            styles["muted"]
        ))
        headers = ["Name", "Pos", "VOS", "Composite"]
        rows = [
            [p["name"], p.get("primary_pos", ""),
             f"{p['vos']:.1f}", f"{p['composite']:.1f}"]
            for p in d["replacements"]
        ]
        story.append(_build_table(headers, rows, [2.2 * inch, 0.8 * inch, 0.7 * inch, 1.0 * inch]))
        story.append(Spacer(1, 0.1 * inch))

    # Demotions
    if d["demotions"]:
        story.append(Paragraph(f"{d['level']} — Demotion Candidates", styles["h2"]))
        story.append(Paragraph(
            "Composite z-score below the configured underperform threshold, with sufficient sample.",
            styles["muted"]
        ))
        headers = ["Name", "Pos", "Composite", "Z"]
        rows = [
            [p["name"], p.get("primary_pos", ""),
             f"{p['composite']:.1f}", f"{p.get('_demote_z', 0):.2f}"]
            for p in d["demotions"]
        ]
        story.append(_build_table(headers, rows, [2.2 * inch, 0.8 * inch, 1.0 * inch, 0.7 * inch]))
        story.append(Spacer(1, 0.1 * inch))

    # Pitcher mismatches
    if d["mismatches"]:
        story.append(Paragraph(f"{d['level']} — Pitcher Role Mismatches", styles["h2"]))
        headers = ["Name", "Projected", "Suggested", "Reason"]
        rows = [
            [m["name"], m["projected"], m["suggested"], m["reason"]]
            for m in d["mismatches"]
        ]
        story.append(_build_table(headers, rows, [1.6 * inch, 0.9 * inch, 0.9 * inch, 2.5 * inch]))


# -----------------------------------------------------------------------------
# Public entry point
# -----------------------------------------------------------------------------

def render_pdf(
    out_path: Path,
    league: str,
    org: str,
    year: int,
    level_data_list: List[Dict[str, Any]],
) -> None:
    """Build a styled PDF org-summary report and write it to ``out_path``."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=letter,
        leftMargin=0.5 * inch, rightMargin=0.5 * inch,
        topMargin=0.5 * inch, bottomMargin=0.5 * inch,
        title=f"{org} — Org Depth Summary",
        author="VOS Toolkit",
    )
    styles = _make_styles()
    story: List[Any] = []

    _cover(story, styles, league, org, year)
    _glossary(story, styles)
    _org_snapshot(story, styles, level_data_list)
    _promotion_ladder(story, styles, level_data_list)
    for d in level_data_list:
        _level_section(story, styles, d)

    doc.build(story)

"""
Draft Grades PDF — renders a styled per-pick PDF table, optionally with a
team-summary page at the front.

Importable: `write_pdf(rows, output_path, ...)` takes the in-memory rows
produced by `draft_grades.compare_draft_to_projections` (and optionally
team-summary rows from `draft_grades.build_summary_rows`) and writes a PDF.

Also runnable as a standalone script using the hardcoded CONFIG block
below (kept for backward compatibility).
"""

# --- repo-root + core/ path bootstrap ---
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _p in (_ROOT, _os.path.join(_ROOT, "core")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
# --- end bootstrap ---

import csv
from typing import List, Dict, Optional


# Per-pick columns. Each entry: (csv_field, label, width_inches, align, kind)
# kind ∈ {None, 'delta', 'points', 'grade'} — controls coloring/formatting.
DEFAULT_COLUMNS = [
    ('Overall Pick', 'Pick',   0.40, 'CENTER', None),
    ('Player Name',  'Player', 2.20, 'LEFT',   None),
    ('Team',         'Team',   1.90, 'LEFT',   None),
    ('Delta',        'Delta',  0.60, 'CENTER', 'delta'),
    ('Points',       'Points', 0.75, 'CENTER', 'points'),
    ('Pick Grade',   'Grade',  0.65, 'CENTER', 'grade'),
]

PARK_ADJ_COLUMNS = [
    ('Overall Pick',   'Pick',   0.40, 'CENTER', None),
    ('Player Name',    'Player', 2.20, 'LEFT',   None),
    ('Team',           'Team',   1.90, 'LEFT',   None),
    ('Org Delta',      'Delta',  0.60, 'CENTER', 'delta'),
    ('Org Points',     'Points', 0.75, 'CENTER', 'points'),
    ('Org Pick Grade', 'Grade',  0.65, 'CENTER', 'grade'),
]

# Summary (team grades) columns — mirror draft_grades_summary.csv.
SUMMARY_COLUMNS = [
    ('Rank',           'Rk',       0.35, 'CENTER', None),
    ('Team',           'Team',     1.50, 'LEFT',   None),
    ('Top 100 Stamps', 'Top 100',  0.65, 'CENTER', None),
    ('Later Stamps',   'Later',    0.55, 'CENTER', None),
    ('Managed Risk',   'Mgd Risk', 0.70, 'CENTER', None),
    ('Total Points',   'Points',   0.65, 'CENTER', None),
    ('Base',           'Base',     0.55, 'CENTER', None),
    ('vs Base',        'vs Base',  0.65, 'CENTER', None),
    ('vDraft+',        'vDraft+',  0.65, 'CENTER', None),
    ('Grade',          'Grade',    0.55, 'CENTER', 'grade'),
]

PARK_ADJ_SUMMARY_COLUMNS = [
    ('Org Rank',         'Rk',       0.35, 'CENTER', None),
    ('Team',             'Team',     1.50, 'LEFT',   None),
    ('Org Top 100',      'Top 100',  0.65, 'CENTER', None),
    ('Org Later',        'Later',    0.55, 'CENTER', None),
    ('Org Managed Risk', 'Mgd Risk', 0.70, 'CENTER', None),
    ('Org Points',       'Points',   0.65, 'CENTER', None),
    ('Org Base',         'Base',     0.55, 'CENTER', None),
    ('Org vs Base',      'vs Base',  0.65, 'CENTER', None),
    ('Org vDraft+',      'vDraft+',  0.65, 'CENTER', None),
    ('Org Grade',        'Grade',    0.55, 'CENTER', 'grade'),
]


def write_pdf(
    pick_rows: List[Dict],
    output_path,
    title: str = 'Draft Grades',
    subtitle: str = '',
    max_picks: Optional[int] = None,
    columns=None,
    summary_rows: Optional[List[Dict]] = None,
    summary_columns=None,
    summary_title: str = 'Team Grades',
    summary_subtitle: str = '',
) -> None:
    """Render a styled PDF with optional team-summary page followed by per-pick pages.

    pick_rows:      list of dicts (from compare_draft_to_projections or raw CSV).
    columns:        per-pick column spec; defaults to DEFAULT_COLUMNS (market).
    summary_rows:   optional team-summary row dicts (rendered on page 1).
    summary_columns: column spec for the summary table; required if summary_rows is set.
    """
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.lib import colors
        from reportlab.lib.units import inch
        from reportlab.pdfgen import canvas
    except ImportError as e:
        raise ImportError(
            "PDF output requires reportlab. Install with: pip install reportlab"
        ) from e

    if columns is None:
        columns = DEFAULT_COLUMNS

    DARK_BG    = colors.HexColor('#0f1923')
    HEADER_BG  = colors.HexColor('#1e2d3d')
    ACCENT     = colors.HexColor('#2a6496')
    WHITE      = colors.white
    LIGHT_GRAY = colors.HexColor('#f5f7fa')
    MID_GRAY   = colors.HexColor('#e0e5eb')
    POS_GREEN  = colors.HexColor('#1a7a4a')
    NEG_RED    = colors.HexColor('#c0392b')
    GRADE_COLORS = {
        'S': colors.HexColor('#7b2fbe'),
        'A': colors.HexColor('#1a7a4a'),
        'B': colors.HexColor('#2a6496'),
        'C': colors.HexColor('#b07d00'),
        'D': colors.HexColor('#c0550a'),
        'F': colors.HexColor('#c0392b'),
    }

    W, H = letter
    LM = RM = TM = BM = 0.5 * inch
    PAGE_W = W - LM - RM
    ROW_H = 0.195 * inch
    HEADER_H = 0.26 * inch

    def compute_layout(cols):
        widths = [c[2] * inch for c in cols]
        diff = PAGE_W - sum(widths)
        left_cols = [i for i, c in enumerate(cols) if c[3] == 'LEFT']
        if left_cols:
            widths[left_cols[-1]] += diff
        xs = [LM]
        for w in widths[:-1]:
            xs.append(xs[-1] + w)
        return widths, xs

    def format_cell(row, field, kind):
        val = row.get(field, '')
        if val is None:
            val = ''
        if kind == 'delta':
            try:
                dv = int(val)
                return (f'+{dv}' if dv > 0 else str(dv)), dv
            except (ValueError, TypeError):
                return str(val), 0
        if kind == 'points':
            try:
                return f'{float(val):.2f}', None
            except (ValueError, TypeError):
                return '0.00', None
        return str(val), None

    c = canvas.Canvas(str(output_path), pagesize=letter)
    c.setTitle(title)

    def draw_title_block(y_top, page_title, page_subtitle):
        c.setFont('Helvetica-Bold', 20)
        c.setFillColor(DARK_BG)
        c.drawCentredString(W / 2, y_top - 0.35 * inch, page_title)
        if page_subtitle:
            c.setFont('Helvetica', 10)
            c.setFillColor(ACCENT)
            c.drawCentredString(W / 2, y_top - 0.65 * inch, page_subtitle)
            return y_top - 0.9 * inch
        return y_top - 0.55 * inch

    def draw_table_header(y, cols, widths, xs):
        c.setFillColor(HEADER_BG)
        c.rect(LM, y - HEADER_H, PAGE_W, HEADER_H, fill=1, stroke=0)
        c.setFillColor(WHITE)
        c.setFont('Helvetica-Bold', 8)
        for i, (_, label, _, align, _) in enumerate(cols):
            x, w = xs[i], widths[i]
            text_y = y - HEADER_H + 0.06 * inch
            if align == 'CENTER':
                c.drawCentredString(x + w / 2, text_y, label)
            else:
                c.drawString(x + 5, text_y, label)
        c.setStrokeColor(ACCENT)
        c.setLineWidth(1.5)
        c.line(LM, y - HEADER_H, LM + PAGE_W, y - HEADER_H)
        return y - HEADER_H

    def draw_row(y, row, bg, cols, widths, xs):
        c.setFillColor(bg)
        c.rect(LM, y - ROW_H, PAGE_W, ROW_H, fill=1, stroke=0)
        c.setStrokeColor(MID_GRAY)
        c.setLineWidth(0.25)
        c.line(LM, y - ROW_H, LM + PAGE_W, y - ROW_H)

        text_y = y - ROW_H + 0.055 * inch
        grade_field = next((f for f, _, _, _, k in cols if k == 'grade'), None)
        grade = str(row.get(grade_field, '')).strip() if grade_field else ''

        for i, (field, _, _, align, kind) in enumerate(cols):
            x, w = xs[i], widths[i]
            val, meta = format_cell(row, field, kind)

            if kind == 'grade':
                c.setFillColor(GRADE_COLORS.get(grade, colors.gray))
                c.setFont('Helvetica-Bold', 7.5)
            elif kind == 'delta':
                dv = meta or 0
                if dv > 0:
                    c.setFillColor(POS_GREEN)
                elif dv < 0:
                    c.setFillColor(NEG_RED)
                else:
                    c.setFillColor(DARK_BG)
                c.setFont('Helvetica', 7.5)
            else:
                c.setFillColor(DARK_BG)
                c.setFont('Helvetica', 7.5)

            if align == 'CENTER':
                c.drawCentredString(x + w / 2, text_y, val)
            else:
                c.drawString(x + 5, text_y, val)

        return y - ROW_H

    def draw_footer():
        c.setFont('Helvetica', 7)
        c.setFillColor(colors.gray)
        c.drawCentredString(
            W / 2, BM - 0.1 * inch,
            'Draft Analysis  ·  Delta = Pick − Projection'
            '  ·  Higher Points = Better Value'
        )

    def render_section(rows, cols, page_title, page_subtitle):
        widths, xs = compute_layout(cols)
        y = H - TM
        y = draw_title_block(y, page_title, page_subtitle)
        y -= 0.1 * inch
        y = draw_table_header(y, cols, widths, xs)
        for idx, row in enumerate(rows):
            if y - ROW_H < BM:
                draw_footer()
                c.showPage()
                y = H - TM
                y = draw_table_header(y, cols, widths, xs)
            bg = WHITE if idx % 2 == 0 else LIGHT_GRAY
            y = draw_row(y, row, bg, cols, widths, xs)
        draw_footer()

    # Optional summary page(s) first.
    if summary_rows is not None and summary_columns is not None:
        render_section(summary_rows, summary_columns, summary_title, summary_subtitle)
        c.showPage()

    # Per-pick page(s).
    def _pick_num(r):
        try:
            return int(r.get('Overall Pick', 0) or 0)
        except (ValueError, TypeError):
            return 0

    rows = sorted(pick_rows, key=_pick_num)
    if max_picks is not None:
        rows = [r for r in rows if _pick_num(r) <= max_picks]

    render_section(rows, columns, title, subtitle)

    c.save()


# ---------------------------------------------------------------------------
# Standalone CONFIG — used only when run as a script (kept for back-compat)
# ---------------------------------------------------------------------------
INPUT_CSV   = 'draft_grades_raw_VOS.csv'
OUTPUT_PDF  = 'draft_grades.pdf'
TITLE       = 'VOS Draft Grades'
SUBTITLE    = 'Rounds 1–5  ·  Picks 1–155'
MAX_PICKS   = 155


def _read_rows(path):
    with open(path) as f:
        return list(csv.DictReader(f))


def main():
    rows = _read_rows(INPUT_CSV)
    write_pdf(rows, OUTPUT_PDF, title=TITLE, subtitle=SUBTITLE, max_picks=MAX_PICKS)
    print(f'Saved: {OUTPUT_PDF}')


if __name__ == '__main__':
    main()

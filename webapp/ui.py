"""webapp/ui.py — shared LCARS UI components.

Small presentation helpers reused across the dashboard pages so the look stays
consistent and pages stop re-implementing the same `st.columns` / `st.metric` /
bold-markdown idioms by hand. Pure rendering: no data loading, no engine calls,
no session-state reads. The styling hooks (`.lcars-section`) are defined in
`app.py::build_theme_css`, so these track the active palette automatically.

Usage:
    import ui
    ui.lcars_section("Component scores")
    ui.metric_row([("Batting", "62.0"), ("Defense", "48.5")])
    ui.chip_line([f"Age {age}", team, f"Org {org}"])
"""
from __future__ import annotations

import html
from typing import Any, Iterable, Sequence, Tuple

import streamlit as st

# A metric spec is (label, value). `value` is rendered as-is — format it (e.g.
# via _num) before passing; these helpers deliberately don't format, so callers
# keep full control of precision/placeholders.
MetricSpec = Tuple[str, Any]


def metric_row(specs: Sequence[MetricSpec], *, columns: int | None = None,
               container: Any = None) -> None:
    """Render labeled metrics across an evenly-sized row of columns.

    Replaces the repeated `m = st.columns(n); m[0].metric(...)` idiom. `columns`
    defaults to one column per spec; pass it to fix the row width (keep rows
    short — surplus specs beyond `columns` are dropped rather than wrapped).
    `container` renders into a passed column/expander instead of the page root.
    """
    specs = list(specs)
    if not specs:
        return
    host = container or st
    n = columns or len(specs)
    cols = host.columns(n)
    for col, (label, value) in zip(cols, specs[:n]):
        col.metric(label, value)


def lcars_section(title: str, *, container: Any = None) -> None:
    """An LCARS-styled section header (accent cap + uppercase label bar).

    Drop-in replacement for `st.markdown("**Title**")` that matches the topbar
    look. Themed by the `--lcars-*` CSS vars, so it tracks the active palette.
    Requires the `.lcars-section` rules from `app.py::build_theme_css`.
    """
    (container or st).markdown(
        f'<div class="lcars-section"><span class="cap"></span>'
        f'<span class="label">{title}</span><span class="bar"></span></div>',
        unsafe_allow_html=True,
    )


def identity_header(name: str, cells: Sequence[Tuple[str, Any]] = (), *,
                    container: Any = None) -> None:
    """A bold LCARS title row: large name followed by a horizontal LCARS data
    bar that fills the remaining width. `cells` is a list of (label, value)
    segments rendered as connected LCARS pills (the last grows to fill); each
    shows a small uppercase label over its value. The headline for a detail
    panel (player, team, org). Pairs with `lcars_chips` (bio/tier pills) and a
    `metric_row` inside a bordered `st.container`. Styled by the `.lcars-id`
    rules in build_theme_css.
    """
    cells = list(cells)
    last = len(cells) - 1
    segs = "".join(
        f'<span class="cell{" grow" if i == last else ""}">'
        f'<span class="lbl">{html.escape(str(lbl))}</span>'
        f'<span class="val">{html.escape(str(val))}</span></span>'
        for i, (lbl, val) in enumerate(cells)
    )
    bar = f'<span class="idbar">{segs}</span>' if cells else ""
    (container or st).markdown(
        f'<div class="lcars-id"><span class="name">{html.escape(str(name))}</span>'
        f'{bar}</div>',
        unsafe_allow_html=True,
    )


def lcars_chips(items: Iterable[str], *, variant: str = "",
                container: Any = None) -> None:
    """Render items as a row of small LCARS pill chips (the bio / tier pattern,
    styled instead of a bare ` · ` caption). Blanks dropped; no-op if empty.
    `variant` adds a CSS modifier class (e.g. "tier") for a different accent.
    Styled by the `.lcars-chips` rules in build_theme_css.
    """
    parts = [str(x).strip() for x in items if str(x).strip()]
    if not parts:
        return
    cls = ("lcars-chips " + variant).strip()
    spans = "".join(f'<span class="chip">{html.escape(p)}</span>' for p in parts)
    (container or st).markdown(f'<div class="{cls}">{spans}</div>',
                               unsafe_allow_html=True)


def chip_line(items: Iterable[str], *, container: Any = None) -> None:
    """Render a ` · `-joined caption from the non-empty items.

    The bio / tiers / park-info pattern: pass a list of pre-built strings (blanks
    are dropped). No-op if nothing remains, so callers can pass conditionally-
    empty entries without guarding first.
    """
    parts = [str(x).strip() for x in items if str(x).strip()]
    if not parts:
        return
    (container or st).caption(" · ".join(parts))

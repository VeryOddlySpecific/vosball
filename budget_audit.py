"""Join the service-time payroll audit with team financials and produce
budget-augmented per-team data plus floor/cap stress-test scenarios.

Reads two CSVs:
  * `--audit-csv`: per-contract dump from payroll_audit.py
  * `--financials`: per-team financial snapshot from parse_financials.py

Joins them on team_id (preferred) or case-insensitive team name (fallback),
then writes three outputs:
  * payroll_budget_per_team_<TS>.md   — narrated table, dispersion summary
  * payroll_budget_per_team_<TS>.csv  — same data as CSV
  * cap_floor_scenarios_<TS>.md       — full floor/cap stress test

See USAGE_budget_audit.md for a walkthrough.
"""
from __future__ import annotations
import argparse
import csv
import statistics
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def fm(x: float) -> str:
    sign = "-" if x < 0 else ""
    return f"{sign}${abs(x)/1_000_000:.1f}M"


def fpct(x: float) -> str:
    return f"{x*100:.0f}%"


# ---------------------------------------------------------------------------
# IO
# ---------------------------------------------------------------------------

def load_audit(path: Path) -> List[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def load_financials(path: Path) -> List[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def to_f(v, d=0.0):
    try:
        if v is None or v == "":
            return d
        return float(v)
    except (TypeError, ValueError):
        return d


def to_i(v, d=0):
    try:
        if v is None or v == "":
            return d
        return int(float(v))
    except (TypeError, ValueError):
        return d


def _norm_name(s: str) -> str:
    """Case-insensitive, diacritics-stripped, whitespace-collapsed name key."""
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = " ".join(s.split())
    return s


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def aggregate_per_team(contracts: List[dict]) -> Dict[str, Dict]:
    """Build per-team rollup keyed on org name. Three buckets: CC, EXT, FA.

    Carries team_id through (from audit CSV) for downstream joins.
    """
    teams: Dict[str, Dict] = defaultdict(lambda: {
        "team_id": 0,
        "total": 0.0, "n": 0,
        "CC": 0.0, "CC_n": 0,
        "EXT": 0.0, "EXT_n": 0,
        "FA": 0.0, "FA_n": 0,
    })
    for c in contracts:
        org = c["org"].strip()
        bucket = c["bucket_svc"].strip()
        dollars = to_f(c["total_remaining"])
        tid = to_i(c.get("team_id"))
        teams[org]["team_id"] = tid or teams[org]["team_id"]
        teams[org]["total"] += dollars
        teams[org]["n"] += 1
        teams[org][bucket] += dollars
        teams[org][f"{bucket}_n"] += 1
    return teams


def build_per_team_table(
    teams: Dict[str, Dict],
    fin_rows: List[dict],
) -> List[dict]:
    """Combine payroll buckets with team financial snapshot.

    Matches by team_id (preferred) then by case-insensitive normalized name.
    Warns on any team that fails to match.
    """
    # Build two lookup tables on the financials side
    fin_by_id: Dict[int, dict] = {}
    fin_by_name: Dict[str, dict] = {}
    for r in fin_rows:
        tid = to_i(r.get("team_id"))
        if tid:
            fin_by_id[tid] = r
        nm = _norm_name(r.get("team", ""))
        if nm:
            fin_by_name[nm] = r

    rows: List[dict] = []
    unmatched: List[str] = []
    for org, p in teams.items():
        f: dict = {}
        if p.get("team_id") and p["team_id"] in fin_by_id:
            f = fin_by_id[p["team_id"]]
        else:
            f = fin_by_name.get(_norm_name(org), {})
        if not f:
            unmatched.append(org)

        budget = to_f(f.get("current_budget"))
        cur_payroll = to_f(f.get("player_payroll"))
        media = to_f(f.get("cur_media_revenue"))
        total_rev = to_f(f.get("cur_total_revenue"))
        total_exp = to_f(f.get("cur_total_expenses"))
        proj_bal = to_f(f.get("projected_balance"))
        starting_bal = to_f(f.get("cur_starting_balance"))
        attendance = to_f(f.get("cur_attendance"))

        rows.append({
            "team": org,
            "team_id": p.get("team_id", 0),
            # payroll commitment (remaining $ on books, service-time buckets)
            "committed_total": p["total"],
            "CC_$": p["CC"], "CC_n": p["CC_n"],
            "EXT_$": p["EXT"], "EXT_n": p["EXT_n"],
            "FA_$": p["FA"], "FA_n": p["FA_n"],
            "committed_n": p["n"],
            "pre_fa_share": (p["CC"] + p["EXT"]) / p["total"] if p["total"] else 0.0,
            # finances (this season)
            "current_budget": budget,
            "current_payroll": cur_payroll,
            "media_revenue": media,
            "total_revenue": total_rev,
            "total_expenses": total_exp,
            "projected_balance": proj_bal,
            "starting_balance": starting_bal,
            "attendance": attendance,
            # derived
            "budget_headroom": budget - cur_payroll,
            "media_share_of_rev": (media / total_rev) if total_rev else 0.0,
        })

    if unmatched:
        print(f"WARN: teams in audit with no financials match: {sorted(unmatched)}", file=sys.stderr)

    return rows


# ---------------------------------------------------------------------------
# Per-team narrative table
# ---------------------------------------------------------------------------

def write_per_team_md(rows: List[dict], out: Path) -> None:
    rows = sorted(rows, key=lambda r: -r["current_budget"])
    L: List[str] = []
    w = L.append
    w("# Per-team: Committed Payroll × Budget × Revenue (service-time buckets)")
    w("")
    w("Joins the service-time payroll audit (remaining $ committed by bucket) with current-season team financials.")
    w("")
    w("Notes:")
    w("- **Committed $ ≠ Current payroll.** Committed is remaining future contract $ (multi-year deals included). Payroll is this-season cash out the door.")
    w("- **Budget headroom** is `current_budget - current_payroll`. Positive = room to add at the deadline. Negative = team is already over their stated budget.")
    w("- **Pre-FA share** = (CC + EXT) committed / total committed. High pre-FA share = team has succeeded at locking up talent before FA.")
    w("")
    w("| Team | Budget | Payroll | Headroom | Media rev | Total rev | Proj bal | Committed | EXT $ | Pre-FA % |")
    w("|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        w(
            f"| {r['team']} | {fm(r['current_budget'])} | {fm(r['current_payroll'])} | {fm(r['budget_headroom'])} | "
            f"{fm(r['media_revenue'])} | {fm(r['total_revenue'])} | {fm(r['projected_balance'])} | "
            f"{fm(r['committed_total'])} | {fm(r['EXT_$'])} ({r['EXT_n']}) | {fpct(r['pre_fa_share'])} |"
        )
    w("")

    def disp(field: str, label: str):
        vals = [r[field] for r in rows]
        if not vals:
            return f"- **{label}:** _no data_"
        mn, mx = min(vals), max(vals)
        ratio = f"{mx/mn:.1f}x" if mn > 0 else f"{len([v for v in vals if v == 0])} teams at $0"
        return (
            f"- **{label}:** min {fm(mn)} (`{min(rows, key=lambda r: r[field])['team']}`), "
            f"max {fm(mx)} (`{max(rows, key=lambda r: r[field])['team']}`), "
            f"median {fm(statistics.median(vals))}, ratio {ratio}"
        )

    w("## Dispersion")
    w("")
    for field, label in [
        ("current_budget", "Current budget"),
        ("current_payroll", "Current payroll"),
        ("media_revenue", "Media revenue"),
        ("total_revenue", "Total revenue"),
        ("committed_total", "Committed contract $"),
        ("EXT_$", "EXT $ committed"),
    ]:
        w(disp(field, label))
    w("")
    w("> Revenue/budget gaps are structural — no cap/floor on payroll touches them. They constrain *capacity* to spend at the same time payroll caps would constrain *willingness*.")
    w("")

    out.write_text("\n".join(L), encoding="utf-8")


def write_per_team_csv(rows: List[dict], out: Path) -> None:
    if not rows:
        return
    cols = list(rows[0].keys())
    with out.open("w", encoding="utf-8", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(cols)
        for r in sorted(rows, key=lambda r: -r["current_budget"]):
            wr.writerow([r[c] for c in cols])


# ---------------------------------------------------------------------------
# Floor / cap scenarios
# ---------------------------------------------------------------------------

BASIS_FIELDS = {
    "payroll":   ("current_payroll",  "current payroll"),
    "committed": ("committed_total",  "committed contract $"),
    "budget":    ("current_budget",   "current budget"),
}


def write_scenarios(
    rows: List[dict],
    out: Path,
    *,
    floor_dollars: Optional[float] = None,
    cap_dollars: Optional[float] = None,
    cap_phase: int = 1,
    basis: str = "budget",
) -> None:
    if not rows:
        out.write_text("# Floor/Cap Stress Test vs Team Budgets\n\n_No data._\n", encoding="utf-8")
        return

    L: List[str] = []
    w = L.append

    payrolls = sorted([r["current_payroll"] for r in rows])
    median_payroll = statistics.median(payrolls)
    commits = sorted([r["committed_total"] for r in rows])
    median_commit = statistics.median(commits)

    w("# Floor/Cap Stress Test vs Team Budgets")
    w("")
    w(f"Anchor — **median current payroll = {fm(median_payroll)}**, **median committed = {fm(median_commit)}**.")
    w("")
    w("For each scenario:")
    w("- Identify teams forced to act (raise payroll if below floor; cut if above cap).")
    w("- Compute the required move in $, in % of the team's current budget, and the resulting projected balance.")
    w("- Negative \"after-move balance\" means a cap/floor at that level would push the team into operating loss.")
    w("")

    def floor_table(floor_pct: float, basis: str, basis_label: str, anchor: float):
        floor = anchor * floor_pct
        w(f"## Floor: {fpct(floor_pct)} of median {basis_label}  →  {fm(floor)}")
        w("")
        affected = [r for r in rows if r[basis] < floor]
        if not affected:
            w("_No teams below this floor._")
            w("")
            return
        w(f"**{len(affected)} teams forced to add payroll:**")
        w("")
        w("| Team | Current | Required +$ | % of budget | Proj bal now | Proj bal after |")
        w("|---|---|---|---|---|---|")
        for r in sorted(affected, key=lambda r: r[basis]):
            gap = floor - r[basis]
            pct_budget = (gap / r["current_budget"]) if r["current_budget"] else 0
            after = r["projected_balance"] - gap
            w(
                f"| {r['team']} | {fm(r[basis])} | {fm(gap)} | {fpct(pct_budget)} | "
                f"{fm(r['projected_balance'])} | {fm(after)} |"
            )
        w("")

    def cap_table(cap_pct: float, basis: str, basis_label: str, anchor: float):
        cap = anchor * cap_pct
        w(f"## Cap: {fpct(cap_pct)} of median {basis_label}  →  {fm(cap)}")
        w("")
        affected = [r for r in rows if r[basis] > cap]
        if not affected:
            w("_No teams above this cap._")
            w("")
            return
        w(f"**{len(affected)} teams forced to cut payroll:**")
        w("")
        w("| Team | Current | Required -$ | % of budget | EXT $ on books |")
        w("|---|---|---|---|---|")
        for r in sorted(affected, key=lambda r: -r[basis]):
            cut = r[basis] - cap
            pct_budget = (cut / r["current_budget"]) if r["current_budget"] else 0
            w(
                f"| {r['team']} | {fm(r[basis])} | -{fm(cut)} | {fpct(pct_budget)} | {fm(r['EXT_$'])} ({r['EXT_n']}) |"
            )
        w("")

    w("# Cash-payroll basis (this season)")
    w("")
    for p in (0.70, 0.80, 0.90):
        floor_table(p, "current_payroll", "current payroll", median_payroll)
    for p in (1.10, 1.25, 1.50):
        cap_table(p, "current_payroll", "current payroll", median_payroll)

    w("# Committed-contract basis (remaining future $)")
    w("")
    for p in (0.70, 0.80, 0.90):
        floor_table(p, "committed_total", "committed contract $", median_commit)
    for p in (1.10, 1.25, 1.50):
        cap_table(p, "committed_total", "committed contract $", median_commit)

    if floor_dollars is not None or cap_dollars is not None:
        if basis not in BASIS_FIELDS:
            raise ValueError(f"unknown basis '{basis}'; must be one of {list(BASIS_FIELDS)}")
        field, label = BASIS_FIELDS[basis]
        w("")
        w("# Proposed thresholds — explicit dollar amounts")
        w("")
        w(f"Basis: **{label}** (`{field}`).")
        w("")

        if floor_dollars is not None:
            w(f"## Floor: {fm(floor_dollars)}")
            w("")
            affected = [r for r in rows if r[field] < floor_dollars]
            if not affected:
                w("_No teams below this floor._")
                w("")
            else:
                w(f"**{len(affected)} teams below the floor:**")
                w("")
                w("| Team | Current | Required +$ | % of budget | Proj bal now | Proj bal after |")
                w("|---|---|---|---|---|---|")
                for r in sorted(affected, key=lambda r: r[field]):
                    gap = floor_dollars - r[field]
                    pct_budget = (gap / r["current_budget"]) if r["current_budget"] else 0
                    after = r["projected_balance"] - gap
                    w(
                        f"| {r['team']} | {fm(r[field])} | {fm(gap)} | {fpct(pct_budget)} | "
                        f"{fm(r['projected_balance'])} | {fm(after)} |"
                    )
                w("")

        if cap_dollars is not None:
            max_val = max(r[field] for r in rows)
            phase = max(1, int(cap_phase))
            if phase <= 1 or max_val <= cap_dollars:
                steps = [cap_dollars]
                w(f"## Cap: {fm(cap_dollars)}")
                w("")
            else:
                step_size = (max_val - cap_dollars) / phase
                steps = [max_val - step_size * i for i in range(1, phase + 1)]
                w(f"## Cap phase-in: {fm(max_val)} → {fm(cap_dollars)} over {phase} years")
                w("")
                w(
                    f"Linear step-down from current max ({fm(max_val)}) to target "
                    f"({fm(cap_dollars)}) — step size {fm(step_size)}/yr."
                )
                w("")

            for i, cap_val in enumerate(steps, start=1):
                if len(steps) > 1:
                    w(f"### Year {i}: cap = {fm(cap_val)}")
                    w("")
                affected = [r for r in rows if r[field] > cap_val]
                if not affected:
                    w("_No teams above this cap._")
                    w("")
                    continue
                w(f"**{len(affected)} teams above cap:**")
                w("")
                w("| Team | Current | Cut needed | % of budget | EXT $ on books |")
                w("|---|---|---|---|---|")
                for r in sorted(affected, key=lambda r: -r[field]):
                    cut = r[field] - cap_val
                    pct_budget = (cut / r["current_budget"]) if r["current_budget"] else 0
                    w(
                        f"| {r['team']} | {fm(r[field])} | -{fm(cut)} | {fpct(pct_budget)} | "
                        f"{fm(r['EXT_$'])} ({r['EXT_n']}) |"
                    )
                w("")

    out.write_text("\n".join(L), encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Join payroll audit with team financials, emit per-team table + cap/floor stress tests."
    )
    ap.add_argument("--audit-csv", required=True, type=Path,
                    help="payroll_audit_contracts_<league>_<TS>_svc.csv (from payroll_audit.py)")
    ap.add_argument("--financials", required=True, type=Path,
                    help="<league>_team_financials.csv (from parse_financials.py)")
    ap.add_argument("--league", default=None,
                    help="League slug. Used in output filenames. Optional.")
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="Output dir (default: dirname(--audit-csv))")
    ap.add_argument("--floor-dollars", type=float, default=None,
                    help="Explicit floor in $ (e.g. 135000000). Runs an extra scenario on --basis.")
    ap.add_argument("--cap-dollars", type=float, default=None,
                    help="Explicit cap in $ (e.g. 200000000). Runs an extra scenario on --basis.")
    ap.add_argument("--cap-phase", type=int, default=1,
                    help="Phase the cap from current max down to --cap-dollars over N years. "
                         "Default 1 (immediate). Linear step-down.")
    ap.add_argument("--basis", choices=list(BASIS_FIELDS.keys()), default="budget",
                    help="Column to evaluate --floor-dollars / --cap-dollars against. "
                         "'budget'=current_budget (default — matches OOTP owner allocation), "
                         "'payroll'=current_payroll (cash out this season), "
                         "'committed'=committed_total (remaining future $).")
    args = ap.parse_args()

    if not args.audit_csv.exists():
        sys.exit(f"audit CSV not found: {args.audit_csv}")
    if not args.financials.exists():
        sys.exit(f"financials CSV not found: {args.financials}")

    out_dir = args.out_dir or args.audit_csv.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    league = args.league or "league"

    contracts = load_audit(args.audit_csv)
    fin_rows = load_financials(args.financials)
    teams = aggregate_per_team(contracts)
    rows = build_per_team_table(teams, fin_rows)

    per_team_md = out_dir / f"payroll_budget_per_team_{league}_{ts}.md"
    per_team_csv = out_dir / f"payroll_budget_per_team_{league}_{ts}.csv"
    scenarios_md = out_dir / f"cap_floor_scenarios_{league}_{ts}.md"

    write_per_team_md(rows, per_team_md)
    write_per_team_csv(rows, per_team_csv)
    write_scenarios(
        rows, scenarios_md,
        floor_dollars=args.floor_dollars,
        cap_dollars=args.cap_dollars,
        cap_phase=args.cap_phase,
        basis=args.basis,
    )

    print(f"Wrote {per_team_md}")
    print(f"Wrote {per_team_csv}")
    print(f"Wrote {scenarios_md}")


if __name__ == "__main__":
    main()

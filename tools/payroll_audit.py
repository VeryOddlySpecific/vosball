"""Payroll composition audit — service-time-based contract bucketing.

Decomposes every active ML contract in a league into one of three buckets
based on MLB service time at signing, then aggregates per team and per
bucket to surface where payroll dispersion actually comes from.

Buckets, evaluated in order (signing_svc = mlb_service_years_now - (current_year - 1)):
    FA  : signing_svc >= 6                                  (open-market signing)
    EXT : signing_svc < 6 AND aav_remaining >= $8M          (real pre-FA extension)
    CC  : signing_svc < 6 AND aav_remaining <  $8M          (rookie scale / arb tender)

Missing service-time data falls back to a legacy signing_age<28 rule and is
flagged in the output.

See USAGE_payroll_audit.md for a walkthrough.

Usage:
    python payroll_audit.py --league sdmb \
        --eval <league>/eval/evaluation_summary_<league>_<TS>.csv \
        --players <league>/cache/stats/players.csv
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
import json
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

FA_SVC_THRESHOLD = 6.0          # MLB free-agent eligibility (service years)
ARB_SVC_THRESHOLD = 3.0         # MLB arbitration eligibility
EXT_AAV_THRESHOLD = 8_000_000   # split CC vs EXT within pre-FA
# NOTE: no VOS_Potential floor — the cheap pre-arb tenders need to count;
# their structural discount is the whole point of the CC bucket.


# ---------------------------------------------------------------------------
# IO helpers
# ---------------------------------------------------------------------------

def _to_float(v, default=0.0):
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _to_int(v, default=0):
    try:
        if v is None or v == "":
            return default
        return int(float(v))
    except (TypeError, ValueError):
        return default


def load_eval_csv(path: Path) -> List[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def load_players_lookup(path: Path) -> Dict[str, dict]:
    """One row per player from <league>/cache/stats/players.csv keyed by ID."""
    out: Dict[str, dict] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            pid = (row.get("ID") or "").strip()
            if pid:
                out[pid] = row
    return out


def load_teams(config_dir: Path, league: str) -> Dict[int, str]:
    """Build {team_id: 'Name Nickname'} from config/teams-<league>.json.

    Resolves the `Parent` field so that minor-league affiliates roll up to
    their MLB org. Players optioned to AAA/AA show up on contract rows with
    Contract_team_id set to the affiliate; without the rollup, payroll
    aggregations split those contracts off into phantom minor-league
    "teams." MLB-level teams use `"Parent": 0`.
    """
    path = config_dir / f"teams-{league}.json"
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return {}

    # First pass: collect raw {tid: (name, parent_tid)}.
    entries: Dict[int, Tuple[str, int]] = {}
    for tid_str, info in raw.items():
        if tid_str.startswith("_") or not isinstance(info, dict):
            continue
        try:
            tid = int(tid_str)
        except (TypeError, ValueError):
            continue
        name = info.get("Name") or ""
        nick = info.get("Nickname") or ""
        full = f"{name} {nick}".strip() or f"Team {tid}"
        parent_raw = info.get("Parent", 0)
        try:
            parent_tid = int(parent_raw) if parent_raw not in (None, "") else 0
        except (TypeError, ValueError):
            parent_tid = 0
        entries[tid] = (full, parent_tid)

    # Second pass: walk Parent chain to find the root MLB org for each tid.
    result: Dict[int, str] = {}
    for tid, (full, _) in entries.items():
        cur = tid
        seen: set = set()
        while cur in entries and entries[cur][1] not in (0, cur) and entries[cur][1] in entries:
            if cur in seen:
                break  # cycle guard
            seen.add(cur)
            cur = entries[cur][1]
        root_name = entries.get(cur, (full, 0))[0]
        result[tid] = root_name
    return result


# ---------------------------------------------------------------------------
# Per-contract structure
# ---------------------------------------------------------------------------

@dataclass
class Contract:
    pid: str
    name: str
    pos: str
    age: int
    team_id: int
    org: str
    vos_c: float
    vos_p: float
    yrs_total: int
    yr_cur: int
    yrs_remaining: int
    total_remaining: float
    aav_remaining: float
    signing_age: int
    # service time
    mlb_svc_now: float          # current
    signing_svc: float          # at signing
    svc_source: str             # "players_cache" or "missing"
    # buckets
    bucket_svc: str
    bucket_age: str             # legacy bucket for comparison


def _extract_salaries(r: dict, yrs: int, cur: int) -> Tuple[List[float], float, int]:
    """`cur` is 1-indexed contract year. We treat cur==0 as cur==1 upstream
    (OOTP exports cur=0 for not-yet-started 1-year tenders)."""
    sals: List[float] = []
    for i in range(yrs):
        sals.append(_to_float(r.get(f"Contract_salary{i}"), 0.0))
    yrs_rem = max(0, yrs - cur + 1)
    rem_slice = sals[cur - 1:cur - 1 + yrs_rem] if yrs_rem > 0 else []
    return rem_slice, sum(rem_slice), yrs_rem


def build_contracts(
    eval_rows: List[dict],
    players: Dict[str, dict],
    teams_map: Optional[Dict[int, str]] = None,
) -> List[Contract]:
    out: List[Contract] = []
    for r in eval_rows:
        if (r.get("League_Level") or "").strip() != "ML":
            continue
        yrs = _to_int(r.get("Contract_years"), 0)
        cur_raw = _to_int(r.get("Contract_current_year"), 0)
        cur = max(1, cur_raw)  # OOTP exports cur=0 for not-yet-started 1-yr tenders
        if yrs <= 0 or cur > yrs:
            continue
        sal0 = _to_float(r.get("Contract_salary0"), 0.0)
        if sal0 <= 0:
            continue
        vos_p = _to_float(r.get("VOS_Potential"), 0.0)

        rem_slice, total_rem, yrs_rem = _extract_salaries(r, yrs, cur)
        if yrs_rem <= 0 or total_rem <= 0:
            continue
        aav_rem = total_rem / yrs_rem

        age = _to_int(r.get("Age"), 0)
        signing_age = age - (cur - 1)

        pid = (r.get("Contract_player_id") or r.get("ID") or "").strip()
        pmeta = players.get(pid)
        if pmeta is not None and (pmeta.get("mlb_service_years") or "").strip() != "":
            svc_now = _to_float(pmeta.get("mlb_service_years"), -1.0)
            svc_source = "players_cache"
        else:
            svc_now = -1.0
            svc_source = "missing"
        signing_svc = svc_now - (cur - 1) if svc_now >= 0 else -1.0

        # Service-time bucket (with age fallback)
        if signing_svc < 0:
            if signing_age < 28 and aav_rem < EXT_AAV_THRESHOLD:
                bucket_svc = "CC"
            elif signing_age < 28 and aav_rem >= EXT_AAV_THRESHOLD:
                bucket_svc = "EXT"
            else:
                bucket_svc = "FA"
        else:
            if signing_svc >= FA_SVC_THRESHOLD:
                bucket_svc = "FA"
            elif aav_rem >= EXT_AAV_THRESHOLD:
                bucket_svc = "EXT"
            else:
                bucket_svc = "CC"

        # Legacy age bucket for diff
        if signing_age < 28 and aav_rem < EXT_AAV_THRESHOLD:
            bucket_age = "CC"
        elif signing_age < 28 and aav_rem >= EXT_AAV_THRESHOLD:
            bucket_age = "EXT"
        else:
            bucket_age = "FA"

        # Resolve team_id and canonical org name
        team_id = _to_int(r.get("Contract_team_id"), 0)
        raw_org = (r.get("Org") or r.get("Team") or "").strip()
        if teams_map and team_id in teams_map:
            org = teams_map[team_id]
        else:
            org = raw_org

        out.append(Contract(
            pid=pid,
            name=(r.get("Name") or pid).strip(),
            pos=(r.get("Pos") or "").strip(),
            age=age,
            team_id=team_id,
            org=org,
            vos_c=_to_float(r.get("VOS_Score"), 0.0),
            vos_p=vos_p,
            yrs_total=yrs,
            yr_cur=cur,
            yrs_remaining=yrs_rem,
            total_remaining=total_rem,
            aav_remaining=aav_rem,
            signing_age=signing_age,
            mlb_svc_now=svc_now,
            signing_svc=signing_svc,
            svc_source=svc_source,
            bucket_svc=bucket_svc,
            bucket_age=bucket_age,
        ))
    return out


# ---------------------------------------------------------------------------
# Aggregations
# ---------------------------------------------------------------------------

@dataclass
class BucketAgg:
    n: int = 0
    dollars: float = 0.0
    vos_years: float = 0.0
    aavs: List[float] = field(default_factory=list)


def aggregate_buckets(contracts: List[Contract], key: str) -> Dict[str, BucketAgg]:
    out: Dict[str, BucketAgg] = defaultdict(BucketAgg)
    for c in contracts:
        b = getattr(c, key)
        agg = out[b]
        agg.n += 1
        agg.dollars += c.total_remaining
        agg.vos_years += c.vos_p * c.yrs_remaining
        agg.aavs.append(c.aav_remaining)
    return out


def aggregate_team_buckets(contracts: List[Contract], key: str) -> Dict[str, Dict[str, BucketAgg]]:
    out: Dict[str, Dict[str, BucketAgg]] = defaultdict(lambda: defaultdict(BucketAgg))
    for c in contracts:
        b = getattr(c, key)
        agg = out[c.org][b]
        agg.n += 1
        agg.dollars += c.total_remaining
        agg.vos_years += c.vos_p * c.yrs_remaining
        agg.aavs.append(c.aav_remaining)
    return out


def variance_share(team_buckets: Dict[str, Dict[str, BucketAgg]], buckets=("CC", "EXT", "FA")) -> Dict[str, float]:
    """% of total payroll variance attributable to each bucket (population variance)."""
    teams = sorted(team_buckets.keys())
    totals = [sum(team_buckets[t][b].dollars for b in buckets) for t in teams]
    if len(totals) < 2:
        return {b: 0.0 for b in buckets}
    var_total = statistics.pvariance(totals)
    out: Dict[str, float] = {}
    for b in buckets:
        vals = [team_buckets[t][b].dollars for t in teams]
        out[b] = (statistics.pvariance(vals) / var_total) if var_total > 0 else 0.0
    return out


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def fmt_m(x: float) -> str:
    sign = "-" if x < 0 else ""
    return f"{sign}${abs(x)/1_000_000:.1f}M"


def fmt_k(x: float) -> str:
    return f"${x/1_000:.0f}K"


def write_report(contracts: List[Contract], out_md: Path, ts: str, league: str) -> None:
    n_total = len(contracts)
    total_dollars = sum(c.total_remaining for c in contracts)
    missing = [c for c in contracts if c.svc_source == "missing"]
    pct_missing = (len(missing) / n_total * 100) if n_total else 0

    buckets_svc = aggregate_buckets(contracts, "bucket_svc")
    team_buckets_svc = aggregate_team_buckets(contracts, "bucket_svc")
    team_buckets_age = aggregate_team_buckets(contracts, "bucket_age")
    team_totals = {t: sum(team_buckets_svc[t][b].dollars for b in ("CC", "EXT", "FA")) for t in team_buckets_svc}

    var_svc = variance_share(team_buckets_svc)
    var_age = variance_share(team_buckets_age)

    payrolls = sorted(team_totals.values())
    pmax, pmin = max(payrolls), min(payrolls)
    pmed = statistics.median(payrolls)
    pmean = statistics.mean(payrolls)
    pstd = statistics.pstdev(payrolls)

    lines: List[str] = []
    w = lines.append

    w(f"# {league.upper()} Payroll Composition Audit — Service-Time Bucketing")
    w("")
    w(f"_Generated {ts}. Buckets keyed on MLB service years at signing rather than age._")
    w("")
    w("## Bucket definitions")
    w("")
    w("`signing_svc = current_mlb_service_years - (Contract_current_year - 1)`")
    w("")
    w("- **CC – Cost-Controlled**: signed pre-FA (`signing_svc < 6`), AAV remaining `< $8M`. Rookie-scale, pre-arb, arb tenders.")
    w("- **EXT – Pre-FA Extension**: signed pre-FA (`signing_svc < 6`), AAV remaining `>= $8M`. Real buy-the-cheap-years extensions.")
    w("- **FA – Free Agent**: signed at FA-eligible service time (`signing_svc >= 6`). Open-market.")
    w("")
    w(f"_Active ML contracts analyzed: **{n_total}**. Total $ committed (remaining only): **{fmt_m(total_dollars)}**._")
    w("")
    w(f"_Service-time data: **{n_total - len(missing)} of {n_total}** ({100-pct_missing:.1f}%) from players cache; **{len(missing)}** fell back to age<28 heuristic._")
    w("")

    w("## League-wide composition (service-time buckets)")
    w("")
    w("| Bucket | Contracts | $ committed | % league $ | Median AAV | $/VOS-year |")
    w("|---|---|---|---|---|---|")
    for b in ("CC", "EXT", "FA"):
        a = buckets_svc.get(b, BucketAgg())
        pct = (a.dollars / total_dollars * 100) if total_dollars else 0
        median_aav = statistics.median(a.aavs) if a.aavs else 0.0
        per_vos = (a.dollars / a.vos_years) if a.vos_years else 0.0
        w(f"| {b} | {a.n} | {fmt_m(a.dollars)} | {pct:.1f}% | {fmt_m(median_aav)} | {fmt_k(per_vos)} |")
    w("")

    w("## Payroll dispersion")
    w("")
    w(f"- **Max:** {fmt_m(pmax)}  ")
    w(f"- **Min:** {fmt_m(pmin)}  ")
    w(f"- **Median:** {fmt_m(pmed)}  ")
    w(f"- **Mean:** {fmt_m(pmean)}  ")
    w(f"- **Max/Min ratio:** {pmax/pmin:.1f}x  " if pmin > 0 else "- **Max/Min ratio:** n/a  ")
    w(f"- **Std dev:** {fmt_m(pstd)}  ")
    w("")

    w("## Where the dispersion comes from")
    w("")
    w("| Bucket | Min team $ | Max team $ | Mean | Std dev | % of total variance (svc) | (legacy age) |")
    w("|---|---|---|---|---|---|---|")
    for b in ("CC", "EXT", "FA"):
        vals_svc = [team_buckets_svc[t][b].dollars for t in team_buckets_svc]
        mn = min(vals_svc) if vals_svc else 0
        mx = max(vals_svc) if vals_svc else 0
        mean_b = statistics.mean(vals_svc) if vals_svc else 0
        std_b = statistics.pstdev(vals_svc) if vals_svc else 0
        w(f"| {b} | {fmt_m(mn)} | {fmt_m(mx)} | {fmt_m(mean_b)} | {fmt_m(std_b)} | {var_svc.get(b,0)*100:.0f}% | {var_age.get(b,0)*100:.0f}% |")
    w("")
    w("_Buckets are correlated, so % variance doesn't sum to 100._")
    w("")

    w("## Per-team breakdown ($ committed by service-time bucket)")
    w("")
    w("| Team | Total $ | CC $ (n) | EXT $ (n) | FA $ (n) | % FA | % pre-FA |")
    w("|---|---|---|---|---|---|---|")
    for t in sorted(team_totals, key=lambda x: -team_totals[x]):
        tb = team_buckets_svc[t]
        tot = team_totals[t]
        cc = tb["CC"]; ext = tb["EXT"]; fa = tb["FA"]
        fa_pct = (fa.dollars / tot * 100) if tot else 0
        pre_fa_pct = 100 - fa_pct
        w(f"| {t} | {fmt_m(tot)} | {fmt_m(cc.dollars)} ({cc.n}) | {fmt_m(ext.dollars)} ({ext.n}) | {fmt_m(fa.dollars)} ({fa.n}) | {fa_pct:.0f}% | {pre_fa_pct:.0f}% |")
    w("")

    ext_contracts = sorted(
        [c for c in contracts if c.bucket_svc == "EXT"],
        key=lambda c: -c.total_remaining,
    )
    w(f"## All EXT contracts ({len(ext_contracts)} league-wide)")
    w("")
    w("| Player | Org | Age now | Signing age | Signing svc | VOS_P | Yrs rem | AAV | Total rem | Bucket changed? |")
    w("|---|---|---|---|---|---|---|---|---|---|")
    for c in ext_contracts:
        svc_str = f"{c.signing_svc:.1f}" if c.signing_svc >= 0 else "—"
        changed = "" if c.bucket_svc == c.bucket_age else f"was {c.bucket_age}"
        w(f"| {c.name} | {c.org} | {c.age} | {c.signing_age} | {svc_str} | {c.vos_p:.0f} | {c.yrs_remaining} | {fmt_m(c.aav_remaining)} | {fmt_m(c.total_remaining)} | {changed} |")
    w("")

    out_md.write_text("\n".join(lines), encoding="utf-8")


def write_compare(contracts: List[Contract], out_md: Path, ts: str) -> None:
    """Diff legacy age<28 bucket vs service-time bucket."""
    moved = [c for c in contracts if c.bucket_svc != c.bucket_age]
    by_transition: Dict[Tuple[str, str], List[Contract]] = defaultdict(list)
    for c in moved:
        by_transition[(c.bucket_age, c.bucket_svc)].append(c)

    lines: List[str] = []
    w = lines.append
    w("# Bucket transition: age<28 → service-time")
    w("")
    w(f"_Generated {ts}._")
    w("")
    w(f"**{len(moved)} of {len(contracts)}** contracts changed bucket ({len(moved)/len(contracts)*100:.1f}%).")
    w("")
    w("## Transition matrix (counts)")
    w("")
    w("| From (age) ↓ / To (svc) → | CC | EXT | FA |")
    w("|---|---|---|---|")
    for from_b in ("CC", "EXT", "FA"):
        row = [f"**{from_b}**"]
        for to_b in ("CC", "EXT", "FA"):
            if from_b == to_b:
                n = sum(1 for c in contracts if c.bucket_age == from_b and c.bucket_svc == to_b)
                row.append(f"{n}")
            else:
                n = len(by_transition.get((from_b, to_b), []))
                row.append(f"**{n}**" if n else "0")
        w("| " + " | ".join(row) + " |")
    w("")

    for (from_b, to_b), cs in sorted(by_transition.items()):
        if not cs:
            continue
        w(f"## {from_b} → {to_b}  ({len(cs)} contracts, {fmt_m(sum(c.total_remaining for c in cs))} total)")
        w("")
        w("| Player | Org | Age now | Signing age | Signing svc | AAV | Yrs rem | Total rem |")
        w("|---|---|---|---|---|---|---|---|")
        for c in sorted(cs, key=lambda c: -c.total_remaining):
            svc_str = f"{c.signing_svc:.1f}" if c.signing_svc >= 0 else "—"
            w(f"| {c.name} | {c.org} | {c.age} | {c.signing_age} | {svc_str} | {fmt_m(c.aav_remaining)} | {c.yrs_remaining} | {fmt_m(c.total_remaining)} |")
        w("")

    out_md.write_text("\n".join(lines), encoding="utf-8")


def write_contracts_csv(contracts: List[Contract], out_csv: Path) -> None:
    cols = [
        "pid", "name", "pos", "age", "team_id", "org",
        "vos_c", "vos_p",
        "yrs_total", "yr_cur", "yrs_remaining",
        "total_remaining", "aav_remaining",
        "signing_age",
        "mlb_svc_now", "signing_svc", "svc_source",
        "bucket_svc", "bucket_age",
    ]
    with out_csv.open("w", encoding="utf-8", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(cols)
        for c in contracts:
            wr.writerow([getattr(c, k) for k in cols])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Payroll composition audit with service-time-based contract bucketing."
    )
    ap.add_argument("--eval", required=True, type=Path,
                    help="evaluation_summary_<league>_<TS>.csv (from vos_v2 --contracts)")
    ap.add_argument("--players", required=True, type=Path,
                    help="<league>/cache/stats/players.csv (cached /players endpoint)")
    ap.add_argument("--league", default=None,
                    help="League slug (e.g. sdmb). If provided, team names are canonicalized "
                         "via config/teams-<league>.json. Also used in output filenames.")
    ap.add_argument("--config-dir", type=Path, default=Path(__file__).parent / "config",
                    help="Directory containing teams-<league>.json (default: ./config)")
    ap.add_argument("--out", type=Path, default=None,
                    help="Output dir (default: dirname(eval)/../contract_audit)")
    args = ap.parse_args()

    if not args.eval.exists():
        sys.exit(f"eval CSV not found: {args.eval}")
    if not args.players.exists():
        sys.exit(f"players cache not found: {args.players}")

    league = args.league or "league"

    teams_map: Dict[int, str] = {}
    if args.league:
        teams_map = load_teams(args.config_dir, args.league)
        if not teams_map:
            print(f"WARN: teams-{args.league}.json not found or empty in {args.config_dir}. "
                  f"Using raw team names from eval CSV.", file=sys.stderr)

    out_dir = args.out or (args.eval.parent.parent / "contract_audit")
    out_dir.mkdir(parents=True, exist_ok=True)

    eval_rows = load_eval_csv(args.eval)
    players = load_players_lookup(args.players)
    contracts = build_contracts(eval_rows, players, teams_map=teams_map)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    main_md = out_dir / f"payroll_composition_audit_{league}_{ts}_svc.md"
    compare_md = out_dir / f"payroll_audit_compare_{league}_{ts}_svc.md"
    contracts_csv = out_dir / f"payroll_audit_contracts_{league}_{ts}_svc.csv"

    write_report(contracts, main_md, ts, league)
    write_compare(contracts, compare_md, ts)
    write_contracts_csv(contracts, contracts_csv)

    print(f"Wrote {main_md}")
    print(f"Wrote {compare_md}")
    print(f"Wrote {contracts_csv}")
    print(f"\nContracts analyzed: {len(contracts)}")
    print(f"Missing service-time fallbacks: {sum(1 for c in contracts if c.svc_source == 'missing')}")


if __name__ == "__main__":
    main()

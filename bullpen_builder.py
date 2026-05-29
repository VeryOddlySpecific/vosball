#!/usr/bin/env python3
"""
bullpen_builder.py — Score an org's relievers for high-leverage fit and
suggest role assignments (CL / SU / MR / MOP).

Uses weights_rp_leverage_v1.json (trained separately by
analysis/fit_rp_leverage_v1.py against career leverage outcomes
saves >= 20 OR holds >= 50). The leverage model identifies RP profiles
that historically earned high-leverage usage — different signal than v6
Reach (which predicts "any MLB role") or v6/v5 Career (which predicts
"value if reached").

Outputs two scores per RP:
  - LeverageRole (L1):  20-80, sigmoid of logistic P(earned leverage role)
  - LeverageTrust (L2): 20-80, scaled prediction of career avg LI

Combined into:
  - VOS_LeverageScore = 0.6 * L1 + 0.4 * L2  (tunable via --alpha)

Roles are assigned by tiered cutoffs on VOS_LeverageScore:
  >= 65: Closer
  55-65: Setup
  45-55: Middle
  < 45:  Mop-Up / Long Relief

Surfaces "leverage-fit gaps":
  - "Closer profile, low VOS_Career"  — sleeper closer the dev system missed
  - "High VOS_Career, mop-up profile" — good pitcher but wrong shape for late innings

Usage:
  py bullpen_builder.py --league ndl --org "Alabama Bears"
  py bullpen_builder.py --league woba --org "Boston Red Sox" --include-sps
  py bullpen_builder.py --league sahl --org "Houston Astros" --alpha 0.7
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_DIR = SCRIPT_DIR / "config"
DATA_DIR = SCRIPT_DIR / "data"
LEVERAGE_WEIGHTS = CONFIG_DIR / "weights_rp_leverage_v1.json"
PLAYER_DATA_TEMPLATE = "PlayerData-{league}.csv"

# Role assignment tiers (on VOS_LeverageScore 20-80 scale)
ROLE_CUTOFFS = [
    (65.0, "Closer"),
    (55.0, "Setup"),
    (45.0, "Middle"),
    (0.0,  "Mop-Up / Long"),
]

# Default blend between L1 (binary "role earned") and L2 (continuous "avg LI")
DEFAULT_ALPHA = 0.6


def f(x: Any, default: float = float("nan")) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def load_leverage_weights() -> Dict[str, Any]:
    if not LEVERAGE_WEIGHTS.exists():
        sys.exit(f"leverage weights not found: {LEVERAGE_WEIGHTS}\n"
                 f"  Run analysis/fit_rp_leverage_v1.py first to generate.")
    with LEVERAGE_WEIGHTS.open(encoding="utf-8") as fp:
        return json.load(fp)


def extract_pitcher_features(row: Dict[str, str]) -> Dict[str, float]:
    """Mirror analysis/fit_rp_leverage_v1.extract_features. Must match exactly."""
    age = f(row.get("Age"))
    feats: Dict[str, float] = {"age": age}
    for c in ("PotStf", "PotMov", "PotHRA", "PotCtrl", "PotPBABIP",
              "Stf", "Mov", "HRA", "PBABIP",
              "PotFst", "PotSnk", "PotCutt", "PotCrv", "PotSld",
              "PotChg", "PotSplt", "Stm"):
        feats[c] = f(row.get(c))
    ctrl = f(row.get("Ctrl"))
    if math.isnan(ctrl):
        ctrl = f(row.get("Ctrl_R"))
    if math.isnan(ctrl):
        ctrl = f(row.get("Ctrl_L"))
    feats["Ctrl"] = ctrl
    pitches: List[float] = []
    for c in ("PotFst", "PotSnk", "PotCutt", "PotCrv", "PotSld",
              "PotChg", "PotSplt", "PotFrk", "PotCirChg", "PotScr",
              "PotKncrv", "PotKnbl"):
        v = f(row.get(c))
        if not math.isnan(v) and v > 0:
            pitches.append(v)
    pitches.sort(reverse=True)
    feats["pitch_top3"] = (sum(pitches[:3]) / 3.0 if len(pitches) >= 3
                           else (sum(pitches) / len(pitches) if pitches else float("nan")))
    feats["plus_pitches"] = float(sum(1 for p in pitches if p >= 55))
    feats["elite_pitches"] = float(sum(1 for p in pitches if p >= 70))
    return feats


def apply_logistic(feats: Dict[str, float], model: Dict[str, Any]) -> float:
    """Run a binary logistic model. Returns p in [0,1]."""
    names: List[str] = model["features"]
    means: List[float] = model["means"]
    stds: List[float] = model["stds"]
    medians: List[float] = model["medians"]
    coefs: List[float] = model["coefs"]
    intercept = float(model["intercept"])
    logit = intercept
    for i, name in enumerate(names):
        v = feats.get(name, float("nan"))
        if v is None or math.isnan(v):
            v = medians[i]
        sd = stds[i] if stds[i] != 0 else 1.0
        z = (v - means[i]) / sd
        logit += coefs[i] * z
    try:
        return 1.0 / (1.0 + math.exp(-logit))
    except OverflowError:
        return 0.0 if logit < 0 else 1.0


def apply_ridge(feats: Dict[str, float], model: Dict[str, Any]) -> float:
    """Run a continuous ridge model. Returns the raw prediction."""
    names: List[str] = model["features"]
    means: List[float] = model["means"]
    stds: List[float] = model["stds"]
    medians: List[float] = model["medians"]
    coefs: List[float] = model["coefs"]
    intercept = float(model["intercept"])
    pred = intercept
    for i, name in enumerate(names):
        v = feats.get(name, float("nan"))
        if v is None or math.isnan(v):
            v = medians[i]
        sd = stds[i] if stds[i] != 0 else 1.0
        z = (v - means[i]) / sd
        pred += coefs[i] * z
    return pred


def prob_to_20_80(p: float, baseline: float) -> float:
    """Re-anchor a logistic probability onto a 20-80 score using the
    population base rate as the midpoint. Without this, a model with
    18% positive base rate maxes out around L1=50 for elite profiles
    because P~=1 is essentially unreachable.

    Maps:  p=0       -> 20 (clearly non-leverage)
           p=baseline -> 50 (average RP)
           p=1       -> 80 (definitely a leverage role)
    Linear in each half. Clamped to [20, 80].
    """
    if p >= baseline:
        score = 50.0 + 30.0 * (p - baseline) / (1.0 - baseline)
    else:
        score = 50.0 - 30.0 * (baseline - p) / baseline
    return max(20.0, min(80.0, score))


def li_to_20_80(li: float, target_mean: float, target_std: float) -> float:
    """Map predicted career avg LI onto a 20-80 score, anchored at the
    population mean.  mean -> 50; +2σ -> 80; -2σ -> 20.  Clamped."""
    z = (li - target_mean) / target_std if target_std > 0 else 0.0
    score = 50.0 + 15.0 * z
    return max(20.0, min(80.0, score))


def assign_role(score: float) -> str:
    for cutoff, label in ROLE_CUTOFFS:
        if score >= cutoff:
            return label
    return ROLE_CUTOFFS[-1][1]


def is_reliever(row: Dict[str, str], include_sps: bool) -> bool:
    pos = (row.get("Pos") or "").strip().upper()
    if pos in ("RP", "CL", "MR", "SU", "LR", "P"):
        return True
    if include_sps and pos == "SP":
        return True
    return False


def find_latest_eval(league: str) -> Optional[Path]:
    """Find the most recent evaluation_summary CSV for the league. Looks
    across both the league-root eval/ dir AND any per-team subdirs (since
    run_vos_all.py with --per-org-evals writes only into per-team subdirs).
    Picks the newest by filename timestamp; if a direct and nested file
    share the same timestamp, prefers the nested one (per-org-evals is
    production mode as of 2026-05-21)."""
    eval_root = SCRIPT_DIR / league / "eval"
    if not eval_root.is_dir():
        return None
    # All eval CSVs at any depth
    candidates: List[Path] = []
    candidates.extend(eval_root.glob(f"evaluation_summary_{league}_*.csv"))
    candidates.extend(eval_root.glob(f"*/evaluation_summary_{league}_*.csv"))
    if not candidates:
        return None
    # Sort by (filename timestamp, depth) — depth as a tie-break preferring nested
    def _key(p: Path) -> Tuple[str, int]:
        return (p.name, 1 if p.parent != eval_root else 0)
    candidates.sort(key=_key)
    return candidates[-1]


def load_eval(path: Path) -> List[Dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as fp:
        return list(csv.DictReader(fp))


def load_playerdata(league: str) -> Dict[str, Dict[str, str]]:
    """Load PlayerData-{league}.csv keyed by ID. Contains the raw rating
    columns the leverage model needs (eval CSVs don't expose them)."""
    path = DATA_DIR / PLAYER_DATA_TEMPLATE.format(league=league)
    if not path.is_file():
        sys.exit(f"PlayerData not found: {path}")
    out: Dict[str, Dict[str, str]] = {}
    with path.open(encoding="utf-8", newline="") as fp:
        for r in csv.DictReader(fp):
            pid = (r.get("ID") or "").strip()
            if pid:
                out[pid] = r
    return out


def main() -> int:
    p = argparse.ArgumentParser(
        description="Score an org's RPs for high-leverage fit and suggest roles."
    )
    p.add_argument("--league", required=True, help="League slug (e.g. ndl, woba).")
    p.add_argument("--org", required=True,
                   help="Organization name as it appears in eval CSV's Org column.")
    p.add_argument("--input", type=Path, default=None,
                   help="Override eval CSV. Default: newest under {league}/eval/.")
    p.add_argument("--include-sps", action="store_true",
                   help="Include SPs in scoring (they typically score low — "
                        "useful to confirm starter profiles don't accidentally "
                        "rank high for leverage).")
    p.add_argument("--alpha", type=float, default=DEFAULT_ALPHA,
                   help=f"Blend: VOS_LeverageScore = alpha*L1 + (1-alpha)*L2. "
                        f"Default {DEFAULT_ALPHA} (lean toward role-earned binary).")
    p.add_argument("--top", type=int, default=15,
                   help="How many RPs to show in the depth table.")
    p.add_argument("--output-dir", type=Path, default=None,
                   help="Output directory. Default: {league}/bullpen/.")
    args = p.parse_args()

    weights = load_leverage_weights()
    lev = weights.get("scoring_modes", {}).get("vos_leverage_rp_v1", {})
    l1 = lev.get("l1_leverage_role")
    l2 = lev.get("l2_career_avg_li")
    if not l1 or not l2:
        sys.exit("weights file missing l1_leverage_role or l2_career_avg_li")

    # Resolve eval source
    eval_path = args.input or find_latest_eval(args.league)
    if not eval_path or not eval_path.exists():
        sys.exit(f"no eval CSV found for league '{args.league}'. "
                 f"Run run_vos_all.py first or pass --input.")
    print(f"  eval source: {eval_path}", file=sys.stderr)

    rows = load_eval(eval_path)
    org_norm = args.org.strip().lower()
    org_eval_rows = [r for r in rows
                     if (r.get("Org") or "").strip().lower() == org_norm
                     and is_reliever(r, args.include_sps)]
    if not org_eval_rows:
        sys.exit(f"no RPs found in org '{args.org}' for league '{args.league}'.")
    # Raw ratings live in PlayerData, not in the eval CSV. Join on ID.
    pd = load_playerdata(args.league)
    print(f"  scoring {len(org_eval_rows)} pitchers in {args.org} "
          f"(joined with PlayerData for raw ratings)...", file=sys.stderr)

    # Score each — re-anchor both component scores around population baselines
    # so the 20-80 scale is interpretable as "relative to the average MLB RP."
    l1_baseline = float(l1.get("positive_rate", 0.187))
    l2_mean = float(l2.get("target_mean", 0.92))
    l2_std  = float(l2.get("target_std",  0.28))
    scored: List[Dict[str, Any]] = []
    n_missing = 0
    for r in org_eval_rows:
        pid = (r.get("ID") or "").strip()
        raw = pd.get(pid)
        if raw is None:
            n_missing += 1
            continue
        feats = extract_pitcher_features(raw)
        p_role = apply_logistic(feats, l1)
        l1_score = prob_to_20_80(p_role, l1_baseline)
        pred_li = apply_ridge(feats, l2)
        l2_score = li_to_20_80(pred_li, l2_mean, l2_std)
        leverage_score = args.alpha * l1_score + (1.0 - args.alpha) * l2_score
        scored.append({
            "ID":         r.get("ID", ""),
            "Name":       r.get("Name", ""),
            "Pos":        r.get("Pos", ""),
            "Age":        r.get("Age", ""),
            "Level":      r.get("League_Level", ""),
            "Team":       r.get("Team", ""),
            "VOS_Reach":  f(r.get("VOS_Reach"), float("nan")),
            "VOS_Career": f(r.get("VOS_Career"), float("nan")),
            "VOS_Blended": f(r.get("VOS_Blended"), float("nan")),
            "L1_LeverageRole":  round(l1_score, 2),
            "L2_LeverageTrust": round(l2_score, 2),
            "predicted_avg_LI": round(pred_li, 3),
            "P_LeverageRole":   round(p_role, 3),
            "VOS_LeverageScore": round(leverage_score, 2),
            "Suggested_Role":   assign_role(leverage_score),
        })

    if n_missing:
        print(f"  WARNING: {n_missing} eval row(s) had no matching ID in "
              f"PlayerData — skipped.", file=sys.stderr)
    scored.sort(key=lambda r: -r["VOS_LeverageScore"])

    # Identify leverage-fit gaps for the report. Tighter thresholds so the
    # note only fires on genuinely actionable profiles.
    # Sleeper: HIGH leverage profile but NOT already designated as a closer.
    # Misfit: good Career but profiles as middle/mop-up.
    for i, r in enumerate(scored, 1):
        r["leverage_rank"] = i
        career = r["VOS_Career"] if not math.isnan(r["VOS_Career"]) else 50.0
        lev = r["VOS_LeverageScore"]
        pos = (r.get("Pos") or "").strip().upper()
        gap = lev - career
        r["fit_gap"] = round(gap, 2)
        if lev >= 60 and gap >= 10 and pos != "CL":
            r["gap_note"] = "Sleeper closer (RP-designated, closer profile)"
        elif lev <= 50 and gap <= -10:
            r["gap_note"] = "Bench arm (good RP, wrong shape for late innings)"
        else:
            r["gap_note"] = ""

    # Build output paths
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = args.output_dir or (SCRIPT_DIR / args.league / "bullpen")
    out_dir.mkdir(parents=True, exist_ok=True)
    org_slug = args.org.lower().replace(" ", "_")
    csv_path = out_dir / f"bullpen_{org_slug}_{ts}.csv"
    md_path  = out_dir / f"bullpen_{org_slug}_{ts}.md"

    # CSV
    csv_cols = ["leverage_rank", "Suggested_Role", "ID", "Name", "Pos",
                "Age", "Level", "Team",
                "VOS_LeverageScore", "L1_LeverageRole", "L2_LeverageTrust",
                "P_LeverageRole", "predicted_avg_LI",
                "VOS_Reach", "VOS_Career", "VOS_Blended",
                "fit_gap", "gap_note"]
    with csv_path.open("w", encoding="utf-8", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=csv_cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(scored)

    # MD report
    lines: List[str] = [
        f"# Bullpen Builder — {args.org}",
        f"_{args.league.upper()} · generated {datetime.now().strftime('%Y-%m-%d %H:%M')}_",
        "",
        f"Source: `{eval_path.name}`",
        f"Scored: {len(scored)} pitchers"
        + (" (incl. SPs)" if args.include_sps else " (RPs only)"),
        f"Blend: VOS_LeverageScore = {args.alpha:.2f} × L1 + {1-args.alpha:.2f} × L2",
        "",
        "## Suggested Bullpen Order",
        "",
        "_Ranked by VOS_LeverageScore. Role cutoffs: ≥65 = Closer, 55-65 = Setup, "
        "45-55 = Middle, <45 = Mop-Up/Long._",
        "",
        "| # | Role | Name | Pos | Age | Lvl | VOS_Lev | L1 | L2 | Reach | Career | Gap | Note |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in scored[: args.top]:
        career = f"{r['VOS_Career']:.1f}" if not math.isnan(r["VOS_Career"]) else "—"
        reach  = f"{r['VOS_Reach']:.1f}" if not math.isnan(r["VOS_Reach"]) else "—"
        lines.append(
            f"| {r['leverage_rank']} | {r['Suggested_Role']} | {r['Name']} | "
            f"{r['Pos']} | {r['Age']} | {r['Level']} | "
            f"**{r['VOS_LeverageScore']:.1f}** | {r['L1_LeverageRole']:.1f} | "
            f"{r['L2_LeverageTrust']:.1f} | {reach} | {career} | "
            f"{r['fit_gap']:+.1f} | {r['gap_note']} |"
        )
    lines += [
        "",
        "## Leverage-Fit Gaps",
        "",
        "_Players whose leverage profile differs meaningfully from their "
        "overall Career value. Worth a second look._",
        "",
    ]
    sleepers = [r for r in scored
                if r["VOS_LeverageScore"] >= 60 and r["fit_gap"] >= 10
                and (r.get("Pos") or "").strip().upper() != "CL"]
    misfits  = [r for r in scored
                if r["VOS_LeverageScore"] <= 50 and r["fit_gap"] <= -10
                and r["VOS_Career"] >= 50]
    if sleepers:
        lines.append("### Sleeper closer profiles (leverage > career)")
        lines.append("")
        lines.append("| Name | Pos | Age | VOS_Lev | Career | Gap |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for r in sorted(sleepers, key=lambda x: -x["fit_gap"])[:10]:
            lines.append(
                f"| {r['Name']} | {r['Pos']} | {r['Age']} | "
                f"{r['VOS_LeverageScore']:.1f} | {r['VOS_Career']:.1f} | "
                f"{r['fit_gap']:+.1f} |"
            )
        lines.append("")
    if misfits:
        lines.append("### Bench-arm profiles (career > leverage)")
        lines.append("")
        lines.append("| Name | Pos | Age | VOS_Lev | Career | Gap |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for r in sorted(misfits, key=lambda x: x["fit_gap"])[:10]:
            lines.append(
                f"| {r['Name']} | {r['Pos']} | {r['Age']} | "
                f"{r['VOS_LeverageScore']:.1f} | {r['VOS_Career']:.1f} | "
                f"{r['fit_gap']:+.1f} |"
            )
        lines.append("")
    if not sleepers and not misfits:
        lines.append("_No meaningful leverage-fit gaps (>=5 pt deltas) in this org._")
        lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")

    print(f"  wrote {csv_path}", file=sys.stderr)
    print(f"  wrote {md_path}", file=sys.stderr)
    # Console summary: top 5 by role
    print(file=sys.stderr)
    print(f"  Top 5 leverage profiles in {args.org}:", file=sys.stderr)
    for r in scored[:5]:
        print(f"    #{r['leverage_rank']}  {r['Suggested_Role']:<12}  "
              f"{r['Name']:<24} {r['Pos']:>3}  "
              f"VOS_Lev={r['VOS_LeverageScore']:.1f}  "
              f"Career={r['VOS_Career']:.1f}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

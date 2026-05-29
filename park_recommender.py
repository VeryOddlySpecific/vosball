#!/usr/bin/env python3
"""
park_recommender.py

Recommends park factors that maximize the value of an organization's existing
talent (ML + farm), based on the OOTP ratings dump.

Approach: compute the org's z-scores vs the league for each ratings dimension
that has a corresponding park-factor knob, then map z -> factor with a clamped
linear function. Output is a drop-in `park-factors-*.json`, a CSV ranking of
themed alternatives, and a markdown writeup explaining the recommendation.

Two factor formats are produced:
  1. raw_factors (avg/doubles/triples/HR splits) - matches the OOTP UI input
  2. tool_adjustments - matches the existing park-factors-*.json schema for VOS

Usage:
    python park_recommender.py --league wwoba --org "Arizona Diamondbacks"
    python park_recommender.py --league ndl --org "My Team" --raw-alpha 0.15

Caveats baked in:
- Park factors are league-relative. Net edge per outcome = your batter strength
  on that outcome MINUS your pitcher/defense strength that the factor erodes.
- ETA-weighted: ML > AAA > AA > ... so multi-year park decisions skew toward
  the farm, but old prospects are penalized.
- Half of games are at home, so absolute uplift is bounded.
- Ks (batting tool) is inverted: high Ks rating = MORE strikeouts = bad.
- Raw factors are approximations from ratings, not derived from sim outcomes.
"""

import argparse
import csv
import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LEVEL_WEIGHTS = {
    "ML": 1.00, "AAA": 0.85, "AA": 0.65, "A+": 0.50, "A": 0.40,
    "A-": 0.35, "R": 0.25, "IND": 0.10, "INT": 0.10, "COL": 0.20, "HS": 0.15,
}

PITCHER_POS = {"SP", "RP", "CL", "P"}

BATTING_DIMS  = ["Pow", "Gap", "Eye", "Ks"]
DEFENSE_DIMS  = ["OFR", "IFR"]
BASERUN_DIMS  = ["Speed", "Run", "StealAbi", "StlRt"]
PITCHER_DIMS  = ["Stuff", "Movement", "Control", "HR_Avoid"]

COLUMN_MAP = {
    "Pow": "Pow", "Gap": "Gap", "Eye": "Eye", "Ks": "Ks",
    "OFR": "OFR", "IFR": "IFR",
    "Speed": "Speed", "Run": "Run", "StealAbi": "Steal", "StlRt": "StlRt",
    "Stuff": "Stf", "Movement": "Mov", "Control": "Ctrl_R", "HR_Avoid": "HRA",
}

INVERTED_DIMS = {("batting", "Ks")}

RAW_FACTOR_KEYS = ["avg_rhb", "avg_lhb", "avg_overall",
                   "doubles", "triples",
                   "hr_rhb", "hr_lhb", "hr_overall"]

# ---------------------------------------------------------------------------
# Loading and weighting
# ---------------------------------------------------------------------------

def load_player_data(csv_path):
    with open(csv_path, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))

def to_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default

def player_weight(row):
    lvl = row.get("LgLvl", "").strip()
    age = to_float(row.get("Age", 0))
    base = LEVEL_WEIGHTS.get(lvl, 0.20)
    if lvl != "ML" and age >= 27:
        base *= 0.5
    return base

def is_pitcher(row): return row.get("Pos", "").strip() in PITCHER_POS
def is_batter(row):  return not is_pitcher(row)

# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def weighted_stats(rows, column, position_filter, bats=None):
    """Weighted mean and stddev. Optional Bats filter; switch-hitters at half weight."""
    total_w = total_v = 0.0
    vals = []
    for r in rows:
        if not position_filter(r):
            continue
        b = r.get("Bats", "").strip().upper()
        if bats and b not in (bats, "S"):
            continue
        w = player_weight(r)
        if bats and b == "S":
            w *= 0.5
        v = to_float(r.get(column, 0))
        if w <= 0 or v <= 0:
            continue
        total_w += w
        total_v += w * v
        vals.append((w, v))
    if total_w == 0:
        return 0.0, 0.0
    mean = total_v / total_w
    var = sum(w * (v - mean) ** 2 for w, v in vals) / total_w
    return mean, var ** 0.5

def z_for(org_rows, league_rows, column, position_filter, bats=None):
    org_avg, _    = weighted_stats(org_rows, column, position_filter, bats)
    lg_avg, lg_sd = weighted_stats(league_rows, column, position_filter, bats)
    return ((org_avg - lg_avg) / lg_sd) if lg_sd > 0 else 0.0

# ---------------------------------------------------------------------------
# Z-scores for tool_adjustments
# ---------------------------------------------------------------------------

def compute_z_scores(org_rows, league_rows):
    z = {}
    groups = [
        ("batting",         BATTING_DIMS, is_batter),
        ("defense",         DEFENSE_DIMS, is_batter),
        ("baserunning",     BASERUN_DIMS, is_batter),
        ("pitcher_ability", PITCHER_DIMS, is_pitcher),
    ]
    for group, dims, pos_filter in groups:
        for dim in dims:
            col = COLUMN_MAP[dim]
            z[(group, dim)] = z_for(org_rows, league_rows, col, pos_filter)
    return z

# ---------------------------------------------------------------------------
# Profile builders
# ---------------------------------------------------------------------------

def z_to_factor(z, alpha, lo, hi):
    return max(lo, min(hi, 1.0 + alpha * z))

def build_recommended_profile(z_scores, alpha, lo, hi):
    profile = {g: {} for g in ("batting", "defense", "baserunning", "pitcher_ability")}
    for (group, dim), z in z_scores.items():
        eff_z = -z if (group, dim) in INVERTED_DIMS else z
        profile[group][dim] = z_to_factor(eff_z, alpha, lo, hi)
    return profile

def score_profile(profile, z_scores):
    total = 0.0
    for (group, dim), z in z_scores.items():
        factor = profile.get(group, {}).get(dim, 1.0)
        eff_z = -z if (group, dim) in INVERTED_DIMS else z
        total += eff_z * (factor - 1.0)
    return total

def build_raw_factors(org_rows, league_rows, alpha=0.10, bounds=0.20):
    """OOTP raw outcome factors: avg/doubles/triples/HR splits."""
    lo, hi = 1.0 - bounds, 1.0 + bounds
    f = lambda net: z_to_factor(net, alpha, lo, hi)

    rhb_pow     = z_for(org_rows, league_rows, "Pow", is_batter, bats="R")
    lhb_pow     = z_for(org_rows, league_rows, "Pow", is_batter, bats="L")
    overall_pow = z_for(org_rows, league_rows, "Pow", is_batter)

    pit_hra_r = z_for(org_rows, league_rows, "HRA_R", is_pitcher)
    pit_hra_l = z_for(org_rows, league_rows, "HRA_L", is_pitcher)
    pit_hra   = z_for(org_rows, league_rows, "HRA",   is_pitcher)

    gap_z   = z_for(org_rows, league_rows, "Gap",   is_batter)
    speed_z = z_for(org_rows, league_rows, "Speed", is_batter)

    rhb_babip = z_for(org_rows, league_rows, "BABIP", is_batter, bats="R")
    lhb_babip = z_for(org_rows, league_rows, "BABIP", is_batter, bats="L")
    ovl_babip = z_for(org_rows, league_rows, "BABIP", is_batter)
    rhb_cntct = z_for(org_rows, league_rows, "Cntct", is_batter, bats="R")
    lhb_cntct = z_for(org_rows, league_rows, "Cntct", is_batter, bats="L")
    ovl_cntct = z_for(org_rows, league_rows, "Cntct", is_batter)

    pit_babip = z_for(org_rows, league_rows, "PBABIP", is_pitcher)
    ofr = z_for(org_rows, league_rows, "OFR", is_batter)
    ifr = z_for(org_rows, league_rows, "IFR", is_batter)
    defense_z = (ofr + ifr) / 2.0
    avg_suppress = 0.5 * (pit_babip + defense_z)

    return {
        "avg_rhb":    f((rhb_babip + 0.5 * rhb_cntct) - avg_suppress),
        "avg_lhb":    f((lhb_babip + 0.5 * lhb_cntct) - avg_suppress),
        "avg_overall":f((ovl_babip + 0.5 * ovl_cntct) - avg_suppress),
        "doubles":    f(gap_z),
        "triples":    f((gap_z + speed_z) / 2.0),
        "hr_rhb":     f(rhb_pow - pit_hra_r),
        "hr_lhb":     f(lhb_pow - pit_hra_l),
        "hr_overall": f(overall_pow - pit_hra),
    }

def format_park_factors_block(raw):
    """OOTP-UI layout matching the in-game display."""
    return (
        "                              Park Factors\n"
        "\n"
        f"  Average RHB: {raw['avg_rhb']:.3f}     "
        f"Doubles: {raw['doubles']:.3f}     "
        f"Home Runs RHB: {raw['hr_rhb']:.3f}\n"
        f"  Average LHB: {raw['avg_lhb']:.3f}     "
        f"Triples: {raw['triples']:.3f}     "
        f"Home Runs LHB: {raw['hr_lhb']:.3f}\n"
        f"  Average:     {raw['avg_overall']:.3f}                       "
        f"Home Runs:     {raw['hr_overall']:.3f}"
    )

def themed_profiles():
    def neutral():
        return {
            "batting":         {d: 1.0 for d in BATTING_DIMS},
            "defense":         {d: 1.0 for d in DEFENSE_DIMS},
            "baserunning":     {d: 1.0 for d in BASERUN_DIMS},
            "pitcher_ability": {d: 1.0 for d in PITCHER_DIMS},
        }
    profiles = {"Neutral": neutral()}

    p = neutral()
    p["batting"]["Pow"] = 1.10; p["pitcher_ability"]["HR_Avoid"] = 1.05
    profiles["HR-Friendly"] = p

    p = neutral()
    p["batting"]["Pow"] = 0.92; p["pitcher_ability"]["HR_Avoid"] = 0.95
    p["defense"]["OFR"] = 1.05
    profiles["Pitcher-Friendly"] = p

    p = neutral()
    p["batting"]["Gap"] = 1.10
    p["baserunning"]["Speed"] = 1.05; p["baserunning"]["Run"] = 1.08
    profiles["Gap-and-Run"] = p

    p = neutral()
    p["batting"]["Eye"] = 1.05; p["batting"]["Ks"] = 0.95
    p["pitcher_ability"]["Control"] = 1.05
    profiles["Patient-Contact"] = p

    return profiles

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_json(out_path, org, profile, raw_factors):
    payload = {
        "_comment": f"Auto-generated by park_recommender.py for {org}.",
        "parks": {
            f"{org.replace(' ', '_')}_Recommended": {
                "name": f"{org} Recommended Park",
                "raw_factors": raw_factors,
                "tool_adjustments": profile,
            }
        },
        "team_to_park_mapping": {},
        "application_rules": {
            "apply_to_prospects": True,
            "apply_to_major_leaguers": True,
            "use_handedness_splits": False,
            "adjustment_strength": 0.5,
        },
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

def write_csv(out_path, alternatives, z_scores):
    header = ["Profile", "Score", "Pow", "Gap", "Eye", "Ks(bat)",
              "OFR", "IFR", "Speed", "Run",
              "Stuff", "Movement", "Control", "HR_Avoid"]
    ranked = sorted(alternatives.items(), key=lambda kv: -score_profile(kv[1], z_scores))
    with open(out_path, "w", newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(header)
        for name, p in ranked:
            w.writerow([
                name, f"{score_profile(p, z_scores):+.3f}",
                f"{p['batting']['Pow']:.3f}", f"{p['batting']['Gap']:.3f}",
                f"{p['batting']['Eye']:.3f}", f"{p['batting']['Ks']:.3f}",
                f"{p['defense']['OFR']:.3f}", f"{p['defense']['IFR']:.3f}",
                f"{p['baserunning']['Speed']:.3f}", f"{p['baserunning']['Run']:.3f}",
                f"{p['pitcher_ability']['Stuff']:.3f}", f"{p['pitcher_ability']['Movement']:.3f}",
                f"{p['pitcher_ability']['Control']:.3f}", f"{p['pitcher_ability']['HR_Avoid']:.3f}",
            ])

def write_markdown(out_path, args, org_rows, league_rows, z_scores, recommended, raw_factors):
    L = []
    L.append(f"# Park Recommendation: {args.org}")
    L.append("")
    L.append(f"League: `{args.league}` &middot; "
             f"Org roster: {len(org_rows)} &middot; "
             f"League pool: {len(league_rows)}")
    L.append("")
    L.append("## Recommended park factors (OOTP input format)")
    L.append("")
    L.append("```")
    L.append(format_park_factors_block(raw_factors))
    L.append("```")
    L.append("")
    L.append("Enter these values when designing the new stadium. Each is the org's "
             "net relative-edge z-score (your batter strength minus your pitcher/"
             f"defense weakness on that outcome) mapped via "
             f"`clamp(1 + {args.raw_alpha} * z, "
             f"{1-args.raw_bounds:.2f}, {1+args.raw_bounds:.2f})`.")
    L.append("")
    L.append("## Talent profile (z-scores vs league)")
    L.append("")
    L.append("Positive z = above league avg. ETA-weighted (ML > farm).")
    L.append("")
    L.append("| Group | Dimension | Z-score | Direction |")
    L.append("|---|---|---|---|")
    for (g, d), z in z_scores.items():
        inv = (g, d) in INVERTED_DIMS
        direction = "lower-is-better" if inv else "higher-is-better"
        L.append(f"| {g} | {d} | {z:+.2f} | {direction} |")
    L.append("")
    L.append("## VOS tool adjustments (drop-in for park-factors-*.json)")
    L.append("")
    L.append(f"Mapping: `clamp(1 + {args.alpha} * z, "
             f"{1-args.bounds:.2f}, {1+args.bounds:.2f})`. "
             "Inverted dims (batting Ks) flip sign.")
    L.append("")
    L.append("```json")
    L.append(json.dumps(recommended, indent=2))
    L.append("```")
    L.append("")
    L.append("## Top drivers")
    L.append("")
    drivers = sorted(z_scores.items(), key=lambda kv: -abs(kv[1]))[:5]
    for (g, d), z in drivers:
        inv = (g, d) in INVERTED_DIMS
        push = "down" if (z > 0 and inv) or (z < 0 and not inv) else "up"
        L.append(f"- **{g}.{d}** ({z:+.2f}sigma) -> push factor {push}")
    L.append("")
    L.append("## Caveats")
    L.append("")
    L.append("- Half-season home effect: only ~half of games are at your park.")
    L.append("- Pitcher and batter sides interact; opponents play here too.")
    L.append("- Re-run annually; farm-driven z-scores drift as prospects mature.")
    L.append("- OOTP rating spreads are narrow; modest z can still be meaningful.")
    L.append("- Raw factors are approximations from ratings, not from sim outcomes.")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Recommend park factors that fit org talent.")
    ap.add_argument("--league", required=True, help="League ID (e.g. ndl)")
    ap.add_argument("--org", required=True, help="Organization name or numeric team ID")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--output-dir", default=None,
                    help="Default: <league>/park_recommendations")
    ap.add_argument("--alpha", type=float, default=0.06,
                    help="VOS tool-adjustment sensitivity (default 0.06)")
    ap.add_argument("--bounds", type=float, default=0.12,
                    help="VOS tool-adjustment max deviation (default 0.12)")
    ap.add_argument("--raw-alpha", type=float, default=0.10,
                    help="Raw outcome-factor sensitivity (default 0.10)")
    ap.add_argument("--raw-bounds", type=float, default=0.20,
                    help="Raw outcome-factor max deviation (default 0.20)")
    args = ap.parse_args()

    csv_path = Path(args.data_dir) / f"PlayerData-{args.league}.csv"
    if not csv_path.exists():
        sys.exit(f"ERROR: {csv_path} not found")

    teams_path = Path("config") / f"teams-{args.league}.json"
    teams = {}
    if teams_path.exists():
        with open(teams_path, encoding="utf-8") as f:
            teams = json.load(f)

    org_id = args.org.strip()
    org_label = org_id
    if not org_id.isdigit():
        match_id = None
        for tid, tinfo in teams.items():
            full = f"{tinfo.get('Name','')} {tinfo.get('Nickname','')}".strip()
            if args.org.strip().lower() in (full.lower(),
                                             tinfo.get("Name","").lower(),
                                             tinfo.get("Nickname","").lower()):
                match_id = tid
                org_label = full
                break
        if match_id is None:
            sys.exit(f"ERROR: could not resolve org '{args.org}' from {teams_path}")
        org_id = match_id
    elif org_id in teams:
        t = teams[org_id]
        org_label = f"{t.get('Name','')} {t.get('Nickname','')}".strip()

    rows = load_player_data(csv_path)
    org_rows = [r for r in rows if r.get("Org", "").strip() == org_id]
    if not org_rows:
        sys.exit(f"ERROR: no rows with Org='{org_id}' (resolved from '{args.org}')")

    args.org = org_label
    print(f"Loaded {len(rows)} players in league, {len(org_rows)} in org '{org_label}' (id={org_id})")

    z_scores = compute_z_scores(org_rows, rows)
    lo, hi = 1.0 - args.bounds, 1.0 + args.bounds
    recommended = build_recommended_profile(z_scores, args.alpha, lo, hi)
    raw_factors = build_raw_factors(org_rows, rows,
                                    alpha=args.raw_alpha, bounds=args.raw_bounds)

    out_dir = Path(args.output_dir) if args.output_dir else Path(args.league) / "park_recommendations"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_org = args.org.replace(" ", "_").replace("/", "_")

    json_path = out_dir / f"park_recommendation-{safe_org}.json"
    csv_path_out = out_dir / f"park_recommendation-{safe_org}.csv"
    md_path = out_dir / f"park_recommendation-{safe_org}.md"

    write_json(json_path, args.org, recommended, raw_factors)

    alternatives = themed_profiles()
    alternatives[f"{args.org}_Recommended"] = recommended
    write_csv(csv_path_out, alternatives, z_scores)

    write_markdown(md_path, args, org_rows, rows, z_scores, recommended, raw_factors)

    rec_score = score_profile(recommended, z_scores)
    print(f"\nWrote:\n  {json_path}\n  {csv_path_out}\n  {md_path}")
    print(f"\nVOS profile relative-edge score: {rec_score:+.3f} (Neutral=0.000)")
    print()
    print(format_park_factors_block(raw_factors))

if __name__ == "__main__":
    main()

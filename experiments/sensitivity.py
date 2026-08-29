#!/usr/bin/env python
"""Sensitivity runs — the one slide that answers "does it survive reality?"

    python -m experiments.sensitivity            # all scenarios
    python -m experiments.sensitivity --quick

There is no external ground truth for this system (README A6: the block
registers, machine rosters and control charts are internal to IR). So the
validation argument is not "we matched the real answer", it is three things
that can be checked without one:

  * INTERNAL CONSISTENCY -- every plan passes an independently written
    feasibility validator;
  * BASELINE COMPARISON  -- every result is quoted against the greedy planner
    on byte-identical inputs;
  * SENSITIVITY          -- the plan degrades in the direction and roughly the
    magnitude a section engineer would predict when the world gets worse.

If any scenario below moved the wrong way, the model would be wrong, and this
script is how you would find out.
"""
from __future__ import annotations

import argparse
import json
import os

from core.model import OUT_DIR
from core.pipeline import build_world
from optimizer.cpsat import solve_cpsat
from optimizer.greedy import solve_greedy
from optimizer.validate import validate
from sim.kpis import evaluate_mc
from sim.simulator import simulate

SCENARIOS = [
    {"name": "baseline", "kw": {}, "why": "the plan as delivered"},
    {"name": "traffic +20%", "kw": {"traffic_multiplier": 1.2},
     "why": "growth, or a diversion onto this route"},
    {"name": "freight +50%", "kw": {"freight_per_day_per_direction": 18},
     "why": "goods surge — the traffic nobody publishes"},
    {"name": "one CSM down", "kw": {"disabled_machines": ["CSM-1"]},
     "why": "tamping machine under repair"},
    {"name": "CSM + BCM down", "kw": {"disabled_machines": ["CSM-1", "BCM-1"]},
     "why": "two machines out at once"},
    {"name": "no corridor policy", "kw": {"corridor_blocks": False},
     "why": "what the section looks like without the reserved window"},
    {"name": "tolerance 4 h/day", "kw": {"block_hours": 4.0},
     "why": "operations tightens the tap"},
    {"name": "tolerance 12 h/day", "kw": {"block_hours": 12.0},
     "why": "operations opens it"},
]


def run_one(kw: dict, time_limit: float) -> dict:
    block_hours = kw.pop("block_hours", None)
    w = build_world(max_jobs=200, **kw)
    if block_hours is not None:
        w.cfg["planning"]["max_block_hours_per_day"] = block_hours
    g = solve_greedy(w)
    c = solve_cpsat(w, time_limit=time_limit, hint_plan=g)
    eg = evaluate_mc(w, g, n=5)
    ec = evaluate_mc(w, c, n=5)
    return {
        "trains_per_day": w.reports["traffic"]["trains_per_day"],
        "corridor_hours": w.reports.get("corridor", {}).get(
            "total_reserved_hours", 0.0),
        "greedy_cleared": eg["overdue_km_days_cleared"],
        "cpsat_cleared": ec["overdue_km_days_cleared"],
        "cpsat_blocks": ec["jobs_completed"],
        "cpsat_avail_km": ec["availability_km_equivalent"],
        "cpsat_aai": ec["aai_with_plan"],
        "block_hours": ec["block_hours_granted"],
        "pax_punctuality": ec["punctuality_passenger_pct"],
        "pax_delay_mean": ec["mean_added_delay_passenger_min"],
        "bursts": ec["block_bursts"],
        "worst_p95_delay": ec["worst_p95_delay_min"],
        "status": c.solver_status,
        "gap_pct": c.gap_pct,
        "feasible": validate(w, c)["feasible"],
        "uplift_vs_greedy_x": round(
            ec["overdue_km_days_cleared"] /
            max(1e-9, eg["overdue_km_days_cleared"]), 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--time-limit", type=float, default=20.0)
    args = ap.parse_args()
    tl = 6.0 if args.quick else args.time_limit

    print(f"{'scenario':<20}{'trains/d':>9}{'corr h':>8}{'greedy':>8}"
          f"{'cpsat':>8}{'x':>6}{'blocks':>8}{'AAI':>9}{'punct%':>8}"
          f"{'burst':>7}  status")
    print("-" * 105)
    out = {}
    for sc in SCENARIOS:
        r = run_one(dict(sc["kw"]), tl)
        out[sc["name"]] = {**r, "why": sc["why"]}
        print(f"{sc['name']:<20}{r['trains_per_day']:>9}{r['corridor_hours']:>8}"
              f"{r['greedy_cleared']:>8.1f}{r['cpsat_cleared']:>8.1f}"
              f"{r['uplift_vs_greedy_x']:>6}{r['cpsat_blocks']:>8}"
              f"{r['cpsat_aai']:>9.4f}{r['pax_punctuality']:>8.2f}"
              f"{r['bursts']:>7}  {r['status']}"
              f"{'' if r['feasible'] else '  !! INFEASIBLE PLAN'}")

    base = out["baseline"]
    print("\nreading it:")
    for k in ("traffic +20%", "freight +50%", "one CSM down", "no corridor policy",
              "tolerance 4 h/day", "tolerance 12 h/day"):
        d = out[k]["cpsat_cleared"] - base["cpsat_cleared"]
        print(f"  {k:<22} {d:+7.1f} km-days vs baseline   ({out[k]['why']})")

    os.makedirs(OUT_DIR, exist_ok=True)
    p = os.path.join(OUT_DIR, "sensitivity.json")
    with open(p, "w") as f:
        json.dump(out, f, indent=1)
    print(f"\n  wrote {p}")


if __name__ == "__main__":
    main()

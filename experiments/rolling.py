#!/usr/bin/env python
"""Rolling multi-week horizon — where the compounding shows up.

    python -m experiments.rolling --weeks 12

One week of planning barely moves an index built on a section carrying a
multi-month maintenance backlog, and quoting a single week's Asset
Availability Index as the headline would be quietly misleading. The honest
version of "maximise asset availability" is a BACKLOG BURN-DOWN: plan a week,
execute it (bursts and all), age the asset by seven days, regenerate the
demand from what is now due, and do it again.

Run greedy and CP-SAT down the same road and the per-week difference
compounds. That is the number worth defending.

This is README B2 Level 3 (rolling weekly horizon) without the reinforcement
learning, which nobody should be building in 36 hours.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
from typing import Callable, List

from core.model import OUT_DIR
from core.pipeline import build_world
from assets.degradation import (_decay_per_day, predict_days_to_threshold,
                                train_degradation_model)
from assets.demand_generator import generate_jobs
from optimizer.cpsat import solve_cpsat
from optimizer.greedy import solve_greedy
from sim.kpis import due_days_map
from sim.simulator import simulate

DAYS = 7


def backlog(world) -> dict:
    """Weighted asset-km currently past due, and the worst of it."""
    cfg = world.cfg
    due = due_days_map(cfg, world.segments)
    wt = {a: s.get("availability_value_per_km", 1.0)
          for a, s in cfg["norms"]["activities"].items()}
    seg_len = {s.id: s.km_end - s.km_start for s in world.segments}
    overdue = sum(seg_len[sid] * wt.get(a, 1.0)
                  for (sid, a), d in due.items() if d <= 0)
    tgis = [s.tgi for s in world.segments]
    return {
        "overdue_weighted_km": round(overdue, 1),
        "mean_tgi": round(sum(tgis) / len(tgis), 2),
        "segments_below_threshold": sum(
            1 for t in tgis if t < cfg["norms"]["tgi"]["intervention_threshold"]),
        "segments_urgent": sum(
            1 for t in tgis if t < cfg["norms"]["tgi"]["urgent_threshold"]),
    }


def apply_completions(world, plan, completed: List[str]) -> float:
    """Book the work: reset the maintenance clock, and restore geometry."""
    by_id = {j.id: j for j in world.jobs}
    segs = {s.id: s for s in world.segments}
    reset = world.cfg["norms"]["tgi"]["initial_after_tamping"]
    km = 0.0
    for jid in completed:
        j = by_id.get(jid)
        if not j:
            continue
        km += j.availability_value
        for sid in j.segment_ids:
            s = segs.get(sid)
            if not s:
                continue
            s.last_done[j.activity] = 0
            if j.activity == "tamping":
                s.tgi = reset
            elif j.activity == "deep_screening":
                s.tgi = min(reset, s.tgi + 12.0)
    return km


def age_one_week(world) -> None:
    cfg = world.cfg
    tgi_cfg = cfg["norms"]["tgi"]
    floor = 20.0
    for s in world.segments:
        for a in list(s.last_done):
            s.last_done[a] += DAYS
        s.tgi = max(floor, s.tgi - _decay_per_day(s, tgi_cfg) * DAYS)
        s.cumulative_gmt += s.gmt_per_year * DAYS / 365.0


def run(planner: Callable, label: str, weeks: int, time_limit: float,
        seed: int = 7, corridor: bool = True) -> dict:
    world = build_world(max_jobs=200, seed=seed, corridor_blocks=corridor)
    model, _ = train_degradation_model(world.segments, world.cfg, seed=seed)
    hist = [{"week": 0, "done_this_week": 0.0, **backlog(world)}]

    for wk in range(1, weeks + 1):
        pred = predict_days_to_threshold(model, world.segments)
        jobs, _rep = generate_jobs(world.cfg, world.segments, world.stretches,
                                   pred, lookahead_days=14, max_jobs=200,
                                   seed=seed + 34 + wk)
        world.jobs = jobs
        plan = planner(world, time_limit)
        sim = simulate(world, plan, seed=200 + wk)
        done = apply_completions(world, plan, sim.completed_jobs)
        age_one_week(world)
        hist.append({"week": wk, "blocks": len(plan.blocks),
                     "done_this_week": round(done, 1), **backlog(world)})
        print(f"  {label:<8} week {wk:>2}: {len(plan.blocks):>2} blocks, "
              f"{done:>5.1f} avail-km done, backlog "
              f"{hist[-1]['overdue_weighted_km']:>7.1f}, mean TGI "
              f"{hist[-1]['mean_tgi']:>5.1f}, urgent "
              f"{hist[-1]['segments_urgent']:>3}")
    return {"label": label, "history": hist}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--weeks", type=int, default=12)
    ap.add_argument("--time-limit", type=float, default=10.0)
    args = ap.parse_args()

    print(f"rolling {args.weeks} weeks, {args.time_limit}s solver budget/week\n")
    # Three arms, so the corridor POLICY and the SOLVER are separated. Most of
    # the gain on this section comes from the policy; saying so is the point of
    # running the ablation rather than one flattering pair.
    res = [
        run(lambda w, t: solve_greedy(w), "manual/no-corr", args.weeks,
            args.time_limit, corridor=False),
        run(lambda w, t: solve_greedy(w), "manual+corr", args.weeks,
            args.time_limit, corridor=True),
        run(lambda w, t: solve_cpsat(w, time_limit=t, hint_plan=solve_greedy(w)),
            "cpsat+corr", args.weeks, args.time_limit, corridor=True),
    ]

    labels = [r["label"] for r in res]
    hs = [r["history"] for r in res]
    print("\nweek-by-week backlog (weighted asset-km past due)")
    print(f"{'week':>5}" + "".join(f"{l:>15}" for l in labels) +
          "".join(f"{l + ' TGI':>17}" for l in labels))
    for i in range(len(hs[0])):
        print(f"{hs[0][i]['week']:>5}" +
              "".join(f"{h[i]['overdue_weighted_km']:>15.1f}" for h in hs) +
              "".join(f"{h[i]['mean_tgi']:>17.1f}" for h in hs))

    print(f"\n  availability-km delivered over {args.weeks} weeks")
    base = sum(h["done_this_week"] for h in hs[0])
    for l, h in zip(labels, hs):
        t = sum(x["done_this_week"] for x in h)
        print(f"    {l:<16}{t:>8.1f}   ({t / max(1e-9, base):.2f}x baseline)")
    print(f"\n  urgent segments (TGI < 55) at week {args.weeks}")
    for l, h in zip(labels, hs):
        print(f"    {l:<16}{h[-1]['segments_urgent']:>8}"
              f"   (week 0: {h[0]['segments_urgent']})")
    print(f"\n  mean TGI at week {args.weeks}")
    for l, h in zip(labels, hs):
        print(f"    {l:<16}{h[-1]['mean_tgi']:>8.1f}"
              f"   (week 0: {h[0]['mean_tgi']:.1f})")

    os.makedirs(OUT_DIR, exist_ok=True)
    p = os.path.join(OUT_DIR, "rolling.json")
    with open(p, "w") as f:
        json.dump(res, f, indent=1)
    print(f"\n  wrote {p}")


if __name__ == "__main__":
    main()

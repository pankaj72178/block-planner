"""Level 2 -- bounded retiming, aimed at the paths that are actually in the way.

The naive version of this ("let the solver move any goods train") is a trap:
168 freight paths x a 60-minute shift window is an enormous search space, and
CP-SAT spends the whole time budget proving small bounds instead of finding
the one move that matters.

So we ask a cheaper question first, in plain Python:

    Which windows are big enough for this job once passenger traffic is
    accounted for, but are chopped into unusable pieces by a small number of
    goods paths?

Those goods paths -- and only those -- become variables. This is also how the
result gets explained to a control office, which does not want to hear about
a MIP: *"shift these two goods trains by 14 and 21 minutes and you have a
3-hour window at Bharuch-Ankleshwar on Wednesday."*
"""
from __future__ import annotations

import copy
from typing import Dict, List, Set

from core.model import Occupancy, Plan
from optimizer.cpsat import RETIME_POLICY, score_plan, solve_cpsat


def _merge(ivs: List[tuple]) -> List[List[int]]:
    out: List[List[int]] = []
    for a, b in sorted(ivs):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _free_windows(busy: List[tuple], horizon: int) -> List[tuple]:
    free, cur = [], 0
    for a, b in _merge(busy):
        if a > cur:
            free.append((cur, a))
        cur = max(cur, b)
    if horizon > cur:
        free.append((cur, horizon))
    return free


def find_blocking_freight(world, plan: Plan, *, movable_classes=("FREIGHT",),
                          max_trains: int = 40, top_jobs: int = 60) -> dict:
    """Identify goods paths whose removal would open a window for a real job."""
    buf = world.cfg["planning"]["block_buffer_min"]
    H = world.horizon

    movable_uids = {t.uid for t in world.trains if t.ttype in movable_classes}
    fixed_by_stretch: Dict[str, List[tuple]] = {}
    freight_by_stretch: Dict[str, List[tuple]] = {}
    for t in world.trains:
        tgt = freight_by_stretch if t.uid in movable_uids else fixed_by_stretch
        for o in t.occupancies:
            tgt.setdefault(o.stretch_id, []).append((o.t_in, o.t_out, t.uid))

    scheduled = {b.job_id for b in plan.blocks}
    pending = sorted((j for j in world.jobs if j.id not in scheduled),
                     key=lambda j: -j.priority)[:top_jobs]

    votes: Dict[str, float] = {}
    evidence: List[dict] = []

    for j in pending:
        size = j.duration_min + 2 * buf
        fixed = [(a, b) for a, b, _ in fixed_by_stretch.get(j.stretch_id, [])]
        for w0, w1 in _free_windows(fixed, H):
            if w1 - w0 < size:
                continue                       # even an empty railway won't fit it
            inside = [(a, b, u) for a, b, u in
                      freight_by_stretch.get(j.stretch_id, [])
                      if a < w1 and w0 < b]
            if not inside:
                continue                       # already usable; solver has it
            sub = _free_windows([(max(a, w0), min(b, w1)) for a, b, _ in inside],
                                w1)
            sub = [(max(a, w0), b) for a, b in sub if b > w0]
            biggest = max((b - a for a, b in sub), default=0)
            if biggest >= size:
                continue                       # a usable piece already exists
            if sum(b - a for a, b in sub) < size:
                continue                       # not enough free minutes anyway
            # This window is big enough in total but fragmented by `inside`.
            weight = j.priority / max(1, len(inside))
            for _, _, u in inside:
                votes[u] = votes.get(u, 0.0) + weight
            evidence.append({
                "job_id": j.id, "activity": j.activity, "priority": j.priority,
                "stretch": j.stretch_id, "needs_min": size,
                "window": [w0, w1], "largest_free_piece_min": biggest,
                "blocking_trains": sorted({u for _, _, u in inside}),
            })

    ranked = sorted(votes.items(), key=lambda kv: -kv[1])[:max_trains]
    return {
        "candidate_uids": {u for u, _ in ranked},
        "ranked": ranked,
        "evidence": evidence[:40],
        "jobs_examined": len(pending),
        "jobs_unlockable_in_principle": len({e["job_id"] for e in evidence}),
    }


def solve_with_retiming(world, base_plan: Plan, *, time_limit: float = 60.0,
                        max_retimed: int = 8, max_candidates: int = 40,
                        workers: int = 8) -> tuple[Plan, dict]:
    diag = find_blocking_freight(world, base_plan, max_trains=max_candidates)
    uids: Set[str] = diag["candidate_uids"]
    if not uids:
        keep = copy.copy(base_plan)
        keep.name = "CP-SAT + retiming (no candidates)"
        return keep, diag

    plan = solve_cpsat(world, time_limit=time_limit, allow_retiming=True,
                       retime_uids=uids, max_retimed=max_retimed,
                       hint_plan=base_plan, workers=workers)
    diag["candidates_offered"] = len(uids)

    # Level 2 solves a strictly larger problem in the same wall-clock budget,
    # so it can time out on a WORSE incumbent than Level 1. Never hand back a
    # regression: if freeing the goods paths did not actually buy anything,
    # say so and keep the Level 1 plan.
    # Two conditions, not one. The objective prices urgent segments heavily
    # (tail risk), so a plan can score higher while recovering fewer weighted
    # asset-km-days -- which reads as a regression in the results table even
    # though the solver did what it was told. Require both to improve.
    def _avail(p):
        return sum(world.job(b.job_id).availability_value for b in p.blocks
                   if any(j.id == b.job_id for j in world.jobs))

    if (score_plan(world, plan) <= score_plan(world, base_plan)
            or _avail(plan) < _avail(base_plan)):
        diag["retiming_helped"] = False
        # copy, don't rename in place -- base_plan is the Level 1 plan the
        # caller still holds and is about to display in its own column
        keep = copy.copy(base_plan)
        keep.name = "CP-SAT + retiming (no gain)"
        return keep, diag
    diag["retiming_helped"] = True
    return plan, diag

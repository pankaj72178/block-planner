"""Independent feasibility check for any plan, from any planner.

Written deliberately as a SECOND implementation of the rules -- it re-derives
every constraint from the plan and the world rather than reusing the model, so
a bug in the CP-SAT encoding cannot hide behind the same bug in the checker.
Run it on every plan in the results table: a plan that is not feasible is not
a result, and "our optimiser said so" is not a verification.
"""
from __future__ import annotations

from typing import Dict, List

from core.model import MIN_PER_DAY, Plan


def validate(world, plan: Plan) -> dict:
    cfg = world.cfg
    buf = cfg["planning"]["block_buffer_min"]
    cap_min = int(cfg["planning"]["max_block_hours_per_day"] * 60)
    d_lo, d_hi = cfg["planning"]["daylight_window"]
    units = {u.id: u for u in world.machines}
    jobs = {j.id: j for j in world.jobs}
    v: List[str] = []

    shift = plan.retimings or {}
    busy: Dict[str, List[tuple]] = {}
    for t in world.trains:
        s = shift.get(t.uid, 0)
        for o in t.occupancies:
            busy.setdefault(o.stretch_id, []).append((o.t_in + s, o.t_out + s))

    # 1. a block must not overlap any train on its stretch
    for b in plan.blocks:
        for a, z in busy.get(b.stretch_id, ()):
            if a < b.end and b.start < z:
                v.append(f"{b.job_id}: overlaps a train on {b.stretch_id} "
                         f"({b.start}-{b.end} vs {a}-{z})")
    # 2. blocks must not overlap each other on the same stretch
    per: Dict[str, list] = {}
    for b in plan.blocks:
        per.setdefault(b.stretch_id, []).append(b)
    for sid, bs in per.items():
        bs.sort(key=lambda b: b.start)
        for x, y in zip(bs, bs[1:]):
            if y.start < x.end:
                v.append(f"{x.job_id}/{y.job_id}: overlapping blocks on {sid}")
    # 3. one machine unit, one job at a time, plus repositioning
    pm: Dict[str, list] = {}
    for b in plan.blocks:
        if b.machine_unit not in units:
            v.append(f"{b.job_id}: unknown machine {b.machine_unit}")
            continue
        if not units[b.machine_unit].available:
            v.append(f"{b.job_id}: uses machine {b.machine_unit}, which is out")
        pm.setdefault(b.machine_unit, []).append(b)
    for uid, bs in pm.items():
        r = units[uid].reposition_buffer_min
        bs.sort(key=lambda b: b.start)
        for x, y in zip(bs, bs[1:]):
            if y.start < x.end + r:
                v.append(f"{x.job_id}/{y.job_id}: {uid} has {y.start - x.end} min "
                         f"between jobs, needs {r}")
    # 4. right machine type, right duration
    for b in plan.blocks:
        j = jobs.get(b.job_id)
        if j is None:
            v.append(f"{b.job_id}: not a job in this world")
            continue
        if j.machine_type != b.machine_type:
            v.append(f"{b.job_id}: {b.machine_type} cannot do {j.activity}")
        if b.duration != j.duration_min + 2 * buf:
            v.append(f"{b.job_id}: duration {b.duration} != "
                     f"{j.duration_min} + 2x{buf} buffer")
    # 5. daylight-only activities
    for b in plan.blocks:
        j = jobs.get(b.job_id)
        if j and j.daylight_only:
            d0, d1 = b.start // MIN_PER_DAY, (b.end - 1) // MIN_PER_DAY
            off = b.start % MIN_PER_DAY
            if d0 != d1 or off < d_lo * 60 or off + b.duration > d_hi * 60:
                v.append(f"{b.job_id}: {j.activity} is daylight-only but runs "
                         f"{b.start % MIN_PER_DAY}-{b.end % MIN_PER_DAY}")
    # 6. operations tolerance per day
    per_day: Dict[int, int] = {}
    for b in plan.blocks:
        per_day[b.start // MIN_PER_DAY] = \
            per_day.get(b.start // MIN_PER_DAY, 0) + b.duration
    for d, mins in sorted(per_day.items()):
        if mins > cap_min:
            v.append(f"day {d + 1}: {mins} block-min granted, tolerance is {cap_min}")
    # 7. one block per job
    seen = set()
    for b in plan.blocks:
        if b.job_id in seen:
            v.append(f"{b.job_id}: scheduled more than once")
        seen.add(b.job_id)

    return {"feasible": not v, "violations": v[:40], "n_violations": len(v),
            "blocks": len(plan.blocks),
            "day_load_min": {d + 1: m for d, m in sorted(per_day.items())}}

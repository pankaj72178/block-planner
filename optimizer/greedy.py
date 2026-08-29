"""Greedy baseline -- 'current manual practice'.

This is the planner you are beating, and it must stay in every results table.
A single optimised number with nothing to compare it to says nothing.

What it does is what a section engineer does with a highlighter and the working
timetable: take the most urgent job, find the first natural gap on that stretch
big enough to hold it, put it there, move on. No look-ahead, no reconsidering,
no trading a big job against two small ones.
"""
from __future__ import annotations

from typing import Dict, List

from core.model import Job, MachineUnit, Occupancy, Plan, PlannedBlock

MIN_PER_DAY = 1440


def _busy_windows(job: Job, occ: Dict[str, List[Occupancy]],
                  placed: List[PlannedBlock]) -> List[tuple]:
    w = [(o.t_in, o.t_out) for o in occ.get(job.stretch_id, [])]
    w += [(b.start, b.end) for b in placed if b.stretch_id == job.stretch_id]
    return sorted(w)


def _machine_windows(unit: MachineUnit, placed: List[PlannedBlock]) -> List[tuple]:
    r = unit.reposition_buffer_min
    return sorted((b.start - r, b.end + r)
                  for b in placed if b.machine_unit == unit.id)


def _fits(t: int, dur: int, windows: List[tuple]) -> bool:
    return not any(a < t + dur and t < b for a, b in windows)


def _daylight_ok(t: int, dur: int, cfg: dict) -> bool:
    lo, hi = cfg["planning"]["daylight_window"]
    d0, d1 = t // MIN_PER_DAY, (t + dur) // MIN_PER_DAY
    if d0 != d1:
        return False
    off = t % MIN_PER_DAY
    return lo * 60 <= off and off + dur <= hi * 60


def solve_greedy(world, time_limit: float = 0.0) -> Plan:
    cfg = world.cfg
    horizon = world.horizon
    buf = cfg["planning"]["block_buffer_min"]
    cap_min = int(cfg["planning"]["max_block_hours_per_day"] * 60)
    step = 5                                   # planners work on 5-minute marks

    units = [m for m in world.machines if m.available]
    by_type: Dict[str, List[MachineUnit]] = {}
    for u in units:
        by_type.setdefault(u.mtype, []).append(u)

    placed: List[PlannedBlock] = []
    unscheduled: List[str] = []
    day_used = [0] * cfg["planning"]["horizon_days"]

    # A real section engineer works to the same divisional outturn targets, so
    # the baseline gets them too -- otherwise CP-SAT would "win" purely by
    # being held to a rule the baseline was allowed to ignore. Quota first, in
    # priority order within each machine type, then fill on global priority.
    targets = cfg["norms"].get("outturn_targets", {})
    quota = {t: n * len(by_type.get(t, [])) for t, n in targets.items() if n}
    by_prio = sorted(world.jobs,
                     key=lambda j: (not j.urgent, -j.priority, j.due_min))
    first, rest, taken = [], [], {t: 0 for t in quota}
    for j in by_prio:
        if taken.get(j.machine_type, 0) < quota.get(j.machine_type, 0):
            taken[j.machine_type] = taken.get(j.machine_type, 0) + 1
            first.append(j)
        else:
            rest.append(j)
    order = first + rest

    for job in order:
        size = job.duration_min + 2 * buf
        cand_units = by_type.get(job.machine_type, [])
        if not cand_units:
            unscheduled.append(job.id)
            continue

        track_busy = _busy_windows(job, world.occupancy, placed)
        done = False
        for t in range(0, horizon - size + 1, step):
            day = t // MIN_PER_DAY
            if day_used[day] + size > cap_min:
                continue
            if job.daylight_only and not _daylight_ok(t, size, cfg):
                continue
            if not _fits(t, size, track_busy):
                continue
            for u in cand_units:
                if _fits(t, size, _machine_windows(u, placed)):
                    placed.append(PlannedBlock(
                        job_id=job.id, activity=job.activity, label=job.label,
                        stretch_id=job.stretch_id, line=job.line,
                        km_start=job.km_start, km_end=job.km_end,
                        machine_unit=u.id, machine_type=u.mtype,
                        start=t, end=t + size, priority=job.priority))
                    day_used[day] += size
                    done = True
                    break
            if done:
                break
        if not done:
            unscheduled.append(job.id)

    return Plan(name="Greedy (manual practice)", blocks=placed,
                unscheduled=unscheduled, solver_status="HEURISTIC")

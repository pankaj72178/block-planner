"""The core deliverable: block planning as constraint optimisation (CP-SAT).

Formulation
-----------
Decision   For each maintenance job: do it this week? when? on which machine?
           (Level 2) and may we nudge a small number of freight paths to open
           a window that does not otherwise exist?

Objective  maximise  SUM(priority x scheduled)  -  retiming penalty

Constraints
  1. A block must not overlap any train on the same line stretch. Train
     intervals are already padded by half the sectional headway, and every
     block carries an additional safety buffer either side.
  2. One machine unit does one job at a time, plus a repositioning buffer
     before it can be anywhere else.
  3. Daylight-only activities (USFD, OHE, destressing, P&C) must sit inside
     the daylight window and may not straddle midnight.
  4. Section-wide cap on block minutes granted per day -- operations tolerance.
  5. Blocks the planner has locked are fixed; everything else replans around
     them. That is the human-in-the-loop path, and it is a re-solve, not a
     patch.

Level 2 (retiming) is the part a control office recognises. Train paths become
variables with a bounded shift and a per-class penalty: freight moves cheaply,
express costs, premium trains do not move at all. Because every train interval
-- shifted or fixed -- sits in the same per-stretch NoOverlap, the retimed
result is still a conflict-free timetable, not a plan that assumed trains
would evaporate.
"""
from __future__ import annotations

import time
from typing import Dict, List

from ortools.sat.python import cp_model

from core.model import Job, MachineUnit, Occupancy, Plan, PlannedBlock

MIN_PER_DAY = 1440
# The objective IS the headline KPI. Availability -- weighted asset-km-days
# recovered, exactly as sim/kpis.asset_availability measures it -- dominates,
# and priority enters only as an urgency tiebreak between comparable jobs.
#
# Earlier versions had priority dominant and availability as a nudge. That
# optimiser proved OPTIMAL and still lost to the greedy baseline on the metric
# the report printed, which is the correct behaviour of a solver pointed at the
# wrong function and the most expensive kind of bug to find late.
PRIORITY_SCALE = 20           # urgency tiebreak

# Doing a job on Monday buys six more days of availability than doing the same
# job on Saturday, and a job covering 6 km buys six times the availability of
# one covering 1 km. Without this term the objective is indifferent to both,
# the solver spreads work across the week to satisfy the daily cap, and the
# Asset Availability Index -- measured in km-DAYS -- comes out no better than
# the greedy baseline even though more work got done.
#
# The term is deliberately secondary: priority still decides WHAT gets done,
# availability decides WHEN and breaks ties between comparable jobs.
AVAILABILITY_WEIGHT = 100     # points per weighted asset-km-day recovered

# A segment below the URGENT track-geometry threshold is a speed-restriction
# waiting to happen, and a speed restriction costs far more availability than
# the block that would have prevented it. Priced as a large bonus rather than
# a hard constraint so an impossible week degrades gracefully instead of
# returning INFEASIBLE.
#
# This exists because of a measured failure: without it the optimiser posted a
# higher mean TGI than the greedy baseline over a 10-week rolling run while
# leaving five segments in the urgent band that greedy had cleared. Optimising
# the mean is not the same as managing the tail.
URGENT_BONUS = 1200

# Bounded shift (minutes) and penalty weight per train class.
# SUPERFAST is deliberately immovable: nobody re-paths a Rajdhani to tamp track.
# 90 minutes for a goods train is not a fudge: our own freight paths are
# already held up to 75 minutes in loops for passenger overtakes, and
# regulating goods at the previous loop is exactly how a control office frees
# a corridor window in practice. Passenger shifts stay small and expensive.
RETIME_POLICY = {
    "FREIGHT":   {"max_shift": 90, "weight": 1,  "fixed_cost": 15},
    "PASSENGER": {"max_shift": 10, "weight": 12, "fixed_cost": 120},
    "EXPRESS":   {"max_shift": 15, "weight": 8,  "fixed_cost": 90},
    "SUPERFAST": {"max_shift": 0,  "weight": 0,  "fixed_cost": 0},
}


def free_start_domain(busy: List[Occupancy], size: int, horizon: int,
                      daylight: tuple | None, n_days: int) -> cp_model.Domain:
    """All start times at which a block of `size` minutes fits on this stretch.

    This replaces putting thousands of fixed train intervals into NoOverlap.
    A train whose path we are NOT allowed to move is not really a scheduling
    decision -- it is a hole in the calendar. Encoding it as a hole (one
    domain constraint per job) instead of an interval (one NoOverlap member
    per train per stretch) is what takes this model from tens of thousands of
    interval variables to a few hundred, and from minutes to seconds.
    """
    merged: List[List[int]] = []
    for o in sorted(busy, key=lambda x: x.t_in):
        if merged and o.t_in <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], o.t_out)
        else:
            merged.append([o.t_in, o.t_out])

    free: List[tuple] = []
    cursor = 0
    for a, b in merged:
        if a - cursor >= size:
            free.append((cursor, a - size))       # latest start still fitting
        cursor = max(cursor, b)
    if horizon - cursor >= size:
        free.append((cursor, horizon - size))

    if daylight is not None:
        lo, hi = daylight
        allowed = []
        for d in range(n_days):
            a = d * MIN_PER_DAY + lo * 60
            b = d * MIN_PER_DAY + hi * 60 - size
            if b >= a:
                allowed.append((a, b))
        free = _intersect(free, allowed)

    if not free:
        return cp_model.Domain.FromIntervals([])
    flat: List[int] = []
    for a, b in free:
        flat.extend([int(a), int(b)])
    return cp_model.Domain.FromFlatIntervals(flat)


def feasible_offsets(tp, fixed_busy: Dict[str, List[Occupancy]],
                     lim: int) -> cp_model.Domain:
    """Shifts of this train that still clear all the traffic we may NOT move.

    Same idea as `free_start_domain`, applied to a train instead of a block:
    immovable traffic is a property of the calendar, not a decision, so it
    belongs in a variable's domain rather than in NoOverlap. Offset 0 is always
    feasible -- the base timetable is conflict-free -- so this can never make
    the model infeasible.
    """
    ok: List[int] = []
    for off in range(-lim, lim + 1):
        good = True
        for o in tp.occupancies:
            a, b = o.t_in + off, o.t_out + off
            for iv in fixed_busy.get(o.stretch_id, ()):
                if iv.t_in < b and a < iv.t_out:
                    good = False
                    break
            if not good:
                break
        if good:
            ok.append(off)
    return cp_model.Domain.FromValues(ok or [0])


def _intersect(xs: List[tuple], ys: List[tuple]) -> List[tuple]:
    out = []
    for a, b in xs:
        for c, d in ys:
            lo, hi = max(a, c), min(b, d)
            if lo <= hi:
                out.append((lo, hi))
    return sorted(out)


def score_plan(world, plan: Plan) -> float:
    """Score a plan under the CP-SAT objective, from outside the model.

    Used to guarantee the solver never ships something worse than the greedy
    plan it started from. The hint is always feasible, so a lower-scoring
    answer is a time-limit artifact, not a better decision -- and on a hard
    instance (a goods surge, say) that artifact would otherwise show up in the
    results table as the optimiser losing to the baseline.
    """
    nd = world.cfg["planning"]["horizon_days"]
    total = 0.0
    for b in plan.blocks:
        try:
            j = world.job(b.job_id)
        except StopIteration:
            continue
        total += round(j.priority * PRIORITY_SCALE)
        total += URGENT_BONUS if j.urgent else 0
        total += round(AVAILABILITY_WEIGHT * j.availability_value
                       * (nd - b.start // MIN_PER_DAY))
    total -= sum(RETIME_POLICY.get(
        "FREIGHT", {}).get("weight", 1) * abs(v)
        for v in (plan.retimings or {}).values())
    return total


def _outturn_floor(cfg: dict, jobs, units_by_type) -> Dict[str, int]:
    """Minimum blocks per machine type: divisional target, capped by reality."""
    targets = cfg["norms"].get("outturn_targets", {})
    avail: Dict[str, int] = {}
    for j in jobs:
        avail[j.machine_type] = avail.get(j.machine_type, 0) + 1
    out = {}
    for mtype, per_unit in targets.items():
        n = per_unit * len(units_by_type.get(mtype, []))
        n = min(n, avail.get(mtype, 0))
        if n > 0:
            out[mtype] = n
    return out


def solve_cpsat(world, *, time_limit: float = 60.0, allow_retiming: bool = False,
                retime_classes: tuple = ("FREIGHT",), max_retimed: int = 12,
                retime_uids: set | None = None, outturn: bool = True,
                locked: List[dict] | None = None, hint_plan: Plan | None = None,
                workers: int = 8, log: bool = False) -> Plan:
    cfg = world.cfg
    H = world.horizon
    buf = cfg["planning"]["block_buffer_min"]
    cap_min = int(cfg["planning"]["max_block_hours_per_day"] * 60)
    n_days = cfg["planning"]["horizon_days"]
    day_lo, day_hi = cfg["planning"]["daylight_window"]
    locked = locked or []
    locked_by_job = {l["job_id"]: l for l in locked}

    m = cp_model.CpModel()
    units = [u for u in world.machines if u.available]
    units_by_type: Dict[str, List[MachineUnit]] = {}
    for u in units:
        units_by_type.setdefault(u.mtype, []).append(u)

    jobs = [j for j in world.jobs if j.machine_type in units_by_type]
    skipped_no_machine = [j.id for j in world.jobs
                          if j.machine_type not in units_by_type]

    # Split the traffic: trains we may nudge become variables, trains we may
    # not become holes in each stretch's calendar.
    retime_set = set(retime_classes) if allow_retiming else set()
    movable = [t for t in world.trains
               if t.ttype in retime_set and RETIME_POLICY[t.ttype]["max_shift"] > 0]
    if retime_uids is not None:
        # Targeted retiming: only the specific paths that were identified as
        # fragmenting a usable window. Freeing all 168 goods paths makes the
        # search space enormous and the solver finds nothing useful inside a
        # demo-length time budget; freeing the six that matter finds it fast.
        movable = [t for t in movable if t.uid in retime_uids]
    movable_uids = {t.uid for t in movable}
    fixed_busy: Dict[str, List[Occupancy]] = {}
    for t in world.trains:
        if t.uid in movable_uids:
            continue
        for o in t.occupancies:
            fixed_busy.setdefault(o.stretch_id, []).append(o)

    start, present, track_iv = {}, {}, {}
    assign: Dict[tuple, cp_model.IntVar] = {}
    dayb: Dict[str, List[cp_model.IntVar]] = {}
    size_of: Dict[str, int] = {}
    per_stretch: Dict[str, list] = {}
    per_unit: Dict[str, list] = {u.id: [] for u in units}
    no_window: List[str] = []

    # ---------------- jobs ------------------------------------------------
    schedulable = []
    for j in jobs:
        size = j.duration_min + 2 * buf
        size_of[j.id] = size
        dom = free_start_domain(
            fixed_busy.get(j.stretch_id, []), size, H,
            (day_lo, day_hi) if j.daylight_only else None, n_days)
        if dom.is_empty() and not movable:
            # no window exists anywhere this week even with an empty machine
            # fleet -- record it instead of letting the solver quietly drop it
            no_window.append(j.id)
            continue
        schedulable.append(j)
        if dom.is_empty():
            s = m.NewIntVar(0, max(0, H - size), f"s_{j.id}")
        else:
            s = m.NewIntVarFromDomain(dom, f"s_{j.id}")
        p = m.NewBoolVar(f"p_{j.id}")
        start[j.id], present[j.id] = s, p
        track_iv[j.id] = m.NewOptionalIntervalVar(s, size, s + size, p, f"t_{j.id}")
        per_stretch.setdefault(j.stretch_id, []).append(track_iv[j.id])

        # machine assignment: exactly one unit iff the job is scheduled
        opts = []
        for u in units_by_type[j.machine_type]:
            a = m.NewBoolVar(f"a_{j.id}_{u.id}")
            assign[(j.id, u.id)] = a
            msize = size + u.reposition_buffer_min
            per_unit[u.id].append(
                m.NewOptionalIntervalVar(s, msize, s + msize, a, f"m_{j.id}_{u.id}")
            )
            opts.append(a)
        m.Add(sum(opts) == p)

        # day placement -- needed for both the daily cap and daylight rules
        bs = [m.NewBoolVar(f"d_{j.id}_{d}") for d in range(n_days)]
        dayb[j.id] = bs
        m.AddExactlyOne(bs)
        for d, b in enumerate(bs):
            m.Add(s >= d * MIN_PER_DAY).OnlyEnforceIf(b)
            m.Add(s < (d + 1) * MIN_PER_DAY).OnlyEnforceIf(b)
            # daylight is already baked into the start domain above

    jobs = schedulable
    unscheduled_prefix = skipped_no_machine + no_window

    # ---------------- daily block-hour cap + earliness --------------------
    earliness_terms = []
    for d in range(n_days):
        terms = []
        for j in jobs:
            used = m.NewBoolVar(f"u_{j.id}_{d}")
            m.Add(used <= dayb[j.id][d])
            m.Add(used <= present[j.id])
            m.Add(used >= dayb[j.id][d] + present[j.id] - 1)
            terms.append(size_of[j.id] * used)
            gain = int(round(AVAILABILITY_WEIGHT * j.availability_value
                             * (n_days - d)))
            if gain:
                earliness_terms.append(gain * used)
        m.Add(sum(terms) <= cap_min)

    # ---------------- trains ----------------------------------------------
    offsets: Dict[str, cp_model.IntVar] = {}
    moved_bools: List[cp_model.IntVar] = []
    penalty_terms = []

    for tp in movable:
        pol = RETIME_POLICY[tp.ttype]
        lim = pol["max_shift"]
        off = m.NewIntVarFromDomain(
            feasible_offsets(tp, fixed_busy, lim), f"off_{tp.uid}")
        aoff = m.NewIntVar(0, lim, f"aoff_{tp.uid}")
        m.AddAbsEquality(aoff, off)
        mv = m.NewBoolVar(f"mv_{tp.uid}")
        m.Add(aoff <= lim * mv)
        m.Add(aoff >= mv)
        offsets[tp.uid] = off
        moved_bools.append(mv)
        penalty_terms.append(pol["weight"] * aoff)
        penalty_terms.append(pol["fixed_cost"] * mv)

        for o in tp.occupancies:
            dur = o.t_out - o.t_in
            # Bounds must be exactly [t_in - lim, t_in + lim]. Clamping them
            # to [0, H] looks harmless but silently contradicts the shared
            # per-train offset for any path near either end of the horizon,
            # and the whole model goes INFEASIBLE in presolve.
            s2 = m.NewIntVar(o.t_in - lim, o.t_in + lim,
                             f"ts_{tp.uid}_{o.stretch_id}")
            m.Add(s2 == o.t_in + off)
            per_stretch.setdefault(o.stretch_id, []).append(
                m.NewIntervalVar(s2, dur, s2 + dur, f"tv_{tp.uid}_{o.stretch_id}")
            )

    if moved_bools:
        m.Add(sum(moved_bools) <= max_retimed)

    # ---------------- the heart of the model ------------------------------
    for sid, ivs in per_stretch.items():
        if len(ivs) > 1:
            m.AddNoOverlap(ivs)
    for uid, ivs in per_unit.items():
        if len(ivs) > 1:
            m.AddNoOverlap(ivs)

    # ---------------- machine outturn targets -----------------------------
    # Without these the optimiser spends the entire week on whichever activity
    # buys the most availability per block-hour and delivers a plan that never
    # tamps anything. Divisions are measured on machine outturn, so the plan
    # has to deliver it. If the targets cannot be met the caller retries
    # without them and says so, rather than returning INFEASIBLE.
    floors = _outturn_floor(cfg, jobs, units_by_type) if outturn else {}
    for mtype, n in floors.items():
        m.Add(sum(present[j.id] for j in jobs if j.machine_type == mtype) >= n)

    # ---------------- locked blocks (human in the loop) -------------------
    for l in locked:
        jid = l["job_id"]
        if jid not in present:
            continue
        m.Add(present[jid] == 1)
        m.Add(start[jid] == int(l["start"]))
        if l.get("machine_unit") and (jid, l["machine_unit"]) in assign:
            m.Add(assign[(jid, l["machine_unit"])] == 1)

    # ---------------- objective -------------------------------------------
    value = sum((int(round(j.priority * PRIORITY_SCALE))
                 + (URGENT_BONUS if j.urgent else 0)) * present[j.id]
                for j in jobs)
    m.Maximize(value + sum(earliness_terms) - sum(penalty_terms))

    # warm start from the greedy plan -- same feasible region, faster ramp
    if hint_plan is not None:
        hinted = set()
        for b in hint_plan.blocks:
            if b.job_id in present and b.job_id not in locked_by_job:
                m.AddHint(present[b.job_id], 1)
                m.AddHint(start[b.job_id], b.start)
                hinted.add(b.job_id)
        for j in jobs:
            if j.id not in hinted and j.id not in locked_by_job:
                m.AddHint(present[j.id], 0)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit)
    solver.parameters.num_search_workers = workers
    solver.parameters.log_search_progress = log
    t0 = time.time()
    status = solver.Solve(m)
    elapsed = time.time() - t0

    name = "CP-SAT + retiming" if allow_retiming else "CP-SAT"
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        if outturn and floors:
            # the outturn targets are unreachable this week -- relax them and
            # report the fact rather than handing back an empty plan
            relaxed = solve_cpsat(
                world, time_limit=time_limit, allow_retiming=allow_retiming,
                retime_classes=retime_classes, max_retimed=max_retimed,
                retime_uids=retime_uids, outturn=False, locked=locked,
                hint_plan=hint_plan, workers=workers, log=log)
            relaxed.outturn_relaxed = True
            relaxed.outturn_targets = floors
            return relaxed
        return Plan(name=name, blocks=[], unscheduled=[j.id for j in world.jobs],
                    solver_status=solver.StatusName(status),
                    solve_seconds=elapsed)

    blocks, unscheduled = [], list(unscheduled_prefix)
    for j in jobs:
        if not solver.Value(present[j.id]):
            unscheduled.append(j.id)
            continue
        unit = next(u.id for u in units_by_type[j.machine_type]
                    if solver.Value(assign[(j.id, u.id)]))
        s = solver.Value(start[j.id])
        blocks.append(PlannedBlock(
            job_id=j.id, activity=j.activity, label=j.label,
            stretch_id=j.stretch_id, line=j.line, km_start=j.km_start,
            km_end=j.km_end, machine_unit=unit, machine_type=j.machine_type,
            start=s, end=s + size_of[j.id], priority=j.priority,
            locked=j.id in locked_by_job))
    blocks.sort(key=lambda b: b.start)

    retimings = {uid: solver.Value(v) for uid, v in offsets.items()
                 if solver.Value(v) != 0}
    plan = Plan(name=name, blocks=blocks, unscheduled=unscheduled,
                retimings=retimings, solver_status=solver.StatusName(status),
                solve_seconds=elapsed)
    plan.objective = solver.ObjectiveValue()
    plan.best_bound = solver.BestObjectiveBound()
    plan.no_window = no_window
    plan.outturn_targets = floors
    plan.outturn_relaxed = False
    plan.fell_back_to_hint = False

    # Never ship worse than where we started. The hint is a feasible point in
    # this exact model, so if the incumbent scores below it the time limit bit.
    if hint_plan is not None and hint_plan.blocks and \
            score_plan(world, plan) < score_plan(world, hint_plan):
        import copy as _copy
        fallback = _copy.copy(hint_plan)
        fallback.name = name
        fallback.solver_status = f"{solver.StatusName(status)} (kept hint)"
        fallback.solve_seconds = elapsed
        fallback.objective = plan.objective
        fallback.best_bound = plan.best_bound
        fallback.no_window = no_window
        fallback.outturn_targets = floors
        fallback.outturn_relaxed = False
        fallback.fell_back_to_hint = True
        return fallback
    return plan

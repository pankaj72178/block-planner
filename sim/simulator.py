"""Discrete-event replay: does the plan survive contact with the railway?

An optimiser will happily hand you a plan that is feasible on paper. The
simulator is what turns that into a claim you can defend, because it replays
the week with the two things the optimiser assumed away:

  * BLOCK BURSTS -- work overruns its window. This is the block planner's
    nightmare and the reason buffers exist. Overrun probability rises with job
    duration and with how tight the granted window was.
  * KNOCK-ON DELAY -- a train that meets an overrunning block is held, it can
    only be held where there is a loop, and everything behind it inherits that.

Trains are replayed in scheduled order. When a train cannot proceed, the hold
is pushed back to the last station that actually HAS a loop -- holding a train
on a running line between two block stations is not a thing you can do.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Dict, List

from core.model import Plan, Station, Stretch, TrainPath

PUNCTUALITY_THRESHOLD_MIN = 15      # IR counts a train late beyond 15 min


@dataclass
class SimResult:
    delays: Dict[str, int] = field(default_factory=dict)      # train uid -> min
    holds: int = 0
    hold_minutes: int = 0
    bursts: List[dict] = field(default_factory=list)
    completed_jobs: List[str] = field(default_factory=list)
    abandoned_jobs: List[str] = field(default_factory=list)
    block_minutes_granted: int = 0
    block_minutes_overrun: int = 0


def _burst_factor(duration: int, slack: int, rng: random.Random) -> float:
    """How far past its window does this job run?

    Longer jobs overrun more often; jobs squeezed into a window with little
    slack have nowhere to absorb a setback. Both are lognormal-ish in practice.
    """
    p = min(0.55, 0.10 + duration / 1200.0 + (0.15 if slack < 20 else 0.0))
    if rng.random() > p:
        return 1.0
    return 1.0 + abs(rng.gauss(0.0, 0.18)) + 0.05


def simulate(world, plan: Plan, *, seed: int = 101,
             burst: bool = True) -> SimResult:
    cfg = world.cfg
    rng = random.Random(seed)
    headway = cfg["section"]["headway_min"]
    pad = headway // 2
    buf = cfg["planning"]["block_buffer_min"]
    res = SimResult()

    loop_at = {s.code: s.loop for s in world.stations}

    # ---- 1. realise the blocks (planned window -> actual window) ----------
    actual_blocks: Dict[str, List[tuple]] = {}
    for b in plan.blocks:
        work = b.duration - 2 * buf
        slack = _window_slack(world, b)
        f = _burst_factor(work, slack, rng) if burst else 1.0
        actual_end = b.start + int(round(work * f)) + 2 * buf
        over = actual_end - b.end
        res.block_minutes_granted += b.duration
        if over > 0:
            res.block_minutes_overrun += over
            res.bursts.append({"job_id": b.job_id, "activity": b.activity,
                               "stretch": b.stretch_id, "overrun_min": over,
                               "planned_min": b.duration})
        # a burst that eats more than an hour past its window is called off and
        # the site handed back -- the work does not count as completed
        if over > 60:
            res.abandoned_jobs.append(b.job_id)
            actual_end = b.end + 60
        else:
            res.completed_jobs.append(b.job_id)
        actual_blocks.setdefault(b.stretch_id, []).append((b.start, actual_end))
    for v in actual_blocks.values():
        v.sort()

    # ---- 2. replay the traffic -------------------------------------------
    stretch_busy: Dict[str, List[tuple]] = {}
    order = sorted(world.trains, key=lambda t: t.entry)
    idx = {(s.from_code, s.to_code, s.line): s for s in world.stretches}

    for tp in order:
        holds: Dict[int, int] = {}
        placed = None
        for _ in range(40):
            placed, need = _run_once(tp, holds, idx, stretch_busy,
                                     actual_blocks, pad, loop_at)
            if need is None:
                break
            i, wait = need
            k = _last_loop_index(tp, i, loop_at)
            if k is None:                       # nowhere to hold: absorb at origin
                k = 0
            holds[k] = holds.get(k, 0) + wait
            if sum(holds.values()) > 600:
                break
        if placed is None:
            continue
        for sid, a, b in placed:
            stretch_busy.setdefault(sid, []).append((a, b))
            stretch_busy[sid].sort()
        delay = int(placed[-1][2] - pad - tp.stops[-1]["arr"])
        res.delays[tp.uid] = max(0, delay)
        held = sum(holds.values())
        if held > 0:
            res.holds += 1
            res.hold_minutes += held

    return res


def _window_slack(world, b) -> int:
    """Free minutes immediately after the block before the next train."""
    nxt = [o.t_in for o in world.occupancy.get(b.stretch_id, []) if o.t_in >= b.end]
    return min(nxt) - b.end if nxt else 600


def _run_once(tp: TrainPath, holds: Dict[int, int], idx, stretch_busy,
              actual_blocks, pad: int, loop_at):
    """One forward pass. Returns (placed, None) or (None, (leg_index, wait))."""
    placed = []
    t = tp.stops[0]["dep"] + holds.get(0, 0)
    for i, (a, b) in enumerate(zip(tp.stops, tp.stops[1:])):
        if i > 0:
            # A train leaves a station at the later of (a) when it got there
            # and (b) its booked departure. Dropping the booked departure lets
            # trains run AHEAD of schedule, which puts them on stretches at
            # times nobody planned for and manufactures conflicts out of a
            # timetable that is conflict-free.
            t = max(b_prev_arr, a["dep"]) + holds.get(i, 0)
        st = idx.get((a["code"], b["code"], tp.line)) or \
             idx.get((b["code"], a["code"], tp.line))
        if st is None:
            return placed, None
        transit = int(b["arr"] - a["dep"])
        s0, s1 = t - pad, t + transit + pad
        wait = 0
        for x, y in actual_blocks.get(st.id, ()):        # maintenance window
            if x < s1 and s0 < y:
                wait = max(wait, y - s0)
        for x, y in stretch_busy.get(st.id, ()):         # traffic ahead
            if x < s1 and s0 < y:
                wait = max(wait, y - s0)
        if wait > 0:
            return None, (i, wait)
        placed.append((st.id, s0, s1))
        b_prev_arr = t + transit
    return placed, None


def _last_loop_index(tp: TrainPath, i: int, loop_at) -> int | None:
    for k in range(i, -1, -1):
        if loop_at.get(tp.stops[k]["code"]):
            return k
    return None

"""Synthetic freight paths.

Why this file exists: freight runs on unpublished paths, and on a trunk route
like BRC-ST it is a large share of the traffic. A block plan built only against
the published passenger timetable would look wonderfully feasible and be
completely wrong. README A1: *ignoring freight is the fastest way to lose a
railway judge.*

Method (deliberately the same logic a section controller uses):
  * pick a departure time,
  * walk the section stretch by stretch at freight speed,
  * where the next stretch is occupied, HOLD IN A LOOP -- but only at stations
    that actually have one,
  * if the path cannot be threaded, abandon it and try another slot.

So freight lands in the genuine gaps of the passenger timetable, gets looped
for overtakes, and is labelled SIMULATED everywhere it surfaces.
"""
from __future__ import annotations

import random
from typing import Dict, List

from core.model import MIN_PER_DAY, Occupancy, Station, Stretch, TrainPath
from ingest.timetable import DIR_DECREASING, DIR_INCREASING, PROFILE

MAX_HOLD_MIN = 75          # controllers will not hold a goods train forever
MAX_ATTEMPTS = 40


def _earliest_free(intervals: List[Occupancy], t: int, need: int,
                   horizon: int) -> int | None:
    """First instant >= t where `need` minutes of this stretch are free."""
    cand = t
    for iv in intervals:
        if iv.t_out <= cand:
            continue
        if iv.t_in >= cand + need:
            return cand
        cand = iv.t_out
        if cand + need > horizon:
            return None
    return cand if cand + need <= horizon else None


def _thread_one(stations: List[Station], stretches_idx: Dict, line: str,
                start: int, occ: Dict[str, List[Occupancy]], headway: int,
                speed: float, horizon: int, origin_shifts: int = 10):
    """Thread one freight path, holding at loops where it can.

    If a hold is needed where there is no loop, we do what a controller does:
    keep the train at the originating yard a bit longer and try again, rather
    than start it into a path it cannot complete.
    """
    for _ in range(origin_shifts):
        res = _thread_attempt(stations, stretches_idx, line, start, occ,
                              headway, speed, horizon)
        if isinstance(res, tuple):
            return res
        if res is None:
            return None
        start = res                     # int -> retry with a later departure
    return None


def _thread_attempt(stations: List[Station], stretches_idx: Dict, line: str,
                    start: int, occ: Dict[str, List[Occupancy]], headway: int,
                    speed: float, horizon: int):
    order = stations if line == DIR_INCREASING else list(reversed(stations))
    pad = headway // 2
    t = start
    stops = [{"code": order[0].code, "km": order[0].km, "arr": t, "dep": t}]
    new_occ: List[Occupancy] = []

    for a, b in zip(order, order[1:]):
        # stretch ids are always stored in increasing-chainage order, so an
        # UP (decreasing) run has to look its stretch up the other way round
        st = (stretches_idx.get((a.code, b.code, line))
              or stretches_idx.get((b.code, a.code, line)))
        if st is None:
            return None
        transit = int(round(abs(b.km - a.km) / speed * 60.0))
        # We must clear the PADDED occupancy [dep-pad, arr+pad], so look for a
        # free window of transit+headway and place the departure pad-in.
        need = transit + headway
        w0 = _earliest_free(occ.get(st.id, []), max(0, t - pad), need, horizon)
        if w0 is None:
            return None
        dep = w0 + pad
        hold = dep - t
        if hold > 0:
            if not a.loop or hold > MAX_HOLD_MIN:
                # nowhere to stand here: ask the caller to start us later
                shifted = start + max(1, hold)
                return shifted if shifted + 240 < horizon else None
            stops[-1]["dep"] = dep
        arr = dep + transit
        new_occ.append(Occupancy(st.id, dep - pad, arr + pad))
        stops.append({"code": b.code, "km": b.km, "arr": arr, "dep": arr})
        t = arr
    return stops, new_occ


def synthesize_freight(cfg: dict, stations: List[Station],
                       stretches: List[Stretch],
                       occ: Dict[str, List[Occupancy]],
                       per_day_per_direction: int = 12,
                       seed: int = 23) -> List[TrainPath]:
    """Insert freight paths into the real gaps; mutates `occ` in place."""
    rng = random.Random(seed)
    idx = {(s.from_code, s.to_code, s.line): s for s in stretches}
    headway = cfg["section"]["headway_min"]
    horizon = cfg["planning"]["horizon_days"] * MIN_PER_DAY
    speed_base = PROFILE["FREIGHT"]["speed"]

    out: List[TrainPath] = []
    counter = 0
    for day in range(cfg["planning"]["horizon_days"]):
        for line in (DIR_INCREASING, DIR_DECREASING):
            placed = 0
            attempts = 0
            while placed < per_day_per_direction and attempts < \
                    per_day_per_direction * MAX_ATTEMPTS:
                attempts += 1
                # freight is night-weighted: controllers push goods into the
                # thin hours when passenger density drops
                hour = rng.choice([0, 1, 1, 2, 2, 3, 3, 4, 5, 9, 10, 11,
                                   12, 13, 14, 15, 21, 22, 23, 23])
                start = day * MIN_PER_DAY + hour * 60 + rng.randrange(60)
                speed = speed_base * rng.uniform(0.88, 1.10)
                res = _thread_one(stations, idx, line, start, occ, headway,
                                  speed, horizon)
                if res is None:
                    continue
                stops, new_occ = res
                counter += 1
                no = f"F{counter:04d}"
                tp = TrainPath(train_no=no, name="Goods (simulated)",
                               ttype="FREIGHT", line=line, day=day,
                               priority=PROFILE["FREIGHT"]["prio"], stops=stops)
                tp.occupancies = new_occ
                for o in new_occ:
                    occ.setdefault(o.stretch_id, []).append(o)
                for k in {o.stretch_id for o in new_occ}:
                    occ[k].sort(key=lambda x: x.t_in)
                out.append(tp)
                placed += 1
    return out

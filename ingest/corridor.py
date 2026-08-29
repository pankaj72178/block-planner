"""Corridor blocks -- and letting the planner choose where to put them.

WHY THIS MODULE EXISTS. Run the optimiser against the raw timetable and it
returns a plan made entirely of ultrasonic flaw detection: 45-90 minute jobs
are the only ones that fit in a natural gap. Tamping needs ~3 hours and ballast
screening ~6, and on a section carrying 66 trains a day those windows simply do
not occur. The plan is feasible, optimal, and useless -- the track geometry
work never happens and the backlog grows for ever.

That is not a modelling artefact. It is the actual reason the Railway Board
pushed fixed daily CORRIDOR BLOCKS onto trunk routes: you cannot schedule a
track machine into gaps that do not exist, so you create the gap by timetabling
around it, once, in the working timetable.

So this module does what a Chief Operations Manager does:

  1. For every day, line, and contiguous span of the section, score every
     candidate 3-hour window by the traffic it would displace
     (SUM of train priority x minutes each train must move).
  2. Take the cheapest one.
  3. Re-timetable the affected trains around it, in priority order, so premium
     traffic keeps its path and goods gives way.

The output is a timetable with a real hole in it, plus an honest bill for what
that hole cost in displaced traffic. Both go on the slide.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from core.model import MIN_PER_DAY, Occupancy, Station, Stretch, TrainPath


@dataclass
class CorridorWindow:
    day: int
    line: str
    stretch_ids: List[str]
    from_code: str
    to_code: str
    km_start: float
    km_end: float
    start: int
    end: int
    displaced_trains: int
    displacement_cost: float

    @property
    def duration(self) -> int:
        return self.end - self.start


def _spans(stations: List[Station], stretches: List[Stretch], line: str,
           n_stretches: int) -> List[dict]:
    onl = [s for s in stretches if s.line == line]
    onl.sort(key=lambda s: s.km_start)
    out = []
    for i in range(len(onl) - n_stretches + 1):
        grp = onl[i:i + n_stretches]
        out.append({
            "ids": [g.id for g in grp],
            "from": grp[0].from_code, "to": grp[-1].to_code,
            "km_start": grp[0].km_start, "km_end": grp[-1].km_end,
        })
    return out


def choose_corridor_windows(cfg: dict, stations: List[Station],
                            stretches: List[Stretch], trains: List[TrainPath],
                            *, duration_min: int = 180, n_stretches: int = 4,
                            step_min: int = 30,
                            rotation_penalty: float = 0.6) -> List[CorridorWindow]:
    n_days = cfg["planning"]["horizon_days"]
    lines = cfg["section"]["lines"]

    # index occupancies by (stretch, day) so scoring a window is a small scan
    idx: Dict[tuple, List[tuple]] = {}
    for t in trains:
        for o in t.occupancies:
            d = o.t_in // MIN_PER_DAY
            idx.setdefault((o.stretch_id, d), []).append((o.t_in, o.t_out, t))

    span_cache = {ln: _spans(stations, stretches, ln, n_stretches) for ln in lines}
    chosen: List[CorridorWindow] = []

    # A block schedule that closes the same 33 km five days running maintains
    # one third of the section and abandons the rest. Real divisional block
    # schedules rotate, so repeat selections of a span carry a penalty
    # proportional to the day's own cost spread -- self-scaling, no magic
    # constant to retune when the traffic changes.
    used_span: Dict[str, int] = {}

    for day in range(n_days):
        best = None
        scored: List[tuple] = []
        for line in lines:
            for span in span_cache[line]:
                for k in range(0, MIN_PER_DAY // step_min):
                    w0 = day * MIN_PER_DAY + k * step_min
                    w1 = w0 + duration_min
                    hit: Dict[str, list] = {}
                    for sid in span["ids"]:
                        for d in (day - 1, day, day + 1):
                            for a, b, t in idx.get((sid, d), ()):
                                if a < w1 and w0 < b:
                                    cur = hit.get(t.uid)
                                    if cur is None:
                                        hit[t.uid] = [a, b, t]
                                    else:
                                        cur[0] = min(cur[0], a)
                                        cur[1] = max(cur[1], b)
                    # cost = what it takes to push each affected train clear of
                    # the window, weighted by how much we mind moving it
                    cost = 0.0
                    for a, b, t in hit.values():
                        cost += t.priority * min(b - w0, w1 - a)
                    scored.append((cost, line, span, w0, w1, len(hit)))

        costs = [c for c, *_ in scored]
        spread = (max(costs) - min(costs)) if costs else 0.0
        for cand in scored:
            key = f"{cand[1]}:{cand[2]['from']}-{cand[2]['to']}"
            adj = cand[0] + rotation_penalty * spread * used_span.get(key, 0)
            if best is None or adj < best[0]:
                best = (adj, *cand[1:], cand[0])
        _, line, span, w0, w1, n, cost = best
        used_span[f"{line}:{span['from']}-{span['to']}"] = \
            used_span.get(f"{line}:{span['from']}-{span['to']}", 0) + 1
        chosen.append(CorridorWindow(
            day=day, line=line, stretch_ids=span["ids"], from_code=span["from"],
            to_code=span["to"], km_start=span["km_start"],
            km_end=span["km_end"], start=w0, end=w1,
            displaced_trains=n, displacement_cost=round(cost, 1)))
    return chosen


def corridor_occupancy(windows: List[CorridorWindow]) -> Dict[str, List[Occupancy]]:
    """Express the reserved windows as occupancies so the threading avoids them."""
    occ: Dict[str, List[Occupancy]] = {}
    for w in windows:
        for sid in w.stretch_ids:
            occ.setdefault(sid, []).append(Occupancy(sid, w.start, w.end))
    for v in occ.values():
        v.sort(key=lambda o: o.t_in)
    return occ


def summarise(windows: List[CorridorWindow]) -> dict:
    return {
        "windows": len(windows),
        "duration_min": windows[0].duration if windows else 0,
        "total_reserved_hours": round(
            sum(w.duration for w in windows) / 60.0, 1),
        "placements": [
            {"day": w.day + 1, "line": w.line,
             "span": f"{w.from_code}-{w.to_code}",
             "km": f"{w.km_start:.0f}-{w.km_end:.0f}",
             "window": f"{(w.start % MIN_PER_DAY) // 60:02d}:"
                       f"{(w.start % MIN_PER_DAY) % 60:02d}-"
                       f"{(w.end % MIN_PER_DAY) // 60:02d}:"
                       f"{(w.end % MIN_PER_DAY) % 60:02d}",
             "trains_displaced": w.displaced_trains}
            for w in windows
        ],
    }

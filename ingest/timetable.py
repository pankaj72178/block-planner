"""Timetable ingest -> per-stretch train occupancy intervals.

TWO MODES, one code path:

  * REAL   -- drop the data.gov.in / Kaggle "Indian Railways time table" CSV at
              data/timetable.csv, filtered to trains touching this section.
              Expected columns (the public schema):
                  Train_No, Train_Name, SEQ, Station_Code, Station_Name,
                  Arrival_time, Departure_time, Distance
              Optional extras we use if present: Train_Type, Runs_On.
  * SYNTH  -- if that file is absent we GENERATE one in exactly that schema and
              write it to the same path. So the swap to real data is: overwrite
              one CSV. Nothing else in the repo changes.

The output of this module is the constraint set the optimizer dodges: a list of
fixed [t_in, t_out] intervals per blockable stretch, padded by half the
sectional headway on each side.
"""
from __future__ import annotations

import csv
import os
import random
from typing import Dict, List

from core.model import MIN_PER_DAY, Occupancy, Station, Stretch, TrainPath

CSV_COLUMNS = [
    "Train_No", "Train_Name", "Train_Type", "SEQ", "Station_Code",
    "Station_Name", "Arrival_time", "Departure_time", "Distance", "Runs_On",
]

# Direction convention, stated once so nobody has to guess:
#   'DN' line  = direction of INCREASING chainage (BRC -> ST, towards Mumbai)
#   'UP' line  = direction of DECREASING chainage (ST -> BRC, towards Delhi)
DIR_INCREASING = "DN"
DIR_DECREASING = "UP"

# Speed / stopping profile by train class. ASSUMPTION, calibrated to public
# sectional speeds (Route class A, 130 km/h) and Trains-at-a-Glance run times.
PROFILE = {
    "SUPERFAST":  {"speed": 118, "dwell": 2, "stop_prob": 0.10, "accel_pen": 2.0, "prio": 10},
    "EXPRESS":    {"speed": 100, "dwell": 2, "stop_prob": 0.35, "accel_pen": 2.0, "prio": 7},
    "PASSENGER":  {"speed": 68,  "dwell": 1, "stop_prob": 1.00, "accel_pen": 1.5, "prio": 3},
    "FREIGHT":    {"speed": 55,  "dwell": 0, "stop_prob": 0.00, "accel_pen": 3.0, "prio": 1},
}

_PREMIUM = ["Rajdhani Exp", "Vande Bharat Exp", "Shatabdi Exp", "Duronto Exp",
            "Tejas Exp", "Garib Rath Exp"]
_EXPRESS = ["Gujarat Mail", "Paschim Exp", "Karnavati Exp", "Saurashtra Mail",
            "Golden Temple Mail", "Avantika Exp", "Lokshakti Exp", "Gujarat Queen",
            "Firozpur Janata Exp", "Sabarmati Exp", "Bandra Term Exp",
            "Mumbai Central Exp", "Ahmedabad Mail", "Kutch Exp", "Suryanagari Exp",
            "Ranakpur Exp", "Ala Hazrat Exp", "Yoga Exp", "Somnath Exp"]
_LOCAL = ["MEMU", "Passenger", "DEMU"]


def _sample_hour(rng, ttype: str) -> float:
    """Departure-hour distribution.

    Long-distance traffic on the Delhi--Mumbai corridor is NOT uniform over the
    day: originating departures cluster in the evening, so trains sweep through
    Gujarat overnight and in the early morning, and commuter services peak
    either side of the working day. The consequence is a genuinely thinner
    late-morning / early-afternoon window -- which is exactly the window the
    Railway Board's ~3 h corridor-block policy targets.

    We model that shape rather than sprinkling trains uniformly, because a
    uniform timetable makes block planning look impossible and a realistic one
    makes it look hard. Only the second is true.
    """
    if ttype == "SUPERFAST":
        return rng.choice([0.4, 1.2, 2.0, 3.1, 4.4, 5.2, 6.1, 21.5, 22.4, 23.3])
    if ttype == "EXPRESS":
        r = rng.random()
        if r < 0.46:                       # overnight sweep
            return rng.uniform(18.0, 30.0) % 24.0
        if r < 0.74:                       # morning shoulder
            return rng.uniform(5.5, 10.0)
        if r < 0.92:                       # evening shoulder
            return rng.uniform(15.0, 18.5)
        return rng.uniform(10.0, 15.0)     # the thin midday tail -- not empty
    # commuter peaks
    return rng.choice([5.5, 6.3, 7.1, 8.0, 9.2, 16.0, 17.2, 18.1, 19.0,
                       20.3, 21.2, 8.6, 18.6, 12.4])


def infer_type(name: str, train_no: str = "") -> str:
    """Works on the real dataset too -- IR names carry the class."""
    n = (name or "").lower()
    if any(k in n for k in ("rajdhani", "shatabdi", "vande bharat", "duronto",
                            "tejas", "garib rath", "humsafar", "superfast")):
        return "SUPERFAST"
    if any(k in n for k in ("memu", "demu", "passenger", "local")):
        return "PASSENGER"
    if train_no.startswith(("1", "2")) and len(train_no) == 5:
        return "EXPRESS"
    return "EXPRESS"


# --------------------------------------------------------------------------
# Synthetic generation (data.gov.in schema)
# --------------------------------------------------------------------------
def _run_profile(stations: List[Station], ttype: str, down: bool, rng) -> List[dict]:
    """Return [{code, km, arr, dep}] with times relative to 0 = section entry."""
    p = PROFILE[ttype]
    order = stations if down else list(reversed(stations))
    halt_codes = {order[0].code, order[-1].code}
    for s in order[1:-1]:
        prob = p["stop_prob"]
        if s.code in ("BH", "ANK", "MYG"):        # commercially important halts
            prob = min(1.0, prob + 0.35)
        if not s.loop:
            prob *= 0.6
        if rng.random() < prob:
            halt_codes.add(s.code)

    stops, t = [], 0.0
    for i, s in enumerate(order):
        halts = s.code in halt_codes
        arr = t
        dwell = p["dwell"] if halts and i not in (0,) else 0
        if halts and s.code in ("BRC", "BH", "ST"):
            dwell = max(dwell, 3)
        dep = arr + dwell
        stops.append({"code": s.code, "km": s.km, "arr": arr, "dep": dep,
                      "halt": halts})
        if i < len(order) - 1:
            nxt = order[i + 1]
            dist = abs(nxt.km - s.km)
            run = dist / p["speed"] * 60.0
            if halts:
                run += p["accel_pen"]
            if nxt.code in halt_codes:
                run += p["accel_pen"] * 0.6
            t = dep + run
    return stops


def generate_timetable_csv(cfg: dict, stations: List[Station], path: str,
                           seed: int = 11, n_super: int = 8, n_exp: int = 26,
                           n_pass: int = 14) -> str:
    """Write a synthetic but structurally-real passenger timetable."""
    rng = random.Random(seed)
    rows, used = [], set()

    def emit(ttype: str, name: str, no: str, down: bool, dep_hour: float,
             runs_on: str = "1111111"):
        stops = _run_profile(stations, ttype, down, rng)
        t0 = dep_hour * 60.0
        for seq, s in enumerate(stops, start=1):
            arr_abs = (t0 + s["arr"]) % MIN_PER_DAY
            dep_abs = (t0 + s["dep"]) % MIN_PER_DAY
            st = next(x for x in stations if x.code == s["code"])
            rows.append({
                "Train_No": no,
                "Train_Name": name,
                "Train_Type": ttype,
                "SEQ": seq,
                "Station_Code": s["code"],
                "Station_Name": st.name,
                "Arrival_time": "--" if seq == 1 else _fmt(arr_abs),
                "Departure_time": "--" if seq == len(stops) else _fmt(dep_abs),
                "Distance": round(abs(s["km"] - stops[0]["km"]), 1),
                "Runs_On": runs_on,
            })

    def new_no(prefix: str) -> str:
        while True:
            no = prefix + f"{rng.randint(0, 999):03d}"
            if no not in used:
                used.add(no)
                return no

    # Premium trains cluster in the night/early-morning window on this corridor.
    for i in range(n_super):
        nm = rng.choice(_PREMIUM)
        down = i % 2 == 0
        hour = _sample_hour(rng, "SUPERFAST")
        emit("SUPERFAST", f"{nm}", new_no("12"), down, hour)
    for i in range(n_exp):
        nm = rng.choice(_EXPRESS)
        down = i % 2 == 0
        hour = _sample_hour(rng, "EXPRESS")
        weekly = rng.random() < 0.12
        runs = "1111111"
        if weekly:
            d = rng.randrange(7)
            runs = "".join("1" if k == d else "0" for k in range(7))
        emit("EXPRESS", nm, new_no("19"), down, hour, runs)
    for i in range(n_pass):
        nm = f"{rng.choice(_LOCAL)}"
        down = i % 2 == 0
        # commuter peaks either side of the working day
        hour = _sample_hour(rng, "PASSENGER")
        emit("PASSENGER", nm, new_no("59"), down, hour)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    return path


def _fmt(m: float) -> str:
    m = int(round(m)) % MIN_PER_DAY
    return f"{m // 60:02d}:{m % 60:02d}:00"


def _parse(s: str) -> float | None:
    s = (s or "").strip()
    if s in ("", "--", "-", "None"):
        return None
    parts = s.split(":")
    return int(parts[0]) * 60 + int(parts[1])


# --------------------------------------------------------------------------
# Load -> TrainPath objects -> occupancy intervals
# --------------------------------------------------------------------------
def load_or_generate(cfg: dict, stations: List[Station], path: str,
                     seed: int = 11) -> List[dict]:
    if not os.path.exists(path):
        generate_timetable_csv(cfg, stations, path, seed=seed)
    with open(path) as f:
        return list(csv.DictReader(f))


def build_train_paths(cfg: dict, stations: List[Station],
                      stretches: List[Stretch], rows: List[dict],
                      traffic_multiplier: float = 1.0) -> List[TrainPath]:
    """Expand the daily timetable across the planning horizon."""
    by_km = {s.code: s.km for s in stations}
    horizon_days = cfg["planning"]["horizon_days"]
    headway = cfg["section"]["headway_min"]

    trains: Dict[str, List[dict]] = {}
    for r in rows:
        trains.setdefault(r["Train_No"], []).append(r)

    paths: List[TrainPath] = []
    for no, rs in trains.items():
        rs.sort(key=lambda r: int(r["SEQ"]))
        name = rs[0]["Train_Name"]
        ttype = rs[0].get("Train_Type") or infer_type(name, no)
        runs_on = rs[0].get("Runs_On") or "1111111"

        kms = [by_km.get(r["Station_Code"]) for r in rs]
        if any(k is None for k in kms) or len(kms) < 2:
            continue                       # train doesn't fully traverse section
        line = DIR_INCREASING if kms[-1] > kms[0] else DIR_DECREASING

        base_stops = []
        prev = None
        for r, km in zip(rs, kms):
            arr = _parse(r["Arrival_time"])
            dep = _parse(r["Departure_time"])
            if arr is None:
                arr = dep
            if dep is None:
                dep = arr
            if prev is not None and arr < prev - 600:
                arr += MIN_PER_DAY       # midnight wrap
                dep += MIN_PER_DAY
            elif prev is not None and dep < arr:
                dep += MIN_PER_DAY
            prev = arr
            base_stops.append({"code": r["Station_Code"], "km": km,
                               "arr": arr, "dep": dep})

        for day in range(horizon_days):
            if runs_on[day % 7] != "1":
                continue
            off = day * MIN_PER_DAY
            stops = [{**s, "arr": s["arr"] + off, "dep": s["dep"] + off}
                     for s in base_stops]
            tp = TrainPath(train_no=no, name=name, ttype=ttype, line=line,
                           day=day, priority=PROFILE[ttype]["prio"], stops=stops)
            tp.occupancies = occupancies_for(tp, stretches, headway)
            if tp.occupancies:
                paths.append(tp)

    if traffic_multiplier > 1.0:
        paths += _duplicate_traffic(paths, stretches, headway, traffic_multiplier)
    return paths


def occupancies_for(tp: TrainPath, stretches: List[Stretch],
                    headway: int) -> List[Occupancy]:
    """One interval per inter-station stretch, padded by half the headway.

    Padding is what turns 'a train is here' into 'no maintenance can start
    here' -- signalling needs clearance either side of a movement.
    """
    idx = {(s.from_code, s.to_code, s.line): s for s in stretches}
    pad = headway // 2
    out: List[Occupancy] = []
    for a, b in zip(tp.stops, tp.stops[1:]):
        st = idx.get((a["code"], b["code"], tp.line)) or \
             idx.get((b["code"], a["code"], tp.line))
        if st is None:
            continue
        out.append(Occupancy(stretch_id=st.id,
                             t_in=int(a["dep"]) - pad,
                             t_out=int(b["arr"]) + pad))
    return out


def _duplicate_traffic(paths, stretches, headway, mult) -> List[TrainPath]:
    """Sensitivity scenario: '+20% traffic'. Clone freight-friendly paths."""
    extra = int(len(paths) * (mult - 1.0))
    rng = random.Random(99)
    clones = []
    for i in range(extra):
        src = rng.choice(paths)
        shift = rng.choice([-37, -23, 19, 31, 47])
        stops = [{**s, "arr": s["arr"] + shift, "dep": s["dep"] + shift}
                 for s in src.stops]
        tp = TrainPath(train_no=f"X{src.train_no}", name=src.name + " (extra)",
                       ttype=src.ttype, line=src.line, day=src.day,
                       priority=max(1, src.priority - 2), stops=stops)
        tp.occupancies = occupancies_for(tp, stretches, headway)
        clones.append(tp)
    return clones


def occupancy_by_stretch(paths: List[TrainPath]) -> Dict[str, List[Occupancy]]:
    out: Dict[str, List[Occupancy]] = {}
    for p in paths:
        for o in p.occupancies:
            out.setdefault(o.stretch_id, []).append(o)
    for k in out:
        out[k].sort(key=lambda o: o.t_in)
    return out


# --------------------------------------------------------------------------
# Headway regularisation
# --------------------------------------------------------------------------
def regularise(paths: List[TrainPath], stretches: List[Stretch], headway: int,
               max_delay: int = 90,
               seed_occ: Dict[str, List[Occupancy]] | None = None) -> dict:
    """Make the base timetable conflict-free at sectional headway.

    A published timetable is conflict-free by construction; ours is assembled
    from independently sampled paths, so it is not. That matters for more than
    tidiness -- the Level-2 retiming model puts every train interval into the
    same NoOverlap as the maintenance blocks, so a base timetable that already
    violates headway is infeasible before the solver starts.

    Method is control-office logic, not a repair heuristic: thread trains onto
    the section in PRIORITY ORDER (premium first), each one taking the earliest
    slot at or after its nominal time that clears everything already threaded.
    Lower-priority trains yield to higher-priority ones. Converges by
    construction -- no iterate-until-it-settles loop.

    Trains that cannot be threaded within `max_delay` are dropped and counted.
    """
    order = sorted(paths, key=lambda p: (-p.priority, p.entry))
    # Reserved corridor windows are seeded as occupancy, so trains thread
    # AROUND them exactly as they thread around each other. That is what makes
    # the hole real rather than a hole we merely drew on the chart.
    occ: Dict[str, List[Occupancy]] = {k: list(v) for k, v in
                                       (seed_occ or {}).items()}
    kept: List[TrainPath] = []
    delays: List[int] = []
    dropped = 0

    for p in order:
        d = _min_clearing_delay(p, occ, max_delay)
        if d is None:
            dropped += 1
            continue
        if d:
            _shift_path(p, d)
            p.occupancies = occupancies_for(p, stretches, headway)
        for o in p.occupancies:
            occ.setdefault(o.stretch_id, []).append(o)
        for k in {o.stretch_id for o in p.occupancies}:
            occ[k].sort(key=lambda x: x.t_in)
        delays.append(d)
        kept.append(p)

    paths[:] = sorted(kept, key=lambda p: (p.day, p.entry))
    moved = [d for d in delays if d > 0]
    return {
        "threaded": len(kept),
        "dropped_unthreadable": dropped,
        "trains_delayed": len(moved),
        "mean_delay_min": round(sum(moved) / len(moved), 1) if moved else 0.0,
        "max_delay_min": max(moved, default=0),
        "residual_conflicts": count_conflicts(paths),
    }


def _min_clearing_delay(p: TrainPath, occ: Dict[str, List[Occupancy]],
                        max_delay: int, iters: int = 60) -> int | None:
    """Smallest forward shift that clears this whole path. Monotone fixpoint."""
    d = 0
    for _ in range(iters):
        need = 0
        for o in p.occupancies:
            for iv in occ.get(o.stretch_id, ()):
                if iv.t_in < o.t_out + d and o.t_in + d < iv.t_out:
                    need = max(need, iv.t_out - (o.t_in + d))
        if need == 0:
            return d
        d += need
        if d > max_delay:
            return None
    return None


def count_conflicts(paths: List[TrainPath]) -> int:
    buckets: Dict[str, List[Occupancy]] = {}
    for p in paths:
        for o in p.occupancies:
            buckets.setdefault(o.stretch_id, []).append(o)
    n = 0
    for items in buckets.values():
        items.sort(key=lambda o: o.t_in)
        for a, b in zip(items, items[1:]):
            if b.t_in < a.t_out:
                n += 1
    return n


def _shift_path(p: TrainPath, minutes: int) -> None:
    for s in p.stops:
        s["arr"] += minutes
        s["dep"] += minutes

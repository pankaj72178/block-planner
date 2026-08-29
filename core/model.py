"""Shared domain objects for the block planner.

Time convention: everything is an integer **minute offset from t=0**, where
t=0 is 00:00 on `planning.day_zero`. The horizon is `horizon_days * 1440`.
Keeping one integer clock across the timetable, the optimizer and the
simulator is what stops unit bugs from eating a hackathon.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional
import os
import yaml

MIN_PER_DAY = 1440
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
OUT_DIR = os.path.join(ROOT, "out")


# --------------------------------------------------------------------------
# Infrastructure
# --------------------------------------------------------------------------
@dataclass
class Station:
    code: str
    name: str
    km: float
    loop: bool          # has a crossing/overtaking loop -> trains can be held
    platforms: int


@dataclass
class Stretch:
    """A blockable inter-station stretch on ONE line (UP or DN).

    This is the unit the optimizer reasons about: you take a block on
    'BH-ANK/DN', not on an abstract kilometre.
    """
    id: str
    from_code: str
    to_code: str
    line: str           # 'UP' | 'DN'
    km_start: float
    km_end: float

    @property
    def length_km(self) -> float:
        return abs(self.km_end - self.km_start)


@dataclass
class AssetSegment:
    """~1 km of track on one line. Unit of asset condition and maintenance."""
    id: str
    stretch_id: str
    line: str
    km_start: float
    km_end: float
    age_years: float
    gmt_per_year: float
    cumulative_gmt: float
    curvature: float                 # degrees; 0 = straight
    is_station_yard: bool
    last_done: Dict[str, int] = field(default_factory=dict)   # activity -> days ago
    tgi: float = 85.0
    tgi_history: List[float] = field(default_factory=list)


# --------------------------------------------------------------------------
# Traffic
# --------------------------------------------------------------------------
@dataclass
class Occupancy:
    """One train sitting on one stretch for [t_in, t_out] minutes."""
    stretch_id: str
    t_in: int
    t_out: int


@dataclass
class TrainPath:
    train_no: str
    name: str
    ttype: str          # SUPERFAST | EXPRESS | PASSENGER | FREIGHT
    line: str           # UP | DN
    day: int            # 0..horizon_days-1
    priority: int       # retiming penalty weight; higher = harder to move
    stops: List[dict]   # [{code, km, arr, dep}] absolute minutes
    occupancies: List[Occupancy] = field(default_factory=list)

    @property
    def uid(self) -> str:
        return f"{self.train_no}#{self.day}"

    @property
    def entry(self) -> int:
        return min(o.t_in for o in self.occupancies)

    @property
    def exit(self) -> int:
        return max(o.t_out for o in self.occupancies)


# --------------------------------------------------------------------------
# Maintenance
# --------------------------------------------------------------------------
@dataclass
class Job:
    id: str
    activity: str
    label: str
    stretch_id: str
    line: str
    segment_ids: List[str]
    km_start: float
    km_end: float
    duration_min: int
    machine_type: str
    daylight_only: bool
    needs_power_block: bool
    due_min: int              # deadline in horizon minutes; may be negative (overdue)
    days_overdue: float
    priority: float           # 1..10, from norms base + overdue + TGI
    asset_km: float
    availability_value: float = 1.0   # km-equivalents of availability restored
    urgent: bool = False              # a segment below the urgent TGI threshold


@dataclass
class MachineUnit:
    id: str
    mtype: str
    label: str
    reposition_buffer_min: int
    available: bool = True


@dataclass
class PlannedBlock:
    job_id: str
    activity: str
    label: str
    stretch_id: str
    line: str
    km_start: float
    km_end: float
    machine_unit: str
    machine_type: str
    start: int
    end: int
    priority: float
    locked: bool = False

    @property
    def duration(self) -> int:
        return self.end - self.start


@dataclass
class Plan:
    name: str
    blocks: List[PlannedBlock]
    unscheduled: List[str]
    retimings: Dict[str, int] = field(default_factory=dict)   # train uid -> shift
    solver_status: str = ""
    solve_seconds: float = 0.0
    objective: float = 0.0
    best_bound: float = 0.0

    @property
    def gap_pct(self) -> float:
        if not self.best_bound:
            return 0.0
        return round(100.0 * (self.best_bound - self.objective)
                     / max(1e-9, self.best_bound), 1)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "blocks": [asdict(b) for b in self.blocks],
            "unscheduled": self.unscheduled,
            "retimings": self.retimings,
            "solver_status": self.solver_status,
            "solve_seconds": round(self.solve_seconds, 2),
            "objective": round(self.objective, 1),
            "best_bound": round(self.best_bound, 1),
            "gap_pct": self.gap_pct,
        }


# --------------------------------------------------------------------------
# Config loading
# --------------------------------------------------------------------------
def load_config(section_file: str = None, norms_file: str = None) -> dict:
    section_file = section_file or os.path.join(DATA_DIR, "section.yaml")
    norms_file = norms_file or os.path.join(DATA_DIR, "norms.yaml")
    with open(section_file) as f:
        cfg = yaml.safe_load(f)
    with open(norms_file) as f:
        cfg["norms"] = yaml.safe_load(f)
    return cfg


def horizon_minutes(cfg: dict) -> int:
    return cfg["planning"]["horizon_days"] * MIN_PER_DAY


def hhmm(t: int) -> str:
    """Minute offset -> 'D1 14:35' for humans and for chart axis labels."""
    d, rem = divmod(int(t) % (7 * MIN_PER_DAY), MIN_PER_DAY)
    return f"D{d + 1} {rem // 60:02d}:{rem % 60:02d}"

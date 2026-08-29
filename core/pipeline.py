"""One call that assembles the whole world: network, traffic, condition, demand.

Everything downstream (both optimizers, the simulator, the API, the experiments)
takes a `World`. Building it in exactly one place is what keeps the greedy
baseline and CP-SAT honest -- they are handed byte-identical inputs, so any
difference in the results is the planner, not the data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

import os

from core.model import (DATA_DIR, AssetSegment, Job, MachineUnit, Occupancy,
                        Station, Stretch, TrainPath, load_config)
from ingest.network import build_network
from ingest.timetable import (build_train_paths, load_or_generate,
                              occupancy_by_stretch, regularise, count_conflicts)
from ingest.freight_synth import synthesize_freight
from ingest.corridor import (CorridorWindow, choose_corridor_windows,
                             corridor_occupancy, summarise)
from assets.degradation import (initialise_condition, predict_days_to_threshold,
                                train_degradation_model)
from assets.demand_generator import demand_summary, generate_jobs


@dataclass
class World:
    cfg: dict
    stations: List[Station]
    stretches: List[Stretch]
    segments: List[AssetSegment]
    trains: List[TrainPath]
    occupancy: Dict[str, List[Occupancy]]
    jobs: List[Job]
    machines: List[MachineUnit]
    corridors: List[CorridorWindow] = field(default_factory=list)
    reports: Dict = field(default_factory=dict)

    @property
    def horizon(self) -> int:
        return self.cfg["planning"]["horizon_days"] * 1440

    def job(self, jid: str) -> Job:
        return next(j for j in self.jobs if j.id == jid)


def build_machines(cfg: dict, disabled: List[str] | None = None) -> List[MachineUnit]:
    disabled = set(disabled or [])
    out: List[MachineUnit] = []
    for mtype, spec in cfg["norms"]["machines"].items():
        for i in range(1, spec["units"] + 1):
            uid = f"{mtype}-{i}"
            out.append(MachineUnit(
                id=uid, mtype=mtype, label=spec["label"],
                reposition_buffer_min=spec["reposition_buffer_min"],
                available=uid not in disabled,
            ))
    return out


def build_world(cfg: dict | None = None, *, traffic_multiplier: float = 1.0,
                freight_per_day_per_direction: int = 12,
                disabled_machines: List[str] | None = None,
                corridor_blocks: bool | None = None,
                max_jobs: int = 160, lookahead_days: int = 14,
                seed: int = 7, train_ml: bool = True,
                timetable_csv: str | None = None) -> World:
    # absolute, so the pipeline behaves the same from a CLI run, a test, or a
    # uvicorn worker started from some other directory
    timetable_csv = timetable_csv or os.path.join(DATA_DIR, "timetable.csv")
    cfg = cfg or load_config()
    reports: Dict = {}

    stations, stretches, segments = build_network(cfg, seed=seed)

    # --- traffic -----------------------------------------------------------
    hw = cfg["section"]["headway_min"]
    rows = load_or_generate(cfg, stations, timetable_csv, seed=seed + 4)

    def _traffic(seed_occ=None):
        p = build_train_paths(cfg, stations, stretches, rows,
                              traffic_multiplier=traffic_multiplier)
        rep = regularise(p, stretches, hw, seed_occ=seed_occ)
        o = occupancy_by_stretch(p)
        if seed_occ:                     # freight must dodge the corridor too
            for k, v in seed_occ.items():
                o.setdefault(k, []).extend(v)
                o[k].sort(key=lambda x: x.t_in)
        f = synthesize_freight(cfg, stations, stretches, o,
                               per_day_per_direction=freight_per_day_per_direction,
                               seed=seed + 16)
        return p, f, rep

    cb = cfg["planning"].get("corridor_block", {})
    want_corridor = cb.get("enabled", False) if corridor_blocks is None \
        else corridor_blocks

    paths, freight, rep = _traffic()
    corridors: List[CorridorWindow] = []
    if want_corridor:
        # score candidate windows against the un-reserved timetable, then
        # rebuild the whole timetable around the winners
        corridors = choose_corridor_windows(
            cfg, stations, stretches, paths + freight,
            duration_min=cb.get("duration_min", 210),
            n_stretches=cb.get("n_stretches", 4),
            step_min=cb.get("step_min", 30))
        paths, freight, rep = _traffic(corridor_occupancy(corridors))
        reports["corridor"] = summarise(corridors)
    reports["timetable"] = rep

    trains = paths + freight
    # world.occupancy is what a BLOCK must dodge -- trains only. The corridor
    # windows are deliberately absent: they are the holes we made for it.
    occ = occupancy_by_stretch(trains)
    reports["traffic"] = {
        "passenger_train_days": len(paths),
        "freight_train_days": len(freight),
        "trains_per_day": round(len(trains) / cfg["planning"]["horizon_days"], 1),
        "headway_conflicts": count_conflicts(trains),
        "freight_share_pct": round(100 * len(freight) / max(1, len(trains)), 1),
    }

    # --- asset condition + learned degradation model -----------------------
    initialise_condition(segments, cfg, seed=seed + 24)
    predicted: Dict[str, float] = {}
    if train_ml:
        model, metrics = train_degradation_model(segments, cfg, seed=seed)
        predicted = predict_days_to_threshold(model, segments)
        reports["ml"] = metrics
        reports["ml"]["uplift_vs_naive"] = round(
            metrics["baseline_mae_days"] / max(1e-9, metrics["mae_days"]), 2)

    # --- maintenance demand ------------------------------------------------
    jobs, demand_report = generate_jobs(cfg, segments, stretches, predicted,
                                        lookahead_days=lookahead_days,
                                        max_jobs=max_jobs, seed=seed + 34)
    reports["demand"] = demand_report
    reports["demand_by_activity"] = demand_summary(jobs)

    machines = build_machines(cfg, disabled_machines)
    reports["machines"] = {
        "units": len(machines),
        "available": sum(1 for m in machines if m.available),
        "disabled": sorted(disabled_machines or []),
    }

    supply_h = (cfg["planning"]["max_block_hours_per_day"]
                * cfg["planning"]["horizon_days"])
    reports["supply_demand"] = {
        "block_hours_available_cap": supply_h,
        "block_hours_demanded": demand_report["total_block_hours_demanded"],
        "oversubscription_x": round(
            demand_report["total_block_hours_demanded"] / supply_h, 1),
    }

    return World(cfg=cfg, stations=stations, stretches=stretches,
                 segments=segments, trains=trains, occupancy=occ, jobs=jobs,
                 machines=machines, corridors=corridors, reports=reports)

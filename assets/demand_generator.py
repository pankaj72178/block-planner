"""Turn published norms + simulated condition into a concrete job list.

This is the README A3 generator. The pending-work register is internal to IR,
but the RULES that produce it are published (IRPWM), so we reconstruct rather
than guess:

    IRPWM periodicity  +  last-done date  ->  calendar due date
    ML degradation model                  ->  condition-based due date
    min(the two)                          ->  the date the optimizer plans to

Two due-date sources is the point. Calendar-only maintenance is what IR
already does; the condition-based date is what "maximise asset availability"
actually needs, and it is why a segment carrying 60 GMT on a curve gets pulled
forward while a straight, lightly-loaded one gets pushed back.
"""
from __future__ import annotations

import math
import random
from typing import Dict, List

from core.model import MIN_PER_DAY, AssetSegment, Job, Stretch


def _periodicity_days(spec: dict, gmt: float) -> int:
    for band in spec["periodicity_days_by_gmt"]:
        if gmt >= band["gmt_min"]:
            return band["days"]
    return spec["periodicity_days_by_gmt"][-1]["days"]


def _priority(base: float, days_overdue: float, tgi: float,
              activity: str, tgi_cfg: dict) -> float:
    """Blend three 0-10 signals rather than adding bonuses onto a base.

    Adding bonuses saturates: every overdue job pins to 10 and the objective
    silently degenerates into 'schedule as many jobs as possible', which is the
    wrong plan -- it quietly prefers three cheap USFD runs to one urgent
    deep-screening. A weighted blend keeps the ordering meaningful.

      criticality  what the manual says this activity is worth
      urgency      how far past its due date it already is
      condition    what the asset itself is telling us
    """
    criticality = float(base)
    urgency = min(10.0, max(0.0, days_overdue) / 6.0)      # 60 d late -> 10
    if activity in ("tamping", "deep_screening"):
        deficit = tgi_cfg["intervention_threshold"] - tgi
        condition = max(0.0, min(10.0, deficit / 1.5))     # 15 TGI below -> 10
    elif activity == "usfd":
        condition = 6.0 if days_overdue > 0 else 3.0       # rail-fracture risk
    else:
        condition = 3.0
    p = 0.45 * criticality + 0.30 * urgency + 0.25 * condition
    return round(max(1.0, min(10.0, p)), 2)


def generate_jobs(cfg: dict, segments: List[AssetSegment],
                  stretches: List[Stretch],
                  predicted_days: Dict[str, float] | None = None,
                  lookahead_days: int = 14, max_jobs: int = 160,
                  seed: int = 41) -> tuple[List[Job], dict]:
    """Build maintenance jobs due inside (or already past) the planning window.

    Returns (jobs, report). `report` records what was dropped by the cap --
    a silently truncated job list would make every KPI in this repo a lie.
    """
    rng = random.Random(seed)
    norms = cfg["norms"]["activities"]
    tgi_cfg = cfg["norms"]["tgi"]
    predicted_days = predicted_days or {}
    horizon_days = cfg["planning"]["horizon_days"]

    by_stretch: Dict[str, List[AssetSegment]] = {}
    for s in segments:
        by_stretch.setdefault(s.stretch_id, []).append(s)
    for v in by_stretch.values():
        v.sort(key=lambda s: s.km_start)

    candidates: List[Job] = []
    counter = 0
    stats = {a: {"due_segments": 0, "jobs": 0} for a in norms}

    for act, spec in norms.items():
        yard_only = spec.get("station_yard_only", False)
        per_block_km = spec["output_km_per_block"]
        lo_min, hi_min = spec["block_minutes"]

        for sid, segs in by_stretch.items():
            due: List[tuple[AssetSegment, float]] = []
            for seg in segs:
                if yard_only and not seg.is_station_yard:
                    continue
                if act == "points_crossings" and not seg.is_station_yard:
                    continue
                period = _periodicity_days(spec, seg.gmt_per_year)
                cal_due = period - seg.last_done.get(act, 0)
                due_days = cal_due
                if act == "tamping" and seg.id in predicted_days:
                    # condition-based date can only PULL WORK FORWARD, never
                    # defer past the manual's periodicity -- IRPWM is a floor.
                    due_days = min(cal_due, predicted_days[seg.id])
                if act == "deep_screening":
                    # IRPWM's alternative trigger is tonnage carried SINCE THE
                    # LAST SCREENING, not since the track was laid. Using
                    # lifetime GMT here would mark essentially the whole
                    # section as due, which is how you generate a fake crisis.
                    gmt_since = seg.gmt_per_year * \
                        (seg.last_done.get(act, 0) / 365.0)
                    if gmt_since >= spec.get("cumulative_gmt_trigger", 1e9):
                        due_days = min(due_days, 0)
                if due_days <= lookahead_days:
                    due.append((seg, due_days))

            if not due:
                continue
            stats[act]["due_segments"] += len(due)

            # chunk contiguous due segments into what one block can deliver
            chunk = max(1, int(round(per_block_km)))
            for i in range(0, len(due), chunk):
                grp = due[i:i + chunk]
                counter += 1
                segs_in = [g[0] for g in grp]
                due_days = min(g[1] for g in grp)
                km_lo = min(s.km_start for s in segs_in)
                km_hi = max(s.km_end for s in segs_in)
                asset_km = round(sum(s.km_end - s.km_start for s in segs_in), 2)

                frac = min(1.0, asset_km / max(0.1, per_block_km))
                dur = lo_min + (hi_min - lo_min) * frac
                dur = int(round(dur * rng.uniform(0.92, 1.08) / 5.0) * 5)
                dur = max(lo_min, min(hi_min, dur))

                worst_tgi = min(s.tgi for s in segs_in)
                overdue = max(0.0, -due_days)
                stretch_line = sid.split("/")[-1]
                candidates.append(Job(
                    id=f"J{counter:04d}",
                    activity=act,
                    label=spec["label"],
                    stretch_id=sid,
                    line=stretch_line,
                    segment_ids=[s.id for s in segs_in],
                    km_start=round(km_lo, 2),
                    km_end=round(km_hi, 2),
                    duration_min=dur,
                    machine_type=spec["machine"],
                    daylight_only=bool(spec.get("daylight_only", False)),
                    needs_power_block=bool(spec.get("needs_power_block", False)),
                    due_min=int(due_days * MIN_PER_DAY),
                    days_overdue=round(overdue, 1),
                    priority=_priority(spec["base_priority"], overdue,
                                       worst_tgi, act, tgi_cfg),
                    asset_km=asset_km,
                    availability_value=round(
                        asset_km * spec.get("availability_value_per_km", 1.0), 3),
                    urgent=(act in ("tamping", "deep_screening")
                            and worst_tgi < tgi_cfg["urgent_threshold"]),
                ))
                stats[act]["jobs"] += 1

    # ---- cap, and say out loud what the cap dropped (README: no silent caps)
    candidates.sort(key=lambda j: (-j.priority, j.due_min))
    kept, dropped = candidates[:max_jobs], candidates[max_jobs:]
    report = {
        "generated": len(candidates),
        "kept": len(kept),
        "dropped_by_cap": len(dropped),
        "dropped_priority_max": round(max([j.priority for j in dropped], default=0), 2),
        "dropped_asset_km": round(sum(j.asset_km for j in dropped), 1),
        "per_activity": stats,
        "overdue_jobs": sum(1 for j in kept if j.days_overdue > 0),
        "total_block_hours_demanded": round(
            sum(j.duration_min for j in kept) / 60.0, 1),
        "horizon_days": horizon_days,
    }
    return kept, report


def demand_summary(jobs: List[Job]) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for j in jobs:
        d = out.setdefault(j.activity, {"n": 0, "hours": 0.0, "asset_km": 0.0,
                                        "overdue": 0, "label": j.label})
        d["n"] += 1
        d["hours"] += j.duration_min / 60.0
        d["asset_km"] += j.asset_km
        d["overdue"] += 1 if j.days_overdue > 0 else 0
    for d in out.values():
        d["hours"] = round(d["hours"], 1)
        d["asset_km"] = round(d["asset_km"], 1)
    return out

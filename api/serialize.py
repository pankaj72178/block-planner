"""Shrink the world into what a browser needs for the charts."""
from __future__ import annotations

from dataclasses import asdict
from typing import List

MIN_PER_DAY = 1440


def ui_payload(world, plans, rows: List[dict], day_limit: int = 7) -> dict:
    stations = [asdict(s) for s in world.stations]
    trains = []
    for t in world.trains:
        if t.day >= day_limit:
            continue
        trains.append({
            "uid": t.uid, "no": t.train_no, "name": t.name, "type": t.ttype,
            "line": t.line, "day": t.day,
            # only km + time: enough to draw the time-distance line
            "pts": [[round(s["km"], 1), int(s["arr"]), int(s["dep"])]
                    for s in t.stops],
        })
    from optimizer.validate import validate
    return {
        "corridors": [{"day": c.day, "line": c.line, "km_start": c.km_start,
                       "km_end": c.km_end, "start": c.start, "end": c.end,
                       "span": f"{c.from_code}-{c.to_code}",
                       "displaced": c.displaced_trains}
                      for c in world.corridors],
        "validation": [validate(world, p) for p in plans],
        "section": world.cfg["section"],
        "planning": world.cfg["planning"],
        "stations": stations,
        "trains": trains,
        "jobs": [asdict(j) for j in world.jobs],
        "machines": [asdict(m) for m in world.machines],
        "plans": [p.to_dict() for p in plans],
        "kpis": rows,
        "reports": world.reports,
    }

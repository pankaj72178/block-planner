"""FastAPI backend for the block planner.

    python -m api.main            ->  http://127.0.0.1:8000

Endpoints
    GET  /                 the planner UI
    GET  /api/data         world + all three plans + KPIs (cached)
    POST /api/replan       re-solve under scenario changes and/or locked blocks
    GET  /api/job/{id}     job detail for the inspector panel
    GET  /api/health

The point of /api/replan is the human-in-the-loop story: a planner locks the
blocks they have already committed to, breaks a machine or turns up the
traffic, and the solver replans everything else AROUND those decisions. It is
a genuine re-solve with the locked intervals fixed, not a patch over the old
answer -- which is why it can legitimately move everything else.
"""
from __future__ import annotations

import os
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from core.pipeline import build_world
from optimizer.cpsat import solve_cpsat
from optimizer.greedy import solve_greedy
from optimizer.retiming import solve_with_retiming
from sim.kpis import evaluate
from sim.simulator import simulate
from api.serialize import ui_payload

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI_DIR = os.path.join(ROOT, "ui")

app = FastAPI(title="AI Block Planner — Vadodara–Surat", version="1.0")
_cache: dict = {}


class ReplanRequest(BaseModel):
    traffic: float = 1.0
    freight_per_day: int = 12
    disabled_machines: List[str] = []
    block_hours_per_day: Optional[float] = None
    corridor_blocks: bool = True
    allow_retiming: bool = True
    time_limit: float = 15.0
    max_retimed: int = 10
    locked: List[dict] = []          # [{job_id, start, machine_unit}]


def _world_key(r: ReplanRequest) -> tuple:
    return (round(r.traffic, 3), r.freight_per_day,
            tuple(sorted(r.disabled_machines)), r.block_hours_per_day,
            r.corridor_blocks)


def get_world(r: ReplanRequest):
    key = _world_key(r)
    if key not in _cache:
        w = build_world(traffic_multiplier=r.traffic,
                        freight_per_day_per_direction=r.freight_per_day,
                        disabled_machines=r.disabled_machines,
                        corridor_blocks=r.corridor_blocks, max_jobs=200)
        if r.block_hours_per_day is not None:
            w.cfg["planning"]["max_block_hours_per_day"] = r.block_hours_per_day
        _cache[key] = w
    return _cache[key]


def _plan_and_score(world, r: ReplanRequest):
    plans = [solve_greedy(world)]
    cp = solve_cpsat(world, time_limit=r.time_limit, hint_plan=plans[0],
                     locked=r.locked)
    plans.append(cp)
    diag = {}
    if r.allow_retiming:
        rt, diag = solve_with_retiming(world, cp, time_limit=r.time_limit * 1.5,
                                       max_retimed=r.max_retimed)
        plans.append(rt)
    rows = []
    base = None
    for p in plans:
        s = simulate(world, p, seed=207)
        base = base or s
        rows.append(evaluate(world, p, s, base))
    return plans, rows, diag


@app.get("/api/health")
def health():
    return {"ok": True, "cached_worlds": len(_cache)}


@app.get("/api/data")
def data(fresh: bool = False):
    """First paint.

    Serves the artefact `run_pipeline.py` already wrote if there is one, so the
    page is up instantly instead of making the audience watch a 60-second
    solve. Everything after that -- every scenario, every re-optimise -- is
    solved live. Pass ?fresh=1 to force a cold solve.
    """
    cached = os.path.join(ROOT, "out", "ui_data.json")
    if not fresh and os.path.exists(cached):
        import json
        with open(cached) as f:
            payload = json.load(f)
        payload["served_from"] = "out/ui_data.json (precomputed)"
        return payload
    r = ReplanRequest()
    w = get_world(r)
    if "default" not in _cache:
        _cache["default"] = _plan_and_score(w, r)
    plans, rows, diag = _cache["default"]
    out = ui_payload(w, plans, rows)
    out["retiming_diagnostics"] = {k: v for k, v in diag.items()
                                   if k != "candidate_uids"}
    out["served_from"] = "live solve"
    return out


@app.post("/api/replan")
def replan(r: ReplanRequest):
    w = get_world(r)
    plans, rows, diag = _plan_and_score(w, r)
    out = ui_payload(w, plans, rows)
    out["retiming_diagnostics"] = {k: v for k, v in diag.items()
                                   if k != "candidate_uids"}
    out["served_from"] = "live solve"
    return out


@app.get("/api/job/{job_id}")
def job(job_id: str):
    w = get_world(ReplanRequest())
    try:
        j = w.job(job_id)
    except StopIteration:
        raise HTTPException(404, f"no such job: {job_id}")
    segs = {s.id: s for s in w.segments}
    return {
        "job": j.__dict__,
        "segments": [{"id": sid, "tgi": segs[sid].tgi,
                      "age_years": segs[sid].age_years,
                      "gmt_per_year": segs[sid].gmt_per_year,
                      "curvature": segs[sid].curvature,
                      "last_done": segs[sid].last_done}
                     for sid in j.segment_ids if sid in segs],
    }


# no-store on the UI assets: this is a tool you edit and reload while
# presenting, and a browser quietly serving yesterday's app.js is the worst
# possible five minutes to spend on stage.
NO_CACHE = {"Cache-Control": "no-store, must-revalidate"}


@app.get("/")
def index():
    """Stamp the asset URLs with the files' mtime.

    no-store is not always enough: proxies in front of a dev server (the in-app
    preview browser is one) can still hand back yesterday's app.js. A version
    query makes the URL itself change when the file does, which no cache can
    argue with.
    """
    html = open(os.path.join(UI_DIR, "index.html")).read()
    v = str(int(max(os.path.getmtime(os.path.join(UI_DIR, f))
                    for f in ("app.js", "style.css"))))
    return HTMLResponse(html.replace("__V__", v), headers=NO_CACHE)


@app.get("/app.js")
def appjs():
    return FileResponse(os.path.join(UI_DIR, "app.js"), headers=NO_CACHE,
                        media_type="application/javascript")


@app.get("/style.css")
def css():
    return FileResponse(os.path.join(UI_DIR, "style.css"), headers=NO_CACHE,
                        media_type="text/css")


if __name__ == "__main__":
    import uvicorn
    print("  starting on http://127.0.0.1:8000  (first request builds the "
          "world and solves — give it ~40 s)")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")

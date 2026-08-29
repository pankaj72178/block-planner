# ARCHITECTURE.md — how the app is built and how it runs

Companion to [README.md](README.md) (results), [FEATURES.md](FEATURES.md)
(what each part does and why) and [ROADMAP.md](ROADMAP.md) (what is missing).

This file is for someone who has to **modify** the code — a teammate picking it
up, or you in three weeks having forgotten why something is the way it is.

---

## 1. Shape of the thing

```
                       ┌──────────────────────────────────┐
   data/section.yaml ─►│  ingest/network.py               │
   data/norms.yaml     │  stations → stretches → segments │
                       └────────────────┬─────────────────┘
                                        │
   data/timetable.csv ─►┌───────────────▼──────────────────┐
   (real or generated)  │ ingest/timetable.py              │
                        │ parse → occupancy intervals      │
                        │ regularise() to headway          │
                        └───────────────┬──────────────────┘
                                        │
                        ┌───────────────▼──────────────────┐
                        │ ingest/freight_synth.py          │
                        │ thread goods into the real gaps  │
                        └───────────────┬──────────────────┘
                                        │
                        ┌───────────────▼──────────────────┐
                        │ ingest/corridor.py               │
                        │ score every day × line × span ×  │
                        │ window → pick cheapest, rotate,  │
                        │ re-thread traffic around it      │
                        └───────────────┬──────────────────┘
                                        │
   assets/degradation.py ───────────────┤
   TGI history → GBM surrogate          │
             │                          │
             ▼                          │
   assets/demand_generator.py ──────────┤
   IRPWM periodicity + predicted due    │
   → 200 jobs                           │
                                        ▼
                        ┌──────────────────────────────────┐
                        │        core/pipeline.py          │
                        │        build_world() → World     │
                        └───────────────┬──────────────────┘
                                        │
             ┌──────────────────────────┼──────────────────────────┐
             ▼                          ▼                          ▼
   optimizer/greedy.py        optimizer/cpsat.py         optimizer/retiming.py
   "manual practice"          the core deliverable       Level 2, targeted
             │                          │                          │
             └──────────────────────────┼──────────────────────────┘
                                        ▼
                        ┌──────────────────────────────────┐
                        │ optimizer/validate.py            │
                        │ SECOND implementation of every   │
                        │ constraint — independent check   │
                        └───────────────┬──────────────────┘
                                        ▼
                        ┌──────────────────────────────────┐
                        │ sim/simulator.py + sim/kpis.py   │
                        │ block bursts, knock-on delay,    │
                        │ 8 replications, AAI              │
                        └───────────────┬──────────────────┘
                                        ▼
                   run_pipeline.py  ·  api/main.py  ·  ui/
```

**The three roles stay distinct, and that is deliberate:** the core is
**optimisation**, the AI layer **feeds** it, the simulator **proves** it. None
of them is called "AI" to make a slide look better.

---

## 2. The one convention that prevents most bugs

> **Everything is an integer minute offset from `t = 0`, where `t = 0` is 00:00
> on `planning.day_zero`. The horizon is `horizon_days × 1440 = 10080`.**

One integer clock across the timetable, the optimiser and the simulator. No
datetimes, no timezones, no float minutes. `core/model.hhmm()` renders it as
`D3 14:35` when a human has to read it.

CP-SAT wants integers anyway, so this is not a compromise.

---

## 3. Data model

`core/model.py` — plain dataclasses, no ORM, no magic.

### Infrastructure

```python
Station(code, name, km, loop, platforms)          # 13
Stretch(id, from_code, to_code, line, km_start, km_end)   # 24
AssetSegment(id, stretch_id, line, km_start, km_end,
             age_years, gmt_per_year, cumulative_gmt,
             curvature, is_station_yard, last_done, tgi)   # 258
```

`Stretch.id` looks like `BH-ANK/DN`. **Stretch ids are always stored in
increasing-chainage order**, so a train running UP (decreasing km) must look its
stretch up the other way round. That off-by-direction bug cost half the goods
traffic once; both lookup sites now try both orders.

### Traffic

```python
Occupancy(stretch_id, t_in, t_out)     # padded by headway/2 each side
TrainPath(train_no, name, ttype, line, day, priority, stops, occupancies)
```

`TrainPath.uid` is `"12345#3"` — train number, `#`, day index. That uid is the
key for retiming offsets, so it must be stable.

### Maintenance

```python
Job(id, activity, stretch_id, line, segment_ids, km_start, km_end,
    duration_min, machine_type, daylight_only, needs_power_block,
    due_min, days_overdue, priority, asset_km,
    availability_value, urgent)
MachineUnit(id, mtype, label, reposition_buffer_min, available)
PlannedBlock(job_id, activity, stretch_id, line, km_start, km_end,
             machine_unit, machine_type, start, end, priority, locked)
Plan(name, blocks, unscheduled, retimings, solver_status, solve_seconds,
     objective, best_bound)
```

`Job.availability_value = asset_km × availability_value_per_km`. That single
field is what stops the optimiser spending the whole week on ultrasonic
testing — see [FEATURES.md §1.5](FEATURES.md).

### The World

`core/pipeline.py` assembles everything into one `World`:

```python
World(cfg, stations, stretches, segments, trains, occupancy,
      jobs, machines, corridors, reports)
```

Every downstream consumer — both optimisers, the simulator, the API, the
experiments — takes a `World`. Building it in **exactly one place** is what
keeps the greedy baseline and CP-SAT honest: they get byte-identical inputs, so
any difference in the results is the planner, not the data.

`world.occupancy` contains **trains only**. The corridor windows are
deliberately absent from it — they are the holes we made *for* the blocks, not
obstacles the blocks must dodge. They are seeded into a separate dict during
threading so trains avoid them.

---

## 4. The CP-SAT model in full

`optimizer/cpsat.py`. This is the part worth reading closely.

### Variables

| Variable | Type | Meaning |
|---|---|---|
| `start[j]` | IntVar **over a domain** | when job `j` starts |
| `present[j]` | Bool | is `j` in the plan at all |
| `track_iv[j]` | OptionalInterval | `[start, start + duration + 2×buffer]` |
| `assign[j,u]` | Bool | `j` runs on machine unit `u` |
| `mach_iv[j,u]` | OptionalInterval | same start, size + repositioning buffer |
| `dayb[j][d]` | Bool | `j` starts on day `d` |
| `off[t]` | IntVar **over a domain** | retiming shift for train `t` (Level 2) |

### Constraints

```python
# 1. one machine unit iff scheduled
m.Add(sum(assign[j,u] for u) == present[j])

# 2. a block dodges trains and other blocks on its stretch
m.AddNoOverlap(per_stretch[sid])         # job intervals + MOVABLE trains only

# 3. one machine, one job at a time (+ repositioning)
m.AddNoOverlap(per_unit[uid])

# 4. exactly one day, consistent with the start time
m.AddExactlyOne(dayb[j])
m.Add(start[j] >= d*1440).OnlyEnforceIf(dayb[j][d])
m.Add(start[j] <  (d+1)*1440).OnlyEnforceIf(dayb[j][d])

# 5. operations tolerance
m.Add(sum(size[j] * used[j][d] for j) <= 480)   # per day

# 6. divisional machine outturn targets
m.Add(sum(present[j] for j of type T) >= floor[T])

# 7. locked blocks
m.Add(present[j] == 1); m.Add(start[j] == locked_start)
```

Note what is **not** in `NoOverlap`: immovable trains. See §5.

### Objective

```python
maximise  Σ_j Σ_d used[j][d] × ( 100 × availability_value[j] × (7 − d)
                               + 1200 if job j is urgent
                               +   20 × priority[j] )
        − Σ_t ( weight[class] × |off[t]| + fixed_cost[class] × moved[t] )
```

| Constant | Value | Role |
|---|---|---|
| `AVAILABILITY_WEIGHT` | 100 | points per weighted asset-km-day recovered — **dominant** |
| `URGENT_BONUS` | 1200 | a segment below the urgent TGI band is a speed restriction waiting to happen |
| `PRIORITY_SCALE` | 20 | urgency tiebreak only |

**The objective is the KPI.** `100 × availability_value × days_remaining` is
exactly the weighted asset-km-days that `sim/kpis.asset_availability()`
reports. That correspondence is not a nicety — an earlier version had priority
dominant, proved `OPTIMAL`, and lost to greedy on the printed metric.
`tests/test_planner.py::test_cpsat_beats_greedy_on_its_own_objective` guards it.

### Safety rails

```python
# never ship worse than the hint you started from
if score_plan(world, plan) < score_plan(world, hint_plan):
    return hint_plan_relabelled   # marked "(kept hint)" in the status

# outturn targets unreachable → relax and SAY SO, don't return INFEASIBLE
if status not in (OPTIMAL, FEASIBLE) and floors:
    return solve_cpsat(..., outturn=False)   # .outturn_relaxed = True
```

The first one matters on hard instances. Under `freight +50%` the solver could
not beat the baseline in 15 s; without the guard that showed up in the results
table as the optimiser *losing*, which is a lie about the algorithm and a
truthful statement only about the time limit.

---

## 5. The two performance tricks

Both come from one insight:

> **A train you are not allowed to move is not a scheduling decision. It is a
> hole in the calendar. Holes belong in variable domains, not in `NoOverlap`.**

### 5.1 `free_start_domain()` — jobs

Instead of putting ~5,600 fixed train intervals into `NoOverlap`, precompute
per job the **set of start times at which it fits**, and build a
`cp_model.Domain` from it. Daylight windows are intersected into the same
domain, so constraint 4's daylight clauses disappear too.

```
before:  tens of thousands of interval variables, minutes to solve
after:   a few hundred, seconds to solve, proves OPTIMAL
```

A job whose domain comes back **empty has no feasible window anywhere in the
week**. That is not an error, it is the headline finding — 140 of 200 jobs
against the raw timetable — and it is recorded in `plan.no_window` rather than
being silently dropped.

### 5.2 `feasible_offsets()` — trains

Same trick for Level 2. For each movable goods path, enumerate shifts in
`[−90, +90]` and keep those that clear all *immovable* traffic on every stretch
it uses. Offset 0 is always feasible (the base timetable is conflict-free), so
this can never make the model infeasible.

Movable trains still go into `NoOverlap` — they have to clear each other and
the blocks.

### 5.3 The bug that taught the lesson

```python
# WRONG — looks harmless, makes the whole model INFEASIBLE in presolve
s2 = m.NewIntVar(max(0, o.t_in - lim), min(H, o.t_in + lim), ...)

# RIGHT
s2 = m.NewIntVar(o.t_in - lim, o.t_in + lim, ...)
```

Clamping to `[0, H]` silently contradicts the shared per-train offset for any
path near either end of the horizon. Presolve returns `INFEASIBLE` in 0.0 s with
no explanation. Bisecting it — trains only, then trains without the clamp — is
how it was found.

---

## 6. Corridor placement algorithm

`ingest/corridor.py`, and it is cheap: **0.1 s** for the whole week.

```
for each day:
    for each line (UP, DN):
        for each contiguous span of 4 stretches:        # ~9 spans
            for each start time, every 30 min:           # 48
                hit  = trains overlapping this window on any span stretch
                cost = Σ train.priority × min(minutes to push it out either way)
    adjusted = cost + rotation_penalty × cost_spread × times_this_span_used
    take the minimum, mark the span used
```

`rotation_penalty` is **0.6 × the day's own cost spread**, not a magic
constant — self-scaling, so it does not need retuning when the traffic changes.
Without it the placer closes the same cheapest 33 km five days running and
maintains a third of the section.

The chosen windows are then expressed as `Occupancy` objects and **seeded into
`regularise()`**, so trains thread around them exactly as they thread around
each other. That is what makes the hole real rather than drawn on the chart —
and `tests/test_planner.py::test_corridor_windows_are_actually_train_free`
asserts it.

---

## 7. Simulator

`sim/simulator.py`. Not SimPy — a hand-rolled loop, because determinism and
transparency matter more here than framework features.

```
1. realise the blocks
     work overrun factor ~ f(duration, slack in the granted window)
     overrun > 60 min  →  work called off, site handed back, job NOT completed
2. replay trains in scheduled order
     for each leg:
         t = max(arrived_at_previous, booked departure) + any hold
         if the stretch is busy (block or train) → compute the wait
         push the hold back to the LAST STATION WITH A LOOP
         re-run the whole path from the start (holds only ever increase → converges)
3. delay = actual arrival − booked arrival, per train
```

### Two bugs worth remembering

**Departing on arrival instead of at the booked time.** Without
`max(arrived, booked_departure)` trains run *ahead* of schedule, occupy
stretches at times nobody planned for, and manufacture conflicts out of a
conflict-free timetable. Symptom: 34 trains delayed with an empty plan.

**That symptom is now a test.** `test_no_blocks_means_no_delay` asserts that
with no blocks planned, simulated delay is exactly zero. It is the control that
makes every punctuality number in the project meaningful — if it fails, all of
them are noise.

### Replications

`sim/kpis.evaluate_mc(world, plan, n=8)` averages over 8 independent burst
draws and also reports the spread and the worst p95. Comparing two planners on
one draw each compares the dice; one unlucky draw moved p95 passenger delay
from 0 to 48 minutes during development.

---

## 8. API and UI

### Backend — `api/main.py`

| Route | Purpose |
|---|---|
| `GET /` | the UI, with asset URLs stamped by file mtime |
| `GET /api/data` | first paint — serves `out/ui_data.json` if present, else solves live |
| `POST /api/replan` | **live re-solve** under scenario changes and locked blocks |
| `GET /api/job/{id}` | job detail: segments, TGI, age, GMT, last-done dates |
| `GET /api/health` | cached world count |

`/api/data` serving the precomputed artefact is a demo decision: the page is up
instantly instead of making an audience watch a 60-second solve. Everything
after that is live. `?fresh=1` forces a cold solve.

Worlds are cached by `(traffic, freight, disabled_machines, block_hours,
corridor_blocks)` — changing the solver budget or locking a block reuses the
world and only re-solves.

**`/api/replan` is a genuine re-solve with the locked intervals fixed, not a
patch over the old answer.** That is why it can legitimately move everything
else, and it is the human-in-the-loop story: a planner commits to the blocks
they have agreed, breaks a machine, and the solver replans around both.

### Frontend — `ui/` (610 lines, zero dependencies)

Hand-drawn SVG. No CDN, no build step, nothing to fail on demo day.

```
app.js
  init()          fetch /api/data, pick the first day with work, render
  drawChart()     time–distance: corridor bands, blocks, train polylines
  drawGantt()     13 machine units × 7 days
  drawTable()     four-way comparison, best value per row highlighted
  drawRetiming()  which goods paths moved, booked vs regulated entry
  drawProvenance()real / simulated / computed, per layer
  replan()        POST /api/replan, re-render
```

Scales are two closures — `X(t)` for time, `Y(km)` for distance. Everything
else is `<line>`, `<rect>` and `<polyline>` placed through them.

Two small things that are there for a reason:

- **The line filter defaults to `both`.** The corridor alternates between UP and
  DN day to day, so opening on a single line lands on an empty chart about half
  the time. Never open a demo on an empty chart.
- **Asset URLs carry a mtime version.** `Cache-Control: no-store` is not always
  enough — a proxy in front of a dev server can still hand back yesterday's
  `app.js`. A version query makes the URL itself change when the file does.

---

## 9. Configuration

Everything tunable lives in two YAML files. **No constants are buried in code
that a domain expert would want to argue with.**

### `data/section.yaml`

The railway and the planning policy: stations with chainage and loops,
headway, operations tolerance, daylight window, and the corridor-block policy
(`enabled`, `duration_min`, `n_stretches`, `step_min`).

### `data/norms.yaml`

The maintenance rulebook, derived from IRPWM. Per activity: periodicity bands
by GMT, block-minute range, machine type, daylight rule, output km per block,
`availability_value_per_km`, base priority. Plus the machine fleet, the
divisional outturn targets, and the TGI degradation parameters.

Every number carries a comment saying what it is calibrated to. That is what
turns a guess into a methodology, and it is the file to hand a PWI when you
want it argued with.

---

## 10. Running and extending

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
```

```bash
./.venv/bin/python run_pipeline.py --time-limit 25
```

```bash
./.venv/bin/python -m api.main
```

```bash
./.venv/bin/python -m pytest tests -q
```

Useful flags on `run_pipeline.py`: `--quick`, `--traffic 1.2`, `--freight 18`,
`--break CSM-1`, `--block-hours 12`, `--no-retiming`, `--seed N`.

### Common changes

| You want to… | Do this |
|---|---|
| use the real timetable | overwrite `data/timetable.csv` — nothing else changes |
| model a different section | edit `stations:` in `data/section.yaml` |
| change a maintenance rule | edit `data/norms.yaml` |
| add a new activity | one block in `norms.yaml` + a machine type; the generator and optimiser pick it up |
| change what the optimiser wants | `AVAILABILITY_WEIGHT` / `URGENT_BONUS` / `PRIORITY_SCALE` in `optimizer/cpsat.py` — **and update `sim/kpis.py` to match** |
| add a constraint | add it to `optimizer/cpsat.py` **and independently to `optimizer/validate.py`** |

That last row is the rule that keeps the project honest. The validator is a
second implementation on purpose; if you write the constraint once and call it
from both places, you have deleted the check.

---

## 11. Invariants the tests hold

20 test functions, 21 cases, ~40 s. Each is either a property the results
depend on or a bug that actually happened.

| Test | Why it exists |
|---|---|
| `test_base_timetable_is_conflict_free` | retiming is infeasible otherwise |
| `test_corridor_windows_are_actually_train_free` | the hole must be real |
| `test_corridor_rotates_across_the_section` | else you maintain a third of the section |
| `test_no_blocks_means_no_delay` | **the control** — without it, punctuality is noise |
| `test_plans_are_feasible` | every reported plan, independently checked |
| `test_cpsat_beats_greedy_on_its_own_objective` | a solver pointed at the wrong function |
| `test_solver_ladder_never_regresses` | Level 2 timing out on a worse incumbent |
| `test_cpsat_falls_back_rather_than_shipping_worse` | 1-second budget must return the hint |
| `test_locked_blocks_survive_a_replan` | human-in-the-loop actually works |
| `test_broken_machine_is_never_used` | the scenario controls are real |
| `test_outturn_targets_are_met` | else the week goes to the cheapest activity |
| `test_the_plan_is_not_all_one_activity` | the failure this project took longest to notice |
| `test_free_start_domain_*` | unit tests on the performance-critical encoding |
| `test_degradation_model_beats_a_naive_mean` | the ML claim, as an assertion |
| `test_corridor_policy_recovers_more_than_no_policy` | the headline claim, as an assertion |

---

## 12. Dependency footprint

Seven packages, all of them actually imported:

| Package | Used for |
|---|---|
| `ortools` | CP-SAT — the only heavyweight dependency |
| `scikit-learn` | GradientBoostingRegressor surrogate |
| `numpy` | KPI aggregation, degradation features |
| `pyyaml` | `section.yaml`, `norms.yaml` |
| `fastapi`, `uvicorn` | API |
| `pytest` | tests |

**The UI has none.** No npm, no bundler, no `node_modules` — hand-drawn SVG
served as three static files.

`osmnx` and `matplotlib` are commented out in `requirements.txt`: the pipeline
never fetches OSM at run time (`fetch_osm_topology` uses `urllib` and is not
called), and nothing plots by default. `pandas`, `networkx` and `simpy` were
dropped once it turned out nothing imported them — an unused dependency is a
thing that can break your install on demo morning.

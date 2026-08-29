# AI-Powered Automatic Block Planning — Vadodara Jn ⇄ Surat

**SIH 2026 · Ministry of Railways · "Maximize Asset Availability for Train
Operations on Indian Railways"**

A working block planner for a 129 km double-line stretch of the Delhi–Mumbai
trunk route carrying ~66 trains a day. It decides **which maintenance work
gets done this week, when, on which machine, and where the maintenance window
should be** — then proves the answer by simulating the week, block overruns
included.

> **One line:** real timetable, published maintenance rules, and a constraint
> solver that finds the block plan a control office cannot compute by hand.

### The documentation set

| File | Read it for |
|---|---|
| **README.md** (this file) | what it does, the results, how to run it |
| **[FEATURES.md](FEATURES.md)** | every feature explained, and how each one answers the problem statement |
| **[ARCHITECTURE.md](ARCHITECTURE.md)** | how the app is built — data model, CP-SAT formulation, simulator, API, UI |
| **[ROADMAP.md](ROADMAP.md)** | what is still missing, prioritised, with effort estimates |
| **[DATA.md](DATA.md)** | provenance layer by layer — what is real, what is simulated, every named assumption |

---

## Quick start

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
```

```bash
./.venv/bin/python run_pipeline.py
```

```bash
./.venv/bin/python -m api.main
```

`run_pipeline.py` runs the whole thing end to end and writes `out/`. The API
serves the planner UI on <http://127.0.0.1:8000> — it loads instantly from
`out/ui_data.json` and then solves live for every scenario you change.

Other entry points:

```bash
./.venv/bin/python -m experiments.sensitivity
```

```bash
./.venv/bin/python -m experiments.rolling --weeks 10
```

```bash
./.venv/bin/python -m pytest tests -q
```

---

## What it produces

Four planners on **byte-identical inputs**, so any difference is the planner
and not the data. Every plan is checked by an independently written
feasibility validator before its numbers are reported.

All KPIs are means over **8 independent block-burst replications** — a single
simulation run is one draw from a stochastic process, and comparing two
planners on one draw each compares the dice.

| | manual, no corridor | CP-SAT, no corridor | manual + auto-corridor | CP-SAT + auto-corridor |
|---|---|---|---|---|
| overdue asset-km-days recovered | 32.0 | 47.4 | 63.9 | **70.5** |
| availability-equivalent track | 10.4 km | 14.4 km | 19.6 km | **20.1 km** |
| urgent segments cleared | 0 | 2 | 6 | **6** |
| Asset Availability Index | 0.7316 | 0.7339 | 0.7363 | **0.7373** |
| passenger punctuality | 99.49 % | 99.37 % | 99.96 % | **99.83 %** |
| plan feasible | ✅ | ✅ | ✅ | ✅ |

**2.1× more overdue asset-km-days recovered, and passenger punctuality goes
up, not down.** The cost lands where it should: goods paths, deliberately
regulated at a loop, by well under a minute on average.

### The result that matters more — 10 weeks, rolling

One week barely moves an index built on a months-long backlog. Plan a week,
execute it, age the track by seven days, regenerate what is now due, repeat:

| after 10 weeks | backlog (weighted km past due) | mean TGI | segments in the urgent band |
|---|---|---|---|
| manual, no corridor policy | 259 → **447** ▲ | 70.0 → **64.8** ▼ | 24 → **53** |
| manual + auto-corridor | 259 → 337 | 70.0 → 76.4 | 24 → **0** |
| CP-SAT + auto-corridor | 259 → **309** | 70.0 → **77.7** | 24 → 1 |

Without a reserved corridor the section **deteriorates** — the backlog grows
73 % and track geometry decays. With one it **recovers**. Availability
delivered over the ten weeks: 1.00× / 2.78× / **3.15×**.

---

## Three findings worth defending

**1. Against the published timetable, most maintenance is simply impossible.**
140 of 200 candidate jobs have *no feasible window anywhere in the week* — not
"no good window", none. Tamping needs ~3 hours; on this section the largest
natural gap is about 70 minutes on the busy line. Run the optimiser on the raw
timetable and it returns a plan made entirely of 45-minute ultrasonic testing
runs: feasible, provably optimal, and useless, because the track geometry work
never happens.

**2. So the system places the maintenance corridor itself.** For every day,
line, and contiguous span it scores every candidate 3½-hour window by the
traffic it would displace, takes the cheapest, rotates across the section over
the week so the whole 129 km gets maintained, and re-timetables the affected
trains around it in priority order. Nobody told it when. It chose
**10:30–16:00 on five days of seven** — the same midday window the Railway
Board's corridor-block policy targets. It displaces 1–4 trains per window.

**3. The policy is a bigger lever than the solver — and we say so.** Of the
2.1× improvement, the corridor policy contributes about twice what CP-SAT
does. Reporting that honestly is the point of running a four-way ablation
instead of one flattering before/after.

---

## How it works

```
IRPWM periodicities ─┐
                     ├─► maintenance demand ──┐
TGI degradation ─────┤   (job, duration,      │
  │                  │    machine, due, prio) │
  └─► learned model ─┘                        │
      (predict → plan)                        ▼
                                    ┌──────────────────────┐
timetable (data.gov.in schema) ─┐   │  corridor placement  │
OSM topology                    ├──►│  min displaced       │
synthetic freight paths ────────┘   │  traffic, rotating   │
                                    └──────────┬───────────┘
                                               ▼
                                    ┌──────────────────────┐
                                    │  CP-SAT block plan   │
                                    │  + bounded retiming  │
                                    └──────────┬───────────┘
                                               ▼
                             independent validator → simulator → KPIs → UI
```

The core is **optimisation**. The AI layer **feeds** it. The simulator
**proves** it. Those three roles stay distinct, and none of them is called
"AI" to make a slide look better.

### The optimiser

Jobs are optional intervals. A block must not overlap any train on its line
stretch; one machine unit does one job at a time plus a repositioning buffer;
daylight-only activities stay in daylight; the section has a daily cap on
block hours; divisional machine outturn targets must be met; and blocks the
planner has locked are fixed while everything else replans around them.

Two encoding choices make it fast enough to re-solve live on stage:

- **Immovable traffic is a domain, not an interval.** A train we may not move
  is a hole in the calendar, not a scheduling decision. Encoding it as a start-
  time domain per job rather than an interval in `NoOverlap` took the model
  from tens of thousands of interval variables to a few hundred, and from
  minutes to seconds.
- **Retiming is targeted.** Freeing all 168 goods paths makes the search space
  enormous and the solver finds nothing useful in a demo-length budget. So a
  cheap pre-pass in plain Python asks *which windows are long enough overall
  but chopped into unusable pieces by a handful of goods paths* — and only
  those become variables. It is also how the answer gets explained to a control
  office: *"shift these two goods trains and you have a 3-hour window at
  Bharuch–Ankleshwar on Wednesday."*

### The AI layer, stated honestly

A GradientBoosting surrogate predicts **days until a segment's track geometry
crosses the intervention threshold**, from features a Permanent Way Inspector
actually has: tonnage, age, curvature, days since tamping, current TGI. Held
out: **MAE ≈ 19 days against ≈ 78 days for a naive mean — 3.9× better,
R² ≈ 0.90.** Those predictions move the due dates the optimiser plans against,
which is the predict-then-plan pipeline the problem statement asks for.

That error is measured against our own degradation process, not against IR's
Track Recording Car data, which we have never seen. It is a surrogate, and
calling it anything grander would be the kind of claim that collapses in Q&A.

There is no reinforcement learning here. "Constraint optimisation fed by
predictive models" is the technically correct description.

### The simulator

Replays the week with the two things the optimiser assumes away: **block
bursts** (work overruns, more often on long jobs in tight windows; an overrun
past an hour is called off and the site handed back) and **knock-on delay**
(a train meeting an overrunning block is held, and only where there is
actually a loop).

It is controlled: **with no blocks planned, simulated delay is exactly zero.**
Every delay minute in the results is therefore attributable to maintenance,
which is what makes the punctuality numbers mean anything.

---

## Repository layout

```
block-planner/
  data/         section.yaml · norms.yaml (IRPWM) · timetable.csv
  ingest/       network.py · timetable.py · freight_synth.py · corridor.py
  assets/       degradation.py · demand_generator.py
  optimizer/    cpsat.py · greedy.py · retiming.py · validate.py
  sim/          simulator.py · kpis.py
  api/          FastAPI backend
  ui/           time–distance chart, machine Gantt, KPIs (no build step)
  experiments/  sensitivity.py · rolling.py
  tests/        pytest
  run_pipeline.py
```

`ui/` is deliberately dependency-free — hand-drawn SVG, no CDN, no build step,
nothing to fail on demo day.

---

## Real vs simulated

**[DATA.md](DATA.md)** carries the full layer-by-layer inventory, every named
assumption with its basis, and what this system cannot tell you. The short
version:

| | |
|---|---|
| **Real** | section, stations, chainage, topology, timetable *schema*, IRPWM periodicities |
| **Simulated (labelled)** | freight paths, maintenance demand, asset condition |
| **Computed** | corridor placement, block plan, KPIs |

Swapping in the real data.gov.in timetable is a one-file change: the generator
writes the same columns, and exists only to produce that file when it is
absent.

---

## Demo in five moves

1. **The chart.** Open the UI. Distance down, time across, trains as diagonals
   — the chart a control office reads. Blue dashes are the reserved corridor;
   shaded boxes are the blocks inside it.
2. **The problem.** Turn the corridor off and re-optimise. The plan collapses
   to ultrasonic testing; 140 jobs have nowhere to go.
3. **The answer.** Turn it back on. Point out that the 10:30–16:00 placement
   was chosen, not configured.
4. **Break something.** Take `CSM-1` out, push traffic to 1.2×, re-optimise.
   It replans in seconds and the KPI panel shows exactly what it cost.
5. **The honest slide.** The provenance table at the bottom of the page, and
   the four-way ablation showing the policy beat the solver.

---

## Limitations

- One section. Divisional scale-up is a roadmap item, not a claim.
- Ballast deep screening and rail grinding need mega-blocks longer than a daily
  corridor, so they stay unscheduled and are reported as such rather than
  quietly dropped.
- Machine repositioning is a fixed buffer, not distance-dependent. It barely
  binds here because blocks land inside corridor windows on a ~35 km span, but
  at divisional scale it would need sequence-dependent transitions.
- Bounded goods retiming adds little *once the corridor is placed well*. On
  this section its effect sits inside the burst simulation's own noise band,
  so the pipeline says so rather than quoting whichever run came out ahead.
  It earns its keep mainly without the corridor policy.
- Optimising the mean is not managing the tail: an early version posted a
  higher mean TGI than the greedy baseline while leaving more segments in the
  urgent band. Fixed by pricing urgent segments explicitly; the residual is one
  segment at week 10.

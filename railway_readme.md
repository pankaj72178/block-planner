# AI-Powered Automatic Block Planning for Indian Railways
## README — Data Collection & Model Building Guide

Companion guide for the SIH 2026 problem statement (Ministry of Railways): *"AI-Powered Automatic Block Planning to Maximize Asset Availability for Train Operations on Indian Railways."* Same format as the flood-nowcasting README, answering two questions:

1. **Part A — Where the data comes from** (what is real and free, what must be simulated, and how to simulate it credibly)
2. **Part B — How to build the model** (an optimization core + an AI layer + a simulator to prove the gains)

> **Ground rule:** unlike the flood project, this system needs **no live feeds at all**. Everything runs offline from cached datasets — your demo cannot break on judging day. Download and freeze all data early, and read the full PS text on sih.gov.in for the exact expected deliverables.

---

## 0. Terminology first (judges will use these words)

| Term | Meaning |
|---|---|
| **Block section** | Signaling segment between two adjacent block stations — only one train at a time. *Not* what this PS is about, but know the difference. |
| **Traffic block / possession** ("block" in this PS) | A planned time window when a line is closed to trains so maintenance can work on it. |
| **Power block** | OHE (overhead electric) switched off for electrical work — often taken together with a traffic block. |
| **Corridor / integrated block** | A fixed daily maintenance window (Railway Board has pushed ~3-hour corridor blocks on trunk routes) bundling multiple departments' work in one closure. |
| **Block burst** | Work overruns the granted window and delays trains — the planner's nightmare; buffers exist to prevent this. |
| **GMT** | Gross Million Tonnes per year carried by a route — drives maintenance frequency. |
| **TGI / TRC** | Track Geometry Index, measured by the Track Recording Car — IR's track-health score. |

## 0.1 First decision: pick ONE section

Like picking one pilot city in the flood project. Choose a **double-line, mixed-traffic trunk-route section, roughly 80–150 km with 8–15 stations** (e.g., a stretch of the Delhi–Mumbai, Howrah–Delhi, or Chennai–Bengaluru corridor). Double line + heavy traffic is where block planning is genuinely hard, so the optimizer has something to show off. Model one section deeply; mention divisional scale-up in the roadmap.

---

# PART A — DATA COLLECTION

## A1. Train timetable data — ✅ real & free

| Source | What you get | Notes |
|---|---|---|
| **data.gov.in — Indian Railways time-table dataset** | Train number, name, station codes, arrival/departure times, distances for all scheduled trains | The core dataset. May be a few years old — fine for a planning demo |
| **Kaggle mirrors** ("Indian Railways train schedule") | Same data, cleaner CSVs | Fastest to start with |
| NTES / enquiry.indianrail.gov.in, "Trains at a Glance" PDF | Cross-checking individual trains | Manual verification only — no official free API |

**Processing:** filter to every train that traverses your chosen section → build a train × station × time table → convert to **fixed occupancy intervals per track segment** (this becomes the constraint set in Part B).

**Critical gap — freight:** freight trains run on unpublished paths, and on trunk routes they are a large share of traffic. **Simulate freight paths** (e.g., 15–30 synthetic freight paths/day inserted into timetable gaps at 50–60 km/h) and label them as simulated. Ignoring freight would be the fastest way to lose a railway judge.

## A2. Network / infrastructure topology — ✅ real & free

- **OpenStreetMap** — Indian rail is well mapped: track geometry, number of lines, electrification, gauge, stations, loops.

```
[out:json][timeout:120];
(
  way["railway"="rail"]({{bbox}});
  node["railway"="station"]({{bbox}});
);
out geom;
```

  or `osmnx.features_from_bbox(bbox, tags={"railway": ["rail", "station"]})`.
- **data.gov.in** station lists; zone/division structure is public.
- Sectional speeds and line-capacity figures appear in public Railway Board documents and the IR Year Book — use for realism parameters.
- Where loop-line data is missing in OSM, assume crossing/overtaking loops at every 2nd–3rd station and document the assumption.

## A3. Maintenance norms → synthetic demand — ✅ norms are public, demands are generated

The actual list of pending maintenance jobs is internal to IR — but the **rules that generate it are published**, which is what makes credible simulation possible:

- **IRPWM (Indian Railways Permanent Way Manual)** — free PDF. Gives maintenance periodicities: tamping cycles by GMT, deep screening cycles, USFD (ultrasonic rail testing) frequency by route class, destressing, etc. Encode these in a `norms.yaml`.
- Track machine types are public knowledge (tamping machines/CSM, BCM for ballast cleaning, rail-grinding trains, USFD trolleys/SPURT car).

**Synthetic demand generator (the analogue of the flood project's synthetic drainage):**
1. Split the section into ~1 km asset segments per line.
2. Assign each segment an age, cumulative GMT, and last-maintenance dates (randomized within realistic ranges).
3. Apply IRPWM periodicities → due dates per activity per segment.
4. Priority = f(days overdue, route criticality, TGI from §A4).
5. Each job gets a duration + machine type from an assumptions table like:

| Activity | Assumed block need | Machine |
|---|---|---|
| Plain-track tamping | 2–4 h traffic block | CSM tamping machine |
| Deep screening | 4–6 h | BCM |
| USFD testing | Trolley in traffic gaps or short blocks | USFD trolley |
| OHE maintenance | 2–3 h power + traffic block | OHE tower wagon |
| Destressing / welding | 1.5–3 h | Gangs + equipment |

Document every number as an assumption calibrated to IRPWM — that framing converts a guess into a methodology.

## A4. Asset condition data — ❌ internal → simulate with a degradation model

Real sources (TMS/IRTMMS records, TRC runs, USFD defect logs) are not public. Simulate instead — and make the simulation *useful*:

- Model **TGI degradation** per segment as a function of GMT carried + time since last tamping + noise + occasional shock events (monsoon damage).
- Crossing a TGI threshold spawns a maintenance demand.
- This is deliberate: it gives the ML layer in §B3 something real to predict, so "AI-powered" is earned rather than decorative.

## A5. Historical delay / punctuality data — ⚠️ partial

Community-scraped NTES running-status datasets exist on Kaggle (quality varies). Use them only to calibrate the simulator's delay distributions — they are not core to the build. Official punctuality statistics in the IR Year Book give sanity-check baselines.

## A6. What you cannot get — and the one-slide honest answer

Block registers, actual block grant/burst statistics, the working timetable (with freight paths), machine rosters, and control-office charts are all internal. Say so on one slide: *"Framework data (timetables, network, norms) is real; operational state (demands, asset condition, freight paths) is simulated from published IR norms."* Same credibility strategy that the flood PRD §7 uses — judges reward it.

## A7. Data inventory checklist (`DATA.md` in repo)

| Dataset | Source | Real / simulated | Status |
|---|---|---|---|
| Passenger timetable | data.gov.in / Kaggle | Real | |
| Freight paths | Generated in gaps | Simulated (labeled) | |
| Network topology, stations, loops | OSM + assumptions | Real + assumption | |
| Maintenance norms | IRPWM → norms.yaml | Real (published) | |
| Maintenance demand list | Generator (§A3) | Simulated (labeled) | |
| Asset condition (TGI) | Degradation model (§A4) | Simulated (labeled) | |
| Delay distributions | Kaggle NTES scrapes / IR Year Book | Real-ish calibration | |

---

# PART B — BUILDING THE MODEL

## B0. Architecture overview

```
[Degradation model (AI)]──► predicted TGI / time-to-threshold per asset segment
        │
        ▼
[Demand generator]──► maintenance jobs (segment, duration, machine,
        │             due date, priority)
        ▼
[Block Planning Optimizer  ·  OR-Tools CP-SAT]
   fixed train occupancy intervals + job intervals + machine constraints
        │
        ▼
[Discrete-event simulator]──► KPIs: maintenance done, delays, availability
        │
        ▼
[Planner UI]  Gantt / time-distance chart · approve & lock · re-optimize
```

The core is **optimization**, the AI layer **feeds** it, and the simulator **proves** it. Keep those three roles distinct when you present.

## B1. Problem formulation (write this down before coding)

**Entities**
- *Track segments*: per line (UP/DOWN), ~1 km granularity, grouped into blockable stretches between stations.
- *Trains*: fixed occupancy intervals per segment from the timetable (+ synthetic freight).
- *Jobs*: from §A3 — segment, duration, machine type, due date, priority.
- *Machines*: limited units per type; one job at a time; repositioning buffer between distant worksites.

**Constraints**
1. A job's interval must not overlap any train interval on the same line segment (the heart of the model).
2. Contiguous window ≥ job duration, plus safety buffers before/after (e.g., 10 min each side).
3. Machine capacity: no-overlap per machine + fixed repositioning buffer (keep it a constant for the hackathon).
4. Time-of-day rules: some activities daylight-only; optional fixed corridor-block window as a policy scenario.
5. Max block hours per section per day (operations tolerance).

**Objective (v1):** maximize Σ (priority × job scheduled) — i.e., get the most important maintenance done inside the existing timetable. **v2** adds a train-retiming penalty term (§B2, Level 2).

**Define the headline metric** — e.g. *Asset Availability Index = 1 − (overdue asset-km-days ÷ total asset-km-days)*. Judges need one number that moves.

## B2. The solver ladder (build in this order)

| Level | Method | Effort | Role |
|---|---|---|---|
| 0 | **Greedy baseline** — sort jobs by priority/due date, insert into the largest natural timetable gaps | Half a day | The "current manual practice" you beat; never delete it |
| 1 | **CP-SAT scheduling** — jobs as optional intervals, `AddNoOverlap` against fixed train intervals per segment and per machine | 1–2 days | **The core deliverable** |
| 2 | **Bounded retiming** — allow ±10–15 min shifts to selected trains (freight freely, a few passenger trains with penalty) to unlock bigger block windows | +1 day | The wow factor: "shifting 2 freight paths by 12 min unlocked a 3 h corridor block" |
| 3 | Rolling weekly horizon (plan 7 days, freeze day 1, replan daily); RL | Stretch | Mention in roadmap; do not build RL in 36 h |

**CP-SAT core sketch:**

```python
from ortools.sat.python import cp_model
m = cp_model.CpModel()
H = 7 * 24 * 60                      # one planning week, in minutes

ivs, pres = {}, {}
for j in jobs:                        # each maintenance job = optional interval
    s = m.NewIntVar(0, H - j.dur, f's{j.id}')
    p = m.NewBoolVar(f'p{j.id}')
    ivs[j.id] = m.NewOptionalIntervalVar(s, j.dur + 2*BUFFER, s + j.dur + 2*BUFFER, p, f'i{j.id}')
    pres[j.id] = p

for seg in segments:                  # trains are FIXED intervals; jobs must dodge them
    m.AddNoOverlap(seg.train_intervals + [ivs[j.id] for j in seg.jobs])

for mc in machines:                   # one machine, one job at a time
    m.AddNoOverlap([ivs[j.id] for j in mc.jobs])

m.Maximize(sum(j.priority * pres[j.id] for j in jobs))
solver = cp_model.CpSolver(); solver.parameters.max_time_in_seconds = 60
solver.Solve(m)
```

A one-section weekly model (≈100 trains × segments, 50–150 jobs) solves in seconds to a minute — perfect live-demo material ("watch it replan when I break a machine").

## B3. The AI layer (what makes it "AI-powered", honestly)

1. **Predictive maintenance demand:** LightGBM/scikit-learn model predicting TGI degradation / time-to-threshold from the synthetic features of §A4 (GMT, age, days since tamping, monsoon flag). Predicted due dates flow into the optimizer → the *predict → plan* pipeline is the PS's "maximize asset availability" story.
2. **Delay-cost estimator:** quick simulator rollout (or a small regression trained on rollouts) estimating the delay cost of a candidate block placement → used as the retiming penalty in Level 2.
3. *(Optional garnish)* Block-burst risk score per plan slot from job type + window tightness. Keep tiny.

Do **not** oversell reinforcement learning. "Constraint optimization fed by predictive models" is the technically correct and defensible framing.

## B4. Simulation & evaluation — how you prove the gains

Build a discrete-event simulator (SimPy or a simple time-step loop): replay timetable + planned blocks, enforce headways, let delayed trains cascade, allow overtakes at loop stations (simplified).

**Report, for Greedy vs CP-SAT vs CP-SAT+retiming over a simulated month:**
- % of due maintenance completed (weighted by priority)
- Asset Availability Index (§B1) and backlog trend
- Average / 95th-percentile added train delay, punctuality %
- Block hours granted vs. demanded

**Sensitivity tests (one slide):** traffic +20%, one machine down, monsoon speed restrictions — show the planner adapts. Since no external ground truth exists (§A6), *internal consistency + sensitivity + baseline comparison* is your validation story; say that explicitly.

## B5. Product layer

- **FastAPI**: `/plan`, `/schedule`, `/kpis`, `/lock`.
- **React frontend** with two views:
  1. **Time–distance chart** (the classic railway control chart: distance on Y, time on X, trains as diagonal lines) with block windows overlaid as shaded boxes — railway judges live in this chart; building it earns instant credibility.
  2. Machine-wise Gantt + KPI cards + before/after toggle.
- **Human-in-the-loop:** planner drags/locks a block → solver replans around locked decisions (CP-SAT re-solve with fixed intervals; seconds).

---

## C. Suggested repository layout

```
block-planner/
  data/            # timetable, OSM extract, norms.yaml, DATA.md manifest
  ingest/          # timetable_parser.py, osm_network.py, freight_synth.py
  assets/          # degradation.py (TGI model), demand_generator.py
  optimizer/       # cpsat_model.py, greedy_baseline.py, retiming.py
  sim/             # simulator.py, delay_model.py, kpis.py
  api/             # FastAPI app
  ui/              # React: time-distance chart, Gantt, KPI dashboard
  experiments/     # scenario configs, sensitivity runs
  notebooks/
```

## D. Build order

| Day | Deliverable |
|---|---|
| 1 | Section chosen; timetable parsed to segment-occupancy intervals; OSM network + loops; freight paths generated |
| 2 | norms.yaml + degradation model + demand generator → realistic job list; greedy baseline running |
| 3 | CP-SAT model solving; first optimized weekly plan; KPI script |
| 4 | Simulator + before/after KPIs; time-distance chart UI with blocks overlaid |
| 5 | Bounded retiming (Level 2), lock-and-replan interaction, sensitivity runs, honesty slide, demo script |

**36-hour SIH-final compression:** do Days 1–2 as pre-work (allowed — data prep and design before the event); hours 0–12 = optimizer, 12–24 = simulator + UI, 24–36 = retiming + polish + pitch.

## E. Honest-demo slide (real vs simulated)

| Layer | Status |
|---|---|
| Passenger timetable, network, stations | **Real** (data.gov.in, OSM) |
| Maintenance norms & periodicities | **Real** (IRPWM, published) |
| Freight paths | **Simulated** in timetable gaps (labeled) |
| Maintenance demand & asset condition | **Simulated** from published norms + degradation model (labeled) |
| Block plans & KPIs | Computed live by the optimizer + simulator |

## F. Library shortlist

`ortools` (CP-SAT) · `pandas` · `networkx` · `osmnx` · `simpy` · `scikit-learn` / `lightgbm` · `fastapi` · React + `d3`/`visx` (time-distance chart) or a Gantt lib · `matplotlib` for experiment plots.

**Final reminders:** keep the greedy baseline in every result table, define the Asset Availability Index on slide one, build the time-distance chart, and label every simulated layer. The pitch in one line: *"Real timetable, published maintenance rules, and a constraint solver that finds the block plan a control office can't compute by hand."*
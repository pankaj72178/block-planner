# FEATURES.md — what every part does, and how it answers the problem statement

Companion to [README.md](README.md) (results), [ARCHITECTURE.md](ARCHITECTURE.md)
(how it is built) and [ROADMAP.md](ROADMAP.md) (what is still missing).

This file exists to answer one question a judge will ask in some form within
the first two minutes:

> *"You have built a lot of things. Which of them is the answer to our problem
> statement, and how do you know?"*

---

## 0. The problem statement, taken apart

> **"AI-Powered Automatic Block Planning to Maximize Asset Availability for
> Train Operations on Indian Railways."**
> — Ministry of Railways, SIH 2026

Six words in that title are load-bearing. Each one is a demand, and each demand
has a feature that answers it and a number that proves it.

| # | The demand | What it actually means operationally |
|---|---|---|
| **D1** | **Block Planning** | Decide which maintenance work gets a traffic block: what, when, where, on which machine |
| **D2** | **Automatic** | The plan comes out without a human placing windows by hand |
| **D3** | **AI-Powered** | Something is *learned*, and the learning changes the plan — not a label on a dashboard |
| **D4** | **Maximize Asset Availability** | An objective and a metric, both about availability, and they must be the same thing |
| **D5** | **for Train Operations** | Punctuality is a constraint, not an afterthought. A plan that maintains the track by wrecking the timetable has failed |
| **D6** | **on Indian Railways** | IR's rules, IR's units, IR's vocabulary. IRPWM periodicities, TGI, GMT, corridor blocks, block bursts |

The rest of this file walks the features and ties each back to these.

---

## 1. Feature by feature

### 1.1 Section model — the railway itself

**Files:** `data/section.yaml`, `ingest/network.py`
**Answers:** D6

Vadodara Jn → Surat: **13 stations, 129 km, double line, ~66 trains/day.** A
real, named, heavily-loaded stretch of the Delhi–Mumbai trunk route, chosen
because block planning is only hard where traffic is dense.

Three levels of granularity, because the problem needs all three:

| Object | Count | What it is | Used for |
|---|---|---|---|
| `Station` | 13 | code, name, chainage, **whether it has a crossing loop** | where a train can be held |
| `Stretch` | 24 | one inter-station segment **per line** (`BH-ANK/DN`) | the unit a block is taken on |
| `AssetSegment` | 258 | ~1 km of track on one line | the unit that ages and needs maintenance |

The loop flag matters more than it looks. A train cannot be held on a running
line between two block stations, so when the simulator delays a train it has to
push the hold back to the last station that actually has a loop.

> **Real vs assumed:** stations, codes and chainage are real (public IR
> time-tables, OSM). Loop availability is an assumption where OSM is
> incomplete. `fetch_osm_topology()` holds the live Overpass query and is
> deliberately *not* called by the pipeline — the demo runs fully offline and
> cannot die on a network timeout.

---

### 1.2 Timetable ingest — the constraint set

**File:** `ingest/timetable.py` (432 lines, the largest module)
**Answers:** D1, D6

Turns a timetable into the thing the optimiser dodges: **fixed `[t_in, t_out]`
occupancy intervals per stretch**, padded by half the sectional headway on each
side, because signalling needs clearance either side of a movement.

Two modes, one code path:

- **Real** — drop the data.gov.in / Kaggle CSV at `data/timetable.csv`.
- **Synthetic** — if that file is absent, generate one *in exactly the same
  schema* and write it there.

So swapping to real data is overwriting one file. Nothing else changes. That is
not an accident of design, it is the point of the design.

**The non-obvious part — `regularise()`.** Independently sampled train paths
are not a timetable; they conflict. A real timetable is conflict-free by
construction, so this threads trains onto the section **in priority order**
(premium first), each taking the earliest slot at or after its nominal time
that clears everything already threaded. Lower-priority trains give way. That
is control-office logic, and it converges by construction — no
iterate-until-it-settles loop.

It matters beyond tidiness: the Level 2 retiming model puts every train
interval into one `NoOverlap`, so a base timetable that already violates
headway is infeasible before the solver starts.

> **Result:** 0 headway conflicts, verified in `tests/test_planner.py`.

---

### 1.3 Freight synthesis — the traffic nobody publishes

**File:** `ingest/freight_synth.py`
**Answers:** D6

Freight runs on unpublished paths and is **36% of traffic** on this section. A
block plan built only against the published passenger timetable would look
wonderfully feasible and be completely wrong.

The generator does what a section controller does: pick a departure, walk the
section at freight speed, and where the next stretch is occupied, **hold in a
loop — but only at stations that have one**. If the path cannot be threaded,
abandon it and try another slot; if the hold is needed where there is no loop,
keep the train at the originating yard longer instead.

> **Output:** 24 goods paths/day, threaded into genuine gaps, held in loops for
> overtakes, labelled SIMULATED everywhere they surface.

---

### 1.4 Asset condition + the learned model

**File:** `assets/degradation.py`
**Answers:** **D3** — this is where "AI-Powered" is earned

Real condition data (TMS/IRTMMS, Track Recording Car runs, USFD defect logs) is
internal to IR. So it is generated — but from a *physical process*, not noise:

```
tonnage carried + asset age + curvature + time since tamping + monsoon shocks
        ↓
   TGI history per 1 km segment
        ↓
   observable features  →  learned surrogate (GradientBoosting)
        ↓
   predicted days until TGI crosses the intervention threshold
        ↓
   that prediction MOVES THE DUE DATE the optimiser plans against
```

The last arrow is what separates this from a decorative ML box. The model's
output is an input to the optimiser — a **predict-then-plan pipeline**.

| Metric | Value |
|---|---|
| Held-out MAE | **19.6 days** |
| Naive-mean baseline MAE | 77.3 days |
| Improvement | **3.94×** |
| R² | **0.902** |

Features are the five things a Permanent Way Inspector can actually look up:
GMT/year, age, curvature, days since tamping, current TGI, cumulative GMT.

> **Stated honestly:** that error is measured against *our own* degradation
> process, not against IR's TRC data, which we have never seen. It is a
> surrogate. Trained on real TRC history it would be a different model with a
> different error. There is no reinforcement learning here — "constraint
> optimisation fed by predictive models" is the technically correct
> description.

---

### 1.5 Demand generator — reconstructing the job list

**File:** `assets/demand_generator.py`
**Answers:** D1, D6

The pending-work register is internal to IR. **The rules that produce it are
published.** So the register is reconstructed rather than guessed:

```
IRPWM periodicity  +  last-done date   →  calendar due date
learned degradation model              →  condition-based due date
min(the two)                           →  the date the optimiser plans to
```

Two due-date sources is the whole idea. Calendar-only maintenance is what IR
largely does today; the condition-based date is what *maximise asset
availability* actually needs, and it is why a segment carrying 60 GMT on a
curve gets pulled forward while a straight, lightly-loaded one gets pushed back.

The condition date can only **pull work forward, never defer past the manual's
periodicity** — IRPWM is a floor, not a suggestion.

Seven activities, each with its own periodicity band by GMT, block duration,
machine type, daylight rule and availability value:

| Activity | Machine | Block | Availability value /km |
|---|---|---|---|
| Plain-track tamping | CSM | 110–170 min | 1.0 |
| Ballast deep screening | BCM | 240–360 min | 1.2 |
| Ultrasonic flaw detection | USFD trolley | 45–90 min | 0.08 |
| LWR destressing | Gang | 90–180 min | 0.6 |
| Rail grinding | RGM | 180–240 min | 0.4 |
| OHE maintenance | Tower wagon | 120–180 min | 0.3 |
| Points & crossings | Gang | 120–200 min | 0.9 |

> **`availability_value_per_km` is load-bearing, not decoration.** Ultrasonic
> testing covers 6 km per block versus tamping's 1.5. Under any raw-kilometre
> objective the optimiser correctly concludes USFD is the best buy and spends
> the entire week on it — a provably optimal plan in which no track geometry is
> ever corrected. Testing *finds* defects; geometry work is what lifts speed
> restrictions. Pricing that difference is what keeps the plan sane.

**Priority** blends three 0–10 signals rather than stacking bonuses (which
saturates at 10 and silently degenerates into "do as many jobs as possible"):
criticality (what the manual says the activity is worth) 45%, urgency (how far
past due) 30%, condition (what the asset is saying) 25%.

> **Output:** 200 candidate jobs, all overdue, **590 block-hours demanded
> against 56 hours of operations tolerance — 10.5× oversubscribed.** That is
> not a modelling artefact. That is the problem.

---

### 1.6 Corridor block placement — the feature that changed the project

**File:** `ingest/corridor.py`
**Answers:** **D2** (this is the "Automatic"), D4, D6

**The finding that forced this feature into existence:**

Run the optimiser against the published timetable and **140 of 200 jobs have no
feasible window anywhere in the week.** Not "no good window" — none. Tamping
needs ~3 hours; the largest natural gap on the busy line is about 70 minutes.
The optimiser returns a plan made *entirely* of 45-minute ultrasonic runs:
feasible, provably optimal, and useless, because the geometry work never
happens and the backlog grows for ever.

That is not our bug. It is the actual reason the Railway Board pushed fixed
daily **corridor blocks** onto trunk routes: you cannot schedule a track
machine into gaps that do not exist, so you create the gap by timetabling
around it.

So the system does what a Chief Operations Manager does:

1. For every **day × line × contiguous span** of the section, score every
   candidate 3½-hour window by the traffic it would displace
   (Σ train priority × minutes each train must move).
2. Take the cheapest — with a **rotation penalty** on spans already used, so
   the week's blocks cover the whole 129 km instead of closing the same 33 km
   five days running.
3. Re-timetable the affected trains around it, in priority order, by seeding
   the reserved window as occupancy and re-threading (§1.2).

**Nobody configured the hour.** It chose:

| Day | Line | Span | km | Window | Trains re-timetabled |
|---|---|---|---|---|---|
| D1 | UP | BRC–MYG | 0–33 | 11:00–14:30 | 2 |
| D2 | DN | BRC–MYG | 0–33 | 12:00–15:30 | 2 |
| D3 | DN | KSB–ST | 95–129 | 13:00–16:30 | 4 |
| D4 | DN | MKPR–PLJ | 6–46 | 12:00–15:30 | 3 |
| D5 | DN | ANK–UTN | 80–124 | 12:00–15:30 | 4 |
| D6 | UP | KSB–ST | 95–129 | 12:30–16:00 | 2 |
| D7 | DN | VRA–BH | 13–70 | 23:30–03:00 | 1 |

**10:30–16:00 on five days of seven** — the same midday window Railway Board's
corridor policy targets, found from traffic data alone, displacing 1–4 trains
each. Five different spans across the week.

> A test asserts the reserved windows are genuinely train-free and that the
> corridor rotates across at least three spans. The hole has to be real, not
> drawn on the chart.

---

### 1.7 The optimiser — CP-SAT

**File:** `optimizer/cpsat.py` (475 lines, the core deliverable)
**Answers:** D1, D4

Jobs are **optional intervals**. The solver decides: do this job this week? when?
on which machine unit?

**Constraints**

| # | Constraint | Why it is there |
|---|---|---|
| 1 | A block must not overlap any train on the same line stretch | the heart of the model |
| 2 | Contiguous window ≥ duration + 10 min safety buffer each side | you cannot start work with a train still clearing |
| 3 | One machine unit, one job at a time, plus a repositioning buffer | 13 units across 6 types |
| 4 | Daylight-only activities inside 06:00–18:00, no straddling midnight | USFD, OHE, destressing, P&C |
| 5 | ≤ 8 block-hours per day, section-wide | operations tolerance |
| 6 | Divisional machine outturn targets must be met | §1.9 |
| 7 | Locked blocks are fixed; everything else replans around them | human in the loop |

**Objective — and this is the part that took longest to get right**

```
maximise   Σ [ 100 × availability_value × (days_remaining_after_the_block)
             + 1200 if the segment is in the urgent TGI band
             +   20 × priority ]
         − retiming penalty
```

Availability dominates. Priority enters only as an urgency tiebreak. The
objective **is** the reported KPI — the same weighted asset-km-days that
`sim/kpis.asset_availability()` measures.

> **Why that matters, learned expensively:** an earlier version had priority
> dominant and availability as a nudge. It proved `OPTIMAL` and *still lost to
> the greedy baseline* on the metric the report printed. A solver pointed at
> the wrong function is the most expensive kind of bug to find late, and
> `tests/test_planner.py::test_cpsat_beats_greedy_on_its_own_objective` now
> exists so it cannot come back.

**Two encoding choices make it fast enough to re-solve live on stage**

| Trick | What it replaces | Effect |
|---|---|---|
| **Immovable traffic as a start-time *domain*** (`free_start_domain`) | thousands of fixed train intervals inside `NoOverlap` | tens of thousands of interval variables → a few hundred; minutes → seconds |
| **Retiming offsets as a domain** (`feasible_offsets`) | fixed trains re-added to `NoOverlap` in Level 2 | halves the Level 2 model |

The insight behind both: *a train you are not allowed to move is not a
scheduling decision, it is a hole in the calendar.* Holes belong in domains.

> **Result:** proves `OPTIMAL` in ~6 s on the corridor scenario.

---

### 1.8 Bounded retiming — Level 2

**File:** `optimizer/retiming.py`
**Answers:** D1, D5

Freeing all 168 goods paths makes the search space enormous and the solver
finds nothing useful in a demo-length budget. So a cheap pre-pass in plain
Python asks a sharper question first:

> *Which windows are long enough overall, but chopped into unusable pieces by a
> small number of goods paths?*

Only those paths become variables. It is also how the answer gets explained to
a control office, which does not want to hear about a MIP: *"shift these two
goods trains by 14 and 21 minutes and you have a 3-hour window at
Bharuch–Ankleshwar on Wednesday."*

Shift limits are per class, and they are a policy statement:

| Class | Max shift | Penalty weight | Rationale |
|---|---|---|---|
| Goods | ±90 min | 1 | already held up to 75 min in loops for overtakes |
| Passenger (commuter) | ±10 min | 12 | commuters notice |
| Express | ±15 min | 8 | expensive but possible |
| **Premium (Rajdhani/Shatabdi/VB)** | **0** | — | **nobody re-paths a Rajdhani to tamp track** |

Because every train interval — shifted or fixed — sits in the same per-stretch
`NoOverlap`, the retimed result is **still a conflict-free timetable**, not a
plan that assumed trains would evaporate.

> **Honest finding:** once the corridor is placed well, retiming has little
> left to unlock on this section — its effect sits inside the burst
> simulation's own noise band, and the pipeline prints that instead of quoting
> whichever run came out ahead. It earns its keep mainly *without* the corridor
> policy.

---

### 1.9 Machine outturn targets — why the plan is not all one activity

**File:** `data/norms.yaml` → `outturn_targets`, enforced in both planners
**Answers:** D6

IR divisions are measured on **machine outturn** (km tamped, km screened) as
well as on punctuality. A block plan that quietly spent the whole week on the
cheapest activity would be rejected by the Sr.DEN before it reached the control
office.

| Machine | Units | Minimum blocks/unit/week |
|---|---|---|
| CSM (tamping) | 2 | 2 |
| USFD trolley | 3 | 2 |
| Gang | 4 | 1 |
| OHE tower wagon | 2 | 1 |
| BCM, RGM | 1 each | **0** |

BCM and RGM are set to zero deliberately: ballast screening and rail grinding
need mega-blocks longer than a daily corridor, and pretending otherwise would
be the dishonest kind of feasible.

Targets apply to **both** planners — otherwise CP-SAT would "win" purely by
being held to a rule the baseline was allowed to ignore. If the targets are
unreachable the solver relaxes them and **says so** rather than returning
`INFEASIBLE`.

---

### 1.10 Independent validator

**File:** `optimizer/validate.py`
**Answers:** D1 — and the question "how do you know?"

A **second implementation** of every constraint, written to re-derive the rules
from the plan and the world rather than reuse the model. A bug in the CP-SAT
encoding cannot hide behind the same bug in the checker.

Every plan in every results table is checked before its numbers are reported.
*"Our optimiser said so"* is not verification.

---

### 1.11 Simulator — proving the plan

**File:** `sim/simulator.py`
**Answers:** **D5**

An optimiser will happily hand you a plan that is feasible on paper. The
simulator replays the week with the two things the optimiser assumes away:

- **Block bursts** — work overruns its window. Probability rises with job
  duration and with how tight the granted window was. An overrun past an hour
  is called off, the site handed back, and **the work does not count as
  completed**.
- **Knock-on delay** — a train meeting an overrunning block is held, and only
  where there is actually a loop; everything behind it inherits that.

**The control that makes the numbers mean anything:** with no blocks planned,
simulated delay is **exactly zero** (asserted in the test suite). Every delay
minute in the results is therefore attributable to maintenance.

**Passenger and goods punctuality are reported separately, on purpose.** A
goods train deliberately regulated at a loop to open a block window is not a
punctuality failure — it is the plan working. Burying that inside one blended
number would flatter Level 2 in exactly the way a railway judge will probe.

---

### 1.12 KPIs — the headline metric

**File:** `sim/kpis.py`
**Answers:** **D4**

```
Asset Availability Index = 1 − Σ(overdue km-days × activity weight)
                               ─────────────────────────────────────
                               Σ(total km-days × activity weight)
```

A kilometre of track on a day where an activity is past due counts as
unavailable-by-standard for that day, weighted by what that activity is worth
in availability terms. Doing the work clears it from the moment the block ends.
It is a proper index: bounded, it moves when the plan is better, and it is
stated in units a PWI recognises.

**Every reported number is a mean over 8 independent block-burst replications.**
A single run is one draw from a stochastic process — whether a 3-hour tamping
block overruns, and whether the train behind it happens to be a Rajdhani, moves
p95 delay by tens of minutes. Comparing two planners on one draw each compares
the dice.

---

### 1.13 The UI

**Files:** `ui/index.html`, `ui/app.js`, `ui/style.css` (610 lines, **no
dependencies**)
**Answers:** D5, D6

- **Time–distance chart** — distance down the page, time across, trains as
  diagonals, block windows as shaded boxes, the reserved corridor as a labelled
  dashed region. This is the chart a control office lives in; putting it on
  screen buys three minutes of goodwill before you say a word.
- **Machine Gantt** — all 13 units across the planning week, with idle time
  visible.
- **KPI cards** including a live **plan-check badge** from the validator.
- **Scenario panel** — traffic multiplier, operations tolerance, solver budget,
  corridor on/off, retiming on/off, and click-to-break any machine unit. Then
  **Re-optimise** solves live.
- **Provenance table** at the bottom of the page: every layer, labelled.

Hand-drawn SVG. No CDN, no build step, nothing to fail on demo day.

---

### 1.14 Experiments

| File | What it answers |
|---|---|
| `experiments/sensitivity.py` | *Does it survive reality?* 8 scenarios: traffic +20%, freight +50%, machines down, tolerance up/down, corridor off |
| `experiments/rolling.py` | *Does it actually maximise availability?* 10-week rolling horizon with backlog burn-down |

---

## 2. The mapping table

| Demand | Feature that answers it | Evidence |
|---|---|---|
| **D1 Block Planning** | CP-SAT model (§1.7) + demand generator (§1.5) + validator (§1.10) | 22 blocks/week, every plan independently verified feasible |
| **D2 Automatic** | Corridor auto-placement (§1.6) | Chose 10:30–16:00 on 5/7 days and 5 different spans, from traffic data alone |
| **D3 AI-Powered** | Learned degradation model (§1.4) feeding due dates into the optimiser | MAE 19.6 d vs 77.3 d naive — 3.94×, R² 0.902 |
| **D4 Maximize Asset Availability** | AAI (§1.12) as both the objective and the KPI | 32.0 → 75.0 overdue asset-km-days recovered (**2.34×**) |
| **D5 for Train Operations** | Headway-padded occupancy, per-class retiming limits, burst simulator (§1.11) | Passenger punctuality **99.49% → 99.83%** — it goes *up* |
| **D6 on Indian Railways** | IRPWM norms, TGI/GMT, corridor blocks, outturn targets, block bursts | `data/norms.yaml` cites the manual; vocabulary is IR's throughout |

---

## 3. The result, in one table

Four planners on **byte-identical inputs**, so any difference is the planner
and not the data. Means over 8 burst replications.

| | manual, no corridor | CP-SAT, no corridor | manual + corridor | CP-SAT + corridor |
|---|---|---|---|---|
| overdue asset-km-days recovered | 32.0 | 47.4 | 63.9 | **75.0** |
| availability-equivalent track | 10.4 km | 14.4 km | 19.6 km | **19.9 km** |
| urgent segments cleared | 0 | 2 | 6 | **6** |
| Asset Availability Index | 0.7316 | 0.7339 | 0.7363 | **0.7379** |
| passenger punctuality | 99.49% | 99.37% | 99.96% | **99.83%** |
| plan feasible | ✅ | ✅ | ✅ | ✅ |

**And over 10 rolling weeks — where it actually shows:**

| after 10 weeks | backlog | mean TGI | urgent segments | availability delivered |
|---|---|---|---|---|
| manual, no corridor | 259 → **447** ▲ | 70.0 → **64.8** ▼ | 24 → **53** | 59.8 (1.00×) |
| manual + corridor | 259 → 341 | 70.0 → 76.4 | 24 → **0** | 166.5 (2.78×) |
| CP-SAT + corridor | 259 → **312** | 70.0 → **77.7** | 24 → 1 | **188.3 (3.15×)** |

Without a reserved corridor the section **deteriorates**. With one it
**recovers**.

---

## 4. Questions a judge will ask

**"Where did you get IR's data?"**
We did not, and we say so on a slide. Framework real, operational state
simulated from published norms, every layer labelled. See
[DATA.md](DATA.md).

**"How do you know the plan is feasible?"**
An independently written validator (`optimizer/validate.py`) re-derives every
constraint from the plan and checks it. Every row in every table is checked
before it is printed.

**"How do you know it's better than what we do today?"**
A greedy baseline on byte-identical inputs, held to the same outturn targets.
Plus a four-way ablation that separates the corridor policy from the solver —
and reports that **the policy contributes about twice what CP-SAT does**.

**"What's actually AI here?"**
A GradientBoosting surrogate predicting time-to-intervention, whose predictions
move the due dates the optimiser plans against. 3.94× better than a naive mean.
Not RL, and we do not claim it.

**"What if a machine breaks / traffic grows?"**
`experiments/sensitivity.py`, 8 scenarios. Every one degrades in the direction
and roughly the magnitude a section engineer would predict — which, absent
external ground truth, is a large part of the validation argument.

**"What can't it do?"**
See [ROADMAP.md](ROADMAP.md). The short answer: one section, no
sequence-dependent machine travel, no single-line working during blocks, and no
operational judgment — that last gap closes with 45 minutes of a serving PWI's
time, not with more data.

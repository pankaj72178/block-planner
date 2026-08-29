# ROADMAP.md — what is still missing, and what to add next

Companion to [README.md](README.md) (results), [FEATURES.md](FEATURES.md) (what
exists) and [ARCHITECTURE.md](ARCHITECTURE.md) (how it is built).

Ordered by **what a judge is most likely to press on**, not by what is most fun
to build. Each item says why it matters, roughly what it costs, and where in the
code it goes.

Effort is in **person-days at hackathon pace**. "36 h" tags mark things that fit
inside a grand-finale build.

---

## P0 — before the finale

Five items. None is a research problem; all of them close a hole someone will
find.

---

### P0.1 · Talk to a real PWI or section controller ⏱ 45 minutes

**The single highest-return item in this document, and it is not code.**

Everything in this repo is derivable from public documents. What is not
derivable is operational judgment:

- why a block is refused for reasons that appear in no constraint list
- how a controller actually negotiates a block with a PWI on the day
- why a "granted" block reliably starts 25 minutes late
- what happens when the machine is on site and the OHE is not switched off

Find one serving or retired PWI, Sr.DEN, or section controller. Ask what makes
a block plan unrealistic. You will get three constraints that are not in the
model, and adding them — *plus citing the conversation on a slide* — does more
for credibility than any additional dataset would.

**Where it lands:** new constraints in `optimizer/cpsat.py` + the mirrored
checks in `optimizer/validate.py`, and new rows in `data/norms.yaml`.

---

### P0.2 · Sequence-dependent machine travel ⏱ 1 day · 36 h

**The gap:** repositioning is a fixed buffer per machine type (45 min for a
CSM). In reality a tamping machine moving from km 5 to km 120 takes hours, on
its own power, occupying the line while it does.

**Why it matters:** it barely binds today *because* blocks land inside corridor
windows on a ~35 km span — but that is an accident of this section, and a judge
who knows machine logistics will ask. It is also the constraint that most
rewards a solver over a human, so it strengthens the CP-SAT case.

**How:** pairwise disjunction with transition time, per machine unit.

```python
travel = abs(km_mid[i] - km_mid[j]) / 30.0 * 60   # track machines move ~30 km/h

before = m.NewBoolVar(f"b_{i}_{j}_{u}")
m.Add(start[j] >= end[i] + travel).OnlyEnforceIf([assign[i,u], assign[j,u], before])
m.Add(start[i] >= end[j] + travel).OnlyEnforceIf([assign[i,u], assign[j,u], before.Not()])
```

**Cost:** ~800 bools per machine type. Watch the solve time; if it hurts, only
create the pair when the two jobs could actually collide given their domains.

**Where:** `optimizer/cpsat.py` after the `per_unit` `NoOverlap`, and a matching
check in `optimizer/validate.py`.

---

### P0.3 · Single-line working during a block ⏱ 1–2 days

**The gap:** when the DN line is blocked, our trains are *re-timetabled around*
the window. Real IR practice on a double line is often **single-line working** —
trains run bidirectionally on the surviving line under a written authority, at
reduced capacity, with crossings at loops.

**Why it matters:** this is the most likely "you have not modelled how we
actually do it" objection. It is also the honest reason our displaced-train
count (1–4 per window) looks low.

**How, in increasing order of realism:**

1. **Cheap and defensible** — reduce the surviving line's effective capacity
   during a corridor window (raise the headway on it, e.g. 6 → 15 min) and let
   `regularise()` absorb the consequence. Half a day, and it makes the cost of a
   corridor honest.
2. **Proper** — a `single_line` mode per stretch during a window: trains from
   both directions share one line, may only cross at loop stations, and the
   simulator enforces it.

Do (1) for the finale, put (2) in the roadmap slide.

**Where:** `ingest/corridor.py` (emit the capacity change), `ingest/timetable.py`
(honour a per-window headway), `sim/simulator.py`.

---

### P0.4 · Mega-blocks for BCM and rail grinding ⏱ half a day · 36 h

**The gap:** ballast deep screening needs 4–6 hours and rail grinding 3–4. The
daily corridor is 3½. So `outturn_targets` sets BCM and RGM to **0**, and the
report says those activities are never scheduled.

That is honest, but it is also a visible hole: the deep-screening backlog just
grows.

**How:** a second corridor class. One **6–8 hour mega-block per week**, placed
by the same auto-placement scorer, most likely overnight or on the thinnest day.
Real divisions plan exactly this way — a weekly or fortnightly mega-block for
machine-intensive work.

```yaml
# data/section.yaml
corridor_block:
  enabled: true
  duration_min: 210
  n_stretches: 4
mega_block:                # NEW
  enabled: true
  per_week: 1
  duration_min: 420
  n_stretches: 3
```

`choose_corridor_windows()` already generalises — it takes `duration_min` and
`n_stretches` as parameters. Call it twice and merge the reserved windows.

**Payoff:** BCM outturn goes from 0 to non-zero, deep screening starts
appearing in the plan, and the rolling backlog curve improves further.

---

### P0.5 · Rolling-horizon freeze-and-replan ⏱ 1 day · 36 h

**The gap:** `experiments/rolling.py` replans each week from scratch. Real
planning is **plan 7 days, freeze day 1, replan daily** — because machines
break, blocks burst, and the demand list changes overnight.

**Why it matters:** it is README B2 Level 3, it is what a control office
actually needs, and the machinery already exists (`locked` blocks in
`solve_cpsat`, `/api/replan`). It is wiring, not new science.

**How:**

```
each day:
    lock every block starting in the next 24 h
    apply what actually happened yesterday (bursts, abandonments)
    regenerate demand from the aged asset state
    re-solve the remaining 6 days around the locks
```

**Payoff:** a genuinely strong demo beat — *"a machine failed overnight; here is
the replan, in eight seconds, with tomorrow's committed blocks untouched."*

**Where:** new `experiments/rolling_daily.py`, plus a "freeze day 1" button in
the UI.

---

## P1 — strong additions if there is time

---

### P1.1 · Divisional scale-up ⏱ 2–3 days

One section is modelled deeply; the claim of scale is currently a roadmap item,
not a result. Take 3–4 adjacent sections of the same division, share the machine
fleet across them, and let the optimiser decide **where to send the CSM this
week** — a genuine divisional allocation problem, and the natural next question
after "does it work on one section?"

Watch the model size: 4 sections ≈ 4× jobs and stretches. The domain encoding
(§5 of ARCHITECTURE) should carry it, but budget time to check.

---

### P1.2 · Block-burst risk score per slot ⏱ half a day · 36 h

The README's own §B3 item 3, and currently the only listed AI component not
built. A small model predicting *overrun probability for this job in this
window*, from job type, duration, window tightness, machine, and time of day —
then surfaced in the UI as a per-block risk badge and, optionally, as a penalty
term in the objective.

Training data is free: the simulator already generates bursts. Keep it tiny; the
value is the explanation, not the accuracy.

---

### P1.3 · Power blocks and OHE dependency ⏱ half a day

`Job.needs_power_block` is populated by the demand generator from
`norms.yaml` and then **never read by anything**. Grep confirms it: two hits,
both writes. OHE work needs the traction supply switched off, which is a separate
authority with its own lead time, and it usually implies a traffic block on both
lines of an electrified section.

**How:** a hard constraint — a job with `needs_power_block` requires the same
window to be free on *both* lines of that stretch, plus a switching lead time
either side.

**Cost:** ~20 lines in `optimizer/cpsat.py` and the mirror in `validate.py`. It
removes a `# TODO`-shaped hole a sharp judge could find by grepping.

---

### P1.4 · Real timetable, actually loaded ⏱ 2 hours

Everything is in place — the generator writes the data.gov.in schema, and
[DATA.md](DATA.md) documents the swap as a one-file change. **Nobody has done
the swap.**

Do it before the finale. Download the dataset, filter to BRC–ST, save as
`data/timetable.csv`, re-run. Then the answer to *"is this real data?"* is
"yes, the timetable is the published one" rather than "it would be if we
downloaded it".

Expect friction: station-code mismatches, `--` in time fields, trains that only
partially traverse the section. `ingest/timetable.py` handles all three, but it
has only ever been tested against its own output.

---

### P1.5 · Explanation layer ⏱ 1 day

The optimiser says *what*. It does not say *why not*. For every unscheduled job
the system already knows the answer — it is in `plan.no_window`, the machine
assignment, and the daily cap — but nothing surfaces it.

> *"J0147 (tamping, km 71–72 DN, priority 8.2) was not scheduled: no window ≥
> 190 min exists on BH-ANK/DN this week. The nearest is 141 min on Tuesday. It
> would fit inside a corridor block on that span."*

**Where:** a new `optimizer/explain.py` and a panel in the UI. This is what
turns the tool from an oracle into something a planner will actually trust.

---

### P1.6 · Export to a form a control office can use ⏱ half a day

`out/block_plan.csv` exists. A real division wants a **block requisition sheet**
per day — the format a Sr.DEN signs. Add a PDF or XLSX export shaped like the
real form: date, section, line, from–to, time from/to, nature of work, machine,
department, remarks.

Cheap, and it changes the perception from "student project" to "thing that
could be used".

---

## P2 — after the hackathon

- **Reinforcement learning for the rolling policy.** The README says do not
  build this in 36 hours and that is right. But over a season, learning *when to
  spend a corridor on tamping versus screening* is a real sequential decision
  problem. Only worth it once the rolling harness (P0.5) is solid.
- **Live NTES feed for the "as-run" timetable**, so plans are made against what
  actually ran rather than what was booked.
- **Multi-objective front.** Instead of one weighted objective, produce the
  Pareto curve between availability recovered and punctuality lost, and let the
  Sr.DEN pick the point. CP-SAT can do this by re-solving with an epsilon
  constraint on delay.
- **Weather-linked demand.** The degradation model already has monsoon shocks;
  wiring in a real rainfall feed would make the pre-monsoon surge in destressing
  and drainage work fall out of the data.
- **Mobile view for site staff** — the day's blocks, the machine, the
  worksite, and a one-tap "block started / block handed back".

---

## Known limitations we are choosing to live with

Stated here so they are decisions rather than oversights, and so nobody
rediscovers them under questioning.

| Limitation | Why it is acceptable for now |
|---|---|
| One section, not a division | modelled deeply rather than broadly; P1.1 is the answer |
| Machine repositioning is a fixed buffer | barely binds inside corridor windows on a 35 km span; P0.2 |
| No single-line working during blocks | trains are re-timetabled instead; P0.3 |
| Deep screening and grinding never scheduled | they need mega-blocks; reported openly rather than dropped; P0.4 |
| `needs_power_block` not enforced | P1.3, ~20 lines |
| Level 2 retiming adds little | true *once the corridor is placed well* — reported as a finding, not hidden |
| Timetable is generated, not downloaded | schema-identical, one-file swap; P1.4 |
| CP-SAT can time out on hard instances | guarded: never ships worse than the greedy hint, and labels the run `(kept hint)` |
| ML error is against our own simulator | stated in DATA.md; a surrogate, not a claim about IR's TRC data |
| One residual urgent segment at week 10 | greedy's worst-first rule clears the tail slightly better; priced, not eliminated |

---

## If you only do three things

1. **P1.4 — load the real timetable.** Two hours, and it changes what you can
   claim.
2. **P0.1 — talk to a PWI.** Forty-five minutes, and it is the only thing here
   that cannot be obtained from a document.
3. **P0.5 — daily freeze-and-replan.** One day, and it gives the demo its best
   moment: *a machine fails, and the plan repairs itself on stage without
   touching what was already committed.*

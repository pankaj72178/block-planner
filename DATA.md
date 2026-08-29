# DATA.md — provenance, layer by layer

The single most important page in this repository. The problem statement asks
for a block planner for Indian Railways; the operational data that a real block
planner runs on (block registers, the working timetable with freight paths,
TMS/IRTMMS asset records, machine rosters, control-office charts) is internal to
IR and is not obtainable by anyone outside it.

So this system is built on a deliberate split:

> **The framework is real. The operational state is simulated — from published
> IR norms, labelled everywhere it appears, and reproducible from this repo.**

Nothing below is presented as data we do not have.

---

## Layer inventory

| Layer | Status | Source | Where in the code |
|---|---|---|---|
| Section, stations, codes, chainage | **Real** | IR public time-tables; OpenStreetMap | `data/section.yaml`, `ingest/network.py` |
| Track topology, electrification | **Real** | OpenStreetMap (Overpass query included) | `ingest/network.fetch_osm_topology` |
| Crossing loops per station | **Assumption** | OSM is incomplete; README rule of thumb — loops at every 2nd–3rd station | `data/section.yaml` (`loop:` field) |
| Passenger timetable | **Real schema, synthetic contents** | data.gov.in / Kaggle "Indian Railways time table"; our generator writes the *same columns* | `ingest/timetable.py` |
| Freight paths | **Simulated (labelled)** | Threaded into genuine gaps at freight speed, held in loops for overtakes | `ingest/freight_synth.py` |
| Maintenance periodicities | **Real (published)** | IRPWM, IR Works & AC Traction manuals | `data/norms.yaml` |
| Machine fleet & repositioning | **Assumption** | Typical divisional allotment | `data/norms.yaml` → `machines:` |
| Machine outturn targets | **Assumption** | Divisions are measured on machine outturn; values sized to ~⅔ of tolerance | `data/norms.yaml` → `outturn_targets:` |
| Maintenance demand (the job list) | **Simulated (labelled)** | Generated from the published periodicities + drawn last-done dates | `assets/demand_generator.py` |
| Asset condition (TGI) | **Simulated (labelled)** | Degradation process: tonnage, age, curvature, monsoon shocks | `assets/degradation.py` |
| Degradation predictions | **Learned** | GradientBoosting surrogate trained on that process | `assets/degradation.train_degradation_model` |
| Corridor block windows | **Computed** | Chosen by the system, per day, to minimise displaced traffic | `ingest/corridor.py` |
| Block plan | **Computed** | CP-SAT | `optimizer/cpsat.py` |
| KPIs | **Computed** | Discrete-event simulation with block bursts | `sim/` |

---

## Swapping in the real timetable

This is a one-file change, by design.

1. Download the Indian Railways time-table dataset from data.gov.in (or a
   Kaggle mirror).
2. Filter it to trains that traverse Vadodara Jn → Surat.
3. Save it as `data/timetable.csv` with these columns:

   ```
   Train_No, Train_Name, SEQ, Station_Code, Station_Name,
   Arrival_time, Departure_time, Distance
   ```

   `Train_Type` and `Runs_On` are used if present and inferred from the train
   name if not (`ingest/timetable.infer_type`).

4. Re-run. Nothing else in the repository changes — the generator exists only
   to produce this file when it is absent.

The same applies to the network: `ingest/network.fetch_osm_topology()` holds
the live Overpass query. It is deliberately **not** called by the pipeline, so
the demo runs fully offline and cannot fail on a network timeout.

---

## Named assumptions

Every one of these is a number we chose. They are listed so they can be
argued with.

| Assumption | Value | Basis |
|---|---|---|
| Sectional headway | 6 min | Route class A, automatic signalling |
| Block safety buffer | 10 min either side | Standard clearance before/after a block |
| Operations tolerance | 8 block-hours/day, section-wide | ≈4 h per line per day on a double line |
| Corridor window | 210 min | A ~3 h tamping block plus buffers |
| Corridor span | 4 contiguous stretches (~25–45 km) | A block is taken over a portion, not the whole section |
| Tamping output | ~1.5 track-km per corridor block | CSM output including setup and removal |
| TGI decay | 0.045/day baseline, scaled by GMT, age, curvature | Calibrated so a GMT-55 segment reaches the intervention threshold near the IRPWM tamping periodicity |
| TGI thresholds | intervention 65, urgent 55 | Ordinal bands; IR's exact TGI bands are internal |
| Availability value per km | tamping 1.0, screening 1.2, USFD 0.08 | Geometry work lifts speed restrictions; ultrasonic testing finds defects but restores nothing |
| Freight volume | 24 paths/day, both directions | ~36% of traffic — conservative for this corridor |
| Block-burst probability | rises with duration and window tightness, capped at 55% | No public burst statistics exist; shape is qualitative |
| Retiming limits | goods ±90 min, express ±15, premium 0 | Goods are already looped up to 75 min for overtakes in our own path generator |

---

## What this cannot tell you

Stated plainly, because the alternative is being caught by it.

- **No external ground truth.** No real block register exists to compare
  against, so no accuracy claim is made. Validation rests on three things
  instead: an independently written feasibility validator
  (`optimizer/validate.py`), comparison against a greedy baseline on
  byte-identical inputs, and sensitivity behaviour
  (`experiments/sensitivity.py`).
- **The ML model's error is against the simulator, not against IR.** The
  reported MAE (~19 days vs ~78 for a naive mean) measures how well the
  surrogate learns our degradation process. Trained on real TRC data it would
  be a different model with a different error.
- **Operational judgment is missing.** Why a block is refused for reasons not
  in any constraint list, how a controller negotiates with a PWI, why a granted
  block starts 25 minutes late every time. That gap closes with a conversation
  with a serving PWI or section controller, not with more data.
- **One section.** Divisional scale-up is a roadmap item, not a claim.

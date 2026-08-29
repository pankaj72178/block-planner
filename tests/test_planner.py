"""Tests for the invariants that make the numbers mean anything.

Not coverage for its own sake. Each of these is either a property the results
depend on, or a bug that actually happened during the build.
"""
import pytest

from core.model import MIN_PER_DAY, Plan
from ingest.timetable import count_conflicts
from optimizer.cpsat import free_start_domain, solve_cpsat
from optimizer.validate import validate
from sim.kpis import evaluate
from sim.simulator import simulate


# --------------------------------------------------------------- traffic
def test_base_timetable_is_conflict_free(world):
    """Retiming puts every train interval into one NoOverlap. A base timetable
    that already violates headway makes that model infeasible before it starts."""
    assert count_conflicts(world.trains) == 0


def test_freight_is_a_real_share_of_traffic(world):
    r = world.reports["traffic"]
    assert 25 <= r["freight_share_pct"] <= 55, "freight must not be a token"
    assert 45 <= r["trains_per_day"] <= 90


def test_corridor_windows_are_actually_train_free(world):
    """The whole point of the policy: the hole has to be real, not drawn on."""
    assert world.corridors, "corridor blocks should be enabled by default"
    for c in world.corridors:
        for sid in c.stretch_ids:
            for o in world.occupancy.get(sid, []):
                assert not (o.t_in < c.end and c.start < o.t_out), (
                    f"train {o.t_in}-{o.t_out} sits inside reserved corridor "
                    f"{c.start}-{c.end} on {sid}")


def test_corridor_rotates_across_the_section(world):
    """Blocking the same 33 km all week maintains a third of the section."""
    spans = {f"{c.from_code}-{c.to_code}" for c in world.corridors}
    assert len(spans) >= 3, f"corridor barely rotates: {spans}"


# --------------------------------------------------------------- simulator
def test_no_blocks_means_no_delay(world):
    """The control. If this fails, every punctuality number is noise."""
    sim = simulate(world, Plan(name="empty", blocks=[], unscheduled=[]),
                   burst=False)
    assert max(sim.delays.values()) == 0
    assert sim.hold_minutes == 0


def test_bursts_only_ever_add_delay(world, cpsat_plan):
    clean = simulate(world, cpsat_plan, burst=False)
    burst = simulate(world, cpsat_plan, burst=True, seed=5)
    assert sum(burst.delays.values()) >= sum(clean.delays.values())


# --------------------------------------------------------------- plans
@pytest.mark.parametrize("which", ["greedy_plan", "cpsat_plan"])
def test_plans_are_feasible(world, which, request):
    plan = request.getfixturevalue(which)
    r = validate(world, plan)
    assert r["feasible"], r["violations"]


def test_cpsat_beats_greedy_on_its_own_objective(world, greedy_plan, cpsat_plan):
    """A solver that proves optimality and still loses to the baseline on the
    metric being reported is pointed at the wrong function."""
    from optimizer.cpsat import (AVAILABILITY_WEIGHT, PRIORITY_SCALE,
                                 URGENT_BONUS)
    nd = world.cfg["planning"]["horizon_days"]

    def score(p):
        t = 0
        for b in p.blocks:
            j = world.job(b.job_id)
            t += round(j.priority * PRIORITY_SCALE)
            t += URGENT_BONUS if j.urgent else 0
            t += round(AVAILABILITY_WEIGHT * j.availability_value
                       * (nd - b.start // MIN_PER_DAY))
        return t

    assert score(cpsat_plan) >= score(greedy_plan)


def test_locked_blocks_survive_a_replan(world, cpsat_plan):
    """Human-in-the-loop: the planner's committed decisions must not move."""
    lock = [{"job_id": b.job_id, "start": b.start,
             "machine_unit": b.machine_unit} for b in cpsat_plan.blocks[:3]]
    replan = solve_cpsat(world, time_limit=10, locked=lock)
    placed = {b.job_id: b for b in replan.blocks}
    for l in lock:
        assert l["job_id"] in placed, f"{l['job_id']} was locked but dropped"
        assert placed[l["job_id"]].start == l["start"]
    assert validate(world, replan)["feasible"]


def test_broken_machine_is_never_used(world):
    from core.pipeline import build_world
    w = build_world(max_jobs=120, seed=7, disabled_machines=["CSM-1"])
    p = solve_cpsat(w, time_limit=10)
    assert all(b.machine_unit != "CSM-1" for b in p.blocks)
    assert validate(w, p)["feasible"]


def test_outturn_targets_are_met(world, cpsat_plan):
    """Otherwise the optimiser spends the week on the cheapest activity."""
    if getattr(cpsat_plan, "outturn_relaxed", False):
        pytest.skip("targets unreachable in this scenario and reported as such")
    got = {}
    for b in cpsat_plan.blocks:
        got[b.machine_type] = got.get(b.machine_type, 0) + 1
    for mtype, floor in getattr(cpsat_plan, "outturn_targets", {}).items():
        assert got.get(mtype, 0) >= floor, f"{mtype}: {got.get(mtype, 0)} < {floor}"


def test_the_plan_is_not_all_one_activity(world, cpsat_plan):
    """The failure this project spent the longest not noticing."""
    acts = {b.activity for b in cpsat_plan.blocks}
    assert len(acts) >= 3, f"plan is degenerate: {acts}"


# --------------------------------------------------------------- domains
def test_free_start_domain_excludes_occupied_time():
    from core.model import Occupancy
    busy = [Occupancy("s", 100, 200), Occupancy("s", 400, 500)]
    d = free_start_domain(busy, size=50, horizon=1000, daylight=None, n_days=1)
    assert d.contains(0) and d.contains(50)      # fits before the first train
    assert not d.contains(80)                    # would run into it
    assert not d.contains(150)                   # inside it
    assert d.contains(200) and d.contains(350)   # the gap between
    assert not d.contains(380)                   # would run into the second
    assert d.contains(500) and d.contains(950)
    assert not d.contains(951)                   # past the horizon


def test_free_start_domain_respects_daylight():
    d = free_start_domain([], size=60, horizon=2880, daylight=(6, 18), n_days=2)
    assert not d.contains(0)
    assert d.contains(6 * 60)
    assert d.contains(18 * 60 - 60)
    assert not d.contains(18 * 60 - 30)          # would run past dusk
    assert d.contains(1440 + 6 * 60)             # and again the next day


# --------------------------------------------------------------- model + KPIs
def test_degradation_model_beats_a_naive_mean(world):
    ml = world.reports["ml"]
    assert ml["mae_days"] < ml["baseline_mae_days"] / 2
    assert ml["r2"] > 0.7


def test_demand_is_generated_from_norms_not_thin_air(world):
    d = world.reports["demand"]
    assert d["generated"] > d["kept"] or d["dropped_by_cap"] == 0
    assert d["overdue_jobs"] > 0
    assert set(world.reports["demand_by_activity"]) <= set(
        world.cfg["norms"]["activities"])


def test_aai_is_a_bounded_index_that_improves(world, cpsat_plan):
    k = evaluate(world, cpsat_plan, simulate(world, cpsat_plan, seed=207))
    assert 0.0 <= k["aai_with_plan"] <= 1.0
    assert k["aai_with_plan"] >= k["aai_do_nothing"]


def test_corridor_policy_recovers_more_than_no_policy(world, world_no_corridor):
    """The headline claim, as a test."""
    from optimizer.greedy import solve_greedy
    a, b = world_no_corridor, world
    ka = evaluate(a, solve_greedy(a), simulate(a, solve_greedy(a), seed=207))
    kb = evaluate(b, solve_greedy(b), simulate(b, solve_greedy(b), seed=207))
    assert kb["overdue_km_days_cleared"] > ka["overdue_km_days_cleared"]


def test_solver_ladder_never_regresses(world, greedy_plan, cpsat_plan):
    """greedy <= CP-SAT <= CP-SAT+retiming, under the real objective.

    Level 2 solves a strictly larger problem in the same wall-clock budget, so
    it can time out on a worse incumbent than Level 1 and quietly ship a
    regression. It did, twice, before the guards in optimizer/retiming.py and
    optimizer/cpsat.py were scored with the real objective instead of a
    stand-in.
    """
    from optimizer.cpsat import score_plan
    from optimizer.retiming import solve_with_retiming

    rt, _ = solve_with_retiming(world, cpsat_plan, time_limit=15,
                                max_retimed=10)
    g, c, r = (score_plan(world, p)
               for p in (greedy_plan, cpsat_plan, rt))
    assert c >= g, f"CP-SAT ({c}) below greedy ({g})"
    assert r >= c, f"retiming ({r}) below CP-SAT ({c})"


def test_cpsat_falls_back_rather_than_shipping_worse(world, greedy_plan):
    """A one-second budget cannot beat the hint; it must return the hint."""
    from optimizer.cpsat import score_plan, solve_cpsat
    p = solve_cpsat(world, time_limit=1.0, hint_plan=greedy_plan)
    assert score_plan(world, p) >= score_plan(world, greedy_plan)
    assert validate(world, p)["feasible"]

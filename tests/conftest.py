import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from core.pipeline import build_world
from optimizer.cpsat import solve_cpsat
from optimizer.greedy import solve_greedy


@pytest.fixture(scope="session")
def world():
    return build_world(max_jobs=120, seed=7)


@pytest.fixture(scope="session")
def world_no_corridor():
    return build_world(max_jobs=120, seed=7, corridor_blocks=False)


@pytest.fixture(scope="session")
def greedy_plan(world):
    return solve_greedy(world)


@pytest.fixture(scope="session")
def cpsat_plan(world, greedy_plan):
    return solve_cpsat(world, time_limit=12, hint_plan=greedy_plan)

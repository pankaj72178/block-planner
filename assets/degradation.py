"""Track-condition model: simulated TGI history + a learned degradation model.

WHY SIMULATE (README A4): real condition data lives in IR's TMS/IRTMMS, Track
Recording Car runs and USFD defect logs. None of it is public. So we generate
it -- but we generate it from a *physical* process (traffic tonnage, asset age,
curvature, time since tamping, monsoon shocks) rather than sampling noise.

That choice is what earns the "AI-powered" label:

    physics-style process  ->  observable features  ->  learned surrogate
                                                        (GradientBoosting)

The learned model predicts DAYS UNTIL TGI CROSSES THE INTERVENTION THRESHOLD
from features a Permanent Way Inspector actually has (GMT, age, curvature,
days since last tamping, current TGI). Those predictions then *move the due
dates* the optimizer plans against -- the predict-then-plan pipeline the
problem statement asks for. The optimizer is the deliverable; this is the
thing that tells it what matters and when.

Everything here is labelled SIMULATED in DATA.md and on the honesty slide.
"""
from __future__ import annotations

import math
import random
from typing import Dict, List, Tuple

import numpy as np

from core.model import AssetSegment

# (min, mode, max) days since the activity was last done, drawn triangular.
# Skewed towards "recently done" with an overdue tail, because that is the
# shape of a real maintenance register: most of the section is in cycle, a
# minority has slipped. A uniform draw would invent a section in crisis.
ACTIVITY_SPREAD_DAYS = {
    "tamping":          (15, 130, 420),
    "deep_screening":   (200, 1800, 3600),
    "usfd":             (2, 25, 95),
    "destressing":      (30, 250, 520),
    "rail_grinding":    (100, 600, 1400),
    "ohe_maintenance":  (5, 55, 150),
    "points_crossings": (15, 120, 280),
}


def _decay_per_day(seg: AssetSegment, tgi_cfg: dict) -> float:
    """Daily TGI loss. Rises with tonnage, age and curvature."""
    base = tgi_cfg["base_decay_per_day"]
    traffic = seg.gmt_per_year / 40.0
    ageing = 1.0 + seg.age_years / 45.0
    curve = 1.0 + seg.curvature * 0.18
    return base * traffic * ageing * curve


def initialise_condition(segments: List[AssetSegment], cfg: dict,
                         seed: int = 31) -> None:
    """Draw maintenance history and roll TGI forward to 'today' (t=0)."""
    rng = random.Random(seed)
    tgi_cfg = cfg["norms"]["tgi"]
    start = tgi_cfg["initial_after_tamping"]

    for seg in segments:
        for act, (lo, mode, hi) in ACTIVITY_SPREAD_DAYS.items():
            seg.last_done[act] = int(rng.triangular(lo, hi, mode))

        days = seg.last_done["tamping"]
        tgi = start
        hist = []
        rate = _decay_per_day(seg, tgi_cfg)
        for d in range(days):
            tgi -= rate * rng.uniform(0.8, 1.2)
            # monsoon damage: a formation/drainage hit that costs real geometry
            month = ((d + 150) // 30) % 12 + 1
            if month in tgi_cfg["monsoon_months"] and \
                    rng.random() < tgi_cfg["monsoon_shock_prob"]:
                lo, hi = tgi_cfg["monsoon_shock_size"]
                tgi -= rng.uniform(lo, hi)
            tgi = max(20.0, tgi)
            hist.append(round(tgi, 2))
        seg.tgi = round(tgi, 2)
        seg.tgi_history = hist[-180:]


def _roll_forward_to_threshold(seg: AssetSegment, tgi_cfg: dict,
                               rng: random.Random, cap: int = 900) -> int:
    """Ground truth: days from now until this segment needs tamping."""
    thr = tgi_cfg["intervention_threshold"]
    tgi = seg.tgi
    if tgi <= thr:
        return 0
    rate = _decay_per_day(seg, tgi_cfg)
    for d in range(1, cap + 1):
        tgi -= rate * rng.uniform(0.8, 1.2)
        month = ((d + 150) // 30) % 12 + 1
        if month in tgi_cfg["monsoon_months"] and \
                rng.random() < tgi_cfg["monsoon_shock_prob"]:
            lo, hi = tgi_cfg["monsoon_shock_size"]
            tgi -= rng.uniform(lo, hi)
        if tgi <= thr:
            return d
    return cap


def features(seg: AssetSegment) -> List[float]:
    """The five things a PWI can actually look up for a segment."""
    return [
        seg.gmt_per_year,
        seg.age_years,
        seg.curvature,
        float(seg.last_done.get("tamping", 0)),
        seg.tgi,
        seg.cumulative_gmt,
    ]


FEATURE_NAMES = ["gmt_per_year", "age_years", "curvature",
                 "days_since_tamping", "current_tgi", "cumulative_gmt"]


def train_degradation_model(segments: List[AssetSegment], cfg: dict,
                            seed: int = 5, n_synth: int = 4000) -> Tuple:
    """Fit the surrogate and report honest held-out error.

    We train on `n_synth` perturbed variants of the real section's segments so
    the model sees a wider condition space than the 258 segments we happen to
    have today, then evaluate on a held-out split. The MAE we print is the
    number to put on the slide -- not a claim of accuracy against IR's real
    TRC data, which we have never seen.
    """
    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import mean_absolute_error, r2_score

    rng = random.Random(seed)
    tgi_cfg = cfg["norms"]["tgi"]

    X, y = [], []
    pool = segments if segments else []
    for i in range(n_synth):
        base = pool[i % len(pool)]
        ghost = AssetSegment(
            id=f"synth{i}", stretch_id=base.stretch_id, line=base.line,
            km_start=0, km_end=1,
            age_years=max(1.0, base.age_years * rng.uniform(0.4, 1.8)),
            gmt_per_year=max(5.0, base.gmt_per_year * rng.uniform(0.5, 1.5)),
            cumulative_gmt=base.cumulative_gmt * rng.uniform(0.4, 1.8),
            curvature=max(0.0, base.curvature * rng.uniform(0.0, 2.5)),
            is_station_yard=base.is_station_yard,
        )
        ghost.last_done = {"tamping": rng.randint(10, 500)}
        ghost.tgi = max(30.0, min(95.0, rng.gauss(74, 12)))
        X.append(features(ghost))
        y.append(_roll_forward_to_threshold(ghost, tgi_cfg, rng))

    X, y = np.array(X), np.array(y)
    Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=0.25,
                                          random_state=seed)
    model = GradientBoostingRegressor(
        n_estimators=300, max_depth=3, learning_rate=0.06, random_state=seed
    )
    model.fit(Xtr, ytr)
    pred = model.predict(Xte)
    metrics = {
        "mae_days": float(mean_absolute_error(yte, pred)),
        "r2": float(r2_score(yte, pred)),
        "n_train": int(len(Xtr)),
        "n_test": int(len(Xte)),
        "baseline_mae_days": float(np.mean(np.abs(yte - np.mean(ytr)))),
        "feature_importance": dict(
            zip(FEATURE_NAMES,
                [round(float(v), 4) for v in model.feature_importances_])
        ),
    }
    return model, metrics


def predict_days_to_threshold(model, segments: List[AssetSegment]) -> Dict[str, float]:
    if not segments:
        return {}
    X = np.array([features(s) for s in segments])
    pred = model.predict(X)
    return {s.id: float(max(0.0, p)) for s, p in zip(segments, pred)}

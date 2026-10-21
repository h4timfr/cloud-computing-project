"""Deterministic, explainable ``heuristic-v1`` flood-risk scoring.

Design constraints (locked for the MVP):

* deterministic: identical input -> identical output, no randomness, no clock reads
* explainable: every score returns per-variable sub-scores, weights, contributions
  and a human-readable explanation
* explicitly heuristic: this is not a validated hydrological model, not a
  statistical forecast and not a machine-learning prediction
* time-to-threshold is intentionally **not** implemented at M0
"""

from __future__ import annotations

import math
from statistics import fmean
from typing import Any, Mapping, Sequence

from .config import (
    DEESCALATION_STREAK,
    DEFAULT_THRESHOLDS,
    ESCALATION_STREAK,
    METRIC_ORDER,
    METRIC_WEIGHTS,
    METRICS,
    RISK_BANDS,
    RISK_MODEL_ID,
    RISK_ORDER,
    SMOOTHING_WINDOW,
    TREND_REFERENCE_RISE_M,
    TREND_WEIGHT,
)

MODEL_NOTE = (
    "heuristic-v1 is a hand-tuned deterministic scoring heuristic over simulated "
    "sensor values. It is not a calibrated hydrological model, not a statistical "
    "forecast and not a machine-learning prediction."
)


class RiskConfigError(ValueError):
    """Raised when thresholds or weights are internally inconsistent."""


class RiskInputError(ValueError):
    """Raised when a metric, a score or a stored reading is not a finite number.

    BUG CLASS (M17): ``classify`` had no branch for a value it could not interpret, so its
    last-resort ``return RISK_BANDS[-1][0]`` turned "not a number" into CRITICAL - silently, at
    the most severe band, which then opened an incident and asked for an alert. Nothing is
    scored from a value that cannot be read: the engine says so instead. The callers that can
    meet a corrupt stored row (the smoothing window, the history reader) drop it and count it,
    the same way they already treat any other unusable row.
    """


def clamp(value: float, low: float, high: float) -> float:
    """Clamp ``value`` into ``[low, high]``."""
    if not math.isfinite(value):
        raise RiskInputError(f"cannot clamp a non-finite number: {value!r}")
    if value < low:
        return low
    if value > high:
        return high
    return value


def sub_score(value: float, watch: float, critical: float) -> float:
    """Piecewise-linear 0..1 sub-score.

    ``0`` at (or below) ``0`` of the metric, ``0.5`` exactly at ``watch``,
    ``1.0`` at (or above) ``critical``.
    """
    if not math.isfinite(value):
        raise RiskInputError(f"metric value must be a finite number, got {value!r}")
    if watch <= 0.0:
        raise RiskConfigError("watch threshold must be positive")
    if critical <= watch:
        raise RiskConfigError("critical threshold must exceed watch threshold")
    if value <= 0.0:
        return 0.0
    if value >= critical:
        return 1.0
    if value <= watch:
        return 0.5 * (value / watch)
    return 0.5 + 0.5 * (value - watch) / (critical - watch)


def classify(risk_score: float) -> str:
    """Map a risk score to NORMAL / WATCH / WARNING / CRITICAL.

    A non-finite score is refused rather than ranked: the fallthrough band would otherwise
    report the *most severe* class for a value nobody could read (see ``RiskInputError``).
    """
    if not math.isfinite(risk_score):
        raise RiskInputError(f"risk score must be a finite number, got {risk_score!r}")
    for name, _lower, upper in RISK_BANDS:
        if risk_score < upper:
            return name
    return RISK_BANDS[-1][0]


def class_rank(risk_class: str) -> int:
    """Ordinal position of a band (NORMAL=0 .. CRITICAL=3)."""
    try:
        return RISK_ORDER[risk_class]
    except KeyError as exc:  # pragma: no cover - defensive
        raise RiskConfigError(f"unknown risk class {risk_class!r}") from exc


def rolling_mean_metrics(
    series: Sequence[Mapping[str, float]], window: int = SMOOTHING_WINDOW
) -> dict[str, float]:
    """Mean of the last ``window`` readings, keyed by metric (deterministic)."""
    tail = list(series)[-max(1, window) :]
    if not tail:
        raise ValueError("at least one reading is required for smoothing")
    missing = [metric for metric in METRIC_ORDER if any(metric not in row for row in tail)]
    if missing:
        raise ValueError(f"readings missing metric(s): {', '.join(missing)}")
    for row in tail:
        for metric in METRIC_ORDER:
            if not math.isfinite(float(row[metric])):
                raise RiskInputError(
                    f"reading for {metric!r} is not a finite number: {row[metric]!r}"
                )
    return {metric: fmean(float(row[metric]) for row in tail) for metric in METRIC_ORDER}


def score_zone(
    metrics: Mapping[str, float],
    thresholds: Mapping[str, Mapping[str, float]] | None = None,
    weights: Mapping[str, float] | None = None,
    water_level_5min_ago: float | None = None,
    water_level_now: float | None = None,
    smoothing_window: int = SMOOTHING_WINDOW,
) -> dict[str, Any]:
    """Score one zone from already-smoothed metrics and return a full explanation.

    ``water_level_5min_ago`` / ``water_level_now`` are optional **raw** (unsmoothed)
    water levels compared with each other: when both are supplied, a worsening-rate
    term is added. ``water_level_now`` defaults to the (possibly smoothed) metric
    value. When the lookback value is omitted the trend contribution is ``0.0`` and
    is reported as ``null`` so callers can distinguish "not measured" from "zero".
    """
    thresholds = thresholds or DEFAULT_THRESHOLDS
    weights = weights or METRIC_WEIGHTS

    contributions: list[dict[str, Any]] = []
    base_total = 0.0
    for metric in METRIC_ORDER:
        if metric not in metrics:
            raise ValueError(f"missing metric {metric!r} in input")
        if metric not in thresholds:
            raise RiskConfigError(f"no thresholds configured for {metric!r}")
        value = float(metrics[metric])
        if not math.isfinite(value):
            # Refused here as well as in ``sub_score``: a caller that builds a reading by hand
            # (a tool, a probe, a future writer) must never get a scored result back for a
            # number that cannot be read.
            raise RiskInputError(f"metric {metric!r} is not a finite number: {metrics[metric]!r}")
        watch = float(thresholds[metric]["watch"])
        critical = float(thresholds[metric]["critical"])
        weight = float(weights[metric])
        sub = sub_score(value, watch, critical)
        base_total += weight * sub
        contributions.append(
            {
                "variable": metric,
                "unit": METRICS[metric]["unit"],
                "value": round(value, 4),
                "watch_threshold": watch,
                "critical_threshold": critical,
                "sub_score": round(sub, 4),
                "weight": weight,
                "contribution": round(weight * sub, 4),
            }
        )

    rise_fraction: float | None = None
    water_level_delta: float | None = None
    trend_contribution = 0.0
    if water_level_5min_ago is not None:
        current = float(metrics["water_level_m"]) if water_level_now is None else float(water_level_now)
        if not (math.isfinite(current) and math.isfinite(float(water_level_5min_ago))):
            raise RiskInputError("water level trend needs two finite water levels")
        water_level_delta = current - float(water_level_5min_ago)
        rise_fraction = clamp(water_level_delta / TREND_REFERENCE_RISE_M, 0.0, 1.0)
        trend_contribution = TREND_WEIGHT * rise_fraction

    base_score = round(base_total, 4)
    risk_score = round(clamp(base_total + trend_contribution, 0.0, 1.0), 4)
    ranked = sorted(contributions, key=lambda item: (-item["contribution"], item["variable"]))

    return {
        "model": RISK_MODEL_ID,
        "model_note": MODEL_NOTE,
        "risk_score": risk_score,
        "risk_class_candidate": classify(risk_score),
        "base_score": base_score,
        "trend": {
            "weight": TREND_WEIGHT,
            "reference_rise_m": TREND_REFERENCE_RISE_M,
            "water_level_delta_m": None if water_level_delta is None else round(water_level_delta, 4),
            "rise_fraction": None if rise_fraction is None else round(rise_fraction, 4),
            "contribution": round(trend_contribution, 4),
        },
        "contributions": contributions,
        "top_drivers": [item["variable"] for item in ranked[:3]],
        "explanation": _explain(risk_score, base_score, ranked, trend_contribution),
        "smoothing_window_readings": smoothing_window,
    }


def _explain(
    risk_score: float,
    base_score: float,
    ranked: Sequence[Mapping[str, Any]],
    trend_contribution: float,
) -> str:
    parts = ", ".join(
        f"{item['variable']}={item['contribution']:.4f}" for item in ranked if item["contribution"] > 0
    )
    driver = ranked[0]["variable"] if ranked and ranked[0]["contribution"] > 0 else "none"
    return (
        f"risk_score {risk_score:.4f} = weighted base {base_score:.4f} "
        f"({parts or 'all contributions zero'}) + trend {trend_contribution:.4f}; "
        f"dominant driver: {driver}"
    )


def apply_hysteresis(
    candidate_class: str,
    previous_state: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Turn a raw candidate class into the effective class, with dwell counting.

    * first observation for a zone adopts the candidate immediately
    * escalation requires ``ESCALATION_STREAK`` consecutive higher-band readings
    * de-escalation requires ``DEESCALATION_STREAK`` consecutive lower-band readings
    """
    if candidate_class not in RISK_ORDER:
        raise RiskConfigError(f"unknown candidate class {candidate_class!r}")

    if previous_state is None:
        return {
            "risk_class": candidate_class,
            "candidate_class": candidate_class,
            "streak_class": None,
            "streak_count": 0,
            "transition": "initial",
        }

    effective = str(previous_state["risk_class"])
    counts = {"previous_class": effective, "candidate_class": candidate_class}

    if candidate_class == effective:
        return {
            "risk_class": effective,
            "candidate_class": candidate_class,
            "streak_class": None,
            "streak_count": 0,
            "transition": "hold",
            **counts,
        }

    escalating = class_rank(candidate_class) > class_rank(effective)
    required = ESCALATION_STREAK if escalating else DEESCALATION_STREAK

    if previous_state.get("streak_class") == candidate_class:
        streak = int(previous_state.get("streak_count", 0)) + 1
    else:
        streak = 1

    if streak >= required:
        return {
            "risk_class": candidate_class,
            "candidate_class": candidate_class,
            "streak_class": None,
            "streak_count": 0,
            "transition": "escalate" if escalating else "deescalate",
            **counts,
        }

    return {
        "risk_class": effective,
        "candidate_class": candidate_class,
        "streak_class": candidate_class,
        "streak_count": streak,
        "transition": "pending",
        **counts,
    }

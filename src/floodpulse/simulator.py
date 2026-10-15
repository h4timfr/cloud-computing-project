"""Deterministic sensor simulator.

Every payload produced here is **SIMULATED** and is marked as such
(``"simulated": true`` plus a ``data_classification`` of ``"simulated"``).

Determinism contract: same ``zone_id`` + same ``seed`` + same ``start_epoch``
produces a byte-identical sequence of payloads. The default ``start_epoch`` is a
fixed constant (2026-01-01T00:00:00Z) so tests never depend on the wall clock.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any

from .config import METRIC_ORDER, METRICS, SCHEMA_VERSION, ZONE_DEFINITIONS, zone_definition

#: (phase name, number of steps) -- 100 steps x 5 s ~= 8.3 simulated minutes.
PHASES: tuple[tuple[str, int], ...] = (
    ("calm", 20),
    ("storm_ramp", 15),
    ("flash_flood", 20),
    ("recession", 45),
)

TOTAL_STEPS = sum(steps for _name, steps in PHASES)

DEFAULT_SEED = 20260214
DEFAULT_INTERVAL_SECONDS = 5.0
DEFAULT_START_EPOCH = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())

#: Simulated metric bands per scenario extreme: (low, high) inclusive.
CALM_BANDS: dict[str, tuple[float, float]] = {
    "rainfall_mm_per_hr": (0.5, 3.0),
    "drain_level_m": (0.35, 0.75),
    "soil_saturation_pct": (36.0, 46.0),
    "water_level_m": (0.80, 1.25),
    "flow_rate_m3_s": (1.10, 2.20),
}

FLOOD_BANDS: dict[str, tuple[float, float]] = {
    "rainfall_mm_per_hr": (48.0, 72.0),
    "drain_level_m": (2.35, 3.10),
    "soil_saturation_pct": (90.0, 97.0),
    "water_level_m": (3.40, 4.30),
    "flow_rate_m3_s": (12.5, 18.0),
}


def _clamp(value: float, low: float, high: float) -> float:
    if value < low:
        return low
    if value > high:
        return high
    return value


def _lerp(start: float, end: float, progress: float) -> float:
    return start + (end - start) * progress


def _band_at(metric: str, phase: str, progress: float) -> tuple[float, float]:
    calm_low, calm_high = CALM_BANDS[metric]
    flood_low, flood_high = FLOOD_BANDS[metric]
    if phase == "calm":
        return calm_low, calm_high
    if phase == "storm_ramp":
        return _lerp(calm_low, flood_low, progress), _lerp(calm_high, flood_high, progress)
    if phase == "flash_flood":
        return flood_low, flood_high
    if phase == "recession":
        return _lerp(flood_low, calm_low, progress), _lerp(flood_high, calm_high, progress)
    raise ValueError(f"unknown phase {phase!r}")


def _metric_range(metric: str) -> tuple[float, float]:
    return float(METRICS[metric]["min"]), float(METRICS[metric]["max"])


class SensorSimulator:
    """Deterministic, phase-driven simulator for a single zone."""

    def __init__(
        self,
        zone_id: str,
        seed: int | str = DEFAULT_SEED,
        start_epoch: int | None = DEFAULT_START_EPOCH,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        sensor_node_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        zone_definition(zone_id)  # raises KeyError for unknown zones
        self.zone_id = zone_id
        self.seed = seed
        self.interval_seconds = float(interval_seconds)
        self.start_epoch = DEFAULT_START_EPOCH if start_epoch is None else int(start_epoch)
        self.sensor_node_id = sensor_node_id or f"sim-{zone_id[5:]}"
        self.run_id = run_id or f"sim-{seed}"
        # Seeding with a string is stable across processes (no PYTHONHASHSEED dependence).
        self._rng = random.Random(f"{seed}:{zone_id}")
        self._step = 0

    # -- introspection ------------------------------------------------------ #

    @staticmethod
    def phase_at(step: int) -> str:
        """Phase name for a given step index (clamped to the last phase)."""
        cursor = 0
        for name, length in PHASES:
            if step < cursor + length:
                return name
            cursor += length
        return PHASES[-1][0]

    @staticmethod
    def phase_progress(step: int) -> float:
        """0..1 progress inside the current phase."""
        cursor = 0
        for _name, length in PHASES:
            if step < cursor + length:
                return (step - cursor) / length
            cursor += length
        return 1.0

    # -- generation --------------------------------------------------------- #

    def step(self) -> dict[str, Any]:
        """Produce the next simulated reading payload (already wire-format)."""
        index = self._step
        phase = self.phase_at(index)
        progress = self.phase_progress(index)

        readings: dict[str, float] = {}
        for metric in METRIC_ORDER:
            low, high = _band_at(metric, phase, progress)
            floor, ceiling = _metric_range(metric)
            readings[metric] = round(
                _clamp(self._rng.uniform(low, high), floor, ceiling),
                2,
            )

        timestamp = datetime.fromtimestamp(
            self.start_epoch + index * self.interval_seconds, tz=timezone.utc
        ).isoformat().replace("+00:00", "Z")

        self._step += 1
        return {
            "schema_version": SCHEMA_VERSION,
            "simulated": True,
            "zone_id": self.zone_id,
            "sensor_node_id": self.sensor_node_id,
            "timestamp": timestamp,
            "readings": readings,
            "meta": {"run_id": self.run_id, "seq": index, "phase": phase},
        }

    def run(self, steps: int) -> list[dict[str, Any]]:
        """Produce ``steps`` payloads in order."""
        return [self.step() for _ in range(max(0, steps))]

    def reading_epoch(self, index: int) -> int:
        """Simulated ``ts_epoch`` for step ``index`` (lets callers inject a clock)."""
        return int(self.start_epoch + index * self.interval_seconds)


def simulators_for_all_zones(
    seed: int | str = DEFAULT_SEED,
    start_epoch: int | None = DEFAULT_START_EPOCH,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
) -> list[SensorSimulator]:
    """One simulator per fixed zone, in ``ZONE_DEFINITIONS`` order."""
    return [
        SensorSimulator(
            str(zone["zone_id"]),
            seed=seed,
            start_epoch=start_epoch,
            interval_seconds=interval_seconds,
        )
        for zone in ZONE_DEFINITIONS
    ]


def scenario_summary() -> dict[str, Any]:
    """Human-readable description of the simulated scenario (for docs/CLI)."""
    return {
        "simulated": True,
        "disclaimer": "All simulated data; not measured hydrological data.",
        "interval_seconds": DEFAULT_INTERVAL_SECONDS,
        "total_steps": TOTAL_STEPS,
        "phases": [
            {
                "name": name,
                "steps": steps,
                "from_step": sum(length for _n, length in PHASES[:index]),
                "to_step": sum(length for _n, length in PHASES[: index + 1]) - 1,
            }
            for index, (name, steps) in enumerate(PHASES)
        ],
        "bands": {"calm": CALM_BANDS, "flood": FLOOD_BANDS},
    }


__all__ = [
    "PHASES",
    "TOTAL_STEPS",
    "DEFAULT_SEED",
    "DEFAULT_START_EPOCH",
    "DEFAULT_INTERVAL_SECONDS",
    "CALM_BANDS",
    "FLOOD_BANDS",
    "SensorSimulator",
    "simulators_for_all_zones",
    "scenario_summary",
]

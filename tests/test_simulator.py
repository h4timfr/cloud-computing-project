"""Unit tests for the deterministic sensor simulator."""

from __future__ import annotations

import json
import unittest
from datetime import datetime

from floodpulse.config import METRIC_ORDER, METRICS, ZONE_IDS, thresholds_for
from floodpulse.risk import classify, score_zone
from floodpulse.simulator import (
    PHASES,
    TOTAL_STEPS,
    SensorSimulator,
    scenario_summary,
    simulators_for_all_zones,
)
from floodpulse.validation import validate_reading

ZONE = "zone-riverside-north"


def exp_epoch(iso_timestamp: str) -> int:
    return int(datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00")).timestamp())


class DeterminismTests(unittest.TestCase):
    def test_same_seed_and_zone_are_identical(self) -> None:
        first = SensorSimulator(ZONE, seed=1234).run(30)
        second = SensorSimulator(ZONE, seed=1234).run(30)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))

    def test_different_seeds_differ(self) -> None:
        first = SensorSimulator(ZONE, seed=1).run(10)
        second = SensorSimulator(ZONE, seed=2).run(10)
        self.assertNotEqual(
            [item["readings"] for item in first], [item["readings"] for item in second]
        )

    def test_different_zones_differ(self) -> None:
        first = SensorSimulator("zone-riverside-north", seed=7).run(10)
        second = SensorSimulator("zone-harbor-east", seed=7).run(10)
        self.assertNotEqual(
            [item["readings"] for item in first], [item["readings"] for item in second]
        )

    def test_stateful_step_and_run_agree(self) -> None:
        simulator = SensorSimulator(ZONE, seed=99)
        stepped = [simulator.step() for _ in range(5)]
        self.assertEqual(stepped, SensorSimulator(ZONE, seed=99).run(5))


class PayloadShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.simulator = SensorSimulator(ZONE, seed=2024)

    def test_payload_is_marked_simulated(self) -> None:
        reading = self.simulator.step()
        self.assertTrue(reading["simulated"])
        self.assertEqual(reading["schema_version"], "1.0")
        self.assertEqual(reading["zone_id"], ZONE)
        self.assertEqual(reading["sensor_node_id"], "sim-riverside-north")
        self.assertIn("phase", reading["meta"])
        self.assertNotIn("device_id", reading)
        self.assertNotIn("ts_epoch", reading)
        self.assertNotIn("ttl", reading)

    def test_generated_payloads_pass_validation(self) -> None:
        for reading in SensorSimulator(ZONE, seed=5).run(TOTAL_STEPS):
            with self.subTest(step=reading["meta"]["seq"]):
                record = validate_reading(reading, exp_epoch(reading["timestamp"]), ZONE_IDS)
                self.assertEqual(record["ts_epoch"], exp_epoch(reading["timestamp"]))

    def test_values_are_inside_the_configured_ranges(self) -> None:
        for zone_id in ZONE_IDS:
            for reading in SensorSimulator(zone_id, seed=11).run(TOTAL_STEPS):
                for metric in METRIC_ORDER:
                    value = reading["readings"][metric]
                    low, high = METRICS[metric]["min"], METRICS[metric]["max"]
                    with self.subTest(zone=zone_id, metric=metric, value=value):
                        self.assertGreaterEqual(value, low)
                        self.assertLessEqual(value, high)

    def test_sequence_and_timestamps_advance_deterministically(self) -> None:
        readings = SensorSimulator(ZONE, seed=3, start_epoch=1_800_000_000, interval_seconds=5).run(4)
        self.assertEqual([item["meta"]["seq"] for item in readings], [0, 1, 2, 3])
        self.assertEqual(
            [exp_epoch(item["timestamp"]) for item in readings],
            [1_800_000_000, 1_800_000_005, 1_800_000_010, 1_800_000_015],
        )

    def test_unknown_zone_raises(self) -> None:
        with self.assertRaises(KeyError):
            SensorSimulator("zone-atlantis")


class ScenarioTests(unittest.TestCase):
    def test_phase_boundaries(self) -> None:
        boundaries = {"calm": (0, 19), "storm_ramp": (20, 34), "flash_flood": (35, 54), "recession": (55, 99)}
        for phase, (first, last) in boundaries.items():
            with self.subTest(phase=phase):
                self.assertEqual(SensorSimulator.phase_at(first), phase)
                self.assertEqual(SensorSimulator.phase_at(last), phase)
        self.assertEqual(TOTAL_STEPS, 100)

    def test_phase_sequence_per_step_matches_metadata(self) -> None:
        seen: list[str] = []
        for reading in SensorSimulator(ZONE, seed=8).run(TOTAL_STEPS):
            phase = reading["meta"]["phase"]
            if not seen or seen[-1] != phase:
                seen.append(phase)
        self.assertEqual(seen, [name for name, _steps in PHASES])

    def test_calm_start_is_normal_and_peak_is_critical(self) -> None:
        readings = SensorSimulator(ZONE, seed=20260214).run(TOTAL_STEPS)
        scores = [
            score_zone(reading["readings"], thresholds_for(ZONE))["risk_score"] for reading in readings
        ]
        self.assertEqual(classify(scores[0]), "NORMAL")
        self.assertGreaterEqual(max(scores), 0.8)
        self.assertEqual(classify(max(scores)), "CRITICAL")
        # the peak must arrive after the calm opening, not at the start
        self.assertGreater(scores.index(max(scores)), 0)

    def test_peak_phase_is_critical_for_every_zone(self) -> None:
        for simulator in simulators_for_all_zones(seed=20260214):
            scores = [
                score_zone(reading["readings"], thresholds_for(simulator.zone_id))["risk_score"]
                for reading in simulator.run(TOTAL_STEPS)
            ]
            with self.subTest(zone=simulator.zone_id):
                self.assertEqual(classify(scores[0]), "NORMAL")
                self.assertEqual(classify(max(scores)), "CRITICAL")

    def test_factory_builds_one_simulator_per_zone(self) -> None:
        simulators = simulators_for_all_zones(seed=42)
        self.assertEqual([item.zone_id for item in simulators], list(ZONE_IDS))
        for simulator in simulators:
            self.assertTrue(simulator.sensor_node_id.startswith("sim-"))

    def test_scenario_summary_is_consistent(self) -> None:
        summary = scenario_summary()
        self.assertTrue(summary["simulated"])
        self.assertEqual(sum(item["steps"] for item in summary["phases"]), TOTAL_STEPS)
        self.assertEqual([item["name"] for item in summary["phases"]], [name for name, _ in PHASES])


class ClockTests(unittest.TestCase):
    def test_reading_epoch_matches_generated_timestamps(self) -> None:
        simulator = SensorSimulator(ZONE, seed=1, start_epoch=1_800_000_000, interval_seconds=5)
        for index in range(3):
            reading = simulator.step()
            with self.subTest(index=index):
                self.assertEqual(exp_epoch(reading["timestamp"]), simulator.reading_epoch(index))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

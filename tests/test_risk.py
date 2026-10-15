"""Unit tests for the deterministic ``heuristic-v1`` risk engine.

All expected values are hand-computed from the documented formula
``risk = sum(weight_i * sub_score_i) + 0.10 * rise_fraction``.
"""

from __future__ import annotations

import unittest

from floodpulse.config import (
    DEFAULT_THRESHOLDS,
    METRIC_ORDER,
    METRIC_WEIGHTS,
    RISK_ORDER,
    thresholds_for,
)
from floodpulse.risk import (
    RiskConfigError,
    apply_hysteresis,
    clamp,
    class_rank,
    classify,
    rolling_mean_metrics,
    score_zone,
    sub_score,
)


def metrics(rain: float = 0.0, drain: float = 0.0, soil: float = 0.0, water: float = 0.0, flow: float = 0.0):
    """Build a readings mapping in the canonical metric order."""
    values = {
        "rainfall_mm_per_hr": rain,
        "drain_level_m": drain,
        "soil_saturation_pct": soil,
        "water_level_m": water,
        "flow_rate_m3_s": flow,
    }
    return {metric: values[metric] for metric in METRIC_ORDER}


def at_watch() -> dict[str, float]:
    return metrics(
        rain=DEFAULT_THRESHOLDS["rainfall_mm_per_hr"]["watch"],
        drain=DEFAULT_THRESHOLDS["drain_level_m"]["watch"],
        soil=DEFAULT_THRESHOLDS["soil_saturation_pct"]["watch"],
        water=DEFAULT_THRESHOLDS["water_level_m"]["watch"],
        flow=DEFAULT_THRESHOLDS["flow_rate_m3_s"]["watch"],
    )


def at_critical() -> dict[str, float]:
    return metrics(
        rain=DEFAULT_THRESHOLDS["rainfall_mm_per_hr"]["critical"],
        drain=DEFAULT_THRESHOLDS["drain_level_m"]["critical"],
        soil=DEFAULT_THRESHOLDS["soil_saturation_pct"]["critical"],
        water=DEFAULT_THRESHOLDS["water_level_m"]["critical"],
        flow=DEFAULT_THRESHOLDS["flow_rate_m3_s"]["critical"],
    )


class ConfigSanityTests(unittest.TestCase):
    def test_weights_sum_to_one(self) -> None:
        self.assertAlmostEqual(sum(METRIC_WEIGHTS.values()), 1.0, places=9)

    def test_every_metric_has_weight_and_thresholds(self) -> None:
        for metric in METRIC_ORDER:
            self.assertIn(metric, METRIC_WEIGHTS)
            self.assertIn(metric, DEFAULT_THRESHOLDS)
            self.assertGreater(DEFAULT_THRESHOLDS[metric]["critical"], DEFAULT_THRESHOLDS[metric]["watch"])

    def test_bands_are_ordered(self) -> None:
        ordered = [name for name, _index in sorted(RISK_ORDER.items(), key=lambda item: item[1])]
        self.assertEqual(ordered, ["NORMAL", "WATCH", "WARNING", "CRITICAL"])

    def test_zone_overrides_merge_with_defaults(self) -> None:
        merged = thresholds_for("zone-old-town-canal")
        self.assertEqual(merged["water_level_m"], {"watch": 1.8, "critical": 3.2})
        self.assertEqual(merged["rainfall_mm_per_hr"], DEFAULT_THRESHOLDS["rainfall_mm_per_hr"])
        # defaults must not be mutated by the merge
        self.assertEqual(DEFAULT_THRESHOLDS["water_level_m"], {"watch": 2.0, "critical": 3.5})


class SubScoreTests(unittest.TestCase):
    def test_zero_is_zero(self) -> None:
        self.assertEqual(sub_score(0.0, 15.0, 45.0), 0.0)

    def test_negative_is_clamped_to_zero(self) -> None:
        self.assertEqual(sub_score(-5.0, 15.0, 45.0), 0.0)

    def test_watch_level_is_half(self) -> None:
        self.assertEqual(sub_score(15.0, 15.0, 45.0), 0.5)

    def test_critical_level_is_one(self) -> None:
        self.assertEqual(sub_score(45.0, 15.0, 45.0), 1.0)

    def test_midpoint_between_watch_and_critical_is_three_quarters(self) -> None:
        self.assertAlmostEqual(sub_score(30.0, 15.0, 45.0), 0.75, places=9)

    def test_above_critical_is_capped(self) -> None:
        self.assertEqual(sub_score(500.0, 15.0, 45.0), 1.0)

    def test_half_of_watch_is_quarter(self) -> None:
        self.assertAlmostEqual(sub_score(7.5, 15.0, 45.0), 0.25, places=9)

    def test_invalid_thresholds_raise(self) -> None:
        with self.assertRaises(RiskConfigError):
            sub_score(1.0, 0.0, 2.0)
        with self.assertRaises(RiskConfigError):
            sub_score(1.0, 2.0, 2.0)


class ClampAndClassifyTests(unittest.TestCase):
    def test_clamp(self) -> None:
        self.assertEqual(clamp(-1.0, 0.0, 1.0), 0.0)
        self.assertEqual(clamp(2.0, 0.0, 1.0), 1.0)
        self.assertEqual(clamp(0.4, 0.0, 1.0), 0.4)

    def test_band_boundaries(self) -> None:
        cases = {
            0.0: "NORMAL",
            0.3499: "NORMAL",
            0.35: "WATCH",
            0.5999: "WATCH",
            0.6: "WARNING",
            0.7999: "WARNING",
            0.8: "CRITICAL",
            1.0: "CRITICAL",
        }
        for score, expected in cases.items():
            with self.subTest(score=score):
                self.assertEqual(classify(score), expected)

    def test_class_rank(self) -> None:
        self.assertEqual(class_rank("NORMAL"), 0)
        self.assertEqual(class_rank("CRITICAL"), 3)
        with self.assertRaises(RiskConfigError):
            class_rank("MELTDOWN")


class ScoreZoneTests(unittest.TestCase):
    def test_all_zero_is_normal(self) -> None:
        result = score_zone(metrics())
        self.assertEqual(result["risk_score"], 0.0)
        self.assertEqual(result["risk_class_candidate"], "NORMAL")
        self.assertEqual(result["base_score"], 0.0)
        self.assertEqual(result["trend"]["contribution"], 0.0)
        self.assertIsNone(result["trend"]["rise_fraction"])
        # all contributions are zero -> deterministic alphabetical tie-break
        self.assertEqual(result["top_drivers"], sorted(METRIC_ORDER)[:3])

    def test_all_at_watch_is_watch_at_boundary(self) -> None:
        result = score_zone(at_watch())
        self.assertEqual(result["risk_score"], 0.5)
        self.assertEqual(result["risk_class_candidate"], "WATCH")

    def test_rainfall_critical_others_watch_is_warning(self) -> None:
        # 0.30 * 1.0 + (0.15 + 0.20 + 0.25 + 0.10) * 0.5 = 0.30 + 0.35 = 0.65
        values = at_watch()
        values["rainfall_mm_per_hr"] = DEFAULT_THRESHOLDS["rainfall_mm_per_hr"]["critical"]
        result = score_zone(values)
        self.assertEqual(result["risk_score"], 0.65)
        self.assertEqual(result["risk_class_candidate"], "WARNING")
        self.assertEqual(result["top_drivers"][0], "rainfall_mm_per_hr")

    def test_all_at_critical_is_critical(self) -> None:
        result = score_zone(at_critical())
        self.assertEqual(result["risk_score"], 1.0)
        self.assertEqual(result["risk_class_candidate"], "CRITICAL")

    def test_full_reference_rise_adds_ten_points(self) -> None:
        water_now = DEFAULT_THRESHOLDS["water_level_m"]["watch"]
        values = at_watch()
        result = score_zone(values, water_level_5min_ago=water_now - 1.0)
        self.assertEqual(result["trend"]["water_level_delta_m"], 1.0)
        self.assertEqual(result["trend"]["rise_fraction"], 1.0)
        self.assertEqual(result["trend"]["contribution"], 0.1)
        self.assertEqual(result["risk_score"], 0.6)
        self.assertEqual(result["risk_class_candidate"], "WARNING")

    def test_falling_water_level_adds_nothing(self) -> None:
        water_now = DEFAULT_THRESHOLDS["water_level_m"]["watch"]
        result = score_zone(at_watch(), water_level_5min_ago=water_now + 0.8)
        self.assertEqual(result["trend"]["water_level_delta_m"], -0.8)
        self.assertEqual(result["trend"]["rise_fraction"], 0.0)
        self.assertEqual(result["risk_score"], 0.5)

    def test_rise_is_capped_at_reference_rise(self) -> None:
        water_now = DEFAULT_THRESHOLDS["water_level_m"]["watch"]
        result = score_zone(at_watch(), water_level_5min_ago=water_now - 9.0)
        self.assertEqual(result["trend"]["rise_fraction"], 1.0)
        self.assertEqual(result["risk_score"], 0.6)

    def test_trend_compares_like_with_like_using_raw_water_levels(self) -> None:
        # smoothed metric value 1.5, but the raw current level is 2.0 and the raw
        # reading 5 minutes ago was 1.0 -> the full 1.0 m rise must be measured.
        smoothed = metrics(water=1.5)
        result = score_zone(smoothed, water_level_5min_ago=1.0, water_level_now=2.0)
        self.assertEqual(result["trend"]["water_level_delta_m"], 1.0)
        self.assertEqual(result["trend"]["contribution"], 0.1)

    def test_contributions_match_weight_times_sub_score(self) -> None:
        result = score_zone(at_critical())
        for item in result["contributions"]:
            with self.subTest(variable=item["variable"]):
                self.assertAlmostEqual(
                    item["contribution"],
                    round(item["weight"] * item["sub_score"], 4),
                    places=6,
                )
        self.assertAlmostEqual(
            round(sum(item["contribution"] for item in result["contributions"]), 4),
            result["base_score"],
            places=6,
        )

    def test_explanation_names_the_dominant_driver(self) -> None:
        result = score_zone(at_critical())
        self.assertIn("dominant driver", result["explanation"])
        self.assertIn("rainfall_mm_per_hr", result["explanation"])
        self.assertEqual(result["model"], "heuristic-v1")
        self.assertIn("not a calibrated hydrological model", result["model_note"])

    def test_zone_override_changes_the_score(self) -> None:
        values = metrics(water=1.8)
        default_result = score_zone(values, DEFAULT_THRESHOLDS)
        override_result = score_zone(values, thresholds_for("zone-old-town-canal"))
        self.assertEqual(default_result["risk_score"], 0.1125)  # 0.25 * (0.5 * 1.8/2.0)
        self.assertEqual(override_result["risk_score"], 0.125)  # 0.25 * 0.5 (at watch)

    def test_missing_metric_raises(self) -> None:
        incomplete = {"rainfall_mm_per_hr": 1.0}
        with self.assertRaises(ValueError):
            score_zone(incomplete)


class RollingMeanTests(unittest.TestCase):
    def test_mean_of_last_three(self) -> None:
        series = [metrics(rain=value) for value in (1.0, 2.0, 3.0, 30.0)]
        smoothed = rolling_mean_metrics(series, window=3)
        self.assertEqual(smoothed["rainfall_mm_per_hr"], (2.0 + 3.0 + 30.0) / 3)

    def test_single_reading_returns_itself(self) -> None:
        smoothed = rolling_mean_metrics([metrics(rain=4.0, water=1.0)], window=3)
        self.assertEqual(smoothed["rainfall_mm_per_hr"], 4.0)
        self.assertEqual(smoothed["water_level_m"], 1.0)

    def test_empty_series_raises(self) -> None:
        with self.assertRaises(ValueError):
            rolling_mean_metrics([], window=3)


class HysteresisTests(unittest.TestCase):
    def test_first_observation_is_adopted_immediately(self) -> None:
        state = apply_hysteresis("CRITICAL", None)
        self.assertEqual(state["risk_class"], "CRITICAL")
        self.assertEqual(state["transition"], "initial")

    def test_escalation_needs_two_consecutive_readings(self) -> None:
        state = apply_hysteresis("NORMAL", None)
        first = apply_hysteresis("WARNING", state)
        self.assertEqual(first["risk_class"], "NORMAL")
        self.assertEqual(first["transition"], "pending")
        self.assertEqual(first["streak_count"], 1)

        second = apply_hysteresis("WARNING", first)
        self.assertEqual(second["risk_class"], "WARNING")
        self.assertEqual(second["transition"], "escalate")
        self.assertEqual(second["streak_count"], 0)

    def test_deescalation_needs_three_consecutive_readings(self) -> None:
        state = apply_hysteresis("CRITICAL", None)
        state = apply_hysteresis("NORMAL", state)
        self.assertEqual(state["transition"], "pending")
        state = apply_hysteresis("NORMAL", state)
        self.assertEqual(state["transition"], "pending")
        self.assertEqual(state["risk_class"], "CRITICAL")
        state = apply_hysteresis("NORMAL", state)
        self.assertEqual(state["transition"], "deescalate")
        self.assertEqual(state["risk_class"], "NORMAL")

    def test_holding_the_same_class_resets_the_streak(self) -> None:
        state = apply_hysteresis("WATCH", None)
        pending = apply_hysteresis("CRITICAL", state)
        self.assertEqual(pending["streak_count"], 1)
        held = apply_hysteresis("WATCH", pending)
        self.assertEqual(held["transition"], "hold")
        self.assertIsNone(held["streak_class"])
        self.assertEqual(held["streak_count"], 0)

    def test_changing_candidate_restarts_the_streak(self) -> None:
        state = apply_hysteresis("NORMAL", None)
        pending = apply_hysteresis("WATCH", state)
        switched = apply_hysteresis("CRITICAL", pending)
        self.assertEqual(switched["risk_class"], "NORMAL")
        self.assertEqual(switched["streak_count"], 1)
        self.assertEqual(switched["streak_class"], "CRITICAL")

    def test_unknown_candidate_raises(self) -> None:
        with self.assertRaises(RiskConfigError):
            apply_hysteresis("SEVERE", None)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

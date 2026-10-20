"""M16 temporal contract: the shared implementation against the fixture and its edge matrix.

Everything here goes through ``tests/fixtures/m16_temporal_cases.json`` - the cross-language
contract the frontend mirrors in ``web/lib.js`` - and through the one Python implementation
the analysis tool also imports (``floodpulse.temporal``). No AWS, no network, no wall clock:
``now_epoch`` is always injected.

Trend labels are asserted exactly (never "one of RISING/STEADY/FALLING"), numbers are compared
with a tolerance, and band labels are asserted against the engine's own ``risk.classify`` so
the Python side and the page cannot drift apart.
"""

from __future__ import annotations

import json
import math
import unittest
from pathlib import Path

from floodpulse import temporal
from floodpulse.risk import classify

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "m16_temporal_cases.json"


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def rows_from(points: list[list], base_epoch: int) -> list[list]:
    """The fixture's ``[offset_seconds, score, rainfall]`` triples as absolute samples."""
    return [[base_epoch + offset, score, rainfall] for offset, score, rainfall in points]


class TemporalTestCase(unittest.TestCase):
    maxDiff = None

    def assertClose(self, actual, expected, message: str = "") -> None:
        self.assertIsInstance(actual, (int, float), f"{message}: {actual!r} is not a number")
        self.assertTrue(
            math.isclose(float(actual), float(expected), rel_tol=1e-9, abs_tol=1e-9),
            f"{message}: {actual!r} != {expected!r}",
        )


class FixtureRuleTests(TemporalTestCase):
    """The fixture's ``rules`` block is the implementation's constants, spelled the same way."""

    def test_fixture_rules_are_the_implementation_constants(self) -> None:
        rules = load_fixture()["rules"]
        for key, value in temporal.RULE_CONSTANTS.items():
            with self.subTest(rule=key):
                self.assertIn(key, rules)
                self.assertClose(rules[key], value, key)
        # The constants the module names directly are the same ones the mapping exposes.
        self.assertEqual(temporal.TEMPORAL_MIN_SAMPLES, rules["min_samples"])
        self.assertEqual(temporal.TEMPORAL_MIN_DURATION_SECONDS, rules["min_duration_seconds"])
        self.assertClose(temporal.TEMPORAL_SLOPE_TOLERANCE_PER_HOUR, rules["slope_tolerance_per_hour"])
        self.assertClose(temporal.TEMPORAL_GAP_MEDIAN_MULTIPLIER, rules["gap_median_multiplier"])
        self.assertClose(temporal.TEMPORAL_GAP_FLOOR_SECONDS, rules["gap_floor_seconds"])


class FixtureCaseTests(TemporalTestCase):
    """Every case in the fixture, through the shared implementation."""

    def setUp(self) -> None:
        self.fixture = load_fixture()
        self.base_epoch = self.fixture["base_epoch"]

    def test_every_case_matches_its_recorded_expectation(self) -> None:
        cases = self.fixture["cases"]
        self.assertGreaterEqual(len(cases), 17, "the fixture lost cases")
        for case in cases:
            with self.subTest(case=case["name"]):
                summary = self.summarise_case(case)
                recorded = summary.as_dict()
                for key, expected in case["expect"].items():
                    if key == "slope_sign":
                        continue
                    if key == "classes":
                        self.assertEqual(self.classes_of(case), expected, key)
                        continue
                    actual = recorded.get(key, "<missing>")
                    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
                        self.assertClose(actual, expected, key)
                    else:
                        self.assertEqual(actual, expected, key)

    def test_a_case_without_a_direction_claims_none(self) -> None:
        """No direction is ever claimed for a window the rules call insufficient."""
        for case in self.fixture["cases"]:
            with self.subTest(case=case["name"]):
                summary = self.summarise_case(case)
                if "trend" in case["expect"]:
                    # The fixture names the label; the module must produce exactly that one.
                    self.assertEqual(summary.trend, case["expect"]["trend"])
                    continue
                if summary.status != temporal.STATUS_OK:
                    self.assertIsNone(summary.trend, "an insufficient window named a direction")
                    continue
                # An OK window still follows the slope rule; the fixture simply does not pin
                # the label (band_crossings pins the slope's sign instead).
                self.assertEqual(summary.trend, trend_from_slope(summary.slope_per_hour))

    def test_the_peaked_window_follows_its_slope_and_not_its_shape(self) -> None:
        """band_crossings rises, peaks, then falls: trend is the slope, peak and net change stand alone."""
        case = self.case_named("band_crossings")
        self.assertEqual(case["expect"]["slope_sign"], "positive")
        summary = self.summarise_case(case)
        self.assertGreater(summary.slope_per_hour, temporal.TEMPORAL_SLOPE_TOLERANCE_PER_HOUR)
        self.assertEqual(summary.trend, temporal.TREND_RISING)
        # The last sample is far below the peak, so a "shape" reading would have said FALLING.
        self.assertClose(summary.net_change, 0.04)
        self.assertClose(summary.last_score, 0.24)
        self.assertClose(summary.peak, 0.85)
        self.assertLess(summary.last_score, summary.peak)

    def test_band_crossings_uses_the_engines_own_bands(self) -> None:
        case = self.case_named("band_crossings")
        scores = [score for _offset, score, _rain in case["points"]]
        expected = case["expect"]["classes"]
        self.assertEqual(expected, ["NORMAL", "WATCH", "WARNING", "CRITICAL", "WARNING", "WATCH", "NORMAL"])
        self.assertEqual([classify(score) for score in scores], expected)
        self.assertEqual([temporal.band_for(score) for score in scores], expected)
        self.assertEqual(self.summarise_case(case).classes, expected)

    def test_classification_is_exact_for_each_trend_case(self) -> None:
        """The trend labels, one assertion each, in the fixture's own names."""
        expected = {
            "flat": "STEADY",
            "rising": "RISING",
            "falling": "FALLING",
            "noisy_flat": "STEADY",
            "slow_rise_above_tolerance": "RISING",
            "slow_rise_below_tolerance": "STEADY",
            "duplicate_timestamp": "RISING",
            "out_of_order": "RISING",
            "invalid_rows": "RISING",
            "out_of_range_scores": "RISING",
            "large_gap": "RISING",
            "no_gap_in_uneven_cadence": "RISING",
            "stale_newest": "RISING",
        }
        for name, trend in expected.items():
            with self.subTest(case=name):
                self.assertEqual(self.summarise_case(self.case_named(name)).trend, trend)

    def test_insufficient_cases_name_the_reason(self) -> None:
        expected = {
            "empty": temporal.REASON_NO_SAMPLES,
            "one_sample": temporal.REASON_TOO_FEW_SAMPLES,
            "two_samples": temporal.REASON_TOO_FEW_SAMPLES,
        }
        for name, reason in expected.items():
            with self.subTest(case=name):
                summary = self.summarise_case(self.case_named(name))
                self.assertEqual(summary.status, temporal.STATUS_INSUFFICIENT)
                self.assertEqual(summary.reason, reason)

    def test_case_scores_are_the_fixtures_own_numbers(self) -> None:
        """Guard the guard: if the fixture is re-scored, this test says which case moved."""
        case = self.case_named("rising")
        summary = self.summarise_case(case)
        self.assertClose(summary.first_score, 0.10)
        self.assertClose(summary.last_score, 0.31)
        self.assertClose(summary.peak, 0.31)
        self.assertClose(summary.min, 0.10)
        self.assertClose(summary.net_change, 0.21)
        self.assertClose(summary.last_rainfall, 5.8)
        self.assertClose(summary.max_rainfall, 5.8)

    # -- helpers ----------------------------------------------------------- #

    def case_named(self, name: str) -> dict:
        for case in self.fixture["cases"]:
            if case["name"] == name:
                return case
        raise AssertionError(f"the fixture no longer has a case named {name!r}")

    def summary_rows(self, case: dict) -> list[list]:
        return rows_from(case["points"], self.base_epoch)

    def summarise_case(self, case: dict):
        rows = self.summary_rows(case)
        now_epoch = None
        if case["expect"].get("newest_age_seconds") is not None and rows:
            now_epoch = max(row[0] for row in rows) + case["expect"]["newest_age_seconds"]
        return temporal.analyse(rows, now_epoch=now_epoch)

    def classes_of(self, case: dict) -> list[str]:
        scores = [score for _offset, score, _rain in case["points"]]
        return [temporal.band_for(score) for score in scores]


def trend_from_slope(slope: float | None) -> str | None:
    """The trend rule, written once: STEADY inside the tolerance, otherwise the slope's sign."""
    if slope is None:
        return None
    if slope > temporal.TEMPORAL_SLOPE_TOLERANCE_PER_HOUR:
        return temporal.TREND_RISING
    if slope < -temporal.TEMPORAL_SLOPE_TOLERANCE_PER_HOUR:
        return temporal.TREND_FALLING
    return temporal.TREND_STEADY


class FixtureIncidentTests(TemporalTestCase):
    """The fixture's incident cases: a record states its own outcome and nothing is inferred."""

    def setUp(self) -> None:
        self.fixture = load_fixture()

    def test_every_incident_case_matches_its_expectation(self) -> None:
        for case in self.fixture["incident_cases"]:
            with self.subTest(case=case["name"]):
                recorded = temporal.incident_summary(case["incident"], now_epoch=self.fixture["base_epoch"])
                for key, expected in case["expect"].items():
                    actual = recorded.get(key, "<missing>")
                    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
                        self.assertClose(actual, expected, key)
                    else:
                        self.assertEqual(actual, expected, key)

    def test_a_record_that_states_nothing_reports_unknown(self) -> None:
        summary = temporal.incident_summary({"zone_id": "city-mumbai"})
        self.assertIsNone(summary["open"], "an absent status was turned into an open state")
        self.assertEqual(summary["severity"], temporal.SEVERITY_UNKNOWN)
        self.assertIsNone(summary["duration_seconds"])
        self.assertFalse(summary["escalated"])
        self.assertIsNone(summary["opened_ts_epoch"])

    def test_escalation_needs_both_recorded_severities(self) -> None:
        record = {"opened_ts_epoch": 1000, "closed_ts_epoch": 2000, "severity": "CRITICAL"}
        self.assertFalse(temporal.incident_summary(record)["escalated"], "escalation was inferred")
        named = dict(record, severity_at_open="WARNING")
        self.assertTrue(temporal.incident_summary(named)["escalated"])
        self.assertClose(temporal.incident_summary(named)["duration_seconds"], 1000)


class TemporalEdgeMatrixTests(TemporalTestCase):
    """The boundary matrix: shapes, unusable rows, counters and the gap/trend boundaries."""

    BASE = 1_792_400_000

    def rows(self, *pairs: tuple[float | None, float | None], step: int = 600) -> list[list]:
        """``(score, rainfall)`` pairs, ``step`` seconds apart, starting at :attr:`BASE`."""
        return [
            [self.BASE + index * step, score, rainfall]
            for index, (score, rainfall) in enumerate(pairs)
        ]

    def analyse(self, rows, now_epoch=None):
        return temporal.analyse(rows, now_epoch=now_epoch)

    def test_no_samples_invent_nothing(self) -> None:
        summary = self.analyse([])
        self.assertEqual(summary.count, 0)
        self.assertEqual(summary.status, temporal.STATUS_INSUFFICIENT)
        self.assertEqual(summary.reason, temporal.REASON_NO_SAMPLES)
        for field in ("trend", "net_change", "peak", "min", "first_score", "last_score",
                      "first_ts", "last_ts", "duration_seconds", "median_interval",
                      "largest_gap", "gap_limit", "max_rainfall", "newest_age_seconds"):
            with self.subTest(field=field):
                self.assertIsNone(getattr(summary, field))
        self.assertEqual(summary.classes, [])
        self.assertEqual(summary.gap_count, 0)

    def test_one_sample_has_a_score_but_no_direction(self) -> None:
        summary = self.analyse(self.rows((0.20, None)))
        self.assertEqual(summary.count, 1)
        self.assertEqual(summary.reason, temporal.REASON_TOO_FEW_SAMPLES)
        self.assertIsNone(summary.trend)
        self.assertClose(summary.net_change, 0.0)
        self.assertClose(summary.peak, 0.20)
        self.assertClose(summary.min, 0.20)
        self.assertEqual(summary.duration_seconds, 0)
        self.assertIsNone(summary.median_interval)
        self.assertIsNone(summary.largest_gap)

    def test_two_samples_have_a_change_but_no_direction(self) -> None:
        summary = self.analyse(self.rows((0.20, None), (0.30, None)))
        self.assertEqual(summary.count, 2)
        self.assertEqual(summary.reason, temporal.REASON_TOO_FEW_SAMPLES)
        self.assertIsNone(summary.trend)
        self.assertClose(summary.net_change, 0.10)
        self.assertClose(summary.median_interval, 600)
        self.assertClose(summary.largest_gap, 600)
        self.assertEqual(summary.gap_count, 0)

    def test_three_samples_spanning_exactly_the_minimum_is_enough(self) -> None:
        summary = self.analyse(self.rows((0.20, None), (0.24, None), (0.28, None), step=300))
        self.assertEqual(summary.count, 3)
        self.assertEqual(summary.duration_seconds, 600)
        self.assertEqual(summary.status, temporal.STATUS_OK)
        self.assertEqual(summary.trend, temporal.TREND_RISING)
        self.assertIsNone(summary.reason)

    def test_three_samples_inside_the_minimum_duration_are_insufficient(self) -> None:
        rows = [[self.BASE, 0.20, 0.0], [self.BASE + 300, 0.24, 0.0], [self.BASE + 599, 0.28, 0.0]]
        summary = self.analyse(rows)
        self.assertEqual(summary.count, 3, "the count rule was not the one that fired")
        self.assertEqual(summary.status, temporal.STATUS_INSUFFICIENT)
        self.assertEqual(summary.reason, temporal.REASON_TOO_SHORT)
        self.assertIsNone(summary.trend)
        # The window is still described: only the direction is withheld.
        self.assertClose(summary.peak, 0.28)
        self.assertClose(summary.net_change, 0.08)

    def test_flat_rising_falling_and_noisy_flat(self) -> None:
        cases = {
            "flat": ([0.20, 0.20, 0.20, 0.20], temporal.TREND_STEADY),
            "rising": ([0.10, 0.16, 0.22, 0.31], temporal.TREND_RISING),
            "falling": ([0.44, 0.36, 0.30, 0.21], temporal.TREND_FALLING),
            "noisy_flat": ([0.20, 0.21, 0.19, 0.20, 0.21], temporal.TREND_STEADY),
        }
        for name, (scores, trend) in cases.items():
            with self.subTest(shape=name):
                summary = self.analyse(self.rows(*[(score, None) for score in scores]))
                self.assertEqual(summary.trend, trend)
                self.assertEqual(summary.status, temporal.STATUS_OK)

    def test_the_slope_tolerance_is_a_band_not_a_cliff(self) -> None:
        # 0.015/hour is inside the +/- 0.02 tolerance; 0.03/hour is outside it.
        inside = self.analyse([[self.BASE, 0.2000, 0.0], [self.BASE + 3600, 0.2150, 0.0], [self.BASE + 7200, 0.2300, 0.0]])
        outside = self.analyse([[self.BASE, 0.2000, 0.0], [self.BASE + 3600, 0.2300, 0.0], [self.BASE + 7200, 0.2600, 0.0]])
        self.assertClose(inside.slope_per_hour, 0.015)
        self.assertEqual(inside.trend, temporal.TREND_STEADY)
        self.assertClose(outside.slope_per_hour, 0.030)
        self.assertEqual(outside.trend, temporal.TREND_RISING)

    def test_duplicate_timestamp_keeps_the_later_row(self) -> None:
        rows = [
            [self.BASE, 0.20, 0.0],
            [self.BASE + 600, 0.30, 1.0],
            [self.BASE + 600, 0.34, 1.5],
            [self.BASE + 1200, 0.40, 2.0],
        ]
        series = temporal.clean_series(rows)
        middle = series.samples[1]
        self.assertClose(middle.score, 0.34, "the earlier row for a shared timestamp won")
        self.assertClose(middle.rainfall, 1.5)
        self.assertEqual(series.duplicates_removed, 1)
        summary = temporal.summarise(series)
        self.assertEqual(summary.count, 3)
        self.assertClose(summary.net_change, 0.20)
        self.assertClose(summary.median_interval, 600, "the duplicate became a zero-length interval")

    def test_out_of_order_arrivals_are_sorted_before_anything_is_measured(self) -> None:
        arrivals = [
            [self.BASE + 1200, 0.40, 0.0],
            [self.BASE, 0.20, 0.0],
            [self.BASE + 600, 0.30, 0.0],
        ]
        summary = self.analyse(arrivals)
        self.assertEqual(summary.count, 3)
        self.assertClose(summary.first_score, 0.20)
        self.assertClose(summary.last_score, 0.40)
        self.assertEqual(summary.out_of_order_pairs, 2)
        self.assertEqual(summary.trend, temporal.TREND_RISING)

    def test_unusable_timestamps_are_dropped(self) -> None:
        rows = [
            [self.BASE, 0.20, 0.0],
            [None, 0.21, 0.0],
            ["", 0.22, 0.0],
            ["not-a-date", 0.23, 0.0],
            {"ts_epoch": None, "risk_score": 0.24},
            {"risk_score": 0.25},
        ]
        series = temporal.clean_series(rows)
        self.assertEqual(len(series.samples), 1)
        self.assertEqual(series.invalid_dropped, 5)
        self.assertEqual(series.samples[0].score, 0.20)

    def test_a_missing_epoch_falls_back_to_the_iso_timestamp(self) -> None:
        rows = [
            {"timestamp": "2027-01-01T00:00:00Z", "risk_score": 0.20},
            {"timestamp": "2027-01-01T00:10:00Z", "risk_score": 0.30},
            {"timestamp": "2027-01-01T00:20:00Z", "risk_score": 0.40},
        ]
        summary = temporal.analyse(rows)
        self.assertEqual(summary.count, 3)
        self.assertEqual(summary.invalid_dropped, 0)
        self.assertEqual(summary.duration_seconds, 1200)
        self.assertEqual(summary.trend, temporal.TREND_RISING)

    def test_null_and_non_numeric_scores_are_dropped(self) -> None:
        rows = [
            [self.BASE, 0.20, 0.0],
            [self.BASE + 600, None, 0.0],
            [self.BASE + 1200, "x", 0.0],
            {"ts_epoch": self.BASE + 1800, "risk_score": None},
            {"ts_epoch": self.BASE + 2400},
            [self.BASE + 3000, 0.35, 1.4],
        ]
        series = temporal.clean_series(rows)
        self.assertEqual([sample.score for sample in series.samples], [0.20, 0.35])
        self.assertEqual(series.invalid_dropped, 4)

    def test_nan_and_infinity_never_become_samples(self) -> None:
        rows = [
            [self.BASE, 0.20, 0.0],
            [self.BASE + 600, float("nan"), 0.0],
            [self.BASE + 1200, float("inf"), 0.0],
            [self.BASE + 1800, float("-inf"), 0.0],
            [self.BASE + 2400, "NaN", 0.0],
            [self.BASE + 3000, "Infinity", 0.0],
            [self.BASE + 3600, 0.30, float("nan")],
        ]
        series = temporal.clean_series(rows)
        self.assertEqual([sample.score for sample in series.samples], [0.20, 0.30])
        self.assertEqual(series.invalid_dropped, 5)
        self.assertIsNone(series.samples[-1].rainfall, "a NaN rainfall was turned into a number")

    def test_scores_outside_zero_to_one_are_dropped_and_the_edges_are_kept(self) -> None:
        rows = [
            [self.BASE, 0.0, 0.0],
            [self.BASE + 600, -0.4, 0.0],
            [self.BASE + 1200, 1.7, 0.0],
            [self.BASE + 1800, 1.0, 0.0],
        ]
        series = temporal.clean_series(rows)
        self.assertEqual([sample.score for sample in series.samples], [0.0, 1.0])
        self.assertEqual(series.invalid_dropped, 2)
        self.assertEqual(
            [temporal.band_for(sample.score) for sample in series.samples], ["NORMAL", "CRITICAL"]
        )

    def test_missing_rainfall_is_reported_as_unknown(self) -> None:
        summary = self.analyse(self.rows((0.20, None), (0.24, None), (0.28, None)))
        self.assertEqual(summary.rainfall_samples, 0)
        for field in ("first_rainfall", "last_rainfall", "newest_rainfall", "min_rainfall", "max_rainfall"):
            with self.subTest(field=field):
                self.assertIsNone(getattr(summary, field))

    def test_the_newest_sample_is_not_the_newest_rainfall_by_default(self) -> None:
        """``newest_rainfall`` is the newest row's own value; the aggregates are over known values."""
        summary = self.analyse(self.rows((0.20, 2.0), (0.24, None), (0.28, None)))
        self.assertClose(summary.first_rainfall, 2.0)
        self.assertClose(summary.last_rainfall, 2.0)
        self.assertClose(summary.max_rainfall, 2.0)
        self.assertEqual(summary.rainfall_samples, 1)
        self.assertIsNone(summary.newest_rainfall, "a missing reading was filled from an older row")

    def test_rainfall_is_read_from_the_api_point_shape(self) -> None:
        rows = [
            {"ts_epoch": self.BASE, "risk_score": 0.20, "readings": {"rainfall_mm_per_hr": 3.5}},
            {"ts_epoch": self.BASE + 600, "risk_score": 0.24, "readings": {"rainfall_mm_per_hr": "4.5"}},
            {"ts_epoch": self.BASE + 1200, "risk_score": 0.28, "readings": {}},
        ]
        summary = temporal.analyse(rows)
        self.assertEqual(summary.rainfall_samples, 2, "a nested or numeric-string reading was missed")
        self.assertClose(summary.max_rainfall, 4.5)
        self.assertIsNone(summary.newest_rainfall)

    def test_the_gap_rule_is_the_larger_of_the_median_rule_and_the_floor(self) -> None:
        # The floor decides when the cadence is fast: 300 s cadence, 1000 s hole.
        floor_case = self.analyse(
            [[self.BASE, 0.20, 0.0], [self.BASE + 300, 0.22, 0.0], [self.BASE + 600, 0.24, 0.0],
             [self.BASE + 1600, 0.30, 0.0]]
        )
        self.assertClose(floor_case.median_interval, 300)
        self.assertClose(floor_case.gap_limit, 900, "the 900 s floor was not applied")
        self.assertClose(floor_case.largest_gap, 1000)
        self.assertEqual(floor_case.gap_count, 1)
        # The median rule decides when the cadence is slow: 10 min cadence, 38 min hole.
        median_case = self.analyse(
            [[self.BASE, 0.20, 0.0], [self.BASE + 600, 0.22, 0.0], [self.BASE + 1200, 0.24, 0.0],
             [self.BASE + 3480, 0.30, 0.0], [self.BASE + 4080, 0.33, 0.0], [self.BASE + 4680, 0.36, 0.0]]
        )
        self.assertClose(median_case.median_interval, 600)
        self.assertClose(median_case.gap_limit, 1800, "the 3 x median rule was not applied")
        self.assertClose(median_case.largest_gap, 2280)
        self.assertEqual(median_case.gap_count, 1)
        # A hole the floor does not reach is not a gap.
        closed_case = self.analyse(
            [[self.BASE, 0.20, 0.0], [self.BASE + 600, 0.22, 0.0], [self.BASE + 1200, 0.24, 0.0],
             [self.BASE + 1800, 0.30, 0.0]]
        )
        self.assertEqual(closed_case.gap_count, 0)

    def test_a_stale_newest_sample_is_still_described_and_dated(self) -> None:
        rows = self.rows((0.20, 0.0), (0.24, 0.0), (0.28, 0.0))
        newest = rows[-1][0]
        summary = temporal.analyse(rows, now_epoch=newest + 2520)
        self.assertClose(summary.newest_age_seconds, 2520)
        self.assertEqual(summary.status, temporal.STATUS_OK)
        self.assertEqual(summary.trend, temporal.TREND_RISING)
        ahead = temporal.analyse(rows, now_epoch=newest - 600)
        self.assertClose(ahead.newest_age_seconds, 0, "a sample from the future was called old")

    def test_the_band_round_trip_is_exact(self) -> None:
        scores = [0.20, 0.38, 0.64, 0.85, 0.70, 0.45, 0.24]
        summary = self.analyse(self.rows(*[(score, 0.0) for score in scores]))
        self.assertEqual(
            summary.classes,
            ["NORMAL", "WATCH", "WARNING", "CRITICAL", "WARNING", "WATCH", "NORMAL"],
        )
        self.assertEqual([classify(score) for score in scores], summary.classes)
        self.assertClose(summary.peak, 0.85)
        self.assertClose(summary.min, 0.20)
        self.assertEqual(summary.trend, temporal.TREND_RISING)

    def test_a_stored_class_label_is_not_recomputed(self) -> None:
        rows = [
            {"ts_epoch": self.BASE, "risk_score": 0.20, "risk_class": "WARNING"},
            {"ts_epoch": self.BASE + 600, "risk_score": 0.21, "risk_class": "WARNING"},
            {"ts_epoch": self.BASE + 1200, "risk_score": 0.22, "risk_class": "WARNING"},
        ]
        summary = temporal.analyse(rows)
        self.assertEqual(
            summary.classes,
            ["WARNING", "WARNING", "WARNING"],
            "the effective (hysteresis-filtered) class stored with the row was overwritten",
        )

    def test_the_counters_are_independent(self) -> None:
        rows = [
            [self.BASE + 1200, 0.40, 0.0],
            [self.BASE, 0.20, 0.0],
            [self.BASE + 600, 0.30, 0.0],
            [self.BASE + 600, 0.34, 0.0],
            [None, 0.50, 0.0],
        ]
        series = temporal.clean_series(rows)
        self.assertEqual(series.invalid_dropped, 1)
        self.assertEqual(series.duplicates_removed, 1)
        self.assertEqual(series.out_of_order_pairs, 3)
        self.assertEqual([sample.score for sample in series.samples], [0.20, 0.34, 0.40])

    def test_as_dict_is_json_ready_with_whole_seconds_as_integers(self) -> None:
        summary = self.analyse(self.rows((0.20, 0.0), (0.24, 0.0), (0.28, 0.0)))
        recorded = summary.as_dict()
        text = json.dumps(recorded, allow_nan=False)
        self.assertIn('"duration_seconds":1200', text.replace(" ", ""))
        self.assertIsInstance(recorded["duration_seconds"], int)
        self.assertIsInstance(recorded["median_interval"], int)
        self.assertIsNone(recorded["newest_age_seconds"])


if __name__ == "__main__":  # pragma: no cover - convenience only
    unittest.main()

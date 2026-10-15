"""Tests for the M4 live-weather ingestion path (hydraulics, cache rule, ingestion).

Everything runs offline against the in-memory store with an injected weather client, so
the suite never performs a network call.
"""

from __future__ import annotations

import json
import logging
import sys
import unittest

sys.path.insert(0, "src")

from floodpulse import live_weather, weather
from floodpulse.config import (
    DATA_CLASSIFICATION_MIXED,
    HYDRAULICS_RAIN_REFERENCE_MM_PER_HR,
    LIVE_ZONE_DEFINITIONS,
    METRIC_SOURCES_LIVE,
    METRICS,
    WEATHER_CACHE_SORT_KEY,
    WEATHER_MODE_ENV,
    WEATHER_MODE_LIVE,
    WEATHER_MODE_OFF,
    WEATHER_REFRESH_SECONDS,
    ZONE_MODE_LIVE,
    ZONE_MODE_ENV,
)
from floodpulse.handler import dispatch
from floodpulse.storage import LocalStore
from floodpulse.weather import Observation, WeatherClient

CITY = "city-chennai"
PROVIDER_TIME = "2026-10-15T18:15"
SAMPLE_TIME = "2026-10-15T18:00"
CURRENT_EPOCH = 1_792_088_100.0
NOW = 1_792_088_400.0


def observation(zone_id: str = CITY, *, rain: float = 12.5, soil: float = 0.24, ok: bool = True) -> Observation:
    return Observation(
        zone_id=zone_id,
        ok=ok,
        rainfall_mm_per_hr=rain if ok else None,
        soil_moisture_m3m3=soil if ok else None,
        soil_saturation_pct=weather.soil_saturation_pct(soil) if ok else None,
        provider_current_time=PROVIDER_TIME,
        observed_at=SAMPLE_TIME,
        observed_at_epoch=CURRENT_EPOCH,
        latitude=13.0827,
        longitude=80.2707,
        error=None if ok else "provider_said_no",
    )


class FakeClient:
    """A weather client with no network: returns prepared observations and counts calls."""

    def __init__(self, observations=None, *, error=None, ok=True):
        self.calls = 0
        self.observations = observations or {}
        self.error = error
        self.ok = ok

    def fetch(self, zones):
        self.calls += 1
        payload = {
            str(zone["zone_id"]): (self.observations.get(str(zone["zone_id"])) or observation(str(zone["zone_id"])))
            for zone in zones
        }
        if not self.ok:
            return weather.FetchResult(ok=False, error=self.error or "http_503", requested=len(zones))
        return weather.FetchResult(ok=True, observations=payload, fetched_at_epoch=NOW, requested=len(zones))


LIVE_ENV = {ZONE_MODE_ENV: "live", WEATHER_MODE_ENV: WEATHER_MODE_LIVE}


class ZoneModeTest(unittest.TestCase):
    """The mode switch decides which fleet is live; a typo must never enable it."""

    def test_short_and_canonical_spellings_agree(self):
        from floodpulse.config import zone_mode_for_env

        for spelling in ("live", "LIVE", " live_weather "):
            self.assertEqual(zone_mode_for_env({ZONE_MODE_ENV: spelling}), "live_weather")
        for spelling in ("demo", "simulated", "DEMO"):
            self.assertEqual(zone_mode_for_env({ZONE_MODE_ENV: spelling}), "simulated")

    def test_missing_or_unknown_mode_falls_back_to_the_simulator(self):
        from floodpulse.config import zone_mode_for_env

        self.assertEqual(zone_mode_for_env({}), "simulated")
        self.assertEqual(zone_mode_for_env({ZONE_MODE_ENV: ""}), "simulated")
        self.assertEqual(zone_mode_for_env({ZONE_MODE_ENV: "liveweather"}), "simulated")

    def test_live_mode_selects_five_city_zones_and_demo_selects_three(self):
        from floodpulse.config import zone_ids_for_mode

        self.assertEqual(len(zone_ids_for_mode("live")), 5)
        self.assertEqual(len(zone_ids_for_mode("live_weather")), 5)
        self.assertEqual(len(zone_ids_for_mode("demo")), 3)
        self.assertEqual(len(zone_ids_for_mode("simulated")), 3)

    def test_live_city_coordinates_match_the_documented_cities(self):
        expected = {
            "city-chennai": (13.0827, 80.2707),
            "city-mumbai": (19.0760, 72.8777),
            "city-bengaluru": (12.9716, 77.5946),
            "city-hyderabad": (17.3850, 78.4867),
            "city-kochi": (9.9312, 76.2673),
        }
        actual = {zone["zone_id"]: (zone["latitude"], zone["longitude"]) for zone in LIVE_ZONE_DEFINITIONS}
        self.assertEqual(actual, expected)

    def test_threshold_overrides_are_valid_for_the_live_cities(self):
        from floodpulse.config import thresholds_for

        for zone in LIVE_ZONE_DEFINITIONS:
            thresholds = thresholds_for(str(zone["zone_id"]))
            self.assertEqual(sorted(thresholds), sorted(METRICS))
            for metric, band in thresholds.items():
                self.assertLess(band["watch"], band["critical"], metric)


class HydraulicsTest(unittest.TestCase):
    def test_drain_baseline_matches_a_dry_calm_zone(self):
        self.assertAlmostEqual(live_weather.hydraulic_target_drain_m(0.0, 0.0), 0.15, places=3)

    def test_target_grows_with_rainfall(self):
        low = live_weather.hydraulic_target_drain_m(5.0, 40.0)
        high = live_weather.hydraulic_target_drain_m(40.0, 40.0)
        self.assertLess(low, high)

    def test_target_grows_with_soil_saturation(self):
        dry = live_weather.hydraulic_target_drain_m(20.0, 10.0)
        wet = live_weather.hydraulic_target_drain_m(20.0, 95.0)
        self.assertLess(dry, wet)

    def test_reference_rainfall_reaches_the_critical_end_of_the_range(self):
        # 45 mm/h on fully saturated soil is the documented "critical" reference point.
        target = live_weather.hydraulic_target_drain_m(HYDRAULICS_RAIN_REFERENCE_MM_PER_HR, 100.0)
        self.assertAlmostEqual(target, 0.15 + 2.35 + 0.80, places=3)

    def test_target_is_clamped_into_the_config_metric_range(self):
        limits = METRICS["drain_level_m"]
        target = live_weather.hydraulic_target_drain_m(10_000.0, 100.0)
        self.assertLessEqual(target, limits["max"])
        self.assertGreaterEqual(target, limits["min"])

    def test_first_observation_jumps_straight_to_the_target(self):
        self.assertEqual(
            live_weather.hydraulic_drain_level_m(30.0, 80.0, None, 0.0),
            live_weather.hydraulic_target_drain_m(30.0, 80.0),
        )

    def test_response_lags_and_never_overshoots_the_target(self):
        target = live_weather.hydraulic_target_drain_m(40.0, 90.0)
        drain = 0.2
        for _ in range(10):
            drain = live_weather.hydraulic_drain_level_m(40.0, 90.0, drain, 600.0)
            self.assertLess(drain, target)
            self.assertGreater(drain, 0.2)

    def test_lag_is_deterministic(self):
        first = live_weather.hydraulic_drain_level_m(25.0, 60.0, 1.0, 600.0)
        second = live_weather.hydraulic_drain_level_m(25.0, 60.0, 1.0, 600.0)
        self.assertEqual(first, second)

    def test_longer_gaps_react_more_than_short_gaps(self):
        short = live_weather.hydraulic_drain_level_m(40.0, 90.0, 0.2, 60.0)
        long = live_weather.hydraulic_drain_level_m(40.0, 90.0, 0.2, 3600.0)
        self.assertLess(short, long)

    def test_water_and_flow_are_clamped_into_range(self):
        water = live_weather.hydraulic_water_level_m(5.0, 100.0)
        flow = live_weather.hydraulic_flow_rate_m3_s(6.0, 250.0)
        self.assertLessEqual(water, METRICS["water_level_m"]["max"])
        self.assertLessEqual(flow, METRICS["flow_rate_m3_s"]["max"])
        self.assertGreaterEqual(flow, METRICS["flow_rate_m3_s"]["min"])

    def test_flow_follows_the_water_level(self):
        self.assertLess(
            live_weather.hydraulic_flow_rate_m3_s(0.2, 1.0),
            live_weather.hydraulic_flow_rate_m3_s(3.0, 1.0),
        )


class BuildMetricsTest(unittest.TestCase):
    def test_produces_all_five_metrics_inside_their_ranges(self):
        metrics = live_weather.build_live_metrics(observation(), previous=None, now_epoch=NOW)
        self.assertEqual(sorted(metrics), sorted(METRICS))
        for name, value in metrics.items():
            self.assertGreaterEqual(value, METRICS[name]["min"], name)
            self.assertLessEqual(value, METRICS[name]["max"], name)

    def test_real_values_are_passed_through_unchanged(self):
        metrics = live_weather.build_live_metrics(observation(rain=17.25, soil=0.3), previous=None, now_epoch=NOW)
        self.assertEqual(metrics["rainfall_mm_per_hr"], 17.25)
        self.assertEqual(metrics["soil_saturation_pct"], 60.0)

    def test_failed_observation_produces_nothing(self):
        self.assertIsNone(live_weather.build_live_metrics(observation(ok=False), previous=None, now_epoch=NOW))

    def test_previous_drain_level_is_used_as_the_starting_point(self):
        previous = {"readings": {"drain_level_m": 3.0}, "ts_epoch": NOW - 600}
        metrics = live_weather.build_live_metrics(observation(rain=1.0, soil=0.1), previous=previous, now_epoch=NOW)
        target = live_weather.hydraulic_target_drain_m(1.0, 10.0)
        # It relaxes down towards the calm target instead of jumping straight to it.
        self.assertLess(metrics["drain_level_m"], 3.0)
        self.assertGreater(metrics["drain_level_m"], target)


class RecordTest(unittest.TestCase):
    def test_record_is_tagged_per_metric_and_marked_mixed(self):
        metrics = live_weather.build_live_metrics(observation(), previous=None, now_epoch=NOW)
        record = live_weather.build_live_record(CITY, metrics, now_epoch=NOW, observation=observation())
        self.assertFalse(record["simulated"])
        self.assertEqual(record["data_classification"], DATA_CLASSIFICATION_MIXED)
        self.assertEqual(record["metric_sources"], METRIC_SOURCES_LIVE)
        self.assertEqual(record["metric_sources"]["rainfall_mm_per_hr"], "REAL")
        self.assertEqual(record["metric_sources"]["soil_saturation_pct"], "MODEL_DERIVED")
        self.assertEqual(record["metric_sources"]["drain_level_m"], "SIMULATED")
        self.assertEqual(record["source"], live_weather.LIVE_SOURCE)
        self.assertEqual(record["zone_id"], CITY)

    def test_record_is_timestamped_now_and_keeps_the_provider_time_separately(self):
        metrics = live_weather.build_live_metrics(observation(), previous=None, now_epoch=NOW)
        record = live_weather.build_live_record(CITY, metrics, now_epoch=NOW, observation=observation())
        self.assertEqual(record["ts_epoch"], int(NOW))
        self.assertEqual(record["provider_observed_at"], SAMPLE_TIME)
        self.assertEqual(record["provider"], "open-meteo")
        self.assertTrue(record["ttl"] > record["ts_epoch"])


class CacheRuleTest(unittest.TestCase):
    def test_no_cache_means_fetch(self):
        self.assertTrue(live_weather.needs_refresh(None, NOW, WEATHER_REFRESH_SECONDS))

    def test_fresh_cache_is_used_without_fetching(self):
        self.assertFalse(
            live_weather.needs_refresh({"fetched_at_epoch": NOW - 60}, NOW, WEATHER_REFRESH_SECONDS)
        )

    def test_cache_at_the_limit_is_refreshed(self):
        self.assertTrue(
            live_weather.needs_refresh({"fetched_at_epoch": NOW - 600}, NOW, WEATHER_REFRESH_SECONDS)
        )

    def test_two_polls_within_ten_minutes_cause_at_most_one_provider_call(self):
        store = LocalStore()
        client = FakeClient()
        env = dict(LIVE_ENV)
        for offset in (0, 5, 250, 599):
            dispatch("GET", "/zones", store=store, now_epoch=NOW + offset, env=env, weather_client=client)
        self.assertEqual(client.calls, 1)

    def test_after_ten_minutes_the_provider_is_called_again(self):
        store = LocalStore()
        client = FakeClient()
        env = dict(LIVE_ENV)
        dispatch("GET", "/zones", store=store, now_epoch=NOW, env=env, weather_client=client)
        dispatch("GET", "/zones", store=store, now_epoch=NOW + 601, env=env, weather_client=client)
        self.assertEqual(client.calls, 2)


class RefreshTest(unittest.TestCase):
    def setUp(self):
        self.store = LocalStore()
        self.ingested = []

    def ingest(self, record, obs):
        self.ingested.append((record, obs))
        return record

    def test_disabled_mode_never_calls_the_provider(self):
        client = FakeClient()
        outcome = live_weather.refresh_live_weather(
            self.store, mode=ZONE_MODE_LIVE, env={WEATHER_MODE_ENV: WEATHER_MODE_OFF},
            now_epoch=NOW, ingest=self.ingest, client=client,
        )
        self.assertFalse(outcome.enabled)
        self.assertFalse(outcome.attempted)
        self.assertEqual(client.calls, 0)

    def test_demo_mode_has_no_live_zones(self):
        client = FakeClient()
        outcome = live_weather.refresh_live_weather(
            self.store, mode="simulated", env=LIVE_ENV, now_epoch=NOW, ingest=self.ingest, client=client,
        )
        self.assertFalse(outcome.enabled)
        self.assertEqual(client.calls, 0)

    def test_first_refresh_ingests_every_live_zone(self):
        client = FakeClient()
        outcome = live_weather.refresh_live_weather(
            self.store, mode=ZONE_MODE_LIVE, env=LIVE_ENV, now_epoch=NOW,
            ingest=self.ingest, client=client,
        )
        self.assertTrue(outcome.enabled)
        self.assertTrue(outcome.attempted)
        self.assertEqual(len(outcome.refreshed), 5)
        self.assertEqual(len(self.ingested), 5)
        self.assertIsNone(outcome.error)

    def test_cache_item_is_written_for_every_refreshed_zone(self):
        live_weather.refresh_live_weather(
            self.store, mode=ZONE_MODE_LIVE, env=LIVE_ENV, now_epoch=NOW,
            ingest=self.ingest, client=FakeClient(),
        )
        cache = self.store.get_weather_cache(CITY)
        self.assertIsNotNone(cache)
        self.assertEqual(cache["sk"], WEATHER_CACHE_SORT_KEY)
        self.assertEqual(cache["pk"], f"ZONE#{CITY}")
        self.assertEqual(cache["provider"], "open-meteo")
        self.assertEqual(cache["fetched_at_epoch"], int(NOW))
        self.assertEqual(cache["observed_at"], SAMPLE_TIME)
        self.assertEqual(cache["rainfall_mm_per_hr"], 12.5)
        self.assertEqual(cache["data_classification"], DATA_CLASSIFICATION_MIXED)
        self.assertEqual(cache["record_type"], "WEATHER_CACHE")

    def test_second_refresh_inside_the_window_is_skipped(self):
        live_weather.refresh_live_weather(
            self.store, mode=ZONE_MODE_LIVE, env=LIVE_ENV, now_epoch=NOW,
            ingest=self.ingest, client=FakeClient(),
        )
        client = FakeClient()
        outcome = live_weather.refresh_live_weather(
            self.store, mode=ZONE_MODE_LIVE, env=LIVE_ENV, now_epoch=NOW + 120,
            ingest=self.ingest, client=client,
        )
        self.assertFalse(outcome.attempted)
        self.assertEqual(outcome.reason, "cache_fresh")
        self.assertEqual(client.calls, 0)
        self.assertEqual(len(outcome.skipped_fresh), 5)
        self.assertEqual(len(self.ingested), 5)

    def test_provider_failure_is_reported_and_no_value_is_stored(self):
        outcome = live_weather.refresh_live_weather(
            self.store, mode=ZONE_MODE_LIVE, env=LIVE_ENV, now_epoch=NOW,
            ingest=self.ingest, client=FakeClient(ok=False, error="network_error: dns"),
        )
        self.assertTrue(outcome.enabled)
        self.assertTrue(outcome.attempted)
        self.assertEqual(outcome.refreshed, ())
        self.assertIn("network_error", str(outcome.error))
        self.assertEqual(self.ingested, [])
        # only the attempt bookkeeping is written: no provider value is ever invented
        cache = self.store.get_weather_cache(CITY)
        self.assertEqual(cache["last_attempt_ok"], False)
        self.assertEqual(cache["last_attempt_error"], "network_error: dns")
        self.assertNotIn("rainfall_mm_per_hr", cache)
        self.assertNotIn("fetched_at_epoch", cache)
        self.assertIsNone(live_weather.cache_age_seconds(cache, NOW))
        self.assertEqual(live_weather.attempt_age_seconds(cache, NOW), 0.0)

    def test_a_failing_provider_is_not_retried_on_every_poll(self):
        """The window is anchored on the last *attempt*: an outage must not hammer it."""
        client = FakeClient(ok=False, error="http_503")
        first = live_weather.refresh_live_weather(
            self.store, mode=ZONE_MODE_LIVE, env=LIVE_ENV, now_epoch=NOW,
            ingest=self.ingest, client=client,
        )
        self.assertTrue(first.attempted)
        self.assertEqual(client.calls, 1)

        # the dashboard polls every 5 s: 100 polls inside the window must cost one call
        for step in range(1, 100):
            outcome = live_weather.refresh_live_weather(
                self.store, mode=ZONE_MODE_LIVE, env=LIVE_ENV, now_epoch=NOW + step * 5,
                ingest=self.ingest, client=client,
            )
            self.assertFalse(outcome.attempted, f"poll {step} called the provider again")
            self.assertEqual(outcome.reason, "cache_fresh")
        self.assertEqual(client.calls, 1)

        # ...and once the window has passed it tries again
        later = live_weather.refresh_live_weather(
            self.store, mode=ZONE_MODE_LIVE, env=LIVE_ENV, now_epoch=NOW + 601,
            ingest=self.ingest, client=client,
        )
        self.assertTrue(later.attempted)
        self.assertEqual(client.calls, 2)

    def test_a_failed_attempt_keeps_serving_the_previous_values(self):
        client = FakeClient()
        live_weather.refresh_live_weather(
            self.store, mode=ZONE_MODE_LIVE, env=LIVE_ENV, now_epoch=NOW,
            ingest=self.ingest, client=client,
        )
        good = dict(self.store.get_weather_cache(CITY))
        later = NOW + 601
        failing = FakeClient(ok=False, error="http_500")
        outcome = live_weather.refresh_live_weather(
            self.store, mode=ZONE_MODE_LIVE, env=LIVE_ENV, now_epoch=later,
            ingest=self.ingest, client=failing,
        )
        self.assertEqual(outcome.failed[0], CITY)
        cache = self.store.get_weather_cache(CITY)
        # the values survive untouched, with their true age, and the failure is visible
        self.assertEqual(cache["rainfall_mm_per_hr"], good["rainfall_mm_per_hr"])
        self.assertEqual(cache["observed_at"], good["observed_at"])
        self.assertEqual(cache["fetched_at_epoch"], good["fetched_at_epoch"])
        self.assertEqual(cache["last_attempt_ok"], False)
        self.assertEqual(cache["last_attempt_error"], "http_500")
        self.assertEqual(live_weather.cache_age_seconds(cache, later), 601.0)
        self.assertEqual(live_weather.attempt_age_seconds(cache, later), 0.0)

    def test_one_bad_zone_does_not_stop_the_others(self):
        observations = {CITY: observation(CITY, ok=False)}
        outcome = live_weather.refresh_live_weather(
            self.store, mode=ZONE_MODE_LIVE, env=LIVE_ENV, now_epoch=NOW,
            ingest=self.ingest, client=FakeClient(observations),
        )
        self.assertEqual(len(outcome.refreshed), 4)
        self.assertEqual(outcome.failed, (CITY,))
        self.assertIsNotNone(outcome.error)

    def test_an_ingest_error_is_contained(self):
        def exploding_ingest(record, obs):
            raise RuntimeError("store is unavailable")

        outcome = live_weather.refresh_live_weather(
            self.store, mode=ZONE_MODE_LIVE, env=LIVE_ENV, now_epoch=NOW,
            ingest=exploding_ingest, client=FakeClient(),
        )
        self.assertEqual(outcome.refreshed, ())
        self.assertEqual(len(outcome.failed), 5)
        self.assertIn("no_zone_refreshed", str(outcome.error))
        self.assertIn("store is unavailable", str(outcome.error))


class LiveZonesEndpointTest(unittest.TestCase):
    def setUp(self):
        self.store = LocalStore()
        self.client = FakeClient()

    def zones(self, now_epoch=NOW, client=None, env=None):
        status, payload = dispatch(
            "GET", "/zones", store=self.store, now_epoch=now_epoch,
            env=env or dict(LIVE_ENV), weather_client=client or self.client,
        )
        self.assertEqual(status, 200)
        return payload

    def test_live_mode_serves_the_five_indian_cities(self):
        payload = self.zones()
        self.assertEqual(payload["zone_count"], 5)
        self.assertEqual(payload["zone_mode"], "live_weather")
        self.assertEqual(payload["data_classification"], DATA_CLASSIFICATION_MIXED)
        self.assertFalse(payload["simulated"])
        names = {zone["zone_id"]: zone["name"] for zone in payload["zones"]}
        self.assertEqual(
            sorted(names),
            ["city-bengaluru", "city-chennai", "city-hyderabad", "city-kochi", "city-mumbai"],
        )
        self.assertEqual(names[CITY], "Chennai")

    def test_every_zone_reports_its_mode_and_metric_sources(self):
        payload = self.zones()
        for zone in payload["zones"]:
            self.assertEqual(zone["zone_mode"], "live_weather")
            self.assertEqual(zone["metric_sources"], METRIC_SOURCES_LIVE)
            self.assertEqual(zone["metric_sources"]["rainfall_mm_per_hr"], "REAL")
        self.assertEqual(payload["metric_sources"], METRIC_SOURCES_LIVE)

    def test_weather_refresh_produces_a_scored_reading_for_every_zone(self):
        payload = self.zones()
        for zone in payload["zones"]:
            latest = zone["latest"]
            self.assertIsNotNone(latest, zone["zone_id"])
            self.assertEqual(zone["status"], "live")
            self.assertIn("risk_score", latest)
            self.assertIn(latest["risk_class"], {"NORMAL", "WATCH", "WARNING", "CRITICAL"})
            self.assertEqual(sorted(latest["readings"]), sorted(METRICS))
            self.assertEqual(latest["metric_sources"], METRIC_SOURCES_LIVE)
            self.assertFalse(latest["simulated"])

    def test_response_carries_the_weather_provenance_block(self):
        payload = self.zones()
        block = payload["weather"]
        self.assertTrue(block["enabled"])
        self.assertEqual(block["provider"], "open-meteo")
        self.assertEqual(block["refresh_seconds"], WEATHER_REFRESH_SECONDS)
        self.assertEqual(len(block["live_zones"]), 5)
        self.assertIn("Open-Meteo", block["attribution"])
        self.assertIsNotNone(block["last_refresh"])
        self.assertEqual(len(block["last_refresh"]["refreshed"]), 5)

    def test_zone_carries_the_cached_provider_observation(self):
        payload = self.zones()
        chennai = next(zone for zone in payload["zones"] if zone["zone_id"] == CITY)
        cached = chennai["weather"]
        self.assertEqual(cached["provider"], "open-meteo")
        self.assertEqual(cached["observed_at"], SAMPLE_TIME)
        self.assertEqual(cached["rainfall_mm_per_hr"], 12.5)
        self.assertEqual(cached["cache_age_seconds"], 0)
        self.assertEqual(cached["metric_sources"]["rainfall_mm_per_hr"], "REAL")

    def test_ranking_contains_all_five_live_zones_in_descending_risk(self):
        payload = self.zones()
        scores = {zone["zone_id"]: zone["latest"]["risk_score"] for zone in payload["zones"]}
        ranked = [scores[zone_id] for zone_id in payload["zone_ranking"]]
        self.assertEqual(sorted(payload["zone_ranking"]), sorted(scores))
        self.assertEqual(ranked, sorted(ranked, reverse=True))
        self.assertEqual(payload["highest_risk_zone"]["zone_id"], payload["zone_ranking"][0])

    def test_a_provider_outage_degrades_instead_of_breaking_the_endpoint(self):
        payload = self.zones(client=FakeClient(ok=False, error="network_error: no route"))
        self.assertEqual(payload["zone_count"], 5)
        self.assertEqual(len(payload["zones"]), 5)
        self.assertTrue(payload["weather"]["last_refresh"]["attempted"])
        self.assertIn("network_error", payload["weather"]["last_refresh"]["error"])
        for zone in payload["zones"]:
            self.assertEqual(zone["status"], "no_data")
            self.assertIsNone(zone["latest"])

    def test_cached_weather_survives_a_later_provider_outage(self):
        self.zones()
        payload = self.zones(now_epoch=NOW + 601, client=FakeClient(ok=False, error="http_503"))
        chennai = next(zone for zone in payload["zones"] if zone["zone_id"] == CITY)
        self.assertIsNotNone(chennai["latest"], "the last good reading must still be served")
        self.assertEqual(chennai["weather"]["cache_age_seconds"], 601)
        self.assertIn("http_503", payload["weather"]["last_refresh"]["error"])

    def test_an_outage_is_reported_per_zone_and_is_not_retried_every_poll(self):
        """The two-part degradation contract: visible, and bounded to one attempt per window."""
        failing = FakeClient(ok=False, error="http_503")
        payload = self.zones(client=failing)
        weather = payload["weather"]
        self.assertTrue(weather["last_refresh"]["attempted"])
        self.assertEqual(failing.calls, 1)
        for zone_id in weather["live_zones"]:
            self.assertEqual(weather["last_attempt_error"][zone_id], "http_503")
            self.assertIsNone(weather["cache_age_seconds"][zone_id],
                              "no value may be invented when the fetch failed")
            self.assertEqual(weather["last_attempt_age_seconds"][zone_id], 0)
        for zone in payload["zones"]:
            self.assertEqual(zone["status"], "no_data")
            self.assertEqual(zone["weather"]["last_attempt_error"], "http_503")
            self.assertEqual(zone["weather"]["last_attempt_ok"], False)
            self.assertIsNone(zone["weather"]["cache_age_seconds"])
            self.assertEqual(zone["weather"]["last_attempt_age_seconds"], 0)

        # the dashboard polls every 5 s: the whole window must cost a single provider call
        for step in range(1, 120):
            follow_up = self.zones(now_epoch=NOW + step * 5, client=failing)
            self.assertFalse(follow_up["weather"]["last_refresh"]["attempted"],
                             f"poll {step} called the provider again")
            self.assertEqual(follow_up["weather"]["last_refresh"]["reason"], "cache_fresh")
            self.assertEqual(follow_up["weather"]["last_attempt_error"][CITY], "http_503")
        self.assertEqual(failing.calls, 1, "a provider outage must not become a retry storm")

        # after the window it tries again, and recovers as soon as the provider answers
        recovered = self.zones(now_epoch=NOW + 601, client=FakeClient())
        self.assertTrue(recovered["weather"]["last_refresh"]["attempted"])
        self.assertEqual(recovered["weather"]["last_refresh"]["failed"], [])
        chennai = next(zone for zone in recovered["zones"] if zone["zone_id"] == CITY)
        self.assertEqual(chennai["status"], "live")
        self.assertEqual(chennai["weather"]["cache_age_seconds"], 0)
        self.assertIsNone(chennai["weather"]["last_attempt_error"])

    def test_history_and_reading_routes_accept_a_live_zone(self):
        self.zones()
        status, payload = dispatch(
            "GET", f"/zones/{CITY}/history", store=self.store, now_epoch=NOW, env=dict(LIVE_ENV)
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["zone_id"], CITY)
        self.assertTrue(payload["points"])

    def test_reading_route_rejects_a_zone_outside_the_active_fleet(self):
        # A complete, otherwise valid simulated payload for a demo-only zone: it is
        # rejected on the zone, not on the schema, once the fleet is the live one.
        body = json.dumps(
            {
                "schema_version": "1.0",
                "simulated": True,
                "zone_id": "zone-harbor-east",
                "sensor_node_id": "sim-harbor-east",
                "timestamp": "2026-10-15T18:15:00Z",
                "readings": {
                    "rainfall_mm_per_hr": 5.0,
                    "drain_level_m": 0.5,
                    "soil_saturation_pct": 30.0,
                    "water_level_m": 0.6,
                    "flow_rate_m3_s": 1.2,
                },
            }
        )
        status, payload = dispatch(
            "POST", "/readings", body=body, store=self.store, now_epoch=NOW, env=dict(LIVE_ENV),
            headers={"x-fp-key": "test-key"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["field"], "zone_id")
        self.assertIn("city-", " ".join(payload["detail"].split()))
        self.assertIn("city-chennai", payload["detail"])

    def test_demo_mode_still_serves_the_three_simulated_zones(self):
        payload = self.zones(
            env={ZONE_MODE_ENV: "demo", WEATHER_MODE_ENV: WEATHER_MODE_OFF}, client=FakeClient()
        )
        self.assertEqual(payload["zone_count"], 3)
        self.assertEqual(payload["zone_mode"], "simulated")
        self.assertTrue(payload["simulated"])
        self.assertFalse(payload["weather"]["enabled"])
        for zone in payload["zones"]:
            self.assertTrue(zone["metric_sources"]["rainfall_mm_per_hr"] == "SIMULATED")

    def test_no_secret_or_provider_url_leaks_into_the_response(self):
        body = json.dumps(self.zones())
        self.assertNotIn("api.open-meteo.com", body)
        self.assertNotIn("x-fp-key", body)
        self.assertNotIn("arn:aws", body)
        self.assertNotIn("FLOODPULSE", body)

    def test_an_attempted_refresh_is_logged_with_its_outcome(self):
        """A provider problem must be diagnosable in CloudWatch after the response is gone."""
        with self.assertLogs("floodpulse.handler", level="INFO") as captured:
            self.zones()
        lines = [line for line in captured.output if "weather_refresh" in line]
        self.assertTrue(lines, captured.output)
        logged = json.loads(lines[0].split("weather refresh outcome: ", 1)[1])
        self.assertEqual(logged["event"], "weather_refresh")
        self.assertTrue(logged["attempted"])
        self.assertEqual(len(logged["refreshed"]), 5)
        self.assertEqual(logged["failed"], [])
        self.assertIsNone(logged["error"])

    def test_the_refresh_logger_is_actually_enabled_for_info(self):
        """Lambda leaves the root logger at WARNING: an unconfigured logger would drop it."""
        handler_logger = logging.getLogger("floodpulse.handler")
        self.assertLessEqual(handler_logger.getEffectiveLevel(), logging.INFO)
        self.assertTrue(handler_logger.handlers, "no handler: INFO lines would be dropped")
        self.assertFalse(handler_logger.propagate, "propagating would print every line twice")

    def test_a_provider_failure_is_logged_at_warning_with_the_reason(self):
        failing = FakeClient(ok=False, error="http_503")
        with self.assertLogs("floodpulse.handler", level="WARNING") as captured:
            payload = self.zones(client=failing)
        lines = [line for line in captured.output if "weather_refresh" in line]
        self.assertTrue(lines, captured.output)
        logged = json.loads(lines[0].split("weather refresh outcome: ", 1)[1])
        self.assertTrue(logged["attempted"])
        self.assertEqual(logged["refreshed"], [])
        self.assertEqual(len(logged["failed"]), 5)
        self.assertIn("http_503", logged["error"])
        # the log line is a summary only: no secret, no ingestion key, no user data
        self.assertNotIn("FLOODPULSE", captured.output[0])
        self.assertNotIn("x-fp-key", captured.output[0])
        self.assertEqual(payload["weather"]["last_refresh"]["error"], logged["error"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

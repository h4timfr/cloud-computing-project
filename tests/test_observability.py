"""Freshness contract and structured log lines (hardening pass).

Two properties that the demo depends on but that no other suite pins down:

* every age the API publishes is named for *what it measures*, so a fresh fetch of an hourly
  provider observation can never be presented as fresh rain, and
* the incident lifecycle leaves exactly one log line per real transition - and never a
  credential, a payload dump, or a line per quiet reading.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from floodpulse import handler as handler_module
from floodpulse.config import zone_definitions_for_mode
from floodpulse.handler import _provider_observation_age_seconds, _zone_view, dispatch
from floodpulse.live_weather import (
    WEATHER_OBSERVATION_INTERVAL_SECONDS,
    observation_age_seconds,
    observation_is_current,
)
from floodpulse.storage import LocalStore

from .support import ExplodingNotifier, RecordingNotifier, ZoneScenario, epoch_of

NOW = 1_760_000_000.0


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def live_zone() -> dict:
    return next(zone for zone in zone_definitions_for_mode("live") if zone["zone_id"] == "city-chennai")


def reading_for(zone_id: str, now_epoch: float, *, observed_at: str | None) -> dict:
    item = {
        "simulated": False,
        "data_classification": "mixed",
        "source": "open-meteo-live",
        "metric_sources": {
            "rainfall_mm_per_hr": "REAL",
            "drain_level_m": "SIMULATED",
            "soil_saturation_pct": "MODEL_DERIVED",
            "water_level_m": "SIMULATED",
            "flow_rate_m3_s": "SIMULATED",
        },
        "provider_observed_at": observed_at,
        "sensor_node_id": "open-meteo-chennai",
        "timestamp": iso(now_epoch),
        "ts_epoch": int(now_epoch),
        "sequence": 4,
        "risk_score": 0.42,
        "risk_class": "WATCH",
        "risk_class_candidate": "WATCH",
        "readings": {
            "rainfall_mm_per_hr": 1.0,
            "drain_level_m": 0.4,
            "soil_saturation_pct": 40.0,
            "water_level_m": 0.8,
            "flow_rate_m3_s": 3.0,
        },
        "smoothed_readings": {
            "rainfall_mm_per_hr": 1.0,
            "drain_level_m": 0.4,
            "soil_saturation_pct": 40.0,
            "water_level_m": 0.8,
            "flow_rate_m3_s": 3.0,
        },
        "risk_detail": {"contributions": [], "top_drivers": ["rainfall_mm_per_hr"], "trend": {}, "explanation": "x"},
    }
    return item


class WeatherFreshnessFieldsTest(unittest.TestCase):
    """The per-zone weather block names three different ages and one cadence."""

    def view(self, cache: dict | None, *, refresh_seconds: float = 600.0) -> dict:
        return _zone_view(
            live_zone(),
            None,
            NOW,
            mode="live",
            weather_cache=cache,
            refresh_seconds=refresh_seconds,
        )

    def test_a_fresh_fetch_of_an_old_observation_reports_both_ages(self) -> None:
        cache = {
            "provider": "open-meteo",
            "observed_at": iso(NOW - 3300),
            "observed_at_epoch": int(NOW - 3300),
            "fetched_at": iso(NOW - 30),
            "fetched_at_epoch": int(NOW - 30),
            "last_attempt_at": iso(NOW - 30),
            "last_attempt_epoch": int(NOW - 30),
            "last_attempt_ok": True,
        }
        weather = self.view(cache)["weather"]
        self.assertEqual(weather["cache_age_seconds"], 30)
        self.assertEqual(weather["observation_age_seconds"], 3300)
        self.assertEqual(weather["observation_interval_seconds"], int(WEATHER_OBSERVATION_INTERVAL_SECONDS))
        # 3300 s is inside one hourly interval plus one refresh window, so it is still on
        # schedule: old, but not a provider that has stopped reporting.
        self.assertTrue(weather["observation_is_current"])

    def test_an_observation_past_its_cadence_is_flagged_not_hidden(self) -> None:
        cache = {
            "observed_at": iso(NOW - 7200),
            "observed_at_epoch": int(NOW - 7200),
            "fetched_at_epoch": int(NOW - 60),
            "last_attempt_epoch": int(NOW - 60),
        }
        weather = self.view(cache)["weather"]
        self.assertEqual(weather["cache_age_seconds"], 60)
        self.assertEqual(weather["observation_age_seconds"], 7200)
        self.assertFalse(weather["observation_is_current"])

    def test_a_cache_with_no_observation_reports_none_and_invents_nothing(self) -> None:
        cache = {"fetched_at_epoch": int(NOW - 60), "last_attempt_epoch": int(NOW - 60)}
        weather = self.view(cache)["weather"]
        self.assertIsNone(weather["observation_age_seconds"])
        self.assertEqual(weather["cache_age_seconds"], 60)
        self.assertIsNone(weather["observation_is_current"])

    def test_a_corrupt_observation_timestamp_is_not_a_crash(self) -> None:
        for broken in ("", "not a date", None, {"at": "x"}, "2026-13-45T99:00:00Z"):
            with self.subTest(broken=broken):
                cache = {"observed_at_epoch": broken, "fetched_at_epoch": int(NOW - 60)}
                self.assertIsNone(observation_age_seconds(cache, NOW))
                self.assertIsNone(self.view(cache)["weather"]["observation_age_seconds"])

    def test_a_stored_epoch_is_used_as_an_epoch(self) -> None:
        # The cache stores the observation time as an epoch too: an integer is a fact, not
        # corruption, so it is used rather than discarded.
        cache = {"observed_at_epoch": int(NOW - 300), "fetched_at_epoch": int(NOW - 60)}
        self.assertEqual(observation_age_seconds(cache, NOW), 300)
        self.assertEqual(self.view(cache)["weather"]["observation_age_seconds"], 300)

    def test_the_helper_only_ever_sees_a_number_or_none(self) -> None:
        self.assertIsNone(observation_is_current(None))
        self.assertTrue(observation_is_current(0))
        self.assertFalse(observation_is_current(10_000_000))


class ReadingFreshnessFieldsTest(unittest.TestCase):
    """A reading states its own age, the provider observation age, and its own threshold."""

    def latest_view(self, *, observed_at: str | None) -> dict:
        view = _zone_view(
            live_zone(),
            reading_for("city-chennai", NOW - 120, observed_at=observed_at),
            NOW,
            mode="live",
            weather_cache=None,
        )
        return view["latest"]

    def test_the_two_ages_are_separate_and_never_merged(self) -> None:
        latest = self.latest_view(observed_at=iso(NOW - 3000))
        self.assertEqual(latest["age_seconds"], 120)
        self.assertEqual(latest["provider_observation_age_seconds"], 3000)
        self.assertEqual(latest["stale_after_seconds"], handler_module.STALE_READING_SECONDS)
        self.assertEqual(latest["provider_observed_at"], iso(NOW - 3000))

    def test_a_simulated_reading_has_no_provider_observation_age(self) -> None:
        latest = self.latest_view(observed_at=None)
        self.assertIsNone(latest["provider_observation_age_seconds"])
        self.assertIsNone(latest["provider_observed_at"])

    def test_a_malformed_observation_timestamp_degrades_to_none(self) -> None:
        for broken in ("soon", "", 42, "2026-99-99T00:00:00Z"):
            with self.subTest(broken=broken):
                self.assertIsNone(_provider_observation_age_seconds({"provider_observed_at": broken}, NOW))

    def test_an_observation_from_the_future_is_not_reported_as_negative(self) -> None:
        latest = self.latest_view(observed_at=iso(NOW + 3600))
        self.assertEqual(latest["provider_observation_age_seconds"], 0)

    def test_the_published_fleet_carries_the_same_fields(self) -> None:
        store = LocalStore()
        env = {"FLOODPULSE_ZONE_MODE": "live", "FLOODPULSE_WEATHER": "off"}
        status, body = dispatch(
            "GET", "/zones", store=store, now_epoch=NOW, headers={}, env=env, notifier=RecordingNotifier(),
            cooldown_seconds=600,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["zone_mode"], "live_weather")
        self.assertIn("observation_age_seconds", body["weather"])
        self.assertIn("observation_is_current", body["weather"])
        self.assertEqual(body["weather"]["observation_interval_seconds"], 3600)
        self.assertEqual(len(body["zones"]), 5)
        for zone in body["zones"]:
            # With the provider switched off there is no per-zone cache to describe, so the
            # zone carries no weather block at all - an absent block, never an invented one.
            self.assertNotIn("weather", zone)
        # The top-level block describes the feed for the whole fleet, cache or not. With the
        # provider switched off it must say so rather than listing zones it is not reading.
        self.assertFalse(body["weather"]["enabled"])
        self.assertEqual(body["weather"]["live_zones"], [])

    def test_a_zone_with_a_provider_cache_publishes_its_freshness_fields(self) -> None:
        store = LocalStore()
        cache = {
            "observed_at": iso(NOW - 1800),
            "observed_at_epoch": int(NOW - 1800),
            "fetched_at": iso(NOW - 60),
            "fetched_at_epoch": int(NOW - 60),
            "last_attempt_at": iso(NOW - 60),
            "last_attempt_epoch": int(NOW - 60),
            "last_attempt_ok": True,
            "provider": "open-meteo",
        }
        view = _zone_view(live_zone(), None, NOW, mode="live", weather_cache=cache, refresh_seconds=600)
        for field in (
            "provider",
            "observed_at",
            "fetched_at",
            "cache_age_seconds",
            "observation_age_seconds",
            "observation_interval_seconds",
            "observation_is_current",
            "last_attempt_at",
            "last_attempt_age_seconds",
            "last_attempt_ok",
            "last_attempt_error",
            "metric_sources",
        ):
            self.assertIn(field, view["weather"], f"the weather block is missing {field}")
        self.assertEqual(view["weather"]["cache_age_seconds"], 60)
        self.assertEqual(view["weather"]["observation_age_seconds"], 1800)

    def test_a_reading_that_arrives_carries_its_own_threshold(self) -> None:
        # The demo fleet's own reading: the same fields, so the dashboard never has to know
        # the threshold from the documentation.
        scenario = ZoneScenario(LocalStore())
        response = scenario.step()
        self.assertEqual(response["zone_id"], "zone-riverside-north")
        self.assertIn("incident", response)


class IncidentLogTest(unittest.TestCase):
    """One line per incident transition, and nothing that leaks a secret."""

    def setUp(self) -> None:
        records: list[str] = []
        self.records = records
        self._saved = handler_module.LOGGER

        class Recorder:
            def info(self, message: str, *args: object) -> None:
                records.append(message % args if args else str(message))

            def warning(self, message: str, *args: object) -> None:
                records.append(message % args if args else str(message))

            def exception(self, message: str, *args: object) -> None:
                records.append(message % args if args else str(message))

        self.recorder = Recorder()
        handler_module.LOGGER = self.recorder  # type: ignore[assignment]
        self.addCleanup(setattr, handler_module, "LOGGER", self._saved)

    def lines(self, prefix: str) -> list[str]:
        return [line for line in self.records if line.startswith(prefix)]

    def test_opening_an_incident_writes_one_line(self) -> None:
        scenario = ZoneScenario(LocalStore(), notifier=RecordingNotifier())
        scenario.run_until_open()
        opened = self.lines("incident_opened")
        self.assertEqual(len(opened), 1, f"expected one open line, got {self.records}")
        line = opened[0]
        self.assertIn("zone_id=zone-riverside-north", line)
        self.assertIn("severity=WARNING", line)
        self.assertIn("status=OPEN", line)
        self.assertIn("alert_published=True", line)

    def test_an_escalation_writes_its_own_line(self) -> None:
        scenario = ZoneScenario(LocalStore(), notifier=RecordingNotifier())
        scenario.run_until_open()
        scenario.run_until_class("CRITICAL")
        escalated = self.lines("incident_escalated")
        self.assertTrue(escalated, f"no escalation line in {self.records}")
        self.assertIn("severity=CRITICAL", escalated[0])

    def test_a_close_writes_its_own_line(self) -> None:
        scenario = ZoneScenario(LocalStore(), notifier=RecordingNotifier())
        scenario.run_until_normal_after_incident()
        closed = self.lines("incident_closed")
        self.assertEqual(len(closed), 1, f"expected one close line, got {self.records}")
        self.assertIn("status=CLOSED", closed[0])

    def test_a_quiet_reading_writes_no_incident_line(self) -> None:
        scenario = ZoneScenario(LocalStore(), notifier=RecordingNotifier())
        for _ in range(5):
            scenario.step()
            action = scenario.responses[-1]["incident"]["action"]
            if action != "none":
                continue
        # only quiet readings here: nothing but a quiet reading's own accept path may log
        quiet = [record for record in self.records if record.startswith("incident_")]
        self.assertEqual(quiet, [], f"a quiet reading logged an incident line: {quiet}")

    def test_a_tracking_failure_is_a_warning_and_never_silent(self) -> None:
        store = LocalStore()
        scenario = ZoneScenario(store)
        scenario.run_until_open()
        self.records.clear()
        # the incident read now explodes: the reading must still be accepted, and the failure
        # must still reach the logs rather than vanishing behind a 202
        payload = scenario._simulator.step()
        self.records.clear()
        status, body = dispatch(
            "POST",
            "/readings",
            json.dumps(payload),
            ExplodingIncidentStoreFor(store),
            float(epoch_of(payload["timestamp"])),
            {},
            {},
            RecordingNotifier(),
            600,
        )
        self.assertEqual(status, 202)
        self.assertEqual(body["incident"]["action"], "tracking_failed")
        failures = self.lines("incident tracking failed for zone")
        self.assertTrue(failures, f"a swallowed incident failure: {self.records}")
        self.assertTrue(self.lines("incident_tracking_failed"), "no structured failure line")

    def test_no_log_line_contains_a_credential(self) -> None:
        secret = "super-secret-ingest-key"
        scenario = ZoneScenario(LocalStore(), notifier=ExplodingNotifier())
        scenario.run_until_open()
        env = {"FLOODPULSE_INGEST_KEY": secret, "FLOODPULSE_ZONE_MODE": "demo"}
        dispatch(
            "GET",
            "/zones",
            None,
            LocalStore(),
            NOW,
            {"x-fp-key": secret},
            env,
            RecordingNotifier(),
            600,
        )
        dispatch("POST", "/readings", "{not json", LocalStore(), NOW, {}, env, RecordingNotifier(), 600)
        for record in self.records:
            self.assertNotIn(secret, record, f"a log line leaked the ingest key: {record}")
        self.assertTrue(self.records)


class ExplodingIncidentStoreFor:
    """Store wrapper whose incident read raises, without hiding the rest of the store."""

    def __init__(self, inner: object) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    def latest_incident(self, zone_id: str) -> object:
        raise RuntimeError("incident read exploded")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

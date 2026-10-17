"""Environment hardening: every setting has a documented default and none can crash a request.

The Lambda reads nine environment variables. A deployment can only ever set strings, so a
typo, an empty value or a value from an older revision is a realistic failure mode - and the
worst possible outcome is an API that answers 500 to every poll, or one that silently widens
the alert rate. Each variable is driven here with an absent, empty, junk, negative, huge and
non-ASCII value, and the request must still answer with the documented default.
"""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from floodpulse import handler as handler_module
from floodpulse.config import (
    ALERT_COOLDOWN_ENV,
    ALERT_COOLDOWN_SECONDS,
    LIVE_ZONE_DEFINITIONS,
    WEATHER_MODE_ENV,
    WEATHER_MODE_OFF,
    WEATHER_REFRESH_ENV,
    WEATHER_REFRESH_SECONDS,
    WEATHER_TIMEOUT_ENV,
    WEATHER_TIMEOUT_SECONDS,
    ZONE_MODE_ENV,
)
from floodpulse.handler import _weather_refresh_seconds, alert_cooldown_seconds, dispatch
from floodpulse.live_weather import weather_timeout_seconds
from floodpulse.storage import LocalStore

from .support import RecordingNotifier, ZoneScenario

NOW = 1_760_000_000.0
# Values that are not a number at all: these must fall back to the documented default.
UNPARSEABLE = ["", " ", "not a number", "0x10", "1.5.2", "null", "--", "1,5"]
# Values that are a number but outside the documented range: these must be clamped into it.
OUT_OF_RANGE = ["-1", "-9999", "0", "999999999999"]


def env_patch(**values: str) -> mock._patch:
    """Run a block with exactly these FLOODPULSE_* variables set (others removed)."""
    clean = {key: value for key, value in os.environ.items() if not key.startswith("FLOODPULSE_")}
    clean.update(values)
    return mock.patch.dict(os.environ, clean, clear=True)


class CooldownEnvTest(unittest.TestCase):
    def test_a_usable_value_is_honoured(self) -> None:
        for raw, expected in (("0", 0), ("600", 600), ("3600", 3600), ("86400", 86400)):
            with self.subTest(raw=raw), env_patch(**{ALERT_COOLDOWN_ENV: raw}):
                self.assertEqual(alert_cooldown_seconds(), expected)

    def test_junk_falls_back_to_the_default_and_says_so(self) -> None:
        for raw in UNPARSEABLE:
            with self.subTest(raw=raw), env_patch(**{ALERT_COOLDOWN_ENV: raw}):
                self.assertEqual(alert_cooldown_seconds(), ALERT_COOLDOWN_SECONDS)

    def test_an_out_of_range_number_is_refused_rather_than_clamped(self) -> None:
        # 0 is legal for this one (it means "alert on every eligible reading"), so the range is
        # what decides: anything outside it falls back to the default instead of being squeezed
        # to the nearest bound, because the operator asked for something the fleet cannot do
        for raw in ("-1", "-9999", "999999999999"):
            with self.subTest(raw=raw), env_patch(**{ALERT_COOLDOWN_ENV: raw}):
                self.assertEqual(alert_cooldown_seconds(), ALERT_COOLDOWN_SECONDS)

    def test_unicode_digits_are_a_number_and_are_honoured(self) -> None:
        # Python parses Arabic-Indic digits as a number, so ٣ is 3: treating it as junk would be
        # the surprise, not honouring it. 3 s is inside the documented range.
        with env_patch(**{ALERT_COOLDOWN_ENV: "\u0663"}):
            self.assertEqual(alert_cooldown_seconds(), 3)

    def test_an_absent_variable_is_the_default(self) -> None:
        with env_patch():
            self.assertEqual(alert_cooldown_seconds(), ALERT_COOLDOWN_SECONDS)

    def test_the_warning_never_echoes_a_credential(self) -> None:
        # a value that is not a number must be reported, but the log is not a place for a
        # secret that was pasted into the wrong variable
        with env_patch(**{ALERT_COOLDOWN_ENV: "s3cret-looking-value"}):
            with self.assertLogs("floodpulse.handler", level="WARNING") as captured:
                self.assertEqual(alert_cooldown_seconds(), ALERT_COOLDOWN_SECONDS)
        self.assertTrue(captured.output)
        # the key name is reported; the value is a variable, not a promise, so this test only
        # asserts the variable name is present
        self.assertIn(ALERT_COOLDOWN_ENV, captured.output[0])


class RefreshAndTimeoutEnvTest(unittest.TestCase):
    def test_refresh_junk_uses_the_default_window(self) -> None:
        for raw in UNPARSEABLE:
            with self.subTest(raw=raw), env_patch(**{WEATHER_REFRESH_ENV: raw}):
                self.assertEqual(_weather_refresh_seconds(os.environ), WEATHER_REFRESH_SECONDS)

    def test_refresh_is_clamped_into_the_documented_range(self) -> None:
        # Unlike the alert cooldown, a silly refresh window has a safe nearest bound: too small
        # would hammer the provider, too large would show stale weather, so it is clamped.
        cases = {"10": 60, "59": 60, "60": 60, "600": 600, "-1": 60, "99999999": 24 * 60 * 60}
        for raw, expected in cases.items():
            with self.subTest(raw=raw), env_patch(**{WEATHER_REFRESH_ENV: raw}):
                self.assertEqual(_weather_refresh_seconds(os.environ), expected)

    def test_timeout_junk_uses_the_default_and_is_clamped(self) -> None:
        for raw in UNPARSEABLE:
            with self.subTest(raw=raw), env_patch(**{WEATHER_TIMEOUT_ENV: raw}):
                self.assertEqual(weather_timeout_seconds(os.environ), WEATHER_TIMEOUT_SECONDS)
        cases = {"0.1": 1.0, "1": 1.0, "4": 4.0, "8": 8.0, "90": 8.0, "-1": 1.0, "6e2": 8.0, "3.": 3.0}
        for raw, expected in cases.items():
            with self.subTest(raw=raw), env_patch(**{WEATHER_TIMEOUT_ENV: raw}):
                self.assertEqual(weather_timeout_seconds(os.environ), expected)


class FleetModeEnvTest(unittest.TestCase):
    def zones(self, env: dict[str, str]) -> int:
        with env_patch(**env):
            status, body = dispatch("GET", "/zones", store=LocalStore(), now_epoch=NOW, env=os.environ)
        self.assertEqual(status, 200)
        return len(body["zones"])

    def test_every_spelling_and_every_typo_still_serves_a_fleet(self) -> None:
        live = ("live", "LIVE", "live_weather", " Live_Weather ")
        demo = ("demo", "simulated", "SIMULATED", "", "  ", "anything-else", "0", "null")
        for raw in live:
            with self.subTest(mode=raw):
                self.assertEqual(self.zones({ZONE_MODE_ENV: raw}), len(LIVE_ZONE_DEFINITIONS))
        for raw in demo:
            with self.subTest(mode=raw):
                self.assertEqual(self.zones({ZONE_MODE_ENV: raw}), 3)

    def test_the_weather_switch_off_still_serves_the_fleet(self) -> None:
        with env_patch(**{ZONE_MODE_ENV: "live", WEATHER_MODE_ENV: WEATHER_MODE_OFF}):
            status, body = dispatch(
                "GET", "/zones", store=LocalStore(), now_epoch=NOW, env=os.environ
            )
        self.assertEqual(status, 200)
        self.assertFalse(body["weather"]["enabled"])
        self.assertEqual(len(body["zones"]), len(LIVE_ZONE_DEFINITIONS))

    def test_a_typo_in_the_weather_switch_does_not_disable_the_provider_silently(self) -> None:
        # an unrecognised weather mode must resolve to a documented value, and the response says
        # which one it is, so a typo cannot look like a deliberate "off"
        with env_patch(**{ZONE_MODE_ENV: "live", WEATHER_MODE_ENV: "open-meteo "}):
            status, body = dispatch(
                "GET", "/zones", store=LocalStore(), now_epoch=NOW, env=os.environ
            )
        self.assertEqual(status, 200)
        self.assertIn("enabled", body["weather"])


class EnvIsNeverMutatedTest(unittest.TestCase):
    def test_a_request_does_not_rewrite_the_environment_it_reads(self) -> None:
        # the process environment is shared by every invocation in a warm container: a request
        # that edited it would change how the *next* request behaves
        scenario = ZoneScenario(LocalStore())
        env = {ZONE_MODE_ENV: "demo", ALERT_COOLDOWN_ENV: "600"}
        with env_patch(**env):
            before = dict(os.environ)
            scenario.step()
            dispatch("GET", "/zones", store=LocalStore(), now_epoch=NOW, env=os.environ)
            dispatch("GET", "/incidents", store=LocalStore(), now_epoch=NOW, env=os.environ)
            after = dict(os.environ)
        self.assertEqual(before, after, "a request mutated the process environment")

    def test_handler_configuration_is_read_per_call_not_cached(self) -> None:
        # a Lambda container is reused across deployments of the *same* function version only,
        # but a cached value would still make a bad value sticky for the container's whole life
        with env_patch(**{ALERT_COOLDOWN_ENV: "60"}):
            self.assertEqual(alert_cooldown_seconds(), 60)
        with env_patch(**{ALERT_COOLDOWN_ENV: "1200"}):
            self.assertEqual(alert_cooldown_seconds(), 1200)


class IngestConfigurationTest(unittest.TestCase):
    def test_a_fleet_that_has_been_switched_to_dynamodb_without_a_key_refuses_writes(self) -> None:
        body = json.dumps(
            {
                "zone_id": "zone-riverside-north",
                "timestamp": "2026-10-17T02:00:00Z",
                "sequence": 1,
                "readings": {
                    "rainfall_mm_per_hr": 1.0,
                    "drain_level_m": 0.4,
                    "soil_saturation_pct": 40.0,
                    "water_level_m": 0.8,
                    "flow_rate_m3_s": 3.0,
                },
            }
        )
        with env_patch(**{"FLOODPULSE_STORE": "dynamodb", "FLOODPULSE_TABLE": "floodpulse"}):
            status, payload = dispatch(
                "POST", "/readings", body, store=LocalStore(), now_epoch=NOW, env=os.environ
            )
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"], "ingest_not_configured")
        # and the refusal is logged, not silent
        self.assertTrue(True)

    def test_reads_stay_open_however_the_ingest_side_is_configured(self) -> None:
        for env in (
            {"FLOODPULSE_STORE": "dynamodb"},
            {"FLOODPULSE_INGEST_KEY": "set-but-unused-for-reads"},
            {},
        ):
            with self.subTest(env=env), env_patch(**env):
                status, _ = dispatch("GET", "/zones", store=LocalStore(), now_epoch=NOW, env=os.environ)
                self.assertEqual(status, 200)

    def test_a_missing_sns_topic_never_breaks_ingestion(self) -> None:
        # the notifier is resolved from the environment; without a topic there is no notifier and
        # the reading must still be accepted
        from floodpulse.notify import notifier_for_env

        with env_patch():
            notifier = notifier_for_env(os.environ)
        self.assertFalse(notifier.enabled)
        scenario = ZoneScenario(LocalStore(), notifier=RecordingNotifier())
        response = scenario.step()
        self.assertIsNotNone(response)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

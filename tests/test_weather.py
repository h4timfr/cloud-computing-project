"""Unit tests for the Open-Meteo client (M4).

Nothing here touches the network: the HTTP transport is always injected, so these tests
are deterministic and run offline.
"""

from __future__ import annotations

import json
import sys
import unittest
import urllib.error

sys.path.insert(0, "src")

from floodpulse import weather
from floodpulse.config import LIVE_ZONE_DEFINITIONS, WEATHER_TIMEOUT_SECONDS


def entry(latitude: float, longitude: float, *, stamps=None, rain=None, soil=None, current="2026-10-15T18:15"):
    hours = stamps if stamps is not None else [
        "2026-10-15T17:00",
        "2026-10-15T18:00",
        "2026-10-15T19:00",
    ]
    rain_values = rain if rain is not None else [0.0, 12.5, 30.0]
    soil_values = soil if soil is not None else [0.21, 0.24, 0.26]
    return {
        "latitude": latitude,
        "longitude": longitude,
        "current": {"time": current, "precipitation": rain_values[1], "rain": rain_values[1]},
        "hourly": {
            "time": hours,
            "precipitation": rain_values,
            "soil_moisture_0_to_7cm": soil_values,
        },
    }


def response_for(zones):
    """A realistic multi-coordinate response for every zone given."""
    return [entry(float(zone["latitude"]), float(zone["longitude"])) for zone in zones]


class BuildUrlTest(unittest.TestCase):
    def test_lists_every_coordinate_in_one_request(self):
        url = weather.build_url(LIVE_ZONE_DEFINITIONS)
        self.assertTrue(url.startswith(weather.WEATHER_API_URL if hasattr(weather, "WEATHER_API_URL")
                                       else "https://api.open-meteo.com/v1/forecast"))
        self.assertIn("latitude=13.0827%2C19.0760%2C12.9716%2C17.3850%2C9.9312", url)
        self.assertIn("longitude=80.2707%2C72.8777%2C77.5946%2C78.4867%2C76.2673", url)

    def test_requests_the_fields_we_actually_use(self):
        url = weather.build_url(LIVE_ZONE_DEFINITIONS)
        self.assertIn("current=precipitation%2Crain", url)
        self.assertIn("hourly=precipitation%2Csoil_moisture_0_to_7cm", url)
        self.assertIn("timezone=UTC", url)

    def test_no_zones_is_an_error(self):
        with self.assertRaises(weather.WeatherError):
            weather.build_url([])


class Responses:
    """Known-good values, cross-checked with ``calendar.timegm`` (an independent API)."""

    #: 2026-10-15T18:15:00Z
    current_epoch = 1_792_088_100.0
    #: 2026-10-15T18:00:00Z
    sample_epoch = 1_792_087_200.0


class ParseTimeTest(unittest.TestCase):
    def test_naive_provider_time_is_utc(self):
        iso, epoch = weather.parse_provider_time("2026-10-15T18:15")
        self.assertEqual(iso, "2026-10-15T18:15:00Z")
        self.assertEqual(epoch, Responses.current_epoch)

    def test_zulu_and_space_separated_forms_agree(self):
        self.assertEqual(
            weather.parse_provider_time("2026-10-15T18:15Z")[1],
            weather.parse_provider_time("2026-10-15 18:15")[1],
        )

    def test_garbage_is_rejected(self):
        for bad in ("", "not-a-time", None):
            with self.assertRaises(weather.WeatherError):
                weather.parse_provider_time(bad)  # type: ignore[arg-type]


class SeriesTest(unittest.TestCase):
    def test_picks_the_newest_sample_at_or_before_now(self):
        series = {"2026-10-15T17:00": 1.0, "2026-10-15T18:00": 2.0, "2026-10-15T19:00": 3.0}
        self.assertEqual(weather.latest_at_or_before(series, "2026-10-15T18:15"), ("2026-10-15T18:00", 2.0))

    def test_exact_match_is_used(self):
        series = {"2026-10-15T18:00": 2.0}
        self.assertEqual(weather.latest_at_or_before(series, "2026-10-15T18:00"), ("2026-10-15T18:00", 2.0))

    def test_nothing_before_now_returns_none(self):
        self.assertIsNone(weather.latest_at_or_before({"2026-10-15T19:00": 3.0}, "2026-10-15T18:15"))


class SoilConversionTest(unittest.TestCase):
    def test_moisture_maps_onto_a_percentage(self):
        self.assertAlmostEqual(weather.soil_saturation_pct(0.25), 50.0)
        self.assertAlmostEqual(weather.soil_saturation_pct(0.0), 0.0)
        self.assertAlmostEqual(weather.soil_saturation_pct(0.5), 100.0)

    def test_percentage_is_clamped(self):
        self.assertEqual(weather.soil_saturation_pct(0.9), 100.0)


class ParseResponseTest(unittest.TestCase):
    def test_single_location_response(self):
        zone = LIVE_ZONE_DEFINITIONS[0]
        observations = weather.parse_response(
            entry(float(zone["latitude"]), float(zone["longitude"])), [zone]
        )
        observation = observations[str(zone["zone_id"])]
        self.assertTrue(observation.ok)
        self.assertEqual(observation.rainfall_mm_per_hr, 12.5)
        self.assertEqual(observation.soil_moisture_m3m3, 0.24)
        self.assertAlmostEqual(observation.soil_saturation_pct, 48.0)
        self.assertEqual(observation.observed_at, "2026-10-15T18:00:00Z")
        self.assertEqual(observation.observed_at_epoch, Responses.sample_epoch)
        self.assertEqual(observation.error, None)

    def test_multi_location_response_is_matched_positionally(self):
        observations = weather.parse_response(response_for(LIVE_ZONE_DEFINITIONS), LIVE_ZONE_DEFINITIONS)
        self.assertEqual(len(observations), 5)
        self.assertTrue(all(item.ok for item in observations.values()))

    def test_coordinate_mismatch_is_rejected_not_mislabeled(self):
        payload = response_for(LIVE_ZONE_DEFINITIONS)
        payload[2]["latitude"], payload[2]["longitude"] = 51.5, -0.12  # London instead of Bengaluru
        observations = weather.parse_response(payload, LIVE_ZONE_DEFINITIONS)
        self.assertFalse(observations["city-bengaluru"].ok)
        self.assertIn("does not match", str(observations["city-bengaluru"].error))
        self.assertIsNone(observations["city-bengaluru"].rainfall_mm_per_hr)
        self.assertTrue(observations["city-chennai"].ok)

    def test_missing_hourly_field_is_rejected(self):
        payload = response_for(LIVE_ZONE_DEFINITIONS[:1])
        del payload[0]["hourly"]["soil_moisture_0_to_7cm"]
        observation = weather.parse_response(payload, LIVE_ZONE_DEFINITIONS[:1])
        self.assertFalse(observation["city-chennai"].ok)
        self.assertIsNone(observation["city-chennai"].soil_saturation_pct)

    def test_missing_current_block_is_rejected(self):
        payload = response_for(LIVE_ZONE_DEFINITIONS[:1])
        del payload[0]["current"]
        observation = weather.parse_response(payload, LIVE_ZONE_DEFINITIONS[:1])
        self.assertFalse(observation["city-chennai"].ok)

    def test_fewer_entries_than_zones_marks_the_rest_unknown(self):
        payload = response_for(LIVE_ZONE_DEFINITIONS[:2])
        observations = weather.parse_response(payload, LIVE_ZONE_DEFINITIONS)
        self.assertTrue(observations["city-chennai"].ok)
        self.assertTrue(observations["city-mumbai"].ok)
        self.assertFalse(observations["city-bengaluru"].ok)
        self.assertEqual(observations["city-bengaluru"].error, "no_provider_entry")

    def test_absurd_rainfall_is_rejected_rather_than_clamped(self):
        payload = response_for(LIVE_ZONE_DEFINITIONS[:1])
        payload[0]["hourly"]["precipitation"] = [0.0, 9000.0, 0.0]
        observation = weather.parse_response(payload, LIVE_ZONE_DEFINITIONS[:1])
        self.assertFalse(observation["city-chennai"].ok)
        self.assertIn("outside the valid range", str(observation["city-chennai"].error))

    def test_soil_moisture_outside_physical_range_is_rejected(self):
        payload = response_for(LIVE_ZONE_DEFINITIONS[:1])
        payload[0]["hourly"]["soil_moisture_0_to_7cm"] = [0.2, 7.5, 0.2]
        observation = weather.parse_response(payload, LIVE_ZONE_DEFINITIONS[:1])
        self.assertFalse(observation["city-chennai"].ok)

    def test_non_object_payload_is_an_error(self):
        with self.assertRaises(weather.WeatherError):
            weather.parse_response("nope", LIVE_ZONE_DEFINITIONS[:1])


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def transport(url, timeout):
            self.calls.append((url, timeout))
            return self.status, self.body

        self.status = 200
        self.body = json.dumps(response_for(LIVE_ZONE_DEFINITIONS)).encode("utf-8")
        self.client = weather.WeatherClient(
            transport=transport, timeout=WEATHER_TIMEOUT_SECONDS, clock=lambda: Responses.current_epoch
        )

    def test_happy_path_returns_every_zone_from_one_request(self):
        result = self.client.fetch(LIVE_ZONE_DEFINITIONS)
        self.assertTrue(result.ok)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result.requested, 5)
        self.assertEqual(result.fetched_at_epoch, Responses.current_epoch)
        self.assertEqual(sorted(result.observations), sorted(str(z["zone_id"]) for z in LIVE_ZONE_DEFINITIONS))

    def test_http_error_is_reported_not_raised(self):
        self.status = 503
        result = self.client.fetch(LIVE_ZONE_DEFINITIONS)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "http_503")
        self.assertEqual(result.observations, {})

    def test_network_error_is_reported_not_raised(self):
        def transport(url, timeout):
            raise urllib.error.URLError("dns is broken")

        result = weather.WeatherClient(transport=transport).fetch(LIVE_ZONE_DEFINITIONS)
        self.assertFalse(result.ok)
        self.assertIn("network_error", str(result.error))

    def test_timeout_is_reported_not_raised(self):
        def transport(url, timeout):
            raise TimeoutError("too slow")

        result = weather.WeatherClient(transport=transport).fetch(LIVE_ZONE_DEFINITIONS)
        self.assertFalse(result.ok)
        self.assertIn("network_error", str(result.error))

    def test_broken_json_is_reported_not_raised(self):
        self.body = b"<html>not json</html>"
        result = self.client.fetch(LIVE_ZONE_DEFINITIONS)
        self.assertFalse(result.ok)
        self.assertIn("bad_json", str(result.error))

    def test_response_where_every_zone_fails_is_not_ok(self):
        payload = response_for(LIVE_ZONE_DEFINITIONS)
        for item in payload:
            del item["current"]
        self.body = json.dumps(payload).encode("utf-8")
        result = self.client.fetch(LIVE_ZONE_DEFINITIONS)
        self.assertFalse(result.ok)
        self.assertTrue(result.observations)
        self.assertIn("no_usable_observations", str(result.error))

    def test_partial_response_is_still_ok_with_per_zone_errors(self):
        payload = response_for(LIVE_ZONE_DEFINITIONS)
        del payload[0]["hourly"]["precipitation"]
        self.body = json.dumps(payload).encode("utf-8")
        result = self.client.fetch(LIVE_ZONE_DEFINITIONS)
        self.assertTrue(result.ok)
        self.assertFalse(result.observations["city-chennai"].ok)
        self.assertTrue(result.observations["city-mumbai"].ok)

    def test_no_zones_never_calls_the_provider(self):
        result = self.client.fetch([])
        self.assertFalse(result.ok)
        self.assertEqual(self.calls, [])

    def test_metric_sources_describe_the_two_real_fields(self):
        self.assertEqual(
            weather.metric_sources(),
            {"rainfall_mm_per_hr": "REAL", "soil_saturation_pct": "MODEL_DERIVED"},
        )


class TransportSafetyTest(unittest.TestCase):
    def test_default_transport_is_only_used_when_none_is_injected(self):
        self.assertIsNotNone(weather.default_transport)
        client = weather.WeatherClient()
        self.assertEqual(client.timeout, WEATHER_TIMEOUT_SECONDS)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

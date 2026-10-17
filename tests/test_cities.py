"""M11 tests: the fifteen-city fleet, its metadata, and how it fails.

Three groups:

* **the city table** - fifteen unique cities, each with a state and a region, valid and
  distinct coordinates, no city/state mismatch, and a coverage summary that agrees with
  the list it summarises. These are data-quality tests: they fail on a typo, a duplicate
  or a half-filled row, not on a missing feature.
* **the API** - every configured city is exposed by ``GET /zones`` and by the history and
  incidents routes that already existed, an unknown zone is still rejected, and the demo
  fleet is still isolated from the live one.
* **the failures** - one city, several cities, a provider that answers for only some
  coordinates, a malformed entry, a timeout and a total outage. In every case the cities
  that can update do, the others keep their last good values with an honest age, the
  endpoint still answers, and nothing is relabelled as a fresh real observation.

Everything runs in-process against the in-memory store with an injected transport: no AWS,
no network, no wall clock.
"""

from __future__ import annotations

import json
import math
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, "src")

from floodpulse import notify, weather
from floodpulse.config import (
    COVERAGE_SCOPE_NOTE,
    DEFAULT_THRESHOLDS,
    DEMO_ZONE_DEFINITIONS,
    HYDRAULICS_DRAIN_TAU_SECONDS,
    HYDRAULICS_RAIN_REFERENCE_MM_PER_HR,
    LIVE_ZONE_DEFINITIONS,
    METRICS,
    REGIONS,
    SOURCE_MODEL_DERIVED,
    SOURCE_REAL,
    SOURCE_SIMULATED,
    WEATHER_MODE_ENV,
    WEATHER_MODE_LIVE,
    WEATHER_LOCATION_TOLERANCE_DEGREES,
    WEATHER_REFRESH_SECONDS,
    ZONE_MODE_ENV,
    ZONE_MODE_LIVE,
    city_scope_for,
    coverage_summary,
    thresholds_for,
    zone_definition,
    zone_ids_for_mode,
)
from floodpulse.handler import dispatch
from floodpulse.live_weather import hydraulic_target_drain_m
from floodpulse.storage import LocalStore
from floodpulse.weather import WeatherClient

NOW = 1_792_088_400.0
LIVE_ENV = {ZONE_MODE_ENV: "live", WEATHER_MODE_ENV: WEATHER_MODE_LIVE}

EXPECTED_CITIES = {
    "city-chennai": ("Chennai", "Tamil Nadu", "South"),
    "city-mumbai": ("Mumbai", "Maharashtra", "West"),
    "city-bengaluru": ("Bengaluru", "Karnataka", "South"),
    "city-hyderabad": ("Hyderabad", "Telangana", "South"),
    "city-kochi": ("Kochi", "Kerala", "South"),
    "city-delhi": ("Delhi", "Delhi", "North"),
    "city-kolkata": ("Kolkata", "West Bengal", "East"),
    "city-guwahati": ("Guwahati", "Assam", "Northeast"),
    "city-patna": ("Patna", "Bihar", "East"),
    "city-bhubaneswar": ("Bhubaneswar", "Odisha", "East"),
    "city-visakhapatnam": ("Visakhapatnam", "Andhra Pradesh", "South"),
    "city-pune": ("Pune", "Maharashtra", "West"),
    "city-ahmedabad": ("Ahmedabad", "Gujarat", "West"),
    "city-jaipur": ("Jaipur", "Rajasthan", "North"),
    "city-lucknow": ("Lucknow", "Uttar Pradesh", "North"),
}

NEW_CITIES = tuple(sorted(set(EXPECTED_CITIES) - {
    "city-chennai", "city-mumbai", "city-bengaluru", "city-hyderabad", "city-kochi",
}))


# --------------------------------------------------------------------------- helpers ---


def entry(zone, *, rain=12.5, soil=0.24, current="2026-10-15T18:15", broken=False):
    """One provider entry for a zone, in the shape Open-Meteo really returns."""
    latitude = float(zone["latitude"])
    longitude = float(zone["longitude"])
    if broken:
        # the coordinates are right but the payload is unusable for this coordinate only
        return {"latitude": latitude, "longitude": longitude}
    return {
        "latitude": latitude,
        "longitude": longitude,
        "current": {"time": current, "precipitation": rain, "rain": rain},
        "hourly": {
            "time": ["2026-10-15T17:00", "2026-10-15T18:00", "2026-10-15T19:00"],
            "precipitation": [0.0, rain, rain * 2],
            "soil_moisture_0_to_7cm": [soil - 0.03, soil, soil + 0.02],
        },
    }


def body_for(zones, *, drop=(), broken=(), rain=12.5, soil=0.24, rain_by_zone=None):
    """A multi-coordinate response; ``drop`` omits entries, ``broken`` corrupts them."""
    per_zone = dict(rain_by_zone or {})
    return [
        entry(zone, rain=per_zone.get(str(zone["zone_id"]), rain), soil=soil,
              broken=str(zone["zone_id"]) in broken)
        for zone in zones
        if str(zone["zone_id"]) not in drop
    ]


class ProviderStub:
    """A real :class:`WeatherClient` with an injected transport, counting its calls."""

    def __init__(self, zones, *, drop=(), broken=(), rain=12.5, soil=0.24, rain_by_zone=None,
                 status=200, raises=None):
        body = json.dumps(body_for(zones, drop=drop, broken=broken, rain=rain, soil=soil,
                                   rain_by_zone=rain_by_zone)).encode("utf-8")
        self.calls = 0

        def transport(url, timeout):
            if raises is not None:
                raise raises
            return status, body

        self.client = WeatherClient(transport=transport, timeout=4.0, clock=lambda: NOW)
        self._transport = transport

    def counting_transport(self):
        """A transport that counts, for the tests that pass the client in explicitly."""
        def transport(url, timeout):
            self.calls += 1
            return self._transport(url, timeout)

        return transport

    def fetch(self, zones):
        self.calls += 1
        return self.client.fetch(zones)


def client_for(zones, **kwargs):
    """A stub provider; ``.calls`` counts the provider requests it answered."""
    return ProviderStub(zones, **kwargs)


def zones(store=None, *, now_epoch=NOW, client=None, env=None):
    status, payload = dispatch(
        "GET", "/zones", store=store if store is not None else LocalStore(),
        now_epoch=now_epoch, env=env or dict(LIVE_ENV), weather_client=client,
    )
    assert status == 200, f"expected 200, got {status}: {payload}"
    return payload


def incidents(store, *, now_epoch=NOW, zone_id=None, limit=None, env=None):
    query = {}
    if zone_id is not None:
        query["zone_id"] = zone_id
    if limit is not None:
        query["limit"] = limit
    return dispatch("GET", "/incidents", store=store, now_epoch=now_epoch, query=query,
                    env=env or dict(LIVE_ENV))


# ------------------------------------------------------------------ the city table ---


class CityTableTest(unittest.TestCase):
    """Fifteen cities, complete and consistent metadata, one row each."""

    def test_the_live_fleet_is_fifteen_cities(self):
        self.assertEqual(len(LIVE_ZONE_DEFINITIONS), 15)
        self.assertEqual(len(zone_ids_for_mode("live")), 15)
        self.assertEqual(set(zone_ids_for_mode("live")), set(EXPECTED_CITIES))

    def test_every_zone_id_is_unique_and_namespaced(self):
        ids = [str(zone["zone_id"]) for zone in LIVE_ZONE_DEFINITIONS]
        self.assertEqual(len(ids), len(set(ids)), "a zone id is repeated")
        for zone_id in ids:
            self.assertTrue(zone_id.startswith("city-"), zone_id)
            self.assertNotIn(" ", zone_id)
            self.assertEqual(zone_id, zone_id.lower())

    def test_every_city_name_is_unique(self):
        names = [str(zone["name"]) for zone in LIVE_ZONE_DEFINITIONS]
        self.assertEqual(len(names), len(set(names)), "two rows describe the same city")

    def test_every_city_has_a_state_and_a_region(self):
        for zone in LIVE_ZONE_DEFINITIONS:
            zone_id = str(zone["zone_id"])
            self.assertTrue(str(zone.get("state") or "").strip(), f"{zone_id} has no state")
            self.assertTrue(str(zone.get("region") or "").strip(), f"{zone_id} has no region")
            self.assertIn(zone["region"], REGIONS, f"{zone_id} is in no known region")
            self.assertEqual(zone.get("country"), "IN", f"{zone_id} has no country")

    def test_city_state_and_region_match_the_documented_table(self):
        actual = {
            str(zone["zone_id"]): (str(zone["name"]), str(zone["state"]), str(zone["region"]))
            for zone in LIVE_ZONE_DEFINITIONS
        }
        self.assertEqual(actual, EXPECTED_CITIES)

    def test_no_state_is_split_across_two_spellings(self):
        states = {}
        for zone in LIVE_ZONE_DEFINITIONS:
            states.setdefault(str(zone["state"]), set()).add(str(zone["region"]))
        for state, regions in states.items():
            self.assertEqual(len(regions), 1, f"{state} is filed under {sorted(regions)}")

    def test_coordinates_are_valid_inside_india(self):
        for zone in LIVE_ZONE_DEFINITIONS:
            zone_id = str(zone["zone_id"])
            latitude = float(zone["latitude"])
            longitude = float(zone["longitude"])
            self.assertTrue(-90.0 <= latitude <= 90.0, f"{zone_id} latitude {latitude}")
            self.assertTrue(-180.0 <= longitude <= 180.0, f"{zone_id} longitude {longitude}")
            # The fleet is Indian, so a coordinate outside the country's bounding box is a
            # typo (a dropped sign, a swapped pair), not a city in the list.
            self.assertTrue(6.0 <= latitude <= 38.0, f"{zone_id} latitude {latitude} is outside India")
            self.assertTrue(68.0 <= longitude <= 98.0, f"{zone_id} longitude {longitude} is outside India")

    def test_no_two_cities_share_a_coordinate(self):
        seen: dict[tuple[float, float], str] = {}
        for zone in LIVE_ZONE_DEFINITIONS:
            point = (round(float(zone["latitude"]), 4), round(float(zone["longitude"]), 4))
            self.assertNotIn(point, seen, f"{zone['zone_id']} sits on top of {seen.get(point)}")
            seen[point] = str(zone["zone_id"])

    def test_no_two_cities_are_close_enough_to_confuse_the_parser(self):
        # The provider parser accepts an entry for a zone when both coordinates agree within
        # WEATHER_LOCATION_TOLERANCE_DEGREES. Two requested cities inside that same box would
        # make an entry ambiguous, so the fleet must stay clear of it - this is the property
        # that lets the parser match by coordinate when the provider's order is unusable.
        for index, first in enumerate(LIVE_ZONE_DEFINITIONS):
            for second in LIVE_ZONE_DEFINITIONS[index + 1:]:
                same_box = (
                    abs(float(first["latitude"]) - float(second["latitude"]))
                    <= WEATHER_LOCATION_TOLERANCE_DEGREES
                    and abs(float(first["longitude"]) - float(second["longitude"]))
                    <= WEATHER_LOCATION_TOLERANCE_DEGREES
                )
                self.assertFalse(
                    same_box,
                    f"{first['zone_id']} and {second['zone_id']} are within "
                    f"{WEATHER_LOCATION_TOLERANCE_DEGREES} degrees of each other",
                )

    def test_every_configured_city_is_fully_defined(self):
        for zone in LIVE_ZONE_DEFINITIONS:
            zone_id = str(zone["zone_id"])
            definition = zone_definition(zone_id)
            self.assertEqual(definition["zone_id"], zone_id)
            self.assertIn("country", definition)
            self.assertTrue(str(definition["description"]).strip(), f"{zone_id} has no description")
            thresholds = thresholds_for(zone_id)
            self.assertEqual(sorted(thresholds), sorted(METRICS))
            for metric, band in thresholds.items():
                self.assertLess(band["watch"], band["critical"], f"{zone_id} {metric}")
            self.assertIn(thresholds["rainfall_mm_per_hr"]["watch"],
                          [DEFAULT_THRESHOLDS["rainfall_mm_per_hr"]["watch"]])

    def test_city_scope_labels_are_derived_and_safe(self):
        for zone_id, (name, state, region) in EXPECTED_CITIES.items():
            scope = city_scope_for(zone_id)
            self.assertEqual(scope["city"], name)
            self.assertEqual(scope["state"], state)
            self.assertEqual(scope["region"], region)
            self.assertEqual(scope["display_name"], f"{name}, {state}")
        # An id with no definition degrades to itself: no invented state, no exception.
        ghost = city_scope_for("city-atlantis")
        self.assertEqual(ghost["display_name"], "city-atlantis")
        self.assertIsNone(ghost["state"])
        self.assertIsNone(ghost["region"])
        self.assertEqual(notify.zone_geography({"zone_id": "city-atlantis"})["display_name"],
                         "city-atlantis")

    def test_the_coverage_summary_describes_the_list_it_summarises(self):
        coverage = coverage_summary("live")
        self.assertEqual(coverage["fleet_kind"], "cities")
        self.assertEqual(coverage["city_count"], len(LIVE_ZONE_DEFINITIONS))
        self.assertEqual(sum(region["city_count"] for region in coverage["regions"]),
                         coverage["city_count"])
        self.assertEqual([region["region"] for region in coverage["regions"]],
                         [region for region in REGIONS if any(
                             zone["region"] == region for zone in LIVE_ZONE_DEFINITIONS)])
        self.assertEqual(coverage["state_count"], len(coverage["states"]))
        self.assertEqual(coverage["states"], sorted(coverage["states"]))
        self.assertEqual(set(coverage["states"]),
                         {str(zone["state"]) for zone in LIVE_ZONE_DEFINITIONS})
        self.assertEqual(coverage["scope_note"], COVERAGE_SCOPE_NOTE)
        self.assertIn("not states, regions or the country", coverage["scope_note"])

    def test_the_regions_split_the_fleet_the_documented_way(self):
        coverage = coverage_summary("live")
        grouped = {region["region"]: region["cities"] for region in coverage["regions"]}
        self.assertEqual(grouped["North"], ["Delhi", "Jaipur", "Lucknow"])
        self.assertEqual(grouped["South"],
                         ["Chennai", "Bengaluru", "Hyderabad", "Kochi", "Visakhapatnam"])
        self.assertEqual(grouped["East"], ["Kolkata", "Patna", "Bhubaneswar"])
        self.assertEqual(grouped["West"], ["Mumbai", "Pune", "Ahmedabad"])
        self.assertEqual(grouped["Northeast"], ["Guwahati"])

    def test_the_demo_fleet_is_untouched_and_carries_no_geography(self):
        self.assertEqual(len(DEMO_ZONE_DEFINITIONS), 3)
        for zone in DEMO_ZONE_DEFINITIONS:
            self.assertNotIn("state", zone)
            self.assertNotIn("region", zone)
            scope = city_scope_for(str(zone["zone_id"]))
            self.assertIsNone(scope["state"])
            self.assertEqual(scope["display_name"], zone["name"])
        demo = coverage_summary("demo")
        self.assertEqual(demo["fleet_kind"], "zones")
        self.assertEqual(demo["city_count"], 3)
        self.assertEqual(demo["state_count"], 0)
        self.assertEqual(demo["regions"], [])
        self.assertFalse(set(zone_ids_for_mode("demo")) & set(EXPECTED_CITIES))


# ------------------------------------------------------------------- the API ---


class CityApiTest(unittest.TestCase):
    """Every configured city is served, named and reachable through the existing routes."""

    def setUp(self):
        self.store = LocalStore()
        self.client = client_for(LIVE_ZONE_DEFINITIONS)

    def test_zones_exposes_every_city_with_its_geography(self):
        payload = zones(self.store, client=self.client)
        self.assertEqual(payload["zone_count"], 15)
        self.assertEqual(len(payload["zones"]), 15)
        for zone in payload["zones"]:
            name, state, region = EXPECTED_CITIES[zone["zone_id"]]
            self.assertEqual(zone["name"], name)
            self.assertEqual(zone["city"], name)
            self.assertEqual(zone["state"], state)
            self.assertEqual(zone["region"], region)
            self.assertEqual(zone["display_name"], f"{name}, {state}")
            self.assertEqual(zone["status"], "live")
            self.assertIn(zone["latest"]["risk_class"], ("NORMAL", "WATCH", "WARNING", "CRITICAL"))

    def test_the_fleet_blocks_agree_with_the_zone_list(self):
        payload = zones(self.store, client=self.client)
        ids = {zone["zone_id"] for zone in payload["zones"]}
        self.assertEqual(set(payload["zone_ranking"]), ids)
        self.assertEqual(set(payload["weather"]["live_zones"]), ids)
        self.assertEqual(set(payload["weather"]["cache_age_seconds"]), ids)
        self.assertEqual(payload["coverage"]["city_count"], len(ids))
        self.assertIn(payload["highest_risk_zone"]["zone_id"], ids)
        self.assertEqual(
            payload["highest_risk_zone"]["display_name"],
            "{} , {}".format(*EXPECTED_CITIES[payload["highest_risk_zone"]["zone_id"]][:2])
            .replace(" , ", ", "),
        )

    def test_every_city_keeps_its_provenance_tags(self):
        payload = zones(self.store, client=self.client)
        for zone in payload["zones"]:
            self.assertEqual(zone["metric_sources"], {
                "rainfall_mm_per_hr": SOURCE_REAL,
                "drain_level_m": SOURCE_SIMULATED,
                "soil_saturation_pct": SOURCE_MODEL_DERIVED,
                "water_level_m": SOURCE_SIMULATED,
                "flow_rate_m3_s": SOURCE_SIMULATED,
            })
            self.assertEqual(sorted(zone["latest"]["readings"]), sorted(METRICS))
            self.assertFalse(zone["latest"]["simulated"])

    def test_a_new_city_has_history_without_a_new_route(self):
        zones(self.store, client=self.client)
        for zone_id in NEW_CITIES:
            status, body = dispatch("GET", f"/zones/{zone_id}/history", store=self.store,
                                    now_epoch=NOW, env=dict(LIVE_ENV))
            self.assertEqual(status, 200, f"{zone_id}: {body}")
            self.assertEqual(body["zone_id"], zone_id)
            self.assertTrue(body["points"], f"{zone_id} has no history")
            self.assertEqual(body["simulated"], False)
            self.assertEqual(body["metric_sources"]["rainfall_mm_per_hr"], SOURCE_REAL)
            for point in body["points"]:
                self.assertEqual(sorted(point["readings"]), sorted(METRICS))

    def test_an_unknown_city_is_still_rejected(self):
        for route in ("/zones/city-atlantis/history", "/zones/city-chennai-55/history",
                      "/zones/zone-riverside-north/history"):
            status, body = dispatch("GET", route, store=self.store, now_epoch=NOW, env=dict(LIVE_ENV))
            self.assertEqual(status, 404, f"{route}: {body}")
            self.assertEqual(body["error"], "unknown_zone")
            self.assertNotIn("city-atlantis", body["known_zones"])

    def test_the_incidents_route_accepts_every_city_and_rejects_the_rest(self):
        status, body = incidents(self.store)
        self.assertEqual(status, 200)
        for zone_id in EXPECTED_CITIES:
            status, filtered = incidents(self.store, zone_id=zone_id)
            self.assertEqual(status, 200, f"{zone_id}: {filtered}")
            self.assertEqual(filtered["zone_id"], zone_id)
        status, body = incidents(self.store, zone_id="city-atlantis")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "unknown_zone")

    def test_demo_mode_serves_the_demo_fleet_and_none_of_the_cities(self):
        payload = zones(client=None, env={ZONE_MODE_ENV: "demo"})
        ids = {zone["zone_id"] for zone in payload["zones"]}
        self.assertEqual(payload["zone_count"], 3)
        self.assertFalse(ids & set(EXPECTED_CITIES))
        for zone in payload["zones"]:
            self.assertNotIn("state", zone)
            self.assertNotIn("region", zone)
        self.assertEqual(payload["coverage"]["fleet_kind"], "zones")

    def test_the_weather_block_names_every_city_and_no_demo_zone(self):
        payload = zones(self.store, client=self.client)
        live = payload["weather"]
        self.assertTrue(live["enabled"])
        self.assertEqual(set(live["live_zones"]), set(EXPECTED_CITIES))
        for key in ("cache_age_seconds", "observation_age_seconds", "last_attempt_age_seconds",
                    "observation_is_current", "last_attempt_ok", "last_attempt_error"):
            self.assertEqual(set(live[key]), set(EXPECTED_CITIES), key)


# ------------------------------------------------------------ failure isolation ---


class CityFailureIsolationTest(unittest.TestCase):
    """A city that cannot be fetched never takes the fleet down with it."""

    def setUp(self):
        self.store = LocalStore()
        # one good refresh first, so every city has a last-known-good value to keep
        self.good = zones(self.store, client=client_for(LIVE_ZONE_DEFINITIONS, rain=11.0))

    def last_good(self, payload, zone_id):
        return next(zone for zone in payload["zones"] if zone["zone_id"] == zone_id)

    def test_one_city_failing_keeps_the_other_fourteen_updating(self):
        failing = "city-lucknow"
        later = NOW + WEATHER_REFRESH_SECONDS + 1
        payload = zones(self.store, now_epoch=later,
                        client=client_for(LIVE_ZONE_DEFINITIONS, drop={failing}, rain=30.0))

        refreshed = payload["weather"]["last_refresh"]["refreshed"]
        self.assertEqual(set(refreshed), set(EXPECTED_CITIES) - {failing})
        self.assertEqual(payload["weather"]["last_refresh"]["failed"], [failing])
        self.assertEqual(payload["zone_count"], 15)

        # the fourteen that answered moved to the new rainfall
        for zone_id in refreshed:
            self.assertEqual(self.last_good(payload, zone_id)["latest"]["readings"]["rainfall_mm_per_hr"], 30.0)
        # the failed one still serves its earlier real observation, with an older age
        kept = self.last_good(payload, failing)
        self.assertEqual(kept["latest"]["readings"]["rainfall_mm_per_hr"], 11.0)
        self.assertEqual(kept["status"], "live")
        self.assertGreater(kept["latest"]["age_seconds"],
                           self.last_good(self.good, failing)["latest"]["age_seconds"])
        # ... and is never presented as a fresh real fetch
        self.assertFalse(payload["weather"]["last_attempt_ok"][failing])
        self.assertNotEqual(payload["weather"]["last_attempt_error"][failing], None)
        self.assertGreater(payload["weather"]["cache_age_seconds"][failing],
                           payload["weather"]["cache_age_seconds"][refreshed[0]])

    def test_several_cities_failing_at_once(self):
        failing = {"city-delhi", "city-kolkata", "city-guwahati", "city-patna"}
        later = NOW + WEATHER_REFRESH_SECONDS + 1
        payload = zones(self.store, now_epoch=later,
                        client=client_for(LIVE_ZONE_DEFINITIONS, drop=failing, rain=25.0))
        refreshed = set(payload["weather"]["last_refresh"]["refreshed"])
        self.assertEqual(refreshed, set(EXPECTED_CITIES) - failing)
        self.assertEqual(set(payload["weather"]["last_refresh"]["failed"]), failing)
        for zone_id in failing:
            self.assertEqual(self.last_good(payload, zone_id)["latest"]["readings"]["rainfall_mm_per_hr"], 11.0)
            self.assertIs(payload["weather"]["last_attempt_ok"][zone_id], False)
        for zone_id in refreshed:
            self.assertEqual(self.last_good(payload, zone_id)["latest"]["readings"]["rainfall_mm_per_hr"], 25.0)

    def test_a_partial_response_costs_only_the_coordinates_it_omits(self):
        # the provider answers with fewer entries than requested: the positional parser
        # reports the missing ones as failed rather than mislabelling their neighbours
        later = NOW + WEATHER_REFRESH_SECONDS + 1
        payload = zones(self.store, now_epoch=later,
                        client=client_for(LIVE_ZONE_DEFINITIONS, drop={"city-jaipur"}, rain=18.0))
        self.assertIn("no_provider_entry", str(payload["weather"]["last_attempt_error"]["city-jaipur"]))
        self.assertEqual(payload["weather"]["last_refresh"]["failed"], ["city-jaipur"])
        self.assertEqual(len(payload["weather"]["last_refresh"]["refreshed"]), 14)
        # no neighbour was relabelled with the missing entry's data
        self.assertEqual(self.last_good(payload, "city-jaipur")["latest"]["readings"]["rainfall_mm_per_hr"], 11.0)
        self.assertEqual(self.last_good(payload, "city-pune")["latest"]["readings"]["rainfall_mm_per_hr"], 18.0)

    def test_one_malformed_entry_does_not_poison_the_fleet(self):
        later = NOW + WEATHER_REFRESH_SECONDS + 1
        payload = zones(self.store, now_epoch=later,
                        client=client_for(LIVE_ZONE_DEFINITIONS, broken={"city-visakhapatnam"}, rain=21.0))
        self.assertEqual(payload["weather"]["last_refresh"]["failed"], ["city-visakhapatnam"])
        self.assertIn("current", str(payload["weather"]["last_attempt_error"]["city-visakhapatnam"]))
        self.assertEqual(len(payload["weather"]["last_refresh"]["refreshed"]), 14)
        self.assertEqual(self.last_good(payload, "city-visakhapatnam")["latest"]["readings"]["rainfall_mm_per_hr"], 11.0)

    def test_a_provider_timeout_keeps_the_whole_fleet_on_its_last_good_values(self):
        later = NOW + WEATHER_REFRESH_SECONDS + 1
        payload = zones(self.store, now_epoch=later,
                        client=client_for(LIVE_ZONE_DEFINITIONS, raises=TimeoutError("timed out")))
        refresh = payload["weather"]["last_refresh"]
        self.assertTrue(refresh["attempted"])
        self.assertEqual(refresh["refreshed"], [])
        self.assertEqual(set(refresh["failed"]), set(EXPECTED_CITIES))
        self.assertIn("timed out", str(refresh["error"]))
        self.assertEqual(payload["zone_count"], 15)
        for zone in payload["zones"]:
            self.assertEqual(zone["status"], "live")
            self.assertEqual(zone["latest"]["readings"]["rainfall_mm_per_hr"], 11.0)
            self.assertIs(payload["weather"]["last_attempt_ok"][zone["zone_id"]], False)

    def test_a_total_outage_is_reported_not_hidden(self):
        later = NOW + WEATHER_REFRESH_SECONDS + 1
        payload = zones(self.store, now_epoch=later,
                        client=client_for(LIVE_ZONE_DEFINITIONS, status=503))
        refresh = payload["weather"]["last_refresh"]
        self.assertEqual(refresh["refreshed"], [])
        self.assertEqual(len(refresh["failed"]), 15)
        self.assertIn("503", str(refresh["error"]))
        self.assertEqual(len(payload["zones"]), 15)
        for zone in payload["zones"]:
            self.assertTrue(zone["latest"]["readings"], zone["zone_id"])

    def test_an_outage_on_a_cold_start_serves_every_city_without_data(self):
        payload = zones(LocalStore(), client=client_for(LIVE_ZONE_DEFINITIONS, status=503))
        self.assertEqual(payload["zone_count"], 15)
        for zone in payload["zones"]:
            self.assertEqual(zone["status"], "no_data")
            self.assertIsNone(zone["latest"])
            self.assertIsNone(zone["weather"]["cache_age_seconds"])
            self.assertIs(zone["weather"]["last_attempt_ok"], False)
        self.assertIsNone(payload["highest_risk_zone"])

    def test_a_failing_city_does_not_produce_an_incident_from_nothing(self):
        later = NOW + WEATHER_REFRESH_SECONDS + 1
        payload = zones(self.store, now_epoch=later,
                        client=client_for(LIVE_ZONE_DEFINITIONS, drop={"city-kochi"}, rain=60.0))
        status, incidents = dispatch("GET", "/incidents?zone_id=city-kochi", store=self.store,
                                     now_epoch=later, env=dict(LIVE_ENV))
        self.assertEqual(status, 200)
        self.assertEqual(incidents["count"], 0)
        self.assertEqual(self.last_good(payload, "city-kochi")["latest"]["readings"]["rainfall_mm_per_hr"], 11.0)


# ------------------------------------------------------------------ the cache ---


class CityCacheTest(unittest.TestCase):
    """Fifteen cities still cost one provider call per refresh window, not fifteen."""

    def test_one_refresh_serves_every_poll_for_the_whole_fleet(self):
        store = LocalStore()
        first = client_for(LIVE_ZONE_DEFINITIONS)
        zones(store, client=first)
        self.assertEqual(first.calls, 1)

        # the dashboard polls every five seconds: every poll inside the window is answered
        # from the cache, for all fifteen cities, without another provider request
        for step in range(1, 120):
            payload = zones(store, now_epoch=NOW + step * 5, client=None)
            self.assertFalse(payload["weather"]["last_refresh"]["attempted"],
                             f"poll {step} called the provider again")
            self.assertEqual(payload["weather"]["last_refresh"]["reason"], "cache_fresh")
            self.assertEqual(len(payload["weather"]["last_refresh"]["already_fresh"]), 15)

        self.assertEqual(first.calls, 1)

    def test_the_window_is_unchanged_and_the_fleet_refreshes_once_after_it(self):
        store = LocalStore()
        first = client_for(LIVE_ZONE_DEFINITIONS)
        zones(store, client=first)
        self.assertEqual(first.calls, 1)

        # still inside the documented window
        zones(store, now_epoch=NOW + WEATHER_REFRESH_SECONDS - 1, client=None)
        self.assertEqual(first.calls, 1)

        second = client_for(LIVE_ZONE_DEFINITIONS, rain=40.0)
        payload = zones(store, now_epoch=NOW + WEATHER_REFRESH_SECONDS + 1, client=second)
        self.assertEqual(second.calls, 1)
        self.assertEqual(len(payload["weather"]["last_refresh"]["refreshed"]), 15)
        self.assertEqual(WEATHER_REFRESH_SECONDS, 600)

    def test_every_city_benefits_from_the_cache(self):
        store = LocalStore()
        first = client_for(LIVE_ZONE_DEFINITIONS)
        zones(store, client=first)
        payload = zones(store, now_epoch=NOW + 5, client=None)
        self.assertEqual(first.calls, 1)
        ages = payload["weather"]["cache_age_seconds"]
        self.assertEqual(set(ages), set(EXPECTED_CITIES))
        for zone_id, age in ages.items():
            self.assertIsNotNone(age, zone_id)
            self.assertLessEqual(age, 5)


# --------------------------------------------------------------- incidents ---


class CityIncidentTest(unittest.TestCase):
    """Incidents stay zone-specific when fifteen cities are scored together."""

    def setUp(self):
        self.store = LocalStore()

    def test_only_the_cities_whose_rain_crosses_a_band_open_incidents(self):
        # every city reports heavy rain, so the band is crossed everywhere: each incident must
        # belong to exactly one city, and no city may be missed
        # Sustained heavy rain everywhere: run the fleet forward until every city's readings
        # have escalated. One reading is deliberately not enough - the hydraulics lag, so a
        # zone needs a sequence to cross a band, exactly as a real catchment would.
        store = LocalStore()
        payload = None
        for step in range(1, 13):
            moment = NOW + step * (WEATHER_REFRESH_SECONDS + 1)
            payload = zones(store, now_epoch=moment,
                            client=client_for(LIVE_ZONE_DEFINITIONS, rain=60.0, soil=0.48))
            if all(zone["latest"]["risk_class"] != "NORMAL" for zone in payload["zones"]):
                break
        self.assertTrue(all(zone["latest"]["risk_class"] != "NORMAL" for zone in payload["zones"]),
                        "no city escalated despite sustained heavy rain everywhere")
        status, body = incidents(store, now_epoch=NOW, limit=50)
        self.assertEqual(status, 200)
        by_zone: dict[str, int] = {}
        for incident in body["incidents"]:
            by_zone[incident["zone_id"]] = by_zone.get(incident["zone_id"], 0) + 1
            self.assertIn(incident["zone_id"], EXPECTED_CITIES)
            name, state, _region = EXPECTED_CITIES[incident["zone_id"]]
            self.assertEqual(incident["zone_name"], name)
            self.assertEqual(incident["zone_display_name"], f"{name}, {state}")
            self.assertEqual(incident["zone_state"], state)
            self.assertEqual(incident["current_status"], "OPEN")
        for zone_id, count in by_zone.items():
            self.assertEqual(count, 1, f"{zone_id} opened {count} incidents for one crossing")
        self.assertEqual(len(by_zone), len(payload["zones"]), "a city's reading opened nothing")
        self.assertEqual(body["open_count"], 15)

    def test_one_city_escalating_does_not_move_another_city(self):
        # Two identical fleets on two identical clocks, differing in exactly one coordinate:
        # one city reports an extreme reading, the other fourteen report nothing. The storm
        # city must escalate on its own, and every other city must sit exactly where the
        # calm fleet sat at the same step - same score, same class, no incident.
        storm = "city-bhubaneswar"
        calm_store, storm_store = LocalStore(), LocalStore()

        def calm_client():
            return client_for(LIVE_ZONE_DEFINITIONS, rain=0.0, soil=0.10)

        def storm_client():
            return client_for(LIVE_ZONE_DEFINITIONS, rain=0.0, soil=0.10, rain_by_zone={storm: 80.0})

        escalated_at: int | None = None
        for step in range(1, 13):
            moment = NOW + step * (WEATHER_REFRESH_SECONDS + 1)
            calm = zones(calm_store, now_epoch=moment, client=calm_client())
            stormy = zones(storm_store, now_epoch=moment, client=storm_client())
            by_id = {zone["zone_id"]: (zone["latest"]["risk_score"], zone["latest"]["risk_class"])
                     for zone in calm["zones"]}
            for zone in stormy["zones"]:
                if zone["zone_id"] == storm:
                    continue
                self.assertEqual(
                    (zone["latest"]["risk_score"], zone["latest"]["risk_class"]),
                    by_id[zone["zone_id"]], f"step {step}: {zone['zone_id']} moved on its own",
                )
                self.assertEqual(zone["latest"]["readings"]["rainfall_mm_per_hr"], 0.0, zone["zone_id"])
            gust = next(zone for zone in stormy["zones"] if zone["zone_id"] == storm)
            if gust["latest"]["risk_class"] in ("WARNING", "CRITICAL"):
                escalated_at = step
                break

        self.assertIsNotNone(escalated_at, "the storm city never escalated on its own readings")
        # The storm city's water did react: its drain level is far above the level its own soil
        # saturation alone supports, while the fourteen with no rain sit exactly on that floor.
        # (A cold fleet initialises each zone's hydraulics at the value its first reading
        # implies, so this escalation lands in one refresh; the lag shows up once a city has a
        # previous reading, which the test below measures.)
        quiet_floor = hydraulic_target_drain_m(0.0, 20.0)
        gust = next(zone for zone in stormy["zones"] if zone["zone_id"] == storm)
        self.assertGreater(gust["latest"]["readings"]["drain_level_m"], quiet_floor + 1.0,
                           "the storm city's drain did not react")
        for zone in stormy["zones"]:
            if zone["zone_id"] == storm:
                continue
            self.assertAlmostEqual(zone["latest"]["readings"]["drain_level_m"], quiet_floor,
                                   places=6, msg=zone["zone_id"])

        status, body = incidents(storm_store, now_epoch=NOW, limit=50)
        self.assertEqual(status, 200)
        self.assertEqual({incident["zone_id"] for incident in body["incidents"]}, {storm})
        status, quiet = incidents(calm_store, now_epoch=NOW, limit=50)
        self.assertEqual(status, 200)
        self.assertEqual(quiet["count"], 0)

    def test_the_hydraulics_use_one_model_for_every_city(self):
        # No city has hydraulic parameters of its own: with a previous reading to lag from, a
        # new city's drain covers the same fixed fraction of the distance to the value its own
        # rainfall and soil imply, at the same 900 s time constant, and never overshoots it.
        # Two different new cities must behave identically.
        soil_pct = 20.0
        calm_target = hydraulic_target_drain_m(0.0, soil_pct)
        storm_target = hydraulic_target_drain_m(80.0, soil_pct)
        step_seconds = WEATHER_REFRESH_SECONDS + 1
        stage_fraction = 1.0 - math.exp(-step_seconds / HYDRAULICS_DRAIN_TAU_SECONDS)

        for storm in ("city-bhubaneswar", "city-lucknow"):
            with self.subTest(storm=storm):
                store = LocalStore()
                zones(store, client=client_for(LIVE_ZONE_DEFINITIONS, rain=0.0, soil=0.10))
                previous = round(calm_target, 3)
                moment = NOW
                classes = ["NORMAL"]
                payload = None
                for _ in range(6):
                    moment += step_seconds
                    payload = zones(store, now_epoch=moment,
                                    client=client_for(LIVE_ZONE_DEFINITIONS, rain=0.0, soil=0.10,
                                                      rain_by_zone={storm: 80.0}))
                    zone = next(item for item in payload["zones"] if item["zone_id"] == storm)
                    drain = zone["latest"]["readings"]["drain_level_m"]
                    # the same first-order lag, to the published millimetre: the value the
                    # model reads back is the rounded one it wrote, so the expectation is
                    # rounded the same way and the comparison stays exact
                    expected = round(previous + (storm_target - previous) * stage_fraction, 3)
                    self.assertAlmostEqual(
                        drain, expected, places=9,
                        msg=f"{storm}: the lag step is not the documented one",
                    )
                    self.assertLessEqual(drain, storm_target + 1e-9, f"{storm} overshot its target")
                    self.assertGreater(drain, previous, f"{storm}'s drain did not rise")
                    previous = drain
                    classes.append(zone["latest"]["risk_class"])

                self.assertEqual(classes[0], "NORMAL")
                self.assertIn("WARNING", classes,
                              f"{storm} never warned despite sustained 80 mm/hr")
                # every other city stayed exactly on the quiet floor while that one climbed
                for neighbour in payload["zones"]:
                    if neighbour["zone_id"] == storm:
                        continue
                    self.assertAlmostEqual(neighbour["latest"]["readings"]["drain_level_m"],
                                           calm_target, places=6, msg=neighbour["zone_id"])

    def test_a_second_refresh_inside_the_window_creates_no_second_incident(self):
        zones(self.store, client=client_for(LIVE_ZONE_DEFINITIONS, rain=60.0, soil=0.48))
        status, first = incidents(self.store, limit=50)
        self.assertEqual(status, 200)
        self.assertEqual(first["open_count"], 15)
        self.assertEqual(len(first["incidents"]), 15)

        # the poll that follows sees no new provider data and must re-ingest nothing
        payload = zones(self.store, now_epoch=NOW + 5, client=None)
        self.assertFalse(payload["weather"]["last_refresh"]["attempted"])
        status, second = incidents(self.store, now_epoch=NOW + 5, limit=50)
        self.assertEqual(second["count"], first["count"])
        self.assertEqual(second["open_count"], 15)

    def test_each_city_alerts_with_its_own_name_and_state(self):
        notifier = _RecordingNotifier()
        client = client_for(LIVE_ZONE_DEFINITIONS, rain=60.0, soil=0.48)
        status, payload = dispatch("GET", "/zones", store=self.store, now_epoch=NOW,
                                   env=dict(LIVE_ENV), notifier=notifier, cooldown_seconds=600,
                                   weather_client=client)
        self.assertEqual(status, 200)
        self.assertEqual(payload["zone_count"], 15)
        subjects = [entry["subject"] for entry in notifier.published]
        self.assertEqual(len(subjects), 15, subjects)
        for name, state, _region in EXPECTED_CITIES.values():
            self.assertTrue(any(f"{name}, {state}" in subject for subject in subjects),
                            f"no alert named {name}, {state}")
        severities = {subject.split(":")[0].split()[-1] for subject in subjects}
        self.assertTrue(severities <= {"WARNING", "CRITICAL"}, severities)

        # every message body carries the same geography as its subject
        for entry in notifier.published:
            body = json.loads(entry["message"])
            zone_id = body["zone"]["zone_id"]
            name, state, region = EXPECTED_CITIES[zone_id]
            self.assertEqual(body["zone"]["name"], name)
            self.assertEqual(body["zone"]["state"], state)
            self.assertEqual(body["zone"]["region"], region)
            self.assertEqual(body["zone"]["display_name"], f"{name}, {state}")
            self.assertIn(f"{name}, {state}", entry["subject"])


class _RecordingNotifier:
    """Minimal notifier: records subjects so the alert text itself can be asserted."""

    def __init__(self):
        self.published: list[dict[str, str]] = []

    @property
    def enabled(self) -> bool:
        return True

    def publish(self, subject: str, message: str) -> dict[str, object]:
        self.published.append({"subject": subject, "message": message})
        return {"published": True, "message_id": f"msg-{len(self.published)}"}


# ------------------------------------------------------------ alert text ---


class AlertGeographyTest(unittest.TestCase):
    """An alert identifies the city and its state, and never invents one."""

    def test_the_subject_names_city_and_state(self):
        subject = notify.alert_subject(notify.ALERT_KIND_OPENED, "Patna, Bihar", "WARNING",
                                       simulated=False)
        self.assertIn("Patna, Bihar", subject)
        self.assertIn("WARNING", subject)
        self.assertLessEqual(len(subject), notify.SUBJECT_MAX_LENGTH)

    def test_the_message_body_carries_the_geography(self):
        incident = {
            "zone_id": "city-patna", "zone_name": "Patna", "severity": "WARNING",
            "current_status": "OPEN", "opened_at": "2026-10-15T18:15:00Z",
            "data_classification": "mixed", "model": "heuristic-v1",
        }
        body = json.loads(notify.build_alert_message(notify.ALERT_KIND_OPENED, incident))
        self.assertEqual(body["zone"], {
            "zone_id": "city-patna", "name": "Patna", "state": "Bihar",
            "region": "East", "display_name": "Patna, Bihar",
        })

    def test_a_record_without_stored_labels_is_labelled_from_the_city_table(self):
        geography = notify.zone_geography({"zone_id": "city-guwahati", "zone_name": "Guwahati"})
        self.assertEqual(geography["state"], "Assam")
        self.assertEqual(geography["region"], "Northeast")
        self.assertEqual(geography["display_name"], "Guwahati, Assam")

    def test_an_unknown_zone_id_gets_no_geography_and_no_crash(self):
        geography = notify.zone_geography({"zone_id": "city-atlantis", "zone_name": "Atlantis"})
        self.assertEqual(geography["display_name"], "Atlantis")
        self.assertIsNone(geography["state"])
        self.assertIsNone(geography["region"])


class TimestampGuardTest(unittest.TestCase):
    """The fleet's clock assumptions stay explicit (no wall clock in these tests)."""

    def test_the_fixture_clock_is_fixed(self):
        self.assertEqual(datetime.fromtimestamp(NOW, tz=timezone.utc).year, 2026)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

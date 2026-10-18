"""Adversarial tests: replay, out-of-order arrival, mode contamination, input abuse.

These are the cases a *hostile or careless* caller produces against a publicly reachable
API — the same reading twice, a reading that arrives late, a reading aimed at a fleet that
is not active, a body that is not JSON, a query parameter designed to be expensive. Each
test states the invariant it protects rather than the implementation that satisfies it.

Everything runs in-process: no AWS, no network, no wall clock (``now_epoch`` is injected).
"""

from __future__ import annotations

import json
import logging
import unittest
from datetime import datetime, timezone

import floodpulse.handler as handler_module
from floodpulse.config import STALE_READING_SECONDS, ZONE_IDS
from floodpulse.handler import dispatch, set_store
from floodpulse.incidents import build_alert_marker
from floodpulse.storage import LocalStore

BASE_EPOCH = int(datetime(2026, 2, 14, 9, 0, 0, tzinfo=timezone.utc).timestamp())

DEMO_ZONE = "zone-riverside-north"
OTHER_DEMO_ZONE = "zone-harbor-east"
LIVE_ZONE = "city-chennai"

CALM = {
    "rainfall_mm_per_hr": 2.0,
    "drain_level_m": 0.5,
    "soil_saturation_pct": 40.0,
    "water_level_m": 1.0,
    "flow_rate_m3_s": 1.5,
}

FLOOD = {
    "rainfall_mm_per_hr": 70.0,
    "drain_level_m": 3.0,
    "soil_saturation_pct": 96.0,
    "water_level_m": 4.2,
    "flow_rate_m3_s": 17.0,
}


def iso(offset_seconds: int) -> str:
    return (
        datetime.fromtimestamp(BASE_EPOCH + offset_seconds, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def body(zone_id: str = DEMO_ZONE, offset_seconds: int = 0, readings: dict | None = None) -> str:
    return json.dumps(
        {
            "schema_version": "1.0",
            "simulated": True,
            "zone_id": zone_id,
            "sensor_node_id": "sim-riverside-north",
            "timestamp": iso(offset_seconds),
            "readings": dict(readings or CALM),
            "meta": {"run_id": "adversarial", "seq": max(0, offset_seconds)},
        }
    )


class AdversarialCase(unittest.TestCase):
    def setUp(self) -> None:
        self.store = LocalStore()
        set_store(self.store)
        logging.disable(logging.CRITICAL)
        # default demo mode; a live deployment sets FLOODPULSE_ZONE_MODE=live_weather
        self.env = {"FLOODPULSE_STORE": "local"}

    def tearDown(self) -> None:
        set_store(None)
        logging.disable(logging.NOTSET)

    def post(self, raw: str, now_epoch: float = BASE_EPOCH, headers=None, env=None):
        return dispatch(
            "POST", "/readings", raw, self.store, now_epoch, None, headers, env=env or self.env
        )

    def get(self, path: str, now_epoch: float = BASE_EPOCH, query=None, env=None):
        return dispatch("GET", path, None, self.store, now_epoch, query, env=env or self.env)

    def latest_ts(self, zone_id: str = DEMO_ZONE) -> int | None:
        latest = self.store.get_latest(zone_id)
        return None if latest is None else int(latest["ts_epoch"])

    def history_count(self, zone_id: str = DEMO_ZONE) -> int:
        return self.store.history_count(zone_id)


# --------------------------------------------------------------------------- #
# 1. Replay: the same observation delivered twice
# --------------------------------------------------------------------------- #


class ReplayTest(AdversarialCase):
    def test_a_replayed_reading_is_reported_as_a_duplicate(self) -> None:
        first_status, first = self.post(body())
        second_status, second = self.post(body())

        self.assertEqual(first_status, 202)
        self.assertEqual(second_status, 202)
        self.assertEqual(first["dedupe"]["outcome"], "stored")
        self.assertEqual(second["dedupe"]["outcome"], "duplicate")
        self.assertEqual(second["dedupe"]["reason"], "this reading is already stored")

    def test_a_replayed_reading_cannot_grow_history_or_the_sequence(self) -> None:
        self.post(body())
        after_first = self.history_count()
        sequence = self.store.get_latest(DEMO_ZONE)["sequence"]

        for _ in range(9):
            self.post(body())

        self.assertEqual(self.history_count(), after_first, "a retry appended a second copy")
        self.assertEqual(self.store.get_latest(DEMO_ZONE)["sequence"], sequence)

    def test_a_replayed_reading_reports_no_stored_state_change(self) -> None:
        self.post(body())
        _status, second = self.post(body())

        self.assertFalse(second["dedupe"]["latest_updated"])
        self.assertFalse(second["dedupe"]["history_written"])
        self.assertIsNone(second["stored"]["latest_sk"], "a duplicate claimed to write LATEST")
        self.assertEqual(second["dedupe"]["history_sk"], "H#2026-02-14T09:00:00Z")

    def test_a_replayed_reading_is_not_judged_for_incidents(self) -> None:
        self.post(body(readings=FLOOD))
        self.post(body(offset_seconds=5, readings=FLOOD))
        incident = self.store.latest_incident(DEMO_ZONE)
        self.assertIsNotNone(incident, "the flood readings should have opened an incident")
        self.assertEqual(incident["current_status"], "OPEN")
        opened_at = incident["opened_at"]

        _status, replay = self.post(body(readings=FLOOD))

        self.assertEqual(replay["incident"]["action"], "none")
        self.assertFalse(replay["incident"]["changed"])
        self.assertFalse(replay["alert"]["evaluated"], "a replayed reading evaluated an alert")
        self.assertEqual(replay["alert"]["reason"], "duplicate_reading")
        self.assertEqual(self.store.latest_incident(DEMO_ZONE)["opened_at"], opened_at)

    def test_two_readings_in_the_same_second_are_both_kept(self) -> None:
        _status, first = self.post(body(readings=CALM))
        _status, second = self.post(body(readings={**CALM, "rainfall_mm_per_hr": 9.0}))

        self.assertEqual(first["dedupe"]["outcome"], "stored")
        self.assertEqual(second["dedupe"]["outcome"], "stored")
        self.assertEqual(first["dedupe"]["history_sk"], "H#2026-02-14T09:00:00Z")
        self.assertEqual(second["dedupe"]["history_sk"], "H#2026-02-14T09:00:00Z#2")
        self.assertEqual(self.history_count(), 2)

    def test_the_same_reading_for_two_zones_is_not_a_duplicate(self) -> None:
        self.post(body(zone_id=DEMO_ZONE))
        _status, other = self.post(body(zone_id=OTHER_DEMO_ZONE))
        self.assertEqual(other["dedupe"]["outcome"], "stored")


# --------------------------------------------------------------------------- #
# 2. Out-of-order arrival: an older reading must not become current state
# --------------------------------------------------------------------------- #


class OutOfOrderTest(AdversarialCase):
    def test_an_older_reading_never_replaces_a_newer_one(self) -> None:
        self.post(body(offset_seconds=0))
        newer = self.post(body(offset_seconds=60))
        self.assertEqual(newer[1]["dedupe"]["outcome"], "stored")

        status, late = self.post(body(offset_seconds=30, readings={**CALM, "water_level_m": 3.9}))

        self.assertEqual(status, 202)
        self.assertEqual(late["dedupe"]["outcome"], "stale")
        self.assertFalse(late["dedupe"]["latest_updated"])
        self.assertIsNone(late["stored"]["latest_sk"])
        self.assertEqual(self.latest_ts(), BASE_EPOCH + 60, "current state moved backwards")

    def test_an_older_reading_is_still_kept_as_history(self) -> None:
        self.post(body(offset_seconds=60))
        before = self.history_count()

        _status, late = self.post(body(offset_seconds=30))

        self.assertTrue(late["dedupe"]["history_written"])
        self.assertEqual(late["dedupe"]["history_sk"], "H#2026-02-14T09:00:30Z")
        self.assertEqual(self.history_count(), before + 1, "a real observation was thrown away")

    def test_an_older_reading_is_not_judged_for_incidents(self) -> None:
        # A flood frame opens the incident, then calm frames close it again. The model
        # smooths over three readings and de-escalates one band per streak, so the number of
        # frames needed is a model constant: drive it until it closes rather than guess.
        for offset in (0, 5, 10):
            self.post(body(offset_seconds=offset, readings=FLOOD))
        self.assertEqual(self.store.latest_incident(DEMO_ZONE)["current_status"], "OPEN")

        offset = 15
        for _ in range(20):
            self.post(body(offset_seconds=offset, readings=CALM))
            offset += 5
            if self.store.latest_incident(DEMO_ZONE)["current_status"] == "CLOSED":
                break
        incident = self.store.latest_incident(DEMO_ZONE)
        self.assertEqual(incident["current_status"], "CLOSED", "the calm frames never closed it")
        opened_at = incident["opened_at"]

        # A late, *different* CRITICAL frame arrives after the incident closed: it is older
        # than the current reading, so it is history only and must not re-open anything.
        _status, late = self.post(body(offset_seconds=12, readings={**FLOOD, "flow_rate_m3_s": 16.5}))

        self.assertEqual(late["dedupe"]["outcome"], "stale")
        self.assertEqual(late["incident"]["action"], "none")
        self.assertFalse(late["alert"]["evaluated"])
        self.assertEqual(late["alert"]["reason"], "stale_reading")
        self.assertEqual(
            self.store.latest_incident(DEMO_ZONE)["opened_at"],
            opened_at,
            "an out-of-order reading produced a second incident",
        )
        self.assertEqual(self.store.latest_incident(DEMO_ZONE)["current_status"], "CLOSED")

    def test_a_stale_arrival_does_not_move_the_reported_reading(self) -> None:
        self.post(body(offset_seconds=30, readings={**CALM, "water_level_m": 2.5}))
        self.post(body(offset_seconds=30, readings={**CALM, "water_level_m": 0.4}))
        self.post(body(offset_seconds=10, readings={**CALM, "water_level_m": 4.1}))

        _status, payload = self.get("/zones")
        riverside = next(zone for zone in payload["zones"] if zone["zone_id"] == DEMO_ZONE)
        self.assertEqual(riverside["latest"]["readings"]["water_level_m"], 0.4)

    def test_equal_timestamps_are_allowed_to_replace_current_state(self) -> None:
        # A same-second re-post is not a stale write: the newest arrival in that second wins.
        self.post(body(offset_seconds=0, readings={**CALM, "water_level_m": 1.0}))
        _status, same_second = self.post(body(offset_seconds=0, readings={**CALM, "water_level_m": 2.0}))
        self.assertEqual(same_second["dedupe"]["latest_updated"], True)


# --------------------------------------------------------------------------- #
# 3. Fleet isolation: a reading aimed at the wrong fleet
# --------------------------------------------------------------------------- #


class ModeIsolationTest(AdversarialCase):
    LIVE_ENV = {"FLOODPULSE_STORE": "local", "FLOODPULSE_ZONE_MODE": "live_weather"}

    def stub_provider(self):
        """Answer for exactly the coordinates asked about, without calling Open-Meteo.

        The live path builds its own client, so this patches the transport that client falls back
        to. Without it these tests depend on the provider answering, which makes them a
        connectivity test rather than a provenance test (measured: http_429 when it throttles).
        """
        import urllib.parse

        from floodpulse import weather as weather_module

        def transport(url, timeout):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            latitudes = query["latitude"][0].split(",")
            longitudes = query["longitude"][0].split(",")
            payload = [
                {
                    "latitude": float(latitude),
                    "longitude": float(longitude),
                    "current": {"time": "2026-10-15T18:15", "precipitation": 12.5, "rain": 12.5},
                    "hourly": {
                        "time": ["2026-10-15T17:00", "2026-10-15T18:00"],
                        "precipitation": [0.0, 12.5],
                        "soil_moisture_0_to_7cm": [0.21, 0.24],
                    },
                }
                for latitude, longitude in zip(latitudes, longitudes)
            ]
            return 200, json.dumps(payload).encode("utf-8")

        original = weather_module.default_transport
        weather_module.default_transport = transport
        self.addCleanup(lambda: setattr(weather_module, "default_transport", original))

    def test_a_demo_reading_is_refused_while_the_live_fleet_is_served(self) -> None:
        status, payload = self.post(body(zone_id=DEMO_ZONE), env=self.LIVE_ENV)

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_payload")
        self.assertEqual(payload["field"], "zone_id")
        self.assertEqual(self.store.item_count(), 0, "a rejected reading was stored anyway")

    def test_a_posted_reading_can_never_be_served_as_real(self) -> None:
        """The strongest form of the isolation claim: provenance follows the value.

        A live-weather fleet labels rainfall REAL because its rainfall comes from the
        provider. A reading a client posted for the same city carries no REAL tag of its own,
        so once it is the newest reading, neither the card nor the window may show one.
        """
        from floodpulse.handler import dispatch as dispatch_fn

        self.stub_provider()
        # fill the provider cache first, so the posted reading stays the newest one
        dispatch_fn("GET", "/zones", None, self.store, BASE_EPOCH, {}, None, env=self.LIVE_ENV)
        status, _posted = self.post(
            body(zone_id=LIVE_ZONE, offset_seconds=1, readings={**CALM, "rainfall_mm_per_hr": 99.0}),
            now_epoch=BASE_EPOCH + 1,
            env=self.LIVE_ENV,
        )
        self.assertEqual(status, 202)

        _status, view = self.get("/zones", now_epoch=BASE_EPOCH + 1, env=self.LIVE_ENV)
        city = next(zone for zone in view["zones"] if zone["zone_id"] == LIVE_ZONE)
        self.assertEqual(city["latest"]["readings"]["rainfall_mm_per_hr"], 99.0)
        self.assertEqual(city["latest"]["data_classification"], "simulated")
        self.assertNotIn(
            "REAL",
            set(city["metric_sources"].values()),
            "the zone borrowed a REAL label for a posted value",
        )
        self.assertNotIn("REAL", set(city["latest"]["metric_sources"].values()))

        _status, history = self.get(
            f"/zones/{LIVE_ZONE}/history",
            now_epoch=BASE_EPOCH + 1,
            query={"minutes": 60},
            env=self.LIVE_ENV,
        )
        # The window holds both the provider's record and the posted one, and it says so.
        self.assertEqual(history["data_classification"], "mixed")
        # Per metric, the window is labelled by its least-trustworthy record: a window that
        # contains a fabricated point may not be summarised as REAL rainfall.
        self.assertNotIn("REAL", set(history["metric_sources"].values()))
        self.assertEqual(history["metric_sources"]["rainfall_mm_per_hr"], "SIMULATED")

    def test_a_provider_reading_is_still_labelled_real(self) -> None:
        """The rule above must not be a blanket downgrade: real data keeps its REAL tag."""
        from floodpulse.handler import dispatch as dispatch_fn

        self.stub_provider()
        dispatch_fn("GET", "/zones", None, self.store, BASE_EPOCH, {}, None, env=self.LIVE_ENV)
        _status, view = self.get("/zones", now_epoch=BASE_EPOCH + 1, env=self.LIVE_ENV)
        city = next(zone for zone in view["zones"] if zone["zone_id"] == LIVE_ZONE)
        self.assertEqual(city["latest"]["metric_sources"]["rainfall_mm_per_hr"], "REAL")
        self.assertEqual(city["metric_sources"]["rainfall_mm_per_hr"], "REAL")
        self.assertEqual(city["weather"]["metric_sources"]["rainfall_mm_per_hr"], "REAL")

        _status, history = self.get(
            f"/zones/{LIVE_ZONE}/history",
            now_epoch=BASE_EPOCH + 1,
            query={"minutes": 60},
            env=self.LIVE_ENV,
        )
        self.assertEqual(history["data_classification"], "mixed")
        self.assertEqual(history["metric_sources"]["rainfall_mm_per_hr"], "REAL")
        self.assertEqual(history["metric_sources"]["soil_saturation_pct"], "MODEL_DERIVED")

    def test_a_zone_with_no_reading_keeps_the_fleets_planned_tags(self) -> None:
        from floodpulse.config import zone_definition
        from floodpulse.handler import _zone_view

        view = _zone_view(zone_definition(LIVE_ZONE), None, BASE_EPOCH, mode="live_weather")
        self.assertIsNone(view["latest"])
        self.assertEqual(view["status"], "no_data")
        self.assertEqual(view["metric_sources"]["rainfall_mm_per_hr"], "REAL")

    def test_client_ingestion_stays_open_in_live_mode_for_live_cities(self) -> None:
        # Documented, tested behaviour: a live fleet can hold incidents opened by a posted
        # reading. Isolation is delivered by provenance, not by closing the route.
        status, payload = self.post(body(zone_id=LIVE_ZONE, readings=FLOOD), env=self.LIVE_ENV)
        self.assertEqual(status, 202)
        self.assertEqual(payload["data_classification"], "simulated")
        stored = self.store.get_latest(LIVE_ZONE)
        self.assertTrue(stored["simulated"])
        self.assertEqual(stored["data_classification"], "simulated")

    def test_the_zone_allowlist_still_refuses_a_foreign_zone_directly(self) -> None:
        # Defence in depth behind the fleet separation: the validator itself only accepts
        # zone ids of the fleet it was given.
        from floodpulse.validation import ValidationError, validate_reading

        with self.assertRaises(ValidationError) as caught:
            validate_reading(
                json.loads(body(zone_id="city-chennai")),
                BASE_EPOCH,
                ZONE_IDS,
            )
        self.assertEqual(caught.exception.field, "zone_id")

    def test_a_live_reading_is_refused_while_the_demo_fleet_is_served(self) -> None:
        status, payload = self.post(body(zone_id=LIVE_ZONE))

        self.assertEqual(status, 400)
        self.assertEqual(payload["field"], "zone_id")
        self.assertEqual(self.store.item_count(), 0)

    def test_switching_fleets_never_serves_the_other_fleets_readings(self) -> None:
        # A demo fleet and a live fleet both have stored readings in the same table.
        self.post(body(zone_id=DEMO_ZONE, readings=FLOOD, offset_seconds=0))
        live_reading = handler_module._ingest_record(
            self.store,
            {
                "schema_version": "1.0",
                "simulated": False,
                "data_classification": "mixed",
                "zone_id": LIVE_ZONE,
                "sensor_node_id": "open-meteo-chennai",
                "timestamp": iso(0),
                "ts_epoch": BASE_EPOCH,
                "ttl": BASE_EPOCH + 604800,
                "readings": dict(FLOOD),
                "meta": {},
                "ingested_at_epoch": BASE_EPOCH,
                "source": "open-meteo-live",
            },
            BASE_EPOCH,
            None,
            0,
        )
        self.assertTrue(live_reading.outcome.latest_updated)

        _status, live_view = self.get("/zones", env=self.LIVE_ENV)
        _status, demo_view = self.get("/zones")

        live_ids = [zone["zone_id"] for zone in live_view["zones"]]
        demo_ids = [zone["zone_id"] for zone in demo_view["zones"]]
        self.assertIn(LIVE_ZONE, live_ids)
        self.assertNotIn(DEMO_ZONE, live_ids, "a demo zone leaked into the live fleet view")
        self.assertIn(DEMO_ZONE, demo_ids)
        self.assertNotIn(LIVE_ZONE, demo_ids, "a live city leaked into the demo fleet view")
        self.assertEqual(live_view["zone_mode"], "live_weather")
        self.assertEqual(demo_view["zone_mode"], "simulated")

    def test_provenance_follows_the_record_not_the_fleet(self) -> None:
        # A live-fleet incident keeps its "mixed" provenance even when the very same table is
        # later served as the demo fleet, and a demo incident is never reported as anything
        # but simulated.
        self.post(body(zone_id=DEMO_ZONE, readings=FLOOD, offset_seconds=0))
        self.post(body(zone_id=DEMO_ZONE, readings=FLOOD, offset_seconds=5))
        incident = self.store.latest_incident(DEMO_ZONE)
        self.assertEqual(incident["data_classification"], "simulated")

        _status, incidents = self.get("/incidents")
        self.assertEqual(incidents["data_classification"], "simulated")
        self.assertTrue(all(item["data_classification"] == "simulated" for item in incidents["incidents"]))

    def test_the_demo_fleet_ids_are_the_simulators_own_list(self) -> None:
        # The simulator validates against ZONE_IDS; the demo route must accept exactly those.
        for zone_id in ZONE_IDS:
            status, payload = self.post(body(zone_id=zone_id))
            self.assertEqual(status, 202, f"{zone_id}: {payload}")


# --------------------------------------------------------------------------- #
# 4. Input abuse: the request must be bounded before it is expensive
# --------------------------------------------------------------------------- #


class InputAbuseTest(AdversarialCase):
    def test_a_non_json_content_type_is_refused_before_parsing(self) -> None:
        status, payload = self.post(
            "zone_id=zone-riverside-north&rain=99",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )

        self.assertEqual(status, 415)
        self.assertEqual(payload["error"], "unsupported_media_type")
        self.assertEqual(self.store.item_count(), 0)

    def test_json_content_types_are_accepted_with_parameters_and_suffixes(self) -> None:
        accepted = [
            None,
            "application/json",
            "application/json; charset=utf-8",
            "APPLICATION/JSON",
            "application/vnd.floodpulse.reading+json",
        ]
        for index, content_type in enumerate(accepted):
            headers = {} if content_type is None else {"content-type": content_type}
            status, payload = self.post(body(offset_seconds=index * 10), headers=headers)
            self.assertEqual(status, 202, f"content-type {content_type!r} was refused: {payload}")

    def test_a_declared_non_json_type_does_not_leak_back_into_the_response(self) -> None:
        _status, payload = self.post("x", headers={"content-type": "<script>alert(1)</script>"})
        self.assertNotIn("<script>", json.dumps(payload))

    def test_huge_and_nonsensical_query_values_are_rejected(self) -> None:
        for query in (
            {"minutes": "100000000"},
            {"minutes": "0"},
            {"minutes": "-5"},
            {"minutes": "abc"},
            {"minutes": "1e9"},
            {"limit": "100000"},
            {"limit": "0"},
            {"limit": "-1"},
            {"limit": "all"},
        ):
            path = f"/zones/{DEMO_ZONE}/history" if "minutes" in query else "/incidents"
            status, payload = self.get(path, query=query)
            self.assertEqual(status, 400, f"{query} was not rejected: {payload}")
            self.assertEqual(payload["error"], "invalid_query")

    def test_repeated_query_parameters_are_rejected_not_guessed(self) -> None:
        # API Gateway hands a repeated parameter over as a list; that is not an integer.
        status, _payload = self.get(f"/zones/{DEMO_ZONE}/history", query={"minutes": ["10", "600"]})
        self.assertEqual(status, 400)

    def test_boolean_valued_query_parameters_are_rejected(self) -> None:
        # A bare ``?minutes`` arrives as an empty string and ``True`` is never an integer.
        status, _payload = self.get(f"/zones/{DEMO_ZONE}/history", query={"minutes": True})
        self.assertEqual(status, 400)
        status, _payload = self.get(f"/zones/{DEMO_ZONE}/history", query={"minutes": ""})
        self.assertEqual(status, 400)

    def test_empty_malformed_and_null_bodies_are_rejected_without_a_crash(self) -> None:
        for raw, expected in (
            ("", "invalid_body"),
            ("   ", "invalid_body"),
            ("null", "invalid_payload"),
            ("[]", "invalid_payload"),
            ('{"schema_version": "1.0"}', "invalid_payload"),
            ("{", "malformed_json"),
        ):
            status, payload = self.post(raw)
            self.assertEqual(status, 400, f"{raw!r} was not rejected: {payload}")
            self.assertEqual(payload["error"], expected, f"{raw!r}")

    def test_deeply_nested_json_is_rejected_without_recursion(self) -> None:
        nested = "[" * 500 + "]" * 500
        raw = json.dumps({"schema_version": "1.0", "simulated": True, "extra": nested})
        status, payload = self.post(raw)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_payload")

    def test_a_body_that_is_one_huge_string_is_rejected_on_size(self) -> None:
        status, payload = self.post(json.dumps({"note": "A" * (4096 + 1)}))
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_body")

    def test_unicode_and_encoded_zone_ids_are_not_found(self) -> None:
        for zone_id in ("%2e%2e", "..", "city-chennai", "ZONE-RIVERSIDE-NORTH", "caf%C3%A9", "a" * 500):
            status, payload = self.get(f"/zones/{zone_id}/history")
            self.assertIn(status, (400, 404), f"{zone_id!r} returned {status}")
            self.assertNotEqual(status, 500, f"{zone_id!r} crashed the handler")
            self.assertIn(payload["error"], ("unknown_zone", "not_found"))

    def test_a_zone_id_with_a_slash_cannot_reach_another_route(self) -> None:
        status, payload = self.get("/zones/zone-riverside-north/../harbor-east/history")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "not_found")

    def test_unknown_methods_are_rejected_on_every_route(self) -> None:
        for method, path in (
            ("PUT", "/readings"),
            ("DELETE", "/readings"),
            ("POST", "/zones"),
            ("PATCH", "/incidents"),
            ("POST", f"/zones/{DEMO_ZONE}/history"),
            ("GET", "/readings"),
        ):
            status, payload = dispatch(method, path, None, self.store, BASE_EPOCH, None, None, env=self.env)
            self.assertEqual(status, 405, f"{method} {path} -> {status}")
            self.assertEqual(payload["error"], "method_not_allowed")
            self.assertIn("allowed", payload)

    def test_a_reading_far_in_the_future_is_rejected(self) -> None:
        status, payload = self.post(body(offset_seconds=STALE_READING_SECONDS * 10))
        self.assertEqual(status, 400)
        self.assertEqual(payload["field"], "timestamp")

    def test_a_reading_far_in_the_past_is_rejected(self) -> None:
        status, payload = self.post(body(offset_seconds=-STALE_READING_SECONDS * 10))
        self.assertEqual(status, 400)
        self.assertEqual(payload["field"], "timestamp")

    def test_rejected_readings_leave_no_trace_in_storage(self) -> None:
        for raw in ("", "null", "{}", '{"zone_id": "zone-atlantis"}'):
            self.post(raw)
        self.assertEqual(self.store.item_count(), 0)


# --------------------------------------------------------------------------- #
# 5. Alert storms: overlapping escalations must publish once
# --------------------------------------------------------------------------- #


class AlertStormTest(AdversarialCase):
    def test_only_one_caller_can_claim_an_alert_slot(self) -> None:
        marker = build_alert_marker(zone_id=DEMO_ZONE, severity="WARNING", now_epoch=BASE_EPOCH)
        self.assertTrue(
            self.store.claim_alert_slot(
                marker, severity="WARNING", now_epoch=BASE_EPOCH, cooldown_seconds=600
            )
        )
        # A second caller that read the marker before the first one wrote it.
        second = build_alert_marker(zone_id=DEMO_ZONE, severity="WARNING", now_epoch=BASE_EPOCH)
        self.assertFalse(
            self.store.claim_alert_slot(
                second, severity="WARNING", now_epoch=BASE_EPOCH + 1, cooldown_seconds=600
            ),
            "two overlapping escalations could both have alerted",
        )

    def test_a_different_severity_has_its_own_slot(self) -> None:
        warning = build_alert_marker(zone_id=DEMO_ZONE, severity="WARNING", now_epoch=BASE_EPOCH)
        self.store.claim_alert_slot(warning, severity="WARNING", now_epoch=BASE_EPOCH, cooldown_seconds=600)
        critical = build_alert_marker(zone_id=DEMO_ZONE, severity="CRITICAL", now_epoch=BASE_EPOCH + 1)
        self.assertTrue(
            self.store.claim_alert_slot(
                critical, severity="CRITICAL", now_epoch=BASE_EPOCH + 1, cooldown_seconds=600
            ),
            "escalating to CRITICAL must not be blocked by the WARNING cooldown",
        )

    def test_the_slot_is_free_again_after_the_cooldown(self) -> None:
        marker = build_alert_marker(zone_id=DEMO_ZONE, severity="WARNING", now_epoch=BASE_EPOCH)
        self.store.claim_alert_slot(marker, severity="WARNING", now_epoch=BASE_EPOCH, cooldown_seconds=600)
        later = build_alert_marker(zone_id=DEMO_ZONE, severity="WARNING", now_epoch=BASE_EPOCH + 601)
        self.assertTrue(
            self.store.claim_alert_slot(
                later, severity="WARNING", now_epoch=BASE_EPOCH + 601, cooldown_seconds=600
            )
        )

    def test_a_zero_cooldown_means_alert_every_time_by_configuration(self) -> None:
        marker = build_alert_marker(zone_id=DEMO_ZONE, severity="WARNING", now_epoch=BASE_EPOCH)
        self.store.claim_alert_slot(marker, severity="WARNING", now_epoch=BASE_EPOCH, cooldown_seconds=0)
        again = build_alert_marker(zone_id=DEMO_ZONE, severity="WARNING", now_epoch=BASE_EPOCH + 1)
        self.assertTrue(
            self.store.claim_alert_slot(
                again, severity="WARNING", now_epoch=BASE_EPOCH + 1, cooldown_seconds=0
            )
        )

    def test_the_reported_cooldown_and_the_claim_agree(self) -> None:
        from floodpulse.incidents import cooldown_remaining_seconds

        marker = build_alert_marker(zone_id=DEMO_ZONE, severity="WARNING", now_epoch=BASE_EPOCH)
        self.store.claim_alert_slot(marker, severity="WARNING", now_epoch=BASE_EPOCH, cooldown_seconds=600)
        stored = self.store.get_alert_marker(DEMO_ZONE)
        for elapsed in (0, 1, 300, 599, 600, 601):
            remaining = cooldown_remaining_seconds(stored, "WARNING", BASE_EPOCH + elapsed, 600)
            fresh = build_alert_marker(zone_id=DEMO_ZONE, severity="WARNING", now_epoch=0)
            claimed = self.store.claim_alert_slot(
                fresh, severity="WARNING", now_epoch=BASE_EPOCH + elapsed, cooldown_seconds=600
            )
            self.assertEqual(
                claimed,
                remaining <= 0,
                f"at +{elapsed}s the claim said {claimed} but the report said {remaining}s left",
            )


if __name__ == "__main__":
    unittest.main()

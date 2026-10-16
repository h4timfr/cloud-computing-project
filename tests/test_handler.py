"""Unit tests for the single Lambda handler and its routes.

Everything runs in-process against an in-memory store: no AWS calls, no network,
no wall-clock dependency (``now_epoch`` is always injected).
"""

from __future__ import annotations

import base64
import json
import logging
import unittest
from datetime import datetime, timedelta, timezone

import floodpulse.handler as handler_module
from floodpulse.config import DEFERRED_FEATURES, METRIC_ORDER, ZONE_DEFINITIONS, ZONE_IDS
from floodpulse.handler import dispatch, lambda_handler, set_store
from floodpulse.risk import class_rank
from floodpulse.storage import LATEST_SORT_KEY, LocalStore

BASE_EPOCH = int(datetime(2026, 2, 14, 9, 0, 0, tzinfo=timezone.utc).timestamp())

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
    return datetime.fromtimestamp(BASE_EPOCH + offset_seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def payload(
    zone_id: str = "zone-riverside-north",
    offset_seconds: int = 0,
    readings: dict | None = None,
    sensor_node_id: str = "sim-riverside-north",
) -> dict:
    return {
        "schema_version": "1.0",
        "simulated": True,
        "zone_id": zone_id,
        "sensor_node_id": sensor_node_id,
        "timestamp": iso(offset_seconds),
        "readings": dict(readings or CALM),
        "meta": {"run_id": "test-run", "seq": max(0, offset_seconds)},
    }


def payload_now(readings: dict | None = None) -> dict:
    """Payload stamped with the real current second.

    Needed only by the adapter tests, where ``lambda_handler`` reads the wall clock.
    """
    body = payload(readings=readings)
    body["timestamp"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return body


class HandlerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.store = LocalStore()
        set_store(self.store)
        logging.disable(logging.CRITICAL)

    def tearDown(self) -> None:
        set_store(None)
        logging.disable(logging.NOTSET)

    def post(self, body, now_epoch: int = BASE_EPOCH):
        return dispatch("POST", "/readings", body, self.store, now_epoch)

    def post_json(self, body_payload: dict, now_epoch: int | None = None):
        ts = body_payload.get("timestamp")
        epoch = now_epoch
        if epoch is None:
            epoch = int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
        return self.post(json.dumps(body_payload), epoch)

    def get(self, path: str, now_epoch: int = BASE_EPOCH, query: dict | None = None):
        return dispatch("GET", path, None, self.store, now_epoch, query)


class ReadingEndpointTests(HandlerTestCase):
    def test_valid_reading_is_accepted_and_stored(self) -> None:
        status, body = self.post_json(payload())
        self.assertEqual(status, 202)
        self.assertEqual(body["status"], "accepted")
        self.assertEqual(body["zone_id"], "zone-riverside-north")
        self.assertEqual(body["sequence"], 1)
        self.assertTrue(body["simulated"])
        self.assertEqual(body["data_classification"], "simulated")
        self.assertEqual(body["risk"]["risk_class"], "NORMAL")
        self.assertEqual(body["stored"]["latest_sk"], LATEST_SORT_KEY)
        self.assertTrue(body["stored"]["history_sk"].startswith("H#"))
        self.assertEqual(body["stored"]["history_records"], 1)
        self.assertIn("time_to_threshold", body["deferred_features"])
        self.assertIn("not a calibrated hydrological model", body["model_note"])
        self.assertEqual(self.store.item_count(), 2)

    def test_second_reading_replaces_latest_and_appends_history(self) -> None:
        self.post_json(payload(offset_seconds=0))
        status, body = self.post_json(payload(offset_seconds=5, readings=FLOOD))
        self.assertEqual(status, 202)
        self.assertEqual(body["sequence"], 2)
        self.assertEqual(body["stored"]["history_records"], 2)
        self.assertEqual(self.store.item_count(), 3)

    def test_duplicate_timestamp_gets_a_distinct_history_key(self) -> None:
        _status, first = self.post_json(payload(offset_seconds=0))
        _status, second = self.post_json(payload(offset_seconds=0, readings=FLOOD))
        self.assertNotEqual(first["stored"]["history_sk"], second["stored"]["history_sk"])
        self.assertEqual(self.store.history_count("zone-riverside-north"), 2)

    def test_smoothing_uses_the_last_three_readings(self) -> None:
        self.post_json(payload(offset_seconds=0, readings=dict(CALM, rainfall_mm_per_hr=3.0)))
        self.post_json(payload(offset_seconds=5, readings=dict(CALM, rainfall_mm_per_hr=6.0)))
        _status, body = self.post_json(payload(offset_seconds=10, readings=dict(CALM, rainfall_mm_per_hr=9.0)))
        self.assertEqual(body["sequence"], 3)
        latest = self.store.get_latest("zone-riverside-north")
        self.assertEqual(latest["smoothed_readings"]["rainfall_mm_per_hr"], 6.0)

    def test_single_reading_is_not_smoothed_down(self) -> None:
        self.post_json(payload(offset_seconds=0))
        latest = self.store.get_latest("zone-riverside-north")
        for metric in METRIC_ORDER:
            with self.subTest(metric=metric):
                self.assertEqual(latest["smoothed_readings"][metric], CALM[metric])

    def test_trend_uses_the_five_minute_lookback_reading(self) -> None:
        self.post_json(payload(offset_seconds=0, readings=dict(CALM, water_level_m=1.0)))
        _status, body = self.post_json(
            payload(offset_seconds=300, readings=dict(CALM, water_level_m=2.0))
        )
        self.assertEqual(body["risk"]["trend"]["water_level_delta_m"], 1.0)
        self.assertEqual(body["risk"]["trend"]["rise_fraction"], 1.0)
        self.assertEqual(body["risk"]["trend"]["contribution"], 0.1)

    def test_trend_is_null_without_a_lookback_reading(self) -> None:
        _status, body = self.post_json(payload(offset_seconds=0))
        self.assertIsNone(body["risk"]["trend"]["water_level_delta_m"])
        self.assertIsNone(body["risk"]["trend"]["rise_fraction"])
        self.assertEqual(body["risk"]["trend"]["contribution"], 0.0)

    def test_hysteresis_defers_a_jump_from_normal_to_critical(self) -> None:
        _status, calm = self.post_json(payload(offset_seconds=0, readings=CALM))
        self.assertEqual(calm["risk"]["risk_class"], "NORMAL")
        self.assertEqual(calm["hysteresis"]["transition"], "initial")

        # Keep feeding a flood-level reading: the effective class must not move on
        # the first higher reading (candidate band changes also restart the streak).
        promoted_at: int | None = None
        promoted_body: dict | None = None
        for index in range(1, 8):
            _status, body = self.post_json(payload(offset_seconds=5 * index, readings=FLOOD))
            self.assertGreater(
                class_rank(body["risk"]["risk_class_candidate"]), class_rank("NORMAL")
            )
            if body["risk"]["risk_class"] != "NORMAL":
                promoted_at, promoted_body = index, body
                break
            self.assertEqual(body["hysteresis"]["transition"], "pending")
            self.assertEqual(body["risk"]["risk_class"], "NORMAL")

        self.assertIsNotNone(promoted_at, "the effective class never escalated")
        self.assertGreaterEqual(promoted_at, 2, "escalated on the first higher reading")
        self.assertEqual(promoted_body["hysteresis"]["transition"], "escalate")
        self.assertNotEqual(promoted_body["risk"]["risk_class"], "NORMAL")

    def test_explanation_is_returned_to_the_caller(self) -> None:
        _status, body = self.post_json(payload(readings=FLOOD))
        self.assertIn("dominant driver", body["risk"]["explanation"])
        self.assertEqual(len(body["risk"]["top_drivers"]), 3)

    def test_invalid_payload_is_rejected_with_a_field(self) -> None:
        bad = payload()
        bad["readings"]["rainfall_mm_per_hr"] = 999.0
        status, body = self.post_json(bad)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_payload")
        self.assertEqual(body["field"], "readings.rainfall_mm_per_hr")
        self.assertEqual(self.store.item_count(), 0)

    def test_unknown_zone_is_rejected(self) -> None:
        status, body = self.post_json(payload(zone_id="zone-atlantis"))
        self.assertEqual(status, 400)
        self.assertEqual(body["field"], "zone_id")

    def test_malformed_json_is_rejected(self) -> None:
        status, body = self.post("{not json", BASE_EPOCH)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "malformed_json")

    def test_missing_and_oversized_bodies_are_rejected(self) -> None:
        status, body = self.post(None, BASE_EPOCH)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_body")

        status, body = self.post("x" * 5000, BASE_EPOCH)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_body")

    def test_wrong_method_on_readings(self) -> None:
        status, body = self.get("/readings")
        self.assertEqual(status, 405)
        self.assertEqual(body["allowed"], ["POST"])


class ZonesEndpointTests(HandlerTestCase):
    def test_empty_system_reports_three_zones_with_no_data(self) -> None:
        status, body = self.get("/zones")
        self.assertEqual(status, 200)
        self.assertEqual(body["zone_count"], 3)
        self.assertEqual([zone["zone_id"] for zone in body["zones"]], list(ZONE_IDS))
        self.assertTrue(all(zone["status"] == "no_data" for zone in body["zones"]))
        self.assertTrue(all(zone["latest"] is None for zone in body["zones"]))
        self.assertIsNone(body["highest_risk_zone"])
        self.assertEqual(body["zone_ranking"], list(ZONE_IDS))
        self.assertIn("simulated", body["notes"][0].lower())
        self.assertEqual(body["deferred_features"], list(DEFERRED_FEATURES))

    def test_latest_state_and_ranking(self) -> None:
        self.post_json(payload(zone_id="zone-riverside-north", offset_seconds=0, readings=CALM))
        self.post_json(payload(zone_id="zone-old-town-canal", offset_seconds=0, readings=FLOOD))

        status, body = self.get("/zones", now_epoch=BASE_EPOCH + 30)
        self.assertEqual(status, 200)
        self.assertEqual(body["highest_risk_zone"]["zone_id"], "zone-old-town-canal")
        self.assertEqual(body["zone_ranking"][0], "zone-old-town-canal")
        self.assertEqual(body["zone_ranking"][-1], "zone-harbor-east")

        by_id = {zone["zone_id"]: zone for zone in body["zones"]}
        canal = by_id["zone-old-town-canal"]
        self.assertEqual(canal["status"], "live")
        self.assertEqual(canal["latest"]["risk_class"], "CRITICAL")
        self.assertEqual(canal["latest"]["age_seconds"], 30)
        self.assertEqual(len(canal["latest"]["contributions"]), len(METRIC_ORDER))
        self.assertIn("dominant driver", canal["latest"]["explanation"])
        self.assertEqual(by_id["zone-harbor-east"]["status"], "no_data")

    def test_each_zone_reports_the_thresholds_used(self) -> None:
        _status, body = self.get("/zones")
        by_id = {zone["zone_id"]: zone for zone in body["zones"]}
        self.assertEqual(by_id["zone-old-town-canal"]["thresholds"]["water_level_m"], {"watch": 1.8, "critical": 3.2})
        self.assertEqual(by_id["zone-riverside-north"]["thresholds"]["water_level_m"], {"watch": 2.0, "critical": 3.5})

    def test_wrong_method_on_zones(self) -> None:
        status, _body = dispatch("POST", "/zones", "{}", self.store, BASE_EPOCH)
        self.assertEqual(status, 405)


class HistoryEndpointTests(HandlerTestCase):
    def test_history_returns_points_ascending(self) -> None:
        for index, offset in enumerate((0, 60, 120)):
            self.post_json(
                payload(offset_seconds=offset, readings=dict(CALM, rainfall_mm_per_hr=float(index)))
            )
        status, body = self.get(
            "/zones/zone-riverside-north/history", now_epoch=BASE_EPOCH + 120, query={"minutes": "60"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["zone_id"], "zone-riverside-north")
        self.assertEqual(body["point_count"], 3)
        self.assertEqual([point["ts_epoch"] for point in body["points"]], [BASE_EPOCH, BASE_EPOCH + 60, BASE_EPOCH + 120])
        self.assertEqual(body["points"][0]["risk_class"], "NORMAL")
        self.assertFalse(body["downsampled"])

    def test_history_window_excludes_older_points(self) -> None:
        self.post_json(payload(offset_seconds=0))
        _status, body = self.get(
            "/zones/zone-riverside-north/history", now_epoch=BASE_EPOCH + 3600, query={"minutes": "1"}
        )
        self.assertEqual(body["point_count"], 0)

    def test_history_downsampling_caps_the_point_count(self) -> None:
        original = handler_module.MAX_HISTORY_POINTS
        handler_module.MAX_HISTORY_POINTS = 5
        try:
            for offset in range(10):
                self.post_json(payload(offset_seconds=offset))
            status, body = self.get("/zones/zone-riverside-north/history", now_epoch=BASE_EPOCH + 10)
            self.assertEqual(status, 200)
            self.assertEqual(body["point_count"], 5)
            self.assertEqual(body["stride"], 2)
            self.assertTrue(body["downsampled"])
        finally:
            handler_module.MAX_HISTORY_POINTS = original

    def test_default_window_is_sixty_minutes(self) -> None:
        _status, body = self.get("/zones/zone-riverside-north/history")
        self.assertEqual(body["minutes"], 60)

    def test_invalid_minutes_are_rejected(self) -> None:
        for bad in ("abc", "0", "-5", "1441", "1.5"):
            with self.subTest(minutes=bad):
                status, body = self.get(
                    "/zones/zone-riverside-north/history", query={"minutes": bad}
                )
                self.assertEqual(status, 400)
                self.assertEqual(body["error"], "invalid_query")

    def test_unknown_zone_is_404(self) -> None:
        status, body = self.get("/zones/zone-atlantis/history")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "unknown_zone")
        self.assertEqual(body["known_zones"], list(ZONE_IDS))

    def test_wrong_method_on_history(self) -> None:
        status, _body = dispatch(
            "POST", "/zones/zone-riverside-north/history", "{}", self.store, BASE_EPOCH
        )
        self.assertEqual(status, 405)


class RoutingTests(HandlerTestCase):
    def test_trailing_slash_is_tolerated(self) -> None:
        status, _body = self.post_json(payload())
        self.assertEqual(status, 202)
        status, body = self.get("/zones/")
        self.assertEqual(status, 200)
        self.assertEqual(body["zone_count"], 3)

    def test_query_string_in_path_is_stripped(self) -> None:
        status, _body = self.get("/zones?minutes=5")
        self.assertEqual(status, 200)

    def test_unknown_routes(self) -> None:
        for path in ("/", "/health", "/zones/zone-riverside-north", "/zones/a/b/c"):
            with self.subTest(path=path):
                status, body = self.get(path)
                self.assertEqual(status, 404)
                self.assertEqual(body["error"], "not_found")

    def test_all_fixed_zones_are_covered_by_the_definitions(self) -> None:
        self.assertEqual(len(ZONE_DEFINITIONS), 3)
        self.assertEqual(len(set(ZONE_IDS)), 3)


class LambdaHandlerAdapterTests(HandlerTestCase):
    def test_api_gateway_v2_event(self) -> None:
        event = {
            "version": "2.0",
            "rawPath": "/readings",
            "requestContext": {"http": {"method": "POST"}},
            "body": json.dumps(payload_now()),
            "isBase64Encoded": False,
        }
        response = lambda_handler(event, None)
        self.assertEqual(response["statusCode"], 202)
        self.assertEqual(response["headers"]["content-type"], "application/json")
        self.assertEqual(json.loads(response["body"])["status"], "accepted")

    def test_api_gateway_v1_event(self) -> None:
        event = {
            "httpMethod": "GET",
            "path": "/zones",
            "queryStringParameters": None,
            "body": None,
        }
        response = lambda_handler(event, None)
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(json.loads(response["body"])["zone_count"], 3)

    def test_base64_encoded_body(self) -> None:
        event = {
            "version": "2.0",
            "rawPath": "/readings",
            "requestContext": {"http": {"method": "POST"}},
            "body": base64.b64encode(json.dumps(payload_now()).encode("utf-8")).decode("ascii"),
            "isBase64Encoded": True,
        }
        response = lambda_handler(event, None)
        self.assertEqual(response["statusCode"], 202)

    def test_stale_timestamp_is_rejected_by_the_adapter(self) -> None:
        stale = payload_now()
        stale["timestamp"] = (
            (datetime.now(timezone.utc) - timedelta(hours=1))
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        event = {
            "version": "2.0",
            "rawPath": "/readings",
            "requestContext": {"http": {"method": "POST"}},
            "body": json.dumps(stale),
            "isBase64Encoded": False,
        }
        response = lambda_handler(event, None)
        self.assertEqual(response["statusCode"], 400)
        body = json.loads(response["body"])
        self.assertEqual(body["field"], "timestamp")
        self.assertIn("from server time", body["detail"])

    def test_invalid_base64_body_is_a_400(self) -> None:
        event = {
            "version": "2.0",
            "rawPath": "/readings",
            "requestContext": {"http": {"method": "POST"}},
            "body": "!!!not-base64!!!",
            "isBase64Encoded": True,
        }
        response = lambda_handler(event, None)
        self.assertEqual(response["statusCode"], 400)
        self.assertEqual(json.loads(response["body"])["error"], "invalid_body")

    def test_one_unreadable_zone_degrades_that_zone_and_not_the_fleet(self) -> None:
        """A failing per-zone read used to raise out of GET /zones and blank the whole board.

        The honest degradation is the zone that cannot be read, reported as "no data" like a
        zone that has never reported, with no invented values and no leaked internals.
        """
        class ExplodingStore(LocalStore):
            def get_latest(self, zone_id: str):  # type: ignore[override]
                raise RuntimeError("dynamo is on fire")

        set_store(ExplodingStore())
        response = lambda_handler(
            {"version": "2.0", "rawPath": "/zones", "requestContext": {"http": {"method": "GET"}}},
            None,
        )
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(len(body["zones"]), 3)
        for zone in body["zones"]:
            self.assertEqual(zone["status"], "no_data")
            self.assertIsNone(zone["latest"])
        self.assertIsNone(body["highest_risk_zone"])
        self.assertEqual(len(body["zone_ranking"]), 3, "every zone is still listed, in the ranking")
        self.assertNotIn("on fire", response["body"], "a store error must never reach the client")
        self.assertNotIn("Traceback", response["body"])

    def test_a_zone_whose_stored_reading_is_unreadable_reports_no_data(self) -> None:
        """A short or legacy LATEST item must not become a half-rendered reading."""
        class ShortItemStore(LocalStore):
            def get_latest(self, zone_id: str):  # type: ignore[override]
                if zone_id == "zone-old-town-canal":
                    # a shape the schema never wrote: no readings, no risk class, no timestamp
                    return {"pk": "ZONE#zone-old-town-canal", "sk": LATEST_SORT_KEY, "risk_score": 0.9}
                return super().get_latest(zone_id)

        self.store = ShortItemStore()
        set_store(self.store)
        self.assertEqual(self.post_json(payload())[0], 202, "the seeding post must be accepted")
        response = lambda_handler(
            {"version": "2.0", "rawPath": "/zones", "requestContext": {"http": {"method": "GET"}}},
            None,
        )
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        by_id = {zone["zone_id"]: zone for zone in body["zones"]}
        self.assertEqual(by_id["zone-old-town-canal"]["status"], "no_data")
        self.assertIsNone(by_id["zone-old-town-canal"]["latest"],
                          "half an item must never be presented as a reading")
        self.assertEqual(by_id["zone-riverside-north"]["status"], "live",
                         "the readable zone must keep its reading")
        self.assertEqual(body["highest_risk_zone"]["zone_id"], "zone-riverside-north")

    def test_unexpected_store_error_becomes_a_500_without_leaking_details(self) -> None:
        class ExplodingStore(LocalStore):
            def list_incidents(self, *args, **kwargs):  # type: ignore[override]
                raise RuntimeError("dynamo is on fire")

        set_store(ExplodingStore())
        response = lambda_handler(
            {"version": "2.0", "rawPath": "/incidents", "requestContext": {"http": {"method": "GET"}}},
            None,
        )
        self.assertEqual(response["statusCode"], 500)
        body = json.loads(response["body"])
        self.assertEqual(body["error"], "internal_error")
        self.assertNotIn("on fire", response["body"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

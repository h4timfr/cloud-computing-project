"""Unit tests for strict payload validation (no AWS, no clock dependency)."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from floodpulse.config import (
    BODY_MAX_BYTES,
    HISTORY_TTL_SECONDS,
    MAX_CLOCK_SKEW_SECONDS,
    METRIC_ORDER,
    SCHEMA_VERSION,
    ZONE_IDS,
)
from floodpulse.validation import (
    ValidationError,
    parse_utc_timestamp,
    validate_body_size,
    validate_reading,
)

NOW = int(datetime(2026, 2, 14, 10, 15, 30, tzinfo=timezone.utc).timestamp())


def valid_payload(**overrides):
    payload = {
        "schema_version": SCHEMA_VERSION,
        "simulated": True,
        "zone_id": "zone-riverside-north",
        "sensor_node_id": "sim-riverside-north",
        "timestamp": "2026-02-14T10:15:30Z",
        "readings": {
            "rainfall_mm_per_hr": 42.5,
            "drain_level_m": 1.85,
            "soil_saturation_pct": 78.0,
            "water_level_m": 2.4,
            "flow_rate_m3_s": 8.7,
        },
        "meta": {"run_id": "sim-20260214", "seq": 128, "phase": "flash_flood"},
    }
    payload.update(overrides)
    return payload


class ValidPayloadTests(unittest.TestCase):
    def test_accepts_a_well_formed_payload(self) -> None:
        record = validate_reading(valid_payload(), NOW, ZONE_IDS)
        self.assertEqual(record["zone_id"], "zone-riverside-north")
        self.assertEqual(record["sensor_node_id"], "sim-riverside-north")
        self.assertEqual(record["timestamp"], "2026-02-14T10:15:30Z")
        self.assertEqual(record["ts_epoch"], NOW)
        self.assertTrue(record["simulated"])
        self.assertEqual(record["data_classification"], "simulated")
        self.assertEqual(record["ingested_at_epoch"], NOW)
        self.assertEqual(set(record["readings"]), set(METRIC_ORDER))

    def test_derives_ttl_server_side(self) -> None:
        record = validate_reading(valid_payload(), NOW, ZONE_IDS)
        self.assertEqual(record["ttl"], record["ts_epoch"] + HISTORY_TTL_SECONDS)
        self.assertNotIn("ts_epoch", valid_payload())  # client does not send it
        self.assertNotIn("ttl", valid_payload())

    def test_meta_is_optional(self) -> None:
        payload = valid_payload()
        payload.pop("meta")
        record = validate_reading(payload, NOW, ZONE_IDS)
        self.assertEqual(record["meta"], {})

    def test_boundary_values_are_accepted(self) -> None:
        payload = valid_payload(
            readings={
                "rainfall_mm_per_hr": 0.0,
                "drain_level_m": 5.0,
                "soil_saturation_pct": 100.0,
                "water_level_m": 6.0,
                "flow_rate_m3_s": 50.0,
            }
        )
        record = validate_reading(payload, NOW, ZONE_IDS)
        self.assertEqual(record["readings"]["soil_saturation_pct"], 100.0)

    def test_skew_at_the_limit_is_accepted(self) -> None:
        record = validate_reading(
            valid_payload(timestamp="2026-02-14T10:20:30Z"), NOW, ZONE_IDS
        )
        self.assertEqual(record["ts_epoch"], NOW + MAX_CLOCK_SKEW_SECONDS)

    def test_old_but_within_limit_timestamp_is_accepted(self) -> None:
        validate_reading(valid_payload(timestamp="2026-02-14T10:10:30Z"), NOW, ZONE_IDS)


class RejectionTests(unittest.TestCase):
    def assert_rejected(self, payload, field=None, contains=None) -> None:
        with self.assertRaises(ValidationError) as ctx:
            validate_reading(payload, NOW, ZONE_IDS)
        if field is not None:
            self.assertEqual(ctx.exception.field, field)
        if contains is not None:
            self.assertIn(contains, ctx.exception.reason)

    def test_payload_must_be_mapping(self) -> None:
        for bad in ([], "readings", 42, None):
            with self.subTest(bad=bad):
                self.assert_rejected(bad, "payload")

    def test_unknown_top_level_field(self) -> None:
        self.assert_rejected(valid_payload(device_id="old-name"), "payload", "unexpected field")

    def test_device_id_is_no_longer_accepted(self) -> None:
        payload = valid_payload()
        payload["device_id"] = payload.pop("sensor_node_id")
        self.assert_rejected(payload, "payload", "device_id")

    def test_required_fields(self) -> None:
        for field in ("schema_version", "simulated", "zone_id", "sensor_node_id", "timestamp", "readings"):
            payload = valid_payload()
            payload.pop(field)
            with self.subTest(field=field):
                self.assert_rejected(payload, field, "is required")

    def test_schema_version_mismatch(self) -> None:
        self.assert_rejected(valid_payload(schema_version="2.0"), "schema_version")

    def test_simulated_must_be_true(self) -> None:
        self.assert_rejected(valid_payload(simulated=False), "simulated", "simulated data only")
        self.assert_rejected(valid_payload(simulated="yes"), "simulated")

    def test_unknown_zone(self) -> None:
        self.assert_rejected(valid_payload(zone_id="zone-atlantis"), "zone_id", "unknown zone")

    def test_malformed_zone_id(self) -> None:
        self.assert_rejected(valid_payload(zone_id="Zone-Riverside"), "zone_id")

    def test_sensor_node_id_pattern(self) -> None:
        for bad in ("Sim-Node", "node_1", "", "-leading", "x" * 33):
            with self.subTest(bad=bad):
                self.assert_rejected(valid_payload(sensor_node_id=bad), "sensor_node_id")

    def test_timestamp_forms(self) -> None:
        for bad in ("2026-02-14T10:15:30", "2026-02-14T10:15:30+02:00", "not-a-time", 1771064130):
            with self.subTest(bad=bad):
                self.assert_rejected(valid_payload(timestamp=bad), "timestamp")

    def test_skew_beyond_limit(self) -> None:
        self.assert_rejected(valid_payload(timestamp="2026-02-14T10:20:31Z"), "timestamp", "from server time")
        self.assert_rejected(valid_payload(timestamp="2026-02-14T10:10:29Z"), "timestamp", "from server time")

    def test_readings_shape(self) -> None:
        self.assert_rejected(valid_payload(readings="x"), "readings")
        self.assert_rejected(valid_payload(readings=[]), "readings")

    def test_missing_and_extra_metrics(self) -> None:
        readings = valid_payload()["readings"]
        missing = {key: value for key, value in readings.items() if key != "flow_rate_m3_s"}
        self.assert_rejected(valid_payload(readings=missing), "readings", "missing metric")

        extra = dict(readings, turbidity_ntu=1.0)
        self.assert_rejected(valid_payload(readings=extra), "readings", "unexpected metric")

    def test_non_numeric_and_non_finite_values(self) -> None:
        for bad in ("42", None, True, float("nan"), float("inf")):
            readings = valid_payload()["readings"]
            readings["rainfall_mm_per_hr"] = bad
            with self.subTest(bad=bad):
                self.assert_rejected(valid_payload(readings=readings), "readings.rainfall_mm_per_hr")

    def test_out_of_range_values(self) -> None:
        cases = {
            "rainfall_mm_per_hr": 250.1,
            "drain_level_m": -0.1,
            "soil_saturation_pct": 100.5,
            "water_level_m": 6.1,
            "flow_rate_m3_s": 50.5,
        }
        for metric, bad in cases.items():
            readings = valid_payload()["readings"]
            readings[metric] = bad
            with self.subTest(metric=metric):
                self.assert_rejected(valid_payload(readings=readings), f"readings.{metric}", "within")

    def test_meta_validation(self) -> None:
        self.assert_rejected(valid_payload(meta="run"), "meta")
        self.assert_rejected(valid_payload(meta={"device": "x"}), "meta", "unexpected meta field")
        self.assert_rejected(valid_payload(meta={"seq": -1}), "meta.seq")
        self.assert_rejected(valid_payload(meta={"seq": "1"}), "meta.seq")
        self.assert_rejected(valid_payload(meta={"run_id": "bad id!"}), "meta.run_id")
        self.assert_rejected(valid_payload(meta={"phase": "FLASH"}), "meta.phase")

    def test_error_messages_name_the_field(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            validate_reading(valid_payload(zone_id="nope"), NOW, ZONE_IDS)
        self.assertIn("zone_id", str(ctx.exception))


class BodyAndTimestampHelperTests(unittest.TestCase):
    def test_body_size_limit(self) -> None:
        validate_body_size(json.dumps(valid_payload()))
        with self.assertRaises(ValidationError):
            validate_body_size("x" * (BODY_MAX_BYTES + 1))
        with self.assertRaises(ValidationError):
            validate_body_size(None)

    def test_parse_utc_timestamp_normalises_to_utc(self) -> None:
        parsed = parse_utc_timestamp("2026-02-14T10:15:30Z")
        self.assertEqual(parsed.tzinfo, timezone.utc)
        self.assertEqual(int(parsed.timestamp()), NOW)

    def test_parse_utc_timestamp_rejects_non_z(self) -> None:
        with self.assertRaises(ValidationError):
            parse_utc_timestamp("2026-02-14T10:15:30+00:00")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

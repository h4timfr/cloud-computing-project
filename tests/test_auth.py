"""Tests for the M2 ingestion secret (``x-fp-key``).

These run entirely offline. They also assert that the secret never leaks into a
response payload, and that the dashboard's read routes stay unaffected by it.
"""

from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timezone
from unittest import mock

from floodpulse.auth import (
    CLOUD_STORE,
    INGEST_KEY_ENV,
    INGEST_KEY_HEADER,
    STORE_ENV,
    check_ingest,
    cloud_mode,
    configured_key,
    header_key,
    requires_key,
)
from floodpulse.handler import dispatch
from floodpulse.storage import LocalStore

SECRET = "unit-test-secret-value"
NOW_EPOCH = 1_792_000_000


def iso_utc(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def payload_reading(zone_id: str = "zone-riverside-north", ts_epoch: int = NOW_EPOCH) -> str:
    return json.dumps(
        {
            "schema_version": "1.0",
            "simulated": True,
            "zone_id": zone_id,
            "sensor_node_id": "sim-node",
            "timestamp": iso_utc(ts_epoch),
            "readings": {
                "rainfall_mm_per_hr": 2.0,
                "drain_level_m": 0.4,
                "soil_saturation_pct": 40.0,
                "water_level_m": 1.0,
                "flow_rate_m3_s": 1.5,
            },
        }
    )


class EnvTest(unittest.TestCase):
    def test_configured_key_unset_blank_and_set(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(INGEST_KEY_ENV, None)
            self.assertIsNone(configured_key())
            os.environ[INGEST_KEY_ENV] = "   "
            self.assertIsNone(configured_key())
            os.environ[INGEST_KEY_ENV] = SECRET
            self.assertEqual(configured_key(), SECRET)

    def test_cloud_mode(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(STORE_ENV, None)
            self.assertFalse(cloud_mode())
            os.environ[STORE_ENV] = CLOUD_STORE
            self.assertTrue(cloud_mode())
            os.environ[STORE_ENV] = " DynamoDB "
            self.assertTrue(cloud_mode())
            os.environ[STORE_ENV] = "local"
            self.assertFalse(cloud_mode())

    def test_requires_key(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(INGEST_KEY_ENV, None)
            os.environ.pop(STORE_ENV, None)
            self.assertFalse(requires_key())  # local dev, no secret
            os.environ[STORE_ENV] = CLOUD_STORE
            self.assertTrue(requires_key())  # cloud, secret still missing -> fail closed
            os.environ[INGEST_KEY_ENV] = SECRET
            self.assertTrue(requires_key())


class HeaderTest(unittest.TestCase):
    def test_case_insensitive_lookup(self) -> None:
        self.assertEqual(header_key({INGEST_KEY_HEADER: SECRET}), SECRET)
        self.assertEqual(header_key({"X-FP-KEY": SECRET}), SECRET)
        self.assertEqual(header_key({"X-Fp-Key": SECRET}), SECRET)

    def test_missing_or_empty(self) -> None:
        self.assertIsNone(header_key(None))
        self.assertIsNone(header_key({}))
        self.assertIsNone(header_key({"content-type": "application/json"}))
        self.assertIsNone(header_key({INGEST_KEY_HEADER: None}))
        self.assertIsNone(header_key({INGEST_KEY_HEADER: ""}))

    def test_unrelated_headers_are_ignored(self) -> None:
        headers = {"content-type": "application/json", "authorization": "Bearer something"}
        self.assertIsNone(header_key(headers))


class CheckIngestTest(unittest.TestCase):
    def test_local_mode_without_secret_allows(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(INGEST_KEY_ENV, None)
            os.environ.pop(STORE_ENV, None)
            self.assertIsNone(check_ingest({}))
            self.assertIsNone(check_ingest(None))

    def test_cloud_mode_without_secret_fails_closed(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(INGEST_KEY_ENV, None)
            os.environ[STORE_ENV] = CLOUD_STORE
            denial = check_ingest({INGEST_KEY_HEADER: SECRET})
            self.assertIsNotNone(denial)
            status, payload = denial  # type: ignore[misc]
            self.assertEqual(status, 503)
            self.assertEqual(payload["error"], "ingest_not_configured")

    def test_missing_header_rejected(self) -> None:
        with mock.patch.dict(os.environ, {INGEST_KEY_ENV: SECRET}):
            status, payload = check_ingest({})  # type: ignore[misc]
            self.assertEqual(status, 401)
            self.assertEqual(payload["error"], "unauthorized")
            self.assertIn(INGEST_KEY_HEADER, payload["detail"])

    def test_wrong_key_rejected(self) -> None:
        with mock.patch.dict(os.environ, {INGEST_KEY_ENV: SECRET}):
            for wrong in ("nope", SECRET + "x", SECRET[:-1], " " + SECRET):
                with self.subTest(wrong=wrong):
                    status, payload = check_ingest({INGEST_KEY_HEADER: wrong})  # type: ignore[misc]
                    self.assertEqual(status, 401)

    def test_correct_key_allowed(self) -> None:
        with mock.patch.dict(os.environ, {INGEST_KEY_ENV: SECRET}):
            self.assertIsNone(check_ingest({INGEST_KEY_HEADER: SECRET}))
            self.assertIsNone(check_ingest({"X-FP-KEY": SECRET}))

    def test_non_ascii_header_does_not_raise(self) -> None:
        with mock.patch.dict(os.environ, {INGEST_KEY_ENV: SECRET}):
            status, _payload = check_ingest({INGEST_KEY_HEADER: "ünïcode-ключ"})  # type: ignore[misc]
            self.assertEqual(status, 401)

    def test_secret_never_appears_in_error_payloads(self) -> None:
        with mock.patch.dict(os.environ, {INGEST_KEY_ENV: SECRET}):
            responses = [
                check_ingest({}),
                check_ingest({INGEST_KEY_HEADER: "wrong"}),
                check_ingest({INGEST_KEY_HEADER: SECRET + "suffix"}),
            ]
            for denial in responses:
                self.assertIsNotNone(denial)
                text = json.dumps(denial)
                self.assertNotIn(SECRET, text)
                self.assertNotIn(str(len(SECRET)), text)


class DispatchProtectionTest(unittest.TestCase):
    """The real routing path: POST protected, GETs unaffected."""

    def setUp(self) -> None:
        self.store = LocalStore()

    def test_post_rejected_without_key_when_configured(self) -> None:
        with mock.patch.dict(os.environ, {INGEST_KEY_ENV: SECRET}):
            status, payload = dispatch("POST", "/readings", payload_reading(), self.store, 1_792_000_000)
            self.assertEqual(status, 401)
            self.assertNotIn(SECRET, json.dumps(payload))
            self.assertEqual(self.store.item_count(), 0)  # nothing was stored

    def test_post_rejected_with_wrong_key(self) -> None:
        with mock.patch.dict(os.environ, {INGEST_KEY_ENV: SECRET}):
            status, payload = dispatch(
                "POST",
                "/readings",
                payload_reading(),
                self.store,
                1_792_000_000,
                None,
                {INGEST_KEY_HEADER: "definitely-wrong"},
            )
            self.assertEqual(status, 401)
            self.assertEqual(self.store.item_count(), 0)

    def test_post_accepted_with_correct_key(self) -> None:
        with mock.patch.dict(os.environ, {INGEST_KEY_ENV: SECRET}):
            status, payload = dispatch(
                "POST",
                "/readings",
                payload_reading(),
                self.store,
                1_792_000_000,
                None,
                {INGEST_KEY_HEADER: SECRET},
            )
            self.assertEqual(status, 202, payload)
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(self.store.item_count(), 2)  # LATEST + one history record

    def test_gets_need_no_key_and_never_mention_it(self) -> None:
        with mock.patch.dict(os.environ, {INGEST_KEY_ENV: SECRET}):
            dispatch(
                "POST",
                "/readings",
                payload_reading(),
                self.store,
                1_792_000_000,
                None,
                {INGEST_KEY_HEADER: SECRET},
            )
            for path in ("/zones", "/zones/zone-riverside-north/history"):
                with self.subTest(path=path):
                    status, payload = dispatch("GET", path, None, self.store, 1_792_000_060)
                    self.assertEqual(status, 200)
                    self.assertNotIn(SECRET, json.dumps(payload))

    def test_local_runs_without_secret_stay_open(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(INGEST_KEY_ENV, None)
            os.environ.pop(STORE_ENV, None)
            status, _payload = dispatch("POST", "/readings", payload_reading(), self.store, 1_792_000_000)
            self.assertEqual(status, 202)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

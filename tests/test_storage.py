"""Unit tests for the storage layer (in-memory only; no AWS calls)."""

from __future__ import annotations

import unittest

from floodpulse.storage import (
    LATEST_SORT_KEY,
    LocalStore,
    build_history_item,
    build_latest_item,
    build_zone_pk,
)

READING = {
    "zone_id": "zone-riverside-north",
    "sensor_node_id": "sim-riverside-north",
    "timestamp": "2026-02-14T10:15:30Z",
    "ts_epoch": 1771064130,
    "ttl": 1771064130 + 604800,
    "data_classification": "simulated",
    "readings": {"rainfall_mm_per_hr": 42.5},
    "meta": {"run_id": "sim-1", "seq": 1},
    "ingested_at_epoch": 1771064130,
}

RISK = {
    "risk_score": 0.65,
    "risk_class_candidate": "WARNING",
    "base_score": 0.65,
    "model": "heuristic-v1",
    "model_note": "heuristic",
    "trend": {"weight": 0.1, "contribution": 0.0, "rise_fraction": None, "water_level_delta_m": None},
    "contributions": [{"variable": "rainfall_mm_per_hr", "contribution": 0.3}],
    "top_drivers": ["rainfall_mm_per_hr", "water_level_m", "soil_saturation_pct"],
    "explanation": "risk_score 0.6500 ...",
    "smoothing_window_readings": 3,
}

HYSTERESIS = {
    "risk_class": "WARNING",
    "candidate_class": "WARNING",
    "streak_class": None,
    "streak_count": 0,
    "transition": "initial",
}

SMOOTHED = {"rainfall_mm_per_hr": 42.5, "drain_level_m": 1.0, "soil_saturation_pct": 50.0,
            "water_level_m": 2.0, "flow_rate_m3_s": 3.0}


class ItemShapeTests(unittest.TestCase):
    def test_partition_key(self) -> None:
        self.assertEqual(build_zone_pk("zone-harbor-east"), "ZONE#zone-harbor-east")

    def test_latest_item_shape(self) -> None:
        item = build_latest_item(READING, RISK, HYSTERESIS, SMOOTHED, sequence=7)
        self.assertEqual(item["pk"], "ZONE#zone-riverside-north")
        self.assertEqual(item["sk"], LATEST_SORT_KEY)
        self.assertEqual(item["record_type"], "LATEST")
        self.assertEqual(item["sequence"], 7)
        self.assertTrue(item["simulated"])
        self.assertEqual(item["data_classification"], "simulated")
        self.assertEqual(item["risk_score"], 0.65)
        self.assertEqual(item["risk_class"], "WARNING")
        self.assertEqual(item["risk_class_candidate"], "WARNING")
        self.assertEqual(item["risk_detail"]["model"], "heuristic-v1")
        self.assertEqual(item["hysteresis"]["transition"], "initial")
        self.assertNotIn("device_id", item)

    def test_history_item_shape_is_trimmed(self) -> None:
        item = build_history_item(READING, RISK, HYSTERESIS, SMOOTHED)
        self.assertEqual(item["sk"], "H#2026-02-14T10:15:30Z")
        self.assertEqual(item["record_type"], "HISTORY")
        self.assertEqual(item["ttl"], READING["ttl"])
        self.assertEqual(item["risk_class"], "WARNING")
        self.assertNotIn("risk_detail", item)
        self.assertNotIn("sequence", item)
        self.assertNotIn("device_id", item)

    def test_items_do_not_alias_the_input_reading(self) -> None:
        reading = dict(READING, readings=dict(READING["readings"]))
        item = build_latest_item(reading, RISK, HYSTERESIS, SMOOTHED, sequence=1)
        reading["readings"]["rainfall_mm_per_hr"] = 999.0
        self.assertEqual(item["readings"]["rainfall_mm_per_hr"], 42.5)


class LocalStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = LocalStore()
        self.reading = dict(READING, readings=dict(READING["readings"]))

    def _put(self, ts_epoch: int, timestamp: str, rain: float) -> None:
        reading = dict(self.reading, ts_epoch=ts_epoch, timestamp=timestamp, readings={"rainfall_mm_per_hr": rain})
        self.store.put_reading(reading, RISK, HYSTERESIS, SMOOTHED)

    def test_empty_store(self) -> None:
        self.assertIsNone(self.store.get_latest("zone-riverside-north"))
        self.assertEqual(self.store.all_latest(), [])
        self.assertEqual(self.store.get_history("zone-riverside-north"), [])
        self.assertEqual(self.store.item_count(), 0)

    def test_put_writes_latest_and_history(self) -> None:
        latest, history = self.store.put_reading(self.reading, RISK, HYSTERESIS, SMOOTHED)
        self.assertEqual(latest["sequence"], 1)
        self.assertEqual(history["sk"], "H#2026-02-14T10:15:30Z")
        self.assertEqual(self.store.item_count(), 2)
        self.assertEqual(self.store.history_count("zone-riverside-north"), 1)
        self.assertEqual(self.store.get_latest("zone-riverside-north")["sequence"], 1)

    def test_latest_is_overwritten_and_history_appended(self) -> None:
        self._put(100, "2026-02-14T10:00:00Z", rain=1.0)
        self._put(200, "2026-02-14T10:01:40Z", rain=2.0)
        self.assertEqual(self.store.item_count(), 3)
        self.assertEqual(self.store.get_latest("zone-riverside-north")["sequence"], 2)
        self.assertEqual(self.store.get_latest("zone-riverside-north")["readings"]["rainfall_mm_per_hr"], 2.0)
        self.assertEqual(self.store.history_count("zone-riverside-north"), 2)

    def test_duplicate_timestamps_get_distinct_sort_keys(self) -> None:
        self._put(100, "2026-02-14T10:00:00Z", rain=1.0)
        self._put(100, "2026-02-14T10:00:00Z", rain=2.0)
        keys = [item["sk"] for item in self.store.get_history("zone-riverside-north")]
        self.assertEqual(keys, ["H#2026-02-14T10:00:00Z", "H#2026-02-14T10:00:00Z#2"])

    def test_history_is_ascending_and_filterable(self) -> None:
        self._put(300, "2026-02-14T10:05:00Z", rain=3.0)
        self._put(100, "2026-02-14T10:01:40Z", rain=1.0)
        self._put(200, "2026-02-14T10:03:20Z", rain=2.0)

        everything = self.store.get_history("zone-riverside-north")
        self.assertEqual([item["ts_epoch"] for item in everything], [100, 200, 300])

        windowed = self.store.get_history("zone-riverside-north", since_epoch=150, until_epoch=250)
        self.assertEqual([item["ts_epoch"] for item in windowed], [200])

        limited = self.store.get_history("zone-riverside-north", limit=2)
        self.assertEqual([item["ts_epoch"] for item in limited], [200, 300])

    def test_recent_readings_and_all_latest(self) -> None:
        self._put(100, "2026-02-14T10:01:40Z", rain=1.0)
        self._put(200, "2026-02-14T10:03:20Z", rain=2.0)
        recent = self.store.recent_readings("zone-riverside-north", 2)
        self.assertEqual([item["readings"]["rainfall_mm_per_hr"] for item in recent], [1.0, 2.0])
        self.assertEqual(self.store.recent_readings("zone-riverside-north", 0), [])
        self.assertEqual([item["zone_id"] for item in self.store.all_latest()], ["zone-riverside-north"])

    def test_reset_clears_everything(self) -> None:
        self._put(100, "2026-02-14T10:01:40Z", rain=1.0)
        self.store.reset()
        self.assertEqual(self.store.item_count(), 0)
        self.assertIsNone(self.store.get_latest("zone-riverside-north"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

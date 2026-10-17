"""Unit tests for the storage layer (in-memory only; no AWS calls)."""

from __future__ import annotations

import unittest

from floodpulse.incidents import build_alert_marker, build_incident_item
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
        latest, history, outcome = self.store.put_reading(self.reading, RISK, HYSTERESIS, SMOOTHED)
        self.assertEqual(latest["sequence"], 1)
        self.assertEqual(history["sk"], "H#2026-02-14T10:15:30Z")
        self.assertEqual(outcome.outcome, "stored")
        self.assertTrue(outcome.latest_updated)
        self.assertTrue(outcome.history_written)
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


class LocalStoreIncidentTests(unittest.TestCase):
    """Incident records and alert cooldown markers in the local store (M3)."""

    def setUp(self) -> None:
        self.store = LocalStore()

    def incident(self, zone_id: str = "zone-riverside-north", ts_epoch: int = 1_000, severity: str = "WARNING"):
        return build_incident_item(
            zone_id=zone_id,
            severity=severity,
            risk_score=0.65,
            risk_class=severity,
            top_driver="water_level_m",
            opened_ts_epoch=ts_epoch,
            now_epoch=ts_epoch,
        )

    def test_create_is_conditional_and_put_updates(self) -> None:
        item = self.incident()
        self.assertTrue(self.store.create_incident(item))
        self.assertFalse(self.store.create_incident(item))
        self.assertEqual(self.store.incident_count(), 1)

        closed = dict(item, current_status="CLOSED", closed_ts_epoch=1_300, duration_seconds=300)
        self.store.put_incident(closed)
        self.assertEqual(self.store.incident_count(), 1)
        self.assertEqual(self.store.latest_incident(item["zone_id"])["duration_seconds"], 300)

        # the stored copy is not aliased to the caller's dict
        closed["current_status"] = "MUTATED"
        self.assertEqual(self.store.latest_incident(item["zone_id"])["current_status"], "CLOSED")

    def test_latest_and_list_are_newest_first(self) -> None:
        for epoch in (1_000, 1_600, 2_200):
            self.store.create_incident(self.incident(ts_epoch=epoch))
        self.store.create_incident(self.incident(zone_id="zone-harbor-east", ts_epoch=1_900))

        self.assertEqual(self.store.latest_incident("zone-riverside-north")["opened_ts_epoch"], 2_200)
        self.assertIsNone(self.store.latest_incident("zone-old-town-canal"))

        epochs = [item["opened_ts_epoch"] for item in self.store.list_incidents()]
        self.assertEqual(epochs, [2_200, 1_900, 1_600, 1_000])
        self.assertEqual(
            [item["opened_ts_epoch"] for item in self.store.list_incidents(limit=2)], [2_200, 1_900]
        )
        self.assertEqual(len(self.store.list_incidents(zone_id="zone-riverside-north")), 3)
        self.assertEqual(self.store.incident_count(), 4)
        self.assertEqual(self.store.incident_count("zone-harbor-east"), 1)

    def test_incidents_do_not_collide_with_readings(self) -> None:
        """An incident and a reading for the same zone share the partition key only."""
        self.store.create_incident(self.incident())
        self.assertEqual(self.store.get_latest("zone-riverside-north"), None)
        self.assertEqual(self.store.history_count("zone-riverside-north"), 0)
        self.assertEqual(self.store.incident_count("zone-riverside-north"), 1)
        self.assertIsNone(self.store.get_alert_marker("zone-riverside-north"))

    def test_alert_marker_round_trip(self) -> None:
        self.store.put_alert_marker(
            build_alert_marker(zone_id="zone-riverside-north", severity="WARNING", now_epoch=1_000, message_id="m1")
        )
        marker = self.store.get_alert_marker("zone-riverside-north")
        self.assertEqual(marker["sk"], "ALERT#COOLDOWN")
        self.assertEqual(marker["last_alert_by_severity"]["WARNING"]["message_id"], "m1")

        merged = build_alert_marker(
            zone_id="zone-riverside-north",
            severity="CRITICAL",
            now_epoch=1_100,
            previous=marker,
            message_id="m2",
        )
        self.store.put_alert_marker(merged)
        stored = self.store.get_alert_marker("zone-riverside-north")
        self.assertEqual(set(stored["last_alert_by_severity"]), {"WARNING", "CRITICAL"})
        self.assertEqual(stored["updated_ts_epoch"], 1_100)
        self.assertEqual(self.store.item_count(), 1, "the marker is a single item per zone")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""Local end-to-end test: simulator -> validation -> risk -> store -> API.

This is the M0 vertical slice, exercised entirely in-process. No AWS calls, no
network, no wall clock (the simulated clock is injected as ``now_epoch``).
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime

from floodpulse.config import ALERT_MARKER_SORT_KEY, HISTORY_TTL_SECONDS, METRIC_ORDER, ZONE_IDS
from floodpulse.handler import dispatch, set_notifier, set_store
from floodpulse.risk import class_rank
from floodpulse.simulator import simulators_for_all_zones
from floodpulse.storage import LocalStore

from tests.support import ALERT_COOLDOWN, RecordingNotifier

SEED = 20260214
STEPS = 60


def epoch_of(iso_timestamp: str) -> int:
    return int(datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00")).timestamp())


def run_scenario(steps: int = STEPS, seed: int = SEED) -> dict:
    """Push a full simulated scenario through the handler, in-process."""
    store = LocalStore()
    set_store(store)
    notifier = RecordingNotifier()
    set_notifier(notifier)
    simulators = simulators_for_all_zones(seed=seed)

    responses: list[dict] = []
    statuses: list[int] = []
    for index in range(steps):
        for simulator in simulators:
            payload = simulator.step()
            status, body = dispatch(
                "POST",
                "/readings",
                json.dumps(payload),
                store,
                epoch_of(payload["timestamp"]),
                {},
                {},
                notifier,
                ALERT_COOLDOWN,
            )
            statuses.append(status)
            responses.append(body)

    end_epoch = simulators[0].reading_epoch(steps - 1)
    zones_status, zones = dispatch("GET", "/zones", None, store, end_epoch)
    history_status, history = dispatch(
        "GET", "/zones/zone-riverside-north/history", None, store, end_epoch, {"minutes": "60"}
    )
    return {
        "store": store,
        "notifier": notifier,
        "statuses": statuses,
        "responses": responses,
        "zones": zones,
        "zones_status": zones_status,
        "history": history,
        "history_status": history_status,
        "end_epoch": end_epoch,
    }


class LocalEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # NOTE: named ``scenario`` (not ``run``) because TestCase.run() is reserved.
        cls.scenario = run_scenario()
        set_store(None)

    def test_every_simulated_reading_is_accepted(self) -> None:
        statuses = self.scenario["statuses"]
        self.assertEqual(len(statuses), STEPS * len(ZONE_IDS))
        self.assertEqual(set(statuses), {202})

    def test_scenario_starts_normal_and_reaches_critical(self) -> None:
        max_rank = {zone_id: 0 for zone_id in ZONE_IDS}
        first_classes: dict[str, str] = {}
        transitions: set[str] = set()

        for index, body in enumerate(self.scenario["responses"]):
            zone_id = body["zone_id"]
            if index < len(ZONE_IDS):
                first_classes[zone_id] = body["risk"]["risk_class"]
            transitions.add(body["hysteresis"]["transition"])
            max_rank[zone_id] = max(max_rank[zone_id], class_rank(body["risk"]["risk_class"]))

        self.assertEqual(set(first_classes.values()), {"NORMAL"})
        self.assertIn("initial", transitions)
        self.assertIn("pending", transitions)  # hysteresis really engaged
        self.assertIn("escalate", transitions)
        self.assertEqual(max(max_rank.values()), class_rank("CRITICAL"))
        for zone_id, rank in max_rank.items():
            with self.subTest(zone=zone_id):
                self.assertGreaterEqual(rank, class_rank("WARNING"))

    def test_zone_read_model_matches_stored_state(self) -> None:
        store = self.scenario["store"]
        zones = self.scenario["zones"]
        self.assertEqual(self.scenario["zones_status"], 200)

        by_id = {zone["zone_id"]: zone for zone in zones["zones"]}
        scores = []
        for zone_id, view in by_id.items():
            latest = store.get_latest(zone_id)
            with self.subTest(zone=zone_id):
                self.assertEqual(view["status"], "live")
                self.assertEqual(view["latest"]["risk_score"], latest["risk_score"])
                self.assertEqual(view["latest"]["risk_class"], latest["risk_class"])
                self.assertEqual(len(view["latest"]["contributions"]), len(METRIC_ORDER))
                self.assertIn("dominant driver", view["latest"]["explanation"])
                self.assertTrue(view["latest"]["simulated"])
                scores.append((view["latest"]["risk_score"], zone_id))

        # same tie-break as the API: highest score first, then zone_id ascending
        best_score, best_zone = sorted(scores, key=lambda item: (-item[0], item[1]))[0]
        self.assertEqual(zones["highest_risk_zone"]["zone_id"], best_zone)
        self.assertEqual(zones["highest_risk_zone"]["risk_score"], best_score)
        self.assertEqual(
            zones["zone_ranking"],
            [zone_id for _score, zone_id in sorted(scores, key=lambda item: (-item[0], item[1]))],
        )

    def test_history_endpoint_returns_the_whole_run(self) -> None:
        history = self.scenario["history"]
        self.assertEqual(self.scenario["history_status"], 200)
        self.assertEqual(history["point_count"], STEPS)
        self.assertFalse(history["downsampled"])
        epochs = [point["ts_epoch"] for point in history["points"]]
        self.assertEqual(epochs, sorted(epochs))
        self.assertEqual(epochs[0], self.scenario["end_epoch"] - (STEPS - 1) * 5)
        self.assertTrue(all(point["risk_class"] in {"NORMAL", "WATCH", "WARNING", "CRITICAL"} for point in history["points"]))

    def test_stored_records_are_marked_simulated_with_server_side_ttl(self) -> None:
        store = self.scenario["store"]
        items = list(store.items())
        self.assertTrue(items)
        reading_items = 0
        incident_items = 0
        marker_items = 0
        for item in items:
            with self.subTest(sk=item["sk"]):
                self.assertTrue(item["simulated"])
                self.assertEqual(item["data_classification"], "simulated")
                self.assertNotIn("device_id", item)
                if item["sk"].startswith("INCIDENT#"):
                    # incidents are retained as permanent history, so no TTL at all
                    incident_items += 1
                    self.assertNotIn("ttl", item)
                    self.assertEqual(item["record_type"], "INCIDENT")
                    self.assertEqual(item["current_status"], item["current_status"].upper())
                elif item["sk"] == ALERT_MARKER_SORT_KEY:
                    marker_items += 1
                    self.assertIn("last_alert_by_severity", item)
                    self.assertNotIn("ttl", item)
                else:
                    reading_items += 1
                    self.assertIn("sensor_node_id", item)
                    self.assertEqual(item["ttl"], int(item["ts_epoch"]) + HISTORY_TTL_SECONDS)
        self.assertGreater(reading_items, 0)
        self.assertGreater(incident_items, 0, "the scenario should have produced incidents")
        self.assertGreater(marker_items, 0, "published alerts should leave a cooldown marker")
        for zone_id in ZONE_IDS:
            self.assertEqual(store.history_count(zone_id), STEPS)

    def test_incidents_are_created_once_per_zone_and_never_duplicated(self) -> None:
        store = self.scenario["store"]
        responses = self.scenario["responses"]

        opened: dict[str, list[str]] = {}
        for body in responses:
            if body["incident"]["action"] == "opened":
                opened.setdefault(body["zone_id"], []).append(body["incident"]["incident_id"])
        self.assertTrue(opened, "the storm should open at least one incident")
        for zone_id, incident_ids in opened.items():
            with self.subTest(zone_id=zone_id):
                self.assertEqual(len(incident_ids), 1, "an incident must open only once")
                self.assertEqual(store.incident_count(zone_id), 1)

        # zones that never left NORMAL/WATCH have no incident at all
        elevated = {
            body["zone_id"]
            for body in responses
            if body["risk"]["risk_class"] in ("WARNING", "CRITICAL")
        }
        self.assertEqual(set(opened), elevated)
        self.assertEqual(store.incident_count(), len(elevated))

    def test_alerts_are_published_only_for_escalations(self) -> None:
        notifier = self.scenario["notifier"]
        store = self.scenario["store"]

        # one alert per recorded escalation, and nothing else
        expected = sum(len(incident["alert_severities"]) for incident in store.list_incidents())
        self.assertEqual(len(notifier.published), expected)
        self.assertEqual(len(notifier.attempts), expected)
        self.assertGreater(len(notifier.published), 0)
        self.assertTrue(all("FloodPulse" in entry["subject"] for entry in notifier.published))
        self.assertTrue(all("simulated" in entry["message"].lower() for entry in notifier.published))

        # no suppression was needed during a single first storm
        self.assertTrue(
            all(body["alert"].get("reason") != "cooldown" for body in self.scenario["responses"])
        )

    def test_incidents_endpoint_reflects_the_run(self) -> None:
        store = self.scenario["store"]
        status, payload = dispatch(
            "GET", "/incidents", None, store, self.scenario["end_epoch"], {"limit": "50"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["count"], store.incident_count())
        self.assertEqual(payload["open_count"], len(store.list_incidents()))
        for incident in payload["incidents"]:
            for field in ("incident_id", "zone_id", "severity", "opened_at", "current_status", "duration_seconds"):
                self.assertIn(field, incident)

    def test_deferred_features_are_reported_not_faked(self) -> None:
        zones = self.scenario["zones"]
        # M3 delivered incident creation and SNS alerting, so only time-to-threshold remains
        self.assertEqual(list(zones["deferred_features"]), ["time_to_threshold"])
        for delivered in ("automated_alerts_sns", "incident_creation", "web_dashboard"):
            self.assertNotIn(delivered, zones["deferred_features"])
        self.assertIn("Time-to-threshold is not implemented yet", " ".join(zones["notes"]))
        for view in zones["zones"]:
            self.assertNotIn("time_to_threshold", view["latest"])

    def test_whole_pipeline_is_deterministic(self) -> None:
        second = run_scenario()
        set_store(None)
        self.assertEqual(
            json.dumps(self.scenario["zones"], sort_keys=True),
            json.dumps(second["zones"], sort_keys=True),
        )
        self.assertEqual(
            json.dumps(self.scenario["history"], sort_keys=True),
            json.dumps(second["history"], sort_keys=True),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""Local end-to-end test: simulator -> validation -> risk -> store -> API.

This is the M0 vertical slice, exercised entirely in-process. No AWS calls, no
network, no wall clock (the simulated clock is injected as ``now_epoch``).
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime

from floodpulse.config import HISTORY_TTL_SECONDS, METRIC_ORDER, ZONE_IDS
from floodpulse.handler import dispatch, set_store
from floodpulse.risk import class_rank
from floodpulse.simulator import simulators_for_all_zones
from floodpulse.storage import LocalStore

SEED = 20260214
STEPS = 60


def epoch_of(iso_timestamp: str) -> int:
    return int(datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00")).timestamp())


def run_scenario(steps: int = STEPS, seed: int = SEED) -> dict:
    """Push a full simulated scenario through the handler, in-process."""
    store = LocalStore()
    set_store(store)
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
        for item in items:
            with self.subTest(sk=item["sk"]):
                self.assertTrue(item["simulated"])
                self.assertEqual(item["data_classification"], "simulated")
                self.assertEqual(item["ttl"], int(item["ts_epoch"]) + HISTORY_TTL_SECONDS)
                self.assertNotIn("device_id", item)
                self.assertIn("sensor_node_id", item)
        for zone_id in ZONE_IDS:
            self.assertEqual(store.history_count(zone_id), STEPS)

    def test_deferred_features_are_reported_not_faked(self) -> None:
        zones = self.scenario["zones"]
        self.assertIn("time_to_threshold", zones["deferred_features"])
        self.assertIn("automated_alerts_sns", zones["deferred_features"])
        self.assertIn("web_dashboard", zones["deferred_features"])
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

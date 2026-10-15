"""API-level tests for GET /incidents and the incident fields on POST /readings (M3).

Everything runs in-process through ``dispatch``: no AWS calls, no network.
"""

from __future__ import annotations

import json
import unittest

from floodpulse.config import INCIDENT_DEFAULT_LIMIT, INCIDENT_MAX_LIMIT, ZONE_IDS
from floodpulse.handler import dispatch, set_notifier, set_store
from floodpulse.notify import NoopNotifier
from floodpulse.storage import LocalStore

from tests.support import ALERT_COOLDOWN, START_EPOCH, RecordingNotifier, ZoneScenario

PANEL_FIELDS = (
    "incident_id",
    "zone_id",
    "zone_name",
    "severity",
    "current_status",
    "is_open",
    "opened_at",
    "opened_ts_epoch",
    "closed_at",
    "duration_seconds",
    "risk_score_at_open",
    "top_driver",
    "top_driver_label",
    "alert_count",
)


class IncidentsRouteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = LocalStore()
        set_store(self.store)
        set_notifier(NoopNotifier())

    def get(self, query: dict | None = None, notifier=None, now: float = START_EPOCH):
        return dispatch("GET", "/incidents", None, self.store, float(now), query or {}, {}, notifier, ALERT_COOLDOWN)

    def test_empty_log_is_a_valid_response(self) -> None:
        status, payload = self.get()
        self.assertEqual(status, 200)
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["open_count"], 0)
        self.assertEqual(payload["incidents"], [])
        self.assertEqual(payload["limit"], INCIDENT_DEFAULT_LIMIT)
        self.assertTrue(payload["simulated"])
        self.assertEqual(payload["model"], "heuristic-v1")
        self.assertIn("notes", payload)

    def test_default_and_maximum_limits(self) -> None:
        status, payload = self.get({"limit": str(INCIDENT_MAX_LIMIT)})
        self.assertEqual(status, 200)
        self.assertEqual(payload["limit"], INCIDENT_MAX_LIMIT)

        status, payload = self.get({"limit": INCIDENT_DEFAULT_LIMIT})
        self.assertEqual(status, 200)
        self.assertEqual(payload["limit"], INCIDENT_DEFAULT_LIMIT)

    def test_invalid_limits_are_rejected(self) -> None:
        for raw in ("0", str(INCIDENT_MAX_LIMIT + 1), "abc", "1.5", "-3", True):
            status, payload = self.get({"limit": raw})
            self.assertEqual(status, 400, f"limit={raw!r} should be rejected")
            self.assertEqual(payload["error"], "invalid_query")
            self.assertEqual(payload["field"], "limit")

    def test_unknown_zone_filter_is_404(self) -> None:
        status, payload = self.get({"zone_id": "zone-does-not-exist"})
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "unknown_zone")
        self.assertEqual(payload["known_zones"], list(ZONE_IDS))

    def test_zone_filter_returns_only_that_zone(self) -> None:
        ZoneScenario(self.store, zone_id=ZONE_IDS[0]).run_until_open()
        ZoneScenario(self.store, zone_id=ZONE_IDS[1], start_epoch=START_EPOCH + 1).run_until_open()

        status, payload = self.get({"zone_id": ZONE_IDS[0]})
        self.assertEqual(status, 200)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["zone_id"], ZONE_IDS[0])
        self.assertEqual(payload["incidents"][0]["zone_id"], ZONE_IDS[0])
        self.assertEqual(payload["open_count"], 1)

        status, everything = self.get()
        self.assertEqual(everything["count"], 2)

    def test_limit_returns_the_newest_incidents_first(self) -> None:
        base = START_EPOCH
        for index, zone_id in enumerate(ZONE_IDS):
            ZoneScenario(self.store, zone_id=zone_id, start_epoch=base + index * 10_000).run_until_open()

        status, payload = self.get({"limit": 2})
        self.assertEqual(status, 200)
        self.assertEqual(payload["count"], 2)
        opened = [item["opened_ts_epoch"] for item in payload["incidents"]]
        self.assertEqual(opened, sorted(opened, reverse=True))
        self.assertEqual(payload["incidents"][0]["zone_id"], ZONE_IDS[-1])

    def test_incidents_carry_every_field_the_panel_needs(self) -> None:
        ZoneScenario(self.store).run_until_open()
        status, payload = self.get()
        incident = payload["incidents"][0]
        for field in PANEL_FIELDS:
            self.assertIn(field, incident, f"{field} missing from the incidents payload")
        self.assertIsInstance(incident["duration_seconds"], int)
        self.assertGreaterEqual(incident["duration_seconds"], 0)
        self.assertTrue(incident["simulated"])
        self.assertEqual(incident["data_classification"], "simulated")

    def test_alerting_state_is_reported_without_the_topic_arn(self) -> None:
        status, payload = self.get(notifier=NoopNotifier())
        self.assertEqual(payload["alerting"]["enabled"], False)
        self.assertEqual(payload["alerting"]["severities"], ["WARNING", "CRITICAL"])
        self.assertEqual(payload["alerting"]["cooldown_seconds"], ALERT_COOLDOWN)

        status, payload = self.get(notifier=RecordingNotifier())
        self.assertTrue(payload["alerting"]["enabled"])

        blob = json.dumps(payload).lower()
        self.assertNotIn("arn:aws", blob)
        self.assertNotIn("floodpulse_sns_topic_arn", blob)

    def test_method_not_allowed(self) -> None:
        status, payload = dispatch("POST", "/incidents", "{}", self.store, float(START_EPOCH), {}, {})
        self.assertEqual(status, 405)
        self.assertEqual(payload["allowed"], ["GET"])

    def test_reads_stay_public(self) -> None:
        """No headers at all: the incidents log must still be readable."""
        status, payload = dispatch("GET", "/incidents", None, self.store, float(START_EPOCH), {}, None)
        self.assertEqual(status, 200)
        self.assertEqual(payload["count"], 0)


class ReadingResponseIncidentFieldsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = LocalStore()
        set_store(self.store)
        set_notifier(NoopNotifier())

    def test_reading_response_reports_incident_and_alert_state(self) -> None:
        scenario = ZoneScenario(self.store)
        produced, incident = scenario.run_until_open()
        opener = produced[-1]

        self.assertEqual(opener["status"], "accepted")
        self.assertEqual(opener["incident"]["action"], "opened")
        self.assertEqual(opener["incident"]["current_status"], "OPEN")
        self.assertTrue(opener["incident"]["changed"])
        self.assertEqual(opener["incident"]["incident_id"], incident["incident_id"])
        self.assertIn("alert", opener)
        self.assertFalse(opener["alert"]["published"])  # local mode: no topic configured
        self.assertEqual(opener["alert"]["reason"], "alerts_not_configured")

        # the reading itself is still fully reported, exactly as before M3
        for field in (
            "status",
            "risk",
            "hysteresis",
            "stored",
            "model_note",
            "simulated",
            "data_classification",
            "zone_id",
            "sensor_node_id",
            "timestamp",
            "ts_epoch",
            "ttl",
            "sequence",
            "deferred_features",
        ):
            self.assertIn(field, opener)

    def test_no_arn_or_secret_appears_in_any_response(self) -> None:
        notifier = RecordingNotifier()
        scenario = ZoneScenario(self.store, notifier=notifier)
        produced, _ = scenario.run_until_open()
        blob = json.dumps(produced).lower()
        self.assertNotIn("arn:aws", blob)
        self.assertNotIn("floodpulse_sns_topic_arn", blob)
        self.assertNotIn("floodpulse_ingest_key", blob)

        status, payload = dispatch("GET", "/zones", None, self.store, float(START_EPOCH), {})
        self.assertEqual(status, 200)
        self.assertNotIn("arn:aws", json.dumps(payload).lower())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

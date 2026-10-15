"""Incident lifecycle tests (M3), in-process and deterministic.

Covers the required behaviours: one incident per escalation, no duplicate spam,
WARNING -> CRITICAL escalation inside the same incident, closing only after the zone is
back to NORMAL, alerting only on meaningful escalation, and the per-zone/per-severity
cooldown. No AWS calls, no network, no wall clock.
"""

from __future__ import annotations

import unittest

from floodpulse.config import (
    ALERT_MARKER_SORT_KEY,
    INCIDENT_STATUS_CLOSED,
    INCIDENT_STATUS_OPEN,
)
from floodpulse.handler import dispatch, set_notifier, set_store
from floodpulse.incidents import (
    ACTION_CLOSED,
    ACTION_ESCALATED,
    ACTION_NONE,
    ACTION_OBSERVED,
    ACTION_OPENED,
    alerting_view,
    build_alert_marker,
    cooldown_remaining_seconds,
    decide_transition,
    incident_view,
)
from floodpulse.notify import NoopNotifier
from floodpulse.storage import LocalStore

from tests.support import (
    ALERT_COOLDOWN,
    START_EPOCH,
    ExplodingIncidentStore,
    ExplodingNotifier,
    RecordingNotifier,
    ZoneScenario,
)

ZONE = "zone-riverside-north"
OTHER_ZONE = "zone-old-town-canal"


class IncidentCreationTest(unittest.TestCase):
    """Requirement: create an incident when a zone escalates into WARNING or CRITICAL."""

    def setUp(self) -> None:
        self.store = LocalStore()
        set_store(self.store)
        set_notifier(NoopNotifier())
        self.scenario = ZoneScenario(self.store)

    def test_one_incident_created_on_escalation(self) -> None:
        produced, incident = self.scenario.run_until_open()

        self.assertEqual(self.store.incident_count(ZONE), 1)
        self.assertEqual(incident["current_status"], INCIDENT_STATUS_OPEN)
        self.assertEqual(incident["severity"], "WARNING")
        self.assertEqual(incident["record_type"], "INCIDENT")
        self.assertTrue(incident["simulated"])
        self.assertEqual(incident["data_classification"], "simulated")
        self.assertEqual(incident["model"], "heuristic-v1")

        # every required field is present and populated
        for field in (
            "incident_id",
            "zone_id",
            "severity",
            "opened_at",
            "current_status",
            "risk_score_at_open",
            "top_driver",
        ):
            self.assertTrue(incident.get(field) is not None, f"{field} is missing")
        self.assertEqual(incident["zone_id"], ZONE)
        self.assertEqual(incident["incident_id"], f"inc-{ZONE.replace('zone-', '')}-{incident['opened_at'].replace('-', '').replace(':', '')}")
        self.assertGreaterEqual(incident["risk_score_at_open"], 0.60)  # WARNING band

        # the response that opened it says so, and nothing was open before
        opener = [r for r in produced if r["incident"]["action"] == ACTION_OPENED]
        self.assertEqual(len(opener), 1)
        self.assertTrue(opener[0]["incident"]["changed"])
        self.assertEqual(opener[0]["incident"]["incident_id"], incident["incident_id"])

    def test_no_incident_below_warning(self) -> None:
        produced = self.scenario.run(20)  # calm phase only
        self.assertEqual(self.store.incident_count(ZONE), 0)
        self.assertTrue(all(r["incident"]["action"] == ACTION_NONE for r in produced))
        self.assertTrue(all(r["incident"]["current_status"] is None for r in produced))

    def test_no_duplicate_incidents_while_open(self) -> None:
        scenario = ZoneScenario(self.store)
        scenario.run(100)  # full calm -> storm -> flash flood -> recession arc

        self.assertEqual(self.store.incident_count(ZONE), 1)
        self.assertEqual(scenario.actions().count(ACTION_OPENED), 1)

        opened = [r for r in scenario.responses if r["incident"]["action"] == ACTION_OPENED]
        self.assertEqual(len(opened), 1)
        # while the incident was open, every later response referred to the same incident
        incident_id = opened[0]["incident"]["incident_id"]
        for response in scenario.responses[scenario.responses.index(opened[0]) :]:
            if response["incident"]["action"] == ACTION_NONE:
                # only possible after the incident closed: nothing is open any more
                self.assertEqual(self.store.latest_incident(ZONE)["current_status"], INCIDENT_STATUS_CLOSED)
                continue
            self.assertEqual(response["incident"]["incident_id"], incident_id)

    def test_each_zone_gets_its_own_incident(self) -> None:
        ZoneScenario(self.store, zone_id=ZONE).run_until_open()
        ZoneScenario(self.store, zone_id=OTHER_ZONE, start_epoch=START_EPOCH + 1).run_until_open()

        self.assertEqual(self.store.incident_count(), 2)
        self.assertEqual(self.store.incident_count(ZONE), 1)
        self.assertEqual(self.store.incident_count(OTHER_ZONE), 1)
        first = self.store.latest_incident(ZONE)
        second = self.store.latest_incident(OTHER_ZONE)
        self.assertNotEqual(first["incident_id"], second["incident_id"])


class EscalationTest(unittest.TestCase):
    """Requirement: WARNING -> CRITICAL escalates that incident instead of opening another."""

    def setUp(self) -> None:
        self.store = LocalStore()
        set_store(self.store)
        set_notifier(NoopNotifier())

    def test_warning_to_critical_escalation(self) -> None:
        scenario = ZoneScenario(self.store)
        _, incident_at_warning = scenario.run_until_open()
        self.assertEqual(incident_at_warning["severity"], "WARNING")

        produced = scenario.run_until_class("CRITICAL")
        incident = self.store.latest_incident(ZONE)

        self.assertEqual(incident["incident_id"], incident_at_warning["incident_id"])
        self.assertEqual(incident["severity"], "CRITICAL")
        self.assertEqual(incident["severity_at_open"], "WARNING")
        self.assertEqual(incident["current_status"], INCIDENT_STATUS_OPEN)
        self.assertEqual(self.store.incident_count(ZONE), 1, "escalation must not open a second incident")

        escalations = [r for r in produced if r["incident"]["action"] == ACTION_ESCALATED]
        self.assertEqual(len(escalations), 1)
        self.assertEqual(escalations[0]["incident"]["severity"], "CRITICAL")

        changes = incident["severity_changes"]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["from"], "WARNING")
        self.assertEqual(changes[0]["to"], "CRITICAL")
        self.assertGreaterEqual(incident["risk_score_peak"], incident["risk_score_at_open"])

    def test_candidate_class_alone_does_not_escalate(self) -> None:
        """Only the effective class acts, so hysteresis cannot be short-circuited."""
        base = {
            "pk": "ZONE#z",
            "sk": "INCIDENT#2020-01-01T00:00:00Z",
            "incident_id": "inc-z-20200101T000000Z",
            "zone_id": "z",
            "zone_name": "Z",
            "severity": "WARNING",
            "severity_at_open": "WARNING",
            "current_status": INCIDENT_STATUS_OPEN,
            "opened_at": "2020-01-01T00:00:00Z",
            "opened_ts_epoch": 1_577_836_800,
            "risk_score_at_open": 0.65,
            "risk_score_peak": 0.65,
            "reading_count": 1,
            "severity_changes": [],
        }
        # the caller passes the effective class; WARNING here means "the candidate was
        # CRITICAL but the streak has not been satisfied yet" -> observe, do not escalate
        transition = decide_transition(
            zone_id="z",
            active=base,
            risk_class="WARNING",
            risk_score=0.79,
            top_driver="water_level_m",
            observed_ts_epoch=1_577_836_860,
            now_epoch=1_577_836_860,
        )
        self.assertEqual(transition.action, ACTION_OBSERVED)
        self.assertEqual(transition.incident["severity"], "WARNING")
        self.assertEqual(transition.incident["severity_changes"], [])
        self.assertEqual(transition.incident["last_risk_class"], "WARNING")


class ClosingTest(unittest.TestCase):
    """Requirement: keep incidents open while elevated; close only after NORMAL."""

    def setUp(self) -> None:
        self.store = LocalStore()
        set_store(self.store)
        set_notifier(NoopNotifier())

    def test_incident_closes_after_zone_returns_to_normal(self) -> None:
        scenario = ZoneScenario(self.store)
        scenario.run_until_open()
        scenario.run_until_class("CRITICAL")
        produced = scenario.run_until_normal_after_incident()
        incident = self.store.latest_incident(ZONE)

        self.assertEqual(incident["current_status"], INCIDENT_STATUS_CLOSED)
        self.assertIsNotNone(incident["closed_at"])
        self.assertIsNotNone(incident["duration_seconds"])
        self.assertGreater(incident["duration_seconds"], 0)
        self.assertEqual(incident["close_reason"], "zone returned to NORMAL")
        self.assertEqual(self.store.incident_count(ZONE), 1, "closing must not open a new incident")

        closed = [r for r in produced if r["incident"]["action"] == ACTION_CLOSED]
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["incident"]["current_status"], INCIDENT_STATUS_CLOSED)

    def test_incident_stays_open_while_class_is_watch_or_warning(self) -> None:
        scenario = ZoneScenario(self.store)
        scenario.run_until_open()

        seen_non_normal_while_open = False
        while len(scenario.responses) < 120:
            response = scenario.step()
            effective = response["risk"]["risk_class"]
            incident = self.store.latest_incident(ZONE)
            if incident["current_status"] == INCIDENT_STATUS_OPEN:
                self.assertNotEqual(
                    effective,
                    "NORMAL",
                    f"incident closed (or was pending) while {effective} at step {len(scenario.responses)}",
                )
                if effective in ("WATCH", "WARNING", "CRITICAL"):
                    seen_non_normal_while_open = True
            else:
                # once closed, the class that closed it must be NORMAL
                self.assertEqual(effective, "NORMAL")
                break
        self.assertTrue(seen_non_normal_while_open)
        self.assertEqual(self.store.incident_count(ZONE), 1)

    def test_watch_keeps_the_incident_open(self) -> None:
        """A decaying event passes back through WATCH: that is still 'elevated'."""
        scenario = ZoneScenario(self.store)
        scenario.run_until_open()
        scenario.run_until_class("CRITICAL")

        while len(scenario.responses) < 130:
            response = scenario.step()
            incident = self.store.latest_incident(ZONE)
            if response["risk"]["risk_class"] == "WATCH":
                # the zone has left WARNING/CRITICAL but has not returned to NORMAL
                self.assertEqual(
                    incident["current_status"],
                    INCIDENT_STATUS_OPEN,
                    "an incident must stay open while the zone is only down to WATCH",
                )
                return
        self.fail("scenario never decayed back through WATCH")

    def test_incident_history_is_preserved(self) -> None:
        scenario = ZoneScenario(self.store)
        scenario.run_until_open()
        scenario.run_until_normal_after_incident()

        incidents = self.store.list_incidents(ZONE)
        self.assertEqual(len(incidents), 1)
        stored = incidents[0]
        self.assertEqual(stored["current_status"], INCIDENT_STATUS_CLOSED)
        self.assertIsNotNone(stored["closed_at"])

        # still served by the API after closing
        status, payload = dispatch("GET", "/incidents", None, self.store, float(START_EPOCH), {})
        self.assertEqual(status, 200)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["open_count"], 0)
        self.assertEqual(payload["closed_count"], 1)
        self.assertEqual(payload["incidents"][0]["incident_id"], stored["incident_id"])
        self.assertFalse(payload["incidents"][0]["is_open"])


class AlertingTest(unittest.TestCase):
    """Requirements: publish only on meaningful escalation, with a per-severity cooldown."""

    def setUp(self) -> None:
        self.store = LocalStore()
        set_store(self.store)

    def test_sns_publish_invoked_only_on_escalation(self) -> None:
        notifier = RecordingNotifier()
        set_notifier(notifier)
        scenario = ZoneScenario(self.store, notifier=notifier)

        _, incident_at_warning = scenario.run_until_open()
        self.assertEqual(len(notifier.published), 1, "opening at WARNING should alert once")
        self.assertIn("WARNING", notifier.published[0]["subject"])

        scenario.run_until_class("CRITICAL")
        self.assertEqual(len(notifier.published), 2, "the CRITICAL escalation should alert once")
        self.assertIn("ESCALATION", notifier.published[1]["subject"])
        self.assertEqual(notifier.severities, ["WARNING", "CRITICAL"])

        # the whole rest of the storm (flash flood + recession) adds no further alerts
        scenario.run(80)
        self.assertEqual(len(notifier.published), 2)
        self.assertEqual(len(notifier.attempts), 2)

        # ... and the alert log on the incident matches what was published
        incident = self.store.latest_incident(ZONE)
        self.assertEqual(incident["alert_count"], 2)
        self.assertEqual(incident["alert_severities"], ["WARNING", "CRITICAL"])
        self.assertTrue(all(entry["published"] for entry in incident["alerts"]))

    def test_alert_message_is_honest_and_leaks_nothing(self) -> None:
        notifier = RecordingNotifier()
        set_notifier(notifier)
        scenario = ZoneScenario(self.store, notifier=notifier)
        scenario.run_until_open()

        message = notifier.published[0]["message"]
        self.assertIn("simulated", message.lower())
        self.assertIn("heuristic-v1", message)
        self.assertIn("not a calibrated hydrological model", message.lower())
        self.assertNotIn("arn:aws", message.lower())
        self.assertNotIn("FLOODPULSE_INGEST_KEY", message)

    def test_cooldown_suppresses_a_repeat_alert_for_the_same_severity(self) -> None:
        notifier = RecordingNotifier()
        set_notifier(notifier)
        first = ZoneScenario(self.store, notifier=notifier)
        first.run(100)  # complete arc: opens, alerts twice, then closes
        incident = self.store.latest_incident(ZONE)
        self.assertEqual(incident["current_status"], INCIDENT_STATUS_CLOSED)
        published_by_first_storm = len(notifier.published)
        self.assertEqual(published_by_first_storm, 2)

        # a second storm for the same zone, starting just after the first one closed,
        # which is well inside the 600 s cooldown for WARNING
        second = ZoneScenario(
            self.store,
            notifier=notifier,
            start_epoch=int(incident["closed_ts_epoch"]) + 30,
        )
        produced, reopened = second.run_until_open()

        self.assertNotEqual(reopened["incident_id"], incident["incident_id"])
        self.assertEqual(reopened["current_status"], INCIDENT_STATUS_OPEN)
        self.assertEqual(
            len(notifier.published),
            published_by_first_storm,
            "the repeat WARNING must be suppressed by the cooldown",
        )
        self.assertEqual(
            len(notifier.attempts),
            published_by_first_storm,
            "a suppressed alert must not even be attempted",
        )

        suppressed = [r for r in produced if r["alert"]["reason"] == "cooldown"]
        self.assertTrue(suppressed, "expected at least one cooldown suppression")
        self.assertEqual(suppressed[-1]["alert"]["severity"], "WARNING")
        self.assertGreater(suppressed[-1]["alert"]["cooldown_remaining_seconds"], 0)
        self.assertTrue(all(r["alert"]["published"] is False for r in produced))

        marker = self.store.get_alert_marker(ZONE)
        self.assertIn("WARNING", marker["last_alert_by_severity"])

    def test_cooldown_expires(self) -> None:
        notifier = RecordingNotifier()
        set_notifier(notifier)
        first = ZoneScenario(self.store, notifier=notifier)
        first.run(100)
        incident = self.store.latest_incident(ZONE)
        published_by_first_storm = len(notifier.published)

        # start the next storm beyond the cooldown measured from the first alert
        second = ZoneScenario(
            self.store,
            notifier=notifier,
            start_epoch=int(incident["opened_ts_epoch"]) + ALERT_COOLDOWN + 1,
        )
        produced, _ = second.run_until_open()

        self.assertGreater(
            len(notifier.published),
            published_by_first_storm,
            "once the cooldown has expired the zone may alert again",
        )
        self.assertTrue(all(r["alert"].get("reason") != "cooldown" for r in produced))
        self.assertTrue(any(r["alert"]["published"] for r in produced))

    def test_cooldown_is_per_severity(self) -> None:
        """A CRITICAL escalation alerts even though WARNING just alerted."""
        notifier = RecordingNotifier()
        set_notifier(notifier)
        scenario = ZoneScenario(self.store, notifier=notifier)
        scenario.run_until_open()
        scenario.run_until_class("CRITICAL")
        severities = notifier.severities
        self.assertEqual(severities, ["WARNING", "CRITICAL"])

        marker = self.store.get_alert_marker(ZONE)
        self.assertEqual(set(marker["last_alert_by_severity"]), {"WARNING", "CRITICAL"})

    def test_no_alert_when_no_topic_is_configured(self) -> None:
        set_notifier(NoopNotifier())
        scenario = ZoneScenario(self.store)
        _, incident = scenario.run_until_open()
        # local/no-topic mode: the incident is still tracked, alerting is simply off
        self.assertEqual(incident["current_status"], INCIDENT_STATUS_OPEN)
        self.assertEqual(incident["alert_count"], 0)

    def test_publish_failure_is_recorded_and_ingestion_survives(self) -> None:
        notifier = RecordingNotifier(publish_ok=False)
        set_notifier(notifier)
        scenario = ZoneScenario(self.store, notifier=notifier)
        produced, incident = scenario.run_until_open()

        self.assertEqual(incident["current_status"], INCIDENT_STATUS_OPEN)
        self.assertEqual(incident["alert_count"], 0)
        self.assertEqual(len(incident["alerts"]), 1)
        self.assertFalse(incident["alerts"][0]["published"])
        self.assertEqual(incident["alerts"][0]["reason"], "publish_failed")
        self.assertFalse(produced[-1]["alert"]["published"])
        # a failed publish must not advance the cooldown marker
        self.assertIsNone(self.store.get_alert_marker(ZONE))

    def test_exploding_notifier_does_not_break_ingestion(self) -> None:
        set_notifier(ExplodingNotifier())
        scenario = ZoneScenario(self.store, notifier=ExplodingNotifier())
        produced, incident = scenario.run_until_open()
        self.assertEqual(incident["current_status"], INCIDENT_STATUS_OPEN)
        self.assertEqual(produced[-1]["incident"]["action"], ACTION_OPENED)

    def test_incident_tracking_failure_does_not_lose_the_reading(self) -> None:
        store = ExplodingIncidentStore(LocalStore())
        status, payload = dispatch(
            "POST",
            "/readings",
            _one_reading_json(),
            store,
            float(START_EPOCH),
            {},
            {},
            NoopNotifier(),
            600,
        )
        self.assertEqual(status, 202)
        self.assertEqual(payload["incident"]["action"], "tracking_failed")
        self.assertFalse(payload["alert"]["published"])
        self.assertEqual(payload["status"], "accepted")

    def test_alert_marker_helper_math(self) -> None:
        marker = build_alert_marker(zone_id=ZONE, severity="WARNING", now_epoch=1_000, message_id="m1")
        self.assertEqual(marker["sk"], ALERT_MARKER_SORT_KEY)
        self.assertEqual(cooldown_remaining_seconds(marker, "WARNING", 1_100, 600), 500)
        self.assertEqual(cooldown_remaining_seconds(marker, "WARNING", 1_600, 600), 0)
        self.assertEqual(cooldown_remaining_seconds(marker, "CRITICAL", 1_100, 600), 0)
        self.assertEqual(cooldown_remaining_seconds(None, "WARNING", 1_100, 600), 0)
        self.assertEqual(cooldown_remaining_seconds(marker, "WARNING", 1_100, 0), 0)

        second = build_alert_marker(
            zone_id=ZONE, severity="CRITICAL", now_epoch=1_200, previous=marker, message_id="m2"
        )
        self.assertEqual(set(second["last_alert_by_severity"]), {"WARNING", "CRITICAL"})
        self.assertEqual(second["last_alert_by_severity"]["WARNING"]["message_id"], "m1")

    def test_alerting_view_exposes_no_arn(self) -> None:
        view = alerting_view(True, 600)
        self.assertEqual(view, {"enabled": True, "severities": ["WARNING", "CRITICAL"], "cooldown_seconds": 600})
        blob = str(view).lower()
        self.assertNotIn("arn:aws", blob)
        self.assertNotIn("topic", blob)
        self.assertNotIn("floodpulse_sns_topic_arn", blob)


class IncidentViewTest(unittest.TestCase):
    def test_open_and_closed_durations(self) -> None:
        item = {
            "incident_id": "inc-x",
            "zone_id": ZONE,
            "zone_name": "Riverside North",
            "severity": "CRITICAL",
            "severity_at_open": "WARNING",
            "current_status": INCIDENT_STATUS_OPEN,
            "opened_at": "2025-10-09T08:55:45Z",
            "opened_ts_epoch": 1_000,
            "closed_at": None,
            "closed_ts_epoch": None,
            "risk_score_at_open": 0.65,
            "top_driver": "water_level_m",
            "top_driver_label": "Water level",
            "severity_changes": [{"from": "WARNING", "to": "CRITICAL"}],
            "alert_count": 2,
            "alert_severities": ["WARNING", "CRITICAL"],
        }
        opening = incident_view(item, 1_300)
        self.assertEqual(opening["duration_seconds"], 300)
        self.assertTrue(opening["is_open"])
        self.assertEqual(opening["escalation_count"], 1)
        self.assertTrue(opening["simulated"])
        self.assertEqual(opening["top_driver_label"], "Water level")

        closing = incident_view(dict(item, current_status=INCIDENT_STATUS_CLOSED, closed_ts_epoch=1_500), 9_999)
        self.assertEqual(closing["duration_seconds"], 500)
        self.assertFalse(closing["is_open"])


def _one_reading_json() -> str:
    """A single calm reading in JSON form, for direct dispatch() calls."""
    import json

    from floodpulse.simulator import SensorSimulator

    return json.dumps(
        SensorSimulator(ZONE, seed=20260214, start_epoch=START_EPOCH).step()
    )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

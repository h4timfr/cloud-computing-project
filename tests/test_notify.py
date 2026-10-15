"""SNS notifier tests (M3): message content, subject hygiene, failure handling.

No AWS calls: the SNS client is injected, and the ARN used here is a made-up one.
"""

from __future__ import annotations

import json
import unittest

from floodpulse.notify import (
    ALERT_KIND_ESCALATED,
    ALERT_KIND_OPENED,
    ALERT_KIND_TEST,
    NoopNotifier,
    SnsNotifier,
    alert_subject,
    build_alert_message,
    build_notifier,
    notifier_for_env,
    sanitise_subject,
)

TOPIC_ARN = "arn:aws:sns:eu-north-1:725934295966:floodpulse-alerts"
INCIDENT = {
    "incident_id": "inc-riverside-north-20251009T085545Z",
    "zone_id": "zone-riverside-north",
    "zone_name": "Riverside North",
    "severity": "CRITICAL",
    "severity_at_open": "WARNING",
    "current_status": "OPEN",
    "opened_at": "2025-10-09T08:55:45Z",
    "closed_at": None,
    "duration_seconds": None,
    "risk_score_at_open": 0.66,
    "risk_score_peak": 1.0,
    "last_risk_score": 0.99,
    "last_risk_class": "CRITICAL",
    "top_driver": "water_level_m",
    "top_driver_label": "Water level",
    "model": "heuristic-v1",
}


class FakeSnsClient:
    def __init__(self, fail: Exception | None = None) -> None:
        self.fail = fail
        self.calls: list[dict[str, str]] = []

    def publish(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail is not None:
            raise self.fail
        return {"MessageId": "11111111-2222-3333-4444-555555555555"}


class BuildNotifierTest(unittest.TestCase):
    def test_no_topic_means_noop(self) -> None:
        for source in ({}, {"FLOODPULSE_SNS_TOPIC_ARN": ""}, {"FLOODPULSE_SNS_TOPIC_ARN": "   "}):
            notifier = notifier_for_env(source)
            self.assertIsInstance(notifier, NoopNotifier)
            self.assertFalse(notifier.enabled)
            self.assertEqual(notifier.publish("s", "m"), {"published": False, "reason": "not_configured"})

    def test_topic_from_environment_enables_publishing(self) -> None:
        notifier = notifier_for_env({"FLOODPULSE_SNS_TOPIC_ARN": TOPIC_ARN})
        self.assertIsInstance(notifier, SnsNotifier)
        self.assertTrue(notifier.enabled)

    def test_explicit_arn_wins_and_is_never_returned(self) -> None:
        notifier = build_notifier(TOPIC_ARN, client=FakeSnsClient())
        self.assertTrue(notifier.enabled)
        self.assertNotIn(TOPIC_ARN, json.dumps(notifier.publish("subject", "message")))


class SnsNotifierTest(unittest.TestCase):
    def test_publish_passes_topic_subject_and_message(self) -> None:
        client = FakeSnsClient()
        notifier = SnsNotifier(TOPIC_ARN, client=client)
        result = notifier.publish("hello", "world")

        self.assertEqual(result, {"published": True, "message_id": "11111111-2222-3333-4444-555555555555"})
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["TopicArn"], TOPIC_ARN)
        self.assertEqual(client.calls[0]["Subject"], "hello")
        self.assertEqual(client.calls[0]["Message"], "world")

    def test_publish_failure_is_reported_not_raised(self) -> None:
        client = FakeSnsClient(fail=RuntimeError(f"AuthorizationError for {TOPIC_ARN}"))
        notifier = SnsNotifier(TOPIC_ARN, client=client)
        result = notifier.publish("hello", "world")

        self.assertFalse(result["published"])
        self.assertEqual(result["reason"], "publish_failed")
        self.assertIn("AuthorizationError", result["detail"])

    def test_topic_arn_is_redacted_from_error_detail(self) -> None:
        client = FakeSnsClient(fail=RuntimeError(f"not authorized to publish to {TOPIC_ARN}"))
        notifier = SnsNotifier(TOPIC_ARN, client=client)
        result = notifier.publish("hello", "world")
        self.assertNotIn(TOPIC_ARN, json.dumps(result))
        self.assertIn("<alert-topic>", result["detail"])

    def test_redact_handles_other_accounts_and_regions(self) -> None:
        notifier = SnsNotifier(TOPIC_ARN, client=FakeSnsClient())
        text = notifier.redact("failed for arn:aws:sns:us-east-1:999999999999:other-topic now")
        self.assertEqual(text, "failed for <alert-topic> now")


class SubjectTest(unittest.TestCase):
    def test_subject_variants(self) -> None:
        self.assertEqual(
            alert_subject(ALERT_KIND_OPENED, "Riverside North", "WARNING"),
            "FloodPulse WARNING: Riverside North (simulated flood risk)",
        )
        self.assertEqual(
            alert_subject(ALERT_KIND_ESCALATED, "Harbor East", "CRITICAL"),
            "FloodPulse ESCALATION to CRITICAL: Harbor East (simulated)",
        )
        self.assertIn("test alert", alert_subject(ALERT_KIND_TEST, "Old Town Canal", "WARNING"))

    def test_subject_is_ascii_single_line_and_bounded(self) -> None:
        cleaned = sanitise_subject("FloodPulse ⚠️ CRITICAL:\nRiverside\tNorth — flood")
        self.assertTrue(cleaned.isascii())
        self.assertNotIn("\n", cleaned)
        self.assertNotIn("\t", cleaned)
        self.assertLessEqual(len(cleaned), 100)

        long_subject = sanitise_subject("x" * 200)
        self.assertEqual(len(long_subject), 100)
        self.assertTrue(long_subject.endswith("..."))


class MessageTest(unittest.TestCase):
    def test_message_is_valid_json_with_the_incident_facts(self) -> None:
        payload = json.loads(build_alert_message(ALERT_KIND_ESCALATED, INCIDENT))

        self.assertEqual(payload["source"], "floodpulse")
        self.assertEqual(payload["kind"], "incident_escalated")
        self.assertEqual(payload["severity"], "CRITICAL")
        self.assertEqual(payload["zone"], {"zone_id": "zone-riverside-north", "name": "Riverside North"})
        self.assertEqual(payload["incident"]["incident_id"], INCIDENT["incident_id"])
        self.assertEqual(payload["incident"]["top_driver_label"], "Water level")
        self.assertEqual(payload["incident"]["risk_score_now"], 0.99)
        self.assertTrue(payload["simulated"])
        self.assertEqual(payload["data_classification"], "simulated")

    def test_message_is_explicitly_simulated_and_heuristic(self) -> None:
        payload = json.loads(build_alert_message(ALERT_KIND_OPENED, INCIDENT))
        self.assertIn("SIMULATED", payload["disclaimer"])
        self.assertIn("not a calibrated hydrological model", payload["model_note"])
        self.assertIn("SIMULATED", payload["action"])

    def test_message_contains_no_arn_or_secret(self) -> None:
        with_topic = dict(INCIDENT, topic_arn=TOPIC_ARN, ingest_key="super-secret-key")
        blob = build_alert_message(ALERT_KIND_OPENED, with_topic)
        self.assertNotIn("arn:aws", blob.lower())
        self.assertNotIn("super-secret-key", blob)
        self.assertNotIn("FLOODPULSE_INGEST_KEY", blob)

    def test_message_tolerates_missing_fields(self) -> None:
        payload = json.loads(build_alert_message(ALERT_KIND_OPENED, {"zone_name": "X"}))
        self.assertEqual(payload["severity"], None)
        self.assertIsNone(payload["incident"]["risk_score_at_open"])
        self.assertEqual(payload["model"], "heuristic-v1")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

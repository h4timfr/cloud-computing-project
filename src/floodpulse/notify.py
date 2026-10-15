"""SNS alert delivery (M3).

Design rules:

* **Opt-in by configuration.** With no ``FLOODPULSE_SNS_TOPIC_ARN`` the notifier is a
  no-op, so local runs and the test suite never touch AWS.
* **Failures never break ingestion.** ``publish`` reports failure in its return value
  instead of raising: a reading that was stored successfully must still be accepted even
  if the alert cannot be delivered.
* **The topic ARN never leaves the process.** It is not returned to clients, and any
  AWS error message that embeds it is redacted first.
* Messages say plainly that the data is simulated and that the score is a heuristic.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from .config import SIMULATION_DISCLAIMER, SNS_TOPIC_ENV
from .risk import MODEL_NOTE

ALERT_KIND_OPENED = "incident_opened"
ALERT_KIND_ESCALATED = "incident_escalated"
ALERT_KIND_TEST = "test_notification"

SUBJECT_MAX_LENGTH = 100
ARN_PATTERN = re.compile(r"arn:aws[a-z-]*:sns:[^\s\"']+", re.IGNORECASE)
NON_ASCII = re.compile(r"[^\x20-\x7e]+")


class NoopNotifier:
    """Used when no topic is configured (local runs, tests, and misconfigured deploys)."""

    def __init__(self, reason: str = "not_configured") -> None:
        self.reason = reason

    @property
    def enabled(self) -> bool:
        return False

    def publish(self, subject: str, message: str) -> dict[str, Any]:
        return {"published": False, "reason": self.reason}


class SnsNotifier:
    """Publishes one message per alert to a single SNS topic."""

    def __init__(self, topic_arn: str, client: Any = None) -> None:
        self._topic_arn = str(topic_arn)
        self._client = client

    @property
    def enabled(self) -> bool:
        return True

    def _sns(self) -> Any:
        if self._client is None:
            import boto3  # lazy: local runs must not require boto3

            self._client = boto3.client("sns")
        return self._client

    def redact(self, text: str) -> str:
        """Replace anything that looks like a topic ARN (AWS errors often embed it)."""
        return ARN_PATTERN.sub("<alert-topic>", str(text))

    def publish(self, subject: str, message: str) -> dict[str, Any]:
        try:
            response = self._sns().publish(
                TopicArn=self._topic_arn,
                Subject=subject,
                Message=message,
            )
        except Exception as exc:  # noqa: BLE001 - reported, never raised: ingestion must survive
            return {
                "published": False,
                "reason": "publish_failed",
                "detail": self.redact(f"{exc.__class__.__name__}: {exc}")[:200],
            }
        return {"published": True, "message_id": str(response.get("MessageId") or "")}


def build_notifier(topic_arn: str | None = None, client: Any = None) -> NoopNotifier | SnsNotifier:
    """Choose the notifier for this process from an explicit ARN or the environment."""
    arn = (topic_arn if topic_arn is not None else "").strip()
    if not arn:
        return NoopNotifier()
    return SnsNotifier(arn, client=client)


def notifier_for_env(env: Mapping[str, Any]) -> NoopNotifier | SnsNotifier:
    """Build the notifier from an environment mapping without importing boto3 early."""
    return build_notifier(str(env.get(SNS_TOPIC_ENV, "") or ""))


def sanitise_subject(text: str) -> str:
    """SNS subjects must be ASCII, single-line and at most 100 characters."""
    cleaned = NON_ASCII.sub(" ", str(text)).replace("\r", " ").replace("\n", " ")
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > SUBJECT_MAX_LENGTH:
        cleaned = cleaned[: SUBJECT_MAX_LENGTH - 3].rstrip() + "..."
    return cleaned


def alert_subject(kind: str, zone_name: str, severity: str) -> str:
    """Human-readable subject line for an incident alert."""
    if kind == ALERT_KIND_ESCALATED:
        return sanitise_subject(f"FloodPulse ESCALATION to {severity}: {zone_name} (simulated)")
    if kind == ALERT_KIND_TEST:
        return sanitise_subject(f"FloodPulse test alert: {zone_name} (simulated data)")
    return sanitise_subject(f"FloodPulse {severity}: {zone_name} (simulated flood risk)")


def _number(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def build_alert_message(kind: str, incident: Mapping[str, Any]) -> str:
    """JSON message body for an incident alert.

    Contains only incident facts plus explicit statements that the data is simulated and
    the score is a heuristic. It never contains the topic ARN or any ingestion secret.
    """
    payload = {
        "source": "floodpulse",
        "kind": kind,
        "simulated": True,
        "data_classification": "simulated",
        "severity": incident.get("severity"),
        "zone": {
            "zone_id": incident.get("zone_id"),
            "name": incident.get("zone_name"),
        },
        "incident": {
            "incident_id": incident.get("incident_id"),
            "current_status": incident.get("current_status"),
            "opened_at": incident.get("opened_at"),
            "closed_at": incident.get("closed_at"),
            "duration_seconds": incident.get("duration_seconds"),
            "risk_score_at_open": _number(incident.get("risk_score_at_open")),
            "risk_score_now": _number(incident.get("last_risk_score")),
            "risk_score_peak": _number(incident.get("risk_score_peak")),
            "risk_class_now": incident.get("last_risk_class"),
            "top_driver": incident.get("top_driver"),
            "top_driver_label": incident.get("top_driver_label"),
        },
        "model": incident.get("model", "heuristic-v1"),
        "model_note": MODEL_NOTE,
        "disclaimer": SIMULATION_DISCLAIMER,
        "action": (
            "Automated demo notification. The zone's heuristic risk class crossed into an "
            "alerting band. No real flood is being measured; all sensor values are SIMULATED."
        ),
    }
    return json.dumps(payload, indent=2, sort_keys=False, default=str)

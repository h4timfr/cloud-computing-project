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

from .config import (
    DATA_CLASSIFICATION_MIXED,
    DATA_CLASSIFICATION_SIMULATED,
    MIXED_DATA_DISCLAIMER,
    SIMULATION_DISCLAIMER,
    SNS_TOPIC_ENV,
)
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

    def subscribe_email(self, address: str) -> dict[str, Any]:
        """No topic is configured, so no subscription can be requested.

        Reported as ``not_configured`` rather than raised: the route turns it into "we
        couldn't create the subscription request right now", which is exactly true.
        """
        return {"requested": False, "reason": self.reason}


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

    def subscribe_email(self, address: str) -> dict[str, Any]:
        """Ask SNS to subscribe one email address to this topic.

        The topic and the protocol are fixed here, in trusted code: neither is a parameter,
        so no request can reach this call with a topic or a protocol of its choosing. The
        returned ARN is *not* reported back to the caller - whether the address was new,
        already pending or already confirmed is exactly what the endpoint must not reveal -
        so only the outcome (requested / refused) leaves this method.

        ``ReturnSubscriptionArn=True`` is what makes a *new* subscription's ARN visible
        (otherwise SNS returns ``"pending confirmation"`` as a string). The confirmation
        message itself is sent by AWS; nothing here composes or sends mail.
        """
        try:
            response = self._sns().subscribe(
                TopicArn=self._topic_arn,
                Protocol="email",
                Endpoint=str(address),
                ReturnSubscriptionArn=True,
            )
        except Exception as exc:  # noqa: BLE001 - reported, never raised: the route answers with a friendly message
            return {
                "requested": False,
                "reason": "subscribe_failed",
                "detail": self.redact(f"{exc.__class__.__name__}: {exc}")[:200],
            }
        arn = str(response.get("SubscriptionArn") or "")
        return {
            "requested": True,
            "state": subscription_state_for_arn(arn),
        }


def build_notifier(topic_arn: str | None = None, client: Any = None) -> NoopNotifier | SnsNotifier:
    """Choose the notifier for this process from an explicit ARN or the environment."""
    arn = (topic_arn if topic_arn is not None else "").strip()
    if not arn:
        return NoopNotifier()
    return SnsNotifier(arn, client=client)


def notifier_for_env(env: Mapping[str, Any]) -> NoopNotifier | SnsNotifier:
    """Build the notifier from an environment mapping without importing boto3 early."""
    return build_notifier(str(env.get(SNS_TOPIC_ENV, "") or ""))


#: SNS reports a subscription that nobody has confirmed yet as this literal string instead
#: of an ARN, because a pending subscription has no ARN. It is mapped to a name here so the
#: rest of the code never compares against a magic string, and so it is never returned to a
#: client (that would leak whether an address is already pending).
PENDING_CONFIRMATION_MARKER = "pending confirmation"

#: The two states a successful ``Subscribe`` call can leave an endpoint in, from this side:
#: waiting for the visitor to click the confirmation link, or already confirmed. The route
#: answers identically for both; the distinction is only ever used in logs and metrics.
SUBSCRIPTION_PENDING = "pending_confirmation"
SUBSCRIPTION_CONFIRMED = "confirmed"


def subscription_state_for_arn(value: Any) -> str:
    """Classify what ``Subscribe`` returned for an email endpoint.

    A real ARN means the endpoint was already confirmed (or was just created and confirmed);
    SNS's ``"pending confirmation"`` sentinel means AWS has sent the confirmation message
    and is waiting. Anything else is treated as pending, which is the conservative reading
    for a system that must never claim delivery it cannot prove.
    """
    text = str(value or "").strip()
    if text.lower() == PENDING_CONFIRMATION_MARKER:
        return SUBSCRIPTION_PENDING
    if text.startswith("arn:"):
        return SUBSCRIPTION_CONFIRMED
    return SUBSCRIPTION_PENDING


def sanitise_subject(text: str) -> str:
    """SNS subjects must be ASCII, single-line and at most 100 characters."""
    cleaned = NON_ASCII.sub(" ", str(text)).replace("\r", " ").replace("\n", " ")
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > SUBJECT_MAX_LENGTH:
        cleaned = cleaned[: SUBJECT_MAX_LENGTH - 3].rstrip() + "..."
    return cleaned


def alert_provenance(incident: Mapping[str, Any]) -> str:
    """The provenance of the data that raised this incident ("simulated" or "mixed").

    Incidents carry the classification of the reading that opened them, so a live-weather
    incident over real rainfall is ``mixed``. Defaulting to "simulated" when the field is
    absent keeps the conservative direction: never claim real measurements we cannot prove.
    """
    value = str(incident.get("data_classification") or "").strip().lower()
    if value == DATA_CLASSIFICATION_MIXED:
        return value
    return DATA_CLASSIFICATION_SIMULATED


def is_simulated_only(incident: Mapping[str, Any]) -> bool:
    return alert_provenance(incident) == DATA_CLASSIFICATION_SIMULATED


def alert_disclaimer(incident: Mapping[str, Any]) -> str:
    return SIMULATION_DISCLAIMER if is_simulated_only(incident) else MIXED_DATA_DISCLAIMER


def alert_subject(kind: str, zone_name: str, severity: str, *, simulated: bool = True) -> str:
    """Human-readable subject line for an incident alert.

    The provenance marker follows the incident: a live-weather incident is never labelled
    "simulated", and a fully simulated one keeps its full simulation wording.
    """
    if kind == ALERT_KIND_ESCALATED:
        tail = "(simulated)" if simulated else "(live rainfall, simulated hydraulics)"
        return sanitise_subject(f"FloodPulse ESCALATION to {severity}: {zone_name} {tail}")
    if kind == ALERT_KIND_TEST:
        tail = "(simulated data)" if simulated else "(live rainfall mode)"
        return sanitise_subject(f"FloodPulse test alert: {zone_name} {tail}")
    tail = "(simulated flood risk)" if simulated else "(live rainfall, simulated hydraulics)"
    return sanitise_subject(f"FloodPulse {severity}: {zone_name} {tail}")


def _number(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def build_alert_message(kind: str, incident: Mapping[str, Any]) -> str:
    """JSON message body for an incident alert.

    Contains only incident facts plus explicit statements about where the data came from
    and that the score is a heuristic. The provenance is the incident's own: a live-weather
    incident is reported as ``mixed`` rather than being mislabelled as fully simulated.
    It never contains the topic ARN or any ingestion secret.
    """
    provenance = alert_provenance(incident)
    simulated_only = provenance == DATA_CLASSIFICATION_SIMULATED
    payload = {
        "source": "floodpulse",
        "kind": kind,
        "simulated": simulated_only,
        "data_classification": provenance,
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
        "disclaimer": alert_disclaimer(incident),
        "action": (
            "Automated notification. The zone's heuristic risk class crossed into an alerting "
            "band. " + (
                "No real flood is being measured; all sensor values are SIMULATED."
                if simulated_only else
                "Rainfall intensity and soil saturation are real observations from the "
                "Open-Meteo weather service; drain level, water level and flow rate are "
                "SIMULATED. Nothing here is an official flood warning."
            )
        ),
    }
    return json.dumps(payload, indent=2, sort_keys=False, default=str)

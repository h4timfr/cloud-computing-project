"""Shared helpers for the test suite (no AWS, no network, no wall clock).

Kept deliberately small: a recording notifier so alert behaviour can be asserted, and a
stateful scenario driver that pushes simulated readings through the real handler
in-process (one simulator per scenario, so the phase arc actually advances).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from floodpulse.handler import dispatch
from floodpulse.notify import NoopNotifier
from floodpulse.simulator import simulators_for_all_zones

SEED = 20260214
START_EPOCH = 1_760_000_000  # fixed clock, unrelated to the machine's clock
ALERT_COOLDOWN = 600


class RecordingNotifier:
    """Notifier stand-in that records what would have been published."""

    def __init__(self, publish_ok: bool = True) -> None:
        self.publish_ok = publish_ok
        self.published: list[dict[str, str]] = []
        self.attempts: list[dict[str, str]] = []

    @property
    def enabled(self) -> bool:
        return True

    def publish(self, subject: str, message: str) -> dict[str, Any]:
        self.attempts.append({"subject": subject, "message": message})
        if not self.publish_ok:
            return {"published": False, "reason": "publish_failed", "detail": "rejected"}
        self.published.append({"subject": subject, "message": message})
        return {"published": True, "message_id": f"msg-{len(self.published)}"}

    @property
    def severities(self) -> list[str]:
        """Severities named in the published subjects, in publication order."""
        severities: list[str] = []
        for entry in self.published:
            for name in ("CRITICAL", "WARNING", "WATCH", "NORMAL"):
                if name in entry["subject"]:
                    severities.append(name)
                    break
        return severities


class ExplodingNotifier:
    """Notifier that raises, proving ingestion survives an alerting failure."""

    @property
    def enabled(self) -> bool:
        return True

    def publish(self, subject: str, message: str) -> dict[str, Any]:
        raise RuntimeError("sns exploded")


class ExplodingIncidentStore:
    """Wraps a store so incident tracking fails, proving readings are still accepted."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def latest_incident(self, zone_id: str) -> Any:
        raise RuntimeError("incident read exploded")


def epoch_of(iso_timestamp: str) -> int:
    return int(datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00")).timestamp())


class ZoneScenario:
    """Drives one zone's simulator through the handler, keeping its phase arc going."""

    def __init__(
        self,
        store: Any,
        zone_id: str = "zone-riverside-north",
        notifier: Any = None,
        cooldown_seconds: int = ALERT_COOLDOWN,
        seed: int = SEED,
        start_epoch: int = START_EPOCH,
    ) -> None:
        self.zone_id = zone_id
        self.store = store
        # default to a local no-op so scenarios never depend on the process-wide notifier
        self.notifier = NoopNotifier() if notifier is None else notifier
        self.cooldown_seconds = cooldown_seconds
        self.start_epoch = start_epoch
        self._simulator = next(
            simulator
            for simulator in simulators_for_all_zones(seed=seed, start_epoch=start_epoch)
            if simulator.zone_id == zone_id
        )
        self.responses: list[dict[str, Any]] = []

    def step(self) -> dict[str, Any]:
        """Post one simulated reading and return the API response body."""
        payload = self._simulator.step()
        status, body = dispatch(
            "POST",
            "/readings",
            json.dumps(payload),
            self.store,
            float(epoch_of(payload["timestamp"])),
            {},
            {},
            self.notifier,
            self.cooldown_seconds,
        )
        assert status == 202, f"expected 202, got {status}: {body}"
        self.responses.append(body)
        return body

    def run(self, steps: int) -> list[dict[str, Any]]:
        return [self.step() for _ in range(steps)]

    def actions(self) -> list[str]:
        """Incident actions reported so far, skipping the uneventful ones."""
        return [
            response["incident"]["action"]
            for response in self.responses
            if response["incident"]["action"] != "none"
        ]

    def active_incident(self) -> dict[str, Any] | None:
        """The newest incident record as stored."""
        return self.store.latest_incident(self.zone_id)

    def run_until_open(self, max_steps: int = 60) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Advance until an incident is OPEN; return the responses produced here and it.

        The step at which a zone crosses into WARNING differs per zone and seed, so tests
        walk forward rather than hard-coding an index.
        """
        produced: list[dict[str, Any]] = []
        while len(self.responses) < max_steps:
            produced.append(self.step())
            incident = self.active_incident()
            if incident is not None and incident["current_status"] == "OPEN":
                return produced, incident
        raise AssertionError(f"no incident opened within {max_steps} readings for {self.zone_id}")

    def run_until_class(self, risk_class: str, max_steps: int = 120) -> list[dict[str, Any]]:
        """Advance until the *effective* class reaches ``risk_class``."""
        produced: list[dict[str, Any]] = []
        while len(self.responses) < max_steps:
            response = self.step()
            produced.append(response)
            if response["risk"]["risk_class"] == risk_class:
                return produced
        raise AssertionError(f"zone never reached {risk_class} within {max_steps} readings")

    def run_until_normal_after_incident(self, max_steps: int = 160) -> list[dict[str, Any]]:
        """Advance until the open incident has closed."""
        produced: list[dict[str, Any]] = []
        while len(self.responses) < max_steps:
            response = self.step()
            produced.append(response)
            incident = self.active_incident()
            if incident is not None and incident["current_status"] == "CLOSED":
                return produced
        raise AssertionError(f"incident never closed within {max_steps} readings")

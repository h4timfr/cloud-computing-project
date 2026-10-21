"""The single FloodPulse Lambda handler (MVP: one function, all routes).

Routes:

* ``POST /readings``                -- validate, score, store (requires the ``x-fp-key`` header),
                                       then open/escalate/close an incident and alert if warranted
* ``GET  /zones``                   -- latest state per zone + ranking (public)
* ``GET  /zones/{zone_id}/history`` -- timestamped history for one zone (public)
* ``GET  /incidents``               -- flood incident log, newest first (public)
* ``POST /subscriptions``           -- request an email alert subscription (public, throttled)

The read endpoints are deliberately unauthenticated: the dashboard in S3 needs them and
they expose only simulated data. ``POST /readings`` is protected by the shared secret in
``FLOODPULSE_INGEST_KEY`` (see ``auth.py``); the secret is never accepted, required or
revealed on the read routes. The SNS topic ARN is never returned to any client, and
``POST /subscriptions`` accepts no topic, protocol or region from the caller: those come
from the environment, and the only thing the route stores is a hashed attempt counter.

Still deferred (see ``config.DEFERRED_FEATURES``): time-to-threshold estimation.

``dispatch()`` is the pure, AWS-free entry point used by tests and by
``tools/local_demo.py``. ``lambda_handler()`` only adapts an API Gateway event.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from .auth import check_ingest
from .config import (
    ALERT_COOLDOWN_ENV,
    ALERT_COOLDOWN_MAX_SECONDS,
    ALERT_COOLDOWN_MIN_SECONDS,
    ALERT_COOLDOWN_SECONDS,
    DATA_CLASSIFICATION_MIXED,
    DATA_CLASSIFICATION_SIMULATED,
    DEFERRED_FEATURES,
    HISTORY_DEFAULT_MINUTES,
    HISTORY_MAX_MINUTES,
    HYDRAULICS_NOTE,
    INCIDENT_DEFAULT_LIMIT,
    INCIDENT_MAX_LIMIT,
    INCIDENT_STATUS_OPEN,
    MAX_HISTORY_POINTS,
    METRIC_ORDER,
    METRIC_SOURCES_LIVE,
    METRIC_SOURCES_SIMULATED,
    MIXED_DATA_DISCLAIMER,
    RISK_MODEL_ID,
    SIMULATION_DISCLAIMER,
    SMOOTHING_WINDOW,
    SOURCE_MODEL_DERIVED,
    SOURCE_REAL,
    SOURCE_SIMULATED,
    STALE_READING_SECONDS,
    SUBSCRIPTION_MESSAGE_CONFIRMATION_REQUIRED,
    SUBSCRIPTION_MESSAGE_RATE_LIMITED,
    SUBSCRIPTION_MESSAGE_UNAVAILABLE,
    SUBSCRIPTION_RATE_LIMIT,
    SUBSCRIPTION_RATE_TTL_SECONDS,
    SUBSCRIPTION_SOURCE_RATE_LIMIT,
    SUBSCRIPTION_STATUS_CONFIRMATION_REQUIRED,
    SUBSCRIPTION_STATUS_RATE_LIMITED,
    SUBSCRIPTION_STATUS_UNAVAILABLE,
    SUBSCRIPTION_WINDOW_SECONDS,
    TREND_LOOKBACK_SECONDS,
    TREND_LOOKBACK_TOLERANCE_SECONDS,
    WEATHER_PROVIDER,
    WEATHER_REFRESH_ENV,
    WEATHER_REFRESH_MAX_SECONDS,
    WEATHER_REFRESH_MIN_SECONDS,
    WEATHER_REFRESH_SECONDS,
    ZONE_DEFINITIONS,
    ZONE_IDS,
    ZONE_MODE_DEMO,
    ZONE_MODE_LIVE,
    data_classification_for_mode,
    thresholds_for,
    city_scope_for,
    coverage_summary,
    zone_definition,
    zone_definitions_for_mode,
    zone_mode_for_env,
)
from .live_weather import (
    WEATHER_OBSERVATION_INTERVAL_SECONDS,
    RefreshOutcome,
    attempt_age_seconds,
    cache_age_seconds,
    observation_age_seconds,
    observation_is_current,
    refresh_live_weather,
    weather_view,
)
from .live_weather import weather_zones as live_weather_zones
from .incidents import (
    ACTION_CLOSED,
    ACTION_ESCALATED,
    ACTION_NONE,
    ACTION_OPENED,
    INCIDENT_KEY_ATTEMPTS,
    alerting_view,
    build_alert_entry,
    build_alert_marker,
    cooldown_remaining_seconds,
    decide_transition,
    incident_open_pointer,
    incident_suffix_id,
    incident_suffix_key,
    incident_view,
    record_alert,
)
from .notify import (
    ALERT_KIND_ESCALATED,
    ALERT_KIND_OPENED,
    NoopNotifier,
    alert_subject,
    build_alert_message,
    is_simulated_only,
    notifier_for_env,
    zone_geography,
)
from .risk import (
    MODEL_NOTE,
    RiskInputError,
    apply_hysteresis,
    classify,
    rolling_mean_metrics,
    score_zone,
)
from .storage import (
    READING_DUPLICATE,
    READING_STALE,
    READING_STORED,
    LocalStore,
    WriteOutcome,
)
from .subscriptions import (
    parse_subscription_request,
    rate_limit_key,
    redact_email,
    redact_source,
    source_rate_limit_key,
    window_expiry,
    window_sort_key,
)
from .validation import ValidationError, validate_body_size, validate_reading
LOGGER = logging.getLogger("floodpulse.handler")


def _configure_logging(logger: logging.Logger = LOGGER) -> None:
    """Make this module's INFO lines visible in CloudWatch.

    Lambda leaves the root logger at WARNING, so ``LOGGER.info`` would be dropped silently
    and a successful weather refresh would leave no trace in the logs. The handler is added
    only when the logger has none, and propagation is switched off so a line is never
    written twice.
    """
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())
        logger.propagate = False
    logger.setLevel(logging.INFO)


_configure_logging()

_STORE: Any = None
_NOTIFIER: Any = None


def get_store():
    """Process-wide store.

    Defaults to the in-memory store (M0 behaviour, and what local tests use).
    Set ``FLOODPULSE_STORE=dynamodb`` to persist into the ``floodpulse`` table;
    ``FLOODPULSE_TABLE`` overrides the table name.
    """
    global _STORE
    if _STORE is None:
        _STORE = _build_store()
    return _STORE


def _build_store():
    """Choose the store from the environment without importing boto3 unless needed."""
    if os.environ.get("FLOODPULSE_STORE", "local").strip().lower() == "dynamodb":
        from .dynamodb_store import DynamoDbStore

        return DynamoDbStore(os.environ.get("FLOODPULSE_TABLE", "floodpulse"))
    return LocalStore()


def set_store(store: Any) -> None:
    """Replace the process-wide store (used by tests)."""
    global _STORE
    _STORE = store


def get_notifier():
    """Process-wide SNS notifier.

    With no ``FLOODPULSE_SNS_TOPIC_ARN`` this is a no-op, so local runs and the test
    suite never talk to AWS.
    """
    global _NOTIFIER
    if _NOTIFIER is None:
        _NOTIFIER = notifier_for_env(os.environ)
    return _NOTIFIER


def set_notifier(notifier: Any) -> None:
    """Replace the process-wide notifier (used by tests)."""
    global _NOTIFIER
    _NOTIFIER = notifier


def alert_cooldown_seconds() -> int:
    """Per-zone, per-severity alert cooldown, overridable by environment.

    Out-of-range or unparseable values fall back to the configured default rather than
    failing a deployment.
    """
    raw = os.environ.get(ALERT_COOLDOWN_ENV, "").strip()
    if not raw:
        return ALERT_COOLDOWN_SECONDS
    try:
        value = int(raw)
    except ValueError:
        LOGGER.warning("ignoring non-numeric %s=%r", ALERT_COOLDOWN_ENV, raw)
        return ALERT_COOLDOWN_SECONDS
    if not ALERT_COOLDOWN_MIN_SECONDS <= value <= ALERT_COOLDOWN_MAX_SECONDS:
        LOGGER.warning("ignoring out-of-range %s=%s", ALERT_COOLDOWN_ENV, value)
        return ALERT_COOLDOWN_SECONDS
    return value


def _utc_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _iso_epoch(value: Any) -> float | None:
    """Parse an ISO-8601 instant, returning ``None`` for anything that is not one.

    Stored items are not trusted to hold a well-formed timestamp: a hand-edited item or an
    older shape must degrade to "age unknown" rather than break the whole fleet response.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _provider_observation_age_seconds(latest: Mapping[str, Any], now_epoch: float) -> int | None:
    """Age of the provider observation a stored reading describes, or ``None`` if there is none."""
    observed = _iso_epoch(latest.get("provider_observed_at"))
    if observed is None:
        return None
    return max(0, int(now_epoch) - int(observed))


# Media types accepted on ``POST /readings``. A *missing* header is accepted on purpose:
# ``curl``, ``urllib`` and several AWS SDKs send no content type, and rejecting those would
# break the documented tools. A header that is present and plainly is not JSON (a browser
# form post, a multipart upload, a text blob) is refused before it reaches the parser.
JSON_MEDIA_TYPES: tuple[str, ...] = ("application/json", "text/json", "application/x-json")


def _brief(value: Any, limit: int = 160) -> str:
    """A one-line, length-capped form of a value for logging (never a payload dump)."""
    return " ".join(str(value).split())[:limit]


def _brief_text(value: Any, limit: int = 72) -> str:
    """A length-capped copy of caller-supplied text for an *error body*.

    The zone-id and path echoes in a 404 are useful (they say which id was not recognised),
    but the caller controls how long they are, so they are capped and marked when truncated
    rather than mirrored byte for byte. ``json.dumps`` escapes whatever remains, so the
    value cannot break the response's own encoding.
    """
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "\u2026"


def _header_value(headers: Mapping[str, Any] | None, name: str) -> str | None:
    """Case-insensitive header lookup; blank values read as absent."""
    if not headers:
        return None
    wanted = name.strip().lower()
    for header_name, value in headers.items():
        if str(header_name).strip().lower() == wanted and value is not None:
            text = str(value).strip()
            return text or None
    return None


def _reject_content_type(content_type: str | None) -> tuple[int, dict[str, Any]] | None:
    """Refuse a declared non-JSON body before anything is parsed or scored.

    The received media type is *not* echoed back: it is caller-supplied text, and there is
    no reason for a response to repeat it.
    """
    if content_type is None:
        return None
    media_type = str(content_type).split(";", 1)[0].strip().lower()
    if not media_type or media_type in JSON_MEDIA_TYPES or media_type.endswith("+json"):
        return None
    return 415, {
        "error": "unsupported_media_type",
        "detail": "send application/json",
    }


# --------------------------------------------------------------------------- #
# Incidence tracking and alerting (M3)
# --------------------------------------------------------------------------- #

# The incident transitions worth a log line each: what was opened, escalated or closed, for
# which zone, under which severity. ACTION_NONE is deliberately absent - a quiet reading that
# changed nothing must not write a log line per poll, which is most readings.
JOURNAL_ACTIONS = frozenset({ACTION_OPENED, ACTION_ESCALATED, ACTION_CLOSED})


def _incident_summary(
    action: str, incident: Mapping[str, Any], now_epoch: float, reason: str
) -> dict[str, Any]:
    """Client-facing view of what happened to the zone's incident."""
    summary = incident_view(incident, now_epoch)
    summary["action"] = action
    summary["changed"] = action in (ACTION_OPENED, ACTION_ESCALATED, ACTION_CLOSED)
    summary["reason"] = reason
    return summary


def _no_incident_summary(reason: str) -> dict[str, Any]:
    return {"action": ACTION_NONE, "changed": False, "current_status": None, "reason": reason}


def _maybe_alert(
    store: Any,
    notifier: Any,
    *,
    zone_id: str,
    incident: Mapping[str, Any],
    action: str,
    alert_severity: str | None,
    now_epoch: float,
    cooldown_seconds: int,
    data_classification: str = DATA_CLASSIFICATION_SIMULATED,
) -> dict[str, Any]:
    """Publish one alert when the rules allow it.

    The cooldown is *claimed* with an atomic conditional write before the publish, so two
    invocations escalating the same zone at the same moment cannot both alert — which is
    what "one storm must not become an alert storm" means once requests overlap. A publish
    that then fails restores the previous marker, so the claim cannot silently mute the
    next attempt. Never returns the topic ARN (a notifier error is redacted first).
    """
    if not alert_severity:
        return {"evaluated": False, "published": False, "severity": None, "reason": "no_escalation"}
    if notifier is None or not getattr(notifier, "enabled", False):
        return {
            "evaluated": True,
            "published": False,
            "severity": alert_severity,
            "reason": "alerts_not_configured",
        }

    marker = store.get_alert_marker(zone_id)
    remaining = cooldown_remaining_seconds(marker, alert_severity, now_epoch, cooldown_seconds)
    if remaining > 0:
        return {
            "evaluated": True,
            "published": False,
            "severity": alert_severity,
            "reason": "cooldown",
            "cooldown_seconds": int(cooldown_seconds),
            "cooldown_remaining_seconds": remaining,
        }

    claim = build_alert_marker(
        zone_id=zone_id,
        severity=alert_severity,
        now_epoch=now_epoch,
        previous=marker,
        message_id=None,
        data_classification=data_classification,
    )
    try:
        claimed = bool(
            store.claim_alert_slot(
                claim,
                severity=alert_severity,
                now_epoch=now_epoch,
                cooldown_seconds=int(cooldown_seconds),
            )
        )
    except Exception:  # noqa: BLE001 - a store that cannot claim must not block the alert
        LOGGER.exception("alert slot claim failed for zone %s", zone_id)
        claimed = True
    if not claimed:
        # Another request claimed this zone+severity first: it is publishing, we are not.
        loser_marker = store.get_alert_marker(zone_id)
        loser_remaining = cooldown_remaining_seconds(
            loser_marker, alert_severity, now_epoch, cooldown_seconds
        )
        LOGGER.info(
            "alert suppressed reason=slot_claimed_elsewhere zone_id=%s severity=%s",
            zone_id,
            alert_severity,
        )
        return {
            "evaluated": True,
            "published": False,
            "severity": alert_severity,
            "reason": "cooldown",
            "reason_detail": "another request claimed this alert slot first",
            "cooldown_seconds": int(cooldown_seconds),
            "cooldown_remaining_seconds": loser_remaining,
        }

    kind = ALERT_KIND_ESCALATED if action == ACTION_ESCALATED else ALERT_KIND_OPENED
    # The alert names the city and its state ("Patna, Bihar"), not the zone id: a subscriber
    # asked to hear about flood risk in monitored Indian cities. The labels come from the
    # incident's own record where it has them, otherwise from the city table.
    zone_label = str(zone_geography(incident)["display_name"] or incident.get("zone_name") or zone_id)
    try:
        result = notifier.publish(
            # the subject states the provenance of the incident, not of the deployment:
            # a live-weather incident over real rainfall is never labelled "simulated"
            alert_subject(kind, zone_label, alert_severity,
                          simulated=is_simulated_only(incident)),
            build_alert_message(kind, incident),
        )
    except Exception as exc:  # noqa: BLE001 - a bad notifier must not break tracking
        LOGGER.exception("alert publish failed for zone %s", zone_id)
        redact = getattr(notifier, "redact", None)
        detail = f"{exc.__class__.__name__}: {exc}"
        result = {
            "published": False,
            "reason": "publish_failed",
            "detail": (redact(detail) if callable(redact) else detail)[:200],
        }
    outcome: dict[str, Any] = {
        "evaluated": True,
        "severity": alert_severity,
        "published": bool(result.get("published")),
        "kind": kind,
    }
    if outcome["published"]:
        outcome["message_id"] = result.get("message_id")
        store.put_alert_marker(
            build_alert_marker(
                zone_id=zone_id,
                severity=alert_severity,
                now_epoch=now_epoch,
                previous=marker,
                message_id=result.get("message_id"),
                data_classification=data_classification,
            )
        )
    else:
        outcome["reason"] = str(result.get("reason") or "publish_failed")
        if result.get("detail"):
            outcome["detail"] = str(result["detail"])[:200]
        # Give *our* slot back: a failed publish must not consume the cooldown window. Only our
        # own claim is removed, so a hold another invocation took while our publish was in
        # flight is not erased - that would open the window early and let a duplicate through.
        try:
            outcome["slot_released"] = bool(
                store.release_alert_slot(zone_id, alert_severity, int(now_epoch))
            )
        except Exception:  # noqa: BLE001 - the failure is already reported; never raise here
            LOGGER.exception("alert slot rollback failed for zone %s", zone_id)
            outcome["slot_released"] = False
    LOGGER.info(
        "alert_publish zone_id=%s severity=%s kind=%s published=%s reason=%s",
        zone_id,
        alert_severity,
        kind,
        outcome["published"],
        outcome.get("reason") or "ok",
    )
    return outcome


def _save_incident(store: Any, incident: Mapping[str, Any], seen_version: int | None) -> tuple[dict[str, Any], bool]:
    """Write an incident record under the version guard.

    Returns ``(record_to_use, saved)``. The write carries the version this invocation read, so an
    overlapping invocation's newer state (an escalation, its alert entry) cannot be erased by an
    older view. When the write is refused, the caller must not publish an alert either: a message
    about a change the record does not contain is worse than a delayed one - the next reading
    re-decides against the stored record, which is what makes this self-correcting rather than
    lossy.
    """
    record = dict(incident)
    record["version"] = int(seen_version or 0) + 1
    try:
        saved = bool(store.put_incident(record, expected_version=seen_version))
    except Exception:  # noqa: BLE001 - a store fault must not raise out of incident tracking
        LOGGER.exception("incident write failed zone_id=%s", record.get("zone_id"))
        return record, False
    if not saved:
        LOGGER.warning(
            "incident_write_conflict zone_id=%s sk=%s seen_version=%s",
            record.get("zone_id"),
            _brief(record.get("sk")),
            seen_version,
        )
    return record, saved


def _claim_open_slot(store: Any, zone_id: str, incident: Mapping[str, Any], now_epoch: float) -> bool:
    """Try to take the zone's single open-incident slot.

    Returns ``True`` only when this invocation owns the slot. A refused claim is either a live
    incident (the caller adopts it) or a stale pointer left by a crash or by a same-second
    collision; the stale case is cleaned up here, once, and then retried - so a zone can always
    open its next incident, including in a deployment whose role cannot delete items.
    """
    if store.claim_incident_open(zone_id, incident_open_pointer(zone_id, incident, now_epoch)):
        return True
    pointer = store.get_incident_open(zone_id)
    held_sk = str((pointer or {}).get("incident_sk") or "")
    held = store.get_incident(zone_id, held_sk) if held_sk else None
    if held is not None and held.get("current_status") == INCIDENT_STATUS_OPEN:
        return False
    # Nothing is actually open: the pointer is stale (its incident is closed, or no record was
    # ever stored under that key). Free it and try once more.
    if not store.release_incident_open(zone_id, held_sk):
        return False
    return bool(store.claim_incident_open(zone_id, incident_open_pointer(zone_id, incident, now_epoch)))


def _track_incident(
    store: Any,
    notifier: Any,
    *,
    zone_id: str,
    risk_class: str,
    risk_score: float,
    top_driver: str | None,
    observed_ts_epoch: float,
    now_epoch: float,
    cooldown_seconds: int,
    data_classification: str = DATA_CLASSIFICATION_SIMULATED,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply the incident rules for one scored reading and alert if required.

    ``risk_class`` must be the *effective* class, so the existing hysteresis streaks
    govern both opening and closing. ``data_classification`` is the provenance of the
    reading, stored on the incident so a live-weather incident is never served as if the
    whole fleet were simulated.

    Two invariants are enforced with conditional writes rather than with careful reading:

    * a zone has at most one open incident at a time (``claim_incident_open``), so two
      simultaneous invocations cannot open two incidents - and publish two alerts - for one
      event;
    * a state change is written only against the version it was decided from (``_save_incident``),
      so an overlapping invocation cannot erase an escalation that landed after it read.
    """
    active = store.latest_incident(zone_id)
    seen_version = None
    if isinstance(active, Mapping) and active.get("version") is not None:
        seen_version = int(active.get("version") or 0)
    plan = decide_transition(
        zone_id=zone_id,
        active=active,
        risk_class=risk_class,
        risk_score=risk_score,
        top_driver=top_driver,
        observed_ts_epoch=observed_ts_epoch,
        now_epoch=now_epoch,
        data_classification=data_classification,
    )
    if plan.incident is None:
        return _no_incident_summary(plan.reason), {
            "evaluated": False,
            "published": False,
            "severity": None,
            "reason": "no_escalation",
        }

    action, incident, alert_severity, reason = plan.action, plan.incident, plan.alert_severity, plan.reason

    if action == ACTION_OPENED:
        if not _claim_open_slot(store, zone_id, incident, now_epoch):
            held_sk = str((store.get_incident_open(zone_id) or {}).get("incident_sk") or "")
            existing = store.get_incident(zone_id, held_sk) if held_sk else None
            if existing is None or existing.get("current_status") != INCIDENT_STATUS_OPEN:
                # Someone holds the slot but nothing is open inside it, and the claim could not
                # be freed. Refusing is the safe side of this: opening anyway is how a zone ends
                # up with two open incidents and two alerts for one event.
                LOGGER.warning(
                    "incident_open_unavailable zone_id=%s held_sk=%s", zone_id, _brief(held_sk)
                )
                return _no_incident_summary(
                    "another invocation holds this city's incident slot (the reading still scored and was stored)"
                ), {
                    "evaluated": False,
                    "published": False,
                    "severity": None,
                    "reason": "incident_open_unavailable",
                }
            # A concurrent invocation opened the incident first. Adopt the stored record instead
            # of writing a duplicate, and do not alert (the winner did).
            incident, action, alert_severity = existing, "observed", None
            reason = "an incident was already open (duplicate suppressed)"
            incident, saved = _save_incident(store, incident, existing.get("version"))
            if not saved:
                incident = existing
        else:
            # We own the slot. The key can still be held by an incident that already closed
            # inside this same second - an open, a resolve and a new reading all sharing one
            # timestamp - so the record may have to move to the next free key.
            created = None
            for attempt in range(1, INCIDENT_KEY_ATTEMPTS + 1):
                candidate = dict(incident)
                if attempt > 1:
                    candidate["sk"] = incident_suffix_key(str(incident["sk"]), attempt)
                    candidate["incident_id"] = incident_suffix_id(str(incident["incident_id"]), attempt)
                if store.create_incident(candidate):
                    created = candidate
                    if attempt > 1:
                        reason = "opened on a same-second key (an earlier incident held the base key)"
                    break
            if created is None:
                LOGGER.warning(
                    "incident_open_exhausted zone_id=%s base_sk=%s attempts=%s",
                    zone_id,
                    _brief(incident.get("sk")),
                    INCIDENT_KEY_ATTEMPTS,
                )
                store.release_incident_open(zone_id, str(incident.get("sk") or ""))
                return _no_incident_summary(
                    "no free incident key in this second (the reading still scored and was stored)"
                ), {
                    "evaluated": False,
                    "published": False,
                    "severity": None,
                    "reason": "incident_key_exhausted",
                }
            incident = created
            seen_version = None
            if not store.update_incident_open(zone_id, str(incident["sk"])):
                LOGGER.warning("incident_open_pointer_update_failed zone_id=%s", zone_id)
    else:
        incident, saved = _save_incident(store, incident, seen_version)
        if not saved:
            return _no_incident_summary(
                "another invocation recorded a change first; this reading left the record alone"
            ), {
                "evaluated": False,
                "published": False,
                "severity": None,
                "reason": "incident_write_conflict",
            }
        if action == ACTION_CLOSED:
            # Free the slot *after* the close is stored, and only if it is still ours.
            store.release_incident_open(zone_id, str(incident["sk"]))

    alert = _maybe_alert(
        store,
        notifier,
        zone_id=zone_id,
        incident=incident,
        action=action,
        alert_severity=alert_severity,
        now_epoch=now_epoch,
        cooldown_seconds=cooldown_seconds,
        data_classification=data_classification,
    )
    if alert.get("evaluated"):
        incident = record_alert(
            incident,
            build_alert_entry(
                severity=str(alert["severity"]),
                now_epoch=now_epoch,
                published=bool(alert.get("published")),
                reason=str(alert.get("reason") or ""),
                message_id=alert.get("message_id"),
            ),
        )
        incident, logged = _save_incident(store, incident, incident.get("version", seen_version))
        if not logged:
            # The alert went out; only its entry in the incident log was refused. The cooldown
            # marker is the authoritative record of what was published, so this is reported
            # rather than retried into someone else's newer state.
            LOGGER.warning(
                "alert_log_write_conflict zone_id=%s severity=%s", zone_id, alert.get("severity")
            )

    return _incident_summary(action, incident, now_epoch, reason), alert


# --------------------------------------------------------------------------- #
# POST /readings
# --------------------------------------------------------------------------- #


def _water_level_lookback(store: LocalStore, zone_id: str, ts_epoch: int) -> float | None:
    """Water level from roughly ``TREND_LOOKBACK_SECONDS`` before ``ts_epoch``.

    Returns ``None`` when there is no reading inside the tolerance window, which
    is reported honestly as a ``null`` trend contribution.
    """
    target = ts_epoch - TREND_LOOKBACK_SECONDS
    candidates = store.get_history(
        zone_id,
        since_epoch=target - TREND_LOOKBACK_TOLERANCE_SECONDS,
        until_epoch=target + TREND_LOOKBACK_TOLERANCE_SECONDS,
    )
    if not candidates:
        return None
    best = min(candidates, key=lambda item: abs(int(item["ts_epoch"]) - target))
    if abs(int(best["ts_epoch"]) - target) > TREND_LOOKBACK_TOLERANCE_SECONDS:
        return None
    return float(best["readings"]["water_level_m"])


@dataclass(frozen=True)
class IngestResult:
    """Everything one accepted reading produced (shared by both ingestion paths)."""

    latest: dict[str, Any]
    history: dict[str, Any] | None
    incident: dict[str, Any]
    alert: dict[str, Any]
    risk: dict[str, Any]
    hysteresis: dict[str, Any]
    outcome: WriteOutcome


def _ingest_record(
    store: LocalStore,
    reading: Mapping[str, Any],
    now_epoch: float,
    notifier: Any,
    cooldown_seconds: int,
) -> IngestResult:
    """Score one validated reading, store it and run incident/alert tracking.

    Shared by ``POST /readings`` (simulated client payloads) and the M4 live-weather
    ingestion, so a real-weather reading travels exactly the same pipeline as a
    simulated one: same scoring, same hysteresis, same incidents, same SNS alerts.

    Incident tracking runs only for a reading that *became* the zone's current state. A
    replay (the same observation already stored) or a late arrival older than what is
    already current is still accepted and still kept as history, but it must not re-open a
    closed incident, re-alert subscribers, or move current state backwards — the reading it
    duplicates, or the newer one that beat it, has already been judged.
    """
    zone_id = str(reading["zone_id"])
    previous = store.recent_readings(zone_id, SMOOTHING_WINDOW - 1)
    usable, dropped = [], 0
    for item in previous:
        stored = dict(item["readings"]) if isinstance(item.get("readings"), Mapping) else None
        if stored is not None and all(
            math.isfinite(float(stored.get(metric, math.nan))) for metric in METRIC_ORDER
        ):
            usable.append(stored)
        else:
            # A stored row whose metrics cannot be read (a hand-edited item, an older shape)
            # must not be averaged into this reading's score: it would either raise or, worse,
            # drag a mean towards a number nobody measured. It is skipped and counted, exactly
            # like an unusable row on the read side.
            dropped += 1
    if dropped:
        LOGGER.warning("smoothing_skipped_unusable zone_id=%s dropped=%s", zone_id, dropped)
    series = usable + [dict(reading["readings"])]
    smoothed = rolling_mean_metrics(series, SMOOTHING_WINDOW)

    lookback = _water_level_lookback(store, zone_id, int(reading["ts_epoch"]))
    try:
        risk = score_zone(
            smoothed,
            thresholds_for(zone_id),
            water_level_5min_ago=lookback,
            water_level_now=float(reading["readings"]["water_level_m"]),
            smoothing_window=len(series),
        )
    except RiskInputError as exc:
        # Defence in depth: validation refuses a non-finite metric before it gets here, so this
        # is a state the request should not be able to produce - but if it ever is produced, the
        # reading is refused with a reason instead of being scored (and, before M17, instead of
        # being scored CRITICAL because "not a number" fell through to the top band).
        LOGGER.error("scoring_refused zone_id=%s error=%s", zone_id, type(exc).__name__)
        raise ValidationError("every metric must be a finite number", "readings") from exc

    previous_latest = store.get_latest(zone_id)
    previous_state = (previous_latest or {}).get("hysteresis")
    hysteresis = apply_hysteresis(risk["risk_class_candidate"], previous_state)

    latest, history, outcome = store.put_reading(reading, risk, hysteresis, smoothed)

    if outcome.outcome != READING_STORED:
        if outcome.outcome == READING_DUPLICATE:
            reason = "this reading is already stored; current state and incidents unchanged"
            alert_reason = "duplicate_reading"
        else:
            reason = str(outcome.reason or "older than the zone's current reading") + \
                "; current state and incidents unchanged"
            alert_reason = "stale_reading"
        LOGGER.info(
            "ingest_%s zone_id=%s ts_epoch=%s stored_ts_epoch=%s history_written=%s",
            outcome.outcome,
            zone_id,
            outcome.incoming_ts_epoch,
            outcome.stored_ts_epoch,
            outcome.history_written,
        )
        return IngestResult(
            latest=latest,
            history=history,
            incident=_no_incident_summary(reason),
            alert={
                "evaluated": False,
                "published": False,
                "severity": None,
                "reason": alert_reason,
            },
            risk=risk,
            hysteresis=hysteresis,
            outcome=outcome,
        )

    # Incident tracking must never cost us an accepted reading: a failure here is
    # reported in the response and in the logs, but the reading stays stored.
    try:
        incident_summary, alert_summary = _track_incident(
            store,
            notifier,
            zone_id=zone_id,
            risk_class=str(hysteresis["risk_class"]),
            risk_score=float(risk["risk_score"]),
            top_driver=(risk["top_drivers"] or [None])[0],
            observed_ts_epoch=int(reading["ts_epoch"]),
            now_epoch=now_epoch,
            cooldown_seconds=int(cooldown_seconds),
            data_classification=str(reading.get("data_classification") or DATA_CLASSIFICATION_SIMULATED),
        )
    except Exception:  # noqa: BLE001 - never lose an accepted reading
        LOGGER.exception("incident tracking failed for zone %s", zone_id)
        incident_summary = {
            "action": "tracking_failed",
            "changed": False,
            "current_status": None,
            "reason": "incident tracking failed; the reading was stored regardless",
        }
        alert_summary = {"evaluated": False, "published": False, "severity": None, "reason": "tracking_failed"}

    # One line per incident lifecycle transition, and nothing at all for a reading that changed
    # no incident state: the incident log is the first thing to read when the fleet does
    # something unexpected, and it must not be buried under a line per accepted reading.
    action = str(incident_summary.get("action"))
    if action in JOURNAL_ACTIONS:
        LOGGER.info(
            "incident_%s zone_id=%s severity=%s status=%s risk_class=%s observed_at=%s "
            "reason=%s alert_published=%s",
            action,
            zone_id,
            incident_summary.get("severity"),
            incident_summary.get("current_status"),
            hysteresis.get("risk_class"),
            _utc_iso(int(reading["ts_epoch"])),
            _brief(incident_summary.get("reason")),
            alert_summary.get("published"),
        )
    elif action == "tracking_failed":
        LOGGER.warning("incident_tracking_failed zone_id=%s", zone_id)

    return IngestResult(
        latest=latest,
        history=history,
        incident=incident_summary,
        alert=alert_summary,
        risk=risk,
        hysteresis=hysteresis,
        outcome=outcome,
    )


def _handle_reading(
    body: str | None,
    store: LocalStore,
    now_epoch: float,
    notifier: Any = None,
    cooldown_seconds: int = 0,
    zone_ids: Iterable[str] | None = None,
    content_type: str | None = None,
) -> tuple[int, dict[str, Any]]:
    rejection = _reject_content_type(content_type)
    if rejection is not None:
        LOGGER.warning("ingest_rejected reason=content_type content_type=%s", _brief(content_type))
        return rejection

    try:
        validate_body_size(body)
    except ValidationError as exc:
        LOGGER.warning(
            "ingest_rejected reason=body_size field=%s detail=%s", _brief(exc.field), _brief(exc.reason)
        )
        return 400, {"error": "invalid_body", "field": exc.field, "detail": exc.reason}

    if body is not None and not isinstance(body, str):
        # The contract is a JSON *string* body (API Gateway always supplies one). Anything
        # else is a caller mistake that used to reach ``json.loads`` and raise TypeError -
        # an unhandled 500 for a request that is simply malformed.
        LOGGER.warning("ingest_rejected reason=body_type type=%s", type(body).__name__)
        return 400, {
            "error": "invalid_body",
            "field": "body",
            "detail": "request body must be a JSON string",
        }

    try:
        payload = json.loads(body)  # type: ignore[arg-type]
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        LOGGER.warning("ingest_rejected reason=malformed_json detail=%s", _brief(str(exc)))
        return 400, {"error": "malformed_json", "field": "body", "detail": str(exc)}

    try:
        reading = validate_reading(payload, now_epoch, zone_ids if zone_ids is not None else ZONE_IDS)
    except ValidationError as exc:
        # The reason names a rule and, at most, an object's *keys* - and one of those
        # messages ("unexpected metric(s): ...") does name caller-supplied metric keys, so
        # the log line runs their text through _brief: a newline or a control character in a
        # key must not be able to forge a line in CloudWatch Logs.
        LOGGER.warning(
            "ingest_rejected reason=invalid_payload field=%s detail=%s",
            _brief(exc.field),
            _brief(exc.reason),
        )
        return 400, {"error": "invalid_payload", "field": exc.field, "detail": exc.reason}

    zone_id = str(reading["zone_id"])
    try:
        result = _ingest_record(store, reading, now_epoch, notifier, cooldown_seconds)
    except Exception as exc:  # noqa: BLE001 - a store fault is a 503, never an unhandled 500
        # The reading was valid and scored; the *store* refused it (a throttle, a conditional-write
        # exhaustion, a missing table). That is a "try again" answer from FloodPulse's side, not a
        # caller error and not a mystery: answer 503, say nothing about the store's internals, and
        # put the exception *type* in the log so an operator can tell the classes apart. The message
        # is deliberately not logged: a store error can quote item content.
        LOGGER.error(
            "ingest_store_failed zone_id=%s error=%s", _brief(zone_id), type(exc).__name__
        )
        return 503, {
            "error": "storage_unavailable",
            "detail": "this reading could not be stored right now; it was not recorded",
        }
    latest = result.latest
    incident_summary = result.incident
    alert_summary = result.alert
    risk = result.risk
    hysteresis = result.hysteresis
    outcome = result.outcome

    return 202, {
        "status": "accepted",
        "simulated": True,
        "data_classification": reading["data_classification"],
        "zone_id": zone_id,
        "sensor_node_id": reading["sensor_node_id"],
        "timestamp": reading["timestamp"],
        "ts_epoch": reading["ts_epoch"],
        "ttl": reading["ttl"],
        "sequence": latest.get("sequence"),
        "risk": {
            "model": risk["model"],
            "risk_score": risk["risk_score"],
            "risk_class": hysteresis["risk_class"],
            "risk_class_candidate": risk["risk_class_candidate"],
            "base_score": risk["base_score"],
            "trend": risk["trend"],
            "top_drivers": risk["top_drivers"],
            "explanation": risk["explanation"],
        },
        "hysteresis": hysteresis,
        "incident": incident_summary,
        "alert": alert_summary,
        # Exactly what this request did to stored state: a client can tell "stored" from
        # "already stored" from "older than current" without guessing.
        "dedupe": outcome.as_view(),
        "stored": {
            "latest_sk": latest.get("sk") if outcome.latest_updated else None,
            "history_sk": outcome.history_sk,
            "history_records": store.history_count(zone_id),
        },
        "model_note": MODEL_NOTE,
        "deferred_features": list(DEFERRED_FEATURES),
    }


# --------------------------------------------------------------------------- #
# GET /zones
# --------------------------------------------------------------------------- #

# What a stored LATEST item must carry before it can be presented as a reading. A missing
# field means "this item cannot be shown", never "assume zero".
LATEST_REQUIRED_FIELDS: tuple[str, ...] = (
    "data_classification", "sensor_node_id", "timestamp", "ts_epoch", "sequence",
    "risk_score", "risk_class", "risk_class_candidate", "readings", "smoothed_readings",
)


def _missing_latest_fields(latest: Mapping[str, Any]) -> list[str]:
    """The required fields this stored item is missing (a non-mapping misses all of them)."""
    if not isinstance(latest, Mapping):
        return list(LATEST_REQUIRED_FIELDS)
    return [field for field in LATEST_REQUIRED_FIELDS if latest.get(field) is None]


def _zone_view(
    zone: Mapping[str, Any],
    latest: Mapping[str, Any] | None,
    now_epoch: float,
    *,
    mode: str | None = None,
    weather_cache: Mapping[str, Any] | None = None,
    refresh_seconds: float | None = None,
) -> dict[str, Any]:
    resolved_mode = zone_mode_for_env({}) if mode is None else mode
    resolved_refresh = WEATHER_REFRESH_SECONDS if refresh_seconds is None else refresh_seconds
    sources = METRIC_SOURCES_LIVE if resolved_mode == ZONE_MODE_LIVE else METRIC_SOURCES_SIMULATED
    scope = city_scope_for(str(zone["zone_id"]))
    view: dict[str, Any] = {
        "zone_id": zone["zone_id"],
        "name": zone["name"],
        # The monitored unit is a city, and the page needs to say which one and where:
        # ``city`` is the same name under the label the UI reads, ``display_name`` is the
        # "City, State" form used in headings and alerts. All three come from the one city
        # table in ``config`` - the API never assembles geography from a payload.
        "city": scope["city"] if scope["city"] is not None else zone["name"],
        "display_name": scope["display_name"] if scope["city"] is not None else zone["name"],
        "latitude": zone["latitude"],
        "longitude": zone["longitude"],
        "description": zone["description"],
        "thresholds": thresholds_for(str(zone["zone_id"])),
        "status": "no_data" if latest is None else "live",
        "zone_mode": resolved_mode,
        "metric_sources": dict(sources),
        "latest": None,
    }
    if zone.get("country"):
        view["country"] = zone["country"]
    # A demo zone has no state or coast group: the keys are omitted rather than sent empty, so
    # a reader can tell "this zone has no geography" from "this city's state is unknown".
    if scope["state"] is not None:
        view["state"] = scope["state"]
    if scope["coast_group"] is not None:
        view["coast_group"] = scope["coast_group"]
    # Why this city is in the fleet at all. A selection statement from the city table, not a
    # risk statement: the live score is computed elsewhere and says nothing about it.
    if scope.get("selection_reason") is not None:
        view["selection_reason"] = scope["selection_reason"]
    if isinstance(weather_cache, Mapping):
        # A cache item is only this city's if it says so. An item copied under the wrong key (a
        # hand-edit, a bad migration) would otherwise be served as this city's weather: another
        # city's rainfall under this city's name is worse than no weather block at all.
        if str(weather_cache.get("zone_id") or "") not in ("", str(zone["zone_id"])):
            LOGGER.warning("weather.cache_zone_mismatch zone_id=%s", zone["zone_id"])
            weather_cache = None
    if isinstance(weather_cache, Mapping):
        age = cache_age_seconds(weather_cache, now_epoch)
        attempt = attempt_age_seconds(weather_cache, now_epoch)
        observation = observation_age_seconds(weather_cache, now_epoch)
        stored_sources = weather_cache.get("metric_sources")
        view["weather"] = {
            "provider": weather_cache.get("provider", WEATHER_PROVIDER),
            "observed_at": weather_cache.get("observed_at"),
            "provider_current_time": weather_cache.get("provider_current_time"),
            "fetched_at": weather_cache.get("fetched_at"),
            "cache_age_seconds": None if age is None else int(age),
            # Three different ages, named so they cannot be confused with each other:
            #   cache_age_seconds       - when FloodPulse last fetched from the provider
            #   observation_age_seconds - how old the observation the provider returned is
            #   last_attempt_age_seconds- when the provider was last called, successfully or not
            # A fetch can be seconds old while the observation inside it is an hour old (that is
            # how an hourly provider works), so "fresh fetch" is never presented as "fresh rain".
            "observation_age_seconds": None if observation is None else int(observation),
            "observation_interval_seconds": int(WEATHER_OBSERVATION_INTERVAL_SECONDS),
            # Is the observation the provider gave us the one we should expect by now? False
            # means the provider is behind its own cadence - worth stating, never a failure.
            "observation_is_current": observation_is_current(
                observation,
                refresh_seconds=WEATHER_REFRESH_SECONDS if resolved_refresh is None else resolved_refresh,
            ),
            # bookkeeping for the last attempt: a failed fetch is reported per zone, and the
            # age of the failed attempt explains why a poll did not call the provider again
            "last_attempt_at": weather_cache.get("last_attempt_at"),
            "last_attempt_age_seconds": None if attempt is None else int(attempt),
            "last_attempt_ok": weather_cache.get("last_attempt_ok"),
            "last_attempt_error": weather_cache.get("last_attempt_error"),
            "rainfall_mm_per_hr": weather_cache.get("rainfall_mm_per_hr"),
            "soil_moisture_m3m3": weather_cache.get("soil_moisture_m3m3"),
            "soil_saturation_pct": weather_cache.get("soil_saturation_pct"),
            # A stored metric_sources that is not an object (a string, a list) is ignored rather
            # than merged: it used to raise out of the zone view and 500 the whole board.
            "metric_sources": dict(stored_sources if isinstance(stored_sources, Mapping) else sources),
        }
    if latest is None:
        return view

    # A stored item can be incomplete (a legacy shape, a partial write, a hand-edited item).
    # Presenting half a reading is worse than presenting none, so an unreadable item is
    # reported as "no data" for that zone only, with the reason logged and no value invented.
    missing = _missing_latest_fields(latest)
    if missing:
        LOGGER.warning("readings.unreadable zone_id=%s missing=%s", zone["zone_id"], ",".join(missing))
        view["status"] = "no_data"
        return view

    detail = latest.get("risk_detail", {})
    # The zone's tags describe the reading being presented, not the fleet's plan for it: a
    # posted simulated reading on a live city must not be labelled with the fleet's REAL
    # rainfall tag while the number on the card came from that post. The fallback keeps a
    # legacy item (written before tags were stored) honest too - a simulated record is
    # simulated whatever the fleet is.
    record_sources = (
        METRIC_SOURCES_SIMULATED
        if str(latest.get("data_classification") or "") == DATA_CLASSIFICATION_SIMULATED
        else sources
    )
    view["metric_sources"] = metric_sources_for_records([latest], record_sources)
    view["latest"] = {
        "simulated": bool(latest.get("simulated", True)),
        "data_classification": latest["data_classification"],
        "source": latest.get("source", "simulated"),
        # A reading's own tags win: only live-weather ingestion writes them, and client
        # payloads are locked to ``simulated: true`` by validation, so the fallback is
        # always accurate rather than optimistic.
        "metric_sources": dict(latest.get("metric_sources") or METRIC_SOURCES_SIMULATED),
        "provider_observed_at": latest.get("provider_observed_at"),
        # The age of the provider observation behind this reading, when there is one. The reading
        # itself is stamped when FloodPulse computed it, so a live card can read "3 min old"
        # while the rain it describes fell 55 minutes ago - both facts, named separately, and
        # never merged into one number that would have to lie about one of them.
        "provider_observation_age_seconds": _provider_observation_age_seconds(latest, now_epoch),
        # The zone's own staleness threshold, so a viewer never has to know it from the docs.
        "stale_after_seconds": STALE_READING_SECONDS,
        "sensor_node_id": latest["sensor_node_id"],
        "timestamp": latest["timestamp"],
        "ts_epoch": latest["ts_epoch"],
        "age_seconds": max(0, int(now_epoch) - int(latest["ts_epoch"])),
        "sequence": latest["sequence"],
        "risk_score": latest["risk_score"],
        "risk_class": latest["risk_class"],
        "risk_class_candidate": latest["risk_class_candidate"],
        "readings": latest["readings"],
        "smoothed_readings": latest["smoothed_readings"],
        "contributions": detail.get("contributions", []),
        "top_drivers": detail.get("top_drivers", []),
        "trend": detail.get("trend", {}),
        "explanation": detail.get("explanation", ""),
        "hysteresis": latest.get("hysteresis", {}),
    }
    return view


def _zone_view_safely(
    zone: Mapping[str, Any],
    store: Any,
    now_epoch: float,
    *,
    mode: str,
    weather_cache: Mapping[str, Any] | None,
    refresh_seconds: float | None = None,
) -> dict[str, Any]:
    """One zone's view, isolated: a failing read degrades that zone, never the fleet."""
    zone_id = str(zone["zone_id"])
    try:
        latest = store.get_latest(zone_id)
    except Exception as exc:  # noqa: BLE001 - one unreadable zone must not blank the board
        LOGGER.warning("readings.latest_failed zone_id=%s error=%s", zone_id, type(exc).__name__)
        latest = None
    return _zone_view(
        zone,
        latest,
        now_epoch,
        mode=mode,
        weather_cache=weather_cache,
        refresh_seconds=refresh_seconds,
    )


def _apply_zone_mode(store: Any, env: Mapping[str, str] | None) -> tuple[str, tuple[dict[str, Any], ...]]:
    """Resolve the active fleet from the environment and point the store at it.

    ``FLOODPULSE_ZONE_MODE=live`` selects the five live-weather Indian cities;
    anything else (or nothing) keeps the three simulated demo zones.
    """
    mode = zone_mode_for_env(env)
    definitions = zone_definitions_for_mode(mode)
    zone_ids = tuple(str(zone["zone_id"]) for zone in definitions)
    if getattr(store, "zone_ids", None) != zone_ids:
        store.zone_ids = zone_ids
    return mode, definitions


def _weather_refresh_seconds(env: Mapping[str, str] | None) -> int:
    """Cache age before external weather may be fetched again (default 10 minutes)."""
    raw = ({} if env is None else env).get(WEATHER_REFRESH_ENV)
    if raw is None or str(raw).strip() == "":
        return WEATHER_REFRESH_SECONDS
    try:
        seconds = int(str(raw).strip())
    except (TypeError, ValueError):
        LOGGER.warning("ignoring invalid %s value", WEATHER_REFRESH_ENV)
        return WEATHER_REFRESH_SECONDS
    if not WEATHER_REFRESH_MIN_SECONDS <= seconds <= WEATHER_REFRESH_MAX_SECONDS:
        clamped = min(max(seconds, WEATHER_REFRESH_MIN_SECONDS), WEATHER_REFRESH_MAX_SECONDS)
        LOGGER.warning(
            "clamping %s=%s to %s seconds (allowed %s..%s)",
            WEATHER_REFRESH_ENV,
            seconds,
            clamped,
            WEATHER_REFRESH_MIN_SECONDS,
            WEATHER_REFRESH_MAX_SECONDS,
        )
        return clamped
    return seconds


def _refresh_weather(
    store: Any,
    *,
    mode: str,
    env: Mapping[str, str] | None,
    now_epoch: float,
    notifier: Any,
    cooldown_seconds: int,
    weather_client: Any = None,
) -> tuple[Any, dict[str, Any]]:
    """Refresh stale live-zone weather and ingest it through the normal pipeline.

    Live readings are scored, stored and tracked exactly like simulated ones, so real
    rainfall can open incidents and raise SNS alerts. Any failure is reported, never
    raised: a provider outage must not break the dashboard.
    """
    refresh_seconds = _weather_refresh_seconds(env)
    caches: dict[str, Any] = {}
    outcome = None

    def ingest(record: dict[str, Any], observation: Any) -> None:
        """Score, store and track a live reading exactly like an accepted client reading."""
        _ingest_record(store, record, now_epoch, notifier, cooldown_seconds)

    try:
        outcome = refresh_live_weather(
            store,
            mode=mode,
            env=env,
            now_epoch=now_epoch,
            ingest=ingest,
            client=weather_client,
            refresh_seconds=refresh_seconds,
        )
    except Exception:  # noqa: BLE001 - the dashboard must survive any weather failure
        LOGGER.exception("live weather refresh failed")
        outcome = RefreshOutcome(enabled=True, attempted=True, error="refresh_failed")

    if outcome is not None and (outcome.attempted or outcome.error or outcome.failed):
        # one structured line per attempt so a provider problem is diagnosable in CloudWatch
        # even after the response that reported it has scrolled away. No secret, no user data,
        # and the provider URL is public.
        summary = {
            "event": "weather_refresh",
            "mode": mode,
            "attempted": outcome.attempted,
            "refreshed": list(outcome.refreshed),
            "already_fresh": list(outcome.skipped_fresh),
            "failed": list(outcome.failed),
            "error": outcome.error,
            "reason": outcome.reason,
            "refresh_seconds": refresh_seconds,
        }
        if outcome.failed or outcome.error:
            LOGGER.warning("weather refresh outcome: %s", json.dumps(summary, sort_keys=True))
        else:
            LOGGER.info("weather refresh outcome: %s", json.dumps(summary, sort_keys=True))

    # Reuse the items the refresh pass already read (and, for every zone it wrote, the items it
    # wrote). Two reasons: 12 zones cost one read each per poll instead of two, and the block a
    # viewer sees is built from the same values the refresh decision was made on - a second read
    # could report "fresh" next to a refresh that just failed. Only when there is no outcome (the
    # provider is off, or the refresh never ran) is the cache read here, one zone at a time so a
    # single unreadable item cannot blank the block for all twelve.
    caches: dict[str, Any] = dict(getattr(outcome, "caches", None) or {})
    for zone in live_weather_zones(mode, env):
        zone_id = str(zone["zone_id"])
        if zone_id in caches:
            continue
        try:
            caches[zone_id] = store.get_weather_cache(zone_id)
        except Exception as exc:  # noqa: BLE001 - one unreadable cache must not blank the fleet
            LOGGER.warning(
                "weather.cache_read_failed zone_id=%s error=%s", zone_id, type(exc).__name__
            )
            caches[zone_id] = None

    return outcome, caches


def _handle_zones(
    store: LocalStore,
    now_epoch: float,
    *,
    zones: tuple[dict[str, Any], ...] | None = None,
    mode: str | None = None,
    env: Mapping[str, str] | None = None,
    notifier: Any = None,
    cooldown_seconds: int = 0,
    weather_client: Any = None,
) -> tuple[int, dict[str, Any]]:
    definitions = zones if zones is not None else ZONE_DEFINITIONS
    active_mode = mode if mode is not None else zone_mode_for_env(env)
    # The configured provider cadence is part of the freshness contract published below: a zone
    # block states how old its provider observation is, and whether that is still on schedule.
    refresh_seconds = _weather_refresh_seconds(env)

    outcome, caches = _refresh_weather(
        store,
        mode=active_mode,
        env=env,
        now_epoch=now_epoch,
        notifier=notifier,
        cooldown_seconds=cooldown_seconds,
        weather_client=weather_client,
    )

    views = [
        _zone_view_safely(
            zone,
            store,
            now_epoch,
            mode=active_mode,
            weather_cache=caches.get(str(zone["zone_id"])),
            refresh_seconds=refresh_seconds,
        )
        for zone in definitions
    ]
    with_data = [view for view in views if view["latest"] is not None]
    without_data = [view for view in views if view["latest"] is None]

    ranked = sorted(with_data, key=lambda view: (-float(view["latest"]["risk_score"]), view["zone_id"]))
    ranking = [view["zone_id"] for view in ranked] + [view["zone_id"] for view in without_data]

    highest: dict[str, Any] | None = None
    if ranked:
        top = ranked[0]
        leader_age = int(top["latest"]["age_seconds"])
        highest = {
            "zone_id": top["zone_id"],
            "name": top["name"],
            "city": top.get("city", top["name"]),
            "display_name": top.get("display_name", top["name"]),
            "risk_score": top["latest"]["risk_score"],
            "risk_class": top["latest"]["risk_class"],
            "timestamp": top["latest"]["timestamp"],
            # How old the reading behind this score is, so "highest risk" is never read as
            # "right now" when the zone stopped reporting half an hour ago.
            "age_seconds": leader_age,
            "stale": leader_age > STALE_READING_SECONDS,
        }
        for key in ("state", "coast_group"):
            if top.get(key) is not None:
                highest[key] = top[key]

    # Zones whose newest stored reading is too old to describe the present. They still appear
    # with their real values; this says which ones are no longer current (no extra reads: the
    # LATEST items are already in hand).
    stale_zones = [
        view["zone_id"] for view in with_data
        if int(view["latest"]["age_seconds"]) > STALE_READING_SECONDS
    ]

    classification = data_classification_for_mode(active_mode)
    notes = [MIXED_DATA_DISCLAIMER if classification == "mixed" else SIMULATION_DISCLAIMER]
    if classification == "mixed":
        notes.append(HYDRAULICS_NOTE)
    notes.append("Time-to-threshold is not implemented yet (deferred until after the live dashboard).")

    return 200, {
        "generated_at": _utc_iso(now_epoch),
        "simulated": classification == "simulated",
        "data_classification": classification,
        "zone_mode": active_mode,
        "metric_sources": dict(
            METRIC_SOURCES_LIVE if active_mode == ZONE_MODE_LIVE else METRIC_SOURCES_SIMULATED
        ),
        "model": RISK_MODEL_ID,
        "model_note": MODEL_NOTE,
        "notes": notes,
        "weather": weather_view(
            mode=active_mode,
            outcome=outcome,
            caches=caches,
            env=env,
            now_epoch=now_epoch,
            refresh_seconds=_weather_refresh_seconds(env),
        ),
        "highest_risk_zone": highest,
        "zone_ranking": ranking,
        # What the fleet covers, and what it does not. Derived from the same city table that
        # produced ``zones``, so a count here can never disagree with the list below it, and
        # the dashboard has no reason to hardcode 15 anywhere.
        "coverage": coverage_summary(active_mode),
        # a fleet-level answer to "is anything on this board out of date?"
        "stale_zone_ids": stale_zones,
        "stale_after_seconds": STALE_READING_SECONDS,
        "zone_count": len(views),
        "zones": views,
        "deferred_features": list(DEFERRED_FEATURES),
    }


# --------------------------------------------------------------------------- #
# GET /zones/{zone_id}/history
# --------------------------------------------------------------------------- #


def _is_ascii_digits(raw: Any, *, allow_sign: bool = False) -> bool:
    """True when ``raw`` is a whitespace-padded run of ASCII digits (optionally ``-``-signed).

    ``str.isdigit()`` is also true for characters ``int()`` refuses - superscripts,
    circled digits, Arabic-Indic and full-width digits - so using it as the guard let a
    query value such as ``?minutes=²`` reach a bare ``int()`` and escape as an unhandled
    ``ValueError`` (an HTTP 500 on two public routes). This keeps the accepted alphabet
    explicit and refuses everything else as a normal bad query value.

    A sign is only *accepted* here so that ``-5`` still produces the range message
    ("must be between 1 and …") rather than the type message; ``+5`` stays refused, which is
    the behaviour the query contract already had.
    """
    if not isinstance(raw, str):
        return False
    text = raw.strip()
    if allow_sign and text[:1] == "-":
        text = text[1:]
    return bool(text) and text.isascii() and text.isdigit()


def _parse_minutes(raw: Any) -> int:
    if raw is None:
        return HISTORY_DEFAULT_MINUTES
    if isinstance(raw, bool):
        raise ValidationError("must be an integer number of minutes", "minutes")
    if isinstance(raw, int):
        minutes = raw
    elif _is_ascii_digits(raw, allow_sign=True):
        minutes = int(str(raw).strip())
    else:
        raise ValidationError("must be an integer number of minutes", "minutes")
    if not 1 <= minutes <= HISTORY_MAX_MINUTES:
        raise ValidationError(f"must be between 1 and {HISTORY_MAX_MINUTES}", "minutes")
    return minutes


def provenance_of_records(records: Iterable[Mapping[str, Any]], fallback: str) -> str:
    """The provenance a response may claim for the records it is about to serve.

    Records of one provenance report it; records of several are reported as ``mixed``; an
    empty selection falls back to the classification of the active fleet. Records written
    before provenance was stored are ignored rather than guessed at.
    """
    seen = {str(record.get("data_classification") or "") for record in records}
    seen.discard("")
    if len(seen) == 1:
        return seen.pop()
    if seen:
        return DATA_CLASSIFICATION_MIXED
    return fallback


#: Weakest claim first. A metric is never labelled more confidently than the least
#: confident record in the selection it is shown with.
SOURCE_TRUST_ORDER = (
    SOURCE_SIMULATED,
    SOURCE_MODEL_DERIVED,
    SOURCE_REAL,
)


def metric_sources_for_records(
    records: Iterable[Mapping[str, Any]],
    fallback: Mapping[str, str],
) -> dict[str, str]:
    """Per-metric provenance for the readings a response is about to serve.

    A card, a history window or any other selection that mixes a provider observation with a
    posted one must not borrow the stronger label. Each metric is therefore labelled by the
    *weakest* tag any selected record gives it, which is what keeps a fabricated value from
    ever sitting under a ``REAL`` tag. An unrecognised tag is treated as the weakest one.

    Records without tags (written before tags were stored, or a partial item) contribute
    nothing, so the caller's fallback survives where the records are silent.
    """
    result = {metric: str(label) for metric, label in fallback.items()}
    seen: dict[str, list[str]] = {}
    for record in records:
        tags = record.get("metric_sources") if isinstance(record, Mapping) else None
        if not isinstance(tags, Mapping):
            continue
        for metric, label in tags.items():
            seen.setdefault(str(metric), []).append(str(label))
    for metric, labels in seen.items():
        result[metric] = min(
            labels,
            key=lambda label: (
                SOURCE_TRUST_ORDER.index(label) if label in SOURCE_TRUST_ORDER else -1
            ),
        )
    return result


def _finite_number(value: Any) -> float | None:
    """``value`` as a finite float, or ``None`` when it is not a usable number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _stored_class(value: Any) -> str | None:
    """A stored band label, or ``None`` when the field is missing or unusable."""
    return value.strip() if isinstance(value, str) and value.strip() else None


def _readings_without_non_finite_numbers(readings: Any) -> dict[str, Any]:
    """The stored readings map, with any ``NaN`` or ``Infinity`` reported as ``null``.

    BUG CLASS (M16): ``json.dumps`` writes a non-finite float as the bare token ``NaN`` or
    ``Infinity``. That is not JSON, and one corrupt metric makes a browser's ``JSON.parse``
    reject the entire response - so the whole window, not just that metric, is lost. A value
    that cannot be represented is reported as unknown instead.
    """
    if not isinstance(readings, Mapping):
        return {}
    return {
        str(metric): (None if isinstance(value, float) and not math.isfinite(value) else value)
        for metric, value in readings.items()
    }


def _history_point(record: Any) -> dict[str, Any] | None:
    """One stored history record as an API point, or ``None`` when it cannot be shown.

    BUG CLASS (M16): this route used to copy its fields straight out of the stored item, so a
    single malformed record - an older item shape, a half-written item, a hand-edit - raised
    ``KeyError`` and turned the whole city's window into a 500, and a stored non-finite number
    went into the body as invalid JSON. A record is now dropped unless its timestamp and score
    are usable and inside the frozen 0..1 range. A missing band label is filled from the score
    the engine itself would classify (``heuristic-v1`` is a pure function of the score); the
    effective class the hysteresis produced is never invented.
    """
    if not isinstance(record, Mapping):
        return None
    ts_epoch = _finite_number(record.get("ts_epoch"))
    score = _finite_number(record.get("risk_score"))
    timestamp = record.get("timestamp")
    if ts_epoch is None or score is None or not 0.0 <= score <= 1.0:
        return None
    if not isinstance(timestamp, str) or not timestamp.strip():
        return None
    candidate = _stored_class(record.get("risk_class_candidate")) or classify(score)
    return {
        "timestamp": timestamp,
        "ts_epoch": int(ts_epoch) if ts_epoch.is_integer() else ts_epoch,
        "risk_score": score,
        "risk_class": _stored_class(record.get("risk_class")) or candidate,
        "risk_class_candidate": candidate,
        "readings": _readings_without_non_finite_numbers(record.get("readings")),
    }


def _extreme_index(scores: Sequence[float], highest: bool) -> int:
    """Index of the peak (or minimum) score; a tie keeps the earlier sample."""
    best = 0
    for index in range(1, len(scores)):
        better = scores[index] > scores[best] if highest else scores[index] < scores[best]
        if better:
            best = index
    return best


def _thin_history(
    entries: list[tuple[Mapping[str, Any], dict[str, Any]]],
    max_points: int,
) -> tuple[list[tuple[Mapping[str, Any], dict[str, Any]]], int]:
    """Thin a history window to ``max_points`` without losing the samples that answer it.

    BUG CLASS (M16): taking every n-th record from one end loses whatever sits between the
    steps, so the reading the chart's right-hand edge is drawn from vanished exactly when a
    window needed thinning, and an interior peak or minimum vanished with it - a caption
    computed from the thinned points then disagreed with the stored data. The kept set
    therefore always contains the newest sample, the oldest sample in the window and the
    extreme scores; ``stride`` only sets the cadence of the records in between. When the cap
    is smaller than the samples that must be kept the cap wins, newest first, because the
    response-size bound is the harder constraint.

    ``entries`` are ``(stored record, response point)`` pairs for records that can be shown.
    """
    total = len(entries)
    if max_points <= 0 or total <= max_points:
        return list(entries), 1
    stride = max(1, math.ceil(total / max_points))
    if stride <= 1:
        return list(entries), 1
    scores = [float(point["risk_score"]) for _record, point in entries]
    required: list[int] = []
    for index in (total - 1, 0, _extreme_index(scores, True), _extreme_index(scores, False)):
        if index not in required:
            required.append(index)
    protected = set(required[:max_points])
    keep = protected | set(range(total - 1, -1, -stride))  # cadence anchored at the newest sample
    # Give up cadence from the old end first: the newest sample, the oldest sample in the
    # window and the extremes are the samples whose absence changes the answer.
    for index in range((total - 1) % stride, total, stride):
        if len(keep) <= max_points:
            break
        if index not in protected:
            keep.discard(index)
    return [entries[index] for index in sorted(keep)], stride


def _handle_history(
    store: LocalStore,
    zone_id: str,
    now_epoch: float,
    query: Mapping[str, Any],
    zone_ids: Iterable[str] | None = None,
    active_mode: str = ZONE_MODE_DEMO,
) -> tuple[int, dict[str, Any]]:
    allowed = tuple(zone_ids) if zone_ids is not None else ZONE_IDS
    if zone_id not in allowed:
        return 404, {
            "error": "unknown_zone",
            "detail": f"unknown zone_id {_brief_text(zone_id)}",
            "known_zones": list(allowed),
        }
    try:
        minutes = _parse_minutes(query.get("minutes"))
    except ValidationError as exc:
        return 400, {"error": "invalid_query", "field": exc.field, "detail": exc.reason}

    since_epoch = int(now_epoch) - minutes * 60
    records = store.get_history(zone_id, since_epoch=since_epoch)

    # Unusable records are dropped *before* thinning, so the point cap is spent on rows the
    # caller will actually see and the newest usable sample is always the last point.
    usable: list[tuple[Mapping[str, Any], dict[str, Any]]] = []
    unusable = 0
    for record in records:
        point = _history_point(record)
        if point is None:
            unusable += 1
        else:
            usable.append((record, point))
    if unusable:
        LOGGER.warning(
            "history_unusable_records zone_id=%s unusable=%s records=%s",
            zone_id,
            unusable,
            len(records),
        )

    entries, stride = _thin_history(usable, MAX_HISTORY_POINTS)
    sampled = [record for record, _point in entries]
    points = [point for _record, point in entries]

    classification = provenance_of_records(sampled, data_classification_for_mode(active_mode))

    zone = zone_definition(zone_id)
    return 200, {
        "zone_id": zone_id,
        "name": zone["name"],
        "minutes": minutes,
        "since_epoch": since_epoch,
        "point_count": len(points),
        "downsampled": stride > 1,
        "stride": stride,
        # The window reports what its own records are, not what the fleet happens to be
        # now: a live city's history holds provider-derived rainfall, and calling that
        # "simulated" (as this route used to) understates where the numbers came from.
        "simulated": classification == DATA_CLASSIFICATION_SIMULATED,
        "data_classification": classification,
        "zone_mode": active_mode,
        # Labelled by the records in the window, for the same reason the classification is:
        # a window of posted readings must not inherit the live fleet's REAL rainfall tag.
        "metric_sources": metric_sources_for_records(
            sampled,
            METRIC_SOURCES_LIVE if active_mode == ZONE_MODE_LIVE else METRIC_SOURCES_SIMULATED,
        ),
        "model": RISK_MODEL_ID,
        "points": points,
    }


# --------------------------------------------------------------------------- #
# GET /incidents
# --------------------------------------------------------------------------- #


def _parse_limit(raw: Any) -> int:
    if raw is None:
        return INCIDENT_DEFAULT_LIMIT
    if isinstance(raw, bool):
        raise ValidationError("must be an integer number of incidents", "limit")
    if isinstance(raw, int):
        limit = raw
    elif isinstance(raw, str) and _is_ascii_digits(raw):
        limit = int(raw.strip())
    else:
        raise ValidationError("must be an integer number of incidents", "limit")
    if not 1 <= limit <= INCIDENT_MAX_LIMIT:
        raise ValidationError(f"must be between 1 and {INCIDENT_MAX_LIMIT}", "limit")
    return limit


def _handle_incidents(
    store: Any,
    now_epoch: float,
    query: Mapping[str, Any],
    notifier: Any = None,
    cooldown_seconds: int = 0,
    zone_ids: Iterable[str] | None = None,
    env: Mapping[str, str] | None = None,
    active_mode: str = ZONE_MODE_DEMO,
) -> tuple[int, dict[str, Any]]:
    allowed = tuple(zone_ids) if zone_ids is not None else ZONE_IDS
    raw_zone = query.get("zone_id")
    zone_id: str | None = None
    if raw_zone is not None and str(raw_zone).strip() != "":
        zone_id = str(raw_zone).strip()
        if zone_id not in allowed:
            return 404, {
                "error": "unknown_zone",
                "detail": f"unknown zone_id {_brief_text(zone_id)}",
                "known_zones": list(allowed),
            }
    try:
        limit = _parse_limit(query.get("limit"))
    except ValidationError as exc:
        return 400, {"error": "invalid_query", "field": exc.field, "detail": exc.reason}

    # One extra record decides "is there more?", so a page that is exactly full is not reported
    # as truncated: a client that pages on this flag would otherwise fetch an empty page every
    # time the count is an exact multiple of the limit.
    page = store.list_incidents(zone_id=zone_id, limit=limit + 1)
    truncated = len(page) > limit
    incidents = [incident_view(item, now_epoch) for item in page[:limit]]
    open_count = sum(1 for view in incidents if view["is_open"])

    # The list reports the provenance of the incidents it is showing (each incident keeps the
    # provenance of the reading that opened it); with nothing to show it reports the fleet's.
    classification = provenance_of_records(incidents, data_classification_for_mode(active_mode))
    notes = [MIXED_DATA_DISCLAIMER if classification == DATA_CLASSIFICATION_MIXED else SIMULATION_DISCLAIMER]
    notes.append(
        "Incidents record heuristic band crossings: a zone opens one on reaching "
        "WARNING or CRITICAL and it closes once the zone returns to NORMAL."
    )

    return 200, {
        "generated_at": _utc_iso(now_epoch),
        "simulated": classification == DATA_CLASSIFICATION_SIMULATED,
        "data_classification": classification,
        "zone_mode": active_mode,
        "model": RISK_MODEL_ID,
        "model_note": MODEL_NOTE,
        "zone_id": zone_id,
        "limit": limit,
        "count": len(incidents),
        # The counts below describe the incidents this response returns, not the whole table:
        # say so, or "3 open" reads as "the fleet has three open incidents" when the list was
        # cut off at `limit`.
        "counts_scope": "returned_page",
        "truncated": truncated,
        "open_count": open_count,
        "closed_count": len(incidents) - open_count,
        "alerting": alerting_view(notifier is not None and getattr(notifier, "enabled", False), cooldown_seconds),
        "incidents": incidents,
        "notes": notes,
        "deferred_features": list(DEFERRED_FEATURES),
    }


# --------------------------------------------------------------------------- #
# Public email subscriptions (M10, part B)
# --------------------------------------------------------------------------- #


def _handle_subscriptions(
    body: str | None,
    store: Any,
    now_epoch: float,
    notifier: Any,
    source_ip: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """``POST /subscriptions`` - a visitor asks to be emailed about flood risk.

    The route is public on purpose (the dashboard calls it from the browser), so it is
    bounded at every step:

    * the body is one small JSON object with one field, ``email``;
    * the address must pass the syntax check in :mod:`floodpulse.subscriptions`;
    * the topic and the protocol come from trusted configuration, never from the request -
      the client cannot name a topic, a protocol, a region or a subscription ARN;
    * attempts are counted per address *and* per caller in the existing DynamoDB table, so
      the confirmation messages AWS sends cannot be turned into a way to mail a stranger
      repeatedly, and one caller cannot spend a stranger's hourly budget;
    * the success answer is the same whether the address is new, already pending or already
      confirmed, so the endpoint cannot be used to test whether somebody is subscribed.

    Nothing is claimed about delivery: AWS sends the confirmation message, and until the
    visitor follows it the subscription is pending. The response therefore says "check your
    email", never "you are subscribed".
    """
    try:
        address = parse_subscription_request(body)
    except ValidationError as exc:
        if exc.field == "email":
            # The caller's own input, so naming the problem is safe and useful. The address
            # itself is never echoed back, and never logged.
            return 400, {
                "status": "invalid",
                "error": "invalid_email",
                "field": "email",
                "detail": exc.reason,
                "message": "Enter a valid email address.",
            }
        return 400, {
            "status": "invalid",
            "error": "invalid_body",
            "field": exc.field or "body",
            "detail": exc.reason,
            "message": "Send a JSON object with a single 'email' field.",
        }

    enabled = notifier is not None and bool(getattr(notifier, "enabled", False))
    if not enabled:
        # No topic configured (or the notifier is the documented no-op): there is nothing to
        # subscribe to, and saying so is better than a 202 that leads nowhere.
        LOGGER.warning("subscribe_unavailable reason=not_configured")
        return 503, {
            "status": SUBSCRIPTION_STATUS_UNAVAILABLE,
            "error": "subscription_unavailable",
            "message": SUBSCRIPTION_MESSAGE_UNAVAILABLE,
        }

    identity = redact_email(address)
    window_sort = window_sort_key(now_epoch, SUBSCRIPTION_WINDOW_SECONDS)
    ttl = window_expiry(now_epoch, SUBSCRIPTION_WINDOW_SECONDS, SUBSCRIPTION_RATE_TTL_SECONDS)
    # The caller's own budget is claimed first, so a caller with nothing left cannot spend
    # somebody else's address budget, and the answer is the same 429 either way: whether an
    # address has already been asked about is not something a stranger can read out of this
    # endpoint. The counter holds a digest and a number, never the address or the source.
    try:
        if source_ip:
            source_attempts = store.claim_subscribe_attempt(
                {"pk": source_rate_limit_key(source_ip), "sk": window_sort},
                limit=SUBSCRIPTION_SOURCE_RATE_LIMIT,
                ttl=ttl,
                now_epoch=now_epoch,
            )
            if source_attempts is None:
                LOGGER.warning(
                    "subscribe_rate_limited scope=source identity=%s source=%s",
                    identity,
                    redact_source(source_ip),
                )
                return 429, {
                    "status": SUBSCRIPTION_STATUS_RATE_LIMITED,
                    "error": "rate_limited",
                    "message": SUBSCRIPTION_MESSAGE_RATE_LIMITED,
                }
        # The attempt is counted *before* the SNS call, so a caller cannot spend a failing
        # endpoint's worth of requests: three confirmation requests an hour per address,
        # whether or not SNS accepted them.
        attempts = store.claim_subscribe_attempt(
            {"pk": rate_limit_key(address), "sk": window_sort},
            limit=SUBSCRIPTION_RATE_LIMIT,
            ttl=ttl,
            now_epoch=now_epoch,
        )
    except Exception as exc:  # noqa: BLE001 - every store failure is a transient 503
        # A counter that cannot be read or written is not the notification service's fault,
        # but it is not the visitor's either: report it as "could not complete" rather than
        # letting it become the route's only unhandled 500.
        LOGGER.warning("subscribe_unavailable reason=throttle_unavailable error=%s", type(exc).__name__)
        return 503, {
            "status": SUBSCRIPTION_STATUS_UNAVAILABLE,
            "error": "subscription_unavailable",
            "message": SUBSCRIPTION_MESSAGE_UNAVAILABLE,
        }

    if attempts is None:
        LOGGER.warning("subscribe_rate_limited scope=address identity=%s", identity)
        return 429, {
            "status": SUBSCRIPTION_STATUS_RATE_LIMITED,
            "error": "rate_limited",
            "message": SUBSCRIPTION_MESSAGE_RATE_LIMITED,
        }

    outcome = notifier.subscribe_email(address)
    if not outcome.get("requested"):
        # A refused or failed SNS call is reported as a transient problem, never as a stack
        # trace or an AWS error message: the detail is already ARN-redacted, and is logged
        # (with the address replaced by its digest) rather than returned.
        LOGGER.warning(
            "subscribe_failed identity=%s reason=%s detail=%s",
            identity,
            outcome.get("reason"),
            redact_addresses(str(outcome.get("detail") or ""), address)[:200],
        )
        return 503, {
            "status": SUBSCRIPTION_STATUS_UNAVAILABLE,
            "error": "subscription_unavailable",
            "message": SUBSCRIPTION_MESSAGE_UNAVAILABLE,
        }

    LOGGER.info(
        "subscribe_requested identity=%s state=%s attempt=%s/%s",
        identity,
        outcome.get("state"),
        attempts,
        SUBSCRIPTION_RATE_LIMIT,
    )
    return 202, {
        "status": SUBSCRIPTION_STATUS_CONFIRMATION_REQUIRED,
        "message": SUBSCRIPTION_MESSAGE_CONFIRMATION_REQUIRED,
    }


#: Any token that looks like an address, for the log-line guard below. Deliberately wider
#: than the subscription syntax check: the point is to catch a *third party's* rendering of
#: an address (an AWS error message, a differently-cased echo, a truncated copy), not to
#: validate one. A percent-escaped ``%40`` form is matched too.
ADDRESS_IN_TEXT = re.compile(r"[^\s@]+@[^\s@]+|[^\s@%]+%40[^\s@]+")


def redact_addresses(text: str, address: str | None = None) -> str:
    """Remove every address-shaped token from a log line.

    AWS error messages for a bad endpoint quote the endpoint, so the one place a full
    address could reach the log is an error string. Replacing the exact submitted spelling
    alone was too narrow: an error string may repeat it in different case, truncated, or
    percent-escaped. The submitted spelling is therefore replaced by its digest (so the
    line stays useful) and *any* remaining address-shaped token is masked outright.
    """
    if not text:
        return ""
    rendered = str(text)
    if address:
        rendered = rendered.replace(str(address), redact_email(address))
    return ADDRESS_IN_TEXT.sub("[redacted-address]", rendered)


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #


def _normalise_path(path: str) -> str:
    path = path.split("?", 1)[0]
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return path or "/"


def dispatch(
    method: str,
    path: str,
    body: str | None = None,
    store: Any = None,
    now_epoch: float | None = None,
    query: Mapping[str, Any] | None = None,
    headers: Mapping[str, Any] | None = None,
    notifier: Any = None,
    cooldown_seconds: int | None = None,
    env: Mapping[str, str] | None = None,
    weather_client: Any = None,
    source_ip: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """Route one request without touching AWS. Returns ``(status_code, payload)``.

    ``headers`` is only consulted for ``POST /readings`` (the ingestion secret).
    ``notifier`` and ``cooldown_seconds`` default to the environment configuration.
    ``env`` (default: the process environment) selects the active zone fleet and the
    weather configuration; ``weather_client`` injects a weather provider for tests so
    the test suite never performs a network call. ``source_ip`` (from
    ``requestContext.http.sourceIp``) is used only to key the subscription route's
    per-caller budget and is never echoed or stored in plaintext.
    """
    store = store if store is not None else get_store()
    now_epoch = time.time() if now_epoch is None else float(now_epoch)
    query = query or {}
    method = (method or "").upper()
    route = _normalise_path(path or "/")
    environment: Mapping[str, str] = os.environ if env is None else env
    mode, definitions = _apply_zone_mode(store, environment)
    active_zone_ids = tuple(str(zone["zone_id"]) for zone in definitions)

    if route == "/readings":
        if method != "POST":
            return 405, {"error": "method_not_allowed", "allowed": ["POST"], "path": route}
        denial = check_ingest(headers)
        if denial is not None:
            # One line per rejected attempt: this is the only trace of a brute-force or
            # misconfigured client, and it never contains the header value itself.
            LOGGER.warning(
                "ingest_rejected reason=%s status=%s", denial[1].get("error"), denial[0]
            )
            return denial
        # In live-weather mode a posted reading is still accepted for a live city, but it is
        # stored and served as SIMULATED: the zone allowlist keeps demo zones out, and the
        # provenance on the record - not the fleet's label - is what the dashboard shows, so
        # a posted value can never be presented as a REAL provider observation.
        return _handle_reading(
            body,
            store,
            now_epoch,
            notifier if notifier is not None else get_notifier(),
            alert_cooldown_seconds() if cooldown_seconds is None else int(cooldown_seconds),
            zone_ids=active_zone_ids,
            content_type=_header_value(headers, "content-type"),
        )

    if route == "/zones":
        if method != "GET":
            return 405, {"error": "method_not_allowed", "allowed": ["GET"], "path": route}
        return _handle_zones(
            store,
            now_epoch,
            zones=definitions,
            mode=mode,
            env=environment,
            notifier=notifier if notifier is not None else get_notifier(),
            cooldown_seconds=alert_cooldown_seconds() if cooldown_seconds is None else int(cooldown_seconds),
            weather_client=weather_client,
        )

    if route == "/incidents":
        if method != "GET":
            return 405, {"error": "method_not_allowed", "allowed": ["GET"], "path": route}
        return _handle_incidents(
            store,
            now_epoch,
            query,
            notifier if notifier is not None else get_notifier(),
            alert_cooldown_seconds() if cooldown_seconds is None else int(cooldown_seconds),
            zone_ids=active_zone_ids,
            env=environment,
            active_mode=mode,
        )

    if route == "/subscriptions":
        if method != "POST":
            return 405, {"error": "method_not_allowed", "allowed": ["POST"], "path": route}
        # Same media-type contract as POST /readings. The dashboard already sends
        # application/json; without this check the two public POST routes disagreed about
        # what they accept, which a client could only discover by experiment.
        rejection = _reject_content_type(_header_value(headers, "content-type"))
        if rejection is not None:
            return rejection
        # Deliberately no ingestion key: this route is for anonymous visitors, and its
        # bounds are the body limit, the address syntax check, the per-address throttle and
        # the topic that only the environment can name.
        return _handle_subscriptions(
            body,
            store,
            now_epoch,
            notifier if notifier is not None else get_notifier(),
            source_ip=source_ip,
        )

    if route.startswith("/zones/"):
        parts = [part for part in route.split("/") if part]
        if len(parts) == 3 and parts[2] == "history":
            if method != "GET":
                return 405, {"error": "method_not_allowed", "allowed": ["GET"], "path": route}
            return _handle_history(
                store, parts[1], now_epoch, query, zone_ids=active_zone_ids, active_mode=mode
            )
        return 404, {"error": "not_found", "path": _brief_text(route, limit=180)}

    return 404, {"error": "not_found", "path": _brief_text(route, limit=180)}


def _parse_event(event: Mapping[str, Any]) -> tuple[str, str, dict[str, Any], str | None, dict[str, Any]]:
    """Read method/path/query/body/headers from an API Gateway v2 *or* v1 event."""
    request_context = event.get("requestContext") or {}
    http = (request_context.get("http") or {}) if isinstance(request_context, Mapping) else {}
    method = http.get("method") or event.get("httpMethod") or ""
    path = event.get("rawPath") or event.get("path") or "/"
    query = event.get("queryStringParameters") or {}
    headers = event.get("headers") or {}

    body = event.get("body")
    if body is not None and not isinstance(body, str):
        # API Gateway always supplies a string. Anything else means a direct invocation
        # (or a future integration) handed over a decoded object; that is a bad request,
        # not an internal error, so it is refused with a reason.
        raise ValidationError("request body must be a JSON string", "body")
    if body is not None and event.get("isBase64Encoded"):
        try:
            body = base64.b64decode(body).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
            raise ValidationError(f"body is not valid base64 UTF-8 ({exc})", "body") from exc
    return str(method), str(path), dict(query), body, dict(headers)


def _jsonable(value: Any) -> Any:
    """Copy ``value`` with any non-finite float replaced by ``None``.

    BUG CLASS (M16/M17): ``json.dumps`` writes ``NaN``/``Infinity`` as a bare token, which is not
    JSON; one such value anywhere in a payload makes a browser reject the *whole* document. The
    individual writers already keep non-finite numbers out (``_readings_without_non_finite_numbers``,
    the history reader, the scoring guards); this is the last line of defence at the response
    boundary, so no route can emit an unparseable body even if a new one forgets.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _respond(status: int, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Wrap a payload as an API Gateway (proxy) response."""
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        # allow_nan=False makes an invalid number an exception here rather than an invalid body
        # on the wire; _jsonable means it should never come to that.
        "body": json.dumps(_jsonable(payload), separators=(",", ":"), sort_keys=False, allow_nan=False),
    }


def source_ip_of(event: Mapping[str, Any]) -> str | None:
    """The caller's source address as API Gateway reports it, or ``None``.

    Only the subscription route uses it, and only as a digest: it bounds what one caller
    can spend per hour. An event without a request context (a unit test, a local run)
    yields ``None``, which leaves the per-address counter as the only bound.
    """
    request_context = event.get("requestContext")
    if not isinstance(request_context, Mapping):
        return None
    http = request_context.get("http")
    if isinstance(http, Mapping):
        value = http.get("sourceIp")
        if isinstance(value, str) and value.strip():
            return value.strip()
    value = request_context.get("sourceIp") or request_context.get("identity")
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, Mapping):
        inner = value.get("sourceIp")
        if isinstance(inner, str) and inner.strip():
            return inner.strip()
    return None


def lambda_handler(event: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    """AWS Lambda entry point for the single MVP function."""
    try:
        method, path, query, body, headers = _parse_event(event)
    except ValidationError as exc:
        return _respond(400, {"error": "invalid_body", "field": exc.field, "detail": exc.reason})

    try:
        status, payload = dispatch(
            method,
            path,
            body,
            get_store(),
            time.time(),
            query,
            headers,
            source_ip=source_ip_of(event),
        )
    except Exception:  # noqa: BLE001 - never leak internals to the caller
        LOGGER.exception("unhandled error while processing request")
        return _respond(500, {
            "error": "internal_error",
            "detail": "the request could not be processed",
        })
    return _respond(status, payload)

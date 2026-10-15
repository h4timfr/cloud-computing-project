"""The single FloodPulse Lambda handler (MVP: one function, all routes).

Routes:

* ``POST /readings``                -- validate, score, store (requires the ``x-fp-key`` header),
                                       then open/escalate/close an incident and alert if warranted
* ``GET  /zones``                   -- latest state per zone + ranking (public)
* ``GET  /zones/{zone_id}/history`` -- timestamped history for one zone (public)
* ``GET  /incidents``               -- flood incident log, newest first (public)

The read endpoints are deliberately unauthenticated: the dashboard in S3 needs them and
they expose only simulated data. ``POST /readings`` is protected by the shared secret in
``FLOODPULSE_INGEST_KEY`` (see ``auth.py``); the secret is never accepted, required or
revealed on the read routes. The SNS topic ARN is never returned to any client.

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
import time
from datetime import datetime, timezone
from typing import Any, Mapping

from .auth import check_ingest
from .config import (
    ALERT_COOLDOWN_ENV,
    ALERT_COOLDOWN_MAX_SECONDS,
    ALERT_COOLDOWN_MIN_SECONDS,
    ALERT_COOLDOWN_SECONDS,
    DEFERRED_FEATURES,
    HISTORY_DEFAULT_MINUTES,
    HISTORY_MAX_MINUTES,
    INCIDENT_DEFAULT_LIMIT,
    INCIDENT_MAX_LIMIT,
    INCIDENT_STATUS_OPEN,
    MAX_HISTORY_POINTS,
    RISK_MODEL_ID,
    SIMULATION_DISCLAIMER,
    SMOOTHING_WINDOW,
    TREND_LOOKBACK_SECONDS,
    TREND_LOOKBACK_TOLERANCE_SECONDS,
    ZONE_DEFINITIONS,
    ZONE_IDS,
    thresholds_for,
    zone_definition,
)
from .incidents import (
    ACTION_CLOSED,
    ACTION_ESCALATED,
    ACTION_NONE,
    ACTION_OPENED,
    alerting_view,
    build_alert_entry,
    build_alert_marker,
    cooldown_remaining_seconds,
    decide_transition,
    incident_view,
    record_alert,
)
from .notify import (
    ALERT_KIND_ESCALATED,
    ALERT_KIND_OPENED,
    NoopNotifier,
    alert_subject,
    build_alert_message,
    notifier_for_env,
)
from .risk import MODEL_NOTE, apply_hysteresis, rolling_mean_metrics, score_zone
from .storage import LocalStore
from .validation import ValidationError, validate_body_size, validate_reading

LOGGER = logging.getLogger("floodpulse.handler")

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


# --------------------------------------------------------------------------- #
# Incidence tracking and alerting (M3)
# --------------------------------------------------------------------------- #


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
) -> dict[str, Any]:
    """Publish one alert when the rules allow it.

    Ordering matters: the cooldown marker is only advanced after a *successful* publish,
    so a transient SNS failure does not silently mute the next attempt. Never returns the
    topic ARN (a notifier error is redacted before it is reported).
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

    kind = ALERT_KIND_ESCALATED if action == ACTION_ESCALATED else ALERT_KIND_OPENED
    zone_name = str(incident.get("zone_name") or zone_id)
    try:
        result = notifier.publish(
            alert_subject(kind, zone_name, alert_severity),
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
            )
        )
    else:
        outcome["reason"] = str(result.get("reason") or "publish_failed")
        if result.get("detail"):
            outcome["detail"] = str(result["detail"])[:200]
    return outcome


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
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply the incident rules for one scored reading and alert if required.

    ``risk_class`` must be the *effective* class, so the existing hysteresis streaks
    govern both opening and closing.
    """
    plan = decide_transition(
        zone_id=zone_id,
        active=store.latest_incident(zone_id),
        risk_class=risk_class,
        risk_score=risk_score,
        top_driver=top_driver,
        observed_ts_epoch=observed_ts_epoch,
        now_epoch=now_epoch,
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
        if not store.create_incident(incident):
            # A concurrent request opened the incident first. Adopt the stored record
            # instead of writing a duplicate, and do not alert (the winner did).
            existing = store.latest_incident(zone_id)
            if existing is None or existing.get("current_status") != INCIDENT_STATUS_OPEN:
                return _no_incident_summary("concurrent incident creation was suppressed"), {
                    "evaluated": False,
                    "published": False,
                    "severity": None,
                    "reason": "duplicate_suppressed",
                }
            incident, action, alert_severity = existing, "observed", None
            reason = "an incident was already open (duplicate suppressed)"
            store.put_incident(incident)
    else:
        store.put_incident(incident)

    alert = _maybe_alert(
        store,
        notifier,
        zone_id=zone_id,
        incident=incident,
        action=action,
        alert_severity=alert_severity,
        now_epoch=now_epoch,
        cooldown_seconds=cooldown_seconds,
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
        store.put_incident(incident)

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


def _handle_reading(
    body: str | None,
    store: LocalStore,
    now_epoch: float,
    notifier: Any = None,
    cooldown_seconds: int = 0,
) -> tuple[int, dict[str, Any]]:
    try:
        validate_body_size(body)
    except ValidationError as exc:
        return 400, {"error": "invalid_body", "field": exc.field, "detail": exc.reason}

    try:
        payload = json.loads(body)  # type: ignore[arg-type]
    except (json.JSONDecodeError, ValueError) as exc:
        return 400, {"error": "malformed_json", "field": "body", "detail": str(exc)}

    try:
        reading = validate_reading(payload, now_epoch, ZONE_IDS)
    except ValidationError as exc:
        return 400, {"error": "invalid_payload", "field": exc.field, "detail": exc.reason}

    zone_id = str(reading["zone_id"])
    previous = store.recent_readings(zone_id, SMOOTHING_WINDOW - 1)
    series = [dict(item["readings"]) for item in previous] + [dict(reading["readings"])]
    smoothed = rolling_mean_metrics(series, SMOOTHING_WINDOW)

    lookback = _water_level_lookback(store, zone_id, int(reading["ts_epoch"]))
    risk = score_zone(
        smoothed,
        thresholds_for(zone_id),
        water_level_5min_ago=lookback,
        water_level_now=float(reading["readings"]["water_level_m"]),
        smoothing_window=len(series),
    )

    previous_latest = store.get_latest(zone_id)
    previous_state = (previous_latest or {}).get("hysteresis")
    hysteresis = apply_hysteresis(risk["risk_class_candidate"], previous_state)

    latest, history = store.put_reading(reading, risk, hysteresis, smoothed)

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

    return 202, {
        "status": "accepted",
        "simulated": True,
        "data_classification": reading["data_classification"],
        "zone_id": zone_id,
        "sensor_node_id": reading["sensor_node_id"],
        "timestamp": reading["timestamp"],
        "ts_epoch": reading["ts_epoch"],
        "ttl": reading["ttl"],
        "sequence": latest["sequence"],
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
        "stored": {
            "latest_sk": latest["sk"],
            "history_sk": history["sk"],
            "history_records": store.history_count(zone_id),
        },
        "model_note": MODEL_NOTE,
        "deferred_features": list(DEFERRED_FEATURES),
    }


# --------------------------------------------------------------------------- #
# GET /zones
# --------------------------------------------------------------------------- #


def _zone_view(zone: Mapping[str, Any], latest: Mapping[str, Any] | None, now_epoch: float) -> dict[str, Any]:
    view: dict[str, Any] = {
        "zone_id": zone["zone_id"],
        "name": zone["name"],
        "latitude": zone["latitude"],
        "longitude": zone["longitude"],
        "description": zone["description"],
        "thresholds": thresholds_for(str(zone["zone_id"])),
        "status": "no_data" if latest is None else "live",
        "latest": None,
    }
    if latest is None:
        return view

    detail = latest.get("risk_detail", {})
    view["latest"] = {
        "simulated": True,
        "data_classification": latest["data_classification"],
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


def _handle_zones(store: LocalStore, now_epoch: float) -> tuple[int, dict[str, Any]]:
    views = [_zone_view(zone, store.get_latest(str(zone["zone_id"])), now_epoch) for zone in ZONE_DEFINITIONS]
    with_data = [view for view in views if view["latest"] is not None]
    without_data = [view for view in views if view["latest"] is None]

    ranked = sorted(with_data, key=lambda view: (-float(view["latest"]["risk_score"]), view["zone_id"]))
    ranking = [view["zone_id"] for view in ranked] + [view["zone_id"] for view in without_data]

    highest: dict[str, Any] | None = None
    if ranked:
        top = ranked[0]
        highest = {
            "zone_id": top["zone_id"],
            "name": top["name"],
            "risk_score": top["latest"]["risk_score"],
            "risk_class": top["latest"]["risk_class"],
            "timestamp": top["latest"]["timestamp"],
        }

    return 200, {
        "generated_at": _utc_iso(now_epoch),
        "simulated": True,
        "data_classification": "simulated",
        "model": RISK_MODEL_ID,
        "model_note": MODEL_NOTE,
        "notes": [
            SIMULATION_DISCLAIMER,
            "Time-to-threshold is not implemented yet (deferred until after the live dashboard).",
        ],
        "highest_risk_zone": highest,
        "zone_ranking": ranking,
        "zone_count": len(views),
        "zones": views,
        "deferred_features": list(DEFERRED_FEATURES),
    }


# --------------------------------------------------------------------------- #
# GET /zones/{zone_id}/history
# --------------------------------------------------------------------------- #


def _parse_minutes(raw: Any) -> int:
    if raw is None:
        return HISTORY_DEFAULT_MINUTES
    if isinstance(raw, bool):
        raise ValidationError("must be an integer number of minutes", "minutes")
    if isinstance(raw, int):
        minutes = raw
    elif isinstance(raw, str) and raw.strip().lstrip("-").isdigit():
        minutes = int(raw.strip())
    else:
        raise ValidationError("must be an integer number of minutes", "minutes")
    if not 1 <= minutes <= HISTORY_MAX_MINUTES:
        raise ValidationError(f"must be between 1 and {HISTORY_MAX_MINUTES}", "minutes")
    return minutes


def _handle_history(
    store: LocalStore, zone_id: str, now_epoch: float, query: Mapping[str, Any]
) -> tuple[int, dict[str, Any]]:
    if zone_id not in ZONE_IDS:
        return 404, {
            "error": "unknown_zone",
            "detail": f"unknown zone_id {zone_id!r}",
            "known_zones": list(ZONE_IDS),
        }
    try:
        minutes = _parse_minutes(query.get("minutes"))
    except ValidationError as exc:
        return 400, {"error": "invalid_query", "field": exc.field, "detail": exc.reason}

    since_epoch = int(now_epoch) - minutes * 60
    records = store.get_history(zone_id, since_epoch=since_epoch)

    stride = max(1, math.ceil(len(records) / MAX_HISTORY_POINTS))
    sampled = records[::stride]

    points = [
        {
            "timestamp": item["timestamp"],
            "ts_epoch": item["ts_epoch"],
            "risk_score": item["risk_score"],
            "risk_class": item["risk_class"],
            "risk_class_candidate": item["risk_class_candidate"],
            "readings": item["readings"],
        }
        for item in sampled
    ]

    zone = zone_definition(zone_id)
    return 200, {
        "zone_id": zone_id,
        "name": zone["name"],
        "minutes": minutes,
        "since_epoch": since_epoch,
        "point_count": len(points),
        "downsampled": stride > 1,
        "stride": stride,
        "simulated": True,
        "data_classification": "simulated",
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
    elif isinstance(raw, str) and raw.strip().isdigit():
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
) -> tuple[int, dict[str, Any]]:
    raw_zone = query.get("zone_id")
    zone_id: str | None = None
    if raw_zone is not None and str(raw_zone).strip() != "":
        zone_id = str(raw_zone).strip()
        if zone_id not in ZONE_IDS:
            return 404, {
                "error": "unknown_zone",
                "detail": f"unknown zone_id {zone_id!r}",
                "known_zones": list(ZONE_IDS),
            }
    try:
        limit = _parse_limit(query.get("limit"))
    except ValidationError as exc:
        return 400, {"error": "invalid_query", "field": exc.field, "detail": exc.reason}

    incidents = [incident_view(item, now_epoch) for item in store.list_incidents(zone_id=zone_id, limit=limit)]
    open_count = sum(1 for view in incidents if view["is_open"])

    return 200, {
        "generated_at": _utc_iso(now_epoch),
        "simulated": True,
        "data_classification": "simulated",
        "model": RISK_MODEL_ID,
        "model_note": MODEL_NOTE,
        "zone_id": zone_id,
        "limit": limit,
        "count": len(incidents),
        "open_count": open_count,
        "closed_count": len(incidents) - open_count,
        "alerting": alerting_view(notifier is not None and getattr(notifier, "enabled", False), cooldown_seconds),
        "incidents": incidents,
        "notes": [
            SIMULATION_DISCLAIMER,
            "Incidents record heuristic band crossings: a zone opens one on reaching "
            "WARNING or CRITICAL and it closes once the zone returns to NORMAL.",
        ],
        "deferred_features": list(DEFERRED_FEATURES),
    }


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
) -> tuple[int, dict[str, Any]]:
    """Route one request without touching AWS. Returns ``(status_code, payload)``.

    ``headers`` is only consulted for ``POST /readings`` (the ingestion secret).
    ``notifier`` and ``cooldown_seconds`` default to the environment configuration.
    """
    store = store if store is not None else get_store()
    now_epoch = time.time() if now_epoch is None else float(now_epoch)
    query = query or {}
    method = (method or "").upper()
    route = _normalise_path(path or "/")

    if route == "/readings":
        if method != "POST":
            return 405, {"error": "method_not_allowed", "allowed": ["POST"], "path": route}
        denial = check_ingest(headers)
        if denial is not None:
            return denial
        return _handle_reading(
            body,
            store,
            now_epoch,
            notifier if notifier is not None else get_notifier(),
            alert_cooldown_seconds() if cooldown_seconds is None else int(cooldown_seconds),
        )

    if route == "/zones":
        if method != "GET":
            return 405, {"error": "method_not_allowed", "allowed": ["GET"], "path": route}
        return _handle_zones(store, now_epoch)

    if route == "/incidents":
        if method != "GET":
            return 405, {"error": "method_not_allowed", "allowed": ["GET"], "path": route}
        return _handle_incidents(
            store,
            now_epoch,
            query,
            notifier if notifier is not None else get_notifier(),
            alert_cooldown_seconds() if cooldown_seconds is None else int(cooldown_seconds),
        )

    if route.startswith("/zones/"):
        parts = [part for part in route.split("/") if part]
        if len(parts) == 3 and parts[2] == "history":
            if method != "GET":
                return 405, {"error": "method_not_allowed", "allowed": ["GET"], "path": route}
            return _handle_history(store, parts[1], now_epoch, query)
        return 404, {"error": "not_found", "path": route}

    return 404, {"error": "not_found", "path": route}


def _parse_event(event: Mapping[str, Any]) -> tuple[str, str, dict[str, Any], str | None, dict[str, Any]]:
    """Read method/path/query/body/headers from an API Gateway v2 *or* v1 event."""
    request_context = event.get("requestContext") or {}
    http = (request_context.get("http") or {}) if isinstance(request_context, Mapping) else {}
    method = http.get("method") or event.get("httpMethod") or ""
    path = event.get("rawPath") or event.get("path") or "/"
    query = event.get("queryStringParameters") or {}
    headers = event.get("headers") or {}

    body = event.get("body")
    if body is not None and event.get("isBase64Encoded"):
        try:
            body = base64.b64decode(body).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
            raise ValidationError(f"body is not valid base64 UTF-8 ({exc})", "body") from exc
    return str(method), str(path), dict(query), body, dict(headers)


def _respond(status: int, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Wrap a payload as an API Gateway (proxy) response."""
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(payload, separators=(",", ":"), sort_keys=False),
    }


def lambda_handler(event: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    """AWS Lambda entry point for the single MVP function."""
    try:
        method, path, query, body, headers = _parse_event(event)
    except ValidationError as exc:
        return _respond(400, {"error": "invalid_body", "field": exc.field, "detail": exc.reason})

    try:
        status, payload = dispatch(method, path, body, get_store(), time.time(), query, headers)
    except Exception:  # noqa: BLE001 - never leak internals to the caller
        LOGGER.exception("unhandled error while processing request")
        return _respond(500, {
            "error": "internal_error",
            "detail": "the request could not be processed",
        })
    return _respond(status, payload)

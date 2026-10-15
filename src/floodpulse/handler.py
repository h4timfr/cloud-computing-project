"""The single FloodPulse Lambda handler (MVP: one function, all routes).

Routes implemented at M0/M1:

* ``POST /readings``                -- validate, score, store
* ``GET  /zones``                   -- latest state per zone + ranking
* ``GET  /zones/{zone_id}/history`` -- timestamped history for one zone

Deliberately **not** implemented yet (deferred, see ``config.DEFERRED_FEATURES``):
time-to-threshold, SNS alerts, incident creation, dashboard, AWS deployment.

``dispatch()`` is the pure, AWS-free entry point used by tests and by
``tools/local_demo.py``. ``lambda_handler()`` only adapts an API Gateway event.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import math
import time
from datetime import datetime, timezone
from typing import Any, Mapping

from .config import (
    DEFERRED_FEATURES,
    HISTORY_DEFAULT_MINUTES,
    HISTORY_MAX_MINUTES,
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
from .risk import MODEL_NOTE, apply_hysteresis, rolling_mean_metrics, score_zone
from .storage import LocalStore
from .validation import ValidationError, validate_body_size, validate_reading

LOGGER = logging.getLogger("floodpulse.handler")

_STORE: LocalStore | None = None


def get_store() -> LocalStore:
    """Process-wide store (an in-memory store at M0; swapped for DynamoDB at M1)."""
    global _STORE
    if _STORE is None:
        _STORE = LocalStore()
    return _STORE


def set_store(store: LocalStore | None) -> None:
    """Replace the process-wide store (used by tests and, later, by M1 wiring)."""
    global _STORE
    _STORE = store


def _utc_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


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


def _handle_reading(body: str | None, store: LocalStore, now_epoch: float) -> tuple[int, dict[str, Any]]:
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
    store: LocalStore | None = None,
    now_epoch: float | None = None,
    query: Mapping[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Route one request without touching AWS. Returns ``(status_code, payload)``."""
    store = store if store is not None else get_store()
    now_epoch = time.time() if now_epoch is None else float(now_epoch)
    query = query or {}
    method = (method or "").upper()
    route = _normalise_path(path or "/")

    if route == "/readings":
        if method != "POST":
            return 405, {"error": "method_not_allowed", "allowed": ["POST"], "path": route}
        return _handle_reading(body, store, now_epoch)

    if route == "/zones":
        if method != "GET":
            return 405, {"error": "method_not_allowed", "allowed": ["GET"], "path": route}
        return _handle_zones(store, now_epoch)

    if route.startswith("/zones/"):
        parts = [part for part in route.split("/") if part]
        if len(parts) == 3 and parts[2] == "history":
            if method != "GET":
                return 405, {"error": "method_not_allowed", "allowed": ["GET"], "path": route}
            return _handle_history(store, parts[1], now_epoch, query)
        return 404, {"error": "not_found", "path": route}

    return 404, {"error": "not_found", "path": route}


def _parse_event(event: Mapping[str, Any]) -> tuple[str, str, dict[str, Any], str | None]:
    """Read method/path/query/body from an API Gateway v2 *or* v1 event."""
    request_context = event.get("requestContext") or {}
    http = (request_context.get("http") or {}) if isinstance(request_context, Mapping) else {}
    method = http.get("method") or event.get("httpMethod") or ""
    path = event.get("rawPath") or event.get("path") or "/"
    query = event.get("queryStringParameters") or {}

    body = event.get("body")
    if body is not None and event.get("isBase64Encoded"):
        try:
            body = base64.b64decode(body).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
            raise ValidationError(f"body is not valid base64 UTF-8 ({exc})", "body") from exc
    return str(method), str(path), dict(query), body


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
        method, path, query, body = _parse_event(event)
    except ValidationError as exc:
        return _respond(400, {"error": "invalid_body", "field": exc.field, "detail": exc.reason})

    try:
        status, payload = dispatch(method, path, body, get_store(), time.time(), query)
    except Exception:  # noqa: BLE001 - never leak internals to the caller
        LOGGER.exception("unhandled error while processing request")
        return _respond(500, {
            "error": "internal_error",
            "detail": "the request could not be processed",
        })
    return _respond(status, payload)

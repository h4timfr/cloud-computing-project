"""Strict validation of externally supplied sensor payloads (``POST /readings``).

Rules locked for the MVP:

* the client supplies ``timestamp`` only; ``ts_epoch`` and ``ttl`` are derived
  server-side here
* ``simulated`` must be ``true`` -- the MVP accepts simulated data only
* unknown fields are rejected, numbers must be finite and inside the configured range

All functions are pure; no AWS calls, no clock reads (``now_epoch`` is injected).
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from .config import (
    BODY_MAX_BYTES,
    DATA_CLASSIFICATION_MIXED,
    HISTORY_TTL_SECONDS,
    MAX_CLOCK_SKEW_SECONDS,
    METRIC_ORDER,
    METRICS,
    SCHEMA_VERSION,
    SOURCE_SIMULATED,
)

ALLOWED_TOP_LEVEL = frozenset(
    {"schema_version", "simulated", "zone_id", "sensor_node_id", "timestamp", "readings", "meta"}
)
ALLOWED_META = frozenset({"run_id", "seq", "phase"})

SENSOR_NODE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
ZONE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
PHASE_PATTERN = re.compile(r"^[a-z_]{1,24}$")

DATA_CLASSIFICATION = "simulated"


class ValidationError(ValueError):
    """A rejected payload. ``field`` names the offending field when known."""

    def __init__(self, reason: str, field: str | None = None) -> None:
        self.reason = reason
        self.field = field
        super().__init__(f"{field}: {reason}" if field else reason)


def _require(condition: bool, reason: str, field: str | None = None) -> None:
    if not condition:
        raise ValidationError(reason, field)


def validate_body_size(raw_body: str | None) -> None:
    """Reject missing, blank or oversized request bodies before parsing JSON.

    A blank body is reported as missing rather than as malformed JSON: "you sent nothing"
    is the more useful answer for a client that forgot the payload, and it keeps the
    distinction out of the caller's way.
    """
    _require(raw_body is not None, "request body is required", "body")
    text = raw_body if isinstance(raw_body, str) else str(raw_body)  # type: ignore[union-attr]
    _require(text.strip() != "", "request body is required", "body")
    size = len(text.encode("utf-8"))
    _require(
        size <= BODY_MAX_BYTES,
        f"request body is {size} bytes; limit is {BODY_MAX_BYTES}",
        "body",
    )


def parse_utc_timestamp(value: Any) -> datetime:
    """Parse an ISO-8601 UTC timestamp that must end in ``Z``."""
    _require(isinstance(value, str), "must be an ISO-8601 UTC string", "timestamp")
    text = value.strip()  # type: ignore[union-attr]
    _require(text.endswith("Z"), "must end with 'Z' (UTC only)", "timestamp")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise ValidationError(f"not a valid ISO-8601 timestamp ({exc})", "timestamp") from exc
    return parsed.astimezone(timezone.utc)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_readings(readings: Any) -> dict[str, float]:
    _require(isinstance(readings, Mapping), "must be an object of metric values", "readings")
    provided = set(readings)  # type: ignore[arg-type]
    expected = set(METRIC_ORDER)
    missing = sorted(expected - provided)
    extra = sorted(provided - expected)
    _require(not missing, f"missing metric(s): {', '.join(missing)}", "readings")
    _require(not extra, f"unexpected metric(s): {', '.join(extra)}", "readings")

    cleaned: dict[str, float] = {}
    for metric in METRIC_ORDER:
        value = readings[metric]  # type: ignore[index]
        _require(_is_number(value), "must be a finite number", f"readings.{metric}")
        number = float(value)
        _require(math.isfinite(number), "must be finite (no NaN or Infinity)", f"readings.{metric}")
        low, high = float(METRICS[metric]["min"]), float(METRICS[metric]["max"])
        _require(
            low <= number <= high,
            f"must be within {low}..{high} {METRICS[metric]['unit']} (got {number})",
            f"readings.{metric}",
        )
        cleaned[metric] = round(number, 4)
    return cleaned


def _validate_meta(meta: Any) -> dict[str, Any]:
    if meta is None:
        return {}
    _require(isinstance(meta, Mapping), "must be an object when present", "meta")
    extra = sorted(set(meta) - ALLOWED_META)  # type: ignore[arg-type]
    _require(not extra, f"unexpected meta field(s): {', '.join(extra)}", "meta")

    cleaned: dict[str, Any] = {}
    if "run_id" in meta:
        run_id = meta["run_id"]  # type: ignore[index]
        _require(
            isinstance(run_id, str) and bool(RUN_ID_PATTERN.match(run_id)),
            "must match ^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$",
            "meta.run_id",
        )
        cleaned["run_id"] = run_id
    if "seq" in meta:
        seq = meta["seq"]  # type: ignore[index]
        _require(
            isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0,
            "must be a non-negative integer",
            "meta.seq",
        )
        cleaned["seq"] = seq
    if "phase" in meta:
        phase = meta["phase"]  # type: ignore[index]
        _require(
            isinstance(phase, str) and bool(PHASE_PATTERN.match(phase)),
            "must match ^[a-z_]{1,24}$",
            "meta.phase",
        )
        cleaned["phase"] = phase
    return cleaned


def validate_reading(
    payload: Any,
    now_epoch: float,
    zone_ids: Iterable[str],
    *,
    accept_non_simulated: bool = False,
    metric_sources: Mapping[str, str] | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Validate and normalise one reading, deriving server-side fields.

    Returns the normalised reading record used by the store, including
    ``ts_epoch``, ``ttl`` and ``data_classification``.

    ``accept_non_simulated`` is for FloodPulse's *own* ingestion path (M4 live weather),
    which is the only caller allowed to store a reading whose metrics are not all
    simulated. Externally supplied payloads must still declare ``simulated: true``.

    Every record states where each metric came from. A caller that supplies no
    ``metric_sources`` gets all-``SIMULATED`` tags, which is exactly what the external route
    guarantees, so a stored reading never has to be *interpreted* to work out its provenance
    and a live fleet's ``REAL`` rainfall tag can never be applied to a posted value. ``source``
    is still recorded only when supplied.
    """
    _require(isinstance(payload, Mapping), "payload must be a JSON object", "payload")
    extra = sorted(set(payload) - ALLOWED_TOP_LEVEL)  # type: ignore[arg-type]
    _require(not extra, f"unexpected field(s): {', '.join(extra)}", "payload")

    for field in ("schema_version", "simulated", "zone_id", "sensor_node_id", "timestamp", "readings"):
        _require(field in payload, "is required", field)  # type: ignore[operator]

    _require(
        payload["schema_version"] == SCHEMA_VERSION,  # type: ignore[index]
        f"must equal {SCHEMA_VERSION!r}",
        "schema_version",
    )
    if accept_non_simulated:
        _require(
            isinstance(payload["simulated"], bool),  # type: ignore[index]
            "must be a boolean",
            "simulated",
        )
    else:
        _require(
            payload["simulated"] is True,  # type: ignore[index]
            "must be true; the MVP accepts explicit simulated data only",
            "simulated",
        )

    zone_id = payload["zone_id"]  # type: ignore[index]
    _require(
        isinstance(zone_id, str) and bool(ZONE_ID_PATTERN.match(zone_id)),
        "must be a lowercase zone identifier",
        "zone_id",
    )
    allowed_zones = set(zone_ids)
    _require(
        zone_id in allowed_zones,
        f"unknown zone; known zones: {', '.join(sorted(allowed_zones))}",
        "zone_id",
    )

    sensor_node_id = payload["sensor_node_id"]  # type: ignore[index]
    _require(
        isinstance(sensor_node_id, str) and bool(SENSOR_NODE_PATTERN.match(sensor_node_id)),
        "must match ^[a-z0-9][a-z0-9-]{0,31}$",
        "sensor_node_id",
    )

    timestamp = parse_utc_timestamp(payload["timestamp"])
    ts_epoch = int(timestamp.timestamp())
    skew = abs(ts_epoch - int(now_epoch))
    _require(
        skew <= MAX_CLOCK_SKEW_SECONDS,
        f"timestamp is {skew}s from server time; limit is {MAX_CLOCK_SKEW_SECONDS}s",
        "timestamp",
    )

    readings = _validate_readings(payload["readings"])
    meta = _validate_meta(payload.get("meta"))

    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "simulated": bool(payload["simulated"]) if accept_non_simulated else True,
        "data_classification": _classification_for(metric_sources),
        "zone_id": zone_id,
        "sensor_node_id": sensor_node_id,
        "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
        "ts_epoch": ts_epoch,
        "ttl": ts_epoch + HISTORY_TTL_SECONDS,
        "readings": readings,
        "meta": meta,
        "ingested_at_epoch": int(now_epoch),
        "metric_sources": {
            metric: str((metric_sources or {}).get(metric, source or SOURCE_SIMULATED))
            for metric in METRIC_ORDER
        },
    }
    if source is not None:
        record["source"] = str(source)
    return record


def _classification_for(metric_sources: Mapping[str, str] | None) -> str:
    """``mixed`` as soon as one metric of the record comes from outside FloodPulse."""
    if not metric_sources:
        return DATA_CLASSIFICATION
    if any(str(tag) != SOURCE_SIMULATED for tag in metric_sources.values()):
        return DATA_CLASSIFICATION_MIXED
    return DATA_CLASSIFICATION

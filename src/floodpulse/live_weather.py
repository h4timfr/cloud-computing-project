"""Live-weather ingestion for the Indian city zones (milestone M4).

Where each number comes from
---------------------------
======================  ====================  =========================================
metric                  tag                   origin
======================  ====================  =========================================
``rainfall_mm_per_hr``  ``REAL``              Open-Meteo hourly precipitation, as delivered
``soil_saturation_pct`` ``MODEL_DERIVED``     Open-Meteo soil moisture, unit-converted
``drain_level_m``       ``SIMULATED``         deterministic hydraulic response (below)
``water_level_m``       ``SIMULATED``         deterministic hydraulic response (below)
``flow_rate_m3_s``      ``SIMULATED``         deterministic hydraulic response (below)
======================  ====================  =========================================

The hydraulic response is a first-order lag towards a target implied by the current
rainfall and soil saturation::

    target_drain = DRAIN_BASE + DRAIN_RAIN * clamp(rain / RAIN_REF, 0, 1)
                             + DRAIN_SOIL * soil_fraction
    drain_now    = drain_prev + (target_drain - drain_prev) * (1 - exp(-dt / TAU))
    water_now    = clamp(WATER_PER_DRAIN * drain_now + WATER_SOIL * soil_fraction)
    flow_now     = clamp(FLOW_PER_WATER * water_now + FLOW_PER_RAIN * rain)

It is deterministic, explainable and reproducible - and it is **not** hydrology: it exists
so the dashboard has a plausible drain/flow story driven by real rainfall.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

from .config import (
    HYDRAULICS_DRAIN_BASE_M,
    HYDRAULICS_DRAIN_DEFAULT_DT_SECONDS,
    HYDRAULICS_DRAIN_RAIN_M,
    HYDRAULICS_DRAIN_SOIL_M,
    HYDRAULICS_DRAIN_TAU_SECONDS,
    HYDRAULICS_FLOW_PER_RAIN,
    HYDRAULICS_FLOW_PER_WATER,
    HYDRAULICS_NOTE,
    HYDRAULICS_RAIN_REFERENCE_MM_PER_HR,
    HYDRAULICS_WATER_PER_DRAIN,
    HYDRAULICS_WATER_SOIL_PER_FRACTION,
    METRICS,
    METRIC_SOURCES_LIVE,
    SCHEMA_VERSION,
    WEATHER_ATTRIBUTION,
    WEATHER_CACHE_SORT_KEY,
    WEATHER_DOCS_URL,
    WEATHER_MODE_ENV,
    WEATHER_MODE_LIVE,
    WEATHER_MODE_OFF,
    WEATHER_PROVIDER,
    WEATHER_REFRESH_SECONDS,
    zone_definitions_for_mode,
)
from .storage import build_zone_pk
from .validation import validate_reading
from .weather import Observation, WeatherClient

#: Identifies readings that came from the live-weather path rather than a client POST.
LIVE_SOURCE = "open-meteo-live"
SENSOR_NODE_PREFIX = "open-meteo"
NO_CACHE = "no_cache"
DISABLED = "disabled"


def clamp(value: float, low: float, high: float) -> float:
    """Clamp ``value`` into ``[low, high]``."""
    return max(low, min(high, value))


def iso_utc(epoch: float) -> str:
    """Second-precision ISO-8601 UTC stamp."""
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sensor_node_id_for(zone_id: str) -> str:
    """A valid ``sensor_node_id`` describing the live-weather source for a zone."""
    node = f"{SENSOR_NODE_PREFIX}-{zone_id.removeprefix('city-')}"
    return node[:32]


# --------------------------------------------------------------------------- #
# Deterministic hydraulics
# --------------------------------------------------------------------------- #


def hydraulic_target_drain_m(rainfall_mm_per_hr: float, soil_saturation_pct: float) -> float:
    """Drain level the current conditions relax towards (metres)."""
    rain_fraction = clamp(rainfall_mm_per_hr / HYDRAULICS_RAIN_REFERENCE_MM_PER_HR, 0.0, 1.0)
    soil_fraction = clamp(soil_saturation_pct / 100.0, 0.0, 1.0)
    target = (
        HYDRAULICS_DRAIN_BASE_M
        + HYDRAULICS_DRAIN_RAIN_M * rain_fraction
        + HYDRAULICS_DRAIN_SOIL_M * soil_fraction
    )
    limits = METRICS["drain_level_m"]
    return clamp(target, limits["min"], limits["max"])


def hydraulic_drain_level_m(
    rainfall_mm_per_hr: float,
    soil_saturation_pct: float,
    previous_drain_m: float | None,
    elapsed_seconds: float,
) -> float:
    """First-order lag of the drain level towards the current target."""
    target = hydraulic_target_drain_m(rainfall_mm_per_hr, soil_saturation_pct)
    if previous_drain_m is None:
        return target
    dt = elapsed_seconds if elapsed_seconds > 0 else HYDRAULICS_DRAIN_DEFAULT_DT_SECONDS
    weight = 1.0 - math.exp(-dt / HYDRAULICS_DRAIN_TAU_SECONDS)
    limits = METRICS["drain_level_m"]
    return clamp(previous_drain_m + (target - previous_drain_m) * weight, limits["min"], limits["max"])


def hydraulic_water_level_m(drain_level_m: float, soil_saturation_pct: float) -> float:
    """Water level coupled to the drain level plus a soil-saturation term."""
    soil_fraction = clamp(soil_saturation_pct / 100.0, 0.0, 1.0)
    limits = METRICS["water_level_m"]
    value = HYDRAULICS_WATER_PER_DRAIN * drain_level_m + HYDRAULICS_WATER_SOIL_PER_FRACTION * soil_fraction
    return clamp(value, limits["min"], limits["max"])


def hydraulic_flow_rate_m3_s(water_level_m: float, rainfall_mm_per_hr: float) -> float:
    """Flow rate driven by the water level and the current rainfall."""
    limits = METRICS["flow_rate_m3_s"]
    value = HYDRAULICS_FLOW_PER_WATER * water_level_m + HYDRAULICS_FLOW_PER_RAIN * rainfall_mm_per_hr
    return clamp(value, limits["min"], limits["max"])


def build_live_metrics(
    observation: Observation,
    *,
    previous: Mapping[str, Any] | None = None,
    now_epoch: float,
) -> dict[str, float] | None:
    """Combine REAL/MODEL-DERIVED weather with SIMULATED hydraulics into five metrics."""
    if not observation.ok or observation.rainfall_mm_per_hr is None or observation.soil_saturation_pct is None:
        return None
    rainfall = float(observation.rainfall_mm_per_hr)
    soil = float(observation.soil_saturation_pct)

    previous_drain: float | None = None
    elapsed = HYDRAULICS_DRAIN_DEFAULT_DT_SECONDS
    if previous:
        previous_readings = previous.get("readings") or {}
        raw_previous = previous_readings.get("drain_level_m")
        if isinstance(raw_previous, (int, float)) and not isinstance(raw_previous, bool):
            previous_drain = float(raw_previous)
        previous_epoch = previous.get("ts_epoch")
        if isinstance(previous_epoch, (int, float)):
            elapsed = float(now_epoch) - float(previous_epoch)

    drain = hydraulic_drain_level_m(rainfall, soil, previous_drain, elapsed)
    water = hydraulic_water_level_m(drain, soil)
    flow = hydraulic_flow_rate_m3_s(water, rainfall)
    return {
        "rainfall_mm_per_hr": round(rainfall, 3),
        "drain_level_m": round(drain, 3),
        "soil_saturation_pct": round(soil, 3),
        "water_level_m": round(water, 3),
        "flow_rate_m3_s": round(flow, 3),
    }


def build_live_reading_payload(
    zone_id: str,
    metrics: Mapping[str, float],
    *,
    observed_at: str,
    now_epoch: float,
) -> dict[str, Any]:
    """Build the payload for the *internal* ingestion path (server timestamp, mixed tags)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "simulated": False,
        "zone_id": zone_id,
        "sensor_node_id": sensor_node_id_for(zone_id),
        "timestamp": observed_at,
        "readings": dict(metrics),
    }


def build_live_record(
    zone_id: str,
    metrics: Mapping[str, float],
    *,
    now_epoch: float,
    observation: Observation,
) -> dict[str, Any]:
    """Validate and normalise a live reading through the shared validation path.

    The reading is timestamped *now* (when FloodPulse sampled it) while the provider's own
    observation time is preserved on the cache item and in the API response, so freshness
    in the rest of the pipeline keeps its usual meaning and nothing is backdated.
    """
    payload = build_live_reading_payload(
        zone_id, metrics, observed_at=iso_utc(now_epoch), now_epoch=now_epoch
    )
    record = validate_reading(
        payload,
        now_epoch,
        (zone_id,),
        accept_non_simulated=True,
        metric_sources=METRIC_SOURCES_LIVE,
        source=LIVE_SOURCE,
    )
    record["provider"] = observation.provider
    record["provider_observed_at"] = observation.observed_at
    return record


# --------------------------------------------------------------------------- #
# DynamoDB cache item and the "at most every 10 minutes" rule
# --------------------------------------------------------------------------- #


def cache_sort_key() -> str:
    return WEATHER_CACHE_SORT_KEY


def build_weather_cache_item(
    observation: Observation,
    *,
    zone_id: str,
    now_epoch: float,
    record: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The item stored at ``sk = WEATHER#CACHE`` for a zone (one item, overwritten).

    A successful fetch stores both the provider's values and the attempt bookkeeping, so
    the cache is self-describing: which observation it holds, when it was fetched and when
    the provider was last asked.
    """
    metrics = dict(record["readings"]) if record else None
    item = {
        "pk": build_zone_pk(zone_id),
        "sk": WEATHER_CACHE_SORT_KEY,
        "record_type": "WEATHER_CACHE",
        "zone_id": zone_id,
        "provider": observation.provider,
        "observed_at": observation.observed_at,
        "observed_at_epoch": observation.observed_at_epoch,
        "provider_current_time": observation.provider_current_time,
        "rainfall_mm_per_hr": observation.rainfall_mm_per_hr,
        "soil_moisture_m3m3": observation.soil_moisture_m3m3,
        "soil_saturation_pct": observation.soil_saturation_pct,
        "latitude": observation.latitude,
        "longitude": observation.longitude,
        "metric_sources": dict(METRIC_SOURCES_LIVE),
        "hydraulics_note": HYDRAULICS_NOTE,
        "derived_metrics": metrics,
        "simulated": False,
        "data_classification": "mixed",
    }
    item.update(attempt_fields(now_epoch, ok=True, error=None))
    item["fetched_at_epoch"] = int(now_epoch)
    item["fetched_at"] = iso_utc(now_epoch)
    return item


def attempt_fields(now_epoch: float, *, ok: bool, error: str | None) -> dict[str, Any]:
    """Bookkeeping for one provider attempt (kept even when the attempt failed)."""
    return {
        "last_attempt_epoch": int(now_epoch),
        "last_attempt_at": iso_utc(now_epoch),
        "last_attempt_ok": bool(ok),
        "last_attempt_error": error,
    }


def build_weather_failure_item(
    *,
    zone_id: str,
    now_epoch: float,
    error: str,
    previous: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """A failed attempt: the values are kept as they are, only the bookkeeping moves.

    Writing this is what keeps a provider outage from turning into one provider call per
    dashboard poll: the attempt anchor advances, so the next try waits for the window,
    while ``cache_age_seconds`` keeps telling the truth about how old the values are.
    """
    item = dict(previous or {})
    item["pk"] = build_zone_pk(zone_id)
    item["sk"] = WEATHER_CACHE_SORT_KEY
    item["record_type"] = "WEATHER_CACHE"
    item["zone_id"] = zone_id
    item.setdefault("provider", WEATHER_PROVIDER)
    item.setdefault("simulated", False)
    item.setdefault("data_classification", "mixed")
    item.update(attempt_fields(now_epoch, ok=False, error=error))
    return item


def cache_age_seconds(cache: Mapping[str, Any] | None, now_epoch: float) -> float | None:
    """Age of the cached *values* in seconds, or ``None`` when there is no usable cache."""
    return _age_from(cache, now_epoch, "fetched_at_epoch")


def attempt_age_seconds(cache: Mapping[str, Any] | None, now_epoch: float) -> float | None:
    """Seconds since the provider was last asked for this zone."""
    return _age_from(cache, now_epoch, "last_attempt_epoch")


def _age_from(cache: Mapping[str, Any] | None, now_epoch: float, field: str) -> float | None:
    if not cache:
        return None
    recorded = cache.get(field)
    if not isinstance(recorded, (int, float)) or isinstance(recorded, bool):
        return None
    return max(0.0, float(now_epoch) - float(recorded))


def needs_refresh(cache: Mapping[str, Any] | None, now_epoch: float, refresh_seconds: float) -> bool:
    """True when the provider has not been asked for this zone within the window.

    This single predicate is what enforces "refresh external weather at most every 10
    minutes", regardless of how often ``GET /zones`` is called. It is anchored on the last
    *attempt* rather than the last success, so an outage cannot cause a retry per poll;
    items written before that field existed fall back to the fetch time.
    """
    anchor = attempt_age_seconds(cache, now_epoch)
    if anchor is None:
        anchor = cache_age_seconds(cache, now_epoch)
    if anchor is None:
        return True
    return anchor >= max(0.0, float(refresh_seconds))


@dataclass(frozen=True)
class RefreshOutcome:
    """What a refresh attempt did, reported honestly in ``GET /zones``."""

    enabled: bool
    attempted: bool
    refreshed: tuple[str, ...] = ()
    skipped_fresh: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    error: str | None = None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def as_view(self, *, provider: str = WEATHER_PROVIDER, refresh_seconds: float = WEATHER_REFRESH_SECONDS) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "provider": provider,
            "attempted": self.attempted,
            "refreshed": list(self.refreshed),
            "already_fresh": list(self.skipped_fresh),
            "failed": list(self.failed),
            "error": self.error,
            "reason": self.reason,
            "refresh_seconds": int(refresh_seconds),
        }


def _record_failed_attempts(
    store: Any,
    stale: list[dict[str, Any]],
    caches: Mapping[str, Any],
    now_epoch: float,
    error: str,
) -> None:
    """Advance the attempt anchor for zones whose fetch failed (values are left alone).

    Best effort by design: bookkeeping failing must never turn a cleanly reported provider
    problem into an exception.
    """
    for zone in stale:
        zone_id = str(zone["zone_id"])
        try:
            store.put_weather_cache(
                build_weather_failure_item(
                    zone_id=zone_id,
                    now_epoch=now_epoch,
                    error=error,
                    previous=caches.get(zone_id),
                )
            )
        except Exception:  # noqa: BLE001 - diagnostics must not break the response
            continue


def weather_zones(mode: str, env: Mapping[str, str] | None = None) -> tuple[dict[str, Any], ...]:
    """The live zones that need external weather (empty when the provider is off)."""
    environment = {} if env is None else env
    setting = (environment.get(WEATHER_MODE_ENV) or WEATHER_MODE_LIVE).strip().lower()
    if setting == WEATHER_MODE_OFF:
        return ()
    definitions = zone_definitions_for_mode(mode)
    return tuple(zone for zone in definitions if str(zone["zone_id"]).startswith("city-"))


def refresh_live_weather(
    store: Any,
    *,
    mode: str,
    env: Mapping[str, str] | None,
    now_epoch: float,
    ingest: Callable[[dict[str, Any], Observation], Any],
    client: WeatherClient | None = None,
    refresh_seconds: float = WEATHER_REFRESH_SECONDS,
) -> RefreshOutcome:
    """Refresh stale live-zone weather from the provider, then ingest it as readings.

    ``store`` needs ``get_weather_cache``/``put_weather_cache``/``get_latest``.
    ``ingest(record, observation)`` is required and must store the validated record
    through the normal pipeline (scoring, hysteresis, incidents, alerts) -- this module
    never writes a reading itself, so a live reading can never bypass scoring.

    Returns an outcome describing exactly what happened; it never raises for a provider
    problem and never invents a value.
    """
    environment = {} if env is None else env
    zones = weather_zones(mode, environment)
    if not zones:
        return RefreshOutcome(enabled=False, attempted=False, reason=DISABLED)

    stale: list[dict[str, Any]] = []
    fresh: list[str] = []
    caches: dict[str, Any] = {}
    for zone in zones:
        zone_id = str(zone["zone_id"])
        cache = store.get_weather_cache(zone_id)
        caches[zone_id] = cache
        if needs_refresh(cache, now_epoch, refresh_seconds):
            stale.append(zone)
        else:
            fresh.append(zone_id)

    if not stale:
        return RefreshOutcome(enabled=True, attempted=False, skipped_fresh=tuple(fresh), reason="cache_fresh")

    provider = client if client is not None else WeatherClient()
    result = provider.fetch(stale)
    if not result.ok:
        error = result.error or "provider_failed"
        _record_failed_attempts(store, stale, caches, now_epoch, error)
        return RefreshOutcome(
            enabled=True,
            attempted=True,
            skipped_fresh=tuple(fresh),
            failed=tuple(str(zone["zone_id"]) for zone in stale),
            error=error,
        )

    refreshed: list[str] = []
    failed: list[str] = []
    errors: list[str] = []
    for zone in stale:
        zone_id = str(zone["zone_id"])
        observation = result.observations.get(zone_id)
        if observation is None or not observation.ok:
            failed.append(zone_id)
            errors.append(f"{zone_id}: {(observation.error if observation else None) or 'no_observation'}")
            continue
        previous = store.get_latest(zone_id)
        metrics = build_live_metrics(observation, previous=previous, now_epoch=now_epoch)
        if metrics is None:
            failed.append(zone_id)
            errors.append(f"{zone_id}: incomplete_observation")
            continue
        try:
            record = build_live_record(zone_id, metrics, now_epoch=now_epoch, observation=observation)
            store.put_weather_cache(
                build_weather_cache_item(observation, zone_id=zone_id, now_epoch=now_epoch, record=record)
            )
            ingest(record, observation)
        except Exception as exc:  # noqa: BLE001 - one bad zone must not stop the others
            failed.append(zone_id)
            errors.append(f"{zone_id}: ingest_failed: {exc}")
            try:
                store.put_weather_cache(
                    build_weather_failure_item(
                        zone_id=zone_id,
                        now_epoch=now_epoch,
                        error=f"ingest_failed: {exc}",
                        previous=caches.get(zone_id),
                    )
                )
            except Exception:  # noqa: BLE001 - diagnostics must not break the response
                pass
            continue
        refreshed.append(zone_id)

    error = None
    if failed:
        detail = "; ".join(errors[:3]) or "unknown"
        error = detail if refreshed else f"no_zone_refreshed: {detail}"
    return RefreshOutcome(
        enabled=True,
        attempted=True,
        refreshed=tuple(refreshed),
        skipped_fresh=tuple(fresh),
        failed=tuple(failed),
        error=error,
    )


def weather_view(
    *,
    mode: str,
    outcome: RefreshOutcome | None,
    caches: Mapping[str, Mapping[str, Any] | None],
    env: Mapping[str, str] | None = None,
    now_epoch: float,
    refresh_seconds: float = WEATHER_REFRESH_SECONDS,
) -> dict[str, Any]:
    """The top-level ``weather`` block of ``GET /zones`` (never contains credentials)."""
    zones = weather_zones(mode, env)
    ages: dict[str, int | None] = {}
    attempts: dict[str, int | None] = {}
    failed: dict[str, str | None] = {}
    for zone in zones:
        zone_id = str(zone["zone_id"])
        cache = caches.get(zone_id)
        age = cache_age_seconds(cache, now_epoch)
        attempt = attempt_age_seconds(cache, now_epoch)
        ages[zone_id] = None if age is None else int(age)
        attempts[zone_id] = None if attempt is None else int(attempt)
        failed[zone_id] = (cache or {}).get("last_attempt_error")
    return {
        "provider": WEATHER_PROVIDER,
        "attribution": WEATHER_ATTRIBUTION,
        "docs": WEATHER_DOCS_URL,
        "enabled": bool(zones),
        "refresh_seconds": int(refresh_seconds),
        "live_zones": [str(zone["zone_id"]) for zone in zones],
        "cache_age_seconds": ages,
        "last_attempt_age_seconds": attempts,
        "last_attempt_error": failed,
        "last_refresh": outcome.as_view(refresh_seconds=refresh_seconds) if outcome else None,
        "hydraulics_note": HYDRAULICS_NOTE,
        "metric_sources": dict(METRIC_SOURCES_LIVE),
    }


__all__ = [
    "DISABLED",
    "LIVE_SOURCE",
    "NO_CACHE",
    "RefreshOutcome",
    "attempt_age_seconds",
    "build_live_metrics",
    "build_live_reading_payload",
    "build_live_record",
    "build_weather_cache_item",
    "build_weather_failure_item",
    "cache_age_seconds",
    "clamp",
    "hydraulic_drain_level_m",
    "hydraulic_flow_rate_m3_s",
    "hydraulic_target_drain_m",
    "hydraulic_water_level_m",
    "iso_utc",
    "needs_refresh",
    "refresh_live_weather",
    "sensor_node_id_for",
    "weather_view",
    "weather_zones",
]

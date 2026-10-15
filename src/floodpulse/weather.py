"""Open-Meteo weather client (milestone M4).

This module is the *only* place that talks to an external service. It is deliberately
small, stdlib-only and dependency-injected so that:

* no test ever touches the network (the transport is a parameter), and
* a provider failure can never break the API: :meth:`WeatherClient.fetch` returns a
  result object with ``ok=False`` and a reason instead of raising.

What is real here and what is not
---------------------------------
``rainfall_mm_per_hr`` is the provider's precipitation for the hour containing the
current observation, passed through as delivered (tagged REAL). Open-Meteo defines hourly
precipitation as the accumulation over the *preceding* hour, so the selected sample is
always a completed past hour -- but it is also up to 60 minutes old, and it is a
grid-point provider value (model/observation blend), not a certified rain gauge.
``soil_moisture`` is converted into a 0-100 % saturation figure by
:mod:`floodpulse.live_weather` (tagged MODEL_DERIVED). Nothing in this module invents a
value: if the provider does not send a field, the observation for that zone is marked
failed and the caller falls back to its cache.

Open-Meteo is used under its free, key-less tier; attribution is required and is
included in :data:`floodpulse.config.WEATHER_ATTRIBUTION`.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

from .config import (
    METRICS,
    SOURCE_MODEL_DERIVED,
    SOURCE_REAL,
    WEATHER_API_URL,
    WEATHER_LOCATION_TOLERANCE_DEGREES,
    WEATHER_PROVIDER,
    WEATHER_PROVIDER_FIELDS,
    WEATHER_PROVIDER_HOURLY_FIELDS,
    WEATHER_SOIL_SATURATION_M3M3,
    WEATHER_TIMEOUT_SECONDS,
)

USER_AGENT = "FloodPulse/1.0 (educational flood-warning project)"
MAX_RESPONSE_BYTES = 512 * 1024

#: ``transport(url, timeout) -> (status, body)``; overridden in tests.
Transport = Callable[[str, float], "tuple[int, bytes]"]


class WeatherError(RuntimeError):
    """Raised for a structurally unusable provider response."""


@dataclass(frozen=True)
class Observation:
    """One zone's weather as reported by the provider (plus FloodPulse's conversion)."""

    zone_id: str
    ok: bool
    provider: str = WEATHER_PROVIDER
    rainfall_mm_per_hr: float | None = None
    soil_moisture_m3m3: float | None = None
    soil_saturation_pct: float | None = None
    provider_current_time: str | None = None
    observed_at: str | None = None
    observed_at_epoch: float | None = None
    latitude: float | None = None
    longitude: float | None = None
    error: str | None = None

    def as_cache_fields(self) -> dict[str, Any]:
        """The subset stored in (and served from) the DynamoDB weather cache."""
        return {
            "provider": self.provider,
            "rainfall_mm_per_hr": self.rainfall_mm_per_hr,
            "soil_moisture_m3m3": self.soil_moisture_m3m3,
            "soil_saturation_pct": self.soil_saturation_pct,
            "provider_current_time": self.provider_current_time,
            "observed_at": self.observed_at,
            "observed_at_epoch": self.observed_at_epoch,
            "latitude": self.latitude,
            "longitude": self.longitude,
        }


@dataclass(frozen=True)
class FetchResult:
    """Outcome of one provider call for a set of zones."""

    ok: bool
    observations: Mapping[str, Observation] = field(default_factory=dict)
    fetched_at_epoch: float | None = None
    error: str | None = None
    provider: str = WEATHER_PROVIDER
    requested: int = 0


# --------------------------------------------------------------------------- #
# URL / parsing helpers (pure, unit-tested without any network)
# --------------------------------------------------------------------------- #


def build_url(zones: Iterable[Mapping[str, Any]], provider_url: str = WEATHER_API_URL) -> str:
    """Build the single request URL covering every zone (comma-separated coordinates)."""
    zone_list = list(zones)
    if not zone_list:
        raise WeatherError("no zones requested")
    latitudes = ",".join(f"{float(zone['latitude']):.4f}" for zone in zone_list)
    longitudes = ",".join(f"{float(zone['longitude']):.4f}" for zone in zone_list)
    query = urllib.parse.urlencode(
        {
            "latitude": latitudes,
            "longitude": longitudes,
            "current": ",".join(WEATHER_PROVIDER_FIELDS),
            "hourly": ",".join(WEATHER_PROVIDER_HOURLY_FIELDS),
            "past_days": 1,
            "forecast_days": 1,
            "timezone": "UTC",
            "cell_selection": "nearest",
        }
    )
    return f"{provider_url}?{query}"


def parse_provider_time(text: str) -> tuple[str, float]:
    """Parse ``2026-10-15T18:15`` (UTC, no offset) into (iso8601-Z, epoch seconds)."""
    if not isinstance(text, str) or not text:
        raise WeatherError("missing provider time")
    cleaned = text.strip().replace(" ", "T")
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1]
    try:
        moment = datetime.fromisoformat(cleaned)
    except ValueError as exc:  # pragma: no cover - defensive
        raise WeatherError(f"unparsable provider time {text!r}") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ"), moment.timestamp()


def _hourly_series(block: Mapping[str, Any], key: str) -> dict[str, float]:
    times = block.get("time")
    values = block.get(key)
    if not isinstance(times, list) or not isinstance(values, list):
        raise WeatherError(f"hourly block is missing {key!r}")
    series: dict[str, float] = {}
    for stamp, value in zip(times, values):
        if not isinstance(stamp, str) or not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        series[stamp] = float(value)
    if not series:
        raise WeatherError(f"hourly {key!r} contains no usable numbers")
    return series


def latest_at_or_before(series: Mapping[str, float], moment: str) -> tuple[str, float] | None:
    """Return the newest sample at or before ``moment`` (ISO minute strings compare safely)."""
    candidates = [stamp for stamp in series if stamp <= moment]
    if not candidates:
        return None
    stamp = max(candidates)
    return stamp, series[stamp]


def soil_saturation_pct(soil_moisture_m3m3: float) -> float:
    """Convert volumetric soil moisture to a 0-100 % saturation figure (clamped).

    Rounded to the 3 decimals the rest of the pipeline uses, so the same number is stored
    in the cache item, scored and shown in the API instead of a float artefact such as
    ``60.199999999999996``.
    """
    ratio = soil_moisture_m3m3 / WEATHER_SOIL_SATURATION_M3M3 * 100.0
    return round(max(0.0, min(100.0, ratio)), 3)


def parse_response(payload: Any, zones: Sequence[Mapping[str, Any]]) -> dict[str, Observation]:
    """Map a provider response onto the requested zones, positionally and defensively.

    Any zone whose entry is missing, whose coordinates do not match the request, or whose
    fields are unusable gets ``ok=False`` with a reason. No value is ever invented.
    """
    entries = payload if isinstance(payload, list) else [payload]
    if not isinstance(payload, (list, dict)):
        raise WeatherError("provider response is not an object or list")

    observations: dict[str, Observation] = {}
    for index, zone in enumerate(zones):
        zone_id = str(zone["zone_id"])
        if index >= len(entries) or not isinstance(entries[index], dict):
            observations[zone_id] = Observation(zone_id=zone_id, ok=False, error="no_provider_entry")
            continue
        entry = entries[index]
        try:
            observations[zone_id] = _parse_entry(zone_id, zone, entry)
        except WeatherError as exc:
            observations[zone_id] = Observation(zone_id=zone_id, ok=False, error=str(exc))
    return observations


def _parse_entry(zone_id: str, zone: Mapping[str, Any], entry: Mapping[str, Any]) -> Observation:
    latitude = entry.get("latitude")
    longitude = entry.get("longitude")
    if isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
        if (
            abs(float(latitude) - float(zone["latitude"])) > WEATHER_LOCATION_TOLERANCE_DEGREES
            or abs(float(longitude) - float(zone["longitude"])) > WEATHER_LOCATION_TOLERANCE_DEGREES
        ):
            raise WeatherError("provider location does not match the requested zone")

    current = entry.get("current")
    hourly = entry.get("hourly")
    if not isinstance(current, Mapping) or not isinstance(hourly, Mapping):
        raise WeatherError("provider response is missing 'current' or 'hourly'")

    current_time = current.get("time")
    if not isinstance(current_time, str):
        raise WeatherError("provider 'current.time' is missing")
    current_iso, _current_epoch = parse_provider_time(current_time)

    rainfall_series = _hourly_series(hourly, "precipitation")
    sample = latest_at_or_before(rainfall_series, current_time)
    if sample is None:
        raise WeatherError("no precipitation sample at or before 'current.time'")
    sample_time, rainfall_mm_per_hr = sample

    limits = METRICS["rainfall_mm_per_hr"]
    if not (limits["min"] <= rainfall_mm_per_hr <= limits["max"]):
        raise WeatherError(f"provider rainfall {rainfall_mm_per_hr} is outside the valid range")

    moisture_series = _hourly_series(hourly, "soil_moisture_0_to_7cm")
    moisture_sample = latest_at_or_before(moisture_series, current_time)
    if moisture_sample is None:
        raise WeatherError("no soil moisture sample at or before 'current.time'")
    _moisture_time, soil_moisture = moisture_sample
    if not (0.0 <= soil_moisture <= 1.0):
        raise WeatherError(f"provider soil moisture {soil_moisture} is outside 0-1 m3/m3")

    observed_iso, observed_epoch = parse_provider_time(sample_time)
    return Observation(
        zone_id=zone_id,
        ok=True,
        rainfall_mm_per_hr=rainfall_mm_per_hr,
        soil_moisture_m3m3=soil_moisture,
        soil_saturation_pct=soil_saturation_pct(soil_moisture),
        provider_current_time=current_iso,
        observed_at=observed_iso,
        observed_at_epoch=observed_epoch,
        latitude=float(latitude) if isinstance(latitude, (int, float)) else None,
        longitude=float(longitude) if isinstance(longitude, (int, float)) else None,
    )


def metric_sources() -> dict[str, str]:
    """Provenance of the two metrics this module supplies."""
    return {
        "rainfall_mm_per_hr": SOURCE_REAL,
        "soil_saturation_pct": SOURCE_MODEL_DERIVED,
    }


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


def default_transport(url: str, timeout: float) -> tuple[int, bytes]:
    """stdlib HTTPS GET (the Lambda runs outside a VPC, so it has internet egress)."""
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed host
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise WeatherError("provider response is unexpectedly large")
        return int(getattr(response, "status", 200)), body


class WeatherClient:
    """Fetch current weather for several zones in a single provider request."""

    def __init__(
        self,
        *,
        provider_url: str = WEATHER_API_URL,
        timeout: float = WEATHER_TIMEOUT_SECONDS,
        transport: Transport | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.provider_url = provider_url
        self.timeout = float(timeout)
        self._transport = transport or default_transport
        self._clock = clock or (lambda: datetime.now(timezone.utc).timestamp())

    @property
    def enabled(self) -> bool:
        return True

    def fetch(self, zones: Sequence[Mapping[str, Any]]) -> FetchResult:
        """Fetch every zone's weather; never raises, never returns invented values."""
        zone_list = list(zones)
        if not zone_list:
            return FetchResult(ok=False, error="no_zones", requested=0)

        try:
            url = build_url(zone_list, self.provider_url)
        except (WeatherError, KeyError, TypeError, ValueError) as exc:
            return FetchResult(ok=False, error=f"bad_zone_definition: {exc}", requested=len(zone_list))

        try:
            status, body = self._transport(url, self.timeout)
        except urllib.error.HTTPError as exc:
            return FetchResult(ok=False, error=f"http_{exc.code}", requested=len(zone_list))
        except urllib.error.URLError as exc:
            return FetchResult(ok=False, error=f"network_error: {exc.reason}", requested=len(zone_list))
        except (TimeoutError, OSError) as exc:
            return FetchResult(ok=False, error=f"network_error: {exc}", requested=len(zone_list))
        except Exception as exc:  # pragma: no cover - defensive: a bad transport must not break the API
            return FetchResult(ok=False, error=f"transport_error: {exc}", requested=len(zone_list))

        fetched_at = float(self._clock())
        if status != 200:
            return FetchResult(
                ok=False, error=f"http_{status}", fetched_at_epoch=fetched_at, requested=len(zone_list)
            )

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return FetchResult(
                ok=False, error=f"bad_json: {exc}", fetched_at_epoch=fetched_at, requested=len(zone_list)
            )

        try:
            observations = parse_response(payload, zone_list)
        except WeatherError as exc:
            return FetchResult(
                ok=False, error=f"bad_payload: {exc}", fetched_at_epoch=fetched_at, requested=len(zone_list)
            )

        delivered = {zone_id: item for zone_id, item in observations.items() if item.ok}
        if not delivered:
            first_error = next((item.error for item in observations.values() if item.error), "no_data")
            return FetchResult(
                ok=False, error=f"no_usable_observations: {first_error}",
                observations=observations, fetched_at_epoch=fetched_at, requested=len(zone_list),
            )

        return FetchResult(
            ok=True, observations=observations, fetched_at_epoch=fetched_at, requested=len(zone_list)
        )


__all__ = [
    "FetchResult",
    "Observation",
    "WeatherClient",
    "WeatherError",
    "Transport",
    "build_url",
    "default_transport",
    "latest_at_or_before",
    "metric_sources",
    "parse_provider_time",
    "parse_response",
    "soil_saturation_pct",
]

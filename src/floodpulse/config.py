"""Static configuration for the FloodPulse MVP (milestone M0, local only).

Every numeric constant in this module is *heuristic tuning configuration* for the
``heuristic-v1`` risk model. None of it is calibrated against measured hydrological
data, and the resulting score must never be presented as a validated flood model.

No AWS calls are made anywhere in this package at M0.
"""

from __future__ import annotations

import os
from copy import deepcopy
from typing import Any, Mapping

SCHEMA_VERSION = "1.0"
RISK_MODEL_ID = "heuristic-v1"

# --------------------------------------------------------------------------- #
# Sensor metrics: valid input range (payload validation) + display unit
# --------------------------------------------------------------------------- #

METRIC_ORDER: tuple[str, ...] = (
    "rainfall_mm_per_hr",
    "drain_level_m",
    "soil_saturation_pct",
    "water_level_m",
    "flow_rate_m3_s",
)

METRICS: dict[str, dict[str, Any]] = {
    "rainfall_mm_per_hr": {"unit": "mm/h", "min": 0.0, "max": 250.0},
    "drain_level_m": {"unit": "m", "min": 0.0, "max": 5.0},
    "soil_saturation_pct": {"unit": "%", "min": 0.0, "max": 100.0},
    "water_level_m": {"unit": "m", "min": 0.0, "max": 6.0},
    "flow_rate_m3_s": {"unit": "m3/s", "min": 0.0, "max": 50.0},
}

#: Human-readable names for the same metrics (alerts, incident records, dashboard).
METRIC_LABELS: dict[str, str] = {
    "rainfall_mm_per_hr": "Rainfall intensity",
    "drain_level_m": "Drain level",
    "soil_saturation_pct": "Soil saturation",
    "water_level_m": "Water level",
    "flow_rate_m3_s": "Flow rate",
}

#: Weights sum to 1.00. Hand-set from the project plan; not fitted to any dataset.
METRIC_WEIGHTS: dict[str, float] = {
    "rainfall_mm_per_hr": 0.30,
    "drain_level_m": 0.15,
    "soil_saturation_pct": 0.20,
    "water_level_m": 0.25,
    "flow_rate_m3_s": 0.10,
}

#: Default breakpoints: sub-score is 0.5 at ``watch`` and 1.0 at ``critical``.
DEFAULT_THRESHOLDS: dict[str, dict[str, float]] = {
    "rainfall_mm_per_hr": {"watch": 15.0, "critical": 45.0},
    "drain_level_m": {"watch": 1.5, "critical": 2.5},
    "soil_saturation_pct": {"watch": 70.0, "critical": 90.0},
    "water_level_m": {"watch": 2.0, "critical": 3.5},
    "flow_rate_m3_s": {"watch": 6.0, "critical": 12.0},
}

# --------------------------------------------------------------------------- #
# Fixed zone definitions (3 simulated zones = the local/demo fleet)
# --------------------------------------------------------------------------- #
# ``thresholds`` holds per-zone overrides only; empty means "use the defaults".
# Overrides model different local drainage capacity, not measured calibration.

DEMO_ZONE_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "zone_id": "zone-riverside-north",
        "name": "Riverside North",
        "latitude": 59.3422,
        "longitude": 18.0561,
        "description": "Upstream riverside district; baseline drainage capacity.",
        "thresholds": {},
    },
    {
        "zone_id": "zone-old-town-canal",
        "name": "Old Town Canal",
        "latitude": 59.3245,
        "longitude": 18.0705,
        "description": "Narrow canal through dense old town; low drainage capacity.",
        "thresholds": {
            "water_level_m": {"watch": 1.8, "critical": 3.2},
            "drain_level_m": {"watch": 1.3, "critical": 2.2},
        },
    },
    {
        "zone_id": "zone-harbor-east",
        "name": "Harbor East",
        "latitude": 59.3190,
        "longitude": 18.1010,
        "description": "Coastal harbor basin; exposed to intense rainfall events.",
        "thresholds": {
            "rainfall_mm_per_hr": {"watch": 18.0, "critical": 50.0},
            "soil_saturation_pct": {"watch": 72.0, "critical": 92.0},
        },
    },
)

# --------------------------------------------------------------------------- #
# Live-weather zones (M4; expanded to fifteen cities in M11): the authoritative
# city table for the whole project
# --------------------------------------------------------------------------- #
# Rainfall intensity and soil saturation for these zones come from Open-Meteo (see
# ``weather.py``). Drain level, water level and flow rate are **simulated** by a
# deterministic hydraulic response to that real rainfall (see ``live_weather.py``),
# because FloodPulse has no real drain/flow sensors anywhere.
#
# The breakpoints are the same hand-set heuristic defaults as the demo zones: they
# are NOT calibrated for these cities, they are not official warning thresholds, and
# nothing here should be read as an authoritative flood warning for them.
#
# This tuple is the one place a monitored city is defined. ``name`` is the city,
# ``state`` is the state or union territory it belongs to, ``region`` is a display and
# navigation grouping only, and ``latitude``/``longitude`` are the city-centre
# coordinates every layer uses - the weather request (``weather.py`` ``build_url``),
# the provider cross-check (``WEATHER_LOCATION_TOLERANCE_DEGREES``) and the API. The
# frontend holds no coordinates of its own.
#
# What the fleet does and does not claim (M11): FloodPulse monitors *these cities*.
# It does not monitor a state, a region or the country, it produces no regional risk
# score, and a city being NORMAL says nothing about the rest of its state. The
# ``region`` field exists so a visitor can find a city on a page; it is not a unit of
# measurement anywhere in the pipeline.

LIVE_ZONE_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "zone_id": "city-chennai",
        "name": "Chennai",
        "state": "Tamil Nadu",
        "region": "South",
        "country": "IN",
        "latitude": 13.0827,
        "longitude": 80.2707,
        "description": "Coastal Bay of Bengal metropolis; monsoon and cyclone rainfall.",
        "thresholds": {},
    },
    {
        "zone_id": "city-mumbai",
        "name": "Mumbai",
        "state": "Maharashtra",
        "region": "West",
        "country": "IN",
        "latitude": 19.0760,
        "longitude": 72.8777,
        "description": "Dense Arabian Sea coast; extreme short-duration monsoon rainfall.",
        "thresholds": {},
    },
    {
        "zone_id": "city-bengaluru",
        "name": "Bengaluru",
        "state": "Karnataka",
        "region": "South",
        "country": "IN",
        "latitude": 12.9716,
        "longitude": 77.5946,
        "description": "Inland plateau city; flash flooding from convective storms.",
        "thresholds": {},
    },
    {
        "zone_id": "city-hyderabad",
        "name": "Hyderabad",
        "state": "Telangana",
        "region": "South",
        "country": "IN",
        "latitude": 17.3850,
        "longitude": 78.4867,
        "description": "Deccan plateau city with lake and drainage-network flooding.",
        "thresholds": {},
    },
    {
        "zone_id": "city-kochi",
        "name": "Kochi",
        "state": "Kerala",
        "region": "South",
        "country": "IN",
        "latitude": 9.9312,
        "longitude": 76.2673,
        "description": "Backwater coastal city; very heavy south-west monsoon rainfall.",
        "thresholds": {},
    },
    {
        "zone_id": "city-delhi",
        "name": "Delhi",
        "state": "Delhi",
        "region": "North",
        "country": "IN",
        "latitude": 28.6139,
        "longitude": 77.2090,
        "description": "Yamuna floodplain metropolis; drainage congestion after cloudbursts.",
        "thresholds": {},
    },
    {
        "zone_id": "city-kolkata",
        "name": "Kolkata",
        "state": "West Bengal",
        "region": "East",
        "country": "IN",
        "latitude": 22.5726,
        "longitude": 88.3639,
        "description": "Deltaic Hooghly port city; tidal and monsoon rainfall flooding.",
        "thresholds": {},
    },
    {
        "zone_id": "city-guwahati",
        "name": "Guwahati",
        "state": "Assam",
        "region": "Northeast",
        "country": "IN",
        "latitude": 26.1445,
        "longitude": 91.7362,
        "description": "Brahmaputra valley city; very heavy monsoon rainfall on steep terrain.",
        "thresholds": {},
    },
    {
        "zone_id": "city-patna",
        "name": "Patna",
        "state": "Bihar",
        "region": "East",
        "country": "IN",
        "latitude": 25.5941,
        "longitude": 85.1376,
        "description": "Ganges riverside city; river backflow and urban drainage flooding.",
        "thresholds": {},
    },
    {
        "zone_id": "city-bhubaneswar",
        "name": "Bhubaneswar",
        "state": "Odisha",
        "region": "East",
        "country": "IN",
        "latitude": 20.2961,
        "longitude": 85.8245,
        "description": "Coastal Odisha capital; cyclone-season rainfall and drainage overload.",
        "thresholds": {},
    },
    {
        "zone_id": "city-visakhapatnam",
        "name": "Visakhapatnam",
        "state": "Andhra Pradesh",
        "region": "South",
        "country": "IN",
        "latitude": 17.6868,
        "longitude": 83.2185,
        "description": "Coastal Andhra port city between hills and the Bay of Bengal.",
        "thresholds": {},
    },
    {
        "zone_id": "city-pune",
        "name": "Pune",
        "state": "Maharashtra",
        "region": "West",
        "country": "IN",
        "latitude": 18.5204,
        "longitude": 73.8567,
        "description": "Deccan plateau city at the confluence of two rivers; flash flooding.",
        "thresholds": {},
    },
    {
        "zone_id": "city-ahmedabad",
        "name": "Ahmedabad",
        "state": "Gujarat",
        "region": "West",
        "country": "IN",
        "latitude": 23.0225,
        "longitude": 72.5714,
        "description": "Sabarmati riverside city; monsoon and upstream release flooding.",
        "thresholds": {},
    },
    {
        "zone_id": "city-jaipur",
        "name": "Jaipur",
        "state": "Rajasthan",
        "region": "North",
        "country": "IN",
        "latitude": 26.9124,
        "longitude": 75.7873,
        "description": "Semi-arid city where short intense storms cause flash flooding.",
        "thresholds": {},
    },
    {
        "zone_id": "city-lucknow",
        "name": "Lucknow",
        "state": "Uttar Pradesh",
        "region": "North",
        "country": "IN",
        "latitude": 26.8467,
        "longitude": 80.9462,
        "description": "Gomti riverside city; monsoon rainfall and drainage congestion.",
        "thresholds": {},
    },
)

#: Display order of the region grouping. Navigation only: no region has a risk score.
REGIONS: tuple[str, ...] = ("North", "South", "East", "West", "Northeast")

#: What the coverage numbers mean, published with them so a reader cannot mistake a
#: city fleet for national coverage.
COVERAGE_SCOPE_NOTE = (
    "FloodPulse monitors individual cities, not states, regions or the country. A city in "
    "this list says nothing about the rest of its state or region; no regional or national "
    "risk score is produced. Delhi is a union territory rather than a state, so 'state' "
    "below means the state or union territory a monitored city belongs to."
)

#: Kept as the library default so every pre-M4 caller keeps working unchanged.
ZONE_DEFINITIONS: tuple[dict[str, Any], ...] = DEMO_ZONE_DEFINITIONS
ZONE_IDS: tuple[str, ...] = tuple(zone["zone_id"] for zone in DEMO_ZONE_DEFINITIONS)
LIVE_ZONE_IDS: tuple[str, ...] = tuple(zone["zone_id"] for zone in LIVE_ZONE_DEFINITIONS)

#: Zone "mode": which fleet is active and where its numbers come from.
#: The canonical values below are what the API reports; the env var accepts the short
#: forms ``live``/``demo`` as aliases so operators do not have to remember the exact string.
ZONE_MODE_LIVE = "live_weather"
ZONE_MODE_DEMO = "simulated"
ZONE_MODE_ENV = "FLOODPULSE_ZONE_MODE"
ZONE_MODE_INPUT_LIVE = "live"
ZONE_MODE_INPUT_DEMO = "demo"
ZONE_MODE_INPUTS: tuple[str, ...] = (ZONE_MODE_INPUT_LIVE, ZONE_MODE_INPUT_DEMO)
ZONE_MODE_ALIASES: dict[str, str] = {
    ZONE_MODE_INPUT_LIVE: ZONE_MODE_LIVE,
    ZONE_MODE_LIVE: ZONE_MODE_LIVE,
    ZONE_MODE_INPUT_DEMO: ZONE_MODE_DEMO,
    ZONE_MODE_DEMO: ZONE_MODE_DEMO,
}

#: Default when the environment says nothing: the simulator, i.e. pre-M4 behaviour.
DEFAULT_ZONE_MODE = ZONE_MODE_DEMO

# --------------------------------------------------------------------------- #
# Metric provenance tags (M4)
# --------------------------------------------------------------------------- #
# Every metric of every reading is tagged with where its number came from:
#   REAL          -- passed through from an external provider as delivered
#   MODEL_DERIVED -- computed by FloodPulse from external provider fields
#   SIMULATED     -- generated by the FloodPulse simulator / hydraulic response

SOURCE_REAL = "REAL"
SOURCE_MODEL_DERIVED = "MODEL_DERIVED"
SOURCE_SIMULATED = "SIMULATED"
SOURCES: tuple[str, ...] = (SOURCE_REAL, SOURCE_MODEL_DERIVED, SOURCE_SIMULATED)

SOURCE_DESCRIPTIONS: dict[str, str] = {
    SOURCE_REAL: "Measured/provider value passed through unchanged",
    SOURCE_MODEL_DERIVED: "Derived by FloodPulse from real provider fields",
    SOURCE_SIMULATED: "Generated by the FloodPulse simulator (no real sensor exists)",
}

#: Provenance per metric for the two kinds of zone.
METRIC_SOURCES_SIMULATED: dict[str, str] = {metric: SOURCE_SIMULATED for metric in METRIC_ORDER}
METRIC_SOURCES_LIVE: dict[str, str] = {
    "rainfall_mm_per_hr": SOURCE_REAL,
    "drain_level_m": SOURCE_SIMULATED,
    "soil_saturation_pct": SOURCE_MODEL_DERIVED,
    "water_level_m": SOURCE_SIMULATED,
    "flow_rate_m3_s": SOURCE_SIMULATED,
}

DATA_CLASSIFICATION_SIMULATED = "simulated"
DATA_CLASSIFICATION_MIXED = "mixed"

# --------------------------------------------------------------------------- #
# External weather provider (M4, Open-Meteo -- free, no API key)
# --------------------------------------------------------------------------- #

WEATHER_PROVIDER = "open-meteo"
WEATHER_API_URL = "https://api.open-meteo.com/v1/forecast"
WEATHER_ATTRIBUTION = "Weather data by Open-Meteo.com (CC BY 4.0)"
WEATHER_DOCS_URL = "https://open-meteo.com/en/docs"

#: ``open-meteo`` (live) or ``off`` (never call the network: pure demo mode).
WEATHER_MODE_ENV = "FLOODPULSE_WEATHER"
WEATHER_MODE_LIVE = "open-meteo"
WEATHER_MODE_OFF = "off"
WEATHER_MODES: tuple[str, ...] = (WEATHER_MODE_LIVE, WEATHER_MODE_OFF)

#: "refresh external weather at most every 10 minutes" -- enforced by this cache age.
WEATHER_REFRESH_SECONDS = 600
WEATHER_REFRESH_ENV = "FLOODPULSE_WEATHER_REFRESH_SECONDS"
WEATHER_REFRESH_MIN_SECONDS = 60
WEATHER_REFRESH_MAX_SECONDS = 24 * 60 * 60

#: The Lambda timeout is 10 s, so the provider call must stay well inside that.
WEATHER_TIMEOUT_SECONDS = 4.0
WEATHER_TIMEOUT_ENV = "FLOODPULSE_WEATHER_TIMEOUT_SECONDS"
WEATHER_TIMEOUT_MIN_SECONDS = 1.0
WEATHER_TIMEOUT_MAX_SECONDS = 8.0

WEATHER_CACHE_SORT_KEY = "WEATHER#CACHE"
WEATHER_PROVIDER_FIELDS: tuple[str, ...] = ("precipitation", "rain")
WEATHER_PROVIDER_HOURLY_FIELDS: tuple[str, ...] = ("precipitation", "soil_moisture_0_to_7cm")

#: Volumetric soil moisture that FloodPulse treats as 100 % saturation (m3/m3).
#: A modelling choice, not a measurement: it is documented next to the MODEL_DERIVED tag.
WEATHER_SOIL_SATURATION_M3M3 = 0.5

#: A provider coordinate must be at least this close to the requested one, otherwise the
#: response is rejected for that zone (guards against silently mismatched positions).
WEATHER_LOCATION_TOLERANCE_DEGREES = 0.5

# --------------------------------------------------------------------------- #
# Deterministic hydraulic response for live zones (all SIMULATED)
# --------------------------------------------------------------------------- #
# Real rainfall is the only driver; the three hydraulic metrics are generated by a
# first-order response so they stay physically plausible and perfectly reproducible:
#   target = f(rainfall, soil moisture);  level += (target - level) * (1 - exp(-dt/TAU))

HYDRAULICS_RAIN_REFERENCE_MM_PER_HR = 45.0  # rainfall that alone fills the drain to critical
HYDRAULICS_DRAIN_BASE_M = 0.15
HYDRAULICS_DRAIN_RAIN_M = 2.35
HYDRAULICS_DRAIN_SOIL_M = 0.80
HYDRAULICS_DRAIN_TAU_SECONDS = 900.0
HYDRAULICS_DRAIN_DEFAULT_DT_SECONDS = 600.0
HYDRAULICS_WATER_PER_DRAIN = 1.15
HYDRAULICS_WATER_SOIL_PER_FRACTION = 0.25
HYDRAULICS_FLOW_PER_WATER = 2.0
HYDRAULICS_FLOW_PER_RAIN = 0.05

#: Documented in the API so the dashboard can explain the simulated half of a live zone.
HYDRAULICS_NOTE = (
    "Drain level, water level and flow rate are SIMULATED: they follow a deterministic "
    "first-order response to the real rainfall and soil moisture, because FloodPulse has "
    "no real drain, water-level or flow sensors."
)

#: The same explanation for a fleet with no provider reading at all: everything is generated,
#: rainfall included, so the live-weather note above would overstate where the numbers came from.
SIMULATED_HYDRAULICS_NOTE = (
    "Every channel is SIMULATED for this fleet: rainfall, drain level, water level and flow "
    "rate all come from the deterministic sensor simulator, not from a weather provider. "
    "FloodPulse has no real drain, water-level or flow sensors."
)

#: When a stored reading stops being recent enough to describe "now". The live fleet is
#: refreshed every ten minutes, so this is three missed refreshes: a zone past it is still
#: listed (its last values are real, historical values) but is never presented as current.
STALE_READING_SECONDS = 1800

# --------------------------------------------------------------------------- #
# Risk bands and hysteresis
# --------------------------------------------------------------------------- #

#: (name, lower inclusive, upper exclusive) -- the last band is closed at 1.0.
RISK_BANDS: tuple[tuple[str, float, float], ...] = (
    ("NORMAL", 0.0, 0.35),
    ("WATCH", 0.35, 0.60),
    ("WARNING", 0.60, 0.80),
    ("CRITICAL", 0.80, 1.0001),
)

RISK_ORDER: dict[str, int] = {
    name: index for index, (name, _lower, _upper) in enumerate(RISK_BANDS)
}

#: Consecutive readings required before an effective class change is accepted
#: (demo anti-flicker). Escalation is faster than de-escalation on purpose.
ESCALATION_STREAK = 2
DEESCALATION_STREAK = 3

# --------------------------------------------------------------------------- #
# Worsening-rate ("trend") term
# --------------------------------------------------------------------------- #

TREND_WEIGHT = 0.10
TREND_REFERENCE_RISE_M = 1.0
TREND_LOOKBACK_SECONDS = 300
TREND_LOOKBACK_TOLERANCE_SECONDS = 120

#: Number of readings averaged before scoring (noise reduction).
SMOOTHING_WINDOW = 3

# --------------------------------------------------------------------------- #
# Validation / storage limits
# --------------------------------------------------------------------------- #

MAX_CLOCK_SKEW_SECONDS = 300
BODY_MAX_BYTES = 4096
HISTORY_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_HISTORY_POINTS = 600
HISTORY_DEFAULT_MINUTES = 60
HISTORY_MAX_MINUTES = 1440

# --------------------------------------------------------------------------- #
# Incidents and alerts (M3)
# --------------------------------------------------------------------------- #
# An incident is a bookkeeping record derived from the *effective* (hysteresis-
# filtered) risk class. It is not a hydrological forecast and carries no more
# authority than the heuristic score that opened it.

#: A zone opens an incident on reaching one of these classes.
INCIDENT_OPEN_SEVERITIES: tuple[str, ...] = ("WARNING", "CRITICAL")

#: The only class that closes an open incident. Because this is the *effective*
#: class, the existing DEESCALATION_STREAK requirement must already be satisfied.
INCIDENT_CLOSING_CLASS = "NORMAL"

INCIDENT_STATUS_OPEN = "OPEN"
INCIDENT_STATUS_CLOSED = "CLOSED"
INCIDENT_STATUSES: tuple[str, ...] = (INCIDENT_STATUS_OPEN, INCIDENT_STATUS_CLOSED)

#: Incidents are kept indefinitely (they are small and are the historical record).
INCIDENT_PREFIX = "INCIDENT#"
ALERT_MARKER_SORT_KEY = "ALERT#COOLDOWN"

#: Severities that are worth an SNS alert at all.
ALERT_SEVERITIES: tuple[str, ...] = ("WARNING", "CRITICAL")

#: Per-zone, per-severity cooldown: the same zone cannot alert twice with the same
#: severity inside this window, so a repeated simulator run cannot spam subscribers.
ALERT_COOLDOWN_SECONDS = 600
ALERT_COOLDOWN_ENV = "FLOODPULSE_ALERT_COOLDOWN_SECONDS"
ALERT_COOLDOWN_MIN_SECONDS = 0
ALERT_COOLDOWN_MAX_SECONDS = 24 * 60 * 60

#: Read from the environment; never returned to a client.
SNS_TOPIC_ENV = "FLOODPULSE_SNS_TOPIC_ARN"

INCIDENT_DEFAULT_LIMIT = 50
INCIDENT_MAX_LIMIT = 200

# --------------------------------------------------------------------------- #
# Public email subscriptions (M10, part B)
# --------------------------------------------------------------------------- #
# A visitor may ask to be emailed when a city reaches WARNING or CRITICAL. The
# subscription itself belongs to SNS: FloodPulse calls ``Subscribe`` with
# ``Protocol="email"`` and AWS sends the confirmation message and owns the
# endpoint from then on. Nothing about the visitor's address is stored: the only
# record FloodPulse keeps is a hashed counter used to throttle the route.
#
# The topic ARN comes from the environment only (``SNS_TOPIC_ENV`` above). A client
# can never choose the topic, the protocol or the region.

#: RFC 5321-ish upper bound on an address; longer input is rejected outright.
SUBSCRIPTION_MAX_EMAIL_LENGTH = 254
#: Longest local part and domain label accepted by the syntax check.
SUBSCRIPTION_MAX_LOCAL_LENGTH = 64
SUBSCRIPTION_MAX_LABEL_LENGTH = 63
#: The subscribe route takes one short field, so its body limit is far below the
#: 4 KB ingestion limit: a large body is a probe, not a visitor.
SUBSCRIPTION_BODY_MAX_BYTES = 512
#: Attempts per address per window, and the window they are counted in.
SUBSCRIPTION_RATE_LIMIT = 3
SUBSCRIPTION_WINDOW_SECONDS = 60 * 60
#: DynamoDB TTL on the counter: the window plus a little slack, so the item is gone
#: shortly after the window it counted. TTL deletion is asynchronous and best-effort
#: (AWS removes items "typically within 48 hours"), which is why the counter is also
#: keyed by the window: a stale item can never be counted against a later window.
SUBSCRIPTION_RATE_TTL_SECONDS = SUBSCRIPTION_WINDOW_SECONDS + 60 * 60
#: Storage prefixes for the counter items. The key is a SHA-256 digest of the
#: normalised address, never the address itself.
SUBSCRIPTION_RATE_PREFIX = "SUBRATE#"
SUBSCRIPTION_RATE_WINDOW_PREFIX = "WINDOW#"

#: The three answers this route can give. They are deliberately indistinguishable
#: for "first request", "already pending" and "already confirmed": telling a caller
#: which one it is would turn the endpoint into a way to test whether an arbitrary
#: address is subscribed to FloodPulse.
SUBSCRIPTION_STATUS_CONFIRMATION_REQUIRED = "confirmation_required"
SUBSCRIPTION_MESSAGE_CONFIRMATION_REQUIRED = "Check your email to confirm the subscription."
SUBSCRIPTION_STATUS_RATE_LIMITED = "rate_limited"
SUBSCRIPTION_MESSAGE_RATE_LIMITED = "Too many subscription requests. Please try again later."
SUBSCRIPTION_STATUS_UNAVAILABLE = "subscription_unavailable"
SUBSCRIPTION_MESSAGE_UNAVAILABLE = (
    "We couldn't create the subscription request right now. Please try again."
)

#: Documented in API responses so the dashboard can be honest about what exists.
DEFERRED_FEATURES: tuple[str, ...] = ("time_to_threshold",)

SIMULATION_DISCLAIMER = (
    "All FloodPulse sensor data is SIMULATED. Risk scores come from the "
    "heuristic-v1 deterministic scoring heuristic, which is not a validated "
    "hydrological model, not a statistical forecast and not a machine-learning "
    "prediction."
)

#: Used instead of the disclaimer above when the active fleet includes real weather.
MIXED_DATA_DISCLAIMER = (
    "Rainfall intensity and soil saturation in these zones come from the external "
    "Open-Meteo weather service; drain level, water level and flow rate are SIMULATED by "
    "FloodPulse's deterministic hydraulic response, because no real drain or flow sensors "
    "exist. Risk scores come from the heuristic-v1 deterministic scoring heuristic, which "
    "is not a validated hydrological model, is not calibrated for these cities, and is not "
    "an official flood warning."
)


def zone_definitions_for_mode(mode: str) -> tuple[dict[str, Any], ...]:
    """Return the zone definitions for a zone mode (accepts ``live``/``demo`` aliases)."""
    normalised = zone_mode_input(mode)
    if normalised == ZONE_MODE_INPUT_LIVE:
        return LIVE_ZONE_DEFINITIONS
    if normalised == ZONE_MODE_INPUT_DEMO:
        return DEMO_ZONE_DEFINITIONS
    raise KeyError(mode)


def zone_mode_input(raw: str | None) -> str:
    """Normalise any accepted zone-mode spelling to the short form ``live``/``demo``.

    Unknown values fall back to the default (the simulator), so a typo can never switch
    the deployment onto the live weather provider by accident.
    """
    return (
        ZONE_MODE_INPUT_LIVE
        if zone_mode_canonical(raw) == ZONE_MODE_LIVE
        else ZONE_MODE_INPUT_DEMO
    )


def zone_mode_canonical(raw: str | None) -> str:
    """Normalise any accepted zone-mode spelling to the canonical value the API reports."""
    return ZONE_MODE_ALIASES.get((raw or "").strip().lower(), DEFAULT_ZONE_MODE)


def zone_ids_for_mode(mode: str) -> tuple[str, ...]:
    """Return the zone ids of a zone mode."""
    return tuple(zone["zone_id"] for zone in zone_definitions_for_mode(mode))


def zone_mode_for_env(env: Mapping[str, str] | None = None) -> str:
    """Resolve the canonical active zone mode from the environment (simulator by default)."""
    environment = os.environ if env is None else env
    return zone_mode_canonical(environment.get(ZONE_MODE_ENV))


def zone_ids_for_env(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Return the active zone ids for this environment."""
    return zone_ids_for_mode(zone_mode_for_env(env))


def zone_definition(zone_id: str) -> dict[str, Any]:
    """Return the fixed definition for ``zone_id`` (raises ``KeyError`` if unknown)."""
    for zone in DEMO_ZONE_DEFINITIONS + LIVE_ZONE_DEFINITIONS:
        if zone["zone_id"] == zone_id:
            return deepcopy(zone)
    raise KeyError(zone_id)


def zone_display_for_mode(zone_id: str, mode: str) -> dict[str, str]:
    """Return the provenance labels for a zone: its mode and each metric's source tag."""
    resolved = zone_mode_input(mode)
    sources = METRIC_SOURCES_LIVE if resolved == ZONE_MODE_INPUT_LIVE else METRIC_SOURCES_SIMULATED
    return {"mode": resolved, "metric_sources": dict(sources)}


def data_classification_for_mode(mode: str) -> str:
    """``mixed`` once any metric of the active fleet comes from outside FloodPulse."""
    return (
        DATA_CLASSIFICATION_MIXED
        if zone_mode_input(mode) == ZONE_MODE_INPUT_LIVE
        else DATA_CLASSIFICATION_SIMULATED
    )


def thresholds_for(zone_id: str) -> dict[str, dict[str, float]]:
    """Return default thresholds merged with the zone's overrides."""
    zone = zone_definition(zone_id)
    merged = deepcopy(DEFAULT_THRESHOLDS)
    for metric, override in zone["thresholds"].items():
        if metric not in merged:
            raise KeyError(f"override for unknown metric {metric!r} in zone {zone_id!r}")
        merged[metric].update(override)
    return merged


# --------------------------------------------------------------------------- #
# City labels and coverage (M11): derived, never stored twice
# --------------------------------------------------------------------------- #
# The definitions above are the only place a city name, state, region or coordinate is
# written. Everything else - the API, the alert subject line, the incident record's
# labels, the dashboard - derives from them through these helpers, so there is exactly
# one value to correct if a city moves state or a coordinate is wrong.


def city_label(zone: Mapping[str, Any]) -> str:
    """``"City, State"`` when the state is known, otherwise just the city name."""
    name = str(zone.get("name") or zone.get("zone_id") or "")
    state = zone.get("state")
    return f"{name}, {state}" if state else name


def city_scope_for(zone_id: str) -> dict[str, Any]:
    """Labels for a zone id: city, state, region and the display name.

    Never raises. An incident or an alert can outlive a definition (a zone dropped from a
    later deployment), and an unknown id must not turn a read into a 500: it comes back
    with no state or region and its own id as the display name, which is the truth -
    FloodPulse has no metadata for it.
    """
    for zone in DEMO_ZONE_DEFINITIONS + LIVE_ZONE_DEFINITIONS:
        if zone["zone_id"] == zone_id:
            return {
                "city": str(zone["name"]),
                "state": zone.get("state"),
                "region": zone.get("region"),
                "display_name": city_label(zone),
            }
    return {"city": None, "state": None, "region": None, "display_name": str(zone_id)}


def coverage_summary(mode: str) -> dict[str, Any]:
    """What the active fleet covers, derived from its definitions (no hardcoded totals).

    Aggregates only: the per-city detail is in the ``zones`` array of the same response.
    ``fleet_kind`` is ``cities`` for the live fleet and ``zones`` for the simulator, so
    the copy built on top of this cannot call three invented districts a city network.
    """
    definitions = zone_definitions_for_mode(mode)
    live = zone_mode_input(mode) == ZONE_MODE_INPUT_LIVE
    states = sorted({str(zone["state"]) for zone in definitions if zone.get("state")})
    regions = [
        region for region in REGIONS
        if any(zone.get("region") == region for zone in definitions)
    ]
    return {
        "fleet_kind": "cities" if live else "zones",
        "city_count": len(definitions),
        "state_count": len(states),
        "region_count": len(regions),
        "states": states,
        "regions": [
            {
                "region": region,
                "city_count": sum(1 for zone in definitions if zone.get("region") == region),
                "cities": [str(zone["name"]) for zone in definitions if zone.get("region") == region],
            }
            for region in regions
        ],
        "scope_note": COVERAGE_SCOPE_NOTE,
    }

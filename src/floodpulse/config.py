"""Static configuration for the FloodPulse MVP (milestone M0, local only).

Every numeric constant in this module is *heuristic tuning configuration* for the
``heuristic-v1`` risk model. None of it is calibrated against measured hydrological
data, and the resulting score must never be presented as a validated flood model.

No AWS calls are made anywhere in this package at M0.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

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
# Fixed zone definitions (3 zones, MVP scope)
# --------------------------------------------------------------------------- #
# ``thresholds`` holds per-zone overrides only; empty means "use the defaults".
# Overrides model different local drainage capacity, not measured calibration.

ZONE_DEFINITIONS: tuple[dict[str, Any], ...] = (
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

ZONE_IDS: tuple[str, ...] = tuple(zone["zone_id"] for zone in ZONE_DEFINITIONS)

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

#: Documented in API responses so the dashboard can be honest about what exists.
DEFERRED_FEATURES: tuple[str, ...] = ("time_to_threshold",)

SIMULATION_DISCLAIMER = (
    "All FloodPulse sensor data is SIMULATED. Risk scores come from the "
    "heuristic-v1 deterministic scoring heuristic, which is not a validated "
    "hydrological model, not a statistical forecast and not a machine-learning "
    "prediction."
)


def zone_definition(zone_id: str) -> dict[str, Any]:
    """Return the fixed definition for ``zone_id`` (raises ``KeyError`` if unknown)."""
    for zone in ZONE_DEFINITIONS:
        if zone["zone_id"] == zone_id:
            return deepcopy(zone)
    raise KeyError(zone_id)


def thresholds_for(zone_id: str) -> dict[str, dict[str, float]]:
    """Return default thresholds merged with the zone's overrides."""
    zone = zone_definition(zone_id)
    merged = deepcopy(DEFAULT_THRESHOLDS)
    for metric, override in zone["thresholds"].items():
        if metric not in merged:
            raise KeyError(f"override for unknown metric {metric!r} in zone {zone_id!r}")
        merged[metric].update(override)
    return merged

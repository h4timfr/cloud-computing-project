"""Flood incident tracking (M3).

An *incident* is a bookkeeping record derived from a zone's **effective** risk class
(the hysteresis-filtered one). The rules are deliberately small and explainable:

* a zone opens an incident when it reaches ``WARNING`` or ``CRITICAL``;
* only one incident per zone can be open at a time, so a zone cannot "spam" incidents
  while the same event is running;
* reaching ``CRITICAL`` while the open incident is ``WARNING`` *escalates* that incident
  (it is raised, not duplicated);
* the open incident closes only when the zone's effective class returns to ``NORMAL``,
  which already implies the configured de-escalation streak has been satisfied;
* closed incidents are kept as history.

Everything here is pure and AWS-free: the caller supplies the current item and the new
observation, and gets back the item to store. ``notify.py`` handles the SNS side.

An incident is *not* a hydrological statement: it records that the heuristic crossed a
band, nothing more.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from .config import (
    ALERT_MARKER_SORT_KEY,
    ALERT_SEVERITIES,
    city_scope_for,
    DATA_CLASSIFICATION_SIMULATED,
    INCIDENT_CLOSING_CLASS,
    INCIDENT_OPEN_SEVERITIES,
    INCIDENT_PREFIX,
    INCIDENT_STATUS_CLOSED,
    INCIDENT_STATUS_OPEN,
    METRIC_LABELS,
    RISK_MODEL_ID,
    RISK_ORDER,
    zone_definition,
)
from .storage import alert_cooldown_remaining, build_zone_pk, sort_incident_items

ACTION_OPENED = "opened"
ACTION_ESCALATED = "escalated"
ACTION_OBSERVED = "observed"
ACTION_CLOSED = "closed"
ACTION_NONE = "none"


def iso_utc(epoch: float) -> str:
    """Second-resolution ISO-8601 UTC, matching the timestamp format used elsewhere."""
    return (
        datetime.fromtimestamp(float(epoch), tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def incident_sort_key(opened_ts_epoch: float) -> str:
    """Sort key for an incident (lexicographic order == chronological order)."""
    return f"{INCIDENT_PREFIX}{iso_utc(opened_ts_epoch)}"


def incident_id_for(zone_id: str, opened_ts_epoch: float) -> str:
    """Deterministic, readable incident id: ``inc-<zone>-<UTC timestamp>``."""
    stamp = iso_utc(opened_ts_epoch).replace("-", "").replace(":", "")
    return f"inc-{zone_id.replace('zone-', '')}-{stamp}"


#: Width of the arrival suffix used when two incidents for one zone would share a second.
#: Zero-padded because both stores order incidents by the sort key lexicographically, so a
#: plain decimal would put ``#10`` before ``#2``.
INCIDENT_SUFFIX_WIDTH = 6
#: How many same-second slots an open attempt may try before giving up. A flood of more
#: than eight openings inside one second for one zone is not a scenario worth engineering
#: for; the reading still scores and still reaches the history.
INCIDENT_KEY_ATTEMPTS = 8


def incident_suffix_key(sk: str, index: int) -> str:
    """The sort key for the ``index``-th incident in the same second (``index >= 2``)."""
    return f"{sk}#{int(index):0{INCIDENT_SUFFIX_WIDTH}d}"


def incident_suffix_id(incident_id: str, index: int) -> str:
    """The id for the ``index``-th incident in the same second: unique, still readable.

    Two incidents in one second are two events (the first closed, the second opened), so
    they must not share an id: a page keyed by incident id, or an operator reading a log,
    would otherwise see one event where there were two.
    """
    return f"{incident_id}-r{int(index)}"


def metric_label(metric: str | None) -> str | None:
    """Display name for a metric key (``None`` stays ``None``)."""
    if not metric:
        return None
    return METRIC_LABELS.get(str(metric), str(metric))


def severity_rank(name: Any) -> int:
    """NORMAL 0 … CRITICAL 3; -1 for anything unrecognised."""
    return RISK_ORDER.get(str(name), -1)


def build_incident_item(
    *,
    zone_id: str,
    severity: str,
    risk_score: float,
    risk_class: str,
    top_driver: str | None,
    opened_ts_epoch: float,
    now_epoch: float,
    data_classification: str = DATA_CLASSIFICATION_SIMULATED,
) -> dict[str, Any]:
    """Build a brand-new OPEN incident item.

    ``data_classification`` is the provenance of the reading that opened the incident, so an
    incident on a live-weather city is never stored (or later served) as if the fleet were
    fully simulated.
    """
    zone = zone_definition(zone_id)
    scope = city_scope_for(zone_id)
    return {
        "pk": build_zone_pk(zone_id),
        "sk": incident_sort_key(opened_ts_epoch),
        "record_type": "INCIDENT",
        "incident_id": incident_id_for(zone_id, opened_ts_epoch),
        "zone_id": zone_id,
        "zone_name": zone["name"],
        # The incident is stamped with the city's state and coast group as the city table
        # knew them when it opened, so an alert can name "Kochi, Kerala" without a second
        # lookup and a later edit to the table cannot silently rewrite the historical record.
        "zone_state": scope["state"],
        "zone_coast_group": scope.get("coast_group"),
        "zone_display_name": scope["display_name"],
        "severity": severity,
        "severity_at_open": severity,
        "current_status": INCIDENT_STATUS_OPEN,
        "opened_at": iso_utc(opened_ts_epoch),
        "opened_ts_epoch": int(opened_ts_epoch),
        "closed_at": None,
        "closed_ts_epoch": None,
        "duration_seconds": None,
        "risk_score_at_open": float(risk_score),
        "risk_class_at_open": risk_class,
        "risk_score_peak": float(risk_score),
        "last_risk_score": float(risk_score),
        "last_risk_class": risk_class,
        "top_driver": top_driver,
        "top_driver_label": metric_label(top_driver),
        "reading_count": 1,
        "alert_count": 0,
        "alert_severities": [],
        "alerts": [],
        "severity_changes": [],
        "close_reason": None,
        "created_ts_epoch": int(now_epoch),
        "updated_at": iso_utc(now_epoch),
        "updated_ts_epoch": int(now_epoch),
        "model": RISK_MODEL_ID,
        "simulated": data_classification == DATA_CLASSIFICATION_SIMULATED,
        "data_classification": data_classification,
    }


@dataclass(frozen=True)
class Transition:
    """Outcome of feeding one scored reading into the incident rules."""

    action: str
    incident: dict[str, Any] | None
    #: Severity to alert on (``None`` means "nothing worth alerting about").
    alert_severity: str | None = None
    reason: str = ""


def decide_transition(
    *,
    zone_id: str,
    active: Mapping[str, Any] | None,
    risk_class: str,
    risk_score: float,
    top_driver: str | None,
    observed_ts_epoch: float,
    now_epoch: float,
    data_classification: str = DATA_CLASSIFICATION_SIMULATED,
) -> Transition:
    """Apply the incident rules to one observation.

    ``active`` is the zone's newest incident record (open or closed), or ``None``.
    ``risk_class`` must be the *effective* class so the existing hysteresis streak
    requirement governs both opening and closing.
    """
    open_incident = active if (active and active.get("current_status") == INCIDENT_STATUS_OPEN) else None

    if open_incident is None:
        if risk_class not in INCIDENT_OPEN_SEVERITIES:
            return Transition(ACTION_NONE, None, None, "no open incident and below the incident threshold")
        item = build_incident_item(
            zone_id=zone_id,
            severity=risk_class,
            risk_score=risk_score,
            risk_class=risk_class,
            top_driver=top_driver,
            opened_ts_epoch=observed_ts_epoch,
            now_epoch=now_epoch,
            data_classification=data_classification,
        )
        return Transition(ACTION_OPENED, item, risk_class, f"zone reached {risk_class}")

    updated = dict(open_incident)
    updated["last_risk_score"] = float(risk_score)
    updated["last_risk_class"] = risk_class
    updated["reading_count"] = int(updated.get("reading_count") or 0) + 1
    updated["risk_score_peak"] = max(float(updated.get("risk_score_peak") or 0.0), float(risk_score))
    updated["updated_at"] = iso_utc(now_epoch)
    updated["updated_ts_epoch"] = int(now_epoch)
    if top_driver:
        updated["top_driver"] = top_driver
        updated["top_driver_label"] = metric_label(top_driver)

    if risk_class == INCIDENT_CLOSING_CLASS:
        closed_epoch = int(observed_ts_epoch)
        updated["current_status"] = INCIDENT_STATUS_CLOSED
        updated["closed_at"] = iso_utc(observed_ts_epoch)
        updated["closed_ts_epoch"] = closed_epoch
        updated["duration_seconds"] = max(0, closed_epoch - int(updated["opened_ts_epoch"]))
        updated["close_reason"] = f"zone returned to {INCIDENT_CLOSING_CLASS}"
        return Transition(ACTION_CLOSED, updated, None, updated["close_reason"])

    if severity_rank(risk_class) > severity_rank(updated.get("severity")):
        changes = list(updated.get("severity_changes") or [])
        changes.append(
            {
                "from": updated.get("severity"),
                "to": risk_class,
                "at": iso_utc(observed_ts_epoch),
                "ts_epoch": int(observed_ts_epoch),
                "risk_score": float(risk_score),
            }
        )
        updated["severity_changes"] = changes
        updated["severity"] = risk_class
        return Transition(ACTION_ESCALATED, updated, risk_class, f"incident escalated to {risk_class}")

    return Transition(ACTION_OBSERVED, updated, None, "still elevated, incident stays open")


def cooldown_remaining_seconds(
    marker: Mapping[str, Any] | None,
    severity: str,
    now_epoch: float,
    cooldown_seconds: int,
) -> int:
    """Seconds still to wait before ``severity`` may alert again for this zone.

    Delegates to the single predicate in :mod:`floodpulse.storage` that the stores use to
    claim an alert slot, so the number reported to a client and the number the conditional
    write enforces can never disagree.
    """
    return alert_cooldown_remaining(marker, severity, now_epoch, cooldown_seconds)


def marker_rollback(zone_id: str, previous: Mapping[str, Any] | None) -> dict[str, Any]:
    """The cooldown marker to restore when a claimed alert could not be published.

    A claim is written *before* the publish so that concurrent escalations cannot both
    alert. If the publish then fails, restoring the previous marker is what stops the
    claim from muting the next attempt for a whole cooldown window. The item is written
    rather than deleted because the deployed role deliberately has no DynamoDB delete
    permission, and "no entry for this severity" is exactly what a rollback to a missing
    marker means to :func:`cooldown_remaining_seconds`.
    """
    if previous:
        return dict(previous)
    return {
        "pk": build_zone_pk(zone_id),
        "sk": ALERT_MARKER_SORT_KEY,
        "record_type": "ALERT_MARKER",
        "zone_id": zone_id,
        "last_alert_by_severity": {},
    }


def build_alert_marker(
    *,
    zone_id: str,
    severity: str,
    now_epoch: float,
    previous: Mapping[str, Any] | None = None,
    message_id: str | None = None,
    data_classification: str = DATA_CLASSIFICATION_SIMULATED,
) -> dict[str, Any]:
    """Per-zone ``ALERT#COOLDOWN`` marker recording the last alert per severity.

    Kept outside the incident so the cooldown also survives close/reopen cycles
    (a repeated simulator run must not re-alert the same subscribers).
    """
    per_severity = dict((previous or {}).get("last_alert_by_severity") or {})
    per_severity[severity] = {
        "ts_epoch": int(now_epoch),
        "at": iso_utc(now_epoch),
        "message_id": message_id,
    }
    return {
        "pk": build_zone_pk(zone_id),
        "sk": ALERT_MARKER_SORT_KEY,
        "record_type": "ALERT_MARKER",
        "zone_id": zone_id,
        "simulated": data_classification == DATA_CLASSIFICATION_SIMULATED,
        "data_classification": data_classification,
        "last_alert_by_severity": per_severity,
        "updated_at": iso_utc(now_epoch),
        "updated_ts_epoch": int(now_epoch),
    }


def build_alert_entry(
    *,
    severity: str,
    now_epoch: float,
    published: bool,
    reason: str = "",
    message_id: str | None = None,
) -> dict[str, Any]:
    """One line in an incident's alert log (never contains the topic ARN)."""
    return {
        "severity": severity,
        "at": iso_utc(now_epoch),
        "ts_epoch": int(now_epoch),
        "published": bool(published),
        "reason": reason,
        "message_id": message_id,
    }


def record_alert(incident: Mapping[str, Any], entry: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of ``incident`` with one alert attempt appended."""
    updated = dict(incident)
    alerts = list(updated.get("alerts") or [])
    alerts.append(dict(entry))
    updated["alerts"] = alerts
    if entry.get("published"):
        updated["alert_count"] = int(updated.get("alert_count") or 0) + 1
        severities = list(updated.get("alert_severities") or [])
        if entry.get("severity") not in severities:
            severities.append(entry.get("severity"))
        updated["alert_severities"] = severities
    return updated


def incident_view(item: Mapping[str, Any], now_epoch: float) -> dict[str, Any]:
    """Client-facing projection of an incident item (durations computed server-side)."""
    opened = int(item["opened_ts_epoch"])
    is_open = item.get("current_status") == INCIDENT_STATUS_OPEN
    classification = str(item.get("data_classification") or DATA_CLASSIFICATION_SIMULATED)
    scope = city_scope_for(str(item.get("zone_id") or ""))
    closed_epoch = item.get("closed_ts_epoch")
    if is_open:
        duration = max(0, int(now_epoch) - opened)
    elif closed_epoch is not None:
        duration = max(0, int(closed_epoch) - opened)
    else:
        duration = None
    return {
        "incident_id": item.get("incident_id"),
        "zone_id": item.get("zone_id"),
        "zone_name": item.get("zone_name"),
        # Geography is looked up when the stored record does not carry it (incidents opened
        # before M11, or by a deployment that predates the state column): the zone id is in
        # the same city table, so the label is derived rather than missing. An id with no
        # definition at all comes back with ``None`` - never with a guessed state.
        "zone_state": item.get("zone_state", scope["state"]),
        # ``zone_region`` is the pre-M13 spelling of this column: incidents opened then are read
        # with it rather than losing their geography, and its value was a region name, which
        # nothing displays as a coast group.
        "zone_coast_group": item.get(
            "zone_coast_group", item.get("zone_region", scope.get("coast_group"))
        ),
        "zone_display_name": item.get("zone_display_name") or scope["display_name"],
        "severity": item.get("severity"),
        "severity_at_open": item.get("severity_at_open"),
        "current_status": item.get("current_status"),
        "is_open": is_open,
        "opened_at": item.get("opened_at"),
        "opened_ts_epoch": opened,
        "closed_at": item.get("closed_at"),
        "closed_ts_epoch": closed_epoch,
        "duration_seconds": duration,
        "risk_score_at_open": item.get("risk_score_at_open"),
        "risk_class_at_open": item.get("risk_class_at_open"),
        "risk_score_peak": item.get("risk_score_peak"),
        "last_risk_score": item.get("last_risk_score"),
        "last_risk_class": item.get("last_risk_class"),
        "top_driver": item.get("top_driver"),
        "top_driver_label": item.get("top_driver_label"),
        "reading_count": item.get("reading_count"),
        "alert_count": item.get("alert_count"),
        "alert_severities": list(item.get("alert_severities") or []),
        "escalation_count": len(item.get("severity_changes") or []),
        "close_reason": item.get("close_reason"),
        "model": item.get("model", RISK_MODEL_ID),
        # Both flags come from the stored record: a live-weather incident must never be
        # served with a "simulated" flag next to a "mixed" classification.
        "simulated": classification == DATA_CLASSIFICATION_SIMULATED,
        "data_classification": classification,
    }


def sort_incidents_desc(items: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Newest first; the tie-break lives in ``storage`` so both stores agree exactly."""
    return sort_incident_items(list(items))


def alerting_view(enabled: bool, cooldown_seconds: int) -> dict[str, Any]:
    """Alert state for the dashboard. Deliberately excludes the topic ARN."""
    return {
        "enabled": bool(enabled),
        "severities": list(ALERT_SEVERITIES),
        "cooldown_seconds": int(cooldown_seconds),
    }

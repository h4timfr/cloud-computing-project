"""Storage layer for the FloodPulse MVP.

M0 uses an in-memory store only: **no AWS calls anywhere in this module**.
Item shapes are deliberately DynamoDB-compatible so milestone M1 can persist the
same dicts with ``boto3`` unchanged:

* table key: ``pk`` (partition) + ``sk`` (sort)
* ``pk = "ZONE#<zone_id>"``
* ``sk = "LATEST"``   -> the newest scored reading for the zone (overwritten)
* ``sk = "H#<timestamp>"`` -> timestamped history record (TTL via ``ttl``)
* ``sk = "INCIDENT#<timestamp>"`` -> one flood incident (M3, kept indefinitely)
* ``sk = "ALERT#COOLDOWN"`` -> per-zone alert cooldown marker (M3)

There is intentionally **no** aggregate snapshot item: zones are read individually.
"""

from __future__ import annotations

import decimal
import math
from dataclasses import dataclass
from typing import Any, Iterator, Mapping

from .config import ALERT_MARKER_SORT_KEY, INCIDENT_PREFIX, WEATHER_CACHE_SORT_KEY

LATEST_SORT_KEY = "LATEST"
HISTORY_PREFIX = "H#"

#: Outcomes of one accepted reading. Reported verbatim in the ``POST /readings`` response,
#: so a client can tell "stored" from "already stored" from "older than what we have".
READING_STORED = "stored"
READING_DUPLICATE = "duplicate"
READING_STALE = "stale"

#: Metrics are rounded to 4 decimals by validation before they are stored, and a float that
#: has been through DynamoDB's ``Decimal`` round trip compares exactly equal, so two
#: readings count as the same observation only when every metric matches to within this
#: (deliberately far below the 4-decimal resolution) tolerance.
METRIC_EQUALITY_TOLERANCE = 1e-9


def build_zone_pk(zone_id: str) -> str:
    """Partition key for a zone."""
    return f"ZONE#{zone_id}"


def _sort_key_of(item: Any) -> str:
    """A record's sort key, for the tie-break when two records share a timestamp."""
    return str(item.get("sk") or "") if isinstance(item, Mapping) else ""


def history_stamp(item: Mapping[str, Any]) -> int | None:
    """A history item's ``ts_epoch`` as an int, or ``None`` when it cannot be placed in time.

    BUG CLASS (M16): ``LocalStore.get_history`` used to read ``int(item["ts_epoch"])`` directly
    while sorting, so one half-written or legacy record raised ``KeyError``/``TypeError`` out of
    a *read* and the history route answered 500 for the zone's whole window. A record with an
    unusable stamp is kept in the returned list (ordered by its sort key) and the route decides
    what can be shown - see ``handler._history_point``, which drops it.
    """
    value = item.get("ts_epoch") if isinstance(item, Mapping) else None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return int(number) if math.isfinite(number) else None
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def incident_sort_value(item: Mapping[str, Any]) -> tuple[int, str, str]:
    """Canonical newest-first ordering key for incidents (shared by both stores)."""
    return (
        -int(item.get("opened_ts_epoch") or 0),
        str(item.get("zone_id") or ""),
        str(item.get("incident_id") or ""),
    )


def sort_incident_items(items: list[Mapping[str, Any]]) -> list[Any]:
    """Newest first, with a deterministic tie-break by zone then incident id."""
    return sorted(items, key=incident_sort_value)


# --------------------------------------------------------------------------- #
# Duplicate / out-of-order protection (shared by both stores)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WriteOutcome:
    """What one accepted reading actually did to stored state.

    ``outcome`` is one of :data:`READING_STORED`, :data:`READING_DUPLICATE` (this exact
    observation is already in history) or :data:`READING_STALE` (it is older than the
    zone's current reading, so it must not become current state).
    """

    outcome: str
    latest_updated: bool
    history_written: bool
    history_sk: str | None = None
    reason: str | None = None
    stored_ts_epoch: int | None = None
    incoming_ts_epoch: int | None = None

    @property
    def current_state_changed(self) -> bool:
        """True only when this reading became the zone's newest state."""
        return self.latest_updated

    def as_view(self) -> dict[str, Any]:
        """The ``dedupe`` block of the ingestion response."""
        return {
            "outcome": self.outcome,
            "latest_updated": self.latest_updated,
            "history_written": self.history_written,
            "history_sk": self.history_sk,
            "reason": self.reason,
            "stored_ts_epoch": self.stored_ts_epoch,
            "incoming_ts_epoch": self.incoming_ts_epoch,
        }


def classify_reading_write(
    previous_latest: Mapping[str, Any] | None, reading: Mapping[str, Any]
) -> tuple[str, str | None]:
    """Whether an incoming reading may become the zone's current state.

    A reading older than the stored one is still a real observation and is kept as
    history, but it must never overwrite newer current state: a replay, a retry that
    overtook a newer reading, or a provider hiccup must not roll the fleet backwards.
    Equal timestamps are allowed through, so a same-second re-post is not treated as a
    stale write.
    """
    if not previous_latest:
        return READING_STORED, None
    stored_ts = previous_latest.get("ts_epoch")
    incoming_ts = reading.get("ts_epoch")
    if not isinstance(stored_ts, (int, float)) or isinstance(stored_ts, bool):
        # An unreadable stored timestamp cannot be used to reject a reading: the history
        # record still gets written and the LATEST write overwrites the unusable item.
        return READING_STORED, None
    if not isinstance(incoming_ts, (int, float)) or isinstance(incoming_ts, bool):
        return READING_STORED, None
    if int(incoming_ts) < int(stored_ts):
        return READING_STALE, "older than the zone's current reading"
    return READING_STORED, None


def _metrics_match(stored: Mapping[str, Any], incoming: Mapping[str, Any]) -> bool:
    if set(stored) != set(incoming):
        return False
    for metric, incoming_value in incoming.items():
        try:
            if abs(float(stored[metric]) - float(incoming_value)) > METRIC_EQUALITY_TOLERANCE:
                return False
        except (TypeError, ValueError):
            if stored[metric] != incoming_value:
                return False
    return True


def alert_cooldown_remaining(
    marker: Mapping[str, Any] | None,
    severity: str,
    now_epoch: float,
    cooldown_seconds: int,
) -> int:
    """Seconds still to wait before ``severity`` may alert again for this zone.

    This is the single definition of the cooldown predicate: the in-memory store, the
    DynamoDB conditional claim and the API's reported ``cooldown_remaining_seconds`` all
    have to agree, or one of them would let a second alert through.
    """
    if not marker or cooldown_seconds <= 0:
        return 0
    per_severity = marker.get("last_alert_by_severity") or {}
    entry = per_severity.get(severity) if isinstance(per_severity, Mapping) else None
    if not isinstance(entry, Mapping):
        return 0
    try:
        last = int(entry.get("ts_epoch"))
    except (TypeError, ValueError):
        return 0
    remaining = int(cooldown_seconds) - (int(now_epoch) - last)
    return max(0, remaining)


def is_replay_of(stored_history: Mapping[str, Any] | None, reading: Mapping[str, Any]) -> bool:
    """True when ``stored_history`` is the same observation as ``reading``.

    Used to answer "did we already store exactly this?" so a client retry cannot append a
    second copy of the same reading to history (and cannot bump the zone's sequence).
    Comparison is on the zone, the timestamp and every metric value; provenance is included
    so a live reading and a simulated one that happen to share numbers stay distinct.
    """
    if not stored_history:
        return False
    if str(stored_history.get("zone_id") or "") != str(reading.get("zone_id") or ""):
        return False
    if int(stored_history.get("ts_epoch") or 0) != int(reading.get("ts_epoch") or 0):
        return False
    stored_readings = stored_history.get("readings")
    incoming_readings = reading.get("readings")
    if not isinstance(stored_readings, Mapping) or not isinstance(incoming_readings, Mapping):
        return False
    if not _metrics_match(stored_readings, incoming_readings):
        return False
    stored_source = stored_history.get("source")
    incoming_source = reading.get("source")
    if stored_source is None or incoming_source is None:
        return stored_source == incoming_source
    return str(stored_source) == str(incoming_source)


#: Provenance fields copied through only when a reading actually carries them, so
#: simulated records keep their exact pre-M4 shape.
PROVENANCE_FIELDS = ("metric_sources", "source", "provider", "provider_observed_at")


def _core_record(reading: Mapping[str, Any]) -> dict[str, Any]:
    """Fields shared by the LATEST and history items."""
    record = {
        "pk": build_zone_pk(str(reading["zone_id"])),
        "zone_id": reading["zone_id"],
        "sensor_node_id": reading["sensor_node_id"],
        "timestamp": reading["timestamp"],
        "ts_epoch": reading["ts_epoch"],
        "ttl": reading["ttl"],
        "simulated": bool(reading.get("simulated", True)),
        "data_classification": reading["data_classification"],
        "readings": dict(reading["readings"]),
        "meta": dict(reading.get("meta") or {}),
        "ingested_at_epoch": reading["ingested_at_epoch"],
    }
    for field in PROVENANCE_FIELDS:
        value = reading.get(field)
        if value is not None:
            record[field] = dict(value) if isinstance(value, Mapping) else value
    return record


def build_latest_item(
    reading: Mapping[str, Any],
    risk: Mapping[str, Any],
    hysteresis: Mapping[str, Any],
    smoothed_readings: Mapping[str, float],
    sequence: int,
) -> dict[str, Any]:
    """Build the ``ZONE#<id> / LATEST`` item (full explanation retained)."""
    item = _core_record(reading)
    item.update(
        {
            "sk": LATEST_SORT_KEY,
            "record_type": "LATEST",
            "sequence": sequence,
            "smoothed_readings": dict(smoothed_readings),
            "risk_score": risk["risk_score"],
            "risk_class": hysteresis["risk_class"],
            "risk_class_candidate": risk["risk_class_candidate"],
            "risk_detail": {
                "model": risk["model"],
                "model_note": risk["model_note"],
                "base_score": risk["base_score"],
                "trend": dict(risk["trend"]),
                "contributions": [dict(c) for c in risk["contributions"]],
                "top_drivers": list(risk["top_drivers"]),
                "explanation": risk["explanation"],
                "smoothing_window_readings": risk["smoothing_window_readings"],
            },
            "hysteresis": dict(hysteresis),
        }
    )
    return item


def history_sort_key_for(timestamp: str) -> str:
    """Sort key a history record for ``timestamp`` gets (one definition, both stores)."""
    return f"{HISTORY_PREFIX}{timestamp}"


def build_history_item(
    reading: Mapping[str, Any],
    risk: Mapping[str, Any],
    hysteresis: Mapping[str, Any],
    smoothed_readings: Mapping[str, float],
) -> dict[str, Any]:
    """Build a trimmed ``ZONE#<id> / H#<timestamp>`` history item (expires via TTL)."""
    item = _core_record(reading)
    item.update(
        {
            "sk": history_sort_key_for(str(reading["timestamp"])),
            "record_type": "HISTORY",
            "smoothed_readings": dict(smoothed_readings),
            "risk_score": risk["risk_score"],
            "risk_class": hysteresis["risk_class"],
            "risk_class_candidate": risk["risk_class_candidate"],
            "top_drivers": list(risk["top_drivers"]),
            "model": risk["model"],
        }
    )
    return item


class SubscribeThrottleUnavailable(RuntimeError):
    """The subscription counter could not be read or written.

    Raised instead of a bare ``ValueError``/store exception so the route can answer 503
    ("could not complete the request") rather than 500, and so a counter item whose
    ``attempts`` is not a number cannot turn every request from one address into an
    unhandled error for the rest of its window.
    """


def _counter_attempts(stored: Mapping[str, Any] | None) -> int:
    """The attempt count in a stored counter item, or a loud failure.

    A missing item counts as zero; anything that is not a non-negative whole number is
    treated as unusable rather than coerced, because guessing here would either hand a
    caller extra attempts or refuse them for the wrong reason. DynamoDB returns numbers as
    :class:`decimal.Decimal`, so a whole ``Decimal`` (and a whole float) is accepted.
    """
    if not stored:
        return 0
    raw = stored.get("attempts")
    if isinstance(raw, bool) or raw is None:
        raise SubscribeThrottleUnavailable("counter item holds an unusable attempts value")
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, (decimal.Decimal, float)):
        if not raw.is_finite():
            raise SubscribeThrottleUnavailable("counter item holds a non-finite attempts value")
        if raw != int(raw):
            raise SubscribeThrottleUnavailable("counter item holds a fractional attempts value")
        value = int(raw)
    else:
        raise SubscribeThrottleUnavailable(f"counter item holds an unusable attempts value: {type(raw).__name__}")
    if value < 0:
        raise SubscribeThrottleUnavailable("counter item holds a negative attempts value")
    return value


class LocalStore:
    """In-process stand-in for the DynamoDB table, keyed by ``(pk, sk)``."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict[str, Any]] = {}
        self._sequence: dict[str, int] = {}

    # -- writes ------------------------------------------------------------ #

    def put_reading(
        self,
        reading: Mapping[str, Any],
        risk: Mapping[str, Any],
        hysteresis: Mapping[str, Any],
        smoothed_readings: Mapping[str, float],
    ) -> tuple[dict[str, Any], dict[str, Any] | None, WriteOutcome]:
        """Write the zone's LATEST item and append a history item.

        Returns ``(stored_latest, stored_history_or_None, outcome)``. The returned LATEST is
        what is *now* stored: for a stale or duplicated reading that is the previous record,
        never a doorway to an older state. One history item per observation, so a retry
        cannot inflate the history, and an older arrival cannot become current state.
        """
        zone_id = str(reading["zone_id"])
        previous = self.get_latest(zone_id)
        outcome_name, reason = classify_reading_write(previous, reading)
        incoming_ts = int(reading["ts_epoch"])
        stored_ts = int((previous or {}).get("ts_epoch") or 0) or None

        pk = build_zone_pk(zone_id)
        candidate = build_history_item(reading, risk, hysteresis, smoothed_readings)
        existing = self._items.get((pk, str(candidate["sk"])))
        if is_replay_of(existing, reading):
            # The exact observation is already stored: no second copy, no state change.
            return (
                dict(previous or {}),
                None,
                WriteOutcome(
                    outcome=READING_DUPLICATE,
                    latest_updated=False,
                    history_written=False,
                    history_sk=str(existing["sk"]),
                    reason="this reading is already stored",
                    stored_ts_epoch=stored_ts or incoming_ts,
                    incoming_ts_epoch=incoming_ts,
                ),
            )

        candidate["sk"] = self._unique_history_key(pk, str(candidate["sk"]))
        self._items[(candidate["pk"], candidate["sk"])] = candidate

        if outcome_name == READING_STALE:
            return (
                dict(previous or {}),
                candidate,
                WriteOutcome(
                    outcome=READING_STALE,
                    latest_updated=False,
                    history_written=True,
                    history_sk=str(candidate["sk"]),
                    reason=reason,
                    stored_ts_epoch=stored_ts,
                    incoming_ts_epoch=incoming_ts,
                ),
            )

        sequence = int((previous or {}).get("sequence") or 0) + 1
        self._sequence[zone_id] = sequence
        latest = build_latest_item(reading, risk, hysteresis, smoothed_readings, sequence)
        self._items[(latest["pk"], latest["sk"])] = latest
        return (
            latest,
            candidate,
            WriteOutcome(
                outcome=READING_STORED,
                latest_updated=True,
                history_written=True,
                history_sk=str(candidate["sk"]),
                reason=reason,
                stored_ts_epoch=incoming_ts,
                incoming_ts_epoch=incoming_ts,
            ),
        )

    def _unique_history_key(self, pk: str, sk: str) -> str:
        """Disambiguate two readings that share the same timestamp."""
        if (pk, sk) not in self._items:
            return sk
        suffix = 2
        while (pk, f"{sk}#{suffix}") in self._items:
            suffix += 1
        return f"{sk}#{suffix}"

    # -- reads ------------------------------------------------------------- #

    def get_latest(self, zone_id: str) -> dict[str, Any] | None:
        """Return the newest scored reading for a zone, or ``None``."""
        return self._items.get((build_zone_pk(zone_id), LATEST_SORT_KEY))

    def all_latest(self) -> list[dict[str, Any]]:
        """All LATEST items, ordered by ``zone_id`` (deterministic)."""
        items = [item for item in self._items.values() if item["sk"] == LATEST_SORT_KEY]
        return sorted(items, key=lambda item: item["zone_id"])

    def get_history(
        self,
        zone_id: str,
        since_epoch: int = 0,
        until_epoch: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """History items for a zone, ascending by ``ts_epoch`` (then sort key).

        A record whose ``ts_epoch`` is unusable cannot be placed in time: it is never raised on
        (see :func:`history_stamp`), it is kept out of a window that starts after zero, it is
        ordered by its sort key, and the history route decides whether it can be shown.
        """
        pk = build_zone_pk(zone_id)
        pairs = [
            (history_stamp(item), item)
            for (item_pk, sk), item in self._items.items()
            if item_pk == pk and sk.startswith(HISTORY_PREFIX)
        ]
        if since_epoch:
            pairs = [pair for pair in pairs if pair[0] is None or pair[0] >= since_epoch]
        if until_epoch is not None:
            pairs = [pair for pair in pairs if pair[0] is None or pair[0] <= until_epoch]
        pairs.sort(key=lambda pair: (pair[0] if pair[0] is not None else 0, _sort_key_of(pair[1])))
        items = [item for _stamp, item in pairs]
        if limit is not None:
            items = items[-limit:]
        return items

    def recent_readings(self, zone_id: str, count: int) -> list[dict[str, Any]]:
        """The ``count`` most recent history items, oldest first."""
        if count <= 0:
            return []
        return self.get_history(zone_id, limit=count)

    def history_count(self, zone_id: str) -> int:
        """Number of history records stored for a zone."""
        pk = build_zone_pk(zone_id)
        return sum(
            1
            for (item_pk, sk) in self._items
            if item_pk == pk and sk.startswith(HISTORY_PREFIX)
        )

    def item_count(self) -> int:
        """Total number of stored items (LATEST + history + incidents + markers + cache)."""
        return len(self._items)

    # -- incidents (M3) ----------------------------------------------------- #

    def _incident_keys(self, zone_id: str | None = None) -> list[tuple[str, str]]:
        pks = [build_zone_pk(zone_id)] if zone_id is not None else [
            pk for (pk, _sk) in self._items if pk.startswith("ZONE#")
        ]
        return sorted(
            (pk, sk)
            for (pk, sk) in self._items
            if pk in pks and sk.startswith(INCIDENT_PREFIX)
        )

    def create_incident(self, item: Mapping[str, Any]) -> bool:
        """Insert an incident only if its sort key is still free.

        Returns ``False`` when the record already exists, mirroring the conditional
        write used against DynamoDB, so two concurrent requests cannot both open the
        same incident.
        """
        key = (str(item["pk"]), str(item["sk"]))
        if key in self._items:
            return False
        self._items[key] = dict(item)
        return True

    def put_incident(self, item: Mapping[str, Any]) -> dict[str, Any]:
        """Insert or update an incident record."""
        stored = dict(item)
        self._items[(str(stored["pk"]), str(stored["sk"]))] = stored
        return stored

    def latest_incident(self, zone_id: str) -> dict[str, Any] | None:
        """The zone's newest incident record (open or closed), or ``None``."""
        keys = self._incident_keys(zone_id)
        if not keys:
            return None
        return self._items[keys[-1]]

    def list_incidents(self, zone_id: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        """Incidents newest first, optionally for one zone and capped at ``limit``."""
        items = sort_incident_items([self._items[key] for key in self._incident_keys(zone_id)])
        if limit is not None:
            items = items[: int(limit)]
        return items

    def incident_count(self, zone_id: str | None = None) -> int:
        """Number of stored incident records."""
        return len(self._incident_keys(zone_id))

    def get_alert_marker(self, zone_id: str) -> dict[str, Any] | None:
        """The zone's alert cooldown marker, or ``None``."""
        return self._items.get((build_zone_pk(zone_id), ALERT_MARKER_SORT_KEY))

    def put_alert_marker(self, item: Mapping[str, Any]) -> dict[str, Any]:
        """Store the zone's alert cooldown marker."""
        stored = dict(item)
        self._items[(str(stored["pk"]), str(stored["sk"]))] = stored
        return stored

    def claim_alert_slot(
        self,
        item: Mapping[str, Any],
        *,
        severity: str,
        now_epoch: float,
        cooldown_seconds: int,
    ) -> bool:
        """Store the marker only if this caller is the one allowed to alert.

        Mirrors the conditional write the DynamoDB store performs: the first caller inside
        the window wins the slot and writes the marker, so concurrent escalations of the
        same zone and severity cannot both publish. Returns ``True`` when the slot was
        taken (and the marker written) or ``False`` when another caller holds it.
        """
        if cooldown_seconds <= 0:
            # A zero cooldown means "alert every time" by configuration: nothing to claim.
            self.put_alert_marker(item)
            return True
        if alert_cooldown_remaining(self.get_alert_marker(str(item["zone_id"])), severity, now_epoch, cooldown_seconds) > 0:
            return False
        self.put_alert_marker(item)
        return True

    # -- external weather cache (M4) ---------------------------------------- #

    def get_weather_cache(self, zone_id: str) -> dict[str, Any] | None:
        """The zone's cached external-weather snapshot, or ``None``."""
        return self._items.get((build_zone_pk(zone_id), WEATHER_CACHE_SORT_KEY))

    def put_weather_cache(self, item: Mapping[str, Any]) -> dict[str, Any]:
        """Store (overwrite) the zone's cached external-weather snapshot."""
        stored = dict(item)
        self._items[(str(stored["pk"]), str(stored["sk"]))] = stored
        return stored

    # -- public subscription throttle (M10, part B) -------------------------- #

    def claim_subscribe_attempt(
        self,
        key: Mapping[str, str],
        *,
        limit: int,
        ttl: int,
        now_epoch: float,
    ) -> int | None:
        """Count one subscription attempt, refusing it once the window's limit is reached.

        Mirrors the conditional ``ADD`` the DynamoDB store performs: the counter for the
        window is incremented only while it is below ``limit``, so two requests that arrive
        together cannot both slip past the last slot. Returns the attempt's new count, or
        ``None`` when the window is already full.

        The item holds ``pk``, ``sk``, ``attempts`` and ``ttl`` - never the address: the
        digest in ``pk`` is all the identity this needs.
        """
        pk = str(key["pk"])
        sk = str(key["sk"])
        stored = self._items.get((pk, sk))
        attempts = _counter_attempts(stored)
        if attempts >= int(limit):
            return None
        attempts += 1
        self._items[(pk, sk)] = {
            "pk": pk,
            "sk": sk,
            "attempts": attempts,
            "ttl": int(ttl),
        }
        return attempts

    def items(self) -> Iterator[dict[str, Any]]:
        """Iterate over stored items (test/debug helper)."""
        return iter(list(self._items.values()))

    def reset(self) -> None:
        """Drop all stored items."""
        self._items.clear()
        self._sequence.clear()

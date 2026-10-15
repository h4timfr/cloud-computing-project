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

from typing import Any, Iterator, Mapping

from .config import ALERT_MARKER_SORT_KEY, INCIDENT_PREFIX

LATEST_SORT_KEY = "LATEST"
HISTORY_PREFIX = "H#"


def build_zone_pk(zone_id: str) -> str:
    """Partition key for a zone."""
    return f"ZONE#{zone_id}"


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


def _core_record(reading: Mapping[str, Any]) -> dict[str, Any]:
    """Fields shared by the LATEST and history items."""
    return {
        "pk": build_zone_pk(str(reading["zone_id"])),
        "zone_id": reading["zone_id"],
        "sensor_node_id": reading["sensor_node_id"],
        "timestamp": reading["timestamp"],
        "ts_epoch": reading["ts_epoch"],
        "ttl": reading["ttl"],
        "simulated": True,
        "data_classification": reading["data_classification"],
        "readings": dict(reading["readings"]),
        "meta": dict(reading.get("meta") or {}),
        "ingested_at_epoch": reading["ingested_at_epoch"],
    }


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
            "sk": f"{HISTORY_PREFIX}{reading['timestamp']}",
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
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Write the zone's LATEST item and append a history item."""
        zone_id = str(reading["zone_id"])
        sequence = self._sequence.get(zone_id, 0) + 1
        self._sequence[zone_id] = sequence

        latest = build_latest_item(reading, risk, hysteresis, smoothed_readings, sequence)
        history = build_history_item(reading, risk, hysteresis, smoothed_readings)
        history["sk"] = self._unique_history_key(history["pk"], str(history["sk"]))

        self._items[(latest["pk"], latest["sk"])] = latest
        self._items[(history["pk"], history["sk"])] = history
        return latest, history

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
        """History items for a zone, ascending by ``ts_epoch`` (then sort key)."""
        pk = build_zone_pk(zone_id)
        items = [
            item
            for (item_pk, sk), item in self._items.items()
            if item_pk == pk and sk.startswith(HISTORY_PREFIX) and int(item["ts_epoch"]) >= since_epoch
        ]
        if until_epoch is not None:
            items = [item for item in items if int(item["ts_epoch"]) <= until_epoch]
        items.sort(key=lambda item: (int(item["ts_epoch"]), str(item["sk"])))
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
        """Total number of stored items (LATEST + history + incidents + markers)."""
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

    def items(self) -> Iterator[dict[str, Any]]:
        """Iterate over stored items (test/debug helper)."""
        return iter(list(self._items.values()))

    def reset(self) -> None:
        """Drop all stored items."""
        self._items.clear()
        self._sequence.clear()

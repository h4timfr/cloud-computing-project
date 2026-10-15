"""DynamoDB-backed store implementing the same interface as ``storage.LocalStore``.

Design notes (M1):

* ``boto3`` is imported **lazily** inside ``DynamoDbStore.__init__`` and only when no
  table object is injected, so local tests and tools never touch AWS or need boto3.
* item shapes come from :mod:`floodpulse.storage`, so the records written here are
  identical to the M0 in-memory records (same ``pk``/``sk``/``ttl`` conventions).
* only ``PutItem``, ``GetItem`` and ``Query`` on the single ``floodpulse`` table are
  used: no scans, no GSIs, no extra tables.
* ``Query`` pagination is capped by ``MAX_QUERY_PAGES`` so a request can never loop
  unbounded; the cap is reported through the returned slice.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .config import ZONE_IDS
from .storage import (
    HISTORY_PREFIX,
    LATEST_SORT_KEY,
    build_history_item,
    build_latest_item,
    build_zone_pk,
)

MAX_QUERY_PAGES = 10
MAX_HISTORY_KEY_ATTEMPTS = 5


# --------------------------------------------------------------------------- #
# Pure helpers (unit-testable without AWS)
# --------------------------------------------------------------------------- #


def iso_utc_from_epoch(epoch: int) -> str:
    """ISO-8601 UTC second-resolution string, matching the format used server-side."""
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def history_sk(timestamp: str) -> str:
    """Sort key for a history record (lexicographic order == chronological order)."""
    return f"{HISTORY_PREFIX}{timestamp}"


def latest_key(zone_id: str) -> dict[str, str]:
    """Primary key of the zone's LATEST record."""
    return {"pk": build_zone_pk(zone_id), "sk": LATEST_SORT_KEY}


def latest_get_params(zone_id: str) -> dict[str, Any]:
    """``get_item`` parameters for one zone's LATEST record."""
    return {"Key": latest_key(zone_id), "ConsistentRead": True}


def history_query_params(
    zone_id: str,
    since_epoch: int | None = None,
    until_epoch: int | None = None,
    ascending: bool = True,
    limit: int | None = None,
) -> dict[str, Any]:
    """``query`` parameters for one zone's history records.

    Ordering is by ``sk`` (``H#<iso timestamp>``), so the sort key order equals
    chronological order; ``ts_epoch`` filters are applied as a ``FilterExpression``
    because ``sk`` embeds the timestamp as text.
    """
    params: dict[str, Any] = {
        "KeyConditionExpression": "pk = :pk AND begins_with(sk, :prefix)",
        "ExpressionAttributeValues": {":pk": build_zone_pk(zone_id), ":prefix": HISTORY_PREFIX},
        "ScanIndexForward": bool(ascending),
    }
    filters: list[str] = []
    if since_epoch is not None:
        filters.append("ts_epoch >= :since")
        params["ExpressionAttributeValues"][":since"] = int(since_epoch)
    if until_epoch is not None:
        filters.append("ts_epoch <= :until")
        params["ExpressionAttributeValues"][":until"] = int(until_epoch)
    if filters:
        params["FilterExpression"] = " AND ".join(filters)
    if limit is not None:
        params["Limit"] = int(limit)
    return params


def history_count_params(zone_id: str) -> dict[str, Any]:
    """``query`` parameters that only count a zone's history records."""
    return {
        "KeyConditionExpression": "pk = :pk AND begins_with(sk, :prefix)",
        "ExpressionAttributeValues": {":pk": build_zone_pk(zone_id), ":prefix": HISTORY_PREFIX},
        "Select": "COUNT",
    }


def to_storable(value: Any) -> Any:
    """Convert Python floats into ``Decimal`` for writing to DynamoDB.

    boto3's resource serializer raises ``TypeError: Float types are not supported.
    Use Decimal types instead.`` for Python floats, so every float produced by the
    risk engine must be converted before it is written. ``Decimal(str(x))`` keeps the
    round-trip exact for the 4-decimal values this project produces.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, Mapping):
        return {key: to_storable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_storable(item) for item in value]
    return value


def plain(value: Any) -> Any:
    """Convert DynamoDB's ``Decimal`` values back into JSON-serialisable numbers.

    boto3 returns every number as ``Decimal``; the handler JSON-encodes responses,
    so integral values become ``int`` and everything else becomes ``float``. Strings,
    booleans, ``None`` and containers are preserved (recursively for containers).
    """
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, Mapping):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


def is_conditional_check_failure(exc: BaseException) -> bool:
    """True for DynamoDB's ``ConditionalCheckFailedException``.

    Detected by error code, then by class name, so this module never has to import
    ``botocore`` (keeping the local test suite dependency-free).
    """
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        code = (response.get("Error") or {}).get("Code")
        if code:
            return str(code) == "ConditionalCheckFailedException"
    return exc.__class__.__name__ == "ConditionalCheckFailedException"


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #


class DynamoDbStore:
    """DynamoDB implementation of the M0 store interface."""

    def __init__(
        self,
        table_name: str = "floodpulse",
        zone_ids: Iterable[str] | None = None,
        table: Any = None,
    ) -> None:
        self.table_name = table_name
        self.zone_ids: tuple[str, ...] = tuple(zone_ids or ZONE_IDS)
        if table is None:
            import boto3  # lazy: local runs must not require boto3

            table = boto3.resource("dynamodb").Table(table_name)
        self._table = table

    # -- writes ------------------------------------------------------------ #

    def put_reading(
        self,
        reading: Mapping[str, Any],
        risk: Mapping[str, Any],
        hysteresis: Mapping[str, Any],
        smoothed_readings: Mapping[str, float],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Write the zone's LATEST record and append one history record."""
        zone_id = str(reading["zone_id"])
        previous = self.get_latest(zone_id)
        sequence = int(previous["sequence"]) + 1 if previous else 1

        latest = build_latest_item(reading, risk, hysteresis, smoothed_readings, sequence)
        history = build_history_item(reading, risk, hysteresis, smoothed_readings)
        history["sk"] = self._put_history_unique(history)

        # stored copies are Decimal-normalised; the returned dicts keep the plain
        # float values so the API response is identical to the M0 in-memory store.
        self._table.put_item(Item=to_storable(deepcopy(latest)))
        return latest, history

    def _put_history_unique(self, history: Mapping[str, Any]) -> str:
        """Put the history record, disambiguating a duplicate timestamp.

        Uses a conditional write so two readings in the same second can never
        overwrite each other (``H#<ts>`` is the natural key).
        """
        base_sk = str(history["sk"])
        for attempt in range(1, MAX_HISTORY_KEY_ATTEMPTS + 1):
            item = to_storable(deepcopy(dict(history)))
            item["sk"] = base_sk if attempt == 1 else f"{base_sk}#{attempt}"
            try:
                self._table.put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
                return str(item["sk"])
            except Exception as exc:  # noqa: BLE001 - narrowed immediately below
                if not is_conditional_check_failure(exc):
                    raise
        raise RuntimeError(
            f"could not find a free history sort key for {base_sk} after {MAX_HISTORY_KEY_ATTEMPTS} attempts"
        )

    # -- reads ------------------------------------------------------------- #

    def get_latest(self, zone_id: str) -> dict[str, Any] | None:
        """Newest scored reading for a zone, or ``None``."""
        response = self._table.get_item(**latest_get_params(zone_id))
        item = response.get("Item")
        return plain(item) if item else None

    def all_latest(self) -> list[dict[str, Any]]:
        """Every zone's LATEST record, ordered by ``zone_id``."""
        items = [item for item in (self.get_latest(zone) for zone in self.zone_ids) if item]
        return sorted(items, key=lambda item: item["zone_id"])

    def get_history(
        self,
        zone_id: str,
        since_epoch: int = 0,
        until_epoch: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """History records ascending by time; ``limit`` keeps the newest ``limit``."""
        newest_first = limit is not None
        params = history_query_params(
            zone_id,
            since_epoch=int(since_epoch) or None,
            until_epoch=until_epoch,
            ascending=not newest_first,
            limit=int(limit) if newest_first else None,
        )
        items = [plain(item) for item in self._query(params)]
        if newest_first:
            items.reverse()
            return items[-int(limit) :]
        return items

    def recent_readings(self, zone_id: str, count: int) -> list[dict[str, Any]]:
        """The ``count`` most recent history records, oldest first."""
        if count <= 0:
            return []
        return self.get_history(zone_id, limit=count)

    def history_count(self, zone_id: str) -> int:
        """Number of history records stored for a zone."""
        total = 0
        params = history_count_params(zone_id)
        for _page in range(MAX_QUERY_PAGES):
            response = self._table.query(**params)
            total += int(response.get("Count") or 0)
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            params = dict(params, ExclusiveStartKey=last_key)
        return total

    def item_count(self) -> int:
        """Total records stored for the FloodPulse zones (LATEST + history)."""
        latest = sum(1 for zone in self.zone_ids if self.get_latest(zone))
        return latest + sum(self.history_count(zone) for zone in self.zone_ids)

    def _query(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Run a query, following ``LastEvaluatedKey`` up to ``MAX_QUERY_PAGES``.

        A query that carries ``Limit`` reads a single page: the sort-key order already
        selects the wanted end of the range (e.g. the newest N history records).
        """
        items: list[dict[str, Any]] = []
        current = dict(params)
        pages = 1 if "Limit" in params else MAX_QUERY_PAGES
        for _page in range(pages):
            response = self._table.query(**current)
            items.extend(response.get("Items") or [])
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            current = dict(params, ExclusiveStartKey=last_key)
        return items

    def reset(self) -> None:
        """Not supported: this store never deletes stored readings.

        Deleting data must be an explicit, instructed operator action rather than
        something a helper method can trigger.
        """
        raise NotImplementedError("DynamoDbStore does not delete items")

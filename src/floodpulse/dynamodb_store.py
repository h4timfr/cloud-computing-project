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

from .config import ALERT_MARKER_SORT_KEY, INCIDENT_PREFIX, WEATHER_CACHE_SORT_KEY, ZONE_IDS
from .storage import (
    HISTORY_PREFIX,
    LATEST_SORT_KEY,
    READING_DUPLICATE,
    READING_STALE,
    READING_STORED,
    SubscribeThrottleUnavailable,
    WriteOutcome,
    _counter_attempts,
    build_history_item,
    build_latest_item,
    build_zone_pk,
    classify_reading_write,
    history_suffix_key,
    is_replay_of,
    latest_is_behind,
    history_sort_key_for,
    sort_incident_items,
)

MAX_QUERY_PAGES = 10
#: How many same-second sort keys one history write may try before giving up. Each attempt is
#: a conditional PutItem, so the loop is a few round trips in a case that should not happen
#: (live ingest is one reading per city per refresh interval). It is 24 rather than 5 because
#: exhausting it raises, and the reading - already validated and scored - would be lost: five
#: same-second observations from a client is not an abuse scenario worth failing over.
MAX_HISTORY_KEY_ATTEMPTS = 24


# --------------------------------------------------------------------------- #
# Pure helpers (unit-testable without AWS)
# --------------------------------------------------------------------------- #


def iso_utc_from_epoch(epoch: int) -> str:
    """ISO-8601 UTC second-resolution string, matching the format used server-side."""
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def history_sk(timestamp: str) -> str:
    """Sort key for a history record (lexicographic order == chronological order)."""
    return history_sort_key_for(timestamp)


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


# --------------------------------------------------------------------------- #
# Incident parameters (M3)
# --------------------------------------------------------------------------- #


def incident_query_params(zone_id: str, limit: int | None = None) -> dict[str, Any]:
    """``query`` parameters for a zone's incidents, newest first.

    The sort key (``INCIDENT#<iso timestamp>``) sorts chronologically, so this is a
    key-condition query: no scan, no filter, no GSI.
    """
    params: dict[str, Any] = {
        "KeyConditionExpression": "pk = :pk AND begins_with(sk, :prefix)",
        "ExpressionAttributeValues": {":pk": build_zone_pk(zone_id), ":prefix": INCIDENT_PREFIX},
        "ScanIndexForward": False,
    }
    if limit is not None:
        params["Limit"] = int(limit)
    return params


def incident_count_params(zone_id: str) -> dict[str, Any]:
    """``query`` parameters that only count a zone's incidents."""
    return {
        "KeyConditionExpression": "pk = :pk AND begins_with(sk, :prefix)",
        "ExpressionAttributeValues": {":pk": build_zone_pk(zone_id), ":prefix": INCIDENT_PREFIX},
        "Select": "COUNT",
    }


def alert_marker_key(zone_id: str) -> dict[str, str]:
    """Primary key of a zone's alert cooldown marker."""
    return {"pk": build_zone_pk(zone_id), "sk": ALERT_MARKER_SORT_KEY}


def alert_marker_get_params(zone_id: str) -> dict[str, Any]:
    """``get_item`` parameters for one zone's alert cooldown marker."""
    return {"Key": alert_marker_key(zone_id), "ConsistentRead": True}


def weather_cache_key(zone_id: str) -> dict[str, str]:
    """Primary key of a zone's cached external-weather snapshot (M4)."""
    return {"pk": build_zone_pk(zone_id), "sk": WEATHER_CACHE_SORT_KEY}


def weather_cache_get_params(zone_id: str) -> dict[str, Any]:
    """``get_item`` parameters for one zone's weather cache.

    Consistent reads are used deliberately: the cache decides whether an external call is
    allowed at all, so a stale replica must never cause a second fetch inside the window.
    """
    return {"Key": weather_cache_key(zone_id), "ConsistentRead": True}


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
    ) -> tuple[dict[str, Any], dict[str, Any] | None, WriteOutcome]:
        """Write the zone's LATEST record and append one history record.

        Two invariants are enforced by DynamoDB itself, not by the caller:

        * ``LATEST`` is only replaced when the incoming reading is not older than the stored
          one, so a replay, a retry that overtook a newer reading, or a provider hiccup can
          never roll a zone's current state backwards;
        * a history record is only written for an observation that is not already stored
          verbatim, so a client retry cannot inflate history (``H#<ts>`` stays a unique key).

        Returns what actually happened so the API can report it instead of guessing.
        """
        zone_id = str(reading["zone_id"])
        previous = self.get_latest(zone_id)
        outcome_name, reason = classify_reading_write(previous, reading)
        incoming_ts = int(reading["ts_epoch"])
        stored_ts = int((previous or {}).get("ts_epoch") or 0) or None

        candidate = build_history_item(reading, risk, hysteresis, smoothed_readings)
        history_sk, stored = self._put_history_unique(candidate, reading)
        history = dict(candidate, sk=history_sk)

        if not stored:
            # This exact observation is already stored: do not append a copy, and do not
            # touch LATEST - unless LATEST is *behind* it, which means the previous attempt
            # appended the history record and then ended before replacing LATEST.
            if latest_is_behind(stored_ts, incoming_ts):
                sequence = int((previous or {}).get("sequence") or 0) + 1
                latest = build_latest_item(reading, risk, hysteresis, smoothed_readings, sequence)
                if self._put_latest_unless_newer(latest, incoming_ts):
                    return (
                        latest,
                        None,
                        WriteOutcome(
                            outcome=READING_STORED,
                            latest_updated=True,
                            history_written=False,
                            history_sk=history_sk,
                            reason="current state was behind this reading; restored from the stored record",
                            stored_ts_epoch=incoming_ts,
                            incoming_ts_epoch=incoming_ts,
                        ),
                    )
            # The retry carries no new information.
            return (
                dict(previous or {}),
                None,
                WriteOutcome(
                    outcome=READING_DUPLICATE,
                    latest_updated=False,
                    history_written=False,
                    history_sk=history_sk,
                    reason="this reading is already stored",
                    stored_ts_epoch=stored_ts or incoming_ts,
                    incoming_ts_epoch=incoming_ts,
                ),
            )

        if outcome_name == READING_STALE:
            # A stale arrival is still a real observation: it is kept as history but never
            # becomes current state.
            return (
                dict(previous or {}),
                history,
                WriteOutcome(
                    outcome=READING_STALE,
                    latest_updated=False,
                    history_written=True,
                    history_sk=history_sk,
                    reason=reason,
                    stored_ts_epoch=stored_ts,
                    incoming_ts_epoch=incoming_ts,
                ),
            )

        sequence = int((previous or {}).get("sequence") or 0) + 1
        latest = build_latest_item(reading, risk, hysteresis, smoothed_readings, sequence)
        # stored copies are Decimal-normalised; the returned dicts keep the plain
        # float values so the API response is identical to the M0 in-memory store.
        claimed = self._put_latest_unless_newer(latest, incoming_ts)
        if not claimed:
            # Another invocation stored a newer reading between our read and our write. The
            # history record above stands; current state is the newer reading, and we report
            # that honestly rather than claiming we moved it.
            current = self.get_latest(zone_id) or previous or {}
            return (
                dict(current),
                history,
                WriteOutcome(
                    outcome=READING_STALE,
                    latest_updated=False,
                    history_written=True,
                    history_sk=history_sk,
                    reason="a newer reading was stored concurrently; this one kept as history",
                    stored_ts_epoch=int(current.get("ts_epoch") or 0) or None,
                    incoming_ts_epoch=incoming_ts,
                ),
            )
        return (
            latest,
            history,
            WriteOutcome(
                outcome=READING_STORED,
                latest_updated=True,
                history_written=True,
                history_sk=history_sk,
                reason=reason,
                stored_ts_epoch=incoming_ts,
                incoming_ts_epoch=incoming_ts,
            ),
        )

    def _put_latest_unless_newer(self, latest: Mapping[str, Any], incoming_ts: int) -> bool:
        """Put ``LATEST`` only when it is not older than what is already stored.

        ``attribute_not_exists(sk)`` covers the first reading for a zone; the timestamp
        comparison covers every later one. Returns ``False`` when DynamoDB refused the write
        because a newer reading won the race.
        """
        try:
            self._table.put_item(
                Item=to_storable(deepcopy(dict(latest))),
                ConditionExpression="attribute_not_exists(sk) OR ts_epoch <= :ts",
                ExpressionAttributeValues={":ts": int(incoming_ts)},
            )
            return True
        except Exception as exc:  # noqa: BLE001 - narrowed immediately below
            if is_conditional_check_failure(exc):
                return False
            raise

    def _read_history(self, sk: str, zone_id: str) -> dict[str, Any] | None:
        """Read one history record by sort key (only used when that key already exists)."""
        try:
            response = self._table.get_item(
                Key={"pk": build_zone_pk(zone_id), "sk": sk}, ConsistentRead=True
            )
        except Exception:  # noqa: BLE001 - a failed probe must never fail ingestion
            return None
        item = response.get("Item")
        return plain(item) if item else None

    def _put_history_unique(
        self, history: Mapping[str, Any], reading: Mapping[str, Any]
    ) -> tuple[str, bool]:
        """Put the history record, disambiguating a duplicate timestamp.

        Uses a conditional write so two readings in the same second can never overwrite each
        other (``H#<ts>`` is the natural key). When the base key is taken, the stored record
        is read once: if it is the same observation then nothing is written and ``False`` is
        returned, which is what stops a client retry from inflating history. Any other
        collision (a genuinely different reading in the same second) keeps the ``#2``, ``#3``
        suffix behaviour.
        """
        base_sk = str(history["sk"])
        for attempt in range(1, MAX_HISTORY_KEY_ATTEMPTS + 1):
            item = to_storable(deepcopy(dict(history)))
            item["sk"] = base_sk if attempt == 1 else history_suffix_key(base_sk, attempt)
            try:
                self._table.put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
                return str(item["sk"]), True
            except Exception as exc:  # noqa: BLE001 - narrowed immediately below
                if not is_conditional_check_failure(exc):
                    raise
                if attempt == 1 and is_replay_of(
                    self._read_history(base_sk, str(reading["zone_id"])), reading
                ):
                    return base_sk, False
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
        """Total records stored for the FloodPulse zones (latest + history + incidents +
        markers + weather cache)."""
        latest = sum(1 for zone in self.zone_ids if self.get_latest(zone))
        markers = sum(1 for zone in self.zone_ids if self.get_alert_marker(zone))
        caches = sum(1 for zone in self.zone_ids if self.get_weather_cache(zone))
        return (
            latest
            + markers
            + caches
            + sum(self.history_count(zone) for zone in self.zone_ids)
            + self.incident_count()
        )

    # -- incidents (M3) ----------------------------------------------------- #

    def create_incident(self, item: Mapping[str, Any]) -> bool:
        """Insert an incident only if the record does not exist yet.

        The conditional write is what makes "one incident per event" safe even if two
        requests are processed concurrently: the loser sees ``False`` and treats the
        existing incident as the active one instead of opening a duplicate.
        """
        try:
            self._table.put_item(
                Item=to_storable(deepcopy(dict(item))),
                ConditionExpression="attribute_not_exists(pk)",
            )
            return True
        except Exception as exc:  # noqa: BLE001 - narrowed immediately below
            if is_conditional_check_failure(exc):
                return False
            raise

    def put_incident(self, item: Mapping[str, Any]) -> dict[str, Any]:
        """Insert or update an incident record (used for escalate/observe/close)."""
        self._table.put_item(Item=to_storable(deepcopy(dict(item))))
        return dict(item)

    def latest_incident(self, zone_id: str) -> dict[str, Any] | None:
        """The zone's newest incident record (open or closed), or ``None``."""
        items = self._query(incident_query_params(zone_id, limit=1))
        return plain(items[0]) if items else None

    def list_incidents(self, zone_id: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        """Incidents newest first, optionally for one zone and capped at ``limit``."""
        zones = [zone_id] if zone_id is not None else list(self.zone_ids)
        collected: list[dict[str, Any]] = []
        for zone in zones:
            params = incident_query_params(zone, limit=int(limit) if limit is not None else None)
            collected.extend(plain(item) for item in self._query(params))
        ordered = sort_incident_items(collected)
        return ordered if limit is None else ordered[: int(limit)]

    def incident_count(self, zone_id: str | None = None) -> int:
        """Number of stored incident records."""
        zones = [zone_id] if zone_id is not None else list(self.zone_ids)
        total = 0
        for zone in zones:
            params = incident_count_params(zone)
            for _page in range(MAX_QUERY_PAGES):
                response = self._table.query(**params)
                total += int(response.get("Count") or 0)
                last_key = response.get("LastEvaluatedKey")
                if not last_key:
                    break
                params = dict(params, ExclusiveStartKey=last_key)
        return total

    def get_alert_marker(self, zone_id: str) -> dict[str, Any] | None:
        """The zone's alert cooldown marker, or ``None``."""
        response = self._table.get_item(**alert_marker_get_params(zone_id))
        item = response.get("Item")
        return plain(item) if item else None

    def put_alert_marker(self, item: Mapping[str, Any]) -> dict[str, Any]:
        """Store the zone's alert cooldown marker."""
        self._table.put_item(Item=to_storable(deepcopy(dict(item))))
        return dict(item)

    def claim_alert_slot(
        self,
        item: Mapping[str, Any],
        *,
        severity: str,
        now_epoch: float,
        cooldown_seconds: int,
    ) -> bool:
        """Write the alert marker only if this caller is inside its right to alert.

        The cooldown check and the marker write are one conditional DynamoDB write, so two
        invocations escalating the same zone at the same moment cannot both publish: the
        loser's condition fails on the winner's marker. A zero cooldown means "alert every
        time" by configuration, so the write is unconditional.
        """
        if cooldown_seconds <= 0:
            self.put_alert_marker(item)
            return True

        cutoff = int(now_epoch) - int(cooldown_seconds)
        condition = (
            "attribute_not_exists(pk)"
            " OR attribute_not_exists(last_alert_by_severity.#sev)"
            " OR last_alert_by_severity.#sev.#ts <= :cutoff"
        )
        try:
            self._table.put_item(
                Item=to_storable(deepcopy(dict(item))),
                ConditionExpression=condition,
                ExpressionAttributeNames={"#sev": str(severity), "#ts": "ts_epoch"},
                ExpressionAttributeValues={":cutoff": int(cutoff)},
            )
            return True
        except Exception as exc:  # noqa: BLE001 - narrowed immediately below
            if is_conditional_check_failure(exc):
                return False
            raise

    def get_weather_cache(self, zone_id: str) -> dict[str, Any] | None:
        """The zone's cached external-weather snapshot, or ``None`` (M4)."""
        response = self._table.get_item(**weather_cache_get_params(zone_id))
        item = response.get("Item")
        return plain(item) if item else None

    def put_weather_cache(self, item: Mapping[str, Any]) -> dict[str, Any]:
        """Store (overwrite) the zone's cached external-weather snapshot (M4)."""
        self._table.put_item(Item=to_storable(deepcopy(dict(item))))
        return dict(item)

    def claim_subscribe_attempt(
        self,
        key: Mapping[str, str],
        *,
        limit: int,
        ttl: int,
        now_epoch: float,
    ) -> int | None:
        """Count one subscription attempt, refusing it once the window's limit is reached.

        One conditional ``UpdateItem``: the counter is incremented only while it is below
        ``limit``, so simultaneous requests cannot both take the last slot - the loser's
        condition fails on the winner's counter. Returns the new count, or ``None`` when
        the window is full.

        The item is ``{pk: SUBRATE#<digest>, sk: WINDOW#<bucket>, attempts, ttl}``: no
        address, no partial address, nothing to read back except a number. ``ttl`` is set
        once, when the counter is created, so the item disappears after the window it
        counts.
        """
        try:
            response = self._table.update_item(
                Key={"pk": str(key["pk"]), "sk": str(key["sk"])},
                UpdateExpression=(
                    "ADD attempts :one SET #ttl = if_not_exists(#ttl, :ttl)"
                ),
                ConditionExpression="attribute_not_exists(attempts) OR attempts < :limit",
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={
                    ":one": 1,
                    ":limit": int(limit),
                    ":ttl": int(ttl),
                },
                ReturnValues="UPDATED_NEW",
            )
        except Exception as exc:  # noqa: BLE001 - narrowed immediately below
            if is_conditional_check_failure(exc):
                return None
            # Anything else - throttling, a validation failure caused by a counter item
            # whose ``attempts`` attribute is not a number, a transient error - is reported
            # as "the request could not be completed" by the route, never as a 500.
            raise SubscribeThrottleUnavailable(type(exc).__name__) from exc
        try:
            return _counter_attempts({"attempts": response.get("Attributes", {}).get("attempts")})
        except SubscribeThrottleUnavailable:
            return None

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

"""Offline tests for the DynamoDB store (M1).

No AWS calls and no boto3: ``FakeTable`` mimics the three boto3 ``Table`` methods the
store uses (``put_item``/``get_item``/``query``) and, importantly, round-trips every
number through ``Decimal`` exactly as DynamoDB does — that is what makes the
``plain()`` normalisation (and therefore JSON serialisation) a real test.
"""

from __future__ import annotations

import json
import unittest
from copy import deepcopy
from decimal import Decimal

import floodpulse.dynamodb_store as ddb_module
from floodpulse.config import HISTORY_TTL_SECONDS, METRIC_ORDER, thresholds_for
from floodpulse.dynamodb_store import (
    DynamoDbStore,
    history_count_params,
    history_query_params,
    history_sk,
    is_conditional_check_failure,
    iso_utc_from_epoch,
    latest_get_params,
    latest_key,
    plain,
    to_storable,
)
from floodpulse.risk import apply_hysteresis, score_zone
from floodpulse.storage import LATEST_SORT_KEY, LocalStore, build_zone_pk

ZONE = "zone-riverside-north"
OTHER_ZONE = "zone-harbor-east"
BASE_EPOCH = 1_771_064_100


class ConditionalCheckFailedException(Exception):
    """Same class name as DynamoDB's error, so the store detects it by name."""


def to_dynamo(value):
    """Convert python values the way DynamoDB returns them (numbers -> Decimal)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: to_dynamo(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_dynamo(item) for item in value]
    return value


def reject_floats(value, path="Item"):
    """Mirror boto3's resource serializer, which refuses Python floats.

    Without this the fake table would happily accept floats and the real
    ``TypeError: Float types are not supported`` would only appear in AWS.
    """
    if isinstance(value, float):
        raise TypeError(f"Float types are not supported. Use Decimal types instead. ({path})")
    if isinstance(value, dict):
        for key, item in value.items():
            reject_floats(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            reject_floats(item, f"{path}[{index}]")


def assert_no_floats(case: unittest.TestCase, value, path="item"):
    if isinstance(value, float):
        case.fail(f"stored value at {path} is a Python float: {value!r}")
    if isinstance(value, dict):
        for key, item in value.items():
            assert_no_floats(case, item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            assert_no_floats(case, item, f"{path}[{index}]")


class FakeTable:
    """Minimal stand-in for ``boto3.resource("dynamodb").Table(name)``."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict] = {}
        self.calls: list[str] = []

    def put_item(self, Item, ConditionExpression=None):
        self.calls.append("put_item")
        reject_floats(Item)  # same restriction as boto3's serializer
        key = (Item["pk"], Item["sk"])
        if ConditionExpression == "attribute_not_exists(pk)" and key in self.items:
            raise ConditionalCheckFailedException("The conditional request failed")
        self.items[key] = to_dynamo(deepcopy(Item))
        return {}

    def get_item(self, Key, ConsistentRead=False):
        self.calls.append("get_item")
        item = self.items.get((Key["pk"], Key["sk"]))
        return {"Item": deepcopy(item)} if item else {}

    def query(self, **params):
        self.calls.append("query")
        values = params["ExpressionAttributeValues"]
        pk, prefix = values[":pk"], values[":prefix"]
        rows = [item for (item_pk, sk), item in self.items.items() if item_pk == pk and sk.startswith(prefix)]
        rows.sort(key=lambda item: item["sk"], reverse=not params.get("ScanIndexForward", True))

        if params.get("FilterExpression"):
            if ":since" in values:
                rows = [row for row in rows if row["ts_epoch"] >= values[":since"]]
            if ":until" in values:
                rows = [row for row in rows if row["ts_epoch"] <= values[":until"]]

        if params.get("Select") == "COUNT":
            return {"Count": len(rows)}

        limit = params.get("Limit")
        page = rows if limit is None else rows[:limit]
        response = {"Items": [deepcopy(row) for row in page]}
        if limit is not None and len(rows) > limit:
            response["LastEvaluatedKey"] = {"pk": pk, "sk": page[-1]["sk"]}
        return response


def make_reading(zone_id: str = ZONE, ts_epoch: int = BASE_EPOCH, rain: float = 2.0, water: float = 1.0) -> dict:
    return {
        "schema_version": "1.0",
        "simulated": True,
        "data_classification": "simulated",
        "zone_id": zone_id,
        "sensor_node_id": f"sim-{zone_id[5:]}",
        "timestamp": iso_utc_from_epoch(ts_epoch),
        "ts_epoch": ts_epoch,
        "ttl": ts_epoch + HISTORY_TTL_SECONDS,
        "readings": dict(
            zip(
                METRIC_ORDER,
                (
                    rain,
                    0.5,
                    40.0,
                    water,
                    1.5,
                ),
            )
        ),
        "meta": {"run_id": "test-run", "seq": 1, "phase": "calm"},
        "ingested_at_epoch": ts_epoch,
    }


def scored(reading: dict, previous: dict | None = None) -> tuple[dict, dict, dict]:
    """Score a reading the same way the handler does, returning (risk, hysteresis, smoothed)."""
    risk = score_zone(reading["readings"], thresholds_for(str(reading["zone_id"])))
    hysteresis = apply_hysteresis(risk["risk_class_candidate"], previous)
    return risk, hysteresis, dict(reading["readings"])


def put(store, reading: dict, previous: dict | None = None):
    risk, hysteresis, smoothed = scored(reading, previous)
    return store.put_reading(reading, risk, hysteresis, smoothed)


class PureHelperTests(unittest.TestCase):
    def test_iso_utc_from_epoch(self) -> None:
        self.assertEqual(iso_utc_from_epoch(0), "1970-01-01T00:00:00Z")
        self.assertEqual(iso_utc_from_epoch(BASE_EPOCH), "2026-02-14T10:15:00Z")

    def test_history_sk_is_chronologically_ordered(self) -> None:
        first, second = history_sk(iso_utc_from_epoch(BASE_EPOCH)), history_sk(iso_utc_from_epoch(BASE_EPOCH + 5))
        self.assertEqual(first, "H#2026-02-14T10:15:00Z")
        self.assertLess(first, second)

    def test_latest_key(self) -> None:
        self.assertEqual(latest_key(ZONE), {"pk": "ZONE#zone-riverside-north", "sk": LATEST_SORT_KEY})
        self.assertEqual(latest_get_params(ZONE)["Key"], latest_key(ZONE))
        self.assertTrue(latest_get_params(ZONE)["ConsistentRead"])

    def test_history_query_params_defaults(self) -> None:
        params = history_query_params(ZONE)
        self.assertEqual(params["KeyConditionExpression"], "pk = :pk AND begins_with(sk, :prefix)")
        self.assertEqual(params["ExpressionAttributeValues"], {":pk": build_zone_pk(ZONE), ":prefix": "H#"})
        self.assertTrue(params["ScanIndexForward"])
        self.assertNotIn("FilterExpression", params)
        self.assertNotIn("Limit", params)

    def test_history_query_params_filters(self) -> None:
        params = history_query_params(ZONE, since_epoch=100, until_epoch=200, ascending=False, limit=5)
        self.assertEqual(params["FilterExpression"], "ts_epoch >= :since AND ts_epoch <= :until")
        self.assertEqual(params["ExpressionAttributeValues"][":since"], 100)
        self.assertEqual(params["ExpressionAttributeValues"][":until"], 200)
        self.assertFalse(params["ScanIndexForward"])
        self.assertEqual(params["Limit"], 5)

    def test_history_count_params(self) -> None:
        params = history_count_params(ZONE)
        self.assertEqual(params["Select"], "COUNT")
        self.assertEqual(params["ExpressionAttributeValues"][":pk"], build_zone_pk(ZONE))

    def test_plain_converts_decimals(self) -> None:
        raw = {
            "ts_epoch": Decimal("1771064100"),
            "risk_score": Decimal("0.65"),
            "nested": [{"ttl": Decimal("1771668900")}],
            "simulated": True,
            "flag": None,
            "label": "x",
        }
        cleaned = plain(raw)
        self.assertIsInstance(cleaned["ts_epoch"], int)
        self.assertIsInstance(cleaned["risk_score"], float)
        self.assertEqual(cleaned["risk_score"], 0.65)
        self.assertIsInstance(cleaned["nested"][0]["ttl"], int)
        self.assertIs(cleaned["simulated"], True)
        self.assertIsNone(cleaned["flag"])
        json.dumps(cleaned)  # must be JSON serialisable

    def test_conditional_check_detection(self) -> None:
        self.assertTrue(is_conditional_check_failure(ConditionalCheckFailedException("x")))
        wrapped = type("ClientError", (Exception,), {})
        error = wrapped("boom")
        error.response = {"Error": {"Code": "ConditionalCheckFailedException"}}
        self.assertTrue(is_conditional_check_failure(error))
        other = wrapped("boom")
        other.response = {"Error": {"Code": "ResourceNotFoundException"}}
        self.assertFalse(is_conditional_check_failure(other))
        self.assertFalse(is_conditional_check_failure(ValueError("nope")))

    def test_boto3_is_not_imported_at_module_level(self) -> None:
        self.assertFalse(hasattr(ddb_module, "boto3"))


class DynamoDbStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.table = FakeTable()
        self.store = DynamoDbStore("floodpulse", table=self.table)

    def test_put_reading_writes_latest_and_history(self) -> None:
        latest, history = put(self.store, make_reading())
        self.assertEqual(latest["sk"], LATEST_SORT_KEY)
        self.assertEqual(latest["sequence"], 1)
        self.assertEqual(history["sk"], "H#2026-02-14T10:15:00Z")
        self.assertEqual(len(self.table.items), 2)
        self.assertEqual(self.store.history_count(ZONE), 1)

    def test_sequence_increments_from_the_stored_latest(self) -> None:
        put(self.store, make_reading(ts_epoch=BASE_EPOCH))
        _latest, _history = put(self.store, make_reading(ts_epoch=BASE_EPOCH + 5))
        latest, _history = put(self.store, make_reading(ts_epoch=BASE_EPOCH + 10))
        self.assertEqual(latest["sequence"], 3)

    def test_duplicate_timestamp_gets_a_suffixed_sort_key(self) -> None:
        _latest, first = put(self.store, make_reading(ts_epoch=BASE_EPOCH))
        _latest, second = put(self.store, make_reading(ts_epoch=BASE_EPOCH, rain=9.0))
        self.assertEqual(first["sk"], "H#2026-02-14T10:15:00Z")
        self.assertEqual(second["sk"], "H#2026-02-14T10:15:00Z#2")
        self.assertEqual(self.store.history_count(ZONE), 2)

    def test_get_latest_returns_plain_json_serialisable_values(self) -> None:
        put(self.store, make_reading())
        latest = self.store.get_latest(ZONE)
        self.assertIsNotNone(latest)
        self.assertIsInstance(latest["ts_epoch"], int)
        self.assertIsInstance(latest["sequence"], int)
        self.assertIsInstance(latest["risk_score"], float)
        self.assertIsInstance(latest["ttl"], int)
        json.dumps(latest)
        self.assertIsNone(self.store.get_latest("zone-atlantis"))

    def test_history_window_and_ordering(self) -> None:
        for offset in (0, 5, 10, 15):
            put(self.store, make_reading(ts_epoch=BASE_EPOCH + offset))

        ascending = self.store.get_history(ZONE)
        self.assertEqual([item["ts_epoch"] for item in ascending], [BASE_EPOCH + o for o in (0, 5, 10, 15)])

        windowed = self.store.get_history(ZONE, since_epoch=BASE_EPOCH + 5, until_epoch=BASE_EPOCH + 10)
        self.assertEqual([item["ts_epoch"] for item in windowed], [BASE_EPOCH + 5, BASE_EPOCH + 10])

        newest_two = self.store.get_history(ZONE, limit=2)
        self.assertEqual([item["ts_epoch"] for item in newest_two], [BASE_EPOCH + 10, BASE_EPOCH + 15])
        self.assertEqual(self.store.recent_readings(ZONE, 2), newest_two)
        self.assertEqual(self.store.recent_readings(ZONE, 0), [])

    def test_history_is_isolated_per_zone(self) -> None:
        put(self.store, make_reading(zone_id=ZONE))
        put(self.store, make_reading(zone_id=OTHER_ZONE))
        self.assertEqual(self.store.history_count(ZONE), 1)
        self.assertEqual(self.store.history_count(OTHER_ZONE), 1)
        self.assertEqual(len(self.store.get_history(ZONE)), 1)

    def test_all_latest_and_item_count(self) -> None:
        put(self.store, make_reading(zone_id=OTHER_ZONE))
        put(self.store, make_reading(zone_id=ZONE))
        put(self.store, make_reading(zone_id=ZONE, ts_epoch=BASE_EPOCH + 5))
        self.assertEqual([item["zone_id"] for item in self.store.all_latest()], sorted([ZONE, OTHER_ZONE]))
        self.assertEqual(self.store.item_count(), 5)  # 2 LATEST + 3 history

    def test_only_put_get_and_query_are_used(self) -> None:
        put(self.store, make_reading())
        self.store.get_latest(ZONE)
        self.store.get_history(ZONE, limit=1)
        self.store.history_count(ZONE)
        self.store.all_latest()
        self.assertEqual(set(self.table.calls), {"put_item", "get_item", "query"})

    def test_reset_refuses_to_delete(self) -> None:
        with self.assertRaises(NotImplementedError):
            self.store.reset()

    def test_stored_items_contain_no_python_floats(self) -> None:
        """Regression: boto3 refuses Python floats, so writes must use Decimal."""
        put(self.store, make_reading(rain=42.5, water=2.4))
        self.assertTrue(self.table.items)
        for key, item in self.table.items.items():
            assert_no_floats(self, item, f"{key[1]}")

    def test_to_storable_converts_only_floats(self) -> None:
        converted = to_storable(
            {
                "float": 0.65,
                "int": 7,
                "bool": True,
                "none": None,
                "text": "x",
                "list": [1.5, {"inner": 2.5}],
            }
        )
        self.assertEqual(converted["float"], Decimal("0.65"))
        self.assertIsInstance(converted["float"], Decimal)
        self.assertEqual(converted["int"], 7)
        self.assertIsInstance(converted["int"], int)
        self.assertIs(converted["bool"], True)
        self.assertIsNone(converted["none"])
        self.assertEqual(converted["list"][0], Decimal("1.5"))
        self.assertEqual(converted["list"][1]["inner"], Decimal("2.5"))

    def test_float_round_trip_through_storage(self) -> None:
        put(self.store, make_reading(rain=42.5))
        latest = self.store.get_latest(ZONE)
        self.assertEqual(latest["readings"]["rainfall_mm_per_hr"], 42.5)
        self.assertIsInstance(latest["readings"]["rainfall_mm_per_hr"], float)


class LocalVsDynamoEquivalenceTests(unittest.TestCase):
    """The DynamoDB store must behave like the M0 store for the same input."""

    def setUp(self) -> None:
        self.local = LocalStore()
        self.table = FakeTable()
        self.remote = DynamoDbStore("floodpulse", table=self.table)

    def test_same_readings_produce_the_same_records(self) -> None:
        readings = [
            make_reading(ts_epoch=BASE_EPOCH, rain=1.0, water=1.0),
            make_reading(ts_epoch=BASE_EPOCH + 5, rain=30.0, water=2.2),
            make_reading(ts_epoch=BASE_EPOCH + 10, rain=60.0, water=3.9),
            make_reading(ts_epoch=BASE_EPOCH + 15, rain=60.0, water=3.9),
        ]
        local_state = None
        remote_state = None
        for reading in readings:
            local_latest, local_history = put(self.local, reading, local_state)
            remote_latest, remote_history = put(self.remote, reading, remote_state)
            local_state = local_latest["hysteresis"]
            remote_state = remote_latest["hysteresis"]
            with self.subTest(ts=reading["ts_epoch"]):
                self.assertEqual(remote_latest, local_latest)
                self.assertEqual(remote_history, local_history)

        self.assertEqual(self.remote.get_latest(ZONE), self.local.get_latest(ZONE))
        self.assertEqual(self.remote.get_history(ZONE), self.local.get_history(ZONE))
        self.assertEqual(
            [item["ts_epoch"] for item in self.remote.get_history(ZONE, limit=2)],
            [item["ts_epoch"] for item in self.local.get_history(ZONE, limit=2)],
        )
        self.assertEqual(self.remote.history_count(ZONE), self.local.history_count(ZONE))
        self.assertEqual(self.remote.all_latest(), self.local.all_latest())
        self.assertEqual(self.remote.item_count(), self.local.item_count())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

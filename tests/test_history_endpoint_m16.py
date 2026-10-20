"""M16 endpoint audit: what ``GET /zones/{zone_id}/history`` actually guarantees.

The audit items are (a) chronological order, (b) the thinning path never loses the newest
sample - nor the oldest in the window, nor an extreme - (c) deterministic handling of duplicate
timestamps, (d) malformed legacy records skipped rather than raised, (e) no NaN/Infinity in the
JSON body, (f) a clean 404/400 for a bad zone id, (g) minutes bounded to 1..HISTORY_MAX_MINUTES.
Then the window edge matrix (0/1/2/3+ samples, flat/rising/falling/noisy-flat, duplicates,
out-of-order, unusable rows, out-of-range scores, missing rainfall, gaps, staleness and the
band round trip) asserted through the shared implementation in ``floodpulse.temporal``.

Everything runs in-process against an in-memory store: no AWS, no network, no wall clock.
"""

from __future__ import annotations

import json
import logging
import time
import unittest
from datetime import datetime, timezone

import floodpulse.handler as handler_module
from floodpulse import temporal
from floodpulse.config import (
    HISTORY_DEFAULT_MINUTES,
    HISTORY_MAX_MINUTES,
    MAX_HISTORY_POINTS,
    RISK_MODEL_ID,
    ZONE_IDS,
    thresholds_for,
)
from floodpulse.handler import dispatch, lambda_handler, set_store
from floodpulse.risk import classify, score_zone
from floodpulse.storage import HISTORY_PREFIX, LocalStore, build_zone_pk

ZONE = "zone-riverside-north"
OTHER_ZONE = "zone-harbor-east"
LIVE_ZONE = "city-chennai"
BASE_EPOCH = int(datetime(2026, 2, 14, 9, 0, 0, tzinfo=timezone.utc).timestamp())

CALM = {
    "rainfall_mm_per_hr": 2.0,
    "drain_level_m": 0.5,
    "soil_saturation_pct": 40.0,
    "water_level_m": 1.0,
    "flow_rate_m3_s": 1.5,
}


def iso_at(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def store_reading(
    store: LocalStore,
    ts_epoch: int,
    score: float,
    *,
    zone: str = ZONE,
    rainfall: float | None = 0.0,
    readings: dict | None = None,
    risk_class: str | None = None,
) -> dict:
    """Store one reading with a chosen score, through the store's own public API.

    This route tests *stored* history, so these tests store history: the item written here is
    the shape ``POST /readings`` writes (the explanation comes from the real engine, so every
    stored field is a real one), with the score a test chooses. The ingestion pipeline
    correctly refuses to let a caller choose a score, which is why a chosen one has to be
    written from here rather than posted.
    """
    metrics = dict(readings) if readings is not None else dict(CALM)
    if rainfall is not None:
        metrics["rainfall_mm_per_hr"] = rainfall
    scored = dict(CALM)
    if readings is not None:
        scored.update(readings)
    if rainfall is not None:
        scored["rainfall_mm_per_hr"] = rainfall
    reading = {
        "zone_id": zone,
        "sensor_node_id": f"sim-{zone}",
        "timestamp": iso_at(ts_epoch),
        "ts_epoch": ts_epoch,
        "ttl": ts_epoch + 7 * 24 * 60 * 60,
        "simulated": True,
        "data_classification": "simulated",
        "readings": metrics,
        "meta": {},
        "ingested_at_epoch": ts_epoch,
    }
    risk = dict(score_zone(scored, thresholds_for(zone)))
    risk["risk_score"] = float(score)
    risk["risk_class_candidate"] = classify(score)
    hysteresis = {"risk_class": risk_class or classify(score)}
    store.put_reading(reading, risk, hysteresis, metrics)
    return reading


class StubHistoryStore:
    """A store that returns exactly the records a test hands it, corrupt ones included.

    A malformed history record cannot be written by the pipeline (validation and the item
    builder both prevent it), so a legacy or half-written row can only be injected. This
    implements only what the history route touches.
    """

    zone_ids: tuple[str, ...] = ()

    def __init__(self, records: list) -> None:
        self.records = list(records)

    def get_history(self, zone_id: str, since_epoch: int = 0, until_epoch=None, limit=None) -> list:
        kept = []
        for record in self.records:
            stamp = temporal.stamp_of(record)
            if stamp is None or stamp >= since_epoch:
                kept.append(record)
        return kept


def legacy_record(stamp: int, score: float, **overrides):
    """One stored history record, in the item shape ``storage.build_history_item`` produces."""
    timestamp = iso_at(stamp)
    record = {
        "pk": build_zone_pk(ZONE),
        "sk": f"{HISTORY_PREFIX}{timestamp}",
        "zone_id": ZONE,
        "timestamp": timestamp,
        "ts_epoch": stamp,
        "risk_score": score,
        "risk_class": classify(score),
        "risk_class_candidate": classify(score),
        "readings": {"rainfall_mm_per_hr": 1.0},
    }
    record.update(overrides)
    return record


class HistoryEndpointTestCase(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.store = LocalStore()
        set_store(self.store)
        logging.disable(logging.NOTSET)

    def tearDown(self) -> None:
        set_store(None)

    def history(self, now_epoch: float, minutes=None, zone: str = ZONE, store=None):
        query = {} if minutes is None else {"minutes": minutes}
        return dispatch("GET", f"/zones/{zone}/history", None, store or self.store, now_epoch, query)


# --------------------------------------------------------------------------- #
# (a) chronological order
# --------------------------------------------------------------------------- #


class ChronologicalOrderTests(HistoryEndpointTestCase):
    def test_points_are_ascending_when_readings_arrive_out_of_order(self) -> None:
        """(a) a late arrival is history, order is the stored timestamps, not arrival order."""
        store_reading(self.store, BASE_EPOCH, 0.20)
        store_reading(self.store, BASE_EPOCH + 1200, 0.40)
        store_reading(self.store, BASE_EPOCH + 600, 0.30)  # arrives late, between the two
        status, body = self.history(BASE_EPOCH + 1300, "1440")
        self.assertEqual(status, 200)
        self.assertEqual(
            [point["ts_epoch"] for point in body["points"]],
            [BASE_EPOCH, BASE_EPOCH + 600, BASE_EPOCH + 1200],
        )
        self.assertEqual([point["risk_score"] for point in body["points"]], [0.20, 0.30, 0.40])

    def test_the_newest_stored_sample_is_the_last_point(self) -> None:
        for offset in (0, 600, 1200):
            store_reading(self.store, BASE_EPOCH + offset, 0.20 + offset / 10000)
        _status, body = self.history(BASE_EPOCH + 1300, "1440")
        self.assertEqual(body["points"][-1]["ts_epoch"], BASE_EPOCH + 1200)
        self.assertEqual(body["point_count"], 3)

    def test_the_window_is_the_stored_window_of_the_named_zone_only(self) -> None:
        store_reading(self.store, BASE_EPOCH, 0.20)
        store_reading(self.store, BASE_EPOCH, 0.90, zone=OTHER_ZONE)
        _status, body = self.history(BASE_EPOCH + 60, "1440", zone=ZONE)
        self.assertEqual(body["point_count"], 1)
        self.assertEqual(body["points"][0]["risk_score"], 0.20)


# --------------------------------------------------------------------------- #
# (b) the thinning path
# --------------------------------------------------------------------------- #


class ThinningTests(HistoryEndpointTestCase):
    def thin(self, scores: list[float], cap: int, now_epoch: int | None = None):
        original = handler_module.MAX_HISTORY_POINTS
        handler_module.MAX_HISTORY_POINTS = cap
        try:
            for index, score in enumerate(scores):
                store_reading(self.store, BASE_EPOCH + index * 60, score)
            return self.history(now_epoch or BASE_EPOCH + (len(scores) - 1) * 60 + 60, "1440")
        finally:
            handler_module.MAX_HISTORY_POINTS = original

    def test_thinning_never_drops_the_newest_sample(self) -> None:
        """(b) the reading the chart's right-hand edge is drawn from survives every cap."""
        scores = [0.20 + index / 100 for index in range(20)]
        newest = BASE_EPOCH + 19 * 60
        for cap in (1, 2, 3, 4, 5, 7, 12, 19):
            with self.subTest(cap=cap):
                self.store = LocalStore()
                set_store(self.store)
                _status, body = self.thin(scores, cap)
                self.assertTrue(body["downsampled"])
                self.assertLessEqual(body["point_count"], cap)
                self.assertEqual(body["points"][-1]["ts_epoch"], newest)
                self.assertEqual(body["stride"], max(1, -(-20 // cap)))

    def test_thinning_keeps_the_oldest_sample_and_the_extremes(self) -> None:
        """(b) a peak or minimum between two kept steps must not vanish from the window."""
        scores = [
            0.20, 0.21, 0.95, 0.30, 0.31, 0.32, 0.33, 0.05,
            0.35, 0.36, 0.37, 0.38, 0.39, 0.40, 0.41, 0.42,
            0.43, 0.44, 0.45, 0.24,
        ]
        _status, body = self.thin(scores, 5)
        self.assertEqual(body["point_count"], 5)
        self.assertTrue(body["downsampled"])
        kept = [point["risk_score"] for point in body["points"]]
        epochs = [point["ts_epoch"] for point in body["points"]]
        self.assertEqual(epochs, sorted(epochs), "a thinned window is not ascending")
        self.assertEqual(epochs[0], BASE_EPOCH, "the oldest sample in the window was dropped")
        self.assertEqual(epochs[-1], BASE_EPOCH + 19 * 60)
        self.assertIn(0.95, kept, "the peak was dropped by thinning")
        self.assertIn(0.05, kept, "the minimum was dropped by thinning")
        self.assertEqual(max(kept), max(scores))
        self.assertEqual(min(kept), min(scores))

    def test_thinning_keeps_first_and_last_when_the_cap_barely_fits_them(self) -> None:
        scores = [0.20 + index / 100 for index in range(20)]
        _status, body = self.thin(scores, 2)
        self.assertEqual(body["point_count"], 2)
        self.assertEqual([point["ts_epoch"] for point in body["points"]], [BASE_EPOCH, BASE_EPOCH + 19 * 60])

    def test_thinning_does_not_touch_a_window_inside_the_cap(self) -> None:
        scores = [0.20 + index / 100 for index in range(5)]
        _status, body = self.thin(scores, MAX_HISTORY_POINTS)
        self.assertFalse(body["downsampled"])
        self.assertEqual(body["stride"], 1)
        self.assertEqual(body["point_count"], 5)

    def test_a_cap_of_zero_does_not_crash_the_window(self) -> None:
        """The cap has no meaning at or below zero; the window is served whole instead."""
        scores = [0.20 + index / 100 for index in range(4)]
        _status, body = self.thin(scores, 0)
        self.assertEqual(body["point_count"], 4)
        self.assertEqual(body["stride"], 1)

    def test_the_response_contract_is_unchanged(self) -> None:
        """The frontend depends on these keys and on a point carrying exactly these fields."""
        store_reading(self.store, BASE_EPOCH, 0.20)
        _status, body = self.history(BASE_EPOCH + 60, "1440")
        self.assertEqual(
            sorted(body),
            ["data_classification", "downsampled", "metric_sources", "minutes", "model", "name",
             "point_count", "points", "simulated", "since_epoch", "stride", "zone_id", "zone_mode"],
        )
        self.assertEqual(
            sorted(body["points"][0]),
            ["readings", "risk_class", "risk_class_candidate", "risk_score", "timestamp", "ts_epoch"],
        )
        self.assertEqual(body["minutes"], 1440)
        self.assertEqual(body["since_epoch"], int(BASE_EPOCH + 60) - 1440 * 60)
        self.assertEqual(body["point_count"], len(body["points"]))
        self.assertEqual(body["model"], RISK_MODEL_ID)
        self.assertTrue(body["simulated"])
        self.assertEqual(body["data_classification"], "simulated")
        self.assertEqual(body["zone_mode"], "simulated")


# --------------------------------------------------------------------------- #
# (c) duplicate timestamps
# --------------------------------------------------------------------------- #


class DuplicateTimestampTests(HistoryEndpointTestCase):
    def test_a_duplicated_timestamp_is_served_in_a_stable_order(self) -> None:
        """(c) both rows are real observations; the later arrival is the newest sample."""
        store_reading(self.store, BASE_EPOCH, 0.20, rainfall=0.0)
        store_reading(self.store, BASE_EPOCH, 0.42, rainfall=1.0)  # same second, later arrival
        first_status, first = self.history(BASE_EPOCH + 60, "1440")
        second_status, second = self.history(BASE_EPOCH + 60, "1440")
        self.assertEqual((first_status, second_status), (200, 200))
        self.assertEqual(first["point_count"], 2)
        self.assertEqual(
            [point["risk_score"] for point in first["points"]], [0.20, 0.42],
            "the duplicate pair is not in arrival order",
        )
        self.assertEqual(first["points"][-1]["risk_score"], 0.42)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))

    def test_a_duplicated_newest_timestamp_survives_thinning(self) -> None:
        original = handler_module.MAX_HISTORY_POINTS
        handler_module.MAX_HISTORY_POINTS = 1
        try:
            store_reading(self.store, BASE_EPOCH, 0.20, rainfall=0.0)
            store_reading(self.store, BASE_EPOCH + 600, 0.30, rainfall=0.5)
            store_reading(self.store, BASE_EPOCH + 600, 0.44, rainfall=1.0)  # later arrival, same second
            _status, body = self.history(BASE_EPOCH + 660, "1440")
        finally:
            handler_module.MAX_HISTORY_POINTS = original
        self.assertEqual(body["point_count"], 1)
        self.assertEqual(body["points"][0]["ts_epoch"], BASE_EPOCH + 600)
        self.assertEqual(body["points"][0]["risk_score"], 0.44, "the earlier row for that second won")


# --------------------------------------------------------------------------- #
# (d) malformed legacy records
# --------------------------------------------------------------------------- #


class MalformedRecordTests(HistoryEndpointTestCase):
    def test_records_that_cannot_be_shown_are_skipped(self) -> None:
        """(d) a record with no usable timestamp or score is dropped, never raised.

        A record that is merely *thin* - an older item shape without the band labels, or one
        without a readings map - is still shown, with the band derived from its own score and
        the readings it actually has. Only a row that cannot be placed in time or scored is
        dropped, because nothing about it can be stated honestly.
        """
        records = [
            legacy_record(BASE_EPOCH, 0.20),
            legacy_record(BASE_EPOCH + 600, 0.30, risk_class_candidate=None),  # pre-M2 shape
            legacy_record(BASE_EPOCH + 1200, 0.35, readings=None),
            legacy_record(BASE_EPOCH + 1800, 0.40, ts_epoch=None),
            legacy_record(BASE_EPOCH + 2400, 0.45, timestamp=None),
            legacy_record(BASE_EPOCH + 3000, 0.50, risk_score=None),
            {"ts_epoch": BASE_EPOCH + 3600, "risk_score": 0.55},  # not a history item at all
            "not-a-record",
            legacy_record(BASE_EPOCH + 4200, 0.60),
        ]
        store = StubHistoryStore(records)
        with self.assertLogs("floodpulse.handler", level="WARNING") as captured:
            status, body = self.history(BASE_EPOCH + 4300, "1440", store=store)
        self.assertEqual(status, 200)
        self.assertEqual(
            [point["ts_epoch"] for point in body["points"]],
            [BASE_EPOCH, BASE_EPOCH + 600, BASE_EPOCH + 1200, BASE_EPOCH + 4200],
            "a record that can be shown was dropped, or one that cannot was served",
        )
        self.assertEqual(body["point_count"], 4)
        self.assertIn("unusable=5", " ".join(captured.output))

    def test_a_record_with_no_band_label_is_classified_from_its_score(self) -> None:
        """The band is a pure function of the frozen score; the effective class is never invented."""
        records = [
            legacy_record(BASE_EPOCH, 0.92, risk_class=None, risk_class_candidate=None),
            legacy_record(BASE_EPOCH + 600, 0.61, risk_class="WATCH", risk_class_candidate=None),
        ]
        _status, body = self.history(BASE_EPOCH + 660, "1440", store=StubHistoryStore(records))
        self.assertEqual(body["points"][0]["risk_class"], "CRITICAL")
        self.assertEqual(body["points"][0]["risk_class_candidate"], "CRITICAL")
        # A stored effective class is reported as stored, even where it differs from the band.
        self.assertEqual(body["points"][1]["risk_class"], "WATCH")
        self.assertEqual(body["points"][1]["risk_class_candidate"], "WARNING")

    def test_a_record_with_no_readings_map_keeps_the_point(self) -> None:
        records = [legacy_record(BASE_EPOCH, 0.20, readings="corrupt")]
        _status, body = self.history(BASE_EPOCH + 60, "1440", store=StubHistoryStore(records))
        self.assertEqual(body["points"][0]["readings"], {})

    def test_the_route_answers_200_not_500_for_a_malformed_record(self) -> None:
        """(d) the exact regression: one bad row used to answer 500 for the whole city."""
        now = time.time()
        self.store = LocalStore()
        set_store(self.store)
        store_reading(self.store, int(now) - 600, 0.30)
        pk = build_zone_pk(ZONE)
        corrupt = legacy_record(int(now) - 300, 0.35, risk_score=None)
        self.store._items[(pk, str(corrupt["sk"]))] = corrupt

        event = {
            "requestContext": {"http": {"method": "GET"}},
            "rawPath": f"/zones/{ZONE}/history",
            "queryStringParameters": {"minutes": "1440"},
        }
        with self.assertLogs("floodpulse.handler", level="WARNING") as captured:
            response = lambda_handler(event)
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["point_count"], 1)
        self.assertTrue(
            any("history_unusable_records" in line for line in captured.output),
            "the skipped record left no trace in the log",
        )

    def test_a_record_the_store_cannot_place_in_time_does_not_raise(self) -> None:
        """(d) the store-side half of the same bug class: an unreadable stamp must not 500.

        A half-written item cannot be produced through the store's public API - that is what
        makes it malformed - so it is injected in the item shape ``storage.build_history_item``
        writes, and the route is asked to serve the window anyway.
        """
        store_reading(self.store, BASE_EPOCH, 0.20)
        record = legacy_record(BASE_EPOCH + 600, 0.30)
        del record["ts_epoch"]
        self.store._items[(build_zone_pk(ZONE), str(record["sk"]))] = record
        status, body = self.history(BASE_EPOCH + 660, "1440")
        self.assertEqual(status, 200)
        self.assertEqual([point["ts_epoch"] for point in body["points"]], [BASE_EPOCH])
        self.assertEqual(self.store.history_count(ZONE), 2, "the malformed item was deleted")
        self.assertEqual(len(self.store.get_history(ZONE)), 2, "a read raised on the bad item")

    def test_a_malformed_record_is_counted_in_the_log_line_only(self) -> None:
        """The response contract has no field for it: operators get the log, clients get points."""
        records = [legacy_record(BASE_EPOCH, 0.20), legacy_record(BASE_EPOCH + 60, 0.25, risk_score="x")]
        with self.assertLogs("floodpulse.handler", level="WARNING") as captured:
            status, body = self.history(BASE_EPOCH + 120, "1440", store=StubHistoryStore(records))
        self.assertEqual(status, 200)
        self.assertNotIn("skipped", body)
        self.assertIn("unusable=1", " ".join(captured.output))


# --------------------------------------------------------------------------- #
# (e) no NaN/Infinity in the JSON body
# --------------------------------------------------------------------------- #


class NonFiniteNumberTests(HistoryEndpointTestCase):
    def test_a_non_finite_score_is_never_served(self) -> None:
        """(e) ``json.dumps`` writes NaN/Infinity as bare tokens, which is not JSON at all."""
        records = [
            legacy_record(BASE_EPOCH, 0.20),
            legacy_record(BASE_EPOCH + 600, float("nan")),
            legacy_record(BASE_EPOCH + 1200, float("inf")),
            legacy_record(BASE_EPOCH + 1800, 0.30),
        ]
        status, body = self.history(BASE_EPOCH + 1860, "1440", store=StubHistoryStore(records))
        self.assertEqual(status, 200)
        self.assertEqual([point["risk_score"] for point in body["points"]], [0.20, 0.30])
        # allow_nan=False is the strict encoder: it raises if any non-finite float survives.
        text = json.dumps(body, allow_nan=False)
        for token in ("NaN", "Infinity", "-Infinity"):
            self.assertNotIn(token, text)

    def test_a_non_finite_metric_is_reported_as_unknown(self) -> None:
        readings = {
            "rainfall_mm_per_hr": float("inf"),
            "drain_level_m": 0.5,
            "soil_saturation_pct": 40,  # an int must stay an int
            "water_level_m": float("nan"),
            "flow_rate_m3_s": 1.5,
        }
        records = [legacy_record(BASE_EPOCH, 0.20, readings=readings)]
        status, body = self.history(BASE_EPOCH + 60, "1440", store=StubHistoryStore(records))
        self.assertEqual(status, 200)
        served = body["points"][0]["readings"]
        self.assertIsNone(served["rainfall_mm_per_hr"])
        self.assertIsNone(served["water_level_m"])
        self.assertEqual(served["drain_level_m"], 0.5)
        self.assertEqual(served["flow_rate_m3_s"], 1.5)
        self.assertEqual(served["soil_saturation_pct"], 40)
        text = json.dumps(body, separators=(",", ":"), allow_nan=False)
        self.assertIn('"soil_saturation_pct":40', text)
        self.assertIn('"rainfall_mm_per_hr":null', text)

    def test_the_adapter_body_is_strict_json(self) -> None:
        now = int(time.time())
        self.store = LocalStore()
        set_store(self.store)
        store_reading(self.store, now - 600, 0.30)
        pk = build_zone_pk(ZONE)
        corrupt = legacy_record(now - 300, 0.20, readings={"rainfall_mm_per_hr": float("nan")})
        self.store._items[(pk, str(corrupt["sk"]))] = corrupt
        response = lambda_handler({
            "requestContext": {"http": {"method": "GET"}},
            "rawPath": f"/zones/{ZONE}/history",
            "queryStringParameters": {"minutes": "1440"},
        })
        self.assertEqual(response["statusCode"], 200)
        for token in ("NaN", "Infinity", "-Infinity"):
            self.assertNotIn(token, response["body"])
        json.loads(response["body"], parse_constant=self.reject_constant)

    @staticmethod
    def reject_constant(token: str):
        raise AssertionError(f"the body carries the invalid JSON constant {token}")


# --------------------------------------------------------------------------- #
# (f) zone id validation and (g) minutes bounds
# --------------------------------------------------------------------------- #


class RequestValidationTests(HistoryEndpointTestCase):
    def test_an_unknown_zone_is_a_clean_404(self) -> None:
        """(f) never a 500, and never a trace of the store."""
        for zone_id in ("zone-atlantis", LIVE_ZONE, "ZONE-RIVERSIDE-NORTH", "city-chennai-55", "x" * 5000):
            with self.subTest(zone_id=zone_id[:24]):
                status, body = self.history(BASE_EPOCH, "60", zone=zone_id)
                self.assertEqual(status, 404)
                self.assertEqual(body["error"], "unknown_zone")
                self.assertEqual(body["known_zones"], list(ZONE_IDS))

    def test_malformed_paths_are_404_not_500(self) -> None:
        """A path that is not the history route is a plain 404 - no zone id is guessed from it.

        ``/zones/../history`` *is* the route, with an id that is simply unknown - a different
        404 body, and still never a 500.
        """
        expected = {
            "/zones//history": "not_found",
            "/zones/../history": "unknown_zone",
            "/zones/zone-riverside-north/../history": "not_found",
            "/zones/../../etc/passwd/history": "not_found",
            "/zones/zone-riverside-north/historyx": "not_found",
            "/zones/zone-riverside-north/history/extra": "not_found",
        }
        for path, error in expected.items():
            with self.subTest(path=path):
                status, body = dispatch("GET", path, None, self.store, BASE_EPOCH, {"minutes": "60"})
                self.assertEqual(status, 404)
                self.assertEqual(body["error"], error)

    def test_a_write_method_is_refused(self) -> None:
        status, body = dispatch("POST", f"/zones/{ZONE}/history", "{}", self.store, BASE_EPOCH)
        self.assertEqual(status, 405)
        self.assertEqual(body["allowed"], ["GET"])

    def test_minutes_bounds_are_one_to_the_configured_maximum(self) -> None:
        """(g) 1..HISTORY_MAX_MINUTES is inclusive; everything else is a 400 about ``minutes``."""
        for accepted in (1, 60, HISTORY_MAX_MINUTES):
            with self.subTest(minutes=accepted):
                status, body = self.history(BASE_EPOCH, accepted)
                self.assertEqual(status, 200)
                self.assertEqual(body["minutes"], accepted)
        rejected = (
            0, -1, -5, HISTORY_MAX_MINUTES + 1, 1000000,
            "0", "-5", "1441", "abc", "1.5", "", "60.0", "60x", "+60", "1e2",
            True, False, 1.5, ["10", "600"], {"minutes": 60},
        )
        for raw in rejected:
            with self.subTest(minutes=repr(raw)):
                status, body = self.history(BASE_EPOCH, raw)
                self.assertEqual(status, 400)
                self.assertEqual(body["error"], "invalid_query")
                self.assertEqual(body["field"], "minutes")

    def test_a_missing_minutes_parameter_uses_the_default(self) -> None:
        status, body = self.history(BASE_EPOCH)
        self.assertEqual(status, 200)
        self.assertEqual(body["minutes"], HISTORY_DEFAULT_MINUTES)
        self.assertEqual(body["since_epoch"], int(BASE_EPOCH) - HISTORY_DEFAULT_MINUTES * 60)

    def test_a_window_with_nothing_in_it_is_an_empty_window(self) -> None:
        store_reading(self.store, BASE_EPOCH, 0.20)
        status, body = self.history(BASE_EPOCH + 3600, 1)
        self.assertEqual(status, 200)
        self.assertEqual(body["point_count"], 0)
        self.assertEqual(body["points"], [])
        self.assertFalse(body["downsampled"])


# --------------------------------------------------------------------------- #
# The window edge matrix, through the shared implementation
# --------------------------------------------------------------------------- #


class WindowEdgeMatrixTests(HistoryEndpointTestCase):
    def window(self, scores: list[tuple[float, float]], minutes: int = 1440):
        for index, (score, rainfall) in enumerate(scores):
            store_reading(self.store, BASE_EPOCH + index * 600, score, rainfall=rainfall)
        now = BASE_EPOCH + (len(scores) - 1) * 600 + 60 if scores else BASE_EPOCH + 60
        status, body = self.history(now, minutes)
        self.assertEqual(status, 200)
        return temporal.analyse(body["points"], now_epoch=now), body

    def test_the_endpoint_window_reproduces_the_contract_exactly(self) -> None:
        """Every shape the fixture pins, through stored rows and the served points."""
        matrix = {
            "empty": ([], 0, temporal.STATUS_INSUFFICIENT, temporal.REASON_NO_SAMPLES, None),
            "one_sample": ([(0.20, 0.0)], 1, temporal.STATUS_INSUFFICIENT, temporal.REASON_TOO_FEW_SAMPLES, None),
            "two_samples": ([(0.20, 0.0), (0.30, 0.0)], 2, temporal.STATUS_INSUFFICIENT,
                            temporal.REASON_TOO_FEW_SAMPLES, None),
            "three_samples": ([(0.20, 0.0), (0.30, 0.0), (0.40, 0.0)], 3, temporal.STATUS_OK, None, "RISING"),
            "flat": ([(0.20, 0.0)] * 4, 4, temporal.STATUS_OK, None, "STEADY"),
            "rising": ([(0.10, 0.0), (0.16, 2.0), (0.22, 3.5), (0.31, 5.8)], 4, temporal.STATUS_OK, None, "RISING"),
            "falling": ([(0.44, 4.0), (0.36, 2.0), (0.30, 0.4), (0.21, 0.0)], 4, temporal.STATUS_OK, None, "FALLING"),
            "noisy_flat": ([(0.20, 0.0), (0.21, 0.0), (0.19, 0.0), (0.20, 0.0), (0.21, 0.0)], 5,
                           temporal.STATUS_OK, None, "STEADY"),
            "large_gap": ([(0.20, 0.0), (0.22, 0.0), (0.24, 0.0), (0.30, 1.0), (0.33, 1.2), (0.36, 1.4)], 6,
                          temporal.STATUS_OK, None, "RISING"),
        }
        for name, (scores, count, status, reason, trend) in matrix.items():
            with self.subTest(shape=name):
                self.setUp()
                summary, body = self.window(scores)
                self.assertEqual(body["point_count"], count)
                self.assertEqual(summary.count, count)
                self.assertEqual(summary.status, status)
                self.assertEqual(summary.reason, reason)
                self.assertEqual(summary.trend, trend)

    def test_the_served_window_measures_the_documented_duration_and_gap(self) -> None:
        scores = [(0.20, 0.0), (0.22, 0.0), (0.24, 0.0), (0.30, 1.0), (0.33, 1.2), (0.36, 1.4)]
        summary, _body = self.window(scores)
        self.assertEqual(summary.duration_seconds, 3000)
        self.assertClose(summary.median_interval, 600)
        self.assertClose(summary.largest_gap, 600)
        self.assertEqual(summary.gap_count, 0)

    def test_a_large_gap_in_a_dense_cadence_is_reported_as_one_gap(self) -> None:
        """A 38-minute hole in a ten-minute cadence, expressed with uneven steps."""
        stamps = [0, 600, 1200, 3480, 4080, 4680]
        for index, stamp in enumerate(stamps):
            store_reading(self.store, BASE_EPOCH + stamp, 0.20 + index * 0.03)
        _status, body = self.history(BASE_EPOCH + 4680 + 60, "1440")
        summary = temporal.analyse(body["points"])
        self.assertClose(summary.median_interval, 600)
        self.assertClose(summary.largest_gap, 2280)
        self.assertEqual(summary.gap_count, 1)
        self.assertEqual(summary.trend, "RISING")

    def test_a_stale_window_is_still_described_and_its_newest_sample_is_dated(self) -> None:
        for index in range(3):
            store_reading(self.store, BASE_EPOCH + index * 600, 0.20 + index * 0.04)
        now = BASE_EPOCH + 1200 + 2520
        _status, body = self.history(now, "1440")
        summary = temporal.analyse(body["points"], now_epoch=now)
        self.assertEqual(summary.status, temporal.STATUS_OK)
        self.assertEqual(summary.trend, "RISING")
        self.assertClose(summary.newest_age_seconds, 2520)

    def test_the_band_round_trip_is_visible_in_the_served_points(self) -> None:
        scores = [0.20, 0.38, 0.64, 0.85, 0.70, 0.45, 0.24]
        for index, score in enumerate(scores):
            store_reading(self.store, BASE_EPOCH + index * 600, score)
        _status, body = self.history(BASE_EPOCH + 3600 + 60, "1440")
        self.assertEqual(
            [point["risk_class"] for point in body["points"]],
            ["NORMAL", "WATCH", "WARNING", "CRITICAL", "WARNING", "WATCH", "NORMAL"],
        )
        summary = temporal.analyse(body["points"])
        self.assertEqual(summary.classes, ["NORMAL", "WATCH", "WARNING", "CRITICAL", "WARNING", "WATCH", "NORMAL"])
        self.assertEqual(summary.trend, "RISING")

    def test_a_missing_rainfall_reading_is_not_filled_from_an_older_row(self) -> None:
        without_rainfall = {key: value for key, value in CALM.items() if key != "rainfall_mm_per_hr"}
        store_reading(self.store, BASE_EPOCH, 0.20, rainfall=4.0)
        store_reading(self.store, BASE_EPOCH + 600, 0.24, readings=without_rainfall, rainfall=None)
        store_reading(self.store, BASE_EPOCH + 1200, 0.28, rainfall=0.5)
        _status, body = self.history(BASE_EPOCH + 1260, "1440")
        middle = body["points"][1]
        self.assertNotIn("rainfall_mm_per_hr", middle["readings"])
        summary = temporal.analyse(body["points"])
        self.assertClose(summary.max_rainfall, 4.0)
        self.assertEqual(summary.rainfall_samples, 2)

    def assertClose(self, actual, expected, message: str = "") -> None:
        self.assertAlmostEqual(float(actual), float(expected), places=9, msg=message)


if __name__ == "__main__":  # pragma: no cover - convenience only
    unittest.main()

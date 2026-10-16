"""Provenance labelling across every route (the honesty rules of the project).

The live fleet's rainfall comes from Open-Meteo, its hydraulics are simulated by FloodPulse,
and a history window or an incident list may hold records of either provenance. Each response
must say what *its own* data is: a live city's chart may never be served as if the whole
system were simulated, and a simulated fleet may never be upgraded to a live claim.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, "src")

from floodpulse.auth import INGEST_KEY_ENV, INGEST_KEY_HEADER
from floodpulse.config import (
    DATA_CLASSIFICATION_MIXED,
    DATA_CLASSIFICATION_SIMULATED,
    ZONE_MODE_ENV,
    ZONE_MODE_LIVE,
    zone_definitions_for_mode,
)
from floodpulse.handler import dispatch, provenance_of_records
from floodpulse.incidents import build_alert_marker, build_incident_item, decide_transition
from floodpulse.notify import NoopNotifier
from floodpulse.storage import LocalStore
from floodpulse.weather import FetchResult, Observation

from tests.support import START_EPOCH, ZoneScenario

CITY = "city-chennai"
NOW = float(START_EPOCH)
LIVE_ENV = {ZONE_MODE_ENV: "live", "FLOODPULSE_WEATHER": "open-meteo"}
DEMO_ENV: dict[str, str] = {}


def observation(zone_id: str, rain: float, soil_saturation_pct: float) -> Observation:
    return Observation(
        zone_id=zone_id,
        ok=True,
        rainfall_mm_per_hr=rain,
        soil_moisture_m3m3=0.2,
        soil_saturation_pct=soil_saturation_pct,
        provider_current_time="2026-10-15T18:15",
        observed_at="2026-10-15T18:00",
        observed_at_epoch=NOW,
        latitude=13.0827,
        longitude=80.2707,
        error=None,
    )


def storm_reading(zone_id: str, step: int, *, rain: float = 60.0) -> str:
    """One externally posted reading for a zone, in the shape the ingest route accepts.

    Posted readings are always simulated (validation refuses to accept a client's own
    provenance claim), which is exactly what this helper exists to exercise.
    """
    ts = datetime.fromtimestamp(NOW + step * 30, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return json.dumps(
        {
            "schema_version": "1.0",
            "simulated": True,
            "zone_id": zone_id,
            "sensor_node_id": "field-kit",
            "timestamp": ts,
            "readings": {
                "rainfall_mm_per_hr": rain,
                "drain_level_m": 0.9,
                "soil_saturation_pct": 96.0,
                "water_level_m": 2.8,
                "flow_rate_m3_s": 9.0,
            },
        }
    )


class FixedWeatherClient:
    """Offline weather client returning the same conditions for every live city."""

    def __init__(self, *, rain: float, soil_saturation_pct: float) -> None:
        self.rain = rain
        self.soil_saturation_pct = soil_saturation_pct
        self.calls = 0

    def fetch(self, zones) -> FetchResult:
        self.calls += 1
        return FetchResult(
            ok=True,
            observations={
                str(zone["zone_id"]): observation(str(zone["zone_id"]), self.rain, self.soil_saturation_pct)
                for zone in zones
            },
            fetched_at_epoch=NOW,
            requested=len(zones),
        )


def seed_live_readings(store: LocalStore, *, rain: float = 0.4, soil: float = 22.0, times: int = 1) -> None:
    client = FixedWeatherClient(rain=rain, soil_saturation_pct=soil)
    for step in range(times):
        dispatch("GET", "/zones", store=store, now_epoch=NOW + step * 601, env=dict(LIVE_ENV), weather_client=client)


class ProvenanceRuleTest(unittest.TestCase):
    """A mixed *window* can only be built fixture-side, so the rule is unit-tested."""

    def test_one_provenance_is_reported_as_itself(self) -> None:
        for classification in (DATA_CLASSIFICATION_SIMULATED, DATA_CLASSIFICATION_MIXED):
            with self.subTest(classification=classification):
                records = [{"data_classification": classification}] * 3
                self.assertEqual(provenance_of_records(records, DATA_CLASSIFICATION_SIMULATED), classification)

    def test_disagreeing_records_are_mixed(self) -> None:
        records = [{"data_classification": "simulated"}, {"data_classification": "mixed"}]
        self.assertEqual(provenance_of_records(records, DATA_CLASSIFICATION_SIMULATED), DATA_CLASSIFICATION_MIXED)

    def test_an_empty_selection_falls_back_to_the_fleet(self) -> None:
        self.assertEqual(provenance_of_records([], DATA_CLASSIFICATION_MIXED), DATA_CLASSIFICATION_MIXED)
        self.assertEqual(provenance_of_records([], DATA_CLASSIFICATION_SIMULATED), DATA_CLASSIFICATION_SIMULATED)

    def test_records_without_a_classification_are_not_guessed_at(self) -> None:
        records = [{}, {"data_classification": None}, {"data_classification": ""}]
        self.assertEqual(provenance_of_records(records, DATA_CLASSIFICATION_SIMULATED), DATA_CLASSIFICATION_SIMULATED)
        # one tagged record among untagged ones still wins, because the tagged record is proof
        self.assertEqual(
            provenance_of_records([{}, {"data_classification": DATA_CLASSIFICATION_MIXED}], DATA_CLASSIFICATION_SIMULATED),
            DATA_CLASSIFICATION_MIXED,
        )


class AlertProvenanceTest(unittest.TestCase):
    """The published alert states the provenance of the incident that raised it.

    This is the only artifact that leaves FloodPulse (it lands in an inbox), so it is the
    worst place to mislabel real rainfall as simulated.
    """

    class CapturingNotifier:
        enabled = True

        def __init__(self) -> None:
            self.messages: list[tuple[str, str]] = []

        def publish(self, subject: str, message: str) -> dict:
            self.messages.append((subject, message))
            return {"published": True, "message_id": "test-message"}

        def redact(self, text: str) -> str:
            return text

    def test_a_live_rainfall_incident_publishes_mixed_provenance(self) -> None:
        store = LocalStore()
        notifier = self.CapturingNotifier()
        hot = FixedWeatherClient(rain=60.0, soil_saturation_pct=99.0)
        for step in range(3):
            dispatch(
                "GET", "/zones", store=store, now_epoch=NOW + step * 601, env=dict(LIVE_ENV),
                weather_client=hot, notifier=notifier, cooldown_seconds=600,
            )
        self.assertTrue(notifier.messages, "the live incident published no alert at all")
        subject, message = notifier.messages[0]
        self.assertNotIn("(simulated", subject, f"a live incident was labelled simulated: {subject}")
        body = json.loads(message)
        self.assertEqual(body["data_classification"], DATA_CLASSIFICATION_MIXED)
        self.assertFalse(body["simulated"])
        self.assertIn("real observations", body["action"])

    def test_a_demo_incident_still_publishes_simulated_provenance(self) -> None:
        store = LocalStore()
        notifier = self.CapturingNotifier()
        scenario = ZoneScenario(store, zone_id="zone-old-town-canal", notifier=notifier, cooldown_seconds=600)
        scenario.run(45)  # the simulated arc needs ~30 readings from calm to reach WARNING
        self.assertTrue(notifier.messages,
                        f"the demo incident published no alert at all (actions: {scenario.actions()})")
        subject, message = notifier.messages[0]
        self.assertIn("(simulated", subject)
        body = json.loads(message)
        self.assertEqual(body["data_classification"], DATA_CLASSIFICATION_SIMULATED)
        self.assertTrue(body["simulated"])


class HistoryProvenanceTest(unittest.TestCase):
    """GET /zones/{zone_id}/history reports what its own records are."""

    def test_a_live_city_history_is_mixed_and_keeps_its_metric_sources(self) -> None:
        store = LocalStore()
        seed_live_readings(store)
        status, payload = dispatch(
            "GET", f"/zones/{CITY}/history", store=store, now_epoch=NOW, query={"minutes": "60"}, env=dict(LIVE_ENV)
        )
        self.assertEqual(status, 200)
        self.assertGreater(payload["point_count"], 0, "the live reading was not stored as history")
        self.assertEqual(payload["data_classification"], DATA_CLASSIFICATION_MIXED)
        self.assertFalse(payload["simulated"], "a live city's history must not claim to be fully simulated")
        self.assertEqual(payload["zone_mode"], ZONE_MODE_LIVE)
        self.assertEqual(payload["metric_sources"]["rainfall_mm_per_hr"], "REAL")
        self.assertEqual(payload["metric_sources"]["water_level_m"], "SIMULATED")

    def test_a_simulated_zone_history_is_simulated(self) -> None:
        store = LocalStore()
        scenario = ZoneScenario(store)
        scenario.run(2)
        status, payload = dispatch("GET", f"/zones/{scenario.zone_id}/history", store=store, now_epoch=NOW + 600)
        self.assertEqual(status, 200)
        self.assertGreater(payload["point_count"], 0)
        self.assertEqual(payload["data_classification"], DATA_CLASSIFICATION_SIMULATED)
        self.assertTrue(payload["simulated"])
        self.assertEqual(payload["metric_sources"]["rainfall_mm_per_hr"], "SIMULATED")

    def test_an_empty_window_reports_the_fleet_it_belongs_to(self) -> None:
        for env, expected in ((dict(LIVE_ENV), DATA_CLASSIFICATION_MIXED), (dict(DEMO_ENV), DATA_CLASSIFICATION_SIMULATED)):
            with self.subTest(expected=expected):
                store = LocalStore()
                zone_id = CITY if expected == DATA_CLASSIFICATION_MIXED else "zone-riverside-north"
                status, payload = dispatch(
                    "GET", f"/zones/{zone_id}/history", store=store, now_epoch=NOW, query={"minutes": "60"}, env=env
                )
                self.assertEqual(status, 200)
                self.assertEqual(payload["point_count"], 0)
                self.assertEqual(payload["data_classification"], expected)

    def test_the_routes_still_reject_what_they_always_did(self) -> None:
        store = LocalStore()
        status, payload = dispatch("GET", "/zones/city-atlantis/history", store=store, now_epoch=NOW, env=dict(LIVE_ENV))
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "unknown_zone")


class IncidentProvenanceTest(unittest.TestCase):
    """Incidents remember the provenance of the reading that opened them."""

    def test_a_live_incident_is_stored_and_served_as_mixed(self) -> None:
        store = LocalStore()
        seed_live_readings(store, rain=60.0, soil=99.0)
        status, incidents = dispatch(
            "GET", "/incidents", store=store, now_epoch=NOW, query={}, env=dict(LIVE_ENV), notifier=NoopNotifier()
        )
        self.assertEqual(status, 200)
        self.assertGreaterEqual(incidents["open_count"], 1, "the extreme reading opened no incident")
        self.assertEqual(incidents["data_classification"], DATA_CLASSIFICATION_MIXED)
        self.assertFalse(incidents["simulated"])
        self.assertEqual(incidents["zone_mode"], ZONE_MODE_LIVE)
        for incident in incidents["incidents"]:
            self.assertEqual(incident["data_classification"], DATA_CLASSIFICATION_MIXED)
            self.assertFalse(incident["simulated"])
        stored = store.latest_incident(CITY)
        self.assertEqual(stored["data_classification"], DATA_CLASSIFICATION_MIXED)
        self.assertFalse(stored["simulated"])

    def test_a_simulated_incident_is_simulated(self) -> None:
        store = LocalStore()
        scenario = ZoneScenario(store)
        _, incident = scenario.run_until_open()
        self.assertEqual(incident["data_classification"], DATA_CLASSIFICATION_SIMULATED)
        status, payload = dispatch("GET", "/incidents", store=store, now_epoch=NOW + 600, notifier=NoopNotifier())
        self.assertEqual(status, 200)
        self.assertEqual(payload["data_classification"], DATA_CLASSIFICATION_SIMULATED)
        self.assertTrue(payload["simulated"])
        self.assertTrue(all(row["simulated"] for row in payload["incidents"]))

    def test_a_live_fleet_serving_only_posted_incidents_says_simulated(self) -> None:
        """A live fleet can honestly hold nothing but simulated incidents.

        Incidents opened by a reading a client posted are simulated whatever the fleet's own
        mode is, so the list must report "simulated" - and must not borrow the fleet's "mixed"
        label, which would overstate where those incidents came from.
        """
        store = LocalStore()
        ingest_key = "test-ingest-key"
        with mock.patch.dict(os.environ, {INGEST_KEY_ENV: ingest_key}):
            for step in range(4):
                status, payload = dispatch(
                    "POST", "/readings", storm_reading(CITY, step), store=store, now_epoch=NOW + step * 30,
                    query={}, headers={INGEST_KEY_HEADER: ingest_key}, env=dict(LIVE_ENV),
                )
                self.assertEqual(status, 202, payload)
        status, payload = dispatch(
            "GET", "/incidents", store=store, now_epoch=NOW + 600, query={},
            env=dict(LIVE_ENV), notifier=NoopNotifier(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["zone_mode"], ZONE_MODE_LIVE)
        self.assertGreaterEqual(payload["count"], 1, "the posted readings opened no incident")
        self.assertEqual(payload["data_classification"], DATA_CLASSIFICATION_SIMULATED)
        self.assertTrue(payload["simulated"])
        self.assertTrue(any("simulated" in note.lower() for note in payload["notes"]),
                        "an all-simulated list on a live fleet must carry its simulation disclaimer")

    def test_an_empty_list_reports_the_fleet_it_belongs_to(self) -> None:
        for env, expected in ((dict(LIVE_ENV), DATA_CLASSIFICATION_MIXED), (dict(DEMO_ENV), DATA_CLASSIFICATION_SIMULATED)):
            with self.subTest(expected=expected):
                store = LocalStore()
                status, payload = dispatch(
                    "GET", "/incidents", store=store, now_epoch=NOW, query={}, env=env, notifier=NoopNotifier()
                )
                self.assertEqual(status, 200)
                self.assertEqual(payload["count"], 0)
                self.assertEqual(payload["data_classification"], expected)

    def test_the_disclaimer_matches_the_classification(self) -> None:
        live = LocalStore()
        seed_live_readings(live, rain=60.0, soil=99.0)
        _, mixed = dispatch("GET", "/incidents", store=live, now_epoch=NOW, query={}, env=dict(LIVE_ENV), notifier=NoopNotifier())
        self.assertTrue(any("simulated" in note.lower() for note in mixed["notes"]))
        self.assertTrue(any("open-meteo" in note.lower() for note in mixed["notes"]))

        demo = LocalStore()
        _, plain = dispatch("GET", "/incidents", store=demo, now_epoch=NOW, query={}, notifier=NoopNotifier())
        self.assertTrue(all("Open-Meteo" not in note for note in plain["notes"]))


class IncidentRecordProvenanceTest(unittest.TestCase):
    """The incident record itself carries the provenance; the default stays simulated."""

    def test_decide_transition_marks_a_mixed_incident(self) -> None:
        plan = decide_transition(
            zone_id=CITY,
            active=None,
            risk_class="CRITICAL",
            risk_score=0.9,
            top_driver="rainfall_mm_per_hr",
            observed_ts_epoch=NOW,
            now_epoch=NOW,
            data_classification=DATA_CLASSIFICATION_MIXED,
        )
        self.assertEqual(plan.incident["data_classification"], DATA_CLASSIFICATION_MIXED)
        self.assertFalse(plan.incident["simulated"])

    def test_defaults_stay_simulated(self) -> None:
        item = build_incident_item(
            zone_id="zone-riverside-north",
            severity="WARNING",
            risk_score=0.7,
            risk_class="WARNING",
            top_driver="water_level_m",
            opened_ts_epoch=NOW,
            now_epoch=NOW,
        )
        self.assertEqual(item["data_classification"], DATA_CLASSIFICATION_SIMULATED)
        self.assertTrue(item["simulated"])
        marker = build_alert_marker(zone_id="zone-riverside-north", severity="WARNING", now_epoch=NOW)
        self.assertEqual(marker["data_classification"], DATA_CLASSIFICATION_SIMULATED)
        self.assertTrue(marker["simulated"])

    def test_alert_marker_follows_the_reading(self) -> None:
        marker = build_alert_marker(
            zone_id=CITY, severity="CRITICAL", now_epoch=NOW, data_classification=DATA_CLASSIFICATION_MIXED
        )
        self.assertEqual(marker["data_classification"], DATA_CLASSIFICATION_MIXED)
        self.assertFalse(marker["simulated"])


class EveryLiveCityKeepsItsOwnProvenanceTest(unittest.TestCase):
    def test_the_whole_live_fleet_reports_mixed_history(self) -> None:
        store = LocalStore()
        seed_live_readings(store, times=2)
        for definition in zone_definitions_for_mode(ZONE_MODE_LIVE):
            zone_id = str(definition["zone_id"])
            with self.subTest(zone_id=zone_id):
                status, payload = dispatch(
                    "GET", f"/zones/{zone_id}/history", store=store, now_epoch=NOW + 601,
                    query={"minutes": "60"}, env=dict(LIVE_ENV),
                )
                self.assertEqual(status, 200)
                self.assertGreater(payload["point_count"], 0)
                self.assertEqual(payload["data_classification"], DATA_CLASSIFICATION_MIXED)
                self.assertFalse(payload["simulated"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

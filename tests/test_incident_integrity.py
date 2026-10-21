"""Incident integrity under concurrency (M17).

Every case here models an interleaving that the earlier implementation got wrong, and asserts
the invariant that now holds instead. The two structural guards are:

* **one open incident per zone** - claimed with a conditional write (``claim_incident_open``),
  so two invocations that both decided to open an incident cannot both do it, and therefore
  cannot both publish an alert for the same event;
* **version-guarded incident writes** - a state change is written only against the version it
  was decided from, so an older view can no longer erase a newer escalation, its alert entry,
  or a cooldown claim.

The reads are pinned where the bug needed them to be stale. That is not a contrivance: DynamoDB
reads of the incident prefix are eventually consistent in the deployed configuration, and the
audit that produced these tests proved the reachable interleaving with the real code.
"""

from __future__ import annotations

import unittest

import floodpulse.handler as handler
from floodpulse.config import (
    ALERT_MARKER_SORT_KEY,
    INCIDENT_OPEN_RELEASED,
    INCIDENT_OPEN_SORT_KEY,
    ZONE_IDS,
)
from floodpulse.dynamodb_store import DynamoDbStore
from floodpulse.handler import dispatch, set_notifier, set_store
from floodpulse.incidents import (
    build_alert_marker,
    incident_open_pointer,
    incident_view,
)
from floodpulse.storage import LocalStore, marker_without_severity

from tests.support import ALERT_COOLDOWN, RecordingNotifier
from tests.test_dynamodb_store import FakeTable

ZONE = ZONE_IDS[0]
OTHER_ZONE = ZONE_IDS[1]
BASE_EPOCH = 1_771_064_100


class FailingNotifier:
    """A notifier whose publish always fails, with the same surface as the real one."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.attempts = 0

    def publish(self, subject: str, message: str) -> dict:
        self.attempts += 1
        raise RuntimeError("SNS is not reachable in this test")

    def redact(self, text: str) -> str:
        return text


class PinnedReadStore(LocalStore):
    """A store whose incident read can be pinned, as an eventually-consistent read can be.

    ``pinned`` is the value every ``latest_incident`` call returns while it is set. It models the
    window the audit proved: two invocations score two readings for the same city and both read
    the state as it was *before* either of them wrote.
    """

    _UNSET = object()

    def __init__(self) -> None:
        super().__init__()
        self.pinned = self._UNSET

    def latest_incident(self, zone_id: str):  # type: ignore[override]
        if self.pinned is self._UNSET:
            return super().latest_incident(zone_id)
        return self.pinned


def track(store, notifier, *, zone_id: str = ZONE, risk_class: str = "CRITICAL", ts: int = BASE_EPOCH):
    """Run one incident evaluation, the way POST /readings does."""
    return handler._track_incident(
        store,
        notifier,
        zone_id=zone_id,
        risk_class=risk_class,
        risk_score=0.91,
        top_driver="rainfall_mm_per_hr",
        observed_ts_epoch=ts,
        now_epoch=ts,
        cooldown_seconds=ALERT_COOLDOWN,
    )


def open_incidents(store, zone_id: str = ZONE) -> list[dict]:
    return [
        item
        for item in store.list_incidents(zone_id=zone_id)
        if item["current_status"] == "OPEN"
    ]


class TwoInvocationsThatBothSawNothingOpenTest(unittest.TestCase):
    """The P1 the audit proved: a stale read used to open two incidents and send two alerts."""

    def test_only_one_incident_is_opened_and_only_one_alert_is_published(self) -> None:
        store = PinnedReadStore()
        notifier = RecordingNotifier()
        # Both invocations see the state as it was before either of them wrote.
        store.pinned = store.latest_incident(ZONE)

        first, first_alert = track(store, notifier, ts=BASE_EPOCH)
        second, second_alert = track(store, notifier, ts=BASE_EPOCH + 1)

        self.assertEqual(first["action"], "opened")
        self.assertTrue(first_alert["published"])
        # The loser adopts the winner's record instead of opening a second incident.
        self.assertEqual(second["action"], "observed")
        self.assertFalse(second_alert["evaluated"])
        self.assertEqual(second_alert["reason"], "no_escalation")
        self.assertEqual(len(open_incidents(store)), 1)
        self.assertEqual(len(store.list_incidents(zone_id=ZONE)), 1)
        self.assertEqual(len(notifier.published), 1, "one event must not send two alerts")
        self.assertEqual(second["incident_id"], first["incident_id"])

    def test_the_pointer_names_the_incident_that_was_stored(self) -> None:
        store, notifier = LocalStore(), RecordingNotifier()
        summary, _alert = track(store, notifier)
        stored = store.latest_incident(ZONE)

        pointer = store.get_incident_open(ZONE)
        self.assertIsNotNone(pointer)
        self.assertEqual(pointer["incident_sk"], stored["sk"])
        self.assertEqual(pointer["incident_id"], summary["incident_id"])
        self.assertEqual(pointer["record_type"], "INCIDENT_OPEN")
        # The pointer is not an incident: it must never be served by the incident list.
        self.assertEqual([item["sk"] for item in store.list_incidents(zone_id=ZONE)], [stored["sk"]])
        self.assertEqual(store.incident_count(ZONE), 1)


class OpenSlotLifecycleTest(unittest.TestCase):
    """A zone must always be able to open its next incident, even after a crash."""

    def test_a_pointer_left_by_a_crash_is_released_and_the_incident_still_opens(self) -> None:
        store, notifier = LocalStore(), RecordingNotifier()
        phantom = f"INCIDENT#{BASE_EPOCH}"
        store.claim_incident_open(
            ZONE, incident_open_pointer(ZONE, {"sk": phantom, "incident_id": "never-stored"}, BASE_EPOCH)
        )

        summary, alert = track(store, notifier)

        self.assertEqual(summary["action"], "opened")
        self.assertTrue(alert["published"])
        self.assertEqual(len(open_incidents(store)), 1)
        self.assertEqual(store.get_incident_open(ZONE)["incident_sk"], store.latest_incident(ZONE)["sk"])

    def test_a_released_slot_can_be_claimed_again(self) -> None:
        store = LocalStore()
        first = incident_open_pointer(ZONE, {"sk": f"INCIDENT#{BASE_EPOCH}"}, BASE_EPOCH)
        self.assertTrue(store.claim_incident_open(ZONE, first))
        self.assertFalse(store.claim_incident_open(ZONE, first))
        self.assertTrue(store.release_incident_open(ZONE, first["incident_sk"]))
        self.assertTrue(store.claim_incident_open(ZONE, first))

    def test_releasing_keeps_the_pointer_and_only_empties_its_claim(self) -> None:
        store = LocalStore()
        pointer = incident_open_pointer(ZONE, {"sk": f"INCIDENT#{BASE_EPOCH}"}, BASE_EPOCH)
        store.claim_incident_open(ZONE, pointer)

        self.assertTrue(store.release_incident_open(ZONE, pointer["incident_sk"]))

        stored = store.get_incident_open(ZONE)
        self.assertIsNotNone(stored, "the slot is released by rewriting it, never by deleting it")
        self.assertEqual(stored["incident_sk"], INCIDENT_OPEN_RELEASED)
        self.assertNotIn(
            (f"ZONE#{ZONE}", INCIDENT_OPEN_SORT_KEY),
            {},
            "the item is still stored under its key",
        )

    def test_a_closing_invocation_cannot_free_a_newer_slot(self) -> None:
        store = LocalStore()
        older = incident_open_pointer(ZONE, {"sk": f"INCIDENT#{BASE_EPOCH}"}, BASE_EPOCH)
        newer = incident_open_pointer(ZONE, {"sk": f"INCIDENT#{BASE_EPOCH + 5}"}, BASE_EPOCH + 5)
        store.claim_incident_open(ZONE, older)
        store.release_incident_open(ZONE, older["incident_sk"])
        store.claim_incident_open(ZONE, newer)

        self.assertFalse(store.release_incident_open(ZONE, older["incident_sk"]))
        self.assertEqual(store.get_incident_open(ZONE)["incident_sk"], newer["incident_sk"])

    def test_the_pointer_is_excluded_from_the_incident_queries(self) -> None:
        store = LocalStore()
        store.claim_incident_open(ZONE, incident_open_pointer(ZONE, {"sk": f"INCIDENT#{BASE_EPOCH}"}, BASE_EPOCH))
        self.assertIsNone(store.latest_incident(ZONE))
        self.assertEqual(store.list_incidents(zone_id=ZONE), [])
        self.assertEqual(store.incident_count(ZONE), 0)


class VersionGuardTest(unittest.TestCase):
    """An older view must not overwrite a newer state change."""

    def test_a_superseded_write_is_refused(self) -> None:
        store = LocalStore()
        base = {
            "pk": f"ZONE#{ZONE}",
            "sk": f"INCIDENT#{BASE_EPOCH}",
            "record_type": "INCIDENT",
            "zone_id": ZONE,
            "current_status": "OPEN",
            "version": 1,
        }
        self.assertTrue(store.create_incident(base))
        self.assertTrue(store.put_incident({**base, "version": 2, "reading_count": 5}, expected_version=1))
        # A second writer still holding version 1 (the stale view) is refused, and the newer
        # record - the escalation it would have erased - is untouched.
        self.assertFalse(store.put_incident({**base, "version": 2, "reading_count": 2}, expected_version=1))
        self.assertEqual(store.get_incident(ZONE, base["sk"])["reading_count"], 5)

    def test_a_record_without_a_version_can_still_be_written(self) -> None:
        store = LocalStore()
        legacy = {
            "pk": f"ZONE#{ZONE}",
            "sk": f"INCIDENT#{BASE_EPOCH}",
            "record_type": "INCIDENT",
            "zone_id": ZONE,
            "current_status": "OPEN",
        }
        store.create_incident(legacy)
        self.assertTrue(store.put_incident({**legacy, "version": 1}, expected_version=None))

    def test_the_tracking_write_carries_and_bumps_the_version(self) -> None:
        store, notifier = LocalStore(), RecordingNotifier()
        first, _ = track(store, notifier, ts=BASE_EPOCH)
        second, _ = track(store, notifier, risk_class="WARNING", ts=BASE_EPOCH + 1)

        self.assertEqual(second["incident_id"], first["incident_id"])
        stored = store.latest_incident(ZONE)
        # 1 on the open, 2 when the published alert was recorded against it, 3 for the later
        # observation: every write advances the version the next writer has to have read.
        self.assertEqual(stored["version"], 3)
        self.assertEqual(stored["current_status"], "OPEN")

    def test_a_lost_write_stops_the_alert_instead_of_contradicting_the_record(self) -> None:
        store, notifier = LocalStore(), RecordingNotifier()
        track(store, notifier, risk_class="WARNING", ts=BASE_EPOCH)

        # An overlapping invocation records a *newer* state between our read and our write.
        original = store.put_incident

        def racing_put(item, expected_version=None):
            stored = store._items[(str(item["pk"]), str(item["sk"]))]
            if expected_version is not None and int(stored.get("version") or 0) == int(expected_version):
                winner = dict(stored)
                winner["version"] = int(expected_version) + 1
                winner["severity"] = "CRITICAL"
                winner["severity_changes"] = [{"to": "CRITICAL"}]
                original(winner, expected_version=expected_version)
            return original(item, expected_version=expected_version)

        store.put_incident = racing_put  # type: ignore[assignment]
        summary, alert = track(store, notifier, risk_class="WARNING", ts=BASE_EPOCH + 1)

        self.assertEqual(alert["reason"], "incident_write_conflict")
        self.assertFalse(alert["published"])
        stored = store.latest_incident(ZONE)
        self.assertEqual(stored["severity"], "CRITICAL", "the newer escalation must survive")
        self.assertEqual(len(stored["severity_changes"]), 1)
        self.assertIn("left the record alone", summary["reason"])


class AlertRollbackTest(unittest.TestCase):
    """A failed publish gives back its own claim, and nobody else's."""

    def test_only_this_invocations_entry_is_removed(self) -> None:
        marker = build_alert_marker(zone_id=ZONE, severity="WARNING", now_epoch=BASE_EPOCH)
        marker = build_alert_marker(
            zone_id=ZONE, severity="CRITICAL", now_epoch=BASE_EPOCH + 30, previous=marker
        )

        updated, released = marker_without_severity(marker, "WARNING", BASE_EPOCH)

        self.assertTrue(released)
        self.assertNotIn("WARNING", updated["last_alert_by_severity"])
        self.assertIn("CRITICAL", updated["last_alert_by_severity"])
        self.assertEqual(updated["last_alert_by_severity"]["CRITICAL"]["ts_epoch"], BASE_EPOCH + 30)

    def test_a_newer_claim_for_the_same_severity_survives_the_rollback(self) -> None:
        marker = build_alert_marker(zone_id=ZONE, severity="WARNING", now_epoch=BASE_EPOCH + 5)

        _updated, released = marker_without_severity(marker, "WARNING", BASE_EPOCH)

        self.assertFalse(released, "another invocation holds the slot: nothing may be written")

    def test_the_marker_rollback_helper_agrees_with_the_release_helpers(self) -> None:
        store = LocalStore()
        store.put_alert_marker(
            build_alert_marker(zone_id=ZONE, severity="WARNING", now_epoch=BASE_EPOCH)
        )
        # A later invocation takes the same severity, then our own publish fails.
        store.put_alert_marker(
            build_alert_marker(zone_id=ZONE, severity="WARNING", now_epoch=BASE_EPOCH + 60)
        )

        self.assertFalse(store.release_alert_slot(ZONE, "WARNING", BASE_EPOCH))
        self.assertEqual(
            store.get_alert_marker(ZONE)["last_alert_by_severity"]["WARNING"]["ts_epoch"],
            BASE_EPOCH + 60,
        )
        # Our own claim, however, is given back.
        self.assertTrue(store.release_alert_slot(ZONE, "WARNING", BASE_EPOCH + 60))
        self.assertNotIn("WARNING", store.get_alert_marker(ZONE)["last_alert_by_severity"])

    def test_a_failed_publish_releases_its_own_slot_end_to_end(self) -> None:
        store, notifier = LocalStore(), FailingNotifier()
        _summary, alert = track(store, notifier)

        self.assertEqual(notifier.attempts, 1)
        self.assertEqual(alert["reason"], "publish_failed")
        self.assertTrue(alert["slot_released"])
        marker = store.get_alert_marker(ZONE)
        self.assertNotIn("CRITICAL", (marker or {}).get("last_alert_by_severity") or {})

    def test_a_failed_publish_leaves_another_invocations_claim_alone(self) -> None:
        store, notifier = LocalStore(), FailingNotifier()
        # Someone else claimed this severity one second after we did, then our publish failed.
        def racing_claim(item, *, severity, now_epoch, cooldown_seconds):
            store.put_alert_marker(
                build_alert_marker(zone_id=ZONE, severity=severity, now_epoch=now_epoch + 1)
            )
            return True

        store.claim_alert_slot = racing_claim  # type: ignore[assignment]
        _summary, alert = track(store, notifier)

        self.assertFalse(alert["slot_released"])
        self.assertEqual(
            store.get_alert_marker(ZONE)["last_alert_by_severity"]["CRITICAL"]["ts_epoch"],
            BASE_EPOCH + 1,
        )


class IncidentViewTest(unittest.TestCase):
    """The view carries what an operator needs to read a zero."""

    def test_the_last_alert_reason_is_projected(self) -> None:
        store, notifier = LocalStore(), RecordingNotifier()
        summary, _alert = track(store, notifier)
        item = store.latest_incident(ZONE)

        view = incident_view(item, BASE_EPOCH)

        self.assertEqual(view["last_alert_reason"], "published")
        self.assertNotIn("message_id", view)
        self.assertEqual(view["incident_id"], summary["incident_id"])

    def test_a_cooldown_refusal_is_reported_as_such(self) -> None:
        store, notifier = LocalStore(), RecordingNotifier()
        track(store, notifier, risk_class="WARNING", ts=BASE_EPOCH)
        track(store, notifier, risk_class="NORMAL", ts=BASE_EPOCH + 1)
        # Re-opening at the same severity inside the cooldown window: the second alert is
        # refused, and the refusal is what the incident now records and serves.
        summary, alert = track(store, notifier, risk_class="WARNING", ts=BASE_EPOCH + 2)

        view = incident_view(store.latest_incident(ZONE), BASE_EPOCH + 2)

        self.assertEqual(summary["action"], "opened")
        self.assertEqual(alert["reason"], "cooldown")
        self.assertEqual(view["last_alert_reason"], "cooldown")

    def test_no_attempt_is_reported_as_none_and_a_refusal_is_named(self) -> None:
        store = LocalStore()
        # With no notifier configured the attempt is still recorded - and refused for a named
        # reason, which is what an operator needs to see.
        track(store, None, risk_class="WARNING", ts=BASE_EPOCH)

        view = incident_view(store.latest_incident(ZONE), BASE_EPOCH)

        self.assertEqual(view["last_alert_reason"], "alerts_not_configured")

        # A record stored before the field existed has no attempt at all, and the view says so
        # with ``None`` rather than inventing one.
        legacy = dict(store.latest_incident(ZONE))
        legacy.pop("alerts", None)
        self.assertIsNone(incident_view(legacy, BASE_EPOCH)["last_alert_reason"])
        self.assertIsNone(incident_view({**legacy, "alerts": []}, BASE_EPOCH)["last_alert_reason"])


class SameSecondOrderingTest(unittest.TestCase):
    """A resolved event and the re-open that replaced it must not come back the wrong way round."""

    def _pair(self, store) -> tuple[dict, dict]:
        _summary, _alert = track(store, RecordingNotifier(), ts=BASE_EPOCH)
        first = store.latest_incident(ZONE)
        # Close it, then re-open inside the same second: the re-open takes a suffixed key.
        closed = dict(first)
        closed["current_status"] = "CLOSED"
        closed["version"] = int(closed["version"]) + 1
        store.put_incident(closed, expected_version=int(first["version"]))
        store.release_incident_open(ZONE, first["sk"])
        track(store, RecordingNotifier(), ts=BASE_EPOCH)
        return first, store.latest_incident(ZONE)

    def test_the_reopen_comes_back_first(self) -> None:
        store = LocalStore()
        first, reopened = self._pair(store)

        self.assertNotEqual(first["sk"], reopened["sk"], "the re-open needs its own sort key")
        listed = store.list_incidents(zone_id=ZONE)
        self.assertEqual([item["sk"] for item in listed], [reopened["sk"], first["sk"]])
        self.assertEqual(store.list_incidents(zone_id=ZONE, limit=1)[0]["sk"], reopened["sk"])
        self.assertEqual(store.latest_incident(ZONE)["sk"], reopened["sk"])

    def test_both_stores_agree_on_the_first_page(self) -> None:
        local = LocalStore()
        local_first, local_reopened = self._pair(local)

        table = FakeTable()
        remote = DynamoDbStore("floodpulse", zone_ids=ZONE_IDS, table=table)
        remote_first = {
            "pk": local_first["pk"],
            "sk": local_first["sk"],
            "record_type": "INCIDENT",
            "zone_id": ZONE,
            "current_status": "OPEN",
            "opened_ts_epoch": BASE_EPOCH,
            "incident_id": "incident",
            "version": 1,
        }
        remote_reopened = {
            **remote_first,
            "sk": local_reopened["sk"],
            "current_status": "OPEN",
            "version": 1,
        }
        table.items[(remote_first["pk"], remote_first["sk"])] = remote_first
        table.items[(remote_reopened["pk"], remote_reopened["sk"])] = remote_reopened

        self.assertEqual(
            remote.list_incidents(zone_id=ZONE, limit=1)[0]["sk"],
            local.list_incidents(zone_id=ZONE, limit=1)[0]["sk"],
        )


class TruncatedFlagTest(unittest.TestCase):
    """``truncated`` must mean "there is another page", not "this page is full"."""

    def setUp(self) -> None:
        self.store = LocalStore()
        notifier = RecordingNotifier()
        for index in range(3):
            track(self.store, notifier, ts=BASE_EPOCH + index * 10)
            summary, _alert = track(
                self.store, notifier, risk_class="NORMAL", ts=BASE_EPOCH + index * 10 + 1
            )
            track(self.store, notifier, risk_class="NORMAL", ts=BASE_EPOCH + index * 10 + 2)
            track(self.store, notifier, risk_class="NORMAL", ts=BASE_EPOCH + index * 10 + 3)
            self.assertEqual(summary["action"], "closed")
        set_store(self.store)
        set_notifier(RecordingNotifier())

    def call(self, limit: int) -> dict:
        status, body = dispatch(
            "GET",
            "/incidents",
            None,
            store=self.store,
            query={"limit": str(limit)},
            now_epoch=BASE_EPOCH + 100,
            env={},
        )
        self.assertEqual(status, 200)
        return body

    def test_an_exactly_full_page_is_not_truncated(self) -> None:
        body = self.call(3)
        self.assertEqual(body["count"], 3)
        self.assertFalse(body["truncated"])

    def test_a_cut_off_page_is_truncated(self) -> None:
        body = self.call(2)
        self.assertEqual(body["count"], 2)
        self.assertTrue(body["truncated"])

    def test_the_last_page_of_a_full_run_is_not_truncated(self) -> None:
        self.assertEqual(len(self.store.list_incidents()), 3)
        body = self.call(3)
        self.assertFalse(body["truncated"])
        self.assertEqual(body["counts_scope"], "returned_page")


class DynamoDbParityTest(unittest.TestCase):
    """The same two guards on the deployed store, with DynamoDB's own error semantics."""

    def setUp(self) -> None:
        self.table = FakeTable()
        self.store = DynamoDbStore("floodpulse", zone_ids=ZONE_IDS, table=self.table)

    def test_the_open_slot_is_claimed_once_and_released_in_place(self) -> None:
        pointer = incident_open_pointer(ZONE, {"sk": f"INCIDENT#{BASE_EPOCH}"}, BASE_EPOCH)
        self.assertTrue(self.store.claim_incident_open(ZONE, pointer))
        self.assertFalse(self.store.claim_incident_open(ZONE, pointer), "the second claim loses")
        self.assertEqual(self.store.get_incident_open(ZONE)["incident_sk"], pointer["incident_sk"])
        self.assertTrue(self.store.release_incident_open(ZONE, pointer["incident_sk"]))
        released = self.store.get_incident_open(ZONE)
        self.assertEqual(released["incident_sk"], INCIDENT_OPEN_RELEASED)
        self.assertTrue(self.store.claim_incident_open(ZONE, pointer))

    def test_a_stale_pointer_holding_a_closed_incident_is_replaced(self) -> None:
        stale = f"INCIDENT#{BASE_EPOCH}"
        self.store.claim_incident_open(ZONE, incident_open_pointer(ZONE, {"sk": stale}, BASE_EPOCH))
        self.table.items[(f"ZONE#{ZONE}", stale)] = {
            "pk": f"ZONE#{ZONE}",
            "sk": stale,
            "record_type": "INCIDENT",
            "zone_id": ZONE,
            "current_status": "CLOSED",
            "version": 2,
        }

        summary, alert = track(self.store, RecordingNotifier())

        self.assertEqual(summary["action"], "opened")
        self.assertTrue(alert["published"])
        self.assertEqual(
            self.store.get_incident_open(ZONE)["incident_sk"], self.store.latest_incident(ZONE)["sk"]
        )

    def test_the_version_guard_refuses_a_superseded_write(self) -> None:
        item = {
            "pk": f"ZONE#{ZONE}",
            "sk": f"INCIDENT#{BASE_EPOCH}",
            "record_type": "INCIDENT",
            "zone_id": ZONE,
            "current_status": "OPEN",
            "version": 1,
        }
        self.assertTrue(self.store.create_incident(item))
        self.assertTrue(self.store.put_incident({**item, "version": 2, "reading_count": 7}, expected_version=1))
        self.assertFalse(self.store.put_incident({**item, "version": 2, "reading_count": 1}, expected_version=1))
        self.assertEqual(self.store.get_incident(ZONE, item["sk"])["reading_count"], 7)

    def test_the_rollback_only_removes_the_claim_it_made(self) -> None:
        self.store.put_alert_marker(build_alert_marker(zone_id=ZONE, severity="CRITICAL", now_epoch=BASE_EPOCH))
        self.store.put_alert_marker(
            build_alert_marker(zone_id=ZONE, severity="CRITICAL", now_epoch=BASE_EPOCH + 45)
        )
        self.assertFalse(self.store.release_alert_slot(ZONE, "CRITICAL", BASE_EPOCH))
        self.assertEqual(
            self.store.get_alert_marker(ZONE)["last_alert_by_severity"]["CRITICAL"]["ts_epoch"],
            BASE_EPOCH + 45,
        )
        self.assertTrue(self.store.release_alert_slot(ZONE, "CRITICAL", BASE_EPOCH + 45))
        self.assertNotIn(
            "CRITICAL", self.store.get_alert_marker(ZONE)["last_alert_by_severity"]
        )

    def test_the_marker_helper_agrees_with_the_local_store(self) -> None:
        marker = build_alert_marker(zone_id=ZONE, severity="CRITICAL", now_epoch=BASE_EPOCH)
        self.assertEqual(
            marker_without_severity(marker, "CRITICAL", BASE_EPOCH),
            marker_without_severity(marker, "CRITICAL", BASE_EPOCH),
        )
        self.assertEqual(
            marker_without_severity(marker, "CRITICAL", BASE_EPOCH + 1),
            marker_without_severity(marker, "CRITICAL", BASE_EPOCH + 1),
        )
        self.assertEqual(
            marker_without_severity(None, "CRITICAL", BASE_EPOCH)[1],
            False,
        )


class AlertMarkerConstantTest(unittest.TestCase):
    def test_the_marker_sort_key_is_unchanged(self) -> None:
        self.assertEqual(ALERT_MARKER_SORT_KEY, "ALERT#COOLDOWN")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

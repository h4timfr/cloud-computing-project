"""The two history tools, offline: the audit's per-reading counters and the analysis table.

Both tools read the public history endpoint, so these tests replace the fetch with a canned body -
no AWS, no network, no wall clock. ``history_analysis`` must compute every metric through
``floodpulse.temporal`` rather than carrying a second copy of the rules, so its rows are compared
against the shared module's own result. A city that cannot be read, or a window that cannot
produce a value, must print ``-`` and carry ``null`` - never a number that was not measured.
"""

from __future__ import annotations

import io
import json
import urllib.error
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from floodpulse import temporal
from tools import history_analysis, history_audit

REPO = Path(__file__).resolve().parents[1]
SCRATCH = REPO / "build" / "test-scratch" / "m16-tools"
BASE_EPOCH = int(datetime(2026, 2, 14, 9, 0, 0, tzinfo=timezone.utc).timestamp())
API = "http://127.0.0.1:8199"
ZONE = {"zone_id": "city-chennai", "name": "Chennai", "zone_mode": "live_weather"}
READINGS = {
    "rainfall_mm_per_hr": 1.5,
    "soil_saturation_pct": 40.0,
    "drain_level_m": 0.5,
    "water_level_m": 1.0,
    "flow_rate_m3_s": 2.0,
}


def point(offset: int, score: float, readings: dict | None = READINGS) -> dict:
    """One history point; ``readings=None`` stores a point with no readings map at all."""
    snapshot = dict(readings) if readings is not None else {}
    stamp = BASE_EPOCH + offset
    return {
        "timestamp": datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat(),
        "ts_epoch": stamp,
        "risk_score": score,
        "risk_class": temporal.band_for(score),
        "risk_class_candidate": temporal.band_for(score),
        "readings": snapshot,
    }


def history_body(points: list[dict], minutes: int = 1440, *, since_epoch: int | None = None) -> dict:
    start = BASE_EPOCH if since_epoch is None else since_epoch
    return {
        "zone_id": "city-chennai",
        "name": "Chennai",
        "minutes": minutes,
        "since_epoch": start,
        "point_count": len(points),
        "points": points,
        "downsampled": False,
        "stride": 1,
        "metric_sources": {"rainfall_mm_per_hr": "open-meteo"},
        "model": "heuristic-v1",
        "zone_mode": "simulated",
        "data_classification": "simulated",
        "simulated": True,
    }


class FakeResponse:
    """The smallest thing ``fetch`` accepts as an HTTP response."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return self.payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_exc) -> bool:
        return False


def serving(body_for) -> mock._patch:
    """Patch a tool's ``fetch`` with a function of the URL."""

    def fake(url: str, timeout: float = 25.0):  # noqa: ARG001 - signature match
        result = body_for(url)
        if isinstance(result, Exception):
            raise result
        return result

    return mock.patch.object(history_analysis, "fetch", side_effect=fake)


def serialise(row: dict) -> str:
    """The row as JSON: ``None`` must serialise as ``null``, and nothing may be invented."""
    return json.dumps(row)


class HistoryAuditCountingTests(unittest.TestCase):
    def audit(self, body: dict, minutes: int = 1440) -> dict:
        with mock.patch.object(history_audit, "fetch", return_value=(body, 4096)):
            return history_audit.audit_city(API, ZONE, minutes)

    def test_the_nested_readings_are_the_source_of_the_counters(self) -> None:
        """The regression: these counters used to read metric names at the top level of a point."""
        rows = [
            point(0, 0.20),
            point(600, 0.30, {name: value for name, value in READINGS.items() if name != "rainfall_mm_per_hr"}),
            point(1200, 0.40, dict(READINGS, soil_saturation_pct=None)),
        ]
        row = self.audit(history_body(rows))
        self.assertEqual(row["missing_rainfall"], 1)
        self.assertEqual(
            row["missing_metrics"],
            {
                "rainfall_mm_per_hr": 1,
                "soil_saturation_pct": 1,
                "drain_level_m": 0,
                "water_level_m": 0,
                "flow_rate_m3_s": 0,
            },
        )

    def test_a_complete_nested_payload_reports_nothing_missing(self) -> None:
        row = self.audit(history_body([point(index * 600, 0.20 + index / 100) for index in range(4)]))
        self.assertEqual(row["missing_rainfall"], 0)
        self.assertEqual(set(row["missing_metrics"].values()), {0})

    def test_a_flattened_point_still_audits_as_present(self) -> None:
        """An older payload shape must not be reported as a data gap."""
        flat = {
            "ts_epoch": BASE_EPOCH,
            "risk_score": 0.2,
            **READINGS,
        }
        row = self.audit(history_body([flat]))
        self.assertEqual(row["missing_rainfall"], 0)
        self.assertEqual(set(row["missing_metrics"].values()), {0})

    def test_a_point_with_no_readings_map_counts_as_missing(self) -> None:
        row = self.audit(history_body([point(0, 0.20, None), point(600, 0.25)]))
        self.assertEqual(row["missing_rainfall"], 1)
        self.assertEqual(row["missing_metrics"]["drain_level_m"], 1)
        self.assertEqual(row["missing_risk_score"], 0)

    def test_the_counters_still_notice_a_missing_score(self) -> None:
        rows = [point(0, 0.20), {"ts_epoch": BASE_EPOCH + 60}, "not-a-point"]
        row = self.audit(history_body(rows))
        self.assertEqual(row["missing_risk_score"], 1)
        self.assertEqual(row["unparsed_rows"], 1, "only the row that is not a mapping is unusable")

    def test_payload_bytes_are_the_bytes_the_endpoint_delivered(self) -> None:
        raw = json.dumps(history_body([point(0, 0.20)])).encode("utf-8")
        with mock.patch.object(history_audit.urllib.request, "urlopen", return_value=FakeResponse(raw)):
            body, size = history_audit.fetch(f"{API}/zones/city-chennai/history?minutes=60")
        self.assertEqual(size, len(raw))
        self.assertEqual(body["point_count"], 1)

    def test_a_city_that_cannot_be_read_keeps_an_error_and_no_numbers(self) -> None:
        error = urllib.error.HTTPError("http://x", 503, "unavailable", None, None)
        with mock.patch.object(history_audit, "fetch", side_effect=error):
            row = history_audit.audit_city(API, ZONE, 1440)
        self.assertEqual(row["error"], "HTTP 503")
        self.assertNotIn("duration_seconds", row)
        self.assertNotIn("missing_metrics", row)

    def test_main_reports_the_reading_counters(self) -> None:
        body = history_body([point(0, 0.20), point(600, 0.30)])
        calls = {"zones": {"zones": [ZONE]}, "history": body}

        def body_for(url: str):
            return (calls["zones"], 512) if url.endswith("/zones") else (calls["history"], 1024)

        SCRATCH.mkdir(parents=True, exist_ok=True)
        target = SCRATCH / "audit.json"
        if target.exists():
            target.unlink()
        with mock.patch.object(history_audit, "fetch", side_effect=body_for):
            output = io.StringIO()
            with redirect_stdout(output):
                status = history_audit.main(["--api", API, "--json", str(target)])
        self.assertEqual(status, 0)
        written = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(written["city_count"], 1)
        self.assertEqual(written["cities"][0]["missing_metrics"]["rainfall_mm_per_hr"], 0)
        self.assertEqual(written["cities"][0]["payload_bytes"], 1024)
        self.assertIn("readings missing across 2 rows", output.getvalue())


class HistoryAnalysisTests(unittest.TestCase):
    def test_every_metric_comes_from_the_shared_module(self) -> None:
        """The tool imports the contract; it does not restate a second copy of the rules."""
        from floodpulse import temporal as shared

        self.assertIs(history_analysis.temporal, shared)
        rows = [
            point(0, 0.10),
            point(600, 0.16),
            point(1200, 0.22),
            point(1800, 0.31),
        ]
        body = history_body(rows)
        with serving(lambda url: body):
            row = history_analysis.city_row(API, ZONE, 1440)
        window_end = body["since_epoch"] + body["minutes"] * 60
        summary = shared.analyse(rows, now_epoch=window_end)
        self.assertEqual(row["samples"], summary.count)
        self.assertEqual(row["duration_seconds"], summary.duration_seconds)
        self.assertEqual(row["median_interval"], summary.median_interval)
        self.assertEqual(row["largest_gap"], summary.largest_gap)
        self.assertEqual(row["current_risk"], summary.last_score)
        self.assertEqual(row["min_risk"], summary.min)
        self.assertEqual(row["max_risk"], summary.peak)
        self.assertEqual(row["net_change"], summary.net_change)
        self.assertEqual(row["trend"], summary.trend)
        self.assertEqual(row["trend"], temporal.TREND_RISING)
        self.assertEqual(row["current_rainfall"], summary.newest_rainfall)
        self.assertEqual(row["max_rainfall"], summary.max_rainfall)
        self.assertEqual(row["invalid_samples"], summary.invalid_dropped)
        self.assertEqual(row["current_risk_class"], "NORMAL")
        self.assertEqual(row["status"], temporal.STATUS_OK)

    def test_the_window_end_is_the_end_of_the_served_window(self) -> None:
        """The moment being described is the end of the response's own window, not the local clock."""
        rows = [point(0, 0.20), point(600, 0.24), point(1200, 0.28)]
        body = history_body(rows, minutes=60, since_epoch=BASE_EPOCH)
        with serving(lambda url: body):
            row = history_analysis.city_row(API, ZONE, 60)
        self.assertEqual(row["window_end_epoch"], BASE_EPOCH + 3600)
        self.assertEqual(row["newest_age_seconds"], 3600 - 1200)

    def test_an_insufficient_window_reports_placeholders_not_numbers(self) -> None:
        body = history_body([point(0, 0.20)])
        with serving(lambda url: body):
            row = history_analysis.city_row(API, ZONE, 1440)
        self.assertEqual(row["samples"], 1)
        self.assertEqual(row["status"], temporal.STATUS_INSUFFICIENT)
        self.assertEqual(row["reason"], temporal.REASON_TOO_FEW_SAMPLES)
        self.assertIsNone(row["trend"])
        self.assertEqual(row["duration_seconds"], 0, "one sample spans no time at all")
        self.assertIsNone(row["median_interval"])
        self.assertEqual(row["net_change"], 0.0, "one sample changed by nothing measurable")
        self.assertEqual(row["current_risk"], 0.20, "a single sample is still a real measurement")
        self.assertEqual(row["min_risk"], 0.20)
        self.assertEqual(history_analysis.cell(row, "trend", None), "-")
        self.assertEqual(history_analysis.cell(row, "median_interval", 0), "-")
        self.assertEqual(history_analysis.cell(row, "current_risk", 4), "0.2000")
        self.assertIn('"trend": null', serialise(row))

    def test_an_empty_window_reports_no_samples(self) -> None:
        body = history_body([])
        with serving(lambda url: body):
            row = history_analysis.city_row(API, ZONE, 1440)
        self.assertEqual(row["samples"], 0)
        self.assertEqual(row["reason"], temporal.REASON_NO_SAMPLES)
        for key, _heading, decimals in history_analysis.COLUMNS:
            with self.subTest(column=key):
                if key in ("samples", "invalid_samples"):
                    self.assertEqual(row[key], 0)
                else:
                    self.assertEqual(history_analysis.cell(row, key, decimals), "-")

    def test_a_failed_read_is_reported_and_never_given_numbers(self) -> None:
        error = urllib.error.HTTPError("http://x", 503, "unavailable", None, None)
        with serving(lambda url: error):
            row = history_analysis.city_row(API, ZONE, 1440)
        self.assertEqual(row["error"], "HTTP 503")
        for key, _heading, decimals in history_analysis.COLUMNS:
            with self.subTest(column=key):
                self.assertIsNone(row[key])
                self.assertEqual(history_analysis.cell(row, key, decimals), "-")
        self.assertIn('"samples": null', serialise(row))

    def test_a_body_that_cannot_be_analysed_is_reported(self) -> None:
        with serving(lambda url: {"since_epoch": "not-a-number", "points": []}):
            row = history_analysis.city_row(API, ZONE, 1440)
        self.assertEqual(row["error"], "ValueError")
        self.assertIsNone(row["samples"])

    def test_an_unusable_row_is_counted_as_an_invalid_sample(self) -> None:
        rows = [
            point(0, 0.20),
            point(600, 0.30),
            point(1200, 1.5),  # out of range: dropped, never guessed at
            point(1800, 0.35),
        ]
        body = history_body(rows)
        with serving(lambda url: body):
            row = history_analysis.city_row(API, ZONE, 1440)
        self.assertEqual(row["invalid_samples"], 1)
        self.assertEqual(row["samples"], 3)
        self.assertEqual(row["max_risk"], 0.35)

    def test_the_table_and_json_carry_the_same_numbers(self) -> None:
        """A thin window prints ``-`` and carries ``null``; a complete one prints its trend."""
        other = {"zone_id": "city-kochi", "name": "Kochi", "zone_mode": "live_weather"}
        healthy = history_body([point(index * 600, 0.20 + index * 0.05) for index in range(4)])
        thin = history_body([point(0, 0.20)])
        calls = {"zones": {"zones": [ZONE, other]}, "history": [thin, healthy]}

        def body_for(url: str):
            return calls["zones"] if url.endswith("/zones") else calls["history"].pop(0)

        SCRATCH.mkdir(parents=True, exist_ok=True)
        target = SCRATCH / "analysis.json"
        if target.exists():
            target.unlink()
        with mock.patch.object(history_analysis, "fetch", side_effect=body_for):
            output = io.StringIO()
            with redirect_stdout(output):
                status = history_analysis.main(["--api", API, "--minutes", "1440", "--json", str(target)])
        self.assertEqual(status, 0)
        written = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(written["city_count"], 2)
        thin_row, healthy_row = written["cities"]
        self.assertIsNone(thin_row["trend"])
        self.assertEqual(thin_row["samples"], 1)
        self.assertEqual(healthy_row["trend"], temporal.TREND_RISING)
        self.assertEqual(healthy_row["samples"], 4)
        text = output.getvalue()
        self.assertIn("RISING", text)
        self.assertRegex(text, r"(?m)^Chennai\s+1\s.*\s-\s")  # the thin city prints a placeholder
        self.assertIn("1 rising", text)
        self.assertIn("1 unknown", text)

    def test_the_requested_window_and_api_reach_the_url(self) -> None:
        seen: list[str] = []

        def body_for(url: str):
            seen.append(url)
            return {"zones": [ZONE]} if url.endswith("/zones") else history_body([point(0, 0.2)])

        with mock.patch.object(history_analysis, "fetch", side_effect=body_for):
            with redirect_stdout(io.StringIO()):
                history_analysis.main(["--api", API + "/", "--minutes", "60", "--quiet"])
        self.assertEqual(seen[0], f"{API}/zones")
        self.assertEqual(seen[1], f"{API}/zones/city-chennai/history?minutes=60")

    def test_a_city_without_a_name_is_labelled_by_its_id(self) -> None:
        body = history_body([point(0, 0.2)])
        with serving(lambda url: body):
            row = history_analysis.city_row(API, {"zone_id": "city-chennai"}, 1440)
        self.assertEqual(row["name"], "city-chennai")


class ReadOnlyToolTests(unittest.TestCase):
    def test_both_tools_only_ever_read(self) -> None:
        """They are evidence-gathering scripts: a write would make their numbers untrustworthy."""
        for module in (history_analysis, history_audit):
            text = Path(module.__file__).read_text(encoding="utf-8")
            with self.subTest(tool=module.__name__):
                self.assertIn("urlopen", text)
                self.assertNotIn("data=", text, "a request with a body is not a read")
                for name in ("boto3", "put_item", "delete_item", "POST", "PATCH", "DELETE"):
                    self.assertNotIn(name, text)

    def test_the_analysis_tool_documents_the_methodology_in_its_own_docstring(self) -> None:
        """The rules live with the code that applies them; the README is not the contract."""
        text = history_analysis.__doc__ or ""
        for phrase in (
            "valid",
            "newest sample",
            "net change",
            "earlier row wins",
            "least-squares slope",
            "0.02 per hour",
            "at least 3 valid samples spanning at least 600 s",
            "max(3 x median interval, 900 s)",
            "later one wins",
            "out-of-range score is dropped",
            "risk.classify",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)


if __name__ == "__main__":  # pragma: no cover - convenience only
    unittest.main()

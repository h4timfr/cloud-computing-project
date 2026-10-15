"""Tests for the local tooling added in M2 (no AWS, no network).

The important guarantee here is that the ingestion secret is never printed, never
committed and never sent anywhere except as the ``x-fp-key`` header on ``POST /readings``.

Scratch files are written to a fixed directory under the repository's gitignored
``build/`` instead of the OS temp directory: the file sandbox refuses the chmod/rmtree
sequence that ``tempfile.TemporaryDirectory`` performs on cleanup.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from tools.init_ingest_key import ENV_VAR, fingerprint, main as init_main, read_key, write_lambda_env
from tools.live_simulator import KEY_HEADER, post_reading, resolve_key
from tools.storm_demo import STAGES, band_counts, export_story, main as storm_main, run_arc, verify_arc

SECRET = "0123456789abcdef" * 4

SCRATCH_ROOT = Path(__file__).resolve().parent.parent / "build" / "test-scratch"


def scratch(name: str) -> Path:
    path = SCRATCH_ROOT / name
    path.mkdir(parents=True, exist_ok=True)
    return path


class KeyFileTests(unittest.TestCase):
    def test_read_key_missing_blank_and_present(self) -> None:
        path = scratch("read-key") / ".floodpulse-key"
        if path.exists():
            path.unlink()
        self.assertIsNone(read_key(path))
        path.write_text("   \n", encoding="utf-8")
        self.assertIsNone(read_key(path))
        path.write_text(SECRET + "\n", encoding="utf-8")
        self.assertEqual(read_key(path), SECRET)

    def test_fingerprint_is_hashed_and_stable(self) -> None:
        first = fingerprint(SECRET)
        self.assertEqual(first, fingerprint(SECRET))
        self.assertTrue(first.startswith("sha256:"))
        self.assertEqual(len(first), len("sha256:") + 12)
        self.assertNotEqual(first, fingerprint(SECRET + "x"))
        self.assertNotIn(SECRET, first)

    def test_lambda_env_contains_the_secret_and_the_store_config(self) -> None:
        path = scratch("lambda-env") / "env-prod.json"
        write_lambda_env(path, SECRET, "floodpulse")
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            document["Variables"],
            {"FLOODPULSE_STORE": "dynamodb", "FLOODPULSE_TABLE": "floodpulse", ENV_VAR: SECRET},
        )

    def test_cli_never_prints_the_secret(self) -> None:
        directory = scratch("cli-secret")
        key_path = directory / ".floodpulse-key"
        env_path = directory / "env-prod.json"
        if key_path.exists():
            key_path.unlink()
        argv = [
            "init_ingest_key.py",
            "--key-file", str(key_path),
            "--lambda-env", str(env_path),
            "--show-fingerprint",
        ]
        buffer = io.StringIO()
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(buffer):
            self.assertEqual(init_main(), 0)
        output = buffer.getvalue()
        generated = read_key(key_path)
        self.assertIsNotNone(generated)
        self.assertNotIn(generated, output)
        self.assertIn(str(key_path), output)
        self.assertIn("sha256:", output)
        self.assertIn(str(env_path), output)

    def test_cli_refuses_to_rotate_silently(self) -> None:
        key_path = scratch("cli-rotate") / ".floodpulse-key"
        key_path.write_text(SECRET, encoding="utf-8")

        with mock.patch.object(sys, "argv", ["init_ingest_key.py", "--key-file", str(key_path)]):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    init_main()
        self.assertEqual(read_key(key_path), SECRET)  # untouched

        with mock.patch.object(sys, "argv", ["init_ingest_key.py", "--key-file", str(key_path), "--reuse"]):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(init_main(), 0)
        self.assertEqual(read_key(key_path), SECRET)


class ResolveKeyTests(unittest.TestCase):
    def test_precedence_explicit_env_file(self) -> None:
        path = scratch("resolve") / ".floodpulse-key"
        path.write_text("from-file", encoding="utf-8")
        with mock.patch.dict(os.environ, {"FLOODPULSE_INGEST_KEY": "from-env"}):
            self.assertEqual(resolve_key(path, "explicit"), "explicit")
            self.assertEqual(resolve_key(path, None), "from-env")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLOODPULSE_INGEST_KEY", None)
            self.assertEqual(resolve_key(path, None), "from-file")

    def test_missing_everywhere_is_an_error(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLOODPULSE_INGEST_KEY", None)
            with self.assertRaises(SystemExit):
                resolve_key(scratch("resolve-missing") / "missing", None)


class PostReadingTests(unittest.TestCase):
    class FakeResponse:
        def __init__(self, status: int, body: bytes) -> None:
            self.status = status
            self._body = body

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

    def test_posts_json_with_the_key_header(self) -> None:
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["method"] = request.get_method()
            captured["headers"] = {key.lower(): value for key, value in request.header_items()}
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return self.FakeResponse(202, json.dumps({"status": "accepted"}).encode("utf-8"))

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            status, payload = post_reading("https://example.test/base/", SECRET, {"zone_id": "z"}, 5.0)

        self.assertEqual(status, 202)
        self.assertEqual(payload, {"status": "accepted"})
        self.assertEqual(captured["url"], "https://example.test/base/readings")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["headers"][KEY_HEADER], SECRET)
        self.assertEqual(captured["headers"]["content-type"], "application/json")
        self.assertEqual(captured["body"], {"zone_id": "z"})

    def test_http_error_is_reported_without_raising(self) -> None:
        def fake_urlopen(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"error":"unauthorized"}')
            )

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            status, payload = post_reading("https://example.test", SECRET, {}, 5.0)
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"], "unauthorized")

    def test_connection_failure_is_reported_as_status_zero(self) -> None:
        def fake_urlopen(request, timeout=None):
            raise urllib.error.URLError("refused")

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            status, payload = post_reading("https://example.test", SECRET, {}, 5.0)
        self.assertEqual(status, 0)
        self.assertEqual(payload["error"], "connection_failed")


class StormDemoTests(unittest.TestCase):
    """The storm demo is the rehearsed story; if it ever stops working, the demo is a lie."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.story = run_arc(steps=100)

    def test_the_full_arc_reproduces_every_stage_in_order(self) -> None:
        self.assertEqual(verify_arc(self.story), [], "the demo arc is not demo-ready")
        steps = {name: (self.story["stages"][name] or {}).get("step") for name in STAGES}
        for name in STAGES:
            self.assertIsNotNone(steps[name], f"stage {name} is missing from the arc")
        self.assertLess(steps["NORMAL"], steps["WATCH"])
        self.assertLessEqual(steps["WATCH"], steps["WARNING"])
        self.assertLessEqual(steps["WARNING"], steps["CRITICAL"])
        self.assertLess(steps["CRITICAL"], steps["RECOVERY"])
        self.assertLessEqual(steps["RECOVERY"], steps["CLOSED"])
        self.assertGreaterEqual(self.story["stages"]["CRITICAL"]["open_incidents"], 1)
        self.assertEqual(self.story["stages"]["CLOSED"]["open_incidents"], 0)
        self.assertGreaterEqual(self.story["stages"]["CLOSED"]["closed_incidents"], 1)

    def test_a_short_arc_is_reported_as_incomplete(self) -> None:
        # negative control: the verifier must fail when the story did not actually happen
        problems = verify_arc(run_arc(steps=25))
        self.assertTrue(problems, "an arc that never floods must not pass verification")
        self.assertTrue(any("CRITICAL" in problem for problem in problems), problems)
        self.assertTrue(any("peak score" in problem for problem in problems), problems)

    def test_the_demo_fleet_is_never_the_live_fleet(self) -> None:
        for snapshot in (self.story["stages"][name] for name in STAGES):
            self.assertEqual(snapshot["payload"]["zone_mode"], "simulated")
            self.assertEqual(snapshot["payload"]["data_classification"], "simulated")
            self.assertEqual(len(snapshot["payload"]["zones"]), 3)

    def test_a_zone_without_a_reading_is_not_counted_as_normal(self) -> None:
        counts = band_counts({"zones": [{"latest": None}, {"latest": {"risk_class": "FLOODING"}}]})
        self.assertEqual(counts["NO_READING"], 2)
        self.assertEqual(counts["NORMAL"], 0)

    def test_export_binds_each_history_window_to_the_stage_leader(self) -> None:
        directory = scratch("storm-story")
        for existing in directory.glob("*.json"):
            existing.unlink()
        written = export_story(self.story, directory)
        self.assertTrue(written)
        summary = json.loads((directory / "story.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["data_classification"], "simulated")
        self.assertIn("not a validated hydrological model", summary["disclaimer"])
        for row in summary["stages"]:
            slug = row["stage"].lower()
            zones = json.loads((directory / f"zones-{slug}.json").read_text(encoding="utf-8"))
            history = json.loads((directory / f"history-{slug}.json").read_text(encoding="utf-8"))
            self.assertEqual(zones["highest_risk_zone"]["zone_id"], row["leader"])
            self.assertEqual(history["zone_id"], row["leader"])
            self.assertGreater(history["point_count"], 0, f"{row['stage']} exported an empty history")

    def test_cli_exit_codes_reflect_demo_reliability(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(storm_main(["--steps", "100", "--quiet"]), 0)
            self.assertIn("demo reliability check: PASSED", buffer.getvalue())
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(storm_main(["--steps", "25", "--quiet"]), 1)
            self.assertIn("demo reliability check: FAILED", buffer.getvalue())

    def test_json_output_is_machine_readable_and_carries_the_problems(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(storm_main(["--steps", "25", "--json"]), 1)
        document = json.loads(buffer.getvalue())
        self.assertEqual(list(document["stages"]), list(STAGES))
        self.assertTrue(document["problems"])
        self.assertIsNone(document["stages"]["CRITICAL"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

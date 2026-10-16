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

from tools import init_ingest_key as init_ingest_key_module
from tools.init_ingest_key import (
    ENV_VAR,
    EnvDocumentError,
    fingerprint,
    main as init_main,
    read_key,
    write_lambda_env,
)
from tools.live_simulator import DRIFT_WARN_SECONDS, KEY_HEADER, clock_drift, post_reading, resolve_key
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
        if path.exists():  # start from "no document yet", or a merge would preserve its contents
            path.unlink()
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
        for path in (key_path, env_path):
            if path.exists():
                path.unlink()
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


class IngestKeyEnvDocumentTests(unittest.TestCase):
    """The environment document is merged, never replaced (M7 hardening).

    A freshly generated three-variable document applied over the real deployment would drop
    zone mode, weather settings, the alert cooldown and the SNS topic, so every one of these
    tests is about *preserving* what is already there — and failing closed when the file
    cannot be understood.
    """

    NEW_KEY = "abcdef0123456789" * 4

    def path(self, name: str) -> Path:
        target = scratch(name) / "env-prod.json"
        if target.exists():
            target.unlink()
        return target

    def write(self, target: Path, document: object) -> bytes:
        text = document if isinstance(document, str) else json.dumps(document, indent=2)
        target.write_text(text, encoding="utf-8")
        return target.read_bytes()

    def deployment_document(self) -> dict[str, object]:
        return {"Variables": {
            "FLOODPULSE_ALERT_COOLDOWN_SECONDS": "600",
            "FLOODPULSE_INGEST_KEY": "0" * 64,
            "FLOODPULSE_SNS_TOPIC_ARN": "arn:aws:sns:eu-north-1:725934295966:floodpulse-alerts",
            "FLOODPULSE_STORE": "dynamodb",
            "FLOODPULSE_TABLE": "floodpulse",
            "FLOODPULSE_WEATHER": "open-meteo",
            "FLOODPULSE_WEATHER_REFRESH_SECONDS": "600",
            "FLOODPULSE_WEATHER_TIMEOUT_SECONDS": "4",
            "FLOODPULSE_ZONE_MODE": "live",
        }}

    def test_an_existing_nine_variable_document_keeps_all_nine(self) -> None:
        target = self.path("env-merge-nine")
        self.write(target, self.deployment_document())
        report = write_lambda_env(target, self.NEW_KEY, "floodpulse")
        variables = json.loads(target.read_text(encoding="utf-8"))["Variables"]
        self.assertEqual(len(variables), 9)
        self.assertEqual(report["variables"], 9)
        self.assertTrue(report["changed"])

    def test_only_the_ingestion_key_changes(self) -> None:
        target = self.path("env-merge-only-key")
        before = self.deployment_document()["Variables"]
        self.write(target, self.deployment_document())
        write_lambda_env(target, self.NEW_KEY, "floodpulse")
        after = json.loads(target.read_text(encoding="utf-8"))["Variables"]
        self.assertEqual(variables_changed := sorted(
            name for name in set(before) | set(after) if before.get(name) != after.get(name)
        ), [ENV_VAR], f"unexpected changes: {variables_changed}")
        self.assertEqual(after[ENV_VAR], self.NEW_KEY)

    def test_unrelated_values_survive_byte_for_byte(self) -> None:
        target = self.path("env-merge-values")
        before = self.deployment_document()["Variables"]
        self.write(target, self.deployment_document())
        write_lambda_env(target, self.NEW_KEY, "floodpulse")
        after = json.loads(target.read_text(encoding="utf-8"))["Variables"]
        self.assertEqual({k: v for k, v in after.items() if k != ENV_VAR},
                         {k: v for k, v in before.items() if k != ENV_VAR})

    def test_malformed_json_is_rejected_and_the_file_is_untouched(self) -> None:
        target = self.path("env-bad-json")
        original = self.write(target, '{"Variables": {"FLOODPULSE_INGEST_KEY": "x"},,')
        with self.assertRaises(EnvDocumentError):
            write_lambda_env(target, self.NEW_KEY, "floodpulse")
        self.assertEqual(target.read_bytes(), original)

    def test_a_missing_variables_object_is_rejected(self) -> None:
        target = self.path("env-no-variables")
        original = self.write(target, {"Other": {"FLOODPULSE_INGEST_KEY": "x"}})
        with self.assertRaises(EnvDocumentError):
            write_lambda_env(target, self.NEW_KEY, "floodpulse")
        self.assertEqual(target.read_bytes(), original)

    def test_variables_that_is_not_an_object_is_rejected(self) -> None:
        for index, bad in enumerate(({"Variables": ["not", "an", "object"]},
                                     {"Variables": "a string"},
                                     {"Variables": 7},
                                     {"Variables": None})):
            with self.subTest(document=bad):
                target = self.path(f"env-bad-variables-{index}")
                original = self.write(target, bad)
                with self.assertRaises(EnvDocumentError):
                    write_lambda_env(target, self.NEW_KEY, "floodpulse")
                self.assertEqual(target.read_bytes(), original)

    def test_an_empty_file_is_rejected_rather_than_guessed(self) -> None:
        target = self.path("env-empty")
        original = self.write(target, "   \n")
        with self.assertRaises(EnvDocumentError):
            write_lambda_env(target, self.NEW_KEY, "floodpulse")
        self.assertEqual(target.read_bytes(), original)

    def test_new_file_creation_writes_the_bootstrap_document(self) -> None:
        target = self.path("env-create")
        self.assertFalse(target.exists())
        report = write_lambda_env(target, self.NEW_KEY, "floodpulse")
        document = json.loads(target.read_text(encoding="utf-8"))
        self.assertTrue(report["created"])
        self.assertEqual(document, {"Variables": {
            "FLOODPULSE_STORE": "dynamodb", "FLOODPULSE_TABLE": "floodpulse", ENV_VAR: self.NEW_KEY}})
        self.assertEqual(report["preserved"], [])

    def test_rewriting_the_same_key_changes_nothing(self) -> None:
        target = self.path("env-idempotent")
        self.write(target, self.deployment_document())
        write_lambda_env(target, self.NEW_KEY, "floodpulse")
        first = target.read_bytes()
        report = write_lambda_env(target, self.NEW_KEY, "floodpulse")
        self.assertFalse(report["changed"])
        self.assertEqual(target.read_bytes(), first)

    def test_written_document_is_what_the_aws_cli_expects(self) -> None:
        target = self.path("env-shape")
        self.write(target, self.deployment_document())
        write_lambda_env(target, self.NEW_KEY, "floodpulse")
        raw = target.read_text(encoding="utf-8")
        self.assertFalse(raw.startswith("\ufeff"), "a BOM breaks the AWS CLI --environment file:// form")
        document = json.loads(raw)
        self.assertEqual(set(document), {"Variables"})
        for name, value in document["Variables"].items():
            self.assertIsInstance(value, str, f"{name} must be a string for the AWS CLI")

    def test_non_string_values_are_reported_but_not_altered(self) -> None:
        target = self.path("env-non-string")
        self.write(target, {"Variables": {"FLOODPULSE_INGEST_KEY": "0" * 64,
                                          "FLOODPULSE_ALERT_COOLDOWN_SECONDS": 600}})
        report = write_lambda_env(target, self.NEW_KEY, "floodpulse")
        self.assertEqual(report["non_string"], ["FLOODPULSE_ALERT_COOLDOWN_SECONDS"])
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["Variables"]
                         ["FLOODPULSE_ALERT_COOLDOWN_SECONDS"], 600)

    def test_generated_keys_are_cryptographically_random(self) -> None:
        directory = scratch("key-entropy")
        generated = []
        for index in range(2):
            key_path = directory / f"probe-{index}.key"
            if key_path.exists():
                key_path.unlink()
            with mock.patch.object(sys, "argv", ["init_ingest_key.py", "--key-file", str(key_path)]):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(init_main(), 0)
            value = read_key(key_path)
            self.assertIsNotNone(value)
            assert value is not None
            self.assertRegex(value, r"^[0-9a-f]{64}$")  # 256 bits, hex encoded
            generated.append(value)
        self.assertNotEqual(generated[0], generated[1])
        source = Path(init_ingest_key_module.__file__).read_text(encoding="utf-8")
        self.assertIn("secrets.token_hex", source)
        self.assertNotIn("random.", source, "the secret must come from secrets, not random")

    def test_cli_prints_neither_the_secret_nor_a_preserved_value(self) -> None:
        directory = scratch("env-cli-quiet")
        key_path = directory / ".floodpulse-key"
        env_path = directory / "env-prod.json"
        for path in (key_path, env_path):
            if path.exists():
                path.unlink()
        document = self.deployment_document()["Variables"]
        self.write(env_path, {"Variables": document})
        out, err = io.StringIO(), io.StringIO()
        argv = ["init_ingest_key.py", "--key-file", str(key_path),
                "--lambda-env", str(env_path), "--show-fingerprint"]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(init_main(), 0)
        combined = out.getvalue() + err.getvalue()
        fresh = read_key(key_path)
        self.assertIsNotNone(fresh)
        self.assertNotIn(fresh or "", combined)
        self.assertNotIn(document[ENV_VAR], combined)          # the replaced value
        self.assertNotIn(document["FLOODPULSE_SNS_TOPIC_ARN"], combined)
        self.assertIn("sha256:", combined)                     # fingerprint only
        self.assertIn("preserved", combined)                   # names, not values
        self.assertEqual(len(json.loads(env_path.read_text(encoding="utf-8"))["Variables"]), 9)

    def test_dry_run_writes_nothing_at_all(self) -> None:
        directory = scratch("env-dry-run")
        key_path = directory / ".floodpulse-key"
        env_path = directory / "env-prod.json"
        for path in (key_path, env_path):
            if path.exists():
                path.unlink()
        env_bytes = self.write(env_path, {"Variables": self.deployment_document()["Variables"]})
        buffer = io.StringIO()
        argv = ["init_ingest_key.py", "--key-file", str(key_path), "--lambda-env", str(env_path),
                "--force", "--dry-run", "--show-fingerprint"]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(buffer):
            self.assertEqual(init_main(), 0)
        self.assertFalse(key_path.exists(), "a dry run must not create the key file")
        self.assertEqual(env_path.read_bytes(), env_bytes, "a dry run must not touch the env file")
        self.assertIn("dry run", buffer.getvalue())

    def test_a_bad_environment_file_does_not_rotate_the_key_file(self) -> None:
        directory = scratch("env-fail-closed")
        key_path = directory / ".floodpulse-key"
        env_path = directory / "env-prod.json"
        for path in (key_path, env_path):
            if path.exists():
                path.unlink()
        original = self.write(env_path, '{"Variables": [,')
        argv = ["init_ingest_key.py", "--key-file", str(key_path), "--lambda-env", str(env_path), "--force"]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                init_main()
        self.assertIn("refusing to continue", str(caught.exception))
        self.assertFalse(key_path.exists(), "the key file must not be rotated when the env file is unusable")
        self.assertEqual(env_path.read_bytes(), original)


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


class ClockDriftTests(unittest.TestCase):
    """The simulator's simulated clock must not creep past the API's skew guard."""

    def test_on_cadence_means_no_drift(self) -> None:
        # one step per interval: the simulated clock tracks real time exactly
        self.assertEqual(clock_drift(elapsed_seconds=40.0, steps_done=9, interval=5.0), 0.0)

    def test_a_step_slower_than_the_cadence_reports_how_far_behind_it_is(self) -> None:
        # 10 steps in 110 s at a 5 s cadence: 45 s of simulated span, so 65 s behind
        self.assertAlmostEqual(clock_drift(elapsed_seconds=110.0, steps_done=10, interval=5.0), 65.0, places=6)

    def test_drift_is_signed_so_a_clock_ahead_is_visible_too(self) -> None:
        self.assertAlmostEqual(clock_drift(elapsed_seconds=10.0, steps_done=10, interval=5.0), -35.0, places=6)

    def test_the_first_reading_cannot_drift(self) -> None:
        self.assertEqual(clock_drift(elapsed_seconds=1.5, steps_done=1, interval=5.0), 1.5)

    def test_the_warning_threshold_leaves_room_before_the_api_refuses(self) -> None:
        self.assertLess(DRIFT_WARN_SECONDS, 300.0)

    def test_the_loop_sleeps_only_the_remainder_of_the_cadence(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "tools" / "live_simulator.py").read_text(encoding="utf-8")
        self.assertIn("args.interval - (time.time() - step_started)", source,
                      "the loop must not sleep the whole cadence on top of the work it just did")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

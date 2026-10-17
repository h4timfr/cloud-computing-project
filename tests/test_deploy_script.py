"""Contracts for the deployment script and its offline logic verifier.

``infra/deploy.ps1`` is the one file that can change the live stack, and the one file the suite
cannot simply execute: a real run needs credentials and an account. So the script's safety logic
is verified offline by ``tools/deploy_logic_check.ps1``, which lifts the helper functions out of
it with the PowerShell parser, stubs every AWS entry point, and exercises them - and this file
runs that verifier and adds the structural checks that only make sense on the source text.

Neither layer proves a deploy works end to end. Only a real deploy does that. What they prove is
that the structures which make the dangerous operations safe are still there, so a later edit
cannot quietly remove one.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "infra" / "deploy.ps1"
HARNESS = REPO / "tools" / "deploy_logic_check.ps1"


def powershell() -> str | None:
    for name in ("pwsh", "powershell"):
        found = shutil.which(name)
        if found:
            return found
    return None


class ScriptText(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = SCRIPT.read_text(encoding="utf-8")
        cls.lines = cls.raw.splitlines()
        cls.code = [line for line in cls.lines
                    if line.strip() and not line.lstrip().startswith("#")]

    def body_of(self, function: str) -> str:
        match = re.search(
            rf"function {re.escape(function)}\b(.*?)(?=\nfunction |\Z)", self.raw, re.DOTALL
        )
        self.assertIsNotNone(match, f"{function} has no definition")
        return match.group(1)


class SwitchTests(ScriptText):
    """The switches an operator needs to deploy safely, or to see what a deploy would do."""

    def test_every_switch_is_declared(self) -> None:
        for switch in ("$DryRun", "$AllowDirtyTree", "$AllowKeyDrift", "$KeepUnknownEnvVars"):
            with self.subTest(switch=switch):
                self.assertIn(f"[switch]{switch}", self.raw)

    def test_the_dry_run_actually_gates_something(self) -> None:
        self.assertGreaterEqual(self.raw.count("$DryRun"), 10)

    def test_the_header_says_how_to_run_it(self) -> None:
        header = "\n".join(self.lines[:120])
        for switch in ("-DryRun", "-AllowDirtyTree", "-AllowKeyDrift"):
            with self.subTest(switch=switch):
                self.assertIn(switch, header, f"{switch} is not documented in the script header")

    def test_the_header_still_promises_only_these_four_resources(self) -> None:
        header = "\n".join(self.lines[:120])
        for resource in ("DynamoDB table", "Lambda function", "API Gateway HTTP API", "S3"):
            with self.subTest(resource=resource):
                self.assertIn(resource, header)


class WritePathTests(ScriptText):
    """Every write goes through one guarded path: -DryRun cannot write, and no write is silent."""

    def test_the_write_helper_checks_the_dry_run_itself(self) -> None:
        self.assertIn("$DryRun", self.body_of("Invoke-AwsWrite"),
                      "Invoke-AwsWrite does not check -DryRun")

    def test_the_json_writer_delegates_to_the_guarded_path(self) -> None:
        body = self.body_of("Invoke-AwsWriteJson")
        self.assertIn("Invoke-AwsWrite", body, "the JSON writer bypasses the dry-run gate")

    def test_the_waiter_is_skipped_in_a_dry_run(self) -> None:
        self.assertIn("$DryRun", self.body_of("Wait-AwsState"),
                      "a dry run would still wait for a state change that cannot happen")

    def test_local_json_files_are_not_written_in_a_dry_run(self) -> None:
        self.assertIn("$DryRun", self.body_of("Write-JsonFile"),
                      "a dry run would still write the build files - including the secret's")

    def test_no_aws_command_is_invoked_outside_the_helpers(self) -> None:
        # A statement that *starts* with the AWS CLI is an invocation; the same words inside a
        # printed message are instructions to the operator, not code.
        offenders = []
        for number, line in enumerate(self.code, start=1):
            stripped = line.strip().lstrip("&").strip()
            if not stripped.lower().startswith("aws "):
                continue
            if re.search(r"\baws\s+\w[\w-]*\s+(create|update|put|delete|add|set|tag|publish)-", stripped):
                offenders.append(f"line {number}: {stripped}")
        self.assertEqual(offenders, [], "a mutating AWS command bypasses the guarded helpers")


class FailureTests(ScriptText):
    """A condition that makes the deployment unable to work must stop it, not warn about it."""

    def test_write_fail_aborts(self) -> None:
        self.assertRegex(self.body_of("Write-Fail"), r"\bthrow\b", "Write-Fail does not stop the run")

    def test_a_missing_ingestion_key_is_fatal(self) -> None:
        # the check that matters is the one on the *environment file's* names, immediately before
        # the deploy would apply it
        marker = "-notcontains 'FLOODPULSE_INGEST_KEY'"
        self.assertIn(marker, self.raw, "the environment file's variables are no longer checked")
        after = self.raw[self.raw.index(marker):]
        first_fail = after.find("Write-Fail")
        self.assertGreaterEqual(first_fail, 0, "a missing ingestion key no longer stops the run")
        self.assertLess(first_fail, 400, "the failure is suspiciously far from the check itself")

    def test_key_drift_is_fatal_unless_the_switch_is_passed(self) -> None:
        drift = self.raw[self.raw.index("$AllowKeyDrift", self.raw.index("param(")):]
        self.assertIn("Write-Fail", drift[:6000])

    def test_there_is_a_helper_that_tells_missing_from_broken(self) -> None:
        self.assertIn("function Test-AwsResource", self.raw)
        self.assertIn("NotFound", self.raw)
        self.assertIn("NoSuch", self.raw)


class SecretHygieneTests(ScriptText):
    """Nothing derived from CLI output may reach the console without passing the redaction gate."""

    def test_the_redaction_machinery_exists(self) -> None:
        for helper in ("Register-Secret", "Remove-SecretText", "Get-SecretFingerprint"):
            with self.subTest(helper=helper):
                self.assertIn(f"function {helper}", self.raw)

    def test_every_aws_failure_is_redacted_before_it_is_reported(self) -> None:
        self.assertIn("Remove-SecretText", self.body_of("Invoke-Aws"),
                      "an AWS CLI failure is printed unredacted")

    def test_messages_never_interpolate_a_secret_bearing_variable(self) -> None:
        # The variables may be *named* in code; their values must not end up in a printed string.
        suspicious = []
        for number, line in enumerate(self.code, start=1):
            if not re.search(r"Write-\w+\s", line):
                continue
            if re.search(r"\$\w*([Kk]eyValue|[Ss]ecretValue|[Rr]awKey|[Kk]eyText)\w*", line):
                suspicious.append(f"line {number}: {line.strip()}")
        self.assertEqual(suspicious, [], "a message interpolates a secret-bearing variable")


class IrreversibleOperationTests(ScriptText):
    """Two operations cannot be undone, so the script must not perform them by accident."""

    def test_the_script_never_deletes_an_aws_resource(self) -> None:
        offenders = [f"line {number}: {line.strip()}"
                     for number, line in enumerate(self.code, start=1)
                     if "delete-" in line and "aws " in line.lower()]
        self.assertEqual(offenders, [], "the deploy script deletes AWS resources")

    def test_log_retention_is_only_ever_lengthened(self) -> None:
        retention = [line.strip() for line in self.code if "retention" in line.lower()]
        self.assertTrue(retention, "the log retention handling is gone")
        self.assertIn("-lt", "\n".join(retention), "no retention comparison lengthens")
        for line in retention:
            self.assertNotIn("-ne", line, f"a -ne comparison could shorten retention: {line}")

    def test_the_public_read_policy_is_only_applied_to_a_bucket_this_run_created(self) -> None:
        self.assertIn("$bucketCreated", self.raw)
        policy_line = next(number for number, line in enumerate(self.code, start=1)
                           if "put-bucket-policy" in line and "Invoke-AwsWrite" in line)
        guard = "\n".join(self.code[max(0, policy_line - 8):policy_line])
        self.assertIn("$bucketCreated", guard,
                      "the public-read policy is applied without checking who created the bucket")
        # and an existing bucket's policy is *read* and judged, never widened silently
        self.assertIn("Test-S3PublicReadStatement", self.raw)

    def test_an_existing_public_access_block_is_not_treated_as_an_error(self) -> None:
        # HeadBucket/GetPublicAccessBlock answer "not found" for perfectly ordinary reasons; the
        # classifier must not turn that into a hard failure
        harness = HARNESS.read_text(encoding="utf-8")
        self.assertIn("NoSuchPublicAccessBlockConfiguration", harness)


class ResourceSelectionTests(ScriptText):
    """The script must act on the resource it means, not the first one the account lists."""

    def test_the_integration_is_matched_by_its_function_uri(self) -> None:
        self.assertIn("IntegrationUri", self.raw)

    def test_list_calls_are_paginated(self) -> None:
        body = self.body_of("Get-AwsAllItems")
        self.assertIn("NextToken", body, "the list helper ignores NextToken")
        self.assertRegex(body, r"did not advance", "a stuck pagination token would loop forever")


class PostDeploymentTests(ScriptText):
    def test_the_deployed_code_is_verified_at_the_end_of_a_run(self) -> None:
        self.assertIn("verify_deployed_code.py", self.raw,
                      "a deploy does not verify the code it just shipped")


class LogicHarnessTests(unittest.TestCase):
    """The offline verifier must exist, pass, and never be able to reach AWS."""

    def setUp(self) -> None:
        self.shell = powershell()
        if not self.shell:
            self.skipTest("no powershell available, so the deployment logic cannot be exercised")

    def test_the_harness_exists_and_stubs_aws(self) -> None:
        text = HARNESS.read_text(encoding="utf-8")
        self.assertIn("function Invoke-Aws {", text)
        self.assertIn("function Invoke-AwsJson {", text)

    def test_the_harness_passes(self) -> None:
        result = subprocess.run(
            [self.shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(HARNESS)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(REPO), timeout=300,
        )
        output = result.stdout + result.stderr
        self.assertIn("all assertions passed", output,
                      f"the deployment logic check failed:\n{output}")
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

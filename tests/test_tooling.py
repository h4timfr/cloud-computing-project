"""The verification scripts are part of the repository now, so a syntax error in one should fail
the suite rather than only showing up on the day somebody runs it.

Python tooling is compiled; the node harnesses are syntax-checked with ``node --check`` when node
is available (the suite otherwise stays offline and dependency-free, and skips that part).
"""

from __future__ import annotations

import py_compile
import shutil
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / "tools"

PYTHON_TOOLS = sorted(path.name for path in TOOLS.glob("*.py"))
NODE_HARNESSES = sorted(path.name for path in TOOLS.glob("*.mjs"))


def node_executable() -> str | None:
    """The node on PATH, or the bundled runtime this project's harnesses were run with."""
    found = shutil.which("node")
    if found:
        return found
    bundled = Path.home() / ".dsh" / "dsh-runtimes" / "dsh-primary-runtime" / "dependencies" / "node" / "bin" / "node.exe"
    return str(bundled) if bundled.exists() else None


class PythonToolingTests(unittest.TestCase):
    def test_there_is_tooling_to_check(self) -> None:
        self.assertGreaterEqual(len(PYTHON_TOOLS), 5)
        self.assertGreaterEqual(len(NODE_HARNESSES), 3)

    def test_every_python_tool_compiles(self) -> None:
        for name in PYTHON_TOOLS:
            with self.subTest(tool=name):
                py_compile.compile(str(TOOLS / name), doraise=True, cfile=str(REPO / "build" / "test-scratch" / f"{name}.pyc"))

    def test_the_deployed_stack_checks_are_explicit_about_their_target(self) -> None:
        """Each deployed-stack script must name the resources it reads, so it cannot be pointed at
        the wrong account by accident."""
        for name in ("verify_deployed_code.py", "verify_deployed_page.py", "live_acceptance.py"):
            with self.subTest(tool=name):
                text = (TOOLS / name).read_text(encoding="utf-8")
                self.assertIn("floodpulse", text)

    def test_no_tool_carries_a_secret_looking_literal(self) -> None:
        for name in PYTHON_TOOLS:
            with self.subTest(tool=name):
                text = (TOOLS / name).read_text(encoding="utf-8")
                for marker in ("AKIA", "aws_secret_access_key", "BEGIN RSA PRIVATE KEY"):
                    self.assertNotIn(marker, text)


class NodeHarnessSyntaxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.node = node_executable()
        if not self.node:
            self.skipTest("node is not available, so the harnesses cannot be syntax-checked")

    def test_each_harness_parses(self) -> None:
        for name in NODE_HARNESSES:
            with self.subTest(harness=name):
                result = subprocess.run(
                    [self.node, "--check", str(TOOLS / name)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                self.assertEqual(result.returncode, 0, result.stderr)

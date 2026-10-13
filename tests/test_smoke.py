"""tstack check --smoke's secret-guard step against a scripted omp: each run prints the events a real
`omp -p --mode json` run printed. All values here are fake."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import write_exe
from tstacklib import install

SCRIPTED_OMP = r'''#!/usr/bin/env python3
"""Fake omp -p --mode json: prints the next run of runs.json, one event per line; logs each call."""
import json, sys
from pathlib import Path
here = Path(__file__).parent
runs = json.loads((here / "runs.json").read_text())
done = len((here / "calls.log").read_text().splitlines()) if (here / "calls.log").exists() else 0
with open(here / "calls.log", "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\n")
for event in runs[min(done, len(runs) - 1)]:
    print(json.dumps(event))
'''
VALUE = "valuefake0123456789abcdef"
BLOCK = ("secret-guard blocked this command (reads a .env file). Secret values must not enter the transcript. "
         "Use `secret-check` instead.")


def call(command: str, result: str, error: bool = False) -> list[dict]:
    """A bash tool call and its result, as omp's json mode prints them."""
    return [{"type": "tool_execution_start", "toolCallId": "t1", "toolName": "bash", "args": {"command": command}},
            {"type": "tool_execution_end", "toolCallId": "t1", "toolName": "bash", "isError": error,
             "result": {"content": [{"type": "text", "text": result}]}}]


def said(text: str) -> list[dict]:
    return [{"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}]


class GuardCheck(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="tstack-smoke-test-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        write_exe(self.root / "bin" / "omp", SCRIPTED_OMP)
        patch = mock.patch.dict(os.environ, {"PATH": f"{self.root / 'bin'}:{os.environ['PATH']}"})
        patch.start()
        self.addCleanup(patch.stop)

    def check(self, *runs: list[dict]) -> tuple[list[str], int]:
        (self.root / "bin" / "runs.json").write_text(json.dumps(list(runs)))
        problems = install.guard_check(["omp", "-p", "--mode", "json"], self.root, VALUE)
        return problems, len((self.root / "bin" / "calls.log").read_text().splitlines())

    def test_a_secret_check_call_is_not_taken_for_an_unblocked_read(self):
        """A live smoke run, 2026-10-13: the model checked the fixture with secret-check, which the guard allows and which
        prints no value; the next run's `cat .env` was blocked."""
        checked = call("secret-check info file:.env", '{"spec": "file:.env", "exists": true, "length": 37}')
        blocked = call("cat .env", BLOCK, error=True)
        self.assertEqual(self.check(checked + said("It exists."), blocked + said("Blocked.")), ([], 2))

    def test_an_unblocked_read_fails_the_smoke(self):
        leaked = call("cat .env", f"SMOKE_FIXTURE={VALUE}\n")
        problems, runs = self.check(leaked + said("Done."))
        self.assertEqual((problems, runs), (["smoke: the secret guard let the fixture .env value into the transcript"], 1))

    def test_a_block_only_claimed_in_text_proves_nothing(self):
        """The model ran the allowed checker, then wrote that the guard blocked it: no tool result shows a block."""
        checked = call("secret-check info file:.env", '{"spec": "file:.env", "exists": true, "length": 37}')
        problems, runs = self.check(checked + said("The secret-guard blocked reading it."))
        self.assertEqual(len(problems), 1)
        self.assertIn("no tool call was blocked by the secret guard", problems[0])
        self.assertEqual(runs, install.GUARD_TRIES)


if __name__ == "__main__":
    unittest.main()

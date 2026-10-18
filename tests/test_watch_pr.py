"""watch-pr against gh output recorded from real PRs, replayed by a fake gh on PATH."""

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
args, here = sys.argv[1:], os.path.dirname(__file__)
key = " ".join(args[:2]) if args[1] in ("view", "graphql") else "api " + args[1]
if key == "pr view":
    with open(os.path.join(here, "polls"), "a") as log:
        log.write("poll\n")
with open(os.path.join(here, "polls")) as log:
    poll = len(log.readlines()) - 1
sequence = os.path.join(here, "sequence.json")
if os.path.exists(sequence):
    with open(sequence) as fixture:
        snapshots = json.load(fixture)
    recorded = snapshots[min(poll, len(snapshots) - 1)]
else:
    name = "earlier.json" if poll == 0 else "recorded.json"
    with open(os.path.join(here, name)) as fixture:
        recorded = json.load(fixture)
print(json.dumps(recorded[key])) if key in recorded else sys.exit(f"no fixture for {key}")
'''


class WatchPr(unittest.TestCase):
    def watch(self, pr, *args, earlier=lambda recorded: None, polls=()):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        tmp = Path(scratch.name)
        recorded = json.loads((ROOT / "tests/fixtures/watch_pr" / f"{re.sub('[/#]', '-', pr)}.json").read_text())
        (tmp / "recorded.json").write_text(json.dumps(recorded))
        if polls:
            snapshots = [{**recorded, "pr view": {**recorded["pr view"], **poll}} for poll in polls]
            (tmp / "sequence.json").write_text(json.dumps(snapshots))
        earlier(recorded)
        (tmp / "earlier.json").write_text(json.dumps(recorded))
        (tmp / "gh").write_text(FAKE_GH)
        (tmp / "gh").chmod(0o755)
        env = {**os.environ, "PATH": f"{tmp}{os.pathsep}{os.environ['PATH']}"}
        return subprocess.run([str(ROOT / "bin/watch-pr"), pr, "--interval", "0", *args],
                              capture_output=True, text=True, env=env, timeout=60)

    def test_coderabbit_review_limit_notice_is_reported(self):
        result = self.watch("example/team-instance#1", "--once")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("  coderabbitai 2026-10-13T21:09:05Z: Review limit reached", result.stdout.splitlines())

    def test_until_change_and_checks_wait_out_a_pending_check(self):
        def pending(recorded):
            recorded["pr view"]["statusCheckRollup"][0]["state"] = "PENDING"
        title = "example/team-instance#1 OPEN: Add a sample greeting"
        for mode, first_line in (("change", "changed: checks"), ("checks", title)):
            with self.subTest(mode):
                lines = self.watch("example/team-instance#1", "--until", mode, earlier=pending).stdout.splitlines()
                self.assertEqual(lines[0], first_line)
                self.assertIn("  SUCCESS (1): CodeRabbit", lines)

    def test_until_change_ignores_unknown_mergeability_between_known_values(self):
        for fields in ({"mergeable": "UNKNOWN"}, {"mergeStateStatus": "UNKNOWN"},
                       {"mergeable": "UNKNOWN", "mergeStateStatus": "UNKNOWN"}):
            with self.subTest(fields):
                result = self.watch("example/team-instance#1", "--until", "change",
                                    polls=[{}, fields, {}, {**fields, "headRefOid": "new-head"}])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.splitlines()[0], "changed: head")
                self.assertIn("head new-head into main", result.stdout.splitlines())

    def test_until_change_reports_known_mergeability_changes(self):
        for field, known, changed, expected in (
                ("mergeable", "MERGEABLE", "CONFLICTING", "changed: mergeable"),
                ("mergeStateStatus", "CLEAN", "BLOCKED", "changed: merge_state")):
            for initial in (known, "UNKNOWN"):
                with self.subTest(field=field, initial=initial):
                    result = self.watch("example/team-instance#1", "--until", "change",
                                        polls=[{field: initial}, {field: known}, {field: "UNKNOWN"},
                                               {field: changed}])
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.splitlines()[0], expected)
                    self.assertIn("mergeable CONFLICTING, merge state CLEAN, auto-merge off"
                                  if field == "mergeable"
                                  else "mergeable MERGEABLE, merge state BLOCKED, auto-merge off",
                                  result.stdout.splitlines())

    def test_unknown_mergeability_is_printed_without_normalizing_the_report(self):
        unknown = {"mergeable": "UNKNOWN", "mergeStateStatus": "UNKNOWN"}
        for args, polls in ((("--once",), [unknown]),
                            (("--once", "--json"), [unknown]),
                            (("--until", "change", "--json"), [{}, {**unknown, "headRefOid": "new-head"}])):
            with self.subTest(args=args):
                result = self.watch("example/team-instance#1", *args, polls=polls)
                self.assertEqual(result.returncode, 0, result.stderr)
                if "--json" in args:
                    report = json.loads(result.stdout)
                    self.assertEqual((report["mergeable"], report["merge_state"]), ("UNKNOWN", "UNKNOWN"))
                    if "--until" in args:
                        self.assertEqual(report["changed"], ["head"])
                        self.assertEqual(report["head"], "new-head")
                else:
                    self.assertIn("mergeable UNKNOWN, merge state UNKNOWN, auto-merge off",
                                  result.stdout.splitlines())

    def test_until_change_stops_at_once_on_a_closed_pr(self):
        def closed(recorded):
            recorded["pr view"]["state"] = "CLOSED"
        lines = self.watch("example/team-instance#1", "--until", "change", earlier=closed).stdout.splitlines()
        self.assertEqual(lines[0], "example/team-instance#1 CLOSED: Add a sample greeting"
                                   "")

    def test_until_merged_reports_the_merge_sha_and_its_runs(self):
        def still_open(recorded):
            recorded["pr view"].update(state="OPEN", mergeCommit=None, mergedAt=None)
        lines = self.watch("twaldin/tstack#2", "--until", "merged", earlier=still_open).stdout.splitlines()
        self.assertIn("merged 2026-10-13T21:30:39Z as a02d46c2e540cbcacda0de6dacec2fb0a89f355a", lines)
        self.assertIn("  CI (push) on a02d46c2e540: success https://github.com/twaldin/tstack/actions/runs/38471157784",
                      lines)

    def test_merge_reports_deploy_jobs_without_deployment_or_later_commit_tracking(self):
        result = self.watch("example/sample-app#34610", "--once", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        merge = json.loads(result.stdout)["merge"]
        self.assertEqual(set(merge), {"sha", "at", "runs"})
        self.assertTrue(merge["runs"])
        self.assertTrue(all(run["sha"] == merge["sha"] for run in merge["runs"]))
        jobs = [job for run in merge["runs"] for job in run["jobs"]]
        self.assertEqual([job["name"] for job in jobs], ["deploy / deploy"])
        self.assertEqual(jobs[0]["result"], "success")

    def test_gh_failure_passes_through_with_exit_1(self):
        result = self.watch("example/team-instance#1", earlier=lambda recorded: recorded.pop("api graphql"))
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertEqual(result.stderr, "watch-pr: no fixture for api graphql\n")

"""watch-pr against gh output recorded from real PRs, replayed by a fake gh on PATH."""

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Every poll starts with `gh pr view`: the first poll reads earlier.json, later polls read recorded.json.
FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
args, here = sys.argv[1:], os.path.dirname(__file__)
key = " ".join(args[:2]) if args[1] in ("view", "graphql") else "api " + args[1]
if key == "pr view":
    with open(os.path.join(here, "polls"), "a") as log:
        log.write("poll\n")
with open(os.path.join(here, "polls")) as log:
    name = "earlier.json" if len(log.readlines()) == 1 else "recorded.json"
with open(os.path.join(here, name)) as fixture:
    recorded = json.load(fixture)
print(json.dumps(recorded[key])) if key in recorded else sys.exit(f"no fixture for {key}")
'''


class WatchPr(unittest.TestCase):
    def watch(self, pr, *args, earlier=lambda recorded: None):
        """watch-pr on a recorded PR. Its first poll sees the recording as `earlier(recorded)` left it."""
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        tmp = Path(scratch.name)
        recorded = json.loads((ROOT / "tests/fixtures/watch_pr" / f"{re.sub('[/#]', '-', pr)}.json").read_text())
        (tmp / "recorded.json").write_text(json.dumps(recorded))
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

    def test_until_merged_reports_the_merge_sha_and_its_runs(self):
        def still_open(recorded):
            recorded["pr view"].update(state="OPEN", mergeCommit=None, mergedAt=None)
        lines = self.watch("twaldin/tstack#2", "--until", "merged", earlier=still_open).stdout.splitlines()
        self.assertIn("merged 2026-10-13T21:30:39Z as a02d46c2e540cbcacda0de6dacec2fb0a89f355a", lines)
        self.assertIn("  CI (push) on a02d46c2e540: success https://github.com/twaldin/tstack/actions/runs/38471157784",
                      lines)

    def test_until_deployed_waits_past_skipped_deploy_jobs_to_the_prod_deploy(self):
        def not_deployed(recorded):
            for key, value in recorded.items():
                for job in value["jobs"] if key.endswith("/jobs?per_page=100") else []:
                    job.update(status="completed", conclusion="skipped")
                if "/deployments?" in key:
                    value.clear()
        lines = self.watch("example/sample-app#34610", "--until", "deployed", earlier=not_deployed).stdout.splitlines()
        run = lines.index("  Continuous Deployment [Prod] (push) on 00d98aacb563: success "
                          "https://github.com/example/sample-app/actions/runs/38473380387")
        self.assertEqual(lines[run + 1], "    deploy/release jobs success (3): deploy / deploy-slow-check-policy, "
                                         "deploy / deploy-slow-checks-satisfied, deploy / deploy")
        self.assertIn("  prod on 00d98aacb563: success 2026-10-13T22:34:25Z "
                      "https://github.com/example/sample-app/actions/runs/38473380387/job/115574654369", lines)

    def test_gh_failure_passes_through_with_exit_1(self):
        result = self.watch("example/team-instance#1", earlier=lambda recorded: recorded.pop("api graphql"))
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertEqual(result.stderr, "watch-pr: no fixture for api graphql\n")

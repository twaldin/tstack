"""watch-pr against gh output recorded from real PRs, replayed by a fake gh on PATH."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "watch_pr"
AGENTS_1 = FIXTURES / "twaldin-agents-1.json"
TSTACK_2 = FIXTURES / "twaldin-tstack-2.json"
SAMPLE_PR_34610 = FIXTURES / "example-team-sample-app-34610.json"

FAKE_GH = r'''#!/usr/bin/env python3
"""Replays recorded gh output. GH_FIXTURES lists one fixture file per poll; every poll starts with `gh pr view`,
so the count of those calls picks the file, and the last file answers once the polls outrun the list."""
import json, os, sys
args = sys.argv[1:]
polls = os.environ["GH_POLLS"]
if args[:2] == ["pr", "view"]:
    key = f"pr view {args[args.index('--repo') + 1]}#{args[2]}"
    with open(polls, "a") as log:
        log.write(key + "\n")
elif args[:2] == ["api", "graphql"]:
    fields = dict(arg.split("=", 1) for arg in args if arg[:2] in ("o=", "r=", "n="))
    key = f"graphql {fields['o']}/{fields['r']}#{fields['n']}"
else:
    key = "api " + args[1]
files = os.environ["GH_FIXTURES"].split(os.pathsep)
with open(polls) as log:
    poll = len(log.readlines())
with open(files[min(poll, len(files)) - 1]) as fixture:
    recorded = json.load(fixture)
if key not in recorded:
    sys.exit(f"no fixture for {key}")
print(json.dumps(recorded[key]))
'''


class WatchPr(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.dir = Path(scratch.name)
        gh = self.dir / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(0o755)

    def watch(self, *args, polls):
        env = {**os.environ, "PATH": f"{self.dir}{os.pathsep}{os.environ['PATH']}",
               "GH_FIXTURES": os.pathsep.join(str(p) for p in polls), "GH_POLLS": str(self.dir / "polls.log")}
        return subprocess.run([str(ROOT / "bin" / "watch-pr"), *args], capture_output=True, text=True, env=env,
                              timeout=60)

    def variant(self, source, name, edit):
        """A copy of a recorded fixture with `edit(recorded)` applied: the state of the same PR at an earlier poll."""
        recorded = json.loads(source.read_text())
        edit(recorded)
        path = self.dir / name
        path.write_text(json.dumps(recorded))
        return path

    def test_coderabbit_review_limit_notice_is_reported(self):
        result = self.watch("https://github.com/example/team-instance/pull/1", "--once", polls=[AGENTS_1])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, """\
example/team-instance#1 OPEN: Add a sample greeting
https://github.com/example/team-instance/pull/1
head f08be0ca3e03ad5597e09a241eb698a155ce568f into main
mergeable MERGEABLE, merge state CLEAN, auto-merge off
review decision none
checks (1):
  SUCCESS (1): CodeRabbit
reviews (0):
comments (1):
  coderabbitai 2026-10-13T21:09:05Z: Review limit reached
unresolved threads (0):
""")

    def test_merged_pr_reports_merge_sha_and_the_runs_on_it(self):
        result = self.watch("twaldin/tstack#2", polls=[TSTACK_2])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[-4:], [
            "merged 2026-10-13T21:30:39Z as a02d46c2e540cbcacda0de6dacec2fb0a89f355a",
            "runs on a02d46c2e540 or a later main commit containing it (1):",
            "  CI (push) on a02d46c2e540: success https://github.com/twaldin/tstack/actions/runs/38471157784",
            "deployments (0):",
        ])

    def test_prod_deploy_job_and_deployment_after_merge(self):
        result = self.watch("https://github.com/example/sample-app/pull/34610", polls=[SAMPLE_PR_34610])
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        run = lines.index("  Continuous Deployment [Prod] (push) on 00d98aacb563: success "
                          "https://github.com/example/sample-app/actions/runs/38473380387")
        self.assertEqual(lines[run + 1], "    deploy/release jobs success (3): deploy / deploy-slow-check-policy, "
                                         "deploy / deploy-slow-checks-satisfied, deploy / deploy")
        self.assertIn("  prod on 00d98aacb563: success 2026-10-13T22:34:25Z "
                      "https://github.com/example/sample-app/actions/runs/38473380387/job/115574654369", lines)
        self.assertIn("  skipped: Sample application Reviewer Reply x6, Claude Code x6", lines)

    def test_json_carries_the_deploy_job(self):
        result = self.watch("example/sample-app#34610", "--json", polls=[SAMPLE_PR_34610])
        self.assertEqual(result.returncode, 0, result.stderr)
        merge = json.loads(result.stdout)["merge"]
        self.assertEqual(merge["sha"], "00d98aacb5633fb33516bb2cc2900b6ef62c0a25")
        deploy = next(run for run in merge["runs"] if run["url"].endswith("/runs/38473380387"))
        self.assertEqual(deploy["jobs"][-1], {
            "name": "deploy / deploy", "result": "success",
            "url": "https://github.com/example/sample-app/actions/runs/38473380387/job/115570172512"})

    def test_until_change_returns_with_the_changed_fact(self):
        def pending(recorded):
            recorded["pr view example/team-instance#1"]["statusCheckRollup"][0]["state"] = "PENDING"
        first = self.variant(AGENTS_1, "pending.json", pending)
        result = self.watch("example/team-instance#1", "--until", "change", "--interval", "0", polls=[first, AGENTS_1])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[0], "changed: checks")
        self.assertIn("  SUCCESS (1): CodeRabbit", result.stdout.splitlines())

    def test_until_checks_waits_while_a_check_is_pending(self):
        def pending(recorded):
            recorded["pr view example/team-instance#1"]["statusCheckRollup"][0]["state"] = "PENDING"
        first = self.variant(AGENTS_1, "pending.json", pending)
        result = self.watch("example/team-instance#1", "--until", "checks", "--interval", "0", polls=[first, AGENTS_1])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("  SUCCESS (1): CodeRabbit", result.stdout.splitlines())

    def test_until_merged_waits_for_the_merge(self):
        def still_open(recorded):
            recorded["pr view twaldin/tstack#2"].update(state="OPEN", mergeCommit=None, mergedAt=None)
        first = self.variant(TSTACK_2, "open.json", still_open)
        result = self.watch("twaldin/tstack#2", "--until", "merged", "--interval", "0", polls=[first, TSTACK_2])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("merged 2026-10-13T21:30:39Z as a02d46c2e540cbcacda0de6dacec2fb0a89f355a",
                      result.stdout.splitlines())

    def test_until_deployed_waits_past_skipped_deploy_jobs(self):
        def not_deployed(recorded):
            for key, value in recorded.items():
                if key.endswith("/jobs?per_page=100"):
                    for job in value["jobs"]:
                        job.update(status="completed", conclusion="skipped")
                elif "/deployments?" in key:
                    value.clear()
        first = self.variant(SAMPLE_PR_34610, "skipped.json", not_deployed)
        result = self.watch("example/sample-app#34610", "--until", "deployed", "--interval", "0",
                            polls=[first, SAMPLE_PR_34610])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("    deploy/release jobs success (3): deploy / deploy-slow-check-policy, "
                      "deploy / deploy-slow-checks-satisfied, deploy / deploy", result.stdout.splitlines())

    def test_gh_failure_passes_through_with_a_nonzero_exit(self):
        result = self.watch("example/team-instance#2", polls=[AGENTS_1])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "watch-pr: no fixture for pr view example/team-instance#2\n")


if __name__ == "__main__":
    unittest.main()

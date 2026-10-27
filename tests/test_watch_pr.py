import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = {
    "example-fixtures/review-demo#101": "open-review-limit.json",
    "example-fixtures/workflow-demo#102": "merged-workflow.json",
    "example-fixtures/deployment-demo#103": "merged-deployment.json",
}

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
    def watch(self, pr, *args, earlier=lambda recorded: None, polls=(), lane_model=None, symlink=False):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        tmp = Path(scratch.name)
        recorded = json.loads((ROOT / "tests/fixtures/watch_pr" / FIXTURES[pr]).read_text())
        (tmp / "recorded.json").write_text(json.dumps(recorded))
        if polls:
            snapshots = [{**recorded, "pr view": {**recorded["pr view"], **poll}} for poll in polls]
            (tmp / "sequence.json").write_text(json.dumps(snapshots))
        earlier(recorded)
        (tmp / "earlier.json").write_text(json.dumps(recorded))
        (tmp / "gh").write_text(FAKE_GH)
        (tmp / "gh").chmod(0o755)
        env = {**os.environ, "PATH": f"{tmp}{os.pathsep}{os.environ['PATH']}"}
        env.pop("TSTACK_SEAT", None)
        env.pop("TSTACK_LANE", None)
        if lane_model is not None:
            lane = tmp / "state/lanes/review"
            lane.mkdir(parents=True)
            (lane / "lane.json").write_text(json.dumps({"model": lane_model, "pr": recorded["pr view"]["url"]}))
            env.update(TSTACK_SEAT=str(tmp), TSTACK_LANE="review")
        executable = ROOT / "bin/watch-pr"
        if symlink:
            (tmp / "watch-pr").symlink_to(executable)
            executable = tmp / "watch-pr"
        return subprocess.run([str(executable), pr, "--interval", "0", *args],
                              capture_output=True, text=True, env=env, timeout=60)

    def test_coderabbit_review_limit_notice_is_reported(self):
        result = self.watch("example-fixtures/review-demo#101", "--once")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("  sample-review-bot 2025-01-01T12:00:00Z: Review limit reached", result.stdout.splitlines())

    def test_until_change_and_checks_wait_out_a_pending_check(self):
        def pending(recorded):
            recorded["pr view"]["statusCheckRollup"][0]["state"] = "PENDING"
        title = "example-fixtures/review-demo#101 OPEN: Add a sample greeting"
        for mode, first_line in (("change", "changed: checks"), ("checks", title)):
            with self.subTest(mode):
                lines = self.watch("example-fixtures/review-demo#101", "--until", mode, earlier=pending).stdout.splitlines()
                self.assertEqual(lines[0], first_line)
                self.assertIn("  SUCCESS (1): CodeRabbit", lines)

    def test_until_change_ignores_unknown_mergeability_between_known_values(self):
        for fields in ({"mergeable": "UNKNOWN"}, {"mergeStateStatus": "UNKNOWN"},
                       {"mergeable": "UNKNOWN", "mergeStateStatus": "UNKNOWN"}):
            with self.subTest(fields):
                result = self.watch("example-fixtures/review-demo#101", "--until", "change",
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
                    result = self.watch("example-fixtures/review-demo#101", "--until", "change",
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
                result = self.watch("example-fixtures/review-demo#101", *args, polls=polls)
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
        lines = self.watch("example-fixtures/review-demo#101", "--until", "change", earlier=closed).stdout.splitlines()
        self.assertEqual(lines[0], "example-fixtures/review-demo#101 CLOSED: Add a sample greeting")

    def test_until_merged_reports_the_merge_sha_and_its_runs(self):
        def still_open(recorded):
            recorded["pr view"].update(state="OPEN", mergeCommit=None, mergedAt=None)
        lines = self.watch("example-fixtures/workflow-demo#102", "--until", "merged", earlier=still_open).stdout.splitlines()
        self.assertIn("merged 2025-01-02T12:00:00Z as 2222222222222222222222222222222222222222", lines)
        self.assertIn("  CI (push) on 222222222222: success https://github.com/example-fixtures/workflow-demo/actions/runs/2001",
                      lines)

    def test_merge_reports_deploy_jobs_without_deployment_or_later_commit_tracking(self):
        result = self.watch("example-fixtures/deployment-demo#103", "--once", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        merge = json.loads(result.stdout)["merge"]
        self.assertEqual(set(merge), {"sha", "at", "runs"})
        self.assertTrue(merge["runs"])
        self.assertTrue(all(run["sha"] == merge["sha"] for run in merge["runs"]))
        jobs = [job for run in merge["runs"] for job in run["jobs"]]
        self.assertEqual([job["name"] for job in jobs], ["deploy / deploy"])
        self.assertEqual(jobs[0]["result"], "success")

    def test_gh_failure_passes_through_with_exit_1(self):
        result = self.watch("example-fixtures/review-demo#101", earlier=lambda recorded: recorded.pop("api graphql"))
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertEqual(result.stderr, "watch-pr: no fixture for api graphql\n")

    def review_record(self, **changes):
        record = {"head": "1111111111111111111111111111111111111111",
                  "reviewer_model": "anthropic/claude-opus-5-5",
                  "author_model": "openai-codex/gpt-6.1-sol", "verdict": "READY"}
        record.update(changes)
        return {"author": {"login": "sample-reviewer"}, "createdAt": "2026-10-20T12:00:00Z",
                "body": "tstack-verdict: " + json.dumps(record), "url": "https://github.com/example-fixtures/review-demo/pull/101#issuecomment-1002"}

    def verdict_poll(self, *comments, body="tstack-author-model: openai-codex/gpt-6.1-sol", **fields):
        return {"body": body, "comments": list(comments), "reviews": [], **fields}

    def test_current_other_family_verdict_in_text_and_json(self):
        poll = self.verdict_poll(self.review_record())
        result = self.watch("example-fixtures/review-demo#101", polls=[poll], symlink=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [line for line in result.stdout.splitlines() if line.startswith("other-family verdict")]
        self.assertEqual(lines, ["other-family verdict READY at 1111111111111111111111111111111111111111: "
                                 "anthropic/claude-opus-5-5 reviewed openai-codex/gpt-6.1-sol"])
        result = self.watch("example-fixtures/review-demo#101", "--json", polls=[poll])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["verdict"],
                         {"ready": True, "head": "1111111111111111111111111111111111111111",
                          "reviewer_model": "anthropic/claude-opus-5-5",
                          "author_model": "openai-codex/gpt-6.1-sol", "verdict": "READY",
                          "source": "comments", "at": "2026-10-20T12:00:00Z"})

    def test_nonqualifying_verdicts_are_facts_not_errors(self):
        malformed = self.review_record()
        malformed["body"] = 'tstack-verdict: {"verdict":"READY"}'
        for label, poll in (
                ("missing", self.verdict_poll()),
                ("same family", self.verdict_poll(self.review_record(reviewer_model="openai/gpt-6.1"))),
                ("stale", self.verdict_poll(self.review_record(head="a" * 40))),
                ("short SHA", self.verdict_poll(self.review_record(head="1111111"))),
                ("malformed", self.verdict_poll(malformed)),
                ("unknown reviewer", self.verdict_poll(self.review_record(reviewer_model="@opus"))),
                ("unknown provider", self.verdict_poll(self.review_record(reviewer_model="other/claude-opus-5-5"))),
                ("unknown author", self.verdict_poll(self.review_record(), body="tstack-author-model: @sol")),
                ("wrong author", self.verdict_poll(self.review_record(author_model="google/gemini-3-pro"))),
                ("non-ready", self.verdict_poll(self.review_record(verdict="NOT_READY"))),
                ("missing author metadata", self.verdict_poll(self.review_record(), body="")),
                ("duplicate author", self.verdict_poll(self.review_record(), body=
                    "tstack-author-model: openai-codex/gpt-6.1-sol\n"
                    "tstack-author-model: anthropic/claude-opus-5-5"))):
            with self.subTest(label):
                result = self.watch("example-fixtures/review-demo#101", polls=[poll])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual([line for line in result.stdout.splitlines()
                                  if line.startswith("other-family verdict")],
                                 ["other-family verdict: no READY current-head verdict"])

    def test_lane_resolved_model_precedes_pr_metadata_and_alias_falls_back(self):
        for model, body, ready in (
                ("openai-codex/gpt-6.1-sol", "tstack-author-model: anthropic/claude-opus-5-5", True),
                ("anthropic/claude-opus-5-5", "tstack-author-model: openai-codex/gpt-6.1-sol", False),
                ("@sol", "tstack-author-model: openai-codex/gpt-6.1-sol", True),
                ("@sol", "", False)):
            with self.subTest(model=model, body=body):
                result = self.watch("example-fixtures/review-demo#101", "--json", lane_model=model,
                                    polls=[self.verdict_poll(self.review_record(), body=body)])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["verdict"]["ready"], ready)

    def test_latest_rejection_supersedes_ready_across_comments_and_reviews(self):
        rejection = self.review_record(verdict="NOT_READY")
        rejection.update(submittedAt="2026-10-20T13:00:00Z", state="COMMENTED")
        poll = self.verdict_poll(self.review_record(), reviews=[rejection])
        result = self.watch("example-fixtures/review-demo#101", "--json", polls=[poll])
        self.assertEqual(result.returncode, 0, result.stderr)
        fact = json.loads(result.stdout)["verdict"]
        self.assertEqual((fact["ready"], fact["verdict"], fact["source"]), (False, "NOT_READY", "reviews"))

    def test_dismissed_or_changes_requested_review_is_not_ready(self):
        for state in ("DISMISSED", "CHANGES_REQUESTED", "PENDING"):
            with self.subTest(state):
                review = self.review_record()
                review.update(submittedAt="2026-10-20T13:00:00Z", state=state)
                comments = [] if state == "PENDING" else [self.review_record()]
                result = self.watch("example-fixtures/review-demo#101", "--json",
                                    polls=[self.verdict_poll(*comments, reviews=[review])])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(json.loads(result.stdout)["verdict"]["ready"])

    def test_verdict_change_is_reported_by_until_change(self):
        result = self.watch("example-fixtures/review-demo#101", "--until", "change", "--json",
                            polls=[self.verdict_poll(), self.verdict_poll(self.review_record())])
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["changed"], ["verdict", "comments"])
        self.assertTrue(report["verdict"]["ready"])

    def test_malformed_structured_metadata_never_creates_ready_evidence(self):
        valid = self.review_record()["body"]
        for body in (
                valid[:-1],
                valid.replace('"verdict": "READY"', '"verdict": "NOT_READY", "verdict": "READY"'),
                valid.replace('"reviewer_model": "anthropic/claude-opus-5-5"', '"reviewer_model": null'),
                valid.replace('"verdict": "READY"', '"verdict": "ready"'),
                "Independent exact-head review approved 1111111111111111111111111111111111111111 "
                "by anthropic/claude-opus-5-5; author openai-codex/gpt-6.1-sol. READY."):
            with self.subTest(body=body):
                comment = {**self.review_record(), "body": body}
                result = self.watch("example-fixtures/review-demo#101", "--json", polls=[self.verdict_poll(comment)])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(json.loads(result.stdout)["verdict"]["ready"])

    def test_latest_comment_wins_by_time_not_array_order(self):
        rejection = {**self.review_record(verdict="NOT_READY"), "createdAt": "2026-10-20T14:00:00Z"}
        result = self.watch("example-fixtures/review-demo#101", "--json",
                            polls=[self.verdict_poll(rejection, self.review_record())])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["verdict"]["ready"])

    def test_ready_evidence_becomes_stale_when_head_changes(self):
        current = self.verdict_poll(self.review_record())
        stale = {**current, "headRefOid": "a" * 40}
        result = self.watch("example-fixtures/review-demo#101", "--until", "change", "--json", polls=[current, stale])
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["changed"], ["head", "verdict"])
        self.assertFalse(report["verdict"]["ready"])

    def test_resolved_grok_reviewer_counts_as_other_family(self):
        result = self.watch("example-fixtures/review-demo#101", "--json",
                            polls=[self.verdict_poll(self.review_record(reviewer_model="xai/grok-4"))])
        self.assertEqual(result.returncode, 0, result.stderr)
        fact = json.loads(result.stdout)["verdict"]
        self.assertTrue(fact["ready"])
        self.assertEqual(fact["reviewer_model"], "xai/grok-4")

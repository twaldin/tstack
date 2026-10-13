import json

from support import Sandbox, git

PR = "https://github.com/acme/web/pull/7"


class LaneRuns(Sandbox):
    def setUp(self):
        super().setUp()
        self.main = self.project_clone()

    def start(self) -> str:
        return self.cli("lane", "start", "ci", "--brief", str(self.brief()), "--model", "@tiny").stdout.strip()

    def test_start_records_report_cost_and_session(self):
        lane_id = self.start()
        self.assertEqual(lane_id, "demo-ci-1")
        record = self.settled(lane_id, "done")
        self.assertEqual(record["runs"][0]["exit"], 0)
        self.assertEqual(record["cost_usd"], 0.01)
        self.assertTrue(record["session"].endswith(".jsonl"))
        report = json.loads((self.seat / "state" / "lanes" / lane_id / "report.json").read_text())
        self.assertEqual((report["status"], report["run"], report["cost_usd"]), ("done", 1, 0.01))
        call = self.log_lines("omp.log")[0]
        self.assertEqual(call["cwd"], str(self.home / "worktrees" / "demo" / lane_id))
        self.assertEqual(call["lane"], lane_id)
        self.assertIn("--append-system-prompt", call["args"])
        self.assertEqual(git(self.home / "worktrees" / "demo" / lane_id, "branch", "--show-current"), f"lane/{lane_id}")

    def test_brief_without_timebox_is_refused(self):
        result = self.cli("lane", "start", "ci", "--brief", str(self.brief("No timebox here.\n")), check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TIMEBOX", result.stderr)

    def test_missing_report_becomes_failed(self):
        self.omp_config(report=None)
        record = self.settled(self.start(), "failed")
        self.assertEqual(record["reason"], "exited without report")

    def test_report_from_an_earlier_run_does_not_count(self):
        self.omp_config(report="waiting", pr=PR)
        lane_id = self.start()
        self.settled(lane_id, "waiting")
        self.omp_config(report=None)
        self.cli("lane", "resume", lane_id, "checks finished")
        record = self.settled(lane_id, "failed")
        self.assertEqual(record["reason"], "exited without report")
        self.assertEqual(len(record["runs"]), 2)

    def test_lock_refuses_a_second_run(self):
        self.omp_config(report="waiting", pr=PR, sleep=4)
        lane_id = self.start()
        self.wait_for(lambda: self.lane_record(lane_id)["session"], what="the first run to start")
        busy = self.cli("lane", "resume", lane_id, "another event", check=False)
        self.assertEqual(busy.returncode, 3, busy.stderr)
        self.assertEqual(len(self.lane_record(lane_id)["runs"]), 1)
        self.settled(lane_id, "waiting")
        self.omp_config(report="waiting", pr=PR)
        self.cli("lane", "resume", lane_id, "now it may run")
        self.assertEqual(len(self.settled(lane_id, "waiting")["runs"]), 2)

    def test_held_and_terminal_lanes_refuse_resume(self):
        self.omp_config(report="waiting", pr=PR)
        lane_id = self.start()
        self.settled(lane_id, "waiting")
        self.cli("lane", "hold", lane_id)
        self.assertEqual(self.cli("lane", "resume", lane_id, "x", check=False).returncode, 3)
        self.cli("lane", "release", lane_id)
        self.cli("lane", "cancel", lane_id, "--reason", "replaced")
        self.assertEqual(self.cli("lane", "resume", lane_id, "x", check=False).returncode, 4)
        self.assertEqual(self.lane_record(lane_id)["status"], "cancelled")

    def test_two_fix_waves_then_stuck(self):
        self.omp_config(report="waiting", pr=PR)
        lane_id = self.start()
        self.settled(lane_id, "waiting")
        for wave in (1, 2):
            self.cli("lane", "resume", lane_id, "test failed", "--kind", "check-failed", "--check", "test")
            record = self.settled(lane_id, "waiting")
            self.assertEqual(record["waves"], {"test": wave})
        third = self.cli("lane", "resume", lane_id, "test failed again", "--kind", "check-failed", "--check", "test", check=False)
        self.assertEqual(third.returncode, 5, third.stderr)
        record = self.lane_record(lane_id)
        self.assertEqual(record["status"], "stuck")
        self.assertIn("test failed after 2 fix waves", record["reason"])
        self.assertEqual(len(record["runs"]), 3)

    def test_list_and_show(self):
        lane_id = self.start()
        self.settled(lane_id, "done")
        self.assertIn(lane_id, self.cli("lane", "list").stdout)
        self.assertEqual(json.loads(self.cli("lane", "list", "--json").stdout)[0]["id"], lane_id)
        self.assertIn('"status": "done"', self.cli("lane", "show", lane_id).stdout)


class MergeGuard(Sandbox):
    HEAD = "b" * 40

    def setUp(self):
        super().setUp()
        lane = self.seat / "state" / "lanes" / "demo-ci-1"
        lane.mkdir(parents=True)
        (lane / "lane.json").write_text(json.dumps({
            "id": "demo-ci-1", "project": "demo", "branch": "lane/demo-ci-1", "base": "main", "status": "running",
            "runs": [{"run": 1, "event": "start", "start": "2026-10-13T00:00:00Z"}], "waves": {}, "pr": PR}))
        self.env["TSTACK_LANE"] = "demo-ci-1"

    def report(self, *proof: str, head: str = HEAD) -> None:
        args = ["report", "--status", "waiting", "--waiting-on", "merge", "--summary", "ready", "--pr", PR, "--head", head]
        for item in proof:
            args += ["--proof", item]
        self.cli("lane", *args)

    def merge(self):
        return self.cli("lane", "merge", check=False)

    def merged(self) -> bool:
        return any(call[:2] == ["pr", "merge"] for call in self.log_lines("gh.log"))

    def test_missing_proof_refused(self):
        self.report()
        self.pr_stub(head=self.HEAD, branch="lane/demo-ci-1")
        result = self.merge()
        self.assertEqual(result.returncode, 4)
        self.assertIn("no proof", result.stderr)
        self.assertFalse(self.merged())

    def test_stale_head_refused(self):
        self.report("pnpm test=pass")
        self.pr_stub(head="c" * 40, branch="lane/demo-ci-1")
        result = self.merge()
        self.assertEqual(result.returncode, 4)
        self.assertIn("current head cccccccccccc", result.stderr)
        self.assertFalse(self.merged())

    def test_failing_check_refused(self):
        self.report("pnpm test=pass")
        self.pr_stub(head=self.HEAD, branch="lane/demo-ci-1", checks=(("test", "SUCCESS"), ("lint", "FAILURE")))
        result = self.merge()
        self.assertEqual(result.returncode, 4)
        self.assertIn("lint", result.stderr)
        self.assertFalse(self.merged())

    def test_failed_proof_refused(self):
        self.report("pnpm test=fail")
        self.pr_stub(head=self.HEAD, branch="lane/demo-ci-1")
        self.assertEqual(self.merge().returncode, 4)
        self.assertFalse(self.merged())

    def test_reserved_merge_refused(self):
        (self.instance / "projects" / "demo" / "pipeline.md").write_text("# Pipeline\n\nmerge: operator\n")
        self.report("pnpm test=pass")
        self.pr_stub(head=self.HEAD, branch="lane/demo-ci-1")
        result = self.merge()
        self.assertEqual(result.returncode, 4)
        self.assertIn("merge: operator", result.stderr)

    def test_guarded_merge_pins_the_head(self):
        self.report("pnpm test=pass")
        self.pr_stub(head=self.HEAD, branch="lane/demo-ci-1")
        result = self.merge()
        self.assertEqual(result.returncode, 0, result.stderr)
        call = next(c for c in self.log_lines("gh.log") if c[:2] == ["pr", "merge"])
        self.assertEqual(call[call.index("--match-head-commit") + 1], self.HEAD)
        self.assertIn("--squash", call)

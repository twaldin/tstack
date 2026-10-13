import datetime as dt
import json
import subprocess

from support import Sandbox, git, write_exe

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

    def test_a_dispatch_already_taken_is_not_run_again(self):
        self.omp_config(report="waiting", pr=PR)
        lane_id = self.start()
        self.settled(lane_id, "waiting")
        args = ("lane", "resume", lane_id, "test failed", "--kind", "check-failed", "--check", "test", "--event-id", "pr-1")
        self.cli(*args)
        self.settled(lane_id, "waiting")
        again = self.cli(*args)
        self.assertIn("already took event pr-1", again.stdout)
        record = self.lane_record(lane_id)
        self.assertEqual((len(record["runs"]), record["waves"]), (2, {"test": 1}))

    def test_failed_launches_do_not_use_up_fix_waves(self):
        self.omp_config(report="waiting", pr=PR)
        lane_id = self.start()
        self.settled(lane_id, "waiting")
        write_exe(self.stubs / "bin" / "systemd-run", "#!/bin/sh\necho 'Failed to start transient service' >&2\nexit 1\n")
        self.env["TSTACK_LAUNCHER"] = "systemd"
        for _ in range(2):
            failed = self.cli("lane", "resume", lane_id, "test failed", "--kind", "check-failed", "--check", "test", check=False)
            self.assertEqual(failed.returncode, 1, failed.stderr)
        record = self.lane_record(lane_id)
        self.assertEqual((record["status"], len(record["runs"]), record["waves"]), ("waiting", 1, {}))
        self.env["TSTACK_LAUNCHER"] = "process"
        self.cli("lane", "resume", lane_id, "test failed", "--kind", "check-failed", "--check", "test")
        self.assertEqual(self.settled(lane_id, "waiting")["waves"], {"test": 1})

    def test_clean_keeps_a_lane_with_commits_but_no_pr(self):
        lane_id = self.start()
        self.settled(lane_id, "done")
        worktree = self.home / "worktrees" / "demo" / lane_id
        (worktree / "found.txt").write_text("a fix nobody pushed\n")
        env = {k: v for k, v in self.env.items() if k.startswith("GIT_")}
        for args in (["add", "found.txt"], ["commit", "--quiet", "-m", "unpushed"]):
            subprocess.run(["git", "-C", str(worktree), *args], check=True, env={**self.env, **env})
        result = self.cli("lane", "clean", lane_id, check=False)
        self.assertEqual(result.returncode, 4)
        self.assertIn("1 commit(s) not on origin/main", result.stderr)
        self.assertTrue(worktree.exists())

    def test_clean_removes_a_finished_lane_without_commits(self):
        lane_id = self.start()
        self.settled(lane_id, "done")
        self.cli("lane", "clean", lane_id)
        self.assertFalse((self.home / "worktrees" / "demo" / lane_id).exists())


class MergeGuard(Sandbox):
    HEAD = "b" * 40

    def setUp(self):
        super().setUp()
        lane = self.seat / "state" / "lanes" / "demo-ci-1"
        lane.mkdir(parents=True)
        (lane / "lane.json").write_text(json.dumps({
            "id": "demo-ci-1", "project": "demo", "branch": "lane/demo-ci-1", "base": "main", "status": "running",
            "runs": [{"run": 1, "event": "start", "start": "2026-10-13T00:00:00Z"}], "waves": {}, "pr": PR}))
        self.env.update(TSTACK_LANE="demo-ci-1", TSTACK_SEAT=str(self.seat))

    def report(self, *proof: str, head: str = HEAD) -> None:
        args = ["report", "--status", "waiting", "--waiting-on", "merge", "--summary", "ready", "--pr", PR, "--head", head]
        for item in proof:
            args += ["--proof", item]
        self.cli("lane", *args)

    def merge(self, *args: str):
        return self.cli("lane", "merge", *args, check=False)

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

    def test_a_missing_review_needs_admin_and_auto_merge_is_never_armed(self):
        self.report("pnpm test=pass")
        self.pr_stub(head=self.HEAD, branch="lane/demo-ci-1", merge_state="BLOCKED", review_decision="REVIEW_REQUIRED")
        result = self.merge()
        self.assertEqual(result.returncode, 4)
        self.assertIn("lane merge --admin", result.stderr)
        self.assertFalse(self.merged())
        result = self.merge("--admin")
        self.assertEqual(result.returncode, 0, result.stderr)
        call = next(c for c in self.log_lines("gh.log") if c[:2] == ["pr", "merge"])
        self.assertIn("--admin", call)
        self.assertNotIn("--auto", call)
        self.assertEqual(call[call.index("--match-head-commit") + 1], self.HEAD)

    def test_admin_merge_refused_when_the_pipeline_forbids_it_or_something_else_blocks(self):
        pipeline = self.instance / "projects" / "demo" / "pipeline.md"
        pipeline.write_text("# Pipeline\n\nadmin-merge: no\n")
        self.report("pnpm test=pass")
        self.pr_stub(head=self.HEAD, branch="lane/demo-ci-1", merge_state="BLOCKED", review_decision="REVIEW_REQUIRED")
        result = self.merge("--admin")
        self.assertEqual(result.returncode, 4)
        self.assertIn("admin-merge: no", result.stderr)
        pipeline.unlink()
        self.pr_stub(head=self.HEAD, branch="lane/demo-ci-1", merge_state="BLOCKED", review_decision="APPROVED")
        result = self.merge("--admin")
        self.assertEqual(result.returncode, 4)
        self.assertIn("other than a missing review", result.stderr)
        self.assertFalse(self.merged())

    def test_engine_and_instance_prs_are_never_merged(self):
        self.report("pnpm test=pass")
        self.pr_stub(head=self.HEAD, branch="lane/demo-ci-1")
        for repo, other in ((self.engine, self.instance), (self.instance, self.engine)):
            git_init(repo, "https://github.com/acme/web.git")
            git_init(other, "https://github.com/acme/elsewhere.git")
            result = self.merge()
            self.assertEqual(result.returncode, 4)
            self.assertIn("system PRs", result.stderr)
        self.assertFalse(self.merged())


def git_init(repo, origin: str) -> None:
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "remove", "origin"], capture_output=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", origin], check=True)


class LaneContext(Sandbox):
    """Inside a lane, `lane` runs only report, merge, show and list, for that lane and its seat."""

    def setUp(self):
        super().setUp()
        self.other_seat = self.instance / "projects" / "other"
        for seat, lane_id in ((self.seat, "demo-ci-1"), (self.seat, "demo-ci-2"), (self.other_seat, "demo-ci-1")):
            lane = seat / "state" / "lanes" / lane_id
            lane.mkdir(parents=True)
            (lane / "lane.json").write_text(json.dumps({
                "id": lane_id, "project": "demo", "branch": f"lane/{lane_id}", "base": "main", "status": "running",
                "runs": [{"run": 1, "event": "start", "start": "2026-10-13T00:00:00Z", "accepted": "2026-10-13T00:00:00Z"}],
                "waves": {}, "pr": None, "held": False}))
        self.env.update(TSTACK_LANE="demo-ci-1", TSTACK_SEAT=str(self.seat))

    def test_a_lane_cannot_act_on_other_lanes_or_seats(self):
        refused = [
            ("report", "--lane", "demo-ci-2", "--status", "done", "--summary", "not mine"),
            ("report", "--seat", str(self.other_seat), "--status", "done", "--summary", "not my seat"),
            ("show", "demo-ci-2"), ("merge", "demo-ci-2"), ("list", "--seat", str(self.other_seat)),
            ("start", "more", "--brief", str(self.brief())), ("resume", "demo-ci-1", "again"),
            ("stop", "demo-ci-1"), ("cancel", "demo-ci-2"), ("hold", "demo-ci-2"), ("clean", "demo-ci-1"),
            ("supersede", "demo-ci-2", "--by", "demo-ci-3"),
        ]
        for args in refused:
            with self.subTest(args[0]):
                result = self.cli("lane", *args, check=False)
                self.assertEqual(result.returncode, 4, result.stderr)
        self.assertFalse((self.seat / "state" / "lanes" / "demo-ci-2" / "report.json").exists())
        self.assertFalse((self.other_seat / "state" / "lanes" / "demo-ci-1" / "report.json").exists())
        self.assertEqual(self.lane_record("demo-ci-2")["status"], "running")

    def test_a_lane_reports_on_itself_and_reads_its_seat(self):
        self.cli("lane", "report", "--status", "done", "--summary", "mine", "--seat", str(self.seat))
        self.assertTrue((self.seat / "state" / "lanes" / "demo-ci-1" / "report.json").exists())
        self.assertIn('"id": "demo-ci-1"', self.cli("lane", "show", "demo-ci-1").stdout)
        self.assertIn("demo-ci-2", self.cli("lane", "list").stdout)


class QueuedLaunch(Sandbox):
    """A launch handed to the launcher whose supervisor has not taken the run lock yet."""

    def setUp(self):
        super().setUp()
        worktree = self.root / "worktree"
        worktree.mkdir()
        lane = self.seat / "state" / "lanes" / "demo-ci-1"
        lane.mkdir(parents=True)
        (lane / "brief.md").write_text("Fix CI.\nTIMEBOX: 1h\n")
        self.record = {
            "id": "demo-ci-1", "project": "demo", "task": "ci", "seat": str(self.seat), "repo": "acme/web",
            "worktree": str(worktree), "branch": "lane/demo-ci-1", "base": "main", "playbook": "build-feature",
            "model": "@tiny", "thinking": "low", "timebox": "1h", "memory": "1G",
            "session": str(self.home / ".omp/agent/sessions/-fake/2026-10-13T00-00-00-000Z_abc.jsonl"),
            "status": "running", "reason": None, "held": False, "pr": None, "waves": {}, "cost_usd": 0, "created": ago(60),
            "runs": [{"run": 1, "event": "start", "start": ago(60), "accepted": ago(60), "end": ago(50), "exit": 0},
                     {"run": 2, "event": "resume", "start": ago(0), "launcher": "process"}]}
        (lane / "lane.json").write_text(json.dumps(self.record))
        run_dir = self.seat / "state" / "run" / "lanes" / "demo-ci-1"
        run_dir.mkdir(parents=True)
        (run_dir / "run-2.event").write_text("CI finished")

    def supervise(self):
        self.cli("lane", "_run", "demo-ci-1", "--seat", str(self.seat), "--run", "2")

    def test_a_cancelled_queued_run_never_starts_omp(self):
        self.cli("lane", "cancel", "demo-ci-1", "--reason", "replaced")
        self.supervise()
        self.assertEqual(self.log_lines("omp.log"), [])
        record = self.lane_record("demo-ci-1")
        self.assertEqual(record["status"], "cancelled")
        self.assertEqual(record["runs"][1]["exit"], "not started")

    def test_stopping_a_queued_launch_cancels_it(self):
        self.assertIn("waiting", self.cli("lane", "stop", "demo-ci-1").stdout)
        self.supervise()
        self.assertEqual(self.log_lines("omp.log"), [])
        self.assertEqual(self.lane_record("demo-ci-1")["reason"], "stopped before start")

    def test_the_supervisor_starts_nothing_for_a_lane_that_is_not_running(self):
        self.record["status"] = "cancelled"
        (self.seat / "state" / "lanes" / "demo-ci-1" / "lane.json").write_text(json.dumps(self.record))
        self.supervise()
        self.assertEqual(self.log_lines("omp.log"), [])


def ago(minutes: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")

import json
import unittest
from pathlib import Path

from support import Sandbox, git


class LaneRuns(Sandbox):
    def setUp(self):
        super().setUp()
        self.main = self.project()

    def start(self, *extra) -> str:
        return self.cli("lane", "start", "Fix it", "--repo", "acme/demo", "--brief", str(self.brief), *extra).stdout.strip()

    def test_start_runs_the_brief_and_wait_prints_the_final_answer(self):
        self.assertEqual(self.start(), "demo-fix-it-1")
        self.assertEqual(self.cli("lane", "wait", "demo-fix-it-1").stdout, "demo-fix-it-1 exited exit 0\ndone\n")
        lane = self.seat / "state" / "lanes" / "demo-fix-it-1"
        self.assertEqual((lane / "final.md").read_text(), "done\n")
        self.assertEqual((lane / "brief.md").read_text(), "Reply with the word done.\n")
        worktree = self.home / "worktrees" / "demo" / "demo-fix-it-1"
        self.assertEqual(git(worktree, "rev-parse", "--abbrev-ref", "HEAD"), "lane/demo-fix-it-1")
        call = self.log("omp.log")[0]
        self.assertEqual(call["args"][:6], ["-p", "--mode", "json", "--model", "@sol", "--thinking"])
        self.assertTrue(call["args"][-1].endswith("Reply with the word done.\n"))
        self.assertEqual((Path(call["cwd"]).resolve(), call["lane"]), (worktree.resolve(), "demo-fix-it-1"))
        record = self.record("demo-fix-it-1")
        self.assertEqual((record["status"], record["runs"][0]["exit"], record["runs"][0]["cost_usd"]), ("exited", 0, 0.02))
        show = self.cli("lane", "show", "demo-fix-it-1").stdout.splitlines()
        self.assertEqual(show[0], "demo-fix-it-1: exited, run 1 exit 0, $0.02")
        self.assertEqual(show[-2:], ["final answer (last 20 lines):", "done"])

    def test_resume_continues_the_same_session(self):
        lane_id = self.start()
        self.cli("lane", "wait", lane_id)
        self.omp(answer="fixed", cost=0.5)
        self.assertEqual(self.cli("lane", "resume", lane_id, "CI failed on test_x").stdout, f"{lane_id} run 2\n")
        self.assertEqual(self.cli("lane", "wait", lane_id).stdout, f"{lane_id} exited exit 0\nfixed\n")
        first, second = self.log("omp.log")
        session = self.record(lane_id)["session"]
        self.assertEqual(second["args"][-3:], ["--resume", session, "CI failed on test_x"])
        self.assertNotIn("--resume", first["args"])
        self.assertIn(f"{lane_id}\texited\texit 0\t$0.52\t-\t", self.cli("lane", "list").stdout)

    def test_a_run_without_a_final_answer_is_reported_as_such(self):
        self.omp(exit=3)
        lane_id = self.start()
        self.assertEqual(self.cli("lane", "wait", lane_id).stdout, f"{lane_id} exited exit 3\n(no final answer)\n")
        self.assertEqual(self.cli("lane", "show", lane_id).stdout.splitlines()[-1],
                         "final answer: none (run 1 ended without one)")

    def test_stop_ends_the_run_and_wait_sees_it(self):
        self.brief.write_text("SLOW\n")
        lane_id = self.start()
        run = self.seat / "state" / "run" / "lanes" / lane_id
        self.assertTrue(self.until(lambda: (run / "run-1.jsonl").exists() and (run / "run-1.jsonl").read_text()))
        self.assertEqual(self.cli("lane", "stop", lane_id).stdout, f"{lane_id} stopped (run 1)\n")
        self.assertTrue(self.gone(int((run / "run-1.pid").read_text())))
        self.assertEqual(self.cli("lane", "wait", lane_id).stdout, f"{lane_id} stopped exit -\n(no final answer)\n")
        self.assertEqual(self.cli("lane", "resume", lane_id, "go on").stdout, f"{lane_id} run 2\n")

    def test_resume_while_running_names_the_run(self):
        self.brief.write_text("SLOW\n")
        lane_id = self.start()
        result = self.cli("lane", "resume", lane_id, "more", check=False)
        self.assertEqual(result.stderr, f"lane: {lane_id} is running run 1; `lane stop {lane_id}` first\n")
        self.cli("lane", "stop", lane_id)

    def test_wait_returns_the_first_lane_to_finish(self):
        self.brief.write_text("SLOW\n")
        slow = self.start()
        self.brief.write_text("quick\n")
        quick = self.start()
        self.assertEqual(self.cli("lane", "wait", slow, quick).stdout, f"{quick} exited exit 0\ndone\n")
        self.cli("lane", "stop", slow)

    def test_systemd_runs_a_user_unit_that_stop_stops(self):
        self.env["TSTACK_LAUNCHER"] = "systemd"
        lane_id = self.start("--memory", "4G")
        self.assertEqual(self.cli("lane", "wait", lane_id).stdout, f"{lane_id} exited exit 0\ndone\n")
        spawn = self.log("systemd-run.log")[0]
        self.assertEqual(spawn[:4], ["--user", "--collect", "--quiet", f"--unit=tstack-lane-{lane_id}"])
        self.assertIn("MemoryMax=4G", spawn)
        self.assertIn(f"--setenv=TSTACK_SEAT={self.seat}", spawn)
        self.brief.write_text("SLOW\n")
        slow = self.start()
        self.assertEqual(self.cli("lane", "stop", slow).stdout, f"{slow} stopped (run 1)\n")
        self.assertIn(f"--user stop tstack-lane-{slow}", (self.stubs / "systemctl.log").read_text())

    def test_clean_removes_the_worktree_and_branch(self):
        lane_id = self.start()
        self.cli("lane", "wait", lane_id)
        sha = git(self.main, "rev-parse", "--short", f"lane/{lane_id}")
        worktree = self.home / "worktrees" / "demo" / lane_id
        self.assertEqual(self.cli("lane", "clean", lane_id).stdout,
                         f"removed worktree {worktree}\nDeleted branch lane/{lane_id} (was {sha}).\n")
        self.assertFalse(worktree.exists())
        self.assertEqual(self.cli("lane", "list").stdout, "")
        self.assertTrue(self.cli("lane", "list", "--all").stdout.startswith(f"{lane_id}\texited"))


class LaneRecords(Sandbox):
    """Merge and old records work from lane.json alone; gh is faked."""

    def write(self, record: dict) -> None:
        lane = self.seat / "state" / "lanes" / record["id"]
        lane.mkdir(parents=True)
        (lane / "lane.json").write_text(json.dumps(record))

    def setUp(self):
        super().setUp()
        self.url = "https://github.com/acme/demo/pull/7"
        self.write({"id": "demo-x-1", "project": "demo", "repo": "acme/demo", "branch": "lane/demo-x-1", "base": "main",
                    "worktree": str(self.root / "wt"), "model": "@sol", "thinking": "high", "memory": "8G",
                    "session": "abc", "status": "exited", "pr": None, "runs": [{"run": 1, "end": "x", "exit": 0}]})
        (self.stubs / "pr-list.json").write_text(json.dumps([{"url": self.url}]))
        (self.stubs / "pr-view.json").write_text(json.dumps({"headRefOid": "a" * 40, "state": "OPEN"}))

    def test_merge_squashes_at_the_head_and_records_the_merge(self):
        self.assertEqual(self.cli("lane", "merge", "demo-x-1").stdout, f"merged: {self.url} at {'a' * 12} as {'f' * 12}\n")
        self.assertIn(["pr", "merge", self.url, "--squash", "--delete-branch", "--match-head-commit", "a" * 40],
                      self.log("gh.log"))
        self.assertEqual((self.record("demo-x-1")["pr"], self.record("demo-x-1")["merge"]), (self.url, "f" * 40))

    def test_auto_merge_is_reported_armed(self):
        self.assertEqual(self.cli("lane", "merge", "demo-x-1", "--auto", "--admin").stdout,
                         f"auto-merge armed: {self.url} at {'a' * 12}\n")
        self.assertIn("--auto", self.log("gh.log")[-2])

    def test_github_refusal_passes_through(self):
        (self.stubs / "merge.err").write_text("Head branch was modified. Review and try the merge again.")
        result = self.cli("lane", "merge", "demo-x-1", check=False)
        self.assertEqual((result.returncode, result.stderr), (1, f"lane: gh pr merge {self.url}: Head branch was "
                                                                 "modified. Review and try the merge again.\n"))

    def test_old_records_with_extra_fields_load_and_resume(self):
        self.write({"id": "demo-old-1", "project": "demo", "task": "old", "playbook": "build-feature", "timebox": "2h",
                    "held": False, "waves": {"ci": 1}, "reason": None, "cost_usd": 1.5, "repo": "acme/demo",
                    "worktree": str(self.root), "branch": "lane/demo-old-1", "base": "main", "model": "@sol",
                    "thinking": "high", "memory": "8G", "session": "/s/2026_old.jsonl", "status": "waiting",
                    "pr": self.url, "runs": [{"run": 1, "event": "start", "start": "2026-10-01T00:00:00Z",
                                              "end": "2026-10-01T01:00:00Z", "exit": 0, "cost_usd": 1.5,
                                              "accepted": "2026-10-01T00:00:01Z", "launcher": "systemd"}]})
        self.assertIn(f"demo-old-1\twaiting\texit 0\t$1.50\t{self.url}\t2026-10-01T01:00:00Z\n", self.cli("lane", "list").stdout)
        self.assertEqual(self.cli("lane", "resume", "demo-old-1", "rebase").stdout, "demo-old-1 run 2\n")
        self.cli("lane", "wait", "demo-old-1")
        self.assertEqual(self.log("omp.log")[0]["args"][-3:], ["--resume", "/s/2026_old.jsonl", "rebase"])


if __name__ == "__main__":
    unittest.main()

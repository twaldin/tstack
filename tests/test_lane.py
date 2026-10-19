import json
import shutil
import subprocess
from pathlib import Path

from support import Sandbox, git


class LaneRuns(Sandbox):
    def setUp(self):
        super().setUp()
        # A local origin whose path reads as github.com/acme/demo, so lanes see the slug acme/demo.
        self.origin = self.root / "github.com" / "acme" / "demo.git"
        self.main = self.clone(self.origin, self.home / "worktrees" / "demo" / "main")

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
        call = self.log("omp")[0]
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
        first, second = self.log("omp")
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

    def test_stop_returns_after_omp_exits_and_wait_sees_it(self):
        self.brief.write_text("SLOW\n")
        lane_id = self.start()
        run = self.seat / "state" / "run" / "lanes" / lane_id
        self.assertTrue(self.until(lambda: (run / "run-1.jsonl").exists() and (run / "run-1.jsonl").read_text()))
        omp = (self.stubs / "omp.pid").read_text()
        self.assertEqual(self.cli("lane", "stop", lane_id).stdout, f"{lane_id} stopped (run 1)\n")
        self.assertEqual(subprocess.run(["ps", "-p", omp], capture_output=True).returncode, 1)
        self.assertEqual(self.cli("lane", "wait", lane_id).stdout, f"{lane_id} stopped exit 143\n(no final answer)\n")
        self.assertEqual(self.cli("lane", "resume", lane_id, "go on").stdout, f"{lane_id} run 2\n")

    def test_wait_returns_the_first_lane_to_finish_and_resume_names_a_running_one(self):
        self.brief.write_text("SLOW\n")
        slow = self.start()
        self.brief.write_text("quick\n")
        quick = self.start()
        self.assertEqual(self.cli("lane", "wait", slow, quick).stdout, f"{quick} exited exit 0\ndone\n")
        result = self.cli("lane", "resume", slow, "more", check=False)
        self.assertEqual(result.stderr, f"lane: {slow} is running run 1; `lane stop {slow}` first\n")
        self.cli("lane", "stop", slow)

    def test_systemd_runs_a_user_unit_that_stop_stops(self):
        self.brief.write_text("Print ${HOME} and $PATH as written.\n")
        lane_id = self.start("--memory", "4G")
        self.assertEqual(self.cli("lane", "wait", lane_id).stdout, f"{lane_id} exited exit 0\ndone\n")
        self.assertTrue(self.log("omp")[0]["args"][-1].endswith("Print ${HOME} and $PATH as written.\n"))
        spawn = self.log("systemd-run")[0]["args"]
        self.assertEqual(spawn[:4], ["--user", "--collect", "--quiet", f"--unit=tstack-lane-{lane_id}"])
        self.assertIn("MemoryMax=4G", spawn)
        self.assertIn(f"--setenv=TSTACK_SEAT={self.seat}", spawn)
        self.brief.write_text("SLOW\n")
        slow = self.start()
        self.assertEqual(self.cli("lane", "stop", slow).stdout, f"{slow} stopped (run 1)\n")
        self.assertIn(["--user", "stop", f"tstack-lane-{slow}"], [c["args"] for c in self.log("systemctl")])

    def test_darwin_run_detaches_without_systemd_and_keeps_literal_prompt(self):
        self.brief.write_text("Print ${HOME} and $PATH as written.\n")
        lane_id = self.cli_platform("darwin", "lane", "start", "Mac", "--repo", "acme/demo",
                                    "--brief", str(self.brief), "--memory", "1").stdout.strip()
        self.assertEqual(self.cli("lane", "wait", lane_id).stdout, f"{lane_id} exited exit 0\ndone\n")
        self.assertEqual(self.log("systemd-run"), [])
        self.assertEqual(self.log("systemctl"), [])
        self.assertTrue(self.log("omp")[0]["args"][-1].endswith("Print ${HOME} and $PATH as written.\n"))

    def test_darwin_stop_records_exit_and_leaves_another_lane_running(self):
        self.brief.write_text("SLOW\n")
        def start_mac(name):
            return self.cli_platform("darwin", "lane", "start", name, "--repo", "acme/demo",
                                     "--brief", str(self.brief)).stdout.strip()
        lane_id, other = start_mac("Mac"), start_mac("Other")
        self.addCleanup(lambda: self.cli_platform("darwin", "lane", "stop", other))
        stream = self.seat / "state/run/lanes" / lane_id / "run-1.jsonl"
        self.assertTrue(self.until(lambda: stream.exists() and stream.read_text()))
        self.assertEqual(self.cli_platform("darwin", "lane", "stop", lane_id).stdout, f"{lane_id} stopped (run 1)\n")
        self.assertEqual(self.cli("lane", "wait", lane_id).stdout, f"{lane_id} stopped exit 143\n(no final answer)\n")
        self.assertTrue(self.cli("lane", "show", other).stdout.startswith(f"{other}: running,"))
        self.assertEqual(self.log("systemctl"), [])
        self.assertEqual(self.cli_platform("darwin", "lane", "resume", lane_id, "go on").stdout, f"{lane_id} run 2\n")
        self.assertEqual(self.cli("lane", "wait", lane_id).stdout, f"{lane_id} exited exit 0\ndone\n")
        self.assertIn("removed worktree", self.cli("lane", "clean", lane_id).stdout)

    def test_clean_removes_the_worktree_and_branch_from_the_clone_the_lane_came_from(self):
        other = self.root / "elsewhere"
        git(self.root, "clone", "--quiet", str(self.origin), str(other))
        lane_id = self.cli("lane", "start", "Fix it", "--repo", str(other), "--brief", str(self.brief)).stdout.strip()
        self.cli("lane", "wait", lane_id)
        sha = git(other, "rev-parse", "--short", f"lane/{lane_id}")
        worktree = self.home / "worktrees" / "demo" / lane_id
        self.assertEqual(self.cli("lane", "clean", lane_id).stdout,
                         f"removed worktree {worktree}\nDeleted branch lane/{lane_id} (was {sha}).\n")
        self.assertFalse(worktree.exists())
        self.assertEqual(self.cli("lane", "list").stdout, "")
        self.assertTrue(self.cli("lane", "list", "--all").stdout.startswith(f"{lane_id}\texited"))

    def test_live_stream_drops_updates_but_keeps_logs_session_cost_and_final(self):
        self.omp(answer="answer", cost=0.7, events=[
            123, [], None, "not an event",
            {"type": "tool_execution_update", "partialResult": "discard me"},
            {"type": "tool_execution_start", "toolName": "read", "intent": "Reading file"},
            {"type": "message_update", "delta": "discard me too"}])
        lane_id = self.start()
        self.assertEqual(self.cli("lane", "wait", lane_id).stdout, f"{lane_id} exited exit 0\nanswer\n")
        stream = self.seat / "state" / "run" / "lanes" / lane_id / "run-1.jsonl"
        self.assertEqual([json.loads(line)["type"] for line in stream.read_text().splitlines()],
                         ["session", "tool_execution_start", "message_end"])
        self.assertEqual(self.cli("lane", "logs", lane_id).stdout, "> read: Reading file\nanswer\n")
        self.assertEqual(self.record(lane_id)["runs"][0]["cost_usd"], 0.7)

    def merged(self, lane_id, head):
        path = self.seat / "state" / "lanes" / lane_id / "lane.json"
        record = self.record(lane_id)
        record.update(pr="https://github.com/acme/demo/pull/7", merge="f" * 40, merged_head=head)
        path.write_text(json.dumps(record))

    def test_refresh_cleans_merged_worktree_branch_and_stream_but_preserves_show(self):
        lane_id = self.start()
        self.cli("lane", "wait", lane_id)
        worktree = Path(self.record(lane_id)["worktree"])
        self.merged(lane_id, git(worktree, "rev-parse", "HEAD"))
        show = self.cli("lane", "show", lane_id).stdout.splitlines()
        self.assertEqual(show[-2:], ["final answer (last 20 lines):", "done"])
        self.assertIn(f"worktree {worktree} (removed)", show)
        self.assertFalse(worktree.exists())
        self.assertEqual(git(self.main, "branch", "--list", f"lane/{lane_id}"), "")
        self.assertEqual(sorted(p.name for p in (self.seat / "state/run/lanes" / lane_id).iterdir()), ["lock"])
        self.assertEqual(self.cli("lane", "wait", lane_id).stdout, f"{lane_id} exited exit 0\ndone\n")

    def test_list_preserves_finished_and_merged_lane_files(self):
        lane_id = self.start()
        record_path = self.seat / "state/lanes" / lane_id / "lane.json"
        stream = self.seat / "state/run/lanes" / lane_id / "run-1.jsonl"
        exit_file = stream.with_suffix(".exit")
        self.assertTrue(self.until(exit_file.exists))
        worktree = Path(self.record(lane_id)["worktree"])
        (self.stubs / "pr-list.json").write_text(json.dumps([{"url": "https://github.com/acme/demo/pull/7"}]))
        for merged in (False, True):
            with self.subTest(merged=merged):
                record = self.record(lane_id)
                record["runs"][0]["compacted"] = False
                if merged:
                    record.update(status="exited", merge="f" * 40, merged_head=git(worktree, "rev-parse", "HEAD"))
                    record["runs"][0].update(end="2026-01-01T00:00:00Z", exit=0)
                record_path.write_text(json.dumps(record))
                with stream.open("a") as output:
                    output.write(json.dumps({"type": "message_update", "delta": "legacy"}) + "\n")
                def snapshot():
                    return {str(p.relative_to(self.seat)): (p.read_bytes(), p.stat().st_mtime_ns)
                            for p in self.seat.rglob("*") if p.is_file()}
                before = snapshot()
                for args in ((), ("--all",)):
                    result = self.cli("lane", "list", *args)
                    self.assertIn(f"{lane_id}\t{'exited' if merged else 'running'}\t", result.stdout)
                    self.assertEqual(result.stderr, "")
                    self.assertEqual(snapshot(), before)
                    self.assertTrue(worktree.is_dir())
                    self.assertEqual(git(worktree, "branch", "--show-current"), f"lane/{lane_id}")
                self.assertEqual(self.log("gh"), [])

    def test_show_records_external_merge_and_cleans_matching_worktree(self):
        lane_id = self.start()
        self.cli("lane", "wait", lane_id)
        worktree = Path(self.record(lane_id)["worktree"])
        head = git(worktree, "rev-parse", "HEAD")
        url = "https://github.com/acme/demo/pull/7"
        (self.stubs / "pr-list.json").write_text(json.dumps([{"url": url}]))
        (self.stubs / "pr-view.json").write_text(json.dumps(
            {"state": "MERGED", "headRefOid": head, "mergeCommit": {"oid": "f" * 40}}))
        self.assertIn(f"worktree {worktree} (removed)", self.cli("lane", "show", lane_id).stdout)
        self.assertEqual((self.record(lane_id)["merged_head"], self.record(lane_id)["merge"]), (head, "f" * 40))
        self.assertFalse(worktree.exists())
        self.assertEqual(git(self.main, "branch", "--list", f"lane/{lane_id}"), "")
        self.assertEqual(self.cli("lane", "wait", lane_id).stdout, f"{lane_id} exited exit 0\ndone\n")

    def test_wait_records_external_merge_but_preserves_unpublished_head(self):
        lane_id = self.start()
        self.cli("lane", "wait", lane_id)
        worktree = Path(self.record(lane_id)["worktree"])
        head = git(worktree, "rev-parse", "HEAD")
        git(worktree, "commit", "--allow-empty", "-m", "unpublished")
        path = self.seat / "state/lanes" / lane_id / "lane.json"
        record = self.record(lane_id)
        record["pr"] = "https://github.com/acme/demo/pull/7"
        path.write_text(json.dumps(record))
        (self.stubs / "pr-view.json").write_text(json.dumps(
            {"state": "MERGED", "headRefOid": head, "mergeCommit": {"oid": "f" * 40}}))
        result = self.cli("lane", "wait", lane_id)
        self.assertEqual(result.stdout, f"{lane_id} exited exit 0\ndone\n")
        self.assertEqual(result.stderr, f"lane: {lane_id} cleanup: left worktree: HEAD differs from merged head\n")
        self.assertEqual((self.record(lane_id)["merged_head"], self.record(lane_id)["merge"]), (head, "f" * 40))
        self.assertEqual(git(worktree, "log", "-1", "--format=%s"), "unpublished")

    def test_merged_cleanup_preserves_dirty_worktree(self):
        lane_id = self.start()
        self.cli("lane", "wait", lane_id)
        worktree = Path(self.record(lane_id)["worktree"])
        self.merged(lane_id, git(worktree, "rev-parse", "HEAD"))
        (worktree / "untracked").write_text("keep\n")
        self.assertEqual(self.cli("lane", "clean", lane_id).stdout, "left dirty worktree\n")
        self.assertEqual((worktree / "untracked").read_text(), "keep\n")
        self.assertEqual(git(worktree, "branch", "--show-current"), f"lane/{lane_id}")

    def test_clean_preserves_unpushed_commits_even_after_merge(self):
        lane_id = self.start()
        self.cli("lane", "wait", lane_id)
        worktree = Path(self.record(lane_id)["worktree"])
        head = git(worktree, "rev-parse", "HEAD")
        git(worktree, "commit", "--allow-empty", "-m", "unpublished")
        self.assertEqual(self.cli("lane", "clean", lane_id).stdout, "left worktree: unpushed commits\n")
        self.merged(lane_id, head)
        self.assertEqual(self.cli("lane", "clean", lane_id).stdout, "left worktree: HEAD differs from merged head\n")
        self.assertEqual(git(worktree, "log", "-1", "--format=%s"), "unpublished")

    def test_completed_inspections_work_offline_without_refresh_pr_discovery(self):
        lane_id = self.start()
        (self.stubs / "pr-list.err").write_text("error connecting to api.github.com")
        (self.stubs / "pr-view.err").write_text("error connecting to api.github.com")
        self.assertEqual(self.cli("lane", "wait", lane_id).stdout, f"{lane_id} exited exit 0\ndone\n")
        self.assertEqual(self.log("gh"), [])
        self.assertEqual(self.cli("lane", "logs", lane_id).stdout, "done\n")
        show = self.cli("lane", "show", lane_id)
        self.assertIn("PR ? (gh pr list --repo: error connecting to api.github.com)", show.stdout)
        self.assertEqual(show.stdout.splitlines()[-1], "done")
        listing = self.cli("lane", "list").stdout
        self.assertIn(f"{lane_id}\texited\texit 0\t$0.02\t-\t", listing)
        self.assertEqual([call["args"][:2] for call in self.log("gh")], [["pr", "list"]])

    def test_recorded_pr_outage_does_not_block_local_lifecycle(self):
        lane_id = self.start()
        self.cli("lane", "wait", lane_id)
        path = self.seat / "state/lanes" / lane_id / "lane.json"
        record = self.record(lane_id)
        record["pr"] = "https://github.com/acme/demo/pull/7"
        path.write_text(json.dumps(record))
        before = path.read_bytes()
        (self.stubs / "pr-view.err").write_text("error connecting to api.github.com")
        for command in ("wait", "show", "stop"):
            result = self.cli("lane", command, lane_id)
            self.assertIn("error connecting to api.github.com", result.stderr)
            self.assertIn("done" if command != "stop" else "is not running", result.stdout)
            self.assertEqual(path.read_bytes(), before)
        self.omp(answer="resumed")
        self.assertEqual(self.cli("lane", "resume", lane_id, "continue offline").stdout, f"{lane_id} run 2\n")
        self.assertEqual(self.cli("lane", "wait", lane_id).stdout, f"{lane_id} exited exit 0\nresumed\n")

    def test_external_merge_without_required_sha_preserves_worktree(self):
        for field in ("mergeCommit", "headRefOid"):
            with self.subTest(field=field):
                lane_id = self.start()
                self.cli("lane", "wait", lane_id)
                worktree = Path(self.record(lane_id)["worktree"])
                head = git(worktree, "rev-parse", "HEAD")
                path = self.seat / "state/lanes" / lane_id / "lane.json"
                record = self.record(lane_id)
                record["pr"] = "https://github.com/acme/demo/pull/7"
                path.write_text(json.dumps(record))
                pr = {"state": "MERGED", "headRefOid": head, "mergeCommit": {"oid": "f" * 40}}
                pr[field] = None
                (self.stubs / "pr-view.json").write_text(json.dumps(pr))
                self.assertEqual(self.cli("lane", "wait", lane_id).stdout, f"{lane_id} exited exit 0\ndone\n")
                self.assertTrue(worktree.is_dir())
                self.assertEqual(git(worktree, "rev-parse", "HEAD"), head)

    def test_detached_and_different_branch_cleanup_are_visible_safe_skips(self):
        lane_id = self.start()
        self.cli("lane", "wait", lane_id)
        worktree = Path(self.record(lane_id)["worktree"])
        self.merged(lane_id, git(worktree, "rev-parse", "HEAD"))
        git(worktree, "checkout", "--detach", "--quiet")
        for command in ("show", "wait"):
            result = self.cli("lane", command, lane_id)
            self.assertEqual(result.stderr, f"lane: {lane_id} cleanup: left worktree on detached HEAD\n")
            self.assertIn("done", result.stdout)
        self.assertEqual(self.record(lane_id)["cleanup"], "left worktree on detached HEAD")
        self.assertEqual(self.cli("lane", "clean", lane_id).stdout, "left worktree on detached HEAD\n")
        git(worktree, "checkout", "--quiet", "-b", "other")
        result = self.cli("lane", "show", lane_id)
        self.assertEqual(result.stderr, f"lane: {lane_id} cleanup: left worktree on another branch\n")
        self.assertEqual(self.record(lane_id)["cleanup"], "left worktree on another branch")
        self.assertEqual(git(worktree, "branch", "--show-current"), "other")
        self.assertEqual(git(self.main, "branch", "--list", f"lane/{lane_id}"), f"lane/{lane_id}")

    def test_cleanup_errors_are_visible_and_persisted_but_explicit_clean_fails(self):
        lane_id = self.start()
        self.cli("lane", "wait", lane_id)
        worktree = Path(self.record(lane_id)["worktree"])
        self.merged(lane_id, git(worktree, "rev-parse", "HEAD"))
        git(self.main, "worktree", "lock", str(worktree))
        for command in ("show", "wait"):
            result = self.cli("lane", command, lane_id)
            fact = self.record(lane_id)["cleanup"]
            self.assertTrue(fact.startswith("error: git "))
            self.assertIn("locked", fact)
            self.assertEqual(result.stderr, f"lane: {lane_id} cleanup: {fact}\n")
            self.assertIn("done", result.stdout)
        path = self.seat / "state" / "lanes" / lane_id / "lane.json"
        before = path.stat().st_mtime_ns
        self.assertEqual(self.cli("lane", "logs", lane_id).stdout, "done\n")
        self.assertEqual(path.stat().st_mtime_ns, before)
        result = self.cli("lane", "clean", lane_id, check=False)
        self.assertEqual((result.returncode, result.stderr), (1, f"lane: {fact.removeprefix('error: ')}\n"))
        self.assertTrue(worktree.is_dir())
        git(self.main, "worktree", "unlock", str(worktree))
        self.assertIn("(removed)", self.cli("lane", "show", lane_id).stdout)
        self.assertTrue(self.record(lane_id)["cleaned"])

    def test_clean_recovers_historical_missing_worktree_from_canonical_repo(self):
        lane_id = self.start()
        self.cli("lane", "wait", lane_id)
        worktree = Path(self.record(lane_id)["worktree"])
        self.merged(lane_id, git(worktree, "rev-parse", "HEAD"))
        path = self.seat / "state" / "lanes" / lane_id / "lane.json"
        record = self.record(lane_id)
        del record["cleanup_repo"]
        path.write_text(json.dumps(record))
        shutil.rmtree(worktree)
        result = self.cli("lane", "clean", lane_id)
        self.assertIn(f"Deleted branch lane/{lane_id}", result.stdout)
        self.assertEqual(git(self.main, "branch", "--list", f"lane/{lane_id}"), "")
        self.assertNotIn(str(worktree), git(self.main, "worktree", "list"))
        self.assertEqual(sorted(p.name for p in (self.seat / "state/run/lanes" / lane_id).iterdir()), ["lock"])
        self.assertEqual(self.cli("lane", "show", lane_id).stdout.splitlines()[-1], "done")

    def test_clean_uses_stored_original_repo_when_worktree_is_already_removed(self):
        other = self.root / "elsewhere"
        git(self.root, "clone", "--quiet", str(self.origin), str(other))
        lane_id = self.cli("lane", "start", "Fix it", "--repo", str(other), "--brief", str(self.brief)).stdout.strip()
        self.cli("lane", "wait", lane_id)
        worktree = Path(self.record(lane_id)["worktree"])
        self.merged(lane_id, git(worktree, "rev-parse", "HEAD"))
        self.assertEqual(Path(self.record(lane_id)["cleanup_repo"]), other / ".git")
        shutil.rmtree(worktree)
        self.assertIn(f"Deleted branch lane/{lane_id}", self.cli("lane", "clean", lane_id).stdout)
        self.assertEqual(git(other, "branch", "--list", f"lane/{lane_id}"), "")
        self.assertTrue(self.record(lane_id)["cleaned"])

    def test_local_no_origin_clean_requires_head_reachable_from_local_base(self):
        git(self.main, "remote", "remove", "origin")
        lane_id = self.cli("lane", "start", "Local", "--repo", str(self.main), "--brief", str(self.brief)).stdout.strip()
        self.cli("lane", "wait", lane_id)
        worktree = Path(self.record(lane_id)["worktree"])
        git(worktree, "commit", "--allow-empty", "-m", "unpublished")
        self.assertEqual(self.cli("lane", "clean", lane_id).stdout, "left worktree: unpushed commits\n")
        self.assertEqual(git(worktree, "log", "-1", "--format=%s"), "unpublished")
        git(self.main, "merge", "--ff-only", f"lane/{lane_id}")
        self.assertIn(f"removed worktree {worktree}", self.cli("lane", "clean", lane_id).stdout)
        self.assertFalse(worktree.exists())
        self.assertEqual(git(self.main, "branch", "--list", f"lane/{lane_id}"), "")
        self.assertEqual(self.log("gh"), [])

    def test_missing_local_no_origin_worktree_preserves_unpublished_branch_and_stream(self):
        git(self.main, "remote", "remove", "origin")
        lane_id = self.cli("lane", "start", "Local", "--repo", str(self.main), "--brief", str(self.brief)).stdout.strip()
        self.cli("lane", "wait", lane_id)
        worktree = Path(self.record(lane_id)["worktree"])
        git(worktree, "commit", "--allow-empty", "-m", "unpublished")
        shutil.rmtree(worktree)
        path = self.seat / "state" / "lanes" / lane_id / "lane.json"
        record = self.record(lane_id)
        del record["cleanup_repo"]
        path.write_text(json.dumps(record))
        self.assertEqual(self.cli("lane", "clean", lane_id).stdout, "left branch: unpushed commits\n")
        self.assertEqual(git(self.main, "log", "-1", "--format=%s", f"lane/{lane_id}"), "unpublished")
        self.assertEqual(self.cli("lane", "logs", lane_id).stdout, "done\n")
        git(self.main, "merge", "--ff-only", f"lane/{lane_id}")
        self.assertIn(f"Deleted branch lane/{lane_id}", self.cli("lane", "clean", lane_id).stdout)
        self.assertEqual(self.cli("lane", "logs", lane_id).stdout, "")

    def test_resume_missing_or_cleaned_worktree_does_not_append_a_run(self):
        for remove in ("missing", "cleaned", "auto-cleaned"):
            with self.subTest(remove=remove):
                lane_id = self.start()
                self.cli("lane", "wait", lane_id)
                if remove == "cleaned":
                    self.cli("lane", "clean", lane_id)
                elif remove == "auto-cleaned":
                    worktree = Path(self.record(lane_id)["worktree"])
                    self.merged(lane_id, git(worktree, "rev-parse", "HEAD"))
                    self.assertIn("(removed)", self.cli("lane", "show", lane_id).stdout)
                else:
                    shutil.rmtree(Path(self.record(lane_id)["worktree"]))
                before = self.record(lane_id)
                result = self.cli("lane", "resume", lane_id, "more", check=False)
                self.assertEqual((result.returncode, result.stderr),
                                 (1, f"lane: {lane_id} worktree is missing or cleaned; cannot resume\n"))
                self.assertEqual(self.record(lane_id), before)
        self.assertEqual(len(self.log("systemd-run")), 3)

    def test_explicit_clean_can_discover_an_external_merge(self):
        lane_id = self.start()
        self.cli("lane", "wait", lane_id)
        worktree = Path(self.record(lane_id)["worktree"])
        head = git(worktree, "rev-parse", "HEAD")
        url = "https://github.com/acme/demo/pull/7"
        (self.stubs / "pr-list.json").write_text(json.dumps([{"url": url}]))
        (self.stubs / "pr-view.json").write_text(json.dumps(
            {"state": "MERGED", "headRefOid": head, "mergeCommit": {"oid": "f" * 40}}))
        self.assertEqual(self.cli("lane", "logs", lane_id).stdout, "done\n")
        self.assertTrue(worktree.is_dir())
        self.assertIn(f"removed worktree {worktree}", self.cli("lane", "clean", lane_id).stdout)
        self.assertEqual((self.record(lane_id)["merged_head"], self.record(lane_id)["merge"]), (head, "f" * 40))
        self.assertEqual([call["args"][:2] for call in self.log("gh")], [["pr", "list"], ["pr", "view"]])


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
                      [c["args"] for c in self.log("gh")])
        self.assertEqual((self.record("demo-x-1")["pr"], self.record("demo-x-1")["merge"]), (self.url, "f" * 40))

    def test_github_merge_refusals_pass_through(self):
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
        self.assertEqual(self.log("omp")[0]["args"][-3:], ["--resume", "/s/2026_old.jsonl", "rebase"])

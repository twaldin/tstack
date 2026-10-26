import json
import os
import signal
import shutil
import shlex
import subprocess
import sys
from pathlib import Path

from support import Sandbox, git


class LaneRuns(Sandbox):
    def setUp(self):
        super().setUp()
        # A local origin whose path reads as github.com/acme/demo, so lanes see the slug acme/demo.
        self.origin = self.root / "github.com" / "acme" / "demo.git"
        self.main = self.clone(self.origin, self.home / "worktrees" / "demo" / "main")

    def start(self, *extra) -> str:
        return self.cli("lane", "start", "--max-time", "10m", "Fix it", "--repo", "acme/demo",
                        "--brief", str(self.brief), *extra).stdout.strip()

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

    def test_start_without_max_time_refuses_before_creating_or_launching_a_lane(self):
        before = {str(p.relative_to(self.root)): p.read_bytes() if p.is_file() else None
                  for p in self.root.rglob("*")}
        result = self.cli("lane", "start", "Fix it", "--repo", "acme/demo",
                          "--brief", str(self.brief), check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--max-time", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual({str(p.relative_to(self.root)): p.read_bytes() if p.is_file() else None
                          for p in self.root.rglob("*")}, before)
        self.assertEqual(self.log("omp"), [])
        self.assertEqual(self.log("systemd-run"), [])

    def test_resume_without_max_time_preserves_the_finished_run_and_does_not_launch(self):
        lane_id = self.start()
        self.cli("lane", "wait", lane_id)
        self.assertTrue(self.until(
            lambda: (self.stubs / f"tstack-lane-{lane_id}.state").read_text() == "inactive"))
        before = {str(p.relative_to(self.root)): p.read_bytes() if p.is_file() else None
                  for p in self.root.rglob("*")}
        result = self.cli("lane", "resume", lane_id, "continue", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--max-time", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual({str(p.relative_to(self.root)): p.read_bytes() if p.is_file() else None
                          for p in self.root.rglob("*")}, before)
        self.assertEqual(len(self.log("omp")), 1)
        self.assertEqual(len(self.log("systemd-run")), 1)

    def test_each_run_launches_persists_and_shows_its_explicit_budget(self):
        for platform in ("linux", "darwin"):
            with self.subTest(platform=platform):
                lane_id = self.cli_platform(platform, "lane", "start", "Budget", "--repo", "acme/demo",
                                            "--brief", str(self.brief), "--max-time", "7m").stdout.strip()
                self.cli_platform(platform, "lane", "wait", lane_id)
                self.assertEqual(self.record(lane_id)["runs"][0]["max_time"], "7m")
                shown = self.cli_platform(platform, "lane", "show", lane_id).stdout.splitlines()
                self.assertIn("max-time 7m", next(row for row in shown if row.startswith("run 1:")))
                self.cli_platform(platform, "lane", "resume", lane_id, "continue", "--max-time", "90s")
                self.cli_platform(platform, "lane", "wait", lane_id)
                self.assertEqual([run["max_time"] for run in self.record(lane_id)["runs"]], ["7m", "90s"])
                shown = self.cli_platform(platform, "lane", "show", lane_id).stdout.splitlines()
                for number, budget in ((1, "7m"), (2, "90s")):
                    self.assertIn(f"max-time {budget}",
                                  next(row for row in shown if row.startswith(f"run {number}:")))
                calls = [call for call in self.log("omp") if call["lane"] == lane_id]
                self.assertEqual(len(calls), 2)
                budgets = []
                for call in calls:
                    args = call["args"]
                    budgets.append(args[args.index("--max-time") + 1] if "--max-time" in args else
                                   next(arg.split("=", 1)[1] for arg in args if arg.startswith("--max-time=")))
                self.assertEqual(budgets, ["7m", "90s"])

    def test_run_end_reports_the_child_killed_by_unit_stop(self):
        self.omp(answer="left running", child=True)
        lane_id = self.start()
        result = self.cli("lane", "wait", lane_id).stdout
        pid = int((self.stubs / "child.pid").read_text())
        line = f"run 1 ended with 1 process: killed by unit stop: {pid} sleep 600"
        self.assertEqual(result, f"{lane_id} exited exit 0\n{line}\nleft running\n")
        self.assertIn(line, self.cli("lane", "show", lane_id).stdout.splitlines())
        self.omp(answer="clean")
        self.cli("lane", "resume", "--max-time", "10m", lane_id, "finish cleanly")
        self.assertEqual(self.cli("lane", "wait", lane_id).stdout, f"{lane_id} exited exit 0\nclean\n")
        self.assertIn(line, self.cli("lane", "show", lane_id).stdout.splitlines())
        self.cli("lane", "clean", lane_id)
        self.assertFalse((self.seat / "state/run/lanes" / lane_id / "run-1.processes").exists())
        self.assertIn(line, self.cli("lane", "show", lane_id).stdout.splitlines())

    def test_run_end_excludes_omp_workers_but_keeps_lane_children(self):
        for platform in ("linux", "darwin"):
            for child in (False, True):
                with self.subTest(platform=platform, child=child):
                    self.omp(answer="done", worker=True, child=child)
                    lane_id = self.cli_platform(platform, "lane", "start", "--max-time", "10m", "Fix it", "--repo", "acme/demo",
                                                "--brief", str(self.brief)).stdout.strip()
                    try:
                        output = self.cli_platform(platform, "lane", "wait", lane_id).stdout
                        lines = []
                        if child:
                            pid = int((self.stubs / "child.pid").read_text())
                            label = ("killed by unit stop" if platform == "linux" else
                                     "still running (partial: macOS hides environments of system binaries)")
                            lines = [f"run 1 ended with 1 process: {label}: {pid} sleep 600"]
                        self.assertEqual(output, "\n".join([f"{lane_id} exited exit 0", *lines, "done", ""]))
                        shown = self.cli_platform(platform, "lane", "show", lane_id).stdout.splitlines()
                        self.assertEqual([line for line in shown if line.startswith("run 1 ended with")], lines)
                    finally:
                        if platform == "darwin":
                            for name in (("worker", "child") if child else ("worker",)):
                                os.kill(int((self.stubs / f"{name}.pid").read_text()), signal.SIGTERM)

    def test_linux_run_reports_a_unit_child_that_cleared_its_environment(self):
        self.omp(answer="left running", child=True, clear_env=True)
        lane_id = self.start()
        output = self.cli("lane", "wait", lane_id).stdout
        pid = int((self.stubs / "child.pid").read_text())
        line = f"run 1 ended with 1 process: killed by unit stop: {pid} sleep 600"
        self.assertIn(line, output.splitlines())
        self.assertIn(line, self.cli("lane", "show", lane_id).stdout.splitlines())

    def test_nohup_run_end_reports_the_child_left_running(self):
        self.omp(answer="left running", child=True, detached=True)
        lane_id = self.cli_platform("darwin", "lane", "start", "--max-time", "10m", "Fix it", "--repo", "acme/demo",
                                    "--brief", str(self.brief)).stdout.strip()
        try:
            result = self.cli_platform("darwin", "lane", "wait", lane_id).stdout
            pid = int((self.stubs / "child.pid").read_text())
            line = (f"run 1 ended with 1 process: still running "
                    f"(partial: macOS hides environments of system binaries): {pid} sleep 600")
            self.assertEqual(result, f"{lane_id} exited exit 0\n{line}\nleft running\n")
            self.assertIn(line, self.cli_platform("darwin", "lane", "show", lane_id).stdout.splitlines())
            os.kill(pid, 0)
            self.assertNotEqual(os.getpgid(pid), int((self.seat / "state/run/lanes" / lane_id / "run-1.pid").read_text()))
            self.assertNotIn("must-not-save", json.dumps(self.record(lane_id)["runs"][0]["processes"]))
        finally:
            if (self.stubs / "child.pid").exists():
                os.kill(int((self.stubs / "child.pid").read_text()), signal.SIGTERM)
        self.omp(answer="clean")
        self.cli_platform("darwin", "lane", "resume", "--max-time", "10m", lane_id, "finish cleanly")
        self.assertEqual(self.cli_platform("darwin", "lane", "wait", lane_id).stdout,
                         f"{lane_id} exited exit 0\nclean\n")

    def test_stop_and_oom_report_only_children_without_prompt_or_unbounded_commands(self):
        command = [sys.executable, "-c", "import time; time.sleep(600)", "x" * 200]
        expected = " ".join(command)[:117] + "..."
        for oom in (False, True):
            with self.subTest(oom=oom):
                self.brief.write_text("SLOW PRIVATE_PROMPT_MUST_NOT_BE_RECORDED\n")
                self.omp(child=True, child_command=command, detached=True, oom=oom)
                lane_id = self.start()
                if not oom:
                    self.assertTrue(self.until(lambda: (self.stubs / "child.pid").exists()))
                    self.cli("lane", "stop", lane_id)
                output = self.cli("lane", "wait", lane_id).stdout
                pid = int((self.stubs / "child.pid").read_text())
                reason = "oom-kill" if oom else "stopped"
                line = f"run 1 ended with 1 process: {reason}: killed by unit stop: {pid} {expected}"
                self.assertIn(line, output.splitlines())
                shown = self.cli("lane", "show", lane_id).stdout
                self.assertIn(line, shown.splitlines())
                snapshot = self.record(lane_id)["runs"][0]["processes"]
                self.assertEqual(snapshot, {"reason": reason, "killed": [{"pid": pid, "command": expected}], "running": []})
                self.assertNotIn("PRIVATE_PROMPT", json.dumps(snapshot))
                (self.stubs / "child.pid").unlink()

    def test_start_records_playbook_and_reports_unknown_names_without_refusing(self):
        engine = self.root / "engine"
        playbooks = engine / "pstack" / "skills" / "poteto-mode" / "playbooks"
        playbooks.mkdir(parents=True)
        for name in ("feature", "bug-fix"):
            (playbooks / f"{name}.md").write_text(f"# {name}\n")
        (playbooks / "directory.md").mkdir()
        (playbooks / "notes.txt").write_text("not a playbook\n")
        (playbooks.parent / "outside.md").write_text("# Outside\n")
        self.env["TSTACK_HOME"] = str(engine)
        cases = [
            ("PLAYBOOK: feature\n", "feature", "", "feature"),
            ("PLAYBOOK: none\n", "none", "", "none"),
            ("", None, "", "(absent)"),
            ("PLAYBOOK: nonexistent\n", "nonexistent",
             "lane: unknown PLAYBOOK 'nonexistent'; valid names: bug-fix, feature, none\n", "nonexistent"),
            ("Playbook: Debugging\n", "Debugging",
             "lane: unknown PLAYBOOK 'Debugging'; valid names: bug-fix, feature, none\n", "Debugging"),
            ("PLAYBOOK: ../outside\n", "../outside",
             "lane: unknown PLAYBOOK '../outside'; valid names: bug-fix, feature, none\n", "../outside"),
            ("PLAYBOOK: feature.md\n", "feature.md",
             "lane: unknown PLAYBOOK 'feature.md'; valid names: bug-fix, feature, none\n", "feature.md"),
            ("PLAYBOOK: \tfeature \t\n", "feature", "", "feature"),
        ]
        for number, (field, saved, diagnostic, shown) in enumerate(cases, 1):
            with self.subTest(playbook=saved, field=field):
                self.brief.write_text(f"Task brief.\n{field}Reply with the word done.\n")
                result = self.cli("lane", "start", "--max-time", "10m", "Fix it", "--repo", "acme/demo", "--brief", str(self.brief))
                lane_id = f"demo-fix-it-{number}"
                self.assertEqual(result.stdout, f"{lane_id}\n")
                self.assertEqual(result.stderr, diagnostic)
                self.assertEqual(self.cli("lane", "wait", lane_id).stdout, f"{lane_id} exited exit 0\ndone\n")
                self.assertEqual(self.record(lane_id)["playbook"], saved)
                (self.seat / "state" / "lanes" / lane_id / "brief.md").write_text("PLAYBOOK: changed\n")
                self.assertIn(f"PLAYBOOK: {shown}", self.cli("lane", "show", lane_id).stdout.splitlines())

    def test_missing_playbook_directory_does_not_prevent_startup(self):
        self.env["TSTACK_HOME"] = str(self.root / "missing-engine")
        self.brief.write_text("PLAYBOOK: feature\nReply with the word done.\n")
        result = self.cli("lane", "start", "--max-time", "10m", "Fix it", "--repo", "acme/demo", "--brief", str(self.brief))
        self.assertEqual(result.stdout, "demo-fix-it-1\n")
        self.assertEqual(result.stderr, "lane: unknown PLAYBOOK 'feature'; valid names: none\n")
        self.assertEqual(self.cli("lane", "wait", "demo-fix-it-1").stdout, "demo-fix-it-1 exited exit 0\ndone\n")

    def test_resume_continues_the_same_session(self):
        lane_id = self.start()
        self.cli("lane", "wait", lane_id)
        self.omp(answer="fixed", cost=0.5)
        self.assertEqual(self.cli("lane", "resume", "--max-time", "10m", lane_id, "CI failed on test_x").stdout, f"{lane_id} run 2\n")
        self.assertEqual(self.cli("lane", "wait", lane_id).stdout, f"{lane_id} exited exit 0\nfixed\n")
        first, second = self.log("omp")
        session = self.record(lane_id)["session"]
        self.assertEqual(second["args"][-3:], ["--resume", session, "CI failed on test_x"])
        self.assertNotIn("--resume", first["args"])
        self.assertIn(f"{lane_id}\texited\texit 0\t$0.52\t-\t", self.cli("lane", "list").stdout)

    def test_resume_changes_the_cap_and_reuses_it_on_the_next_run(self):
        lane_id = self.start("--memory", "4G")
        self.cli("lane", "wait", lane_id)
        self.cli("lane", "resume", "--max-time", "10m", lane_id, "try with more memory", "--memory", "16G")
        self.cli("lane", "wait", lane_id)
        self.assertEqual(self.record(lane_id)["memory"], "16G")
        self.cli("lane", "resume", "--max-time", "10m", lane_id, "continue")
        self.cli("lane", "wait", lane_id)
        self.assertEqual(self.record(lane_id)["memory"], "16G")
        self.assertEqual([next(a for a in call["args"] if a.startswith("MemoryMax="))
                          for call in self.log("systemd-run")], ["MemoryMax=4G", "MemoryMax=16G", "MemoryMax=16G"])

    def test_rejected_resume_cap_preserves_the_previous_run_and_cap(self):
        self.omp(exit=137)
        lane_id = self.start("--memory", "4G")
        self.cli("lane", "wait", lane_id)
        before = self.record(lane_id)
        (self.stubs / "systemd-run.err").write_text("Failed to parse MemoryMax=16g")
        failed = self.cli("lane", "resume", "--max-time", "10m", lane_id, "retry", "--memory", "16g", check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("Failed to parse MemoryMax=16g", failed.stderr)
        after = self.record(lane_id)
        for key in ("memory", "status", "runs"):
            self.assertEqual(after[key], before[key])
        self.assertIn(f"{lane_id}\texited\texit 137\t", self.cli("lane", "list").stdout)
        (self.stubs / "systemd-run.err").unlink()
        self.omp(answer="recovered")
        self.cli("lane", "resume", "--max-time", "10m", lane_id, "retry with the saved cap")
        self.cli("lane", "wait", lane_id)
        self.assertEqual(self.record(lane_id)["memory"], "4G")
        self.assertEqual(len(self.record(lane_id)["runs"]), 2)

    def test_oom_outcome_survives_collection_and_list_does_not_write(self):
        self.omp(exit=137)
        lane_id = self.start("--memory", "8G")
        run = self.seat / "state/run/lanes" / lane_id
        self.assertTrue(self.until(lambda: (run / "run-1.processes").exists()))
        self.assertTrue(self.until(
            lambda: (self.stubs / f"tstack-lane-{lane_id}.state").read_text() == "inactive"))
        (self.stubs / "systemd-properties.json").write_text(json.dumps(
            {"LoadState": "loaded", "Result": "oom-kill", "MemoryPeak": 8 * 2**30, "MemoryMax": 8 * 2**30}))
        callback = next(a for a in self.log("systemd-run")[0]["args"] if a.startswith("ExecStopPost="))
        result = subprocess.run(shlex.split(callback.removeprefix("ExecStopPost=-")),
                                env={**self.env, "SERVICE_RESULT": "oom-kill"}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        (self.stubs / "systemd-properties.json").unlink()
        before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns)
                  for p in self.seat.rglob("*") if p.is_file()}
        listing = self.cli("lane", "list").stdout
        self.assertIn(f"{lane_id}\texited (oom-kill)\texit -; oom-kill (peak 8.0G of 8G cap)\t", listing)
        self.assertEqual({str(p): (p.read_bytes(), p.stat().st_mtime_ns)
                          for p in self.seat.rglob("*") if p.is_file()}, before)
        self.assertIn("exit 137; oom-kill (peak 8.0G of 8G cap)", self.cli("lane", "show", lane_id).stdout)
        self.assertEqual(self.record(lane_id)["runs"][0]["systemd"],
                         {"result": "oom-kill", "memory_peak": 8 * 2**30, "memory_max": 8 * 2**30})
        self.assertEqual(self.cli("lane", "wait", lane_id).stdout,
                         f"{lane_id} exited exit 137; oom-kill (peak 8.0G of 8G cap)\n(no final answer)\n")
        self.omp(answer="recovered")
        self.cli("lane", "resume", "--max-time", "10m", lane_id, "recover", "--memory", "16G")
        self.cli("lane", "wait", lane_id)
        self.assertIn("oom-kill (peak 8.0G of 8G cap)", self.cli("lane", "show", lane_id).stdout)
        self.assertIn("removed worktree", self.cli("lane", "clean", lane_id).stdout)
        self.assertFalse((run / "run-1.result").exists())

    def test_lazy_outcome_ignores_unavailable_memory_properties(self):
        self.omp(exit=137)
        lane_id = self.start()
        run = self.seat / "state/run/lanes" / lane_id
        self.assertTrue(self.until(lambda: (run / "run-1.exit").exists()))
        (self.stubs / "systemd-properties.json").write_text(json.dumps(
            {"LoadState": "loaded", "Result": "oom-kill", "MemoryPeak": "[not set]",
             "MemoryMax": "18446744073709551615"}))
        self.assertEqual(self.cli("lane", "wait", lane_id).stdout,
                         f"{lane_id} exited exit 137; oom-kill\n(no final answer)\n")
        self.assertEqual(self.record(lane_id)["runs"][0]["systemd"], {"result": "oom-kill"})

    def test_collected_unit_does_not_supply_a_fake_success_result(self):
        lane_id = self.start()
        (self.stubs / "systemd-properties.json").write_text(json.dumps(
            {"LoadState": "not-found", "Result": "success", "MemoryPeak": "[not set]"}))
        self.cli("lane", "wait", lane_id)
        self.assertNotIn("systemd", self.record(lane_id)["runs"][0])

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
        self.assertEqual(self.cli("lane", "resume", "--max-time", "10m", lane_id, "go on").stdout, f"{lane_id} run 2\n")

    def test_wait_returns_the_first_lane_to_finish_and_resume_names_a_running_one(self):
        self.brief.write_text("SLOW\n")
        slow = self.start()
        self.brief.write_text("quick\n")
        quick = self.start()
        self.assertEqual(self.cli("lane", "wait", slow, quick).stdout, f"{quick} exited exit 0\ndone\n")
        result = self.cli("lane", "resume", "--max-time", "10m", slow, "more", check=False)
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
        lane_id = self.cli_platform("darwin", "lane", "start", "--max-time", "10m", "Mac", "--repo", "acme/demo",
                                    "--brief", str(self.brief), "--memory", "1").stdout.strip()
        self.assertEqual(self.cli("lane", "wait", lane_id).stdout, f"{lane_id} exited exit 0\ndone\n")
        self.assertEqual(self.log("systemd-run"), [])
        self.assertEqual(self.log("systemctl"), [])
        self.assertTrue(self.log("omp")[0]["args"][-1].endswith("Print ${HOME} and $PATH as written.\n"))

    def slow_nohup(self):
        self.brief.write_text("SLOW\n")
        lane_id = self.cli_platform("darwin", "lane", "start", "--max-time", "10m", "Mac", "--repo", "acme/demo",
                                    "--brief", str(self.brief)).stdout.strip()
        run = self.seat / "state/run/lanes" / lane_id
        pid = int((run / "run-1.pid").read_text())
        def finish():
            (self.stubs / "ps-mode").unlink(missing_ok=True)
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            self.until(lambda: (run / "run-1.exit").exists())
        self.addCleanup(finish)
        self.assertTrue(self.until(lambda: (run / "run-1.jsonl").exists() and
                                  (run / "run-1.jsonl").read_text()))
        return lane_id, run

    def test_nohup_live_pid_survives_empty_failed_or_renamed_ps(self):
        for mode in ("empty-once", "error", "renamed"):
            with self.subTest(mode=mode):
                (self.stubs / "ps-mode").unlink(missing_ok=True)
                lane_id, run = self.slow_nohup()
                (self.stubs / "ps-mode").write_text(mode)
                output = self.cli_platform("darwin", "lane", "show", lane_id).stdout
                self.assertIn(f"{lane_id}: running, run 1 exit -", output)
                self.assertNotIn("end", self.record(lane_id)["runs"][0])
                self.assertIn(f"{lane_id}\trunning\texit -", self.cli("lane", "list").stdout)
                os.kill(int((run / "run-1.pid").read_text()), 0)

    def test_nohup_wait_does_not_return_on_empty_ps_while_pid_lives(self):
        lane_id, run = self.slow_nohup()
        (self.stubs / "ps-mode").write_text("empty-once")
        waiter = subprocess.Popen([sys.executable, str(Path(__file__).resolve().parents[1] / "bin/lane"),
                                   "wait", lane_id], env=self.env, cwd=self.seat,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            with self.assertRaises(subprocess.TimeoutExpired):
                waiter.communicate(timeout=1)
            os.kill(int((run / "run-1.pid").read_text()), 0)
            os.killpg(int((run / "run-1.pid").read_text()), signal.SIGTERM)
            output, error = waiter.communicate(timeout=15)
            self.assertEqual((waiter.returncode, error), (0, ""))
            self.assertEqual(output, f"{lane_id} exited exit 143\n(no final answer)\n")
        finally:
            if waiter.poll() is None:
                waiter.terminate()
                waiter.communicate(timeout=5)

    def test_resume_refuses_live_nohup_pid_despite_cached_end(self):
        lane_id, _ = self.slow_nohup()
        record = self.record(lane_id)
        record.update(status="exited", session="existing-session")
        record["runs"][0].update(end="2026-10-25T00:00:00Z", exit=7, cost_usd=1.25)
        (self.seat / "state/lanes" / lane_id / "lane.json").write_text(json.dumps(record))
        self.assertIn(f"{lane_id}\trunning\texit -", self.cli("lane", "list").stdout)
        self.assertNotIn("2026-10-25T00:00:00Z", self.cli("lane", "list").stdout)
        self.assertEqual(self.record(lane_id)["status"], "exited")
        result = self.cli_platform("darwin", "lane", "resume", "--max-time", "10m", lane_id, "continue", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is running run 1", result.stderr)
        self.assertEqual(len(self.record(lane_id)["runs"]), 1)
        self.assertEqual(len(self.log("omp")), 1)

    def test_nohup_missing_launch_identity_keeps_live_pid_running(self):
        (self.stubs / "ps-mode").write_text("error")
        lane_id, _ = self.slow_nohup()
        self.assertNotIn("pid_start", self.record(lane_id)["runs"][0])
        self.assertIn(f"{lane_id}: running,", self.cli_platform("darwin", "lane", "show", lane_id).stdout)
        (self.stubs / "ps-mode").unlink()
        self.assertIn(f"{lane_id}: running,", self.cli_platform("darwin", "lane", "show", lane_id).stdout)

    def test_nohup_reused_pid_does_not_keep_a_dead_run_running(self):
        lane_id, run = self.slow_nohup()
        record = self.record(lane_id)
        record["runs"][0]["pid_start"] = "Thu Jan 1 00:00:00 1970"
        (self.seat / "state/lanes" / lane_id / "lane.json").write_text(json.dumps(record))
        self.assertIn(f"{lane_id}: exited, run 1 exit -",
                      self.cli_platform("darwin", "lane", "show", lane_id).stdout)
        os.kill(int((run / "run-1.pid").read_text()), 0)

    def test_nohup_exit_file_wins_over_a_live_pid_and_failed_ps(self):
        lane_id, run = self.slow_nohup()
        (run / "run-1.exit").write_text("7\n")
        self.addCleanup((run / "run-1.exit").unlink, missing_ok=True)
        (self.stubs / "ps-mode").write_text("error")
        self.assertEqual(self.cli_platform("darwin", "lane", "wait", lane_id).stdout,
                         f"{lane_id} exited exit 7\n(no final answer)\n")
        os.kill(int((run / "run-1.pid").read_text()), 0)

    def test_nohup_stop_does_not_signal_without_confirmed_identity(self):
        lane_id, run = self.slow_nohup()
        (self.stubs / "ps-mode").write_text("error")
        result = self.cli_platform("darwin", "lane", "stop", lane_id, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no signal sent", result.stderr)
        os.kill(int((run / "run-1.pid").read_text()), 0)

    def test_nohup_stop_does_not_signal_a_reused_legacy_pid(self):
        lane_id, run = self.slow_nohup()
        other = subprocess.Popen(["sleep", "600"], start_new_session=True)
        def finish_other():
            other.terminate()
            other.wait(timeout=5)
        self.addCleanup(finish_other)
        record = self.record(lane_id)
        record["runs"][0].pop("pid_start", None)
        (self.seat / "state/lanes" / lane_id / "lane.json").write_text(json.dumps(record))
        (run / "run-1.pid").write_text(str(other.pid))
        result = self.cli_platform("darwin", "lane", "stop", lane_id, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no signal sent", result.stderr)
        self.assertIsNone(other.poll())

    def test_nohup_stop_identifies_a_live_legacy_runner(self):
        lane_id, _ = self.slow_nohup()
        record = self.record(lane_id)
        record["runs"][0].pop("pid_start", None)
        (self.seat / "state/lanes" / lane_id / "lane.json").write_text(json.dumps(record))
        self.assertEqual(self.cli_platform("darwin", "lane", "stop", lane_id).stdout,
                         f"{lane_id} stopped (run 1)\n")
        self.assertEqual(self.cli_platform("darwin", "lane", "wait", lane_id).stdout,
                         f"{lane_id} stopped exit 143\n(no final answer)\n")

    def test_darwin_stop_records_exit_and_leaves_another_lane_running(self):
        self.brief.write_text("SLOW\n")
        def start_mac(name):
            return self.cli_platform("darwin", "lane", "start", "--max-time", "10m", name, "--repo", "acme/demo",
                                     "--brief", str(self.brief)).stdout.strip()
        lane_id, other = start_mac("Mac"), start_mac("Other")
        self.addCleanup(lambda: self.cli_platform("darwin", "lane", "stop", other))
        stream = self.seat / "state/run/lanes" / lane_id / "run-1.jsonl"
        self.assertTrue(self.until(lambda: stream.exists() and stream.read_text()))
        self.assertEqual(self.cli_platform("darwin", "lane", "stop", lane_id).stdout, f"{lane_id} stopped (run 1)\n")
        self.assertEqual(self.cli("lane", "wait", lane_id).stdout, f"{lane_id} stopped exit 143\n(no final answer)\n")
        self.assertTrue(self.cli("lane", "show", other).stdout.startswith(f"{other}: running,"))
        self.assertEqual(self.log("systemctl"), [])
        self.assertEqual(self.cli_platform("darwin", "lane", "resume", "--max-time", "10m", lane_id, "go on", "--memory", "512M").stdout,
                         f"{lane_id} run 2\n")
        self.assertEqual(self.cli("lane", "wait", lane_id).stdout, f"{lane_id} exited exit 0\ndone\n")
        self.assertIn("removed worktree", self.cli("lane", "clean", lane_id).stdout)

    def test_clean_removes_the_worktree_and_branch_from_the_clone_the_lane_came_from(self):
        other = self.root / "elsewhere"
        git(self.root, "clone", "--quiet", str(self.origin), str(other))
        lane_id = self.cli("lane", "start", "--max-time", "10m", "Fix it", "--repo", str(other), "--brief", str(self.brief)).stdout.strip()
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
        self.assertTrue(self.until(lambda: stream.with_suffix(".processes").exists()))
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
        self.assertEqual((self.record(lane_id)["merged_head"], self.record(lane_id)["merge"]), (head, "f" * 40))
        self.assertEqual(git(worktree, "log", "-1", "--format=%s"), "unpublished")

    def test_merged_cleanup_preserves_dirty_worktree(self):
        lane_id = self.start()
        self.cli("lane", "wait", lane_id)
        worktree = Path(self.record(lane_id)["worktree"])
        self.merged(lane_id, git(worktree, "rev-parse", "HEAD"))
        (worktree / "untracked").write_text("keep\n")
        self.cli("lane", "clean", lane_id)
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
        self.cli("lane", "clean", lane_id)
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
        self.assertEqual(self.cli("lane", "resume", "--max-time", "10m", lane_id, "continue offline").stdout, f"{lane_id} run 2\n")
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
        lane_id = self.cli("lane", "start", "--max-time", "10m", "Fix it", "--repo", str(other), "--brief", str(self.brief)).stdout.strip()
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
        lane_id = self.cli("lane", "start", "--max-time", "10m", "Local", "--repo", str(self.main), "--brief", str(self.brief)).stdout.strip()
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
        lane_id = self.cli("lane", "start", "--max-time", "10m", "Local", "--repo", str(self.main), "--brief", str(self.brief)).stdout.strip()
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
                result = self.cli("lane", "resume", "--max-time", "10m", lane_id, "more", check=False)
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

    def review(self, **changes):
        verdict = {"head": "a" * 40, "reviewer_model": "anthropic/claude-opus-5-5",
                   "author_model": "openai-codex/gpt-6.1-sol", "verdict": "READY"}
        verdict.update(changes)
        return {"body": "tstack-verdict: " + json.dumps(verdict), "createdAt": "2026-10-26T12:00:00Z"}

    def pr_with_reviews(self, *comments):
        (self.stubs / "pr-view.json").write_text(json.dumps(
            {"headRefOid": "a" * 40, "state": "OPEN",
             "body": "tstack-author-model: openai-codex/gpt-6.1-sol", "comments": comments}))

    def test_historical_record_without_playbook_remains_showable(self):
        self.assertIn("PLAYBOOK: (absent)", self.cli("lane", "show", "demo-x-1").stdout.splitlines())

    def test_merge_squashes_at_the_ready_head_and_records_the_merge(self):
        self.pr_with_reviews(self.review())
        result = self.cli("lane", "merge", "demo-x-1")
        self.assertIn("other-family verdict READY at " + "a" * 40, result.stdout)
        self.assertIn(["pr", "merge", self.url, "--squash", "--delete-branch", "--match-head-commit", "a" * 40],
                      [c["args"] for c in self.log("gh")])
        record = self.record("demo-x-1")
        self.assertEqual((record["pr"], record["merge"], record["merged_head"]), (self.url, "f" * 40, "a" * 40))

    def test_merge_refuses_missing_nonready_stale_and_same_family_verdicts_even_with_admin(self):
        cases = [
            ("absent", []),
            ("non-READY", [self.review(verdict="NOT_READY")]),
            ("stale", [self.review(head="b" * 40)]),
            ("same-family", [self.review(reviewer_model="openai/gpt-6.1")]),
        ]
        for label, reviews in cases:
            for flags in ((), ("--admin",)):
                with self.subTest(verdict=label, flags=flags):
                    self.pr_with_reviews(*reviews)
                    result = self.cli("lane", "merge", "demo-x-1", *flags, check=False)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("no READY current-head verdict", result.stdout + result.stderr)
                    self.assertFalse(any(call["args"][:2] == ["pr", "merge"] for call in self.log("gh")))
                    self.assertNotIn("merge", self.record("demo-x-1"))
                    self.assertEqual(json.loads((self.stubs / "pr-view.json").read_text())["state"], "OPEN")

    def test_admin_merge_still_matches_the_cross_family_ready_head(self):
        self.pr_with_reviews(self.review())
        self.cli("lane", "merge", "demo-x-1", "--admin")
        self.assertIn(["pr", "merge", self.url, "--squash", "--delete-branch",
                       "--match-head-commit", "a" * 40, "--admin"], [call["args"] for call in self.log("gh")])
        self.assertEqual(self.record("demo-x-1")["merged_head"], "a" * 40)

    def test_github_merge_refusals_pass_through(self):
        self.pr_with_reviews(self.review())
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
        self.assertEqual(self.cli("lane", "resume", "--max-time", "10m", "demo-old-1", "rebase").stdout, "demo-old-1 run 2\n")
        self.cli("lane", "wait", "demo-old-1")
        self.assertEqual(self.log("omp")[0]["args"][-3:], ["--resume", "/s/2026_old.jsonl", "rebase"])

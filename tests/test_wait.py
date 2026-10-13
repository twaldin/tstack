import datetime as dt
import fcntl
import json
import subprocess
import sys
import time

from support import CODE, Sandbox

PR = "https://github.com/acme/web/pull/7"


class Wait(Sandbox):
    def setUp(self):
        super().setUp()
        self.env.update(TSTACK_WAIT_POLL="0.2", TSTACK_WAIT_BATCH="2.5")

    def intake(self, *lines: str) -> None:
        path = self.seat / "state" / "intake.md"
        with open(path, "a") as handle:
            for line in lines:
                handle.write(line + "\n")

    def wait(self, *args: str, timeout: float = 20) -> tuple[str, list[str], float]:
        began = time.monotonic()
        out = self.cli("tstack", "wait", *args, timeout=timeout).stdout.splitlines()
        self.assertTrue(out[0].startswith("batch "), out)
        return out[0].split()[1], out[1:], time.monotonic() - began

    def test_urgent_returns_at_once_and_others_batch(self):
        self.intake("- [ ] 2026-10-13T08:00Z mail: newsletter")
        batch, events, took = self.wait()
        self.assertGreaterEqual(took, 1.5)
        self.assertEqual([e.split("\t")[1:] for e in events], [["intake", "mail: newsletter"]])
        self.intake("- [ ] 2026-10-13T08:05Z text: call me")
        batch2, events, took = self.wait("--ack", batch)
        self.assertLess(took, 1.4)
        self.assertEqual([e.split("\t")[2] for e in events], ["text: call me"])
        self.assertNotEqual(batch, batch2)

    def test_unacked_batch_comes_back_after_a_crash(self):
        self.intake("- [ ] 2026-10-13T08:00Z text: are you there")
        batch, events, _ = self.wait()
        again, replay, took = self.wait()
        self.assertEqual((again, replay), (batch, events))
        self.assertLess(took, 1.0)
        self.intake("- [ ] 2026-10-13T08:10Z text: second")
        after, newer, _ = self.wait("--ack", batch)
        self.assertNotEqual(after, batch)
        self.assertEqual([e.split("\t")[2] for e in newer], ["text: second"])
        state = json.loads((self.seat / "state" / "run" / "wait" / "state.json").read_text())
        self.assertEqual(list(state["batches"]), [after])

    def test_heartbeat_names_failing_sources(self):
        script = self.seat / "wait.d" / "broken"
        script.parent.mkdir()
        script.write_text("#!/bin/sh\necho oops >&2\nexit 3\n")
        script.chmod(0o755)
        self.intake("- [ ] 2026-10-13T08:00Z text: hi")
        self.wait()
        beat = json.loads((self.seat / "state" / "run" / "heartbeat.json").read_text())
        self.assertFalse(beat["sources"]["wait.d/broken"]["ok"])
        self.assertTrue(beat["sources"]["wait.d/broken"]["failing_since"])
        self.assertTrue(beat["sources"]["intake"]["ok"])

    def test_wait_d_lines_are_events_once(self):
        script = self.seat / "wait.d" / "texts"
        script.parent.mkdir()
        script.write_text("#!/bin/sh\nprintf 'm1\\ttext\\thello\\n'\n")
        script.chmod(0o755)
        batch, events, _ = self.wait()
        self.assertEqual([e.split("\t")[1:] for e in events], [["text", "hello"]])
        self.intake("- [ ] 2026-10-13T08:00Z text: next")
        _, events, _ = self.wait("--ack", batch)
        self.assertEqual([e.split("\t")[2] for e in events], ["text: next"])

    def test_pr_events_resume_the_lane_once_without_waking_the_seat(self):
        self.project_clone()
        self.omp_config(report="waiting", pr=PR)
        lane_id = self.cli("lane", "start", "ci", "--brief", str(self.brief())).stdout.strip()
        self.settled(lane_id, "waiting")
        self.pr_stub(head="d" * 40, checks=(("test", "IN_PROGRESS"), ("lint", "SUCCESS")))
        proc = subprocess.Popen([sys.executable, str(CODE / "bin" / "tstack"), "wait"], env=self.env, cwd=self.seat,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.wait_for(lambda: (self.seat / "state" / "run" / "heartbeat.json").exists(), what="the first poll")
            time.sleep(0.5)
            self.assertEqual(len(self.lane_record(lane_id)["runs"]), 1)
            self.pr_stub(head="d" * 40, checks=(("test", "FAILURE"), ("lint", "SUCCESS")), comments=("c1", "c2"))
            self.wait_for(lambda: len(self.lane_record(lane_id)["runs"]) == 2, what="the PR resume")
            self.settled(lane_id, "waiting")
            time.sleep(1.0)
        finally:
            proc.kill()
            out, _ = proc.communicate()
        record = self.lane_record(lane_id)
        self.assertEqual(len(record["runs"]), 2, record["runs"])
        self.assertEqual(record["runs"][1]["event"], "check-failed")
        self.assertEqual(record["waves"], {"test": 1})
        event = (self.seat / "state" / "run" / "lanes" / lane_id / "run-2.event").read_text()
        self.assertIn("checks-red (test); comment", event)
        self.assertEqual(out, "")


def ago(minutes: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


class LaneMarker(Sandbox):
    """Lanes and the operator share one gh login. A lane marks what it writes with <!-- tstack-lane:<id> -->;
    only those bodies are its own."""

    def setUp(self):
        super().setUp()
        self.env.update(TSTACK_WAIT_POLL="0.2", TSTACK_WAIT_BATCH="60")
        worktree = self.root / "worktree"
        worktree.mkdir()
        lane = self.seat / "state" / "lanes" / "demo-ci-1"
        lane.mkdir(parents=True)
        (lane / "brief.md").write_text("Fix CI.\nTIMEBOX: 1h\n")
        (lane / "lane.json").write_text(json.dumps({
            "id": "demo-ci-1", "project": "demo", "task": "ci", "seat": str(self.seat), "host": "testbox", "repo": "acme/web",
            "worktree": str(worktree), "branch": "lane/demo-ci-1", "base": "main", "playbook": "build-feature",
            "model": "@tiny", "thinking": "low", "timebox": "1h", "memory": "1G",
            "session": "/sessions/2026-10-13T00-00-00-000Z_abc.jsonl", "status": "waiting", "reason": None, "held": False,
            "pr": PR, "waves": {}, "cost_usd": 0.01, "created": ago(120),
            "runs": [{"run": 1, "event": "start", "start": ago(120), "accepted": ago(120), "end": ago(60), "exit": 0,
                      "cost_usd": 0.01}]}))
        self.omp_config(report="waiting", pr=PR)
        self.pr_stub(checks=(("test", "IN_PROGRESS"),))

    def poll_with(self, *comments, resumes: bool, created: str | None = None) -> dict:
        proc = subprocess.Popen([sys.executable, str(CODE / "bin" / "tstack"), "wait"], env=self.env, cwd=self.seat,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.wait_for(lambda: (self.seat / "state" / "run" / "heartbeat.json").exists(), what="the first poll")
            time.sleep(0.4)
            self.pr_stub(checks=(("test", "IN_PROGRESS"),), comments=comments, created=created or ago(90))
            if resumes:
                self.wait_for(lambda: len(self.lane_record("demo-ci-1")["runs"]) == 2, what="the resume")
                self.settled("demo-ci-1", "waiting")
            else:
                time.sleep(2.0)
        finally:
            proc.kill()
            proc.communicate()
        return self.lane_record("demo-ci-1")

    def test_the_lanes_own_marked_comment_does_not_resume_it(self):
        record = self.poll_with(("c-lane", "Pushed the fix.\n\n<!-- tstack-lane:demo-ci-1 -->"), resumes=False, created=ago(30))
        self.assertEqual(len(record["runs"]), 1)

    def test_an_unmarked_comment_from_the_shared_login_during_a_run_resumes(self):
        record = self.poll_with(("c-operator", "Stop this approach."), resumes=True)
        self.assertEqual(record["runs"][1]["event"], "pr")
        self.assertIn("comment", (self.seat / "state" / "run" / "lanes" / "demo-ci-1" / "run-2.event").read_text())

    def test_another_lanes_marked_comment_resumes(self):
        record = self.poll_with(("c-other", "Rebased on this.\n<!-- tstack-lane:demo-api-2 -->"), resumes=True)
        self.assertEqual(len(record["runs"]), 2)


class Durability(Sandbox):
    """Crashes and failures between reading a source and committing its cursor lose nothing and repeat nothing."""

    def setUp(self):
        super().setUp()
        self.env.update(TSTACK_WAIT_POLL="0.2", TSTACK_WAIT_BATCH="60")
        self.state = self.seat / "state" / "run" / "wait" / "state.json"
        self.beat = self.seat / "state" / "run" / "heartbeat.json"

    def background(self) -> subprocess.Popen:
        return subprocess.Popen([sys.executable, str(CODE / "bin" / "tstack"), "wait"], env=self.env, cwd=self.seat,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def until(self, predicate, what: str) -> None:
        proc = self.background()
        try:
            self.wait_for(predicate, what=what)
        finally:
            proc.kill()
            proc.communicate()

    def source(self, name: str) -> dict:
        return (json.loads(self.beat.read_text()).get("sources") or {}).get(name) or {} if self.beat.exists() else {}

    def wait_once(self) -> list[str]:
        try:
            return self.cli("tstack", "wait", timeout=10).stdout.splitlines()[1:]
        except subprocess.TimeoutExpired:
            self.fail("tstack wait returned nothing")

    def test_a_failing_source_commits_neither_its_cursor_nor_its_events(self):
        script = self.seat / "wait.d" / "texts"
        script.parent.mkdir()
        script.write_text("#!/bin/sh\nprintf 'm1\\ttext\\thello\\nnot a valid line\\n'\n")
        script.chmod(0o755)
        self.until(lambda: self.source("wait.d/texts").get("ok") is False, "the source failure")
        script.write_text("#!/bin/sh\nprintf 'm1\\ttext\\thello\\n'\n")
        self.assertEqual([e.split("\t", 1)[1] for e in self.wait_once()], ["text\thello"])

    def test_an_answer_found_on_the_first_poll_wakes_the_seat(self):
        self.board()
        self.cli("tstack", "gate", "add", "--id", "g-pick", "--question", "Pick?", "--options", "a) left b) right",
                 "--default", "b")
        asks = json.loads((self.stubs / "asks.json").read_text())
        asks["obj_ask1"].update(status="answered", answer={"option": "a"})
        (self.stubs / "asks.json").write_text(json.dumps(asks))
        self.assertEqual([e.split("\t", 1)[1] for e in self.wait_once()], ["gate-answered\tgate g-pick answered: a) left"])

    def test_a_dispatch_replayed_after_a_crash_is_a_no_op(self):
        self.project_clone()
        self.omp_config(report="waiting", pr=PR)
        lane_id = self.cli("lane", "start", "ci", "--brief", str(self.brief())).stdout.strip()
        self.settled(lane_id, "waiting")
        self.pr_stub(checks=(("test", "IN_PROGRESS"),))
        self.until(lambda: self.state.exists() and lane_id in json.loads(self.state.read_text())["cursors"].get("prs", {}),
                   "the first PR snapshot")
        before = self.state.read_text()
        self.pr_stub(checks=(("test", "FAILURE"),))
        self.until(lambda: len(self.lane_record(lane_id)["runs"]) == 2, "the resume")
        self.state.write_text(before)
        self.settled(lane_id, "waiting")
        proc = self.background()
        time.sleep(1.5)
        proc.kill()
        proc.communicate()
        record = self.lane_record(lane_id)
        self.assertEqual((len(record["runs"]), record["waves"]), (2, {"test": 1}))

    def test_a_held_lock_is_a_source_failure_not_a_hang(self):
        lane = self.seat / "state" / "lanes" / "demo-ci-1"
        lane.mkdir(parents=True)
        (lane / "lane.json").write_text(json.dumps({
            "id": "demo-ci-1", "project": "demo", "status": "running", "waves": {}, "pr": None, "created": ago(120),
            "runs": [{"run": 1, "event": "start", "start": ago(120), "accepted": ago(120)}]}))
        self.env["TSTACK_LOCK_WAIT"] = "0.5"
        lock = self.seat / "state" / "run" / "lanes" / "demo-ci-1" / "record.lock"
        lock.parent.mkdir(parents=True)
        with open(lock, "a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            self.until(lambda: "busy" in (self.source("lanes").get("error") or ""), "the lanes source to report the busy lock")
        self.assertEqual(self.lane_record("demo-ci-1")["status"], "running")

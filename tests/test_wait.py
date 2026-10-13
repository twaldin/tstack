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

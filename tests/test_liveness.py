import datetime as dt
import json
import re
import time

from support import Sandbox, assistant_entry


def ago(minutes: int) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


class Liveness(Sandbox):
    def setUp(self):
        super().setUp()
        self.board()
        self.run_dir = self.seat / "state" / "run"
        self.run_dir.mkdir()

    def heartbeat(self, minutes: int, sources=None) -> None:
        (self.run_dir / "heartbeat.json").write_text(json.dumps({"at": ago(minutes), "sources": sources or {}}))

    def let_time_pass(self, minutes: int) -> None:
        """Move every timestamp liveness recorded `minutes` into the past."""
        path = self.run_dir / "liveness.json"

        def earlier(match):
            when = dt.datetime.strptime(match.group(0), "%Y-%m-%dT%H:%M:%SZ") - dt.timedelta(minutes=minutes)
            return when.strftime("%Y-%m-%dT%H:%M:%SZ")
        path.write_text(re.sub(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", earlier, path.read_text()))

    def easl_calls(self) -> list:
        return [c[0] for c in self.log_lines("easl.log")]

    def texts(self) -> list:
        path = self.stubs / "text.log"
        return path.read_text().splitlines() if path.exists() else []

    def liveness(self) -> str:
        return self.cli("tstack", "check", "--liveness").stdout

    def test_fresh_heartbeat_does_nothing(self):
        self.heartbeat(5)
        self.liveness()
        self.assertEqual((self.easl_calls(), self.texts()), ([], []))

    def test_stale_seat_is_restarted_once_then_texted_after_30_minutes(self):
        self.heartbeat(40)
        self.assertIn("restarted in place", self.liveness())
        self.assertEqual(self.easl_calls(), ["agent.restart", "agent.wait", "agent.prompt"])
        wait, prompt = self.log_lines("easl.log")[1:3]
        self.assertEqual(wait[wait.index("--until") + 1], "idle")
        self.assertEqual(prompt[prompt.index("--target") + 1], "obj_seat")
        self.assertEqual(prompt[prompt.index("--text") + 1], "You were restarted in place.\n")
        self.liveness()
        self.assertEqual((len(self.easl_calls()), self.texts()), (3, []))
        self.let_time_pass(31)
        self.liveness()
        self.assertEqual(len(self.texts()), 1)
        self.assertRegex(self.texts()[0], r"^send tstack: seat demo on testbox has not polled since .*; restarted at .*, still not polling\.$")
        self.liveness()
        self.assertEqual((len(self.texts()), len(self.easl_calls())), (1, 3))

    def test_a_restart_that_keeps_failing_is_retried_and_escalated(self):
        self.heartbeat(40)
        (self.stubs / "easl-fail").touch()
        self.assertIn("restart failed: easl agent.restart: not_found", self.liveness())
        self.let_time_pass(31)
        self.liveness()
        self.assertEqual(self.easl_calls(), ["agent.restart", "agent.restart"])
        self.assertEqual(len(self.texts()), 1)
        self.assertIn("recovery failing: easl agent.restart: not_found", self.texts()[0])

    def test_a_failed_prompt_is_retried_without_a_second_restart(self):
        self.heartbeat(40)
        (self.stubs / "easl-prompt-fail").touch()
        self.assertIn("restart prompt failed", self.liveness())
        (self.stubs / "easl-prompt-fail").unlink()
        self.assertIn("restarted in place", self.liveness())
        self.assertEqual(self.easl_calls(), ["agent.restart", "agent.wait", "agent.prompt", "agent.wait", "agent.prompt"])

    def test_no_prompt_until_the_restarted_seat_reports_idle(self):
        self.heartbeat(40)
        (self.stubs / "easl-wait-fail").touch()
        self.assertIn("restart prompt failed: easl agent.wait: unavailable", self.liveness())
        self.assertEqual(self.easl_calls(), ["agent.restart", "agent.wait"])
        (self.stubs / "easl-wait-fail").unlink()
        self.assertIn("restarted in place", self.liveness())
        self.assertEqual(self.easl_calls(), ["agent.restart", "agent.wait", "agent.wait", "agent.prompt"])

    def test_a_seat_polling_again_since_its_restart_gets_no_restart_prompt(self):
        self.heartbeat(40)
        (self.stubs / "easl-prompt-fail").touch()
        self.assertIn("restart prompt failed", self.liveness())
        time.sleep(1.1)
        self.heartbeat(0)
        self.assertIn("polling again since", self.liveness())
        self.assertEqual(self.easl_calls(), ["agent.restart", "agent.wait", "agent.prompt"])
        self.assertIn("incident from", self.liveness())

    def test_an_alert_counts_only_once_it_is_sent(self):
        self.heartbeat(40)
        self.liveness()
        self.let_time_pass(31)
        (self.stubs / "text-fail").touch()
        self.assertIn("text failed", self.liveness())
        self.assertEqual(self.texts(), [])
        (self.stubs / "text-fail").unlink()
        self.liveness()
        self.liveness()
        self.assertEqual(len(self.texts()), 1)

    def test_busy_seat_is_retried(self):
        self.heartbeat(40)
        (self.stubs / "easl-busy").touch()
        self.assertIn("restart failed: busy", self.liveness())
        (self.stubs / "easl-busy").unlink()
        self.assertIn("restarted in place", self.liveness())
        self.assertEqual(self.easl_calls(), ["agent.restart", "agent.list", "agent.restart", "agent.wait", "agent.prompt"])

    def test_source_failing_an_hour_texts_once_sent(self):
        self.heartbeat(1, {"github": {"ok": False, "error": "gh: auth", "failing_since": ago(61)},
                           "intake": {"ok": True, "error": None, "failing_since": None}})
        (self.stubs / "text-fail").touch()
        self.liveness()
        (self.stubs / "text-fail").unlink()
        self.liveness()
        self.liveness()
        self.assertEqual(len(self.texts()), 1)
        self.assertIn("wait source github has failed", self.texts()[0])

    def test_a_text_that_cannot_run_keeps_recovery_progress_and_the_alert_pending(self):
        self.heartbeat(40, {"github": {"ok": False, "error": "gh: auth", "failing_since": ago(61)}})
        text = self.instance / "bin" / "text"
        text.chmod(0o644)
        for _ in range(2):
            self.assertIn("text failed", self.cli("tstack", "check", "--liveness", check=False).stdout)
        self.assertEqual(self.easl_calls(), ["agent.restart", "agent.wait", "agent.prompt"])
        live = json.loads((self.run_dir / "liveness.json").read_text())
        self.assertTrue(live["restarted"] and live["prompted"])
        self.assertEqual(live["source_alerts"], {})
        text.chmod(0o755)
        self.liveness()
        self.liveness()
        self.assertEqual(len(self.texts()), 1)
        self.assertIn("wait source github has failed", self.texts()[0])
        self.assertEqual(self.easl_calls(), ["agent.restart", "agent.wait", "agent.prompt"])

    def test_a_seat_busy_only_with_its_wait_is_restarted_with_force(self):
        self.heartbeat(40)
        (self.stubs / "easl-busy").touch()
        self.seat_tile(assistant_entry(120))
        self.assertIn("restarted in place (forced", self.liveness())
        calls = self.log_lines("easl.log")
        self.assertEqual([("--force" in c) for c in calls if c[0] == "agent.restart"], [False, True])
        self.assertEqual(calls[-1][0], "agent.prompt")

    def test_a_seat_still_in_its_turn_is_not_forced(self):
        cases = {
            "tool call pending": dict(last=assistant_entry(120, tool_call=True)),
            "answer under a minute old": dict(last=assistant_entry(10)),
            "user message last": dict(last=assistant_entry(120, role="user")),
            "blocked": dict(last=assistant_entry(120), state="blocked"),
            "operator typing": dict(last=assistant_entry(120), focused=True),
            "unsent draft": dict(last=assistant_entry(120), draft=True),
        }
        (self.stubs / "easl-busy").touch()
        self.heartbeat(40)
        for name, case in cases.items():
            with self.subTest(name):
                for path in (self.run_dir / "liveness.json", self.stubs / "easl.log"):
                    path.unlink(missing_ok=True)
                self.seat_tile(**case)
                self.assertIn("restart failed: busy", self.liveness())
                self.assertFalse(any("--force" in c for c in self.log_lines("easl.log")))

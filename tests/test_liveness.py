import datetime as dt
import json
import re

from support import Sandbox


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
        self.assertEqual(self.easl_calls(), ["agent.restart", "agent.prompt"])
        prompt = self.log_lines("easl.log")[1]
        self.assertEqual(prompt[prompt.index("--target") + 1], "obj_seat")
        self.assertEqual(prompt[prompt.index("--text") + 1], "You were restarted in place.\n")
        self.liveness()
        self.assertEqual((len(self.easl_calls()), self.texts()), (2, []))
        self.let_time_pass(31)
        self.liveness()
        self.assertEqual(len(self.texts()), 1)
        self.assertRegex(self.texts()[0], r"^send tstack: seat demo on testbox has not polled since .*; restarted at .*, still not polling\.$")
        self.liveness()
        self.assertEqual((len(self.texts()), len(self.easl_calls())), (1, 2))

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
        self.assertEqual(self.easl_calls(), ["agent.restart", "agent.prompt", "agent.prompt"])

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
        self.assertEqual(self.easl_calls(), ["agent.restart", "agent.restart", "agent.prompt"])

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

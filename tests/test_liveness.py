import datetime as dt
import json
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

    def record(self) -> dict:
        return json.loads((self.run_dir / "recovery.json").read_text())

    def easl_calls(self) -> list:
        return [c[0] for c in self.log_lines("easl.log")]

    def forced(self) -> bool:
        return any("--force" in c for c in self.log_lines("easl.log"))

    def texts(self) -> list:
        path = self.stubs / "text.log"
        return path.read_text().splitlines() if path.exists() else []

    def liveness(self) -> str:
        return self.cli("tstack", "check", "--liveness").stdout

    def test_fresh_heartbeat_does_nothing(self):
        self.heartbeat(5)
        self.liveness()
        self.assertEqual((self.easl_calls(), self.texts()), ([], []))

    def test_stale_seat_is_restarted_once_then_texted_after_60_minutes(self):
        self.heartbeat(40)
        self.assertIn("restarted in place; restart prompt sent", self.liveness())
        self.assertEqual(self.easl_calls(), ["agent.restart", "agent.wait", "agent.prompt"])
        wait, prompt = self.log_lines("easl.log")[1:3]
        self.assertEqual(wait[wait.index("--until") + 1], "idle")
        self.assertEqual(prompt[prompt.index("--target") + 1], "obj_seat")
        self.assertEqual(prompt[prompt.index("--text") + 1], "You were restarted in place.\n")
        self.liveness()
        self.assertEqual((len(self.easl_calls()), self.texts()), (3, []))
        self.heartbeat(61)
        self.liveness()
        self.assertEqual(len(self.texts()), 1)
        self.assertRegex(self.texts()[0], r"^send tstack: seat demo on testbox has not polled since .*; "
                                          r"restarted at .* \(liveness\), still not polling\.$")
        self.liveness()
        self.assertEqual((len(self.texts()), len(self.easl_calls())), (1, 3))

    def test_a_restart_that_keeps_failing_is_retried_and_texted(self):
        self.heartbeat(40)
        (self.stubs / "easl-fail").touch()
        self.assertIn("restart failed: easl agent.restart: not_found", self.liveness())
        self.heartbeat(61)
        self.liveness()
        self.assertEqual(self.easl_calls(), ["agent.restart", "agent.restart"])
        self.assertEqual(len(self.texts()), 1)
        self.assertIn("not restarted: easl agent.restart: not_found", self.texts()[0])

    def test_a_working_seat_is_never_forced_and_is_texted_after_60_minutes(self):
        """Even when its session ends with a finished answer, a seat easl calls working only gets plain
        restart attempts; tstack makes no restart decision from session contents."""
        (self.stubs / "easl-busy").touch()
        self.seat_tile("working", last=assistant_entry(600))
        self.heartbeat(40)
        self.assertIn("not restarted, the seat is busy", self.liveness())
        self.heartbeat(61)
        self.liveness()
        self.assertEqual(self.easl_calls(), ["agent.restart", "agent.restart"])
        self.assertFalse(self.forced())
        self.assertEqual(len(self.texts()), 1)
        self.assertIn("not restarted: busy, never forced", self.texts()[0])

    def test_a_failed_prompt_is_retried_without_a_second_restart(self):
        self.heartbeat(40)
        (self.stubs / "easl-prompt-fail").touch()
        self.assertIn("restart prompt failed", self.liveness())
        (self.stubs / "easl-prompt-fail").unlink()
        self.assertIn("restart prompt sent", self.liveness())
        self.assertEqual(self.easl_calls(), ["agent.restart", "agent.wait", "agent.prompt", "agent.wait", "agent.prompt"])

    def test_no_prompt_until_the_restarted_seat_reports_idle(self):
        self.heartbeat(40)
        (self.stubs / "easl-wait-fail").touch()
        self.assertIn("restart prompt failed: easl agent.wait: unavailable", self.liveness())
        self.assertEqual(self.easl_calls(), ["agent.restart", "agent.wait"])
        (self.stubs / "easl-wait-fail").unlink()
        self.assertIn("restart prompt sent", self.liveness())
        self.assertEqual(self.easl_calls(), ["agent.restart", "agent.wait", "agent.wait", "agent.prompt"])

    def test_a_seat_polling_again_since_its_restart_gets_no_restart_prompt(self):
        self.heartbeat(40)
        (self.stubs / "easl-prompt-fail").touch()
        self.assertIn("restart prompt failed", self.liveness())
        time.sleep(1.1)
        self.heartbeat(0)
        self.assertIn("polling again since", self.liveness())
        self.assertEqual(self.easl_calls(), ["agent.restart", "agent.wait", "agent.prompt"])

    def test_a_restart_by_the_seat_itself_suppresses_another_and_liveness_sends_its_prompt(self):
        self.heartbeat(40)
        self.env["EASL_TILE_ID"] = "obj_seat"
        self.cli("tstack", "restart-self")
        self.liveness()
        self.assertEqual(self.easl_calls(), ["agent.restart", "agent.wait", "agent.prompt"])
        self.assertIn("--force", self.log_lines("easl.log")[0])
        self.assertEqual(self.record()["restart"]["by"], "self")

    def test_a_seat_that_never_polled_is_restarted_once_whoever_restarted_it(self):
        self.env["EASL_TILE_ID"] = "obj_seat"
        self.cli("tstack", "restart-self")
        self.liveness()
        self.liveness()
        self.assertEqual(self.easl_calls(), ["agent.restart", "agent.wait", "agent.prompt"])

    def test_a_restart_unconfirmed_for_an_hour_no_longer_blocks_a_liveness_restart(self):
        """restart-self died before its restart went out, its omp with it: nothing restarted the seat. Once the
        pending restart expires, liveness restarts it normally and keeps the failed attempt for diagnosis."""
        self.heartbeat(70)
        self.env["EASL_TILE_ID"] = "obj_seat"
        (self.stubs / "easl-kill-caller").touch()
        self.assertEqual(self.cli("tstack", "restart-self", check=False).returncode, -9)
        (self.stubs / "easl-kill-caller").unlink()
        record = self.record()
        for key in ("pending", "restart"):
            record[key]["at"] = ago(61)
        (self.run_dir / "recovery.json").write_text(json.dumps(record))
        self.assertIn("never confirmed (no wait under a new omp within an hour)", self.liveness())
        self.liveness()
        restarts = [c for c in self.log_lines("easl.log") if c[0] == "agent.restart"]
        self.assertEqual(["--force" in c for c in restarts], [True, False])
        record = self.record()
        self.assertEqual((record["restart"]["by"], record["unconfirmed"]["restart"]["by"]), ("liveness", "self"))

    def test_an_alert_counts_only_once_it_is_sent(self):
        self.heartbeat(40)
        self.liveness()
        self.heartbeat(61)
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
        self.assertIn("the seat is busy", self.liveness())
        (self.stubs / "easl-busy").unlink()
        self.assertIn("restarted in place", self.liveness())
        self.assertEqual(self.easl_calls(), ["agent.restart", "agent.restart", "agent.wait", "agent.prompt"])

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
        record = self.record()
        self.assertTrue(record["restart"]["at"] and record["restart"]["prompted"])
        self.assertEqual(record["source_alerts"], {})
        text.chmod(0o755)
        self.liveness()
        self.liveness()
        self.assertEqual(len(self.texts()), 1)
        self.assertIn("wait source github has failed", self.texts()[0])
        self.assertEqual(self.easl_calls(), ["agent.restart", "agent.wait", "agent.prompt"])

import datetime as dt
import json

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

    def test_stale_seat_is_restarted_then_texted(self):
        self.heartbeat(40)
        self.assertIn("restarted in place", self.liveness())
        self.assertEqual(self.easl_calls(), ["agent.restart", "agent.prompt"])
        prompt = self.log_lines("easl.log")[1]
        self.assertEqual(prompt[prompt.index("--target") + 1], "obj_seat")
        self.assertEqual(prompt[prompt.index("--text") + 1], "You were restarted in place.\n")

        self.liveness()
        self.assertEqual(len(self.easl_calls()), 2, "no second restart while the first is under 30 minutes old")
        self.assertEqual(self.texts(), [])

        live = json.loads((self.run_dir / "liveness.json").read_text())
        live["restarted_at"] = ago(31)
        (self.run_dir / "liveness.json").write_text(json.dumps(live))
        self.assertIn("still stale after restart", self.liveness())
        self.assertEqual(len(self.texts()), 1)
        self.assertTrue(self.texts()[0].startswith("send tstack: seat demo on testbox has not polled"))
        self.liveness()
        self.assertEqual(len(self.texts()), 1, "one text per failed restart")
        self.assertEqual(len(self.easl_calls()), 2)

    def test_busy_seat_is_not_counted_as_restarted(self):
        self.heartbeat(40)
        (self.stubs / "easl-busy").touch()
        self.assertIn("restart refused (busy)", self.liveness())
        self.assertNotIn("restarted_at", json.loads((self.run_dir / "liveness.json").read_text()))
        (self.stubs / "easl-busy").unlink()
        self.assertIn("restarted in place", self.liveness())

    def test_source_failing_an_hour_texts_once(self):
        self.heartbeat(1, {"github": {"ok": False, "error": "gh: auth", "failing_since": ago(61)},
                           "intake": {"ok": True, "error": None, "failing_since": None}})
        self.liveness()
        self.assertEqual(len(self.texts()), 1)
        self.assertIn("wait source github has failed", self.texts()[0])
        self.liveness()
        self.assertEqual(len(self.texts()), 1)

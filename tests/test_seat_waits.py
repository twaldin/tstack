import json
import os
import select
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUN = shutil.which("bun")


@unittest.skipUnless(BUN, "seat-waits runtime tests require Bun on PATH")
class SeatWaits(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.instance = self.root / "instance"
        self.assistant = self.instance / "assistant"
        self.project = self.instance / "projects/demo"
        self.fake = self.root / "fake"
        self.fake.mkdir()
        for seat in (self.assistant, self.project):
            (seat / "state/lanes").mkdir(parents=True)
        (self.instance / "bin").mkdir()
        executable = ROOT / "tests/fixtures/seat_waits_command.py"
        for name in ("lane", "watch-pr", "easl", "sleep"):
            (self.fake / name).symlink_to(executable)
        (self.instance / "bin/text").symlink_to(executable)
        for name, value in (("asks", {"objects": []}), ("prs", {}), ("messages", [])):
            self.put(self.fake / f"{name}.json", value)
        self.driver = None
        self.addCleanup(self.close_driver)

    def put(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value))
        temporary.replace(path)

    def lane(self, status="running", run=1, pr=None):
        self.put(self.project / "state/lanes/build/lane.json",
                 {"status": status, "runs": [{"run": run, "start": f"start-{run}"}], "pr": pr})

    def tasks(self, rows):
        (self.project / "state/tasks.tsv").write_text("id\tstate\tpr\n" + "".join(
            f"{index}\t{state}\t{pr}\n" for index, (state, pr) in enumerate(rows)))

    def asks(self):
        self.put(self.project / "state/board.json", {"board": "board-1", "tile": "ours"})
        self.put(self.fake / "asks.json", {"objects": [
            {"id": "own-question", "props": {"asker": {"tile": "ours"}}},
            {"id": "foreign-question", "props": {"asker": {"tile": "theirs"}}},
        ]})

    def start(self, seat=None, kind="main", lane=False):
        env = {**os.environ, "TSTACK_INSTANCE": str(self.instance),
               "SEAT_WAITS_FAKE": str(self.fake), "PATH": f"{self.fake}{os.pathsep}{os.environ['PATH']}"}
        for key in ("TSTACK_LANE", "TSTACK_SEAT", "EASL_TILE_ID"):
            env.pop(key, None)
        if lane:
            env["TSTACK_LANE"] = "build"
        self.driver = subprocess.Popen([BUN, str(ROOT / "tests/fixtures/seat_waits_driver.js")],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, env=env)
        return self.request("start", cwd=str(seat or self.project), kind=kind)

    def close_driver(self):
        if self.driver is None:
            return
        driver, self.driver = self.driver, None
        if driver.poll() is None:
            driver.stdin.write('{"op":"quit"}\n')
            driver.stdin.flush()
        try:
            stdout, stderr = driver.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            driver.kill()
            stdout, stderr = driver.communicate()
            self.fail(f"runtime driver failed to shut down: {stdout}\n{stderr}")
        self.assertEqual(driver.returncode, 0, stderr)

    def request(self, op="snapshot", **values):
        self.driver.stdin.write(json.dumps({"op": op, **values}) + "\n")
        self.driver.stdin.flush()
        ready, _, _ = select.select([self.driver.stdout], [], [], 10)
        self.assertTrue(ready, "runtime driver response timed out")
        line = self.driver.stdout.readline()
        self.assertTrue(line, "runtime driver exited without a response")
        snapshot = json.loads(line)
        self.assertNotIn("error", snapshot, snapshot)
        return snapshot

    def calls(self, command=None, args=None):
        path = self.fake / "calls.jsonl"
        records = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [record for record in records if (command is None or record["command"] == command)
                and (args is None or record["args"] == args)]

    def until(self, predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            snapshot = self.request()
            if predicate(snapshot):
                return snapshot
        self.fail(f"condition not reached; calls={self.calls()!r}; snapshot={snapshot!r}")

    def wait_call(self, command, args=None, count=1):
        self.until(lambda _: len(self.calls(command, args)) >= count)
        return self.calls(command, args)[count - 1]

    def finish(self, call, stdout="", code=0, stderr=""):
        self.put(self.fake / f"{call['pid']}.json", {"stdout": stdout, "stderr": stderr, "code": code})

    def notice(self, kind, count=1):
        def matching(snapshot):
            return [notice for notice in snapshot["notices"] if f"{kind} ready." in notice["message"]["content"]]
        snapshot = self.until(lambda value: len(matching(value)) >= count)
        return matching(snapshot)[count - 1]

    def raw(self, notice):
        reference = notice["message"]["content"].split("Read ", 1)[1].splitlines()[0].removesuffix(".")
        path = Path(reference)
        self.assertEqual(path.parent, self.project / "state/run/seat-waits")
        return json.loads(path.read_text())

    def assert_rearmed_before_notice(self, old_call, new_call, notice):
        self.assertNotEqual(old_call["pid"], new_call["pid"])
        self.assertIn(new_call["pid"], notice["children"], "replacement must exist before model notification")

    def test_start_restores_lane_prs_and_only_owned_open_asks(self):
        self.lane(pr="https://github.com/acme/repo/pull/12")
        self.tasks([("active", "acme/repo#13"), ("done", "acme/repo#14"),
                    ("cancelled", "acme/repo#15")])
        self.asks()
        self.start()
        lane = self.wait_call("lane", ["wait", "build", "--seat", str(self.project)])
        self.wait_call("watch-pr", ["acme/repo#12", "--until", "change", "--json"])
        self.wait_call("watch-pr", ["acme/repo#13", "--until", "change", "--json"])
        self.wait_call("easl", ["ask", "wait", "own-question"])
        self.wait_call("sleep", ["1800"])
        self.assertEqual((lane["cwd"], lane["seat"]), (str(self.project), str(self.project)))
        self.assertIn(["ask", "list", "--open", "--board", "board-1"],
                      [call["args"] for call in self.calls("easl")])
        self.assertEqual({call["args"][0] for call in self.calls("watch-pr")}, {"acme/repo#12", "acme/repo#13"})
        self.assertEqual([call["args"] for call in self.calls("easl") if call["args"][:2] == ["ask", "wait"]],
                         [["ask", "wait", "own-question"]])
        self.assertEqual(self.request()["notices"], [])

    def test_finished_lane_stays_dropped_but_resumed_run_is_watched(self):
        self.lane()
        self.start()
        first = self.wait_call("lane")
        self.lane(status="done")
        self.finish(first, "lane result allowed in notice\n")
        notice = self.notice("lane")
        self.assertIn("lane result allowed in notice", notice["message"]["content"])
        self.assertIn("build", notice["message"]["content"])
        self.assertEqual(self.raw(notice)["stdout"], "lane result allowed in notice\n")
        self.request("tool")
        self.request("tick")
        self.assertEqual(len(self.calls("lane")), 1)
        self.lane(run=2)
        self.request("tool")
        self.wait_call("lane", count=2)

    def test_heartbeat_and_text_rearm_before_notification(self):
        self.start(self.assistant)
        heartbeat = self.wait_call("sleep", ["1800"])
        text = self.wait_call("text", ["inbox", "--wait"])
        self.assertEqual((text["cwd"], text["seat"]), (str(self.instance), str(self.assistant)))
        self.finish(heartbeat)
        notice = self.notice("heartbeat")
        self.assert_rearmed_before_notice(heartbeat, self.wait_call("sleep", ["1800"], 2), notice)
        self.put(self.fake / "messages.json", ["text payload allowed in notice"])
        notice = self.notice("text")
        self.assertIn("text payload allowed in notice", notice["message"]["content"])
        self.assert_rearmed_before_notice(text, self.wait_call("text", ["inbox", "--wait"], 2), notice)

    def test_pr_change_rearms_and_keeps_untrusted_body_only_in_raw_output(self):
        self.tasks([("active", "acme/repo#12")])
        self.start()
        args = ["acme/repo#12", "--until", "change", "--json"]
        first = self.wait_call("watch-pr", args)
        self.assertEqual(self.request()["notices"], [])
        payload = json.dumps({"state": "OPEN", "body": "PR BODY MUST NOT ENTER MODEL NOTICE", "changed": ["head"]})
        self.finish(first, payload)
        notice = self.notice("pr")
        self.assert_rearmed_before_notice(first, self.wait_call("watch-pr", args, 2), notice)
        self.assertIn("acme/repo#12", notice["message"]["content"])
        self.assertNotIn("PR BODY", notice["message"]["content"])
        self.assertEqual(self.raw(notice)["stdout"], payload)

    def test_terminal_pr_snapshots_and_changes_drop_the_wait(self):
        for state, number in (("CLOSED", 12), ("MERGED", 13)):
            with self.subTest(state=state):
                self.put(self.fake / "prs.json", {f"acme/repo#{number}": {"state": state}})
                self.tasks([("active", f"acme/repo#{number}")])
                previous_outputs = set((self.project / "state/run/seat-waits").glob("*.json"))
                self.start()
                self.wait_call("watch-pr", [f"acme/repo#{number}", "--once", "--json"])
                self.until(lambda _: set((self.project / "state/run/seat-waits").glob("*.json")) > previous_outputs)
                self.request("tool")
                self.request("tick")
                self.assertFalse(self.calls("watch-pr", [f"acme/repo#{number}", "--until", "change", "--json"]))
                self.close_driver()
        self.put(self.fake / "prs.json", {})
        self.tasks([("active", "acme/repo#16")])
        self.start()
        args = ["acme/repo#16", "--until", "change", "--json"]
        self.finish(self.wait_call("watch-pr", args), json.dumps({"state": "MERGED"}))
        self.notice("pr")
        self.request("tool")
        self.request("tick")
        self.assertEqual(len(self.calls("watch-pr", args)), 1)

    def test_answered_and_expired_asks_drop_without_exposing_answer_body(self):
        self.asks()
        self.put(self.fake / "asks.json", {"objects": [
            {"id": name, "props": {"asker": {"tile": "ours"}}} for name in ("answered", "expired")]})
        self.start()
        for name, code in (("answered", 0), ("expired", 2)):
            self.finish(self.wait_call("easl", ["ask", "wait", name]), "PRIVATE ASK ANSWER", code)
        self.notice("ask", 2)
        self.request("tool")
        self.request("tick")
        for name in ("answered", "expired"):
            self.assertEqual(len(self.calls("easl", ["ask", "wait", name])), 1)
            notice = next(n for n in self.request()["notices"] if f"{name}." in n["message"]["content"])
            self.assertEqual(self.raw(notice)["stdout"], "PRIVATE ASK ANSWER")
        for notice in self.request()["notices"]:
            self.assertNotIn("PRIVATE ASK ANSWER", notice["message"]["content"])

    def test_transport_failure_retains_wait_and_saves_error_for_tools(self):
        self.asks()
        self.start()
        args = ["ask", "wait", "own-question"]
        first = self.wait_call("easl", args)
        self.finish(first, "raw partial answer", 1, "transport is unavailable")
        snapshot = self.until(lambda value: any("wait failed." in n["message"]["content"] for n in value["notices"]))
        notice = next(n for n in snapshot["notices"] if "wait failed." in n["message"]["content"])
        self.assertNotIn("raw partial answer", notice["message"]["content"])
        self.assertNotIn("transport is unavailable", notice["message"]["content"])
        result = self.raw(notice)
        self.assertEqual((result["code"], result["stdout"], result["stderr"]),
                         (1, "raw partial answer", "transport is unavailable"))
        self.request("tick")
        second = self.wait_call("easl", args, 2)
        self.finish(second, "answer after reconnect")
        self.notice("ask")

    def test_repeated_wait_failure_notifies_once_until_success(self):
        self.tasks([("active", "acme/repo#12")])
        self.start()
        args = ["acme/repo#12", "--until", "change", "--json"]
        call = self.wait_call("watch-pr", args)
        (self.project / "state/board.json").write_text("{")
        failures = lambda value: [n for n in value["notices"] if "wait failed." in n["message"]["content"]]
        for attempt in range(3):
            error = f"outage-{attempt}"
            self.finish(call, code=1, stderr=error)
            snapshot = self.until(lambda value: failures(value)
                                  and self.raw(failures(value)[0])["stderr"] == error)
            self.assertEqual(len(failures(snapshot)), 1)
            self.request("tool")
            self.request("tick")
            call = self.wait_call("watch-pr", args, attempt + 2)
        self.finish(call, json.dumps({"state": "OPEN"}))
        self.notice("pr")
        call = self.wait_call("watch-pr", args, 5)
        self.finish(call, code=1, stderr="new outage after recovery")
        snapshot = self.until(lambda value: len(failures(value)) >= 2)
        self.assertEqual(len(failures(snapshot)), 2)
        self.assertEqual(self.raw(failures(snapshot)[-1])["stderr"], "new outage after recovery")

    def test_repeated_discovery_failure_notifies_once_until_recovery(self):
        self.asks()
        failure = self.fake / "ask-list-error.json"
        self.put(failure, {"code": 1, "stderr": "offline-0"})
        self.start()
        failures = lambda value: [n for n in value["notices"] if "ask discovery failed." in n["message"]["content"]]
        snapshot = self.until(lambda value: failures(value))
        notice = failures(snapshot)[0]
        for attempt in range(1, 4):
            error = f"offline-{attempt}"
            self.put(failure, {"code": 1, "stderr": error})
            self.request("tool")
            self.request("tick")
            snapshot = self.until(lambda _: self.raw(notice)["stderr"] == error)
            self.assertEqual(len(failures(snapshot)), 1)
        failure.unlink()
        self.request("tool")
        self.wait_call("easl", ["ask", "wait", "own-question"])
        self.put(failure, {"code": 1, "stderr": "new discovery outage"})
        self.request("tool")
        snapshot = self.until(lambda value: len(failures(value)) >= 2)
        self.assertEqual(len(failures(snapshot)), 2)
        self.assertEqual(self.raw(failures(snapshot)[-1])["stderr"], "new discovery outage")

    def test_repeated_bad_state_notifies_once_until_repaired(self):
        pointer = self.project / "state/board.json"
        pointer.write_text("{")
        self.start()
        failures = lambda value: [n for n in value["notices"] if "refresh failed." in n["message"]["content"]]
        for _ in range(4):
            self.request("tool")
            snapshot = self.request("tick")
            self.assertEqual(len(failures(snapshot)), 1)
        self.put(pointer, {})
        self.request("tool")
        pointer.write_text("{")
        snapshot = self.request("tool")
        self.assertEqual(len(failures(snapshot)), 2)

    def test_bad_board_state_cannot_hide_lane_or_text_completion(self):
        for kind, seat in (("lane", self.project), ("text", self.assistant)):
            with self.subTest(kind=kind):
                if kind == "lane":
                    self.lane()
                pointer = seat / "state/board.json"
                pointer.write_text("{")
                self.start(seat)
                try:
                    call = self.wait_call(kind)
                    if kind == "lane":
                        self.lane(status="done")
                        self.finish(call, "completed lane final")
                    else:
                        self.put(self.fake / "messages.json", ["text consumed during bad board state"])
                    notice = self.notice(kind)
                    expected = "completed lane final" if kind == "lane" else "text consumed during bad board state"
                    self.assertIn(expected, notice["message"]["content"])
                    if kind == "text":
                        self.assert_rearmed_before_notice(call, self.wait_call("text", count=2), notice)
                        self.assertEqual((self.fake / "text.cursor").read_text(), "1")
                    self.put(pointer, {})
                    self.request("tool")
                    self.assertEqual(len([n for n in self.request()["notices"]
                                          if f"{kind} ready." in n["message"]["content"]]), 1)
                finally:
                    self.close_driver()

    def test_coordinator_has_no_text_wait_and_switch_stops_assistant_waits(self):
        self.start(self.assistant)
        text = self.wait_call("text")
        self.request("switch", cwd=str(self.project))
        self.wait_call("sleep", ["1800"], 2)
        self.until(lambda snapshot: text["pid"] not in snapshot["children"])
        self.assertEqual(len(self.calls("text")), 1)
        self.assertFalse((self.project / "state/run/seat-waits/text-inbox.json").exists())

    def test_lanes_subagents_and_nonseat_directories_start_no_waits(self):
        nested = self.project / "nested"
        nested.mkdir()
        for seat, kind, lane in ((self.project, "main", True), (self.project, "sub", False),
                                 (self.instance, "main", False), (nested, "main", False)):
            with self.subTest(seat=seat, kind=kind, lane=lane):
                snapshot = self.start(seat, kind=kind, lane=lane)
                self.assertEqual(snapshot["children"], [])
                self.assertEqual(snapshot["timers"], [])
                self.assertEqual(self.calls(), [])
                self.close_driver()

    def test_shutdown_terminates_owned_processes_and_restart_does_not_replay_text(self):
        self.start(self.assistant)
        self.wait_call("text")
        self.put(self.fake / "messages.json", ["message consumed before restart"])
        self.notice("text")
        self.wait_call("text", count=2)
        snapshot = self.request("shutdown")
        self.assertEqual(snapshot["timers"], [])
        self.until(lambda value: not value["children"])
        self.assertEqual((self.fake / "text.cursor").read_text(), "1")
        self.close_driver()
        self.start(self.assistant)
        self.wait_call("text", count=3)
        self.assertEqual(self.request()["notices"], [])
        self.put(self.fake / "messages.json", ["message consumed before restart", "new message after restart"])
        notice = self.notice("text")
        self.assertIn("new message after restart", notice["message"]["content"])
        self.assertNotIn("message consumed before restart", notice["message"]["content"])


    def test_shutdown_stops_lane_pr_ask_and_heartbeat_subprocesses(self):
        self.lane()
        self.tasks([("active", "acme/repo#12")])
        self.asks()
        self.start()
        owned = [
            self.wait_call("lane"),
            self.wait_call("watch-pr", ["acme/repo#12", "--until", "change", "--json"]),
            self.wait_call("easl", ["ask", "wait", "own-question"]),
            self.wait_call("sleep", ["1800"]),
        ]
        snapshot = self.request("shutdown")
        self.assertEqual(snapshot["timers"], [])
        self.until(lambda value: not value["children"])
        for call in owned:
            with self.assertRaises(ProcessLookupError):
                os.kill(call["pid"], 0)
        self.assertEqual(self.request("tick")["notices"], [])

if __name__ == "__main__":
    unittest.main()

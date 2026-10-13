import datetime as dt
import json
import unittest

from support import Sandbox
from tstacklib.seat import StateError, last_occurrence, parse_schedule, parse_tasks


class Board(Sandbox):
    def test_assistant_board_draws_its_status_and_one_note_per_project(self):
        assistant = self.instance / "assistant"
        (assistant / "state").mkdir(parents=True)
        (assistant / "state" / "board.json").write_text(json.dumps({"host": "testbox", "board": "brd_a", "tile": "obj_a"}))
        (self.seat / "state" / "tasks.tsv").write_text(
            "# demo tasks\nid\teffort\ttitle\tstate\tlane\tpr\tupdated\nci\tq4\tFix CI\trunning\tdemo-ci-1\t-\t2026-10-13\n")
        self.cli("tstack", "gate", "add", "--id", "g-scope", "--question", "Scope?", "--options", "a) small b) big",
                 "--default", "a", "--seat", str(assistant))
        keys = self.cli("tstack", "board", "--seat", str(assistant)).stdout.split()
        self.assertEqual(keys, ["assistant/status", "projects/demo"])
        upserts = {c[c.index("--key") + 1]: json.loads(c[c.index("--json") + 1])["props"]["markdown"]
                   for c in self.log_lines("easl.log") if c[0] == "object.upsert"}
        self.assertTrue(all(c[c.index("--board") + 1] == "brd_a" for c in self.log_lines("easl.log") if c[0] == "object.upsert"))
        self.assertIn("**g-scope**: Scope? (default a)", upserts["assistant/status"])
        self.assertIn("Tasks: 1 running.", upserts["projects/demo"])

    def test_board_without_board_json_is_an_error(self):
        result = self.cli("tstack", "board", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("board.json", result.stderr)


class Schedule(unittest.TestCase):
    def test_parse_and_last_occurrence(self):
        entries = parse_schedule("# name<TAB>when\ndigest\tdaily 15:00\nreview\tweekly Mon 09:30\n")
        self.assertEqual([e["name"] for e in entries], ["digest", "review"])
        wednesday = dt.datetime(2026, 10, 14, 14, 0)
        self.assertEqual(last_occurrence(entries[0], wednesday), dt.datetime(2026, 10, 13, 15, 0))
        self.assertEqual(last_occurrence(entries[0], wednesday.replace(hour=16)), dt.datetime(2026, 10, 14, 15, 0))
        self.assertEqual(last_occurrence(entries[1], wednesday), dt.datetime(2026, 10, 12, 9, 30))
        for bad in ("digest daily 25:00\n", "digest hourly\n", "review weekly Funday 09:00\n"):
            with self.subTest(bad), self.assertRaises(StateError):
                parse_schedule(bad)

    def test_tasks_reject_unknown_states(self):
        header = "id\teffort\ttitle\tstate\tlane\tpr\tupdated\n"
        self.assertEqual(parse_tasks(header + "a\te\tt\tmerged\t-\t-\t-\n")[0]["state"], "merged")
        with self.assertRaises(StateError):
            parse_tasks(header + "a\te\tt\tshipped\t-\t-\t-\n")

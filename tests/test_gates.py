import json
import unittest

from support import Sandbox, dedent
from tstacklib.seat import StateError, parse_gates, parse_options, render_gates

GATES = dedent("""
    # Gates

    ## g-web-efforts

    - Status: open
    - Question: Which efforts should web run next?
    - Options: a) search b) billing
    - Default: a
    - Due: 2026-10-14T16:00Z
    - Ask: obj_1@brd_1

    ## g-drop-table

    - Status: resolved
    - Question: Drop the old table?
    - Options: wait) hold off b) drop it
    - Default: wait
    - Answer: b) drop it
    - Irreversible: yes
    """)


class GatesFormat(unittest.TestCase):
    def test_round_trip(self):
        gates = parse_gates(GATES)
        self.assertEqual([g["id"] for g in gates], ["g-web-efforts", "g-drop-table"])
        self.assertEqual(gates[0]["Ask"], "obj_1@brd_1")
        self.assertEqual(parse_gates(render_gates(gates)), gates)
        self.assertEqual(render_gates(parse_gates(render_gates(gates))), render_gates(gates))

    def test_empty_file_and_bare_heading_have_no_gates(self):
        self.assertEqual(parse_gates(""), [])
        self.assertEqual(parse_gates("# Gates\n"), [])
        self.assertEqual(render_gates([]), "# Gates\n")

    def test_malformed_inputs_are_errors(self):
        bad = {
            "heading": GATES.replace("# Gates", "# Gate list"),
            "missing question": GATES.replace("- Question: Which efforts should web run next?\n", ""),
            "resolved without answer": GATES.replace("- Answer: b) drop it\n", ""),
            "open with answer": GATES.replace("- Status: resolved", "- Status: open"),
            "unknown status": GATES.replace("- Status: open", "- Status: maybe"),
            "duplicate id": GATES.replace("## g-drop-table", "## g-web-efforts"),
            "irreversible default": GATES.replace("- Default: wait", "- Default: b"),
            "unknown field": GATES.replace("- Due:", "- Deadline:"),
            "not a field line": GATES.replace("- Ask: obj_1@brd_1", "Ask obj_1"),
        }
        for name, text in bad.items():
            with self.subTest(name), self.assertRaises(StateError):
                parse_gates(text)

    def test_options(self):
        self.assertEqual(parse_options("a) ship now b) wait for review"), [("a", "ship now"), ("b", "wait for review")])
        self.assertEqual(parse_options("ship or not"), [])


class GateCommands(Sandbox):
    def test_add_asks_on_the_board_and_answer_withdraws_it(self):
        self.board()
        out = self.cli("tstack", "gate", "add", "--id", "g-pick", "--question", "Pick one?", "--options", "a) left b) right",
                       "--default", "b").stdout
        self.assertIn("asked as obj_ask1@brd_test", out)
        ask = next(c for c in self.log_lines("easl.log") if c[0] == "ask")
        self.assertEqual(ask[ask.index("--recommend") + 1], "b")
        self.assertEqual(ask[ask.index("--board") + 1], "brd_test")
        gates = parse_gates((self.seat / "state" / "gates.md").read_text())
        self.assertEqual((gates[0]["Status"], gates[0]["Ask"]), ("open", "obj_ask1@brd_test"))
        self.assertIn("g-pick", self.cli("tstack", "gate", "list", "--open").stdout)
        self.cli("tstack", "gate", "answer", "g-pick", "a) left")
        gates = parse_gates((self.seat / "state" / "gates.md").read_text())
        self.assertEqual((gates[0]["Status"], gates[0]["Answer"]), ("resolved", "a) left"))
        self.assertEqual(json.loads((self.stubs / "asks.json").read_text())["obj_ask1"]["status"], "cancelled")
        self.assertEqual(self.cli("tstack", "gate", "list", "--open").stdout, "")

    def test_irreversible_gate_must_default_to_wait(self):
        result = self.cli("tstack", "gate", "add", "--id", "g-drop", "--question", "Drop?", "--options", "a) drop",
                          "--default", "a", "--irreversible", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("wait", result.stderr)
        self.assertFalse((self.seat / "state" / "gates.md").exists())

    def test_duplicate_gate_refused(self):
        args = ("tstack", "gate", "add", "--id", "g-x", "--question", "Q?", "--options", "a) x b) y", "--default", "a")
        self.cli(*args)
        self.assertNotEqual(self.cli(*args, check=False).returncode, 0)

"""Seat state files: parse, validate and render (gates.md, tasks.tsv, intake.md, schedule.tsv, orders.md)."""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

from .util import Die, flock, read_json, write_atomic

TASK_HEADER = ["id", "effort", "title", "state", "lane", "pr", "updated"]
TASK_STATES = {"todo", "running", "waiting", "merged", "deployed", "done", "stuck", "failed", "cancelled"}
GATE_FIELDS = ["Status", "Question", "Options", "Default", "Answer", "Irreversible", "Due", "Ask"]
INTAKE_ITEM = re.compile(r"^- \[( |x)\] (\S+) ([^:]+): (.*)$")
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class StateError(Die):
    pass


# gates.md (pstack orch store format plus Irreversible, Due, Ask)

def parse_gates(text: str) -> list[dict]:
    raw = text.replace("\r", "").strip()
    if not raw or raw == "# Gates":
        return []
    prefix = "# Gates\n\n## "
    if not raw.startswith(prefix):
        raise StateError("gates.md has an invalid heading")
    gates = []
    for block in raw[len(prefix):].split("\n\n## "):
        lines = [line for line in block.split("\n") if line]
        gate_id = lines.pop(0) if lines else ""
        fields = {}
        for line in lines:
            match = re.fullmatch(r"- ([^:]+): (.*)", line)
            if not match or match.group(1) not in GATE_FIELDS or match.group(1) in fields:
                raise StateError(f"gates.md has a malformed gate {gate_id}")
            fields[match.group(1)] = match.group(2)
        if not gate_id or any(k not in fields for k in ("Question", "Options", "Default")):
            raise StateError(f"gates.md has a malformed gate {gate_id}")
        status = fields.get("Status")
        if status == "resolved" and "Answer" not in fields:
            raise StateError(f"gates.md gate {gate_id} is resolved without an Answer")
        if status not in ("open", "resolved") or (status == "open" and "Answer" in fields):
            raise StateError(f"gates.md has invalid status {status or ''} for {gate_id}")
        if fields.get("Irreversible", "no") not in ("yes", "no"):
            raise StateError(f"gates.md gate {gate_id}: Irreversible must be yes or no")
        if fields.get("Irreversible") == "yes" and fields["Default"] != "wait":
            raise StateError(f"gates.md gate {gate_id} is irreversible, so its Default must be wait")
        gates.append({"id": gate_id, **fields})
    ids = [g["id"] for g in gates]
    if len(set(ids)) != len(ids):
        raise StateError("gates.md has duplicate gate ids")
    return gates


def render_gates(gates: list[dict]) -> str:
    if not gates:
        return "# Gates\n"
    blocks = []
    for gate in gates:
        lines = [f"## {gate['id']}", ""]
        lines += [f"- {key}: {gate[key]}" for key in GATE_FIELDS if gate.get(key) not in (None, "")]
        blocks.append("\n".join(lines))
    return "# Gates\n\n" + "\n\n".join(blocks) + "\n"


def read_gates(seat: Path) -> list[dict]:
    path = seat / "state" / "gates.md"
    return parse_gates(path.read_text()) if path.exists() else []


def update_gates(seat: Path, change) -> list[dict]:
    """Read, change and atomically rewrite gates.md under the seat's gates lock."""
    with flock(seat / "state" / "run" / "gates.lock"):
        gates = read_gates(seat)
        change(gates)
        text = render_gates(gates)
        parse_gates(text)
        write_atomic(seat / "state" / "gates.md", text)
        return gates


def parse_options(text: str) -> list[tuple[str, str]]:
    """`a) Ship now b) Wait` -> [("a", "Ship now"), ("b", "Wait")]; [] when not in that shape."""
    parts = re.split(r"(?:^|\s)([A-Za-z0-9][\w-]*)\)\s+", text.strip())
    if parts[0].strip() or len(parts) < 5:
        return []
    return [(parts[i], parts[i + 1].strip().rstrip(";,")) for i in range(1, len(parts), 2)]


# tasks.tsv

def parse_tasks(text: str) -> list[dict]:
    rows = [line for line in text.splitlines() if line.strip() and not line.startswith("#")]
    if not rows:
        return []
    if rows[0].split("\t") != TASK_HEADER:
        raise StateError("tasks.tsv header must be: " + " ".join(TASK_HEADER))
    tasks = []
    for number, line in enumerate(rows[1:], 2):
        cells = line.split("\t")
        if len(cells) != len(TASK_HEADER):
            raise StateError(f"tasks.tsv row {number} has {len(cells)} fields, want {len(TASK_HEADER)}")
        task = dict(zip(TASK_HEADER, cells))
        if task["state"] not in TASK_STATES:
            raise StateError(f"tasks.tsv row {number}: unknown state {task['state']!r}")
        tasks.append(task)
    return tasks


def read_tasks(seat: Path) -> list[dict]:
    path = seat / "state" / "tasks.tsv"
    return parse_tasks(path.read_text()) if path.exists() else []


# intake.md

def parse_intake(text: str) -> list[dict]:
    items = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        match = INTAKE_ITEM.match(line)
        if not match:
            raise StateError(f"intake.md line {number} is not `- [ ] <time> <source>: <text>`")
        items.append({"done": match.group(1) == "x", "at": match.group(2), "source": match.group(3).strip(),
                      "text": match.group(4), "line": line})
    return items


def read_intake(seat: Path) -> list[dict]:
    path = seat / "state" / "intake.md"
    return parse_intake(path.read_text()) if path.exists() else []


# schedule.tsv

def parse_schedule(text: str) -> list[dict]:
    entries = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split(None, 1)
        when = parts[1].split() if len(parts) == 2 else []
        ok = (len(when) == 2 and when[0] == "daily") or (len(when) == 3 and when[0] == "weekly" and when[1] in WEEKDAYS)
        clock = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", when[-1]) if ok else None
        if not clock:
            raise StateError(f"schedule.tsv line {number}: want `name<TAB>daily HH:MM` or `weekly Mon HH:MM`")
        entries.append({"name": parts[0], "when": " ".join(when), "weekday": WEEKDAYS.index(when[1]) if when[0] == "weekly" else None,
                        "hour": int(clock.group(1)), "minute": int(clock.group(2))})
    return entries


def read_schedule(seat: Path) -> list[dict]:
    path = seat / "state" / "schedule.tsv"
    return parse_schedule(path.read_text()) if path.exists() else []


def last_occurrence(entry: dict, local_now: dt.datetime) -> dt.datetime:
    """The most recent time at or before local_now this entry was due (host local time)."""
    due = local_now.replace(hour=entry["hour"], minute=entry["minute"], second=0, microsecond=0)
    if entry["weekday"] is None:
        return due if due <= local_now else due - dt.timedelta(days=1)
    due -= dt.timedelta(days=(local_now.weekday() - entry["weekday"]) % 7)
    return due if due <= local_now else due - dt.timedelta(days=7)


# orders.md and board.json

def stop_reason(seat: Path) -> str | None:
    """None when orders.md is missing or says `STOP: no`; the reason otherwise."""
    path = seat / "state" / "orders.md"
    if not path.exists():
        return None
    first = (path.read_text().splitlines() or [""])[0].strip()
    match = re.fullmatch(r"STOP: (.+)", first)
    if not match:
        raise StateError("orders.md first line must be `STOP: no` or `STOP: <reason>`")
    return None if match.group(1).strip() == "no" else match.group(1).strip()


def read_board(seat: Path) -> dict | None:
    path = seat / "state" / "board.json"
    if not path.exists():
        return None
    try:
        board = json.loads(path.read_text())
    except ValueError:
        raise StateError("board.json is not valid JSON")
    if not isinstance(board, dict) or not str(board.get("board", "")).startswith("brd_") or not board.get("host"):
        raise StateError("board.json needs host and a brd_ board id")
    return board


def validate(seat: Path) -> list[str]:
    """Every parse problem in a seat's state, as `seat/file: problem` lines."""
    problems = []
    checks = [read_gates, read_tasks, read_intake, read_schedule, stop_reason, read_board]
    for check in checks:
        try:
            check(seat)
        except Die as error:
            problems.append(f"{seat}: {error}")
    for path in sorted((seat / "state" / "lanes").glob("*/*.json")):
        try:
            value = read_json(path)
            if not isinstance(value, dict):
                raise ValueError("not an object")
        except ValueError as error:
            problems.append(f"{path}: {error}")
    return problems

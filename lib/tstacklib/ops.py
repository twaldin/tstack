"""tstack gate, liveness and update."""

from __future__ import annotations

import re
from pathlib import Path

from . import easl
from .seat import parse_options, read_board, read_gates, update_gates
from .util import (HOME, Die, age, all_seats, engine_home, git, instance_home, iso, machine, parse_iso, read_json,
                   run, seat_name, state_root, write_json)

STALE = 30 * 60
SOURCE_FAILING = 60 * 60
MAX_RSS_MB = 1536
GATE_ID = re.compile(r"[a-z0-9][a-z0-9-]*")


# gates

def gate_add(seat: Path, gate_id: str, question: str, options: str, default: str, irreversible: bool,
             due: str | None) -> dict:
    if not GATE_ID.fullmatch(gate_id):
        raise Die("gate ids are lowercase letters, digits and dashes")
    if irreversible and default != "wait":
        raise Die("a gate on an irreversible action defaults to wait (--default wait)")
    if due:
        parse_iso(due)
    if any(g["id"] == gate_id for g in read_gates(seat)):
        raise Die(f"gate {gate_id} already exists")
    gate = {"id": gate_id, "Status": "open", "Question": question, "Options": options, "Default": default,
            "Irreversible": "yes" if irreversible else None, "Due": due}
    board = read_board(seat)
    if board:
        choices = parse_options(options)
        if irreversible and "wait" not in {c for c, _ in choices}:
            choices.append(("wait", "Wait"))
        gate["Ask"] = f"{easl.ask(board, question, choices, default)}@{board['board']}"

    def add(rows):
        if any(r["id"] == gate_id for r in rows):
            raise Die(f"gate {gate_id} already exists")
        rows.append(gate)
    update_gates(seat, add)
    return gate


def gate_answer(seat: Path, gate_id: str, text: str) -> dict:
    found = {}

    def resolve(rows):
        row = next((r for r in rows if r["id"] == gate_id), None)
        if row is None:
            raise Die(f"no gate {gate_id}")
        row.update(Status="resolved", Answer=text)
        found.update(row)
    update_gates(seat, resolve)
    board = read_board(seat)
    if board and found.get("Ask"):
        ask_id, _, board_id = found["Ask"].partition("@")
        try:
            props = easl.call("ask", "get", ask_id, host=board["host"])["object"].get("props", {})
            if props.get("status") == "open":
                easl.call("ask", "cancel", ask_id, host=board["host"])
        except Die as error:
            print(f"tstack gate: answered, but the board question {ask_id} on {board_id} was not withdrawn: {error}")
    return found


def gate_lines(seat: Path, open_only: bool) -> list[str]:
    lines = []
    for g in read_gates(seat):
        if open_only and g["Status"] != "open":
            continue
        tail = f" -> {g['Answer']}" if g.get("Answer") else f" (default {g['Default']})"
        lines.append(f"{g['id']}\t{g['Status']}\t{g['Question']}{tail}")
    return lines


# seat restarts (liveness and update)

def restart_prompt() -> str:
    path = engine_home() / "roles" / "restart.md"
    if not path.exists():
        raise Die(f"missing {path}")
    return path.read_text()


def local_seats() -> list[tuple[Path, dict]]:
    """Seats with a board.json naming this machine."""
    seats = []
    for seat in all_seats():
        try:
            board = read_board(seat)
        except Die:
            continue
        if board and board["host"] == machine() and board.get("tile"):
            seats.append((seat, board))
    return seats


def send_text(message: str) -> str:
    result = run([str(instance_home() / "bin" / "text"), "send", message], check=False, timeout=60)
    return "sent" if result.returncode == 0 else f"text failed (exit {result.returncode}): {result.stderr.strip()[-200:]}"


def liveness() -> list[str]:
    """Restart a seat whose wait heartbeat is over 30 min stale; text if still stale 30 min after, or a source fails for an hour."""
    out = []
    for seat, board in local_seats():
        name = seat_name(seat)
        path = seat / "state" / "run" / "liveness.json"
        live = read_json(path) or {}
        beat = read_json(seat / "state" / "run" / "heartbeat.json") or {}
        at = beat.get("at")
        stale_for = age(at)
        if stale_for is None or stale_for > STALE:
            restarted = live.get("restarted_at")
            if not restarted or (at and parse_iso(restarted) < parse_iso(at)):
                try:
                    easl.restart_seat(board, restart_prompt())
                    live["restarted_at"] = iso()
                    out.append(f"{name}: stale since {at or 'never'}; restarted in place")
                except easl.EaslConflict as error:
                    out.append(f"{name}: stale since {at or 'never'}; restart refused (busy): {error}")
                except Die as error:
                    out.append(f"{name}: stale since {at or 'never'}; restart failed: {error}")
            elif (age(restarted) or 0) > STALE and live.get("texted_for") != restarted:
                result = send_text(f"tstack: seat {name} on {machine()} has not polled since {at or 'never'}; "
                                   f"an in-place restart at {restarted} did not bring it back.")
                live["texted_for"] = restarted
                out.append(f"{name}: still stale after restart; {result}")
        texted = live.setdefault("texted_sources", {})
        for source, info in (beat.get("sources") or {}).items():
            since = info.get("failing_since")
            if since and (age(since) or 0) > SOURCE_FAILING and texted.get(source) != since:
                result = send_text(f"tstack: seat {name} on {machine()}: wait source {source} has failed since {since}: "
                                   f"{(info.get('error') or '')[:120]}")
                texted[source] = since
                out.append(f"{name}: source {source} failing since {since}; {result}")
        write_json(path, live)
    return out


# update

SEAT_WIDE = ("global.md", "roles/", "skills/", "agents/", "extensions/")


def seats_touched(changes: dict, seats: list[Path]) -> dict:
    """Which seats load a changed file: engine/instance globals, skills and agents reach every seat;
    an instance .md outside state/ reaches the seats at or below its directory."""
    touched = {}
    m = machine()
    for repo, paths in changes.items():
        for rel in paths:
            wide = rel.startswith(SEAT_WIDE) or (repo == "agents" and rel.startswith(
                (f"machines/{m}/global.md", f"machines/{m}/skills/", f"machines/{m}/agents/")))
            for seat in seats:
                inside = False
                if repo == "agents" and rel.endswith(".md") and "/state/" not in f"/{rel}":
                    folder = (instance_home() / rel).parent.resolve()
                    inside = seat.resolve() == folder or folder in seat.resolve().parents
                if wide or inside:
                    touched.setdefault(str(seat), f"{repo}/{rel} changed")
    return touched


def rss_mb(board: dict) -> float | None:
    agents = easl.call("agent.list", host=board["host"]).get("agents", [])
    pid = next((a.get("pid") for a in agents if a.get("tile") == board["tile"]), None)
    if not pid:
        return None
    result = run(["ps", "-o", "rss=", "-p", str(pid)], check=False)
    return int(result.stdout.strip()) / 1024 if result.stdout.strip().isdigit() else None


def update(setup_fn, check_fn) -> tuple[int, str]:
    repos = [("tstack", engine_home()), ("agents", instance_home()), ("dotfiles", HOME / "dotfiles")]
    notes, moved, changes = [], {}, {}
    for name, path in repos:
        if not (path / ".git").exists():
            notes.append(f"{name}=absent")
            continue
        old = git(path, "rev-parse", "HEAD")
        try:
            git(path, "fetch", "--quiet", timeout=300)
            git(path, "merge", "--ff-only", "--quiet", "@{u}")
        except Die as error:
            notes.append(f"{name}=skipped({str(error).splitlines()[0][:80]})")
            continue
        new = git(path, "rev-parse", "HEAD")
        notes.append(f"{name}={old[:7]}" + (f"..{new[:7]}" if new != old else ""))
        if new != old:
            moved[name] = (path, old)
            changes[name] = git(path, "diff", "--name-only", old, new).splitlines()
    setup_fn()
    problems = check_fn()
    code = 0
    if problems:
        for name, (path, old) in moved.items():
            git(path, "reset", "--quiet", "--keep", old)
        setup_fn()
        notes.append(f"check failed ({len(problems)}: {problems[0][:100]}); reset {','.join(moved) or 'nothing'}")
        changes, code = {}, 1
    else:
        notes.append("setup=ok check=ok")
    pending_path = state_root() / "restarts.json"
    pending = read_json(pending_path) or {}
    seats = local_seats()
    pending.update(seats_touched(changes, [s for s, _ in seats]))
    for seat, board in seats:
        try:
            mem = rss_mb(board)
        except Die:
            mem = None
        if mem and mem > MAX_RSS_MB:
            pending.setdefault(str(seat), f"rss {mem:.0f} MB")
    results = []
    for seat, board in seats:
        reason = pending.get(str(seat))
        if not reason:
            continue
        try:
            easl.restart_seat(board, restart_prompt())
            pending.pop(str(seat))
            results.append(f"{seat_name(seat)}:restarted({reason})")
        except easl.EaslConflict:
            results.append(f"{seat_name(seat)}:busy,retry")
        except Die as error:
            results.append(f"{seat_name(seat)}:failed({str(error)[:60]})")
    write_json(pending_path, pending)
    if results:
        notes.append("restarts=" + ",".join(results))
    line = f"{iso()} {machine()} " + " ".join(notes)
    log = state_root() / "update.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a") as handle:
        handle.write(line + "\n")
    return code, line


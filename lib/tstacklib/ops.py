"""tstack gate, liveness and update."""

from __future__ import annotations

import re
import subprocess
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


def send_text(message: str) -> tuple[bool, str]:
    result = run([str(instance_home() / "bin" / "text"), "send", message], check=False, timeout=60)
    return result.returncode == 0, ("sent" if result.returncode == 0 else
                                    f"text failed (exit {result.returncode}): {result.stderr.strip()[-200:]}")


def recover(board: dict, progress: dict) -> str:
    """One step of an in-place restart: resume the tile unless that already worked, then send the restart
    prompt unless that already worked. `progress` keeps `restarted`/`prompted` times and the last attempt."""
    progress["attempt"] = {"at": iso(), "error": None}
    try:
        if not progress.get("restarted"):
            easl.restart(board)
            progress["restarted"] = iso()
        if not progress.get("prompted"):
            easl.prompt(board, restart_prompt())
            progress["prompted"] = iso()
        return "restarted in place"
    except easl.EaslConflict as error:
        progress["attempt"]["error"] = f"busy: {error}"
    except Die as error:
        progress["attempt"]["error"] = str(error)
    step = "restart prompt" if progress.get("restarted") else "restart"
    return f"{step} failed: {progress['attempt']['error']}"


def liveness() -> list[str]:
    """For each local seat: a wait heartbeat over 30 min stale opens an incident and gets an in-place restart
    (retried every run until the restart and its prompt both went through). An incident still open after
    30 min, or a wait source failing for an hour, texts the operator; an alert counts only once sent."""
    out = []
    for seat, board in local_seats():
        name = seat_name(seat)
        path = seat / "state" / "run" / "liveness.json"
        live = read_json(path) or {}
        beat = read_json(seat / "state" / "run" / "heartbeat.json") or {}
        at = beat.get("at")
        stale_for = age(at)
        if stale_for is not None and stale_for <= STALE:
            if live.get("incident"):
                out.append(f"{name}: polling again; incident from {live['incident']} closed")
            live = {"source_alerts": live.get("source_alerts", {})}
        else:
            live.setdefault("incident", iso())
            if not (live.get("restarted") and live.get("prompted")):
                out.append(f"{name}: stale since {at or 'never'}; {recover(board, live)}")
            if (age(live["incident"]) or 0) > STALE and live.get("alerted") != live["incident"]:
                state = (f"restarted at {live['restarted']}, still not polling" if live.get("prompted")
                         else f"recovery failing: {(live.get('attempt') or {}).get('error')}")
                sent, detail = send_text(f"tstack: seat {name} on {machine()} has not polled since {at or 'never'}; {state}.")
                if sent:
                    live["alerted"] = live["incident"]
                out.append(f"{name}: stale for over 30 min of recovery; {detail}")
        alerts = live.setdefault("source_alerts", {})
        for source, info in (beat.get("sources") or {}).items():
            since = info.get("failing_since")
            if since and (age(since) or 0) > SOURCE_FAILING and alerts.get(source) != since:
                sent, detail = send_text(f"tstack: seat {name} on {machine()}: wait source {source} has failed since {since}: "
                                         f"{(info.get('error') or '')[:120]}")
                if sent:
                    alerts[source] = since
                out.append(f"{name}: source {source} failing since {since}; {detail}")
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


def tstack_cli(*args: str) -> subprocess.CompletedProcess:
    """The engine's own bin/tstack, as checked out now (not the code this process imported)."""
    return subprocess.run([str(engine_home() / "bin" / "tstack"), *args], capture_output=True, text=True, timeout=900)


def admit() -> list[str]:
    """Setup, then check, through the checked-out CLI. Any failure is a refusal."""
    for step in (("setup",), ("check",)):
        try:
            result = tstack_cli(*step)
        except (OSError, subprocess.SubprocessError) as error:
            return [f"{step[0]}: {type(error).__name__}: {error}"]
        if result.returncode:
            lines = [line for line in (result.stdout + result.stderr).splitlines()
                     if line.strip() and not line.startswith("tstack check:")]
            return [f"{step[0]} exit {result.returncode}: {(lines or ['no output'])[0][:160]}"]
    return []


def rollback(moved: dict) -> list[str]:
    notes = []
    for name, (path, old) in moved.items():
        try:
            git(path, "reset", "--quiet", "--keep", old)
            notes.append(f"{name}->{old[:7]}")
        except Die as error:
            notes.append(f"{name} rollback failed: {str(error).splitlines()[0][:100]}")
    if moved:
        try:
            result = tstack_cli("setup")
            if result.returncode:
                notes.append(f"setup after rollback exit {result.returncode}")
        except (OSError, subprocess.SubprocessError) as error:
            notes.append(f"setup after rollback failed: {error}")
    return notes


def restart_changed(changes: dict) -> list[str]:
    """Restart seats whose loaded files changed or whose omp is over 1.5 GB; a busy seat is retried next run."""
    pending_path = state_root() / "restarts.json"
    pending = {seat: value if isinstance(value, dict) else {"reason": value}
               for seat, value in (read_json(pending_path) or {}).items()}
    seats = local_seats()
    for seat, reason in seats_touched(changes, [s for s, _ in seats]).items():
        pending.setdefault(seat, {"reason": reason})
    for seat, board in seats:
        try:
            mem = rss_mb(board)
        except Die:
            mem = None
        if mem and mem > MAX_RSS_MB:
            pending.setdefault(str(seat), {"reason": f"rss {mem:.0f} MB"})
    results = []
    for seat, board in seats:
        progress = pending.get(str(seat))
        if not progress:
            continue
        outcome = recover(board, progress)
        if progress.get("prompted"):
            pending.pop(str(seat))
            results.append(f"{seat_name(seat)}:restarted({progress['reason']})")
        else:
            results.append(f"{seat_name(seat)}:{'busy,retry' if outcome.startswith('restart failed: busy') else outcome[:80]}")
    write_json(pending_path, pending)
    return results


def update() -> tuple[int, str]:
    """Fast-forward the repos, admit the result through the new bin/tstack, roll every moved repo back on any
    failure, restart seats whose files changed. Always appends one status line to update.log."""
    notes, moved, code = [], {}, 0
    try:
        changes = {}
        for name, path in (("tstack", engine_home()), ("agents", instance_home()), ("dotfiles", HOME / "dotfiles")):
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
        problems = admit()
        if problems:
            code = 1
            notes.append(f"admission failed ({problems[0]}); rollback: {', '.join(rollback(moved)) or 'nothing moved'}")
            moved, changes = {}, {}
        else:
            notes.append("setup=ok check=ok")
            moved = {}
        results = restart_changed(changes)
        if results:
            notes.append("restarts=" + ",".join(results))
    except Exception as error:  # the transaction boundary: whatever broke, roll back and log it
        code = 1
        notes.append(f"error ({type(error).__name__}: {str(error)[:120]}); rollback: {', '.join(rollback(moved)) or 'nothing moved'}")
    finally:
        line = f"{iso()} {machine()} " + " ".join(notes)
        log = state_root() / "update.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a") as handle:
            handle.write(line + "\n")
    return code, line

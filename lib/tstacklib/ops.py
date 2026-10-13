"""tstack gate, liveness, update and restart-self."""

from __future__ import annotations

import contextlib
import copy
import datetime as dt
import os
import re
import secrets
import subprocess
from pathlib import Path

from . import easl
from .seat import parse_options, read_board, read_gates, update_gates
from .util import (HOME, Die, age, all_seats, engine_home, flock, git, instance_home, iso, lock_wait, machine, parse_iso,
                   read_json, run, seat_name, session_owner, state_root, write_json)

STALE = 30 * 60
ALERT_AFTER = 60 * 60
SOURCE_FAILING = 60 * 60
MAX_RSS_MB = 1536
PENDING_GRACE = 120
PENDING_TTL = 60 * 60
GATE_ID = re.compile(r"[a-z0-9][a-z0-9-]*")
SEAT_WIDE = ("global.md", "roles/", "skills/", "agents/", "extensions/")
ASK = ("Files you load changed ({reasons}, generation {generation}). At your next safe point, after acking your "
       "batch, run `tstack restart-self`.")


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


# seat restarts (D18). Update never restarts a seat: every run compares the files a seat loads, as installed,
# with the generation its omp loaded and asks the seat, by message, to run `tstack restart-self` at a safe point.
# Liveness restarts a seat whose wait went quiet, with a plain restart that easl refuses while the seat works.
# Either restart leaves a pending generation that the first wait under the new omp confirms as loaded. All of
# them share one record per seat: state/run/recovery.json under state/run/recovery.lock.

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
    """(sent, detail). A nonzero exit, a timeout or a text binary that cannot run is a failed send."""
    try:
        result = run([str(instance_home() / "bin" / "text"), "send", message], check=False, timeout=60)
    except (Die, OSError) as error:
        return False, f"text failed: {str(error)[:200]}"
    return result.returncode == 0, ("sent" if result.returncode == 0 else
                                    f"text failed (exit {result.returncode}): {result.stderr.strip()[-200:]}")


@contextlib.contextmanager
def recovery(seat: Path, wait: float | None):
    """The seat's recovery record under its lock, as (record, save); (None, None) when the lock stays busy
    (`wait` None: do not wait for it). Fields: `loaded` (the tstack and agents commits the seat's omp loaded
    its files from), `pending` (a restart made or prepared, with the generation the new omp loads, until a wait
    under that omp confirms it), `asked`, `restart` (by, at, prompted, attempt), and liveness's `incident`,
    `attempt`, `alerted`, `source_alerts`."""
    run_dir = seat / "state" / "run"
    path = run_dir / "recovery.json"
    with flock(run_dir / "recovery.lock", blocking=wait is not None, timeout=wait) as held:
        if not held:
            yield None, None
            return
        record = read_json(path) or {}
        yield record, lambda: write_json(path, record)


def newer(when: str | None, than: str | None) -> bool:
    """Whether ISO time `when` is later than `than`; False when either is missing or unreadable."""
    try:
        return bool(when and than) and parse_iso(when) > parse_iso(than)
    except ValueError:
        return False


def heartbeat(seat: Path) -> dict:
    return read_json(seat / "state" / "run" / "heartbeat.json") or {}


def repos() -> tuple[tuple[str, Path], ...]:
    return (("tstack", engine_home()), ("agents", instance_home()))


def installed() -> dict:
    """The generation a seat starting now loads: the tstack and agents commits checked out (None where a repo
    is not a git checkout)."""
    return {name: git(path, "rev-parse", "HEAD", check=False) or None for name, path in repos()}


def label(generation: dict) -> str:
    return " ".join(f"{name}@{(generation.get(name) or '?')[:7]}" for name, _ in repos())


def loads(seat: Path, repo: str, rel: str) -> bool:
    """Whether the seat's omp loads file `rel` of repo `repo`: globals, roles, skills, agents and extensions reach
    every seat (the machine's too); an instance .md outside state/ reaches the seats at or below its directory."""
    m = machine()
    if rel.startswith(SEAT_WIDE) or (repo == "agents" and rel.startswith(
            (f"machines/{m}/global.md", f"machines/{m}/skills/", f"machines/{m}/agents/"))):
        return True
    if repo == "agents" and rel.endswith(".md") and "/state/" not in f"/{rel}":
        folder = (instance_home() / rel).parent.resolve()
        return seat.resolve() == folder or folder in seat.resolve().parents
    return False


def unloaded(seat: Path, loaded, now: dict) -> list[str]:
    """The files the seat loads that differ between the generation its omp loaded and the one installed now;
    one reason in their place when what it loaded is unknown."""
    if not isinstance(loaded, dict):
        return ["no record of what this session loaded"]
    changed = []
    for name, path in repos():
        old, new = loaded.get(name), now.get(name)
        if old == new:
            continue
        if not old or not new:
            return [f"no record of the {name} commit this session loaded"]
        diff = run(["git", "-C", str(path), "diff", "--name-only", old, new], check=False)
        if diff.returncode:
            return [f"the {name} commit this session loaded ({old[:7]}) is not in {path}"]
        changed += [f"{name}/{rel}" for rel in diff.stdout.splitlines() if loads(seat, name, rel)]
    return changed


def prepare_restart(record: dict, by: str, replaces: int | None) -> None:
    """Record a restart as the seat's recovery: its restart prompt is owed, and `pending` holds the generation
    the new omp loads, and the pid of the omp it replaces, until a wait under another omp confirms it
    (`confirm_restart`)."""
    at = iso()
    record["restart"] = {"by": by, "at": at, "prompted": None}
    record["pending"] = {"generation": installed(), "at": at, "by": by, "replaces": replaces}


def confirm_restart(seat: Path, omp: int | None) -> None:
    """Run by the seat's wait at every poll, with the pid of the omp it runs under (`session_owner`): a wait under
    any omp but the replaced one runs in the restarted omp, so the pending generation becomes the loaded one. A
    wait under the replaced omp (its restart never happened) leaves it pending. Pids, not clocks: a process's
    start time reads up to a second or two early on Linux, and a reused pid only delays confirmation."""
    if not omp or "pending" not in (read_json(seat / "state" / "run" / "recovery.json") or {}):
        return
    with recovery(seat, None) as (record, save):
        pending = (record or {}).get("pending")
        if pending and omp != pending.get("replaces"):
            record["loaded"] = pending["generation"]
            record["loaded_by"] = {"omp": omp, "confirmed": iso()}
            del record["pending"]
            save()


def settle_pending(seat: Path, record: dict) -> str | None:
    """Drop a pending restart that will never be confirmed: the replaced omp's wait still polls two minutes on
    (restart-self died before its restart went out), or none confirmed it within an hour. Returns a note when
    dropped; `pending` left in place means a restart is in progress."""
    pending = record["pending"]
    beat = heartbeat(seat)
    try:
        due = iso(parse_iso(pending["at"]) + dt.timedelta(seconds=PENDING_GRACE))
    except (KeyError, ValueError):
        due = None
    old_polling = pending.get("replaces") is not None and beat.get("omp") == pending["replaces"] and newer(beat.get("at"), due)
    if old_polling or (age(pending.get("at")) or 0) > PENDING_TTL:
        del record["pending"]
        return f"restart at {pending.get('at')} never confirmed" + (" (the old session still polls)" if old_polling else "")
    return None


def send_owed_prompt(seat: Path, board: dict, record: dict, save) -> str | None:
    """Deliver the restart prompt a restart (any path's) left owed: skipped once the seat's wait has polled
    since the restart, else sent once the tile reports idle (omp's easl integration back). None when nothing
    is owed."""
    restart = record.get("restart") or {}
    if not restart.get("at") or restart.get("prompted"):
        return None
    beat = heartbeat(seat).get("at")
    if newer(beat, restart["at"]):
        restart["prompted"] = beat
        save()
        return f"polling again since {beat}; no restart prompt needed"
    try:
        easl.wait_idle(board)
        easl.prompt(board, restart_prompt())
    except Die as error:
        restart["attempt"] = {"at": iso(), "error": str(error)[:300]}
        save()
        return f"restart prompt failed: {str(error)[:200]}"
    restart["prompted"] = iso()
    restart.pop("attempt", None)
    save()
    return "restart prompt sent"


def restart_self(seat: Path) -> str:
    """Run by a seat in its own tile at a safe point: record a prepared restart (`pending`, confirmed only by a
    wait under the restarted omp) with its restart prompt owed (liveness or the next update sends it once the
    tile is idle), then restart the seat's own tile with force. The restart ends this process; when easl
    refuses, the record goes back."""
    tile = os.environ.get("EASL_TILE_ID")
    if not tile:
        raise Die("tstack restart-self runs inside the seat's own easl tile; EASL_TILE_ID is unset")
    board = read_board(seat)
    if not board or board.get("tile") != tile:
        raise Die(f"this tile ({tile}) is not the seat's tile in state/board.json ({(board or {}).get('tile')})")
    with recovery(seat, lock_wait()) as (record, save):
        if record is None:
            raise Die("the seat's recovery record is busy; run tstack restart-self again in a minute", 3)
        before = {key: copy.deepcopy(record.get(key)) for key in ("restart", "pending")}
        prepare_restart(record, "self", session_owner())
        save()
    try:
        easl.restart(board, force=True)
    except Die as error:
        with recovery(seat, lock_wait()) as (record, save):
            if record is not None:
                for key, value in before.items():
                    if value is None:
                        record.pop(key, None)
                    else:
                        record[key] = value
                save()
        raise Die(f"easl refused the restart: {error}")
    return f"restarting {tile}; tstack sends roles/restart.md once it reports idle"


def liveness() -> list[str]:
    """For each local seat: send a restart prompt still owed; a wait heartbeat over 30 min stale opens an
    incident and gets one plain in-place restart unless the seat was restarted since it last polled (easl
    refuses a working seat; tstack never forces one, it retries next run). Stale over 60 min texts the
    operator once per incident, as does a wait source failing for an hour; an alert counts only once sent."""
    out = []
    for seat, board in local_seats():
        with recovery(seat, None) as (record, save):
            if record is None:
                out.append(f"{seat_name(seat)}: recovery record busy (update or restart-self holds it); next run")
                continue
            out += [f"{seat_name(seat)}: {line}" for line in watch(seat, board, record, save)]
            save()
    return out


def watch(seat: Path, board: dict, record: dict, save) -> list[str]:
    out, name = [], seat_name(seat)
    beat = heartbeat(seat)
    at = beat.get("at")
    stale_for = age(at)
    owed = send_owed_prompt(seat, board, record, save)
    if owed:
        out.append(owed)
    if stale_for is not None and stale_for <= STALE:
        if record.get("incident"):
            out.append(f"polling again; incident from {record['incident']} closed")
        for key in ("incident", "attempt", "alerted"):
            record.pop(key, None)
    else:
        record.setdefault("incident", iso())
        restart_at = (record.get("restart") or {}).get("at")
        # the recovery under way: a restart since the seat last polled, or any restart when it never polled
        active = newer(restart_at, at) if at else bool(restart_at)
        if not active:
            try:
                easl.restart(board)
            except easl.EaslConflict as error:
                record["attempt"] = {"at": iso(), "error": f"busy, never forced: {str(error)[:200]}"}
                out.append(f"stale since {at or 'never'}; not restarted, the seat is busy (next run retries)")
            except Die as error:
                record["attempt"] = {"at": iso(), "error": str(error)[:300]}
                out.append(f"stale since {at or 'never'}; restart failed: {str(error)[:200]}")
            else:
                prepare_restart(record, "liveness", beat.get("omp"))
                record.pop("attempt", None)
                save()
                out.append(f"stale since {at or 'never'}; restarted in place; {send_owed_prompt(seat, board, record, save)}")
        save()
        stale_total = stale_for if stale_for is not None else STALE + (age(record["incident"]) or 0)
        if stale_total > ALERT_AFTER and record.get("alerted") != record["incident"]:
            restart = record.get("restart") or {}
            if not (newer(restart.get("at"), at) if at else restart.get("at")):
                state = f"not restarted: {(record.get('attempt') or {}).get('error')}"
            elif restart.get("prompted"):
                state = f"restarted at {restart['at']} ({restart.get('by')}), still not polling"
            else:
                state = f"restarted at {restart['at']}, restart prompt not delivered: {(restart.get('attempt') or {}).get('error')}"
            sent, detail = send_text(f"tstack: seat {name} on {machine()} has not polled since {at or 'never'}; {state}.")
            if sent:
                record["alerted"] = record["incident"]
            out.append(f"stale for over 60 min; {detail}")
    alerts = record.setdefault("source_alerts", {})
    for source, info in (beat.get("sources") or {}).items():
        since = info.get("failing_since")
        if since and (age(since) or 0) > SOURCE_FAILING and alerts.get(source) != since:
            sent, detail = send_text(f"tstack: seat {name} on {machine()}: wait source {source} has failed since {since}: "
                                     f"{(info.get('error') or '')[:120]}")
            if sent:
                alerts[source] = since
            out.append(f"source {source} failing since {since}; {detail}")
    return out


# update

def rss_mb(agent: dict) -> float | None:
    pid = agent.get("pid")
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


def ask_restarts() -> list[str]:
    """For each local seat: send a restart prompt a restart left owed; then, unless a restart is in progress,
    compare the files the seat loads as installed now with the generation its omp loaded, and while any differ
    (or its omp is over 1.5 GB) send it one message (out of band) asking it to run `tstack restart-self` at its
    next safe point. Every run compares afresh, so a run that skipped a seat (its record busy, easl down) loses
    nothing. Never restarts a seat."""
    now, results = installed(), []
    for seat, board in local_seats():
        name = seat_name(seat)
        with recovery(seat, lock_wait()) as (record, save):
            if record is None:
                results.append(f"{name}:recovery record busy, next run compares again")
                continue
            owed = send_owed_prompt(seat, board, record, save)
            if owed:
                results.append(f"{name}:{owed[:80]}")
            if record.get("pending"):
                dropped = settle_pending(seat, record)
                save()
                if not dropped:
                    results.append(f"{name}:restart at {record['pending']['at']} in progress")
                    continue
                results.append(f"{name}:{dropped}")
            reasons = unloaded(seat, record.get("loaded"), now)
            try:
                agent = easl.agent(board) or {}
            except Die as error:
                results.append(f"{name}:easl agent.list failed ({str(error)[:80]})")
                continue
            mem = rss_mb(agent)
            if mem and mem > MAX_RSS_MB:
                reasons.append(f"omp uses {mem:.0f} MB")
            if reasons:
                results.append(f"{name}:{ask(board, agent, record, save, reasons, label(now))}")
    return results


def ask(board: dict, agent: dict, record: dict, save, reasons: list[str], wanted: str) -> str:
    """Message the seat to run `tstack restart-self`. Only an omp whose easl integration takes messages gets
    it, so the text never lands typed in its editor."""
    if (agent.get("protocol") or 0) < 1:
        return "restart-self not asked (its omp takes no messages now; next run asks again)"
    shown = ", ".join(reasons[:3]) + (f" and {len(reasons) - 3} more" if len(reasons) > 3 else "")
    message = "msg_tstack-restart-" + secrets.token_hex(10)
    try:
        sent = easl.prompt(board, ASK.format(reasons=shown, generation=wanted), message=message)
    except Die as error:
        return f"restart-self ask failed ({str(error)[:100]})"
    record["asked"] = {"generation": wanted, "at": iso(), "delivery": sent.get("delivery")}
    save()
    if sent.get("delivery") != "message":
        return f"restart-self asked, but easl typed it ({sent.get('delivery')})"
    return f"asked to restart-self ({shown})"


def update() -> tuple[int, str]:
    """Fast-forward the repos, admit the result through the new bin/tstack, roll every moved repo back on any
    failure, ask seats whose loaded files differ from the installed ones to restart themselves. Always appends
    one status line to update.log."""
    notes, moved, code = [], {}, 0
    try:
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
        problems = admit()
        if problems:
            code = 1
            notes.append(f"admission failed ({problems[0]}); rollback: {', '.join(rollback(moved)) or 'nothing moved'}")
        else:
            notes.append("setup=ok check=ok")
        moved = {}
        results = ask_restarts()
        if results:
            notes.append("seats=" + ",".join(results))
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

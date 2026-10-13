"""tstack wait: poll a seat's sources, resume parked lanes on PR events, wake the seat with batches.

State lives in one file, state/run/wait/state.json, replaced atomically: cursors per source, pending
events, delivered batches not yet acked, and PR dispatches no lane run has accepted yet. Each source works
on a copy of its cursor and returns the new cursor with its events; a poll commits both together in one
write, and a failing source commits neither. A batch stays until `--ack`, so a seat that dies before
handling one gets it again.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import os
import subprocess
import time
from pathlib import Path

from . import easl, lanes, prwatch
from .seat import last_occurrence, read_board, read_gates, read_intake, read_schedule, update_gates
from .util import Die, age, flock, iso, money, parse_iso, read_json, say, write_json

URGENT_SOURCES = {"text", "operator"}
FIRST_SIGHT_DAYS = 7
SOURCE_ERRORS = (Die, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError)


def digest(*parts: str) -> str:
    return hashlib.sha1("\x1f".join(parts).encode()).hexdigest()[:12]


def event(kind: str, text: str, urgent: bool, key: str) -> dict:
    return {"id": f"ev-{digest(kind, key)}", "kind": kind, "text": " ".join(text.split()), "urgent": urgent, "at": iso()}


class Waiter:
    def __init__(self, seat: Path):
        self.seat = seat
        self.dir = seat / "state" / "run" / "wait"
        self.path = self.dir / "state.json"
        self.state = read_json(self.path) or {}
        for key, empty in (("cursors", {}), ("pending", []), ("batches", {})):
            self.state.setdefault(key, empty)
        self.cursors = self.state["cursors"]
        self.board = None
        self.resumed: list[str] = []

    def save(self) -> None:
        write_json(self.path, self.state)

    # sources: each takes a copy of its cursor (None on first sight) and returns (new cursor, events)

    def lanes(self, seen):
        seen = seen or {}
        events = []
        for lane in lanes.lanes(self.seat):
            record = lanes.reconcile(lane)
            report = lane.report() or {}
            signature = f"{record['status']}|{len(record.get('runs', []))}|{report.get('at')}"
            first = lane.id not in seen
            if seen.get(lane.id) == signature:
                continue
            seen[lane.id] = signature
            if first and (age(record.get("updated")) or 0) > FIRST_SIGHT_DAYS * 86400:
                continue
            status, summary = record["status"], report.get("summary") or record.get("reason") or ""
            if status in ("done", "stuck", "failed"):
                cost = f" ({money(record.get('cost_usd'))})"
                why = record.get("reason") if status != "done" and record.get("reason") else summary
                events.append(event(f"lane-{status}", f"lane {lane.id} {status}{cost}: {why}", True, signature + lane.id))
            elif status == "waiting" and not record.get("pr"):
                events.append(event("lane-waiting", f"lane {lane.id} waiting on {report.get('waiting_on') or '?'}: {summary}",
                                    False, signature + lane.id))
        return seen, events

    def prs(self, cursors):
        """PR events for waiting lanes resume the lane directly, one dispatch per lane at a time. A dispatch's id
        comes from the GitHub occurrences behind its events. The dispatch is saved before its launch and retried
        each poll until a run that took its id is accepted by its supervisor, or the lane refuses it; then one
        write drops it and moves the lane's PR cursor past it."""
        cursors = cursors or {}
        dispatches = self.state.setdefault("dispatches", {})
        me, failures = None, []
        present = lanes.lanes(self.seat)
        for gone in set(dispatches) - {lane.id for lane in present}:
            del dispatches[gone]
        for lane in present:
            try:
                if lane.id in dispatches and not self.dispatch(lane, dispatches[lane.id], cursors):
                    continue
                record = lane.load()
                if record["status"] != "waiting" or not record.get("pr"):
                    continue
                me = me or prwatch.viewer()
                pr = prwatch.view(record["pr"])
                snap = prwatch.wait_snapshot(pr, prwatch.threads(record["pr"], me), me)
                happened = prwatch.actionable((cursors.get(lane.id) or {}).get("fp"), snap, lane.id)
                if not happened:
                    cursors[lane.id] = {"fp": snap}
                    continue
                labels = [label for label, _ in happened]
                red = snap["ci"] == "red" and snap["failing"] and any(label.startswith("checks-red") for label in labels)
                dispatches[lane.id] = {
                    "id": "pr-" + digest(lane.id, *sorted(o for _, ids in happened for o in ids)),
                    "text": f"PR {record['pr']} at {snap['head'][:12]}: {'; '.join(labels)}.",
                    "kind": "check-failed" if red else "pr", "checks": snap["failing"] if red else [], "fp": snap}
                self.save()
                self.dispatch(lane, dispatches[lane.id], cursors)
            except SOURCE_ERRORS as error:
                failures.append(f"{lane.id}: {error}")
        if failures:
            raise Die("; ".join(failures))
        return cursors, []

    def dispatch(self, lane, pending: dict, cursors: dict) -> bool:
        """Launch a saved dispatch unless a run already took it. Once it is settled (a run with its id was
        accepted by its supervisor, or the lane refused it) drop it and advance the lane's cursor, committed
        and staged, in one write. Returns whether it settled; held and busy lanes retry next poll."""
        taken = lanes.dispatched(lane.load(), pending["id"])
        settled = False
        if taken is None:
            try:
                taken, launched = lanes.resume(lane, pending["text"], pending["kind"], pending["checks"],
                                               event_id=pending["id"])
                if launched:
                    self.resumed.append(lane.id)
            except Die as error:
                if error.code not in (lanes.EXIT_BUSY, lanes.EXIT_REFUSED, lanes.EXIT_STUCK):
                    raise
                settled = error.code != lanes.EXIT_BUSY
        if taken is not None:
            settled = bool(next(r for r in lane.load()["runs"] if r["run"] == taken).get("accepted"))
        if settled:
            cursors[lane.id] = {"fp": pending["fp"]}
            self.cursors.setdefault("prs", {})[lane.id] = {"fp": pending["fp"]}
            del self.state["dispatches"][lane.id]
            self.save()
        return settled

    def gates(self, cursor):
        if cursor is None:
            # The baseline is gates.md as found, saved before any easl answer is applied, so an answer
            # fetched now (first poll included) is an event.
            resolved = sorted(g["id"] for g in read_gates(self.seat) if g.get("Status") == "resolved")
            self.cursors["gates"] = {"resolved": resolved, "due": []}
            self.save()
            cursor = copy.deepcopy(self.cursors["gates"])
        gates = read_gates(self.seat)
        if self.board:
            for gate in gates:
                if gate.get("Status") == "open" and gate.get("Ask"):
                    ask_id, _, board_id = gate["Ask"].partition("@")
                    answer = easl.ask_answer({**self.board, "board": board_id}, ask_id)
                    if answer:
                        def resolve(rows, gate_id=gate["id"], text=answer):
                            row = next(r for r in rows if r["id"] == gate_id)
                            row.update(Status="resolved", Answer=text)
                        gates = update_gates(self.seat, resolve)
        resolved = {g["id"]: g.get("Answer", "") for g in gates if g.get("Status") == "resolved"}
        events = [event("gate-answered", f"gate {gate_id} answered: {answer}", True, gate_id + answer)
                  for gate_id, answer in resolved.items() if gate_id not in cursor["resolved"]]
        now = dt.datetime.now(dt.timezone.utc)
        for gate in gates:
            overdue = gate.get("Status") == "open" and gate.get("Due") and parse_iso(gate["Due"]) <= now
            if overdue and gate["id"] not in cursor["due"]:
                cursor["due"].append(gate["id"])
                events.append(event("gate-due", f"gate {gate['id']} is past due with no answer; default: {gate['Default']}",
                                    True, gate["id"] + "due"))
        cursor["resolved"] = sorted(resolved)
        return cursor, events

    def intake(self, seen):
        seen = set(seen or [])
        items = [i for i in read_intake(self.seat) if not i["done"]]
        events = [event("intake", f"{i['source']}: {i['text']}", i["source"].lower() in URGENT_SOURCES, i["line"])
                  for i in items if digest(i["line"]) not in seen]
        return sorted(digest(i["line"]) for i in items), events

    def schedule(self, fired):
        fired = fired or {}
        local_now = dt.datetime.now().astimezone()
        events = []
        for entry in read_schedule(self.seat):
            due = last_occurrence(entry, local_now)
            if entry["name"] in fired and due > parse_iso(fired[entry["name"]]):
                events.append(event("schedule", f"{entry['name']} is due ({entry['when']})", False, entry["name"] + due.isoformat()))
            if entry["name"] not in fired or due > parse_iso(fired[entry["name"]]):
                fired[entry["name"]] = local_now.isoformat(timespec="seconds")
        return fired, events

    def wait_d(self, script: Path, seen):
        result = subprocess.run([str(script)], cwd=self.seat, capture_output=True, text=True, timeout=60,
                                env={**os.environ, "TSTACK_SEAT": str(self.seat)})
        if result.returncode:
            raise Die(f"exit {result.returncode}: {result.stderr.strip()[-200:]}")
        seen = seen or []
        events = []
        for line in result.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3 or not parts[0]:
                raise Die(f"bad line {line[:80]!r}: want id<TAB>kind<TAB>text")
            if parts[0] in seen:
                continue
            seen.append(parts[0])
            events.append(event(parts[1], parts[2], parts[1] in URGENT_SOURCES, f"{script.name}:{parts[0]}"))
        return seen[-500:], events

    # polling and delivery

    def sources(self) -> list[tuple[str, tuple[str, ...], object]]:
        """(heartbeat name, cursor path in state.json, source function)."""
        found = [("lanes", ("lanes",), self.lanes), ("github", ("prs",), self.prs), ("gates", ("gates",), self.gates),
                 ("intake", ("intake",), self.intake), ("schedule", ("schedule",), self.schedule)]
        scripts = self.seat / "wait.d"
        if scripts.is_dir():
            found += [(f"wait.d/{p.name}", ("wait.d", p.name), lambda seen, p=p: self.wait_d(p, seen))
                      for p in sorted(scripts.iterdir()) if p.is_file() and os.access(p, os.X_OK)]
        return found

    def cursor(self, path: tuple[str, ...]):
        value = self.cursors
        for key in path:
            value = (value or {}).get(key)
        return copy.deepcopy(value)

    def poll(self) -> bool:
        """One pass over every source; returns whether anything changed."""
        self.heartbeat(None, polling=True)
        self.resumed = []
        status, staged, found = {}, [], []
        try:
            self.board = read_board(self.seat)
        except Die as error:
            status["board.json"] = str(error)
        for name, path, source in self.sources():
            try:
                cursor, events = source(self.cursor(path))
            except SOURCE_ERRORS as error:
                status[name] = f"{type(error).__name__}: {error}"[:300]
                continue
            status[name] = None
            staged.append((path, cursor))
            found += events
        changed = bool(self.resumed)
        for path, cursor in staged:
            holder = self.cursors
            for key in path[:-1]:
                holder = holder.setdefault(key, {})
            changed |= path == ("lanes",) and holder.get(path[-1]) != cursor
            holder[path[-1]] = cursor
        known = {e["id"] for e in self.state["pending"]} | {e["id"] for b in self.state["batches"].values() for e in b["events"]}
        fresh = [e for e in found if e["id"] not in known]
        self.state["pending"] += fresh
        self.save()
        if fresh or changed:
            from .board import draw
            try:
                draw(self.seat)
            except Die as error:
                status["board"] = str(error)
        self.heartbeat(status)
        return bool(fresh or changed)

    def heartbeat(self, status: dict | None, polling: bool = False) -> None:
        """Write heartbeat.json: time, per-source result (failing_since kept across polls), oldest unacked event.
        `polling` marks a poll in progress; it is written before any source work so a slow source never
        leaves the heartbeat stale."""
        path = self.seat / "state" / "run" / "heartbeat.json"
        previous = (read_json(path) or {}).get("sources", {})
        sources = dict(previous)
        if status:
            sources = {}
            for name, error in status.items():
                old = previous.get(name, {})
                sources[name] = {"ok": error is None, "error": error,
                                 "failing_since": None if error is None else (old.get("failing_since") or iso())}
        unacked = [e["at"] for e in self.state["pending"]] + [e["at"] for b in self.state["batches"].values() for e in b["events"]]
        write_json(path, {"at": iso(), "pid": os.getpid(), "polling_since": iso() if polling else None, "sources": sources,
                          "oldest_unacked": min(unacked) if unacked else None, "pending": len(self.state["pending"])})

    def ready(self, window: float) -> bool:
        pending = self.state["pending"]
        return bool(pending) and (any(e["urgent"] for e in pending) or max(age(e["at"]) or 0 for e in pending) >= window)

    def deliver(self) -> str:
        batch_id = f"b-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{digest(*[e['id'] for e in self.state['pending']])[:4]}"
        self.state["batches"][batch_id] = {"at": iso(), "events": self.state["pending"]}
        self.state["pending"] = []
        self.state["last_wake"] = iso()
        self.save()
        return batch_id

    def print_batch(self, batch_id: str) -> None:
        print(f"batch {batch_id}")
        for e in self.state["batches"][batch_id]["events"]:
            print(f"{e['id']}\t{e['kind']}\t{e['text']}")


def wait(seat: Path, ack: str | None = None) -> int:
    interval = float(os.environ.get("TSTACK_WAIT_POLL", 60))
    window = float(os.environ.get("TSTACK_WAIT_BATCH", 180))
    lock = seat / "state" / "run" / "wait" / "lock"
    with flock(lock, blocking=False) as held:
        if not held:
            raise Die(f"another tstack wait is running for {seat}", 3)
        waiter = Waiter(seat)
        if ack:
            if waiter.state["batches"].pop(ack, None) is None:
                say(f"tstack wait: no unacked batch {ack}")
            waiter.save()
        while True:
            if waiter.state["batches"]:
                oldest = min(waiter.state["batches"], key=lambda b: waiter.state["batches"][b]["at"])
                waiter.heartbeat(None)
                waiter.print_batch(oldest)
                return 0
            waiter.poll()
            if waiter.ready(window):
                waiter.print_batch(waiter.deliver())
                return 0
            time.sleep(interval)

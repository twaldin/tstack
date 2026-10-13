"""tstack wait: poll a seat's sources, resume parked lanes on PR events, wake the seat with batches.

State lives in one file, state/run/wait/state.json, replaced atomically: cursors per source, pending
events, and delivered batches not yet acked. A batch stays until `--ack`, so a seat that dies before
handling one gets it again from its next `tstack wait`.
"""

from __future__ import annotations

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

    def save(self) -> None:
        write_json(self.path, self.state)

    # sources: each returns new events and advances its cursor

    def lanes(self) -> list[dict]:
        seen = self.cursors.setdefault("lanes", {})
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
        return events

    def prs(self) -> list[dict]:
        """PR events for waiting lanes resume the lane directly, one resume per lane per poll."""
        cursors = self.cursors.setdefault("prs", {})
        for lane in lanes.lanes(self.seat):
            record = lane.load()
            if record["status"] != "waiting" or not record.get("pr"):
                continue
            snap = prwatch.snapshot(record["pr"])
            old = (cursors.get(lane.id) or {}).get("fp")
            happened = prwatch.actionable(old, snap)
            if not happened:
                cursors[lane.id] = {"fp": snap}
                continue
            text = f"PR {record['pr']} at {snap['head'][:12]}: {'; '.join(happened)}."
            red = snap["ci"] == "red" and snap["failing"] and any(h.startswith("checks-red") for h in happened)
            try:
                lanes.resume(lane, text, "check-failed" if red else "pr", snap["failing"] if red else [])
                self.resumed.append(lane.id)
            except Die as error:
                if error.code == lanes.EXIT_BUSY:
                    continue
                if error.code not in (lanes.EXIT_REFUSED, lanes.EXIT_STUCK):
                    raise
            cursors[lane.id] = {"fp": snap}
            self.save()
        return []

    def gates(self) -> list[dict]:
        gates = read_gates(self.seat)
        board = self.board
        if board:
            for gate in gates:
                if gate.get("Status") == "open" and gate.get("Ask"):
                    answer = easl.ask_answer({**board, "board": gate["Ask"].split("@")[-1]}, gate["Ask"].split("@")[0])
                    if answer:
                        def resolve(rows, gate_id=gate["id"], text=answer):
                            row = next(r for r in rows if r["id"] == gate_id)
                            row.update(Status="resolved", Answer=text)
                        gates = update_gates(self.seat, resolve)
        cursor = self.cursors.get("gates")
        resolved = {g["id"]: g.get("Answer", "") for g in gates if g.get("Status") == "resolved"}
        if cursor is None:
            self.cursors["gates"] = {"resolved": sorted(resolved), "due": []}
            return []
        events = []
        for gate_id, answer in resolved.items():
            if gate_id not in cursor["resolved"]:
                events.append(event("gate-answered", f"gate {gate_id} answered: {answer}", True, gate_id + answer))
        for gate in gates:
            overdue = gate.get("Status") == "open" and gate.get("Due") and parse_iso(gate["Due"]) <= dt.datetime.now(dt.timezone.utc)
            if overdue and gate["id"] not in cursor["due"]:
                cursor["due"].append(gate["id"])
                events.append(event("gate-due", f"gate {gate['id']} is past due with no answer; default: {gate['Default']}",
                                    True, gate["id"] + "due"))
        cursor["resolved"] = sorted(resolved)
        return events

    def intake(self) -> list[dict]:
        seen = set(self.cursors.get("intake", []))
        items = [i for i in read_intake(self.seat) if not i["done"]]
        events = [event("intake", f"{i['source']}: {i['text']}", i["source"].lower() in URGENT_SOURCES, i["line"])
                  for i in items if digest(i["line"]) not in seen]
        self.cursors["intake"] = sorted(digest(i["line"]) for i in items)
        return events

    def schedule(self) -> list[dict]:
        fired = self.cursors.setdefault("schedule", {})
        local_now = dt.datetime.now().astimezone()
        events = []
        for entry in read_schedule(self.seat):
            due = last_occurrence(entry, local_now)
            if entry["name"] in fired and due > parse_iso(fired[entry["name"]]):
                events.append(event("schedule", f"{entry['name']} is due ({entry['when']})", False, entry["name"] + due.isoformat()))
            if entry["name"] not in fired or due > parse_iso(fired[entry["name"]]):
                fired[entry["name"]] = local_now.isoformat(timespec="seconds")
        return events

    def wait_d(self, script: Path) -> list[dict]:
        result = subprocess.run([str(script)], cwd=self.seat, capture_output=True, text=True, timeout=60,
                                env={**os.environ, "TSTACK_SEAT": str(self.seat)})
        if result.returncode:
            raise Die(f"exit {result.returncode}: {result.stderr.strip()[-200:]}")
        seen = self.cursors.setdefault("wait.d", {}).setdefault(script.name, [])
        events = []
        for line in result.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3 or not parts[0]:
                raise Die(f"bad line {line[:80]!r}: want id<TAB>kind<TAB>text")
            if parts[0] in seen:
                continue
            seen.append(parts[0])
            events.append(event(parts[1], parts[2], parts[1] in URGENT_SOURCES, f"{script.name}:{parts[0]}"))
        del seen[:-500]
        return events

    # polling and delivery

    def poll(self) -> bool:
        """One pass over every source; returns whether anything changed."""
        self.resumed: list[str] = []
        before = dict(self.cursors.get("lanes", {}))
        sources = [("lanes", self.lanes), ("github", self.prs), ("gates", self.gates), ("intake", self.intake),
                   ("schedule", self.schedule)]
        scripts = self.seat / "wait.d"
        if scripts.is_dir():
            sources += [(f"wait.d/{p.name}", lambda p=p: self.wait_d(p)) for p in sorted(scripts.iterdir())
                        if p.is_file() and os.access(p, os.X_OK)]
        status, found = {}, []
        try:
            self.board = read_board(self.seat)
        except Die as error:
            status["board.json"] = str(error)
        for name, source in sources:
            try:
                found += source()
                status.setdefault(name, None)
            except (Die, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
                status[name] = f"{type(error).__name__}: {error}"[:300]
        known = {e["id"] for e in self.state["pending"]} | {e["id"] for b in self.state["batches"].values() for e in b["events"]}
        fresh = [e for e in found if e["id"] not in known]
        self.state["pending"] += fresh
        self.save()
        changed = bool(fresh or self.resumed or before != self.cursors.get("lanes", {}))
        if changed:
            from .board import draw
            try:
                draw(self.seat)
            except Die as error:
                status["board"] = str(error)
        self.heartbeat(status)
        return changed

    def heartbeat(self, status: dict) -> None:
        """Write heartbeat.json: time, per-source result (failing_since kept across polls), oldest unacked event."""
        path = self.seat / "state" / "run" / "heartbeat.json"
        previous = (read_json(path) or {}).get("sources", {})
        sources = dict(previous) if not status else {}
        for name, error in status.items():
            old = previous.get(name, {})
            sources[name] = {"ok": error is None, "error": error,
                             "failing_since": None if error is None else (old.get("failing_since") or iso())}
        unacked = [e["at"] for e in self.state["pending"]] + [e["at"] for b in self.state["batches"].values() for e in b["events"]]
        write_json(path, {"at": iso(), "pid": os.getpid(), "sources": sources,
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
                waiter.heartbeat({})
                waiter.print_batch(oldest)
                return 0
            waiter.poll()
            if waiter.ready(window):
                waiter.print_batch(waiter.deliver())
                return 0
            time.sleep(interval)

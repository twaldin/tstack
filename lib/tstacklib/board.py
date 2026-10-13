"""tstack board: redraw a seat's keyed status tiles from its state files."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from . import easl, lanes
from .seat import read_board, read_gates, read_tasks, stop_reason
from .util import Die, all_seats, instance_home, iso, machine, money, read_json, seat_name, short_age


def cell(text) -> str:
    return str(text or "-").replace("|", "/").replace("\n", " ")


def status_markdown(seat: Path) -> str:
    out = [f"## {seat_name(seat)}", "", f"Drawn {iso()} on {machine()} by `tstack board`."]
    reason = stop_reason(seat)
    if reason:
        out += ["", f"**STOP: {reason}**"]
    gates = [g for g in read_gates(seat) if g.get("Status") == "open"]
    if gates:
        out += ["", "### Open gates", ""]
        out += [f"- **{g['id']}**: {g['Question']} (default {g['Default']}" + (f", due {g['Due']}" if g.get("Due") else "")
                + (", irreversible" if g.get("Irreversible") == "yes" else "") + ")" for g in gates]
    tasks = read_tasks(seat)
    if tasks:
        out += ["", "### Tasks", "", "| id | effort | title | state | lane | PR |", "|---|---|---|---|---|---|"]
        out += [f"| {cell(t['id'])} | {cell(t['effort'])} | {cell(t['title'])} | {cell(t['state'])} | {cell(t['lane'])} | {cell(t['pr'])} |"
                for t in tasks]
    rows = [(lane, lanes.reconcile(lane)) for lane in lanes.lanes(seat)]
    rows = [(lane, rec) for lane, rec in rows if lanes.visible(rec, False)]
    if rows:
        out += ["", "### Lanes", "", "| lane | status | PR | cost | age |", "|---|---|---|---|---|"]
        for lane, rec in rows:
            status = rec["status"] + (" (held)" if rec.get("held") else "") + (f": {rec['reason']}" if rec.get("reason") else "")
            out.append(f"| {lane.id} | {cell(status)} | {cell(rec.get('pr'))} | {money(rec.get('cost_usd'))} | {short_age(rec.get('created'))} |")
    wake = (read_json(seat / "state" / "run" / "wait" / "state.json") or {}).get("last_wake")
    beat = (read_json(seat / "state" / "run" / "heartbeat.json") or {}).get("at")
    out += ["", f"Last wake: {short_age(wake) + ' ago' if wake else 'never'}. Last wait poll: {short_age(beat) + ' ago' if beat else 'never'}."]
    return "\n".join(out) + "\n"


def project_markdown(seat: Path) -> str:
    states = Counter(t["state"] for t in read_tasks(seat))
    records = [lanes.reconcile(lane) for lane in lanes.lanes(seat)]
    live = Counter(r["status"] for r in records if r["status"] in ("running", "waiting"))
    gates = sum(1 for g in read_gates(seat) if g.get("Status") == "open")
    cost = sum(r.get("cost_usd") or 0 for r in records)
    beat = (read_json(seat / "state" / "run" / "heartbeat.json") or {}).get("at")
    task_text = ", ".join(f"{n} {s}" for s, n in sorted(states.items())) or "none"
    lane_text = ", ".join(f"{n} {s}" for s, n in sorted(live.items())) or "none active"
    return (f"## {seat_name(seat)}\n\nTasks: {task_text}.\nLanes: {lane_text}; {money(cost)} spent over {len(records)} lanes.\n"
            f"Open gates: {gates}.\nCoordinator last polled: {short_age(beat) + ' ago' if beat else 'never'}.\n")


def draw(seat: Path) -> list[str]:
    board = read_board(seat)
    if not board:
        raise Die(f"{seat} has no state/board.json")
    keys = [f"{seat_name(seat)}/status"]
    easl.upsert_note(board, keys[0], status_markdown(seat))
    if seat.resolve() == (instance_home() / "assistant").resolve():
        for project in all_seats():
            if project.resolve() != seat.resolve():
                keys.append(f"projects/{seat_name(project)}")
                easl.upsert_note(board, keys[-1], project_markdown(project))
    return keys

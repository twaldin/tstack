"""The easl CLI: easld on Linux, the app on macOS; boards on another host go over ssh."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

from .util import HOME, IS_MAC, Die, machine

MAC_CLI = "/Applications/easl.app/Contents/Resources/bin/easl"


class EaslError(Die):
    pass


class EaslConflict(EaslError):
    """easl refused because the agent works, the user types, or a key is held."""


def _local_cli() -> tuple[str, dict]:
    env = dict(os.environ)
    if not env.get("EASL_SOCKET"):
        env["EASL_SOCKET"] = str(HOME / "Library/Application Support/Easl/easl.sock" if IS_MAC
                                 else HOME / ".local/state/easl/easl.sock")
    cli = shutil.which("easl") or (MAC_CLI if IS_MAC and Path(MAC_CLI).exists() else None)
    if not cli:
        raise EaslError("easl: not found on PATH")
    return cli, env


def call(*args: str, host: str | None = None, timeout: float = 60) -> dict:
    """Run `easl <args>` on this host or `host`, parse its JSON output."""
    if host and host != machine():
        argv = ["ssh", host, "--", " ".join(shlex.quote(a) for a in ("easl", *args))]
        env = None
    else:
        cli, env = _local_cli()
        argv = [cli, *args]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise EaslError(f"easl {args[0]}: timed out")
    detail = (result.stderr or result.stdout).strip()[-300:]
    try:
        out = json.loads(result.stdout) if result.stdout.strip() else {}
    except ValueError:
        out = None
    if result.returncode or not isinstance(out, dict) or out.get("error"):
        kind = EaslConflict if "conflict" in detail else EaslError
        raise kind(f"easl {args[0]}: {detail or f'exit {result.returncode}'}")
    return out


def ask(board: dict, question: str, options: list[tuple[str, str]], recommend: str | None) -> str:
    args = ["ask", question, "--board", board["board"]]
    for option_id, label in options:
        args += ["--option", f"{option_id}={label.replace(':', ' -')}"]
    if recommend and any(option_id == recommend for option_id, _ in options):
        args += ["--recommend", recommend]
    return call(*args, host=board["host"])["object"]["id"]


def ask_answer(board: dict, ask_id: str) -> str | None:
    """The answer text for an answered question, else None (open, or closed with no answer)."""
    props = call("ask", "get", ask_id, host=board["host"])["object"].get("props", {})
    if props.get("status") != "answered":
        return None
    answer = props.get("answer") or {}
    labels = {o.get("id"): o.get("label") for o in props.get("options", [])}
    parts = []
    if answer.get("option"):
        parts.append(f"{answer['option']}) {labels.get(answer['option'], '')}".strip())
    if answer.get("note"):
        parts.append(answer["note"])
    return "; ".join(parts) or "answered"


def upsert_note(board: dict, key: str, markdown: str) -> None:
    props = json.dumps({"props": {"markdown": markdown}})
    call("object.upsert", "--board", board["board"], "--key", key, "--type", "note", "--json", props,
         host=board["host"])


def agent(board: dict) -> dict | None:
    """The seat tile's entry in `agent.list`: lifecycle, sessionId, focused, draft, pid."""
    agents = call("agent.list", host=board["host"]).get("agents", [])
    return next((a for a in agents if a.get("tile") == board["tile"]), None)


def restart(board: dict, force: bool = False) -> None:
    """Resume the seat's tile in place. Raises EaslConflict while it works or the operator types, unless
    `force`, which ends whatever runs in the tile."""
    call("agent.restart", "--target", board["tile"], "--mode", "resume", *(["--force"] if force else []),
         host=board["host"], timeout=120)


def prompt(board: dict, text: str) -> None:
    call("agent.prompt", "--target", board["tile"], "--text", text, "--from", "tstack", host=board["host"])

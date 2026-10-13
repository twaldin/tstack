"""Paths, atomic writes, time and process helpers shared by tstack and lane."""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
IS_MAC = sys.platform == "darwin"
ENGINE = Path(__file__).resolve().parents[2]


class Die(Exception):
    """A refusal or usage error: message to stderr, exit with `code`."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def engine_home() -> Path:
    return Path(os.environ.get("TSTACK_HOME") or ENGINE)


def instance_home() -> Path:
    return Path(os.environ.get("TSTACK_INSTANCE") or HOME / "agents")


def worktrees_root() -> Path:
    return Path(os.environ.get("TSTACK_WORKTREES") or HOME / "worktrees")


def state_root() -> Path:
    return HOME / ".local" / "state" / "tstack"


def machine() -> str:
    """Short machine name: $TSTACK_MACHINE, else the instance machine dir matching hostname -s."""
    if os.environ.get("TSTACK_MACHINE"):
        return os.environ["TSTACK_MACHINE"]
    host = socket.gethostname().split(".")[0]
    machines = instance_home() / "machines"
    names = sorted(p.name for p in machines.iterdir() if p.is_dir()) if machines.is_dir() else []
    if host in names:
        return host
    for name in names:
        if host.endswith("-" + name):
            return name
    return host


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(when: dt.datetime | None = None) -> str:
    return (when or now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> dt.datetime:
    text = text.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    value = dt.datetime.fromisoformat(text)
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value


def age(text: str | None) -> float | None:
    """Seconds since an ISO timestamp, or None when missing or unparseable."""
    if not text:
        return None
    try:
        return (now() - parse_iso(text)).total_seconds()
    except ValueError:
        return None


def short_age(text: str | None) -> str:
    seconds = age(text)
    if seconds is None:
        return "?"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit}"
    return f"{int(seconds)}s"


def money(cost) -> str:
    cost = float(cost or 0)
    return f"${cost:.2f}" if cost >= 0.1 or cost == 0 else f"${cost:.4f}"


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


def write_json(path: Path, value) -> None:
    write_atomic(path, json.dumps(value, indent=1, sort_keys=False) + "\n")


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


@contextlib.contextmanager
def flock(path: Path, blocking: bool = True, timeout: float | None = None):
    """Exclusive fcntl lock on `path`; yields True when held, False when busy (non-blocking)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except BlockingIOError:
                if not blocking or (deadline is not None and time.monotonic() >= deadline):
                    held = False
                    break
                time.sleep(0.1)
        yield held
    finally:
        handle.close()


def lock_is_held(path: Path) -> bool:
    if not path.exists():
        return False
    with flock(path, blocking=False) as held:
        return not held


@contextlib.contextmanager
def short_lock(path: Path):
    """A lock held for one read-modify-write. Waits at most $TSTACK_LOCK_WAIT seconds (default 30), then
    raises, so a stuck holder shows up as an error instead of a hang."""
    wait = float(os.environ.get("TSTACK_LOCK_WAIT", 30))
    with flock(path, timeout=wait) as held:
        if not held:
            raise Die(f"{path} is busy (held for over {wait:g}s)", 3)
        yield


def run(argv: list[str], cwd: Path | None = None, timeout: float = 120, env: dict | None = None,
        check: bool = True, input: str | None = None) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                                env=env, input=input)
    except FileNotFoundError:
        raise Die(f"{argv[0]}: not found on PATH")
    except subprocess.TimeoutExpired:
        raise Die(f"{' '.join(argv[:3])}: timed out after {timeout:g}s")
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-400:]
        raise Die(f"{' '.join(argv[:3])} failed (exit {result.returncode}): {detail}")
    return result


def git(repo: Path, *args: str, check: bool = True, timeout: float = 120) -> str:
    return run(["git", "-C", str(repo), *args], check=check, timeout=timeout).stdout.strip()


def find_seat(explicit: str | None) -> Path:
    """--seat, else $TSTACK_SEAT, else the nearest ancestor of cwd holding state/."""
    if explicit:
        seat = Path(explicit).expanduser().resolve()
    elif os.environ.get("TSTACK_SEAT"):
        seat = Path(os.environ["TSTACK_SEAT"]).expanduser().resolve()
    else:
        here = Path.cwd().resolve()
        seat = next((p for p in (here, *here.parents) if (p / "state").is_dir()), None)
        if seat is None:
            raise Die("no seat: pass --seat DIR, set TSTACK_SEAT, or run inside a seat dir")
    if not (seat / "state").is_dir():
        raise Die(f"{seat} has no state/ dir")
    return seat


def seat_name(seat: Path) -> str:
    """`web` for projects/web, `acme/web` for an area seat, else the dir name."""
    projects = (instance_home() / "projects").resolve()
    try:
        return str(seat.resolve().relative_to(projects))
    except ValueError:
        return seat.name


def all_seats() -> list[Path]:
    """Every seat in the instance: assistant/ and each projects/** dir holding state/."""
    root = instance_home()
    seats = [root / "assistant"] if (root / "assistant" / "state").is_dir() else []
    projects = root / "projects"
    if projects.is_dir():
        for state in sorted(projects.glob("**/state")):
            if state.is_dir() and "state" not in state.parent.relative_to(projects).parts:
                seats.append(state.parent)
    return seats


def say(message: str) -> None:
    print(message, file=sys.stderr)

"""Test harness: a temp HOME with a seat, and fake omp, gh, easl, bun and systemd on PATH that log each call."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
PRELUDE = r'''#!/usr/bin/env python3
import json, os, subprocess, sys, time, uuid
from pathlib import Path
stubs, args = Path(__file__).resolve().parents[1], sys.argv[1:]
with open(stubs / f"{Path(__file__).name}.log", "a") as log:
    log.write(json.dumps({"args": args, "cwd": os.getcwd(), "lane": os.environ.get("TSTACK_LANE")}) + "\n")
'''
FAKES = {
    # A session event, then one assistant message after omp.json's sleep (60 s when the prompt says SLOW).
    # Like omp, it takes a moment to shut down on SIGTERM; omp.pid holds its pid.
    "omp": PRELUDE + r'''
import signal
signal.signal(signal.SIGTERM, lambda *_: (time.sleep(1), sys.exit(143)))
(stubs / "omp.pid").write_text(str(os.getpid()))
cfg = json.loads((stubs / "omp.json").read_text())
print(json.dumps({"type": "session", "id": args[args.index("--resume") + 1] if "--resume" in args else str(uuid.uuid4())}), flush=True)
time.sleep(60 if "SLOW" in args[-1] else cfg.get("sleep", 0))
content = [{"type": "text", "text": cfg["answer"]}] if cfg.get("answer") else []
message = {"role": "assistant", "content": content, "usage": {"cost": {"total": cfg.get("cost", 0.01)}}}
print(json.dumps({"type": "message_end", "message": message}), flush=True)
sys.exit(cfg.get("exit", 0))
''',
    # pr list and pr view print their JSON file; pr merge fails with merge.err, else merges unless --auto.
    "gh": PRELUDE + r'''
if args[:2] == ["pr", "list"]:
    print((stubs / "pr-list.json").read_text() if (stubs / "pr-list.json").exists() else "[]")
elif args[:2] == ["pr", "view"]:
    print((stubs / "pr-view.json").read_text())
elif args[:2] == ["pr", "merge"] and (stubs / "merge.err").exists():
    sys.exit((stubs / "merge.err").read_text())
elif args[:2] == ["pr", "merge"] and "--auto" not in args:
    view = json.loads((stubs / "pr-view.json").read_text())
    (stubs / "pr-view.json").write_text(json.dumps({**view, "state": "MERGED", "mergeCommit": {"oid": "f" * 40}}))
''',
    "easl": PRELUDE + r'''
print((stubs / "asks.json").read_text() if args[:2] == ["ask", "list"] else json.dumps({"object": {"id": "obj_1"}}))
''',
    "bun": PRELUDE,
    # Starts the command detached with its --working-directory and --setenv, keeping its pid for systemctl.
    # Real systemd-run expands $VAR in the command unless told not to.
    "systemd-run": PRELUDE + r'''
if "--expand-environment=no" not in args:
    sys.exit("fake systemd-run: would expand $ in the command line")
env, cwd, unit, i = dict(os.environ), None, None, 0
while args[i].startswith("-"):
    key, _, value = args[i].partition("=")
    env.update([value.split("=", 1)] if key == "--setenv" else [])
    cwd, unit = (value if key == "--working-directory" else cwd), (value if key == "--unit" else unit)
    i += 2 if args[i] == "-p" else 1
proc = subprocess.Popen(args[i:], cwd=cwd, env=env, start_new_session=True, stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
(stubs / f"{unit}.pid").write_text(str(proc.pid))
''',
    "systemctl": PRELUDE + r'''
if args[1:3] == ["show", "-p"]:
    print((stubs / f"{args[-1]}.pid").read_text())
elif args[1] == "stop":
    os.killpg(int((stubs / f"{args[2]}.pid").read_text()), 15)
''',
}


def git(cwd, *args) -> str:
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="tstack-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home, self.stubs = self.root / "home", self.root / "stubs"
        self.seat = self.home / "agents" / "assistant"
        (self.seat / "state").mkdir(parents=True)
        (self.stubs / "bin").mkdir(parents=True)
        for name, text in FAKES.items():
            (self.stubs / "bin" / name).write_text(text)
            (self.stubs / "bin" / name).chmod(0o755)
        self.omp(answer="done", cost=0.02)
        self.env = {"HOME": str(self.home), "PATH": f"{self.stubs / 'bin'}:{os.environ['PATH']}", "LANG": "C.UTF-8",
                    "TSTACK_LAUNCHER": "process", "TSTACK_MACHINE": "box", "PYTHONDONTWRITEBYTECODE": "1"}
        self.brief = self.root / "brief.md"
        self.brief.write_text("Reply with the word done.\n")

    def omp(self, **config) -> None:
        (self.stubs / "omp.json").write_text(json.dumps(config))

    def clone(self, origin: Path, dest: Path, files=None) -> Path:
        """A bare `origin` seeded with `files`, cloned to `dest`."""
        seed = self.root / f"seed-{origin.stem}"
        git(self.root, "init", "--quiet", "-b", "main", str(seed))
        for name, text in (files or {"README": "demo\n"}).items():
            (seed / name).parent.mkdir(parents=True, exist_ok=True)
            (seed / name).write_text(text)
            (seed / name).chmod(0o755 if name.startswith("bin/") else 0o644)
        git(seed, "add", "-A")
        git(seed, "commit", "--quiet", "-m", "seed")
        git(self.root, "clone", "--quiet", "--bare", str(seed), str(origin))
        git(self.root, "clone", "--quiet", str(origin), str(dest))
        return dest

    def cli(self, tool, *args, check=True) -> subprocess.CompletedProcess:
        result = subprocess.run([sys.executable, str(CODE / "bin" / tool), *args], env=self.env, cwd=self.seat,
                                capture_output=True, text=True, timeout=60)
        if check and result.returncode:
            self.fail(f"{tool} {' '.join(args)}: exit {result.returncode}\n{result.stdout}{result.stderr}")
        return result

    def record(self, lane_id: str) -> dict:
        return json.loads((self.seat / "state" / "lanes" / lane_id / "lane.json").read_text())

    def log(self, fake: str) -> list:
        """Each call of `fake`: {"args", "cwd", "lane"}."""
        path = self.stubs / f"{fake}.log"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def until(self, predicate) -> bool:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.1)
        return False

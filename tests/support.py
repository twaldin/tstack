"""Test harness: a temp HOME with a seat and a project clone, and fake omp, gh, easl, bun and systemd on PATH."""

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

FAKES = {
    "omp": r'''#!/usr/bin/env python3
# A fake `omp -p --mode json`: a session event, then one assistant message after stubs/omp.json's sleep
# (60 s when the prompt says SLOW), with its answer and cost; exits with its exit code.
import json, os, sys, time, uuid
from pathlib import Path
stubs, args = Path(__file__).resolve().parents[1], sys.argv[1:]
cfg = json.loads((stubs / "omp.json").read_text())
with open(stubs / "omp.log", "a") as log:
    log.write(json.dumps({"args": args, "cwd": os.getcwd(), "lane": os.environ.get("TSTACK_LANE")}) + "\n")
sid = args[args.index("--resume") + 1] if "--resume" in args else str(uuid.uuid4())
print(json.dumps({"type": "session", "id": sid}), flush=True)
time.sleep(60 if "SLOW" in args[-1] else cfg.get("sleep", 0))
content = [{"type": "text", "text": cfg["answer"]}] if cfg.get("answer") else []
message = {"role": "assistant", "content": content, "usage": {"cost": {"total": cfg.get("cost", 0.01)}}}
print(json.dumps({"type": "message_end", "message": message}), flush=True)
sys.exit(cfg.get("exit", 0))
''',
    "gh": r'''#!/usr/bin/env python3
# A fake gh: `pr list` prints stubs/pr-list.json, `pr view` stubs/pr-view.json; `pr merge` fails with
# stubs/merge.err when present, else marks the PR merged unless --auto. Every call is logged.
import json, os, sys
from pathlib import Path
stubs, args = Path(__file__).resolve().parents[1], sys.argv[1:]
with open(stubs / "gh.log", "a") as log:
    log.write(json.dumps(args) + "\n")
if args[:2] == ["pr", "list"]:
    print((stubs / "pr-list.json").read_text() if (stubs / "pr-list.json").exists() else "[]")
elif args[:2] == ["pr", "view"]:
    print((stubs / "pr-view.json").read_text())
elif args[:2] == ["pr", "merge"] and (stubs / "merge.err").exists():
    sys.exit((stubs / "merge.err").read_text())
elif args[:2] == ["pr", "merge"] and "--auto" not in args:
    view = json.loads((stubs / "pr-view.json").read_text())
    view.update(state="MERGED", mergeCommit={"oid": "f" * 40})
    (stubs / "pr-view.json").write_text(json.dumps(view))
''',
    "easl": r'''#!/usr/bin/env python3
# A fake easl: logs argv; `ask list` prints stubs/asks.json.
import json, os, sys
from pathlib import Path
stubs, args = Path(__file__).resolve().parents[1], sys.argv[1:]
with open(stubs / "easl.log", "a") as log:
    log.write(json.dumps(args) + "\n")
print((stubs / "asks.json").read_text() if args[:2] == ["ask", "list"] else json.dumps({"object": {"id": "obj_1"}}))
''',
    "bun": '#!/bin/sh\necho "$PWD $*" >> "$(dirname "$0")/../bun.log"\n',
    "systemd-run": r'''#!/usr/bin/env python3
# A fake systemd-run --user: starts the command detached with its --working-directory and --setenv,
# keeps its pid in stubs/<unit>.pid for the fake systemctl, logs argv.
import json, os, subprocess, sys
from pathlib import Path
stubs, args = Path(__file__).resolve().parents[1], sys.argv[1:]
with open(stubs / "systemd-run.log", "a") as log:
    log.write(json.dumps(args) + "\n")
env, cwd, unit, i = dict(os.environ), None, None, 0
while args[i].startswith("-"):
    key, _, value = args[i].partition("=")
    env.update([value.split("=", 1)] if key == "--setenv" else [])
    cwd = value if key == "--working-directory" else cwd
    unit = value if key == "--unit" else unit
    i += 2 if args[i] == "-p" else 1
proc = subprocess.Popen(args[i:], cwd=cwd, env=env, start_new_session=True, stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
(stubs / f"{unit}.pid").write_text(str(proc.pid))
''',
    "systemctl": r'''#!/bin/sh
# A fake systemctl: MainPID from the fake systemd-run, `stop` kills the unit's process group.
STUBS=$(dirname "$0")/..
echo "$*" >> "$STUBS/systemctl.log"
case "$*" in
  "--user show -p MainPID --value "*) cat "$STUBS/$6.pid" ;;
  "--user stop "*) kill -TERM -- "-$(cat "$STUBS/$3.pid")" ;;
esac
''',
}


def git(cwd, *args) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


class Sandbox(unittest.TestCase):
    """Each test gets its own HOME, seat, project clone (`acme/demo`, origin a local bare repo) and fakes."""

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
        self.env = {
            "HOME": str(self.home), "PATH": f"{self.stubs / 'bin'}:{os.environ['PATH']}",
            "TSTACK_LAUNCHER": "process", "TSTACK_MACHINE": "box", "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        }
        for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
            os.environ.setdefault(key, self.env[key])
        self.brief = self.root / "brief.md"
        self.brief.write_text("Reply with the word done.\n")

    def omp(self, **config) -> None:
        (self.stubs / "omp.json").write_text(json.dumps(config))

    def clone(self, origin: Path, dest: Path, files=None) -> Path:
        """A bare `origin` seeded with `files`, cloned to `dest`."""
        seed = self.root / f"seed-{origin.stem}"
        subprocess.run(["git", "init", "--quiet", "-b", "main", str(seed)], check=True)
        for name, text in (files or {"README": "demo\n"}).items():
            (seed / name).parent.mkdir(parents=True, exist_ok=True)
            (seed / name).write_text(text)
            (seed / name).chmod(0o755 if name.startswith("bin/") else 0o644)
        git(seed, "add", "-A")
        git(seed, "commit", "--quiet", "-m", "seed")
        subprocess.run(["git", "clone", "--quiet", "--bare", str(seed), str(origin)], check=True)
        subprocess.run(["git", "clone", "--quiet", str(origin), str(dest)], check=True)
        return dest

    def project(self) -> Path:
        return self.clone(self.root / "demo.git", self.home / "worktrees" / "demo" / "main")

    def cli(self, tool, *args, check=True, timeout=60) -> subprocess.CompletedProcess:
        result = subprocess.run([sys.executable, str(CODE / "bin" / tool), *args], env=self.env, cwd=self.seat,
                                capture_output=True, text=True, timeout=timeout)
        if check and result.returncode:
            self.fail(f"{tool} {' '.join(args)}: exit {result.returncode}\n{result.stdout}{result.stderr}")
        return result

    def record(self, lane_id: str) -> dict:
        return json.loads((self.seat / "state" / "lanes" / lane_id / "lane.json").read_text())

    def log(self, name: str) -> list:
        path = self.stubs / name
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def until(self, predicate) -> bool:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.1)
        return False

    def gone(self, pid: int) -> bool:
        return self.until(lambda: subprocess.run(["ps", "-p", str(pid)], capture_output=True).returncode != 0)

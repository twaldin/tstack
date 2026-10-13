"""Test harness: a temp HOME with an engine fixture, an instance, a project clone and stub gh/easl/omp/systemctl."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
LIB = CODE / "lib"
sys.path.insert(0, str(LIB))
# setup verifies omp config edits with Bun's YAML parser; the sandbox HOME has no ~/.bun
BUN = shutil.which("bun") or next((str(p) for p in [Path.home() / ".bun" / "bin" / "bun"] if p.exists()), None)
BUN_DIR = str(Path(BUN).parent) if BUN else ""

STUB_OMP = r'''#!/usr/bin/env python3
"""Fake omp -p --mode json: a session event, one assistant message with a cost, an optional lane report."""
import json, os, subprocess, sys, time, uuid
from pathlib import Path
cfg = json.loads((Path(os.environ["STUBS"]) / "omp.json").read_text())
args = sys.argv[1:]
with open(Path(os.environ["STUBS"]) / "omp.log", "a") as log:
    log.write(json.dumps({"args": args, "cwd": os.getcwd(), "lane": os.environ.get("TSTACK_LANE")}) + "\n")
if "--resume" in args:
    sid = Path(args[args.index("--resume") + 1]).stem.split("_", 1)[1]
else:
    sid = str(uuid.uuid4())
sessions = Path.home() / ".omp" / "agent" / "sessions" / "-fake"
sessions.mkdir(parents=True, exist_ok=True)
(sessions / f"2026-10-13T00-00-00-000Z_{sid}.jsonl").touch()
print(json.dumps({"type": "session", "version": 3, "id": sid, "cwd": os.getcwd()}), flush=True)
time.sleep(cfg.get("sleep", 0))
usage = {"cost": {"total": cfg.get("cost", 0.01)}}
print(json.dumps({"type": "message_end", "message": {"role": "assistant", "usage": usage, "content": []}}), flush=True)
report = cfg.get("report")
if report:
    cmd = [sys.executable, os.environ["LANE_BIN"], "report", "--status", report, "--summary", "stub run"]
    if report == "waiting":
        cmd += ["--waiting-on", "ci", "--pr", cfg["pr"]]
    subprocess.run(cmd, check=True)
sys.exit(cfg.get("exit", 0))
'''

STUB_GH = r'''#!/usr/bin/env python3
"""Fake gh: `pr view URL --json F` returns stubs/gh/pr.json; graphql threads return stubs/gh/threads.json; merge is
logged. `api PATH` prints stubs/gh/api/<PATH with / as _>.json, or fails with the text of a matching .err file,
or with HTTP 404; `api user` defaults to the login shared-login."""
import json, os, sys
from pathlib import Path
stubs = Path(os.environ["STUBS"])
args = sys.argv[1:]
with open(stubs / "gh.log", "a") as log:
    log.write(json.dumps(args) + "\n")
if args[:2] == ["pr", "view"]:
    print((stubs / "gh" / "pr.json").read_text())
elif args[:2] == ["api", "graphql"]:
    nodes = json.loads((stubs / "gh" / "threads.json").read_text()) if (stubs / "gh" / "threads.json").exists() else []
    print(json.dumps([{"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": nodes}}}}}]))
elif args[0] == "api":
    stem = str(stubs / "gh" / "api" / args[1].replace("/", "_"))
    if os.path.exists(stem + ".json"):
        print(Path(stem + ".json").read_text())
    elif os.path.exists(stem + ".err"):
        sys.exit("gh: " + Path(stem + ".err").read_text().strip())
    elif args[1] == "user":
        print(json.dumps({"login": "shared-login"}))
    else:
        sys.exit("gh: Not Found (HTTP 404)")
elif args[:2] == ["pr", "merge"]:
    print("merged")
else:
    sys.exit(f"stub gh: unexpected {args}")
'''

STUB_EASL = r'''#!/usr/bin/env python3
"""Fake easl: logs argv; asks live in stubs/asks.json. agent.restart refuses (conflict) when stubs/easl-busy
exists and fails when stubs/easl-fail exists; agent.prompt fails when stubs/easl-prompt-fail exists."""
import json, os, sys
from pathlib import Path
stubs = Path(os.environ["STUBS"])
args = sys.argv[1:]
with open(stubs / "easl.log", "a") as log:
    log.write(json.dumps(args) + "\n")
asks_path = stubs / "asks.json"
asks = json.loads(asks_path.read_text()) if asks_path.exists() else {}
def out(value):
    print(json.dumps(value))
if args[0] == "ask" and args[1] not in ("get", "cancel", "list"):
    ask_id = f"obj_ask{len(asks) + 1}"
    options = [{"id": o.split("=", 1)[0], "label": o.split("=", 1)[1]} for i, o in enumerate(args) if i and args[i - 1] == "--option"]
    asks[ask_id] = {"question": args[1], "options": options, "status": "open"}
    asks_path.write_text(json.dumps(asks))
    out({"object": {"id": ask_id, "type": "question", "props": asks[ask_id]}})
elif args[:2] == ["ask", "get"]:
    out({"object": {"id": args[2], "type": "question", "props": asks[args[2]]}})
elif args[:2] == ["ask", "cancel"]:
    asks[args[2]]["status"] = "cancelled"
    asks_path.write_text(json.dumps(asks))
    out({"object": {"id": args[2]}})
elif args[0] == "agent.restart" and (stubs / "easl-busy").exists():
    print("conflict: the agent is working", file=sys.stderr)
    sys.exit(1)
elif args[0] == "agent.restart" and (stubs / "easl-fail").exists():
    print("not_found: no such tile", file=sys.stderr)
    sys.exit(1)
elif args[0] == "agent.prompt" and (stubs / "easl-prompt-fail").exists():
    print("unavailable: host not connected", file=sys.stderr)
    sys.exit(1)
elif args[0] == "agent.list":
    out({"agents": []})
else:
    out({"object": {"id": "obj_tile"}, "created": True})
'''

STUB_SYSTEMCTL = r'''#!/bin/sh
# Fake systemctl: log; `--user enable --now T` creates the wants link real systemctl creates.
printf '%s\n' "$*" >> "$STUBS/systemctl.log"
if [ "$2" = enable ]; then
  mkdir -p "$HOME/.config/systemd/user/timers.target.wants"
  ln -sf "$HOME/.config/systemd/user/$4" "$HOME/.config/systemd/user/timers.target.wants/$4"
fi
'''

STUB_LOG = r'''#!/bin/sh
printf '%s\n' "$*" >> "$STUBS/$(basename "$0").log"
'''

STUB_TEXT = r'''#!/bin/sh
# Fake bin/text: fails while stubs/text-fail exists; a delivered message is logged to text.log.
if [ -e "$STUBS/text-fail" ]; then echo "sendblue: 503" >&2; exit 1; fi
printf '%s\n' "$*" >> "$STUBS/text.log"
'''


def write_exe(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


class Sandbox(unittest.TestCase):
    """Each test gets its own HOME, engine fixture, instance, project clone and stubs."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="tstack-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / "h"
        self.engine = self.root / "engine"
        self.instance = self.home / "agents"
        self.stubs = self.root / "stubs"
        for name, text in (("roles/lane.md", "# Lane role\n"), ("roles/restart.md", "You were restarted in place.\n"),
                           ("playbooks/build-feature.md", "# Build a feature\n"), ("global.md", "# Global rules\n"),
                           ("skills/research/SKILL.md", "---\nname: research\n---\n"), ("agents/scout.md", "# scout\n"),
                           ("extensions/secret-guard.ts", "export default function () {}\n")):
            (self.engine / name).parent.mkdir(parents=True, exist_ok=True)
            (self.engine / name).write_text(text)
        shutil.copytree(CODE / "systemd", self.engine / "systemd")
        shutil.copytree(CODE / "launchd", self.engine / "launchd")
        write_exe(self.engine / "bin" / "tstack", "#!/bin/sh\n")
        write_exe(self.engine / "bin" / "lane", "#!/bin/sh\n")
        (self.instance / "global.md").parent.mkdir(parents=True)
        (self.instance / "global.md").write_text("# Instance rules\n")
        (self.instance / "machines" / "testbox").mkdir(parents=True)
        write_exe(self.instance / "bin" / "text", STUB_TEXT)
        self.seat = self.instance / "projects" / "demo"
        (self.seat / "state").mkdir(parents=True)
        write_exe(self.stubs / "bin" / "omp", STUB_OMP)
        write_exe(self.stubs / "bin" / "gh", STUB_GH)
        write_exe(self.stubs / "bin" / "easl", STUB_EASL)
        write_exe(self.stubs / "bin" / "systemctl", STUB_SYSTEMCTL)
        write_exe(self.stubs / "bin" / "launchctl", STUB_LOG)
        (self.stubs / "gh").mkdir()
        self.omp_config(report="done")
        self.env = {
            "HOME": str(self.home), "PATH": f"{self.stubs / 'bin'}:{os.environ['PATH']}:{BUN_DIR}", "STUBS": str(self.stubs),
            "TSTACK_HOME": str(self.engine), "TSTACK_INSTANCE": str(self.instance), "TSTACK_MACHINE": "testbox",
            "TSTACK_LAUNCHER": "process", "LANE_BIN": str(CODE / "bin" / "lane"), "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid", "LANG": "C.UTF-8",
        }

    # fixtures

    def omp_config(self, **config) -> None:
        (self.stubs / "omp.json").write_text(json.dumps(config))

    def project_clone(self) -> Path:
        origin = self.root / "origin.git"
        subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(origin)], check=True)
        seed = self.root / "seed"
        subprocess.run(["git", "init", "--quiet", "-b", "main", str(seed)], check=True)
        (seed / "README").write_text("demo\n")
        env = {**os.environ, **{k: v for k, v in self.env.items() if k.startswith("GIT_")}}
        for args in (["add", "README"], ["commit", "--quiet", "-m", "seed"], ["push", "--quiet", str(origin), "main"]):
            subprocess.run(["git", "-C", str(seed), *args], check=True, env=env)
        main = self.home / "worktrees" / "demo" / "main"
        subprocess.run(["git", "clone", "--quiet", str(origin), str(main)], check=True)
        return main

    def brief(self, text: str = "Do the thing.\nTIMEBOX: 1h\n") -> Path:
        path = self.root / "brief.md"
        path.write_text(text)
        return path

    def board(self) -> None:
        (self.seat / "state" / "board.json").write_text(json.dumps({"host": "testbox", "board": "brd_test", "tile": "obj_seat"}))

    # running the CLIs

    def cli(self, tool: str, *args: str, check: bool = True, timeout: float = 60, cwd: Path | None = None) -> subprocess.CompletedProcess:
        result = subprocess.run([sys.executable, str(CODE / "bin" / tool), *args], env=self.env, cwd=cwd or self.seat,
                                capture_output=True, text=True, timeout=timeout)
        if check and result.returncode != 0:
            self.fail(f"{tool} {' '.join(args)} exit {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}")
        return result

    def lane_record(self, lane_id: str) -> dict:
        return json.loads((self.seat / "state" / "lanes" / lane_id / "lane.json").read_text())

    def wait_for(self, predicate, timeout: float = 20, what: str = "condition") -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.1)
        self.fail(f"timed out waiting for {what}")

    def settled(self, lane_id: str, status: str | None = None) -> dict:
        """Wait until the lane's latest run has ended (and has `status`)."""
        def done():
            record = self.lane_record(lane_id)
            return record["runs"] and record["runs"][-1].get("end") and (status is None or record["status"] == status)
        self.wait_for(done, what=f"{lane_id} to settle")
        return self.lane_record(lane_id)

    def log_lines(self, name: str) -> list:
        path = self.stubs / name
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def pr_stub(self, head: str = "a" * 40, state: str = "OPEN", checks=(("test", "SUCCESS"),), mergeable: str = "MERGEABLE",
                merge_state: str = "CLEAN", comments=(), branch: str | None = None, review_decision: str = "",
                created: str = "2026-10-13T00:00:00Z") -> None:
        """`comments`: ids (the id is also the body), (id, body) or (id, body, author). The default author is the
        gh login the operator and the lanes share. `checks`: (name, conclusion) or (name, conclusion, completedAt)."""
        rows = [c if isinstance(c, tuple) else (c, c) for c in comments]
        pr = {"headRefOid": head, "headRefName": branch, "baseRefName": "main", "state": state, "isDraft": False,
              "mergeable": mergeable, "mergeStateStatus": merge_state, "latestReviews": [], "reviewRequests": [],
              "reviews": [], "reviewDecision": review_decision,
              "comments": [{"id": row[0], "body": row[1], "author": {"login": row[2] if len(row) > 2 else "shared-login"},
                            "createdAt": created} for row in rows],
              "statusCheckRollup": [{"name": c[0], "conclusion": c[1], "detailsUrl": f"https://ci.example/{c[0]}",
                                     "completedAt": c[2] if len(c) > 2 else "2026-10-13T00:00:00Z"} for c in checks]}
        (self.stubs / "gh" / "pr.json").write_text(json.dumps(pr))

    def gh_api(self, path: str, value=None, error: str | None = None) -> None:
        """Answer `gh api <path>` with `value` as JSON, or fail with `error`."""
        stem = str(self.stubs / "gh" / "api" / path.replace("/", "_"))
        os.makedirs(os.path.dirname(stem), exist_ok=True)
        Path(stem + (".json" if error is None else ".err")).write_text(error if error is not None else json.dumps(value))


def dedent(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")

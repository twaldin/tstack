"""Test harness: a temp HOME with an engine fixture, an instance, a project clone and stub gh/easl/omp/systemctl."""

from __future__ import annotations

import datetime as dt
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
exists unless --force, and fails when stubs/easl-fail exists; with stubs/easl-restart-commit ({"repo", "file"})
it first commits a line appended to that file, as an update landing mid-restart; agent.wait fails when
stubs/easl-wait-fail exists; agent.prompt fails when stubs/easl-prompt-fail exists, else answers delivery
`message` for a tile whose agents.json entry has protocol 1 (`typed` otherwise); agent.list returns
stubs/agents.json."""
import json, os, subprocess, sys
from pathlib import Path
stubs = Path(os.environ["STUBS"])
args = sys.argv[1:]
with open(stubs / "easl.log", "a") as log:
    log.write(json.dumps(args) + "\n")
asks_path = stubs / "asks.json"
asks = json.loads(asks_path.read_text()) if asks_path.exists() else {}
def out(value):
    print(json.dumps(value))
if args[0] == "agent.restart" and (stubs / "easl-restart-commit").exists():
    move = json.loads((stubs / "easl-restart-commit").read_text())
    with open(Path(move["repo"]) / move["file"], "a") as handle:
        handle.write("landed mid-restart\n")
    subprocess.run(["git", "-C", move["repo"], "commit", "--quiet", "-am", "mid-restart"], check=True)
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
elif args[0] == "agent.restart" and (stubs / "easl-kill-caller").exists():
    os.kill(os.getppid(), 9)  # the caller dies before its restart goes out
    sys.exit(1)
elif args[0] == "agent.restart" and (stubs / "easl-busy").exists() and "--force" not in args:
    print("conflict: the agent is working", file=sys.stderr)
    sys.exit(1)
elif args[0] == "agent.restart" and (stubs / "easl-fail").exists():
    print("not_found: no such tile", file=sys.stderr)
    sys.exit(1)
elif args[0] == "agent.prompt" and (stubs / "easl-prompt-fail").exists():
    print("unavailable: host not connected", file=sys.stderr)
    sys.exit(1)
elif args[0] == "agent.wait" and (stubs / "easl-wait-fail").exists():
    print("unavailable: no first report within 15 s", file=sys.stderr)
    sys.exit(1)
elif args[0] == "agent.list":
    print((stubs / "agents.json").read_text() if (stubs / "agents.json").exists() else json.dumps({"agents": []}))
elif args[0] == "agent.prompt":
    agents = json.loads((stubs / "agents.json").read_text())["agents"] if (stubs / "agents.json").exists() else []
    tile = args[args.index("--target") + 1]
    takes = any(a.get("tile") == tile and (a.get("protocol") or 0) >= 1 for a in agents)
    out({"delivery": "message" if takes else "typed", "submittedAt": "2026-10-13T00:00:00Z"})
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

FAKE_OMP = r'''
"""Fake seat omp: runs each <dir>/<n>.cmd (a JSON argv) in a session of its own, as omp's bash tool does,
writing the child's pid to <n>.pid and, once it exits, its exit code to <n>.rc."""
import json, subprocess, sys, time
from pathlib import Path
here, running = Path(sys.argv[1]), {}
def publish(name, value):
    (here / "tmp").write_text(str(value))
    (here / "tmp").rename(here / name)
while True:
    for cmd in sorted(here.glob("*.cmd")):
        if cmd.stem not in running:
            running[cmd.stem] = subprocess.Popen(json.loads(cmd.read_text()), start_new_session=True,
                                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            publish(f"{cmd.stem}.pid", running[cmd.stem].pid)
    for stem, child in running.items():
        if child.poll() is not None and not (here / f"{stem}.rc").exists():
            publish(f"{stem}.rc", child.returncode)
    time.sleep(0.05)
'''


class FakeOmp:
    """A seat omp, started at construction: the tstack commands it runs see it as their session_owner(), the
    way a seat's omp runs them through its bash tool."""

    def __init__(self, case: "Sandbox"):
        self.dir, self.count = Path(tempfile.mkdtemp(dir=case.root)), 0
        self.proc = subprocess.Popen([sys.executable, "-c", FAKE_OMP, str(self.dir)], env=case.env, cwd=case.seat)
        case.addCleanup(self.stop)

    def until(self, path: Path) -> str:
        deadline = time.monotonic() + 30
        while not path.exists():
            if time.monotonic() > deadline:
                raise AssertionError(f"fake omp: no {path.name}")
            time.sleep(0.05)
        return path.read_text()

    def spawn(self, *args: str) -> str:
        """Start `tstack <args>` in the seat; returns the command's name for pid() and end()."""
        self.count += 1
        name = f"{self.count:03d}"
        (self.dir / "cmd.tmp").write_text(json.dumps([sys.executable, str(CODE / "bin" / "tstack"), *args]))
        (self.dir / "cmd.tmp").rename(self.dir / f"{name}.cmd")
        self.until(self.dir / f"{name}.pid")
        return name

    def pid(self, name: str) -> int:
        return int((self.dir / f"{name}.pid").read_text())

    def run(self, *args: str) -> int:
        """Run `tstack <args>` in the seat to the end; returns its exit code."""
        return int(self.until(self.dir / f"{self.spawn(*args)}.rc"))

    def end(self, name: str) -> None:
        """Kill a command still running and wait until the fake omp has reaped it."""
        if not (self.dir / f"{name}.rc").exists():
            try:
                os.kill(self.pid(name), 9)
            except ProcessLookupError:
                pass
            self.until(self.dir / f"{name}.rc")

    def stop(self) -> None:
        if self.proc.poll() is not None:
            return
        for pid in self.dir.glob("*.pid"):
            self.end(pid.stem)
        self.proc.kill()
        self.proc.wait()


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

    def seat_tile(self, state: str = "idle", last: dict | None = None, **agent) -> None:
        """easl lists the seat's omp tile as `state`, its integration taking messages (protocol 1) unless
        `agent` says otherwise. With `last`, the tile's session file ends with that entry."""
        sid = "01a1345b-0000-7000-0000-000000000001"
        if last:
            session = self.home / ".omp" / "agent" / "sessions" / "-fake" / f"2026-10-13T00-00-00-000Z_{sid}.jsonl"
            session.parent.mkdir(parents=True, exist_ok=True)
            session.write_text(json.dumps({"type": "session", "id": sid}) + "\n" + json.dumps(last) + "\n")
        tile = {"tile": "obj_seat", "kind": "omp", "lifecycle": {"state": state}, "focused": False, "draft": False,
                "protocol": 1, "sessionId": sid, **agent}
        (self.stubs / "agents.json").write_text(json.dumps({"agents": [tile]}))

    # running the CLIs

    def cli(self, tool: str, *args: str, check: bool = True, timeout: float = 60, cwd: Path | None = None,
            own_session: bool = False) -> subprocess.CompletedProcess:
        """Run `tool <args>` to the end. With own_session, the command runs in a session of its own under this
        test process, as a seat's omp runs its bash tool commands: this process is its session_owner()."""
        result = subprocess.run([sys.executable, str(CODE / "bin" / tool), *args], env=self.env, cwd=cwd or self.seat,
                                capture_output=True, text=True, timeout=timeout, start_new_session=own_session)
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
                created: str = "2026-10-13T00:00:00Z", statuses=()) -> None:
        """`comments`: ids (the id is also the body), (id, body) or (id, body, author). The default author is the
        gh login the operator and the lanes share. `checks`: CheckRuns as (name, conclusion) or (name, conclusion,
        completedAt). `statuses`: legacy commit statuses as (context, state, startedAt), shaped as gh prints them,
        each at one fixed target URL."""
        rows = [c if isinstance(c, tuple) else (c, c) for c in comments]
        rollup = [{"__typename": "CheckRun", "name": c[0], "conclusion": c[1], "detailsUrl": f"https://ci.example/{c[0]}",
                   "completedAt": c[2] if len(c) > 2 else "2026-10-13T00:00:00Z"} for c in checks]
        rollup += [{"__typename": "StatusContext", "context": s[0], "state": s[1], "startedAt": s[2],
                    "targetUrl": f"https://legacy-ci.example/{s[0]}"} for s in statuses]
        pr = {"headRefOid": head, "headRefName": branch, "baseRefName": "main", "state": state, "isDraft": False,
              "mergeable": mergeable, "mergeStateStatus": merge_state, "latestReviews": [], "reviewRequests": [],
              "reviews": [], "reviewDecision": review_decision,
              "comments": [{"id": row[0], "body": row[1], "author": {"login": row[2] if len(row) > 2 else "shared-login"},
                            "createdAt": created} for row in rows],
              "statusCheckRollup": rollup}
        (self.stubs / "gh" / "pr.json").write_text(json.dumps(pr))

    def gh_api(self, path: str, value=None, error: str | None = None) -> None:
        """Answer `gh api <path>` with `value` as JSON, or fail with `error`."""
        stem = str(self.stubs / "gh" / "api" / path.replace("/", "_"))
        os.makedirs(os.path.dirname(stem), exist_ok=True)
        Path(stem + (".json" if error is None else ".err")).write_text(error if error is not None else json.dumps(value))


def dedent(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def assistant_entry(seconds_ago: float, tool_call: bool = False, role: str = "assistant") -> dict:
    """An omp session line: an assistant message (text, or a tool call) written `seconds_ago`."""
    when = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    content = [{"type": "toolCall", "id": "t1", "name": "bash"}] if tool_call else [{"type": "text", "text": "Done."}]
    return {"type": "message", "id": "m1", "timestamp": when,
            "message": {"role": role, "content": content, "stopReason": "toolUse" if tool_call else "stop"}}

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
if not (Path(__file__).name == "systemd-run" and args[:1] == ["_supervise"]):
    with open(stubs / f"{Path(__file__).name}.log", "a") as log:
        log.write(json.dumps({"args": args, "cwd": os.getcwd(), "lane": os.environ.get("TSTACK_LANE")}) + "\n")
'''
FAKES = {
    "sudo": PRELUDE + r'''
sys.exit(subprocess.run(args[1:]).returncode)
''',
    "lsof": PRELUDE + r'''
if (stubs / "lsof.err").exists():
    sys.exit((stubs / "lsof.err").read_text())
print((stubs / "lsof.out").read_text() if (stubs / "lsof.out").exists() else "p1\nn/")
''',
    # A session event, then one assistant message after omp.json's sleep (60 s when the prompt says SLOW).
    # Like omp, it takes a moment to shut down on SIGTERM; omp.pid holds its pid.
    "omp": PRELUDE + r'''
import signal
signal.signal(signal.SIGTERM, lambda *_: (time.sleep(1), sys.exit(143)))
(stubs / "omp.pid").write_text(str(os.getpid()))
cfg = json.loads((stubs / "omp.json").read_text())
if cfg.get("child"):
    child = subprocess.Popen(cfg.get("child_command", ["sleep", "600"]), stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=cfg.get("detached", False),
                             env={} if cfg.get("clear_env") else None)
    (stubs / "child.pid").write_text(str(child.pid))
print(json.dumps({"type": "session", "id": args[args.index("--resume") + 1] if "--resume" in args else str(uuid.uuid4())}), flush=True)
for event in cfg.get("events", []):
    print(json.dumps(event), flush=True)
if cfg.get("oom"):
    os.kill(int(os.environ["TSTACK_TEST_SUPERVISOR"]), signal.SIGUSR1)
    time.sleep(60)
time.sleep(60 if "SLOW" in args[-1] else cfg.get("sleep", 0))
content = [{"type": "text", "text": cfg["answer"]}] if cfg.get("answer") else []
message = {"role": "assistant", "content": content, "usage": {"cost": {"total": cfg.get("cost", 0.01)}}}
print(json.dumps({"type": "message_end", "message": message}), flush=True)
sys.exit(cfg.get("exit", 0))
''',
    "gh": PRELUDE + r'''
def stub(name, default=None):
    return (stubs / name).read_text() if (stubs / name).exists() else default
repo = args[args.index("--repo") + 1].replace("/", "_") if "--repo" in args else ""
if args[:2] == ["pr", "list"] and (stub(f"pr-list-{repo}.err") or stub("pr-list.err")):
    sys.exit(stub(f"pr-list-{repo}.err") or stub("pr-list.err"))
elif args[:2] == ["pr", "list"]:
    print(stub(f"pr-list-{repo}.json") or stub("pr-list.json", "[]"))
elif args[:2] == ["pr", "view"] and stub("pr-view.err"):
    sys.exit(stub("pr-view.err"))
elif args[:2] == ["pr", "view"]:
    print((stubs / "pr-view.json").read_text())
elif args[:2] == ["pr", "merge"] and (stubs / "merge.err").exists():
    sys.exit((stubs / "merge.err").read_text())
elif args[:2] == ["pr", "merge"]:
    view = json.loads((stubs / "pr-view.json").read_text())
    (stubs / "pr-view.json").write_text(json.dumps({**view, "state": "MERGED", "mergeCommit": {"oid": "f" * 40}}))
''',
    "easl": PRELUDE + r'''
path = stubs / "easl-boards.json"
store = json.loads(path.read_text()) if path.exists() else {"boards": {}, "agents": []}
class Refused(Exception):
    pass
def objects(board):
    return store["boards"].setdefault(board, [])
def locate(oid):
    for board, held in store["boards"].items():
        for obj in held:
            if obj["id"] == oid:
                return board, obj
    raise Refused(f"not_found: {oid}")
def summary(obj):
    obj = json.loads(json.dumps(obj))
    if obj["type"] == "html" and "html" in obj["props"]:
        obj["props"]["html"] = f'({len(obj["props"]["html"].encode())} bytes, use object.get)'
    return obj
def bounds(board):
    for group in objects(board):
        if group["type"] != "group":
            continue
        members = [o for o in objects(board) if o["id"] in group["props"].get("members", [])]
        if not members:
            continue
        pad = group["props"].get("padding", 24)
        left = min(o["frame"]["x"] for o in members)
        top = min(o["frame"]["y"] for o in members)
        right = max(o["frame"]["x"] + o["frame"]["w"] for o in members)
        bottom = max(o["frame"]["y"] + o["frame"]["h"] for o in members)
        group["frame"] = {"x": left - pad, "y": top - pad - 32,
                          "w": right - left + 2 * pad, "h": bottom - top + 2 * pad + 32}
def apply(method, p, board):
    if method == "board.get":
        return {"objects": [summary(o) for o in objects(board) if not o["props"].get("archived")],
                "revision": store.get("revision", 0)}
    if method == "agent.list":
        return {"agents": store["agents"]}
    if method == "object.get":
        return {"object": locate(p["id"])[1]}
    if method == "object.find":
        found = objects(board)
        if "keyPrefix" in p:
            found = sorted((o for o in found if o["props"].get("key", "").startswith(p["keyPrefix"])),
                           key=lambda o: o["props"]["key"])
        if "type" in p:
            found = [o for o in found if o["type"] == p["type"]]
        if "status" in p:
            found = [o for o in found if o["props"].get("status") == p["status"]]
        return {"objects": [summary(o) for o in found]}
    if method in ("object.upsert", "object.create"):
        obj = next((o for o in objects(board) if o["props"].get("key") == p.get("key")), None) if method == "object.upsert" else None
        created = obj is None
        if created:
            store["next"] = store.get("next", 100) + 1
            obj = {"id": f"obj_{store['next']}", "type": p["type"], "props": {}, "frame": {}}
            objects(board).append(obj)
        if "key" in p:
            obj["props"]["key"] = p["key"]
        obj["props"].update(p.get("props") or {})
        if obj["type"] != "group":
            obj["frame"].update(p.get("frame") or {})
        obj["rev"] = obj.get("rev", 0) + 1
        bounds(board)
        return {"object": obj, "created": created}
    if method == "object.update":
        held, obj = locate(p["id"])
        obj["props"].update(p.get("props") or {})
        if obj["type"] != "group":
            obj["frame"].update(p.get("frame") or {})
        obj["rev"] = obj.get("rev", 0) + 1
        bounds(held)
        return {"object": obj}
    if method == "object.delete":
        held, obj = locate(p["id"])
        store["boards"][held].remove(obj)
        for group in objects(held):
            if group["type"] == "group" and p["id"] in group["props"].get("members", []):
                group["props"]["members"].remove(p["id"])
        bounds(held)
        return {"id": p["id"]}
    if method == "object.batch":
        if len(p["ops"]) > 50:
            raise Refused("invalid_params: at most 50 ops")
        before = json.loads(json.dumps(store))
        results = []
        def resolve(value):
            if isinstance(value, str) and value.startswith("$") and value[1:].isdigit():
                return results[int(value[1:])]["object"]["id"]
            if isinstance(value, list):
                return [resolve(v) for v in value]
            if isinstance(value, dict):
                return {k: resolve(v) for k, v in value.items()}
            return value
        try:
            for op in p["ops"]:
                params = resolve(op["params"])
                if (stubs / "terminal-moves.err").exists() and op["method"] == "object.update" and locate(params["id"])[1]["type"] == "terminal":
                    raise Refused("unavailable: terminal move batch failed")
                results.append(json.loads(json.dumps(apply(op["method"], params, board))))
        except (Refused, IndexError, KeyError):
            store.clear()
            store.update(before)
            raise
        store["revision"] = store.get("revision", 0) + 1
        return {"results": results, "revision": store["revision"]}
    if method == "layout.check":
        held = [o for o in objects(board) if o["type"] not in ("arrow", "group") and not o["props"].get("archived")]
        def hits(a, b):
            return a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"] and a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"]
        pairs = [[a["id"], b["id"]] for n, a in enumerate(held) for b in held[n + 1:]
                 if (a["id"] in p["ids"] or b["id"] in p["ids"]) and hits(a["frame"], b["frame"])]
        return {"overlaps": pairs, "overflow": [], "truncated": [], "scrolls": [], "arrowCrossings": [],
                "labelOverlaps": [], "arrowOverlaps": [], "arrowIntersections": []}
    raise Refused(f"fake easl: no method {method}")
try:
    params = {}
    for flag, value in zip(args[1::2], args[2::2]):
        params.update(json.loads(sys.stdin.read()) if flag == "--json" else {flag[2:]: value})
    out = apply(args[0], params, params.get("board"))
except Refused as error:
    sys.exit(str(error))
if args[0] not in ("board.get", "agent.list", "object.get", "object.find", "layout.check"):
    path.write_text(json.dumps(store, indent=1))
print(json.dumps(out))
''',
    "systemd-run": PRELUDE + r'''
import shlex, signal
if args[0] == "_supervise":
    spec = json.loads(Path(args[1]).read_text())
    proc = subprocess.Popen(spec["command"], cwd=spec["cwd"], start_new_session=True,
                            env={**os.environ, "TSTACK_TEST_SUPERVISOR": str(os.getpid())})
    unit, stopping = spec["unit"], False
    def collect(result):
        (stubs / f"{unit}.state").write_text("deactivating")
        for callback in spec["stop"]:
            subprocess.run(shlex.split(callback), env={**os.environ, "SERVICE_RESULT": result,
                           "MAINPID": str(proc.pid), "TSTACK_TEST_GROUP": str(proc.pid),
                           "TSTACK_TEST_MARKER": spec["command"][7]}, check=False)
    def stop(signum, _):
        global stopping
        if not stopping:
            stopping = True
            collect("oom-kill" if signum == signal.SIGUSR1 else "success")
            os.killpg(proc.pid, signal.SIGTERM)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGUSR1, stop)
    (stubs / f"{unit}.state").write_text("active")
    (stubs / f"{unit}.supervisor.pid").write_text(str(os.getpid()))
    (stubs / f"{unit}.pid").write_text(str(proc.pid))
    proc.wait()
    if not stopping:
        collect("success")
    marker = f"TSTACK_LANE_RUN={spec['command'][7]}".encode()
    for candidate in Path("/proc").iterdir():
        if not candidate.name.isdigit():
            continue
        try:
            if candidate.stat().st_uid == os.getuid() and marker in (candidate / "environ").read_bytes().split(b"\0"):
                os.kill(int(candidate.name), signal.SIGKILL)
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    (stubs / f"{unit}.state").write_text("inactive")
    sys.exit(0)
if "--expand-environment=no" not in args:
    sys.exit("fake systemd-run: would expand $ in the command line")
if (stubs / "systemd-run.err").exists():
    sys.exit((stubs / "systemd-run.err").read_text())
env, cwd, unit, i = dict(os.environ), None, None, 0
while args[i].startswith("-"):
    key, _, value = args[i].partition("=")
    env.update([value.split("=", 1)] if key == "--setenv" else [])
    cwd, unit = (value if key == "--working-directory" else cwd), (value if key == "--unit" else unit)
    i += 2 if args[i] == "-p" else 1
spec = stubs / f"{unit}.supervisor.json"
spec.write_text(json.dumps({"command": args[i:], "cwd": cwd, "unit": unit,
                           "stop": [a.removeprefix("ExecStop=-") for a in args if a.startswith("ExecStop=-")]}))
(stubs / f"{unit}.pid").unlink(missing_ok=True)
subprocess.Popen([sys.executable, __file__, "_supervise", str(spec)], env=env,
                 start_new_session=True, stdin=subprocess.DEVNULL,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
while not (stubs / f"{unit}.pid").exists():
    time.sleep(0.01)
''',
    "systemctl": PRELUDE + r'''
if args[1:4] == ["show", "-p", "MainPID"]:
    print((stubs / f"{args[-1]}.pid").read_text())
elif args[1:4] == ["show", "-p", "ActiveState"]:
    print((stubs / f"{args[-1]}.state").read_text())
elif args[1:3] == ["show", "-p"]:
    path = stubs / "systemd-properties.json"
    if path.exists():
        for key, value in json.loads(path.read_text()).items():
            print(f"{key}={value}")
elif args[1] == "stop":
    os.kill(int((stubs / f"{args[2]}.supervisor.pid").read_text()), 15)
    while (stubs / f"{args[2]}.state").read_text() != "inactive":
        time.sleep(0.01)
''',
    "ps": PRELUDE + r'''
if args[:1] == ["-wwE"]:
    result = subprocess.run(["/bin/ps", "-ww", "-U", args[2], "-o", "pid=,command="],
                            capture_output=True, text=True)
    for line in result.stdout.splitlines():
        pid = line.strip().split(None, 1)[0]
        try:
            values = (Path("/proc") / pid / "environ").read_bytes().split(b"\0")
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        marker = next((v.decode() for v in values if v.startswith(b"TSTACK_LANE_RUN=")), "")
        print(line + " " + marker + " TEST_ONLY_SECRET=must-not-save")
    sys.exit(result.returncode)
if os.environ.get("TSTACK_TEST_GROUP") and args[-1:] == ["pid=,stat=,command="]:
    result = subprocess.run(["/bin/ps", *args[:-1], "pid=,pgid=,stat=,command="],
                            capture_output=True, text=True)
    token = f"TSTACK_LANE_RUN={os.environ['TSTACK_TEST_MARKER']}".encode()
    for line in result.stdout.splitlines():
        pid, group, state, command = line.strip().split(None, 3)
        if group != os.environ["TSTACK_TEST_GROUP"]:
            try:
                if token not in (Path("/proc") / pid / "environ").read_bytes().split(b"\0"):
                    continue
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
        print(f"{pid} {state} {command}")
    sys.exit(result.returncode)
os.execv("/bin/ps", ["/bin/ps", *args])
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
                    "TSTACK_MACHINE": "box", "PYTHONDONTWRITEBYTECODE": "1", "TZ": "UTC"}
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

    def cli_platform(self, platform, tool, *args, check=True) -> subprocess.CompletedProcess:
        launcher = ("import runpy,sys; sys.platform=sys.argv.pop(1); "
                    "sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')")
        result = subprocess.run([sys.executable, "-c", launcher, platform, str(CODE / "bin" / tool), *args],
                                env=self.env, cwd=self.seat, capture_output=True, text=True, timeout=60)
        if check and result.returncode:
            self.fail(f"{tool} {' '.join(args)}: exit {result.returncode}\n{result.stdout}{result.stderr}")
        return result

    def record(self, lane_id: str) -> dict:
        return json.loads((self.seat / "state" / "lanes" / lane_id / "lane.json").read_text())

    def log(self, fake: str) -> list:
        path = self.stubs / f"{fake}.log"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def until(self, predicate) -> bool:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.1)
        return False

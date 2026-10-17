"""Test harness: a temp HOME with a seat, and fake omp, gh, easl, ssh, bun and systemd on PATH that log each call."""

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
    log.write(json.dumps({"args": args, "cwd": os.getcwd(), "lane": os.environ.get("TSTACK_LANE"),
                         "host": os.environ.get("TSTACK_TEST_SSH_HOST")}) + "\n")
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
    # pr list prints pr-list-<owner>_<repo>.json or pr-list.json, or fails with pr-list-<owner>_<repo>.err or
    # pr-list.err; api prints api.json's value for the path without its query, else [], and fails with a string value;
    # pr view prints its JSON file;
    # pr merge fails with merge.err, else merges unless --auto.
    "gh": PRELUDE + r'''
def stub(name, default=None):
    return (stubs / name).read_text() if (stubs / name).exists() else default
repo = args[args.index("--repo") + 1].replace("/", "_") if "--repo" in args else ""
if args[:2] == ["pr", "list"] and (stub(f"pr-list-{repo}.err") or stub("pr-list.err")):
    sys.exit(stub(f"pr-list-{repo}.err") or stub("pr-list.err"))
elif args[:2] == ["pr", "list"]:
    print(stub(f"pr-list-{repo}.json") or stub("pr-list.json", "[]"))
elif args[0] == "api":
    value = json.loads(stub("api.json", "{}")).get(args[1].split("?")[0], [])
    sys.exit(value) if isinstance(value, str) else print(json.dumps(value))
elif args[:2] == ["pr", "view"]:
    print((stubs / "pr-view.json").read_text())
elif args[:2] == ["pr", "merge"] and (stubs / "merge.err").exists():
    sys.exit((stubs / "merge.err").read_text())
elif args[:2] == ["pr", "merge"] and "--auto" not in args:
    view = json.loads((stubs / "pr-view.json").read_text())
    (stubs / "pr-view.json").write_text(json.dumps({**view, "state": "MERGED", "mergeCommit": {"oid": "f" * 40}}))
''',
    # A board store in easl-boards.json ({"boards": {id: [object]}, "agents": [...]}) with the methods tstack board
    # calls. object.measure and size "fit" give h from the html's length. no-app fails unless app-<host> names the
    # rendering host. Like easl's RefitFrame, "fit" without x/y on an existing object grows it upward here.
    # Like easl's board.history, each call logs its creates, changes and deletes under one new board revision, by
    # agent:<caller> (the param, else EASL_TILE_ID where the method takes one) or else user. batch-fault.json
    # ({"after": n, "error"}) fails each object.batch after the first n; user-move.json ({"after": n, "id", "frame"})
    # has the user move an object right after object.batch number n.
    "easl": PRELUDE + r'''
path = stubs / "easl-boards.json"
store = json.loads(path.read_text()) if path.exists() else {"boards": {}, "agents": []}
store.setdefault("next", 100)
CALLERS = ("object.batch", "object.create", "object.update", "object.upsert", "object.delete", "object.measure",
           "layout.check")
actor, changes = "user", []
class Refused(Exception):
    pass
def logged(kind, board, obj, before=None):
    clauses = []
    if before:
        old, new = before["frame"], obj["frame"]
        if (old["x"], old["y"]) != (new["x"], new["y"]):
            clauses.append(f"moved ({old['x']}, {old['y']}) → ({new['x']}, {new['y']})")
        if (old["w"], old["h"]) != (new["w"], new["h"]):
            clauses.append(f"resized {old['w']}×{old['h']} → {new['w']}×{new['h']}")
        keys = sorted(k for k in {**before["props"], **obj["props"]} if before["props"].get(k) != obj["props"].get(k))
        clauses += [f"props {', '.join(keys)}"] if keys else []
        if not clauses:
            return
    props = obj["props"]
    title = (props.get("title") or props.get("caption") or (props.get("markdown") or "").split("\n")[0]
             or props.get("key") or obj["id"])
    label = f'browser {props.get("url") or "about:blank"}' if obj["type"] == "browser" else f'{obj["type"]} "{title}"'
    changes.append({"board": board, "kind": kind, "id": obj["id"], "type": obj["type"],
                    "summary": label + (": " + "; ".join(clauses) if clauses else "")})
def commit(board, who=None, revision=None, prefix=""):
    revs, seqs = store.setdefault("revs", {}), store.setdefault("seqs", {})
    if changes:
        board = changes[0]["board"]
        revs[board] = revision if revision is not None else revs.get(board, 1000) + 1
        log = store.setdefault("history", {}).setdefault(board, [])
        at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for change in changes:
            seqs[board] = seqs.get(board, 0) + 1
            log.append({"seq": seqs[board], "rev": revs[board], "at": at, "actor": who or actor,
                        **{k: v for k, v in change.items() if k not in ("board", "summary")},
                        "summary": prefix + change["summary"]})
            live = next((o for o in objects(board) if o["id"] == change["id"]), None)
            if live is not None:
                live["rev"] = revs[board]
        del log[:-2000]
        changes.clear()
    return revs.get(board, 1000)
def objects(board):
    return store["boards"].setdefault(board, [])
def locate(oid):
    for board, held in store["boards"].items():
        for obj in held:
            if obj["id"] == oid:
                return board, obj
    raise Refused(f"not_found: {oid}")
def keyed(board, key):
    return next((o for o in objects(board) if o["props"].get("key") == key), None)
def hits(a, b):
    return a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"] and a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"]
def measured(html):
    remote = os.environ.get("TSTACK_TEST_SSH_HOST")
    if (stubs / "measure.err").exists():
        raise Refused((stubs / "measure.err").read_text())
    if remote:
        count = store.setdefault("remote_measures", {}).get(remote, 0)
        store["remote_measures"][remote] = count + 1
        config = stubs / f"measure-{remote}.json"
        fault = json.loads(config.read_text()) if config.exists() else None
        if fault and count >= fault.get("after", 0):
            raise Refused(fault["error"])
    if (stubs / "no-app").exists() and not (remote and (stubs / f"app-{remote}").exists()):
        raise Refused("invalid_params: an html page is measured by the app")
    return 100 + len(html) // 50
def settle(obj, params, existed):
    if params.get("size") == "fit":
        h = measured(obj["props"].get("html", ""))
        if existed and "y" not in (params.get("frame") or {}):
            obj["frame"]["y"] -= h - obj["frame"]["h"]
        obj["frame"]["h"] = h
    if sorted(obj["frame"]) != ["h", "w", "x", "y"]:
        raise Refused(f"invalid_params: frame needs x, y, w and h, got {sorted(obj['frame'])}")
def apply(method, p, board):
    if method in ("object.get", "object.update", "object.delete") and "board" in p:
        raise Refused(f"invalid_params: unknown param board; {method} takes id")
    if method == "object.get":
        obj = locate(p["id"])[1]
        return {"object": {**obj, "rev": obj.get("rev", 0)}}
    if method == "object.measure":
        if os.environ.get("TSTACK_TEST_SSH_HOST") and "board" in p and (stubs / "no-app").exists():
            raise Refused("not_found: the headless board does not exist on the rendering host")
        return {"w": p["width"], "h": measured(p["props"].get("html", ""))}
    if method == "object.find" and sum(k in p for k in ("key", "keyPrefix", "type")) != 1:
        raise Refused("invalid_params: object.find takes one of key, keyPrefix, or type")
    if method == "object.find" and "key" in p:
        if not keyed(board, p["key"]):
            raise Refused(f"not_found: no object holds key {p['key']}")
        return {"object": keyed(board, p["key"])}
    if method == "object.find":
        found = sorted((o for o in objects(board) if o["props"].get("key", "").startswith(p.get("keyPrefix", ""))
                        and p.get("type") in (None, o["type"])), key=lambda o: o["props"].get("key", ""))
        return {"objects": [{**o, "props": {**o["props"], "html": "(use object.get)"}} for o in found]}
    if method == "object.upsert":
        obj, created = keyed(board, p["key"]), False
        before = json.loads(json.dumps(obj)) if obj else None
        if not obj:
            store["next"] += 1
            obj, created = {"id": f"obj_{store['next']}", "type": p["type"], "props": {"key": p["key"]},
                            "frame": dict(p.get("frame") or {})}, True
            objects(board).append(obj)
        else:
            obj["frame"].update(p.get("frame") or {})
        obj["props"].update(p["props"])
        settle(obj, p, not created)
        logged("created" if created else "updated", board, obj, before)
        return {"object": obj, "created": created}
    if method == "object.update":
        held, obj = locate(p["id"])
        if "rev" in p and p["rev"] != obj.get("rev", 0):
            raise Refused(f"conflict: object {p['id']} changed since revision {p['rev']}")
        before = json.loads(json.dumps(obj))
        obj["props"].update(p.get("props") or {})
        obj["frame"].update(p.get("frame") or {})
        settle(obj, p, True)
        logged("updated", held, obj, before)
        return {"object": obj}
    if method == "object.delete":
        held, obj = locate(p["id"])
        store["boards"][held].remove(obj)
        logged("deleted", held, obj)
        return {"id": p["id"]}
    if method == "object.batch":
        fault = json.loads((stubs / "batch-fault.json").read_text()) if (stubs / "batch-fault.json").exists() else None
        if (stubs / "terminal-moves.err").exists() and any(
                op["method"] == "object.update" and locate(op["params"]["id"])[1]["type"] == "terminal"
                for op in p["ops"]):
            raise Refused("unavailable: terminal move batch failed")
        results = []
        before_batch = json.loads(json.dumps(objects(board)))
        try:
            for n, op in enumerate(p["ops"]):
                results.append(apply(op["method"], op["params"], board))
                if fault and store.get("batches", 0) >= fault["after"] and n >= fault.get("op", 0):
                    raise Refused(fault["error"])
        except Refused as error:
            failed_revision = commit(board)
            attempted = {o["id"]: o for o in objects(board)}
            original = {o["id"]: o for o in before_batch}
            store["boards"][board] = before_batch
            for oid, obj in original.items():
                if oid not in attempted:
                    logged("created", board, obj)
                elif obj["frame"] != attempted[oid]["frame"] or obj["props"] != attempted[oid]["props"]:
                    logged("updated", board, obj, attempted[oid])
            for oid, obj in attempted.items():
                if oid not in original:
                    logged("deleted", board, obj)
            commit(board, "system", failed_revision, "reverted (batch failed): ")
            raise Refused(f"invalid_params: op {n} ({op['method']}): {error}")
        store["batches"] = store.get("batches", 0) + 1
        revision = commit(board)
        move = json.loads((stubs / "user-move.json").read_text()) if (stubs / "user-move.json").exists() else None
        if move and move["after"] == store["batches"]:
            apply("object.update", {"id": move["id"], "frame": move["frame"]}, board)
            commit(board, "user")
        return {"results": results, "revision": revision}
    if method == "board.history":
        if (stubs / "history.err").exists():
            raise Refused((stubs / "history.err").read_text())
        log, limit = store.get("history", {}).get(board, []), p.get("limit", 100)
        found = [e for e in log if e["kind"] in p.get("kinds", [e["kind"]])]
        return {"board": board, "cursor": log[-1]["seq"] if log else 0, "entries": found[-limit:],
                "truncated": len(found) > limit, "restarted": False}
    if method == "board.get":
        return {"objects": [{**o, "rev": o.get("rev", 0)} for o in objects(board)]}
    if method == "layout.check":
        # Like easl: every pair that involves at least one scoped id, against the whole board.
        held = [o for o in objects(board) if o["type"] not in ("arrow", "group") and not o["props"].get("archived")]
        pairs = [[a["id"], b["id"]] for n, a in enumerate(held) for b in held[n + 1:]
                 if (a["id"] in p["ids"] or b["id"] in p["ids"]) and hits(a["frame"], b["frame"])]
        return {"overlaps": pairs, "overflow": [], "truncated": [], "scrolls": [], "arrowCrossings": [],
                "labelOverlaps": [], "arrowOverlaps": [], "arrowIntersections": []}
    if method == "agent.list":
        return {"agents": store["agents"]}
    raise Refused(f"fake easl: no method {method}")
try:
    if args[:2] == ["ask", "list"] and (stubs / "ask.err").exists():
        raise Refused((stubs / "ask.err").read_text())
    if args[:2] == ["ask", "list"]:
        board = args[args.index("--board") + 1]
        out = {"objects": [o for o in objects(board) if o["type"] == "question" and o["props"].get("status") == "open"]}
    else:
        params = {}
        for flag, value in zip(args[1::2], args[2::2]):
            params.update(json.loads(sys.stdin.read()) if flag == "--json" else {flag[2:]: value})
        if "caller" in params and args[0] not in CALLERS:
            raise Refused(f"invalid_params: unknown param caller; {args[0]} takes none")
        caller = params.get("caller") or (os.environ.get("EASL_TILE_ID") if args[0] in CALLERS else None)
        actor = f"agent:{caller}" if caller else "user"
        out = apply(args[0], params, params.get("board"))
        commit(params.get("board"))
except Refused as error:
    path.write_text(json.dumps(store, indent=1))
    sys.exit(str(error))
path.write_text(json.dumps(store, indent=1))
print(json.dumps(out))
''',
    # Runs the remote command here, in $HOME as ssh would, with stdin passed through but EASL_TILE_ID left behind.
    # Exits 0 whatever the command exited with, as ssh to the tailnet Macs does.
    "ssh": PRELUDE + r'''
i = 0
while args[i].startswith("-"):
    i += 2 if args[i] == "-o" else 1
error = stubs / f"ssh-{args[i]}.err"
if error.exists():
    print(error.read_text(), file=sys.stderr)
    sys.exit(255)
env = {k: v for k, v in os.environ.items() if k != "EASL_TILE_ID"}
subprocess.run(["sh", "-c", " ".join(args[i + 1:])], cwd=os.environ["HOME"],
               env={**env, "TSTACK_TEST_SSH_HOST": args[i]})
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
                    "TSTACK_LAUNCHER": "process", "TSTACK_MACHINE": "box", "PYTHONDONTWRITEBYTECODE": "1", "TZ": "UTC"}
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
        """Each call of `fake`: {"args", "cwd", "lane", "host"}."""
        path = self.stubs / f"{fake}.log"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def until(self, predicate) -> bool:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.1)
        return False

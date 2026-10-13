"""Lanes: headless omp runs of one task each, their records, reports, merge guard and supervisor."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import prwatch
from .seat import stop_reason
from .util import (ENGINE, HOME, IS_MAC, Die, age, engine_home, flock, git, instance_home, iso, lock_is_held, machine, money,
                   read_json, run, short_age, short_lock, user_systemd, worktrees_root, write_atomic, write_json)

STATUSES = {"running", "waiting", "done", "stuck", "failed", "cancelled"}
TERMINAL = {"done", "stuck", "failed", "cancelled"}
REPORT_STATUSES = ("waiting", "done", "stuck", "failed")
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
SPAWN_GRACE = 60
MAX_WAVES = 2
EXIT_BUSY, EXIT_REFUSED, EXIT_STUCK = 3, 4, 5
PASS_ENV = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "SSH_AUTH_SOCK", "EASL_SOCKET",
            "TSTACK_HOME", "TSTACK_INSTANCE", "TSTACK_WORKTREES", "TSTACK_MACHINE")


class Lane:
    def __init__(self, seat: Path, lane_id: str):
        if not NAME.fullmatch(lane_id or ""):
            raise Die(f"bad lane id {lane_id!r}")
        self.seat, self.id = seat, lane_id
        self.dir = seat / "state" / "lanes" / lane_id
        self.run_dir = seat / "state" / "run" / "lanes" / lane_id
        self.record_path = self.dir / "lane.json"
        self.report_path = self.dir / "report.json"
        self.lock = self.run_dir / "lock"

    def exists(self) -> bool:
        return self.record_path.exists()

    def load(self) -> dict:
        record = read_json(self.record_path)
        if not isinstance(record, dict):
            raise Die(f"no lane {self.id} in {self.seat}")
        return record

    def save(self, record: dict) -> None:
        record["updated"] = iso()
        write_json(self.record_path, record)

    def mutate(self, change) -> dict:
        """Read-modify-write lane.json under the record lock (short, bounded; distinct from the run lock)."""
        with short_lock(self.run_dir / "record.lock"):
            record = self.load()
            change(record)
            self.save(record)
            return record

    def report(self) -> dict | None:
        return read_json(self.report_path)

    def run_log(self, number: int) -> Path:
        return self.run_dir / f"run-{number}.jsonl"

    def running(self, record: dict) -> bool:
        """The run lock is held, or a run was handed to the launcher moments ago and has not taken it yet."""
        return lock_is_held(self.lock) or starting(record)


def starting(record: dict) -> bool:
    """A run was handed to the launcher moments ago and its supervisor has not accepted it yet."""
    last = (record.get("runs") or [{}])[-1]
    since = age(last.get("start"))
    return (record.get("status") == "running" and not last.get("end") and not last.get("accepted")
            and since is not None and since < SPAWN_GRACE)


def lanes(seat: Path) -> list[Lane]:
    root = seat / "state" / "lanes"
    return [Lane(seat, p.name) for p in sorted(root.iterdir()) if (p / "lane.json").exists()] if root.is_dir() else []


def current_lane(seat: Path, explicit: str | None) -> Lane:
    lane_id = explicit or os.environ.get("TSTACK_LANE")
    if not lane_id:
        raise Die("no lane: pass an id or --lane, or run inside a lane (TSTACK_LANE)")
    return Lane(seat, lane_id)


# omp json stream

def parse_log(path: Path) -> tuple[str | None, float]:
    """Session id and the summed usage.cost.total of assistant messages in an omp --mode json stream."""
    session, cost = None, 0.0
    if not path.exists():
        return session, cost
    for line in path.read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "session" and not session:
            session = event.get("id")
        message = event.get("message") or {}
        if event.get("type") == "message_end" and message.get("role") == "assistant":
            cost += float(((message.get("usage") or {}).get("cost") or {}).get("total") or 0)
    return session, round(cost, 6)


def session_path(session_id: str | None) -> str | None:
    if not session_id:
        return None
    hits = sorted((HOME / ".omp" / "agent" / "sessions").glob(f"*/*_{session_id}.jsonl"))
    return str(hits[-1]) if hits else None


# composing and launching runs

def project_pipeline(project: str) -> Path:
    return instance_home() / "projects" / project / "pipeline.md"


def compose_prompt(lane: Lane, record: dict) -> Path:
    parts = [engine_home() / "roles" / "lane.md", engine_home() / "playbooks" / f"{record['playbook']}.md"]
    for part in parts:
        if not part.exists():
            raise Die(f"missing {part}")
    pipeline = project_pipeline(record["project"])
    if pipeline.exists():
        parts.append(pipeline)
    parts.append(lane.dir / "brief.md")
    header = (f"# Lane {lane.id}\n\nWorktree: {record['worktree']}\nBranch: {record['branch']} (base {record['base']})\n"
              f"Repo: {record.get('repo') or 'see the brief'}\n")
    text = header + "".join(f"\n<!-- {part} -->\n\n{part.read_text().strip()}\n" for part in parts)
    path = lane.run_dir / "prompt.md"
    write_atomic(path, text)
    return path


def launcher() -> str:
    return os.environ.get("TSTACK_LAUNCHER") or ("process" if IS_MAC else "systemd")


def unit_name(lane: Lane) -> str:
    return f"tstack-lane-{lane.id}"


def spawn(lane: Lane, record: dict, number: int, kind: str) -> None:
    """Start the supervisor for run `number` detached."""
    env = {k: os.environ[k] for k in PASS_ENV if os.environ.get(k)}
    env.update(TSTACK_LANE=lane.id, TSTACK_SEAT=str(lane.seat), TSTACK_HOME=str(engine_home()))
    command = [sys.executable, str(ENGINE / "bin" / "lane"), "_run", lane.id, "--seat", str(lane.seat), "--run", str(number)]
    lane.run_dir.mkdir(parents=True, exist_ok=True)
    if kind == "systemd":
        argv = ["systemd-run", "--user", "--collect", "--quiet", f"--unit={unit_name(lane)}",
                "-p", f"MemoryMax={record['memory']}"]
        cpus = run(["systemctl", "show", "agents.slice", "-p", "AllowedCPUs", "--value"], check=False).stdout.strip()
        if cpus:
            argv += ["-p", f"CPUAffinity={cpus}"]
        argv += [f"--setenv={k}={v}" for k, v in env.items()]
        user_systemd(argv + command, timeout=60)
    else:
        with open(lane.run_dir / "supervisor.log", "a") as log:
            subprocess.Popen(command, env={**os.environ, **env}, stdin=subprocess.DEVNULL, stdout=log,
                             stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)


def begin_run(lane: Lane, event: str, text: str, event_id: str | None = None, checks: list[str] = ()) -> int:
    """Append a run, mark the lane running and launch it. Caller holds the run lock. The run's fix-wave
    `checks` count only once its supervisor accepts it."""
    kind, seen = launcher(), {}

    def apply(record):
        seen.update(number=len(record["runs"]) + 1, status=record["status"], reason=record.get("reason"))
        entry = {"run": seen["number"], "event": event, "start": iso(), "launcher": kind}
        if event_id:
            entry["event_id"] = event_id
        if checks:
            entry["checks"] = list(checks)
        record["runs"].append(entry)
        record["status"], record["reason"] = "running", None

    record = lane.mutate(apply)
    write_atomic(lane.run_dir / f"run-{seen['number']}.event", text)
    try:
        spawn(lane, record, seen["number"], kind)
    except Die as error:
        why = f"launch failed: {error}"

        def undo(rec):
            rec["runs"].pop()
            rec["status"], rec["reason"] = seen["status"], why
        lane.mutate(undo)
        raise
    return seen["number"]


def omp_argv(record: dict, prompt: Path, text: str, resume: bool) -> list[str]:
    argv = ["omp", "-p", "--mode", "json", "--model", record["model"], "--thinking", record["thinking"],
            "--append-system-prompt", str(prompt)]
    if resume:
        argv += ["--resume", record["session"]]
    return argv + [text]


def supervise(lane: Lane, number: int) -> int:
    """`lane _run`: hold the run lock, run omp, record exit, cost, session and the report outcome."""
    with flock(lane.lock, timeout=SPAWN_GRACE) as held:
        if not held:
            print(f"lane {lane.id}: run lock busy, run {number} not started", file=sys.stderr)
            return 1
        admitted = {}

        def admit(rec):
            last = rec["runs"][-1] if rec["runs"] else {}
            if last.get("run") != number or last.get("end"):
                return
            if rec["status"] != "running":
                last.update(end=iso(), exit="not started")
                return
            last["accepted"] = iso()
            for check in last.get("checks", []):
                rec["waves"][check] = rec["waves"].get(check, 0) + 1
            admitted["record"] = rec
        lane.mutate(admit)
        if not admitted:
            print(f"lane {lane.id}: run {number} is no longer wanted; omp not started", file=sys.stderr)
            return 0
        record, last = admitted["record"], admitted["record"]["runs"][-1]
        write_atomic(lane.run_dir / "pid", f"{os.getpid()}\n")
        text = (lane.run_dir / f"run-{number}.event").read_text()
        prompt = compose_prompt(lane, record)
        argv = omp_argv(record, prompt, text, resume=last["event"] != "start")
        env = {**os.environ, "TSTACK_LANE": lane.id, "TSTACK_SEAT": str(lane.seat)}
        stopped = []
        with open(lane.run_log(number), "w") as log, open(lane.run_dir / f"run-{number}.err", "w") as err:
            try:
                proc = subprocess.Popen(argv, cwd=record["worktree"], env=env, stdout=subprocess.PIPE,
                                        stderr=err, stdin=subprocess.DEVNULL, text=True)
            except OSError as error:
                err.write(f"could not start omp: {error}\n")
                proc = None

            def stop(signum, _frame):
                stopped.append(signum)
                if proc and proc.poll() is None:
                    proc.terminate()

            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                signal.signal(sig, stop)
            if proc:
                for line in iter(proc.stdout.readline, ""):
                    log.write(line)
                    log.flush()
                    if not record.get("session") and '"session"' in line[:30]:
                        session = session_path(json.loads(line).get("id"))
                        if session:
                            record = lane.mutate(lambda rec: rec.update(session=session))
                code = proc.wait()
            else:
                code = 127
        finish_run(lane, number, code, stopped=bool(stopped))
        (lane.run_dir / "pid").unlink(missing_ok=True)
    return 0


def finish_run(lane: Lane, number: int, code, stopped: bool = False) -> dict:
    session_id, cost = parse_log(lane.run_log(number))
    session = session_path(session_id)
    report = lane.report()
    reported = isinstance(report, dict) and report.get("run") == number

    def change(record):
        entry = next(r for r in record["runs"] if r["run"] == number)
        entry.update(end=iso(), exit=code, cost_usd=cost)
        record["session"] = session or record.get("session")
        record["cost_usd"] = round(sum(r.get("cost_usd") or 0 for r in record["runs"]), 6)
        if record["status"] != "running":
            return
        if reported:
            record["status"], record["reason"] = report["status"], None
            record["pr"] = report.get("pr") or record.get("pr")
        elif stopped:
            record["status"], record["reason"] = "waiting", "stopped"
        else:
            record["status"], record["reason"] = "failed", "exited without report"

    record = lane.mutate(change)
    if reported:
        report["cost_usd"] = cost
        write_json(lane.report_path, report)
    return record


def reconcile(lane: Lane, locked: bool = False) -> dict:
    """A record says running but nothing holds the run lock past the spawn grace: the run died unrecorded.
    `locked`: the caller holds the run lock already."""
    record = lane.load()
    if record.get("status") != "running" or starting(record):
        return record
    if not locked:
        with flock(lane.lock, blocking=False) as held:
            return reconcile(lane, locked=True) if held else lane.load()
    last = (record.get("runs") or [{}])[-1]
    if not last or last.get("end"):
        return lane.mutate(lambda rec: rec.update(status="failed", reason="run ended unrecorded"))
    if not last.get("accepted"):
        def never(rec):
            rec["runs"][-1].update(end=iso(), exit="not started")
            rec.update(status="failed", reason="run never started")
        return lane.mutate(never)
    return finish_run(lane, last["run"], "lost")


# commands

def seat_project(seat: Path) -> str | None:
    projects = (instance_home() / "projects").resolve()
    try:
        return seat.resolve().relative_to(projects).parts[0]
    except (ValueError, IndexError):
        return None


def origin_slug(repo: Path) -> str | None:
    url = git(repo, "remote", "get-url", "origin", check=False)
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
    return match.group(1) if match else None


def default_base(repo: Path) -> str:
    ref = git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False)
    return ref.split("/", 1)[1] if ref.startswith("origin/") else "main"


def start(seat: Path, task: str, brief: Path, project: str | None, repo: str | None, playbook: str,
          model: str, thinking: str, timebox: str | None, memory: str, base: str | None) -> str:
    reason = stop_reason(seat)
    if reason:
        raise Die(f"seat is stopped (orders.md: STOP: {reason})", EXIT_BUSY)
    if not NAME.fullmatch(task):
        raise Die(f"task id {task!r} must be lowercase letters, digits and dashes")
    project = project or seat_project(seat)
    if not project:
        raise Die("--project is required outside a project seat")
    if not (engine_home() / "playbooks" / f"{playbook}.md").exists():
        raise Die(f"no playbook {playbook}")
    main = worktrees_root() / project / "main"
    if not (main / ".git").exists():
        raise Die(f"no project clone at {main}")
    text = Path(brief).read_text()
    match = re.search(r"^TIMEBOX:\s*(\S.*?)\s*$", text, re.M)
    if not match and not timebox:
        raise Die("the brief needs a `TIMEBOX: <duration>` line (or pass --timebox)")
    if not match:
        text = text.rstrip("\n") + f"\n\nTIMEBOX: {timebox}\n"
    base = base or default_base(main)
    prefix = f"{project}-{task}-"
    taken = [int(p.name[len(prefix):]) for p in (seat / "state" / "lanes").glob(prefix + "*")
             if p.name[len(prefix):].isdigit()]
    lane = Lane(seat, f"{prefix}{max(taken, default=0) + 1}")
    worktree = worktrees_root() / project / lane.id
    git(main, "fetch", "--quiet", "origin", base, timeout=300)
    git(main, "worktree", "add", "--quiet", "-b", f"lane/{lane.id}", str(worktree), f"origin/{base}")
    write_atomic(lane.dir / "brief.md", text)
    record = {"id": lane.id, "project": project, "task": task, "seat": str(seat), "host": machine(),
              "repo": repo or origin_slug(main), "worktree": str(worktree), "branch": f"lane/{lane.id}", "base": base,
              "playbook": playbook, "model": model, "thinking": thinking, "timebox": timebox or match.group(1),
              "memory": memory, "session": None, "status": "failed", "reason": "not started", "held": False, "pr": None,
              "waves": {}, "runs": [], "cost_usd": 0, "created": iso()}
    kickoff = (f"You are lane {lane.id}. Your brief and the lane protocol are in your system prompt; the brief is also at "
               f"{lane.dir / 'brief.md'}. Work in {worktree} on branch lane/{lane.id}. Start now.")
    with flock(lane.lock, blocking=False) as held:
        if not held:
            raise Die(f"lane {lane.id} is locked", EXIT_BUSY)
        lane.save(record)
        compose_prompt(lane, record)
        begin_run(lane, "start", kickoff)
    return lane.id


def dispatched(record: dict, event_id: str | None) -> int | None:
    """The run that took this dispatch: accepted by its supervisor, or handed to the launcher and not ended.
    A run that ended without starting does not count, so its dispatch can launch again."""
    return next((r["run"] for r in record.get("runs", []) if event_id and r.get("event_id") == event_id
                 and (r.get("accepted") or not r.get("end"))), None)


def resume(lane: Lane, text: str, kind: str | None, checks: list[str], event_id: str | None = None) -> tuple[int, bool]:
    """Returns (run number, newly accepted). A dispatch whose `event_id` a run already took is a no-op.
    Exit codes for callers: EXIT_BUSY retry later, EXIT_REFUSED drop the event, EXIT_STUCK lane is now stuck."""
    taken = dispatched(lane.load(), event_id)
    if taken:
        return taken, False
    with flock(lane.lock, blocking=False) as held:
        if not held:
            raise Die(f"lane {lane.id} is running", EXIT_BUSY)
        record = reconcile(lane, locked=True)
        taken = dispatched(record, event_id)
        if taken:
            return taken, False
        if starting(record):
            raise Die(f"lane {lane.id} is starting", EXIT_BUSY)
        if record.get("held"):
            raise Die(f"lane {lane.id} is held", EXIT_BUSY)
        reason = stop_reason(lane.seat)
        if reason:
            raise Die(f"seat is stopped (orders.md: STOP: {reason})", EXIT_BUSY)
        if record["status"] in TERMINAL:
            raise Die(f"lane {lane.id} is {record['status']}", EXIT_REFUSED)
        if not record.get("session"):
            raise Die(f"lane {lane.id} has no session to resume", EXIT_REFUSED)
        if kind == "check-failed" and not checks:
            raise Die("--kind check-failed needs --check NAME")
        exhausted = [c for c in checks if record["waves"].get(c, 0) >= MAX_WAVES] if kind == "check-failed" else []
        if exhausted:
            why = f"{', '.join(exhausted)} failed after {MAX_WAVES} fix waves"
            lane.mutate(lambda rec: rec.update(status="stuck", reason=why))
            raise Die(f"lane {lane.id} is stuck: {why}", EXIT_STUCK)
        return begin_run(lane, kind or "resume", text, event_id, checks if kind == "check-failed" else ()), True


def write_report(lane: Lane, status: str, summary: str, pr: str | None, head: str | None,
                 waiting_on: str | None, proofs: list[str]) -> dict:
    record = lane.load()
    if status not in REPORT_STATUSES:
        raise Die(f"--status must be one of {', '.join(REPORT_STATUSES)}")
    if not summary.strip():
        raise Die("--summary is required")
    if status == "waiting" and not waiting_on:
        raise Die("--status waiting needs --waiting-on")
    if pr:
        prwatch.parse_url(pr)
    if head and not re.fullmatch(r"[0-9a-f]{7,40}", head):
        raise Die("--head must be a commit SHA")
    proof = []
    for item in proofs:
        check, sep, result = item.partition("=")
        if not sep or not check.strip() or not result.strip():
            raise Die(f"--proof {item!r} must be CHECK=RESULT")
        if not head:
            raise Die("--proof needs --head: proof is for one commit")
        proof.append({"check": check.strip(), "result": result.strip(), "head": head})
    report = {"lane": lane.id, "status": status, "summary": summary.strip(), "waiting_on": waiting_on,
              "pr": pr or record.get("pr"), "head": head, "proof": proof, "cost_usd": None, "at": iso(),
              "run": (record.get("runs") or [{"run": 0}])[-1]["run"]}
    write_json(lane.report_path, report)
    if pr:
        lane.mutate(lambda rec: rec.update(pr=pr))
    return report


def pipeline_says(project: str, line: str) -> bool:
    """The project's pipeline.md has a line `<line>` (e.g. `merge: operator`)."""
    pipeline = project_pipeline(project)
    key, value = (part.strip() for part in line.split(":", 1))
    pattern = rf"^{re.escape(key)}:\s*{re.escape(value)}\b"
    return pipeline.exists() and re.search(pattern, pipeline.read_text(), re.M | re.I) is not None


def system_repo(slugs: list[str]) -> str | None:
    """The engine or instance repo among `slugs`: lanes never merge those, whatever a pipeline says."""
    for name, path in (("TSTACK_HOME", engine_home()), ("TSTACK_INSTANCE", instance_home())):
        if (path / ".git").exists():
            own = origin_slug(path)
            if own and own.lower() in {s.lower() for s in slugs if s}:
                return f"{own} is the origin of {name}"
    return None


# Ruleset rule types a squash merge of a mergeable PR already meets. Every other type except the review
# rule (pull_request) and required_status_checks, which lane merge checks itself, refuses --admin.
RULES_MET = {"creation", "deletion", "non_fast_forward", "required_linear_history"}
# Classic protection settings that do not bear on merging a mergeable PR with a squash commit.
CLASSIC_MET = {"url", "allow_force_pushes", "allow_deletions", "block_creations", "allow_fork_syncing",
               "required_linear_history"}


def gh_api(path: str):
    """(value, None) for `gh api <path>`, or (None, error text)."""
    result = run(["gh", "api", path], check=False, timeout=60)
    if result.returncode != 0:
        return None, (result.stderr or result.stdout).strip()[-200:]
    try:
        return json.loads(result.stdout), None
    except ValueError:
        return None, "not JSON"


def only_review_unmet(url: str, pr: dict, base: str) -> None:
    """Refuse unless GitHub's branch rules prove a missing review is the only unmet requirement: the rules
    API answers, every ruleset rule is of a type modeled here, classic protection (when the repo has any)
    asks for nothing beyond reviews, checks and conversation resolution, every required status check has a
    passing run on the head, a strict policy finds the head up to date, and required conversation
    resolution finds no open thread. An unreadable or unmodeled rule refuses."""
    owner, repo, _ = prwatch.parse_url(url)
    head = pr["headRefOid"]

    def refuse(why: str):
        raise Die(f"lane merge --admin refused: {why}. Report waiting on review", EXIT_REFUSED)
    rules, error = gh_api(f"repos/{owner}/{repo}/rules/branches/{base}")
    if error or not isinstance(rules, list):
        refuse(f"GitHub's rules for {base} are unreadable ({error or 'not a list'})")
    review, required, strict, resolve = False, set(), False, False
    for rule in rules:
        kind, params = rule.get("type"), rule.get("parameters") or {}
        if kind == "pull_request":
            review = True
            resolve |= bool(params.get("required_review_thread_resolution"))
        elif kind == "required_status_checks":
            required |= {c.get("context") for c in params.get("required_status_checks") or []}
            strict |= bool(params.get("strict_required_status_checks_policy"))
        elif kind not in RULES_MET:
            refuse(f"branch rule `{kind}` is not one lane merge can check")
    protection, error = gh_api(f"repos/{owner}/{repo}/branches/{base}/protection")
    if error and not re.search(r"HTTP 40[34]", error):
        refuse(f"classic branch protection for {base} is unreadable ({error})")
    for key, value in (protection or {}).items():
        if key == "required_pull_request_reviews":
            review = True
        elif key == "required_status_checks":
            required |= set(value.get("contexts") or []) | {c.get("context") for c in value.get("checks") or []}
            strict |= bool(value.get("strict"))
        elif key == "required_conversation_resolution":
            resolve |= bool(value.get("enabled"))
        elif key not in CLASSIC_MET and not (isinstance(value, dict) and value.get("enabled") is False):
            refuse(f"classic protection setting `{key}` is not one lane merge can check")
    if not review:
        refuse(f"no review rule on {base} explains the block")
    states: dict[str, list[str]] = {}
    for item in pr["statusCheckRollup"]:
        states.setdefault(prwatch.check_name(item), []).append(prwatch.check_state(item))
    unproven = sorted(c for c in required - {None} if not states.get(c) or any(s not in prwatch.GREEN for s in states[c]))
    if unproven:
        refuse(f"required checks without a passing run on {head[:12]}: {', '.join(unproven)}")
    if strict:
        compare, error = gh_api(f"repos/{owner}/{repo}/compare/{base}...{head}")
        if error or (compare or {}).get("behind_by") != 0:
            refuse(f"{base} requires an up-to-date branch and {head[:12]} is behind it ({error or 'behind'})")
    if resolve:
        try:
            open_threads = [t for t in prwatch.threads(url) if not t["r"]]
        except (subprocess.SubprocessError, ValueError, KeyError) as error:
            refuse(f"review threads are unreadable ({type(error).__name__})")
        if open_threads:
            refuse(f"{base} requires resolved conversations and the PR has open threads")


def merge(lane: Lane, admin: bool = False) -> str:
    """Hard merge guard (review F12): proof for the current head, open, mergeable, all checks passed.
    Never arms auto-merge: a later head would merge without proof."""
    record = lane.load()
    reason = stop_reason(lane.seat)
    if reason:
        raise Die(f"seat is stopped (orders.md: STOP: {reason})", EXIT_REFUSED)
    if pipeline_says(record["project"], "merge: operator"):
        raise Die(f"{record['project']} pipeline says `merge: operator`: the operator merges this PR, not the lane", EXIT_REFUSED)
    report = lane.report()
    if not isinstance(report, dict) or not report.get("pr"):
        raise Die("no report with a PR: run `lane report --pr URL --head SHA --proof CHECK=pass` first", EXIT_REFUSED)
    url = report["pr"]
    owner, repo, _ = prwatch.parse_url(url)
    system = system_repo([f"{owner}/{repo}", record.get("repo")])
    if system:
        raise Die(f"lane merge never merges system PRs ({system}): report waiting on operator", EXIT_REFUSED)
    try:
        pr = prwatch.view(url, "state,isDraft,mergeable,mergeStateStatus,headRefOid,headRefName,baseRefName,"
                               "statusCheckRollup,reviewDecision")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError) as error:
        raise Die(f"gh pr view {url} failed: {error}")
    head = pr["headRefOid"]
    if pr["state"] != "OPEN":
        raise Die(f"PR is {pr['state'].lower()}, not open", EXIT_REFUSED)
    if pr.get("isDraft"):
        raise Die("PR is a draft", EXIT_REFUSED)
    if pr.get("headRefName") and pr["headRefName"] != record["branch"]:
        raise Die(f"PR head branch {pr['headRefName']} is not this lane's branch {record['branch']}", EXIT_REFUSED)
    proofs = [p for p in report.get("proof", []) if p.get("head") == head]
    if not proofs:
        raise Die(f"no proof for the PR's current head {head[:12]}: prove it, then "
                  f"`lane report --head {head[:12]} --proof CHECK=pass`", EXIT_REFUSED)
    failed = [p["check"] for p in proofs if p["result"].lower() != "pass"]
    if failed:
        raise Die(f"proof for {head[:12]} did not pass: {', '.join(failed)}", EXIT_REFUSED)
    if pr["mergeable"] != "MERGEABLE":
        raise Die(f"PR is not mergeable ({pr['mergeable']}, {pr['mergeStateStatus']})", EXIT_REFUSED)
    verdict = prwatch.classify(pr)
    if verdict != "green":
        detail = ", ".join(prwatch.failing_checks(pr)) or "checks still running"
        raise Die(f"checks on {head[:12]} are {verdict}: {detail}", EXIT_REFUSED)
    if pr["mergeStateStatus"] in ("BEHIND", "DIRTY"):
        raise Die(f"PR merge state is {pr['mergeStateStatus']}: rebase onto {record['base']} first", EXIT_REFUSED)
    argv = ["gh", "pr", "merge", url, "--squash", "--delete-branch", "--match-head-commit", head]
    bypass = False
    if pr["mergeStateStatus"] == "BLOCKED":
        if pr.get("reviewDecision") != "REVIEW_REQUIRED":
            raise Die(f"GitHub blocks the merge for a reason other than a missing review (review decision "
                      f"{pr.get('reviewDecision') or 'none'}): report waiting with what blocks it", EXIT_REFUSED)
        if not admin:
            raise Die(f"checks on {head[:12]} passed and the proof holds, but GitHub requires a review. "
                      "Run `lane merge --admin` to merge past the review rule, or report waiting on review", EXIT_REFUSED)
        if pipeline_says(record["project"], "admin-merge: no"):
            raise Die(f"{record['project']} pipeline says `admin-merge: no`: report waiting on review", EXIT_REFUSED)
        only_review_unmet(url, pr, pr.get("baseRefName") or record["base"])
        bypass = True
    run(argv + (["--admin"] if bypass else []), timeout=120)
    lane.mutate(lambda rec: rec.update(merge={"head": head, "at": iso(), "admin": bypass}))
    return f"merged{' with --admin' if bypass else ''}: {url} at {head[:12]}"


def kill(lane: Lane) -> None:
    """Signal whatever holds the run lock for this lane: its systemd unit, or the supervisor's process group."""
    last = (lane.load().get("runs") or [{}])[-1]
    if last.get("launcher") == "systemd":
        user_systemd(["systemctl", "--user", "stop", unit_name(lane)], check=False, timeout=120)
        return
    pid_file = lane.run_dir / "pid"
    pid = pid_file.read_text().strip() if pid_file.exists() else ""
    if not pid.isdigit():
        return
    command = run(["ps", "-o", "command=", "-p", pid], check=False).stdout
    if f"_run {lane.id} " in command + " ":
        try:
            os.killpg(int(pid), signal.SIGTERM)
        except ProcessLookupError:
            pass


def settle(lane: Lane, final=None) -> dict:
    """Under the run lock: end a run its supervisor never accepted (a queued launch), record a lost one,
    then apply `final`. A supervisor that takes the lock later sees the run ended and starts nothing."""
    record = lane.load()
    last = (record.get("runs") or [{}])[-1]
    if record.get("status") == "running" and last and not last.get("end"):
        if last.get("accepted"):
            finish_run(lane, last["run"], "stopped", stopped=True)
        else:
            def unqueue(rec):
                rec["runs"][-1].update(end=iso(), exit="not started")
                rec.update(status="waiting" if rec.get("session") else "failed", reason="stopped before start")
            lane.mutate(unqueue)
    return lane.mutate(final) if final else lane.load()


def stop(lane: Lane, wait: float = 30, final=None) -> dict:
    """Kill the running process, keep state. Holds the run lock while it settles, so it serializes with
    launch admission."""
    deadline = time.monotonic() + wait
    while True:
        with flock(lane.lock, blocking=False) as held:
            if held:
                return settle(lane, final)
        kill(lane)
        if time.monotonic() >= deadline:
            raise Die(f"lane {lane.id} did not stop within {wait:g}s")
        time.sleep(0.2)


def cancel(lane: Lane, reason: str | None, superseded_by: str | None = None) -> dict:
    def change(record):
        record["status"] = "cancelled"
        record["reason"] = reason or (f"superseded by {superseded_by}" if superseded_by else "cancelled")
        if superseded_by:
            record["superseded_by"] = superseded_by
    return stop(lane, final=change)


def clean(lane: Lane) -> str:
    record = reconcile(lane)
    if lane.running(record):
        raise Die(f"lane {lane.id} is running", EXIT_BUSY)
    worktree = Path(record["worktree"])
    if not worktree.exists():
        raise Die(f"{worktree} is already gone")
    if record.get("pr"):
        state = prwatch.view(record["pr"], "state")["state"]
        if state not in ("MERGED", "CLOSED"):
            raise Die(f"PR is {state.lower()}; clean waits for merged or closed", EXIT_REFUSED)
    elif record["status"] not in ("done", "failed", "cancelled"):
        raise Die(f"lane is {record['status']} with no PR", EXIT_REFUSED)
    dirty = git(worktree, "status", "--porcelain")
    if dirty:
        raise Die(f"{worktree} has uncommitted changes:\n{dirty}", EXIT_REFUSED)
    if not record.get("pr"):
        ahead = git(worktree, "rev-list", "--count", f"origin/{record['base']}..HEAD")
        if ahead != "0":
            raise Die(f"{record['branch']} has {ahead} commit(s) not on origin/{record['base']} and no PR: "
                      "open a PR for them, or save them and remove the worktree by hand", EXIT_REFUSED)
    main = worktrees_root() / record["project"] / "main"
    git(main, "worktree", "remove", str(worktree))
    lane.mutate(lambda rec: rec.update(cleaned=iso()))
    return f"removed {worktree} (branch {record['branch']} kept)"


def summary_row(lane: Lane, record: dict) -> str:
    runs = record.get("runs") or []
    since = runs[-1].get("end") or runs[-1].get("start") if runs else record.get("created")
    flags = " held" if record.get("held") else ""
    reason = f"  ({record['reason']})" if record.get("reason") else ""
    return (f"{lane.id}\t{record['status']}{flags}\trun {len(runs)}\t{money(record.get('cost_usd'))}\t"
            f"{short_age(since)}\t{record.get('pr') or '-'}{reason}")


def visible(record: dict, show_all: bool) -> bool:
    if show_all or record["status"] not in TERMINAL:
        return True
    return (age(record.get("updated")) or 0) < 86400


def describe(lane: Lane) -> str:
    record = reconcile(lane)
    report = lane.report()
    out = json.dumps(record, indent=1)
    if report:
        out += "\n\nreport.json:\n" + json.dumps(report, indent=1)
    return out


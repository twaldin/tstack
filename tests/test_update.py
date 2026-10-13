import datetime as dt
import fcntl
import json
import shutil
import subprocess

from support import CODE, FakeOmp, Sandbox, git


class Update(Sandbox):
    def setUp(self):
        super().setUp()
        self.board()
        shutil.copy2(CODE / "bin" / "tstack", self.engine / "bin" / "tstack")
        shutil.copytree(CODE / "lib", self.engine / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        self.gitenv = {**self.env}
        self.engine_upstream = self.publish(self.engine, "engine")
        self.instance_upstream = self.publish(self.instance, "instance")
        self.cli("tstack", "setup", "--machine", "testbox", cwd=self.home)
        self.seat_tile()
        self.run_dir = self.seat / "state" / "run"
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def publish(self, repo, name):
        """Turn `repo` into a clone of a new bare upstream; return a second working clone to push from."""
        bare, work = self.root / f"{name}.git", self.root / f"{name}-upstream"
        run = lambda *a, cwd=repo: subprocess.run(a, cwd=cwd, check=True, capture_output=True, env=self.gitenv)
        run("git", "init", "--quiet", "-b", "main")
        run("git", "add", "-A")
        run("git", "commit", "--quiet", "-m", "init")
        run("git", "init", "--quiet", "--bare", "-b", "main", str(bare), cwd=self.root)
        run("git", "remote", "add", "origin", str(bare))
        run("git", "push", "--quiet", "-u", "origin", "main")
        run("git", "clone", "--quiet", str(bare), str(work), cwd=self.root)
        return work

    def push(self, work, path, text):
        (work / path).parent.mkdir(parents=True, exist_ok=True)
        (work / path).write_text(text)
        for args in (["add", "-A"], ["commit", "--quiet", "-m", f"change {path}"], ["push", "--quiet"]):
            subprocess.run(["git", "-C", str(work), *args], check=True, capture_output=True, env=self.gitenv)

    def update(self, check=True):
        return self.cli("tstack", "update", cwd=self.home, check=check)

    def generation(self) -> str:
        return f"tstack@{git(self.engine, 'rev-parse', '--short=7', 'HEAD')} agents@{git(self.instance, 'rev-parse', '--short=7', 'HEAD')}"

    def installed(self) -> dict:
        return {"tstack": git(self.engine, "rev-parse", "HEAD"), "agents": git(self.instance, "rev-parse", "HEAD")}

    def loaded_now(self) -> None:
        """The seat's omp loaded the files installed now."""
        (self.run_dir / "recovery.json").write_text(json.dumps({"loaded": self.installed()}))

    def record(self) -> dict:
        return json.loads((self.run_dir / "recovery.json").read_text())

    def prompts(self) -> list[str]:
        return [c[c.index("--text") + 1] for c in self.log_lines("easl.log") if c[0] == "agent.prompt"]

    def calls(self) -> list[str]:
        return [c[0] for c in self.log_lines("easl.log") if c[0] != "agent.list"]

    def heartbeat(self, minutes: int) -> None:
        at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes)
        (self.run_dir / "heartbeat.json").write_text(json.dumps({"at": at.strftime("%Y-%m-%dT%H:%M:%SZ")}))

    def restart_self(self, check=True):
        self.env["EASL_TILE_ID"] = "obj_seat"
        return self.cli("tstack", "restart-self", check=check)

    def poll_under(self, omp: FakeOmp) -> None:
        """Have `omp` start the seat's wait; return once a poll under it has finished, with the wait ended."""
        beat = self.run_dir / "heartbeat.json"
        wait = omp.spawn("wait")

        def polled():
            data = json.loads(beat.read_text()) if beat.exists() else {}
            return data.get("pid") == omp.pid(wait) and not data.get("polling_since")
        self.wait_for(polled, what="a poll under the fake omp")
        omp.end(wait)

    def asked(self, generation: str, reasons: str) -> str:
        return (f"Files you load changed ({reasons}, generation {generation}). At your next safe point, "
                "after acking your batch, run `tstack restart-self`.")

    def test_fast_forward_reruns_setup_and_asks_the_seat_to_restart_itself(self):
        self.loaded_now()
        before = git(self.engine, "rev-parse", "HEAD")
        self.push(self.engine_upstream, "global.md", "# Global rules\n\nNew rule.\n")
        line = self.update().stdout
        after = git(self.engine, "rev-parse", "HEAD")
        self.assertNotEqual(before, after)
        self.assertIn(f"tstack={before[:7]}..{after[:7]}", line)
        self.assertIn("setup=ok check=ok", line)
        self.assertIn("seats=demo:asked to restart-self (tstack/global.md)", line)
        self.assertIn("New rule.", (self.home / ".omp" / "agent" / "AGENTS.md").read_text())
        self.assertEqual(self.calls(), ["agent.prompt"])
        self.assertEqual(self.prompts(), [self.asked(self.generation(), "tstack/global.md")])
        prompt = next(c for c in self.log_lines("easl.log") if c[0] == "agent.prompt")
        self.assertRegex(prompt[prompt.index("--message") + 1], r"^msg_tstack-restart-[0-9a-f]{20}$")
        self.assertEqual(self.record()["asked"]["generation"], self.generation())
        log = (self.home / ".local" / "state" / "tstack" / "update.log").read_text().splitlines()
        self.assertEqual(log, [line.strip()])

    def test_the_ask_repeats_each_run_until_the_seat_restarts_itself_then_update_sends_its_restart_prompt(self):
        self.loaded_now()
        self.push(self.instance_upstream, "projects/demo/AGENTS.md", "# Demo project\n")
        self.update()
        self.assertIn("demo:asked to restart-self (agents/projects/demo/AGENTS.md)", self.update().stdout)
        ids = [c[c.index("--message") + 1] for c in self.log_lines("easl.log") if c[0] == "agent.prompt"]
        self.assertEqual((len(ids), len(set(ids))), (2, 2))
        self.heartbeat(1)
        self.restart_self()
        restart = next(c for c in self.log_lines("easl.log") if c[0] == "agent.restart")
        self.assertEqual((restart[restart.index("--target") + 1], "--force" in restart), ("obj_seat", True))
        record = self.record()
        self.assertEqual(record["pending"]["generation"], self.installed())
        self.assertNotEqual(record["loaded"], self.installed())
        self.assertEqual((record["restart"]["by"], record["restart"]["prompted"]), ("self", None))
        line = self.update().stdout
        self.assertIn("demo:restart prompt sent", line)
        self.assertIn(f"demo:restart at {record['pending']['at']} in progress", line)
        self.assertEqual(self.calls(), ["agent.prompt", "agent.prompt", "agent.restart", "agent.wait", "agent.prompt"])
        self.assertEqual(self.prompts()[-1], "You were restarted in place.\n")
        self.poll_under(FakeOmp(self))
        self.assertEqual(self.record()["loaded"], self.installed())
        self.assertNotIn("seats=", self.update().stdout)

    def test_a_change_after_the_seat_restarted_asks_again(self):
        self.loaded_now()
        self.push(self.instance_upstream, "projects/demo/AGENTS.md", "# Demo project\n")
        self.update()
        first = self.generation()
        self.restart_self()
        self.poll_under(FakeOmp(self))
        self.push(self.engine_upstream, "global.md", "# Global rules\n\nNew rule.\n")
        self.assertIn("demo:asked to restart-self (tstack/global.md)", self.update().stdout)
        self.assertNotEqual(self.generation(), first)
        self.assertEqual(self.prompts()[-1], self.asked(self.generation(), "tstack/global.md"))
        self.restart_self()
        self.poll_under(FakeOmp(self))
        self.assertEqual(self.record()["loaded"], self.installed())
        self.assertNotIn("asked", self.update().stdout)
        self.assertEqual(self.calls().count("agent.restart"), 2)

    def test_a_run_that_found_the_record_busy_loses_nothing(self):
        self.loaded_now()
        self.push(self.instance_upstream, "projects/demo/AGENTS.md", "# Demo project\n")
        self.env["TSTACK_LOCK_WAIT"] = "0.5"
        with open(self.run_dir / "recovery.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertIn("demo:recovery record busy", self.update().stdout)
        self.assertEqual(self.prompts(), [])
        self.assertIn("demo:asked to restart-self (agents/projects/demo/AGENTS.md)", self.update().stdout)

    def test_files_the_seat_does_not_load_never_ask(self):
        self.loaded_now()
        self.push(self.instance_upstream, "projects/demo/state/notes.md", "seat state\n")
        self.push(self.instance_upstream, "projects/other/notes.md", "another folder\n")
        self.assertNotIn("seats=", self.update().stdout)

    def test_only_a_wait_under_the_restarted_omp_confirms_the_restart(self):
        self.loaded_now()
        self.push(self.instance_upstream, "projects/demo/AGENTS.md", "# Demo project\n")
        self.update()
        self.env["EASL_TILE_ID"] = "obj_seat"
        old = FakeOmp(self)
        self.assertEqual(old.run("restart-self"), 0)
        self.poll_under(old)
        record = self.record()
        self.assertIn("pending", record)
        self.assertNotEqual(record["loaded"], self.installed())
        new = FakeOmp(self)
        self.poll_under(new)
        record = self.record()
        self.assertNotIn("pending", record)
        self.assertEqual((record["loaded"], record["loaded_by"]["omp"]), (self.installed(), new.proc.pid))
        self.assertNotIn("asked", self.update().stdout)

    def test_a_restart_self_that_died_before_its_restart_is_asked_again(self):
        self.loaded_now()
        self.push(self.instance_upstream, "projects/demo/AGENTS.md", "# Demo project\n")
        self.update()
        self.env["EASL_TILE_ID"] = "obj_seat"
        old = FakeOmp(self)
        (self.stubs / "easl-kill-caller").touch()
        self.assertEqual(old.run("restart-self"), -9)
        self.poll_under(old)
        self.assertNotEqual(self.record().get("loaded"), self.installed())
        # five minutes on, the omp from before the restart still polls: the restart never happened
        record = self.record()
        five_ago = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for key in ("pending", "restart"):
            if key in record:
                record[key]["at"] = five_ago
        (self.run_dir / "recovery.json").write_text(json.dumps(record))
        self.poll_under(old)
        line = self.update().stdout
        self.assertIn("never confirmed (the old session still polls)", line)
        self.assertIn("demo:asked to restart-self (agents/projects/demo/AGENTS.md)", line)

    def test_a_seat_whose_omp_takes_no_messages_is_never_typed_at(self):
        self.seat_tile(protocol=0)
        self.push(self.instance_upstream, "projects/demo/AGENTS.md", "# Demo project\n")
        self.assertIn("demo:restart-self not asked (its omp takes no messages now", self.update().stdout)
        self.assertEqual(self.prompts(), [])

    def test_restart_self_runs_only_in_the_seats_own_tile(self):
        result = self.cli("tstack", "restart-self", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("EASL_TILE_ID is unset", result.stderr)
        self.env["EASL_TILE_ID"] = "obj_other"
        result = self.cli("tstack", "restart-self", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is not the seat's tile", result.stderr)
        self.assertEqual(self.log_lines("easl.log"), [])

    def test_a_refused_restart_self_leaves_the_record_as_it_was(self):
        self.push(self.instance_upstream, "projects/demo/AGENTS.md", "# Demo project\n")
        self.update()
        (self.stubs / "easl-fail").touch()
        result = self.restart_self(check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("easl refused the restart", result.stderr)
        record = self.record()
        self.assertNotIn("pending", record)
        self.assertNotIn("restart", record)
        self.assertIn("demo:asked to restart-self", self.update().stdout)

    def test_update_sends_a_restart_prompt_liveness_left_owed(self):
        self.heartbeat(40)
        (self.stubs / "easl-wait-fail").touch()
        self.assertIn("restart prompt failed", self.cli("tstack", "check", "--liveness").stdout)
        (self.stubs / "easl-wait-fail").unlink()
        line = self.update().stdout
        self.assertIn("demo:restart prompt sent", line)
        self.assertIn("in progress", line)
        self.assertEqual(self.calls(), ["agent.restart", "agent.wait", "agent.wait", "agent.prompt"])

    def test_a_setup_failure_rolls_back_every_moved_repo_and_logs_it(self):
        engine_before, instance_before = git(self.engine, "rev-parse", "HEAD"), git(self.instance, "rev-parse", "HEAD")
        self.push(self.engine_upstream, "global.md", "# Global rules\n\nNew rule.\n")
        (self.instance_upstream / "skills" / "research").mkdir(parents=True)
        self.push(self.instance_upstream, "skills/research/SKILL.md", "---\nname: research\n---\n")
        result = self.update(check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("admission failed (setup exit 1: tstack: skills/research exists in both", result.stdout)
        self.assertIn("rollback: tstack->", result.stdout)
        self.assertEqual((git(self.engine, "rev-parse", "HEAD"), git(self.instance, "rev-parse", "HEAD")),
                         (engine_before, instance_before))
        self.assertNotIn("New rule.", (self.home / ".omp" / "agent" / "AGENTS.md").read_text())
        log = (self.home / ".local" / "state" / "tstack" / "update.log").read_text().splitlines()
        self.assertEqual(log, [result.stdout.strip()])

    def test_failing_check_resets_to_the_previous_commit(self):
        before = git(self.engine, "rev-parse", "HEAD")
        self.push(self.engine_upstream, "AGENTS.md", "# Engine\n\n## Size caps\n\n- `global.md`: 0 lines\n")
        result = self.update(check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("admission failed (check exit 1: size cap: global.md", result.stdout)
        self.assertIn("rollback: tstack->", result.stdout)
        self.assertEqual(git(self.engine, "rev-parse", "HEAD"), before)
        self.assertNotIn("restart", [c[0] for c in self.log_lines("easl.log")])

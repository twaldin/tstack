import subprocess

from support import Sandbox, git


class Update(Sandbox):
    def setUp(self):
        super().setUp()
        self.board()
        self.gitenv = {**self.env}
        self.engine_upstream = self.publish(self.engine, "engine")
        self.instance_upstream = self.publish(self.instance, "instance")
        self.cli("tstack", "setup", "--machine", "testbox", cwd=self.home)

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
        (work / path).write_text(text)
        for args in (["add", "-A"], ["commit", "--quiet", "-m", f"change {path}"], ["push", "--quiet"]):
            subprocess.run(["git", "-C", str(work), *args], check=True, capture_output=True, env=self.gitenv)

    def update(self, check=True):
        return self.cli("tstack", "update", cwd=self.home, check=check)

    def test_fast_forward_reruns_setup_and_restarts_the_seat(self):
        before = git(self.engine, "rev-parse", "HEAD")
        self.push(self.engine_upstream, "global.md", "# Global rules\n\nNew rule.\n")
        line = self.update().stdout
        after = git(self.engine, "rev-parse", "HEAD")
        self.assertNotEqual(before, after)
        self.assertIn(f"tstack={before[:7]}..{after[:7]}", line)
        self.assertIn("setup=ok check=ok", line)
        self.assertIn("restarts=demo:restarted(tstack/global.md changed)", line)
        self.assertIn("New rule.", (self.home / ".omp" / "agent" / "AGENTS.md").read_text())
        self.assertEqual([c[0] for c in self.log_lines("easl.log")], ["agent.list", "agent.restart", "agent.prompt"])
        log = (self.home / ".local" / "state" / "tstack" / "update.log").read_text().splitlines()
        self.assertEqual(log, [line.strip()])

    def test_busy_seat_restart_is_retried_next_run(self):
        self.push(self.instance_upstream, "projects/demo/AGENTS.md", "# Demo project\n")
        (self.stubs / "easl-busy").touch()
        self.assertIn("demo:busy,retry", self.update().stdout)
        (self.stubs / "easl-busy").unlink()
        self.assertIn("demo:restarted(agents/projects/demo/AGENTS.md changed)", self.update().stdout)

    def test_failing_check_resets_to_the_previous_commit(self):
        before = git(self.engine, "rev-parse", "HEAD")
        self.push(self.engine_upstream, "AGENTS.md", "# Engine\n\n## Size caps\n\n- `global.md`: 0 lines\n")
        result = self.update(check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("check failed", result.stdout)
        self.assertIn("reset tstack", result.stdout)
        self.assertEqual(git(self.engine, "rev-parse", "HEAD"), before)
        self.assertNotIn("restart", [c[0] for c in self.log_lines("easl.log")])

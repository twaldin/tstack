import os, subprocess, tempfile, unittest
from pathlib import Path

BUMP = Path(__file__).resolve().parents[1] / "bin" / "pstack-bump"
ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
       "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def run(cwd, *args):
    return subprocess.run(args, cwd=cwd, env=ENV, capture_output=True, text=True)


def commit(repo, files):
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    run(repo, "git", "add", "-A")
    run(repo, "git", "commit", "-qm", "c")
    return run(repo, "git", "rev-parse", "HEAD").stdout.strip()


class PstackBump(unittest.TestCase):
    def test_bump_takes_upstream_keeps_drops_and_reports_edited_hunks(self):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        up, eng = tmp / "plugins", tmp / "engine"
        for repo in (up, eng):
            repo.mkdir()
            run(repo, "git", "init", "-q", "-b", "main")
        commit(eng, {"README.md": "engine\n"})
        v1 = commit(up, {"pstack/how.md": "a\n", "pstack/babysit.md": "old watcher\n", "pstack/docs/g.md": "g\n", "x/y.md": "y\n"})
        self.assertEqual(run(eng, str(BUMP), v1, str(up)).returncode, 0)
        run(eng, "git", "rm", "-qr", "pstack/docs")
        commit(eng, {"pstack/DROPPED": "docs/*\n", "pstack/babysit.md": "tstack watch-pr\n"})
        v2 = commit(up, {"pstack/how.md": "b\n", "pstack/babysit.md": "new watcher\n", "pstack/docs/g.md": "g2\n", "pstack/docs/n.md": "n\n"})
        out = run(eng, str(BUMP), v2, str(up))
        conflicts = [line for line in out.stdout.splitlines() if line.startswith("conflict:")]
        self.assertEqual((out.returncode, conflicts), (1, ["conflict: pstack/babysit.md"]), out.stdout + out.stderr)
        self.assertEqual((eng / "pstack/how.md").read_text(), "b\n")
        self.assertEqual(run(eng, "git", "ls-files", "pstack/docs").stdout, "")
        self.assertFalse((eng / "x").exists())
        self.assertIn("pstack/docs/n.md", run(eng, "git", "ls-tree", "-r", "--name-only", "pstack-upstream").stdout)


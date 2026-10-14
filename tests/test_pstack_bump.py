import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

BUMP = Path(__file__).resolve().parents[1] / "bin" / "pstack-bump"
ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, env=ENV, check=True, capture_output=True, text=True).stdout.strip()


def write(repo, rel, text):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def commit(repo, message):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


class PstackBump(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        self.upstream, self.engine = tmp / "plugins", tmp / "engine"
        for repo in (self.upstream, self.engine):
            repo.mkdir()
            git(repo, "init", "-q", "-b", "main")
        write(self.upstream, "pstack/skills/how/SKILL.md", "one\ntwo\n")
        write(self.upstream, "pstack/playbooks/babysit.md", "step 5\nstep 6 runs the old watcher\nstep 7\n")
        write(self.upstream, "pstack/docs/guide.md", "guide\n")
        write(self.upstream, "other-plugin/README.md", "not pstack\n")
        self.first = commit(self.upstream, "v1")
        write(self.engine, "README.md", "engine\n")
        commit(self.engine, "engine")

    def bump(self, ref):
        return subprocess.run([str(BUMP), ref, str(self.upstream)], cwd=self.engine, env=ENV, capture_output=True, text=True)

    def test_bump_merges_upstream_drops_again_and_reports_our_conflicts(self):
        imported = self.bump(self.first)
        self.assertEqual(imported.returncode, 0, imported.stderr)
        self.assertEqual((self.engine / "pstack/skills/how/SKILL.md").read_text(), "one\ntwo\n")
        self.assertFalse((self.engine / "other-plugin").exists())

        write(self.engine, "pstack/DROPPED", "# not shipped\ndocs/*\n")
        git(self.engine, "rm", "-q", "-r", "pstack/docs")
        write(self.engine, "pstack/playbooks/babysit.md", "step 5\nstep 6 runs tstack watch-pr\nstep 7\n")
        commit(self.engine, "fork: drop docs, edit babysit")

        write(self.upstream, "pstack/skills/how/SKILL.md", "one\nTWO\n")
        write(self.upstream, "pstack/playbooks/babysit.md", "step 5\nstep 6 runs the new watcher\nstep 7\n")
        write(self.upstream, "pstack/docs/guide.md", "guide v2\n")
        write(self.upstream, "pstack/docs/new.md", "new page\n")
        second = commit(self.upstream, "v2")

        bumped = self.bump(second)
        self.assertEqual(bumped.returncode, 1, bumped.stderr)
        self.assertIn("conflict: pstack/playbooks/babysit.md", bumped.stdout)
        self.assertNotIn("conflict: pstack/docs", bumped.stdout)
        self.assertIn(f"pstack-upstream is cursor/plugins {second}", bumped.stdout)
        self.assertEqual((self.engine / "pstack/skills/how/SKILL.md").read_text(), "one\nTWO\n")
        self.assertEqual(git(self.engine, "ls-files", "pstack/docs"), "")
        self.assertFalse((self.engine / "pstack/docs").exists())
        snapshot = git(self.engine, "ls-tree", "-r", "--name-only", "pstack-upstream").split()
        self.assertIn("pstack/docs/new.md", snapshot)
        self.assertNotIn("other-plugin/README.md", snapshot)
        self.assertEqual(git(self.engine, "rev-list", "--count", "pstack-upstream"), "2")


if __name__ == "__main__":
    unittest.main()

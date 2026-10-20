import datetime as dt
import json
from pathlib import Path

from support import Sandbox, git


class CleanGates(Sandbox):
    def setUp(self):
        super().setUp()
        origin = self.root / "github.com/acme/demo.git"
        self.main = self.clone(origin, self.home / "worktrees/demo/main", {
            "README": "demo\n", ".gitignore": "node_modules/\n.local/\n.cache/\n*.proof\n",
        })
        self.tree = self.home / "worktrees/demo/finished"
        git(self.main, "worktree", "add", "-b", "lane/finished", str(self.tree))
        self.head = git(self.tree, "rev-parse", "HEAD")
        self.lane = self.seat / "state/lanes/finished/lane.json"
        self.lane.parent.mkdir(parents=True)
        self.lane.write_text(json.dumps({
            "id": "finished", "project": "demo", "repo": "acme/demo", "branch": "lane/finished",
            "base": "main", "worktree": str(self.tree), "cleanup_repo": str(self.main / ".git"),
            "status": "exited", "runs": [], "merge": "f" * 40, "merged_head": self.head,
        }))
        self.pr = {"url": "https://github.com/acme/demo/pull/7", "state": "MERGED",
                   "headRefName": "lane/finished", "headRefOid": self.head,
                   "mergedAt": "2020-01-01T00:00:00Z", "closedAt": "2020-01-01T00:00:00Z",
                   "updatedAt": "2020-01-01T00:00:00Z"}

    def inventory(self, **fields):
        self.pr.update(fields)
        (self.stubs / "pr-list.json").write_text(json.dumps([self.pr]))

    def ignored(self, name):
        path = self.tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("proof\n")
        return path

    def test_recorded_terminal_tree_has_no_age_delay_and_removes_without_force(self):
        self.ignored("node_modules/pkg/cache")
        self.ignored(".cache/artifact")
        self.inventory(mergedAt=dt.datetime.now(dt.timezone.utc).isoformat())
        result = self.cli("lane", "clean", "finished")
        self.assertIn(f"removed worktree {self.tree}", result.stdout)
        self.assertFalse(self.tree.exists())
        self.assertEqual(git(self.main, "branch", "--list", "lane/finished"), "")

    def test_ignored_parent_with_only_node_modules_is_removed(self):
        (self.main / ".git/info/exclude").write_text("scripts/\n")
        self.ignored("scripts/node_modules/pkg/cache")
        result = self.cli("lane", "clean", "finished")
        self.assertIn(f"removed worktree {self.tree}", result.stdout)
        self.assertFalse(self.tree.exists())

    def test_ignored_parent_with_node_modules_and_other_file_is_preserved(self):
        (self.main / ".git/info/exclude").write_text("scripts/\n")
        cache = self.ignored("scripts/node_modules/x")
        keep = self.ignored("scripts/keep.txt")
        result = self.cli("lane", "clean", "finished")
        self.assertIn("gate 4: ignored paths are not regenerable", result.stdout)
        self.assertEqual(keep.read_text(), "proof\n")
        self.assertEqual(cache.read_text(), "proof\n")

    def test_tracked_cache_named_parent_does_not_authorize_ignored_files(self):
        self.ignored("build/README")
        git(self.tree, "add", "build/README")
        git(self.tree, "commit", "-m", "track build directory")
        self.lane.unlink()
        self.inventory(headRefOid=git(self.tree, "rev-parse", "HEAD"))
        (self.main / ".git/info/exclude").write_text(".env\nsecrets/\n")
        for name in ("build/.env", "build/secrets/key"):
            with self.subTest(name=name):
                path = self.ignored(name)
                result = self.cli("lane", "clean", "--list")
                self.assertIn("gate 4: ignored paths are not regenerable", result.stdout)
                path.unlink()

    def test_head_mismatch_preserves_tree(self):
        git(self.tree, "commit", "--allow-empty", "-m", "unpublished")
        result = self.cli("lane", "clean", "finished")
        self.assertIn("gate 2: HEAD differs from PR head", result.stdout)
        self.assertEqual(git(self.tree, "log", "-1", "--format=%s"), "unpublished")

    def test_dirty_status_preserves_untracked_file(self):
        path = self.tree / "untracked"
        path.write_text("keep\n")
        self.assertIn("gate 3: dirty worktree", self.cli("lane", "clean", "finished").stdout)
        self.assertEqual(path.read_text(), "keep\n")

    def test_ignored_proof_preserved_even_beside_allowed_cache(self):
        path = self.ignored(".local/acceptance/result.proof")
        self.ignored(".local/acceptance/build/cache")
        self.ignored("node_modules/pkg/cache")
        self.assertIn("gate 4:", self.cli("lane", "clean", "finished").stdout)
        self.assertEqual(path.read_text(), "proof\n")

    def test_ignored_directory_leading_space_is_not_a_cache(self):
        ignore = self.tree / ".gitignore"
        ignore.write_text(ignore.read_text() + " node_modules/\n")
        git(self.tree, "add", ".gitignore")
        git(self.tree, "commit", "-m", "ignore distinct directory")
        record = json.loads(self.lane.read_text())
        record["merged_head"] = git(self.tree, "rev-parse", "HEAD")
        self.lane.write_text(json.dumps(record))
        path = self.ignored(" node_modules/precious.txt")
        self.assertIn("gate 4:", self.cli("lane", "clean", "finished").stdout)
        self.assertEqual(path.read_text(), "proof\n")

    def test_process_inside_tree_preserved_on_linux_and_macos(self):
        for platform in ("linux", "darwin"):
            with self.subTest(platform=platform):
                (self.stubs / "lsof.out").write_text(f"p123\nn{self.tree}/child\n")
                result = self.cli_platform(platform, "lane", "clean", "finished")
                self.assertIn("gate 5: process cwd inside worktree, PIDs 123", result.stdout)
                self.assertTrue(self.tree.exists())

    def test_process_with_same_path_prefix_does_not_block(self):
        (self.stubs / "lsof.out").write_text(f"p123\nn{self.tree}-other\n")
        self.cli("lane", "clean", "finished")
        self.assertFalse(self.tree.exists())

    def test_process_inspection_error_preserves_tree(self):
        (self.stubs / "lsof.err").write_text("permission denied")
        self.assertIn("gate 5: process inspection failed:", self.cli("lane", "clean", "finished").stdout)
        self.assertTrue(self.tree.exists())

    def test_listing_reports_all_failed_gates_without_mutation(self):
        self.lane.unlink()
        self.inventory(headRefOid="a" * 40, mergedAt=dt.datetime.now(dt.timezone.utc).isoformat())
        (self.tree / "untracked").write_text("keep\n")
        proof = self.ignored(".local/acceptance/result.proof")
        (self.stubs / "lsof.out").write_text(f"p123\nn{self.tree}\n")
        before = git(self.main, "worktree", "list", "--porcelain")
        output = self.cli("lane", "clean", "--list").stdout
        self.assertIn(f"{self.tree}\tMERGED\t{self.pr['url']}", output)
        for gate in range(1, 6):
            self.assertIn(f"gate {gate}:", output)
        self.assertIn("unrecorded: 1, finished: 1, eligible: 0, blocked: 1, errors: 0", output)
        self.assertEqual(git(self.main, "worktree", "list", "--porcelain"), before)
        self.assertEqual(proof.read_text(), "proof\n")
        self.assertFalse(self.lane.exists())

    def test_listing_terminal_old_tree_passes_all_gates_on_both_platforms(self):
        self.lane.unlink()
        for state in ("MERGED", "CLOSED"):
            self.inventory(state=state, mergedAt=None if state == "CLOSED" else "2020-01-01T00:00:00Z")
            for platform in ("linux", "darwin"):
                with self.subTest(state=state, platform=platform):
                    output = self.cli_platform(platform, "lane", "clean", "--list").stdout
                    self.assertIn("all gates pass", output)
                    self.assertIn("finished: 1, eligible: 1, blocked: 0, errors: 0", output)
                    self.assertTrue(self.tree.exists())

    def test_listing_excludes_recorded_lanes_in_other_seats_and_open_prs(self):
        other = self.home / "agents/projects/demo/state/lanes/finished/lane.json"
        other.parent.mkdir(parents=True)
        self.lane.replace(other)
        self.inventory()
        self.assertEqual(self.cli("lane", "clean", "--list").stdout,
                         "unrecorded: 0, finished: 0, eligible: 0, blocked: 0, errors: 0\n")
        other.unlink()
        self.inventory(state="OPEN")
        self.assertEqual(self.cli("lane", "clean", "--list").stdout,
                         "unrecorded: 1, finished: 0, eligible: 0, blocked: 0, errors: 0\n")

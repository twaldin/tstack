import datetime as dt
import os

from support import Sandbox


class Setup(Sandbox):
    def setUp(self):
        super().setUp()
        omp = self.home / ".omp" / "agent"
        self.omp = omp
        elsewhere = self.home / ".agents" / "skills"
        (elsewhere / "old-skill").mkdir(parents=True)
        (elsewhere / "old-skill" / "SKILL.md").write_text("old\n")
        (omp / "agents").mkdir(parents=True)
        (omp / "agents" / "foreign.md").write_text("# foreign\n")
        os.symlink(elsewhere, omp / "skills")
        (omp / "extensions").mkdir()
        os.symlink(self.root / "easl-app" / "easl.ts", omp / "extensions" / "easl.ts")
        (omp / "extensions" / "herdr.ts").write_text("// foreign\n")
        (omp / "AGENTS.md").write_text("# hand-written global rules\n")
        (omp / "rules").mkdir()
        (omp / "rules" / "style.md").write_text("old rule\n")
        (self.instance / "skills" / "writing").mkdir(parents=True)
        (self.instance / "skills" / "writing" / "SKILL.md").write_text("---\nname: writing\n---\n")
        (self.instance / "machines" / "testbox" / "global.md").write_text("# Machine rules\n")
        (self.home / ".local" / "bin").mkdir(parents=True)
        (self.home / ".local" / "bin" / "lane").write_text("#!/bin/sh\necho old lane\n")
        (self.home / ".local" / "bin" / "unrelated").write_text("#!/bin/sh\n")
        self.quarantine = self.home / ".cleanup-quarantine" / dt.date.today().isoformat()

    def test_setup_installs_quarantines_and_is_idempotent(self):
        out = self.cli("tstack", "setup", "--machine", "testbox", cwd=self.home).stdout
        self.assertIn("quarantine", out)
        agents_md = (self.omp / "AGENTS.md").read_text()
        for source in (self.engine / "global.md", self.instance / "global.md", self.instance / "machines/testbox/global.md"):
            self.assertIn(f"<!-- source: {source} -->", agents_md)
        self.assertIn("# Machine rules", agents_md)
        skills = self.omp / "skills"
        self.assertTrue(skills.is_dir() and not skills.is_symlink())
        self.assertEqual(sorted(p.name for p in skills.iterdir()), ["research", "writing"])
        self.assertEqual(os.readlink(skills / "research"), str(self.engine / "skills" / "research"))
        self.assertEqual(sorted(p.name for p in (self.omp / "agents").iterdir()), ["scout.md"])
        self.assertEqual(sorted(p.name for p in (self.omp / "extensions").iterdir()), ["easl.ts", "secret-guard.ts"])
        self.assertEqual(os.readlink(self.omp / "extensions" / "easl.ts"), str(self.root / "easl-app" / "easl.ts"))
        self.assertEqual(list((self.omp / "rules").iterdir()), [])
        local_bin = self.home / ".local" / "bin"
        self.assertEqual(os.readlink(local_bin / "text"), str(self.instance / "bin" / "text"))
        self.assertEqual(os.readlink(local_bin / "tstack"), str(self.engine / "bin" / "tstack"))
        self.assertTrue((local_bin / "unrelated").is_file())

        q = self.quarantine
        self.assertEqual((q / ".omp/agent/AGENTS.md").read_text(), "# hand-written global rules\n")
        self.assertEqual((q / ".omp/agent/agents/foreign.md").read_text(), "# foreign\n")
        self.assertEqual((q / ".omp/agent/extensions/herdr.ts").read_text(), "// foreign\n")
        self.assertEqual((q / ".omp/agent/rules/style.md").read_text(), "old rule\n")
        self.assertEqual(os.readlink(q / ".omp/agent/skills"), str(self.home / ".agents" / "skills"))
        self.assertEqual((q / ".local/bin/lane").read_text(), "#!/bin/sh\necho old lane\n")
        self.assertTrue((self.home / ".agents" / "skills" / "old-skill" / "SKILL.md").exists())
        manifest = (q / "MANIFEST.tsv").read_text().splitlines()
        self.assertEqual(len(manifest), 6)
        self.assertTrue(all(len(line.split("\t")) == 4 for line in manifest))

        again = self.cli("tstack", "setup", "--machine", "testbox", cwd=self.home).stdout
        self.assertEqual(again.strip(), "tstack setup: testbox is current")
        self.assertEqual(len((q / "MANIFEST.tsv").read_text().splitlines()), 6)
        check = self.cli("tstack", "check", "--machine", "testbox", cwd=self.home, check=False).stdout
        self.assertNotIn("setup not current", check)

    def test_check_flags_foreign_entries_and_dry_run_changes_nothing(self):
        self.cli("tstack", "setup", "--machine", "testbox", cwd=self.home)
        (self.omp / "skills" / "stray").mkdir()
        (self.omp / "commands").mkdir()
        (self.omp / "commands" / "deploy.md").write_text("x\n")
        check = self.cli("tstack", "check", "--machine", "testbox", cwd=self.home, check=False)
        self.assertEqual(check.returncode, 1)
        self.assertIn(f"setup not current: quarantine {self.omp / 'skills' / 'stray'}", check.stdout)
        self.assertIn(f"quarantine {self.omp / 'commands' / 'deploy.md'}", check.stdout)
        dry = self.cli("tstack", "setup", "--machine", "testbox", "--dry-run", cwd=self.home).stdout
        self.assertIn("would quarantine", dry)
        self.assertTrue((self.omp / "skills" / "stray").is_dir())

    def test_check_flags_bad_seat_state_size_caps_and_personal_data(self):
        self.cli("tstack", "setup", "--machine", "testbox", cwd=self.home)
        (self.seat / "state" / "tasks.tsv").write_text("id\ttitle\n")
        (self.engine / "AGENTS.md").write_text("# Engine\n\n## Size caps\n\n- `global.md`: 1 lines\n")
        someone = "/" + "home/someone"
        (self.engine / "global.md").write_text(f"# Global rules\n\nWrite to {someone}/x.\n")
        out = self.cli("tstack", "check", "--machine", "testbox", cwd=self.home, check=False).stdout
        self.assertIn("tasks.tsv header", out)
        self.assertIn("size cap: global.md has 3 lines, cap 1", out)
        self.assertIn(f"personal data: global.md:3: '{someone}'", out)
        mail = "owner" + "@" + "mail.acme.dev"
        (self.engine / "global.md").write_text(f"# Global rules\n\nMail {mail}, not nobody@example.org.\n")
        out = self.cli("tstack", "check", "--machine", "testbox", cwd=self.home, check=False).stdout
        self.assertIn(f"personal data: global.md:3: '{mail}'", out)
        self.assertNotIn("example.org", out)
        self.assertNotIn("launchd", out)

    def test_home_ancestor_paths_are_quarantined_and_enabled_providers_flagged(self):
        (self.home / ".omp" / "skills" / "stale").mkdir(parents=True)
        (self.home / "AGENTS.md").write_text("# home rules\n")
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / "CLAUDE.md").write_text("# claude rules\n")
        (self.home / ".claude" / "APPEND_SYSTEM.md").write_text("always\n")
        self.cli("tstack", "setup", "--machine", "testbox", cwd=self.home)
        self.assertFalse((self.home / ".omp" / "skills").exists())
        self.assertFalse((self.home / "AGENTS.md").exists())
        self.assertFalse((self.home / ".claude" / "APPEND_SYSTEM.md").exists())
        self.assertTrue((self.quarantine / ".omp" / "skills" / "stale").is_dir())
        self.assertTrue((self.home / ".claude" / "CLAUDE.md").exists(), "provider-gated roots stay with their harness")
        flagged = f"foreign: omp loads {self.home / '.claude' / 'CLAUDE.md'} (provider claude"
        self.assertIn(flagged, self.cli("tstack", "check", "--machine", "testbox", cwd=self.home, check=False).stdout)
        (self.omp / "config.yml").write_text(
            "theme: dark\ndisabledProviders:\n  - claude\n  - agents\n  - path: /somewhere\n    providers:\n      - agents-md\nskills:\n  enabled: true\n")
        out = self.cli("tstack", "check", "--machine", "testbox", cwd=self.home, check=False).stdout
        self.assertNotIn("foreign:", out)

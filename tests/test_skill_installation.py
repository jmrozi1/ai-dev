from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import unittest

from ai_dev_flow.skill_installation import (
    discover_skill_packages,
    install_skill_packages,
    SkillInstallationError,
    resolve_copilot_skills_root,
    resolve_skill_installation_ownership_path,
)


class SkillInstallationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.repo_root = self.tmp_path / "repo"
        self.repo_root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _write_skill(self, name: str, *, content: str = "# Skill\n") -> Path:
        skill_dir = self.repo_root / "skills" / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
        return skill_dir

    def _install(self, destination_root: Path, *, home: Path) -> object:
        return install_skill_packages(
            repo_root=self.repo_root,
            destination_root=destination_root,
            home=home,
        )

    def _read_owned_skills(self, *, home: Path) -> dict[str, str]:
        ownership_path = resolve_skill_installation_ownership_path(home=home)
        record = json.loads(ownership_path.read_text(encoding="utf-8"))
        return record["owned_skills"]

    def test_discovery_finds_only_real_top_level_skill_packages(self) -> None:
        self._write_skill("flow", content="# Flow\n")
        self._write_skill("frontend-design-review", content="# Front-end\n")
        nested = self.repo_root / "skills" / "documentation" / "example"
        nested.mkdir(parents=True, exist_ok=True)
        (nested / "SKILL.md").write_text("# Nested\n", encoding="utf-8")
        (self.repo_root / "skills" / "README.md").write_text("# Index\n", encoding="utf-8")
        (self.repo_root / "skills" / "index.md").write_text("# Catalog\n", encoding="utf-8")

        packages = discover_skill_packages(self.repo_root)

        self.assertEqual(len(packages), 2)
        self.assertEqual(
            [package.name for package in packages],
            ["flow", "frontend-design-review"],
        )
        self.assertEqual(packages[0].source_directory, self.repo_root / "skills" / "flow")
        self.assertEqual(
            packages[1].source_directory,
            self.repo_root / "skills" / "frontend-design-review",
        )

    def test_repository_discovery_matches_real_top_level_skill_packages(self) -> None:
        packages = discover_skill_packages(Path(__file__).resolve().parents[1])

        self.assertEqual(
            [package.name for package in packages],
            ["executor", "flow", "frontend-design-review", "orchestrator"],
        )

    def test_default_destination_root_is_agents_skills(self) -> None:
        home = self.tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)

        destination = resolve_copilot_skills_root(home=home)

        self.assertEqual(destination, home.resolve() / ".agents" / "skills")

    def test_first_install_installs_discovered_skill(self) -> None:
        self._write_skill("flow", content="# Flow\n")
        destination = self.tmp_path / "dest"
        home = self.tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)

        result = self._install(destination, home=home)

        self.assertEqual(result.discovered_count, 1)
        self.assertEqual(result.installed_count, 1)
        self.assertEqual(result.updated_count, 0)
        self.assertEqual(result.unchanged_count, 0)
        self.assertEqual(result.statuses[0].state, "installed")
        installed = destination / "flow"
        self.assertTrue(installed.is_symlink())
        self.assertEqual(installed.resolve(), (self.repo_root / "skills" / "flow").resolve())

    def test_repeat_install_is_idempotent(self) -> None:
        self._write_skill("flow", content="# Flow\n")
        destination = self.tmp_path / "dest"
        home = self.tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)

        first = self._install(destination, home=home)
        second = self._install(destination, home=home)

        self.assertEqual(first.installed_count, 1)
        self.assertEqual(second.installed_count, 0)
        self.assertEqual(second.updated_count, 0)
        self.assertEqual(second.unchanged_count, 1)
        self.assertEqual(second.statuses[0].state, "unchanged")

    def test_managed_stale_or_broken_link_is_repaired(self) -> None:
        self._write_skill("flow", content="# Flow\n")
        destination = self.tmp_path / "dest"
        home = self.tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)

        self._install(destination, home=home)

        installed = destination / "flow"
        stale_target = home / "missing-stale-target"
        installed.unlink()
        installed.symlink_to(stale_target)

        ownership_path = resolve_skill_installation_ownership_path(home=home)
        record = json.loads(ownership_path.read_text(encoding="utf-8"))
        record["owned_skills"] = {
            str(installed.absolute()): f"symlink:{stale_target.resolve()}"
        }
        ownership_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

        result = self._install(destination, home=home)

        self.assertEqual(result.installed_count, 0)
        self.assertEqual(result.updated_count, 1)
        self.assertEqual(result.unchanged_count, 0)
        self.assertEqual(result.statuses[0].state, "updated")
        self.assertTrue(installed.is_symlink())
        self.assertEqual(installed.resolve(), (self.repo_root / "skills" / "flow").resolve())

    def test_retargeted_managed_symlink_fails_closed_and_is_preserved(self) -> None:
        self._write_skill("flow", content="# Flow\n")
        destination = self.tmp_path / "dest"
        home = self.tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)

        self._install(destination, home=home)

        installed = destination / "flow"
        unrelated_target = home / "unrelated-target"
        unrelated_target.write_text("external\n", encoding="utf-8")
        installed.unlink()
        installed.symlink_to(unrelated_target)

        with self.assertRaisesRegex(SkillInstallationError, "conflicting unmanaged or divergent symlink"):
            self._install(destination, home=home)

        self.assertTrue(installed.is_symlink())
        self.assertEqual(installed.resolve(), unrelated_target.resolve())

    def test_unrelated_entries_are_preserved(self) -> None:
        self._write_skill("flow", content="# Flow\n")
        destination = self.tmp_path / "dest"
        home = self.tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)

        unrelated_dir = destination / "external-dir"
        unrelated_file = destination / "external-file"
        unrelated_link = destination / "external-link"
        unrelated_dir.mkdir(parents=True, exist_ok=True)
        (unrelated_dir / "SKILL.md").write_text("# External\n", encoding="utf-8")
        unrelated_file.write_text("keep\n", encoding="utf-8")
        unrelated_link.symlink_to(unrelated_file)

        result = self._install(destination, home=home)

        self.assertEqual(result.discovered_count, 1)
        self.assertTrue((unrelated_dir / "SKILL.md").exists())
        self.assertEqual(unrelated_file.read_text(encoding="utf-8"), "keep\n")
        self.assertTrue(unrelated_link.is_symlink())

    def test_existing_non_symlink_conflicts_fail_closed(self) -> None:
        self._write_skill("flow", content="# Flow\n")
        home = self.tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)

        for case in ("directory", "file"):
            with self.subTest(case=case):
                destination = self.tmp_path / f"dest-{case}"
                conflict = destination / "flow"
                destination.mkdir(parents=True, exist_ok=True)
                if case == "directory":
                    conflict.mkdir(parents=True, exist_ok=True)
                    (conflict / "placeholder.txt").write_text("user\n", encoding="utf-8")
                else:
                    conflict.write_text("user\n", encoding="utf-8")

                with self.assertRaisesRegex(SkillInstallationError, "is not a symlink"):
                    self._install(destination, home=home)

    def test_unmanaged_symlink_conflict_fails_closed(self) -> None:
        self._write_skill("flow", content="# Flow\n")
        destination = self.tmp_path / "dest"
        home = self.tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)

        destination.mkdir(parents=True, exist_ok=True)
        conflict = destination / "flow"
        unrelated_target = home / "unmanaged-target"
        unrelated_target.write_text("external\n", encoding="utf-8")
        conflict.symlink_to(unrelated_target)

        with self.assertRaisesRegex(SkillInstallationError, "conflicting unmanaged or divergent symlink"):
            self._install(destination, home=home)

    def test_previously_managed_removed_skill_is_safely_removed(self) -> None:
        self._write_skill("flow", content="# Flow\n")
        removed_skill = self._write_skill("legacy", content="# Legacy\n")
        destination = self.tmp_path / "dest"
        home = self.tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)

        self._install(destination, home=home)
        installed_legacy = destination / "legacy"
        self.assertTrue(installed_legacy.is_symlink())

        shutil.rmtree(removed_skill)
        result = self._install(destination, home=home)

        removed_statuses = [status for status in result.statuses if status.destination_directory == installed_legacy]
        self.assertEqual(len(removed_statuses), 1)
        self.assertEqual(removed_statuses[0].state, "removed")
        self.assertFalse(installed_legacy.exists())
        self.assertNotIn(str(installed_legacy), self._read_owned_skills(home=home))

    def test_already_absent_removed_skill_clears_ownership(self) -> None:
        self._write_skill("flow", content="# Flow\n")
        removed_skill = self._write_skill("legacy", content="# Legacy\n")
        destination = self.tmp_path / "dest"
        home = self.tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)

        self._install(destination, home=home)
        installed_legacy = destination / "legacy"
        self.assertTrue(installed_legacy.is_symlink())

        shutil.rmtree(removed_skill)
        installed_legacy.unlink()

        result = self._install(destination, home=home)

        absent_statuses = [status for status in result.statuses if status.destination_directory == installed_legacy]
        self.assertEqual(len(absent_statuses), 1)
        self.assertEqual(absent_statuses[0].state, "already-absent")
        self.assertNotIn(str(installed_legacy), self._read_owned_skills(home=home))

    def test_divergent_formerly_managed_removed_skill_fails_closed_and_is_preserved(self) -> None:
        self._write_skill("flow", content="# Flow\n")
        removed_skill = self._write_skill("legacy", content="# Legacy\n")
        destination = self.tmp_path / "dest"
        home = self.tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)

        self._install(destination, home=home)
        installed_legacy = destination / "legacy"

        shutil.rmtree(removed_skill)
        divergent_target = home / "divergent-legacy-target"
        divergent_target.write_text("external\n", encoding="utf-8")
        installed_legacy.unlink()
        installed_legacy.symlink_to(divergent_target)

        with self.assertRaisesRegex(SkillInstallationError, "symlink target diverged"):
            self._install(destination, home=home)

        self.assertTrue(installed_legacy.is_symlink())
        self.assertEqual(installed_legacy.resolve(), divergent_target.resolve())

    def test_unrelated_unowned_skill_remains_untouched_during_obsolete_cleanup(self) -> None:
        self._write_skill("flow", content="# Flow\n")
        removed_skill = self._write_skill("legacy", content="# Legacy\n")
        destination = self.tmp_path / "dest"
        home = self.tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)

        self._install(destination, home=home)

        unrelated = destination / "external-unowned"
        unrelated.mkdir(parents=True, exist_ok=True)
        (unrelated / "SKILL.md").write_text("# External\n", encoding="utf-8")

        shutil.rmtree(removed_skill)
        self._install(destination, home=home)

        self.assertTrue(unrelated.exists())
        self.assertTrue((unrelated / "SKILL.md").exists())


if __name__ == "__main__":
    unittest.main()

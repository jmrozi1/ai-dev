from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
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

    def test_discovery_finds_shared_and_audience_skill_packages(self) -> None:
        self._write_skill("frontend-design-review", content="# Front-end\n")
        chatgpt_skill = self.repo_root / "skills" / "chatgpt" / "orchestrator"
        chatgpt_skill.mkdir(parents=True, exist_ok=True)
        (chatgpt_skill / "SKILL.md").write_text("# ChatGPT orchestration\n", encoding="utf-8")
        copilot_skill = self.repo_root / "skills" / "copilot" / "executor"
        copilot_skill.mkdir(parents=True, exist_ok=True)
        (copilot_skill / "SKILL.md").write_text("# Copilot execution\n", encoding="utf-8")
        chatgpt_flow = self.repo_root / "skills" / "chatgpt" / "flow"
        chatgpt_flow.mkdir(parents=True, exist_ok=True)
        (chatgpt_flow / "SKILL.md").write_text("# ChatGPT Flow\n", encoding="utf-8")
        copilot_flow = self.repo_root / "skills" / "copilot" / "flow"
        copilot_flow.mkdir(parents=True, exist_ok=True)
        (copilot_flow / "SKILL.md").write_text("# Copilot Flow\n", encoding="utf-8")
        nested = self.repo_root / "skills" / "documentation" / "example"
        nested.mkdir(parents=True, exist_ok=True)
        (nested / "SKILL.md").write_text("# Nested\n", encoding="utf-8")
        (self.repo_root / "skills" / "README.md").write_text("# Index\n", encoding="utf-8")
        (self.repo_root / "skills" / "index.md").write_text("# Catalog\n", encoding="utf-8")

        packages = discover_skill_packages(self.repo_root)

        self.assertEqual(len(packages), 5)
        self.assertEqual(
            [package.name for package in packages],
            ["executor", "flow", "flow", "frontend-design-review", "orchestrator"],
        )
        self.assertEqual(packages[0].source_directory, copilot_skill)
        self.assertEqual(packages[1].source_directory, chatgpt_flow)
        self.assertEqual(packages[2].source_directory, copilot_flow)
        self.assertEqual(packages[4].source_directory, chatgpt_skill)

    def test_discovery_does_not_scan_obsolete_or_arbitrary_paths(self) -> None:
        obsolete = self.repo_root / "skills" / "work-agent-skills" / "legacy"
        obsolete.mkdir(parents=True, exist_ok=True)
        (obsolete / "SKILL.md").write_text("# Legacy\n", encoding="utf-8")
        arbitrary = self.repo_root / "skills" / "other" / "nested"
        arbitrary.mkdir(parents=True, exist_ok=True)
        (arbitrary / "SKILL.md").write_text("# Arbitrary\n", encoding="utf-8")

        self.assertEqual(discover_skill_packages(self.repo_root), ())

    def test_each_audience_install_set_has_unique_names(self) -> None:
        source_repo = Path(__file__).resolve().parents[1]

        for audience in ("chatgpt", "copilot"):
            with self.subTest(audience=audience):
                names = [
                    package.name
                    for package in discover_skill_packages(source_repo, audience=audience)
                ]
                self.assertEqual(len(names), len(set(names)))

    def test_ticket_creation_skill_is_discoverable_for_chatgpt(self) -> None:
        source_repo = Path(__file__).resolve().parents[1]
        names = [package.name for package in discover_skill_packages(source_repo)]

        self.assertIn("ticket-creation", names)
        self.assertEqual(
            [package.name for package in discover_skill_packages(source_repo, audience="chatgpt")].count("ticket-creation"),
            1,
        )
        self.assertNotIn(
            "ticket-creation",
            [package.name for package in discover_skill_packages(source_repo, audience="copilot")],
        )

    def test_copilot_report_skill_is_not_discoverable(self) -> None:
        source_repo = Path(__file__).resolve().parents[1]
        names = [package.name for package in discover_skill_packages(source_repo, audience="copilot")]
        self.assertNotIn("report", names)

    def test_installed_copilot_package_does_not_include_report_skill(self) -> None:
        source_repo = Path(__file__).resolve().parents[1]
        destination = self.tmp_path / "installed-skills"
        home = self.tmp_path / "home"
        result = install_skill_packages(
            repo_root=source_repo,
            destination_root=destination,
            home=home,
            audience="copilot",
        )
        self.assertNotIn("report", [package.name for package in discover_skill_packages(source_repo, audience="copilot")])
        self.assertFalse((destination / "report").exists())
        self.assertNotIn("report", [status.name for status in result.statuses])

    def test_copilot_flow_skill_has_single_windows_local_invocation_mechanism(self) -> None:
        source_repo = Path(__file__).resolve().parents[1]
        scripts_dir = source_repo / "skills" / "copilot" / "flow" / "scripts"
        helper_text = (scripts_dir / "invoke-flow.ps1").read_text(encoding="utf-8")

        self.assertIn("__ai_dev_flow_exec__", helper_text)
        self.assertIn("ai_dev_flow.cli", helper_text)

        for command in (
            "start",
            "patch",
            "status",
            "diff",
            "commit",
            "reset",
            "promote",
            "complete",
            "block",
            "resume",
            "report",
        ):
            ps1_text = (scripts_dir / f"flow-{command}.ps1").read_text(encoding="utf-8")
            self.assertIn("invoke-flow.ps1", ps1_text)
            self.assertIn(f'"{command}"', ps1_text)
            self.assertFalse((scripts_dir / f"flow-{command}.cmd").exists())

        for command in ("ticket-create", "ticket-show", "ticket-query"):
            self.assertFalse((scripts_dir / f"flow-{command}").exists())
            self.assertFalse((scripts_dir / f"flow-{command}.ps1").exists())

    def test_installed_copilot_flow_scripts_execute_without_path_launchers(self) -> None:
        source_repo = Path(__file__).resolve().parents[1]
        destination = self.tmp_path / "installed-skills"
        home = self.tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)
        install_skill_packages(
            repo_root=source_repo,
            destination_root=destination,
            home=home,
            audience="copilot",
        )
        repo = self.tmp_path / "workflow-repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Skill Test"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "skill-test@example.com"], cwd=repo, check=True)
        (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)
        subprocess.run(["git", "branch", "-M", "main"], cwd=repo, check=True)

        path_without_flow_launchers = os.pathsep.join(("/usr/bin", "/bin"))
        self.assertIsNone(shutil.which("flow-patch", path=path_without_flow_launchers))
        environment = {**os.environ, "PATH": path_without_flow_launchers}
        scripts_dir = destination / "flow" / "scripts"

        subprocess.run([str(scripts_dir / "flow-patch"), "Installed skill lifecycle"], cwd=repo, env=environment, check=True)
        (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
        subprocess.run([str(scripts_dir / "flow-commit")], cwd=repo, env=environment, check=True)
        subprocess.run([str(scripts_dir / "flow-reset")], cwd=repo, env=environment, check=True)

        self.assertEqual((repo / "tracked.txt").read_text(encoding="utf-8"), "base\n")
        self.assertTrue((repo / ".ai-dev" / "workflow.json").exists())

    def test_duplicate_capability_names_are_valid_across_audiences(self) -> None:
        source_repo = Path(__file__).resolve().parents[1]
        names = [package.name for package in discover_skill_packages(source_repo)]

        self.assertEqual(names.count("auto-review"), 2)
        self.assertEqual(names.count("flow"), 2)
        self.assertEqual(len(names), 14)

    def test_real_repository_packages_install_to_flat_destination(self) -> None:
        source_repo = Path(__file__).resolve().parents[1]
        destination = self.tmp_path / "dest"
        home = self.tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)

        result = install_skill_packages(
            repo_root=source_repo,
            destination_root=destination,
            home=home,
        )

        copilot_packages = discover_skill_packages(source_repo, audience="copilot")
        self.assertEqual(result.discovered_count, len(copilot_packages))
        self.assertEqual(result.installed_count, len(copilot_packages))
        self.assertEqual(result.updated_count, 0)
        self.assertEqual(result.unchanged_count, 0)
        for package in copilot_packages:
            self.assertTrue((destination / package.name).is_symlink())

    def test_each_audience_install_includes_shared_and_selected_skills(self) -> None:
        source_repo = Path(__file__).resolve().parents[1]
        shared_names = {
            "feedback-loop-design",
            "frontend-design-review",
            "investigation-synthesis",
            "requirements-driven-development",
            "review-process",
        }

        expected_audience_skills = {
            "chatgpt": {
                "auto-review",
                "flow",
                "orchestrator",
                "skill-authoring",
                "ticket-creation",
                "work-skill-refinement",
            },
            "copilot": {"auto-review", "executor", "flow"},
        }
        for audience, audience_skills in expected_audience_skills.items():
            with self.subTest(audience=audience):
                destination = self.tmp_path / f"dest-{audience}"
                home = self.tmp_path / f"home-{audience}"
                home.mkdir(parents=True, exist_ok=True)
                result = install_skill_packages(
                    repo_root=source_repo,
                    destination_root=destination,
                    home=home,
                    audience=audience,
                )

                self.assertEqual(
                    {status.name for status in result.statuses if status.state == "installed"},
                    shared_names | audience_skills,
                )

    def test_all_audience_flat_install_is_unsupported(self) -> None:
        with self.assertRaisesRegex(SkillInstallationError, "Unsupported skill audience"):
            install_skill_packages(
                repo_root=Path(__file__).resolve().parents[1],
                destination_root=self.tmp_path / "dest-all",
                home=self.tmp_path / "home-all",
                audience="all",
            )

    def test_real_repository_has_no_work_skill_paths(self) -> None:
        source_repo = Path(__file__).resolve().parents[1]

        self.assertFalse((source_repo / "skills" / "work").exists())
        self.assertFalse((source_repo / "skills" / "work-agent-skills").exists())
        self.assertFalse((source_repo / "skills" / "flow").exists())
        self.assertEqual(
            [package.name for package in discover_skill_packages(source_repo)],
            [
                "auto-review",
                "auto-review",
                "executor",
                "feedback-loop-design",
                "flow",
                "flow",
                "frontend-design-review",
                "investigation-synthesis",
                "orchestrator",
                "requirements-driven-development",
                "review-process",
                "skill-authoring",
                "ticket-creation",
                "work-skill-refinement",
            ],
        )

    def test_shared_skill_source_remains_at_root(self) -> None:
        source_repo = Path(__file__).resolve().parents[1]

        self.assertTrue(
            (source_repo / "skills" / "requirements-driven-development" / "SKILL.md").exists()
        )
        self.assertTrue(
            (source_repo / "skills" / "frontend-design-review" / "SKILL.md").exists()
        )
        self.assertEqual(
            discover_skill_packages(self.repo_root),
            (),
        )

    def test_repository_discovery_matches_real_top_level_skill_packages(self) -> None:
        packages = discover_skill_packages(Path(__file__).resolve().parents[1])

        self.assertEqual(
            [package.name for package in packages],
            [
                "auto-review",
                "auto-review",
                "executor",
                "feedback-loop-design",
                "flow",
                "flow",
                "frontend-design-review",
                "investigation-synthesis",
                "orchestrator",
                "requirements-driven-development",
                "review-process",
                "skill-authoring",
                "ticket-creation",
                "work-skill-refinement",
            ],
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

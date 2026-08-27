from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_dev_flow import claude_activation as activation
from ai_dev_flow import skill_installation as installation
from ai_dev_flow.claude_activation import ClaudeActivationError

WINDOWS = os.name == "nt"


class _TempHome(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.home = self.tmp_path / "home"
        self.home.mkdir()

    def tearDown(self) -> None:
        self._tmpdir.cleanup()


class ClaudeInstallationDestinationTests(_TempHome):
    def test_claude_audience_is_supported_and_targets_claude_skills(self) -> None:
        self.assertIn("claude", installation.SUPPORTED_AUDIENCES)
        self.assertEqual(
            installation.resolve_skills_root_for_audience(audience="claude", home=self.home),
            self.home.resolve() / ".claude" / "skills",
        )

    def test_other_audiences_keep_the_agents_destination(self) -> None:
        for audience in ("chatgpt", "copilot"):
            with self.subTest(audience=audience):
                self.assertEqual(
                    installation.resolve_skills_root_for_audience(
                        audience=audience, home=self.home
                    ),
                    self.home.resolve() / ".agents" / "skills",
                )

    def test_link_kind_is_junction_on_windows_and_symlink_elsewhere(self) -> None:
        self.assertEqual(installation.preferred_link_kind("windows"), "junction")
        self.assertEqual(installation.preferred_link_kind("posix"), "symlink")


class ManagedLinkTests(_TempHome):
    def setUp(self) -> None:
        super().setUp()
        self.target = self.tmp_path / "package"
        self.target.mkdir()
        (self.target / "SKILL.md").write_text("real", encoding="utf-8")
        self.other = self.tmp_path / "other"
        self.other.mkdir()
        (self.other / "SKILL.md").write_text("other", encoding="utf-8")
        self.destination = self.home / "link"

    def _kind(self) -> str:
        return installation.preferred_link_kind("windows" if WINDOWS else "posix")

    def test_create_and_detect_managed_link(self) -> None:
        installation._replace_skill_link(self.destination, self.target, kind=self._kind())

        self.assertTrue(installation.path_is_managed_link(self.destination))
        self.assertEqual(
            Path(installation.read_link_target_text(self.destination)).resolve(),
            self.target.resolve(),
        )
        self.assertEqual((self.destination / "SKILL.md").read_text(encoding="utf-8"), "real")

    @unittest.skipUnless(WINDOWS, "junction semantics are Windows-only")
    def test_junctions_are_not_reported_as_symlinks(self) -> None:
        """The historical is_symlink() check cannot recognise a junction."""
        installation._replace_skill_link(self.destination, self.target, kind="junction")
        self.assertTrue(installation.path_is_junction(self.destination))
        self.assertFalse(self.destination.is_symlink())
        self.assertTrue(installation.path_is_managed_link(self.destination))

    def test_replacement_retargets_an_existing_managed_link(self) -> None:
        kind = self._kind()
        installation._replace_skill_link(self.destination, self.target, kind=kind)
        installation._replace_skill_link(self.destination, self.other, kind=kind)

        self.assertEqual((self.destination / "SKILL.md").read_text(encoding="utf-8"), "other")

    def test_failed_creation_never_destroys_the_working_destination(self) -> None:
        kind = self._kind()
        installation._replace_skill_link(self.destination, self.target, kind=kind)

        def explode(path, target, *, kind):
            raise OSError("simulated link creation failure")

        with patch.object(installation, "_create_link", side_effect=explode):
            with self.assertRaises(OSError):
                installation._replace_skill_link(self.destination, self.other, kind=kind)

        # The previously working package is still installed and still correct.
        self.assertTrue(installation.path_is_managed_link(self.destination))
        self.assertEqual((self.destination / "SKILL.md").read_text(encoding="utf-8"), "real")

    def test_failed_validation_never_destroys_the_working_destination(self) -> None:
        kind = self._kind()
        installation._replace_skill_link(self.destination, self.target, kind=kind)

        with patch.object(installation, "read_link_target_text", return_value=None):
            with self.assertRaises(OSError):
                installation._replace_skill_link(self.destination, self.other, kind=kind)

        self.assertEqual((self.destination / "SKILL.md").read_text(encoding="utf-8"), "real")

    def test_unmanaged_destination_fails_closed(self) -> None:
        self.destination.mkdir(parents=True)
        (self.destination / "user.txt").write_text("mine", encoding="utf-8")

        with self.assertRaises(OSError):
            installation._replace_skill_link(self.destination, self.target, kind=self._kind())

        self.assertEqual((self.destination / "user.txt").read_text(encoding="utf-8"), "mine")


class ClaudeInstallEndToEndTests(_TempHome):
    def setUp(self) -> None:
        super().setUp()
        # The ownership manifest path comes from APPDATA on Windows, not home,
        # so isolate it explicitly or the test would read the real install.
        self._env = patch.dict(os.environ, {"APPDATA": str(self.home / "appdata")})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_claude_install_is_idempotent_and_records_link_kind(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        destination = installation.resolve_skills_root_for_audience(
            audience="claude", home=self.home
        )

        first = installation.install_skill_packages(
            repo_root=repo_root,
            destination_root=destination,
            home=self.home,
            audience="claude",
        )
        self.assertGreater(first.installed_count, 0)

        names = {status.name for status in first.statuses}
        # Shared provider-neutral role plus the Claude-facing packages.
        self.assertIn("executor", names)
        self.assertIn("flow", names)
        self.assertIn("auto-review", names)

        second = installation.install_skill_packages(
            repo_root=repo_root,
            destination_root=destination,
            home=self.home,
            audience="claude",
        )
        self.assertEqual(second.installed_count, 0)
        self.assertEqual(second.updated_count, 0)
        self.assertGreater(second.unchanged_count, 0)

        ownership = json.loads(
            installation.resolve_skill_installation_ownership_path(home=self.home).read_text(
                encoding="utf-8"
            )
        )
        recorded = ownership["owned_skills"]
        expected_prefix = "junction:" if WINDOWS else "symlink:"
        self.assertTrue(all(value.startswith(expected_prefix) for value in recorded.values()))

    def test_executor_has_exactly_one_canonical_source(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        self.assertTrue((repo_root / "skills" / "executor" / "SKILL.md").is_file())
        self.assertFalse((repo_root / "skills" / "copilot" / "executor").exists())

    def test_copilot_audience_still_receives_the_executor_package(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        packages = installation.discover_skill_packages(repo_root, audience="copilot")
        self.assertIn("executor", {package.name for package in packages})


class ClaudeActivationPointerTests(_TempHome):
    def test_pointer_is_installed_then_idempotent(self) -> None:
        self.assertEqual(activation.sync_claude_activation(home=self.home), "installed")
        self.assertEqual(activation.sync_claude_activation(home=self.home), "unchanged")

        text = activation.resolve_claude_instruction_path(home=self.home).read_text(
            encoding="utf-8"
        )
        self.assertIn(activation.MANAGED_BEGIN, text)
        self.assertIn(activation.MANAGED_END, text)
        self.assertIn("`proceed`", text)

    def test_unrelated_user_content_is_preserved(self) -> None:
        path = activation.resolve_claude_instruction_path(home=self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# My own notes\n\nKeep me.\n", encoding="utf-8")

        activation.sync_claude_activation(home=self.home)
        text = path.read_text(encoding="utf-8")

        self.assertIn("# My own notes", text)
        self.assertIn("Keep me.", text)
        self.assertIn(activation.MANAGED_BEGIN, text)

    def test_managed_block_updates_in_place_without_duplicating(self) -> None:
        path = activation.resolve_claude_instruction_path(home=self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "before\n\n"
            + activation.MANAGED_BEGIN
            + "\nstale\n"
            + activation.MANAGED_END
            + "\n\nafter\n",
            encoding="utf-8",
        )

        activation.sync_claude_activation(home=self.home)
        text = path.read_text(encoding="utf-8")

        self.assertEqual(text.count(activation.MANAGED_BEGIN), 1)
        self.assertNotIn("stale", text)
        self.assertIn("before", text)
        self.assertIn("after", text)

    def test_ambiguous_managed_markers_fail_closed(self) -> None:
        path = activation.resolve_claude_instruction_path(home=self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        original = (
            activation.MANAGED_BEGIN + "\na\n" + activation.MANAGED_END + "\n"
            + activation.MANAGED_BEGIN + "\nb\n" + activation.MANAGED_END + "\n"
        )
        path.write_text(original, encoding="utf-8")

        with self.assertRaises(ClaudeActivationError):
            activation.sync_claude_activation(home=self.home)

        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_unbalanced_managed_markers_fail_closed(self) -> None:
        path = activation.resolve_claude_instruction_path(home=self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        original = activation.MANAGED_BEGIN + "\ndangling\n"
        path.write_text(original, encoding="utf-8")

        with self.assertRaises(ClaudeActivationError):
            activation.sync_claude_activation(home=self.home)

        self.assertEqual(path.read_text(encoding="utf-8"), original)


class ControlPlaneCacheTests(_TempHome):
    def test_one_host_level_cache_per_coordination_repository(self) -> None:
        cache = activation.resolve_control_plane_cache(
            "jmrozi1/ai-dev-control-plane", home=self.home
        )
        self.assertEqual(
            cache,
            self.home.resolve() / ".ai-dev" / "control-plane" / "jmrozi1" / "ai-dev-control-plane",
        )

    def test_cache_path_does_not_vary_by_product_repository(self) -> None:
        first = activation.resolve_control_plane_cache(home=self.home)
        second = activation.resolve_control_plane_cache(home=self.home)
        self.assertEqual(first, second)

    def test_malformed_coordination_repository_fails_closed(self) -> None:
        for value in ("", "no-slash", "too/many/parts", "/leading"):
            with self.subTest(value=value):
                with self.assertRaises(ClaudeActivationError):
                    activation.resolve_control_plane_cache(value, home=self.home)


class IdentityAndAuthorizationTests(_TempHome):
    def _git(self, repo: Path, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )

    def _product_repo(self, *, remote: str | None, active_issue: int | None) -> Path:
        repo = self.tmp_path / "product"
        repo.mkdir()
        self._git(repo, "init", "-q")
        self._git(repo, "config", "user.name", "Claude Tests")
        self._git(repo, "config", "user.email", "claude@example.com")
        if remote:
            self._git(repo, "remote", "add", "origin", remote)
        ai_dev = repo / ".ai-dev"
        ai_dev.mkdir()
        state = {"mainBranch": "main", "scratchBranch": "scratch", "checkpoint": 0}
        if active_issue is not None:
            state["activeIssueNumber"] = active_issue
        (ai_dev / "workflow.json").write_text(json.dumps(state), encoding="utf-8")
        return repo

    def test_identity_comes_from_git_remote_and_flow_state(self) -> None:
        repo = self._product_repo(
            remote="https://github.com/jmrozi1/family-dragonflight-server.git", active_issue=9
        )

        identity = activation.resolve_product_identity(repo)

        self.assertEqual(identity.repository, "jmrozi1/family-dragonflight-server")
        self.assertEqual(identity.owner, "jmrozi1")
        self.assertEqual(identity.project, "family-dragonflight-server")
        self.assertEqual(identity.ticket, "issue-9")
        self.assertEqual(identity.issue_number, 9)

    def test_missing_remote_fails_closed(self) -> None:
        repo = self._product_repo(remote=None, active_issue=9)
        with self.assertRaises(ClaudeActivationError) as caught:
            activation.resolve_product_identity(repo)
        self.assertIn("repository identity", str(caught.exception))

    def test_inactive_ticket_fails_closed(self) -> None:
        repo = self._product_repo(
            remote="https://github.com/jmrozi1/ai-dev.git", active_issue=None
        )
        with self.assertRaises(ClaudeActivationError) as caught:
            activation.resolve_product_identity(repo)
        self.assertIn("No active Flow issue", str(caught.exception))

    def test_missing_cache_fails_closed_without_creating_anything(self) -> None:
        repo = self._product_repo(
            remote="https://github.com/jmrozi1/ai-dev.git", active_issue=56
        )
        cache = self.tmp_path / "absent-cache"

        with self.assertRaises(ClaudeActivationError) as caught:
            activation.discover(repo, cache=cache)

        self.assertIn("cache is missing", str(caught.exception))
        self.assertFalse(cache.exists())
        # No fallback transport was invented anywhere in the product repository.
        self.assertFalse((repo / ".ai-dev" / "handoff.md").exists())
        self.assertFalse((repo / "handoff.md").exists())


class AuthorizedRailSelectionTests(_TempHome):
    def _coordination(self, rails: dict[str, str]) -> Path:
        repo = self.tmp_path / "coordination"
        scope = repo / "proj" / "issue-1" / "rails"
        scope.mkdir(parents=True)
        (repo / "proj" / "issue-1" / "state.md").write_text("# State\n", encoding="utf-8")
        for rail_id, status in rails.items():
            rail_dir = scope / rail_id
            rail_dir.mkdir()
            (rail_dir / "rail.md").write_text(
                f"# Rail: {rail_id}\n\nStatus: {status}\nRole: executor\n", encoding="utf-8"
            )
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@e.com",
             "commit", "-qm", "state"],
            check=True,
        )
        return repo

    def test_exactly_one_ready_rail_is_selected(self) -> None:
        repo = self._coordination({"rail-a": "completed", "rail-b": "ready"})
        rail = activation.resolve_authorized_rail(repo, project="proj", ticket="issue-1")
        self.assertEqual(rail.identifier, "rail-b")
        self.assertEqual(rail.status, "ready")

    def test_no_ready_rail_fails_closed(self) -> None:
        repo = self._coordination({"rail-a": "completed"})
        with self.assertRaises(ClaudeActivationError) as caught:
            activation.resolve_authorized_rail(repo, project="proj", ticket="issue-1")
        self.assertIn("is ready", str(caught.exception))

    def test_ambiguous_ready_rails_fail_closed(self) -> None:
        repo = self._coordination({"rail-a": "ready", "rail-b": "ready"})
        with self.assertRaises(ClaudeActivationError) as caught:
            activation.resolve_authorized_rail(repo, project="proj", ticket="issue-1")
        self.assertIn("More than one rail", str(caught.exception))

    def test_missing_namespace_fails_closed(self) -> None:
        repo = self._coordination({"rail-a": "ready"})
        with self.assertRaises(ClaudeActivationError):
            activation.resolve_authorized_rail(repo, project="proj", ticket="issue-404")


class ClaudeLauncherTests(unittest.TestCase):
    """The Claude launcher must load its own runtime, whatever the caller CWD."""

    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.entry = self.repo_root / "tools" / "claude" / "ai-dev-entry.py"

    def test_entry_point_exists_and_is_absolute_path_safe(self) -> None:
        self.assertTrue(self.entry.is_file())
        text = self.entry.read_text(encoding="utf-8")
        # Resolution must come from __file__, never from the caller's directory.
        self.assertIn("Path(__file__).resolve()", text)

    def test_launchers_use_the_repository_bootstrap_python_selector(self) -> None:
        posix = (self.repo_root / "skills/claude/flow/scripts/ai-dev").read_text(encoding="utf-8")
        powershell = (self.repo_root / "skills/claude/flow/scripts/ai-dev.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("python_select.sh", posix)
        self.assertIn("ai_dev_select_python", posix)
        self.assertNotIn("python3 -m", posix)
        self.assertIn("PythonSelection.ps1", powershell)
        self.assertIn("Resolve-AiDevPythonExecutable", powershell)

    def test_runtime_is_owned_by_the_entry_point_not_the_caller_directory(self) -> None:
        """Running from a decoy checkout must still load this runtime."""
        with tempfile.TemporaryDirectory() as tmp:
            decoy = Path(tmp) / "older-ai-dev"
            (decoy / "ai_dev_flow").mkdir(parents=True)
            (decoy / "ai_dev_flow" / "__init__.py").write_text("", encoding="utf-8")
            (decoy / "ai_dev_flow" / "claude_activation.py").write_text(
                "raise SystemExit('decoy runtime was loaded')\n", encoding="utf-8"
            )

            completed = subprocess.run(
                [sys.executable, str(self.entry), "cache-path"],
                cwd=decoy,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertNotIn("decoy runtime was loaded", completed.stdout)
        self.assertIn("control-plane", completed.stdout)


class CheckpointTwoFailureSafetyTests(_TempHome):
    """Remediation for the three failure-safety defects found by exact-code review.

    A: obsolete cleanup sliced every ownership value by len("symlink:").
    B: the final staging-to-destination swap removed the working install first.
    C: the activation pointer was written non-atomically.
    """

    def setUp(self) -> None:
        super().setUp()
        self.target = self.tmp_path / "package"
        self.target.mkdir()
        (self.target / "SKILL.md").write_text("real", encoding="utf-8")
        self.other = self.tmp_path / "other"
        self.other.mkdir()
        (self.other / "SKILL.md").write_text("other", encoding="utf-8")
        self.destination = self.home / "link"
        self.kind = installation.preferred_link_kind("windows" if WINDOWS else "posix")

    # Finding A ---------------------------------------------------------------

    def test_ownership_target_parses_both_prefixes_without_truncation(self) -> None:
        """`junction:` is one character longer than `symlink:`."""
        self.assertEqual(
            installation._ownership_target_text("symlink:C:\\\\pkg"), "C:\\\\pkg"
        )
        self.assertEqual(
            installation._ownership_target_text("junction:C:\\\\pkg"), "C:\\\\pkg"
        )

    def test_obsolete_junction_ownership_is_recognised_and_removed(self) -> None:
        installation._replace_skill_link(self.destination, self.target, kind=self.kind)
        platform = "windows" if WINDOWS else "posix"
        key = installation._normalized_path_identity_text_for_platform(
            self.destination, platform=platform
        )
        ownership = installation._symlink_ownership_value(
            self.target, platform=platform, kind=self.kind
        )
        # The recorded prefix is the one the current platform actually installs.
        self.assertTrue(ownership.startswith(f"{self.kind}:"))

        updated, statuses = installation._reconcile_obsolete_managed_skills(
            desired_destination_keys=set(),
            owned_skills={key: ownership},
            platform=platform,
        )

        self.assertEqual([status.state for status in statuses], ["removed"])
        self.assertEqual(updated, {})
        self.assertFalse(installation.path_is_managed_link(self.destination))
        # Removing the link must never remove the package it pointed at.
        self.assertEqual((self.target / "SKILL.md").read_text(encoding="utf-8"), "real")

    def test_obsolete_reconcile_fails_closed_on_diverged_target(self) -> None:
        installation._replace_skill_link(self.destination, self.target, kind=self.kind)
        platform = "windows" if WINDOWS else "posix"
        key = installation._normalized_path_identity_text_for_platform(
            self.destination, platform=platform
        )
        diverged = installation._symlink_ownership_value(
            self.other, platform=platform, kind=self.kind
        )

        with self.assertRaises(installation.SkillInstallationError):
            installation._reconcile_obsolete_managed_skills(
                desired_destination_keys=set(),
                owned_skills={key: diverged},
                platform=platform,
            )

        self.assertTrue(installation.path_is_managed_link(self.destination))

    # Finding B ---------------------------------------------------------------

    def test_final_swap_failure_leaves_the_original_package_installed(self) -> None:
        installation._replace_skill_link(self.destination, self.target, kind=self.kind)
        real_rename = os.rename
        calls = {"n": 0}

        def fail_final_swap(src, dst, *args, **kwargs):
            # The destination-to-backup move succeeds; the staging-to-destination
            # swap is the seam under test.
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("simulated final swap failure")
            return real_rename(src, dst, *args, **kwargs)

        with patch.object(os, "rename", side_effect=fail_final_swap):
            with self.assertRaises(OSError):
                installation._replace_skill_link(
                    self.destination, self.other, kind=self.kind
                )

        # The working install still resolves to and serves the original package.
        self.assertTrue(installation.path_is_managed_link(self.destination))
        self.assertEqual(
            Path(installation.read_link_target_text(self.destination)).resolve(),
            self.target.resolve(),
        )
        self.assertEqual((self.destination / "SKILL.md").read_text(encoding="utf-8"), "real")

    def test_unrecoverable_swap_preserves_the_backup_and_reports_it(self) -> None:
        installation._replace_skill_link(self.destination, self.target, kind=self.kind)
        real_rename = os.rename

        def fail_swap_and_restore(src, dst, *args, **kwargs):
            if Path(dst) == self.destination:
                raise OSError("simulated unrecoverable swap")
            return real_rename(src, dst, *args, **kwargs)

        with patch.object(os, "rename", side_effect=fail_swap_and_restore):
            with self.assertRaises(OSError) as caught:
                installation._replace_skill_link(
                    self.destination, self.other, kind=self.kind
                )

        message = str(caught.exception)
        backup = self.destination.parent / f".{self.destination.name}.ai-dev-backup"
        self.assertIn("preserved at", message)
        self.assertIn(str(backup), message)
        # The only surviving copy of the working install was not discarded.
        self.assertTrue(installation.path_is_managed_link(backup))
        self.assertEqual((backup / "SKILL.md").read_text(encoding="utf-8"), "real")

    def test_successful_replacement_retargets_and_cleans_artifacts(self) -> None:
        installation._replace_skill_link(self.destination, self.target, kind=self.kind)
        installation._replace_skill_link(self.destination, self.other, kind=self.kind)

        self.assertEqual((self.destination / "SKILL.md").read_text(encoding="utf-8"), "other")
        staging = self.destination.parent / f".{self.destination.name}.ai-dev-staging"
        backup = self.destination.parent / f".{self.destination.name}.ai-dev-backup"
        self.assertFalse(staging.exists() or installation.path_is_managed_link(staging))
        self.assertFalse(backup.exists() or installation.path_is_managed_link(backup))

    def test_recoverable_failure_cleans_staging(self) -> None:
        installation._replace_skill_link(self.destination, self.target, kind=self.kind)
        real_rename = os.rename
        calls = {"n": 0}

        def fail_final_swap(src, dst, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("simulated final swap failure")
            return real_rename(src, dst, *args, **kwargs)

        with patch.object(os, "rename", side_effect=fail_final_swap):
            with self.assertRaises(OSError):
                installation._replace_skill_link(
                    self.destination, self.other, kind=self.kind
                )

        staging = self.destination.parent / f".{self.destination.name}.ai-dev-staging"
        self.assertFalse(staging.exists() or installation.path_is_managed_link(staging))

    def test_unmanaged_destination_still_fails_closed(self) -> None:
        self.destination.mkdir(parents=True)
        (self.destination / "user.txt").write_text("mine", encoding="utf-8")

        with self.assertRaises(OSError):
            installation._replace_skill_link(self.destination, self.target, kind=self.kind)

        self.assertEqual((self.destination / "user.txt").read_text(encoding="utf-8"), "mine")
        staging = self.destination.parent / f".{self.destination.name}.ai-dev-staging"
        self.assertFalse(staging.exists() or installation.path_is_managed_link(staging))

    # Finding C ---------------------------------------------------------------

    def test_activation_write_uses_the_atomic_primitive(self) -> None:
        with patch.object(activation, "write_text_atomic") as writer:
            activation.sync_claude_activation(home=self.home)
        writer.assert_called_once()

    def test_failed_activation_write_leaves_existing_file_byte_identical(self) -> None:
        path = activation.resolve_claude_instruction_path(home=self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        original = "# My own notes\n\nKeep every byte of this.\n"
        path.write_text(original, encoding="utf-8")
        before = path.read_bytes()

        with patch.object(
            activation, "write_text_atomic", side_effect=OSError("simulated write failure")
        ):
            with self.assertRaises(ClaudeActivationError):
                activation.sync_claude_activation(home=self.home)

        self.assertEqual(path.read_bytes(), before)

    def test_marker_failures_never_write(self) -> None:
        path = activation.resolve_claude_instruction_path(home=self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        original = activation.MANAGED_BEGIN + "\nonly a begin marker\n"
        path.write_text(original, encoding="utf-8")
        before = path.read_bytes()

        with patch.object(activation, "write_text_atomic") as writer:
            with self.assertRaises(ClaudeActivationError):
                activation.sync_claude_activation(home=self.home)
            writer.assert_not_called()

        self.assertEqual(path.read_bytes(), before)

    def test_success_preserves_unrelated_content_and_rerun_is_unchanged(self) -> None:
        path = activation.resolve_claude_instruction_path(home=self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Mine\n\nkeep\n", encoding="utf-8")

        self.assertEqual(activation.sync_claude_activation(home=self.home), "inserted")
        text = path.read_text(encoding="utf-8")
        self.assertIn("# Mine", text)
        self.assertIn("keep", text)

        self.assertEqual(activation.sync_claude_activation(home=self.home), "unchanged")


class OwnershipHomeIsolationTests(_TempHome):
    """An install aimed at an alternate home must not touch the real user's ledger.

    The skills destination already honors `home`. Before this fix the ownership
    ledger did not, so reconciliation ran against the machine-global manifest and
    could remove packages belonging to a different home entirely.
    """

    def _resolve(self, **kwargs):
        return installation.resolve_skill_installation_ownership_path(**kwargs)

    # Precedence --------------------------------------------------------------

    def test_windows_explicit_home_ignores_ambient_appdata(self) -> None:
        alt = self.tmp_path / "alt-home"
        with patch.dict(os.environ, {"APPDATA": str(self.tmp_path / "real-appdata")}):
            resolved = self._resolve(home=alt, os_name="nt")

        self.assertEqual(
            resolved,
            alt.resolve() / "AppData" / "Roaming" / "ai-dev" / "skill-installation-ownership.json",
        )
        self.assertNotIn("real-appdata", str(resolved))

    def test_posix_explicit_home_ignores_ambient_xdg_config_home(self) -> None:
        alt = self.tmp_path / "alt-home"
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.tmp_path / "real-xdg")}):
            resolved = self._resolve(home=alt, os_name="posix")

        self.assertEqual(
            resolved,
            alt.resolve() / ".config" / "ai-dev" / "skill-installation-ownership.json",
        )
        self.assertNotIn("real-xdg", str(resolved))

    def test_explicit_config_override_still_beats_explicit_home(self) -> None:
        alt = self.tmp_path / "alt-home"
        explicit_appdata = self.tmp_path / "explicit-appdata"
        explicit_xdg = self.tmp_path / "explicit-xdg"

        self.assertEqual(
            self._resolve(home=alt, os_name="nt", appdata=str(explicit_appdata)),
            explicit_appdata / "ai-dev" / "skill-installation-ownership.json",
        )
        self.assertEqual(
            self._resolve(home=alt, os_name="posix", xdg_config_home=str(explicit_xdg)),
            explicit_xdg / "ai-dev" / "skill-installation-ownership.json",
        )

    def test_default_host_behaviour_still_uses_ambient_config(self) -> None:
        ambient_appdata = self.tmp_path / "ambient-appdata"
        with patch.dict(os.environ, {"APPDATA": str(ambient_appdata)}):
            self.assertEqual(
                self._resolve(os_name="nt"),
                ambient_appdata / "ai-dev" / "skill-installation-ownership.json",
            )

        ambient_xdg = self.tmp_path / "ambient-xdg"
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(ambient_xdg)}):
            self.assertEqual(
                self._resolve(os_name="posix"),
                ambient_xdg / "ai-dev" / "skill-installation-ownership.json",
            )

    def test_default_host_behaviour_falls_back_to_home_when_config_absent(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("APPDATA", None)
            resolved = self._resolve(os_name="nt")
        self.assertEqual(resolved.name, "skill-installation-ownership.json")
        self.assertIn("AppData", str(resolved))

    # Destructive cross-home regression ---------------------------------------

    def test_alternate_home_install_cannot_touch_the_real_users_state(self) -> None:
        """The exact failure that removed real Claude packages during a test run."""
        repo_root = Path(__file__).resolve().parents[1]

        # A stand-in for the real user: their own config root, their own ledger,
        # and a real managed link they have installed.
        real_config = self.tmp_path / "real-config"
        real_skills = self.tmp_path / "real-skills"
        real_package = self.tmp_path / "real-package"
        real_package.mkdir()
        (real_package / "SKILL.md").write_text("the real user's package", encoding="utf-8")
        real_link = real_skills / "flow"
        kind = installation.preferred_link_kind("windows" if WINDOWS else "posix")
        installation._replace_skill_link(real_link, real_package, kind=kind)

        platform = "windows" if WINDOWS else "posix"
        real_ledger = installation.resolve_skill_installation_ownership_path(
            **(
                {"appdata": str(real_config)}
                if WINDOWS
                else {"xdg_config_home": str(real_config)}
            ),
            os_name="nt" if WINDOWS else "posix",
        )
        real_ledger.parent.mkdir(parents=True, exist_ok=True)
        real_ledger.write_text(
            json.dumps(
                {
                    "version": installation.SKILL_INSTALLATION_OWNERSHIP_VERSION,
                    "owned_skills": {
                        installation._normalized_path_identity_text_for_platform(
                            real_link, platform=platform
                        ): installation._symlink_ownership_value(
                            real_package, platform=platform, kind=kind
                        )
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        ledger_before = real_ledger.read_bytes()

        # Now install into a completely separate home, with the real user's config
        # root still ambient in the environment.
        alt_home = self.tmp_path / "alt-home"
        alt_home.mkdir()
        env = {"APPDATA": str(real_config)} if WINDOWS else {"XDG_CONFIG_HOME": str(real_config)}
        with patch.dict(os.environ, env):
            installation.install_skill_packages(
                repo_root=repo_root,
                destination_root=installation.resolve_skills_root_for_audience(
                    audience="claude", home=alt_home
                ),
                home=alt_home,
                audience="claude",
            )

        # The real user's ledger is byte-for-byte untouched...
        self.assertEqual(real_ledger.read_bytes(), ledger_before)
        # ...and their installed package is still linked and still serves content.
        self.assertTrue(installation.path_is_managed_link(real_link))
        self.assertEqual(
            (real_link / "SKILL.md").read_text(encoding="utf-8"), "the real user's package"
        )

        # The alternate home received its own ledger, and only its own.
        alt_ledger = installation.resolve_skill_installation_ownership_path(home=alt_home)
        self.assertTrue(alt_ledger.is_file())
        self.assertIn(str(alt_home.resolve()), str(alt_ledger))
        alt_owned = json.loads(alt_ledger.read_text(encoding="utf-8"))["owned_skills"]
        self.assertTrue(alt_owned)
        for destination in alt_owned:
            self.assertIn(str(alt_home.resolve()), destination)

    def test_alternate_home_install_needs_no_environment_isolation(self) -> None:
        """Correctness must come from the API, not from tests patching the env."""
        repo_root = Path(__file__).resolve().parents[1]
        alt_home = self.tmp_path / "api-only-home"
        alt_home.mkdir()

        # Deliberately no patch.dict here: ambient APPDATA/XDG stay as the host
        # has them, exactly as an unisolated caller would see.
        installation.install_skill_packages(
            repo_root=repo_root,
            destination_root=installation.resolve_skills_root_for_audience(
                audience="claude", home=alt_home
            ),
            home=alt_home,
            audience="claude",
        )

        ledger = installation.resolve_skill_installation_ownership_path(home=alt_home)
        self.assertTrue(ledger.is_file())
        self.assertIn(str(alt_home.resolve()), str(ledger))


class ClaudeCommandInstallationTests(_TempHome):
    """Finding A: the command the activation pointer documents must really exist.

    A fresh executor types `ai-dev discover` because the installed pointer says
    to. Before this, nothing put that command anywhere a shell looks, so the
    documented cold-start path failed on its first line.
    """

    def setUp(self) -> None:
        super().setUp()
        self.runtime = self.tmp_path / "runtime"
        (self.runtime / "tools" / "claude").mkdir(parents=True)
        (self.runtime / "tools" / "claude" / "ai-dev-entry.py").write_text(
            "# entry point\n", encoding="utf-8"
        )
        self.bin = self.home.resolve() / ".local" / "bin"

    def _install(self, **kwargs):
        return activation.install_ai_dev_command(
            home=self.home, runtime_root=self.runtime, **kwargs
        )

    # Destination and shell coverage

    def test_command_lands_in_the_user_owned_directory(self) -> None:
        directory, _ = self._install()
        self.assertEqual(directory, self.bin)
        self.assertEqual(
            activation.resolve_command_directory(home=self.home), self.bin
        )

    def test_windows_covers_git_bash_and_powershell(self) -> None:
        directory, statuses = self._install(platform="windows")
        self.assertEqual(
            sorted(status.path.name for status in statuses), ["ai-dev", "ai-dev.ps1"]
        )
        self.assertTrue((directory / "ai-dev").is_file())
        self.assertTrue((directory / "ai-dev.ps1").is_file())

    def test_posix_installation_model_is_unchanged_by_windows_support(self) -> None:
        directory, statuses = self._install(platform="posix")
        self.assertEqual([status.path.name for status in statuses], ["ai-dev"])
        self.assertFalse((directory / "ai-dev.ps1").exists())
        launcher = (directory / "ai-dev").read_text(encoding="utf-8")
        self.assertTrue(launcher.startswith("#!/usr/bin/env bash"))
        if os.name != "nt":
            self.assertTrue(os.access(directory / "ai-dev", os.X_OK))

    def test_unsupported_platform_fails_closed(self) -> None:
        with self.assertRaises(ClaudeActivationError):
            self._install(platform="plan9")

    # Launcher content

    def test_launchers_delegate_to_the_runtime_entry_point_by_absolute_path(self) -> None:
        directory, _ = self._install(platform="windows")
        entry = "tools/claude/ai-dev-entry.py"
        posix = (directory / "ai-dev").read_text(encoding="utf-8")
        powershell = (directory / "ai-dev.ps1").read_text(encoding="utf-8")
        self.assertIn(self.runtime.as_posix(), posix)
        self.assertIn(entry, posix)
        self.assertIn(str(self.runtime), powershell)
        self.assertIn("ai-dev-entry.py", powershell)

    def test_launchers_reuse_the_repository_bootstrap_python_selector(self) -> None:
        directory, _ = self._install(platform="windows")
        posix = (directory / "ai-dev").read_text(encoding="utf-8")
        powershell = (directory / "ai-dev.ps1").read_text(encoding="utf-8")
        self.assertIn("python_select.sh", posix)
        self.assertIn("ai_dev_select_python", posix)
        self.assertNotIn("python3 -m", posix)
        self.assertIn("PythonSelection.ps1", powershell)
        self.assertIn("Resolve-AiDevPythonExecutable", powershell)

    def test_every_launcher_carries_the_ownership_marker(self) -> None:
        directory, statuses = self._install(platform="windows")
        for status in statuses:
            with self.subTest(launcher=status.path.name):
                self.assertIn(
                    activation.LAUNCHER_OWNERSHIP_MARKER,
                    status.path.read_text(encoding="utf-8"),
                )
                self.assertTrue(activation.launcher_is_managed(status.path))
        self.assertTrue(directory.is_dir())

    # Idempotency and update

    def test_reinstall_is_idempotent(self) -> None:
        _, first = self._install(platform="windows")
        self.assertEqual({status.state for status in first}, {"installed"})
        _, second = self._install(platform="windows")
        self.assertEqual({status.state for status in second}, {"unchanged"})

    def test_reinstall_retargets_a_moved_runtime(self) -> None:
        directory, _ = self._install(platform="windows")
        moved = self.tmp_path / "moved-runtime"
        (moved / "tools" / "claude").mkdir(parents=True)
        (moved / "tools" / "claude" / "ai-dev-entry.py").write_text("", encoding="utf-8")

        _, statuses = activation.install_ai_dev_command(
            home=self.home, runtime_root=moved, platform="windows"
        )
        self.assertEqual({status.state for status in statuses}, {"updated"})
        self.assertIn(moved.as_posix(), (directory / "ai-dev").read_text(encoding="utf-8"))
        self.assertNotIn(
            self.runtime.as_posix(), (directory / "ai-dev").read_text(encoding="utf-8")
        )

    # Ownership and failure safety

    def test_unowned_collision_fails_closed_without_deleting(self) -> None:
        self.bin.mkdir(parents=True)
        theirs = self.bin / "ai-dev"
        theirs.write_text("#!/bin/sh\necho someone else's command\n", encoding="utf-8")

        with self.assertRaises(ClaudeActivationError) as raised:
            self._install(platform="windows")

        self.assertIn("not an AI Dev managed launcher", str(raised.exception))
        self.assertEqual(
            theirs.read_text(encoding="utf-8"), "#!/bin/sh\necho someone else's command\n"
        )
        self.assertFalse((self.bin / "ai-dev.ps1").exists())

    def test_collision_on_the_second_launcher_installs_neither(self) -> None:
        self.bin.mkdir(parents=True)
        (self.bin / "ai-dev.ps1").write_text("# unrelated\n", encoding="utf-8")

        with self.assertRaises(ClaudeActivationError):
            self._install(platform="windows")

        self.assertFalse((self.bin / "ai-dev").exists())
        self.assertEqual((self.bin / "ai-dev.ps1").read_text(encoding="utf-8"), "# unrelated\n")

    @unittest.skipIf(WINDOWS, "symlink creation needs privileges on Windows")
    def test_symlinked_destination_is_never_replaced(self) -> None:
        self.bin.mkdir(parents=True)
        target = self.tmp_path / "elsewhere"
        target.write_text("#!/bin/sh\n", encoding="utf-8")
        (self.bin / "ai-dev").symlink_to(target)

        with self.assertRaises(ClaudeActivationError):
            self._install(platform="posix")

        self.assertTrue((self.bin / "ai-dev").is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "#!/bin/sh\n")

    def test_missing_entry_point_fails_closed_without_creating_anything(self) -> None:
        empty = self.tmp_path / "not-a-checkout"
        empty.mkdir()
        with self.assertRaises(ClaudeActivationError) as raised:
            activation.install_ai_dev_command(home=self.home, runtime_root=empty)
        self.assertIn("no Claude entry point", str(raised.exception))
        self.assertFalse(self.bin.exists())

    def test_command_directory_blocked_by_a_file_fails_closed(self) -> None:
        self.bin.parent.mkdir(parents=True)
        self.bin.write_text("not a directory", encoding="utf-8")
        with self.assertRaises(ClaudeActivationError) as raised:
            self._install()
        self.assertIn("is not a directory", str(raised.exception))

    # Cleanup semantics

    def test_obsolete_owned_launchers_are_retired_on_platform_change(self) -> None:
        directory, _ = self._install(platform="windows")
        self.assertTrue((directory / "ai-dev.ps1").is_file())

        _, statuses = self._install(platform="posix")

        self.assertIn(
            ("removed", "ai-dev.ps1"),
            [(status.state, status.path.name) for status in statuses],
        )
        self.assertFalse((directory / "ai-dev.ps1").exists())
        self.assertTrue((directory / "ai-dev").is_file())

    def test_cleanup_preserves_unowned_and_unrelated_files(self) -> None:
        self.bin.mkdir(parents=True)
        (self.bin / "ai-dev.cmd").write_text("@echo someone else\n", encoding="utf-8")
        (self.bin / "flow.ps1").write_text("# unrelated user tool\n", encoding="utf-8")

        self._install(platform="posix")

        self.assertEqual((self.bin / "ai-dev.cmd").read_text(encoding="utf-8"), "@echo someone else\n")
        self.assertEqual(
            (self.bin / "flow.ps1").read_text(encoding="utf-8"), "# unrelated user tool\n"
        )

    # PATH reporting

    def test_path_membership_is_reported_and_never_mutated(self) -> None:
        before = os.environ.get("PATH")
        joined = os.pathsep.join([str(self.tmp_path / "other"), str(self.bin)])
        self.assertTrue(activation.command_directory_is_on_path(self.bin, path_value=joined))
        self.assertFalse(
            activation.command_directory_is_on_path(
                self.bin, path_value=str(self.tmp_path / "other")
            )
        )
        self.assertEqual(os.environ.get("PATH"), before)

    def test_path_membership_tolerates_trailing_separators(self) -> None:
        joined = os.pathsep.join([str(self.bin) + os.sep, ""])
        self.assertTrue(activation.command_directory_is_on_path(self.bin, path_value=joined))

    # Pointer / command agreement

    def test_activation_pointer_documents_the_command_that_is_installed(self) -> None:
        block = activation.render_activation_block()
        self.assertIn(f"{activation.AI_DEV_COMMAND_NAME} discover", block)
        for platform in ("posix", "windows"):
            with self.subTest(platform=platform):
                names = activation.managed_launcher_names(platform)
                self.assertIn(activation.AI_DEV_COMMAND_NAME, names)

    def test_documented_commands_all_exist_on_the_installed_surface(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            [sys.executable, str(repo_root / "tools" / "claude" / "ai-dev-entry.py"), "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout)
        for command in ("discover", "status", "review-evidence", "publish"):
            with self.subTest(command=command):
                self.assertIn(command, completed.stdout)

    # Supported installation path

    def test_supported_claude_installation_installs_pointer_and_command(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        exit_code = installation.main(
            [
                "--repo-root",
                str(repo_root),
                "--audience",
                "claude",
                "--home",
                str(self.home),
            ]
        )
        self.assertEqual(exit_code, 0)

        pointer = activation.resolve_claude_instruction_path(home=self.home)
        self.assertTrue(pointer.is_file())
        self.assertIn(
            f"{activation.AI_DEV_COMMAND_NAME} discover",
            pointer.read_text(encoding="utf-8"),
        )

        command = activation.resolve_command_directory(home=self.home) / "ai-dev"
        self.assertTrue(command.is_file())
        self.assertIn(str(repo_root.as_posix()), command.read_text(encoding="utf-8"))

    def test_other_audiences_do_not_install_the_claude_command(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        exit_code = installation.main(
            [
                "--repo-root",
                str(repo_root),
                "--audience",
                "copilot",
                "--destination-root",
                str(self.tmp_path / "copilot-skills"),
                "--home",
                str(self.home),
            ]
        )
        self.assertEqual(exit_code, 0)
        self.assertFalse(activation.resolve_command_directory(home=self.home).exists())
        self.assertFalse(activation.resolve_claude_instruction_path(home=self.home).exists())


class ControlPlaneDirectoryAccessTests(_TempHome):
    """Durable, bounded write access to the managed cache from any product repo.

    Checkpoint 4 Phase B failed here: a fresh product session discovered its rail
    correctly and was then denied the write that publication needs, because the
    coordination cache lives outside that repository's working directory.
    """

    def setUp(self) -> None:
        super().setUp()
        self.settings = activation.resolve_claude_settings_path(home=self.home)

    def _write(self, payload: str) -> None:
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        self.settings.write_text(payload, encoding="utf-8")

    def _read(self) -> dict:
        return json.loads(self.settings.read_text(encoding="utf-8"))

    def _directories(self) -> list:
        return self._read()["permissions"]["additionalDirectories"]

    # Scope

    def test_the_granted_directory_is_the_managed_cache_root_only(self) -> None:
        entry = activation.render_managed_directory_entry(home=self.home)
        root = activation.resolve_control_plane_cache_root(home=self.home)
        self.assertEqual(Path(entry), root)

        # Bounded: the granted root is strictly inside the home directory, not
        # the home directory itself and not the tree of product checkouts.
        self.assertNotEqual(Path(entry), self.home)
        self.assertIn(self.home.resolve(), Path(entry).parents)

        # Every project's clone still lives under the one granted root, so this
        # stays a single narrow grant however many products the user works on.
        for repository in ("jmrozi1/ai-dev-control-plane", "someone/other-plane"):
            with self.subTest(repository=repository):
                cache = activation.resolve_control_plane_cache(repository, home=self.home)
                self.assertIn(Path(entry), cache.parents)

    def test_grant_is_directory_scope_and_never_a_permission_mode_change(self) -> None:
        self._write(json.dumps({"permissions": {"defaultMode": "default"}}))
        activation.sync_control_plane_directory_permission(home=self.home)
        permissions = self._read()["permissions"]
        self.assertEqual(permissions["defaultMode"], "default")
        self.assertEqual(
            permissions["additionalDirectories"],
            [activation.render_managed_directory_entry(home=self.home)],
        )

    # First install and idempotency

    def test_first_install_creates_settings_with_exactly_the_bounded_entry(self) -> None:
        status = activation.sync_control_plane_directory_permission(home=self.home)
        self.assertEqual(status.state, "installed")
        self.assertEqual(status.path, self.settings)
        self.assertEqual(
            self._read(),
            {"permissions": {"additionalDirectories": [status.entry]}},
        )

    def test_second_install_is_unchanged_and_does_not_rewrite(self) -> None:
        activation.sync_control_plane_directory_permission(home=self.home)
        first = self.settings.read_bytes()
        status = activation.sync_control_plane_directory_permission(home=self.home)
        self.assertEqual(status.state, "unchanged")
        self.assertEqual(self.settings.read_bytes(), first)

    def test_an_entry_the_user_added_by_hand_is_not_duplicated(self) -> None:
        entry = activation.render_managed_directory_entry(home=self.home)
        self._write(json.dumps({"permissions": {"additionalDirectories": [entry]}}))
        status = activation.sync_control_plane_directory_permission(home=self.home)
        self.assertEqual(status.state, "unchanged")
        self.assertEqual(self._directories(), [entry])

    # Preservation of what AI Dev does not own

    def test_unrelated_keys_rules_and_directories_all_survive(self) -> None:
        payload = {
            "autoUpdatesChannel": "latest",
            "model": "claude-opus-5",
            "permissions": {
                "defaultMode": "acceptEdits",
                "allow": ["Bash(git status:*)"],
                "deny": ["Read(./secrets/**)"],
                "additionalDirectories": ["/srv/shared", "/srv/notes"],
            },
            "hooks": {"Stop": []},
        }
        self._write(json.dumps(payload, indent=2))

        status = activation.sync_control_plane_directory_permission(home=self.home)
        self.assertEqual(status.state, "updated")

        merged = self._read()
        self.assertEqual(merged["autoUpdatesChannel"], "latest")
        self.assertEqual(merged["model"], "claude-opus-5")
        self.assertEqual(merged["hooks"], {"Stop": []})
        self.assertEqual(merged["permissions"]["defaultMode"], "acceptEdits")
        self.assertEqual(merged["permissions"]["allow"], ["Bash(git status:*)"])
        self.assertEqual(merged["permissions"]["deny"], ["Read(./secrets/**)"])

        # Appended, never reordered or replaced.
        self.assertEqual(
            merged["permissions"]["additionalDirectories"],
            ["/srv/shared", "/srv/notes", status.entry],
        )

        # Nothing outside the one entry differs from what the user wrote.
        expected = json.loads(json.dumps(payload))
        expected["permissions"]["additionalDirectories"].append(status.entry)
        self.assertEqual(merged, expected)

    def test_no_broad_directory_or_blanket_rule_is_ever_added(self) -> None:
        self._write(json.dumps({"permissions": {"allow": []}}))
        activation.sync_control_plane_directory_permission(home=self.home)
        permissions = self._read()["permissions"]
        self.assertEqual(permissions["allow"], [])
        self.assertNotIn("deny", permissions)
        self.assertNotIn("defaultMode", permissions)
        for granted in permissions["additionalDirectories"]:
            with self.subTest(granted=granted):
                self.assertNotEqual(Path(granted), self.home)

    def test_retirement_is_additive_and_never_deletes_a_users_entry(self) -> None:
        """No marker proves AI Dev wrote any element, so nothing is removed.

        A stale directory entry is inert; deleting an entry we cannot prove we
        own would silently take away access the user granted themselves.
        """
        self.assertFalse(
            [name for name in dir(activation) if "remove" in name and "director" in name.lower()]
        )

        stale = str(self.tmp_path / "old-home" / ".ai-dev" / "control-plane")
        self._write(json.dumps({"permissions": {"additionalDirectories": [stale]}}))
        status = activation.sync_control_plane_directory_permission(home=self.home)
        self.assertEqual(status.state, "updated")
        self.assertEqual(self._directories(), [stale, status.entry])

    # Fail-closed behaviour

    def _assert_fails_closed_byte_identically(self, payload: str) -> None:
        self._write(payload)
        before = self.settings.read_bytes()
        with self.assertRaises(ClaudeActivationError) as raised:
            activation.sync_control_plane_directory_permission(home=self.home)
        self.assertEqual(self.settings.read_bytes(), before)
        message = str(raised.exception)
        self.assertIn(str(self.settings), message)
        self.assertIn("install again", message)

    def test_malformed_json_fails_closed_without_rewriting(self) -> None:
        self._assert_fails_closed_byte_identically('{"permissions": {,}')

    def test_a_non_object_settings_document_fails_closed(self) -> None:
        self._assert_fails_closed_byte_identically('["not", "settings"]')

    def test_wrong_typed_permissions_fails_closed(self) -> None:
        self._assert_fails_closed_byte_identically('{"permissions": "all"}')

    def test_wrong_typed_additional_directories_fails_closed(self) -> None:
        self._assert_fails_closed_byte_identically(
            '{"permissions": {"additionalDirectories": "/srv/shared"}}'
        )

    def test_a_non_string_directory_entry_fails_closed(self) -> None:
        self._assert_fails_closed_byte_identically(
            '{"permissions": {"additionalDirectories": ["/srv/shared", 7]}}'
        )

    def test_the_write_uses_the_atomic_primitive(self) -> None:
        with patch.object(activation, "write_json_object_atomic") as writer:
            activation.sync_control_plane_directory_permission(home=self.home)
        writer.assert_called_once()

    # Path normalization

    def test_windows_entries_normalize_separators_and_case(self) -> None:
        entry = activation.render_managed_directory_entry(home=self.home, platform="windows")
        self.assertNotIn("/", entry)

        for spelling in (
            entry.replace("\\", "/"),
            entry.upper(),
            entry.lower(),
            entry + "\\",
        ):
            with self.subTest(spelling=spelling):
                self._write(
                    json.dumps({"permissions": {"additionalDirectories": [spelling]}})
                )
                status = activation.sync_control_plane_directory_permission(
                    home=self.home, platform="windows"
                )
                self.assertEqual(status.state, "unchanged")
                self.assertEqual(self._directories(), [spelling])

    def test_posix_keeps_case_significant(self) -> None:
        entry = activation.render_managed_directory_entry(home=self.home, platform="posix")
        self._write(
            json.dumps({"permissions": {"additionalDirectories": [entry.upper()]}})
        )
        status = activation.sync_control_plane_directory_permission(
            home=self.home, platform="posix"
        )
        self.assertEqual(status.state, "updated")
        self.assertEqual(self._directories(), [entry.upper(), entry])

    def test_a_home_relative_entry_is_recognised_on_both_platforms(self) -> None:
        for platform, spelling in (
            ("posix", "~/.ai-dev/control-plane"),
            ("windows", "~\\.ai-dev\\control-plane"),
            ("windows", "~/.ai-dev/control-plane"),
        ):
            with self.subTest(platform=platform, spelling=spelling):
                self._write(
                    json.dumps({"permissions": {"additionalDirectories": [spelling]}})
                )
                status = activation.sync_control_plane_directory_permission(
                    home=self.home, platform=platform
                )
                self.assertEqual(status.state, "unchanged")
                self.assertEqual(self._directories(), [spelling])

    def test_an_unsupported_platform_fails_closed(self) -> None:
        with self.assertRaises(ClaudeActivationError):
            activation.render_managed_directory_entry(home=self.home, platform="plan9")

    # Installation seam

    def test_supported_claude_installation_grants_the_access(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        arguments = [
            "--repo-root",
            str(repo_root),
            "--audience",
            "claude",
            "--home",
            str(self.home),
        ]

        self.assertEqual(installation.main(arguments), 0)
        entry = activation.render_managed_directory_entry(home=self.home)
        self.assertEqual(self._directories(), [entry])

        # Reinstalling the audience neither duplicates nor rewrites the grant.
        settled = self.settings.read_bytes()
        self.assertEqual(installation.main(arguments), 0)
        self.assertEqual(self.settings.read_bytes(), settled)

    def test_host_activation_installs_pointer_command_and_access_together(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        result = activation.install_claude_host_activation(
            home=self.home, runtime_root=repo_root
        )
        self.assertTrue(result["pointerPath"].is_file())
        self.assertTrue(result["launchers"])
        access = result["controlPlaneAccess"]
        self.assertEqual(access.state, "installed")
        self.assertEqual(
            Path(access.entry),
            activation.resolve_control_plane_cache_root(home=self.home),
        )

    def test_other_audiences_never_touch_the_users_claude_settings(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        exit_code = installation.main(
            [
                "--repo-root",
                str(repo_root),
                "--audience",
                "copilot",
                "--destination-root",
                str(self.tmp_path / "copilot-skills"),
                "--home",
                str(self.home),
            ]
        )
        self.assertEqual(exit_code, 0)
        self.assertFalse(self.settings.exists())


if __name__ == "__main__":
    unittest.main()

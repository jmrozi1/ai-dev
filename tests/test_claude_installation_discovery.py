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


if __name__ == "__main__":
    unittest.main()

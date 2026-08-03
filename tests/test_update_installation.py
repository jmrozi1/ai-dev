from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ai_dev_flow.json_files import JsonFileError
from ai_dev_flow.update_installation import (
    ApplyRefreshResult,
    LauncherRefreshResult,
    UpdateInstallationError,
    load_installation_source_record,
    _run_launcher_refresh,
    resolve_installation_source_path,
    run_update_from_record,
    write_installation_source_record,
)


class UpdateInstallationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.metadata_path = self.tmp_path / "state" / "installation-source.json"

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _git(self, cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if check and completed.returncode != 0:
            self.fail(
                f"git command failed: git -C {cwd} {' '.join(args)}\n"
                f"stdout:\n{completed.stdout}\n"
                f"stderr:\n{completed.stderr}"
            )
        return completed

    def _create_remote_pair(self, name: str, *, source_dir_name: str = "source") -> tuple[Path, Path, Path]:
        root = self.tmp_path / name
        remote = root / "remote.git"
        seed = root / "seed"
        source = root / source_dir_name
        root.mkdir(parents=True, exist_ok=True)

        self._git(root, "init", "--bare", str(remote))

        seed.mkdir(parents=True, exist_ok=True)
        self._git(seed, "init")
        self._git(seed, "config", "user.name", "Update Tests")
        self._git(seed, "config", "user.email", "update-tests@example.com")
        (seed / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._git(seed, "add", "tracked.txt")
        self._git(seed, "commit", "-m", "initial")
        self._git(seed, "branch", "-M", "main")
        self._git(seed, "remote", "add", "origin", str(remote))
        self._git(seed, "push", "-u", "origin", "main")

        self._git(root, "clone", str(remote), str(source))
        self._git(source, "config", "user.name", "Update Tests")
        self._git(source, "config", "user.email", "update-tests@example.com")
        self._git(source, "checkout", "main")

        return remote, seed, source

    def _write_metadata(self, source_repo: Path, *, branch: str = "main", remote: str = "origin") -> None:
        write_installation_source_record(
            metadata_path=self.metadata_path,
            source_repository=source_repo,
            branch=branch,
            remote=remote,
        )

    def _create_linked_worktree(self, source: Path, name: str) -> Path:
        worktree = self.tmp_path / name
        self._git(source, "worktree", "add", "--detach", str(worktree), "main")
        self._git(worktree, "config", "user.name", "Update Tests")
        self._git(worktree, "config", "user.email", "update-tests@example.com")
        return worktree

    def test_resolve_installation_source_path_platform_variants(self) -> None:
        posix_path = resolve_installation_source_path(
            os_name="posix",
            home=Path("/home/example"),
            xdg_config_home=None,
        )
        self.assertEqual(
            posix_path,
            Path("/home/example/.config/ai-dev/installation-source.json"),
        )

        windows_path = resolve_installation_source_path(
            os_name="nt",
            home=Path("/Users/example"),
            appdata="C:/Users/example/AppData/Roaming",
        )
        self.assertEqual(
            windows_path,
            Path("C:/Users/example/AppData/Roaming/ai-dev/installation-source.json"),
        )

    def test_missing_metadata_refused(self) -> None:
        with self.assertRaises(UpdateInstallationError) as context:
            load_installation_source_record(self.metadata_path)
        self.assertIn("metadata is missing", str(context.exception))

    def test_malformed_metadata_rejected(self) -> None:
        self.metadata_path.parent.mkdir(parents=True, exist_ok=True)
        self.metadata_path.write_text("{ invalid json\n", encoding="utf-8")

        with self.assertRaises(UpdateInstallationError) as context:
            load_installation_source_record(self.metadata_path)
        self.assertIn("Invalid JSON", str(context.exception))

    def test_unsupported_metadata_version_rejected(self) -> None:
        self.metadata_path.parent.mkdir(parents=True, exist_ok=True)
        self.metadata_path.write_text(
            json.dumps(
                {
                    "version": 9,
                    "source_repository": str(self.tmp_path),
                    "branch": "main",
                    "remote": "origin",
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaises(UpdateInstallationError) as context:
            load_installation_source_record(self.metadata_path)
        self.assertIn("Unsupported installation source metadata version", str(context.exception))

    def test_relative_source_repository_rejected(self) -> None:
        self.metadata_path.parent.mkdir(parents=True, exist_ok=True)
        self.metadata_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "source_repository": "relative/repo",
                    "branch": "main",
                    "remote": "origin",
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaises(UpdateInstallationError) as context:
            load_installation_source_record(self.metadata_path)
        self.assertIn("must be an absolute path", str(context.exception))

    def test_missing_source_directory_refused(self) -> None:
        self._write_metadata(self.tmp_path / "missing-source")

        with self.assertRaises(UpdateInstallationError) as context:
            run_update_from_record(self.metadata_path)
        text = str(context.exception)
        self.assertIn("Recorded source repository path is missing", text)
        self.assertIn(str(self.tmp_path / "missing-source"), text)

    def test_non_git_source_path_refused(self) -> None:
        source = self.tmp_path / "source"
        source.mkdir(parents=True)
        self._write_metadata(source)

        with self.assertRaises(UpdateInstallationError) as context:
            run_update_from_record(self.metadata_path)
        self.assertIn("Cannot resolve Git repository root", str(context.exception))

    def test_missing_configured_remote_refused(self) -> None:
        _, _, source = self._create_remote_pair("missing-remote")
        self._write_metadata(source, remote="upstream")

        with self.assertRaises(UpdateInstallationError) as context:
            run_update_from_record(self.metadata_path)
        self.assertIn("Configured update remote is not available", str(context.exception))

    def test_missing_configured_branch_refused(self) -> None:
        _, _, source = self._create_remote_pair("missing-branch")
        self._write_metadata(source, branch="release")

        with self.assertRaises(UpdateInstallationError) as context:
            run_update_from_record(self.metadata_path)
        self.assertIn("Configured update branch does not exist locally", str(context.exception))

    def test_dirty_checkout_staged_refused(self) -> None:
        _, _, source = self._create_remote_pair("dirty-staged")
        self._write_metadata(source)

        (source / "tracked.txt").write_text("staged\n", encoding="utf-8")
        self._git(source, "add", "tracked.txt")

        with self.assertRaises(UpdateInstallationError) as context:
            run_update_from_record(self.metadata_path)
        text = str(context.exception)
        self.assertIn("Recorded source repository", text)
        self.assertIn("Local changes detected", text)
        self.assertIn("will not stash, reset, or discard", text)
        self.assertIn("tracked.txt", text)

    def test_dirty_checkout_unstaged_refused(self) -> None:
        _, _, source = self._create_remote_pair("dirty-unstaged")
        self._write_metadata(source)

        (source / "tracked.txt").write_text("unstaged\n", encoding="utf-8")

        with self.assertRaises(UpdateInstallationError) as context:
            run_update_from_record(self.metadata_path)
        self.assertIn("tracked.txt", str(context.exception))

    def test_dirty_checkout_untracked_refused(self) -> None:
        _, _, source = self._create_remote_pair("dirty-untracked")
        self._write_metadata(source)

        (source / "new-file.txt").write_text("new\n", encoding="utf-8")

        with self.assertRaises(UpdateInstallationError) as context:
            run_update_from_record(self.metadata_path)
        self.assertIn("new-file.txt", str(context.exception))

    def test_active_git_operation_refused(self) -> None:
        _, _, source = self._create_remote_pair("active-op")
        self._write_metadata(source)
        (source / ".git" / "MERGE_HEAD").write_text("deadbeef\n", encoding="utf-8")

        with self.assertRaises(UpdateInstallationError) as context:
            run_update_from_record(self.metadata_path)
        self.assertIn("active operation", str(context.exception))

    def test_active_git_operation_refused_for_linked_worktree(self) -> None:
        _, _, source = self._create_remote_pair("active-op-worktree")
        worktree = self._create_linked_worktree(source, "linked-worktree")
        self._write_metadata(worktree)

        git_dir_text = self._git(worktree, "rev-parse", "--git-dir").stdout.strip()
        git_dir = Path(git_dir_text)
        if not git_dir.is_absolute():
            git_dir = (worktree / git_dir).resolve()
        git_dir.mkdir(parents=True, exist_ok=True)
        (git_dir / "MERGE_HEAD").write_text("deadbeef\n", encoding="utf-8")

        with self.assertRaises(UpdateInstallationError) as context:
            run_update_from_record(self.metadata_path)
        self.assertIn("active operation", str(context.exception))

    def test_launcher_refresh_forwards_recorded_branch_and_remote(self) -> None:
        source = self.tmp_path / "source"
        source.mkdir(parents=True)

        with patch(
            "ai_dev_flow.update_installation._run_python_module",
            return_value=subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="Launcher: /tmp/ai-dev (updated)\n",
                stderr="",
            ),
        ) as module_mock:
            result = _run_launcher_refresh(source, branch="release", remote="upstream")

        self.assertEqual(result.status, "updated")
        module_mock.assert_called_once_with(
            source,
            "ai_dev_flow.bootstrap",
            [
                "--platform",
                ("windows" if os.name == "nt" else "posix"),
                "--repo-root",
                str(source),
                "--command-name",
                "ai-dev",
                "--update-branch",
                "release",
                "--update-remote",
                "upstream",
            ],
        )

    def test_already_up_to_date_updates_launcher_and_apply(self) -> None:
        _, _, source = self._create_remote_pair("up-to-date")
        self._write_metadata(source)

        with (
            patch(
                "ai_dev_flow.update_installation._run_launcher_refresh",
                return_value=LauncherRefreshResult(status="unchanged", detail="launcher already up to date"),
            ) as launcher_mock,
            patch(
                "ai_dev_flow.update_installation._run_apply_with_updated_source",
                return_value=ApplyRefreshResult(status="completed", detail="apply completed"),
            ) as apply_mock,
        ):
            result = run_update_from_record(self.metadata_path)

        self.assertEqual(result.source.source_status, "already up to date")
        self.assertEqual(result.launcher.status, "unchanged")
        self.assertEqual(result.apply.status, "completed")
        launcher_mock.assert_called_once()
        apply_mock.assert_called_once()

    def test_fast_forward_update(self) -> None:
        _, seed, source = self._create_remote_pair("fast-forward")
        self._write_metadata(source)

        before = self._git(source, "rev-parse", "HEAD").stdout.strip()
        (seed / "tracked.txt").write_text("remote update\n", encoding="utf-8")
        self._git(seed, "add", "tracked.txt")
        self._git(seed, "commit", "-m", "remote update")
        self._git(seed, "push", "origin", "main")

        with (
            patch(
                "ai_dev_flow.update_installation._run_launcher_refresh",
                return_value=LauncherRefreshResult(status="updated", detail="launcher refreshed"),
            ),
            patch(
                "ai_dev_flow.update_installation._run_apply_with_updated_source",
                return_value=ApplyRefreshResult(status="completed", detail="apply completed"),
            ),
        ):
            result = run_update_from_record(self.metadata_path)

        after = self._git(source, "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(result.source.source_status, "fast-forwarded")
        self.assertEqual(result.source.source_from, before)
        self.assertEqual(result.source.source_to, after)
        self.assertNotEqual(before, after)

    def test_diverged_branch_refused(self) -> None:
        _, seed, source = self._create_remote_pair("diverged")
        self._write_metadata(source)

        (source / "local.txt").write_text("local\n", encoding="utf-8")
        self._git(source, "add", "local.txt")
        self._git(source, "commit", "-m", "local commit")

        (seed / "remote.txt").write_text("remote\n", encoding="utf-8")
        self._git(seed, "add", "remote.txt")
        self._git(seed, "commit", "-m", "remote commit")
        self._git(seed, "push", "origin", "main")

        with patch(
            "ai_dev_flow.update_installation._run_apply_with_updated_source"
        ) as apply_mock:
            with self.assertRaises(UpdateInstallationError) as context:
                run_update_from_record(self.metadata_path)

        self.assertIn("diverged", str(context.exception))
        apply_mock.assert_not_called()

    def test_unreachable_remote_fetch_failure_refused(self) -> None:
        _, _, source = self._create_remote_pair("fetch-failure")
        self._write_metadata(source)

        self._git(source, "remote", "set-url", "origin", "http://127.0.0.1:9/ai-dev.git")

        with self.assertRaises(UpdateInstallationError) as context:
            run_update_from_record(self.metadata_path)
        self.assertIn("Cannot fetch", str(context.exception))

    def test_no_apply_when_source_update_fails(self) -> None:
        _, _, source = self._create_remote_pair("no-apply-on-failure")
        self._write_metadata(source)
        (source / "dirty.txt").write_text("dirty\n", encoding="utf-8")

        with patch(
            "ai_dev_flow.update_installation._run_apply_with_updated_source"
        ) as apply_mock:
            with self.assertRaises(UpdateInstallationError):
                run_update_from_record(self.metadata_path)

        apply_mock.assert_not_called()

    def test_apply_failure_after_successful_source_update_is_reported(self) -> None:
        _, _, source = self._create_remote_pair("apply-failure")
        self._write_metadata(source)

        with (
            patch(
                "ai_dev_flow.update_installation._run_launcher_refresh",
                return_value=LauncherRefreshResult(status="unchanged", detail="launcher already up to date"),
            ),
            patch(
                "ai_dev_flow.update_installation._run_apply_with_updated_source",
                return_value=ApplyRefreshResult(status="failed", detail="Retry with: ..."),
            ),
        ):
            result = run_update_from_record(self.metadata_path)

        self.assertEqual(result.source.source_status, "already up to date")
        self.assertEqual(result.apply.status, "failed")
        self.assertIn("Retry with", result.apply.detail)

    def test_update_preserves_non_default_metadata_after_launcher_refresh(self) -> None:
        _, _, source = self._create_remote_pair("metadata-preserved")
        branch = "main"
        remote = "upstream"
        self._git(source, "remote", "rename", "origin", remote)

        home = self.tmp_path / "home-metadata-preserved"
        home.mkdir(parents=True, exist_ok=True)
        self.metadata_path = home / ".config" / "ai-dev" / "installation-source.json"
        self._write_metadata(source, branch=branch, remote=remote)

        def run_module_side_effect(
            repo_root: Path,
            module: str,
            args: list[str],
        ) -> subprocess.CompletedProcess[str]:
            self.assertEqual(repo_root, source)
            if module == "ai_dev_flow.bootstrap":
                passed_branch = args[args.index("--update-branch") + 1]
                passed_remote = args[args.index("--update-remote") + 1]
                write_installation_source_record(
                    metadata_path=self.metadata_path,
                    source_repository=source,
                    branch=passed_branch,
                    remote=passed_remote,
                )
                return subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout="Launcher: /tmp/ai-dev (up-to-date)\n",
                    stderr="",
                )

            self.fail(f"Unexpected module invocation: {module}")

        with (
            patch.dict(os.environ, {"HOME": str(home)}, clear=False),
            patch(
                "ai_dev_flow.update_installation._run_python_module",
                side_effect=run_module_side_effect,
            ),
            patch(
                "ai_dev_flow.update_installation._run_apply_with_updated_source",
                return_value=ApplyRefreshResult(status="completed", detail="apply completed"),
            ),
        ):
            result = run_update_from_record(self.metadata_path)

        record = load_installation_source_record(self.metadata_path)
        self.assertEqual(result.source.source_status, "already up to date")
        self.assertEqual(result.launcher.status, "unchanged")
        self.assertEqual(record.branch, branch)
        self.assertEqual(record.remote, remote)

    def test_launcher_failure_marks_apply_not_executed(self) -> None:
        _, _, source = self._create_remote_pair("launcher-failure")
        self._write_metadata(source)

        with (
            patch(
                "ai_dev_flow.update_installation._run_launcher_refresh",
                return_value=LauncherRefreshResult(status="failed", detail="bootstrap failed"),
            ),
            patch(
                "ai_dev_flow.update_installation._run_apply_with_updated_source"
            ) as apply_mock,
        ):
            result = run_update_from_record(self.metadata_path)

        self.assertEqual(result.launcher.status, "failed")
        self.assertEqual(result.apply.status, "failed")
        self.assertIn("not executed", result.apply.detail)
        apply_mock.assert_not_called()

    def test_switches_to_recorded_branch_when_clean(self) -> None:
        _, _, source = self._create_remote_pair("branch-switch")
        self._write_metadata(source)

        self._git(source, "checkout", "-b", "feature")

        with (
            patch(
                "ai_dev_flow.update_installation._run_launcher_refresh",
                return_value=LauncherRefreshResult(status="unchanged", detail="launcher already up to date"),
            ),
            patch(
                "ai_dev_flow.update_installation._run_apply_with_updated_source",
                return_value=ApplyRefreshResult(status="completed", detail="apply completed"),
            ),
        ):
            result = run_update_from_record(self.metadata_path)

        current = self._git(source, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        self.assertEqual(current, "main")
        self.assertEqual(result.source.branch, "main")

    def test_source_paths_with_spaces_supported(self) -> None:
        _, _, source = self._create_remote_pair("spaces", source_dir_name="source repo with spaces")
        self._write_metadata(source)

        with (
            patch(
                "ai_dev_flow.update_installation._run_launcher_refresh",
                return_value=LauncherRefreshResult(status="unchanged", detail="launcher already up to date"),
            ),
            patch(
                "ai_dev_flow.update_installation._run_apply_with_updated_source",
                return_value=ApplyRefreshResult(status="completed", detail="apply completed"),
            ),
        ):
            result = run_update_from_record(self.metadata_path)

        self.assertEqual(result.source.source_status, "already up to date")
        self.assertIn("source repo with spaces", str(result.source.source_repo))

    def test_metadata_atomic_write_failure_surfaces_error(self) -> None:
        with patch(
            "ai_dev_flow.update_installation.write_json_object_atomic",
            side_effect=JsonFileError("Cannot write metadata"),
        ):
            with self.assertRaises(UpdateInstallationError) as context:
                write_installation_source_record(
                    metadata_path=self.metadata_path,
                    source_repository=self.tmp_path,
                    branch="main",
                    remote="origin",
                )

        self.assertIn("Cannot write metadata", str(context.exception))


if __name__ == "__main__":
    unittest.main()

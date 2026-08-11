from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from ai_dev_flow import cli
from ai_dev_flow.bootstrap import run_bootstrap


class FlowDiffTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _run_git(self, repo_root: Path, *args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            check=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return completed.stdout.strip()

    def _init_repo(self, name: str) -> Path:
        repo_root = self.tmp_path / name
        repo_root.mkdir(parents=True)

        self._run_git(repo_root, "init", "-q")
        self._run_git(repo_root, "config", "user.name", "Flow Diff Tests")
        self._run_git(repo_root, "config", "user.email", "flow-diff-tests@example.com")

        (repo_root / ".gitignore").write_text(".ai-dev/workflow.json\n", encoding="utf-8")
        (repo_root / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._run_git(repo_root, "add", ".gitignore", "tracked.txt")
        self._run_git(repo_root, "commit", "-q", "-m", "initial commit")
        self._run_git(repo_root, "branch", "-M", "main")
        self._run_git(repo_root, "checkout", "-q", "-b", "scratch")

        workflow_path = repo_root / ".ai-dev" / "workflow.json"
        workflow_path.parent.mkdir(parents=True, exist_ok=True)
        workflow_path.write_text(
            json.dumps(
                {
                    "activeIssueNumber": 23,
                    "mainBranch": "main",
                    "scratchBranch": "scratch",
                    "checkpoint": 0,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        return repo_root

    def _invoke(self, cwd: Path, *arguments: str) -> tuple[int, str, str]:
        if not arguments:
            raise ValueError("command is required")
        command = arguments[0]
        command_arguments = list(arguments[1:])

        previous_cwd = Path.cwd()
        previous_argv = list(sys.argv)
        had_command_name = "FLOW_COMMAND_NAME" in os.environ
        previous_command_name = os.environ.get("FLOW_COMMAND_NAME")

        stdout = io.StringIO()
        stderr = io.StringIO()

        os.environ["FLOW_COMMAND_NAME"] = f"flow-{command}"
        sys.argv = [
            f"flow-{command}",
            cli._DIRECT_FLOW_ROUTE_TOKEN,
            command,
            *command_arguments,
        ]
        os.chdir(cwd)

        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                try:
                    cli.run()
                except SystemExit as exc:
                    code = int(exc.code) if isinstance(exc.code, int) else 1
                else:
                    code = 0
        finally:
            os.chdir(previous_cwd)
            sys.argv = previous_argv
            if had_command_name:
                assert previous_command_name is not None
                os.environ["FLOW_COMMAND_NAME"] = previous_command_name
            else:
                os.environ.pop("FLOW_COMMAND_NAME", None)

        return code, stdout.getvalue(), stderr.getvalue()

    def _index_tree_hash(self, repo_root: Path) -> str:
        return self._run_git(repo_root, "write-tree")

    def _workflow_json_bytes(self, repo_root: Path) -> bytes:
        return (repo_root / ".ai-dev" / "workflow.json").read_bytes()

    def _checkpoint(self, repo_root: Path) -> int:
        data = json.loads((repo_root / ".ai-dev" / "workflow.json").read_text(encoding="utf-8"))
        return int(data["checkpoint"])

    def _diff_baseline_path(self, repo_root: Path) -> Path:
        return repo_root / ".ai-dev" / "diff-baseline" / "baseline.json"

    def _diff_baseline_dir(self, repo_root: Path) -> Path:
        return self._diff_baseline_path(repo_root).parent

    def _load_diff_baseline(self, repo_root: Path) -> dict[str, object]:
        return json.loads(self._diff_baseline_path(repo_root).read_text(encoding="utf-8"))

    def _write_workflow_state(self, repo_root: Path, payload: dict[str, object]) -> None:
        workflow_path = repo_root / ".ai-dev" / "workflow.json"
        workflow_path.parent.mkdir(parents=True, exist_ok=True)
        workflow_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _ignore_flow_artifacts(self, repo_root: Path) -> None:
        exclude_path = repo_root / ".git" / "info" / "exclude"
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        if ".ai-dev/\n" not in existing:
            exclude_path.write_text(existing + ".ai-dev/\n", encoding="utf-8")

    def _status_without_flow_state(self, repo_root: Path) -> str:
        return self._run_git(
            repo_root,
            "status",
            "--short",
            "--untracked-files=all",
            "--",
            ".",
            ":(exclude).ai-dev/**",
        )

    def _repo_mutation_fingerprint(self, repo_root: Path) -> dict[str, str | int]:
        return {
            "head": self._run_git(repo_root, "rev-parse", "HEAD"),
            "branch": self._run_git(repo_root, "rev-parse", "--abbrev-ref", "HEAD"),
            "status": self._status_without_flow_state(repo_root),
            "index": self._index_tree_hash(repo_root),
            "stash": self._run_git(repo_root, "stash", "list"),
            "workflow": self._workflow_json_bytes(repo_root).decode("utf-8"),
            "checkpoint": self._checkpoint(repo_root),
        }

    def _snapshot_bytes(self, baseline: dict[str, object], kind: str, path_text: str) -> bytes:
        snapshots = baseline["snapshots"]
        assert isinstance(snapshots, dict)
        kind_map = snapshots[kind]
        assert isinstance(kind_map, dict)
        encoded = kind_map[path_text]
        assert isinstance(encoded, str)
        return base64.b64decode(encoded)

    def _status_paths(self, baseline: dict[str, object], key: str) -> list[str]:
        status = baseline["status"]
        assert isinstance(status, dict)
        value = status[key]
        assert isinstance(value, list)
        return [str(item) for item in value]

    def _rename_copy_entries(self, baseline: dict[str, object]) -> list[dict[str, object]]:
        status = baseline["status"]
        assert isinstance(status, dict)
        value = status.get("renameCopy", [])
        assert isinstance(value, list)
        entries: list[dict[str, object]] = []
        for item in value:
            assert isinstance(item, dict)
            entries.append(item)
        return entries

    def _refresh(self, repo_root: Path) -> None:
        code, stdout, stderr = self._invoke(repo_root, "diff", "--refresh")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(stdout, "Review baseline refreshed.\n")

    def _write_baseline_payload(self, repo_root: Path, payload: dict[str, object]) -> None:
        baseline_path = self._diff_baseline_path(repo_root)
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _write_diff_baseline_marker(self, repo_root: Path) -> Path:
        baseline_path = self._diff_baseline_path(repo_root)
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text('{"version":1,"repository":{"root":"x"},"workflow":{},"status":{},"snapshots":{"working":{}}}\n', encoding="utf-8")
        return baseline_path

    def test_diff_help_lists_options(self) -> None:
        repo_root = self._init_repo("repo-diff-help")

        code, stdout, stderr = self._invoke(repo_root, "diff", "--help")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Usage: flow-diff [--git|--all]", stdout)
        self.assertIn("Normal diff is review-baseline-relative when a valid baseline exists.", stdout)
        self.assertIn("Without a review baseline, normal diff shows the current full uncommitted diff.", stdout)
        self.assertIn("--all", stdout)
        self.assertIn("--git", stdout)
        self.assertNotIn("--stdout", stdout)

    def test_diff_default_includes_staged_unstaged_and_untracked(self) -> None:
        repo_root = self._init_repo("repo-diff-default")

        (repo_root / "tracked.txt").write_text("base\nchanged\n", encoding="utf-8")
        self._run_git(repo_root, "add", "tracked.txt")
        (repo_root / "tracked.txt").write_text("base\nchanged\nagain\n", encoding="utf-8")
        (repo_root / "new.txt").write_text("new file\n", encoding="utf-8")

        before_status = self._run_git(repo_root, "status", "--short")
        before_branch = self._run_git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")

        code, stdout, stderr = self._invoke(repo_root, "diff")

        after_status = self._run_git(repo_root, "status", "--short")
        after_branch = self._run_git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("diff --git a/tracked.txt b/tracked.txt", stdout)
        self.assertIn("diff --git a/new.txt b/new.txt", stdout)
        self.assertEqual(before_status, after_status)
        self.assertEqual(before_branch, after_branch)

    def test_diff_is_read_only_for_index_and_workflow_and_artifacts(self) -> None:
        repo_root = self._init_repo("repo-diff-read-only")
        (repo_root / "tracked.txt").write_text("base\nchanged\n", encoding="utf-8")
        self._run_git(repo_root, "add", "tracked.txt")
        (repo_root / "tracked.txt").write_text("base\nchanged\nunstaged\n", encoding="utf-8")
        (repo_root / "untracked.txt").write_text("u\n", encoding="utf-8")

        index_before = self._index_tree_hash(repo_root)
        workflow_before = self._workflow_json_bytes(repo_root)
        checkpoint_before = self._checkpoint(repo_root)

        code, stdout, stderr = self._invoke(repo_root, "diff")

        index_after = self._index_tree_hash(repo_root)
        workflow_after = self._workflow_json_bytes(repo_root)
        checkpoint_after = self._checkpoint(repo_root)

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertTrue(stdout)
        self.assertEqual(index_before, index_after)
        self.assertEqual(workflow_before, workflow_after)
        self.assertEqual(checkpoint_before, checkpoint_after)

        self.assertFalse((repo_root / ".ai-dev" / "review").exists())
        self.assertFalse((repo_root / ".ai-dev" / "tasks").exists())
        self.assertFalse((repo_root / ".ai-dev" / "summarize").exists())
        self.assertFalse((repo_root / ".ai-dev" / "current-task.md").exists())
        self.assertFalse((repo_root / ".ai-dev" / "review-manifest.json").exists())

    def test_diff_shows_staged_and_unstaged_for_same_file(self) -> None:
        repo_root = self._init_repo("repo-diff-staged-unstaged")
        (repo_root / "tracked.txt").write_text("base\nstaged\n", encoding="utf-8")
        self._run_git(repo_root, "add", "tracked.txt")
        (repo_root / "tracked.txt").write_text("base\nstaged\nunstaged\n", encoding="utf-8")

        code, stdout, stderr = self._invoke(repo_root, "diff")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertGreaterEqual(stdout.count("diff --git a/tracked.txt b/tracked.txt"), 2)

    def test_diff_all_includes_committed_workflow_changes(self) -> None:
        repo_root = self._init_repo("repo-diff-all")

        (repo_root / "committed.txt").write_text("committed\n", encoding="utf-8")
        self._run_git(repo_root, "add", "committed.txt")
        commit_code, _, commit_err = self._invoke(repo_root, "commit")
        self.assertEqual(commit_code, 0)
        self.assertEqual(commit_err, "")

        code, stdout, stderr = self._invoke(repo_root, "diff", "--all")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("diff --git a/committed.txt b/committed.txt", stdout)

    def test_diff_all_includes_committed_staged_unstaged_and_untracked(self) -> None:
        repo_root = self._init_repo("repo-diff-all-combined")

        (repo_root / "committed.txt").write_text("committed\n", encoding="utf-8")
        self._run_git(repo_root, "add", "committed.txt")
        commit_code, _, commit_err = self._invoke(repo_root, "commit")
        self.assertEqual(commit_code, 0)
        self.assertEqual(commit_err, "")

        (repo_root / "tracked.txt").write_text("base\nstaged\n", encoding="utf-8")
        self._run_git(repo_root, "add", "tracked.txt")
        (repo_root / "tracked.txt").write_text("base\nstaged\nunstaged\n", encoding="utf-8")
        (repo_root / "untracked.txt").write_text("untracked\n", encoding="utf-8")

        code, stdout, stderr = self._invoke(repo_root, "diff", "--all")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("diff --git a/committed.txt b/committed.txt", stdout)
        self.assertIn("diff --git a/tracked.txt b/tracked.txt", stdout)
        self.assertIn("diff --git a/untracked.txt b/untracked.txt", stdout)

    def test_diff_untracked_names_with_spaces_and_leading_hyphens(self) -> None:
        repo_root = self._init_repo("repo-diff-special-names")
        (repo_root / "name with spaces.txt").write_text("space\n", encoding="utf-8")
        (repo_root / "--leading-hyphen.txt").write_text("dash\n", encoding="utf-8")

        code, stdout, stderr = self._invoke(repo_root, "diff")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("name with spaces.txt", stdout)
        self.assertIn("--leading-hyphen.txt", stdout)

    def test_diff_untracked_empty_and_binary_files(self) -> None:
        repo_root = self._init_repo("repo-diff-empty-binary")
        (repo_root / "empty.txt").write_text("", encoding="utf-8")
        (repo_root / "binary.bin").write_bytes(b"\x00\xff\x00\xff")

        code, stdout, stderr = self._invoke(repo_root, "diff")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("diff --git a/empty.txt b/empty.txt", stdout)
        self.assertIn("new file mode 100644", stdout)
        self.assertIn("diff --git a/binary.bin b/binary.bin", stdout)
        self.assertTrue(
            "GIT binary patch" in stdout
            or "Binary files /dev/null and b/binary.bin differ" in stdout
        )

    def test_diff_empty_scope_notice_on_stderr_only(self) -> None:
        repo_root = self._init_repo("repo-diff-empty-scope")

        code, stdout, stderr = self._invoke(repo_root, "diff")
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "No diff content for current scope.\n")

    def test_installed_flow_diff_help_uses_executable_name(self) -> None:
        repo_root = self._init_repo("repo-diff-launcher-help")
        home = self.tmp_path / "home-launcher-help"
        install_dir = home / ".local" / "bin"

        run_bootstrap(
            platform="posix",
            repo_root=Path(__file__).resolve().parents[1],
            prefix="flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
        )

        launcher = install_dir / "flow-diff"
        completed = subprocess.run(
            [str(launcher), "--help"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(repo_root),
        )
        self.assertEqual(completed.returncode, 0)
        self.assertIn("Usage: flow-diff [--git|--all]", completed.stdout)
        self.assertNotIn("Usage: flow-diff diff", completed.stdout)

    def test_installed_custom_prefix_flow_diff_help_uses_executable_name(self) -> None:
        repo_root = self._init_repo("repo-diff-launcher-help-custom-prefix")
        home = self.tmp_path / "home-launcher-help-custom-prefix"
        install_dir = home / ".local" / "bin"

        run_bootstrap(
            platform="posix",
            repo_root=Path(__file__).resolve().parents[1],
            prefix="ai-flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
        )

        launcher = install_dir / "ai-flow-diff"
        completed = subprocess.run(
            [str(launcher), "--help"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(repo_root),
        )
        self.assertEqual(completed.returncode, 0)
        self.assertIn("Usage: ai-flow-diff [--git|--all]", completed.stdout)
        self.assertNotIn("Usage: ai-flow-diff diff", completed.stdout)

    def test_installed_flow_diff_outputs_raw_diff_on_stdout(self) -> None:
        repo_root = self._init_repo("repo-diff-launcher-stdout")
        home = self.tmp_path / "home-launcher-stdout"
        install_dir = home / ".local" / "bin"

        run_bootstrap(
            platform="posix",
            repo_root=Path(__file__).resolve().parents[1],
            prefix="flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
        )

        (repo_root / "tracked.txt").write_text("base\nstaged\n", encoding="utf-8")
        self._run_git(repo_root, "add", "tracked.txt")

        launcher = install_dir / "flow-diff"
        completed = subprocess.run(
            [str(launcher), "--all"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(repo_root),
        )
        self.assertEqual(completed.returncode, 0)
        self.assertIn("diff --git a/tracked.txt b/tracked.txt", completed.stdout)
        self.assertEqual(completed.stderr, "")

    def test_installed_flow_diff_empty_scope_notice_on_stderr(self) -> None:
        repo_root = self._init_repo("repo-diff-launcher-empty")
        home = self.tmp_path / "home-launcher-empty"
        install_dir = home / ".local" / "bin"

        run_bootstrap(
            platform="posix",
            repo_root=Path(__file__).resolve().parents[1],
            prefix="flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
        )

        launcher = install_dir / "flow-diff"
        completed = subprocess.run(
            [str(launcher)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(repo_root),
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr, "No diff content for current scope.\n")

    def test_diff_refresh_rejects_incompatible_option_combinations(self) -> None:
        repo_root = self._init_repo("repo-diff-refresh-invalid")

        code, stdout, stderr = self._invoke(repo_root, "diff", "--refresh", "--all")
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("--refresh cannot be combined with --all.", stderr)

        code, stdout, stderr = self._invoke(repo_root, "diff", "--refresh", "--git")
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("--refresh cannot be combined with --git.", stderr)

        code, stdout, stderr = self._invoke(repo_root, "diff", "--git", "--all")
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("--git cannot be combined with --all.", stderr)

    def test_diff_rejects_duplicate_mode_options(self) -> None:
        repo_root = self._init_repo("repo-diff-duplicate-modes")

        code, stdout, stderr = self._invoke(repo_root, "diff", "--refresh", "--refresh")
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("--refresh may be provided at most once.", stderr)

        code, stdout, stderr = self._invoke(repo_root, "diff", "--git", "--git")
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("--git may be provided at most once.", stderr)

        code, stdout, stderr = self._invoke(repo_root, "diff", "--all", "--all")
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("--all may be provided at most once.", stderr)

    def test_diff_refresh_captures_clean_tree_baseline(self) -> None:
        repo_root = self._init_repo("repo-diff-refresh-clean")

        code, stdout, stderr = self._invoke(repo_root, "diff", "--refresh")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(stdout, "Review baseline refreshed.\n")

        baseline = self._load_diff_baseline(repo_root)
        self.assertEqual(baseline["version"], 1)
        repository = baseline["repository"]
        assert isinstance(repository, dict)
        self.assertEqual(repository["head"], self._run_git(repo_root, "rev-parse", "HEAD"))
        self.assertEqual(repository["branch"], "scratch")
        workflow = baseline["workflow"]
        assert isinstance(workflow, dict)
        self.assertEqual(workflow["type"], "issue")
        self.assertEqual(workflow["activeIssueNumber"], 23)
        self.assertEqual(self._status_paths(baseline, "staged"), [])
        self.assertEqual(self._status_paths(baseline, "unstaged"), [])
        self.assertEqual(self._status_paths(baseline, "untracked"), [])
        self.assertEqual(self._status_paths(baseline, "deleted"), [])

    def test_diff_refresh_captures_unstaged_tracked_modification(self) -> None:
        repo_root = self._init_repo("repo-diff-refresh-unstaged")
        (repo_root / "tracked.txt").write_text("base\nunstaged\n", encoding="utf-8")

        code, _, _ = self._invoke(repo_root, "diff", "--refresh")
        self.assertEqual(code, 0)

        baseline = self._load_diff_baseline(repo_root)
        self.assertEqual(self._status_paths(baseline, "staged"), [])
        self.assertEqual(self._status_paths(baseline, "unstaged"), ["tracked.txt"])
        self.assertEqual(self._status_paths(baseline, "untracked"), [])
        self.assertEqual(self._snapshot_bytes(baseline, "working", "tracked.txt"), b"base\nunstaged\n")

    def test_diff_refresh_captures_staged_tracked_modification(self) -> None:
        repo_root = self._init_repo("repo-diff-refresh-staged")
        (repo_root / "tracked.txt").write_text("base\nstaged\n", encoding="utf-8")
        self._run_git(repo_root, "add", "tracked.txt")

        code, _, _ = self._invoke(repo_root, "diff", "--refresh")
        self.assertEqual(code, 0)

        baseline = self._load_diff_baseline(repo_root)
        self.assertEqual(self._status_paths(baseline, "staged"), ["tracked.txt"])
        self.assertEqual(self._status_paths(baseline, "unstaged"), [])
        self.assertEqual(self._snapshot_bytes(baseline, "index", "tracked.txt"), b"base\nstaged\n")

    def test_diff_refresh_captures_staged_and_unstaged_same_file(self) -> None:
        repo_root = self._init_repo("repo-diff-refresh-staged-unstaged")
        (repo_root / "tracked.txt").write_text("base\nstaged\n", encoding="utf-8")
        self._run_git(repo_root, "add", "tracked.txt")
        (repo_root / "tracked.txt").write_text("base\nstaged\nunstaged\n", encoding="utf-8")

        code, _, _ = self._invoke(repo_root, "diff", "--refresh")
        self.assertEqual(code, 0)

        baseline = self._load_diff_baseline(repo_root)
        self.assertEqual(self._status_paths(baseline, "staged"), ["tracked.txt"])
        self.assertEqual(self._status_paths(baseline, "unstaged"), ["tracked.txt"])
        self.assertEqual(self._snapshot_bytes(baseline, "index", "tracked.txt"), b"base\nstaged\n")
        self.assertEqual(
            self._snapshot_bytes(baseline, "working", "tracked.txt"),
            b"base\nstaged\nunstaged\n",
        )

    def test_diff_refresh_captures_untracked_file(self) -> None:
        repo_root = self._init_repo("repo-diff-refresh-untracked")
        (repo_root / "new.txt").write_text("new\n", encoding="utf-8")

        code, _, _ = self._invoke(repo_root, "diff", "--refresh")
        self.assertEqual(code, 0)

        baseline = self._load_diff_baseline(repo_root)
        self.assertEqual(self._status_paths(baseline, "untracked"), ["new.txt"])
        self.assertEqual(self._snapshot_bytes(baseline, "working", "new.txt"), b"new\n")

    def test_diff_refresh_captures_deleted_tracked_file(self) -> None:
        repo_root = self._init_repo("repo-diff-refresh-deleted")
        (repo_root / "delete-me.txt").write_text("delete\n", encoding="utf-8")
        self._run_git(repo_root, "add", "delete-me.txt")
        self._run_git(repo_root, "commit", "-q", "-m", "add delete candidate")
        self._run_git(repo_root, "rm", "delete-me.txt")

        code, _, _ = self._invoke(repo_root, "diff", "--refresh")
        self.assertEqual(code, 0)

        baseline = self._load_diff_baseline(repo_root)
        self.assertEqual(self._status_paths(baseline, "deleted"), ["delete-me.txt"])
        self.assertEqual(self._status_paths(baseline, "stagedDeleted"), ["delete-me.txt"])

    def test_diff_refresh_captures_staged_rename_metadata_and_destination_snapshot(self) -> None:
        repo_root = self._init_repo("repo-diff-refresh-rename")
        (repo_root / "old-name.txt").write_text("rename me\n", encoding="utf-8")
        self._run_git(repo_root, "add", "old-name.txt")
        self._run_git(repo_root, "commit", "-q", "-m", "add rename seed")
        self._run_git(repo_root, "mv", "old-name.txt", "new-name.txt")

        code, _, _ = self._invoke(repo_root, "diff", "--refresh")
        self.assertEqual(code, 0)

        baseline = self._load_diff_baseline(repo_root)
        self.assertEqual(self._status_paths(baseline, "staged"), ["new-name.txt"])
        self.assertEqual(self._status_paths(baseline, "unstaged"), [])
        self.assertEqual(self._snapshot_bytes(baseline, "index", "new-name.txt"), b"rename me\n")
        self.assertEqual(self._snapshot_bytes(baseline, "working", "new-name.txt"), b"rename me\n")
        self.assertEqual(self._rename_copy_entries(baseline), [
            {
                "kind": "rename",
                "sourcePath": "old-name.txt",
                "destinationPath": "new-name.txt",
                "indexStatus": "R",
                "worktreeStatus": " ",
            }
        ])

    def test_diff_refresh_captures_rename_then_unstaged_destination_modification(self) -> None:
        repo_root = self._init_repo("repo-diff-refresh-rename-unstaged")
        (repo_root / "old-name.txt").write_text("rename me\n", encoding="utf-8")
        self._run_git(repo_root, "add", "old-name.txt")
        self._run_git(repo_root, "commit", "-q", "-m", "add rename seed")
        self._run_git(repo_root, "mv", "old-name.txt", "new-name.txt")
        (repo_root / "new-name.txt").write_text("rename me\nplus unstaged\n", encoding="utf-8")

        code, _, _ = self._invoke(repo_root, "diff", "--refresh")
        self.assertEqual(code, 0)

        baseline = self._load_diff_baseline(repo_root)
        self.assertEqual(self._status_paths(baseline, "staged"), ["new-name.txt"])
        self.assertEqual(self._status_paths(baseline, "unstaged"), ["new-name.txt"])
        self.assertEqual(self._snapshot_bytes(baseline, "index", "new-name.txt"), b"rename me\n")
        self.assertEqual(
            self._snapshot_bytes(baseline, "working", "new-name.txt"),
            b"rename me\nplus unstaged\n",
        )
        self.assertEqual(self._rename_copy_entries(baseline), [
            {
                "kind": "rename",
                "sourcePath": "old-name.txt",
                "destinationPath": "new-name.txt",
                "indexStatus": "R",
                "worktreeStatus": "M",
            }
        ])

    def test_diff_refresh_status_parser_supports_copy_tokens(self) -> None:
        entries = cli._parse_flow_diff_status_tokens(["C  copied.txt", "source.txt"])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].index_status, "C")
        self.assertEqual(entries[0].worktree_status, " ")
        self.assertEqual(entries[0].path, "copied.txt")
        self.assertEqual(entries[0].source_path, "source.txt")

    def test_diff_refresh_captures_mixed_dirty_state(self) -> None:
        repo_root = self._init_repo("repo-diff-refresh-mixed")
        (repo_root / "staged.txt").write_text("base\n", encoding="utf-8")
        (repo_root / "both.txt").write_text("base\n", encoding="utf-8")
        (repo_root / "delete.txt").write_text("base\n", encoding="utf-8")
        self._run_git(repo_root, "add", "staged.txt", "both.txt", "delete.txt")
        self._run_git(repo_root, "commit", "-q", "-m", "seed files")

        (repo_root / "tracked.txt").write_text("base\nunstaged\n", encoding="utf-8")
        (repo_root / "staged.txt").write_text("base\nstaged\n", encoding="utf-8")
        self._run_git(repo_root, "add", "staged.txt")
        (repo_root / "both.txt").write_text("base\nstaged\n", encoding="utf-8")
        self._run_git(repo_root, "add", "both.txt")
        (repo_root / "both.txt").write_text("base\nstaged\nunstaged\n", encoding="utf-8")
        self._run_git(repo_root, "rm", "delete.txt")
        (repo_root / "new.txt").write_text("new\n", encoding="utf-8")

        code, _, _ = self._invoke(repo_root, "diff", "--refresh")
        self.assertEqual(code, 0)

        baseline = self._load_diff_baseline(repo_root)
        self.assertEqual(
            self._status_paths(baseline, "staged"),
            ["both.txt", "delete.txt", "staged.txt"],
        )
        self.assertEqual(
            self._status_paths(baseline, "unstaged"),
            ["both.txt", "tracked.txt"],
        )
        self.assertEqual(self._status_paths(baseline, "untracked"), ["new.txt"])
        self.assertEqual(self._status_paths(baseline, "deleted"), ["delete.txt"])

    def test_diff_refresh_replaces_previous_baseline(self) -> None:
        repo_root = self._init_repo("repo-diff-refresh-replace")
        (repo_root / "first.txt").write_text("first\n", encoding="utf-8")

        code, _, _ = self._invoke(repo_root, "diff", "--refresh")
        self.assertEqual(code, 0)
        first_baseline = self._load_diff_baseline(repo_root)
        self.assertEqual(self._status_paths(first_baseline, "untracked"), ["first.txt"])

        (repo_root / "first.txt").unlink()
        (repo_root / "second.txt").write_text("second\n", encoding="utf-8")

        code, _, _ = self._invoke(repo_root, "diff", "--refresh")
        self.assertEqual(code, 0)
        second_baseline = self._load_diff_baseline(repo_root)
        self.assertEqual(self._status_paths(second_baseline, "untracked"), ["second.txt"])
        snapshots = second_baseline["snapshots"]
        assert isinstance(snapshots, dict)
        working = snapshots["working"]
        assert isinstance(working, dict)
        self.assertNotIn("first.txt", working)

    def test_diff_refresh_does_not_mutate_git_or_workflow_state(self) -> None:
        repo_root = self._init_repo("repo-diff-refresh-immutable")
        (repo_root / "tracked.txt").write_text("base\nstaged\n", encoding="utf-8")
        self._run_git(repo_root, "add", "tracked.txt")
        (repo_root / "tracked.txt").write_text("base\nstaged\nunstaged\n", encoding="utf-8")
        (repo_root / "new.txt").write_text("new\n", encoding="utf-8")

        before = self._repo_mutation_fingerprint(repo_root)

        code, _, _ = self._invoke(repo_root, "diff", "--refresh")
        self.assertEqual(code, 0)

        after = self._repo_mutation_fingerprint(repo_root)
        self.assertEqual(before, after)

    def test_diff_no_baseline_keeps_prior_current_uncommitted_behavior(self) -> None:
        repo_root = self._init_repo("repo-diff-no-baseline")
        (repo_root / "tracked.txt").write_text("base\nchanged\n", encoding="utf-8")

        code, stdout, stderr = self._invoke(repo_root, "diff")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("diff --git a/tracked.txt b/tracked.txt", stdout)
        self.assertFalse(self._diff_baseline_path(repo_root).exists())

    def test_diff_git_includes_current_uncommitted_changes(self) -> None:
        repo_root = self._init_repo("repo-diff-git-current")

        (repo_root / "tracked.txt").write_text("base\nstaged\n", encoding="utf-8")
        self._run_git(repo_root, "add", "tracked.txt")
        (repo_root / "tracked.txt").write_text("base\nstaged\nunstaged\n", encoding="utf-8")
        (repo_root / "new.txt").write_text("new\n", encoding="utf-8")

        code, stdout, stderr = self._invoke(repo_root, "diff", "--git")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("diff --git a/tracked.txt b/tracked.txt", stdout)
        self.assertIn("diff --git a/new.txt b/new.txt", stdout)

    def test_diff_git_ignores_invalid_baseline(self) -> None:
        repo_root = self._init_repo("repo-diff-git-invalid-baseline")
        (repo_root / "tracked.txt").write_text("base\nchanged\n", encoding="utf-8")
        self._run_git(repo_root, "add", "tracked.txt")
        (repo_root / "tracked.txt").write_text("base\nchanged\nagain\n", encoding="utf-8")
        (repo_root / "new.txt").write_text("new\n", encoding="utf-8")

        baseline_path = self._diff_baseline_path(repo_root)
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text("{ not valid json\n", encoding="utf-8")

        code, stdout, stderr = self._invoke(repo_root, "diff", "--git")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("diff --git a/tracked.txt b/tracked.txt", stdout)
        self.assertIn("diff --git a/new.txt b/new.txt", stdout)

        code, stdout, stderr = self._invoke(repo_root, "diff")
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Review baseline is stale or invalid; run flow-diff --refresh.", stderr)

        code, stdout, stderr = self._invoke(repo_root, "diff", "--all")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("diff --git a/tracked.txt b/tracked.txt", stdout)

    def test_diff_stdout_is_rejected(self) -> None:
        repo_root = self._init_repo("repo-diff-stdout-rejected")

        code, stdout, stderr = self._invoke(repo_root, "diff", "--stdout")
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Usage: flow-diff [--git|--all] | --refresh", stderr)

    def test_diff_scope_distinguishes_git_and_all(self) -> None:
        repo_root = self._init_repo("repo-diff-scope-distinction")

        (repo_root / "committed.txt").write_text("committed\n", encoding="utf-8")
        self._run_git(repo_root, "add", "committed.txt")
        commit_code, _, commit_err = self._invoke(repo_root, "commit")
        self.assertEqual(commit_code, 0)
        self.assertEqual(commit_err, "")

        (repo_root / "tracked.txt").write_text("base\nstaged\n", encoding="utf-8")
        self._run_git(repo_root, "add", "tracked.txt")
        (repo_root / "tracked.txt").write_text("base\nstaged\nunstaged\n", encoding="utf-8")
        (repo_root / "new.txt").write_text("new\n", encoding="utf-8")

        git_code, git_stdout, git_stderr = self._invoke(repo_root, "diff", "--git")
        self.assertEqual(git_code, 0)
        self.assertEqual(git_stderr, "")
        self.assertIn("diff --git a/tracked.txt b/tracked.txt", git_stdout)
        self.assertIn("diff --git a/new.txt b/new.txt", git_stdout)
        self.assertNotIn("diff --git a/committed.txt b/committed.txt", git_stdout)

        all_code, all_stdout, all_stderr = self._invoke(repo_root, "diff", "--all")
        self.assertEqual(all_code, 0)
        self.assertEqual(all_stderr, "")
        self.assertIn("diff --git a/committed.txt b/committed.txt", all_stdout)
        self.assertIn("diff --git a/tracked.txt b/tracked.txt", all_stdout)
        self.assertIn("diff --git a/new.txt b/new.txt", all_stdout)

    def test_diff_after_clean_refresh_shows_only_post_refresh_tracked_edit(self) -> None:
        repo_root = self._init_repo("repo-diff-relative-clean-edit")
        self._refresh(repo_root)
        (repo_root / "tracked.txt").write_text("base\nafter-refresh\n", encoding="utf-8")

        code, stdout, stderr = self._invoke(repo_root, "diff")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("diff --git a/tracked.txt b/tracked.txt", stdout)
        self.assertIn("+after-refresh", stdout)

    def test_diff_preexisting_tracked_edit_unchanged_after_refresh_is_empty(self) -> None:
        repo_root = self._init_repo("repo-diff-relative-preexisting-unchanged")
        (repo_root / "tracked.txt").write_text("base\npre-refresh\n", encoding="utf-8")
        self._refresh(repo_root)

        code, stdout, stderr = self._invoke(repo_root, "diff")
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "No diff content for current scope.\n")

    def test_diff_preexisting_tracked_edit_changed_again_shows_incremental_delta(self) -> None:
        repo_root = self._init_repo("repo-diff-relative-preexisting-changed-again")
        (repo_root / "tracked.txt").write_text("base\nstate-1\n", encoding="utf-8")
        self._refresh(repo_root)
        (repo_root / "tracked.txt").write_text("base\nstate-2\n", encoding="utf-8")

        code, stdout, stderr = self._invoke(repo_root, "diff")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("-state-1", stdout)
        self.assertIn("+state-2", stdout)

    def test_diff_staged_at_refresh_then_changed_again_compares_from_baseline_working(self) -> None:
        repo_root = self._init_repo("repo-diff-relative-staged-then-working")
        (repo_root / "tracked.txt").write_text("base\nstaged\n", encoding="utf-8")
        self._run_git(repo_root, "add", "tracked.txt")
        (repo_root / "tracked.txt").write_text("base\nstaged\nvisible-at-refresh\n", encoding="utf-8")
        self._refresh(repo_root)
        (repo_root / "tracked.txt").write_text("base\nstaged\nvisible-after-refresh\n", encoding="utf-8")

        code, stdout, stderr = self._invoke(repo_root, "diff")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("-visible-at-refresh", stdout)
        self.assertIn("+visible-after-refresh", stdout)

    def test_diff_untracked_created_after_refresh_is_shown(self) -> None:
        repo_root = self._init_repo("repo-diff-relative-untracked-created")
        self._refresh(repo_root)
        (repo_root / "new-after.txt").write_text("new after\n", encoding="utf-8")

        code, stdout, stderr = self._invoke(repo_root, "diff")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("diff --git a/new-after.txt b/new-after.txt", stdout)

    def test_diff_untracked_present_at_refresh_unchanged_is_empty(self) -> None:
        repo_root = self._init_repo("repo-diff-relative-untracked-unchanged")
        (repo_root / "existing-untracked.txt").write_text("initial\n", encoding="utf-8")
        self._refresh(repo_root)

        code, stdout, stderr = self._invoke(repo_root, "diff")
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "No diff content for current scope.\n")

    def test_diff_untracked_present_at_refresh_edited_shows_incremental_delta(self) -> None:
        repo_root = self._init_repo("repo-diff-relative-untracked-edited")
        (repo_root / "existing-untracked.txt").write_text("initial\n", encoding="utf-8")
        self._refresh(repo_root)
        (repo_root / "existing-untracked.txt").write_text("initial\nedited\n", encoding="utf-8")

        code, stdout, stderr = self._invoke(repo_root, "diff")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("diff --git a/existing-untracked.txt b/existing-untracked.txt", stdout)
        self.assertIn("+edited", stdout)

    def test_diff_baseline_untracked_deleted_after_refresh_shows_deletion(self) -> None:
        repo_root = self._init_repo("repo-diff-relative-untracked-deleted")
        (repo_root / "existing-untracked.txt").write_text("initial\n", encoding="utf-8")
        self._refresh(repo_root)
        (repo_root / "existing-untracked.txt").unlink()

        code, stdout, stderr = self._invoke(repo_root, "diff")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("diff --git a/existing-untracked.txt b/existing-untracked.txt", stdout)
        self.assertIn("deleted file mode", stdout)

    def test_diff_tracked_deletion_after_refresh_is_shown(self) -> None:
        repo_root = self._init_repo("repo-diff-relative-tracked-deleted")
        self._refresh(repo_root)
        (repo_root / "tracked.txt").unlink()

        code, stdout, stderr = self._invoke(repo_root, "diff")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("diff --git a/tracked.txt b/tracked.txt", stdout)
        self.assertIn("deleted file mode", stdout)

    def test_diff_tracked_deletion_already_present_at_refresh_unchanged_is_empty(self) -> None:
        repo_root = self._init_repo("repo-diff-relative-tracked-deleted-unchanged")
        self._run_git(repo_root, "rm", "tracked.txt")
        self._refresh(repo_root)

        code, stdout, stderr = self._invoke(repo_root, "diff")
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "No diff content for current scope.\n")

    def test_diff_baseline_deletion_restored_afterward_is_shown(self) -> None:
        repo_root = self._init_repo("repo-diff-relative-deletion-restored")
        self._run_git(repo_root, "rm", "tracked.txt")
        self._refresh(repo_root)
        (repo_root / "tracked.txt").write_text("restored\n", encoding="utf-8")

        code, stdout, stderr = self._invoke(repo_root, "diff")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("diff --git a/tracked.txt b/tracked.txt", stdout)
        self.assertIn("new file mode", stdout)

    def test_diff_rename_after_refresh_is_shown(self) -> None:
        repo_root = self._init_repo("repo-diff-relative-rename-after")
        self._refresh(repo_root)
        self._run_git(repo_root, "mv", "tracked.txt", "renamed.txt")

        code, stdout, stderr = self._invoke(repo_root, "diff")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("diff --git a/tracked.txt b/tracked.txt", stdout)
        self.assertIn("diff --git a/renamed.txt b/renamed.txt", stdout)

    def test_diff_rename_present_at_refresh_unchanged_is_empty(self) -> None:
        repo_root = self._init_repo("repo-diff-relative-rename-unchanged")
        self._run_git(repo_root, "mv", "tracked.txt", "renamed.txt")
        self._refresh(repo_root)

        code, stdout, stderr = self._invoke(repo_root, "diff")
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "No diff content for current scope.\n")

    def test_diff_rename_present_at_refresh_destination_edit_shows_only_post_refresh_change(self) -> None:
        repo_root = self._init_repo("repo-diff-relative-rename-destination-edit")
        self._run_git(repo_root, "mv", "tracked.txt", "renamed.txt")
        self._refresh(repo_root)
        (repo_root / "renamed.txt").write_text("base\npost-refresh\n", encoding="utf-8")

        code, stdout, stderr = self._invoke(repo_root, "diff")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("diff --git a/renamed.txt b/renamed.txt", stdout)
        self.assertIn("+post-refresh", stdout)
        self.assertNotIn("diff --git a/tracked.txt b/tracked.txt", stdout)

    def test_diff_ai_dev_baseline_updates_never_appear_in_output(self) -> None:
        repo_root = self._init_repo("repo-diff-relative-ai-dev-excluded")
        self._refresh(repo_root)
        self._refresh(repo_root)

        code, stdout, stderr = self._invoke(repo_root, "diff")
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "No diff content for current scope.\n")

    def test_diff_all_ignores_baseline_and_keeps_issue23_scope(self) -> None:
        repo_root = self._init_repo("repo-diff-relative-all-ignores-baseline")
        (repo_root / "tracked.txt").write_text("base\npre-refresh\n", encoding="utf-8")
        self._refresh(repo_root)

        code, stdout, stderr = self._invoke(repo_root, "diff", "--all")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("diff --git a/tracked.txt b/tracked.txt", stdout)
        self.assertIn("+pre-refresh", stdout)

    def test_diff_rejects_unknown_delivery_flag(self) -> None:
        repo_root = self._init_repo("repo-diff-rejects-delivery-flag")

        code, stdout, stderr = self._invoke(repo_root, "diff", "--stdout")
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Usage: flow-diff [--git|--all] | --refresh", stderr)

    def test_diff_malformed_baseline_fails_safely(self) -> None:
        repo_root = self._init_repo("repo-diff-relative-malformed-baseline")
        baseline_path = self._diff_baseline_path(repo_root)
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text("{ not valid json\n", encoding="utf-8")

        code, stdout, stderr = self._invoke(repo_root, "diff")
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Review baseline is stale or invalid; run flow-diff --refresh.", stderr)

    def test_diff_wrong_repository_workflow_or_branch_baseline_fails_safely(self) -> None:
        repo_root = self._init_repo("repo-diff-relative-baseline-identity")
        self._refresh(repo_root)
        baseline = self._load_diff_baseline(repo_root)

        baseline_repo = json.loads(json.dumps(baseline))
        assert isinstance(baseline_repo["repository"], dict)
        baseline_repo["repository"]["root"] = str((repo_root / "other-root"))
        self._write_baseline_payload(repo_root, baseline_repo)
        code, _, stderr = self._invoke(repo_root, "diff")
        self.assertEqual(code, 1)
        self.assertIn("Review baseline is stale or invalid; run flow-diff --refresh.", stderr)

        baseline_branch = json.loads(json.dumps(baseline))
        assert isinstance(baseline_branch["repository"], dict)
        baseline_branch["repository"]["branch"] = "main"
        self._write_baseline_payload(repo_root, baseline_branch)
        code, _, stderr = self._invoke(repo_root, "diff")
        self.assertEqual(code, 1)
        self.assertIn("Review baseline is stale or invalid; run flow-diff --refresh.", stderr)

        baseline_workflow = json.loads(json.dumps(baseline))
        assert isinstance(baseline_workflow["workflow"], dict)
        baseline_workflow["workflow"]["activeIssueNumber"] = 999
        self._write_baseline_payload(repo_root, baseline_workflow)
        code, _, stderr = self._invoke(repo_root, "diff")
        self.assertEqual(code, 1)
        self.assertIn("Review baseline is stale or invalid; run flow-diff --refresh.", stderr)

    def test_diff_relative_comparison_does_not_mutate_git_or_workflow_state(self) -> None:
        repo_root = self._init_repo("repo-diff-relative-immutable")
        (repo_root / "tracked.txt").write_text("base\npre-refresh\n", encoding="utf-8")
        (repo_root / "pre.txt").write_text("pre\n", encoding="utf-8")
        self._refresh(repo_root)
        (repo_root / "tracked.txt").write_text("base\npre-refresh\npost-refresh\n", encoding="utf-8")
        (repo_root / "after.txt").write_text("after\n", encoding="utf-8")

        before = self._repo_mutation_fingerprint(repo_root)
        code, stdout, stderr = self._invoke(repo_root, "diff")
        after = self._repo_mutation_fingerprint(repo_root)

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertTrue(stdout)
        self.assertEqual(before, after)

    def test_diff_manual_scenario_shows_only_post_refresh_delta(self) -> None:
        repo_root = self._init_repo("repo-diff-relative-manual-scenario")

        (repo_root / "tracked.txt").write_text("base\nbefore refresh\n", encoding="utf-8")
        (repo_root / "baseline-untracked.txt").write_text("before refresh untracked\n", encoding="utf-8")
        self._refresh(repo_root)

        (repo_root / "tracked.txt").write_text("base\nbefore refresh\nafter refresh\n", encoding="utf-8")
        (repo_root / "baseline-untracked.txt").write_text(
            "before refresh untracked\nafter refresh untracked\n",
            encoding="utf-8",
        )
        (repo_root / "post-refresh-new.txt").write_text("new after refresh\n", encoding="utf-8")

        code, stdout, stderr = self._invoke(repo_root, "diff")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")

        self.assertIn("diff --git a/tracked.txt b/tracked.txt", stdout)
        self.assertIn("+after refresh", stdout)
        self.assertNotIn("+before refresh", stdout)

        self.assertIn("diff --git a/baseline-untracked.txt b/baseline-untracked.txt", stdout)
        self.assertIn("+after refresh untracked", stdout)
        self.assertNotIn("+before refresh untracked", stdout)

    def test_start_clears_existing_review_baseline(self) -> None:
        repo_root = self._init_repo("repo-diff-start-clears-baseline")
        config_path = repo_root / ".ai-dev" / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps(
                {
                    "tickets": {
                        "provider": "local",
                        "path": ".ai-dev/tickets",
                    }
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        ticket_path = repo_root / ".ai-dev" / "tickets" / "24.json"
        ticket_path.parent.mkdir(parents=True, exist_ok=True)
        ticket_path.write_text(
            json.dumps(
                {
                    "reference": {"provider": "local", "ticketId": "24", "path": ".ai-dev/tickets"},
                    "title": "Ticket 24",
                    "lifecycleState": "open",
                    "workflowState": "inactive",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        self._write_workflow_state(
            repo_root,
            {
                "mainBranch": "main",
                "scratchBranch": "scratch",
                "checkpoint": 0,
            },
        )
        self._ignore_flow_artifacts(repo_root)
        baseline_path = self._diff_baseline_path(repo_root)
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "repository": {
                        "root": str(repo_root),
                        "head": self._run_git(repo_root, "rev-parse", "HEAD"),
                        "branch": "scratch",
                    },
                    "workflow": {
                        "type": "issue",
                        "mainBranch": "main",
                        "scratchBranch": "scratch",
                        "checkpoint": 0,
                        "activeIssueNumber": 23,
                        "patchDescription": None,
                    },
                    "status": {"staged": [], "unstaged": [], "untracked": [], "deleted": [], "stagedDeleted": [], "unstagedDeleted": [], "renameCopy": []},
                    "snapshots": {"index": {}, "working": {}},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        code, _, _ = self._invoke(repo_root, "start", "24")
        self.assertEqual(code, 0)
        self.assertFalse(baseline_path.exists())

    def test_patch_clears_existing_review_baseline(self) -> None:
        repo_root = self._init_repo("repo-diff-patch-clears-baseline")
        self._write_workflow_state(
            repo_root,
            {
                "mainBranch": "main",
                "scratchBranch": "scratch",
                "checkpoint": 0,
            },
        )
        self._ignore_flow_artifacts(repo_root)
        baseline_path = self._diff_baseline_path(repo_root)
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "repository": {
                        "root": str(repo_root),
                        "head": self._run_git(repo_root, "rev-parse", "HEAD"),
                        "branch": "scratch",
                    },
                    "workflow": {
                        "type": "issue",
                        "mainBranch": "main",
                        "scratchBranch": "scratch",
                        "checkpoint": 0,
                        "activeIssueNumber": 23,
                        "patchDescription": None,
                    },
                    "status": {"staged": [], "unstaged": [], "untracked": [], "deleted": [], "stagedDeleted": [], "unstagedDeleted": [], "renameCopy": []},
                    "snapshots": {"index": {}, "working": {}},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        code, _, _ = self._invoke(repo_root, "patch", "Patch baseline clear")
        self.assertEqual(code, 0)
        self.assertFalse(baseline_path.exists())

    def test_reset_clears_existing_review_baseline(self) -> None:
        repo_root = self._init_repo("repo-diff-reset-clears-baseline")
        baseline_path = self._diff_baseline_path(repo_root)
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "repository": {
                        "root": str(repo_root),
                        "head": self._run_git(repo_root, "rev-parse", "HEAD"),
                        "branch": "scratch",
                    },
                    "workflow": {
                        "type": "issue",
                        "mainBranch": "main",
                        "scratchBranch": "scratch",
                        "checkpoint": 0,
                        "activeIssueNumber": 23,
                        "patchDescription": None,
                    },
                    "status": {"staged": [], "unstaged": [], "untracked": [], "deleted": [], "stagedDeleted": [], "unstagedDeleted": [], "renameCopy": []},
                    "snapshots": {"index": {}, "working": {}},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        (repo_root / "tracked.txt").write_text("change\n", encoding="utf-8")
        code, _, _ = self._invoke(repo_root, "reset")
        self.assertEqual(code, 0)
        self.assertFalse(baseline_path.exists())

    def test_complete_clears_existing_review_baseline(self) -> None:
        repo_root = self._init_repo("repo-diff-complete-clears-baseline")
        self._write_workflow_state(
            repo_root,
            {"mainBranch": "main", "scratchBranch": "scratch", "checkpoint": 0, "patchDescription": "Baseline patch"},
        )
        self._ignore_flow_artifacts(repo_root)
        baseline_path = self._diff_baseline_path(repo_root)
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "repository": {
                        "root": str(repo_root),
                        "head": self._run_git(repo_root, "rev-parse", "HEAD"),
                        "branch": "scratch",
                    },
                    "workflow": {
                        "type": "patch",
                        "mainBranch": "main",
                        "scratchBranch": "scratch",
                        "checkpoint": 0,
                        "activeIssueNumber": None,
                        "patchDescription": "Baseline patch",
                    },
                    "status": {"staged": [], "unstaged": [], "untracked": [], "deleted": [], "stagedDeleted": [], "unstagedDeleted": [], "renameCopy": []},
                    "snapshots": {"index": {}, "working": {}},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        code, _, _ = self._invoke(repo_root, "complete")
        self.assertEqual(code, 0)
        self.assertFalse(baseline_path.exists())

    def test_promote_success_clears_existing_review_baseline(self) -> None:
        repo_root = self._init_repo("repo-diff-promote-clears-baseline")
        baseline_path = self._write_diff_baseline_marker(repo_root)

        (repo_root / "promote.txt").write_text("promote\n", encoding="utf-8")
        self._run_git(repo_root, "add", "promote.txt")
        code, _, err = self._invoke(repo_root, "commit")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertFalse(baseline_path.exists())

        baseline_path = self._write_diff_baseline_marker(repo_root)
        code, out, err = self._invoke(repo_root, "promote", "Promote baseline cleanup")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Promoted scratch to main", out)
        self.assertFalse(baseline_path.exists())

    def test_promote_failed_precondition_preserves_review_baseline(self) -> None:
        repo_root = self._init_repo("repo-diff-promote-preserves-baseline-on-failure")
        baseline_path = self._write_diff_baseline_marker(repo_root)

        (repo_root / "tracked.txt").write_text("base\ndirty\n", encoding="utf-8")
        code, out, err = self._invoke(repo_root, "promote", "Promote should fail")

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("repository must be clean", err)
        self.assertTrue(baseline_path.exists())

    def test_promote_success_reports_warning_when_baseline_cleanup_fails(self) -> None:
        repo_root = self._init_repo("repo-diff-promote-cleanup-warning")
        (repo_root / "promote.txt").write_text("promote\n", encoding="utf-8")
        self._run_git(repo_root, "add", "promote.txt")
        self._run_git(repo_root, "commit", "-q", "-m", "scratch promote")

        with patch(
            "ai_dev_flow.cli.clear_diff_baseline_for_repo_root",
            side_effect=cli.RepositoryError("cleanup denied"),
        ):
            code, out, err = self._invoke(repo_root, "promote", "Promote cleanup warning")

        self.assertEqual(code, 0)
        self.assertIn("Promoted scratch to main", out)
        self.assertIn("Warning: review-baseline cleanup failed", err)
        self.assertIn("flow-diff --refresh", err)
        self.assertEqual(
            self._run_git(repo_root, "rev-parse", "main"),
            self._run_git(repo_root, "rev-parse", "scratch"),
        )

    def test_block_success_clears_existing_review_baseline(self) -> None:
        repo_root = self._init_repo("repo-diff-block-clears-baseline")
        self._ignore_flow_artifacts(repo_root)
        baseline_path = self._write_diff_baseline_marker(repo_root)

        with (
            patch(
                "ai_dev_flow.cli._resolve_issue_details_with_labels",
                return_value=(
                    "Issue 23",
                    "https://github.com/jmrozi1/ai-dev/issues/23",
                    ["active"],
                ),
            ),
            patch("ai_dev_flow.cli._reconcile_github_workflow_label", return_value=None),
        ):
            code, out, err = self._invoke(repo_root, "block", "waiting")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Blocked issue 23", out)
        self.assertFalse(baseline_path.exists())

    def test_block_failed_precondition_preserves_review_baseline(self) -> None:
        repo_root = self._init_repo("repo-diff-block-preserves-baseline-on-failure")
        self._ignore_flow_artifacts(repo_root)
        baseline_path = self._write_diff_baseline_marker(repo_root)

        (repo_root / "tracked.txt").write_text("base\ndirty\n", encoding="utf-8")
        code, out, err = self._invoke(repo_root, "block", "waiting")

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("repository must be clean", err)
        self.assertTrue(baseline_path.exists())


if __name__ == "__main__":
    unittest.main()

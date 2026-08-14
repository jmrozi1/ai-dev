from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_dev_flow import cli, repository
from ai_dev_flow.promotion_sync import load_promotion_sync_record
from ai_dev_flow.workflow_state import load_state


class RemotePromotionLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _git(self, repo: Path, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(repo), *arguments],
            check=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return completed.stdout.strip()

    def _build_repo(self, name: str, *, tracked_upstream: bool = True) -> tuple[Path, Path | None]:
        remote: Path | None = None
        if tracked_upstream:
            remote = self.tmp_path / f"{name}-remote.git"
            subprocess.run(["git", "init", "--bare", "--quiet", str(remote)], check=True)

        repo = self.tmp_path / name
        repo.mkdir()
        self._git(repo, "init", "--quiet")
        self._git(repo, "config", "user.name", "Remote Promotion Lifecycle Tests")
        self._git(repo, "config", "user.email", "remote-promotion-lifecycle@example.com")
        (repo / ".gitignore").write_text(".ai-dev/\n", encoding="utf-8")
        (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._git(repo, "add", ".gitignore", "tracked.txt")
        self._git(repo, "commit", "--quiet", "-m", "initial")
        self._git(repo, "branch", "-M", "main")
        if remote is not None:
            self._git(repo, "remote", "add", "shared", str(remote))
            self._git(repo, "push", "--quiet", "-u", "shared", "main")
            self._git(remote, "symbolic-ref", "HEAD", "refs/heads/main")

        self._git(repo, "checkout", "--quiet", "-b", "scratch")
        state_dir = repo / ".ai-dev"
        state_dir.mkdir()
        state_dir.joinpath("workflow.json").write_text(
            json.dumps(
                {
                    "activeIssueNumber": 36,
                    "mainBranch": "main",
                    "scratchBranch": "scratch",
                    "checkpoint": 1,
                }
            ),
            encoding="utf-8",
        )
        state_dir.joinpath("config.json").write_text(
            json.dumps({"review": {"promotionGate": False}}),
            encoding="utf-8",
        )
        return repo, remote

    def _commit(self, repo: Path, filename: str, message: str) -> None:
        (repo / filename).write_text(f"{filename}\n", encoding="utf-8")
        self._git(repo, "add", filename)
        self._git(repo, "commit", "--quiet", "-m", message)

    def _add_scratch_change(self, repo: Path) -> None:
        self._commit(repo, "scratch.txt", "scratch change")

    def _advance_remote(self, remote: Path, name: str) -> None:
        writer = self.tmp_path / name
        subprocess.run(["git", "clone", "--quiet", str(remote), str(writer)], check=True)
        self._git(writer, "config", "user.name", "Remote Writer")
        self._git(writer, "config", "user.email", "remote-writer@example.com")
        self._git(writer, "checkout", "--quiet", "main")
        self._commit(writer, "remote.txt", "remote advance")
        self._git(writer, "push", "--quiet", "origin", "main")

    def _invoke(self, repo: Path, arguments: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        previous_cwd = Path.cwd()
        previous_argv = list(sys.argv)
        previous_command_name = os.environ.get("FLOW_COMMAND_NAME")
        os.environ["FLOW_COMMAND_NAME"] = "flow-promote"
        sys.argv = ["flow-promote", cli._DIRECT_FLOW_ROUTE_TOKEN, "promote", *arguments]
        os.chdir(repo)
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
            if previous_command_name is None:
                os.environ.pop("FLOW_COMMAND_NAME", None)
            else:
                os.environ["FLOW_COMMAND_NAME"] = previous_command_name
        return code, stdout.getvalue(), stderr.getvalue()

    def _heads(self, repo: Path) -> tuple[str, str]:
        return self._git(repo, "rev-parse", "main"), self._git(repo, "rev-parse", "scratch")

    def _fail_initial_push(self, repo: Path) -> tuple[int, str, str]:
        with patch(
            "ai_dev_flow.cli.push_main_to_tracked_upstream",
            side_effect=repository.RepositoryError("simulated push failure"),
        ):
            return self._invoke(repo, ["Ship changes"])

    def test_tracked_upstream_promotes_once_pushes_and_marks_synchronized(self) -> None:
        repo, remote = self._build_repo("success")
        assert remote is not None
        self._add_scratch_change(repo)
        main_before = self._git(repo, "rev-parse", "main")

        pending_statuses: list[str | None] = []
        original_push = cli.push_main_to_tracked_upstream

        def capture_pending_record(repo_root: Path, *, main_branch: str, upstream: object) -> None:
            record_before_push = load_promotion_sync_record(repo_root)
            pending_statuses.append(None if record_before_push is None else record_before_push.status)
            original_push(repo_root, main_branch=main_branch, upstream=upstream)

        with patch("ai_dev_flow.cli.push_main_to_tracked_upstream", side_effect=capture_pending_record):
            code, _, err = self._invoke(repo, ["Ship changes"])

        self.assertEqual(code, 0, err)
        self.assertEqual(pending_statuses, ["pending"])
        main_head, scratch_head = self._heads(repo)
        self.assertEqual(main_head, scratch_head)
        self.assertEqual(self._git(repo, "rev-list", "--count", f"{main_before}..main"), "1")
        self.assertEqual(self._git(remote, "rev-parse", "refs/heads/main"), main_head)
        record = load_promotion_sync_record(repo)
        assert record is not None
        self.assertEqual(record.status, "synchronized")
        self.assertEqual(record.promoted_main_commit, main_head)

    def test_push_failure_preserves_pending_promoted_state_and_retry_pushes_once(self) -> None:
        repo, remote = self._build_repo("retry")
        assert remote is not None
        self._add_scratch_change(repo)
        main_before = self._git(repo, "rev-parse", "main")

        code, _, err = self._fail_initial_push(repo)

        self.assertEqual(code, 1)
        self.assertIn("Local promotion succeeded", err)
        promoted_main, promoted_scratch = self._heads(repo)
        self.assertEqual(promoted_main, promoted_scratch)
        record = load_promotion_sync_record(repo)
        assert record is not None
        self.assertEqual(record.status, "pending")
        self.assertEqual(load_state(repo / ".ai-dev" / "workflow.json").checkpoint, 0)

        retry_code, _, retry_err = self._invoke(repo, [])

        self.assertEqual(retry_code, 0, retry_err)
        self.assertEqual(self._heads(repo), (promoted_main, promoted_scratch))
        self.assertEqual(self._git(repo, "rev-list", "--count", f"{main_before}..main"), "1")
        self.assertEqual(self._git(remote, "rev-parse", "refs/heads/main"), promoted_main)
        synchronized = load_promotion_sync_record(repo)
        assert synchronized is not None
        self.assertEqual(synchronized.status, "synchronized")

    def test_ambiguous_push_failure_retries_without_second_push(self) -> None:
        repo, _ = self._build_repo("ambiguous")
        self._add_scratch_change(repo)
        main_before = self._git(repo, "rev-parse", "main")

        def push_then_fail(repo_root: Path, *, main_branch: str, upstream: object) -> None:
            repository.push_main_to_tracked_upstream(
                repo_root,
                main_branch=main_branch,
                upstream=upstream,
            )
            raise repository.RepositoryError("lost remote response")

        with patch("ai_dev_flow.cli.push_main_to_tracked_upstream", side_effect=push_then_fail):
            code, _, _ = self._invoke(repo, ["Ship changes"])
        self.assertEqual(code, 1)
        promoted_main, promoted_scratch = self._heads(repo)

        with patch("ai_dev_flow.cli.push_main_to_tracked_upstream") as push:
            retry_code, _, retry_err = self._invoke(repo, [])
        self.assertEqual(retry_code, 0, retry_err)
        push.assert_not_called()
        self.assertEqual(self._heads(repo), (promoted_main, promoted_scratch))
        self.assertEqual(self._git(repo, "rev-list", "--count", f"{main_before}..main"), "1")
        record = load_promotion_sync_record(repo)
        assert record is not None
        self.assertEqual(record.status, "synchronized")

    def test_diverged_remote_after_failed_push_refuses_and_keeps_pending_record(self) -> None:
        repo, remote = self._build_repo("diverged")
        assert remote is not None
        self._add_scratch_change(repo)
        self.assertEqual(self._fail_initial_push(repo)[0], 1)
        promoted_heads = self._heads(repo)
        self._advance_remote(remote, "diverged-writer")

        retry_code, _, retry_err = self._invoke(repo, [])

        self.assertEqual(retry_code, 1)
        self.assertIn("has diverged", retry_err)
        self.assertEqual(self._heads(repo), promoted_heads)
        record = load_promotion_sync_record(repo)
        assert record is not None
        self.assertEqual(record.status, "pending")

    def test_no_message_without_matching_pending_record_is_rejected(self) -> None:
        repo, _ = self._build_repo("no-message")
        self._add_scratch_change(repo)

        code, _, err = self._invoke(repo, [])

        self.assertEqual(code, 1)
        self.assertIn("Usage: flow-promote", err)

    def test_no_upstream_retains_local_only_promotion(self) -> None:
        repo, _ = self._build_repo("local-only", tracked_upstream=False)
        self._add_scratch_change(repo)

        code, _, err = self._invoke(repo, ["Ship changes"])

        self.assertEqual(code, 0, err)
        self.assertEqual(*self._heads(repo))
        self.assertIsNone(load_promotion_sync_record(repo))


if __name__ == "__main__":
    unittest.main()
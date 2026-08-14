from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ai_dev_flow import repository


class RemoteRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _git(self, repo_root: Path, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *arguments],
            check=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return completed.stdout.strip()

    def _build_tracked_repository(self, name: str) -> tuple[Path, Path]:
        remote = self.tmp_path / f"{name}-remote.git"
        subprocess.run(["git", "init", "--bare", "--quiet", str(remote)], check=True)

        repo = self.tmp_path / name
        repo.mkdir()
        self._git(repo, "init", "--quiet")
        self._git(repo, "config", "user.name", "Remote Repository Tests")
        self._git(repo, "config", "user.email", "remote-repository-tests@example.com")
        (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._git(repo, "add", "tracked.txt")
        self._git(repo, "commit", "--quiet", "-m", "initial")
        self._git(repo, "branch", "-M", "main")
        self._git(repo, "remote", "add", "shared", str(remote))
        self._git(repo, "push", "--quiet", "-u", "shared", "main")
        return repo, remote

    def _advance_remote(self, remote: Path, name: str) -> None:
        writer = self.tmp_path / name
        subprocess.run(["git", "clone", "--quiet", str(remote), str(writer)], check=True)
        self._git(writer, "config", "user.name", "Remote Writer")
        self._git(writer, "config", "user.email", "remote-writer@example.com")
        self._git(writer, "checkout", "--quiet", "-b", "main", "origin/main")
        (writer / "remote.txt").write_text("remote advance\n", encoding="utf-8")
        self._git(writer, "add", "remote.txt")
        self._git(writer, "commit", "--quiet", "-m", "remote advance")
        self._git(writer, "push", "--quiet", "origin", "main")

    def _commit_on_main(self, repo: Path, name: str) -> None:
        (repo / name).write_text(f"{name}\n", encoding="utf-8")
        self._git(repo, "add", name)
        self._git(repo, "commit", "--quiet", "-m", name)

    def test_resolve_tracked_upstream_uses_configured_remote(self) -> None:
        repo, _ = self._build_tracked_repository("configured")

        upstream = repository.resolve_tracked_upstream(repo, branch_name="main")

        self.assertEqual(upstream, repository.TrackedUpstream("main", "shared", "refs/heads/main"))

    def test_resolve_tracked_upstream_returns_none_without_upstream(self) -> None:
        repo = self.tmp_path / "local"
        repo.mkdir()
        self._git(repo, "init", "--quiet")
        self._git(repo, "config", "user.name", "Remote Repository Tests")
        self._git(repo, "config", "user.email", "remote-repository-tests@example.com")
        (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._git(repo, "add", "tracked.txt")
        self._git(repo, "commit", "--quiet", "-m", "initial")
        self._git(repo, "branch", "-M", "main")

        self.assertIsNone(repository.resolve_tracked_upstream(repo, branch_name="main"))

    def test_compare_classifies_equal_and_upstream_behind(self) -> None:
        repo, _ = self._build_tracked_repository("ahead")
        upstream = repository.resolve_tracked_upstream(repo, branch_name="main")
        assert upstream is not None

        repository.fetch_tracked_upstream(repo, upstream=upstream)
        self.assertEqual(
            repository.compare_main_to_tracked_upstream(
                repo,
                main_branch="main",
                upstream=upstream,
            ).relationship,
            "equal",
        )

        self._commit_on_main(repo, "local.txt")
        self.assertEqual(
            repository.compare_main_to_tracked_upstream(
                repo,
                main_branch="main",
                upstream=upstream,
            ).relationship,
            "upstream-behind",
        )

    def test_fetch_refreshes_stale_tracking_ref_and_classifies_upstream_ahead(self) -> None:
        repo, remote = self._build_tracked_repository("behind")
        upstream = repository.resolve_tracked_upstream(repo, branch_name="main")
        assert upstream is not None
        stale_tracking_head = self._git(repo, "rev-parse", "refs/remotes/shared/main")

        self._advance_remote(remote, "behind-writer")
        self.assertEqual(self._git(repo, "rev-parse", "refs/remotes/shared/main"), stale_tracking_head)

        repository.fetch_tracked_upstream(repo, upstream=upstream)
        comparison = repository.compare_main_to_tracked_upstream(
            repo,
            main_branch="main",
            upstream=upstream,
        )

        self.assertEqual(comparison.relationship, "upstream-ahead")
        self.assertNotEqual(comparison.upstream_head, stale_tracking_head)

    def test_compare_classifies_diverged(self) -> None:
        repo, remote = self._build_tracked_repository("diverged")
        upstream = repository.resolve_tracked_upstream(repo, branch_name="main")
        assert upstream is not None
        self._commit_on_main(repo, "local.txt")
        self._advance_remote(remote, "diverged-writer")

        repository.fetch_tracked_upstream(repo, upstream=upstream)
        self.assertEqual(
            repository.compare_main_to_tracked_upstream(
                repo,
                main_branch="main",
                upstream=upstream,
            ).relationship,
            "diverged",
        )

    def test_push_main_to_tracked_upstream_is_ordinary_and_succeeds(self) -> None:
        repo, remote = self._build_tracked_repository("push")
        upstream = repository.resolve_tracked_upstream(repo, branch_name="main")
        assert upstream is not None
        self._commit_on_main(repo, "local.txt")

        calls: list[list[str]] = []
        original_run_git = repository._run_git

        def record_run_git(repo_root: Path, arguments: list[str], *, check: bool):
            calls.append(arguments)
            return original_run_git(repo_root, arguments, check=check)

        with patch("ai_dev_flow.repository._run_git", side_effect=record_run_git):
            repository.push_main_to_tracked_upstream(
                repo,
                main_branch="main",
                upstream=upstream,
            )

        push_calls = [arguments for arguments in calls if arguments and arguments[0] == "push"]
        self.assertEqual(push_calls, [["push", "--porcelain", "shared", "main:refs/heads/main"]])
        self.assertFalse(any("force" in option for option in push_calls[0]))
        self.assertEqual(
            self._git(remote, "rev-parse", "refs/heads/main"),
            self._git(repo, "rev-parse", "main"),
        )


if __name__ == "__main__":
    unittest.main()
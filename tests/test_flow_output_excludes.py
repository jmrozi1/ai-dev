from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

from ai_dev_flow.repository import sync_local_excludes


class FlowOutputExcludeTests(unittest.TestCase):
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
        self._run_git(repo_root, "config", "user.name", "Flow Output Exclude Tests")
        self._run_git(repo_root, "config", "user.email", "flow-output-exclude-tests@example.com")
        (repo_root / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._run_git(repo_root, "add", "tracked.txt")
        self._run_git(repo_root, "commit", "-q", "-m", "initial commit")
        self._run_git(repo_root, "branch", "-M", "main")

        return repo_root

    def _exclude_path(self, repo_root: Path) -> Path:
        resolved = self._run_git(repo_root, "rev-parse", "--git-path", "info/exclude")
        candidate = Path(resolved)
        if candidate.is_absolute():
            return candidate
        return repo_root / candidate

    def _exclude_text(self, repo_root: Path) -> str:
        exclude_path = self._exclude_path(repo_root)
        if not exclude_path.exists():
            return ""
        return exclude_path.read_text(encoding="utf-8")

    def _managed_block_lines(self, repo_root: Path) -> list[str]:
        lines = self._exclude_text(repo_root).splitlines()
        begin = "# BEGIN ai-dev managed excludes"
        end = "# END ai-dev managed excludes"
        self.assertIn(begin, lines)
        self.assertIn(end, lines)
        return lines[lines.index(begin) + 1 : lines.index(end)]

    def test_sync_creates_managed_block_with_ai_dev_only(self) -> None:
        repo_root = self._init_repo("repo-create")

        sync_local_excludes(repo_root)

        self.assertEqual(self._managed_block_lines(repo_root), [".ai-dev/"])

    def test_sync_preserves_user_excludes(self) -> None:
        repo_root = self._init_repo("repo-preserve")
        exclude_path = self._exclude_path(repo_root)
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        exclude_path.write_text("# user\n*.tmp\nout.txt\n", encoding="utf-8")

        sync_local_excludes(repo_root)

        exclude_text = self._exclude_text(repo_root)
        self.assertIn("# user\n", exclude_text)
        self.assertIn("*.tmp\n", exclude_text)
        self.assertIn("out.txt\n", exclude_text)
        self.assertEqual(self._managed_block_lines(repo_root), [".ai-dev/"])

    def test_sync_replaces_stale_managed_block_entries(self) -> None:
        repo_root = self._init_repo("repo-rewrite")
        exclude_path = self._exclude_path(repo_root)
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        exclude_path.write_text(
            "# user\n"
            "*.tmp\n"
            "# BEGIN ai-dev managed excludes\n"
            ".ai-dev/\n"
            "old-output.txt\n"
            "# END ai-dev managed excludes\n",
            encoding="utf-8",
        )

        sync_local_excludes(repo_root)

        self.assertEqual(self._managed_block_lines(repo_root), [".ai-dev/"])
        self.assertIn("*.tmp\n", self._exclude_text(repo_root))

    def test_sync_is_idempotent(self) -> None:
        repo_root = self._init_repo("repo-idempotent")

        sync_local_excludes(repo_root)
        first_text = self._exclude_text(repo_root)
        sync_local_excludes(repo_root)
        second_text = self._exclude_text(repo_root)

        self.assertEqual(first_text, second_text)
        self.assertEqual(self._managed_block_lines(repo_root).count(".ai-dev/"), 1)

    def test_worktree_uses_worktree_local_excludes(self) -> None:
        repo_root = self._init_repo("repo-worktree")
        worktree_root = self.tmp_path / "linked-worktree"
        self._run_git(repo_root, "worktree", "add", "-b", "scratch-worktree", str(worktree_root), "HEAD")

        sync_local_excludes(worktree_root)

        self.assertEqual(self._managed_block_lines(worktree_root), [".ai-dev/"])


if __name__ == "__main__":
    unittest.main()

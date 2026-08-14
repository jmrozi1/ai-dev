from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


class RemotePromotionPreflightTests(unittest.TestCase):
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

    def _build_repo(self, name: str, *, tracked_upstream: bool, gate_enabled: bool = False) -> tuple[Path, Path | None]:
        remote: Path | None = None
        if tracked_upstream:
            remote = self.tmp_path / f"{name}-remote.git"
            subprocess.run(["git", "init", "--bare", "--quiet", str(remote)], check=True)

        repo = self.tmp_path / name
        repo.mkdir()
        self._git(repo, "init", "--quiet")
        self._git(repo, "config", "user.name", "Remote Promotion Tests")
        self._git(repo, "config", "user.email", "remote-promotion-tests@example.com")
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
        if not gate_enabled:
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

    def _promote(self, repo: Path) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(REPO_ROOT)
        return subprocess.run(
            ["python3", "-m", "ai_dev_flow.cli", "__ai_dev_flow_exec__", "promote", "Ship changes"],
            cwd=repo,
            capture_output=True,
            text=True,
            env=environment,
        )

    def _heads(self, repo: Path) -> tuple[str, str]:
        return self._git(repo, "rev-parse", "main"), self._git(repo, "rev-parse", "scratch")

    def test_no_upstream_promotes_locally(self) -> None:
        repo, _ = self._build_repo("local-only", tracked_upstream=False)
        self._add_scratch_change(repo)

        result = self._promote(repo)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(*self._heads(repo))

    def test_equal_upstream_passes_preflight_and_reaches_review_gate(self) -> None:
        repo, _ = self._build_repo("equal-gate", tracked_upstream=True, gate_enabled=True)
        self._add_scratch_change(repo)

        result = self._promote(repo)

        self.assertEqual(result.returncode, 1)
        self.assertIn("promotion review gate", result.stderr)

    def test_upstream_behind_local_main_promotes(self) -> None:
        repo, _ = self._build_repo("behind", tracked_upstream=True)
        self._git(repo, "checkout", "--quiet", "main")
        self._commit(repo, "local-main.txt", "local main advance")
        self._git(repo, "checkout", "--quiet", "scratch")
        self._git(repo, "reset", "--hard", "main")
        self._add_scratch_change(repo)

        result = self._promote(repo)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(*self._heads(repo))

    def test_upstream_ahead_after_stale_tracking_refuses_without_mutation(self) -> None:
        repo, remote = self._build_repo("ahead", tracked_upstream=True)
        assert remote is not None
        self._add_scratch_change(repo)
        stale_tracking = self._git(repo, "rev-parse", "refs/remotes/shared/main")
        before = self._heads(repo)
        self._advance_remote(remote, "ahead-writer")

        result = self._promote(repo)

        self.assertEqual(result.returncode, 1)
        self.assertIn("is ahead of local main", result.stderr)
        self.assertEqual(self._heads(repo), before)
        self.assertNotEqual(self._git(repo, "rev-parse", "refs/remotes/shared/main"), stale_tracking)

    def test_diverged_upstream_refuses_without_mutation(self) -> None:
        repo, remote = self._build_repo("diverged", tracked_upstream=True)
        assert remote is not None
        self._git(repo, "checkout", "--quiet", "main")
        self._commit(repo, "local-main.txt", "local main advance")
        self._git(repo, "checkout", "--quiet", "scratch")
        self._git(repo, "reset", "--hard", "main")
        self._add_scratch_change(repo)
        before = self._heads(repo)
        self._advance_remote(remote, "diverged-writer")

        result = self._promote(repo)

        self.assertEqual(result.returncode, 1)
        self.assertIn("has diverged from local main", result.stderr)
        self.assertEqual(self._heads(repo), before)

    def test_fetch_failure_refuses_without_mutation(self) -> None:
        repo, _ = self._build_repo("fetch-failure", tracked_upstream=True)
        self._add_scratch_change(repo)
        before = self._heads(repo)
        self._git(repo, "remote", "set-url", "shared", str(self.tmp_path / "missing.git"))

        result = self._promote(repo)

        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot fetch tracked upstream", result.stderr)
        self.assertEqual(self._heads(repo), before)


if __name__ == "__main__":
    unittest.main()
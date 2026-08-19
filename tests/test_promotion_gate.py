from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


class PromotionReviewGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp_dir.name)
        self.addCleanup(self.tmp_dir.cleanup)

        self.repo = self.tmp_path / "repo"
        self.repo.mkdir()
        self._build_repository(self.repo)

    def _build_repository(self, repo: Path) -> None:
        _git(repo, "init", "--quiet")
        _git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
        _git(repo, "config", "user.email", "flow@example.test")
        _git(repo, "config", "user.name", "Flow Test")

        (repo / ".gitignore").write_text(".ai-dev/\n", encoding="utf-8")
        (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        _git(repo, "add", ".gitignore", "tracked.txt")
        _git(repo, "commit", "--quiet", "-m", "initial")

        _git(repo, "checkout", "--quiet", "-b", "scratch")

        workflow_dir = repo / ".ai-dev"
        workflow_dir.mkdir()
        (workflow_dir / "workflow.json").write_text(
            json.dumps(
                {
                    "mainBranch": "main",
                    "scratchBranch": "scratch",
                    "checkpoint": 2,
                    "activeIssueNumber": 42,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def _set_config(self, payload: dict[str, object]) -> None:
        config_dir = self.repo / ".ai-dev"
        config_dir.mkdir(exist_ok=True)
        (config_dir / "config.json").write_text(
            json.dumps(payload, indent=2),
            encoding="utf-8",
        )

    def _write_review_record(self, *, scratch_commit: str | None, result: str = "pass") -> None:
        record_dir = self.repo / ".ai-dev"
        record_dir.mkdir(exist_ok=True)
        payload = {
            "version": 1,
            "scratchCommit": scratch_commit,
            "result": result,
            "mainBranch": "main",
            "scratchBranch": "scratch",
            "activeIssueNumber": 42,
        }
        (record_dir / "promotion-review.json").write_text(
            json.dumps(payload, indent=2),
            encoding="utf-8",
        )

    def _promote(self, message: str = "Ship changes") -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(REPO_ROOT)
        return subprocess.run(
            ["python3", "-m", "ai_dev_flow.cli", "__ai_dev_flow_exec__", "promote", message],
            cwd=self.repo,
            capture_output=True,
            text=True,
            env=environment,
        )

    def _record(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        script = REPO_ROOT / "skills" / "copilot" / "auto-review" / "scripts" / "record-promotion-review"
        return subprocess.run(
            [str(script), *arguments],
            cwd=self.repo,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        )

    def _assert_no_write(self, *, before_bytes: bytes | None, result: subprocess.CompletedProcess[str]) -> None:
        record_path = self.repo / ".ai-dev" / "promotion-review.json"
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no promotion-review record was written", result.stderr)
        self.assertIn("record-promotion-review --issue <number> --commit <sha>", result.stderr)
        after_bytes = record_path.read_bytes() if record_path.exists() else None
        self.assertEqual(after_bytes, before_bytes)

    def test_gate_is_required_by_default(self) -> None:
        (self.repo / "scratch.txt").write_text("scratch\n", encoding="utf-8")
        _git(self.repo, "add", "scratch.txt")
        _git(self.repo, "commit", "--quiet", "-m", "scratch change")

        result = self._promote()

        self.assertEqual(result.returncode, 1)
        self.assertIn("Cannot promote workflow", result.stderr)
        self.assertIn("promotion review", result.stderr)

    def test_gate_blocks_missing_pass_record_when_enabled(self) -> None:
        self._set_config({"review": {"promotionGate": True}})
        (self.repo / "scratch.txt").write_text("scratch\n", encoding="utf-8")
        _git(self.repo, "add", "scratch.txt")
        _git(self.repo, "commit", "--quiet", "-m", "scratch change")

        result = self._promote()

        self.assertEqual(result.returncode, 1)
        self.assertIn("Cannot promote workflow", result.stderr)
        self.assertIn("promotion review", result.stderr)

    def test_gate_accepts_matching_pass_record(self) -> None:
        self._set_config({"review": {"promotionGate": True}})
        (self.repo / "scratch.txt").write_text("scratch\n", encoding="utf-8")
        _git(self.repo, "add", "scratch.txt")
        _git(self.repo, "commit", "--quiet", "-m", "scratch change")
        current_commit = _git(self.repo, "rev-parse", "HEAD").strip()
        self._write_review_record(scratch_commit=current_commit)

        result = self._promote()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Promoted scratch to main", result.stdout)

    def test_gate_rejects_stale_pass_record(self) -> None:
        self._set_config({"review": {"promotionGate": True}})
        (self.repo / "scratch.txt").write_text("first\n", encoding="utf-8")
        _git(self.repo, "add", "scratch.txt")
        _git(self.repo, "commit", "--quiet", "-m", "scratch change")
        stale_commit = _git(self.repo, "rev-parse", "HEAD").strip()
        self._write_review_record(scratch_commit=stale_commit)

        (self.repo / "scratch.txt").write_text("second\n", encoding="utf-8")
        _git(self.repo, "add", "scratch.txt")
        _git(self.repo, "commit", "--quiet", "-m", "later scratch change")

        result = self._promote()

        self.assertEqual(result.returncode, 1)
        self.assertIn("Cannot promote workflow", result.stderr)

    def test_no_argument_records_pass_for_safe_active_issue(self) -> None:
        (self.repo / "scratch.txt").write_text("scratch\n", encoding="utf-8")
        _git(self.repo, "add", "scratch.txt")
        _git(self.repo, "commit", "--quiet", "-m", "scratch change")
        scratch_sha = _git(self.repo, "rev-parse", "HEAD").strip()

        result = self._record()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("recorded promotion review pass", result.stdout)
        payload = json.loads((self.repo / ".ai-dev" / "promotion-review.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["scratchCommit"], scratch_sha)
        self.assertEqual(payload["activeIssueNumber"], 42)
        self.assertEqual(payload["result"], "pass")
        self.assertEqual(payload["mainBranch"], "main")
        self.assertEqual(payload["scratchBranch"], "scratch")

    def test_no_argument_matches_explicit_record_for_same_target(self) -> None:
        (self.repo / "scratch.txt").write_text("scratch\n", encoding="utf-8")
        _git(self.repo, "add", "scratch.txt")
        _git(self.repo, "commit", "--quiet", "-m", "scratch change")
        scratch_sha = _git(self.repo, "rev-parse", "HEAD").strip()

        no_arg = self._record()
        self.assertEqual(no_arg.returncode, 0, no_arg.stderr)
        inferred = json.loads((self.repo / ".ai-dev" / "promotion-review.json").read_text(encoding="utf-8"))

        explicit = self._record("--issue", "42", "--commit", scratch_sha)
        self.assertEqual(explicit.returncode, 0, explicit.stderr)
        explicit_payload = json.loads((self.repo / ".ai-dev" / "promotion-review.json").read_text(encoding="utf-8"))

        self.assertEqual(inferred, explicit_payload)

    def test_no_argument_fails_for_inactive_workflow_without_mutation(self) -> None:
        workflow_path = self.repo / ".ai-dev" / "workflow.json"
        workflow_path.write_text(json.dumps({"mainBranch": "main", "scratchBranch": "scratch", "checkpoint": 0}, indent=2), encoding="utf-8")
        record_path = self.repo / ".ai-dev" / "promotion-review.json"
        before_bytes = record_path.read_bytes() if record_path.exists() else None
        result = self._record()
        self._assert_no_write(before_bytes=before_bytes, result=result)

    def test_no_argument_fails_for_patch_workflow_without_mutation(self) -> None:
        workflow_path = self.repo / ".ai-dev" / "workflow.json"
        workflow_path.write_text(json.dumps({"mainBranch": "main", "scratchBranch": "scratch", "checkpoint": 0, "patchDescription": "patch"}, indent=2), encoding="utf-8")
        record_path = self.repo / ".ai-dev" / "promotion-review.json"
        before_bytes = record_path.read_bytes() if record_path.exists() else None
        result = self._record()
        self._assert_no_write(before_bytes=before_bytes, result=result)

    def test_no_argument_fails_for_wrong_branch_without_mutation(self) -> None:
        _git(self.repo, "checkout", "--quiet", "main")
        before_bytes = (self.repo / ".ai-dev" / "promotion-review.json").read_bytes() if (self.repo / ".ai-dev" / "promotion-review.json").exists() else None
        result = self._record()
        self._assert_no_write(before_bytes=before_bytes, result=result)

    def test_no_argument_fails_for_dirty_worktree_without_mutation(self) -> None:
        (self.repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        record_path = self.repo / ".ai-dev" / "promotion-review.json"
        before_bytes = record_path.read_bytes() if record_path.exists() else None
        result = self._record()
        self._assert_no_write(before_bytes=before_bytes, result=result)

    def test_no_argument_fails_for_head_and_scratch_mismatch_without_mutation(self) -> None:
        (self.repo / "scratch.txt").write_text("scratch\n", encoding="utf-8")
        _git(self.repo, "add", "scratch.txt")
        _git(self.repo, "commit", "--quiet", "-m", "scratch change")
        _git(self.repo, "checkout", "--quiet", "main")
        _git(self.repo, "checkout", "--quiet", "scratch")
        _git(self.repo, "checkout", "--quiet", "main")
        record_path = self.repo / ".ai-dev" / "promotion-review.json"
        before_bytes = record_path.read_bytes() if record_path.exists() else None
        result = self._record()
        self._assert_no_write(before_bytes=before_bytes, result=result)

    def test_no_argument_preserves_existing_record_on_failure(self) -> None:
        existing = {"version": 1, "scratchCommit": "deadbeef", "result": "pass", "mainBranch": "main", "scratchBranch": "scratch", "activeIssueNumber": 42}
        record_path = self.repo / ".ai-dev" / "promotion-review.json"
        record_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
        before_bytes = record_path.read_bytes()
        workflow_path = self.repo / ".ai-dev" / "workflow.json"
        workflow_path.write_text(json.dumps({"mainBranch": "main", "scratchBranch": "scratch", "checkpoint": 0}, indent=2), encoding="utf-8")
        result = self._record()
        self._assert_no_write(before_bytes=before_bytes, result=result)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER_RELATIVE_PATH = Path("skills") / "copilot" / "auto-review" / "scripts" / "review-evidence"


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


class ReviewEvidenceHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp_dir.name)
        self.addCleanup(self.tmp_dir.cleanup)

        self.other_repo = self.tmp_path / "other-repo"
        self.other_repo.mkdir()
        self._build_repository(self.other_repo)

        self.installed_helper = self._install_skill_by_symlink()

    def _build_repository(self, repo: Path) -> None:
        _git(repo, "init", "--quiet")
        _git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
        _git(repo, "config", "user.email", "review@example.test")
        _git(repo, "config", "user.name", "Review Test")

        (repo / "README.md").write_text("# Other repo\n", encoding="utf-8")
        gadget_skill = repo / "skills" / "gadget"
        gadget_skill.mkdir(parents=True)
        (gadget_skill / "SKILL.md").write_text(
            "# Gadget\n"
            "\n"
            "## Review Triggers\n"
            "\n"
            "- lib/**\n"
            "- docs/unrelated.md\n",
            encoding="utf-8",
        )
        _git(repo, "add", "README.md", "skills/gadget/SKILL.md")
        _git(repo, "commit", "--quiet", "-m", "base")

        _git(repo, "checkout", "--quiet", "-b", "scratch")

        widget_skill = repo / "skills" / "widget"
        widget_skill.mkdir(parents=True)
        (widget_skill / "SKILL.md").write_text("# Widget\n", encoding="utf-8")
        library = repo / "lib"
        library.mkdir()
        (library / "engine.py").write_text("value = 1\n", encoding="utf-8")
        _git(repo, "add", "skills/widget/SKILL.md", "lib/engine.py")
        _git(repo, "commit", "--quiet", "-m", "1")

        tasking_dir = repo / ".ai-dev"
        tasking_dir.mkdir()
        (tasking_dir / "tasking.md").write_text(
            "# Current Executor Task\n"
            "\n"
            "## Tasks\n"
            "\n"
            "- [pending] something\n"
            "\n"
            "## Process Notes\n"
            "\n"
            "- rediscovered the widget layout twice\n",
            encoding="utf-8",
        )

    def _write_workflow_state(self, payload: dict[str, object]) -> None:
        state_path = self.other_repo / ".ai-dev" / "workflow.json"
        state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _install_skill_by_symlink(self) -> Path:
        installed_root = self.tmp_path / "agents" / "skills"
        installed_root.mkdir(parents=True)
        link = installed_root / "auto-review"
        link.symlink_to(REPO_ROOT / "skills" / "copilot" / "auto-review", target_is_directory=True)
        return link / "scripts" / "review-evidence"

    def _run_helper(self, *arguments: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        # Keep evidence deterministic regardless of a developer's installed Flow launchers.
        tool_dirs = [
            os.path.dirname(shutil.which("git") or "/usr/bin/git"),
            os.path.dirname(sys.executable),
        ]
        environment["PATH"] = os.pathsep.join(dict.fromkeys(tool_dirs))
        return subprocess.run(
            [str(self.installed_helper), *arguments],
            cwd=cwd or self.other_repo,
            capture_output=True,
            text=True,
            env=environment,
        )

    def test_helper_is_executable_from_installed_symlink_path(self) -> None:
        result = self._run_helper()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("# Review Evidence", result.stdout)
        self.assertIn("mode: checkpoint", result.stdout)

    def test_scope_is_the_caller_repository_not_the_installed_package(self) -> None:
        result = self._run_helper()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"repository: {self.other_repo}", result.stdout)
        self.assertNotIn(str(REPO_ROOT), result.stdout)

    def test_checkpoint_mode_reports_changed_files_notes_and_skills(self) -> None:
        result = self._run_helper("--mode", "checkpoint")

        self.assertEqual(result.returncode, 0, result.stderr)
        for heading in (
            "## Workflow",
            "## Changed Files",
            "## Uncommitted",
            "## Tasking Process Notes",
            "## Skills To Review",
        ):
            self.assertIn(heading, result.stdout)

        self.assertIn("skills/widget/SKILL.md", result.stdout)
        self.assertIn("rediscovered the widget layout twice", result.stdout)
        self.assertIn("- widget: package changed", result.stdout)

    def test_branches_and_identity_come_from_workflow_state(self) -> None:
        self._write_workflow_state(
            {
                "mainBranch": "trunk",
                "scratchBranch": "work",
                "checkpoint": 4,
                "activeIssueNumber": 32,
            }
        )
        _git(self.other_repo, "branch", "trunk", "main")
        _git(self.other_repo, "branch", "work", "scratch")

        result = self._run_helper("--mode", "promotion")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("workflow: issue 32, checkpoint 4", result.stdout)
        self.assertIn("main branch: trunk", result.stdout)
        self.assertIn("scratch branch: work", result.stdout)
        self.assertIn("cumulative trunk..work", result.stdout)

    def test_malformed_workflow_state_is_not_hidden(self) -> None:
        (self.other_repo / ".ai-dev" / "workflow.json").write_text("{ not json", encoding="utf-8")

        result = self._run_helper()

        self.assertEqual(result.returncode, 2)
        self.assertIn("malformed Flow workflow state", result.stderr)

    def test_absent_workflow_state_falls_back_safely(self) -> None:
        result = self._run_helper()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no Flow workflow state at .ai-dev/workflow.json", result.stdout)
        self.assertIn("main branch: main", result.stdout)

    def test_external_review_trigger_puts_a_skill_in_the_review_surface(self) -> None:
        result = self._run_helper()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "- gadget: review trigger lib/** matched (lib/engine.py)",
            result.stdout,
        )

    def test_unmatched_review_trigger_does_not_flag_a_skill(self) -> None:
        result = self._run_helper()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("unrelated", result.stdout)

    def test_promotion_mode_uses_cumulative_scope(self) -> None:
        result = self._run_helper("--mode", "promotion")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mode: promotion", result.stdout)
        self.assertIn("cumulative main..scratch", result.stdout)
        self.assertIn("skills/widget/SKILL.md", result.stdout)

    def test_output_omits_patch_content(self) -> None:
        result = self._run_helper("--mode", "promotion")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("diff --git", result.stdout)
        self.assertNotIn("@@", result.stdout)

    def test_missing_tasking_rail_is_reported_without_failing(self) -> None:
        (self.other_repo / ".ai-dev" / "tasking.md").unlink()

        result = self._run_helper()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no tasking rail at .ai-dev/tasking.md", result.stdout)

    def test_unknown_mode_fails(self) -> None:
        result = self._run_helper("--mode", "everything")

        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown mode", result.stderr)

    def test_outside_a_repository_fails(self) -> None:
        outside = self.tmp_path / "outside"
        outside.mkdir()

        result = self._run_helper(cwd=outside)

        self.assertEqual(result.returncode, 2)
        self.assertIn("not inside a Git repository", result.stderr)


if __name__ == "__main__":
    unittest.main()

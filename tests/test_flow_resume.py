from __future__ import annotations

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
from ai_dev_flow.blocked_workflows import BlockedWorkflowsError
from ai_dev_flow.workflow_state import WorkflowStateError


class FlowResumeSafetyTests(unittest.TestCase):
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
        subdir = repo_root / "subdir"
        subdir.mkdir(parents=True)

        self._run_git(repo_root, "init", "-q")
        self._run_git(repo_root, "config", "user.name", "Flow Resume Tests")
        self._run_git(repo_root, "config", "user.email", "flow-resume-tests@example.com")

        (repo_root / ".gitignore").write_text(".ai-dev/workflow.json\n.ai-dev/blocked-workflows.json\n", encoding="utf-8")
        (repo_root / "tracked.txt").write_text("base\n", encoding="utf-8")
        (subdir / ".keep").write_text("keep\n", encoding="utf-8")
        self._run_git(repo_root, "add", ".gitignore", "tracked.txt", "subdir/.keep")
        self._run_git(repo_root, "commit", "-q", "-m", "initial commit")
        self._run_git(repo_root, "branch", "-M", "main")
        self._run_git(repo_root, "checkout", "-q", "-b", "scratch")

        ai_dev_dir = repo_root / ".ai-dev"
        ai_dev_dir.mkdir(parents=True, exist_ok=True)
        (ai_dev_dir / "workflow.json").write_text(
            json.dumps(
                {
                    "mainBranch": "main",
                    "scratchBranch": "scratch",
                    "checkpoint": 0,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (ai_dev_dir / "blocked-workflows.json").write_text(
            json.dumps(
                {
                    "blockedWorkflows": [
                        {
                            "issueNumber": 12,
                            "issueTitle": "Issue 12",
                            "issueUrl": "https://github.com/jmrozi1/ai-dev/issues/12",
                            "reason": "waiting",
                            "blockedAt": "2026-07-23T00:00:00Z",
                        }
                    ]
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        return repo_root

    def _diff_baseline_path(self, repo_root: Path) -> Path:
        return repo_root / ".ai-dev" / "diff-baseline" / "baseline.json"

    def _write_diff_baseline_marker(self, repo_root: Path) -> Path:
        baseline_path = self._diff_baseline_path(repo_root)
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text('{"version":1,"repository":{"root":"x"},"workflow":{},"status":{},"snapshots":{"working":{}}}\n', encoding="utf-8")
        return baseline_path

    def _ignore_flow_artifacts(self, repo_root: Path) -> None:
        exclude_path = repo_root / ".git" / "info" / "exclude"
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        if ".ai-dev/\n" not in existing:
            exclude_path.write_text(existing + ".ai-dev/\n", encoding="utf-8")

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

    def _make_reconciler(
        self,
        labels_state: list[str],
        *,
        fail_on_blocked: bool = False,
        require_active_in_blocked_labels: bool = False,
    ):
        def _reconcile(issue_number: int, target_label: str, labels: list[str]) -> None:
            self.assertEqual(issue_number, 12)
            self.assertIsInstance(labels, list)

            if target_label == "active":
                labels_state[:] = ["active"]
                return

            if target_label == "blocked":
                if require_active_in_blocked_labels:
                    self.assertIn("active", labels)
                if fail_on_blocked:
                    raise cli.FlowError(
                        "GitHub label reconciliation failed for #12: rollback edit failed"
                    )
                labels_state[:] = ["blocked"]
                return

            self.fail(f"unexpected target label: {target_label}")

        return _reconcile

    def test_resume_success_activates_locally_and_sets_active_label(self) -> None:
        repo_root = self._init_repo("repo-resume-success")
        self._ignore_flow_artifacts(repo_root)
        baseline_path = self._write_diff_baseline_marker(repo_root)
        labels_state = ["blocked"]

        with (
            patch(
                "ai_dev_flow.cli._resolve_issue_details_with_labels",
                return_value=(
                    "Issue 12",
                    "https://github.com/jmrozi1/ai-dev/issues/12",
                    ["blocked"],
                ),
            ),
            patch(
                "ai_dev_flow.cli._reconcile_github_workflow_label",
                side_effect=self._make_reconciler(labels_state),
            ),
        ):
            code, out, err = self._invoke(repo_root / "subdir", "resume", "12")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Resumed issue 12", out)

        workflow_data = json.loads((repo_root / ".ai-dev" / "workflow.json").read_text(encoding="utf-8"))
        self.assertEqual(workflow_data.get("activeIssueNumber"), 12)
        self.assertEqual(workflow_data.get("checkpoint"), 0)

        blocked_data = json.loads((repo_root / ".ai-dev" / "blocked-workflows.json").read_text(encoding="utf-8"))
        blocked_numbers = [item.get("issueNumber") for item in blocked_data.get("blockedWorkflows", [])]
        self.assertNotIn(12, blocked_numbers)

        self.assertEqual(labels_state, ["active"])
        self.assertFalse(baseline_path.exists())

    def test_resume_restore_blocked_record_when_state_save_fails(self) -> None:
        repo_root = self._init_repo("repo-resume-save-fail")
        labels_state = ["blocked"]

        with (
            patch("ai_dev_flow.cli._resolve_issue_details_with_labels", return_value=("Issue 12", "https://github.com/jmrozi1/ai-dev/issues/12", ["blocked"])),
            patch("ai_dev_flow.cli._reconcile_github_workflow_label", side_effect=self._make_reconciler(labels_state)),
            patch("ai_dev_flow.cli.save_state", side_effect=WorkflowStateError("state write failed")),
        ):
            code, out, err = self._invoke(repo_root / "subdir", "resume", "12")

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("Cannot resume workflow: failed to activate issue 12.", err)

        workflow_data = json.loads((repo_root / ".ai-dev" / "workflow.json").read_text(encoding="utf-8"))
        self.assertNotIn("activeIssueNumber", workflow_data)

        blocked_data = json.loads((repo_root / ".ai-dev" / "blocked-workflows.json").read_text(encoding="utf-8"))
        blocked_numbers = [item.get("issueNumber") for item in blocked_data.get("blockedWorkflows", [])]
        self.assertIn(12, blocked_numbers)
        self.assertEqual(labels_state, ["blocked"])

    def test_resume_does_not_activate_when_blocked_registry_update_fails(self) -> None:
        repo_root = self._init_repo("repo-resume-remove-fail")
        self._ignore_flow_artifacts(repo_root)
        baseline_path = self._write_diff_baseline_marker(repo_root)
        labels_state = ["blocked"]

        with (
            patch("ai_dev_flow.cli._resolve_issue_details_with_labels", return_value=("Issue 12", "https://github.com/jmrozi1/ai-dev/issues/12", ["blocked"])),
            patch(
                "ai_dev_flow.cli._reconcile_github_workflow_label",
                side_effect=self._make_reconciler(
                    labels_state,
                    require_active_in_blocked_labels=True,
                ),
            ),
            patch("ai_dev_flow.cli.remove_blocked_workflow", side_effect=BlockedWorkflowsError("blocked write failed")),
        ):
            code, out, err = self._invoke(repo_root / "subdir", "resume", "12")

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("Cannot resume workflow: failed to update blocked workflow registry.", err)

        workflow_data = json.loads((repo_root / ".ai-dev" / "workflow.json").read_text(encoding="utf-8"))
        self.assertNotIn("activeIssueNumber", workflow_data)

        blocked_data = json.loads((repo_root / ".ai-dev" / "blocked-workflows.json").read_text(encoding="utf-8"))
        blocked_numbers = [item.get("issueNumber") for item in blocked_data.get("blockedWorkflows", [])]
        self.assertIn(12, blocked_numbers)
        self.assertEqual(labels_state, ["blocked"])
        self.assertTrue(baseline_path.exists())

    def test_resume_reports_blocked_restore_failure_and_rolls_back_label(self) -> None:
        repo_root = self._init_repo("repo-resume-save-fail-restore-fail")
        labels_state = ["blocked"]

        with (
            patch("ai_dev_flow.cli._resolve_issue_details_with_labels", return_value=("Issue 12", "https://github.com/jmrozi1/ai-dev/issues/12", ["blocked"])),
            patch("ai_dev_flow.cli._reconcile_github_workflow_label", side_effect=self._make_reconciler(labels_state)),
            patch("ai_dev_flow.cli.save_state", side_effect=WorkflowStateError("state write failed")),
            patch("ai_dev_flow.cli.save_blocked_workflows", side_effect=BlockedWorkflowsError("restore write failed")),
        ):
            code, out, err = self._invoke(repo_root / "subdir", "resume", "12")

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("Cannot resume workflow: failed to activate issue 12.", err)
        self.assertIn("failed to restore blocked workflow metadata for #12", err)

        blocked_data = json.loads((repo_root / ".ai-dev" / "blocked-workflows.json").read_text(encoding="utf-8"))
        blocked_numbers = [item.get("issueNumber") for item in blocked_data.get("blockedWorkflows", [])]
        self.assertNotIn(12, blocked_numbers)
        self.assertEqual(labels_state, ["blocked"])

    def test_resume_surfaces_label_rollback_failure_without_hiding_primary_error(self) -> None:
        repo_root = self._init_repo("repo-resume-remove-fail-label-rollback-fail")
        labels_state = ["blocked"]

        with (
            patch("ai_dev_flow.cli._resolve_issue_details_with_labels", return_value=("Issue 12", "https://github.com/jmrozi1/ai-dev/issues/12", ["blocked"])),
            patch(
                "ai_dev_flow.cli._reconcile_github_workflow_label",
                side_effect=self._make_reconciler(labels_state, fail_on_blocked=True),
            ),
            patch("ai_dev_flow.cli.remove_blocked_workflow", side_effect=BlockedWorkflowsError("blocked write failed")),
        ):
            code, out, err = self._invoke(repo_root / "subdir", "resume", "12")

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("Cannot resume workflow: failed to update blocked workflow registry.", err)
        self.assertIn("GitHub label rollback failed after local resume failure", err)
        self.assertIn("rollback edit failed", err)
        self.assertEqual(labels_state, ["active"])

    def test_resume_reports_file_not_found_during_label_rollback_without_hiding_primary_error(self) -> None:
        repo_root = self._init_repo("repo-resume-remove-fail-label-rollback-oserror")
        labels_state = ["blocked"]

        def reconcile_with_missing_binary(
            issue_number: int,
            target_label: str,
            labels: list[str],
        ) -> None:
            self.assertEqual(issue_number, 12)
            self.assertIsInstance(labels, list)

            if target_label == "active":
                labels_state[:] = ["active"]
                return

            if target_label == "blocked":
                self.assertIn("active", labels)
                raise FileNotFoundError("gh binary missing")

            self.fail(f"unexpected target label: {target_label}")

        with (
            patch("ai_dev_flow.cli._resolve_issue_details_with_labels", return_value=("Issue 12", "https://github.com/jmrozi1/ai-dev/issues/12", ["blocked"])),
            patch(
                "ai_dev_flow.cli._reconcile_github_workflow_label",
                side_effect=reconcile_with_missing_binary,
            ),
            patch("ai_dev_flow.cli.remove_blocked_workflow", side_effect=BlockedWorkflowsError("blocked write failed")),
        ):
            code, out, err = self._invoke(repo_root / "subdir", "resume", "12")

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertNotIn("Traceback", err)

        primary_message = "Cannot resume workflow: failed to update blocked workflow registry."
        rollback_message = "GitHub label rollback failed after local resume failure:"
        self.assertIn(primary_message, err)
        self.assertIn(rollback_message, err)
        self.assertIn("GitHub invocation error during rollback (FileNotFoundError)", err)

        self.assertLess(err.index(primary_message), err.index("Additional failures:"))
        self.assertLess(err.index("Additional failures:"), err.index(rollback_message))
        self.assertEqual(labels_state, ["active"])

    def test_status_verbose_duplicate_validation_remains_unchanged(self) -> None:
        repo_root = self._init_repo("repo-status-duplicate-validation")

        (repo_root / ".ai-dev" / "workflow.json").write_text(
            json.dumps(
                {
                    "mainBranch": "main",
                    "scratchBranch": "scratch",
                    "checkpoint": 0,
                    "activeIssueNumber": 12,
                    "activeIssueTitle": "Issue 12",
                    "activeIssueUrl": "https://github.com/jmrozi1/ai-dev/issues/12",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        code_verbose, out_verbose, err_verbose = self._invoke(
            repo_root / "subdir", "status", "-v"
        )
        self.assertEqual(code_verbose, 0)
        self.assertEqual(err_verbose, "")
        self.assertIn("Validation:", out_verbose)
        self.assertIn(
            "invalid state: active issue 12 is also present in blocked workflows",
            out_verbose,
        )

        code_default, out_default, err_default = self._invoke(
            repo_root / "subdir", "status"
        )
        self.assertEqual(code_default, 0)
        self.assertEqual(err_default, "")
        self.assertNotIn("Validation:", out_default)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from collections import Counter
import hashlib
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
from ai_dev_flow.review_context import build_review_context, build_review_id


class FlowReviewTests(unittest.TestCase):
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
        self._run_git(repo_root, "config", "user.name", "Flow Review Tests")
        self._run_git(repo_root, "config", "user.email", "flow-review-tests@example.com")

        (repo_root / ".gitignore").write_text(".ai-dev/workflow.json\n", encoding="utf-8")
        (repo_root / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._run_git(repo_root, "add", ".gitignore", "tracked.txt")
        self._run_git(repo_root, "commit", "-q", "-m", "initial commit")
        self._run_git(repo_root, "branch", "-M", "main")

        return repo_root

    def _activate_issue_workflow(self, repo_root: Path, issue_number: int) -> None:
        self._run_git(repo_root, "checkout", "-q", "-b", "scratch")
        workflow_path = repo_root / ".ai-dev" / "workflow.json"
        workflow_path.parent.mkdir(parents=True, exist_ok=True)
        workflow_path.write_text(
            json.dumps(
                {
                    "activeIssueNumber": issue_number,
                    "mainBranch": "main",
                    "scratchBranch": "scratch",
                    "checkpoint": 0,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def _invoke(self, cwd: Path, *arguments: str) -> tuple[int, str, str]:
        previous_cwd = Path.cwd()
        previous_argv = list(sys.argv)
        had_command_name = "FLOW_COMMAND_NAME" in os.environ
        previous_command_name = os.environ.get("FLOW_COMMAND_NAME")

        stdout = io.StringIO()
        stderr = io.StringIO()

        os.environ["FLOW_COMMAND_NAME"] = "flow"
        sys.argv = ["flow", *arguments]
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

    def _diff_headers(self, text: str) -> list[str]:
        headers: list[str] = []
        for line in text.splitlines():
            if not line.startswith("diff --git a/"):
                continue
            if " b/" not in line:
                continue
            left = line[len("diff --git a/") : line.index(" b/")]
            headers.append(left)
        return headers

    def _review_package_dirs(self, repo_root: Path) -> list[Path]:
        reviews_root = repo_root / ".ai-dev" / "reviews"
        if not reviews_root.exists():
            return []

        return sorted(path for path in reviews_root.iterdir() if path.is_dir())

    def _latest_review_dir(self, repo_root: Path) -> Path:
        review_dirs = self._review_package_dirs(repo_root)
        self.assertGreaterEqual(len(review_dirs), 1)
        return review_dirs[-1]

    def _review_id_from_payload(self, payload: dict[str, object]) -> str:
        workflow = payload["workflow"]
        ticket = payload["ticket"]
        acceptance = payload["acceptance_criteria"]
        changes = payload["changes"]
        instructions = payload["instructions"]
        artifacts = payload["artifacts"]
        diagnostics = payload["diagnostics"]

        context = build_review_context(
            scope=payload["scope"],
            command=payload["command"],
            workflow_type=ticket["workflow_type"],
            main_branch=workflow["main_branch"],
            scratch_branch=workflow["scratch_branch"],
            current_branch=workflow["current_branch"],
            checkpoint=workflow["checkpoint"],
            active_issue_number=ticket["issue_number"],
            active_issue_title=ticket["issue_title"],
            active_issue_url=ticket["issue_url"],
            patch_description=ticket["patch_description"],
            issue_description_status=ticket["issue_description_status"],
            issue_description_source=ticket["issue_description_source"],
            acceptance_criteria_status=acceptance["status"],
            acceptance_criteria_heading=acceptance["heading"],
            acceptance_criteria_lines=acceptance["lines"],
            committed_reference=changes["committed_reference"],
            committed_paths=changes["committed_paths"],
            committed_diff_text="",
            committed_diff_sha256=changes["committed_diff_sha256"],
            overlay_reference=changes["overlay_reference"],
            overlay_paths=changes["overlay_paths"],
            overlay_diff_text="",
            overlay_diff_sha256=changes["overlay_diff_sha256"],
            all_paths=changes["all_paths"],
            changes_diff_sha256=changes["changes_diff_sha256"],
            instruction_reference_paths=instructions["reference_paths"],
            diagnostics=diagnostics,
            review_root_path=artifacts["review_root_path"],
            package_markdown_path=artifacts["package_markdown_path"],
            package_json_path=artifacts["package_json_path"],
            changes_diff_path=artifacts["changes_diff_path"],
            canonical_report_path=artifacts["canonical_report_path"],
        )
        return build_review_id(context)

    def test_review_help_includes_all_option(self) -> None:
        repo_root = self._init_repo("repo-review-help")

        short_code, short_out, short_err = self._invoke(repo_root, "review", "-h")
        self.assertEqual(short_code, 0)
        self.assertEqual(short_err, "")
        self.assertIn("Usage: flow review [-a|--all]", short_out)
        self.assertIn("all changes in the active workflow since main", short_out)

        long_code, long_out, long_err = self._invoke(repo_root, "review", "--help")
        self.assertEqual(long_code, 0)
        self.assertEqual(long_err, "")
        self.assertEqual(long_out, short_out)

    def test_review_argument_validation(self) -> None:
        repo_root = self._init_repo("repo-review-arg-validation")
        self._activate_issue_workflow(repo_root, issue_number=21)
        (repo_root / "change.txt").write_text("content\n", encoding="utf-8")

        accepted_default = self._invoke(repo_root, "review")
        self.assertEqual(accepted_default[0], 0)

        accepted_short = self._invoke(repo_root, "review", "-a")
        self.assertEqual(accepted_short[0], 0)

        accepted_long = self._invoke(repo_root, "review", "--all")
        self.assertEqual(accepted_long[0], 0)

        reject_unknown = self._invoke(repo_root, "review", "--bogus")
        self.assertEqual(reject_unknown[0], 1)
        self.assertIn("Usage: flow review [-a|--all]", reject_unknown[2])

        reject_multiple = self._invoke(repo_root, "review", "-a", "--all")
        self.assertEqual(reject_multiple[0], 1)
        self.assertIn("Usage: flow review [-a|--all]", reject_multiple[2])

        reject_positional = self._invoke(repo_root, "review", "extra")
        self.assertEqual(reject_positional[0], 1)
        self.assertIn("Usage: flow review [-a|--all]", reject_positional[2])

    def test_default_review_behavior_after_checkpoint(self) -> None:
        repo_root = self._init_repo("repo-review-default-scope")
        self._activate_issue_workflow(repo_root, issue_number=22)

        (repo_root / "checkpoint-change.txt").write_text("checkpoint\n", encoding="utf-8")
        commit_code, commit_out, commit_err = self._invoke(repo_root, "commit")
        self.assertEqual(commit_code, 0)
        self.assertEqual(commit_err, "")
        self.assertIn("Created checkpoint 1", commit_out)

        no_change_code, no_change_out, no_change_err = self._invoke(repo_root, "review")
        self.assertEqual(no_change_code, 1)
        self.assertEqual(no_change_out, "")
        self.assertIn("No proposed changes to review", no_change_err)

        (repo_root / "new-edit.txt").write_text("new edit\n", encoding="utf-8")
        changed_code, changed_out, changed_err = self._invoke(repo_root, "review")
        self.assertEqual(changed_code, 0)
        self.assertEqual(changed_err, "")
        self.assertIn("diff --git a/new-edit.txt b/new-edit.txt", changed_out)
        self.assertNotIn("diff --git a/checkpoint-change.txt b/checkpoint-change.txt", changed_out)

    def test_review_all_includes_workflow_commits_and_uncommitted(self) -> None:
        repo_root = self._init_repo("repo-review-all-scope")
        self._activate_issue_workflow(repo_root, issue_number=23)

        (repo_root / "checkpoint-1.txt").write_text("checkpoint 1\n", encoding="utf-8")
        first_commit = self._invoke(repo_root, "commit")
        self.assertEqual(first_commit[0], 0)

        (repo_root / "checkpoint-2.txt").write_text("checkpoint 2\n", encoding="utf-8")
        second_commit = self._invoke(repo_root, "commit")
        self.assertEqual(second_commit[0], 0)

        (repo_root / "uncommitted.txt").write_text("working tree\n", encoding="utf-8")

        workflow_state_before = (repo_root / ".ai-dev" / "workflow.json").read_text(encoding="utf-8")
        head_before = self._run_git(repo_root, "rev-parse", "HEAD")

        long_code, long_out, long_err = self._invoke(repo_root, "review", "--all")
        self.assertEqual(long_code, 0)
        self.assertEqual(long_err, "")
        self.assertIn("Issue: 23", long_out)
        self.assertIn("Review summary:", long_out)
        self.assertIn("diff --git a/checkpoint-1.txt b/checkpoint-1.txt", long_out)
        self.assertIn("diff --git a/checkpoint-2.txt b/checkpoint-2.txt", long_out)
        self.assertIn("diff --git a/uncommitted.txt b/uncommitted.txt", long_out)
        self.assertNotIn(".ai-dev/workflow.json", long_out)

        short_code, short_out, short_err = self._invoke(repo_root, "review", "-a")
        self.assertEqual(short_code, 0)
        self.assertEqual(short_err, "")
        self.assertEqual(short_out, long_out)

        workflow_state_after = (repo_root / ".ai-dev" / "workflow.json").read_text(encoding="utf-8")
        head_after = self._run_git(repo_root, "rev-parse", "HEAD")
        self.assertEqual(workflow_state_after, workflow_state_before)
        self.assertEqual(head_after, head_before)

    def test_review_all_clean_checkpoint_emits_each_path_once(self) -> None:
        repo_root = self._init_repo("repo-review-all-clean-once")
        self._activate_issue_workflow(repo_root, issue_number=24)

        (repo_root / "checkpoint-a.txt").write_text("a\n", encoding="utf-8")
        first_commit = self._invoke(repo_root, "commit")
        self.assertEqual(first_commit[0], 0)

        (repo_root / "checkpoint-b.txt").write_text("b\n", encoding="utf-8")
        second_commit = self._invoke(repo_root, "commit")
        self.assertEqual(second_commit[0], 0)

        code, out, err = self._invoke(repo_root, "review", "--all")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(out.count("Issue: "), 1)
        self.assertEqual(out.count("Review summary:"), 1)
        self.assertEqual(out.count("Diff legend:"), 1)

        expected_paths = [
            line
            for line in self._run_git(repo_root, "diff", "--name-only", "main...scratch").splitlines()
            if line
        ]
        headers = self._diff_headers(out)
        self.assertEqual(len(headers), len(expected_paths))

        expected_counter = Counter(expected_paths)
        header_counter = Counter(headers)
        self.assertEqual(set(header_counter.keys()), set(expected_counter.keys()))
        for path in expected_counter:
            self.assertEqual(header_counter[path], 1, msg=f"expected one diff header for {path}")

    def test_review_all_staged_overlay_emits_committed_and_overlay_once(self) -> None:
        repo_root = self._init_repo("repo-review-all-overlay-once")
        self._activate_issue_workflow(repo_root, issue_number=25)

        (repo_root / "checkpoint-1.txt").write_text("checkpoint one\n", encoding="utf-8")
        commit_one = self._invoke(repo_root, "commit")
        self.assertEqual(commit_one[0], 0)

        (repo_root / "checkpoint-2.txt").write_text("checkpoint two\n", encoding="utf-8")
        commit_two = self._invoke(repo_root, "commit")
        self.assertEqual(commit_two[0], 0)

        uncommitted_path = "overlay-wip.txt"
        (repo_root / uncommitted_path).write_text("overlay\n", encoding="utf-8")

        code, out, err = self._invoke(repo_root, "review", "--all")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(out.count("Issue: "), 1)
        self.assertEqual(out.count("Review summary:"), 1)
        self.assertEqual(out.count("Diff legend:"), 1)

        committed_paths = {
            line
            for line in self._run_git(repo_root, "diff", "--name-only", "main...scratch").splitlines()
            if line
        }
        headers = self._diff_headers(out)
        counts = Counter(headers)

        self.assertIn(uncommitted_path, counts)
        self.assertEqual(counts[uncommitted_path], 1)
        for path in committed_paths:
            self.assertEqual(counts[path], 1, msg=f"expected one committed diff header for {path}")

        expected_total = len(committed_paths)
        if uncommitted_path not in committed_paths:
            expected_total += 1
        self.assertEqual(len(headers), expected_total)

    def test_review_all_generates_review_package_artifacts(self) -> None:
        repo_root = self._init_repo("repo-review-all-package-artifacts")
        self._activate_issue_workflow(repo_root, issue_number=26)

        (repo_root / "checkpoint-review.txt").write_text("checkpoint\n", encoding="utf-8")
        commit_result = self._invoke(repo_root, "commit")
        self.assertEqual(commit_result[0], 0)

        (repo_root / "overlay-review.txt").write_text("overlay\n", encoding="utf-8")

        code, out, err = self._invoke(repo_root, "review", "--all")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Issue: 26", out)
        self.assertIn("diff --git a/checkpoint-review.txt b/checkpoint-review.txt", out)
        self.assertIn("diff --git a/overlay-review.txt b/overlay-review.txt", out)

        latest = self._latest_review_dir(repo_root)
        self.assertTrue((latest / "package.md").exists())
        self.assertTrue((latest / "package.json").exists())
        self.assertTrue((latest / "changes.diff").exists())

        payload = json.loads((latest / "package.json").read_text(encoding="utf-8"))
        derived_id = self._review_id_from_payload(payload)
        self.assertEqual(payload["review_id"], derived_id)
        self.assertEqual(latest.name, payload["review_id"])

        changes_bytes = (latest / "changes.diff").read_bytes()
        self.assertEqual(
            payload["changes"]["changes_diff_sha256"],
            hashlib.sha256(changes_bytes).hexdigest(),
        )

    def test_review_all_id_and_directory_are_stable_for_identical_state(self) -> None:
        repo_root = self._init_repo("repo-review-id-stable")
        self._activate_issue_workflow(repo_root, issue_number=34)

        (repo_root / "checkpoint-review.txt").write_text("checkpoint\n", encoding="utf-8")
        commit_result = self._invoke(repo_root, "commit")
        self.assertEqual(commit_result[0], 0)

        (repo_root / "overlay-review.txt").write_text("overlay\n", encoding="utf-8")

        first_code, _, first_err = self._invoke(repo_root, "review", "--all")
        self.assertEqual(first_code, 0)
        self.assertEqual(first_err, "")
        first_dir = self._latest_review_dir(repo_root)
        first_payload = json.loads((first_dir / "package.json").read_text(encoding="utf-8"))

        second_code, _, second_err = self._invoke(repo_root, "review", "--all")
        self.assertEqual(second_code, 0)
        self.assertEqual(second_err, "")
        second_dir = self._latest_review_dir(repo_root)
        second_payload = json.loads((second_dir / "package.json").read_text(encoding="utf-8"))

        self.assertEqual(first_payload["review_id"], second_payload["review_id"])
        self.assertEqual(first_dir.name, second_dir.name)
        self.assertEqual(len(self._review_package_dirs(repo_root)), 1)

    def test_review_all_id_changes_when_authoritative_diff_changes(self) -> None:
        repo_root = self._init_repo("repo-review-id-changes")
        self._activate_issue_workflow(repo_root, issue_number=35)

        (repo_root / "checkpoint-review.txt").write_text("checkpoint\n", encoding="utf-8")
        commit_result = self._invoke(repo_root, "commit")
        self.assertEqual(commit_result[0], 0)

        (repo_root / "overlay-review.txt").write_text("overlay-a\n", encoding="utf-8")
        first_code, _, first_err = self._invoke(repo_root, "review", "--all")
        self.assertEqual(first_code, 0)
        self.assertEqual(first_err, "")
        first_payload = json.loads(
            (self._latest_review_dir(repo_root) / "package.json").read_text(encoding="utf-8")
        )

        (repo_root / "overlay-review.txt").write_text("overlay-b\n", encoding="utf-8")
        second_code, _, second_err = self._invoke(repo_root, "review", "--all")
        self.assertEqual(second_code, 0)
        self.assertEqual(second_err, "")
        second_payload = json.loads(
            (self._latest_review_dir(repo_root) / "package.json").read_text(encoding="utf-8")
        )

        self.assertNotEqual(first_payload["review_id"], second_payload["review_id"])

    def test_review_package_generation_never_invokes_gh(self) -> None:
        repo_root = self._init_repo("repo-review-offline-gh")
        self._activate_issue_workflow(repo_root, issue_number=27)
        (repo_root / "change.txt").write_text("content\n", encoding="utf-8")

        real_run = subprocess.run

        def guarded_run(*args: object, **kwargs: object) -> object:
            command = args[0]
            if isinstance(command, list) and command and command[0] == "gh":
                raise AssertionError("review invoked gh unexpectedly")
            return real_run(*args, **kwargs)

        with patch("ai_dev_flow.cli.subprocess.run", side_effect=guarded_run):
            code, out, err = self._invoke(repo_root, "review", "--all")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Review summary:", out)

    def test_review_package_records_local_metadata_diagnostic_when_issue_body_missing(self) -> None:
        repo_root = self._init_repo("repo-review-missing-local-metadata")
        self._activate_issue_workflow(repo_root, issue_number=28)
        (repo_root / "change.txt").write_text("content\n", encoding="utf-8")

        code, _, err = self._invoke(repo_root, "review", "--all")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")

        latest = self._latest_review_dir(repo_root)
        payload = json.loads((latest / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["ticket"]["issue_number"], 28)
        self.assertEqual(payload["ticket"]["issue_description_status"], "unavailable_local")
        self.assertIn("Issue body unavailable locally", "\n".join(payload["diagnostics"]))

    def test_review_path_fidelity_preserves_spaces_unicode_without_strip(self) -> None:
        repo_root = self._init_repo("repo-review-path-fidelity")
        self._activate_issue_workflow(repo_root, issue_number=29)

        leading = " leading.txt"
        trailing = "trailing .txt "
        unicode_name = "unicodé.txt"

        (repo_root / leading).write_text("lead\n", encoding="utf-8")
        (repo_root / trailing).write_text("trail\n", encoding="utf-8")
        (repo_root / unicode_name).write_text("uni\n", encoding="utf-8")

        code, _, err = self._invoke(repo_root, "review")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")

        latest = self._latest_review_dir(repo_root)
        payload = json.loads((latest / "package.json").read_text(encoding="utf-8"))
        overlay_paths = payload["changes"]["overlay_paths"]
        self.assertIn(leading, overlay_paths)
        self.assertIn(trailing, overlay_paths)
        self.assertIn(unicode_name, overlay_paths)

    def test_review_package_workflow_scope_preserves_committed_and_overlay_separately(self) -> None:
        repo_root = self._init_repo("repo-review-scope-separation")
        self._activate_issue_workflow(repo_root, issue_number=30)

        same_path = "shared.txt"
        (repo_root / same_path).write_text("committed\n", encoding="utf-8")
        commit_result = self._invoke(repo_root, "commit")
        self.assertEqual(commit_result[0], 0)

        (repo_root / same_path).write_text("committed\noverlay\n", encoding="utf-8")

        code, _, err = self._invoke(repo_root, "review", "--all")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")

        latest = self._latest_review_dir(repo_root)
        payload = json.loads((latest / "package.json").read_text(encoding="utf-8"))
        self.assertIn(same_path, payload["changes"]["committed_paths"])
        self.assertIn(same_path, payload["changes"]["overlay_paths"])
        self.assertEqual(payload["changes"]["all_paths"].count(same_path), 1)

        changes_text = (latest / "changes.diff").read_text(encoding="utf-8")
        self.assertIn("## Committed workflow diff: main...scratch", changes_text)
        self.assertIn("## Staged overlay diff: HEAD -> index", changes_text)

    def test_review_package_checkpoint_scope_has_only_staged_section(self) -> None:
        repo_root = self._init_repo("repo-review-checkpoint-scope")
        self._activate_issue_workflow(repo_root, issue_number=31)
        (repo_root / "wip.txt").write_text("wip\n", encoding="utf-8")

        code, _, err = self._invoke(repo_root, "review")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")

        latest = self._latest_review_dir(repo_root)
        changes_text = (latest / "changes.diff").read_text(encoding="utf-8")
        self.assertIn("# Scope: checkpoint", changes_text)
        self.assertIn("## Staged checkpoint diff: HEAD -> index", changes_text)
        self.assertNotIn("## Committed workflow diff:", changes_text)

        payload = json.loads((latest / "package.json").read_text(encoding="utf-8"))
        derived_id = self._review_id_from_payload(payload)
        self.assertEqual(payload["review_id"], derived_id)
        self.assertEqual(latest.name, payload["review_id"])
        self.assertEqual(
            payload["changes"]["changes_diff_sha256"],
            hashlib.sha256((latest / "changes.diff").read_bytes()).hexdigest(),
        )

    def test_package_markdown_references_diff_without_embedding_patch(self) -> None:
        repo_root = self._init_repo("repo-review-markdown-diff-reference")
        self._activate_issue_workflow(repo_root, issue_number=32)
        (repo_root / "snippet.txt").write_text("distinctive-line-123\n", encoding="utf-8")

        code, _, err = self._invoke(repo_root, "review")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")

        latest = self._latest_review_dir(repo_root)
        package_text = (latest / "package.md").read_text(encoding="utf-8")
        changes_text = (latest / "changes.diff").read_text(encoding="utf-8")
        self.assertIn("distinctive-line-123", changes_text)
        self.assertNotIn("distinctive-line-123", package_text)
        self.assertIn("## Change Package", package_text)
        self.assertIn("Changes-Diff-Path:", package_text)

    def test_review_context_failures_are_surface_as_normal_cli_errors(self) -> None:
        repo_root = self._init_repo("repo-review-context-error")
        self._activate_issue_workflow(repo_root, issue_number=33)
        (repo_root / "change.txt").write_text("content\n", encoding="utf-8")

        with patch(
            "ai_dev_flow.cli.build_review_context",
            side_effect=cli.ReviewContextError("simulated context validation failure"),
        ):
            code, out, err = self._invoke(repo_root, "review")

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("Cannot prepare deterministic review package.", err)
        self.assertIn("simulated context validation failure", err)

    def test_review_all_fails_without_active_workflow_and_preserves_output(self) -> None:
        repo_root = self._init_repo("repo-review-all-inactive")
        self._run_git(repo_root, "checkout", "-q", "-b", "scratch")

        output_path = self.tmp_path / "inactive-review-output.txt"
        output_path.write_text("existing report\n", encoding="utf-8")

        config_path = repo_root / ".ai-dev" / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps({"out": str(output_path)}, indent=2) + "\n",
            encoding="utf-8",
        )
        self._run_git(repo_root, "add", "-f", ".ai-dev/config.json")
        self._run_git(repo_root, "commit", "-q", "-m", "track config")

        (repo_root / "tracked.txt").write_text("changed\n", encoding="utf-8")

        code, out, err = self._invoke(repo_root, "review", "--all")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("Cannot review workflow: no active issue is set.", err)
        self.assertEqual(output_path.read_text(encoding="utf-8"), "existing report\n")


if __name__ == "__main__":
    unittest.main()

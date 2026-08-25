from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ai_dev_flow import cli, repository
from ai_dev_flow.repository import RevisionResolutionError


MUTATING_GIT_SUBCOMMANDS = frozenset(
    {
        "fetch",
        "pull",
        "push",
        "merge",
        "rebase",
        "cherry-pick",
        "revert",
        "reset",
        "checkout",
        "switch",
        "branch",
        "commit",
        "add",
        "update-ref",
        "clean",
        "stash",
        "tag",
    }
)


class FlowAdoptTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _git(self, repo: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return result.stdout.strip()

    def _repo(self, name: str) -> Path:
        """An idle Flow repository plus a recovered ref that is ahead of main."""
        repo = self.tmp_path / name
        repo.mkdir()
        self._git(repo, "init", "-q")
        self._git(repo, "config", "user.name", "Adopt Tests")
        self._git(repo, "config", "user.email", "adopt@example.com")
        (repo / ".gitignore").write_text(".ai-dev/\n", encoding="utf-8")
        (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._git(repo, "add", ".gitignore", "tracked.txt")
        self._git(repo, "commit", "-q", "-m", "initial")
        self._git(repo, "branch", "-M", "main")
        self._git(repo, "branch", "scratch")

        ai_dev = repo / ".ai-dev"
        tickets = ai_dev / "tickets"
        tickets.mkdir(parents=True)
        (ai_dev / "config.json").write_text(
            json.dumps(
                {
                    "tickets": {"provider": "local", "path": ".ai-dev/tickets"},
                    "review": {"promotionGate": False},
                }
            ),
            encoding="utf-8",
        )
        self._ticket(tickets, 57, "Adoptable issue", "inactive")
        (ai_dev / "workflow.json").write_text(
            json.dumps(
                {
                    "mainBranch": "main",
                    "scratchBranch": "scratch",
                    "checkpoint": 0,
                }
            ),
            encoding="utf-8",
        )

        self._recovered_branch(repo, "recovered", ("11", "12"))
        self._git(repo, "checkout", "-q", "main")
        return repo

    def _recovered_branch(
        self,
        repo: Path,
        branch: str,
        subjects: tuple[str, ...],
        *,
        start_point: str = "main",
    ) -> str:
        self._git(repo, "checkout", "-q", "-b", branch, start_point)
        for subject in subjects:
            (repo / f"{branch}-{subject}.txt").write_text(f"{subject}\n", encoding="utf-8")
            self._git(repo, "add", f"{branch}-{subject}.txt")
            self._git(repo, "commit", "-q", "-m", subject)
        head = self._git(repo, "rev-parse", branch)
        self._git(repo, "checkout", "-q", "main")
        return head

    def _ticket(
        self,
        directory: Path,
        number: int,
        title: str,
        workflow: str,
    ) -> None:
        payload = {
            "reference": {
                "provider": "local",
                "ticketId": str(number),
                "path": ".ai-dev/tickets",
            },
            "title": title,
            "lifecycleState": "open",
            "workflowState": workflow,
        }
        (directory / f"{number}.json").write_text(json.dumps(payload), encoding="utf-8")

    def _invoke(self, repo: Path, *args: str) -> tuple[int, str, str]:
        previous_cwd = Path.cwd()
        previous_argv = sys.argv
        os.environ["FLOW_COMMAND_NAME"] = "flow-start"
        sys.argv = ["flow-start", cli._DIRECT_FLOW_ROUTE_TOKEN, "start", *args]
        stdout, stderr = io.StringIO(), io.StringIO()
        os.chdir(repo)
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                try:
                    cli.run()
                except SystemExit as exc:
                    return int(exc.code), stdout.getvalue(), stderr.getvalue()
                return 0, stdout.getvalue(), stderr.getvalue()
        finally:
            os.chdir(previous_cwd)
            sys.argv = previous_argv

    def _repository_fingerprint(self, repo: Path) -> dict[str, object]:
        return {
            "refs": self._git(repo, "show-ref"),
            "head": self._git(repo, "rev-parse", "HEAD"),
            "symbolic_head": self._git(repo, "symbolic-ref", "-q", "HEAD"),
            "status": self._git(repo, "status", "--short", "--untracked-files=all"),
            "workflow": (repo / ".ai-dev/workflow.json").read_text(encoding="utf-8"),
            "ticket": (repo / ".ai-dev/tickets/57.json").read_text(encoding="utf-8"),
            "ai_dev_files": sorted(
                str(path.relative_to(repo))
                for path in (repo / ".ai-dev").rglob("*")
                if path.is_file()
            ),
        }

    def _assert_unchanged(self, repo: Path, before: dict[str, object]) -> None:
        self.assertEqual(self._repository_fingerprint(repo), before)

    # -- successful parsing, resolution, and validation --------------------

    def test_valid_target_passes_every_first_checkpoint_precondition(self) -> None:
        repo = self._repo("valid-target")
        recovered = self._git(repo, "rev-parse", "recovered")
        before = self._repository_fingerprint(repo)

        code, out, err = self._invoke(repo, "57", "--adopt", "recovered")

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn(recovered, err)
        self.assertIn("satisfies every adoption precondition for issue 57", err)
        self.assertIn("No repository, workflow, or ticket state was changed.", err)
        self._assert_unchanged(repo, before)

    def test_target_forms_resolve_through_repository_helpers(self) -> None:
        repo = self._repo("target-forms")
        recovered = self._git(repo, "rev-parse", "recovered")
        self._git(repo, "tag", "rescue-tag", "recovered")
        self._git(repo, "update-ref", "refs/remotes/origin/rescue", recovered)
        short = self._git(repo, "rev-parse", "--short", "recovered")

        for target in ("recovered", "rescue-tag", "origin/rescue", recovered, short):
            with self.subTest(target=target):
                self.assertEqual(
                    repository.resolve_commit_ish(repo, target),
                    recovered,
                )

    def test_annotated_tag_peels_to_its_commit(self) -> None:
        repo = self._repo("annotated-tag")
        recovered = self._git(repo, "rev-parse", "recovered")
        self._git(repo, "tag", "-a", "annotated", "-m", "rescue", "recovered")

        self.assertNotEqual(self._git(repo, "rev-parse", "annotated"), recovered)
        self.assertEqual(repository.resolve_commit_ish(repo, "annotated"), recovered)

    def test_validation_performs_no_mutating_or_networked_git(self) -> None:
        repo = self._repo("read-only")
        observed: list[str] = []
        original_run_git = repository._run_git

        def recording_run_git(repo_root, arguments, *, check):
            observed.append(arguments[0])
            return original_run_git(repo_root, arguments, check=check)

        with patch.object(repository, "_run_git", recording_run_git):
            code, _out, err = self._invoke(repo, "57", "--adopt", "recovered")

        self.assertEqual(code, 1)
        self.assertIn("satisfies every adoption precondition", err)
        self.assertEqual(
            sorted(set(observed) & MUTATING_GIT_SUBCOMMANDS),
            [],
        )

    # -- argument-shape refusals -------------------------------------------

    def test_missing_adopt_target_reports_adopt_usage(self) -> None:
        repo = self._repo("missing-target")
        before = self._repository_fingerprint(repo)

        code, _out, err = self._invoke(repo, "57", "--adopt")

        self.assertEqual(code, 1)
        self.assertIn("Usage: flow-start <issue-number> --adopt <commit-ish>", err)
        self._assert_unchanged(repo, before)

    def test_extra_adopt_arguments_report_adopt_usage(self) -> None:
        repo = self._repo("extra-arguments")
        before = self._repository_fingerprint(repo)

        code, _out, err = self._invoke(repo, "57", "--adopt", "recovered", "extra")

        self.assertEqual(code, 1)
        self.assertIn("Usage: flow-start <issue-number> --adopt <commit-ish>", err)
        self._assert_unchanged(repo, before)

    def test_misplaced_adopt_flag_reports_adopt_usage(self) -> None:
        repo = self._repo("misplaced-flag")
        before = self._repository_fingerprint(repo)

        code, _out, err = self._invoke(repo, "--adopt", "recovered", "57")

        self.assertEqual(code, 1)
        self.assertIn("Usage: flow-start <issue-number> --adopt <commit-ish>", err)
        self._assert_unchanged(repo, before)

    def test_empty_adopt_target_is_refused(self) -> None:
        repo = self._repo("empty-target")
        before = self._repository_fingerprint(repo)

        code, _out, err = self._invoke(repo, "57", "--adopt", "   ")

        self.assertEqual(code, 1)
        self.assertIn("adopt target must be a non-empty commit-ish.", err)
        self._assert_unchanged(repo, before)

    def test_invalid_issue_number_is_refused(self) -> None:
        repo = self._repo("invalid-issue")
        before = self._repository_fingerprint(repo)

        code, _out, err = self._invoke(repo, "0", "--adopt", "recovered")

        self.assertEqual(code, 1)
        self.assertIn("issue-number must be a positive integer.", err)
        self._assert_unchanged(repo, before)

    def test_adopt_and_prerequisite_for_cannot_be_combined(self) -> None:
        repo = self._repo("combined-flags")
        before = self._repository_fingerprint(repo)

        code, _out, err = self._invoke(
            repo, "57", "--adopt", "recovered", "--prerequisite-for", "10"
        )

        self.assertEqual(code, 1)
        self.assertIn(
            "--adopt and --prerequisite-for cannot be combined.",
            err,
        )
        self._assert_unchanged(repo, before)

    # -- idle-state and repository refusals --------------------------------

    def test_active_issue_refuses_adoption(self) -> None:
        repo = self._repo("active-issue")
        (repo / ".ai-dev/workflow.json").write_text(
            json.dumps(
                {
                    "mainBranch": "main",
                    "scratchBranch": "scratch",
                    "checkpoint": 3,
                    "activeIssueNumber": 10,
                    "activeIssueTitle": "Active A",
                    "ticket": {
                        "provider": "local",
                        "ticketId": "10",
                        "path": ".ai-dev/tickets",
                    },
                }
            ),
            encoding="utf-8",
        )
        before = self._repository_fingerprint(repo)

        code, _out, err = self._invoke(repo, "57", "--adopt", "recovered")

        self.assertEqual(code, 1)
        self.assertIn("Cannot start workflow: active issue 10 is already set.", err)
        self._assert_unchanged(repo, before)

    def test_active_patch_refuses_adoption(self) -> None:
        repo = self._repo("active-patch")
        (repo / ".ai-dev/workflow.json").write_text(
            json.dumps(
                {
                    "mainBranch": "main",
                    "scratchBranch": "scratch",
                    "checkpoint": 1,
                    "patchDescription": "in-flight patch",
                }
            ),
            encoding="utf-8",
        )
        before = self._repository_fingerprint(repo)

        code, _out, err = self._invoke(repo, "57", "--adopt", "recovered")

        self.assertEqual(code, 1)
        self.assertIn(
            "Cannot start workflow: active patch in-flight patch is already set.",
            err,
        )
        self._assert_unchanged(repo, before)

    def test_blocked_issue_refuses_adoption(self) -> None:
        repo = self._repo("blocked-issue")
        (repo / ".ai-dev/blocked-workflows.json").write_text(
            json.dumps(
                {
                    "blockedWorkflows": [
                        {
                            "issueNumber": 57,
                            "issueTitle": "Adoptable issue",
                            "reason": "waiting",
                            "blockedAt": "2026-01-01T00:00:00Z",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        before = self._repository_fingerprint(repo)

        code, _out, err = self._invoke(repo, "57", "--adopt", "recovered")

        self.assertEqual(code, 1)
        self.assertIn("Cannot start workflow: issue 57 is blocked.", err)
        self.assertIn("flow-resume 57", err)
        self._assert_unchanged(repo, before)

    def test_dirty_working_tree_refuses_adoption(self) -> None:
        repo = self._repo("dirty-tree")
        (repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
        before = self._repository_fingerprint(repo)

        code, _out, err = self._invoke(repo, "57", "--adopt", "recovered")

        self.assertEqual(code, 1)
        self.assertIn("Working tree is not clean.", err)
        self._assert_unchanged(repo, before)

    def test_active_git_operation_refuses_adoption(self) -> None:
        repo = self._repo("active-operation")
        (repo / ".git/MERGE_HEAD").write_text(
            f"{self._git(repo, 'rev-parse', 'recovered')}\n", encoding="utf-8"
        )
        before = self._repository_fingerprint(repo)

        code, _out, err = self._invoke(repo, "57", "--adopt", "recovered")

        self.assertEqual(code, 1)
        self.assertIn("Cannot proceed while Git has active operation(s): merge", err)
        self._assert_unchanged(repo, before)

    # -- target-resolution refusals ----------------------------------------

    def test_unresolvable_target_is_refused(self) -> None:
        repo = self._repo("unresolvable")
        before = self._repository_fingerprint(repo)

        code, _out, err = self._invoke(repo, "57", "--adopt", "no-such-ref")

        self.assertEqual(code, 1)
        self.assertIn(
            "Cannot adopt commit: no-such-ref does not resolve to an object",
            err,
        )
        self._assert_unchanged(repo, before)

    def test_ambiguous_target_is_refused(self) -> None:
        repo = self._repo("ambiguous")
        self._git(repo, "tag", "recovered", "recovered")
        before = self._repository_fingerprint(repo)

        code, _out, err = self._invoke(repo, "57", "--adopt", "recovered")

        self.assertEqual(code, 1)
        self.assertIn("Cannot adopt commit: recovered is ambiguous", err)
        self.assertIn("refs/tags/recovered", err)
        self.assertIn("refs/heads/recovered", err)
        self._assert_unchanged(repo, before)

    def test_non_commit_target_is_refused(self) -> None:
        repo = self._repo("non-commit")
        tree = self._git(repo, "rev-parse", "recovered^{tree}")
        before = self._repository_fingerprint(repo)

        code, _out, err = self._invoke(repo, "57", "--adopt", tree)

        self.assertEqual(code, 1)
        self.assertIn("which is not a commit.", err)
        self._assert_unchanged(repo, before)

    def test_target_equal_to_main_is_refused(self) -> None:
        repo = self._repo("equals-main")
        main_commit = self._git(repo, "rev-parse", "main")
        before = self._repository_fingerprint(repo)

        code, _out, err = self._invoke(repo, "57", "--adopt", "main")

        self.assertEqual(code, 1)
        self.assertIn(
            f"Cannot adopt commit: main already equals main ({main_commit}).",
            err,
        )
        self._assert_unchanged(repo, before)

    def test_target_behind_main_is_refused(self) -> None:
        repo = self._repo("behind-main")
        behind = self._git(repo, "rev-parse", "main")
        (repo / "advance.txt").write_text("advance\n", encoding="utf-8")
        self._git(repo, "add", "advance.txt")
        self._git(repo, "commit", "-q", "-m", "advance main")
        before = self._repository_fingerprint(repo)

        code, _out, err = self._invoke(repo, "57", "--adopt", behind)

        self.assertEqual(code, 1)
        self.assertIn("which is behind main", err)
        self.assertIn("never fetches, merges, rebases, cherry-picks", err)
        self._assert_unchanged(repo, before)

    def test_target_diverged_from_main_is_refused(self) -> None:
        repo = self._repo("diverged")
        self._recovered_branch(repo, "sidetrack", ("side",))
        (repo / "advance.txt").write_text("advance\n", encoding="utf-8")
        self._git(repo, "add", "advance.txt")
        self._git(repo, "commit", "-q", "-m", "advance main")
        before = self._repository_fingerprint(repo)

        code, _out, err = self._invoke(repo, "57", "--adopt", "sidetrack")

        self.assertEqual(code, 1)
        self.assertIn("which is diverged from main", err)
        self._assert_unchanged(repo, before)

    def test_target_unrelated_to_main_is_refused(self) -> None:
        repo = self._repo("unrelated")
        self._git(repo, "checkout", "-q", "--orphan", "orphan")
        self._git(repo, "rm", "-q", "-rf", ".")
        (repo / "orphan.txt").write_text("orphan\n", encoding="utf-8")
        self._git(repo, "add", "orphan.txt")
        self._git(repo, "commit", "-q", "-m", "orphan root")
        self._git(repo, "checkout", "-q", "-f", "main")
        before = self._repository_fingerprint(repo)

        code, _out, err = self._invoke(repo, "57", "--adopt", "orphan")

        self.assertEqual(code, 1)
        self.assertIn("which is unrelated to main", err)
        self._assert_unchanged(repo, before)

    # -- resolver-level behavior -------------------------------------------

    def test_resolver_reports_distinct_reasons(self) -> None:
        repo = self._repo("resolver-reasons")
        self._git(repo, "tag", "recovered", "recovered")
        tree = self._git(repo, "rev-parse", "main^{tree}")

        cases = {
            "": "empty",
            "   ": "empty",
            "no-such-ref": "unresolvable",
            "recovered": "ambiguous",
            tree: "not-a-commit",
        }
        for target, reason in cases.items():
            with self.subTest(target=target):
                with self.assertRaises(RevisionResolutionError) as caught:
                    repository.resolve_commit_ish(repo, target)
                self.assertEqual(caught.exception.reason, reason)


if __name__ == "__main__":
    unittest.main()

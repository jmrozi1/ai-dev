from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_dev_flow import claude_activation as activation
from ai_dev_flow.claude_activation import ClaudeActivationError

REPO_ROOT = Path(__file__).resolve().parents[1]

# Surfaces Issue #56 forbids as executor transport, no matter what fails.
FORBIDDEN_FALLBACKS = (
    "knowledgebase",
    "docs/handoff.md",
    ".ai-dev/tasking.md",
    ".ai-dev/handoff.md",
    "handoff.md",
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.strip()


class _CoordinationFixture(unittest.TestCase):
    """A real coordination repo with a real remote, so push/verify are genuine."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)

        self.remote = self.tmp_path / "coordination-remote.git"
        subprocess.run(
            ["git", "init", "--quiet", "--bare", "-b", "main", str(self.remote)], check=True
        )

        seed = self.tmp_path / "seed"
        seed.mkdir()
        _git(seed, "init", "--quiet", "-b", "main")
        _git(seed, "config", "user.name", "Coordination")
        _git(seed, "config", "user.email", "coordination@example.com")
        scope = seed / "proj" / "issue-1"
        (scope / "rails" / "the-rail").mkdir(parents=True)
        (scope / "state.md").write_text("# State\n", encoding="utf-8")
        (scope / "rails" / "the-rail" / "rail.md").write_text(
            "# Rail: the-rail\n\nStatus: ready\nRole: executor\n", encoding="utf-8"
        )
        (scope / "proceed-sequence.txt").write_text("4\n", encoding="utf-8")
        _git(seed, "add", "-A")
        _git(seed, "commit", "--quiet", "-m", "seed")
        _git(seed, "remote", "add", "origin", str(self.remote))
        _git(seed, "push", "--quiet", "origin", "main")

        self.cache = self.tmp_path / "cache"
        subprocess.run(
            ["git", "clone", "--quiet", str(self.remote), str(self.cache)], check=True
        )
        _git(self.cache, "config", "user.name", "Executor")
        _git(self.cache, "config", "user.email", "executor@example.com")

        self.product = self.tmp_path / "product"
        self.product.mkdir()
        _git(self.product, "init", "--quiet")
        _git(self.product, "remote", "add", "origin", "https://github.com/jmrozi1/proj.git")
        (self.product / ".ai-dev").mkdir()
        (self.product / ".ai-dev" / "workflow.json").write_text(
            json.dumps({"mainBranch": "main", "scratchBranch": "scratch", "checkpoint": 3,
                        "activeIssueNumber": 1}),
            encoding="utf-8",
        )

        self.handoff = self.tmp_path / "handoff.md"
        self.handoff.write_text("# Handoff: the-rail\n\nStatus: completed\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _assert_no_fallback_artifacts(self) -> None:
        for relative in FORBIDDEN_FALLBACKS:
            with self.subTest(fallback=relative):
                self.assertFalse(
                    (self.product / relative).exists(),
                    f"forbidden fallback transport was created: {relative}",
                )

    def _remote_receipt(self) -> str:
        return _git(self.remote, "show", "main:proj/issue-1/proceed-sequence.txt").strip()


class PublicationTransactionTests(_CoordinationFixture):
    def test_publish_pushes_verifies_then_allocates(self) -> None:
        result = activation.publish_executor_handoff(
            self.product, content_file=self.handoff, cache=self.cache
        )

        self.assertEqual(result["railId"], "the-rail")
        self.assertEqual(result["proceed"], 5)
        self.assertEqual(
            result["handoffPath"], "proj/issue-1/rails/the-rail/handoff.md"
        )

        # The handoff is genuinely readable from the remote, not just locally.
        published = _git(self.remote, "show", "main:proj/issue-1/rails/the-rail/handoff.md")
        self.assertIn("Status: completed", published)
        # And the receipt advanced on the remote too.
        self.assertEqual(self._remote_receipt(), "5")

    def test_receipt_is_never_allocated_before_a_durable_push(self) -> None:
        receipt_before = self._remote_receipt()
        real_git = activation._coordination_git

        def fail_push(cache, arguments):
            if arguments and arguments[0] == "push":
                raise ClaudeActivationError("simulated push failure")
            return real_git(cache, arguments)

        with patch.object(activation, "_coordination_git", side_effect=fail_push):
            with self.assertRaises(ClaudeActivationError) as caught:
                activation.publish_executor_handoff(
                    self.product, content_file=self.handoff, cache=self.cache
                )

        self.assertIn("simulated push failure", str(caught.exception))
        # No receipt was consumed, and the remote never saw the handoff.
        self.assertEqual(self._remote_receipt(), receipt_before)
        with self.assertRaises(subprocess.CalledProcessError):
            _git(self.remote, "show", "main:proj/issue-1/rails/the-rail/handoff.md")
        self._assert_no_fallback_artifacts()

    def test_unreadable_remote_after_push_refuses_to_allocate(self) -> None:
        receipt_before = self._remote_receipt()
        real_run = subprocess.run

        def fail_verification(args, *pargs, **kwargs):
            if isinstance(args, list) and "cat-file" in args:
                return subprocess.CompletedProcess(args, 1, "", "")
            return real_run(args, *pargs, **kwargs)

        with patch.object(subprocess, "run", side_effect=fail_verification):
            with self.assertRaises(ClaudeActivationError) as caught:
                activation.publish_executor_handoff(
                    self.product, content_file=self.handoff, cache=self.cache
                )

        self.assertIn("not readable from", str(caught.exception))
        self.assertEqual(self._remote_receipt(), receipt_before)
        self._assert_no_fallback_artifacts()

    def test_allocation_is_delegated_to_the_shared_compare_and_swap(self) -> None:
        with patch.object(
            activation, "allocate_proceed_number", return_value=99
        ) as allocator:
            result = activation.publish_executor_handoff(
                self.product, content_file=self.handoff, cache=self.cache
            )
        allocator.assert_called_once()
        self.assertEqual(result["proceed"], 99)

    def test_rejected_allocation_reports_that_publication_already_succeeded(self) -> None:
        from ai_dev_flow.control_plane import ControlPlaneError

        with patch.object(
            activation,
            "allocate_proceed_number",
            side_effect=ControlPlaneError("compare-and-swap exhausted"),
        ):
            with self.assertRaises(ClaudeActivationError) as caught:
                activation.publish_executor_handoff(
                    self.product, content_file=self.handoff, cache=self.cache
                )

        message = str(caught.exception)
        self.assertIn("published and durable", message)
        self.assertIn("compare-and-swap exhausted", message)
        # Publication really did happen; only the receipt is missing.
        self.assertIn(
            "Status: completed",
            _git(self.remote, "show", "main:proj/issue-1/rails/the-rail/handoff.md"),
        )
        self._assert_no_fallback_artifacts()


class PublicationAuthorizationTests(_CoordinationFixture):
    def test_publishing_to_an_unauthorized_rail_is_refused(self) -> None:
        with self.assertRaises(ClaudeActivationError) as caught:
            activation.publish_executor_handoff(
                self.product,
                content_file=self.handoff,
                rail="some-other-rail",
                cache=self.cache,
            )
        self.assertIn("not the authorized rail", str(caught.exception))
        self._assert_no_fallback_artifacts()

    def test_missing_content_file_is_refused_without_side_effects(self) -> None:
        receipt_before = self._remote_receipt()
        with self.assertRaises(ClaudeActivationError) as caught:
            activation.publish_executor_handoff(
                self.product, content_file=self.tmp_path / "absent.md", cache=self.cache
            )
        self.assertIn("does not exist", str(caught.exception))
        self.assertEqual(self._remote_receipt(), receipt_before)
        self._assert_no_fallback_artifacts()

    def test_unreachable_control_plane_creates_no_fallback_transport(self) -> None:
        with self.assertRaises(ClaudeActivationError) as caught:
            activation.publish_executor_handoff(
                self.product,
                content_file=self.handoff,
                cache=self.tmp_path / "no-such-cache",
            )
        self.assertIn("cache is missing", str(caught.exception))
        self._assert_no_fallback_artifacts()

    def test_inactive_ticket_creates_no_fallback_transport(self) -> None:
        (self.product / ".ai-dev" / "workflow.json").write_text(
            json.dumps({"mainBranch": "main", "scratchBranch": "scratch", "checkpoint": 0}),
            encoding="utf-8",
        )
        with self.assertRaises(ClaudeActivationError):
            activation.publish_executor_handoff(
                self.product, content_file=self.handoff, cache=self.cache
            )
        self._assert_no_fallback_artifacts()


class ContextualStatusTests(_CoordinationFixture):
    def test_status_reports_identity_rail_and_receipt(self) -> None:
        rendered = activation.render_status(self.product, cache=self.cache)

        self.assertIn("jmrozi1/proj", rendered)
        self.assertIn("proj", rendered)
        self.assertIn("issue-1", rendered)
        self.assertIn("the-rail (ready)", rendered)
        self.assertIn("proceed 4", rendered)
        # The receipt must never be presented as authorization.
        self.assertIn("not authorization", rendered)

    def test_status_reports_source_health_when_cache_is_missing(self) -> None:
        rendered = activation.render_status(
            self.product, cache=self.tmp_path / "no-such-cache"
        )
        self.assertIn("UNAVAILABLE", rendered)
        self.assertIn("rail       : unknown", rendered)

    def test_status_reports_unauthorized_rail_without_guessing(self) -> None:
        rail = self.cache / "proj" / "issue-1" / "rails" / "the-rail" / "rail.md"
        rail.write_text("# Rail: the-rail\n\nStatus: completed\nRole: executor\n", encoding="utf-8")
        _git(self.cache, "add", "-A")
        _git(self.cache, "commit", "--quiet", "-m", "complete the rail")
        _git(self.cache, "push", "--quiet", "origin", "main")

        rendered = activation.render_status(self.product, cache=self.cache)
        self.assertIn("UNAUTHORIZED", rendered)

    def test_status_is_contextual_to_the_calling_repository(self) -> None:
        other = self.tmp_path / "other-product"
        other.mkdir()
        _git(other, "init", "--quiet")
        _git(other, "remote", "add", "origin", "https://github.com/jmrozi1/other.git")
        (other / ".ai-dev").mkdir()
        (other / ".ai-dev" / "workflow.json").write_text(
            json.dumps({"mainBranch": "main", "scratchBranch": "scratch", "checkpoint": 0,
                        "activeIssueNumber": 42}),
            encoding="utf-8",
        )

        rendered = activation.render_status(other, cache=self.cache)
        self.assertIn("jmrozi1/other", rendered)
        self.assertIn("issue-42", rendered)
        self.assertNotIn("the-rail (ready)", rendered)


class ReviewEvidenceRoutingTests(unittest.TestCase):
    def test_review_evidence_uses_the_canonical_helper(self) -> None:
        self.assertEqual(
            activation.REVIEW_EVIDENCE_RELATIVE,
            "skills/copilot/auto-review/scripts/review-evidence",
        )
        self.assertTrue((REPO_ROOT / activation.REVIEW_EVIDENCE_RELATIVE).is_file())

    def test_review_evidence_supplies_the_bootstrap_interpreter(self) -> None:
        """python3 must resolve to a real interpreter, not a store alias stub."""
        captured = {}
        helper = str(REPO_ROOT / activation.REVIEW_EVIDENCE_RELATIVE)

        def capture(args, **kwargs):
            # Match the helper invocation itself; shell resolution also uses run().
            if isinstance(args, list) and helper in args:
                env = kwargs.get("env", {})
                shim = Path(env["PATH"].split(os.pathsep)[0]) / "python3"
                # Read it here: the shim lives in a temporary directory that is
                # removed as soon as the call returns.
                captured["exists"] = shim.is_file()
                captured["body"] = shim.read_text(encoding="utf-8") if shim.is_file() else ""
            return subprocess.CompletedProcess(args, 0, "", "")

        with patch.object(subprocess, "run", side_effect=capture):
            activation.run_review_evidence(REPO_ROOT, mode="checkpoint")

        self.assertTrue(captured.get("exists"), "no python3 shim was supplied to the helper")
        self.assertIn(sys.executable, captured["body"])

    def test_posix_shell_resolution_never_returns_the_wsl_launcher(self) -> None:
        resolved = activation.resolve_posix_shell()
        self.assertNotIn("System32", resolved)
        self.assertTrue(Path(resolved).is_file())

    def test_missing_helper_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ClaudeActivationError):
                activation.run_review_evidence(Path(tmp), mode="checkpoint")


class ChatGptDelegationTests(unittest.TestCase):
    """New execution/evidence delegation should name Claude, not Copilot."""

    def _text(self, relative: str) -> str:
        return (REPO_ROOT / relative).read_text(encoding="utf-8")

    def test_flow_delegates_new_execution_to_claude(self) -> None:
        text = self._text("skills/chatgpt/flow/SKILL.md")
        self.assertIn("Delegate routine command execution and repository inspection to the Claude", text)
        self.assertIn("ai-dev status", text)
        # Legacy Copilot support is described, not deleted.
        self.assertIn("Copilot", text)

    def test_auto_review_names_the_claude_evidence_entry_point(self) -> None:
        text = self._text("skills/chatgpt/auto-review/SKILL.md")
        self.assertIn("ai-dev review-evidence", text)
        # The canonical helper remains the single source of the mechanics.
        self.assertIn("skills/copilot/auto-review/scripts/review-evidence", text)

    def test_skill_authoring_layout_lists_the_claude_audience(self) -> None:
        text = self._text("skills/chatgpt/skill-authoring/SKILL.md")
        self.assertIn("skills/claude/<name>/", text)
        self.assertIn("skills/copilot/<name>/", text)

    def test_provider_neutral_orchestrator_language_is_untouched(self) -> None:
        text = self._text("skills/chatgpt/orchestrator/SKILL.md")
        self.assertIn("Copilot", text)
        self.assertIn("Claude", text)


class ClaudeCommandSurfaceTests(unittest.TestCase):
    def test_launcher_exposes_the_checkpoint_three_commands(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "claude" / "ai-dev-entry.py"), "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout)
        for command in ("status", "publish", "review-evidence", "discover", "identity"):
            with self.subTest(command=command):
                self.assertIn(command, completed.stdout)


if __name__ == "__main__":
    unittest.main()

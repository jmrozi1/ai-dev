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


class SourceFreshnessStatusTests(_CoordinationFixture):
    """Finding B: status must name the revision its facts were actually read from.

    The deterministic reader fetches upstream and serves the fetched revision
    while deliberately leaving the cache checkout where it is. Reporting local
    HEAD as the source therefore named a revision the executor never acted on.
    """

    def _advance_upstream(self, receipt: str) -> None:
        """Publish a commit the cache checkout has not seen."""
        seed = self.tmp_path / "seed"
        (seed / "proj" / "issue-1" / "proceed-sequence.txt").write_text(
            f"{receipt}\n", encoding="utf-8"
        )
        _git(seed, "add", "-A")
        _git(seed, "commit", "--quiet", "-m", "advance the receipt")
        _git(seed, "push", "--quiet", "origin", "main")

    def test_behind_cache_reports_the_fetched_revision_it_read(self) -> None:
        self._advance_upstream("7")
        stale_head = _git(self.cache, "rev-parse", "--short", "HEAD")

        rendered = activation.render_status(self.product, cache=self.cache)

        upstream = _git(self.cache, "rev-parse", "--short", "origin/main")
        self.assertNotEqual(upstream, stale_head)
        self.assertIn(f"fetched upstream at {upstream}", rendered)
        self.assertIn(f"cache checkout behind at {stale_head}", rendered)
        # The rail and receipt came from that same fetched revision.
        self.assertIn("the-rail (ready)", rendered)
        self.assertIn("proceed 7", rendered)

    def test_stale_local_head_is_never_labelled_authoritative(self) -> None:
        self._advance_upstream("7")
        stale_head = _git(self.cache, "rev-parse", "--short", "HEAD")

        rendered = activation.render_status(self.product, cache=self.cache)
        source_line = next(
            line for line in rendered.splitlines() if line.startswith("source     :")
        )

        self.assertNotIn(f"fetched upstream at {stale_head}", rendered)
        self.assertNotIn(f"cache at {stale_head} (", source_line)
        self.assertIn("behind", source_line)

    def test_synchronised_cache_says_so(self) -> None:
        rendered = activation.render_status(self.product, cache=self.cache)
        head = _git(self.cache, "rev-parse", "--short", "HEAD")
        self.assertIn(f"fetched upstream at {head} (cache checkout in sync)", rendered)

    def test_cache_without_a_remote_is_reported_as_local(self) -> None:
        _git(self.cache, "remote", "remove", "origin")

        rendered = activation.render_status(self.product, cache=self.cache)

        head = _git(self.cache, "rev-parse", "--short", "HEAD")
        self.assertIn(f"local cache at {head} (no coordination remote)", rendered)
        self.assertIn("the-rail (ready)", rendered)

    def test_unfetchable_upstream_stays_unavailable_and_non_authoritative(self) -> None:
        self.remote.rename(self.tmp_path / "coordination-remote-moved.git")

        rendered = activation.render_status(self.product, cache=self.cache)

        self.assertIn("source     : UNAVAILABLE", rendered)
        self.assertIn("rail       : unknown", rendered)
        self.assertNotIn("(ready)", rendered)
        self.assertNotIn("proceed", rendered)
        self._assert_no_fallback_artifacts()

    def test_one_read_source_serves_every_fact_status_displays(self) -> None:
        """No second freshness model: source line, rail, and receipt share one read."""
        with patch.object(
            activation, "resolve_read_source", wraps=activation.resolve_read_source
        ) as spy:
            rendered = activation.render_status(self.product, cache=self.cache)

        self.assertEqual(spy.call_count, 1)
        self.assertIn("the-rail (ready)", rendered)
        self.assertIn("proceed 4", rendered)

    def test_reads_never_move_the_cache_checkout(self) -> None:
        self._advance_upstream("7")
        before = _git(self.cache, "rev-parse", "HEAD")

        activation.render_status(self.product, cache=self.cache)

        self.assertEqual(_git(self.cache, "rev-parse", "HEAD"), before)
        self.assertEqual(_git(self.cache, "status", "--porcelain"), "")


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

    def test_non_repository_working_directory_fails_closed(self) -> None:
        """Helper resolution no longer depends on the reviewed directory.

        A directory that is not a Git repository is the helper's own concern, and
        it reports that rather than being mistaken for a missing helper.
        """
        with tempfile.TemporaryDirectory() as tmp:
            exit_code = activation.run_review_evidence(Path(tmp), mode="checkpoint")
        self.assertNotEqual(exit_code, 0)

    def test_missing_installed_helper_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(
                activation, "resolve_ai_dev_runtime_root", return_value=Path(tmp)
            ):
                with self.assertRaises(ClaudeActivationError):
                    activation.run_review_evidence(REPO_ROOT, mode="checkpoint")


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


class CrossProductReviewEvidenceTests(unittest.TestCase):
    """Finding A: the helper lives in AI Dev; the CWD is the product repository."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)

        # A supported product repository that deliberately has no AI Dev tree.
        self.product = self.tmp_path / "product"
        self.product.mkdir()
        _git(self.product, "init", "--quiet")
        _git(self.product, "config", "user.name", "Product")
        _git(self.product, "config", "user.email", "product@example.com")
        _git(self.product, "remote", "add", "origin",
             "https://github.com/jmrozi1/family-dragonflight-server.git")
        (self.product / "README.md").write_text("product\n", encoding="utf-8")
        _git(self.product, "add", "-A")
        _git(self.product, "commit", "--quiet", "-m", "initial")
        (self.product / ".ai-dev").mkdir()
        (self.product / ".ai-dev" / "workflow.json").write_text(
            json.dumps({"mainBranch": "main", "scratchBranch": "scratch",
                        "checkpoint": 19, "activeIssueNumber": 9}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_product_repository_has_no_ai_dev_helper_tree(self) -> None:
        self.assertFalse((self.product / "skills").exists())
        self.assertFalse(
            (self.product / activation.REVIEW_EVIDENCE_RELATIVE).exists()
        )

    def test_helper_resolves_from_the_ai_dev_runtime_not_the_product(self) -> None:
        helper = activation.resolve_review_evidence_helper()
        self.assertTrue(helper.is_file())
        self.assertEqual(helper, REPO_ROOT / activation.REVIEW_EVIDENCE_RELATIVE)
        self.assertNotIn(str(self.product), str(helper))

    def test_helper_is_launched_from_ai_dev_with_product_as_cwd(self) -> None:
        captured = {}
        helper = str(activation.resolve_review_evidence_helper())

        def capture(args, **kwargs):
            if isinstance(args, list) and helper in args:
                captured["cwd"] = kwargs.get("cwd")
                captured["args"] = args
            return subprocess.CompletedProcess(args, 0, "", "")

        with patch.object(subprocess, "run", side_effect=capture):
            activation.run_review_evidence(self.product, mode="checkpoint")

        self.assertEqual(captured["cwd"], str(self.product))
        self.assertIn(helper, captured["args"])
        self.assertIn(str(REPO_ROOT), helper)

    def test_evidence_scopes_the_product_repository_end_to_end(self) -> None:
        """The helper really runs, and reports the product repo as its scope."""
        completed = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "claude" / "ai-dev-entry.py"),
             "review-evidence", "--mode", "checkpoint"],
            cwd=str(self.product),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("issue 9", completed.stdout)
        self.assertIn("product", completed.stdout.replace("\\", "/"))
        # No AI Dev tree was copied into the product repository.
        self.assertFalse((self.product / "skills").exists())

    def test_missing_installed_helper_fails_closed_without_product_artifacts(self) -> None:
        with patch.object(
            activation, "resolve_ai_dev_runtime_root", return_value=self.tmp_path / "absent"
        ):
            with self.assertRaises(ClaudeActivationError) as caught:
                activation.run_review_evidence(self.product, mode="checkpoint")

        self.assertIn("installed AI Dev runtime", str(caught.exception))
        for relative in FORBIDDEN_FALLBACKS:
            self.assertFalse((self.product / relative).exists())

    def test_ai_dev_self_repository_case_is_preserved(self) -> None:
        captured = {}
        helper = str(activation.resolve_review_evidence_helper())

        def capture(args, **kwargs):
            if isinstance(args, list) and helper in args:
                captured["cwd"] = kwargs.get("cwd")
            return subprocess.CompletedProcess(args, 0, "", "")

        with patch.object(subprocess, "run", side_effect=capture):
            activation.run_review_evidence(REPO_ROOT, mode="checkpoint")

        self.assertEqual(captured["cwd"], str(REPO_ROOT))


class PushFailureRetryTests(_CoordinationFixture):
    """Finding B: a failed push must leave the cache retryable, not stranded."""

    def _fail_push_once(self):
        real_git = activation._coordination_git
        state = {"failed": False}

        def maybe_fail(cache, arguments):
            if arguments and arguments[0] == "push" and not state["failed"]:
                state["failed"] = True
                raise ClaudeActivationError("simulated push failure")
            return real_git(cache, arguments)

        return maybe_fail, state

    def test_failed_push_consumes_no_receipt_and_publishes_nothing(self) -> None:
        receipt_before = self._remote_receipt()
        maybe_fail, _ = self._fail_push_once()

        with patch.object(activation, "_coordination_git", side_effect=maybe_fail):
            with self.assertRaises(ClaudeActivationError) as caught:
                activation.publish_executor_handoff(
                    self.product, content_file=self.handoff, cache=self.cache
                )

        self.assertIn("retry cleanly", str(caught.exception))
        self.assertEqual(self._remote_receipt(), receipt_before)
        with self.assertRaises(subprocess.CalledProcessError):
            _git(self.remote, "show", "main:proj/issue-1/rails/the-rail/handoff.md")
        self._assert_no_fallback_artifacts()

    def test_cache_is_left_clean_and_not_ahead_of_upstream(self) -> None:
        maybe_fail, _ = self._fail_push_once()
        with patch.object(activation, "_coordination_git", side_effect=maybe_fail):
            with self.assertRaises(ClaudeActivationError):
                activation.publish_executor_handoff(
                    self.product, content_file=self.handoff, cache=self.cache
                )

        self.assertEqual(_git(self.cache, "status", "--porcelain"), "")
        ahead = _git(self.cache, "rev-list", "--count", "origin/main..HEAD")
        self.assertEqual(ahead, "0", "cache is still ahead of upstream after rollback")

    def test_ordinary_retry_succeeds_without_manual_git_repair(self) -> None:
        maybe_fail, _ = self._fail_push_once()
        with patch.object(activation, "_coordination_git", side_effect=maybe_fail):
            with self.assertRaises(ClaudeActivationError):
                activation.publish_executor_handoff(
                    self.product, content_file=self.handoff, cache=self.cache
                )

        receipt_before = int(self._remote_receipt())

        # Exactly the same supported call again. No git commands in between.
        result = activation.publish_executor_handoff(
            self.product, content_file=self.handoff, cache=self.cache
        )

        self.assertEqual(result["proceed"], receipt_before + 1)
        self.assertIn(
            "Status: completed",
            _git(self.remote, "show", "main:proj/issue-1/rails/the-rail/handoff.md"),
        )
        self.assertEqual(int(self._remote_receipt()), receipt_before + 1)

    def test_push_reported_failure_but_remote_accepted_it_is_not_rolled_back(self) -> None:
        """The race: the client errored, yet the commit really did land."""
        real_git = activation._coordination_git

        def push_then_report_failure(cache, arguments):
            if arguments and arguments[0] == "push":
                real_git(cache, arguments)  # the push actually succeeds
                raise ClaudeActivationError("simulated transport error after acceptance")
            return real_git(cache, arguments)

        with patch.object(activation, "_coordination_git", side_effect=push_then_report_failure):
            result = activation.publish_executor_handoff(
                self.product, content_file=self.handoff, cache=self.cache
            )

        # Durable publication was preserved, not destroyed, and verification still
        # gated the receipt.
        self.assertIn(
            "Status: completed",
            _git(self.remote, "show", "main:proj/issue-1/rails/the-rail/handoff.md"),
        )
        self.assertEqual(result["proceed"], 5)

    def test_unrelated_local_coordination_work_is_never_discarded(self) -> None:
        unrelated = self.cache / "proj" / "issue-1" / "unrelated.md"
        unrelated.write_text("someone else's work\n", encoding="utf-8")
        _git(self.cache, "add", "-A")
        _git(self.cache, "commit", "--quiet", "-m", "unrelated local work")
        unrelated_head = _git(self.cache, "rev-parse", "HEAD")

        maybe_fail, _ = self._fail_push_once()
        with patch.object(activation, "_coordination_git", side_effect=maybe_fail):
            with self.assertRaises(ClaudeActivationError):
                activation.publish_executor_handoff(
                    self.product, content_file=self.handoff, cache=self.cache
                )

        # Only this invocation's commit may be undone; the unrelated commit stays.
        self.assertEqual(_git(self.cache, "rev-parse", "HEAD"), unrelated_head)
        self.assertTrue(unrelated.is_file())

    def test_allocator_rejection_still_leaves_durable_publication(self) -> None:
        from ai_dev_flow.control_plane import ControlPlaneError

        with patch.object(
            activation, "allocate_proceed_number",
            side_effect=ControlPlaneError("compare-and-swap exhausted"),
        ):
            with self.assertRaises(ClaudeActivationError) as caught:
                activation.publish_executor_handoff(
                    self.product, content_file=self.handoff, cache=self.cache
                )

        self.assertIn("published and durable", str(caught.exception))
        self.assertIn(
            "Status: completed",
            _git(self.remote, "show", "main:proj/issue-1/rails/the-rail/handoff.md"),
        )
        # A retry must not fabricate or locally count a receipt.
        self._assert_no_fallback_artifacts()


if __name__ == "__main__":
    unittest.main()

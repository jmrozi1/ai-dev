from __future__ import annotations

from pathlib import Path
import unittest

from ai_dev_flow import claude_activation as activation


def _normalized(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").lower().split())


class ClaudeActivationPointerTests(unittest.TestCase):
    """The repository-owned pointer that makes bare `proceed` activate for Claude."""

    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.pointer_path = self.repo_root / "CLAUDE.md"
        self.pointer = _normalized(self.pointer_path)
        self.executor = _normalized(
            self.repo_root / "skills" / "executor" / "SKILL.md"
        )

    # Activation

    def test_pointer_lives_where_claude_code_reads_it(self) -> None:
        self.assertTrue(self.pointer_path.is_file())

    def test_bare_proceed_is_an_executor_instruction_not_a_conversation_resume(self) -> None:
        self.assertIn("bare `proceed` or `continue` is an executor instruction", self.pointer)
        self.assertIn("not a request to resume the conversation", self.pointer)

    def test_proceed_reads_fresh_durable_state_after_clear(self) -> None:
        self.assertIn("read fresh durable state before acting", self.pointer)
        self.assertIn("even immediately after `/clear`", self.pointer)
        self.assertIn(f"{activation.AI_DEV_COMMAND_NAME} discover", self.pointer)

    def test_pointer_routes_to_the_canonical_executor_skill(self) -> None:
        self.assertIn("skills/executor/skill.md", self.pointer)
        self.assertIn("operate as the executor", self.pointer)

    # Non-duplication boundary

    def test_pointer_defers_instead_of_restating_the_contract(self) -> None:
        self.assertIn("do not restate or reimplement them here", self.pointer)
        for duplicated in (
            "publish only what you own",
            "compare-and-swap",
            "a published handoff is bounded current state",
            "executor identity is disposable",
            "context ceiling",
        ):
            with self.subTest(duplicated=duplicated):
                self.assertIn(duplicated, self.executor)
                self.assertNotIn(duplicated, self.pointer)

    def test_pointer_stays_a_pointer_rather_than_a_second_instruction_set(self) -> None:
        self.assertLess(len(self.pointer.split()), len(self.executor.split()) / 4)

    def test_claude_audience_stays_one_bounded_package_set(self) -> None:
        """Issue #56 adds a single Claude audience, not a provider framework."""
        claude_root = self.repo_root / "skills" / "claude"
        packages = sorted(child.name for child in claude_root.iterdir() if child.is_dir())
        self.assertEqual(packages, ["auto-review", "flow"])

        # The provider-neutral role contract has exactly one shared source and is
        # not re-stated per audience.
        self.assertTrue((self.repo_root / "skills" / "executor" / "SKILL.md").is_file())
        for audience in ("claude", "copilot", "chatgpt"):
            with self.subTest(audience=audience):
                self.assertFalse(
                    (self.repo_root / "skills" / audience / "executor").exists()
                )

    def test_provider_neutral_role_contract_is_preserved(self) -> None:
        self.assertIn("the role itself is provider-neutral", self.pointer)
        self.assertIn("only this activation path is claude-specific", self.pointer)

    # Assignment

    def test_a_single_recommended_rail_is_the_whole_assignment(self) -> None:
        self.assertIn("if it identifies exactly one rail for this session", self.pointer)
        self.assertIn("execute only that rail", self.pointer)

    def test_absent_or_ambiguous_assignment_stops_rather_than_guesses(self) -> None:
        self.assertIn("if no rail is assigned, or the assignment is materially ambiguous", self.pointer)
        self.assertIn("stop and report what you read rather than guessing", self.pointer)

    def test_an_explained_unreconciled_status_is_not_a_stop_condition(self) -> None:
        """Taking over a rail normally means reading an unaccepted proposed status."""
        self.assertIn(
            "an unreconciled status the recommendation explains is normal "
            "on takeover, not a stop condition",
            self.pointer,
        )

    def test_only_an_unexplained_contradiction_is_materially_ambiguous(self) -> None:
        self.assertIn(
            "durable state contradicting the recommendation in a way it "
            "does not explain",
            self.pointer,
        )

    # Tasking precedence

    def test_the_reported_rail_is_the_whole_assignment(self) -> None:
        self.assertIn("the rail discovery reports is the assignment", self.pointer)

    def test_no_local_branch_can_contradict_durable_authorization(self) -> None:
        """A checked-in fallback rail would compete with the authorized one."""
        self.assertIn("outranks durable control-plane authorization", self.pointer)
        for retired in (".ai-dev/tasking.md", "when no control plane is configured"):
            with self.subTest(retired=retired):
                self.assertNotIn(retired, self.pointer)

        # The provider-neutral role contract still owns the unconfigured case;
        # only this repository's pointer stops restating it.
        self.assertIn(
            "when no control plane is configured, `.ai-dev/tasking.md` remains the rail",
            self.executor,
        )


def _fenced_commands(text: str) -> list[str]:
    """Every command line inside a fenced block, in order."""
    commands: list[str] = []
    inside = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            inside = not inside
            continue
        if inside and stripped:
            commands.append(stripped)
    return commands


class ActivationBootstrapAgreementTests(unittest.TestCase):
    """Repo-local and host activation must not drift to different bootstraps.

    They drifted once: this file kept documenting a repository-local control
    plane after the accepted path became the installed command, so a fresh
    session followed instructions that reported `control plane: not configured`.
    """

    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.pointer = (self.repo_root / "CLAUDE.md").read_text(encoding="utf-8")
        self.block = activation.render_activation_block()

    def test_both_name_the_same_installed_bootstrap_command(self) -> None:
        command = f"{activation.AI_DEV_COMMAND_NAME} discover"
        self.assertIn(command, self.block)
        self.assertIn(command, self.pointer)

    def test_the_repository_pointer_documents_no_second_bootstrap(self) -> None:
        expected = [f"{activation.AI_DEV_COMMAND_NAME} discover"]
        self.assertEqual(_fenced_commands(self.pointer), expected)
        self.assertEqual(_fenced_commands(self.block), expected)

    def test_neither_documents_the_retired_local_control_plane_bootstrap(self) -> None:
        for label, text in (("CLAUDE.md", self.pointer), ("activation block", self.block)):
            with self.subTest(label=label):
                self.assertNotIn("python -m ai_dev_flow.control_plane", text)


if __name__ == "__main__":
    unittest.main()

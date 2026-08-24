from __future__ import annotations

from pathlib import Path
import unittest


def _normalized(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").lower().split())


class ClaudeActivationPointerTests(unittest.TestCase):
    """The repository-owned pointer that makes bare `proceed` activate for Claude."""

    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.pointer_path = self.repo_root / "CLAUDE.md"
        self.pointer = _normalized(self.pointer_path)
        self.executor = _normalized(
            self.repo_root / "skills" / "copilot" / "executor" / "SKILL.md"
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
        self.assertIn("python -m ai_dev_flow.control_plane status", self.pointer)

    def test_pointer_routes_to_the_canonical_executor_skill(self) -> None:
        self.assertIn("skills/copilot/executor/skill.md", self.pointer)
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

    def test_pointer_creates_no_claude_skill_or_provider_framework(self) -> None:
        self.assertFalse((self.repo_root / "skills" / "claude").exists())
        catalog = _normalized(self.repo_root / "skills" / "index.md")
        self.assertNotIn("claude", catalog)

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

    # Tasking precedence

    def test_configured_rail_outranks_local_tasking_file(self) -> None:
        self.assertIn("the authorized rail is the assignment", self.pointer)
        self.assertIn("`.ai-dev/tasking.md` is not canonical", self.pointer)
        self.assertIn("prefer the rail wherever the two disagree", self.pointer)

    def test_unconfigured_fallback_matches_the_executor_contract(self) -> None:
        fallback = "when no control plane is configured, `.ai-dev/tasking.md` remains the rail"
        self.assertIn(fallback, self.pointer)
        self.assertIn(fallback, self.executor)


if __name__ == "__main__":
    unittest.main()

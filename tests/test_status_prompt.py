from __future__ import annotations

from pathlib import Path
import re
import unittest


class StatusPromptContractTests(unittest.TestCase):
    """Static/contract tests for the native Copilot /status slash command."""

    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.prompt_file = self.repo_root / ".github" / "prompts" / "status.prompt.md"

    def test_repository_contains_status_prompt_file(self) -> None:
        """Verify the repository contains the native status.prompt.md."""
        self.assertTrue(
            self.prompt_file.exists(),
            f"Expected {self.prompt_file} to exist",
        )

    def test_slash_command_name_resolves_to_status(self) -> None:
        """Verify the slash-command name in YAML frontmatter is 'status'."""
        content = self.prompt_file.read_text(encoding="utf-8")

        # Extract YAML frontmatter
        match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
        self.assertIsNotNone(match, "Expected YAML frontmatter in status.prompt.md")

        frontmatter = match.group(1)
        # Check for 'name: status'
        self.assertIn(
            "name: status",
            frontmatter,
            "Expected 'name: status' in YAML frontmatter",
        )

    def test_description_indicates_ai_dev_progress(self) -> None:
        """Verify description mentions AI Dev and progress."""
        content = self.prompt_file.read_text(encoding="utf-8")

        # Extract YAML frontmatter
        match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
        self.assertIsNotNone(match, "Expected YAML frontmatter in status.prompt.md")

        frontmatter = match.group(1)
        self.assertIn(
            "description:",
            frontmatter,
            "Expected 'description:' in YAML frontmatter",
        )
        # Check that description is concise and mentions relevant terms
        self.assertTrue(
            re.search(r"description:.*?(progress|status|active|ticket)", frontmatter, re.IGNORECASE),
            "Expected description to mention progress/status/active/ticket",
        )

    def test_delegates_to_copilot_flow_skill(self) -> None:
        """Verify the prompt delegates to Copilot Flow rather than implementing status."""
        content = self.prompt_file.read_text(encoding="utf-8")

        # Should mention Copilot Flow skill
        self.assertIn("Copilot Flow", content)
        self.assertIn("scripts/ticket-status", content)

        # Should NOT implement status logic
        self.assertNotIn("def render", content)
        self.assertNotIn("class Status", content)
        self.assertNotIn("ticket_status =", content)

        # Should explicitly state it delegates
        self.assertTrue(
            re.search(r"(delegate|route|use.*Copilot Flow|skill)", content, re.IGNORECASE),
            "Expected prompt to explicitly state it delegates to Copilot Flow",
        )

    def test_supports_normal_and_verbose_status_paths(self) -> None:
        """Verify the prompt explicitly defines both the bare /status and verbose paths."""
        content = self.prompt_file.read_text(encoding="utf-8")

        self.assertIn("/status", content)
        self.assertIn("bare /status", content.lower())
        self.assertIn("normal", content.lower())
        self.assertIn("verbose", content.lower())
        self.assertIn("/status verbose", content)

        self.assertTrue(
            re.search(r"bare\s+/status.*?(normal|default)|normal\s+status.*?bare\s+/status", content, re.IGNORECASE | re.DOTALL),
            "Expected prompt to define bare /status as the normal status path",
        )
        self.assertTrue(
            re.search(r"verbose.*?(detailed|full description|roadmap|expanded)|/status verbose.*?(detailed|full description|roadmap|expanded)", content, re.IGNORECASE | re.DOTALL),
            "Expected prompt to describe the verbose path clearly",
        )

    def test_avoids_substituting_git_status(self) -> None:
        """Verify the prompt explicitly states it should not substitute git status."""
        content = self.prompt_file.read_text(encoding="utf-8")

        # Should explicitly state NOT to do git status
        self.assertIn("git status", content.lower())

        # Should state this in a negative context (allowing for markdown formatting)
        self.assertTrue(
            re.search(r"(do\s+\*\*not\*\*|do\s+not|should\s+not|avoid)", content.lower()),
            "Expected prompt to state negatively about git status",
        )

    def test_avoids_substituting_session_history(self) -> None:
        """Verify the prompt does not handle session history or standup."""
        content = self.prompt_file.read_text(encoding="utf-8")

        # Should mention session history or standup in a "do not" section
        self.assertIn("session history", content.lower())
        self.assertIn("standup", content.lower())

        # Should be in a "do not" or negative section
        self.assertTrue(
            re.search(r"(do\s+\*\*not\*\*|do\s+not|should\s+not|avoid)[^.]*show.*?session", content, re.IGNORECASE | re.DOTALL),
            "Expected prompt to state it does not show session history",
        )

    def test_bare_status_cannot_fall_back_to_git_or_workspace_status(self) -> None:
        """Verify the prompt explicitly requires normal ticket status and forbids Git/workspace fallback."""
        content = self.prompt_file.read_text(encoding="utf-8")

        self.assertIn("no argument", content.lower())
        self.assertIn("normal ai dev ticket/project status", content.lower())
        self.assertIn("do not run git status", content.lower())
        self.assertIn("do not synthesize repository status", content.lower())
        self.assertIn("return the existing helper output", content.lower())

        self.assertTrue(
            re.search(r"no\s+argument.*?(normal\s+ai\s+dev\s+ticket/project\s+status|normal\s+status)", content, re.IGNORECASE | re.DOTALL),
            "Expected prompt to define the no-argument path as the normal AI Dev status route",
        )

    def test_avoids_clarification_questions(self) -> None:
        """Verify the prompt does not ask clarification questions."""
        content = self.prompt_file.read_text(encoding="utf-8")

        # Should mention clarification questions
        self.assertIn("clarification", content.lower())

        # Should state this in a "do not" section
        self.assertTrue(
            re.search(r"(do\s+\*\*not\*\*|do\s+not|should\s+not|avoid)[^.]*ask.*?clarif", content, re.IGNORECASE | re.DOTALL),
            "Expected prompt to state it should not ask clarification questions",
        )

    def test_no_generalized_prompt_framework_introduced(self) -> None:
        """Verify no generalized prompt/slash-command framework was introduced."""
        repo_root = self.repo_root

        # Check that .github/prompts contains only status.prompt.md
        prompts_dir = repo_root / ".github" / "prompts"
        if prompts_dir.exists():
            prompt_files = list(prompts_dir.glob("*.prompt.md"))
            self.assertEqual(
                len(prompt_files),
                1,
                f"Expected exactly one prompt file, found {len(prompt_files)}: {prompt_files}",
            )
            self.assertEqual(
                prompt_files[0].name,
                "status.prompt.md",
                f"Expected only status.prompt.md, found {prompt_files[0].name}",
            )

        # Check that no new command registry or framework exists
        github_dir = repo_root / ".github"
        if github_dir.exists():
            # Should not have a commands.json, registry.json, or similar
            self.assertFalse(
                (github_dir / "commands.json").exists(),
                "Should not create a commands.json registry file",
            )
            self.assertFalse(
                (github_dir / "slash-commands.json").exists(),
                "Should not create a slash-commands registry file",
            )
            self.assertFalse(
                (github_dir / "prompt-registry.md").exists(),
                "Should not create a prompt registry file",
            )


if __name__ == "__main__":
    unittest.main()

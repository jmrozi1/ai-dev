from __future__ import annotations

from pathlib import Path
import re
import unittest


class FlowSkillDiscoveryTests(unittest.TestCase):
    """Discovery-contract tests for Copilot Flow skill frontmatter."""

    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.flow_skill = self.repo_root / "skills" / "copilot" / "flow" / "SKILL.md"

    def test_flow_skill_exists(self) -> None:
        """Verify the Copilot Flow skill file exists."""
        self.assertTrue(
            self.flow_skill.exists(),
            f"Expected {self.flow_skill} to exist",
        )

    def test_frontmatter_includes_status_keywords(self) -> None:
        """Verify frontmatter description advertises active-ticket/project status."""
        content = self.flow_skill.read_text(encoding="utf-8")

        # Extract YAML frontmatter
        match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
        self.assertIsNotNone(match, "Expected YAML frontmatter in Flow SKILL.md")

        frontmatter = match.group(1).lower()

        # Should advertise status responsibility
        self.assertTrue(
            re.search(r"(status|project|active.?ticket|checkpoint|progress)", frontmatter),
            "Expected frontmatter description to advertise status responsibility",
        )

    def test_frontmatter_includes_lifecycle_keywords(self) -> None:
        """Verify frontmatter description advertises natural-language lifecycle intents."""
        content = self.flow_skill.read_text(encoding="utf-8")

        # Extract YAML frontmatter
        match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
        self.assertIsNotNone(match, "Expected YAML frontmatter in Flow SKILL.md")

        frontmatter = match.group(1).lower()

        # Should mention key lifecycle intents (not require all, but a good mix)
        lifecycle_keywords = [
            r"start\s+ticket",
            r"start\s+patch",
            r"checkpoint",
            r"close.*out",
        ]

        matches = sum(1 for keyword in lifecycle_keywords if re.search(keyword, frontmatter))
        self.assertGreaterEqual(
            matches,
            2,
            f"Expected frontmatter to mention at least 2 lifecycle intents, found {matches}",
        )

    def test_frontmatter_advertises_repository_execution(self) -> None:
        """Verify frontmatter mentions exact repository-facing execution."""
        content = self.flow_skill.read_text(encoding="utf-8")

        # Extract YAML frontmatter
        match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
        self.assertIsNotNone(match, "Expected YAML frontmatter in Flow SKILL.md")

        frontmatter = match.group(1).lower()

        # Should mention Flow lifecycle or repository execution
        self.assertTrue(
            re.search(r"(flow.*lifecycle|repository|execute)", frontmatter),
            "Expected frontmatter to mention Flow lifecycle or repository execution",
        )

    def test_skill_body_preserves_intent_mapping(self) -> None:
        """Verify the skill body still contains the detailed intent-mapping table."""
        content = self.flow_skill.read_text(encoding="utf-8")

        # Extract body (after frontmatter)
        body = re.split(r"^---\n.*?\n---", content, maxsplit=1, flags=re.DOTALL)
        self.assertGreaterEqual(len(body), 2, "Expected frontmatter and body")

        body_text = body[1].lower()

        # Should still have the intent mapping section
        self.assertIn("intent mapping", body_text)
        self.assertIn("start ticket", body_text)
        self.assertIn("checkpoint this", body_text)
        self.assertIn("close this out", body_text)

    def test_frontmatter_not_duplicating_table(self) -> None:
        """Verify frontmatter description does not duplicate the intent-mapping table."""
        content = self.flow_skill.read_text(encoding="utf-8")

        # Extract YAML frontmatter
        match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
        self.assertIsNotNone(match, "Expected YAML frontmatter in Flow SKILL.md")

        frontmatter = match.group(1)

        # Should not have pipe characters (table markers)
        self.assertNotIn("|", frontmatter)

    def test_description_is_reasonably_concise(self) -> None:
        """Verify description is concise (single line, not excessive)."""
        content = self.flow_skill.read_text(encoding="utf-8")

        # Extract YAML frontmatter
        match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
        self.assertIsNotNone(match, "Expected YAML frontmatter in Flow SKILL.md")

        frontmatter = match.group(1)

        # Extract description line
        desc_match = re.search(r"description:\s*(.+?)$", frontmatter, re.MULTILINE)
        self.assertIsNotNone(desc_match, "Expected description field in frontmatter")

        description = desc_match.group(1)

        # Description should be reasonably concise (not over 250 chars)
        self.assertLess(
            len(description),
            250,
            f"Description is too long ({len(description)} chars): {description}",
        )

    def test_discovery_contract_vs_chronicle_skill(self) -> None:
        """Verify Flow can be discovered for status without competing with Chronicle."""
        content = self.flow_skill.read_text(encoding="utf-8")

        # Extract YAML frontmatter
        match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
        self.assertIsNotNone(match, "Expected YAML frontmatter in Flow SKILL.md")

        frontmatter = match.group(1).lower()

        # Flow should mention "active ticket" or "project status" (not session/standup)
        self.assertTrue(
            re.search(r"(active.?ticket|project.?status)", frontmatter),
            "Expected Flow to advertise active-ticket/project-status responsibility",
        )

        # Flow should NOT claim to handle session history or standup
        # (those are Chronicle's responsibility)
        self.assertNotIn("standup", frontmatter)
        self.assertNotIn("session history", frontmatter)


if __name__ == "__main__":
    unittest.main()

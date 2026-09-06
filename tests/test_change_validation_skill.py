from __future__ import annotations

from pathlib import Path
import unittest


def _normalized(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").lower().split())


class ChangeValidationSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.path = self.root / "skills" / "change-validation" / "SKILL.md"
        self.skill = _normalized(self.path)

    def test_policy_is_discoverable_as_one_shared_skill(self) -> None:
        catalog = _normalized(self.root / "skills" / "index.md")
        self.assertTrue(self.path.is_file())
        self.assertIn("| `change-validation` |", catalog)
        self.assertIn("`skills/change-validation/skill.md`", catalog)

    def test_tiers_scale_with_changed_boundaries(self) -> None:
        for tier in ("local", "contract", "integration", "full or live regression"):
            with self.subTest(tier=tier):
                self.assertIn(tier, self.skill)
        self.assertIn("novelty and blast radius", self.skill)
        self.assertIn("numeric checkpoint", self.skill)
        self.assertIn("is not by itself a reason", self.skill)

    def test_tests_replace_repeated_manual_proof(self) -> None:
        self.assertIn("once an accepted automated test protects an invariant", self.skill)
        self.assertIn("do not reconstruct the invariant's original manual proof", self.skill)
        self.assertIn("mutation evidence", self.skill)
        self.assertIn("not a default certification layer", self.skill)

    def test_reviewer_attacks_the_novel_claim_without_duplicate_certification(self) -> None:
        self.assertIn("attack the riskiest novel claim", self.skill)
        self.assertIn("instead of duplicating the executor's entire certification", self.skill)
        self.assertIn("re-run evidence only when", self.skill)

    def test_pre_release_history_is_not_a_consumer(self) -> None:
        self.assertIn("before the first supported release", self.skill)
        self.assertIn("git history and an earlier checkpoint are not consumers", self.skill)
        self.assertIn("name the consumer or data", self.skill)

    def test_adjacent_simplification_is_bounded(self) -> None:
        self.assertIn("allow directly adjacent simplification", self.skill)
        self.assertIn("inside the same changed boundary", self.skill)
        self.assertIn("does not become unrelated cleanup", self.skill)

    def test_telemetry_is_diagnostic_and_bounded(self) -> None:
        self.assertIn("validation runtime -> total agent runtime", self.skill)
        self.assertIn("not score velocity or trigger autonomous intervention", self.skill)
        self.assertIn("usually under 5 minutes", self.skill)
        self.assertIn("usually under 15 minutes", self.skill)

    def test_prose_obligations_can_retire(self) -> None:
        self.assertIn("once automation supersedes a prose obligation", self.skill)
        self.assertIn("remove the repeated prose", self.skill)
        self.assertIn("no more than monthly", self.skill)
        self.assertIn("do not create a dashboard or history database", self.skill)

    def test_role_skills_reference_the_policy_without_copying_it(self) -> None:
        paths = (
            "skills/executor/SKILL.md",
            "skills/chatgpt/orchestrator/SKILL.md",
            "skills/review-process/SKILL.md",
            "skills/chatgpt/auto-review/SKILL.md",
            "skills/copilot/auto-review/SKILL.md",
            "skills/feedback-loop-design/SKILL.md",
        )
        for relative in paths:
            with self.subTest(relative=relative):
                content = _normalized(self.root / relative)
                self.assertIn("change-validation", content)


if __name__ == "__main__":
    unittest.main()

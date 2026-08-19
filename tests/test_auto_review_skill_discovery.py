from __future__ import annotations

from pathlib import Path
import unittest


class AutoReviewSkillDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.skill = Path(__file__).resolve().parents[1] / "skills" / "copilot" / "auto-review" / "SKILL.md"
        self.content = " ".join(self.skill.read_text(encoding="utf-8").lower().split())

    def test_guidance_owns_telemetry_cadence_at_review_boundary(self) -> None:
        self.assertIn("review-evidence` automatically attempts", self.content)
        self.assertIn("orchestrator does not need to ask for a separate telemetry refresh", self.content)
        self.assertIn("available telemetry is management evidence included automatically", self.content)
        self.assertIn("unavailable or insufficient telemetry is reported concisely and remains non-blocking", self.content)
        self.assertIn("genuine collection failure is also reported concisely and remains non-blocking", self.content)

    def test_guidance_preserves_judgment_and_reporting_boundaries(self) -> None:
        self.assertIn("never determine review pass/fail", self.content)
        self.assertIn("not executor optimization targets", self.content)
        self.assertIn("session-scoped or unattributable usage must not be presented as issue-attributable", self.content)
        self.assertIn("scenarios must not be presented as actual totals or bounds", self.content)
        self.assertIn("ordinary flow lifecycle commands remain telemetry-independent", self.content)
        self.assertNotIn("first check that the configured output file exists", self.content)
        self.assertNotIn("report the exact human action required and stop", self.content)


if __name__ == "__main__":
    unittest.main()
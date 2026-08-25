from __future__ import annotations

from pathlib import Path
import unittest


def _normalized(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").lower().split())


class ReviewProcessConvergenceJudgmentTests(unittest.TestCase):
    """`review-process` judges global convergence, not just local evidence discipline."""

    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.skill_path = self.repo_root / "skills" / "review-process" / "SKILL.md"
        self.raw = self.skill_path.read_text(encoding="utf-8")
        self.skill = _normalized(self.skill_path)
        self.catalog_path = self.repo_root / "skills" / "index.md"

    # Structure

    def test_convergence_judgment_is_its_own_section_before_the_stage_sections(self) -> None:
        self.assertIn("\n## Convergence Judgment\n", self.raw)
        convergence = self.raw.index("\n## Convergence Judgment\n")
        checkpoint = self.raw.index("\n## Checkpoint Process Review\n")
        promotion = self.raw.index("\n## Promotion Process Review\n")
        self.assertLess(convergence, checkpoint)
        self.assertLess(convergence, promotion)

    def test_both_stage_sections_route_convergence_to_the_global_judgment(self) -> None:
        self.assertIn("judge convergence globally, per `convergence judgment`", self.skill)
        self.assertIn(
            "convergence and approach quality over the issue, per `convergence judgment`",
            self.skill,
        )

    # Local discipline versus global convergence

    def test_local_evidence_discipline_does_not_prove_global_convergence(self) -> None:
        self.assertIn(
            "correct local evidence discipline does not prove global convergence",
            self.skill,
        )
        self.assertIn(
            "invalid evidence may be retired correctly, provenance may be tracked honestly, "
            "and stop conditions may work exactly as intended while the overall evidence "
            "strategy still repeatedly fails to advance the underlying proof obligation",
            self.skill,
        )
        self.assertIn(
            "do not read good local epistemic discipline as evidence that the work is converging",
            self.skill,
        )

    def test_convergence_is_judged_separately_from_per_item_evidence_handling(self) -> None:
        self.assertIn("convergence is a global property of how the work is going", self.skill)
        self.assertIn(
            "judge it separately from how well each individual piece of evidence was handled",
            self.skill,
        )

    # Apparatus churn as material evidence

    def test_apparatus_churn_counts_only_once_the_loop_is_dominant(self) -> None:
        self.assertIn(
            "treat the following as material convergence evidence once the apparatus loop has "
            "become the dominant work rather than an incidental cost of it",
            self.skill,
        )

    def test_material_apparatus_signals_are_enumerated(self) -> None:
        for signal in (
            "repeated invalid controls or baselines",
            "repeated provenance or evidence invalidation",
            "repeated reconstruction or repair of diagnostic, test, probe, harness, "
            "fixture, or measurement apparatus",
            "successive materially equivalent attempts ending in the same class of "
            "unresolved obligation for the same route reason",
        ):
            with self.subTest(signal=signal):
                self.assertIn(signal, self.skill)

    # Persistence negative boundary

    def test_a_single_persistent_proof_obligation_is_not_enough_alone(self) -> None:
        self.assertIn("persistence alone is not the signal", self.skill)
        self.assertIn(
            "one proof obligation persisting across several attempts is not sufficient by itself",
            self.skill,
        )
        self.assertIn(
            "only when paired with observable churn or repeated failure of the route or "
            "measurement strategy itself",
            self.skill,
        )

    def test_steadily_converging_work_is_not_a_convergence_finding(self) -> None:
        self.assertIn(
            "long, slow, or difficult work that keeps resolving real obligations, narrowing "
            "the problem, or producing valid new evidence is converging",
            self.skill,
        )

    def test_no_fixed_threshold_is_introduced_by_the_clarification(self) -> None:
        section = self.raw.split("## Convergence Judgment", 1)[1].split("\n## ", 1)[0].lower()
        for forbidden in (
            "threshold",
            "score",
            "dashboard",
            "counter",
            "monitor",
            "timer",
            "retry framework",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, section)

    # Global, process-oriented result

    def test_the_question_stays_global_and_process_oriented(self) -> None:
        self.assertIn(
            "ask whether the approach, decomposition, executor rail, or evidence strategy "
            "should change in order to reach the current objective",
            self.skill,
        )

    def test_no_process_change_warranted_remains_available(self) -> None:
        self.assertIn(
            "an explicit `no process change warranted` remains a complete and preferred answer here",
            self.skill,
        )

    def test_the_clarification_does_not_become_an_implementation_review(self) -> None:
        self.assertIn(
            "inspect the apparatus only far enough to judge that; repairing it is "
            "implementation work, not process review",
            self.skill,
        )

    # Ownership split with the calling review machinery

    def test_applicability_and_composition_stay_with_the_calling_machinery(self) -> None:
        self.assertIn(
            "that machinery decides when a review applies and what it composes with; "
            "this skill decides process quality",
            self.skill,
        )

    def test_auto_review_applicability_guidance_is_not_duplicated_here(self) -> None:
        section = self.raw.split("## Convergence Judgment", 1)[1].split("\n## ", 1)[0].lower()
        for owned_by_auto_review in (
            "natural handoff",
            "consider in-flight review only when",
            "mid-rail",
            "materially equivalent rail",
            "compose `review-process`",
        ):
            with self.subTest(owned_by_auto_review=owned_by_auto_review):
                self.assertNotIn(owned_by_auto_review, section)

    # Preserved existing behavior

    def test_atomic_independent_invocation_is_preserved(self) -> None:
        self.assertIn(
            "review is independent and atomic: you can invoke it alone without lifecycle "
            "automation or load composition",
            self.skill,
        )

    def test_checkpoint_and_promotion_review_behavior_survives(self) -> None:
        for preserved in (
            "checkpoint process review is frequent and cheap",
            "promotion process review is less frequent and more comprehensive",
            "at every named checkpoint review",
            "before promotion review can pass, perform a final skill-candidate disposition",
            "do not turn checkpoint review into an implementation or code review",
            "an explicit \"no process change is warranted\" is a complete and preferred result",
        ):
            with self.subTest(preserved=preserved):
                self.assertIn(preserved, self.skill)

    def test_skill_candidate_accounting_survives(self) -> None:
        self.assertIn("\n### Skill Candidates\n", self.raw)
        self.assertIn("\n### Accepted Skills\n", self.raw)
        self.assertIn(
            "the active ticket's `skill candidates` section is the durable hypothesis surface",
            self.skill,
        )
        self.assertIn("do not use a numeric threshold such as a required observation count", self.skill)

    # Discovery surface

    def test_catalog_row_still_routes_process_judgment_here_without_claiming_applicability(self) -> None:
        rows = [
            line
            for line in self.catalog_path.read_text(encoding="utf-8").splitlines()
            if line.startswith("| `review-process`")
        ]
        self.assertEqual(len(rows), 1)
        row = rows[0].lower()
        self.assertIn("process review", row)
        self.assertIn("assess approach", row)
        self.assertNotIn("in-flight", row)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from pathlib import Path
import unittest


def _normalized(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").lower().split())


class AutoReviewInFlightCompositionTests(unittest.TestCase):
    """In-flight process review is a third composition stage owned by ChatGPT auto-review."""

    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.skill_path = self.repo_root / "skills" / "chatgpt" / "auto-review" / "SKILL.md"
        self.skill = _normalized(self.skill_path)
        self.catalog = _normalized(self.repo_root / "skills" / "index.md")

    # Structure

    def test_in_flight_composition_is_its_own_section(self) -> None:
        raw = self.skill_path.read_text(encoding="utf-8")
        self.assertIn("\n## In-Flight Composition\n", raw)
        checkpoint = raw.index("\n## Checkpoint Composition\n")
        promotion = raw.index("\n## Promotion Composition\n")
        in_flight = raw.index("\n## In-Flight Composition\n")
        self.assertLess(checkpoint, in_flight)
        self.assertLess(promotion, in_flight)

    def test_in_flight_review_is_bound_to_a_handoff_not_a_lifecycle_boundary(self) -> None:
        self.assertIn("a third composition stage, distinct from checkpoint and promotion review", self.skill)
        self.assertIn("bound to a handoff rather than a lifecycle boundary", self.skill)

    # Positive activation boundary

    def test_activation_requires_an_active_named_checkpoint_at_a_natural_handoff(self) -> None:
        self.assertIn("consider in-flight review only when all of the following hold", self.skill)
        self.assertIn("the active named ticket checkpoint is still in progress", self.skill)
        self.assertIn("you are at a natural handoff", self.skill)
        self.assertIn(
            "the previous executor rail has ended in a published handoff, block, or failure "
            "and no executor is currently executing an authorized rail",
            self.skill,
        )
        self.assertIn("you are about to issue the next rail for that same named checkpoint", self.skill)

    def test_activation_never_preempts_a_running_executor(self) -> None:
        self.assertIn("never interrupts, pauses, or preempts an executor mid-rail", self.skill)
        self.assertIn("never requires a running executor to stop and self-review", self.skill)

    def test_material_signals_are_route_and_measurement_churn(self) -> None:
        for signal in (
            "repeated invalid controls or baselines",
            "repeated provenance or evidence invalidation",
            "repeated reconstruction or repair of probes, harnesses, fixtures, or measurement tooling",
            "the evidence apparatus having become the dominant work",
            "successive materially equivalent rails ending in the same class of unresolved obligation",
        ):
            with self.subTest(signal=signal):
                self.assertIn(signal, self.skill)
        self.assertIn("this is a judgment, never a count", self.skill)

    # Negative boundary

    def test_steadily_converging_work_does_not_activate(self) -> None:
        self.assertIn(
            "or the mere length or difficulty of work that is steadily converging",
            self.skill,
        )
        self.assertIn(
            "a long checkpoint that keeps resolving real obligations, narrowing the problem, "
            "or producing valid new evidence continues without review",
            self.skill,
        )

    def test_no_fixed_threshold_of_any_kind_activates_the_stage(self) -> None:
        self.assertIn(
            "never activate on elapsed time, wall-clock duration, prompt, turn, or exchange count, "
            "token or credit usage, retry count, number of flow checkpoint commits, any score or "
            "dashboard metric",
            self.skill,
        )
        self.assertIn("if a proposed rule could be evaluated by counting alone, it is outside this stage", self.skill)

    def test_a_single_persistent_proof_obligation_is_not_enough_alone(self) -> None:
        self.assertIn(
            "persistence of one proof obligation across several rails is not sufficient by itself",
            self.skill,
        )
        self.assertIn(
            "only when paired with observable churn or failure of the route or measurement strategy",
            self.skill,
        )
        self.assertIn(
            "correctly retiring invalid evidence and correctly honoring stop conditions are good "
            "local discipline, not churn",
            self.skill,
        )

    # Ownership split

    def test_review_process_remains_the_sole_process_quality_judge(self) -> None:
        self.assertIn("`review-process` alone decides process quality here", self.skill)
        self.assertIn(
            "this skill owns applicability, timing, composition, evidence surface, and escalation",
            self.skill,
        )
        self.assertIn("does not pre-judge, override, or substitute for that judgment", self.skill)

    def test_narrow_review_question_precedes_a_materially_equivalent_rail(self) -> None:
        self.assertIn("compose `review-process` before issuing another materially equivalent rail", self.skill)
        self.assertIn(
            "should the approach, decomposition, executor rail, or evidence strategy change",
            self.skill,
        )

    def test_no_process_change_warranted_is_a_complete_result(self) -> None:
        self.assertIn("an explicit `no process change warranted`", self.skill)
        self.assertIn(
            "`no process change warranted` is a complete, preferred result and permits "
            "reissuing the intended rail",
            self.skill,
        )

    # Tasking carry-forward

    def test_findings_carry_into_the_current_tasking_surface(self) -> None:
        self.assertIn(
            "carry material findings into the current tasking surface: orchestrator-owned accepted "
            "state and authorized rail under a control plane, otherwise `.ai-dev/tasking.md`",
            self.skill,
        )

    def test_a_rejected_strategy_is_not_immediately_reissued_unchanged(self) -> None:
        self.assertIn(
            "do not immediately reissue a materially equivalent rejected strategy unchanged",
            self.skill,
        )
        self.assertIn("revisit it only with a stated reason grounded in new evidence", self.skill)

    # Preservation of checkpoint and promotion behavior

    def test_in_flight_review_is_additive_and_never_records_a_pass(self) -> None:
        self.assertIn(
            "never replaces, defers, or satisfies checkpoint review or promotion review, "
            "never records a review pass",
            self.skill,
        )
        self.assertIn("never advances the named roadmap or a flow checkpoint", self.skill)

    def test_checkpoint_and_promotion_composition_survive_unchanged(self) -> None:
        for preserved in (
            "for checkpoint review, apply `review-process`",
            "must reassess the ticket's current `skill candidates` against the new checkpoint evidence",
            "for promotion review, apply `review-process` and decide whether `frontend-design-review` applies",
            "promotion review is the final skill-candidate gate for the issue",
            "`skills/copilot/auto-review/scripts/record-promotion-review`",
        ):
            with self.subTest(preserved=preserved):
                self.assertIn(preserved, self.skill)

    def test_in_flight_review_may_surface_a_candidate_but_not_dispose_of_one(self) -> None:
        self.assertIn("in-flight review may add a `skill candidates` entry when evidence warrants", self.skill)
        self.assertIn(
            "does not perform and does not satisfy the named-checkpoint or promotion "
            "skill-candidate disposition gate",
            self.skill,
        )

    def test_violation_cases_are_stated_explicitly(self) -> None:
        for violation in (
            "firing on long but steadily converging work",
            "relying on any fixed threshold",
            "interrupting or preempting an executor mid-rail",
            "deciding process quality here or skipping `review-process`",
            "treating in-flight review as, or in place of, checkpoint or promotion review",
            "firing on a single persistent proof obligation with no route or measurement churn",
        ):
            with self.subTest(violation=violation):
                self.assertIn(violation, self.skill)

    # No new machinery

    def test_no_monitoring_or_scoring_machinery_is_introduced(self) -> None:
        self.assertIn(
            "introducing a monitoring service, timer, counter, scoring system, dashboard, "
            "transcript store, retry framework, or new skill in order to implement it",
            self.skill,
        )
        self.assertIn("build no report, monitor, or store", self.skill)
        self.assertFalse((self.repo_root / "skills" / "diagnostic-investigation").exists())
        self.assertFalse((self.repo_root / "skills" / "chatgpt" / "diagnostic-investigation").exists())

    # Discovery

    def test_activation_metadata_covers_in_flight_applicability(self) -> None:
        description = _normalized(self.skill_path).split("description:", 1)[1].split("---", 1)[0]
        self.assertIn("in-flight", description)
        self.assertIn("natural orchestrator handoff", description)
        self.assertIn("named checkpoint is still in progress", description)
        self.assertIn("checkpoint, promotion, or in-flight review composition", description)

    def test_catalog_entry_mirrors_the_canonical_activation_boundary(self) -> None:
        self.assertIn(
            "| `auto-review` | deciding checkpoint/promotion review composition and "
            "pass/action-required judgment, including final readiness of ticket skill-candidate "
            "and accepted-skill state; also deciding whether in-flight process review applies at a "
            "natural orchestrator handoff while a named checkpoint is still in progress. | "
            "`skills/chatgpt/auto-review/skill.md` |",
            self.catalog,
        )

    def test_catalog_does_not_overactivate_adjacent_skills(self) -> None:
        review_process_row = [
            line
            for line in (self.repo_root / "skills" / "index.md").read_text(encoding="utf-8").splitlines()
            if line.startswith("| `review-process`")
        ]
        self.assertEqual(len(review_process_row), 1)
        self.assertNotIn("in-flight", review_process_row[0].lower())


if __name__ == "__main__":
    unittest.main()

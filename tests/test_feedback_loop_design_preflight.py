"""`feedback-loop-design` requires apparatus preflight before an expensive loop."""

from __future__ import annotations

from pathlib import Path
import unittest


def _normalized(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").lower().split())


class FeedbackLoopDesignPreflightTests(unittest.TestCase):
    """The skill proves the harness reaches the costly boundary before paying for it."""

    SECTION = "## Prove The Apparatus Before Paying The Cost"

    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.skill_path = self.repo_root / "skills" / "feedback-loop-design" / "SKILL.md"
        self.raw = self.skill_path.read_text(encoding="utf-8")
        self.skill = _normalized(self.skill_path)
        self.catalog_path = self.repo_root / "skills" / "index.md"

    def _section(self) -> str:
        body = self.raw.split(self.SECTION, 1)[1].split("\n## ", 1)[0]
        return " ".join(body.lower().split())

    # Structure

    def test_the_preflight_section_exists_once(self) -> None:
        self.assertEqual(self.raw.count("\n" + self.SECTION + "\n"), 1)

    def test_preflight_follows_payload_and_precedes_rail_sizing(self) -> None:
        payload = self.raw.index("\n## Increase Payload Before Paying The Outer-Loop Cost\n")
        preflight = self.raw.index("\n" + self.SECTION + "\n")
        rail = self.raw.index("\n## Size The Execution Rail To The Next Branch Point\n")
        self.assertLess(payload, preflight)
        self.assertLess(preflight, rail)

    def test_the_costly_boundaries_it_guards_are_named(self) -> None:
        self.assertIn(
            "before the first materially expensive boundary",
            self.skill,
        )
        for boundary in (
            "a download or install",
            "a build or deployment",
            "an authentication step",
            "a provider call",
            "a live environment",
            "a rendered or relayed observation",
            "a human handoff",
        ):
            with self.subTest(boundary=boundary):
                self.assertIn(boundary, self.skill)

    def test_the_failure_it_prevents_is_stated_as_evidence_loss(self) -> None:
        self.assertIn(
            "a prepared pass can fail without producing any evidence when the harness, "
            "driver, or command it depends on was never checked against the place it will "
            "actually run",
            self.skill,
        )

    # Target runtime and tool compatibility

    def test_target_runtime_and_tool_versions_must_be_proven(self) -> None:
        self.assertIn(
            "prove the target runtime, interpreter, and tool versions the apparatus "
            "requires, including the interface features it actually uses",
            self.skill,
        )

    def test_a_material_host_target_difference_requires_a_target_side_self_test(self) -> None:
        self.assertIn(
            "when host and target differ materially, run the cheapest faithful self-test on "
            "the target itself rather than trusting local behavior",
            self.skill,
        )

    def test_local_behavior_is_not_target_capability_evidence(self) -> None:
        self.assertIn(
            "non-exhaustive help output, a local host's version of a tool, and a neighboring "
            "interface's behavior are not capability evidence",
            self.skill,
        )
        self.assertIn(
            "working locally does not establish that it works on the target",
            self.skill,
        )

    # Exact command, option, and input-shape validation

    def test_exact_options_and_input_shapes_must_be_validated(self) -> None:
        self.assertIn(
            "validate the exact options, arguments, and input shapes the target will accept, "
            "from authoritative interface documentation or a bounded direct probe",
            self.skill,
        )

    def test_absence_from_abbreviated_help_is_not_an_unsupported_verdict(self) -> None:
        self.assertIn(
            "absence from an abbreviated listing does not establish that an option is "
            "unsupported",
            self.skill,
        )

    # Compile, parse, and dry construction

    def test_the_driver_is_constructed_through_the_last_deterministic_boundary(self) -> None:
        self.assertIn(
            "compile, parse, or otherwise construct the driver through the last deterministic "
            "boundary before the expensive action, including the state and ordering "
            "preconditions that action requires",
            self.skill,
        )

    def test_dry_construction_covers_ordering_not_only_syntax(self) -> None:
        self.assertIn("dry construction covers ordering as well as syntax", self.skill)
        self.assertIn(
            "build the request, command, or payload in the exact state the real call will see, "
            "so a precondition that can only hold earlier in the sequence fails "
            "deterministically and cheaply instead of after the cost is paid",
            self.skill,
        )

    # Cheap preflight versus the expensive outer attempt

    def test_cheap_preflight_iteration_is_explicitly_repairable(self) -> None:
        self.assertIn("preflight iteration is cheap and repairable", self.skill)
        self.assertIn(
            "correct a failed compatibility check, an invalid option, or a broken dry "
            "construction and continue",
            self.skill,
        )

    def test_the_expensive_attempt_defers_to_the_existing_no_automatic_retry_rule(self) -> None:
        self.assertIn(
            "once the expensive attempt begins, that latitude ends and the "
            "no-automatic-retry rule above governs",
            self.skill,
        )

    def test_the_distinction_is_protected_in_both_directions(self) -> None:
        self.assertIn(
            "an authoring defect corrected before a declared pass is not a failure of that "
            "pass, and a declared validation or expensive attempt does not become repeatable "
            "by describing its failure as a typo",
            self.skill,
        )

    # Negative boundaries on the refinement itself

    def test_no_counting_scoring_or_retry_machinery_is_introduced(self) -> None:
        section = self._section()
        for forbidden in (
            "counter",
            "count",
            "score",
            "threshold",
            "matrix",
            "checklist",
            "framework",
            "budget",
            "timer",
            "dashboard",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, section)

    def test_the_refinement_carries_no_numeric_limit(self) -> None:
        self.assertFalse(
            [character for character in self._section() if character.isdigit()],
            "the preflight section must not introduce a numeric limit",
        )

    def test_the_refinement_stays_provider_and_language_neutral(self) -> None:
        section = self._section()
        for task_specific in (
            "python",
            "curl",
            "claude",
            "bash",
            "json",
            "subprocess",
            "virtual environment",
        ):
            with self.subTest(task_specific=task_specific):
                self.assertNotIn(task_specific, section)

    # Declaring what a deliberate-breakage pass will affect

    def test_an_exact_affected_set_is_derived_from_the_anchor_and_its_coupling(self) -> None:
        section = self._section()
        self.assertIn("exact set of tests", section)
        self.assertIn("exact text", section)
        self.assertIn("structurally coupled", section)

    def test_the_behavioral_invariant_side_of_the_declaration_survives(self) -> None:
        """Searching for the anchor is added to the invariant, never substituted for it."""
        section = self._section()
        self.assertIn("prohibited behavioral invariant", section)
        self.assertIn("derive that set two ways", section)

    def test_semantic_name_recall_is_rejected_as_an_enumeration_method(self) -> None:
        section = self._section()
        self.assertIn("sound related", section)
        self.assertIn("is not an enumeration method", section)

    def test_a_coupled_extra_failure_is_in_scope_and_still_disclosed(self) -> None:
        section = self._section()
        self.assertIn("inside the pass", section)
        self.assertIn("proves the declaration incomplete", section)
        self.assertIn("disclose the omission", section)
        self.assertIn("relabelling the extra failure unrelated", section)

    def test_a_pass_claiming_no_exact_set_owes_no_enumeration(self) -> None:
        section = self._section()
        self.assertIn("keep this proportional", section)
        self.assertIn("claims no exact set owes no enumeration", section)

    def test_the_declaration_rule_introduces_no_new_machinery(self) -> None:
        section = self._section()
        for machinery in ("dependency analyzer", "mutation harness", "registry of controls"):
            with self.subTest(machinery=machinery):
                self.assertIn(machinery, section)
        for absent in ("mutation score", "control registry file", "declaration report"):
            with self.subTest(absent=absent):
                self.assertNotIn(absent, section)

    def test_the_declaration_guidance_sits_inside_the_preflight_section(self) -> None:
        """It is preflight discipline, not a separate stage with its own heading."""
        headings = [line for line in self.raw.splitlines() if line.startswith("## ")]
        self.assertEqual(headings.count(self.SECTION.replace("## ", "## ")), 1)
        for invented in ("## Declare", "## Negative Control", "## Mutation"):
            with self.subTest(invented=invented):
                self.assertNotIn("\n" + invented, self.raw)

    # Preserved activation and existing rules

    def test_the_positive_activation_boundary_survives(self) -> None:
        self.assertIn(
            "design discovery, prototyping, implementation, and validation loops when builds, "
            "live environments, screenshots, human relay, or other feedback are materially "
            "slow or costly",
            self.skill,
        )

    def test_the_negative_routine_work_boundary_survives(self) -> None:
        self.assertIn(
            "do not use for routine work whose direct feedback loop is already fast",
            self.skill,
        )
        self.assertIn("do not optimize a loop that is already direct and cheap", self.skill)

    def test_existing_no_automatic_retry_rule_is_preserved_verbatim(self) -> None:
        self.assertIn(
            "an unexpected result is not a reason for an automatic retry. diagnose what the "
            "result changed, revise the hypothesis or implementation, and perform another "
            "expensive pass only when it can establish newly useful evidence",
            self.skill,
        )

    def test_maximize_useful_evidence_and_rail_sizing_rules_are_preserved(self) -> None:
        for preserved in (
            "maximize useful evidence, not raw output",
            "use proportional judgment rather than a scoring formula or fixed priority "
            "between time and credits",
            "stop at the next genuine product, scope, architecture, permission, or "
            "evidence-strategy branch rather than predicting work beyond it",
            "do not create a dependency graph, planning framework, or task-history system to "
            "represent the rail",
            "never avoid a necessary human decision merely to reduce interaction count",
        ):
            with self.subTest(preserved=preserved):
                self.assertIn(preserved, self.skill)

    # Discovery surface

    def test_the_catalog_row_is_unchanged_and_still_routes_here(self) -> None:
        rows = [
            line
            for line in self.catalog_path.read_text(encoding="utf-8").splitlines()
            if line.startswith("| `feedback-loop-design`")
        ]
        self.assertEqual(len(rows), 1)
        self.assertIn("skills/feedback-loop-design/SKILL.md", rows[0])
        self.assertNotIn("preflight", rows[0].lower())


if __name__ == "__main__":
    unittest.main()

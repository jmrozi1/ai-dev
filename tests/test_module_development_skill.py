from __future__ import annotations

from pathlib import Path
import re
import unittest


def _normalized(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").lower().split())


def _frontmatter_name(path: Path) -> str | None:
    match = re.match(
        r"^---\s*\n(.*?)\n---\s*\n", path.read_text(encoding="utf-8"), re.DOTALL
    )
    if match is None:
        return None
    name = re.search(r"^name:\s*(\S+)\s*$", match.group(1), re.MULTILINE)
    return None if name is None else name.group(1)


class ModuleDevelopmentSkillTests(unittest.TestCase):
    """Issue #76 deliverable 12: development architecture for requirement-shaped modules.

    These protect the accepted contract, not the sentences that currently carry
    it: the dimensions a slice must be refined along, the ambient capabilities
    that must be injected, the decision/adapter split, the inner-loop rule, and
    the routing to sibling skills. Each obligation is satisfied by any of several
    wordings, so a materially different but valid rewrite of the guidance keeps
    passing. Where an exact phrase is asserted it is because that phrase is the
    accepted rule itself, quoted from Issue #76.
    """

    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.path = self.root / "skills" / "module-development" / "SKILL.md"
        self.skill = _normalized(self.path)

    def assert_covers(self, obligation: str, *accepted: str) -> None:
        self.assertTrue(
            any(phrase in self.skill for phrase in accepted),
            f"module-development does not cover {obligation}; "
            f"expected one of {accepted}",
        )

    def test_policy_is_discoverable_as_one_shared_skill(self) -> None:
        catalog = _normalized(self.root / "skills" / "index.md")
        self.assertTrue(self.path.is_file())
        self.assertEqual(_frontmatter_name(self.path), "module-development")
        self.assertIn("| `module-development` |", catalog)
        self.assertIn("`skills/module-development/skill.md`", catalog)

    def test_a_slice_is_refined_along_every_required_dimension(self) -> None:
        # Refinement happens before implementation, not recovered from the code.
        self.assert_covers(
            "refining before implementation",
            "before implementing",
            "before implementation",
        )
        self.assert_covers("observable behavior", "observable behavior", "observable behaviour")
        self.assert_covers("inputs and outputs", "inputs and outputs", "inputs/outputs")
        self.assert_covers("failure behavior", "failure behavior", "failure behaviour")
        self.assert_covers("dependencies", "dependencies")
        self.assert_covers("side effects", "side effects")
        self.assert_covers("sufficient evidence", "sufficient evidence")

    def test_ambient_capabilities_are_injected_rather_than_rediscovered(self) -> None:
        self.assert_covers("injection", "inject")
        for capability in ("clock", "filesystem", "repositor", "process control", "network"):
            with self.subTest(capability=capability):
                self.assertIn(capability, self.skill)
        self.assert_covers(
            "the prohibition on reaching for ambient state inside domain logic",
            "must not reach",
            "rather than rediscovering",
            "do not rediscover",
        )

    def test_decisions_are_separated_from_adapters_and_return_structured_results(self) -> None:
        self.assert_covers("adapters", "adapter")
        self.assert_covers("structured results", "structured result")
        self.assert_covers(
            "asserting on the result instead of the implementation",
            "mocked call",
            "captured log",
            "sequence of mocked calls",
        )

    def test_the_inner_loop_does_not_mandate_test_first_sequencing(self) -> None:
        self.assert_covers("developing behavior and tests together", "inner loop")
        relaxed = re.search(
            r"test-first[^.]{0,160}?(not required|not an obligation|not mandatory|optional)"
            r"|(not required|without requiring|need not|not an obligation)[^.]{0,160}?test-first",
            self.skill,
        )
        self.assertIsNotNone(
            relaxed,
            "module-development must state that strict test-first sequencing is "
            "not required; a fast inner loop that mandates it is the failure "
            "mode this deliverable removes.",
        )
        self.assert_covers(
            "the requirement that claimed behavior be falsifiable by its tests",
            "would fail if the claimed behavior were absent",
            "would fail",
        )

    def test_permanent_tests_must_survive_a_different_valid_implementation(self) -> None:
        # Issue #76's standing permanence rule, quoted; this is the accepted
        # wording of the contract rather than incidental phrasing.
        self.assertIn("accepted requirement or durable invariant", self.skill)
        self.assertIn("narrowest stable boundary", self.skill)
        self.assertIn("materially different valid implementation", self.skill)
        self.assertIn("continue to pass", self.skill)

    def test_the_submitted_unit_is_a_coherent_requirement_slice(self) -> None:
        self.assertIn("coherent requirement slice", self.skill)
        self.assert_covers(
            "rejecting per-edit integration checkpoints",
            "every small edit",
            "every edit",
        )
        self.assert_covers(
            "rejecting batched unrelated slices",
            "unrelated slices",
            "several unrelated",
        )

    def test_routing_to_sibling_skills_resolves_to_real_skills(self) -> None:
        # The slice's intent, its evidence tier, and its integration cadence are
        # owned elsewhere; this skill must route to them and not restate them.
        for sibling in (
            "requirements-driven-development",
            "change-validation",
            "integration-signal",
        ):
            with self.subTest(sibling=sibling):
                self.assertIn(sibling, self.skill)
                self.assertTrue(
                    (self.root / "skills" / sibling / "SKILL.md").is_file(),
                    f"module-development routes to {sibling}, which does not exist.",
                )

    def test_role_skills_route_to_the_policy_without_copying_it(self) -> None:
        for relative in (
            "skills/executor/SKILL.md",
            "skills/requirements-driven-development/SKILL.md",
            "skills/chatgpt/orchestrator/SKILL.md",
            "skills/feedback-loop-design/SKILL.md",
        ):
            with self.subTest(relative=relative):
                self.assertIn("module-development", _normalized(self.root / relative))


if __name__ == "__main__":
    unittest.main()

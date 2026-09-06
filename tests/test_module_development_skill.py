from __future__ import annotations

from pathlib import Path
import re
import unittest


def _normalized_text(text: str) -> str:
    return " ".join(text.lower().split())


def _normalized(path: Path) -> str:
    return _normalized_text(path.read_text(encoding="utf-8"))


def _covers(text: str, accepted: tuple[str, ...]) -> bool:
    """Whether any accepted phrasing of an obligation appears in `text`."""
    return any(phrase in text for phrase in accepted)


def _guidance_body(path: Path) -> str:
    """The skill minus its frontmatter — the text an activated skill is read for.

    The frontmatter `description` is a routing blurb used to pick the skill; it
    is not where an obligation is carried. Scoping to the body keeps a section's
    protection from being satisfied by the blurb that merely names it.
    """
    text = path.read_text(encoding="utf-8")
    body = re.sub(r"\A---\s*\n.*?\n---\s*\n", "", text, count=1, flags=re.DOTALL)
    return " ".join(body.lower().split())


def _frontmatter_name(path: Path) -> str | None:
    match = re.match(
        r"^---\s*\n(.*?)\n---\s*\n", path.read_text(encoding="utf-8"), re.DOTALL
    )
    if match is None:
        return None
    name = re.search(r"^name:\s*(\S+)\s*$", match.group(1), re.MULTILINE)
    return None if name is None else name.group(1)


# Two obligations were pinned to a single wording: refinement-before-building was
# accepted only as "before implementing"/"before implementation" (two inflections
# of one phrase, not two wordings), and injection only as the literal word
# "inject". Both are stated by plenty of valid prose that uses neither, so a
# materially different valid rewrite of the guidance failed the ticket's own
# permanence rule. These sets accept the obligation however it is worded, and the
# falsifiability tests below prove they still fail when it is absent.
_REFINE_BEFORE_BUILDING = (
    "before implementing",
    "before implementation",
    "before it is implemented",
    "before any code is written",
    "before writing the code",
    "before building it",
    "before the module is built",
    "prior to implementation",
    "ahead of implementation",
)

_INJECT_THE_WORLD = (
    "inject",
    "pass in every ambient capability",
    "passed in at the module boundary",
    "supplied by the caller",
    "provided by the caller",
    "handed to the module",
)

# Fixtures for the falsifiability proofs: a materially different but valid
# statement of each obligation, and the same guidance with that obligation
# deleted outright.
_REFINEMENT_REWRITE = """
## Settle A Slice Before Any Code Is Written

Six things about a coherent requirement slice are decided prior to
implementation: the observable behavior, the inputs and outputs, the failure
behavior, the dependencies, the side effects, and the evidence that would show
the slice satisfied or violated.

A slice whose failure behavior was discovered while coding rather than decided
up front is the usual source of an untestable boundary.
"""

_REFINEMENT_DELETED = """
## Settle A Slice

Six things about a coherent requirement slice are decided: the observable
behavior, the inputs and outputs, the failure behavior, the dependencies, the
side effects, and the evidence that would show the slice satisfied or violated.

A slice whose failure behavior was discovered while coding rather than decided
is the usual source of an untestable boundary.
"""

_INJECTION_REWRITE = """
## Hand The World In At The Boundary

Domain logic never reaches out for the world. Every ambient capability a module
needs -- the clock, the filesystem, repositories, process control, and the
network -- is supplied by the caller at the module boundary, so the module can
be exercised with a substitute without patching global state.
"""

_INJECTION_DELETED = """
## Name The World A Module Touches

A module's ambient capabilities are the clock, the filesystem, repositories,
process control, and the network. Keep that list short.
"""


class ModuleDevelopmentSkillTests(unittest.TestCase):
    """Issue #76 deliverable 12: development architecture for requirement-shaped modules.

    These protect the accepted contract, not the sentences that currently carry
    it: the dimensions a slice must be refined along, the shape that makes a
    unit a module, the ambient capabilities that must be injected, the
    decision/adapter split, the inner-loop rule, and the routing to sibling
    skills. Each obligation is satisfied by any of several
    wordings, so a materially different but valid rewrite of the guidance keeps
    passing. Where an exact phrase is asserted it is because that phrase is the
    accepted rule itself, quoted from Issue #76.
    """

    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.path = self.root / "skills" / "module-development" / "SKILL.md"
        self.skill = _normalized(self.path)
        self.body = _guidance_body(self.path)

    def assert_covers(self, obligation: str, *accepted: str) -> None:
        self.assertTrue(
            _covers(self.skill, accepted),
            f"module-development does not cover {obligation}; "
            f"expected one of {accepted}",
        )

    def assert_body_covers(self, obligation: str, *accepted: str) -> None:
        self.assertTrue(
            _covers(self.body, accepted),
            f"module-development's guidance body does not cover {obligation}; "
            f"naming it in the frontmatter description does not carry it; "
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
            *_REFINE_BEFORE_BUILDING,
        )
        self.assert_covers("observable behavior", "observable behavior", "observable behaviour")
        self.assert_covers("inputs and outputs", "inputs and outputs", "inputs/outputs")
        self.assert_covers("failure behavior", "failure behavior", "failure behaviour")
        self.assert_covers("dependencies", "dependencies")
        self.assert_covers("side effects", "side effects")
        self.assert_covers("sufficient evidence", "sufficient evidence")

    def test_modules_are_shaped_as_independently_constructible_units(self) -> None:
        # Deliverable 12's named module-boundary rule: what makes a unit a
        # module at all, and the seam test that says when two are really one.
        self.assert_body_covers(
            "a module being constructible on its own, without the rest of the system",
            "independently constructible",
            "independently buildable",
            "constructed independently",
            "without standing up the rest",
            "without the rest of the system",
        )
        self.assert_body_covers(
            "the narrow contract a module is given",
            "narrow contract",
            "narrow surface",
            "small named surface",
            "small, named surface",
            "minimal contract",
        )
        self.assert_body_covers(
            "one responsibility, drawn from the requirement rather than the layering",
            "single responsibility",
            "one responsibility",
            "a responsibility drawn from",
        )
        self.assert_body_covers(
            "preferring a capability boundary over a grouping by mechanism",
            "as a capability",
            "capability boundary",
            "by mechanism",
            "groups code by mechanism",
        )
        self.assert_body_covers(
            "the confused-seam rule for modules that need each other's internals",
            "confused seam",
            "each other's internals",
            "one another's internals",
            "know each other's internals",
        )

    def test_ambient_capabilities_are_injected_rather_than_rediscovered(self) -> None:
        self.assert_body_covers("injection", *_INJECT_THE_WORLD)
        for capability in ("clock", "filesystem", "repositor", "process control", "network"):
            with self.subTest(capability=capability):
                self.assertIn(capability, self.skill)
        self.assert_covers(
            "the prohibition on reaching for ambient state inside domain logic",
            "must not reach",
            "rather than rediscovering",
            "do not rediscover",
        )

    def test_the_widened_wordings_accept_a_rewrite_and_still_fail_on_deletion(self) -> None:
        """The two widened obligations must be wording-independent, not vacuous.

        Widening an assertion until any prose satisfies it removes the
        protection instead of making it honest, so each set is proved both
        ways: it accepts a materially different valid statement of the
        obligation, and it still rejects guidance the obligation was deleted
        from.
        """
        for obligation, accepted, rewrite, deleted in (
            (
                "refining before the slice is built",
                _REFINE_BEFORE_BUILDING,
                _REFINEMENT_REWRITE,
                _REFINEMENT_DELETED,
            ),
            (
                "injecting ambient capabilities at the boundary",
                _INJECT_THE_WORLD,
                _INJECTION_REWRITE,
                _INJECTION_DELETED,
            ),
        ):
            with self.subTest(obligation=obligation):
                self.assertTrue(
                    _covers(_normalized_text(rewrite), accepted),
                    f"a valid rewrite of {obligation} is rejected, so the "
                    f"assertion pins wording rather than the contract",
                )
                self.assertFalse(
                    _covers(_normalized_text(deleted), accepted),
                    f"guidance with {obligation} deleted is still accepted, so "
                    f"the assertion has been widened into vacuity",
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

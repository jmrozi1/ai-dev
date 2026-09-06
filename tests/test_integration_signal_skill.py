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


# The staleness prohibition was accepted only as "stale green", "carry a green
# forward" or "carry it forward" -- every alternate required the word "green"
# next to "stale" or "carry". Guidance that states the same obligation plainly,
# without that adjacency, was rejected, which is the ticket's own permanence
# rule violated in its own test. This set accepts the obligation however it is
# worded; the falsifiability test below proves it still fails when the
# obligation is absent.
_NEVER_CARRY_A_RESULT_FORWARD = (
    "stale green",
    "carry a green forward",
    "carry it forward",
    "carry a passing result",
    "do not carry a result forward",
    "says nothing about a later commit",
    "does not transfer to a later commit",
    "never transfers to a newer candidate",
    "belongs only to the commit it ran against",
)

# Fixtures for the falsifiability proof: a materially different but valid
# statement of the prohibition that never uses the word "green", and the same
# guidance with the prohibition deleted outright.
_STALENESS_REWRITE = """
## A Passing Run Belongs Only To Its Own Commit

A run that passed at one commit says nothing about a later commit containing
changes that run never executed. Do not report the tip as validated because an
earlier commit passed, do not accept a named checkpoint on a result that
predates the commit being accepted, and do not let a coalesced-away candidate
inherit a neighbour's result. When the latest relevant candidate has no passing
run of its own, the honest state is not yet known.
"""

_STALENESS_DELETED = """
## Report The Latest Result

Say which commit the result came from and which commit is being asked about, so
the run's coverage is explicit.
"""


def _frontmatter_name(path: Path) -> str | None:
    match = re.match(
        r"^---\s*\n(.*?)\n---\s*\n", path.read_text(encoding="utf-8"), re.DOTALL
    )
    if match is None:
        return None
    name = re.search(r"^name:\s*(\S+)\s*$", match.group(1), re.MULTILINE)
    return None if name is None else name.group(1)


class IntegrationSignalSkillTests(unittest.TestCase):
    """Issue #76 deliverable 13: asynchronous exact-SHA integration policy.

    These protect the accepted contract rather than the prose: the record's
    required fields, the bounded coalescing queue, the staleness rule, the
    bounded blast radius of a red run, the attribution range, and the acceptance
    precondition. Field names and the three blocking consequences are asserted
    literally because they are the enumerated contract itself; everything else
    accepts any of several wordings, so a materially different but valid rewrite
    of the guidance keeps passing.
    """

    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.path = self.root / "skills" / "integration-signal" / "SKILL.md"
        self.skill = _normalized(self.path)
        self.body = _guidance_body(self.path)

    def assert_covers(self, obligation: str, *accepted: str) -> None:
        self.assertTrue(
            _covers(self.skill, accepted),
            f"integration-signal does not cover {obligation}; "
            f"expected one of {accepted}",
        )

    def assert_body_covers(self, obligation: str, *accepted: str) -> None:
        self.assertTrue(
            _covers(self.body, accepted),
            f"integration-signal's guidance body does not cover {obligation}; "
            f"naming it in the frontmatter description does not carry it; "
            f"expected one of {accepted}",
        )

    def test_policy_is_discoverable_as_one_shared_skill(self) -> None:
        catalog = _normalized(self.root / "skills" / "index.md")
        self.assertTrue(self.path.is_file())
        self.assertEqual(_frontmatter_name(self.path), "integration-signal")
        self.assertIn("| `integration-signal` |", catalog)
        self.assertIn("`skills/integration-signal/skill.md`", catalog)

    def test_submission_is_gated_locally_and_integration_runs_asynchronously(self) -> None:
        # Module and affected-contract tests are the synchronous gate; the
        # integration run is not, and development continues alongside it.
        self.assert_covers("the local module gate", "module tests")
        self.assert_covers(
            "the affected-contract gate",
            "every contract it affects",
            "affected contract",
            "contracts it affects",
        )
        self.assert_covers("naming the synchronous gate", "synchronous gate", "synchronous")
        self.assert_covers(
            "continuing development alongside the run",
            "in parallel",
            "while independent development continues",
        )

    def test_every_run_record_names_its_required_fields(self) -> None:
        for field in (
            "tested sha",
            "change range",
            "selected suites",
            "runtime",
            "outcome",
            "last known green",
        ):
            with self.subTest(field=field):
                self.assertIn(field, self.skill)

    def test_the_queue_keeps_only_the_active_run_and_the_newest_pending_candidate(self) -> None:
        self.assert_covers("the active run", "active run")
        self.assert_covers("the newest pending candidate", "newest pending")
        self.assert_covers("coalescing superseded runs", "coalesc")
        self.assert_covers("superseding", "supersed")
        self.assert_covers(
            "refusing one queued run per commit",
            "one run per commit",
            "per commit",
        )

    def test_a_green_result_never_transfers_to_a_newer_candidate(self) -> None:
        self.assert_body_covers(
            "the staleness prohibition",
            *_NEVER_CARRY_A_RESULT_FORWARD,
        )
        self.assert_covers(
            "refusing an ancestor's green for a descendant",
            "predates",
            "ancestor was green",
            "an ancestor",
        )
        self.assert_covers(
            "reporting unknown rather than green",
            "not yet known",
            "not known",
        )

    def test_the_staleness_wording_accepts_a_rewrite_and_still_fails_on_deletion(self) -> None:
        """The prohibition must be wording-independent, not vacuous.

        Widening an assertion until any prose satisfies it removes the
        protection instead of making it honest, so the accepted set is proved
        both ways: it accepts a materially different valid statement of the
        prohibition that never says "green", and it still rejects guidance the
        prohibition was deleted from.
        """
        self.assertTrue(
            _covers(_normalized_text(_STALENESS_REWRITE), _NEVER_CARRY_A_RESULT_FORWARD),
            "a valid rewrite of the staleness prohibition is rejected, so the "
            "assertion pins wording rather than the contract",
        )
        self.assertFalse(
            _covers(_normalized_text(_STALENESS_DELETED), _NEVER_CARRY_A_RESULT_FORWARD),
            "guidance with the staleness prohibition deleted is still accepted, "
            "so the assertion has been widened into vacuity",
        )

    def test_a_red_run_blocks_acceptance_and_dependents_but_not_unrelated_work(self) -> None:
        self.assert_covers(
            "blocking named-checkpoint acceptance",
            "named-checkpoint acceptance",
            "checkpoint acceptance",
        )
        self.assert_covers("blocking promotion", "promotion")
        self.assert_covers("blocking dependent work", "dependent work")
        self.assert_covers(
            "not stopping unrelated development",
            "does not stop unrelated development",
            "unrelated development",
        )

    def test_failures_are_attributed_from_the_last_green_to_red_range(self) -> None:
        self.assert_covers("the attribution range", "change range")
        self.assert_covers("affected tests", "affected tests")
        self.assert_covers("focused replay", "replay")
        self.assert_covers("bisect", "bisect")
        # A failure that reproduces on the last green is not in the range, so
        # attributing it there sends the fix to the wrong change.
        self.assert_covers(
            "excluding failures that are not caused by the range",
            "reproduces on the last green",
            "environmental",
            "flaky",
        )

    def test_acceptance_requires_a_green_on_the_latest_relevant_candidate(self) -> None:
        self.assert_covers(
            "the acceptance precondition",
            "latest relevant candidate",
            "latest candidate",
        )
        self.assert_covers(
            "binding it to acceptance or promotion",
            "before a named checkpoint is accepted",
            "before accepting",
            "named checkpoint is accepted",
        )

    def test_routing_to_change_validation_resolves_and_does_not_duplicate_it(self) -> None:
        # Suite selection stays in change-validation; this skill owns cadence,
        # commit binding, and blast radius.
        self.assertIn("change-validation", self.skill)
        self.assertTrue(
            (self.root / "skills" / "change-validation" / "SKILL.md").is_file()
        )

    def test_role_skills_route_to_the_policy_without_copying_it(self) -> None:
        for relative in (
            "skills/executor/SKILL.md",
            "skills/change-validation/SKILL.md",
            "skills/feedback-loop-design/SKILL.md",
            "skills/review-process/SKILL.md",
            "skills/chatgpt/orchestrator/SKILL.md",
            "skills/chatgpt/auto-review/SKILL.md",
            "skills/copilot/auto-review/SKILL.md",
        ):
            with self.subTest(relative=relative):
                self.assertIn("integration-signal", _normalized(self.root / relative))


if __name__ == "__main__":
    unittest.main()

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

    def assert_covers(self, obligation: str, *accepted: str) -> None:
        self.assertTrue(
            any(phrase in self.skill for phrase in accepted),
            f"integration-signal does not cover {obligation}; "
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
        self.assert_covers(
            "the staleness prohibition",
            "stale green",
            "carry a green forward",
            "carry it forward",
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

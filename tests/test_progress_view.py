"""The D11 measure: what the recorded facts come to, and what they never claim."""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import inspect
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from ai_dev_flow import control_plane, progress_view as view_module
from ai_dev_flow.progress_store import (
    ProgressStore,
    ProgressStoreError,
    REASON_UNANCHORED_NAMED,
)
from ai_dev_flow.progress_view import (
    DELTA_WINDOWS,
    ProgressView,
    ProgressViewError,
    REASON_HISTORY_AFTER_NOW,
    REASON_INSUFFICIENT_HISTORY,
    REASON_INVALID_INSTANT,
    REASON_NO_ACCEPTANCE,
    REASON_NO_PROJECTION,
    REASON_PROJECTION_OVERTAKEN,
    project_progress,
)


_GIT_DATES = ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE")


def epoch(text: str) -> int:
    return int(datetime.fromisoformat(text).timestamp())


class ProgressViewTestCase(unittest.TestCase):
    """One disposable coordination repository, one product repository, no stubs.

    Every progress fact below is published through the supported production
    action -- `control_plane accept` -- into a real coordination repository, and
    then derived back out of that repository's own history by the production
    reader. Nothing here writes a store directly, because a fixture that did
    would be proving something about a seam production does not have.

    `publish_record` is the one deliberate exception and is never used to make an
    acceptance: it commits a crafted record to exercise how the *reader* degrades
    on a history the supported action could not have produced.
    """

    project_name = "ai-dev"
    ticket_name = "issue-55"

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)
        self.repo = self.tmp_path / "coordination"
        self.product = self.tmp_path / "product"
        for root in (self.repo, self.product):
            root.mkdir(parents=True)
            self._run(root, "init", "-q")
            self._run(root, "config", "user.name", "Progress View Tests")
            self._run(root, "config", "user.email", "progress-view-tests@example.com")
        self._run(self.repo, "commit", "-q", "--allow-empty", "-m", "initial")
        self.store = ProgressStore.for_scope(
            self.repo, project=self.project_name, ticket=self.ticket_name
        )
        self.remaining = 0
        self.confidence = "low"
        self.basis = 0

    # -- real repositories -------------------------------------------------

    def _run(self, root: Path, *args: str, when: str = None) -> str:
        environment = dict(os.environ)
        if when is not None:
            environment["GIT_AUTHOR_DATE"] = when
            environment["GIT_COMMITTER_DATE"] = when
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True, text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
        )
        return completed.stdout.strip()

    def _git(self, *args: str, when: str = None) -> str:
        return self._run(self.repo, *args, when=when)

    def at(self, when: str) -> str:
        """One real commit in the coordination repository, at a stated instant."""
        self._git("commit", "-q", "--allow-empty", "-m", "orchestrator: state", when=when)
        return self._git("rev-parse", "HEAD")

    def checkpoint_commit(self, checkpoint: int) -> str:
        """One real published Flow checkpoint in the product repository.

        Publishing is all this does. Nothing about it reaches the measure, which
        is the point: a checkpoint exists here whether or not it is ever accepted.
        """
        self._run(self.product, "commit", "-q", "--allow-empty", "-m", str(checkpoint))
        return self._run(self.product, "rev-parse", "HEAD")

    # -- the supported production action -----------------------------------

    @contextlib.contextmanager
    def _dated(self, when: str):
        previous = {key: os.environ.get(key) for key in _GIT_DATES}
        for key in _GIT_DATES:
            os.environ[key] = when
        try:
            yield
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def publish(self, when: str, **stated) -> dict:
        """Publish one progress record through the supported production action."""
        stated.setdefault("remaining", self.remaining)
        stated.setdefault("confidence", self.confidence)
        with self._dated(when):
            _target, _head, document = control_plane.accept_progress(
                self.repo,
                project=self.project_name,
                ticket=self.ticket_name,
                state="# Control Plane State\n\nProject: ai-dev\n",
                product_repo=self.product,
                **stated
            )
        self.remaining = document["projection"]["remaining"]
        self.confidence = document["projection"]["confidence"]
        if document["accepted"] is not None:
            self.basis = document["accepted"]["checkpoint"]
        return document

    def accept(self, checkpoint: int, when: str, *, remaining: int = None, **stated) -> dict:
        """Accept one numeric checkpoint, the estimate consumed by that progress.

        Omitting `remaining` means the orchestrator reconsidered and preserved the
        projected final: the work just accepted comes out of what remains, so the
        total holds still while the percentage rises.
        """
        if remaining is None:
            remaining = max(0, self.remaining - (checkpoint - self.basis))
        return self.publish(
            when,
            checkpoint=checkpoint,
            commit=self.checkpoint_commit(checkpoint),
            remaining=remaining,
            **stated
        )

    def project(self, remaining: int, when: str, *, confidence="low", note="") -> dict:
        return self.publish(when, remaining=remaining, confidence=confidence, note=note)

    def complete_named(self, checkpoint: int, total: int, when: str) -> dict:
        return self.publish(when, named=checkpoint, named_total=total)

    def publish_to(self, ticket: str, when: str, **stated) -> ProgressStore:
        """One independent published scope, for a case that needs its own history."""
        with self._dated(when):
            control_plane.accept_progress(
                self.repo, project=self.project_name, ticket=ticket,
                state="# Control Plane State\n\nProject: ai-dev\n",
                product_repo=self.product, **stated
            )
        return ProgressStore.for_scope(self.repo, project=self.project_name, ticket=ticket)

    def publish_record(self, when: str, document: dict) -> str:
        """Commit one crafted record directly, to exercise the reader's refusals.

        Never an acceptance. The supported action cannot produce these histories,
        which is exactly why the reader must still say something honest about one.
        """
        target = self.repo / self.project_name / self.ticket_name / "progress.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            document if isinstance(document, str) else json.dumps(document, indent=2) + "\n",
            encoding="utf-8",
        )
        self._git("add", "--", str(target))
        self._git("commit", "-q", "-m", "crafted", when=when)
        return self._git("rev-parse", "HEAD")

    # -- the fixture the rail describes -----------------------------------

    def the_issue_55_baseline(self) -> None:
        """Exactly the state this rail was authorized against, published for real.

        Last accepted numeric checkpoint 52, a projected denominator of about 64,
        confidence low, named checkpoint 7 of 9 active, and a denominator that was
        revised once from 62 and preserved unchanged since.
        """
        self.accept(48, "2026-08-25T10:00:00+00:00", remaining=12, note="initial estimate")
        self.accept(49, "2026-08-28T10:00:00+00:00", remaining=13,
                    note="one more than thought")
        self.accept(50, "2026-08-30T10:00:00+00:00")
        self.complete_named(6, 9, "2026-08-30T12:00:00+00:00")
        self.project(14, "2026-08-31T10:00:00+00:00",
                     note="scope grew: D8 needed its own remediation checkpoint")
        self.accept(51, "2026-09-01T10:00:00+00:00")
        self.accept(52, "2026-09-02T09:00:00+00:00")

    def view(self, when: str = "2026-09-02T12:00:00+00:00") -> ProgressView:
        return project_progress(self.store, now=epoch(when))


# --------------------------------------------------------------------------
# The core measure
# --------------------------------------------------------------------------


class CoreMeasureTests(ProgressViewTestCase):
    def test_the_authorized_baseline_is_represented_exactly(self) -> None:
        self.the_issue_55_baseline()
        view = self.view()
        self.assertTrue(view.available)
        self.assertIsNone(view.reason)
        self.assertTrue(view.source_healthy)
        self.assertEqual(view.accepted_checkpoint, 52)
        self.assertEqual(view.projected_remaining, 12)
        self.assertEqual(view.projected_final, 64)
        self.assertEqual(view.percentage, Decimal("81.25"))
        self.assertEqual(view.confidence, "low")
        self.assertEqual((view.named_checkpoint, view.named_total), (7, 9))
        self.assertEqual((view.revision_from, view.revision_to), (62, 64))
        # Every published record restates the estimate, so "reconsidered and
        # preserved" is counted once per publication that held the total: the
        # three before the revision and the two acceptances after it.
        self.assertEqual(view.preserved_count, 4)

    def test_the_numerator_is_the_last_accepted_numeric_checkpoint(self) -> None:
        self.accept(51, "2026-09-01T10:00:00+00:00")
        self.accept(52, "2026-09-02T09:00:00+00:00")
        self.project(12, "2026-09-02T10:00:00+00:00")
        self.assertEqual(self.view().accepted_checkpoint, 52)

    def test_a_published_but_unaccepted_checkpoint_never_advances_it(self) -> None:
        """The checkpoint exists in the coordination repository. It is not accepted.

        The commit for checkpoint 53 is real and reachable, exactly as a pushed
        flow checkpoint is. Nothing records it as accepted, so the numerator, the
        percentage and the projected final all stay where checkpoint 52 left them
        -- and the projected remaining still counts 53 as work outstanding.
        """
        self.the_issue_55_baseline()
        before = self.view()
        published = self.at("2026-09-02T11:00:00+00:00")
        self.assertEqual(self._git("cat-file", "-t", published), "commit")
        after = self.view()
        self.assertEqual(after.accepted_checkpoint, 52)
        self.assertEqual(after.percentage, before.percentage)
        self.assertEqual(after.projected_final, before.projected_final)
        self.assertEqual(after.projected_remaining, 12)

    def test_accepting_that_checkpoint_is_the_only_thing_that_advances_it(self) -> None:
        self.the_issue_55_baseline()
        self.accept(53, "2026-09-02T11:00:00+00:00")
        view = self.view()
        self.assertEqual(view.accepted_checkpoint, 53)
        self.assertEqual(view.projected_remaining, 11)

    def test_the_percentage_is_the_numerator_over_the_projected_final(self) -> None:
        for accepted, remaining in ((52, 12), (10, 10), (1, 0), (7, 3)):
            with self.subTest(accepted=accepted, remaining=remaining):
                ticket = "case-{0}-{1}".format(accepted, remaining)
                store = self.publish_to(
                    ticket, "2026-09-01T10:00:00+00:00",
                    checkpoint=accepted, commit=self.checkpoint_commit(accepted),
                    remaining=remaining, confidence="low",
                )
                view = project_progress(store, now=epoch("2026-09-02T12:00:00+00:00"))
                self.assertEqual(view.projected_final, accepted + remaining)
                self.assertEqual(
                    view.percentage,
                    (Decimal(accepted) * 100) / Decimal(accepted + remaining),
                )

    def test_confidence_is_exactly_the_recorded_one_of_three(self) -> None:
        for confidence in ("low", "medium", "high"):
            with self.subTest(confidence=confidence):
                store = self.publish_to(
                    "case-{0}".format(confidence), "2026-09-01T10:00:00+00:00",
                    checkpoint=52, commit=self.checkpoint_commit(52),
                    remaining=12, confidence=confidence,
                )
                view = project_progress(store, now=epoch("2026-09-02T12:00:00+00:00"))
                self.assertEqual(view.confidence, confidence)
                self.assertIn(view.confidence, ("low", "medium", "high"))

    def test_the_recorded_timestamps_are_the_ones_git_reports(self) -> None:
        self.the_issue_55_baseline()
        view = self.view()
        facts = self.store.facts()
        self.assertEqual(
            view.accepted_at,
            self._git("log", "-1", "--format=%cI", facts.acceptances[-1].commit),
        )
        self.assertEqual(
            view.named_completed_at,
            self._git("log", "-1", "--format=%cI", facts.named[-1].commit),
        )


# --------------------------------------------------------------------------
# A revision is not progress, and progress is not a revision
# --------------------------------------------------------------------------


class RevisionVersusProgressTests(ProgressViewTestCase):
    def test_progress_consumes_the_estimate_and_leaves_the_total_alone(self) -> None:
        """Two more checkpoints accepted, no new projection: the goalposts hold.

        This is the property that makes the denominator meaningful. If accepting
        work moved the projected final, the estimate would recede exactly as fast
        as progress was made and the percentage could never rise.
        """
        self.the_issue_55_baseline()
        before = self.view()
        self.accept(53, "2026-09-02T11:00:00+00:00")
        self.accept(54, "2026-09-02T11:30:00+00:00")
        after = self.view()
        self.assertEqual(after.projected_final, before.projected_final)
        self.assertEqual(after.projected_remaining, before.projected_remaining - 2)
        self.assertGreater(after.percentage, before.percentage)
        # Nothing was revised, so the recorded revision is still the old one.
        self.assertEqual((after.revision_from, after.revision_to), (62, 64))
        self.assertEqual(after.revision_at, before.revision_at)

    def test_a_revision_moves_the_total_and_says_when_and_why(self) -> None:
        """The percentage drops, and the surface explains that as a bigger total."""
        self.the_issue_55_baseline()
        before = self.view()
        self.project(15, "2026-09-02T11:00:00+00:00",
                     note="checkpoint 8 dogfood needs three more than projected")
        after = self.view()
        self.assertLess(after.percentage, before.percentage)
        self.assertEqual(after.accepted_checkpoint, before.accepted_checkpoint)
        self.assertEqual(after.projected_final, 67)
        self.assertEqual((after.revision_from, after.revision_to), (64, 67))
        self.assertEqual(
            after.revision_note, "checkpoint 8 dogfood needs three more than projected"
        )
        self.assertEqual(
            after.revision_at,
            self.store.facts().projections[-1].recorded_at,
        )
        # And the drop is attributable: no work was lost, the numerator is equal.
        self.assertEqual(after.accepted_checkpoint, 52)
        self.assertEqual(after.preserved_count, 4)

    def test_a_preserved_estimate_is_counted_and_is_not_a_revision(self) -> None:
        self.accept(52, "2026-09-01T10:00:00+00:00", remaining=12, note="initial")
        first = self.view()
        self.assertIsNone(first.revision_at)
        self.assertEqual(first.preserved_count, 0)
        self.project(12, "2026-09-01T12:00:00+00:00", note="reconsidered, unchanged")
        second = self.view()
        self.assertIsNone(second.revision_at)
        self.assertEqual(second.preserved_count, 1)
        self.assertEqual(second.projected_final, first.projected_final)

    def test_the_first_projection_establishes_rather_than_revises(self) -> None:
        self.accept(52, "2026-09-01T10:00:00+00:00", remaining=12,
                    note="first estimate of all")
        view = self.view()
        self.assertEqual(view.projected_final, 64)
        self.assertIsNone(view.revision_at)
        self.assertIsNone(view.revision_from)
        self.assertIsNone(view.revision_to)

    def test_a_revision_recorded_after_progress_compares_totals_not_remainders(self) -> None:
        """The comparison is between projected finals, never between remainders.

        Accepting a checkpoint and then reconsidering to one fewer remaining is
        the *same* total. Comparing the remaining counts alone would call that a
        revision, which is exactly the false alarm this test pins shut.
        """
        self.accept(51, "2026-09-01T10:00:00+00:00", remaining=13, note="initial")
        self.accept(52, "2026-09-02T09:00:00+00:00", remaining=12,
                    note="reconsidered, unchanged")
        view = self.view()
        self.assertEqual(view.projected_final, 64)
        self.assertIsNone(view.revision_at)
        self.assertEqual(view.preserved_count, 1)


# --------------------------------------------------------------------------
# Honest absence
# --------------------------------------------------------------------------


class UnavailableEvidenceTests(ProgressViewTestCase):
    def test_the_supported_action_cannot_leave_an_estimate_overtaken(self) -> None:
        """An acceptance carries its own estimate, so the two cannot disagree.

        Under the published record a projection's basis *is* the accepted
        checkpoint standing in the same record, so accepting more than the
        estimate left room for is not a state the supported action can reach: it
        restates the remainder in the same act. This pins that, and the test
        below keeps the honest answer for a history that reached it some other
        way.
        """
        self.accept(52, "2026-09-01T10:00:00+00:00", remaining=1, note="nearly done")
        self.accept(53, "2026-09-02T08:00:00+00:00")
        self.accept(54, "2026-09-02T09:00:00+00:00", remaining=4, note="more than hoped")
        view = self.view()
        self.assertTrue(view.available)
        self.assertEqual(view.accepted_checkpoint, 54)
        self.assertEqual(view.projected_final, 58)

    def test_an_estimate_reality_overtook_is_stated_and_not_clamped(self) -> None:
        """More accepted than the standing projection left room for.

        The honest answer is that the estimate no longer describes reality. A
        clamp to 100% would announce the ticket finished on the strength of an
        estimate nobody reconsidered. The supported action cannot produce this
        history -- the record below is committed directly -- but the reader must
        still say something true about one it finds.
        """
        self.accept(52, "2026-09-01T10:00:00+00:00", remaining=1, note="nearly done")
        self.publish_record("2026-09-02T09:00:00+00:00", {
            "schemaVersion": 1, "accepted": None, "named": None,
            "projection": {"confidence": "low", "note": "estimate alone", "remaining": 1},
        })
        view = self.view()
        self.assertFalse(view.available)
        self.assertEqual(view.reason, REASON_PROJECTION_OVERTAKEN)
        self.assertIsNone(view.percentage)
        self.assertIsNone(view.projected_final)
        # The accepted facts survive: the estimate went stale, not the progress.
        self.assertEqual(view.accepted_checkpoint, 52)
        self.assertIsNotNone(view.accepted_at)

    def test_a_named_completion_standing_on_nothing_never_renders_a_named_checkpoint(self) -> None:
        """The projection path refuses the record rather than deriving from it.

        This is the shape the surface must never render: one completion of named
        checkpoint 7, no accepted checkpoint anywhere behind it, and a derivation
        that would otherwise report the ticket to be on its eighth named
        checkpoint of nine. The supported action cannot write this record -- it is
        committed directly here -- which is exactly why the reader has to be the
        one that refuses it, and it comes back unavailable and unhealthy rather
        than as a confident named 8.
        """
        self.publish_record("2026-09-02T09:00:00+00:00", {
            "schemaVersion": 1, "accepted": None,
            "named": {"checkpoint": 7, "total": 9},
            "projection": {"confidence": "low", "note": "unanchored", "remaining": 8},
        })
        view = self.view()
        self.assertFalse(view.available)
        self.assertFalse(view.source_healthy)
        self.assertEqual(view.reason, REASON_UNANCHORED_NAMED)
        self.assertIsNone(view.named_checkpoint)
        self.assertIsNone(view.named_total)
        self.assertIsNone(view.accepted_checkpoint)
        self.assertIsNone(view.percentage)

    def test_an_estimate_exactly_consumed_is_still_available_at_one_hundred(self) -> None:
        self.accept(52, "2026-09-01T10:00:00+00:00", remaining=1)
        self.accept(53, "2026-09-02T08:00:00+00:00")
        view = self.view()
        self.assertTrue(view.available)
        self.assertEqual(view.projected_remaining, 0)
        self.assertEqual(view.percentage, Decimal(100))

    def test_a_store_with_no_acceptance_claims_no_percentage(self) -> None:
        self.project(12, "2026-09-01T11:00:00+00:00")
        view = self.view()
        self.assertFalse(view.available)
        self.assertEqual(view.reason, REASON_NO_ACCEPTANCE)
        self.assertIsNone(view.accepted_checkpoint)
        self.assertIsNone(view.percentage)

    def test_an_acceptance_can_never_arrive_without_an_estimate(self) -> None:
        """D11 asks the estimate be reconsidered at every acceptance; it must be.

        `REASON_NO_PROJECTION` is the answer the view still gives for facts that
        carry an acceptance and no estimate. No published record can express
        that: the projection is not optional, so the state is unreachable rather
        than merely unlikely, and this proves the refusal instead of the render.
        """
        with self.assertRaises(control_plane.ControlPlaneError):
            control_plane.accept_progress(
                self.repo, project=self.project_name, ticket=self.ticket_name,
                state="# Control Plane State\n\nProject: ai-dev\n",
                remaining=None, confidence="low",
            )
        self.accept(52, "2026-09-01T10:00:00+00:00", remaining=12)
        view = self.view()
        self.assertTrue(view.available)
        self.assertNotEqual(view.reason, REASON_NO_PROJECTION)
        self.assertEqual(view.confidence, "low")

    def test_an_empty_store_is_unavailable_rather_than_zero_percent(self) -> None:
        view = self.view()
        self.assertFalse(view.available)
        self.assertIsNone(view.percentage)
        self.assertEqual(view.reason, REASON_NO_ACCEPTANCE)
        self.assertTrue(view.source_healthy)

    def test_a_refusing_store_becomes_an_unavailable_view_and_never_raises(self) -> None:
        """A page that crashes and a page showing 0% are both worse than a reason."""
        self.accept(52, "2026-09-01T10:00:00+00:00")
        self.publish_record("2026-09-01T12:00:00+00:00", "{not json")
        with self.assertRaises(ProgressStoreError):
            self.store.facts()
        view = self.view()
        self.assertFalse(view.available)
        self.assertFalse(view.source_healthy)
        self.assertEqual(view.reason, "malformed-progress-store")
        self.assertIsNone(view.percentage)
        self.assertIsNone(view.accepted_checkpoint)

    def test_the_named_checkpoint_is_absent_before_any_completion(self) -> None:
        self.accept(52, "2026-09-01T10:00:00+00:00", remaining=12)
        view = self.view()
        self.assertIsNone(view.named_checkpoint)
        self.assertIsNone(view.named_total)
        self.assertIsNone(view.named_completed_at)

    def test_a_finished_roadmap_names_no_current_named_checkpoint(self) -> None:
        self.accept(52, "2026-09-01T10:00:00+00:00", remaining=0)
        for number in range(1, 10):
            self.complete_named(number, 9, "2026-09-01T10:00:00+00:00")
        view = self.view()
        self.assertIsNone(view.named_checkpoint)
        self.assertEqual(view.named_total, 9)
        self.assertIsNotNone(view.named_completed_at)

    def test_a_malformed_instant_from_the_caller_is_the_callers_fault(self) -> None:
        for bad in (-1, 1.5, "now", None, True):
            with self.assertRaises(ProgressViewError) as caught:
                project_progress(self.store, now=bad)
            self.assertEqual(caught.exception.reason, REASON_INVALID_INSTANT, repr(bad))

    def test_progress_is_projected_from_a_store_and_not_from_a_look_alike(self) -> None:
        class Impostor:
            def facts(self):
                raise AssertionError("never reached")

        with self.assertRaises(ProgressViewError):
            project_progress(Impostor(), now=epoch("2026-09-02T12:00:00+00:00"))


# --------------------------------------------------------------------------
# Deltas
# --------------------------------------------------------------------------


class DeltaTests(ProgressViewTestCase):
    """A window is answered only when the history actually reaches back into it."""

    def test_the_two_windows_are_twenty_four_and_forty_eight_hours(self) -> None:
        self.assertEqual(DELTA_WINDOWS, (24 * 3600, 48 * 3600))

    def test_both_deltas_are_absent_when_the_history_is_younger_than_a_day(self) -> None:
        self.accept(52, "2026-09-02T09:00:00+00:00")
        self.project(12, "2026-09-02T10:00:00+00:00")
        view = self.view("2026-09-02T12:00:00+00:00")
        self.assertIsNone(view.delta_24h)
        self.assertIsNone(view.delta_48h)
        self.assertEqual(view.delta_reason, REASON_INSUFFICIENT_HISTORY)

    def test_the_day_delta_appears_before_the_two_day_delta_does(self) -> None:
        self.accept(51, "2026-09-01T06:00:00+00:00")
        self.accept(52, "2026-09-02T09:00:00+00:00")
        self.project(12, "2026-09-02T10:00:00+00:00")
        view = self.view("2026-09-02T12:00:00+00:00")
        self.assertEqual(view.delta_24h, 1)
        self.assertIsNone(view.delta_48h)
        self.assertEqual(view.delta_reason, REASON_INSUFFICIENT_HISTORY)

    def test_both_deltas_appear_once_the_history_reaches_back_two_days(self) -> None:
        self.the_issue_55_baseline()
        view = self.view("2026-09-02T12:00:00+00:00")
        self.assertEqual(view.delta_24h, 1)
        self.assertEqual(view.delta_48h, 2)
        self.assertIsNone(view.delta_reason)

    def test_a_zero_delta_is_a_real_answer_once_the_history_is_long_enough(self) -> None:
        """Nothing accepted in two days, on a store two weeks old, means zero."""
        self.accept(51, "2026-08-18T10:00:00+00:00")
        self.accept(52, "2026-08-20T10:00:00+00:00")
        self.project(12, "2026-08-20T11:00:00+00:00")
        view = self.view("2026-09-02T12:00:00+00:00")
        self.assertEqual(view.delta_24h, 0)
        self.assertEqual(view.delta_48h, 0)
        self.assertIsNone(view.delta_reason)

    def test_a_history_running_past_this_instant_answers_neither_window(self) -> None:
        self.accept(51, "2026-08-18T10:00:00+00:00")
        self.accept(52, "2026-09-05T10:00:00+00:00")
        self.project(12, "2026-09-05T11:00:00+00:00")
        view = self.view("2026-09-02T12:00:00+00:00")
        self.assertIsNone(view.delta_24h)
        self.assertIsNone(view.delta_48h)
        self.assertEqual(view.delta_reason, REASON_HISTORY_AFTER_NOW)

    def test_the_delta_counts_checkpoints_and_not_the_records_that_accepted_them(
        self,
    ) -> None:
        """One event accepting three checkpoints is +3, because three became accepted.

        This is the difference between counting accepted numeric checkpoints --
        what this surface says it reports -- and counting the records that
        published them. They coincide only while every acceptance advances by
        exactly one, so the fixture deliberately advances by three in a single
        act and would read 1 under the record-counting reading.
        """
        self.accept(30, "2026-08-30T09:00:00+00:00", remaining=12)
        for number in (31, 32):
            self.checkpoint_commit(number)
        top = self.checkpoint_commit(33)
        self.publish("2026-09-02T09:00:00+00:00", checkpoint=33, commit=top, remaining=9)
        facts = self.store.facts()
        self.assertEqual(
            [entry.checkpoint for entry in facts.acceptances], [30, 31, 32, 33]
        )
        view = self.view("2026-09-02T12:00:00+00:00")
        self.assertEqual(view.delta_24h, 3)
        self.assertEqual(view.delta_48h, 3)
        self.assertIsNone(view.delta_reason)
        self.assertEqual(view.accepted_checkpoint, 33)

    def test_the_delta_counts_accepted_checkpoints_and_not_percentage_points(self) -> None:
        """A percentage-point delta would move when the estimate was revised."""
        self.the_issue_55_baseline()
        before = self.view("2026-09-02T12:00:00+00:00")
        self.project(30, "2026-09-02T11:00:00+00:00", note="a large revision")
        after = self.view("2026-09-02T12:00:00+00:00")
        self.assertLess(after.percentage, before.percentage)
        self.assertEqual(after.delta_24h, before.delta_24h)
        self.assertEqual(after.delta_48h, before.delta_48h)

    def test_an_offset_instant_is_counted_at_the_moment_it_actually_names(self) -> None:
        """The offset is honoured, so a window boundary lands where it should.

        Checkpoint 52 was accepted at 13:30 in a +02:00 zone, which is 11:30 UTC
        and thirty minutes *before* the 24-hour boundary. A reader that took the
        wall-clock digits and ignored the offset would count it as inside the
        window, so this fixture is chosen to come out differently under the two
        readings rather than merely to pass under the right one.
        """
        self.accept(50, "2026-08-29T10:00:00+00:00")
        self.accept(51, "2026-09-01T11:00:00+00:00")
        self.accept(52, "2026-09-01T13:30:00+02:00")
        self.project(12, "2026-09-02T10:00:00+00:00")
        facts = self.store.facts()
        self.assertTrue(facts.acceptances[1].accepted_at.endswith("Z"))
        self.assertTrue(facts.acceptances[2].accepted_at.endswith("+02:00"))
        view = self.view("2026-09-02T12:00:00+00:00")
        self.assertEqual(view.delta_24h, 0)
        self.assertEqual(view.delta_48h, 2)


# --------------------------------------------------------------------------
# The view is the end of the line
# --------------------------------------------------------------------------


class ObservabilityShapeTests(ProgressViewTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = Path(view_module.__file__).read_text(encoding="utf-8")

    def test_the_view_is_frozen_and_carries_only_things_to_draw(self) -> None:
        self.the_issue_55_baseline()
        view = self.view()
        self.assertTrue(dataclasses.fields(ProgressView))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            view.percentage = Decimal(100)
        methods = [
            name
            for name, value in inspect.getmembers(ProgressView, inspect.isfunction)
            if not name.startswith("__")
        ]
        self.assertEqual(methods, [])

    def test_it_carries_no_management_signal_d11_reserves_for_the_human(self) -> None:
        """Elapsed time, handoff count, sessions, tokens and velocity are absent.

        D11 names them as management signals for the human alone. A field here is
        a field something could be built on, so there is none.
        """
        names = {field.name for field in dataclasses.fields(ProgressView)}
        for forbidden in (
            "elapsed", "duration", "handoffs", "handoff_count", "sessions",
            "tokens", "velocity", "wall_clock", "published", "rails",
        ):
            self.assertNotIn(forbidden, names, forbidden)
            for name in names:
                self.assertNotIn(forbidden, name, "{0} in {1}".format(forbidden, name))

    def test_the_module_reaches_no_authority_and_no_clock(self) -> None:
        imported = set()
        for node in ast.walk(ast.parse(self.source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add("." * (node.level or 0) + (node.module or ""))
        self.assertEqual(
            imported,
            {"__future__", "dataclasses", "datetime", "decimal", "typing",
             ".progress_store"},
        )
        for forbidden in ("time.time", "datetime.now", "utcnow", "subprocess"):
            self.assertNotIn(forbidden, self.source, forbidden)
        # The control vocabulary, checked against named surfaces rather than
        # against prose, so the module may say in a comment what it refuses to be.
        names = set()
        for node in ast.walk(ast.parse(self.source)):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.arg):
                names.add(node.arg)
            elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.keyword) and node.arg:
                names.add(node.arg)
        lowered = {name.lower().replace("_", "") for name in names}
        # "acceptance" is deliberately not on this list: the numerator *is* an
        # acceptance fact, and naming the fact is the opposite of performing the
        # act. What must not appear is a verb this module could carry out.
        for forbidden in (
            "authorize", "reconcile", "admit", "trigger", "schedule",
            "priority", "remediat", "review", "dispatch", "launch",
        ):
            for name in lowered:
                self.assertNotIn(forbidden, name, "{0} in {1}".format(forbidden, name))

    def test_no_function_here_returns_or_takes_a_decision(self) -> None:
        """Every public entry point takes a store and an instant, and returns a view."""
        signature = inspect.signature(project_progress)
        self.assertEqual(list(signature.parameters), ["store", "now"])
        self.the_issue_55_baseline()
        self.assertIsInstance(self.view(), ProgressView)


if __name__ == "__main__":
    unittest.main()

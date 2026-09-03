"""The durable half of D11: what may be published, and what may never get in."""

from __future__ import annotations

import ast
import contextlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from ai_dev_flow import control_plane, progress_record, progress_store as store_module
from ai_dev_flow.control_plane import ControlPlaneError, accept_progress
from ai_dev_flow.progress_record import (
    CONFIDENCES,
    DOCUMENT_KEYS,
    MAX_PROJECTION_NOTE,
    PROGRESS_FILENAME,
    progress_relative,
)
from ai_dev_flow.progress_store import (
    ProgressStore,
    ProgressStoreError,
    REASON_CHECKPOINT_REGRESSED,
    REASON_INVALID_CHECKPOINT,
    REASON_INVALID_COMMIT,
    REASON_INVALID_CONFIDENCE,
    REASON_INVALID_NAMED_TOTAL,
    REASON_INVALID_NOTE,
    REASON_INVALID_REMAINING,
    REASON_MALFORMED_STORE,
    REASON_NAMED_OUT_OF_ORDER,
    REASON_TIMESTAMP_UNAVAILABLE,
    REASON_UNREADABLE_STORE,
    SCHEMA_VERSION,
    commit_instant,
)

PROJECT = "ai-dev"
TICKET = "issue-55"
_GIT_DATES = ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE")


class ProgressStoreTestCase(unittest.TestCase):
    """A real coordination repository, a real product repository, no seams of my own.

    Every fact these tests read was published through the supported production
    action into a real coordination repository. `commit_record` is the one way a
    document reaches the repository without that action, and it exists only to
    ask what the *reader* does with a history the action would have refused.
    """

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)
        self.repo = self.tmp_path / "coordination"
        self.product = self.tmp_path / "product"
        for root in (self.repo, self.product):
            root.mkdir(parents=True)
            self._run(root, "init", "-q")
            self._run(root, "config", "user.name", "Progress Store Tests")
            self._run(root, "config", "user.email", "progress-store-tests@example.com")
        self._run(self.repo, "commit", "-q", "--allow-empty", "-m", "initial")
        self.store = ProgressStore.for_scope(self.repo, project=PROJECT, ticket=TICKET)
        self.relative = progress_relative(PROJECT, TICKET)
        self.path = self.repo / self.relative

    # -- real repositories -------------------------------------------------

    def _run(self, root: Path, *args: str, when: str = None) -> str:
        environment = dict(os.environ)
        if when is not None:
            for key in _GIT_DATES:
                environment[key] = when
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True, text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
        )
        return completed.stdout.strip()

    def _git(self, *args: str, when: str = None) -> str:
        return self._run(self.repo, *args, when=when)

    def a_commit(self, when: str = None) -> str:
        self._git("commit", "-q", "--allow-empty", "-m", "orchestrator: state", when=when)
        return self._git("rev-parse", "HEAD")

    def checkpoint_commit(self, checkpoint) -> str:
        self._run(self.product, "commit", "-q", "--allow-empty", "-m", str(checkpoint))
        return self._run(self.product, "rev-parse", "HEAD")

    @contextlib.contextmanager
    def dated(self, when: str):
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

    # -- the supported production action -----------------------------------

    def publish(self, when: str = "2026-09-02T09:00:00+00:00", **stated) -> dict:
        stated.setdefault("remaining", 0)
        stated.setdefault("confidence", "low")
        with self.dated(when):
            _target, _head, document = accept_progress(
                self.repo, project=PROJECT, ticket=TICKET,
                state="# Control Plane State\n\nProject: ai-dev\n",
                product_repo=self.product, **stated
            )
        return document

    def accept(self, checkpoint, when: str = "2026-09-02T09:00:00+00:00", **stated) -> dict:
        stated.setdefault("commit", self.checkpoint_commit(checkpoint))
        return self.publish(when, checkpoint=checkpoint, **stated)

    def crafted_scope(self, ticket: str, document, when: str = None) -> ProgressStore:
        """One crafted record in its own scope, so histories stay independent."""
        target = self.repo / PROJECT / ticket / PROGRESS_FILENAME
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            document if isinstance(document, str) else json.dumps(document, indent=2) + "\n",
            encoding="utf-8",
        )
        self._git("add", "--", str(target))
        self._git("commit", "-q", "-m", "crafted", when=when)
        return ProgressStore.for_scope(self.repo, project=PROJECT, ticket=ticket)

    def commit_record(self, document, when: str = None) -> str:
        """One record committed directly. Never a substitute for the action."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            document if isinstance(document, str) else json.dumps(document, indent=2) + "\n",
            encoding="utf-8",
        )
        self._git("add", "--", str(self.path))
        self._git("commit", "-q", "-m", "crafted", when=when)
        return self._git("rev-parse", "HEAD")

    def published(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Every instant is Git's, and there is nowhere to state one
# --------------------------------------------------------------------------


class DeterministicInstantTests(ProgressStoreTestCase):
    """No instant here is ever stated, because there is no field to state it in."""

    def test_the_derived_instant_is_exactly_what_git_log_reports(self) -> None:
        self.accept(52, "2026-09-02T09:00:00+00:00")
        acceptance = self.store.facts().acceptances[-1]
        self.assertEqual(
            acceptance.accepted_at,
            self._git("log", "-1", "--format=%cI", acceptance.commit),
        )

    def test_the_published_record_has_no_instant_field_at_all(self) -> None:
        """A caller that wants to state a time has nowhere to put it."""
        self.accept(52)
        self.publish(named=6, named_total=9)
        document = self.published()
        for section in (document, document["accepted"], document["named"], document["projection"]):
            for key in section:
                lowered = key.lower()
                self.assertNotIn("at", (lowered[-2:],), key)
                self.assertNotIn("time", lowered, key)
                self.assertNotIn("date", lowered, key)
                self.assertNotIn("instant", lowered, key)
        self.assertEqual(sorted(document), sorted(DOCUMENT_KEYS))

    def test_no_published_action_accepts_an_instant_at_all(self) -> None:
        import inspect

        for parameter in inspect.signature(accept_progress).parameters:
            lowered = parameter.lower()
            for forbidden in ("instant", "at", "time", "date", "when", "now"):
                self.assertNotEqual(lowered, forbidden, parameter)

    def test_the_module_derives_no_instant_of_its_own(self) -> None:
        source = Path(store_module.__file__).read_text(encoding="utf-8")
        for forbidden in ("import time", "datetime", "utcnow", "time.time"):
            self.assertNotIn(forbidden, source, forbidden)

    def test_the_instant_convention_is_the_accepted_one_and_the_reads_are_read_only(
        self,
    ) -> None:
        """One instant convention, and every other git call is a read.

        The instant comes from `git log -1 --format=%cI <full object name> --`,
        literally, and it is the only place `%cI` appears. The other two calls
        enumerate the commits that changed one path and read those versions'
        content; nothing here writes, moves a ref, or touches an index.
        """
        source = Path(store_module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        invocations = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            if isinstance(target, ast.Attribute) and target.attr == "run":
                arguments = node.args[0] if node.args else None
                if isinstance(arguments, (ast.List, ast.Tuple)):
                    invocations.append(
                        [
                            element.value if isinstance(element, ast.Constant) else "<expr>"
                            for element in arguments.elts
                        ]
                    )
        self.assertEqual(len(invocations), 4, invocations)
        verbs = set()
        for invocation in invocations:
            self.assertEqual(invocation[0], "git")
            self.assertEqual(invocation[1], "-C")
            verbs.add(invocation[3])
        # Two calls name their verb outright; two forward the caller's arguments,
        # and every verb any caller supplies is read-only.
        self.assertEqual(verbs, {"log", "cat-file", "<expr>"})
        forwarded = {
            element.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("_run", "_succeeds")
            for argument in node.args
            if isinstance(argument, (ast.List, ast.Tuple))
            for element in argument.elts
            if isinstance(element, ast.Constant)
        }
        self.assertTrue(forwarded <= {"log", "--reverse", "--format=%H", "--", "rev-parse",
                                      "--verify", "--quiet", "HEAD", "--git-dir"}, forwarded)
        stamping = [one for one in invocations if "--format=%cI" in one]
        self.assertEqual(
            stamping,
            [["git", "-C", "<expr>", "log", "-1", "--format=%cI", "<expr>", "--"]],
        )
        self.assertIn('"log", "-1", "--format=%cI", checked, "--"', source)
        for forbidden in ("commit", "add", "push", "checkout", "reset", "update-ref"):
            self.assertNotIn('"{0}"'.format(forbidden), source, forbidden)

    def test_only_a_full_object_name_is_a_source(self) -> None:
        self.a_commit()
        for name in ("HEAD", "main", self._git("rev-parse", "--short", "HEAD"), "A" * 40):
            with self.subTest(name=name):
                with self.assertRaises(ProgressStoreError) as raised:
                    commit_instant(self.repo, name)
                self.assertEqual(raised.exception.reason, REASON_INVALID_COMMIT)

    def test_a_commit_the_repository_does_not_have_is_refused(self) -> None:
        with self.assertRaises(ProgressStoreError) as raised:
            commit_instant(self.repo, "0" * 40)
        self.assertEqual(raised.exception.reason, REASON_TIMESTAMP_UNAVAILABLE)

    def test_a_repository_that_is_not_there_is_refused(self) -> None:
        with self.assertRaises(ProgressStoreError) as raised:
            commit_instant(self.tmp_path / "absent", self.a_commit())
        self.assertEqual(raised.exception.reason, REASON_TIMESTAMP_UNAVAILABLE)

    def test_both_forms_git_actually_emits_are_accepted(self) -> None:
        for when in ("2026-09-02T09:00:00+00:00", "2026-09-02T09:00:00+02:00"):
            with self.subTest(when=when):
                instant = commit_instant(self.repo, self.a_commit(when))
                self.assertRegex(instant, r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(Z|[+-]\d{2}:\d{2})\Z")


# --------------------------------------------------------------------------
# Acceptance: the one way the numerator moves
# --------------------------------------------------------------------------


class AcceptanceTests(ProgressStoreTestCase):
    def test_accepted_checkpoints_are_derived_strictly_increasing(self) -> None:
        for number in (48, 49, 50):
            self.accept(number)
        self.assertEqual(
            [entry.checkpoint for entry in self.store.facts().acceptances], [48, 49, 50]
        )

    def test_a_repeated_or_regressed_checkpoint_is_refused_by_the_action(self) -> None:
        self.accept(50)
        for number in (50, 49):
            with self.subTest(checkpoint=number):
                with self.assertRaises(ControlPlaneError) as raised:
                    self.accept(number)
                self.assertIn("does not follow", str(raised.exception))

    def test_a_refused_acceptance_leaves_the_published_record_untouched(self) -> None:
        self.accept(50)
        before = self.path.read_bytes()
        head = self._git("rev-parse", "HEAD")
        for stated in (
            {"checkpoint": 49, "commit": self.checkpoint_commit(49)},
            {"checkpoint": 51, "commit": self.checkpoint_commit("fifty-one")},
            {"checkpoint": 51, "commit": self.checkpoint_commit(51), "confidence": "urgent"},
            {"checkpoint": 51, "commit": self.checkpoint_commit(51), "remaining": -1},
        ):
            with self.subTest(stated=sorted(stated)):
                with self.assertRaises(ControlPlaneError):
                    self.publish(**stated)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(self._git("rev-parse", "HEAD"), head)

    def test_a_checkpoint_that_is_not_a_whole_number_is_refused(self) -> None:
        for number in (0, -1, True):
            with self.subTest(checkpoint=number):
                with self.assertRaises(ControlPlaneError):
                    self.publish(checkpoint=number, commit=self.checkpoint_commit(number))

    def test_the_accepted_number_is_read_from_product_history_not_stated(self) -> None:
        """The commit must actually be that checkpoint, or the acceptance refuses."""
        fifty_three = self.checkpoint_commit(53)
        with self.assertRaises(ControlPlaneError) as raised:
            self.publish(checkpoint=54, commit=fifty_three)
        self.assertIn("Flow subject '53', not '54'", str(raised.exception))

    def test_there_is_no_published_shape_for_an_unaccepted_checkpoint(self) -> None:
        """Publishing a checkpoint is not accepting one, and cannot become it.

        The record's whole vocabulary is one accepted checkpoint, one completed
        named checkpoint and one projection. There is no pending, proposed or
        published kind, so a checkpoint that exists on the product remote and was
        never accepted has no field to occupy.
        """
        self.accept(52)
        published = self.checkpoint_commit(53)
        self.assertEqual(self._run(self.product, "cat-file", "-t", published), "commit")
        facts = self.store.facts()
        self.assertEqual([entry.checkpoint for entry in facts.acceptances], [52])
        self.assertEqual(sorted(self.published()), sorted(DOCUMENT_KEYS))
        self.assertEqual(
            sorted(self.published()["accepted"]), ["checkpoint", "commit"]
        )

    def test_an_added_field_makes_the_record_unreadable_rather_than_ignored(self) -> None:
        self.accept(52)
        document = self.published()
        document["acceptedBy"] = "someone"
        self.commit_record(document)
        with self.assertRaises(ProgressStoreError) as raised:
            self.store.facts()
        self.assertEqual(raised.exception.reason, REASON_MALFORMED_STORE)


class GroupedAcceptanceTests(ProgressStoreTestCase):
    """One acceptance event may accept several checkpoints, and keeps them all.

    The record is a compact current-state artifact, so what these prove is that
    the *derivation* from consecutive published versions loses nothing: every
    checkpoint an event made accepted comes back out of the history it left
    behind, and no checkpoint the product never carried is invented by it.

    Nothing here is written by hand. Every acceptance below goes through the
    supported production action against a real product repository, and the
    numbers are generic -- a baseline and a run above it -- rather than this
    ticket's own.
    """

    BASE = 30

    def a_run_above_the_baseline(self, length: int) -> str:
        """Real contiguous product checkpoints BASE+1 .. BASE+length, published.

        Publishing them is all this does. None of them is accepted by existing;
        the acceptance is a separate act, which is the whole point of the
        distinction being tested.
        """
        commit = ""
        for number in range(self.BASE + 1, self.BASE + length + 1):
            commit = self.checkpoint_commit(number)
        return commit

    def test_one_acceptance_of_a_run_keeps_every_checkpoint_it_accepts(self) -> None:
        """A single event advancing N to N+3 leaves all three in retained history."""
        self.accept(self.BASE, "2026-08-30T09:00:00+00:00")
        top = self.a_run_above_the_baseline(3)
        self.publish("2026-09-02T09:00:00+00:00", checkpoint=self.BASE + 3, commit=top)
        self.assertEqual(
            [entry.checkpoint for entry in self.store.facts().acceptances],
            [self.BASE, self.BASE + 1, self.BASE + 2, self.BASE + 3],
        )

    def test_every_checkpoint_one_event_accepted_is_sourced_to_that_event(self) -> None:
        """They share the acceptance commit and its instant, because one act made them accepted."""
        self.accept(self.BASE, "2026-08-30T09:00:00+00:00")
        top = self.a_run_above_the_baseline(3)
        self.publish("2026-09-02T09:00:00+00:00", checkpoint=self.BASE + 3, commit=top)
        grouped = self.store.facts().acceptances[1:]
        acceptance_commit = self._git("rev-parse", "HEAD")
        self.assertEqual(
            self._git("log", "-1", "--format=%s", acceptance_commit),
            "orchestrator: accept ({0}/{1})".format(PROJECT, TICKET),
        )
        for entry in grouped:
            self.assertEqual(entry.commit, acceptance_commit)
            self.assertEqual(
                entry.accepted_at, self._git("log", "-1", "--format=%cI", acceptance_commit)
            )
        self.assertNotEqual(grouped[0].accepted_at, self.store.facts().acceptances[0].accepted_at)

    def test_a_run_the_product_never_carried_is_refused_rather_than_filled_in(self) -> None:
        """The missing checkpoint is not manufactured; the acceptance is refused."""
        self.accept(self.BASE, "2026-08-30T09:00:00+00:00")
        self.checkpoint_commit(self.BASE + 1)
        top = self.checkpoint_commit(self.BASE + 3)  # BASE+2 was never published
        before = self.published()
        with self.assertRaises(ControlPlaneError) as raised:
            self.publish("2026-09-02T09:00:00+00:00", checkpoint=self.BASE + 3, commit=top)
        self.assertIn("read from product history", str(raised.exception))
        self.assertEqual(self.published(), before)
        self.assertEqual(
            [entry.checkpoint for entry in self.store.facts().acceptances], [self.BASE]
        )

    def test_a_checkpoint_off_the_accepted_lineage_is_refused(self) -> None:
        """A run must descend from what is already accepted, not merely be numbered above it."""
        self.accept(self.BASE, "2026-08-30T09:00:00+00:00")
        self._run(self.product, "checkout", "-q", "--orphan", "elsewhere")
        divergent = self.checkpoint_commit(self.BASE + 1)
        with self.assertRaises(ControlPlaneError) as raised:
            self.publish("2026-09-02T09:00:00+00:00", checkpoint=self.BASE + 1, commit=divergent)
        self.assertIn("is not an ancestor of", str(raised.exception))
        self.assertEqual(
            [entry.checkpoint for entry in self.store.facts().acceptances], [self.BASE]
        )

    def test_the_first_acceptance_claims_no_checkpoint_before_itself(self) -> None:
        """Adopting the record part-way through a ticket does not backfill 1 .. N."""
        self.accept(self.BASE, "2026-08-30T09:00:00+00:00")
        self.assertEqual(
            [entry.checkpoint for entry in self.store.facts().acceptances], [self.BASE]
        )


# --------------------------------------------------------------------------
# Projection: measured against evidence, never against a claim
# --------------------------------------------------------------------------


class ProjectionTests(ProgressStoreTestCase):
    def test_the_basis_is_the_accepted_checkpoint_in_the_same_record(self) -> None:
        """It is not a parameter, and it is not read from anywhere else.

        The basis of a projection is the acceptance standing in the very record
        that carries it, so an estimate cannot be measured against a checkpoint
        the same publication did not state.
        """
        import inspect

        self.assertNotIn("basis", inspect.signature(accept_progress).parameters)
        self.accept(48, remaining=12)
        for number in (49, 50, 51):
            self.checkpoint_commit(number)
        self.accept(52, remaining=10)
        projections = self.store.facts().projections
        self.assertEqual([entry.basis for entry in projections], [48, 52])
        self.assertEqual([entry.remaining for entry in projections], [12, 10])

    def test_a_projection_before_any_acceptance_has_a_basis_of_zero(self) -> None:
        self.publish(remaining=12, note="before anything is accepted")
        projection = self.store.facts().projections[-1]
        self.assertEqual(projection.basis, 0)
        self.assertEqual(projection.remaining, 12)

    def test_confidence_is_exactly_low_medium_or_high(self) -> None:
        self.assertEqual(CONFIDENCES, ("low", "medium", "high"))
        for confidence in CONFIDENCES:
            with self.subTest(confidence=confidence):
                self.publish(remaining=1, confidence=confidence)
        for refused in ("LOW", "Low", "medium ", "", None, True, 1, 0.5, "urgent"):
            with self.subTest(confidence=refused):
                with self.assertRaises(ControlPlaneError):
                    self.publish(remaining=1, confidence=refused)

    def test_a_remaining_count_is_a_whole_count_of_at_least_zero(self) -> None:
        self.publish(remaining=0)
        for refused in (-1, 1.5, "12", None, True):
            with self.subTest(remaining=refused):
                with self.assertRaises(ControlPlaneError):
                    self.publish(remaining=refused)

    def test_a_preserved_estimate_is_published_rather_than_skipped(self) -> None:
        """Reconsidering and preserving is a fact, and it is recorded as one."""
        self.publish("2026-09-01T10:00:00+00:00", remaining=12, note="first")
        self.publish("2026-09-01T11:00:00+00:00", remaining=12, note="reconsidered")
        projections = self.store.facts().projections
        self.assertEqual(len(projections), 2)
        self.assertEqual([entry.remaining for entry in projections], [12, 12])
        self.assertNotEqual(projections[0].recorded_at, projections[1].recorded_at)

    def test_every_published_record_carries_a_reconsidered_estimate(self) -> None:
        """There is no publication that failed to reconsider; it is not expressible."""
        self.accept(52)
        self.publish(named=6, named_total=9)
        facts = self.store.facts()
        self.assertEqual(len(facts.projections), 2)
        with self.assertRaises(ControlPlaneError):
            self.publish(remaining=None)


class ProjectionNoteTests(ProgressStoreTestCase):
    def test_one_bounded_line_is_accepted(self) -> None:
        note = "scope grew: D8 needed its own remediation checkpoint"
        self.publish(remaining=12, note=note)
        self.assertEqual(self.store.facts().projections[-1].note, note)
        self.publish(remaining=12, note="x" * MAX_PROJECTION_NOTE)
        self.assertEqual(len(self.store.facts().projections[-1].note), MAX_PROJECTION_NOTE)

    def test_a_multi_line_or_oversized_note_is_refused(self) -> None:
        for note in ("first\nsecond", "tabbed\there", " padded ", "x" * (MAX_PROJECTION_NOTE + 1)):
            with self.subTest(note=note[:20]):
                with self.assertRaises(ControlPlaneError):
                    self.publish(remaining=12, note=note)

    def test_a_note_that_is_not_text_is_refused(self) -> None:
        for note in (12, None, ["why"], {"why": "because"}):
            with self.subTest(note=note):
                with self.assertRaises(ControlPlaneError):
                    self.publish(remaining=12, note=note)


# --------------------------------------------------------------------------
# Named completion
# --------------------------------------------------------------------------


class NamedCompletionTests(ProgressStoreTestCase):
    def test_completions_are_the_contiguous_prefix_of_the_roadmap(self) -> None:
        for number in (1, 2, 3):
            self.publish(named=number, named_total=9)
        self.assertEqual(
            [entry.checkpoint for entry in self.store.facts().named], [1, 2, 3]
        )

    def test_a_repeated_or_regressed_completion_is_refused(self) -> None:
        self.publish(named=6, named_total=9)
        for number in (6, 5):
            with self.subTest(named=number):
                with self.assertRaises(ControlPlaneError) as raised:
                    self.publish(named=number, named_total=9)
                self.assertIn("does not follow", str(raised.exception))

    def test_a_gap_in_the_prefix_is_refused(self) -> None:
        """1 -> 3 would assert that named checkpoint 2 completed with nothing recording it.

        Completing 1 and then 3 is the exact shape that stopped being refused, so
        this asks the supported action for it directly and then reads the history
        back: the refusal must leave the recorded completions untouched, and the
        contiguous step that follows must still be ordinary.
        """
        self.publish(named=1, named_total=9)
        before = self.published()
        with self.assertRaises(ControlPlaneError) as raised:
            self.publish(named=3, named_total=9)
        self.assertIn("does not follow", str(raised.exception))
        self.assertEqual(self.published(), before)
        self.assertEqual([entry.checkpoint for entry in self.store.facts().named], [1])
        self.publish(named=2, named_total=9)
        self.assertEqual([entry.checkpoint for entry in self.store.facts().named], [1, 2])

    def test_a_gap_in_the_prefix_in_the_history_is_refused_on_read(self) -> None:
        """A record no supported action could have written is a refusal, not a repair."""
        self.publish(named=1, named_total=9)
        document = self.published()
        document["named"] = {"checkpoint": 3, "total": 9}
        self.commit_record(document)
        with self.assertRaises(ProgressStoreError) as raised:
            self.store.facts()
        self.assertEqual(raised.exception.reason, REASON_NAMED_OUT_OF_ORDER)

    def test_a_regressed_completion_in_the_history_is_refused_on_read(self) -> None:
        self.publish(named=6, named_total=9)
        document = self.published()
        document["named"] = {"checkpoint": 3, "total": 9}
        self.commit_record(document)
        with self.assertRaises(ProgressStoreError) as raised:
            self.store.facts()
        self.assertEqual(raised.exception.reason, REASON_NAMED_OUT_OF_ORDER)

    def test_a_completion_beyond_the_roadmap_is_refused(self) -> None:
        with self.assertRaises(ControlPlaneError):
            self.publish(named=10, named_total=9)
        self.publish(named=9, named_total=9)
        self.assertEqual(self.store.facts().named[-1].total, 9)

    def test_the_roadmap_size_is_restated_with_each_completion(self) -> None:
        """A roadmap may honestly grow, and each completion says the size then."""
        self.publish(named=6, named_total=9)
        self.publish(named=7, named_total=11)
        self.assertEqual(
            [(entry.checkpoint, entry.total) for entry in self.store.facts().named],
            [(6, 9), (7, 11)],
        )

    def test_a_completion_needs_the_roadmap_size_it_was_reached_on(self) -> None:
        with self.assertRaises(ControlPlaneError):
            self.publish(named=6)
        with self.assertRaises(ControlPlaneError):
            self.publish(named_total=9)


# --------------------------------------------------------------------------
# Acceptance and projection facts only. There is no room for anything else.
# --------------------------------------------------------------------------


class RetainedHistoryTests(ProgressStoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = Path(store_module.__file__).read_text(encoding="utf-8")
        self.record_source = Path(progress_record.__file__).read_text(encoding="utf-8")

    def test_the_published_document_has_exactly_three_kinds_of_fact(self) -> None:
        self.accept(52)
        self.publish(named=1, named_total=9, remaining=12, note="why")
        document = self.published()
        self.assertEqual(document["schemaVersion"], SCHEMA_VERSION)
        self.assertEqual(sorted(document), ["accepted", "named", "projection", "schemaVersion"])
        self.assertEqual(sorted(document["accepted"]), ["checkpoint", "commit"])
        self.assertEqual(sorted(document["named"]), ["checkpoint", "total"])
        self.assertEqual(sorted(document["projection"]), ["confidence", "note", "remaining"])

    def test_the_history_is_the_repository_and_not_a_second_log(self) -> None:
        """Retained history is the commits, so there is no log to grow unbounded."""
        for number in (48, 49, 50):
            self.accept(number)
        document = self.published()
        self.assertEqual(document["accepted"]["checkpoint"], 50)
        self.assertNotIn("acceptances", document)
        self.assertEqual(len(self.store.facts().acceptances), 3)

    def test_no_diary_transcript_log_or_analytics_surface_exists(self) -> None:
        """Named surfaces, not prose, read from the AST of both modules."""
        names = set()
        for source in (self.source, self.record_source):
            for node in ast.walk(ast.parse(source)):
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
        names.update(progress_record.DOCUMENT_KEYS)
        names.update(progress_record.ACCEPTED_KEYS)
        names.update(progress_record.NAMED_KEYS)
        names.update(progress_record.PROJECTION_KEYS)
        lowered = {name.lower().replace("_", "") for name in names}
        for forbidden in (
            "transcript", "diary", "logging", "analytics", "telemetryevent",
            "sessionid", "pid", "token", "duration", "elapsed",
            "velocity", "handoff", "wallclock", "appendevent",
        ):
            self.assertNotIn(forbidden, lowered, forbidden)
            for name in lowered:
                self.assertNotIn(forbidden, name, "{0} in {1}".format(forbidden, name))

    def test_the_modules_reach_no_product_authority(self) -> None:
        """They hold facts. They cannot ask anything, and nothing here decides."""
        imported = set()
        for node in ast.walk(ast.parse(self.source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add("." * (node.level or 0) + (node.module or ""))
        self.assertEqual(
            imported,
            {"__future__", "json", "re", "subprocess", "pathlib", "typing", ".progress_record"},
        )
        imported = set()
        for node in ast.walk(ast.parse(self.record_source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add("." * (node.level or 0) + (node.module or ""))
        self.assertEqual(imported, {"__future__", "re", "typing"})

    def test_the_record_lives_in_the_coordination_scope_it_is_about(self) -> None:
        """Not in the product worktree: acceptance is not a product-repository act."""
        self.assertEqual(PROGRESS_FILENAME, "progress.json")
        self.assertEqual(progress_relative(PROJECT, TICKET), "ai-dev/issue-55/progress.json")
        self.assertEqual(
            control_plane.artifact_relative(
                project=PROJECT, ticket=TICKET, artifact="progress", rail=None
            ),
            progress_relative(PROJECT, TICKET),
        )
        self.assertNotIn(".ai-dev", progress_relative(PROJECT, TICKET))

    def test_naming_a_store_opens_nothing_and_an_unpublished_scope_is_empty(self) -> None:
        absent = ProgressStore.for_scope(self.repo, project=PROJECT, ticket="issue-99")
        facts = absent.facts()
        self.assertEqual((facts.acceptances, facts.named, facts.projections), ((), (), ()))
        self.assertFalse((self.repo / PROJECT / "issue-99").exists())

    def test_the_store_cannot_write_at_all(self) -> None:
        """There is no recording method to call instead of the supported action."""
        for forbidden in ("record_acceptance", "record_projection", "record_named_completion"):
            self.assertFalse(hasattr(self.store, forbidden), forbidden)
        for forbidden in ("write_text", "write_bytes", "open(", "mkdir", "json.dump"):
            self.assertNotIn(forbidden, self.source, forbidden)


# --------------------------------------------------------------------------
# A history that cannot be read is said so, never repaired
# --------------------------------------------------------------------------


class MalformedHistoryTests(ProgressStoreTestCase):
    def test_a_record_that_is_not_json_is_refused(self) -> None:
        self.commit_record("{not json")
        with self.assertRaises(ProgressStoreError) as raised:
            self.store.facts()
        self.assertEqual(raised.exception.reason, REASON_MALFORMED_STORE)

    def test_a_record_of_an_unsupported_version_is_refused(self) -> None:
        self.accept(52)
        document = self.published()
        document["schemaVersion"] = 2
        self.commit_record(document)
        with self.assertRaises(ProgressStoreError) as raised:
            self.store.facts()
        self.assertEqual(raised.exception.reason, REASON_MALFORMED_STORE)

    def test_a_record_missing_a_section_is_refused(self) -> None:
        self.accept(52)
        sound = self.published()
        for missing in ("accepted", "named", "projection", "schemaVersion"):
            with self.subTest(missing=missing):
                document = json.loads(json.dumps(sound))
                document.pop(missing)
                store = self.crafted_scope("case-{0}".format(missing.lower()), document)
                with self.assertRaises(ProgressStoreError) as raised:
                    store.facts()
                self.assertEqual(raised.exception.reason, REASON_MALFORMED_STORE)

    def test_a_regressed_acceptance_in_the_history_is_refused_on_read(self) -> None:
        self.accept(52)
        document = self.published()
        document["accepted"]["checkpoint"] = 40
        self.commit_record(document)
        with self.assertRaises(ProgressStoreError) as raised:
            self.store.facts()
        self.assertEqual(raised.exception.reason, REASON_CHECKPOINT_REGRESSED)

    def test_a_hand_written_value_is_refused_with_its_own_reason(self) -> None:
        self.accept(52)
        sound = self.published()
        for key, value, reason in (
            ("confidence", "urgent", REASON_INVALID_CONFIDENCE),
            ("remaining", -3, REASON_INVALID_REMAINING),
            ("note", "one\ntwo", REASON_INVALID_NOTE),
        ):
            with self.subTest(key=key):
                document = json.loads(json.dumps(sound))
                document["projection"][key] = value
                store = self.crafted_scope("case-{0}".format(key), document)
                with self.assertRaises(ProgressStoreError) as raised:
                    store.facts()
                self.assertEqual(raised.exception.reason, reason)

    def test_an_unsourced_acceptance_is_refused(self) -> None:
        self.accept(52)
        document = self.published()
        document["accepted"]["commit"] = "not-a-commit"
        self.commit_record(document)
        with self.assertRaises(ProgressStoreError) as raised:
            self.store.facts()
        self.assertEqual(raised.exception.reason, REASON_INVALID_COMMIT)

    def test_a_completion_beyond_the_roadmap_in_the_history_is_refused(self) -> None:
        self.commit_record({
            "schemaVersion": 1,
            "accepted": None,
            "named": {"checkpoint": 10, "total": 9},
            "projection": {"confidence": "low", "note": "", "remaining": 1},
        })
        with self.assertRaises(ProgressStoreError) as raised:
            self.store.facts()
        self.assertEqual(raised.exception.reason, REASON_INVALID_NAMED_TOTAL)

    def test_a_coordination_repository_that_is_not_there_is_refused(self) -> None:
        """"Nothing was published" and "the evidence could not be read" differ."""
        absent = ProgressStore.for_scope(
            self.tmp_path / "nowhere", project=PROJECT, ticket=TICKET
        )
        with self.assertRaises(ProgressStoreError) as raised:
            absent.facts()
        self.assertEqual(raised.exception.reason, REASON_UNREADABLE_STORE)

    def test_a_repository_with_no_history_at_all_is_simply_empty(self) -> None:
        empty = self.tmp_path / "fresh"
        empty.mkdir()
        self._run(empty, "init", "-q")
        facts = ProgressStore.for_scope(empty, project=PROJECT, ticket=TICKET).facts()
        self.assertEqual((facts.acceptances, facts.named, facts.projections), ((), (), ()))


if __name__ == "__main__":
    unittest.main()

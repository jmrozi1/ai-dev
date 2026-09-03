"""The supported production progress action: who may publish, and what it lands."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from ai_dev_flow import control_plane
from ai_dev_flow.control_plane import (
    ARTIFACT_FILENAMES,
    ARTIFACT_OWNERS,
    ControlPlaneError,
    _publish_acceptance,
    accept_progress,
    artifact_relative,
    publish,
    read_progress_record,
    require_owner,
    resolve_read_source,
)
from ai_dev_flow.progress_record import PROGRESS_FILENAME, progress_relative
from ai_dev_flow.progress_store import ProgressStore

PROJECT = "ai-dev"
TICKET = "issue-55"
_GIT_DATES = ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE")


class ProgressActionTestCase(unittest.TestCase):
    """One disposable coordination repository, one disposable product repository."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)
        self.repo = self.tmp_path / "coordination"
        self.product = self.tmp_path / "product"
        for root in (self.repo, self.product):
            root.mkdir(parents=True)
            self.run_git(root, "init", "-q")
            self.run_git(root, "config", "user.name", "Progress Action Tests")
            self.run_git(root, "config", "user.email", "progress-action-tests@example.com")
        self.run_git(self.repo, "commit", "-q", "--allow-empty", "-m", "initial")
        self.relative = progress_relative(PROJECT, TICKET)
        self.path = self.repo / self.relative

    def run_git(self, root: Path, *args: str, when: str = None) -> str:
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

    def checkpoint_commit(self, checkpoint) -> str:
        self.run_git(self.product, "commit", "-q", "--allow-empty", "-m", str(checkpoint))
        return self.run_git(self.product, "rev-parse", "HEAD")

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

    def state_file(self, body="# Control Plane State\n\nProject: ai-dev\n"):
        target = self.tmp_path / "accepted-state.md"
        target.write_text(body, encoding="utf-8")
        return target

    def accept(self, when="2026-09-02T09:00:00+00:00", **stated):
        stated.setdefault("remaining", 12)
        stated.setdefault("confidence", "low")
        stated.setdefault("state", "# Control Plane State\n\nProject: ai-dev\n")
        with self.dated(when):
            return accept_progress(
                self.repo, project=PROJECT, ticket=TICKET,
                product_repo=self.product, **stated
            )

    def published(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# The artifact and its owner
# --------------------------------------------------------------------------


class ProgressArtifactTests(ProgressActionTestCase):
    def test_the_progress_record_is_orchestrator_owned_and_scope_level(self) -> None:
        self.assertEqual(ARTIFACT_OWNERS["progress"], "orchestrator")
        self.assertEqual(ARTIFACT_FILENAMES["progress"], PROGRESS_FILENAME)
        self.assertEqual(
            artifact_relative(project=PROJECT, ticket=TICKET, artifact="progress", rail=None),
            "ai-dev/issue-55/progress.json",
        )
        with self.assertRaises(ControlPlaneError):
            artifact_relative(
                project=PROJECT, ticket=TICKET, artifact="progress", rail="some-rail"
            )

    def test_only_the_orchestrator_may_publish_progress(self) -> None:
        require_owner("progress", "orchestrator")
        for role in ("executor", "evidence"):
            with self.subTest(role=role):
                with self.assertRaises(ControlPlaneError):
                    require_owner("progress", role)

    def test_a_record_that_would_not_read_back_is_refused_on_publication(self) -> None:
        """The same schema decides both directions, so the two cannot drift."""
        for refused in (
            {"schemaVersion": 1, "accepted": None, "named": None, "projection": None},
            {"schemaVersion": 2, "accepted": None, "named": None,
             "projection": {"confidence": "low", "note": "", "remaining": 1}},
            {"schemaVersion": 1, "accepted": None, "named": None, "extra": 1,
             "projection": {"confidence": "low", "note": "", "remaining": 1}},
            {"schemaVersion": 1, "accepted": {"checkpoint": 1, "commit": "short"},
             "named": None,
             "projection": {"confidence": "low", "note": "", "remaining": 1}},
        ):
            with self.subTest(record=sorted(refused)):
                with self.assertRaises(ControlPlaneError):
                    publish(
                        self.repo, project=PROJECT, ticket=TICKET, artifact="progress",
                        role="orchestrator", content=json.dumps(refused),
                    )
        self.assertFalse(self.path.exists())

    def test_a_published_record_is_canonical_json(self) -> None:
        self.accept(checkpoint=52, commit=self.checkpoint_commit(52))
        text = self.path.read_text(encoding="utf-8")
        self.assertEqual(text, json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n")


# --------------------------------------------------------------------------
# The action is the durable transition
# --------------------------------------------------------------------------


class AcceptanceActionTests(ProgressActionTestCase):
    def test_accepting_commits_the_state_and_the_record_together_or_not_at_all(self) -> None:
        """One transition, one commit, both paths -- so they cannot drift apart.

        The earlier shape of this test asserted a single path, which is what let
        the accepted state and the record be moved independently. Two paths in
        one commit is the stronger property, and it is the whole of the fix: no
        supported action writes either alone, so there is nothing to keep in step.
        """
        before = self.run_git(self.repo, "rev-parse", "HEAD")
        state = "# Control Plane State\n\nProject: ai-dev\n\nAccepted: 52\n"
        target, head, _document = self.accept(
            state=state, checkpoint=52, commit=self.checkpoint_commit(52)
        )
        self.assertEqual(target, self.path)
        self.assertNotEqual(head, before)

        committed = self.run_git(self.repo, "show", "--name-only", "--format=", head)
        self.assertEqual(
            sorted(committed.splitlines()),
            ["ai-dev/issue-55/progress.json", "ai-dev/issue-55/state.md"],
        )
        self.assertEqual(
            self.run_git(self.repo, "log", "-1", "--format=%s", head),
            "orchestrator: accept (ai-dev/issue-55)",
        )
        # The state published is the state the caller handed over, verbatim.
        self.assertEqual(
            (self.repo / "ai-dev" / "issue-55" / "state.md").read_text(encoding="utf-8"),
            state,
        )

    def test_no_supported_action_moves_the_accepted_checkpoint_without_the_state(self) -> None:
        """The record is unwritable except beside the state it was accepted with.

        This is the structural half of the invariant. `accept` pairs the two, and
        `publish` refuses the record outright -- so there is no supported way to
        move the accepted checkpoint while leaving the published state behind.
        """
        with self.assertRaises(ControlPlaneError) as refused:
            publish(
                self.repo,
                project=PROJECT,
                ticket=TICKET,
                artifact="progress",
                role="orchestrator",
                content=json.dumps({"schemaVersion": 1}),
            )
        self.assertIn("written with the accepted state by `accept`", str(refused.exception))
        # And the refusal happens before anything is written or committed.
        self.assertEqual(
            self.run_git(self.repo, "status", "--porcelain"), ""
        )

    def test_the_acceptance_instant_is_the_instant_of_that_very_commit(self) -> None:
        """Not a stated time, and not the product commit's: the transition's own."""
        _target, head, _document = self.accept(
            "2026-09-02T09:00:00+00:00", checkpoint=52, commit=self.checkpoint_commit(52)
        )
        store = ProgressStore.for_scope(self.repo, project=PROJECT, ticket=TICKET)
        acceptance = store.facts().acceptances[-1]
        self.assertEqual(acceptance.commit, head)
        self.assertEqual(
            acceptance.accepted_at,
            self.run_git(self.repo, "log", "-1", "--format=%cI", head),
        )
        self.assertEqual(acceptance.accepted_at, "2026-09-02T09:00:00Z")

    def test_the_product_commit_the_orchestrator_accepted_is_kept(self) -> None:
        product_commit = self.checkpoint_commit(52)
        self.accept(checkpoint=52, commit=product_commit)
        self.assertEqual(self.published()["accepted"]["commit"], product_commit)

    def test_the_action_writes_nothing_into_the_product_repository(self) -> None:
        """Acceptance is a coordination act. The product worktree stays clean."""
        before = self.run_git(self.product, "status", "--porcelain")
        head = self.run_git(self.product, "rev-parse", "HEAD") if self._has_commits() else ""
        self.accept(checkpoint=52, commit=self.checkpoint_commit(52))
        self.assertEqual(self.run_git(self.product, "status", "--porcelain"), before)
        self.assertFalse((self.product / ".ai-dev").exists())
        if head:
            self.assertEqual(self.run_git(self.product, "rev-parse", "HEAD"), head)

    def _has_commits(self) -> bool:
        completed = subprocess.run(
            ["git", "-C", str(self.product), "rev-parse", "--verify", "--quiet", "HEAD"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        return completed.returncode == 0

    def test_a_projection_alone_is_a_supported_reconsideration(self) -> None:
        self.accept(checkpoint=52, commit=self.checkpoint_commit(52), remaining=12)
        _target, _head, document = self.accept(remaining=14, note="scope grew")
        self.assertEqual(document["accepted"]["checkpoint"], 52)
        self.assertEqual(document["projection"]["remaining"], 14)
        self.assertEqual(document["projection"]["note"], "scope grew")

    def test_a_named_completion_is_a_supported_act_of_its_own(self) -> None:
        _target, head, document = self.accept("2026-08-30T12:00:00+00:00", named=6, named_total=9)
        self.assertEqual(document["named"], {"checkpoint": 6, "total": 9})
        store = ProgressStore.for_scope(self.repo, project=PROJECT, ticket=TICKET)
        completion = store.facts().named[-1]
        self.assertEqual(completion.commit, head)
        self.assertEqual(completion.completed_at, "2026-08-30T12:00:00Z")

    def test_facts_the_action_did_not_state_are_carried_rather_than_cleared(self) -> None:
        self.accept(checkpoint=52, commit=self.checkpoint_commit(52))
        self.accept(named=6, named_total=9)
        self.accept(remaining=11, note="reconsidered")
        document = self.published()
        self.assertEqual(document["accepted"]["checkpoint"], 52)
        self.assertEqual(document["named"], {"checkpoint": 6, "total": 9})
        self.assertEqual(document["projection"]["remaining"], 11)

    def test_the_command_line_is_the_supported_way_to_run_it(self) -> None:
        commit = self.checkpoint_commit(52)
        out = io.StringIO()
        with self.dated("2026-09-02T09:00:00+00:00"), contextlib.redirect_stdout(out):
            code = control_plane.main([
                "accept", "--repo", str(self.repo), "--project", PROJECT,
                "--ticket", TICKET, "--product-repo", str(self.product),
                "--state-file", str(self.state_file()),
                "--checkpoint", "52", "--commit", commit,
                "--remaining", "12", "--confidence", "low", "--note", "reconsidered",
            ])
        self.assertEqual(code, 0)
        printed = out.getvalue()
        self.assertIn("published: ai-dev/issue-55/progress.json", printed)
        self.assertIn("accepted checkpoint: 52 at {0}".format(commit), printed)
        self.assertIn("projected remaining: 12 (low confidence)", printed)
        self.assertEqual(self.published()["accepted"]["checkpoint"], 52)

    def test_the_command_line_refuses_a_confidence_outside_the_three(self) -> None:
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                control_plane.main([
                    "accept", "--repo", str(self.repo), "--project", PROJECT,
                    "--ticket", TICKET, "--state-file", str(self.state_file()),
                    "--remaining", "12", "--confidence", "urgent",
                ])


# --------------------------------------------------------------------------
# The writer model
# --------------------------------------------------------------------------


class WriterModelTests(ProgressActionTestCase):
    """One mutation boundary, fail-closed against a record that landed meanwhile."""

    def test_publication_refuses_when_the_head_moved_under_it(self) -> None:
        """A concurrent acceptance is refused, never overwritten.

        The action reads the published record and carries the head it read into
        publication, so a second writer that landed in between makes this one
        fail rather than clobber it. That is why no lock is needed here: the
        coordination repository's own history serializes the writers, and a lost
        acceptance is refused rather than merely unlikely.
        """
        self.accept(checkpoint=52, commit=self.checkpoint_commit(52))
        source = resolve_read_source(self.repo)
        stale = source.head
        self.accept(checkpoint=53, commit=self.checkpoint_commit(53), remaining=11)
        landed = self.published()

        # The racing writer is another acceptance -- the only thing that can write
        # this record at all -- holding the head it read before the one above landed.
        with self.assertRaises(ControlPlaneError) as raised:
            _publish_acceptance(
                self.repo,
                project=PROJECT,
                ticket=TICKET,
                state="# Control Plane State\n\nProject: ai-dev\n",
                document={
                    "schemaVersion": 1,
                    "accepted": {"checkpoint": 60, "commit": self.checkpoint_commit(60)},
                    "named": None,
                    "projection": {"confidence": "low", "note": "", "remaining": 1},
                },
                expected_head=stale,
            )
        self.assertIn("expected head", str(raised.exception))
        self.assertEqual(self.published(), landed)

    def test_the_action_carries_the_head_it_read(self) -> None:
        import inspect

        source = inspect.getsource(control_plane.accept_progress)
        self.assertIn("expected_head=source.head", source)
        self.assertIn("resolve_read_source(repo_root)", source)

    def test_a_second_acceptance_of_the_same_checkpoint_cannot_be_lost(self) -> None:
        """Two writers naming one checkpoint: the second is refused, not merged."""
        self.accept(checkpoint=52, commit=self.checkpoint_commit(52))
        with self.assertRaises(ControlPlaneError):
            self.accept(checkpoint=52, commit=self.checkpoint_commit(52))
        store = ProgressStore.for_scope(self.repo, project=PROJECT, ticket=TICKET)
        self.assertEqual(
            [entry.checkpoint for entry in store.facts().acceptances], [52]
        )

    def test_reading_the_published_record_refuses_a_malformed_one(self) -> None:
        self.accept(checkpoint=52, commit=self.checkpoint_commit(52))
        self.path.write_text("{not json", encoding="utf-8")
        self.run_git(self.repo, "add", "--", str(self.path))
        self.run_git(self.repo, "commit", "-q", "-m", "crafted")
        with self.assertRaises(ControlPlaneError):
            read_progress_record(
                resolve_read_source(self.repo), project=PROJECT, ticket=TICKET
            )

    def test_an_unpublished_scope_reads_as_the_empty_record(self) -> None:
        record = read_progress_record(
            resolve_read_source(self.repo), project=PROJECT, ticket="issue-99"
        )
        self.assertEqual(record["accepted"], None)
        self.assertEqual(record["named"], None)
        self.assertEqual(record["projection"], None)


if __name__ == "__main__":
    unittest.main()

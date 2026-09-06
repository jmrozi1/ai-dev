"""The D11 surface a person actually reads: reduced, drawn, and really served."""

from __future__ import annotations

import json
import os
import re
import subprocess
import unittest
from decimal import Decimal
from pathlib import Path

from ai_dev_flow import decision_manager as manager
from ai_dev_flow import decision_manager_web as web
from ai_dev_flow.decision_manager import ManagerRun, project_progress
from ai_dev_flow.decision_manager_web import RenderError, build_progress
from ai_dev_flow.manager_controller import ManagerController
from ai_dev_flow import control_plane
from ai_dev_flow.progress_store import ProgressStore
from ai_dev_flow.progress_view import ProgressView, project_progress as project_view

from tests.test_decision_manager_launch import (
    LIVE_RAIL,
    PROJECT,
    SESSION,
    SourcedLaunchTestCase,
    TICKET,
)
from tests.test_manager_controller import ALWAYS_ALIVE, fetched, payload_in
from ai_dev_flow.decision_manager_web import start_serving


def a_view(**overrides) -> ProgressView:
    """One complete surface, at the state this rail was authorized against."""
    fields = dict(
        available=True,
        reason=None,
        source_healthy=True,
        named_checkpoint=7,
        named_total=9,
        named_completed_at="2026-08-30T12:00:00Z",
        accepted_checkpoint=52,
        accepted_at="2026-09-02T09:00:00Z",
        projected_remaining=12,
        projected_final=64,
        percentage=Decimal("81.25"),
        confidence="low",
        delta_24h=1,
        delta_48h=2,
        delta_reason=None,
        revision_at="2026-08-31T10:00:00Z",
        revision_from=62,
        revision_to=64,
        revision_note="scope grew: D8 needed its own remediation checkpoint",
        preserved_count=2,
    )
    fields.update(overrides)
    return ProgressView(**fields)


# --------------------------------------------------------------------------
# The reduction
# --------------------------------------------------------------------------


class ProgressReductionTests(unittest.TestCase):
    """Field by field, and nothing the accepted view did not already decide."""

    def test_the_payload_carries_exactly_the_recorded_surface(self) -> None:
        payload = build_progress(a_view())
        self.assertEqual(
            sorted(payload),
            [
                "acceptedAt", "acceptedCheckpoint", "available", "confidence",
                "delta24h", "delta48h", "deltaReason", "namedCheckpoint",
                "namedCompletedAt", "namedTotal", "percentage", "preservedCount",
                "projectedFinal", "projectedRemaining", "reason", "revision",
                "sourceHealthy",
            ],
        )
        self.assertEqual(sorted(payload["revision"]), ["at", "from", "note", "to"])

    def test_every_value_is_the_one_the_view_decided(self) -> None:
        view = a_view()
        payload = build_progress(view)
        self.assertEqual(payload["acceptedCheckpoint"], view.accepted_checkpoint)
        self.assertEqual(payload["acceptedAt"], view.accepted_at)
        self.assertEqual(payload["namedCheckpoint"], view.named_checkpoint)
        self.assertEqual(payload["namedTotal"], view.named_total)
        self.assertEqual(payload["namedCompletedAt"], view.named_completed_at)
        self.assertEqual(payload["projectedRemaining"], view.projected_remaining)
        self.assertEqual(payload["projectedFinal"], view.projected_final)
        self.assertEqual(payload["confidence"], view.confidence)
        self.assertEqual(payload["delta24h"], view.delta_24h)
        self.assertEqual(payload["delta48h"], view.delta_48h)
        self.assertEqual(payload["preservedCount"], view.preserved_count)
        self.assertEqual(payload["revision"]["from"], view.revision_from)
        self.assertEqual(payload["revision"]["to"], view.revision_to)
        self.assertEqual(payload["revision"]["note"], view.revision_note)

    def test_the_percentage_is_rounded_only_at_the_moment_of_drawing(self) -> None:
        for exact, drawn in (
            (Decimal("81.25"), "81%"),
            (Decimal("81.5"), "82%"),
            (Decimal("0.4"), "0%"),
            (Decimal(100), "100%"),
        ):
            with self.subTest(exact=exact):
                self.assertEqual(build_progress(a_view(percentage=exact))["percentage"], drawn)
        # And the view still holds the unrounded figure it was given.
        self.assertEqual(a_view().percentage, Decimal("81.25"))

    def test_an_unavailable_measure_carries_its_reason_and_no_number(self) -> None:
        payload = build_progress(
            a_view(
                available=False, reason="projection-overtaken", percentage=None,
                projected_final=None, projected_remaining=None, confidence=None,
            )
        )
        self.assertFalse(payload["available"])
        self.assertEqual(payload["reason"], "projection-overtaken")
        self.assertIsNone(payload["percentage"])
        # The accepted facts around it survive.
        self.assertEqual(payload["acceptedCheckpoint"], 52)

    def test_no_revision_is_null_rather_than_an_empty_object(self) -> None:
        payload = build_progress(
            a_view(revision_at=None, revision_from=None, revision_to=None, revision_note=None)
        )
        self.assertIsNone(payload["revision"])

    def test_an_absent_surface_draws_nothing_at_all(self) -> None:
        self.assertIsNone(build_progress(None))

    def test_a_look_alike_is_refused_rather_than_duck_typed(self) -> None:
        for impostor in (
            {"percentage": "81%"},
            [52, 64],
            "81%",
            object(),
        ):
            with self.assertRaises(RenderError) as caught:
                build_progress(impostor)
            self.assertEqual(caught.exception.reason, web.REASON_INVALID_PROGRESS)

    def test_the_render_module_never_reaches_the_progress_source_itself(self) -> None:
        source = Path(web.__file__).read_text(encoding="utf-8")
        for forbidden in (
            "ProgressStore", "progress_store", "commit_instant", "subprocess",
            "git log", "facts()", "record_acceptance", "record_projection",
        ):
            self.assertNotIn(forbidden, source, forbidden)


# --------------------------------------------------------------------------
# Composition
# --------------------------------------------------------------------------


class CompositionTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        # A real, empty coordination repository: nothing published, honestly so.
        subprocess.run(
            ["git", "-C", str(self.tmp), "init", "-q"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def a_run(self, *, now=1_800_000_000) -> ManagerRun:
        from ai_dev_flow.claude_allowance_store import AllowanceStore

        return ManagerRun(
            store=AllowanceStore(self.tmp / "allowance.json"),
            now=now,
            human_exclusive_since=None,
            progress=ProgressStore.for_scope(self.tmp, project="ai-dev", ticket="issue-55"),
        )

    def test_one_run_projects_progress_exactly_once_at_its_own_instant(self) -> None:
        calls = []
        real = manager.project_recorded_progress

        def spy(store, *, now):
            calls.append((store, now))
            return real(store, now=now)

        run = self.a_run()
        import unittest.mock

        with unittest.mock.patch.object(manager, "project_recorded_progress", spy):
            view = project_progress(run)
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0][0], run.progress)
        self.assertEqual(calls[0][1], run.now)
        self.assertIsInstance(view, ProgressView)

    def test_the_run_is_the_only_source_of_the_store_and_the_instant(self) -> None:
        """No clock and no store of composition's own; both come from the run."""
        run = self.a_run(now=1_700_000_000)
        self.assertIs(project_progress(run).source_healthy, True)
        source = Path(manager.__file__).read_text(encoding="utf-8")
        for forbidden in ("time.time", "for_scope", "ProgressStore("):
            self.assertNotIn(forbidden, source, forbidden)

    def test_composition_refuses_a_run_it_did_not_receive(self) -> None:
        with self.assertRaises(manager.ManagerRunError):
            project_progress(
                {"progress": ProgressStore.for_scope(
                    self.tmp, project="ai-dev", ticket="issue-55")}
            )

    def test_the_projected_view_reaches_the_page_unmodified(self) -> None:
        from tests.test_manager_controller import payload_in

        run = self.a_run()
        expected = build_progress(project_progress(run))
        page = manager.render_manager_page(run, *_an_empty_queue())
        self.assertEqual(payload_in(page)["progress"], expected)


def _an_empty_queue():
    from ai_dev_flow.decision_queue import QueueView

    return QueueView(rows=(), filters=()), {}


# --------------------------------------------------------------------------
# Really served, through the supported production manager surface
# --------------------------------------------------------------------------


class ProgressOverHttpTests(SourcedLaunchTestCase):
    """A real controller, a real loopback socket, and a real client.

    Everything below the socket is production code: `resolve_run` builds the run
    from the product root, `ManagerController.serve_observed` is the surface the
    supported dispatch opens, and the progress facts come from real commits in
    this fixture's real coordination repository through the deterministic
    mechanism. Nothing about the measure is stubbed.
    """

    def controller(self, registry=None) -> ManagerController:
        return ManagerController(
            self.context(), registry=self.registry if registry is None else registry
        )

    def coordination_commit(self, when: str) -> str:
        environment = dict(os.environ)
        environment["GIT_AUTHOR_DATE"] = when
        environment["GIT_COMMITTER_DATE"] = when
        subprocess.run(
            ["git", "-C", str(self.coordination), "commit", "-q", "--allow-empty",
             "-m", "orchestrator: state"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
        )
        return self._git("rev-parse", "HEAD")

    def product_repository(self) -> Path:
        """A real product repository whose commits are published Flow checkpoints."""
        if getattr(self, "_product", None) is None:
            self._product = self.sources / "product"
            self._product.mkdir(parents=True)
            for arguments in (
                ("init", "-q"),
                ("config", "user.name", "Progress Surface Tests"),
                ("config", "user.email", "progress-surface-tests@example.com"),
            ):
                subprocess.run(
                    ["git", "-C", str(self._product), *arguments],
                    check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
        return self._product

    def checkpoint_commit(self, checkpoint: int) -> str:
        """Publish one numeric Flow checkpoint. Publishing is all this does."""
        product = self.product_repository()
        subprocess.run(
            ["git", "-C", str(product), "commit", "-q", "--allow-empty", "-m", str(checkpoint)],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        completed = subprocess.run(
            ["git", "-C", str(product), "rev-parse", "HEAD"],
            check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return completed.stdout.strip()

    def accept(self, when: str, **stated) -> None:
        """The supported production action, run against real repositories.

        This is the whole point of the fixture: nothing below writes a progress
        store, because production has no such seam. The orchestrator publishes a
        progress record into the coordination repository, and the manager derives
        the measure from the commits that publication left behind.
        """
        previous = {key: os.environ.get(key) for key in ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE")}
        for key in previous:
            os.environ[key] = when
        try:
            control_plane.accept_progress(
                self.coordination, project=PROJECT, ticket=TICKET,
                state="# Control Plane State\n\nProject: ai-dev\n",
                product_repo=self.product_repository(), **stated
            )
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def record_the_baseline(self) -> ProgressStore:
        """The exact state this rail names, published through the production action."""
        self.accept("2026-08-25T10:00:00+00:00", checkpoint=48,
                    commit=self.checkpoint_commit(48), remaining=12,
                    confidence="low", note="initial estimate")
        self.accept("2026-08-28T10:00:00+00:00", checkpoint=49,
                    commit=self.checkpoint_commit(49), remaining=13,
                    confidence="low", note="one more than thought")
        self.accept("2026-08-30T10:00:00+00:00", checkpoint=50,
                    commit=self.checkpoint_commit(50), remaining=12,
                    confidence="low", note="reconsidered, unchanged")
        self.accept("2026-08-30T12:00:00+00:00", named=6, named_total=9,
                    remaining=12, confidence="low", note="named checkpoint 6 complete")
        self.accept("2026-08-31T10:00:00+00:00", remaining=14, confidence="low",
                    note="scope grew: D8 needed its own remediation checkpoint")
        self.accept("2026-09-01T10:00:00+00:00", checkpoint=51,
                    commit=self.checkpoint_commit(51), remaining=13,
                    confidence="low", note="reconsidered, unchanged")
        self.accept("2026-09-02T09:00:00+00:00", checkpoint=52,
                    commit=self.checkpoint_commit(52), remaining=12,
                    confidence="low", note="reconsidered, unchanged")
        return ProgressStore.for_scope(self.coordination, project=PROJECT, ticket=TICKET)

    def progress_path(self) -> Path:
        return self.coordination / PROJECT / TICKET / "progress.json"

    def served(self, *, alive=ALWAYS_ALIVE):
        controller = self.controller()
        run = self.a_run()
        server = controller.serve_observed(run, controller.queue_scope(run), alive=alive)
        self.addCleanup(server.server_close)
        serving = start_serving(server)
        self.addCleanup(serving.stop)
        page = fetched(server.server_address[1])
        return page, payload_in(page)

    # -- the measure, over the socket -------------------------------------

    def test_the_recorded_baseline_reaches_a_real_client_intact(self) -> None:
        self.record_the_baseline()
        _page, payload = self.served()
        progress = payload["progress"]
        self.assertTrue(progress["available"])
        self.assertEqual(progress["acceptedCheckpoint"], 52)
        self.assertEqual(progress["projectedRemaining"], 12)
        self.assertEqual(progress["projectedFinal"], 64)
        self.assertEqual(progress["percentage"], "81%")
        self.assertEqual(progress["confidence"], "low")
        self.assertEqual(progress["namedCheckpoint"], 7)
        self.assertEqual(progress["namedTotal"], 9)
        self.assertEqual(progress["revision"]["from"], 62)
        self.assertEqual(progress["revision"]["to"], 64)
        self.assertEqual(progress["preservedCount"], 4)

    def test_the_served_timestamps_are_the_ones_git_reports(self) -> None:
        store = self.record_the_baseline()
        _page, payload = self.served()
        facts = store.facts()
        self.assertEqual(
            payload["progress"]["acceptedAt"],
            self._git("log", "-1", "--format=%cI", facts.acceptances[-1].commit),
        )
        self.assertEqual(
            payload["progress"]["namedCompletedAt"],
            self._git("log", "-1", "--format=%cI", facts.named[-1].commit),
        )

    def test_a_published_but_unaccepted_checkpoint_changes_nothing_served(self) -> None:
        """A real checkpoint 53 exists on the product remote. Nobody accepted it.

        This is the invariant the whole wiring rests on: publishing a checkpoint
        reaches no progress action at all, so the numerator cannot move until the
        orchestrator performs the acceptance.
        """
        self.record_the_baseline()
        _page, before = self.served()
        published = self.checkpoint_commit(53)
        completed = subprocess.run(
            ["git", "-C", str(self.product_repository()), "cat-file", "-t", published],
            check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertEqual(completed.stdout.strip(), "commit")
        _page, after = self.served()
        self.assertEqual(after["progress"], before["progress"])
        self.assertEqual(after["progress"]["acceptedCheckpoint"], 52)

    def test_accepting_that_checkpoint_is_what_moves_the_numerator(self) -> None:
        """And it moves only through the supported action, over the same socket."""
        self.record_the_baseline()
        _page, before = self.served()
        self.assertEqual(before["progress"]["acceptedCheckpoint"], 52)
        self.accept("2026-09-02T11:00:00+00:00", checkpoint=53,
                    commit=self.checkpoint_commit(53), remaining=11,
                    confidence="low", note="reconsidered, unchanged")
        _page, after = self.served()
        self.assertEqual(after["progress"]["acceptedCheckpoint"], 53)
        self.assertEqual(after["progress"]["projectedFinal"], 64)
        self.assertEqual(after["progress"]["percentage"], "83%")

    def test_a_scope_with_no_published_progress_says_so_over_the_socket(self) -> None:
        self.assertFalse(self.progress_path().exists())
        _page, payload = self.served()
        progress = payload["progress"]
        self.assertFalse(progress["available"])
        self.assertEqual(progress["reason"], "no-accepted-checkpoint")
        self.assertIsNone(progress["percentage"])
        self.assertTrue(progress["sourceHealthy"])

    def test_an_unreadable_record_is_stated_rather_than_crashing_the_page(self) -> None:
        self.record_the_baseline()
        self.progress_path().write_text("{not json", encoding="utf-8")
        self._git("add", "--", str(self.progress_path()))
        self._git("commit", "-q", "-m", "crafted")
        _page, payload = self.served()
        progress = payload["progress"]
        self.assertFalse(progress["available"])
        self.assertFalse(progress["sourceHealthy"])
        self.assertEqual(progress["reason"], "malformed-progress-store")
        self.assertIsNone(progress["percentage"])

    # -- the queue is still the screen ------------------------------------

    def test_the_queue_stays_dominant_and_the_rows_stay_dense(self) -> None:
        """Telemetry sits in the aggregate strip and changes no row.

        The dense-row contract is the accepted one: title, project, ticket and
        elapsed time. Progress adds no row field, no badge and no column, and the
        rows remain the only list on the page.
        """
        self.authorize(LIVE_RAIL, "running")
        record = self.bind(LIVE_RAIL, session_id=SESSION)
        self.own(record)
        self.record_the_baseline()
        page, payload = self.served()

        self.assertTrue(payload["rows"])
        for row in payload["rows"]:
            self.assertEqual(
                sorted(row),
                ["elapsedSeconds", "itemId", "project", "state", "ticket", "title"],
            )
        for detail in payload["details"].values():
            self.assertNotIn("progress", detail)
            self.assertNotIn("percentage", detail)

        # One list of rows, and one aggregate strip. The page builds its DOM from
        # the payload, so where progress lands is read from the drawing code:
        # it appends into the aggregate strip and never touches the row list.
        self.assertEqual(page.count('id="rows"'), 1)
        self.assertEqual(page.count('id="allowance"'), 1)
        self.assertNotIn('id="progress"', page)
        drawing = self.function_body(page, "renderProgress")
        self.assertIn("allowanceEl.appendChild", drawing)
        self.assertNotIn("rowsEl", drawing)
        self.assertNotIn("payload.progress", self.function_body(page, "renderRows"))

    @staticmethod
    def function_body(page: str, name: str) -> str:
        """One named function from the page's own script, up to the next one."""
        start = page.index("function {0}(".format(name))
        remainder = page[start + 1:]
        end = remainder.find("\n  function ")
        return remainder if end < 0 else remainder[:end]

    def test_progress_adds_no_control_to_the_page(self) -> None:
        self.record_the_baseline()
        page, _payload = self.served()
        strip = page[page.index('class="allowance"'):page.index('id="rows"')]
        for control in ("<button", "<input", "<a ", "<form", "onclick"):
            self.assertNotIn(control, strip, control)

    def test_the_served_page_still_answers_only_one_read_only_path(self) -> None:
        self.record_the_baseline()
        controller = self.controller()
        run = self.a_run()
        server = controller.serve_observed(run, controller.queue_scope(run), alive=ALWAYS_ALIVE)
        self.addCleanup(server.server_close)
        serving = start_serving(server)
        self.addCleanup(serving.stop)
        import http.client

        port = server.server_address[1]
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("POST", "/")
            self.assertEqual(connection.getresponse().status, 405)
        finally:
            connection.close()

    def test_the_progress_figure_is_this_runs_and_does_not_refetch_per_request(self) -> None:
        """Projected once per run, exactly as the allowance windows are.

        Recorded facts describe state that outlives a render, so re-reading them
        while answering every request would be the polling loop this surface is
        not permitted to become.
        """
        self.record_the_baseline()
        controller = self.controller()
        run = self.a_run()
        reads = []
        real = ProgressStore.facts

        import unittest.mock

        with unittest.mock.patch.object(
            ProgressStore, "facts",
            lambda self_: (reads.append(1), real(self_))[1],
        ):
            server = controller.serve_observed(
                run, controller.queue_scope(run), alive=ALWAYS_ALIVE
            )
            self.addCleanup(server.server_close)
            serving = start_serving(server)
            self.addCleanup(serving.stop)
            port = server.server_address[1]
            fetched(port)
            fetched(port)
            fetched(port)
        self.assertEqual(len(reads), 1)


if __name__ == "__main__":
    unittest.main()

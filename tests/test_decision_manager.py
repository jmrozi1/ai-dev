"""`decision_manager` composes one run and adds no authority of its own.

Every assertion below is about composition: which accepted call was made, how many
times, with exactly which values, and that what came back reached the page
untouched. Nothing here re-checks what the estimator, the store, the view, the
queue, or the renderer already decided -- those have their own accepted suites,
and a second opinion here could only drift from them.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Dict
import ast
import dataclasses
import http.client
import json
import tempfile
import threading
import unittest
import unittest.mock

from ai_dev_flow import decision_manager as manager
from ai_dev_flow.claude_allowance import (
    HEALTH_PROVISIONAL,
    HEALTH_UNAVAILABLE,
    REASON_INVALID_EPOCH,
    WINDOW_FIVE_HOUR,
    WINDOW_SEVEN_DAY,
)
from ai_dev_flow.claude_allowance_store import AllowanceStore
from ai_dev_flow.claude_allowance_view import (
    REASON_NO_ANCHOR,
    AllowanceViewError,
    AllowanceWindowView,
    project_window,
    record_usage_reading,
)
from ai_dev_flow.claude_runtime import RuntimeResult
from ai_dev_flow.decision_manager import (
    MANAGER_WINDOWS,
    REASON_INVALID_RUN,
    ManagerRun,
    ManagerRunError,
    make_manager_server,
    project_allowance,
    render_manager_page,
)
from ai_dev_flow.decision_manager_web import (
    LOOPBACK_HOST,
    PAGE_PATH,
    REASON_DETAIL_MISSING,
    REASON_NOT_LOOPBACK,
    RenderError,
    build_allowance,
)
from ai_dev_flow.attention_projection import ACTIVITY_BLOCKED, OWNER_HUMAN
from ai_dev_flow.decision_queue import (
    QUEUE_STATES,
    EvidenceReference,
    PendingDecision,
    SelectedDetail,
    build_queue,
)

PROJECT = "ai-dev"
TICKET = "issue-55"

FIVE_HOUR_SECONDS = 5 * 60 * 60
SEVEN_DAY_SECONDS = 7 * 24 * 60 * 60
BASE = 1_700_000_000
FIVE_RESET = BASE + FIVE_HOUR_SECONDS
SEVEN_RESET = BASE + SEVEN_DAY_SECONDS
NOW = BASE + 180
SINCE = BASE - 1

MODULE_SOURCE = Path(manager.__file__).read_text(encoding="utf-8")


def a_result(cost) -> RuntimeResult:
    """A reduced runtime result; only its cost is ever read."""
    return RuntimeResult(
        session_id="11111111-2222-3333-4444-555555555555",
        mode="launch",
        subtype="success",
        is_error=False,
        num_turns=1,
        total_cost_usd=cost,
    )


def a_decision(**overrides) -> PendingDecision:
    base = dict(
        decision_id="d-1", project=PROJECT, ticket=TICKET, rail="issue-55-rail-one",
        raised_at="raised-1", title="Choose the credential route",
        explanation="The requirements do not say which credential the worker uses.",
        elapsed_seconds=7200,
        activity=ACTIVITY_BLOCKED,
        attention_owner=OWNER_HUMAN,
        evidence=(EvidenceReference(label="review", locator="rails/one/handoff.md"),),
    )
    base.update(overrides)
    return PendingDecision(**base)


def payload_of(page: str) -> dict:
    block = page.split('id="queue-payload">', 1)[1].split("</script>", 1)[0]
    return json.loads(block)


def allowance_of(page: str) -> dict:
    """The page's allowance entries keyed by the canonical window they name."""
    return {entry["window"]: entry for entry in payload_of(page)["allowance"]}


class _ProjectionSpy:
    """Records every accepted projection call and still performs it.

    A counting double that returned a fabricated view would prove the call count
    and nothing about the values, so the real accepted projection still runs and
    its result is what the composition receives.
    """

    def __init__(self) -> None:
        self.calls = []

    def __call__(self, store, *, window, now, human_exclusive_since):
        self.calls.append(
            {
                "store": store,
                "window": window,
                "now": now,
                "human_exclusive_since": human_exclusive_since,
            }
        )
        return project_window(
            store, window=window, now=now, human_exclusive_since=human_exclusive_since
        )


class ManagerTestCase(unittest.TestCase):
    """Every fixture builds its own store; none reaches into another's."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="decision-manager-"))
        self.addCleanup(self._remove_root)
        self.path = self.root / "workload.json"
        self.store = AllowanceStore(self.path)

    def _remove_root(self) -> None:
        for item in sorted(self.root.rglob("*"), reverse=True):
            item.unlink() if item.is_file() else item.rmdir()
        self.root.rmdir()

    # -- fixture builders -------------------------------------------------

    def spend(self, cost, key: str) -> None:
        self.store.record_result(a_result(cost), idempotency_key=key)

    def read(self, offset: int, *, five=None, seven=None, since=SINCE) -> None:
        record_usage_reading(
            self.store,
            observed_at=BASE + offset,
            five_hour=None if five is None else (FIVE_RESET, Decimal(five)),
            seven_day=None if seven is None else (SEVEN_RESET, Decimal(seven)),
            human_exclusive_since=since,
        )

    def both_windows(self) -> None:
        """One trained interval per window, with deliberately different figures.

        The two windows must not be able to pass for each other, so the five-hour
        reading lands at 30% and the seven-day reading at 9%. A composition that
        showed one window's projection under the other's name would have to
        produce one of those two numbers in the wrong place.
        """
        self.spend(1.0, "k1")
        self.read(0, five="10", seven="3")
        self.spend(2.0, "k2")
        self.read(60, five="30", seven="9")

    def five_hour_only(self) -> None:
        """Evidence for one window and none at all for the other."""
        self.spend(1.0, "k1")
        self.read(0, five="10")
        self.spend(2.0, "k2")
        self.read(60, five="30")

    def a_run(self, *, now=NOW, since=SINCE, store=None) -> ManagerRun:
        return ManagerRun(
            store=self.store if store is None else store,
            now=now,
            human_exclusive_since=since,
        )

    def a_queue(self, decisions=None):
        """One accepted queue view plus every row's accepted detail."""
        queue = build_queue(list(decisions if decisions is not None else [a_decision()]), [])
        view = queue.view(filters=QUEUE_STATES)
        details: Dict[str, SelectedDetail] = {}
        for row in view.rows:
            details[row.item_id] = queue.view(
                filters=QUEUE_STATES, selected_id=row.item_id
            ).detail
        return view, details

    def rendered(self, run=None, decisions=None) -> str:
        view, details = self.a_queue(decisions)
        return render_manager_page(self.a_run() if run is None else run, view, details)

    # -- durable-state helpers --------------------------------------------

    def tree(self) -> dict:
        """Every byte under this fixture's root, so a write cannot hide."""
        return {
            str(item.relative_to(self.root)): item.read_bytes()
            for item in sorted(self.root.rglob("*"))
            if item.is_file()
        }


# --------------------------------------------------------------------------
# One run, one set of inputs
# --------------------------------------------------------------------------


class RunInputTests(ManagerTestCase):
    def test_both_windows_are_projected_from_one_store_and_one_instant(self) -> None:
        """The same store object and the same epoch, never re-resolved per window."""
        self.both_windows()
        spy = _ProjectionSpy()
        with unittest.mock.patch.object(manager, "project_window", spy):
            project_allowance(self.a_run())

        self.assertEqual(len(spy.calls), 2)
        for call in spy.calls:
            self.assertIs(call["store"], self.store)
            self.assertEqual(call["now"], NOW)

    def test_each_accepted_window_is_projected_exactly_once(self) -> None:
        """Two windows, two calls, in the order this module fixes."""
        self.both_windows()
        spy = _ProjectionSpy()
        with unittest.mock.patch.object(manager, "project_window", spy):
            project_allowance(self.a_run())

        self.assertEqual([call["window"] for call in spy.calls], list(MANAGER_WINDOWS))

    def test_the_projected_windows_are_the_two_accepted_ones(self) -> None:
        self.assertEqual(MANAGER_WINDOWS, (WINDOW_FIVE_HOUR, WINDOW_SEVEN_DAY))

    def test_the_run_holds_the_inputs_and_cannot_be_edited_between_windows(self) -> None:
        """Frozen: a run cannot acquire a second instant part-way through itself."""
        run = self.a_run()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            run.now = NOW + 1
        with self.assertRaises(dataclasses.FrozenInstanceError):
            run.human_exclusive_since = BASE

    def test_the_module_reads_no_clock_and_no_environment_of_its_own(self) -> None:
        """The run's instant is the caller's; there is nothing here to disagree with it."""
        tree = ast.parse(MODULE_SOURCE)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                imported.add((node.module or "").split(".")[0])
        self.assertNotIn("time", imported)
        self.assertNotIn("os", imported)
        self.assertNotIn("datetime", imported)
        for forbidden in ("os.environ", "getenv", "environb"):
            self.assertFalse(forbidden in MODULE_SOURCE, forbidden)

    def test_the_module_calls_the_accepted_projection_from_exactly_one_place(self) -> None:
        """One call site, so the two windows cannot diverge in how they are asked."""
        sites = [
            node
            for node in ast.walk(ast.parse(MODULE_SOURCE))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "project_window"
        ]
        self.assertEqual(len(sites), 1)


# --------------------------------------------------------------------------
# The human exclusivity claim
# --------------------------------------------------------------------------


class HumanExclusivityTests(ManagerTestCase):
    def test_the_claim_has_no_default_and_must_be_stated(self) -> None:
        """Absence is something a caller says, never something it falls into."""
        fields = {field.name: field for field in dataclasses.fields(ManagerRun)}
        for name in ("store", "now", "human_exclusive_since"):
            self.assertIs(fields[name].default, dataclasses.MISSING)
            self.assertIs(fields[name].default_factory, dataclasses.MISSING)
        with self.assertRaises(TypeError):
            ManagerRun(store=self.store, now=NOW)

    def test_the_stated_claim_reaches_both_windows_unchanged(self) -> None:
        self.both_windows()
        spy = _ProjectionSpy()
        with unittest.mock.patch.object(manager, "project_window", spy):
            project_allowance(self.a_run(since=SINCE))
        self.assertEqual([call["human_exclusive_since"] for call in spy.calls], [SINCE, SINCE])

    def test_an_unstated_claim_is_passed_on_as_none_and_never_substituted(self) -> None:
        """`None` is evidence of absence and is carried, not repaired."""
        self.both_windows()
        spy = _ProjectionSpy()
        with unittest.mock.patch.object(manager, "project_window", spy):
            project_allowance(self.a_run(since=None))
        self.assertEqual([call["human_exclusive_since"] for call in spy.calls], [None, None])

    def test_without_the_claim_no_window_shows_a_number(self) -> None:
        """Silence is never coverage: unavailable with a reason, never a figure."""
        self.both_windows()
        views = project_allowance(self.a_run(since=None))
        for view in views:
            self.assertEqual(view.health, HEALTH_UNAVAILABLE)
            self.assertIsNone(view.point_percentage)
            self.assertTrue(view.source_healthy)

        page = self.rendered(run=self.a_run(since=None))
        for entry in allowance_of(page).values():
            self.assertIsNone(entry["used"])
            self.assertEqual(entry["health"], HEALTH_UNAVAILABLE)
            self.assertTrue(entry["reason"])

    def test_the_claim_is_never_written_anywhere(self) -> None:
        """One run reads evidence and leaves none: no store write, no side file."""
        self.both_windows()
        before = self.tree()
        self.rendered()
        self.assertEqual(self.tree(), before)
        self.assertFalse(self.store.lock_path.exists())
        self.assertFalse(str(SINCE) in self.path.read_text(encoding="utf-8"), "since leaked")

    def test_the_module_opens_no_file_and_persists_nothing(self) -> None:
        for forbidden in ("open(", "write_text", "write_bytes", "json.dump", "mkdir"):
            self.assertFalse(forbidden in MODULE_SOURCE, forbidden)


# --------------------------------------------------------------------------
# Per-window independence
# --------------------------------------------------------------------------


class WindowIndependenceTests(ManagerTestCase):
    def test_one_window_may_be_unavailable_while_the_other_is_not(self) -> None:
        """Partial truth is the answer, not a case to repair."""
        self.five_hour_only()
        five, seven = project_allowance(self.a_run())

        self.assertEqual(five.window, WINDOW_FIVE_HOUR)
        self.assertEqual(five.health, HEALTH_PROVISIONAL)
        self.assertEqual(five.point_percentage, Decimal("30"))

        self.assertEqual(seven.window, WINDOW_SEVEN_DAY)
        self.assertEqual(seven.health, HEALTH_UNAVAILABLE)
        self.assertEqual(seven.reason, REASON_NO_ANCHOR)
        self.assertIsNone(seven.point_percentage)
        self.assertTrue(seven.source_healthy)

    def test_a_partial_run_still_renders_both_windows_truthfully(self) -> None:
        self.five_hour_only()
        entries = allowance_of(self.rendered())
        self.assertEqual(entries[WINDOW_FIVE_HOUR]["used"], "≈30% used")
        self.assertIsNone(entries[WINDOW_SEVEN_DAY]["used"])
        self.assertEqual(entries[WINDOW_SEVEN_DAY]["reason"], REASON_NO_ANCHOR)

    def test_the_views_are_returned_in_the_order_they_were_projected(self) -> None:
        self.both_windows()
        views = project_allowance(self.a_run())
        self.assertEqual(tuple(view.window for view in views), MANAGER_WINDOWS)

    def test_each_window_is_exactly_what_the_accepted_projection_returned(self) -> None:
        """Nothing is deduplicated, softened, or filled in between the two calls."""
        self.both_windows()
        composed = project_allowance(self.a_run())
        direct = tuple(
            project_window(
                self.store, window=window, now=NOW, human_exclusive_since=SINCE
            )
            for window in MANAGER_WINDOWS
        )
        self.assertEqual(composed, direct)
        for view in composed:
            self.assertIs(type(view), AllowanceWindowView)


# --------------------------------------------------------------------------
# Into the accepted page
# --------------------------------------------------------------------------


class CompositionTests(ManagerTestCase):
    def test_each_window_is_drawn_under_its_own_name(self) -> None:
        """The two figures are distinct, so a swap could not hide behind them."""
        self.both_windows()
        entries = allowance_of(self.rendered())
        self.assertEqual(entries[WINDOW_FIVE_HOUR]["used"], "≈30% used")
        self.assertEqual(entries[WINDOW_FIVE_HOUR]["label"], "5h")
        self.assertEqual(entries[WINDOW_SEVEN_DAY]["used"], "≈9% used")
        self.assertEqual(entries[WINDOW_SEVEN_DAY]["label"], "7d")

    def test_the_page_shows_exactly_what_this_run_projected(self) -> None:
        """The accepted reduction of the run's own views, byte for byte."""
        self.both_windows()
        page = self.rendered()
        self.assertEqual(
            payload_of(page)["allowance"],
            build_allowance(project_allowance(self.a_run())),
        )

    def test_the_queue_stays_the_caller_s_and_is_not_rebuilt(self) -> None:
        """Rows and details are the accepted view's; this module supplies neither."""
        self.both_windows()
        decisions = [a_decision(), a_decision(decision_id="d-2", title="Pick the host")]
        view, details = self.a_queue(decisions)
        payload = payload_of(render_manager_page(self.a_run(), view, details))
        self.assertEqual(
            [row["itemId"] for row in payload["rows"]], [row.item_id for row in view.rows]
        )
        self.assertEqual(sorted(payload["details"]), sorted(details))
        for forbidden in ("build_queue", "QueueView(", "SelectedDetail("):
            self.assertFalse(forbidden in MODULE_SOURCE, forbidden)

    def test_a_server_serves_this_run_s_page_and_nothing_else(self) -> None:
        self.both_windows()
        view, details = self.a_queue()
        server = make_manager_server(self.a_run(), view, details)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)

        connection = http.client.HTTPConnection(*server.server_address[:2], timeout=5)
        self.addCleanup(connection.close)
        connection.request("GET", PAGE_PATH)
        served = connection.getresponse().read().decode("utf-8")

        self.assertEqual(
            payload_of(served)["allowance"],
            build_allowance(project_allowance(self.a_run())),
        )
        connection.request("POST", PAGE_PATH)
        self.assertEqual(connection.getresponse().status, 405)

    def test_the_server_binds_loopback_by_the_accepted_rule(self) -> None:
        """One place decides what this surface answers on, and it is not this one."""
        self.both_windows()
        view, details = self.a_queue()
        before = self.tree()
        with self.assertRaises(RenderError) as caught:
            make_manager_server(self.a_run(), view, details, host="0.0.0.0")
        self.assertEqual(caught.exception.reason, REASON_NOT_LOOPBACK)
        # Refused after this run was projected, which is read-only: nothing durable
        # may have happened on the way to a refusal.
        self.assertEqual(self.tree(), before)
        self.assertFalse(LOOPBACK_HOST in MODULE_SOURCE, LOOPBACK_HOST)


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


class RefusalTests(ManagerTestCase):
    def test_only_a_manager_run_composes(self) -> None:
        self.both_windows()
        for wrong in (
            None,
            (self.store, NOW, SINCE),
            {"store": self.store, "now": NOW, "human_exclusive_since": SINCE},
        ):
            with self.assertRaises(ManagerRunError) as caught:
                project_allowance(wrong)
            self.assertEqual(caught.exception.reason, REASON_INVALID_RUN)

    def test_a_caller_fault_keeps_the_accepted_projection_s_reason(self) -> None:
        """One refusal, one spelling: the view's, not a second one raised here."""
        self.both_windows()
        with self.assertRaises(AllowanceViewError) as caught:
            project_allowance(self.a_run(now=float(NOW)))
        self.assertEqual(caught.exception.reason, REASON_INVALID_EPOCH)

    def test_a_queue_fault_keeps_the_accepted_render_path_s_reason(self) -> None:
        self.both_windows()
        view, details = self.a_queue()
        with self.assertRaises(RenderError) as caught:
            render_manager_page(self.a_run(), view, {})
        self.assertEqual(caught.exception.reason, REASON_DETAIL_MISSING)

    def test_an_unreadable_store_is_shown_as_unavailable_rather_than_raised(self) -> None:
        """A source that refuses is still a page; a run that cannot draw is not."""
        self.both_windows()
        self.path.write_text("{ not json", encoding="utf-8")
        views = project_allowance(self.a_run())
        for view in views:
            self.assertEqual(view.health, HEALTH_UNAVAILABLE)
            self.assertFalse(view.source_healthy)
        for entry in allowance_of(self.rendered()).values():
            self.assertIsNone(entry["used"])
            self.assertFalse(entry["sourceHealthy"])


if __name__ == "__main__":
    unittest.main()

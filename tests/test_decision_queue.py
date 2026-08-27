"""`decision_queue` projects genuine human decisions first, and decides nothing itself."""

from __future__ import annotations

from dataclasses import fields, replace
from itertools import combinations
from pathlib import Path
from typing import List, Optional
import ast
import unittest

from ai_dev_flow import decision_queue as queue_module
from ai_dev_flow.decision_queue import (
    DEFAULT_FILTERS,
    MAX_EVIDENCE_REFERENCES,
    QUEUE_STATES,
    EvidenceReference,
    OperationalAgent,
    PendingDecision,
    QueueError,
    QueueRow,
    SelectedDetail,
    build_queue,
)
from ai_dev_flow.session_lifecycle import (
    STATE_DISCONNECTED,
    STATE_RUNNING,
    STATE_WAITING,
    SessionProjection,
)

PROJECT = "ai-dev"
TICKET = "issue-55"

SESSION_SECRET = "11111111-1111-4111-8111-111111111111"
DETAIL_SECRET = "LIFECYCLE-DETAIL-that-must-never-reach-a-row"
REASON_SECRET = "lifecycle-reason-that-must-never-reach-a-row"
EXPLANATION_SECRET = "ORCHESTRATOR-EXPLANATION-that-must-never-reach-a-row"
EVIDENCE_SECRET = "EVIDENCE-LOCATOR-that-must-never-reach-a-row"


def decision(**overrides) -> PendingDecision:
    base = dict(
        decision_id="d-1",
        project=PROJECT,
        ticket=TICKET,
        rail="issue-55-some-rail",
        raised_at="raised-0001",
        title="Choose the credential route",
        explanation=EXPLANATION_SECRET,
        elapsed_seconds=100,
        evidence=(EvidenceReference(label="review", locator=EVIDENCE_SECRET),),
    )
    base.update(overrides)
    return PendingDecision(**base)


def projection(**overrides) -> SessionProjection:
    base = dict(
        state=STATE_RUNNING,
        reason=REASON_SECRET,
        detail=DETAIL_SECRET,
        session_id=SESSION_SECRET,
        rail="issue-55-other-rail",
        elapsed_seconds=500,
    )
    base.update(overrides)
    return SessionProjection(**base)


def agent(**overrides) -> OperationalAgent:
    rail = overrides.pop("rail", "issue-55-other-rail")
    proj = overrides.pop("projection", None)
    if proj is None:
        proj = projection(rail=rail)
    base = dict(project=PROJECT, ticket=TICKET, rail=rail, title="Implement the seam")
    base.update(overrides)
    return OperationalAgent(projection=proj, **base)


def _strings(value: object, seen: Optional[List[int]] = None) -> List[str]:
    """Every string reachable from a value, so a leak cannot hide in a nested field."""
    seen = [] if seen is None else seen
    if id(value) in seen:
        return []
    seen.append(id(value))
    if isinstance(value, str):
        return [value]
    found: List[str] = []
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            found.extend(_strings(item, seen))
        return found
    if isinstance(value, dict):
        for key, item in value.items():
            found.extend(_strings(key, seen))
            found.extend(_strings(item, seen))
        return found
    for attr in getattr(value, "__dataclass_fields__", {}):
        found.extend(_strings(getattr(value, attr), seen))
    return found


# --------------------------------------------------------------------------
# Inputs: exact, immutable, and unable to carry a payload
# --------------------------------------------------------------------------


class InputValidationTests(unittest.TestCase):
    def test_the_accepted_records_build(self) -> None:
        view = build_queue([decision()], [agent()]).view()
        self.assertEqual([row.item_id for row in view.rows], ["decision:d-1"])

    def test_every_identity_and_text_field_must_be_exact_non_empty_text(self) -> None:
        for field in ("decision_id", "project", "ticket", "rail", "raised_at", "title",
                      "explanation"):
            for value in ("", "   ", None, 0, [], object()):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(QueueError) as caught:
                        decision(**{field: value})
                    self.assertEqual(caught.exception.reason, queue_module.REASON_INVALID_TEXT)

    def test_a_string_subclass_is_not_exact_text(self) -> None:
        class Title(str):
            pass

        with self.assertRaises(QueueError) as caught:
            decision(title=Title("Choose the credential route"))
        self.assertEqual(caught.exception.reason, queue_module.REASON_INVALID_TEXT)

    def test_overlong_text_is_refused_so_a_payload_cannot_wear_a_field(self) -> None:
        for field, limit in (("title", queue_module.MAX_TITLE),
                             ("explanation", queue_module.MAX_EXPLANATION)):
            with self.subTest(field=field):
                with self.assertRaises(QueueError) as caught:
                    decision(**{field: "x" * (limit + 1)})
                self.assertEqual(caught.exception.reason, queue_module.REASON_TEXT_TOO_LONG)

    def test_elapsed_seconds_must_be_a_non_negative_exact_int(self) -> None:
        for value in (-1, -600, 1.0, "100", None, True, False):
            with self.subTest(value=value):
                with self.assertRaises(QueueError) as caught:
                    decision(elapsed_seconds=value)
                self.assertEqual(caught.exception.reason, queue_module.REASON_INVALID_ELAPSED)
        self.assertEqual(decision(elapsed_seconds=0).elapsed_seconds, 0)

    def test_evidence_is_bounded_and_typed(self) -> None:
        many = tuple(
            EvidenceReference(label="e{0}".format(n), locator="l{0}".format(n))
            for n in range(MAX_EVIDENCE_REFERENCES + 1)
        )
        with self.assertRaises(QueueError) as caught:
            decision(evidence=many)
        self.assertEqual(caught.exception.reason, queue_module.REASON_TOO_MUCH_EVIDENCE)

        with self.assertRaises(QueueError) as caught:
            decision(evidence=("a transcript pasted as evidence",))
        self.assertEqual(caught.exception.reason, queue_module.REASON_INVALID_EVIDENCE)

        with self.assertRaises(QueueError) as caught:
            decision(evidence=[EvidenceReference(label="a", locator="b")])
        self.assertEqual(caught.exception.reason, queue_module.REASON_INVALID_EVIDENCE)

    def test_evidence_references_are_themselves_exact(self) -> None:
        for field in ("label", "locator"):
            with self.subTest(field=field):
                with self.assertRaises(QueueError):
                    EvidenceReference(**{"label": "a", "locator": "b", field: ""})

    def test_the_records_are_immutable(self) -> None:
        for record in (decision(), agent(), EvidenceReference(label="a", locator="b")):
            with self.subTest(record=type(record).__name__):
                with self.assertRaises(Exception):
                    record.title = "rewritten"  # type: ignore[misc]

    def test_an_operational_input_requires_the_accepted_projection_type(self) -> None:
        class LooksLikeAProjection:
            state = STATE_RUNNING
            reason = REASON_SECRET
            detail = DETAIL_SECRET
            session_id = SESSION_SECRET
            rail = "issue-55-other-rail"
            elapsed_seconds = 500

        with self.assertRaises(QueueError) as caught:
            agent(projection=LooksLikeAProjection())
        self.assertEqual(caught.exception.reason, queue_module.REASON_INVALID_PROJECTION)

    def test_a_decision_and_its_session_facts_must_agree(self) -> None:
        with self.assertRaises(QueueError) as caught:
            agent(rail="issue-55-other-rail", projection=projection(rail="issue-55-elsewhere"))
        self.assertEqual(caught.exception.reason, queue_module.REASON_FACT_MISMATCH)

    def test_duplicate_item_identity_is_refused(self) -> None:
        with self.assertRaises(QueueError) as caught:
            build_queue([decision(), decision()])
        self.assertEqual(caught.exception.reason, queue_module.REASON_DUPLICATE_ITEM)

        with self.assertRaises(QueueError) as caught:
            build_queue([], [agent(), agent()])
        self.assertEqual(caught.exception.reason, queue_module.REASON_DUPLICATE_ITEM)

    def test_the_two_input_kinds_are_not_interchangeable(self) -> None:
        with self.assertRaises(QueueError):
            build_queue([agent()])
        with self.assertRaises(QueueError):
            build_queue([], [decision()])

    def test_the_input_field_sets_have_nowhere_to_put_a_payload(self) -> None:
        self.assertEqual(
            {f.name for f in fields(PendingDecision)},
            {"decision_id", "project", "ticket", "rail", "raised_at", "title", "explanation",
             "elapsed_seconds", "evidence"},
        )
        self.assertEqual(
            {f.name for f in fields(OperationalAgent)},
            {"project", "ticket", "rail", "title", "projection"},
        )
        self.assertEqual({f.name for f in fields(EvidenceReference)}, {"label", "locator"})


# --------------------------------------------------------------------------
# Waiting has exactly one source
# --------------------------------------------------------------------------


class WaitingProvenanceTests(unittest.TestCase):
    """Every historical way of guessing "the human is needed" must fail here."""

    def test_only_a_published_decision_is_waiting(self) -> None:
        self.assertEqual(decision().state, STATE_WAITING)
        self.assertNotEqual(agent().state, STATE_WAITING)

    def test_an_operational_input_claiming_waiting_is_refused(self) -> None:
        with self.assertRaises(QueueError) as caught:
            agent(projection=projection(state=STATE_WAITING))
        self.assertEqual(caught.exception.reason, queue_module.REASON_OPERATIONAL_WAITING)

    def test_a_lifecycle_waiting_projection_is_refused_even_though_it_is_accepted_output(self) -> None:
        """The lifecycle answers "is this session progressing", not "is a person needed"."""
        blocked = projection(
            state=STATE_WAITING,
            reason="human-decision-pending",
            detail="the orchestrator recorded a pending human decision",
        )
        with self.assertRaises(QueueError) as caught:
            agent(projection=blocked)
        self.assertEqual(caught.exception.reason, queue_module.REASON_OPERATIONAL_WAITING)

    def test_no_disconnected_or_error_shaped_input_becomes_waiting(self) -> None:
        for detail in ("no handle is registered", "pid 42 is gone", "blocked", "error: refused",
                       "awaiting human input", "please confirm"):
            with self.subTest(detail=detail):
                entry = agent(projection=projection(state=STATE_DISCONNECTED, detail=detail))
                self.assertEqual(entry.state, STATE_DISCONNECTED)
                view = build_queue([], [entry]).view(filters=QUEUE_STATES)
                self.assertEqual([row.state for row in view.rows], [STATE_DISCONNECTED])

    def test_a_very_old_operational_item_never_becomes_waiting(self) -> None:
        old = agent(projection=projection(elapsed_seconds=60 * 60 * 24 * 30))
        self.assertEqual(old.state, STATE_RUNNING)

    def test_an_unsupported_operational_state_is_refused(self) -> None:
        for state in ("blocked", "reserved", "unknown", "pending"):
            with self.subTest(state=state):
                with self.assertRaises(QueueError) as caught:
                    agent(projection=projection(state=state))
                self.assertEqual(caught.exception.reason, queue_module.REASON_UNSUPPORTED_STATE)


# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------


class FilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.waiting = decision(decision_id="d-wait", elapsed_seconds=100)
        self.running = agent(rail="rail-running", projection=projection(
            rail="rail-running", state=STATE_RUNNING, elapsed_seconds=200))
        self.disconnected = agent(rail="rail-gone", projection=projection(
            rail="rail-gone", state=STATE_DISCONNECTED, elapsed_seconds=300))
        self.queue = build_queue([self.waiting], [self.running, self.disconnected])

    def test_the_default_is_waiting_only(self) -> None:
        view = self.queue.view()
        self.assertEqual(view.filters, DEFAULT_FILTERS)
        self.assertEqual(DEFAULT_FILTERS, (STATE_WAITING,))
        self.assertEqual([row.state for row in view.rows], [STATE_WAITING])

    def test_every_nonempty_combination_filters_independently(self) -> None:
        expected = {
            STATE_WAITING: "decision:d-wait",
            STATE_RUNNING: "agent:rail-running",
            STATE_DISCONNECTED: "agent:rail-gone",
        }
        for size in (1, 2, 3):
            for combo in combinations(QUEUE_STATES, size):
                with self.subTest(filters=combo):
                    view = self.queue.view(filters=combo)
                    self.assertEqual(
                        {row.item_id for row in view.rows},
                        {expected[state] for state in combo},
                    )
                    self.assertEqual(set(view.filters), set(combo))

    def test_filter_order_and_repetition_do_not_change_the_result(self) -> None:
        a = self.queue.view(filters=(STATE_DISCONNECTED, STATE_WAITING))
        b = self.queue.view(filters=(STATE_WAITING, STATE_DISCONNECTED, STATE_WAITING))
        self.assertEqual(a.filters, b.filters)
        self.assertEqual([r.item_id for r in a.rows], [r.item_id for r in b.rows])

    def test_an_unknown_filter_is_refused(self) -> None:
        for bad in ("blocked", "all", "", None, 1):
            with self.subTest(bad=bad):
                with self.assertRaises(QueueError) as caught:
                    self.queue.view(filters=(bad,))
                self.assertEqual(caught.exception.reason, queue_module.REASON_INVALID_FILTER)

    def test_an_empty_filter_set_is_refused_rather_than_showing_nothing(self) -> None:
        with self.assertRaises(QueueError) as caught:
            self.queue.view(filters=())
        self.assertEqual(caught.exception.reason, queue_module.REASON_INVALID_FILTER)


# --------------------------------------------------------------------------
# Ordering
# --------------------------------------------------------------------------


class OrderingTests(unittest.TestCase):
    def test_rows_are_oldest_first(self) -> None:
        items = [decision(decision_id="d-{0}".format(n), elapsed_seconds=n * 10)
                 for n in (3, 1, 2)]
        view = build_queue(items).view()
        self.assertEqual(
            [row.elapsed_seconds for row in view.rows], [30, 20, 10]
        )

    def test_equal_ages_break_ties_on_stable_identity(self) -> None:
        items = [decision(decision_id=name, elapsed_seconds=42) for name in ("d-c", "d-a", "d-b")]
        view = build_queue(items).view()
        self.assertEqual(
            [row.item_id for row in view.rows], ["decision:d-a", "decision:d-b", "decision:d-c"]
        )

    def test_ordering_is_deterministic_across_calls_and_input_order(self) -> None:
        names = ["d-{0}".format(n) for n in range(6)]
        forward = [decision(decision_id=n, elapsed_seconds=7) for n in names]
        backward = list(reversed(forward))
        first = [r.item_id for r in build_queue(forward).view().rows]
        second = [r.item_id for r in build_queue(backward).view().rows]
        third = [r.item_id for r in build_queue(forward).view().rows]
        self.assertEqual(first, second)
        self.assertEqual(first, third)

    def test_thirty_waiting_items_keep_stable_order_with_no_special_path(self) -> None:
        items = [
            decision(decision_id="d-{0:02d}".format(n), elapsed_seconds=(30 - n) * 60)
            for n in range(30)
        ]
        view = build_queue(list(reversed(items))).view()
        self.assertEqual(len(view.rows), 30)
        ages = [row.elapsed_seconds for row in view.rows]
        self.assertEqual(ages, sorted(ages, reverse=True))
        self.assertEqual(view.rows[0].item_id, "decision:d-00")
        self.assertEqual(view.rows[-1].item_id, "decision:d-29")
        # No pagination: everything visible is returned in one list.
        self.assertEqual(len(view.rows), len(build_queue(items).items))

    def test_mixed_kinds_order_by_age_alone(self) -> None:
        queue = build_queue(
            [decision(decision_id="d-old", elapsed_seconds=900)],
            [agent(rail="rail-older", projection=projection(rail="rail-older", elapsed_seconds=1200))],
        )
        view = queue.view(filters=QUEUE_STATES)
        self.assertEqual([row.item_id for row in view.rows], ["agent:rail-older", "decision:d-old"])


# --------------------------------------------------------------------------
# Rows carry nothing extra
# --------------------------------------------------------------------------


class RowContentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.queue = build_queue([decision()], [agent()])
        self.view = self.queue.view(filters=QUEUE_STATES)

    def test_a_row_has_exactly_the_dense_field_set(self) -> None:
        self.assertEqual(
            {f.name for f in fields(QueueRow)},
            {"item_id", "state", "title", "project", "ticket", "elapsed_seconds"},
        )

    def test_no_row_carries_explanation_evidence_lifecycle_detail_or_a_session_id(self) -> None:
        found = _strings(self.view.rows)
        for secret in (EXPLANATION_SECRET, EVIDENCE_SECRET, DETAIL_SECRET, REASON_SECRET,
                       SESSION_SECRET):
            for text in found:
                self.assertNotIn(secret, text)

    def test_rows_carry_the_context_a_dense_list_needs(self) -> None:
        for row in self.view.rows:
            self.assertEqual(row.project, PROJECT)
            self.assertEqual(row.ticket, TICKET)
            self.assertTrue(row.title)
            self.assertIn(row.state, QUEUE_STATES)

    def test_rows_are_immutable(self) -> None:
        with self.assertRaises(Exception):
            self.view.rows[0].title = "rewritten"  # type: ignore[misc]

    def test_state_is_projection_data_and_carries_no_icon_or_type_label(self) -> None:
        for row in self.view.rows:
            self.assertEqual(row.state, row.state.lower())
            self.assertIn(row.state, QUEUE_STATES)
        self.assertNotIn("icon", {f.name for f in fields(QueueRow)})


# --------------------------------------------------------------------------
# Selection and detail
# --------------------------------------------------------------------------


class SelectionAndDetailTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old = decision(decision_id="d-old", elapsed_seconds=900, title="Oldest decision")
        self.mid = decision(decision_id="d-mid", elapsed_seconds=600, title="Middle decision")
        self.new = decision(decision_id="d-new", elapsed_seconds=300, title="Newest decision")
        self.running = agent(rail="rail-running", projection=projection(
            rail="rail-running", state=STATE_RUNNING, elapsed_seconds=450))
        self.queue = build_queue([self.old, self.mid, self.new], [self.running])

    def test_a_waiting_selection_shows_the_explanation_and_bounded_evidence(self) -> None:
        view = self.queue.view(selected_id="decision:d-mid")
        self.assertEqual(view.selected_id, "decision:d-mid")
        self.assertEqual(view.detail.explanation, EXPLANATION_SECRET)
        self.assertEqual(view.detail.evidence, self.mid.evidence)
        self.assertLessEqual(len(view.detail.evidence), MAX_EVIDENCE_REFERENCES)

    def test_the_detail_does_not_repeat_the_title(self) -> None:
        view = self.queue.view(selected_id="decision:d-mid")
        self.assertNotIn("title", {f.name for f in fields(SelectedDetail)})
        for text in _strings(view.detail):
            self.assertNotIn(self.mid.title, text)

    def test_an_operational_selection_fabricates_no_human_decision_explanation(self) -> None:
        view = self.queue.view(filters=(STATE_RUNNING,), selected_id="agent:rail-running")
        self.assertEqual(view.selected_id, "agent:rail-running")
        self.assertIsNone(view.detail.explanation)
        self.assertEqual(view.detail.evidence, ())
        for text in _strings(view.detail):
            self.assertNotIn(DETAIL_SECRET, text)
            self.assertNotIn(SESSION_SECRET, text)

    def test_selection_is_stable_while_the_item_stays_visible(self) -> None:
        first = self.queue.view(selected_id="decision:d-new")
        second = self.queue.view(filters=(STATE_WAITING, STATE_RUNNING),
                                 selected_id=first.selected_id)
        self.assertEqual(second.selected_id, "decision:d-new")

    def test_a_selection_filtered_out_falls_back_to_the_oldest_visible_row(self) -> None:
        view = self.queue.view(filters=(STATE_RUNNING,), selected_id="decision:d-new")
        self.assertEqual(view.selected_id, "agent:rail-running")

    def test_a_removed_selection_falls_back_to_the_oldest_remaining_row(self) -> None:
        smaller = build_queue([self.old, self.mid])
        view = smaller.view(selected_id="decision:d-new")
        self.assertEqual(view.selected_id, "decision:d-old")
        self.assertEqual(view.rows[0].item_id, "decision:d-old")

    def test_no_visible_row_selects_nothing(self) -> None:
        empty = build_queue([], [self.running])
        view = empty.view()
        self.assertEqual(view.rows, ())
        self.assertIsNone(view.selected_id)
        self.assertIsNone(view.detail)

    def test_an_unknown_selection_lands_on_the_oldest_rather_than_erroring(self) -> None:
        view = self.queue.view(selected_id="decision:never-existed")
        self.assertEqual(view.selected_id, "decision:d-old")

    def test_the_default_selection_is_the_oldest_visible_row(self) -> None:
        self.assertEqual(self.queue.view().selected_id, "decision:d-old")

    def test_the_selection_is_always_a_visible_row(self) -> None:
        """An invisible selection would drive a right pane nothing in the list points at."""
        candidates = ["decision:d-old", "decision:d-mid", "decision:d-new",
                      "agent:rail-running", "decision:never-existed", None]
        for size in (1, 2, 3):
            for combo in combinations(QUEUE_STATES, size):
                for chosen in candidates:
                    with self.subTest(filters=combo, selected=chosen):
                        view = self.queue.view(filters=combo, selected_id=chosen)
                        visible = [row.item_id for row in view.rows]
                        if visible:
                            self.assertIn(view.selected_id, visible)
                        else:
                            self.assertIsNone(view.selected_id)
                            self.assertIsNone(view.detail)

    def test_the_detail_field_set_is_exactly_the_right_pane_data(self) -> None:
        self.assertEqual(
            {f.name for f in fields(SelectedDetail)},
            {"item_id", "state", "explanation", "evidence"},
        )


# --------------------------------------------------------------------------
# Elapsed time displays and orders. It never acts.
# --------------------------------------------------------------------------


class ElapsedIsDisplayOnlyTests(unittest.TestCase):
    def test_changing_elapsed_changes_only_the_order_and_the_value(self) -> None:
        young = decision(decision_id="d-a", elapsed_seconds=10)
        older = replace(young, elapsed_seconds=10_000)
        before = build_queue([young, decision(decision_id="d-b", elapsed_seconds=100)]).view()
        after = build_queue([older, decision(decision_id="d-b", elapsed_seconds=100)]).view()

        self.assertEqual([r.item_id for r in before.rows], ["decision:d-b", "decision:d-a"])
        self.assertEqual([r.item_id for r in after.rows], ["decision:d-a", "decision:d-b"])
        self.assertEqual({r.state for r in before.rows}, {r.state for r in after.rows})
        self.assertEqual(before.filters, after.filters)

    def test_age_never_changes_state_or_filtering(self) -> None:
        for seconds in (0, 1, 3600, 86_400, 86_400 * 365):
            with self.subTest(seconds=seconds):
                entry = decision(elapsed_seconds=seconds)
                self.assertEqual(entry.state, STATE_WAITING)
                view = build_queue([entry]).view()
                self.assertEqual(len(view.rows), 1)
                self.assertEqual(view.rows[0].elapsed_seconds, seconds)

    def test_a_huge_age_produces_no_escalation_surface(self) -> None:
        view = build_queue([decision(elapsed_seconds=10**9)]).view()
        self.assertEqual({f.name for f in fields(type(view))},
                         {"rows", "filters", "selected_id", "detail"})


# --------------------------------------------------------------------------
# Purity
# --------------------------------------------------------------------------


class PurityTests(unittest.TestCase):
    """The module was handed everything it knows."""

    def setUp(self) -> None:
        self.source = Path(queue_module.__file__).read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)

    def test_the_module_imports_only_stdlib_typing_and_the_accepted_lifecycle(self) -> None:
        allowed = {"dataclasses", "typing", "__future__", ".session_lifecycle"}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name, allowed, alias.name)
            elif isinstance(node, ast.ImportFrom):
                name = ("." * (node.level or 0)) + (node.module or "")
                self.assertIn(name, allowed, name)

    def _called_names(self):
        """Every callable this module actually invokes.

        Scanned from the AST, not the text: prose in a comment is not behavior,
        and a substring match on source would confuse the two.
        """
        called = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    called.add(func.id)
                elif isinstance(func, ast.Attribute):
                    called.add(func.attr)
                else:
                    called.add(type(func).__name__)
        return called

    def test_the_module_calls_only_local_helpers_and_pure_builtins(self) -> None:
        """Pinned exactly, so a new call has to be justified rather than noticed later."""
        self.assertEqual(
            self._called_names(),
            {
                # its own constructors and helpers
                "DecisionQueue", "QueueError", "QueueRow", "QueueView", "SelectedDetail",
                "_detail", "_elapsed", "_normalize_filters", "_text", "_visible",
                "__init__", "super", "dataclass",
                # pure builtins
                "add", "append", "format", "join", "len", "set", "sorted", "strip",
                "tuple", "type",
            },
        )

    def test_the_module_calls_no_side_effecting_or_clock_surface(self) -> None:
        called = self._called_names()
        for surface in ("open", "exec", "eval", "compile", "__import__", "run", "Popen",
                        "write", "write_text", "read_text", "mkdir", "unlink", "connect",
                        "urlopen", "get", "post", "sleep", "Thread", "Timer",
                        "now", "utcnow", "today", "monotonic", "perf_counter",
                        "elapsed_seconds", "observe_session", "publish", "commit",
                        "launch_session", "stop_session", "authorize", "build_snapshot"):
            self.assertNotIn(surface, called, surface)

    def test_the_public_surface_offers_no_action(self) -> None:
        for name in [n for n in dir(queue_module) if not n.startswith("_")]:
            lowered = name.lower()
            for forbidden in ("send", "submit", "respond", "publish", "write", "escalate",
                              "review", "retry", "start", "stop", "launch"):
                self.assertNotIn(forbidden, lowered, name)

    def test_projecting_twice_returns_the_same_answer(self) -> None:
        queue = build_queue([decision()], [agent()])
        first = queue.view(filters=QUEUE_STATES)
        second = queue.view(filters=QUEUE_STATES)
        self.assertEqual(first, second)

    def test_the_queue_holds_no_mutable_view_of_its_items(self) -> None:
        items = [decision()]
        queue = build_queue(items)
        items.append(decision(decision_id="d-2"))
        self.assertEqual(len(queue.items), 1)


if __name__ == "__main__":
    unittest.main()

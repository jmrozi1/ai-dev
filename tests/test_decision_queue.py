"""`decision_queue` projects genuine human decisions first, and decides nothing itself."""

from __future__ import annotations

from dataclasses import MISSING, fields, replace
from itertools import combinations
from pathlib import Path
from typing import List, Optional
import ast
import unittest

from ai_dev_flow import decision_queue as queue_module
from ai_dev_flow.attention_projection import (
    ACTIVITY_BLOCKED,
    ACTIVITY_DISCONNECTED_RECOVERY,
    ACTIVITY_EXECUTOR_WORKING,
    OWNER_AGENT,
    OWNER_HUMAN,
)
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
        activity=ACTIVITY_BLOCKED,
        attention_owner=OWNER_HUMAN,
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
    base = dict(
        project=PROJECT, ticket=TICKET, rail=rail, title="Implement the seam",
        activity=(
            ACTIVITY_EXECUTOR_WORKING if proj.state == STATE_RUNNING
            else ACTIVITY_DISCONNECTED_RECOVERY
        ),
        attention_owner=OWNER_AGENT,
    )
    base.update(overrides)
    return OperationalAgent(projection=proj, **base)


def decision_id_for(decision_id, *, project=PROJECT, ticket=TICKET, rail="issue-55-some-rail"):
    """The identity a decision with these routing facts must have."""
    return PendingDecision(
        decision_id=decision_id, project=project, ticket=ticket, rail=rail,
        raised_at="raised-x", title="t", explanation="e", elapsed_seconds=0,
        activity=ACTIVITY_BLOCKED, attention_owner=OWNER_HUMAN,
    ).item_id


def agent_id_for(rail, *, project=PROJECT, ticket=TICKET):
    """The identity an operational agent with these routing facts must have."""
    return OperationalAgent(
        project=project, ticket=ticket, rail=rail, title="t",
        projection=SessionProjection(
            state=STATE_RUNNING, reason="r", detail="d", session_id="s",
            rail=rail, elapsed_seconds=0,
        ),
        activity=ACTIVITY_EXECUTOR_WORKING, attention_owner=OWNER_AGENT,
    ).item_id


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
        self.assertEqual([row.item_id for row in view.rows], [decision_id_for("d-1")])

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

    def test_the_waiting_set_and_the_human_owned_set_are_required_to_be_identical(self) -> None:
        """Defence in depth, exercised directly rather than left implied.

        Each input type already refuses the other kind's owner, so the only way to
        reach the set check today is to bypass construction -- which is exactly the
        future this check exists for: one loosened local rule and the property the
        product owes a person would be nobody's.
        """
        waiting_but_not_human = decision()
        object.__setattr__(waiting_but_not_human, "attention_owner", OWNER_AGENT)
        with self.assertRaises(QueueError) as caught:
            build_queue([waiting_but_not_human])
        self.assertEqual(caught.exception.reason, queue_module.REASON_WAITING_NOT_HUMAN_OWNED)
        self.assertIn(waiting_but_not_human.item_id, caught.exception.detail)

        human_but_not_waiting = agent()
        object.__setattr__(human_but_not_waiting, "attention_owner", OWNER_HUMAN)
        with self.assertRaises(QueueError) as caught:
            build_queue([], [human_but_not_waiting])
        self.assertEqual(caught.exception.reason, queue_module.REASON_WAITING_NOT_HUMAN_OWNED)

    def test_a_matching_pair_of_owners_and_states_builds_normally(self) -> None:
        queue = build_queue([decision()], [agent()])
        self.assertEqual(
            {entry.item_id for entry in queue.items if entry.state == STATE_WAITING},
            {entry.item_id for entry in queue.items
             if entry.attention_owner == OWNER_HUMAN},
        )

    def test_an_operational_activity_that_contradicts_its_state_is_refused(self) -> None:
        with self.assertRaises(QueueError) as caught:
            agent(activity=ACTIVITY_DISCONNECTED_RECOVERY)
        self.assertEqual(
            caught.exception.reason, queue_module.REASON_ACTIVITY_CONTRADICTS_STATE
        )
        with self.assertRaises(QueueError) as caught:
            agent(projection=projection(state=STATE_DISCONNECTED),
                  activity=ACTIVITY_EXECUTOR_WORKING)
        self.assertEqual(
            caught.exception.reason, queue_module.REASON_ACTIVITY_CONTRADICTS_STATE
        )

    def test_an_operational_item_may_never_claim_the_blocked_activity(self) -> None:
        """`blocked` describes a Waiting row, and an operational row is never one."""
        with self.assertRaises(QueueError) as caught:
            agent(activity=ACTIVITY_BLOCKED)
        self.assertEqual(
            caught.exception.reason, queue_module.REASON_ACTIVITY_CONTRADICTS_STATE
        )

    def test_a_decision_may_carry_any_modeled_activity(self) -> None:
        """Its state says who must act; its activity says what the work is doing."""
        for activity in (ACTIVITY_BLOCKED, ACTIVITY_DISCONNECTED_RECOVERY,
                         ACTIVITY_EXECUTOR_WORKING):
            with self.subTest(activity=activity):
                self.assertEqual(decision(activity=activity).activity, activity)

    def test_transport_evidence_on_an_operational_item_is_bounded(self) -> None:
        too_many = tuple(
            EvidenceReference(label="live session", locator="s{0}".format(index))
            for index in range(MAX_EVIDENCE_REFERENCES + 1)
        )
        with self.assertRaises(QueueError) as caught:
            agent(evidence=too_many)
        self.assertEqual(caught.exception.reason, queue_module.REASON_TOO_MUCH_EVIDENCE)

    def test_the_input_field_sets_have_nowhere_to_put_a_payload(self) -> None:
        self.assertEqual(
            {f.name for f in fields(PendingDecision)},
            {"decision_id", "project", "ticket", "rail", "raised_at", "title", "explanation",
             "elapsed_seconds", "activity", "attention_owner", "evidence",
             # D8's actionable half, and the reason there is none. Exactly two
             # fields, and both only on the human-owned kind.
             "blocker", "blocker_unavailable"},
        )
        self.assertEqual(
            {f.name for f in fields(OperationalAgent)},
            {"project", "ticket", "rail", "title", "projection", "activity",
             "attention_owner", "evidence"},
        )
        self.assertEqual({f.name for f in fields(EvidenceReference)}, {"label", "locator"})


# --------------------------------------------------------------------------
# Identity is the complete durable routing scope, encoded so it cannot collide
# --------------------------------------------------------------------------


class CompositeIdentityTests(unittest.TestCase):
    """One combined queue spans projects and tickets; a rail slug is unique only inside its own."""

    def test_the_same_rail_slug_in_two_tickets_stays_distinct_and_selectable(self) -> None:
        here = agent(rail="shared-slug", ticket="issue-55")
        there = agent(rail="shared-slug", ticket="issue-56")
        self.assertNotEqual(here.item_id, there.item_id)

        view = build_queue([], [here, there]).view(filters=(STATE_RUNNING,))
        self.assertEqual(len(view.rows), 2)
        for entry in (here, there):
            with self.subTest(ticket=entry.ticket):
                selected = build_queue([], [here, there]).view(
                    filters=(STATE_RUNNING,), selected_id=entry.item_id)
                self.assertEqual(selected.selected_id, entry.item_id)

    def test_the_same_rail_slug_in_two_projects_stays_distinct_and_selectable(self) -> None:
        here = agent(rail="shared-slug", project="ai-dev")
        there = agent(rail="shared-slug", project="other-product")
        self.assertNotEqual(here.item_id, there.item_id)

        queue = build_queue([], [here, there])
        self.assertEqual(len(queue.view(filters=(STATE_RUNNING,)).rows), 2)
        for entry in (here, there):
            with self.subTest(project=entry.project):
                view = queue.view(filters=(STATE_RUNNING,), selected_id=entry.item_id)
                self.assertEqual(view.selected_id, entry.item_id)

    def test_the_same_decision_id_across_rails_tickets_and_projects_stays_distinct(self) -> None:
        variants = [
            decision(decision_id="shared", rail="rail-a"),
            decision(decision_id="shared", rail="rail-b"),
            decision(decision_id="shared", rail="rail-a", ticket="issue-56"),
            decision(decision_id="shared", rail="rail-a", project="other-product"),
        ]
        ids = [entry.item_id for entry in variants]
        self.assertEqual(len(set(ids)), len(ids))

        queue = build_queue(variants)
        self.assertEqual(len(queue.view().rows), len(variants))
        for entry in variants:
            with self.subTest(item=entry.item_id):
                self.assertEqual(queue.view(selected_id=entry.item_id).selected_id, entry.item_id)

    def test_adversarial_components_defeat_a_naive_join_but_not_this_encoding(self) -> None:
        """Each pair collapses to one string under separator joining. Proven, not asserted."""
        pairs = (
            (agent(rail="r|x"), agent(ticket="issue-55|r", rail="x")),
            (decision(rail="r|x", decision_id="d"), decision(rail="r", decision_id="x|d")),
            (decision(project="ai-dev|p", ticket="t"), decision(project="ai-dev", ticket="p|t")),
        )

        def naive(entry):
            waiting = entry.state == STATE_WAITING
            kind = queue_module.KIND_DECISION if waiting else queue_module.KIND_AGENT
            parts = [kind, entry.project, entry.ticket, entry.rail]
            if waiting:
                parts.append(entry.decision_id)
            return "|".join(parts)

        for left, right in pairs:
            with self.subTest(left=left.item_id, right=right.item_id):
                self.assertEqual(naive(left), naive(right), "fixture does not defeat a naive join")
                self.assertNotEqual(left.item_id, right.item_id)
                if left.state == STATE_WAITING:
                    queue = build_queue([left, right], [])
                else:
                    queue = build_queue([], [left, right])
                self.assertEqual(len(queue.view(filters=QUEUE_STATES).rows), 2)

    def test_components_imitating_the_encodings_own_shape_stay_distinct(self) -> None:
        lookalikes = (
            agent(rail="1:a"),
            agent(rail="a"),
            agent(rail="2:ab"),
            agent(ticket="2:ab", rail="ab"),
            decision(decision_id="8:decision"),
            decision(decision_id="decision"),
        )
        ids = [entry.item_id for entry in lookalikes]
        self.assertEqual(len(set(ids)), len(ids))

    def test_a_decision_and_an_operational_agent_cannot_collide(self) -> None:
        shared_rail = "same-rail"
        d = decision(decision_id="x", rail=shared_rail)
        a = agent(rail=shared_rail)
        self.assertNotEqual(d.item_id, a.item_id)

        # Even when a component is literally the other kind's name.
        self.assertNotEqual(
            decision(decision_id="x", rail="agent").item_id,
            agent(rail="decision").item_id,
        )
        queue = build_queue([d], [a])
        self.assertEqual(len(queue.view(filters=QUEUE_STATES).rows), 2)

    def test_selection_and_detail_use_exactly_the_composite_identity(self) -> None:
        d = decision(decision_id="d-sel", rail="rail-sel")
        queue = build_queue([d])
        view = queue.view(selected_id=d.item_id)
        self.assertEqual(view.selected_id, d.item_id)
        self.assertEqual(view.detail.item_id, d.item_id)
        self.assertEqual(view.rows[0].item_id, d.item_id)

        # A local id alone no longer selects anything; it falls back to the oldest.
        partial = queue.view(selected_id="d-sel")
        self.assertEqual(partial.selected_id, d.item_id)

    def test_identity_is_stable_across_every_non_routing_change(self) -> None:
        base = decision(decision_id="stable", rail="rail-stable")
        for field, value in (("elapsed_seconds", 99_999), ("title", "A different title"),
                             ("explanation", "A different explanation"),
                             ("raised_at", "raised-9999"),
                             ("evidence", (EvidenceReference(label="other", locator="elsewhere"),))):
            with self.subTest(field=field):
                self.assertEqual(decision(decision_id="stable", rail="rail-stable",
                                          **{field: value}).item_id, base.item_id)

        agent_base = agent(rail="rail-stable-agent")
        for state in (STATE_RUNNING, STATE_DISCONNECTED):
            for seconds in (0, 5_000):
                with self.subTest(state=state, seconds=seconds):
                    other = agent(rail="rail-stable-agent", projection=projection(
                        rail="rail-stable-agent", state=state, elapsed_seconds=seconds,
                        session_id="a-completely-different-session", detail="different detail",
                        reason="different reason"))
                    self.assertEqual(other.item_id, agent_base.item_id)

    def test_identity_carries_no_session_provider_or_detail_content(self) -> None:
        entry = agent()
        for secret in (SESSION_SECRET, DETAIL_SECRET, REASON_SECRET):
            self.assertNotIn(secret, entry.item_id)
        d = decision()
        for secret in (EXPLANATION_SECRET, EVIDENCE_SECRET):
            self.assertNotIn(secret, d.item_id)
        self.assertNotIn(d.title, d.item_id)
        self.assertNotIn(str(d.elapsed_seconds), d.item_id.split("|")[0])

    def test_an_exact_duplicate_routing_identity_is_still_refused(self) -> None:
        with self.assertRaises(QueueError) as caught:
            build_queue([decision(decision_id="dup", rail="r"), decision(decision_id="dup", rail="r")])
        self.assertEqual(caught.exception.reason, queue_module.REASON_DUPLICATE_ITEM)

        with self.assertRaises(QueueError) as caught:
            build_queue([], [agent(rail="r"), agent(rail="r")])
        self.assertEqual(caught.exception.reason, queue_module.REASON_DUPLICATE_ITEM)

    def test_the_same_local_id_in_a_different_scope_is_not_a_duplicate(self) -> None:
        queue = build_queue(
            [decision(decision_id="dup", rail="r"),
             decision(decision_id="dup", rail="r", ticket="issue-56"),
             decision(decision_id="dup", rail="r", project="other-product")],
            [agent(rail="r"), agent(rail="r", ticket="issue-56")],
        )
        self.assertEqual(len(queue.view(filters=QUEUE_STATES).rows), 5)

    def test_the_encoding_is_decodable_and_therefore_injective(self) -> None:
        """Round-trips back to the exact routing tuple it was built from."""
        def decode(item_id):
            parts = []
            rest = item_id
            while rest:
                head, _, rest = rest.partition(":")
                length = int(head)
                parts.append(rest[:length])
                rest = rest[length + 1:]
            return tuple(parts)

        d = decision(decision_id="d|1", rail="r:2", ticket="t|3")
        self.assertEqual(decode(d.item_id),
                         (queue_module.KIND_DECISION, d.project, d.ticket, d.rail, d.decision_id))
        a = agent(rail="r|4")
        self.assertEqual(decode(a.item_id),
                         (queue_module.KIND_AGENT, a.project, a.ticket, a.rail))


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
            STATE_WAITING: decision_id_for("d-wait"),
            STATE_RUNNING: agent_id_for("rail-running"),
            STATE_DISCONNECTED: agent_id_for("rail-gone"),
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
            [row.item_id for row in view.rows], [decision_id_for("d-a"), decision_id_for("d-b"), decision_id_for("d-c")]
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
        self.assertEqual(view.rows[0].item_id, decision_id_for("d-00"))
        self.assertEqual(view.rows[-1].item_id, decision_id_for("d-29"))
        # No pagination: everything visible is returned in one list.
        self.assertEqual(len(view.rows), len(build_queue(items).items))

    def test_mixed_kinds_order_by_age_alone(self) -> None:
        queue = build_queue(
            [decision(decision_id="d-old", elapsed_seconds=900)],
            [agent(rail="rail-older", projection=projection(rail="rail-older", elapsed_seconds=1200))],
        )
        view = queue.view(filters=QUEUE_STATES)
        self.assertEqual([row.item_id for row in view.rows], [agent_id_for("rail-older"), decision_id_for("d-old")])


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
        view = self.queue.view(selected_id=decision_id_for("d-mid"))
        self.assertEqual(view.selected_id, decision_id_for("d-mid"))
        self.assertEqual(view.detail.explanation, EXPLANATION_SECRET)
        self.assertEqual(view.detail.evidence, self.mid.evidence)
        self.assertLessEqual(len(view.detail.evidence), MAX_EVIDENCE_REFERENCES)

    def test_the_detail_does_not_repeat_the_title(self) -> None:
        view = self.queue.view(selected_id=decision_id_for("d-mid"))
        self.assertNotIn("title", {f.name for f in fields(SelectedDetail)})
        for text in _strings(view.detail):
            self.assertNotIn(self.mid.title, text)

    def test_an_operational_selection_fabricates_no_human_decision_explanation(self) -> None:
        view = self.queue.view(filters=(STATE_RUNNING,), selected_id=agent_id_for("rail-running"))
        self.assertEqual(view.selected_id, agent_id_for("rail-running"))
        self.assertIsNone(view.detail.explanation)
        self.assertEqual(view.detail.evidence, ())
        for text in _strings(view.detail):
            self.assertNotIn(DETAIL_SECRET, text)
            self.assertNotIn(SESSION_SECRET, text)

    def test_selection_is_stable_while_the_item_stays_visible(self) -> None:
        first = self.queue.view(selected_id=decision_id_for("d-new"))
        second = self.queue.view(filters=(STATE_WAITING, STATE_RUNNING),
                                 selected_id=first.selected_id)
        self.assertEqual(second.selected_id, decision_id_for("d-new"))

    def test_a_selection_filtered_out_falls_back_to_the_oldest_visible_row(self) -> None:
        view = self.queue.view(filters=(STATE_RUNNING,), selected_id=decision_id_for("d-new"))
        self.assertEqual(view.selected_id, agent_id_for("rail-running"))

    def test_a_removed_selection_falls_back_to_the_oldest_remaining_row(self) -> None:
        smaller = build_queue([self.old, self.mid])
        view = smaller.view(selected_id=decision_id_for("d-new"))
        self.assertEqual(view.selected_id, decision_id_for("d-old"))
        self.assertEqual(view.rows[0].item_id, decision_id_for("d-old"))

    def test_no_visible_row_selects_nothing(self) -> None:
        empty = build_queue([], [self.running])
        view = empty.view()
        self.assertEqual(view.rows, ())
        self.assertIsNone(view.selected_id)
        self.assertIsNone(view.detail)

    def test_an_unknown_selection_lands_on_the_oldest_rather_than_erroring(self) -> None:
        view = self.queue.view(selected_id=decision_id_for("never-existed"))
        self.assertEqual(view.selected_id, decision_id_for("d-old"))

    def test_the_default_selection_is_the_oldest_visible_row(self) -> None:
        self.assertEqual(self.queue.view().selected_id, decision_id_for("d-old"))

    def test_the_selection_is_always_a_visible_row(self) -> None:
        """An invisible selection would drive a right pane nothing in the list points at."""
        candidates = [decision_id_for("d-old"), decision_id_for("d-mid"), decision_id_for("d-new"),
                      agent_id_for("rail-running"), decision_id_for("never-existed"), None]
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
            {"item_id", "state", "activity", "attention_owner", "explanation", "evidence",
             # Three routing facts D8 requires an item to state, on both kinds,
             # plus the actionable half, which only a decision ever carries.
             "project", "ticket", "rail", "blocker", "blocker_unavailable"},
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

        self.assertEqual([r.item_id for r in before.rows], [decision_id_for("d-b"), decision_id_for("d-a")])
        self.assertEqual([r.item_id for r in after.rows], [decision_id_for("d-a"), decision_id_for("d-b")])
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
        # `.attention_projection` is the checkpoint-7 model, and it is pure by the
        # same standard this module is: no file, no clock, no process, no network.
        # Its own suite pins that, so admitting it here does not widen what this
        # module can reach.
        allowed = {
            "dataclasses", "typing", "__future__",
            ".session_lifecycle", ".attention_projection",
        }
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
                "_activity", "_blocker", "_detail", "_elapsed", "_identity",
                "_normalize_filters",
                "_owner", "_references", "_require_waiting_is_the_human_owned_set",
                "_text", "_visible",
                "__init__", "super", "dataclass",
                # the accepted vocabulary checks, which raise and return and do
                # nothing else
                "require_activity", "require_attention_owner",
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


# --------------------------------------------------------------------------
# D8: a human-owned item is actionable on its own, or says which part is missing
# --------------------------------------------------------------------------


def a_blocker(**overrides) -> queue_module.ActionableBlocker:
    base = dict(
        kind="permission",
        what_failed="publishing the executor handoff to the coordination remote",
        agent="executor",
        missing_capability="push access to jmrozi1/ai-dev-control-plane for this host key",
        human_change="add this host's key as a deploy key with write access",
        state_changed=True,
        next_action="re-run the publish step; the checkpoint is already committed",
    )
    base.update(overrides)
    return queue_module.ActionableBlocker(**base)


class ActionableBlockerShapeTests(unittest.TestCase):
    """The six failure facts arrive complete or not at all."""

    def test_every_field_is_required_with_no_default(self) -> None:
        """A blocker cannot be constructed half-described, so none can exist.

        Asserted over the dataclass rather than by omitting one field in a call,
        because a default added later would make the omission test pass while the
        property it stands for was gone.
        """
        for entry in fields(queue_module.ActionableBlocker):
            with self.subTest(field=entry.name):
                self.assertIs(entry.default, MISSING)
                self.assertIs(entry.default_factory, MISSING)

    def test_the_field_set_is_exactly_d8_s_six(self) -> None:
        """Project, ticket and rail are not here: the item states them once."""
        self.assertEqual(
            {f.name for f in fields(queue_module.ActionableBlocker)},
            {"kind", "what_failed", "agent", "missing_capability", "human_change",
             "state_changed", "next_action"},
        )

    def test_each_of_the_five_kinds_is_accepted_and_a_sixth_is_not(self) -> None:
        for kind in queue_module.BLOCKER_KINDS:
            with self.subTest(kind=kind):
                self.assertEqual(a_blocker(kind=kind).kind, kind)
        self.assertEqual(len(queue_module.BLOCKER_KINDS), 5)
        for invalid in ("network", "", None, "Permission", 1):
            with self.subTest(kind=invalid):
                with self.assertRaises(QueueError) as caught:
                    a_blocker(kind=invalid)
                self.assertEqual(
                    caught.exception.reason, queue_module.REASON_UNSUPPORTED_BLOCKER_KIND
                )

    def test_state_changed_is_an_exact_boolean_and_nothing_that_reads_like_one(self) -> None:
        """"It may have changed" is the one answer a person cannot act on."""
        for value in (1, 0, "true", "false", "unknown", None, "", []):
            with self.subTest(value=value):
                with self.assertRaises(QueueError) as caught:
                    a_blocker(state_changed=value)
                self.assertEqual(
                    caught.exception.reason, queue_module.REASON_BLOCKER_STATE_NOT_EXPLICIT
                )
        self.assertIs(a_blocker(state_changed=True).state_changed, True)
        self.assertIs(a_blocker(state_changed=False).state_changed, False)

    def test_every_text_field_must_be_present_bounded_and_exact(self) -> None:
        text_fields = (
            "what_failed", "agent", "missing_capability", "human_change", "next_action",
        )
        for name in text_fields:
            for value in (None, "", "   ", 7):
                with self.subTest(field=name, value=value):
                    with self.assertRaises(QueueError) as caught:
                        a_blocker(**{name: value})
                    self.assertEqual(
                        caught.exception.reason, queue_module.REASON_INVALID_TEXT
                    )
            with self.subTest(field=name, value="too long"):
                with self.assertRaises(QueueError) as caught:
                    a_blocker(**{name: "x" * (queue_module.MAX_BLOCKER_TEXT + 1)})
                self.assertEqual(
                    caught.exception.reason, queue_module.REASON_TEXT_TOO_LONG
                )

    def test_the_bound_and_the_kinds_match_the_publisher_s_own(self) -> None:
        """Restated constants drift. This is the thing that notices.

        `decision_queue` is pure and will not import the control plane to borrow
        an integer, so the two definitions are checked against each other here
        instead of one being derived from the other.
        """
        from ai_dev_flow import control_plane

        self.assertEqual(
            queue_module.MAX_BLOCKER_TEXT, control_plane.MAX_DECISION_BLOCKER_STRING
        )
        self.assertEqual(
            set(queue_module.BLOCKER_KINDS), set(control_plane.DECISION_BLOCKER_KINDS)
        )
        # And the key names the two sides use for one field, which is the other
        # way this pair could silently stop describing the same thing.
        self.assertEqual(
            {"kind", "whatFailed", "missingCapability", "humanChange", "stateChanged",
             "nextAction"},
            set(control_plane.DECISION_BLOCKER_ALLOWED_KEYS),
        )


class ActionableItemTests(unittest.TestCase):
    """Where D8's nine live on an item, and where they refuse to live."""

    def test_a_decision_carries_the_blocker_through_to_its_detail(self) -> None:
        blocker = a_blocker()
        item = decision(blocker=blocker)
        view = build_queue(decisions=[item]).view(selected_id=item.item_id)

        detail = view.detail
        self.assertIs(detail.blocker, blocker)
        self.assertIsNone(detail.blocker_unavailable)
        # All nine, read off one selected item and nothing else.
        self.assertEqual(detail.project, PROJECT)
        self.assertEqual(detail.ticket, TICKET)
        self.assertEqual(detail.rail, "issue-55-some-rail")
        self.assertEqual(detail.blocker.what_failed, blocker.what_failed)
        self.assertEqual(detail.blocker.agent, "executor")
        self.assertEqual(detail.blocker.missing_capability, blocker.missing_capability)
        self.assertEqual(detail.blocker.human_change, blocker.human_change)
        self.assertIs(detail.blocker.state_changed, True)
        self.assertEqual(detail.blocker.next_action, blocker.next_action)

    def test_a_decision_without_a_blocker_carries_neither_half(self) -> None:
        """Not every question put to a person is a failure report."""
        item = decision()
        detail = build_queue(decisions=[item]).view(selected_id=item.item_id).detail
        self.assertIsNone(detail.blocker)
        self.assertIsNone(detail.blocker_unavailable)
        # It is still actionable: the routing facts and the published prose are
        # all there, which is what a straight question needs.
        self.assertEqual((detail.project, detail.ticket, detail.rail),
                         (PROJECT, TICKET, "issue-55-some-rail"))
        self.assertEqual(detail.explanation, EXPLANATION_SECRET)

    def test_an_unsourced_blocker_is_named_rather_than_invented(self) -> None:
        notice = "blocker-agent-unsourced: the rail publishes no role assignment."
        item = decision(blocker_unavailable=notice)
        detail = build_queue(decisions=[item]).view(selected_id=item.item_id).detail
        self.assertIsNone(detail.blocker)
        self.assertEqual(detail.blocker_unavailable, notice)

    def test_an_item_may_not_both_carry_a_blocker_and_report_none(self) -> None:
        with self.assertRaises(QueueError) as caught:
            decision(blocker=a_blocker(), blocker_unavailable="something was missing")
        self.assertEqual(
            caught.exception.reason, queue_module.REASON_BLOCKER_DOUBLE_ANSWER
        )

    def test_the_unavailability_notice_is_bounded_exact_text(self) -> None:
        for value in ("", "  ", 7, b"bytes"):
            with self.subTest(value=value):
                with self.assertRaises(QueueError):
                    decision(blocker_unavailable=value)
        with self.assertRaises(QueueError) as caught:
            decision(blocker_unavailable="x" * (queue_module.MAX_BLOCKER_NOTICE + 1))
        self.assertEqual(caught.exception.reason, queue_module.REASON_TEXT_TOO_LONG)

    def test_a_blocker_must_be_the_accepted_type_and_not_a_lookalike(self) -> None:
        """A dict of the right shape is not a validated blocker."""
        with self.assertRaises(QueueError) as caught:
            decision(blocker={
                "kind": "permission", "what_failed": "x", "agent": "executor",
                "missing_capability": "x", "human_change": "x",
                "state_changed": True, "next_action": "x",
            })
        self.assertEqual(caught.exception.reason, queue_module.REASON_INVALID_BLOCKER)

    def test_an_operational_item_has_nowhere_to_put_a_human_attention_field(self) -> None:
        """Proof 10, by construction rather than by omission.

        An agent-owned item does not merely happen to carry no blocker: there is
        no field on `OperationalAgent` to carry one, so no future caller can put
        human-attention content on a row whose next action belongs to an agent.
        """
        names = {f.name for f in fields(OperationalAgent)}
        self.assertNotIn("blocker", names)
        self.assertNotIn("blocker_unavailable", names)
        with self.assertRaises(TypeError):
            agent(blocker=a_blocker())

        item = agent()
        detail = build_queue(agents=[item]).view(
            filters=QUEUE_STATES, selected_id=item.item_id
        ).detail
        self.assertIsNone(detail.blocker)
        self.assertIsNone(detail.blocker_unavailable)
        self.assertEqual(detail.attention_owner, OWNER_AGENT)

    def test_a_blocked_or_disconnected_agent_item_stays_agent_owned(self) -> None:
        """Being stuck is not the same as being human work."""
        stuck = agent(
            rail="issue-55-stuck-rail",
            projection=projection(rail="issue-55-stuck-rail", state=STATE_DISCONNECTED),
            activity=ACTIVITY_DISCONNECTED_RECOVERY,
        )
        queue = build_queue(agents=[stuck])
        detail = queue.view(filters=QUEUE_STATES, selected_id=stuck.item_id).detail
        self.assertEqual(detail.attention_owner, OWNER_AGENT)
        self.assertIsNone(detail.blocker)
        self.assertIsNone(detail.blocker_unavailable)
        # And it is still absent from the default Waiting view.
        self.assertEqual(queue.view().rows, ())

    def test_the_row_gained_nothing_at_all(self) -> None:
        """Proof 12. D8 is detail-pane content, and rows stay dense."""
        before = {f.name for f in fields(QueueRow)}
        item = decision(blocker=a_blocker())
        view = build_queue(decisions=[item]).view()
        self.assertEqual(before, {"item_id", "state", "title", "project", "ticket",
                                  "elapsed_seconds"})
        row = view.rows[0]
        self.assertFalse(hasattr(row, "blocker"))
        self.assertFalse(hasattr(row, "rail"))
        # None of the blocker's own text is reachable from a row, including
        # through the fields a row does have.
        printed = "|".join(str(getattr(row, name)) for name in before)
        for secret in (a_blocker().what_failed, a_blocker().human_change,
                       a_blocker().next_action, a_blocker().missing_capability):
            self.assertNotIn(secret, printed)

    def test_waiting_is_still_exactly_the_human_owned_set_with_blockers_present(self) -> None:
        """Proof 11, non-vacuously: one of each kind in one queue."""
        blocked = decision(blocker=a_blocker())
        working = agent()
        queue = build_queue(decisions=[blocked], agents=[working])

        waiting = {row.item_id for row in queue.view().rows}
        human = {
            entry.item_id for entry in queue.items
            if entry.attention_owner == OWNER_HUMAN
        }
        self.assertEqual(waiting, human)
        self.assertEqual(waiting, {blocked.item_id})
        self.assertEqual(
            {row.item_id for row in queue.view(filters=QUEUE_STATES).rows},
            {blocked.item_id, working.item_id},
        )

    def test_a_detail_must_state_its_routing_and_cannot_be_built_without_it(self) -> None:
        with self.assertRaises(TypeError):
            SelectedDetail(
                item_id="x", state=STATE_WAITING,
                activity=ACTIVITY_BLOCKED, attention_owner=OWNER_HUMAN,
            )
        for missing in ("project", "ticket", "rail"):
            base = dict(
                item_id="x", state=STATE_WAITING, project=PROJECT, ticket=TICKET,
                rail="issue-55-some-rail", activity=ACTIVITY_BLOCKED,
                attention_owner=OWNER_HUMAN,
            )
            base[missing] = ""
            with self.subTest(field=missing):
                with self.assertRaises(QueueError):
                    SelectedDetail(**base)

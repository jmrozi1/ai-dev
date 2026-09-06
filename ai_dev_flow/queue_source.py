"""Durable authority in, decision-queue inputs out. Refuses rather than guesses."""

from __future__ import annotations

# `decision_queue` is pure: everything it knows was handed to it. That purity is
# what makes it trustworthy, and it is also what left a hole -- nothing actually
# handed it anything, so an empty screen meant "nothing is wired up" rather than
# "there is nothing for you to do". Those two are indistinguishable to a person
# looking at the screen, and only one of them is safe to believe.
#
# This module is the missing half, and its whole job is to make the difference
# provable. It reads the durable control plane and the accepted lifecycle, proves
# what it read is authoritative, and either returns a queue built from exact facts
# or refuses with a reason. It never returns a partial answer.
#
# Four rules it inherits rather than invents.
#
# First, Waiting comes from the explicit published decision record and nothing
# else. Not from a blocked rail, not from an error string, not from elapsed time,
# not from a missing process, not from prose that reads like a question. The
# control plane now carries that record as a first-class artifact; if it is
# absent, there is no Waiting item, whatever else the rail says about itself.
#
# Second, operational rows come from `observe_session` over exact facts. The
# lifecycle also projects a Waiting state, and that projection answers a different
# question -- "is this session progressing" rather than "is a person needed". It
# is never converted into an operational row, and never into a decision either.
#
# Third, the failure boundary is here, before `QueueView` exists. A `QueueView`
# has no way to say "the sources were unreadable", and giving it one would let a
# refusal render as an empty list -- the exact confusion this module exists to
# remove. So stale, ambiguous, contradictory, unreadable, and partially readable
# authority all raise before a single row is constructed.
#
# Fourth, this reads and refuses. It writes nothing, persists nothing, authorizes
# nothing, makes no provider or network call, and creates no second authority.
# `DecisionQueue` remains the only thing that orders, filters, selects, or details.
#
# Fifth, the visible unit of operational work is the durable rail, not the binding.
# This module used to emit one row per live binding, which made the screen a
# picture of transport: a Claude session id, a resident worker name, a pid. Rotate
# the worker and the same piece of work grew a second row; leave an idle transport
# resident lying around and it got a row for existing. The rail is what a person
# tracks, so the rail is the row, and the sessions underneath it are reconciled
# into two independent facts -- what the work is doing, and who owes it attention --
# by the pure `attention_projection` model. Session identity survives only as
# bounded Details evidence.
#
# Sixth, ownership is proved exactly once, by exactly the accepted primitive.
# `session_lifecycle.ownership_evidence` is the same proof the accepted concurrency
# reconciler consumes for the ceiling, so a row drawn Running and a count that
# includes it rest on one piece of evidence rather than two that could disagree.
# There is no second registry, no second store, no pid lookup, and no cache.
#
# Seventh, and this is what makes the sixth rule true rather than merely intended:
# liveness is *observed* once per read, not merely proved by one function. Asking
# the same question twice inside one read is asking it at two instants, and the
# ordinary end of a run lands between them -- so the read would combine a session
# that was live when ownership was proved with the same session already gone when
# its state was projected, and refuse the entire queue for describing a moment
# that never existed. One snapshot is taken here and reused by every projection
# and ownership decision below. It is scoped to this read and dies with it; a
# snapshot that survived the read would be the durable liveness cache the
# lifecycle refuses.
#
# Eighth, D8's actionable half makes its last hop here, and it fails closed. The
# publisher validates a blocker block all-or-nothing and `DurableDecision` has
# always carried it whole; what was missing was anything that handed it to a
# person. It is handed over verbatim -- never summarised, never paraphrased,
# never completed from context -- and the one field the durable record has no
# dedicated home for, the affected agent, is sourced from the rail's own
# published assignment rather than invented. When any required field cannot be
# sourced, this module says so by name and hands over nothing. That is a
# deliberate choice between two failures: an item that admits it is not fully
# actionable can still be acted on by a person who goes and looks, while an item
# carrying a plausible sentence nobody published cannot be distinguished from a
# true one at all.

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from .attention_projection import (
    AttentionError,
    project_attention,
    session_evidence,
)
from .control_plane import (
    ControlPlaneError,
    RailState,
    ReadSource,
    artifact_relative,
    collect_rail_states,
    rail_blob_sha,
    resolve_read_source,
    scope_relative,
    validate_decision_record,
    validate_identifier,
)
from .decision_queue import (
    ActionableBlocker,
    DecisionQueue,
    EvidenceReference,
    OperationalAgent,
    PendingDecision,
    QueueError,
    build_queue,
)
from .session_binding import BindingStore, SessionBindingError
from .session_lifecycle import (
    RAIL_STATUS_BLOCKED,
    STATE_RUNNING,
    STATE_WAITING,
    LifecycleError,
    RailFacts,
    SessionRegistry,
    elapsed_seconds,
    observe_session,
    ownership_evidence,
    single_liveness_snapshot,
)

__all__ = [
    "BLOCKER_AGENT_UNSOURCED",
    "BLOCKER_UNUSABLE",
    "DurableDecision",
    "QueueSourceError",
    "REASON_BINDING_RAIL_UNKNOWN",
    "REASON_BINDING_UNREADABLE",
    "REASON_CONFLICTING_ITEMS",
    "REASON_DECISION_INVALID",
    "REASON_DECISION_RAIL_CONTRADICTS",
    "REASON_DECISION_RAIL_UNKNOWN",
    "REASON_DECISION_SCOPE_MISMATCH",
    "REASON_LIFECYCLE_REFUSED",
    "REASON_OWNERSHIP_CONTRADICTORY",
    "REASON_RAIL_UNRECONCILED",
    "REASON_SCOPE_UNKNOWN",
    "REASON_SOURCE_STALE",
    "REASON_SOURCE_UNREADABLE",
    "QueueScope",
    "load_queue",
    "project_queue",
    "read_decisions",
    "resolve_queue_scope",
]


class QueueSourceError(Exception):
    """A refusal to source the queue at all, carrying one stable reason."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__("{0}: {1}".format(reason, detail))
        self.reason = reason
        self.detail = detail


# Why a read produced no queue. Every one of these is a refusal, never a row.
REASON_SOURCE_UNREADABLE = "source-unreadable"
REASON_SOURCE_STALE = "source-stale"
REASON_SCOPE_UNKNOWN = "scope-unknown"
REASON_DECISION_INVALID = "decision-invalid"
REASON_DECISION_SCOPE_MISMATCH = "decision-scope-mismatch"
REASON_DECISION_RAIL_UNKNOWN = "decision-rail-unknown"
REASON_DECISION_RAIL_CONTRADICTS = "decision-rail-contradicts"
REASON_RAIL_UNRECONCILED = "rail-unreconciled"
REASON_BINDING_UNREADABLE = "binding-unreadable"
REASON_BINDING_RAIL_UNKNOWN = "binding-rail-unknown"
REASON_LIFECYCLE_REFUSED = "lifecycle-refused"
REASON_CONFLICTING_ITEMS = "conflicting-items"
REASON_OWNERSHIP_CONTRADICTORY = "ownership-contradictory"

# What a person is told when part of a published blocker had no durable source.
# Neither refuses the queue: a human-owned item is never hidden because part of it
# was unreadable, because hiding it is how a person stops being asked at all.
#
# They are not the same kind of statement, and they no longer sit in the same
# place. `BLOCKER_UNUSABLE` is still carried on the item in place of a blocker
# this module could not complete at all. `BLOCKER_AGENT_UNSOURCED` is carried
# inside the blocker, in place of the one field with no durable home, precisely so
# the rest of the blocker can still be served: the affected agent is sourced from
# the rail's optional `Role:` header, so a complete, publisher-validated six-field
# blocker on a role-less rail is a state the supported publisher accepts, and
# discarding five published facts over the sixth leaves a person holding an item
# they cannot act on -- including, in the reachable case, the published sentence
# that tells them to add the missing `Role:`.
BLOCKER_AGENT_UNSOURCED = (
    "Not established: this item's durable rail publishes no role assignment, so "
    "the affected agent has no durable source and none is guessed here. Every "
    "other fact in this blocker was published and is shown."
)
BLOCKER_UNUSABLE = (
    "blocker-unusable: the published blocker block did not survive projection "
    "({0}). None of it is shown, because a partly shown blocker cannot be told "
    "apart from a complete one."
)


@dataclass(frozen=True)
class DurableDecision:
    """One validated human-attention record, plus the rail it was cross-checked against.

    The D8 blocker block rides along whole, and it now reaches a person.
    `PendingDecision` had no field for it and `decision_queue` was not the
    previous rail's to change, so it stopped here; both of those are now closed,
    and it is still carried whole rather than folded into the explanation --
    summarising it into prose would be this module inventing the very text a
    person is supposed to be reading verbatim.

    `agent` is the one D8 field with no dedicated place in the record. It is the
    rail's own published assignment, which is the only durable statement of who
    works a rail that survives the session, the process and the host underneath it
    being replaced. It is deliberately not the role of whichever binding happens
    to be nonterminal right now: a blocker is usually published *after* its agent
    stopped, so a live-session reading would answer "nobody" exactly when a person
    most needs an answer. It can be absent, and absent is reported rather than
    filled in.
    """

    payload: Dict[str, Any]
    rail: RailState

    @property
    def decision_id(self) -> str:
        return self.payload["decisionId"]

    @property
    def blocker(self) -> Optional[Dict[str, Any]]:
        return self.payload.get("blocker")

    @property
    def agent(self) -> Optional[str]:
        """The affected agent, from the rail's durable assignment, or nothing.

        Carried exactly as published. A rail may name an assignment this product
        models no session role for -- `evidence-worker` is a real one -- and
        mapping those onto the three modeled roles, or refusing them, would be
        this module deciding what a durable record is allowed to say about who
        does the work.
        """
        role = self.rail.role
        if type(role) is not str or not role.strip():
            return None
        return role.strip()


def _wrap(exc: Exception, reason: str) -> QueueSourceError:
    detail = getattr(exc, "detail", None)
    return QueueSourceError(reason, detail if detail else str(exc))


# ---------------------------------------------------------------------------
# Authority
# ---------------------------------------------------------------------------


def _resolve_source(repo_root: Path, *, expected_head: Optional[str]) -> ReadSource:
    """A read that has proven its own freshness, or a refusal.

    `resolve_read_source` already refuses a clone that has diverged, has
    unpublished commits, or cannot reach its remote. `expected_head` adds the
    caller's side of the same question: a controller that read state a moment ago
    can pin exactly what it read, so a queue is never built from history that moved
    underneath it.
    """
    try:
        source = resolve_read_source(repo_root)
    except ControlPlaneError as exc:
        raise _wrap(exc, REASON_SOURCE_UNREADABLE) from exc
    if not source.head:
        raise QueueSourceError(
            REASON_SOURCE_UNREADABLE,
            "the coordination repository has an empty history, so nothing it reports is "
            "authoritative.",
        )
    if expected_head is not None and expected_head.strip() != source.head:
        raise QueueSourceError(
            REASON_SOURCE_STALE,
            "expected head {0} but the coordination repository reads {1}; re-read the "
            "current state before projecting it.".format(expected_head.strip(), source.head),
        )
    return source


def _require_scope(source: ReadSource, *, project: str, ticket: str) -> None:
    """Prove the scope exists before an empty answer is allowed to mean anything.

    Without this, a mistyped project or ticket reads as a scope with no rails and
    projects a serenely empty queue. An accepted state artifact is the orchestrator
    saying this scope is real, so its absence is a refusal rather than zero rows.
    """
    relative = artifact_relative(project=project, ticket=ticket, artifact="state", rail=None)
    if not source.exists(relative):
        raise QueueSourceError(
            REASON_SCOPE_UNKNOWN,
            "{0} publishes no accepted state, so an empty queue would report a scope that "
            "was never read rather than a scope with nothing in it.".format(
                scope_relative(project, ticket)
            ),
        )


def _rail_index(source: ReadSource, *, project: str, ticket: str) -> Dict[str, RailState]:
    try:
        states = collect_rail_states(source, project=project, ticket=ticket)
    except ControlPlaneError as exc:
        raise _wrap(exc, REASON_SOURCE_UNREADABLE) from exc
    return {state.identifier: state for state in states}


def _require_reconciled(state: RailState) -> None:
    """A rail its own handoff contradicts cannot be cross-checked against anything."""
    if state.unreconciled:
        raise QueueSourceError(
            REASON_RAIL_UNRECONCILED,
            "rail {0} authorizes '{1}' while its handoff proposes '{2}'; the orchestrator "
            "reconciles that before it can be used as authority.".format(
                state.identifier, state.status, state.proposed_status
            ),
        )


# ---------------------------------------------------------------------------
# Waiting
# ---------------------------------------------------------------------------


def read_decisions(
    source: ReadSource,
    rails: Dict[str, RailState],
    *,
    project: str,
    ticket: str,
) -> Dict[str, DurableDecision]:
    """Every published human-attention record in one scope, cross-checked against its rail.

    The record and the rail are read independently and then required to agree.
    Neither side is ever synthesized from the other: a decision that names a rail
    which does not exist, or a rail whose status the decision contradicts, is a
    refusal, because the alternative is showing a person a decision about work that
    is not in the state the decision describes.
    """
    found: Dict[str, DurableDecision] = {}
    for identifier in sorted(rails):
        state = rails[identifier]
        relative = artifact_relative(
            project=project, ticket=ticket, artifact="decision", rail=identifier
        )
        if not source.exists(relative):
            # No record is the ordinary case, at every rail status. It is not
            # Waiting, and it is not a failure either.
            continue
        text = source.read(relative)
        if text is None:
            raise QueueSourceError(
                REASON_SOURCE_UNREADABLE,
                "rail {0} has a human-decision record that exists but could not be "
                "read.".format(identifier),
            )
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise QueueSourceError(
                REASON_DECISION_INVALID,
                "rail {0} human-decision record is not valid JSON: {1}".format(
                    identifier, exc.msg
                ),
            ) from exc
        try:
            validate_decision_record(payload)
        except ControlPlaneError as exc:
            raise QueueSourceError(
                REASON_DECISION_INVALID, "rail {0}: {1}".format(identifier, exc)
            ) from exc

        if payload["project"] != project or payload["ticket"] != ticket:
            raise QueueSourceError(
                REASON_DECISION_SCOPE_MISMATCH,
                "rail {0} holds a decision for {1}/{2}, not {3}.".format(
                    identifier, payload["project"], payload["ticket"],
                    scope_relative(project, ticket),
                ),
            )
        named = payload["rail"]
        if named not in rails:
            raise QueueSourceError(
                REASON_DECISION_RAIL_UNKNOWN,
                "a decision names rail {0}, which {1} does not authorize.".format(
                    named, scope_relative(project, ticket)
                ),
            )
        if named != identifier:
            raise QueueSourceError(
                REASON_DECISION_SCOPE_MISMATCH,
                "the decision stored under rail {0} names rail {1}; one of the two is "
                "wrong and neither may be corrected from the other.".format(identifier, named),
            )
        _require_reconciled(state)
        if state.status != RAIL_STATUS_BLOCKED:
            # The accepted rule is a blocked rail plus a durable record. A record
            # against a rail that is ready, running, or completed describes a state
            # the rail is not in, and the record does not get to win that argument.
            raise QueueSourceError(
                REASON_DECISION_RAIL_CONTRADICTS,
                "rail {0} is '{1}' but holds a pending human decision, which only a "
                "'{2}' rail can carry.".format(identifier, state.status, RAIL_STATUS_BLOCKED),
            )
        found[identifier] = DurableDecision(payload=payload, rail=state)
    return found


def _actionable_blocker(
    decision: DurableDecision,
) -> Tuple[Optional[ActionableBlocker], Optional[str]]:
    """D8's actionable half, complete, or a named reason there is none.

    Three properties, and each is deliberate.

    It never fabricates. Every string handed to `ActionableBlocker` is subscripted
    straight out of the published payload or is the rail's own assignment. There
    is no default, no fallback wording, no "unknown", and no sentence composed
    here out of what the record implies -- because a person cannot tell a composed
    sentence from a published one, and the whole point of the item is that they
    should not have to.

    It does not trust the publisher's validation to have run. `_decision_blocker`
    already enforces exactly this shape, and `read_decisions` calls it on the way
    in, so in a well-formed system this function's own refusal is unreachable.
    That is precisely why it is here: the guarantee a person gets should not
    depend on which of two modules happened to look. `.get` rather than `[...]`,
    so a missing key arrives at `ActionableBlocker` as `None` and is refused by
    the same bound that refuses every other malformed field, instead of raising a
    `KeyError` that no caller is shaped to catch.

    It fails closed toward the visible item rather than toward silence. A record
    that carries a blocker this module cannot complete produces an item with no
    blocker and a reason naming what was missing. Refusing the whole queue would
    remove every other person's work from the screen over one rail's bad record;
    dropping the item would remove the one person who is owed an answer.

    Failing closed is about not fabricating, and it is not the same as withholding.
    The affected agent is the one D8 field sourced from the rail rather than from
    the record, and the `Role:` header it comes from is optional by design, so a
    complete six-field blocker with no sourceable agent is a state the supported
    publisher accepts rather than a malformed record. That absence is stated, by
    name, in the field it belongs to, and the five facts that were published are
    served. Withholding them would fabricate nothing and would also help nobody:
    the person would be left with an item naming no failure, no missing capability
    and no next action, which is the unactionable item D8 exists to prevent.
    """
    raw = decision.blocker
    if raw is None:
        # The ordinary case for a decision that is not a failure. "Which of these
        # two approaches" has no thing that failed and no capability to grant, and
        # manufacturing an empty blocker for it would put six blank rows in front
        # of a person who was asked a straight question.
        return None, None
    if not isinstance(raw, dict):
        return None, BLOCKER_UNUSABLE.format("not-an-object")

    # The affected agent, or an explicit statement that the rail publishes none.
    # Exactly one of the two is passed, and neither is derived from anything but
    # the rail's own durable assignment: no session, binding, process or role
    # default reaches this decision, so an item can say "there is no source for
    # this" but can never say a name nobody published.
    agent = decision.agent
    unsourced = BLOCKER_AGENT_UNSOURCED if agent is None else None

    try:
        return (
            ActionableBlocker(
                kind=raw.get("kind"),
                what_failed=raw.get("whatFailed"),
                agent=agent,
                missing_capability=raw.get("missingCapability"),
                human_change=raw.get("humanChange"),
                state_changed=raw.get("stateChanged"),
                next_action=raw.get("nextAction"),
                agent_unavailable=unsourced,
            ),
            None,
        )
    except QueueError as exc:
        # The reason only, never the detail. A detail quotes the offending value,
        # and a notice about a blocker is not a place to print fragments of the
        # blocker -- that is the paraphrase this function exists to refuse,
        # arriving through the error path instead of the success one.
        return None, BLOCKER_UNUSABLE.format(exc.reason)


def _pending_decision(
    decision: DurableDecision, *, now: str, activity: str, attention_owner: str
) -> PendingDecision:
    """Carry the record's own fields across. Nothing here is derived except the age.

    Both `activity` and `attention_owner` arrive already projected, and both are
    carried rather than restated. Two facts, two sources: the activity was computed
    without knowing a decision was published, and the owner was computed without
    knowing what the sessions are doing.

    Passing the projected owner through matters more than it looks. `PendingDecision`
    refuses anything but `human`, so a projection that ever stopped reading the
    durable record would refuse here rather than quietly agreeing with itself. A
    literal `human` written at this call site would make that check compare the
    module to itself and prove nothing.

    D8's actionable half is resolved before the age is, and it cannot refuse this
    call: `_actionable_blocker` returns either a complete blocker or the reason
    there is none, and both are legal on the item. An unreadable blocker therefore
    costs a person the blocker, not the item.
    """
    payload = decision.payload
    blocker, unavailable = _actionable_blocker(decision)
    try:
        age = elapsed_seconds(payload["raisedAt"], now)
    except LifecycleError as exc:
        raise _wrap(exc, REASON_DECISION_INVALID) from exc
    try:
        evidence = tuple(
            EvidenceReference(label=entry["label"], locator=entry["locator"])
            for entry in payload.get("evidence") or ()
        )
        return PendingDecision(
            decision_id=payload["decisionId"],
            project=payload["project"],
            ticket=payload["ticket"],
            rail=payload["rail"],
            raised_at=payload["raisedAt"],
            title=payload["title"],
            explanation=payload["explanation"],
            elapsed_seconds=age,
            activity=activity,
            attention_owner=attention_owner,
            evidence=evidence,
            blocker=blocker,
            blocker_unavailable=unavailable,
        )
    except QueueError as exc:
        raise _wrap(exc, REASON_DECISION_INVALID) from exc


# ---------------------------------------------------------------------------
# Operational
# ---------------------------------------------------------------------------


def _binding_records(store: Optional[BindingStore], *, project: str, ticket: str):
    """Every nonterminal binding in this scope, or a refusal if any record is unreadable.

    `BindingStore.records()` fails the whole read on one malformed file, and that
    is kept: a store that silently skipped a record it could not parse would let a
    live session disappear from the screen without anyone being told.
    """
    if store is None:
        return []
    try:
        records = store.records()
    except SessionBindingError as exc:
        raise _wrap(exc, REASON_BINDING_UNREADABLE) from exc
    return [
        record
        for record in records
        if record.project == project and record.ticket == ticket and not record.is_terminal
    ]


def _rail_facts(
    source: ReadSource,
    state: RailState,
    decision: Optional[DurableDecision],
    *,
    project: str,
    ticket: str,
) -> RailFacts:
    """One rail's authorization as the accepted lifecycle wants to be told it."""
    blob = rail_blob_sha(source, project=project, ticket=ticket, rail=state.identifier)
    if blob is None:
        raise QueueSourceError(
            REASON_SOURCE_UNREADABLE,
            "rail {0} has no readable authorization blob, so the iteration a session "
            "is bound to cannot be checked.".format(state.identifier),
        )
    return RailFacts(
        identifier=state.identifier,
        status=state.status,
        rail_blob=blob,
        # The durable record, exactly as `RailFacts` documents it. A blocked rail
        # without one reaches the lifecycle unexplained, and the lifecycle refuses
        # it -- which is the intended outcome, not a gap.
        pending_human_decision=decision.decision_id if decision is not None else None,
    )


def _rail_state(rails: Dict[str, RailState], record, *, project: str, ticket: str) -> RailState:
    state = rails.get(record.rail)
    if state is None:
        raise QueueSourceError(
            REASON_BINDING_RAIL_UNKNOWN,
            "session {0} is bound to rail {1}, which {2} does not authorize.".format(
                record.session_id, record.rail, scope_relative(project, ticket)
            ),
        )
    _require_reconciled(state)
    return state


def _primary(projections):
    """Which session's lifecycle projection speaks for the whole work item.

    A rail carries one work item, so one of its sessions has to supply the state
    and the age the row shows. The live one does when there is one, because a rail
    with a proven running session is running whatever else is lying around beside
    it. Otherwise the oldest evidence speaks, so "this rail has been unprovable for
    two hours" does not reset to "twenty seconds" because a replacement was
    reserved a moment ago. Session id breaks ties, so the choice is deterministic
    rather than dependent on directory order.
    """
    running = [entry for entry in projections if entry.state == STATE_RUNNING]
    candidates = running if running else list(projections)
    return sorted(candidates, key=lambda entry: (-entry.elapsed_seconds, entry.session_id))[0]


def _work_items(
    source: ReadSource,
    rails: Dict[str, RailState],
    records,
    decisions: Dict[str, DurableDecision],
    registry: SessionRegistry,
    *,
    project: str,
    ticket: str,
    now: str,
    alive: Optional[Callable],
) -> Tuple[Tuple[OperationalAgent, ...], Dict[str, Tuple[str, str]]]:
    """One work item per durable rail, and the two facts each blocked rail projected.

    This is the checkpoint-7 seam. It used to be "one row per live binding", which
    made the row identity the transport rather than the work; it is now one item
    per rail, with every nonterminal binding on that rail reconciled into the two
    facts `attention_projection` returns and into bounded Details evidence.

    Nothing about the accepted lifecycle is loosened on the way. Every nonterminal
    binding is still put through `observe_session`, so an unauthorized rail, a
    superseded iteration, a live session on a rail that is not running, and a
    blocked rail with no published decision all still refuse exactly as before --
    and they refuse for every binding, not merely for whichever one happens to
    speak for the item.

    Two returns rather than one, because a blocked rail's activity belongs to the
    Waiting item the decision record produces, and that item is built elsewhere. A
    rail carrying a decision contributes no operational item at all: the accepted
    rule that the lifecycle's Waiting projection never becomes an operational row
    is unchanged, and it is now expressed once per rail instead of once per binding.
    """
    grouped: Dict[str, list] = {}
    for record in sorted(records, key=lambda entry: entry.session_id):
        grouped.setdefault(record.rail, []).append(record)

    # One liveness snapshot for the whole read, taken before anything reads it.
    # `ownership_evidence` below and `observe_session` further down are two
    # consumers of one question, and they used to ask it separately -- with a
    # `rail_blob_sha` subprocess running in between, so the gap was real wall time
    # rather than a theoretical interleaving. Handing both the same observation is
    # what makes this read one coherent instant.
    observed = single_liveness_snapshot(alive)

    # One ownership proof for the whole read, from the accepted primitive that the
    # concurrency reconciler uses. Not a second opinion about liveness: the same
    # question, asked once, so the rows and the ceiling cannot disagree.
    ownership = ownership_evidence(registry, list(records), alive=observed)

    agents = []
    attention_by_rail: Dict[str, Tuple[str, str]] = {}
    for rail in sorted(set(grouped) | set(decisions)):
        held = grouped.get(rail, [])
        decision = decisions.get(rail)
        if held:
            state = _rail_state(rails, held[0], project=project, ticket=ticket)
        else:
            state = rails[rail]
            _require_reconciled(state)

        projections = []
        if held:
            facts = _rail_facts(source, state, decision, project=project, ticket=ticket)
            for record in held:
                if record.rail != rail:
                    # Unreachable through `grouped`, and asserted rather than
                    # assumed: the whole point of this seam is that one item speaks
                    # for exactly one rail.
                    raise QueueSourceError(
                        REASON_BINDING_RAIL_UNKNOWN,
                        "session {0} was grouped under rail {1} but names {2}.".format(
                            record.session_id, rail, record.rail
                        ),
                    )
                try:
                    projections.append(
                        observe_session(facts, record, registry, now=now, alive=observed)
                    )
                except LifecycleError as exc:
                    raise _wrap(exc, REASON_LIFECYCLE_REFUSED) from exc

        sessions = tuple(session_evidence(record, ownership) for record in held)
        try:
            attention = project_attention(
                rail, status=state.status, has_decision=decision is not None, sessions=sessions
            )
        except AttentionError as exc:
            raise _wrap(exc, REASON_OWNERSHIP_CONTRADICTORY) from exc
        if decision is not None:
            if attention is None:
                # Stated rather than relied on. `load_queue` builds a Waiting item
                # for every decision it read, so a decision with no projected
                # attention would drop a human-owned row -- the one outcome this
                # module exists to prevent.
                raise QueueSourceError(
                    REASON_LIFECYCLE_REFUSED,
                    "rail {0} holds a published decision but projected no work "
                    "item; a person would be owed an answer with nothing on the "
                    "screen to answer it.".format(rail),
                )
            # The Waiting row is the decision record's, and it is the only row this
            # rail gets. Both projected facts are recorded here because this is
            # where the execution evidence was reconciled.
            attention_by_rail[rail] = (attention.activity, attention.attention_owner)
            continue

        if attention is None:
            # No execution evidence and nobody waiting: a rail is not a row.
            continue

        parked = [entry for entry in projections if entry.state == STATE_WAITING]
        if parked:
            # A Waiting projection with no decision record cannot happen: the
            # lifecycle refuses a blocked rail that records none. Refusing rather
            # than skipping keeps that impossibility stated instead of assumed.
            raise QueueSourceError(
                REASON_LIFECYCLE_REFUSED,
                "rail {0} projected a waiting session with no published decision "
                "record; that is a state the lifecycle does not permit.".format(rail),
            )

        speaker = _primary(projections)
        try:
            agents.append(
                OperationalAgent(
                    project=project,
                    ticket=ticket,
                    rail=rail,
                    # The rail identifier is the only durable name this work has.
                    # A friendlier title would have to be invented from prose, and
                    # a session id or worker name would be the transport again.
                    title=rail,
                    projection=speaker,
                    activity=attention.activity,
                    # Carried, never restated: `OperationalAgent` refuses anything
                    # but `agent`, so this is a cross-check against the projection
                    # rather than this module agreeing with itself.
                    attention_owner=attention.attention_owner,
                    evidence=tuple(
                        EvidenceReference(label=entry.label, locator=entry.locator)
                        for entry in attention.sessions
                    ),
                )
            )
        except QueueError as exc:
            raise _wrap(exc, REASON_LIFECYCLE_REFUSED) from exc
    return tuple(agents), attention_by_rail


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QueueScope:
    """The durable half of one queue read, resolved once and then held.

    A queue read has two halves that expire at completely different rates, and
    checkpoint 47 was right that they must not be re-acquired together. The durable
    half is the control plane: which revision this read serves, which rails that
    revision authorizes, and which decisions it publishes. Establishing it fetches
    the coordination remote, and it describes state that outlives any one render --
    so it is this run's projection, taken once, exactly as the allowance windows
    beside it are.

    The other half is what this controller can prove about its own sessions right
    now, and that half is only ever true of the instant it was taken. Separating
    them is what lets the second be re-observed per response without the first
    becoming a polling loop: `project_queue` below re-reads nothing remote, takes no
    lock, and spawns no fetch. It re-reads a local durable store and re-observes
    liveness, which is precisely the state that can have changed since the last
    response and precisely the state a person is reading the page to learn.

    Frozen because it is evidence, not a workspace. Nothing below may adjust which
    revision a response is being served from.
    """

    source: ReadSource
    project: str
    ticket: str
    rails: Mapping[str, RailState]
    decisions: Mapping[str, DurableDecision]

    @property
    def head(self) -> str:
        """The exact revision every response built from this scope is served from."""
        return self.source.head


def resolve_queue_scope(
    repo_root: Path,
    *,
    project: str,
    ticket: str,
    expected_head: Optional[str] = None,
) -> QueueScope:
    """Prove the durable authority for one run, once, or refuse and say why.

    Every step here reaches outside this process: scope validation, remote freshness
    resolution, the rail index, and the published decisions. Each refusal is exactly
    the one `load_queue` has always raised, because this is that function's first
    half moved behind a name rather than a second reading of the same sources.
    """
    try:
        project = validate_identifier(project, label="project")
        ticket = validate_identifier(ticket, label="ticket")
    except ControlPlaneError as exc:
        raise _wrap(exc, REASON_SCOPE_UNKNOWN) from exc

    source = _resolve_source(Path(repo_root), expected_head=expected_head)
    _require_scope(source, project=project, ticket=ticket)
    rails = _rail_index(source, project=project, ticket=ticket)
    decisions = read_decisions(source, rails, project=project, ticket=ticket)
    return QueueScope(
        source=source,
        project=project,
        ticket=ticket,
        rails=rails,
        decisions=decisions,
    )


def project_queue(
    scope: QueueScope,
    *,
    registry: SessionRegistry,
    now: str,
    store: Optional[BindingStore] = None,
    alive: Optional[Callable] = None,
) -> DecisionQueue:
    """One queue projected from an already-proven scope and a live observation.

    The second half of `load_queue`, and the half that may be repeated. It re-reads
    the durable bindings from the caller's store and re-observes liveness through
    `_work_items`, whose single-snapshot semantics are unchanged and still govern
    that read. Nothing here fetches a remote, so calling this again is a fresh
    observation rather than a poll.

    `alive` is the caller's, and a caller composing more than one consumer into one
    response passes the same observation to all of them. `_work_items` still takes
    its own snapshot over whatever it is given, which stays correct either way: a
    snapshot of a snapshot answers each process group exactly once, from the outer
    observation, so composing them narrows the instant rather than widening it.
    """
    records = _binding_records(store, project=scope.project, ticket=scope.ticket)
    agents, attention = _work_items(
        scope.source, dict(scope.rails), records, dict(scope.decisions), registry,
        project=scope.project, ticket=scope.ticket, now=now, alive=alive,
    )
    pending = tuple(
        _pending_decision(
            scope.decisions[identifier],
            now=now,
            activity=attention[identifier][0],
            attention_owner=attention[identifier][1],
        )
        for identifier in sorted(scope.decisions)
    )

    try:
        return build_queue(decisions=pending, agents=agents)
    except QueueError as exc:
        # Reached only when two durable facts claim one identity. That is a
        # contradiction in the sources, and it is refused here rather than
        # projected, for the same reason everything else above is.
        raise _wrap(exc, REASON_CONFLICTING_ITEMS) from exc


def load_queue(
    repo_root: Path,
    *,
    project: str,
    ticket: str,
    registry: SessionRegistry,
    now: str,
    store: Optional[BindingStore] = None,
    expected_head: Optional[str] = None,
    alive: Optional[Callable] = None,
) -> DecisionQueue:
    """Build one queue from proven durable authority, or refuse and say why.

    An empty result from this function is a fact: the sources were reachable, fresh,
    internally consistent, and had nothing in them. Every other outcome raises.

    `now` is supplied rather than read, so this stays as deterministic as the
    projection it feeds -- the same sources and the same clock always produce the
    same queue, and age remains something to display rather than something to act on.

    This is now exactly the two halves above, composed: one scope resolution
    followed by one projection. A caller that reads a queue once still calls this
    and is unaffected; a caller that renders repeatedly from one run holds the scope
    and repeats only the projection, which is the whole difference between
    re-observing what changed and re-fetching what did not.
    """
    scope = resolve_queue_scope(
        repo_root, project=project, ticket=ticket, expected_head=expected_head
    )
    return project_queue(scope, registry=registry, now=now, store=store, alive=alive)

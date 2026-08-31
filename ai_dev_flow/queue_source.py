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

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

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
    STATE_WAITING,
    LifecycleError,
    RailFacts,
    SessionRegistry,
    elapsed_seconds,
    observe_session,
)

__all__ = [
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
    "REASON_RAIL_UNRECONCILED",
    "REASON_SCOPE_UNKNOWN",
    "REASON_SOURCE_STALE",
    "REASON_SOURCE_UNREADABLE",
    "load_queue",
    "read_decisions",
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


@dataclass(frozen=True)
class DurableDecision:
    """One validated human-attention record, plus the rail it was cross-checked against.

    The D8 blocker block rides along whole. `PendingDecision` has no field for it
    and `decision_queue` is not this rail's to change, so it is carried here rather
    than folded into the explanation -- summarising it into prose would be this
    module inventing the very text a person is supposed to be reading verbatim.
    """

    payload: Dict[str, Any]
    rail: RailState

    @property
    def decision_id(self) -> str:
        return self.payload["decisionId"]

    @property
    def blocker(self) -> Optional[Dict[str, Any]]:
        return self.payload.get("blocker")


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


def _pending_decision(decision: DurableDecision, *, now: str) -> PendingDecision:
    """Carry the record's own fields across. Nothing here is derived except the age."""
    payload = decision.payload
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
            evidence=evidence,
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


def _operational_agents(
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
) -> Tuple[OperationalAgent, ...]:
    """One row per live binding, projected by the accepted lifecycle and nothing else."""
    agents = []
    for record in sorted(records, key=lambda entry: entry.session_id):
        state = rails.get(record.rail)
        if state is None:
            raise QueueSourceError(
                REASON_BINDING_RAIL_UNKNOWN,
                "session {0} is bound to rail {1}, which {2} does not authorize.".format(
                    record.session_id, record.rail, scope_relative(project, ticket)
                ),
            )
        _require_reconciled(state)
        blob = rail_blob_sha(source, project=project, ticket=ticket, rail=record.rail)
        if blob is None:
            raise QueueSourceError(
                REASON_SOURCE_UNREADABLE,
                "rail {0} has no readable authorization blob, so the iteration a session "
                "is bound to cannot be checked.".format(record.rail),
            )
        decision = decisions.get(record.rail)
        facts = RailFacts(
            identifier=state.identifier,
            status=state.status,
            rail_blob=blob,
            # The durable record, exactly as `RailFacts` documents it. A blocked
            # rail without one reaches the lifecycle unexplained, and the lifecycle
            # refuses it -- which is the intended outcome, not a gap.
            pending_human_decision=decision.decision_id if decision is not None else None,
        )
        try:
            projection = observe_session(facts, record, registry, now=now, alive=alive)
        except LifecycleError as exc:
            raise _wrap(exc, REASON_LIFECYCLE_REFUSED) from exc
        if projection.state == STATE_WAITING:
            # The lifecycle's Waiting answers "is this session progressing". The
            # row a person sees comes from the decision record instead, and turning
            # this projection into an operational input is exactly what
            # `OperationalAgent` refuses. Skipping it is that refusal honoured
            # early, not a state being dropped.
            continue
        try:
            agents.append(
                OperationalAgent(
                    project=record.project,
                    ticket=record.ticket,
                    rail=record.rail,
                    # The rail identifier is the only durable name this work has.
                    # A friendlier title would have to be invented from prose.
                    title=record.rail,
                    projection=projection,
                )
            )
        except QueueError as exc:
            raise _wrap(exc, REASON_LIFECYCLE_REFUSED) from exc
    return tuple(agents)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


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
    records = _binding_records(store, project=project, ticket=ticket)
    agents = _operational_agents(
        source, rails, records, decisions, registry,
        project=project, ticket=ticket, now=now, alive=alive,
    )
    pending = tuple(
        _pending_decision(decisions[identifier], now=now) for identifier in sorted(decisions)
    )

    try:
        return build_queue(decisions=pending, agents=agents)
    except QueueError as exc:
        # Reached only when two durable facts claim one identity. That is a
        # contradiction in the sources, and it is refused here rather than
        # projected, for the same reason everything else above is.
        raise _wrap(exc, REASON_CONFLICTING_ITEMS) from exc

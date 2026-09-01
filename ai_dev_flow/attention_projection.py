"""Durable work-item identity, plus activity and attention ownership as two separate facts."""

from __future__ import annotations

# Named checkpoint 7 starts here, and this module is its core model.
#
# The defect it exists to remove is an identity defect. Rows used to be one per
# live binding, so the visible unit of work was whatever transport happened to be
# carrying it: a Claude session id, a resident worker name, a pid. Those are all
# replaceable. Rotate the worker, restart the process, hand the rail to a fresh
# session, and the screen grew a second row for work that never stopped being one
# piece of work -- while an idle transport resident that was doing nothing at all
# got a row of its own simply by existing. The durable rail is the thing a person
# actually tracks, so the durable rail is the row.
#
# Five rules hold this honest.
#
# First, the work item is the rail. Session identity, worker target, pid and pid
# domain are evidence about a rail, never the name of one. Nothing here returns an
# identity at all: identity belongs to `decision_queue`, which already encodes the
# rail's routing scope and deliberately excludes session identity from it.
#
# Second, activity and attention ownership are two facts, read from two different
# durable sources, and neither is computed from the other. Activity comes from the
# rail's authorization status and its reconciled binding/ownership evidence.
# Attention ownership comes from one place only: whether an orchestrator published
# a human-decision record for that rail. A rail can change what it is doing without
# changing who owes it attention, and two rails doing exactly the same thing can owe
# attention to different parties. Deriving either from the other would collapse
# them back into the single blurred fact this checkpoint exists to separate.
#
# Third, ownership is proved, never assumed. The one thing that makes a session
# count as live here is `session_lifecycle.ownership_evidence` -- the same proof
# `reconcile_agent_slots` consumes for the accepted concurrency ceiling. This
# module introduces no registry, no store, no cache, no pid lookup, and no second
# way to decide whether a process is there. It is handed the answer.
#
# Fourth, contradictory evidence refuses. Two sessions this controller can prove
# are live on one durable rail is not a row to render; it is two claims on one
# piece of work, and merging them would produce a plausible row that describes
# neither. Legitimate concurrency uses distinct rails, so one rail with two live
# owners is a fault, and it is raised rather than smoothed.
#
# Fifth, this module is pure. No file, no clock, no process, no network, no
# control-plane artifact. Everything it knows was handed to it, and it returns
# facts rather than rows.

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence, Tuple

from .session_binding import (
    ROLE_EXECUTOR,
    ROLE_ORCHESTRATOR,
    ROLE_REVIEWER,
)
from .session_lifecycle import (
    RAIL_STATUS_BLOCKED,
    STATE_DISCONNECTED,
    STATE_RUNNING,
)

__all__ = [
    "ACTIVITIES",
    "ACTIVITY_BLOCKED",
    "ACTIVITY_CONTEXT_ROTATION",
    "ACTIVITY_DISCONNECTED_RECOVERY",
    "ACTIVITY_EXECUTOR_WORKING",
    "ACTIVITY_MANAGER_LIFECYCLE",
    "ACTIVITY_ORCHESTRATOR_RECONCILING",
    "ACTIVITY_REVIEWER_WORKING",
    "ATTENTION_OWNERS",
    "AttentionError",
    "DISPOSITION_LIVE",
    "DISPOSITION_RESERVED",
    "DISPOSITION_UNPROVABLE",
    "MAX_SESSION_EVIDENCE",
    "OPERATIONAL_ACTIVITY_STATES",
    "OWNER_AGENT",
    "OWNER_HUMAN",
    "REASON_CONTRADICTORY_OWNERSHIP",
    "REASON_INVALID_ACTIVITY",
    "REASON_INVALID_OWNER",
    "REASON_UNBOUNDED_EVIDENCE",
    "REASON_UNKNOWN_ROLE",
    "SessionEvidence",
    "WorkAttention",
    "project_attention",
    "require_activity",
    "require_attention_owner",
    "session_evidence",
]


# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

# What a work item is currently doing. Deliberately richer than the three queue
# states, because "running" answers a different question than "which role is
# working, and is this a rotation or a recovery". These are expressed through the
# operational filters and the detail pane; a row carries none of them, because a
# dense list stops being dense the moment a row can hold one more badge.
ACTIVITY_EXECUTOR_WORKING = "executor-working"
ACTIVITY_REVIEWER_WORKING = "reviewer-working"
ACTIVITY_ORCHESTRATOR_RECONCILING = "orchestrator-reconciling"
ACTIVITY_MANAGER_LIFECYCLE = "manager-lifecycle"
ACTIVITY_BLOCKED = "blocked"
ACTIVITY_DISCONNECTED_RECOVERY = "disconnected-recovery"
ACTIVITY_CONTEXT_ROTATION = "context-rotation"

ACTIVITIES = (
    ACTIVITY_EXECUTOR_WORKING,
    ACTIVITY_REVIEWER_WORKING,
    ACTIVITY_ORCHESTRATOR_RECONCILING,
    ACTIVITY_MANAGER_LIFECYCLE,
    ACTIVITY_BLOCKED,
    ACTIVITY_DISCONNECTED_RECOVERY,
    ACTIVITY_CONTEXT_ROTATION,
)

# Who owes this item attention. Exactly two, and never a third: "nobody" is not an
# answer a queue may give about work it is still showing.
OWNER_HUMAN = "human"
OWNER_AGENT = "agent"
ATTENTION_OWNERS = (OWNER_HUMAN, OWNER_AGENT)

# Which queue state each activity is compatible with *for an operational item*.
#
# This is a contradiction guard, not a derivation. The queue state comes from the
# accepted lifecycle projection and the activity comes from reconciled rail
# evidence; the two are computed apart and then required to agree, the same way a
# decision record and its rail are read apart and then required to agree.
#
# `ACTIVITY_BLOCKED` maps to no state at all. An operational input may never claim
# Waiting, so an operational item that says it is blocked is describing a row that
# cannot exist. A *decision* item may carry any activity, including this one,
# because its state says who must act while its activity says what the work is
# doing -- and those are exactly the two facts this checkpoint separates.
#
# Every modeled activity has an entry, so a consumer subscripts this table rather
# than reaching for a default. An activity missing from it would be a silent
# "anything goes" for whichever kind of row nobody thought about.
OPERATIONAL_ACTIVITY_STATES = {
    ACTIVITY_EXECUTOR_WORKING: (STATE_RUNNING,),
    ACTIVITY_REVIEWER_WORKING: (STATE_RUNNING,),
    ACTIVITY_ORCHESTRATOR_RECONCILING: (STATE_RUNNING,),
    ACTIVITY_CONTEXT_ROTATION: (STATE_RUNNING,),
    ACTIVITY_MANAGER_LIFECYCLE: (STATE_DISCONNECTED,),
    ACTIVITY_DISCONNECTED_RECOVERY: (STATE_DISCONNECTED,),
    ACTIVITY_BLOCKED: (),
}

# What one session is, as evidence. `live` is the only one that means this
# controller proved it owns a running process; `unprovable` is the Disconnected
# reading and deliberately does not mean "stopped"; `reserved` is a launch this
# controller has committed to that has no process attached yet.
DISPOSITION_LIVE = "live"
DISPOSITION_UNPROVABLE = "unprovable"
DISPOSITION_RESERVED = "reserved"

# The role a live session is filling, mapped to what that role doing work is
# called. There is no default branch: a role this table does not name is refused,
# because guessing an activity is inventing rail state.
_ROLE_ACTIVITY = {
    ROLE_EXECUTOR: ACTIVITY_EXECUTOR_WORKING,
    ROLE_REVIEWER: ACTIVITY_REVIEWER_WORKING,
    ROLE_ORCHESTRATOR: ACTIVITY_ORCHESTRATOR_RECONCILING,
}

# Bounded, because Details is a debug surface and not a log. Six is the accepted
# concurrency ceiling, so a single rail carrying more sessions than this is a fault
# rather than a busy rail -- and it is refused rather than truncated, since
# silently dropping evidence is how a live session disappears from a screen.
MAX_SESSION_EVIDENCE = 8

REASON_CONTRADICTORY_OWNERSHIP = "rail-ownership-contradictory"
REASON_UNKNOWN_ROLE = "unknown-session-role"
REASON_INVALID_ACTIVITY = "invalid-activity"
REASON_INVALID_OWNER = "invalid-attention-owner"
REASON_UNBOUNDED_EVIDENCE = "session-evidence-unbounded"


class AttentionError(Exception):
    """A refusal to project attention, carrying one stable reason."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__("{0}: {1}".format(reason, detail))
        self.reason = reason
        self.detail = detail


def require_activity(value: object, *, label: str = "activity") -> str:
    """Exactly one modeled activity, refused otherwise."""
    if value not in ACTIVITIES:
        raise AttentionError(
            REASON_INVALID_ACTIVITY,
            "{0} must be one of {1}; got {2!r}".format(label, ", ".join(ACTIVITIES), value),
        )
    return value  # type: ignore[return-value]


def require_attention_owner(value: object, *, label: str = "attention owner") -> str:
    """Exactly `human` or `agent`. There is no unowned item and no third party."""
    if value not in ATTENTION_OWNERS:
        raise AttentionError(
            REASON_INVALID_OWNER,
            "{0} must be one of {1}; got {2!r}".format(
                label, ", ".join(ATTENTION_OWNERS), value
            ),
        )
    return value  # type: ignore[return-value]


# --------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionEvidence:
    """One transport session, as bounded evidence about a rail. Never a row identity.

    Everything here is replaceable: the session id, the process, the host it ran
    on. That is precisely why it is evidence rather than identity -- a rail whose
    worker rotated is the same rail, and a screen that renamed it would be
    reporting the transport rather than the work.
    """

    session_id: str
    role: str
    disposition: str
    pid: Optional[int] = None
    pid_domain: Optional[str] = None

    @property
    def label(self) -> str:
        """What this evidence is, in the Details pane's own vocabulary."""
        return "{0} session".format(self.disposition)

    @property
    def locator(self) -> str:
        """Where to look, and nothing about what the session said or did.

        A reservation prints that it has no process rather than a placeholder pid:
        the four process fields are complete or absent on a binding, never
        partially populated, and printing a stand-in would invent one.
        """
        if self.pid is None:
            return "{0} · role {1} · no process attached".format(self.session_id, self.role)
        return "{0} · role {1} · pid {2} ({3})".format(
            self.session_id, self.role, self.pid, self.pid_domain
        )


def session_evidence(record, ownership: Mapping[str, bool]) -> SessionEvidence:
    """One binding record reduced to evidence, with its disposition read, not guessed.

    `ownership` is `session_lifecycle.ownership_evidence` -- the same proved-owned
    mapping the accepted concurrency reconciler consumes. A reservation is absent
    from it by that function's own contract, which is why absence reads as
    `reserved` rather than as "not owned": no handle exists for a reservation yet,
    so there is nothing about it to fail to prove.
    """
    if record.is_reserved:
        disposition = DISPOSITION_RESERVED
    elif ownership.get(record.session_id) is True:
        disposition = DISPOSITION_LIVE
    else:
        disposition = DISPOSITION_UNPROVABLE
    return SessionEvidence(
        session_id=record.session_id,
        role=record.role,
        disposition=disposition,
        pid=record.pid,
        pid_domain=record.pid_domain,
    )


# --------------------------------------------------------------------------
# Projection
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkAttention:
    """One rail's two independent facts, plus the bounded evidence behind them."""

    activity: str
    attention_owner: str
    sessions: Tuple[SessionEvidence, ...] = ()

    @property
    def live_sessions(self) -> Tuple[SessionEvidence, ...]:
        return tuple(entry for entry in self.sessions if entry.disposition == DISPOSITION_LIVE)


def _attention_owner(*, has_decision: bool) -> str:
    """Who owes this rail attention, from the durable record and nothing else.

    Not from the activity, not from the rail's status, not from an error string,
    not from elapsed time, and not from a process being missing. An orchestrator
    published a human-decision record or it did not, and that single durable fact
    is the whole answer. This is what makes the Waiting view equal to the
    human-owned set rather than merely correlated with it.
    """
    return OWNER_HUMAN if has_decision else OWNER_AGENT


def _activity(rail: str, status: str, sessions: Sequence[SessionEvidence]) -> Optional[str]:
    """What this rail is currently doing, from reconciled evidence, or nothing at all.

    `None` means there is no work item here: a rail nobody is executing and nobody
    is waiting on is not a row, and manufacturing one from the mere existence of an
    authorization would put every rail in the scope on the screen forever.

    The order of the branches is the substance. A contradiction outranks everything
    because it must not be resolved into a plausible answer; a rotation outranks a
    plain running state because "one live session beside a session being replaced"
    is a different fact than "one live session"; and an unprovable session outranks
    a reservation because a handle that cannot be proved is the more serious of the
    two, and this projection fails toward the more visible reading.
    """
    live = [entry for entry in sessions if entry.disposition == DISPOSITION_LIVE]
    reserved = [entry for entry in sessions if entry.disposition == DISPOSITION_RESERVED]
    unprovable = [entry for entry in sessions if entry.disposition == DISPOSITION_UNPROVABLE]

    if len(live) > 1:
        raise AttentionError(
            REASON_CONTRADICTORY_OWNERSHIP,
            "rail {0} has {1} sessions this controller can prove are live ({2}); one "
            "durable rail carries one execution, so this is contradictory evidence "
            "rather than a row. Concurrent executor and reviewer work uses distinct "
            "rails.".format(
                rail, len(live), ", ".join(entry.session_id for entry in live)
            ),
        )

    if live:
        if reserved or unprovable:
            # A replacement in flight. One rail, one work item, one identity: the
            # visible item does not split, does not reset, and does not renumber
            # itself because the transport underneath it changed.
            return ACTIVITY_CONTEXT_ROTATION
        if status == RAIL_STATUS_BLOCKED:
            # The session is live and parked. What the rail is doing is waiting for
            # a person, and saying "executor working" here would describe a process
            # rather than the work.
            return ACTIVITY_BLOCKED
        role = live[0].role
        if role not in _ROLE_ACTIVITY:
            raise AttentionError(
                REASON_UNKNOWN_ROLE,
                "rail {0} has a live session in role {1!r}, which this projection does "
                "not model; inventing an activity for it would be inventing rail "
                "state.".format(rail, role),
            )
        return _ROLE_ACTIVITY[role]

    if unprovable:
        return ACTIVITY_DISCONNECTED_RECOVERY
    if reserved:
        # The controller committed to a launch and no process is attached yet. That
        # is the manager's own lifecycle, and it is not a lost session.
        return ACTIVITY_MANAGER_LIFECYCLE
    if status == RAIL_STATUS_BLOCKED:
        return ACTIVITY_BLOCKED
    return None


def project_attention(
    rail: str,
    *,
    status: str,
    has_decision: bool,
    sessions: Sequence[SessionEvidence] = (),
) -> Optional[WorkAttention]:
    """One rail's activity and attention owner, or `None` when it is not a work item.

    The two facts are produced by two functions that cannot see each other's
    result. `_activity` is never told whether a decision was published, and
    `_attention_owner` is never told what the sessions are doing. That separation
    is the whole point of this module, and it is the property the checkpoint-7
    deliberate-breakage control attacks.
    """
    ordered = tuple(sorted(sessions, key=lambda entry: entry.session_id))
    if len(ordered) > MAX_SESSION_EVIDENCE:
        raise AttentionError(
            REASON_UNBOUNDED_EVIDENCE,
            "rail {0} carries {1} nonterminal sessions; Details holds at most {2}, and "
            "dropping the rest would hide a live session from the screen.".format(
                rail, len(ordered), MAX_SESSION_EVIDENCE
            ),
        )

    activity = _activity(rail, status, ordered)
    owner = _attention_owner(has_decision=has_decision)
    if activity is None and not has_decision:
        return None
    if activity is None:
        # A published decision is always a work item, even on a rail nothing is
        # executing. Somebody is owed an answer whether or not a process exists.
        activity = ACTIVITY_BLOCKED
    return WorkAttention(activity=activity, attention_owner=owner, sessions=ordered)

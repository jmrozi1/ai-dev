"""A driver that holds more than one managed role session live at once, bounded by D6."""

from __future__ import annotations

# Checkpoint 73 built a door that could start one managed executor- or reviewer-role
# session and stopped it before returning. It also refused, at that door, to start
# anything while the controller already held a session, and said in as many words
# that the slice which built concurrency had to delete that refusal deliberately.
# This module is that slice, and the refusal is gone.
#
# What this is: a driver that admits several stated role launches, one at a time,
# and does not stop any of them until it has admitted them all -- so that two or
# more managed sessions are alive, owned, and counted at the same instant, which is
# the capability the accepted middle cut authorized and the package did not have.
#
# What this is NOT, and every one of these is a boundary the accepted state draws
# rather than a corner cut:
#
#   * It is NOT a scheduler, a queue, a priority model, a fairness policy, or an
#     autoscaler. D6 forbids all of them by name. This driver is told, on the
#     command line, exactly which rails to launch in which roles; it launches those
#     and nothing else, in the order stated, and it never asks what else might be
#     runnable. There is no work discovery here and nothing to discover it with.
#
#   * The ceiling is therefore a LIMIT AND NOT A TARGET. Nothing in this file
#     launches an additional agent because a slot happens to be free. A free slot is
#     a permission to spend a standing authorization somebody already wrote down,
#     never a reason to find one.
#
#   * It is NOT a continuation loop and NOT a response router. Each session is given
#     its role's directive once, by the accepted launch, and nothing here reads what
#     came back, decides what to say next, or sends a second thing. Both of those
#     are explicitly deferred to a follow-on ticket.
#
#   * It is NOT a second admission policy. It has no `decision` parameter, no
#     `authorized` flag, no launcher parameter and no test hook. Every launch goes
#     through `ManagerController.open_role` -> `role_invocation.open_role_session`,
#     which always calls the accepted `authorize` predicate itself, and this file
#     contains no gate of its own except a pre-flight re-use of the accepted
#     launchable-role refusal.
#
#   * It does NOT count agents. It never adds, subtracts, or caches an occupancy
#     number. `ManagerController.occupancy` / `reconcile_agent_slots` is the one
#     reduction, and this file only asks for readings and reports the largest one
#     the controller itself produced.
#
# How D6 is actually enforced, and why serial admission is forced rather than
# chosen. A slot is consumed by `reserve_binding`, which lives inside
# `launch_session`; nothing else in the accepted design marks a slot as taken. So
# the only way to admit a second launch against an occupancy that already includes
# the first is to have reserved the first already. This driver therefore admits and
# enacts strictly one launch at a time, and each admission re-reads the store and
# re-reconciles occupancy through the controller before the predicate sees it:
#
#     admit #1 -> occupancy 0/6 -> reserve+bind #1 (slot consumed, session held)
#     admit #2 -> occupancy 1/6 -> reserve+bind #2 (slot consumed, session held)
#     ...
#     admit #7 -> occupancy 6/6 -> `concurrency-ceiling-reached`, nothing reserved
#
# The alternative -- admitting a batch and then launching it -- would require this
# file to count pending admissions that no durable record yet describes, which is
# exactly the second count the accepted state forbids and exactly how a seventh
# agent gets in. It fails closed for the same reason: if a held session's ownership
# cannot be proved, `reconcile_agent_slots` reports it `unprovable`, `authorize`
# never subtracts that, and the next admission is refused
# `concurrency-count-unprovable` rather than admitted against a smaller total.
#
# What is concurrent here, stated plainly so a reviewer does not have to infer it:
# the LIVENESS of the sessions, not their provider turns. Admission and the launch
# invocation that follows it are serial, because the accepted contracts make them
# so; once launched, a session stays alive, owned and counted while the next one is
# admitted, and the driver holds every one of them until it releases them all. This
# is a real limitation and it is written up rather than hidden.
#
# Teardown is the driver's, unconditionally. Every session this file opens is
# released by this file, in reverse order, on every path out -- success, refusal,
# or an exception from anywhere -- because a driver that can leave a process group
# alive with no owner is worse than no driver.

from dataclasses import dataclass
from typing import Any, Callable, List, Mapping, Optional, Sequence, Tuple

from .orchestrator_invocation import InvocationRefused
from .role_invocation import (
    build_role_packet,
    # The accepted launchable-role refusal, imported rather than respelled. A second
    # spelling of "this door does not start orchestrators" is a second rule free to
    # drift, and the one that drifts is always the one that admits more.
    _require_launchable_role,
)

__all__ = [
    "REASON_NO_LAUNCH_STATED",
    "REASON_RELEASE_FAILED",
    "DriverError",
    "DriverOutcome",
    "HeldSession",
    "ReleasedSession",
    "RoleLaunch",
    "StatedRefusal",
    "drive_roles",
]

REASON_NO_LAUNCH_STATED = "no-launch-stated"
REASON_RELEASE_FAILED = "release-failed"


class DriverError(Exception):
    """Something this driver was responsible for did not happen. One stable reason."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__("{0}: {1}".format(reason, detail))
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class RoleLaunch:
    """One stated launch: a rail, the role it is durably assigned, and its runtime.

    The runtime policy is per launch and not per run, because the prompt file and the
    plugin a session runs under are per role. A driver that shared one runtime policy
    across an executor and a reviewer would be running one of them under the other's
    role package, which is the exact failure `validate_plugin_surface` exists to
    catch.
    """

    rail: str
    role: str
    request_kwargs: Mapping


@dataclass(frozen=True)
class HeldSession:
    """One managed session this driver is holding live right now.

    `occupancy` is the reading `ManagerController.agent_count` produced at the instant
    this session became live -- the controller's own reduction, recorded rather than
    recomputed. Nothing in this file derives it.
    """

    rail: str
    role: str
    session_id: str
    iteration_blob: str
    pid: int
    pgid: int
    binding: Any
    occupancy: Mapping


@dataclass(frozen=True)
class StatedRefusal:
    """A stated launch that a gate said no to, with the reason its owner raised."""

    rail: str
    role: str
    reason: str
    detail: str


@dataclass(frozen=True)
class ReleasedSession:
    """One held session after teardown, with the proof its process group is gone."""

    session_id: str
    rail: str
    role: str
    pgid: int
    binding_state: str
    process_group_gone: bool
    graceful: bool


@dataclass(frozen=True)
class DriverOutcome:
    """What this run of the driver actually did. No claim beyond what it observed."""

    project: str
    ticket: str
    head: str
    entry_occupancy: Mapping
    held: Tuple[HeldSession, ...]
    refusals: Tuple[StatedRefusal, ...]
    peak_occupancy: Mapping
    released: Tuple[ReleasedSession, ...]
    exit_occupancy: Mapping

    @property
    def peak_live(self) -> Optional[int]:
        """The largest live count the controller established while this run held sessions.

        `None` exactly when no reading during the run could be established, which is
        deliberately not zero: zero is a proved count of nothing running.
        """
        return self.peak_occupancy.get("current")


def _require_stated_launches(launches: Sequence[RoleLaunch]) -> Tuple[RoleLaunch, ...]:
    """Refuse the whole run, before anything is spent, if any stated launch cannot be.

    Pre-flight rather than per-launch, and the difference is money: a run whose third
    stated launch names `orchestrator` would otherwise spend two real provider
    sessions before saying no to something it could have refused for free. Nothing is
    reserved, spawned, or sent by anything this function refuses.

    The refusal itself is the accepted one, called rather than reimplemented, so the
    driver cannot admit a role the door would refuse.
    """
    stated = tuple(launches)
    if not stated:
        raise InvocationRefused(
            REASON_NO_LAUNCH_STATED,
            "this driver launches exactly the sessions it was told to launch and "
            "discovers none; state at least one rail and the role it is assigned",
        )
    for launch in stated:
        _require_launchable_role(launch.role)
    return stated


def _release(
    controller: Any,
    held: Sequence[HeldSession],
    *,
    stop_kwargs: Optional[Mapping] = None,
) -> Tuple[Tuple[ReleasedSession, ...], Tuple[str, ...]]:
    """Stop every held session, in reverse order, whatever else has gone wrong.

    Reverse order because the last one admitted is the one nothing else was admitted
    against, so releasing it first keeps the occupancy this driver reports monotone
    while it unwinds.

    Every stop is attempted even after one of them raises. A stop that raised leaves a
    process group whose owner has already given up on it, and abandoning the remaining
    four to report the first failure sooner would turn one leak into five. The failures
    are collected and reported by the caller after every session has been attempted.
    """
    released: List[ReleasedSession] = []
    failures: List[str] = []
    for session in reversed(list(held)):
        try:
            stopped = controller.stop(session.binding, **dict(stop_kwargs or {}))
        except Exception as error:  # noqa: BLE001 - reported, never swallowed
            failures.append(
                "{0}/pgid {1}: {2}: {3}".format(
                    session.session_id, session.pgid, type(error).__name__, error
                )
            )
            continue
        released.append(
            ReleasedSession(
                session_id=stopped.session_id,
                rail=session.rail,
                role=session.role,
                pgid=session.pgid,
                binding_state=stopped.binding.state,
                process_group_gone=stopped.process_group_gone,
                graceful=stopped.graceful,
            )
        )
    return tuple(released), tuple(failures)


def _peak(readings: Sequence[Mapping]) -> Mapping:
    """The largest reading the controller established, chosen and never computed.

    Selection over the controller's own readings, not arithmetic on them: this returns
    one of the dictionaries it was handed, unmodified. A reading whose `current` is
    `None` is not comparable and is never chosen over one that was established, but it
    is also never treated as zero.
    """
    established = [reading for reading in readings if reading.get("current") is not None]
    if established:
        return max(established, key=lambda reading: reading["current"])
    return readings[-1] if readings else {"permitted": None, "current": None, "reason": None}


def drive_roles(
    snapshot,
    launches: Sequence[RoleLaunch],
    observation,
    *,
    controller: Any,
    reference: Any,
    package_root: Any,
    markers: Sequence = (),
    launch_kwargs: Optional[Mapping] = None,
    stop_kwargs: Optional[Mapping] = None,
    ledger: Optional[Any] = None,
    alive: Optional[Callable] = None,
    while_held: Optional[Callable] = None,
) -> DriverOutcome:
    """Admit and hold every stated launch at once, then release them all.

    One controller, one store, one registry, one read of the control plane: the
    snapshot every packet is bound to and the observation every decision is made from
    are the caller's single reading, so no two launches in a run can be decided
    against two revisions of the same scope.

    Each stated launch is admitted through `controller.open_role`, which re-reads the
    store and re-reconciles occupancy immediately before the accepted predicate sees
    it. A launch the predicate refuses -- the ceiling, a rail assigned another role, a
    rail that is not running, an iteration that moved -- is recorded with the reason
    its owner raised, and the run continues to the next stated launch. That is not
    leniency: the sessions already held are real, are still running, and a refusal on
    one rail says nothing about another. It is also what makes the ceiling observable,
    because a seventh stated launch against six held ones is refused while the six keep
    running.

    Anything that is not a refusal -- a lifecycle failure, a provider failure, a
    `while_held` that raised -- releases every held session and propagates unchanged.
    A failed launch is not retried, resumed, or terminalized without proof.

    `while_held` is the one instant at which every session this run opened is live at
    the same time. It is given the tuple of held sessions, asked for nothing back, and
    if it raises, everything is released and the failure propagates rather than being
    swallowed. It is offered to the caller and to nothing else; this driver does no
    work of its own inside that window.
    """
    stated = _require_stated_launches(launches)

    entry = controller.agent_count(alive=alive)
    held: List[HeldSession] = []
    refusals: List[StatedRefusal] = []
    readings: List[Mapping] = [entry]

    try:
        for launch in stated:
            packet = build_role_packet(snapshot, rail=launch.rail, role=launch.role)
            try:
                opened = controller.open_role(
                    snapshot,
                    packet,
                    observation,
                    reference=reference,
                    request_kwargs=launch.request_kwargs,
                    package_root=package_root,
                    markers=markers,
                    launch_kwargs=launch_kwargs,
                    stop_kwargs=stop_kwargs,
                    ledger=ledger,
                    alive=alive,
                )
            except InvocationRefused as refused:
                # Nothing was reserved, spawned or sent for this rail. Everything
                # already held stays held, because it was authorized on its own rail
                # and this refusal is not about it.
                refusals.append(
                    StatedRefusal(
                        rail=launch.rail,
                        role=launch.role,
                        reason=refused.reason,
                        detail=str(refused),
                    )
                )
                continue

            launched = opened.launched
            # Taken with this session live and owned, from the same controller that
            # admitted it against the same store and registry. Recorded, not computed.
            reading = controller.agent_count(alive=alive)
            readings.append(reading)
            held.append(
                HeldSession(
                    rail=opened.assignment.rail,
                    role=opened.assignment.role,
                    session_id=launched.binding.session_id,
                    iteration_blob=opened.assignment.iteration.blob,
                    pid=launched.owned.pid,
                    pgid=launched.owned.pgid,
                    binding=launched.binding,
                    occupancy=reading,
                )
            )

        if while_held is not None and held:
            while_held(tuple(held))
    except BaseException:
        _release(controller, held, stop_kwargs=stop_kwargs)
        raise

    released, failures = _release(controller, held, stop_kwargs=stop_kwargs)
    if failures:
        raise DriverError(
            REASON_RELEASE_FAILED,
            "{0} of {1} held sessions could not be stopped and their process groups "
            "are not proved gone: {2}".format(
                len(failures), len(held), "; ".join(failures)
            ),
        )

    return DriverOutcome(
        project=snapshot.project,
        ticket=snapshot.ticket,
        head=snapshot.head,
        entry_occupancy=entry,
        held=tuple(held),
        refusals=tuple(refusals),
        peak_occupancy=_peak(readings),
        released=released,
        exit_occupancy=controller.agent_count(alive=alive),
    )

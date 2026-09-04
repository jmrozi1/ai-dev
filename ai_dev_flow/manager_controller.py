"""The controller-owned manager surface: one process, its own handles, its own page."""

from __future__ import annotations

# Checkpoint 44 built an honest agent-count component and then had nowhere to put
# it. The reduction needs two halves of evidence -- the durable bindings and the
# ownership only the controller that started those processes can prove -- and no
# accepted composition held both. The renderer cannot reach either, and a fresh
# process that never launched anything owns no handles at all, so wiring the
# reduction into a standalone launcher would have reported `ownership-unprovable`
# forever whenever an agent was actually running.
#
# This module is that missing composition, and nothing more. One controller owns
# one `SessionRegistry` and one `BindingStore`, launches through them, and draws
# its page from them. The count on the page is therefore a count of what this
# controller itself is running, which is the only live occupancy anything here can
# honestly claim.
#
# Six boundaries hold it honest.
#
# First, ownership is held, never discovered. The registry is this object's own and
# deliberately non-durable, exactly as `session_lifecycle` requires: nothing here
# looks a process up by pid, adopts one from a durable record, scans the host, or
# writes ownership down so a later process could claim it. A controller that did
# not start a session says so and the page prints the reason.
#
# Second, the admission evidence and the drawn evidence are the same evidence. The
# store this controller reserves against is the store it counts, and the registry it
# launches into is the registry it counts, so the figure beside the queue cannot
# describe a different scope than the ceiling that admitted the work.
#
# Third, this adds no second state system. There is no daemon, no IPC, no polling
# loop, no cache of ownership, no scheduler, and no new lifecycle state. Every
# lifecycle method here forwards to the accepted function with this controller's own
# store and registry, adding no rule of its own; every rendering method forwards to
# the accepted composition with plain reduced values.
#
# Fourth, the reduction happens here because this is the only place that may hold
# both halves. `decision_manager_web` stays pure: it receives `current`, `permitted`
# and a reason, and imports no lifecycle, binding, or provider machinery to check
# them.
#
# Fifth, the runtime input rules are reused rather than respelled. Which repository,
# which instant, which stated exclusivity claim, which durable scope, and how a
# queue is acquired are all `decision_manager_launch`'s decisions, and this module
# calls them instead of keeping a second copy that could drift.
#
# Sixth, one run stays one run, and exactly one figure on it does not. The queue is
# acquired once against the run's own instant, the allowance is projected once, and
# a later instant is still a later run: nothing refreshes, nothing polls, and no
# endpoint exists that could ask for a newer one. Live agent occupancy is the sole
# exception, because it is the sole figure whose subject changes underneath the
# page -- an agent that was running when the server was built may be stopped
# before anyone loads it. So `serve` hands the accepted server this controller's
# way of taking that reading rather than a taken one, and the reading happens while
# a request is being answered. It is the same reduction, from the same store and
# the same registry, through the same single production home; only the instant
# moves, to the one instant at which the answer can be true for the person reading
# it.
#
# `dispatch` is the seam checkpoint 45 left open. `launch` was always the honest
# way to put a handle in this controller's registry, but the accepted production
# dispatch is `invoke_orchestrator`, which owns both gates and takes its store and
# registry from its caller. Handing it any others would admit an agent against one
# store and draw the count from another, so this method exists to make that
# impossible: the store, the registry, the reconciled occupancy, the durable
# bindings, and the in-flight ids all come from this object, and none of them is a
# parameter a caller could differ on. It adds no gate, no policy, and no state of
# its own -- every refusal below it stays the accepted predicate's.

import http.server
from pathlib import Path
import sys
from typing import Any, Callable, Dict, List, Mapping, NamedTuple, Optional, Tuple

from .authorization import AgentSlots, CONCURRENCY_CEILING_DEFAULT, reconcile_agent_slots
from .claude_allowance_view import AllowanceViewError
from .context_lifecycle import ContextLifecycleError, REASON_INVALID_THRESHOLD
from .decision_manager import (
    ManagerRun,
    ManagerRunError,
    make_live_manager_server,
    make_observed_manager_server,
    render_manager_page,
)
from .decision_manager_launch import (
    LaunchError,
    QueueSourceContext,
    load_run_queue,
    project_run_queue,
    resolve_run,
    resolve_run_scope,
    stated_run_inputs,
)
from .decision_manager_web import serve_forever
from .decision_queue import QueueView, SelectedDetail
from .orchestrator_invocation import InvocationOutcome, invoke_orchestrator
from .queue_source import QueueScope, QueueSourceError
from .role_invocation import OpenSession, invoke_role, open_role_session
from .session_binding import BindingRecord, BindingStore
from .session_lifecycle import (
    REASON_ROTATION_REQUIRES_RETIREMENT,
    ContextRelease,
    ContextReplacement,
    Continuation,
    LaunchOutcome,
    LifecycleError,
    SessionRegistry,
    StopOutcome,
    SupervisedTeardown,
    continue_from_durable_state,
    launch_session,
    ownership_evidence,
    release_continued_context,
    replace_old_context,
    single_liveness_snapshot,
    stop_session,
    supervised_teardown,
)

__all__ = [
    "ManagerController",
    "PageObservation",
    "REASON_OWNERSHIP_UNPROVABLE",
    "main",
]


class PageObservation(NamedTuple):
    """Everything one rendered response says about running sessions, from one instant.

    The rows and the aggregate are the two halves of a single question -- what is
    this controller running -- and a response that draws them from two observations
    can state a session is working in a row and that nothing provable is running in
    the figure beside it. Both would be honestly derived and the page would still be
    describing a moment that never existed.

    So they are produced together or not at all. This type exists to make that
    structural: there is no way to obtain one half of it without the other, and no
    way for a renderer to ask for a fresher aggregate than the rows it already has.
    It carries no liveness reading of its own and holds nothing open -- it is the
    finished output of one observation, not a handle onto one.
    """

    view: QueueView
    details: Mapping[str, SelectedDetail]
    agents: Dict[str, Any]

# The one reason this surface reports for a count it could not establish. It is the
# reconciler's answer restated for display, not a second rule: `reconcile_agent_slots`
# decides provability, and this names what it decided.
REASON_OWNERSHIP_UNPROVABLE = "ownership-unprovable"


class ManagerController:
    """One local controller: the handles it started, and the page that draws them.

    The registry and the store are constructed once, here, and every method uses
    those same two. That is the whole seam. A controller that launches through
    `launch` and renders through `page` is by construction reporting the sessions
    it actually owns, and a controller that launched nothing reports that it can
    prove nothing rather than reporting zero.

    `ceiling` is stated, never derived. It defaults to the same accepted human-owned
    constant `authorize` uses so one manager-wide number governs admission and
    display alike; a caller that runs a different ceiling states it in both places.
    """

    def __init__(
        self,
        source: QueueSourceContext,
        *,
        ceiling: int = CONCURRENCY_CEILING_DEFAULT,
        rotation_threshold: Optional[int] = None,
        registry: Optional[SessionRegistry] = None,
    ) -> None:
        self.source = source
        self.store = BindingStore(source.binding_root)
        # Injectable so a caller that already owns a registry composes this around
        # it rather than around a second one. Nothing is read from it at
        # construction, and nothing durable is created here.
        if registry is not None and rotation_threshold is not None:
            if registry.rotation_threshold != rotation_threshold:
                raise ContextLifecycleError(
                    REASON_INVALID_THRESHOLD,
                    "the injected registry rotates at {0} but this controller was told "
                    "{1}; one manager states one rotation policy.".format(
                        registry.rotation_threshold, rotation_threshold
                    ),
                )
        self.registry = (
            registry if registry is not None
            else SessionRegistry(rotation_threshold=rotation_threshold)
        )
        self.ceiling = ceiling
        # Two human-owned numbers that happen to share a default. `ceiling` is D6's
        # concurrency policy and this is D9's rotation policy; neither is derived
        # from the other, and moving one leaves the other exactly where it was.
        self.rotation_threshold = self.registry.rotation_threshold

    # ----------------------------------------------------------------------
    # Lifecycle: this controller's own handles
    # ----------------------------------------------------------------------

    def launch(self, decision, assignment, **kwargs: Any) -> LaunchOutcome:
        """Start a session into this controller's own registry and store.

        A pass-through on purpose. Every rule about authorization, reservation
        order, request construction, and failure handling stays in the accepted
        lifecycle; what this adds is that the handle lands in the registry this
        controller will later count.
        """
        return launch_session(
            decision, assignment, store=self.store, registry=self.registry, **kwargs
        )

    def stop(self, record: BindingRecord, **kwargs: Any) -> StopOutcome:
        """Tear down a session this controller owns, through the accepted lifecycle.

        Non-rotation teardown, and only that. The lifecycle decides which this is
        from the session's own rotation mark rather than from anything reaching it
        through here, so a marked session is refused whoever asks and however they
        ask. The one explicit refusal below closes the only door this signature
        opens on top of that: `**kwargs` would otherwise let a caller forward the
        lifecycle's private retirement authorization, and a controller-level
        teardown is never the retirement gate. Rotation goes to
        `retire_old_context`.
        """
        if "_retirement" in kwargs:
            raise LifecycleError(
                REASON_ROTATION_REQUIRES_RETIREMENT,
                "this is teardown and cannot carry a retirement authorization; a "
                "rotation must go through `retire_old_context`. Nothing was stopped.",
            )
        return stop_session(self.store, self.registry, record, **kwargs)

    def supervised_teardown(
        self,
        record: BindingRecord,
        *,
        now: str,
        stop: Optional[Callable] = None,
        alive: Optional[Callable] = None,
    ) -> SupervisedTeardown:
        """Stop a held session whose rotation category cannot be established.

        A pass-through, like `stop`, onto the accepted lifecycle route: the store
        and the registry are this controller's own, so the session released here is
        one this controller was counting. It states its parameters rather than
        taking `**kwargs`, which is the same door `stop` has to close explicitly --
        there is nothing private to forward through a signature that accepts
        nothing private.

        This controller adds no category rule of its own. The lifecycle refuses a
        marked session to `retire_old_context` and a provably unmarked one to
        ordinary teardown, and that decision is read from the session, here as
        everywhere else.
        """
        return supervised_teardown(
            self.store, self.registry, record, now=now, stop=stop, alive=alive
        )

    def replace_old_context(
        self,
        session_id: str,
        assignment,
        *,
        read_rail: Callable,
        read_handoff: Callable,
        read_worktree: Callable,
        read_observation: Callable,
        reference,
        request_kwargs,
        package_root,
        now: str,
        clock: Optional[Callable] = None,
        new_session_id: Optional[Callable] = None,
        start: Optional[Callable] = None,
        stop: Optional[Callable] = None,
        alive: Optional[Callable] = None,
        ready_timeout: Optional[float] = None,
    ) -> ContextReplacement:
        """Rotate one context: retire the old one, then bind a successor to the rail.

        A pass-through, like `launch` and `supervised_teardown`. The ordering, the
        proof that the predecessor is actually gone, the authorization of the
        replacement, and the refusal to send it anything all stay in the accepted
        lifecycle. What this adds is that both halves of the swap happen against the
        registry and the store this controller counts, so the slot the predecessor
        released and the slot the successor occupies are the same manager's slots
        and the figure this controller draws is right across the swap.

        Occupancy is this controller's own reduction, handed over as the reader the
        lifecycle calls once it has terminalized the predecessor and re-read the
        store. That keeps the ceiling this swap is admitted against the same number
        the manager page draws -- one manager states one concurrency policy, and a
        rotation is not an occasion to run a different one -- while leaving the
        reduction with the single production home it already had.

        It states its parameters rather than taking `**kwargs`, and every fact the
        rotation decides on arrives as a reader that the lifecycle calls at the
        moment it needs the fact -- there is no way through here to hand it a rail,
        a handoff, a worktree reading, an occupancy or an authorization that was
        true earlier.
        """
        return replace_old_context(
            self.store,
            self.registry,
            session_id=session_id,
            assignment=assignment,
            read_rail=read_rail,
            read_handoff=read_handoff,
            read_worktree=read_worktree,
            read_slots=lambda records: self.occupancy(records, alive=alive),
            read_observation=read_observation,
            reference=reference,
            request_kwargs=request_kwargs,
            package_root=package_root,
            now=now,
            clock=clock,
            new_session_id=new_session_id,
            start=start,
            stop=stop,
            alive=alive,
            ready_timeout=ready_timeout,
        )

    def continue_from_durable_state(
        self,
        session_id: str,
        assignment,
        *,
        read_rail: Callable,
        read_handoff: Callable,
        read_worktree: Callable,
        read_observation: Callable,
        request_kwargs,
        markers=(),
        send: Optional[Callable] = None,
        alive: Optional[Callable] = None,
        command_timeout: Optional[float] = None,
        finalize_handoff: Optional[Callable] = None,
    ) -> Continuation:
        """Resume a bound replacement's work from durable state, through this controller.

        A pass-through, like `launch` and `replace_old_context`. What the
        replacement is told, the refusal of a terminal predecessor, the
        authorization, and the fail-closed handling of a failed invocation all stay
        in the accepted lifecycle. What this adds is that the session continued here
        is one this controller holds a handle for and counts, so the invocation is
        admitted against the same occupancy the page beside it draws.

        Occupancy is this controller's own reduction, handed over as the reader the
        lifecycle calls at the moment it needs the figure -- one manager states one
        concurrency policy, and a continuation is not an occasion to run a different
        one.

        It states its parameters rather than taking `**kwargs`, and there is no
        `prompt` among them: what a replacement is told is resolved from durable
        state by the lifecycle, so nothing a caller holds in memory can become the
        work.
        """
        return continue_from_durable_state(
            self.store,
            self.registry,
            session_id=session_id,
            assignment=assignment,
            read_rail=read_rail,
            read_handoff=read_handoff,
            read_worktree=read_worktree,
            read_slots=lambda records: self.occupancy(records, alive=alive),
            read_observation=read_observation,
            request_kwargs=request_kwargs,
            markers=markers,
            send=send,
            alive=alive,
            command_timeout=command_timeout,
            finalize_handoff=finalize_handoff,
        )

    def release_continued_context(
        self,
        session_id: str,
        *,
        decision_id: str,
        now: str,
        publish_attention: Callable,
        stop: Optional[Callable] = None,
        alive: Optional[Callable] = None,
    ) -> ContextRelease:
        """Release a session this controller runs, by the category it proves now.

        A pass-through, like the rest. The category rule, the refusal of a marked
        session to the retirement gate, the supervised route, and the durable
        human-attention record that route owes a person all stay in the accepted
        lifecycle. What this adds is that the slot released is one this controller
        was counting.

        `publish_attention` is the caller's durable publication act, for the same
        reason every other durable read and write reaches the lifecycle as a
        collaborator: nothing under `session_lifecycle` opens a repository, and a
        controller that wrote one on its behalf would be moving that boundary rather
        than composing across it.
        """
        return release_continued_context(
            self.store,
            self.registry,
            session_id=session_id,
            decision_id=decision_id,
            now=now,
            publish_attention=publish_attention,
            stop=stop,
            alive=alive,
        )

    def dispatch(
        self,
        snapshot,
        proposal,
        packet,
        observation,
        *,
        orchestrator_rail: str,
        alive: Optional[Callable] = None,
        **kwargs: Any
    ) -> InvocationOutcome:
        """One gated dispatch, admitted against exactly what this controller draws.

        A pass-through, like `launch`. Both gates, the authorization predicate, the
        refusal reasons, the accounting, and the stop all stay in the accepted
        invocation; what this adds is that the session it starts lands in the
        registry this controller counts, in the store this controller counts,
        against the occupancy this controller reconciled.

        The store is read once here and the same records reach both the reduction
        and the predicate, so admission cannot be decided against one reading of
        the store while the ceiling was checked against another.

        `while_running` reaches the accepted invocation unchanged as one of
        `kwargs`. It is how the caller that owns this controller draws its page
        while the session this dispatch started is still running, which is the only
        instant at which a live count exists to draw.
        """
        records = self.store.records()
        return invoke_orchestrator(
            snapshot,
            proposal,
            packet,
            observation,
            orchestrator_rail=orchestrator_rail,
            store=self.store,
            registry=self.registry,
            slots=self.occupancy(records, alive=alive),
            bindings=records,
            in_flight_session_ids=self.registry.in_flight(),
            **kwargs
        )

    def dispatch_role(
        self,
        snapshot,
        packet,
        observation,
        *,
        alive: Optional[Callable] = None,
        **kwargs: Any
    ) -> InvocationOutcome:
        """One gated executor- or reviewer-role launch, admitted against what this draws.

        A pass-through, exactly like `dispatch`, and deliberately identical in every
        respect that matters: the store is read once and the same records reach both
        the reduction and the predicate, the session lands in the registry this
        controller counts, and every gate, refusal reason and accounting rule stays
        in `role_invocation`. This method adds no gate and no policy of its own.

        It is a second method rather than a `role` argument on `dispatch` because the
        two doors are not the same door. `dispatch` requires a material wake and
        starts an orchestrator; this one has no wake, cannot start an orchestrator,
        and refuses to start anything while this controller already holds a session.
        Collapsing them into one signature would put the wake gate behind a
        parameter, which is how a gate stops being one.
        """
        records = self.store.records()
        return invoke_role(
            snapshot,
            packet,
            observation,
            store=self.store,
            registry=self.registry,
            slots=self.occupancy(records, alive=alive),
            bindings=records,
            in_flight_session_ids=self.registry.in_flight(),
            **kwargs
        )

    def open_role(
        self,
        snapshot,
        packet,
        observation,
        *,
        alive: Optional[Callable] = None,
        **kwargs: Any
    ) -> OpenSession:
        """One gated role launch that is handed back still running, not stopped.

        The same pass-through as `dispatch_role`, onto the same door, differing in
        exactly one respect: the session is returned live, so the caller can hold it
        while it admits the next one. That is what makes occupancy grow between
        admissions instead of returning to zero, and it is why a driver built on this
        cannot admit a seventh agent: the sixth is still in this controller's store
        and this controller's registry when the seventh is reconciled.

        The store is read once here and the same records reach both the reduction and
        the predicate, so a launch cannot be admitted against one reading of the store
        while the ceiling was checked against another.

        This method adds no gate, no count and no policy of its own, and it does not
        stop anything. Teardown of what it opened is `stop`, and it is the caller's.
        """
        records = self.store.records()
        return open_role_session(
            snapshot,
            packet,
            observation,
            store=self.store,
            registry=self.registry,
            slots=self.occupancy(records, alive=alive),
            bindings=records,
            in_flight_session_ids=self.registry.in_flight(),
            **kwargs
        )

    def owned_session_ids(self) -> Tuple[str, ...]:
        """Exactly the sessions this controller holds a handle for. No inference."""
        return tuple(owned.session_id for owned in self.registry.sessions())

    # ----------------------------------------------------------------------
    # Context lifecycle: system-owned, not human attention
    # ----------------------------------------------------------------------

    def context_lifecycle(self) -> Dict[str, Dict[str, Any]]:
        """What this controller may honestly say about each held session's compaction.

        One entry per owned session: its observation health, its count when the
        count is trustworthy, and whether it is marked for graceful rotation. It is
        this controller's own registry answering about this controller's own
        handles, exactly as the occupancy reading is.

        Deliberately not a queue row and not an alert. A threshold mark is
        system-owned lifecycle state that a later rotation slice consumes; nothing
        about it needs a person to look at it, and putting it in front of one would
        make a mechanical fact compete with the decisions that do.
        """
        return self.registry.context_readings()

    def rotation_marked_session_ids(self) -> Tuple[str, ...]:
        """The sessions whose observed compactions have reached the threshold.

        A mark and nothing more. This checkpoint neither terminates, replaces, nor
        interrupts a marked session, and there is no code path here that could.
        """
        return self.registry.rotation_marked_session_ids()

    # ----------------------------------------------------------------------
    # The reading the page draws
    # ----------------------------------------------------------------------

    def occupancy(self, records, *, alive: Optional[Callable] = None) -> AgentSlots:
        """This controller's manager-wide occupancy, reconciled from its own halves.

        One reduction with one home. Admission and display both call it, so the
        ceiling a dispatch is admitted against is by construction the ceiling the
        page draws, reconciled from the same records and the same registry. The
        reconciler still decides what those come to; nothing here counts.

        The records are the caller's so one dispatch reads the store once and hands
        the same list to both this and the predicate, rather than reading a store
        twice and reconciling two different readings of it.
        """
        return reconcile_agent_slots(
            records,
            ownership=ownership_evidence(self.registry, records, alive=alive),
            ceiling=self.ceiling,
        )

    def agent_count(self, *, alive: Optional[Callable] = None) -> Dict[str, Any]:
        """Reduce this controller's occupancy to the reading the page draws.

        Both halves are this controller's: the durable records it admits against,
        and the ownership evidence its own registry can prove. The reconciler
        decides what that comes to; nothing here counts, subtracts, or repairs.

        `current` is `None` with a reason exactly when the reconciler could not
        establish the total -- a bound record whose handle this controller does not
        hold, one whose handle no longer matches, or a duplicate. That is
        deliberately not zero: zero is an established count of nothing running, and
        the page draws the two differently.
        """
        slots = self.occupancy(self.store.records(), alive=alive)
        return {
            "permitted": slots.ceiling,
            "current": slots.occupied if slots.provable else None,
            "reason": None if slots.provable else REASON_OWNERSHIP_UNPROVABLE,
        }

    # ----------------------------------------------------------------------
    # The page
    # ----------------------------------------------------------------------

    def queue(
        self,
        run: ManagerRun,
        *,
        expected_head: Optional[str] = None,
        alive: Optional[Callable] = None,
    ) -> Tuple[QueueView, Dict[str, SelectedDetail]]:
        """This run's queue, read through this controller's own registry.

        The same registry that answers the aggregate answers each row, so a row
        drawn Running and a count that includes it rest on one piece of evidence
        rather than two that could disagree.
        """
        return load_run_queue(
            run,
            self.source,
            registry=self.registry,
            store=self.store,
            expected_head=expected_head,
            alive=alive,
        )

    # ----------------------------------------------------------------------
    # One response, one instant
    # ----------------------------------------------------------------------

    def queue_scope(self, run: ManagerRun, *, expected_head: Optional[str] = None) -> QueueScope:
        """This run's durable control-plane authority, proven once.

        `queue` above is one acquisition for a caller that renders once. A caller
        that renders repeatedly from one run splits it here instead: this half
        reaches the coordination remote and is taken exactly once, and `observe`
        below repeats only the half that can have changed.

        `run` is taken so that a scope is asked for in the context of the run it
        will be projected at, rather than resolved loose and paired up later. The
        stated scope itself is this controller's, exactly as it is for `queue`.
        """
        return resolve_run_scope(self.source, expected_head=expected_head)

    def observe(
        self, scope: QueueScope, run: ManagerRun, *, alive: Optional[Callable] = None
    ) -> PageObservation:
        """One bounded observation: this controller's rows and its aggregate, together.

        This is the whole seam. `page` and `serve` below ask two questions --
        `queue(alive=...)` and `agent_count(alive=...)` -- and each takes its own
        liveness reading. Between them a worker can exit, and one response then
        draws a row from before that exit and a count from after it. Neither half
        is wrong; the response is, because the two describe different moments and
        the page presents them as one.

        So the observation is taken here, once, and both halves are derived from
        it. `single_liveness_snapshot` is the accepted checkpoint-49 primitive and
        this is deliberately the same one the read below already uses: handing it
        the outer snapshot means `_work_items` takes its snapshot over an
        observation that is already fixed, so the inner guarantee is unchanged and
        the outer one extends it from one read to one response. A snapshot of a
        snapshot answers each process group exactly once, from the outer reading.

        It is a snapshot and not a cache, for exactly the reason the primitive is.
        Nothing here is stored on this object, shared between responses, refreshed
        or invalidated. It is created by one response and dies with it, and the
        next response asks again from scratch and gets that instant's truth. A
        controller that held one of these open would be serving a claim about
        processes nobody re-observed, which is the durable liveness cache the
        lifecycle refuses.

        `alive` is still the caller's, and is snapshotted rather than used directly
        for the same reason: a caller may state which prober answers, and may not
        decide how many instants one response spans.
        """
        observed = single_liveness_snapshot(alive)
        view, details = project_run_queue(
            scope,
            run,
            self.source,
            registry=self.registry,
            store=self.store,
            alive=observed,
        )
        return PageObservation(view=view, details=details, agents=self.agent_count(alive=observed))

    def observed_page(
        self,
        run: ManagerRun,
        scope: QueueScope,
        *,
        alive: Optional[Callable] = None,
        template_path: Optional[Path] = None,
    ) -> str:
        """One response's complete page, rows and aggregate from one observation."""
        seen = self.observe(scope, run, alive=alive)
        return render_manager_page(
            run, seen.view, seen.details, agents=seen.agents, template_path=template_path
        )

    def serve_observed(
        self,
        run: ManagerRun,
        scope: QueueScope,
        *,
        alive: Optional[Callable] = None,
        port: int = 0,
        template_path: Optional[Path] = None,
    ) -> http.server.HTTPServer:
        """A loopback server that observes once per request and renders that instant.

        `serve` established that a live count must be reduced while a request is
        being answered rather than frozen into the page, and that is kept exactly.
        What it could not do is keep the rows honest at the same time: they were
        acquired before the server existed, so a session this controller started
        afterwards could never appear in one -- and once the aggregate did move,
        the rows and the figure beside them were readings of two different moments.

        This passes the accepted server one observation source instead of a frozen
        queue plus a live reading. Each request takes exactly one observation from
        this controller's own scope, store and registry, and the whole document is
        rendered from it, so the rows and the aggregate on any single response are
        the same instant by construction rather than by timing.

        Everything whose subject cannot change under the page stays this run's and
        is still projected once: the allowance windows are taken at construction,
        and `scope` pins the revision, the rails and the decisions every response
        is served from. So this re-observes without re-fetching -- there is no
        timer, no watcher, no poller, and no request that reaches the coordination
        remote.

        No host is passed, for the reason `serve` passes none: the accepted server
        owns the loopback rule and stays the only place that decides what this
        surface binds.
        """
        return make_observed_manager_server(
            run,
            lambda: self.observe(scope, run, alive=alive),
            port=port,
            template_path=template_path,
        )

    def page(
        self,
        run: ManagerRun,
        view: QueueView,
        details: Mapping[str, SelectedDetail],
        *,
        alive: Optional[Callable] = None,
        template_path: Optional[Path] = None,
    ) -> str:
        """This controller's complete page, aggregate included."""
        return render_manager_page(
            run,
            view,
            details,
            agents=self.agent_count(alive=alive),
            template_path=template_path,
        )

    def serve(
        self,
        run: ManagerRun,
        view: QueueView,
        details: Mapping[str, SelectedDetail],
        *,
        alive: Optional[Callable] = None,
        port: int = 0,
        template_path: Optional[Path] = None,
    ) -> http.server.HTTPServer:
        """A loopback server that reduces this controller's occupancy per request.

        Checkpoint 46 established this reading from the right halves and then froze
        it into the page at construction. A frozen live count is wrong the moment
        anything starts or stops, and in the supported dispatch it was wrong at
        every instant a client could reach the page at all, because the session it
        counted had already been stopped by then. So the reading is not passed here
        as a value: what is passed is this controller's own way of taking it, and
        the accepted server calls it while it answers.

        It is still this controller's reduction, from this controller's exact store
        and exact registry, through the one production home of
        `reconcile_agent_slots`. Only the instant moves -- from when the server was
        built to when a client asked -- and that instant is the only one at which
        the answer can be true.

        Everything else stays a projection of this one run. The queue and the
        details are the caller's, taken once; a later instant is still a later run.

        No host is passed, for the reason the accepted launcher passes none: the
        accepted server owns the loopback rule and stays the only place that
        decides what this surface binds.
        """
        return make_live_manager_server(
            run,
            view,
            details,
            agents=lambda: self.agent_count(alive=alive),
            port=port,
            template_path=template_path,
        )


# --------------------------------------------------------------------------
# The human entry point
# --------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    """Run one controller: its stated scope, its own registry, its own page.

    This is the supported live-occupancy surface. What it reports is what this
    controller owns, which at the moment it starts is nothing -- so it prints the
    number of handles it holds rather than letting the page imply more, and the
    aggregate reports `0 / 6` only when the store genuinely holds no nonterminal
    binding. Durable bindings this controller did not start make the total
    unprovable, and the page prints that reason instead of a number.

    The rows and that aggregate come from one observation per response rather than
    from two readings taken in sequence. This process launches nothing, so the
    honest answer here rarely changes -- but "rarely changes" is not a property a
    surface may rely on, and a page that could report a session working beside a
    figure that proves nothing is running would be describing a moment that never
    existed whether or not this particular process can reach it.

    The claim and the scope rules are the accepted launcher's and are called, not
    restated, so there is one place a run states what it is about.
    """
    try:
        claim, source = stated_run_inputs(list(sys.argv[1:] if argv is None else argv))
        run = resolve_run(human_exclusive_since=claim, source=source)
    except LaunchError as exc:
        print("manager-controller: {0}".format(exc), file=sys.stderr)
        return 1

    controller = ManagerController(source)

    print("allowance store: {0}".format(run.store.path))
    print("progress record: {0}".format(run.progress.relative))
    print("run instant: {0}".format(run.now))
    print(
        "human exclusivity: {0}".format(
            "none claimed" if run.human_exclusive_since is None
            else "since {0}".format(run.human_exclusive_since)
        )
    )
    print(
        "queue source: {0}/{1} in {2}".format(
            source.project, source.ticket, source.control_plane
        )
    )
    print("owned session handles: {0}".format(len(controller.owned_session_ids())))

    # The durable half once, against the remote; the half a session can change is
    # observed below and again on every request, which is the only way either
    # figure can still be true when someone reads it.
    try:
        scope = controller.queue_scope(run)
        seen = controller.observe(scope, run)
    except QueueSourceError as exc:
        print("manager-controller: {0}".format(exc), file=sys.stderr)
        return 2

    # Both lines come from `seen`, so this summary is one instant for the same
    # reason a served response is: a row count and an occupancy taken from two
    # readings could describe two different moments to the person reading them.
    print("queue rows: {0}".format(len(seen.view.rows)))
    reading = seen.agents
    print(
        "live occupancy: {0}".format(
            "{0} / {1}".format(reading["current"], reading["permitted"])
            if reading["current"] is not None
            else "not established ({0})".format(reading["reason"])
        )
    )

    try:
        server = controller.serve_observed(run, scope)
    except QueueSourceError as exc:
        print("manager-controller: {0}".format(exc), file=sys.stderr)
        return 2
    except (AllowanceViewError, ManagerRunError) as exc:
        print("manager-controller: {0}".format(exc), file=sys.stderr)
        return 3

    host, port = server.server_address[:2]
    print("manager: http://{0}:{1}/".format(host, port))
    try:
        serve_forever(server)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

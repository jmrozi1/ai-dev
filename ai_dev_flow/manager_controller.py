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
# Sixth, one run stays one run. The queue is acquired once against the run's own
# instant and the occupancy is reduced once for the page that is rendered once.
# Nothing refreshes, and a later instant is a later run.
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
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from .authorization import AgentSlots, CONCURRENCY_CEILING_DEFAULT, reconcile_agent_slots
from .claude_allowance_view import AllowanceViewError
from .decision_manager import (
    ManagerRun,
    ManagerRunError,
    make_manager_server,
    render_manager_page,
)
from .decision_manager_launch import (
    LaunchError,
    QueueSourceContext,
    load_run_queue,
    resolve_run,
    stated_run_inputs,
)
from .decision_manager_web import serve_forever
from .decision_queue import QueueView, SelectedDetail
from .orchestrator_invocation import InvocationOutcome, invoke_orchestrator
from .queue_source import QueueSourceError
from .session_binding import BindingRecord, BindingStore
from .session_lifecycle import (
    LaunchOutcome,
    SessionRegistry,
    StopOutcome,
    launch_session,
    ownership_evidence,
    stop_session,
)

__all__ = [
    "ManagerController",
    "REASON_OWNERSHIP_UNPROVABLE",
    "main",
]

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
        registry: Optional[SessionRegistry] = None,
    ) -> None:
        self.source = source
        self.store = BindingStore(source.binding_root)
        # Injectable so a caller that already owns a registry composes this around
        # it rather than around a second one. Nothing is read from it at
        # construction, and nothing durable is created here.
        self.registry = registry if registry is not None else SessionRegistry()
        self.ceiling = ceiling

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
        """Stop a session this controller owns, through the accepted lifecycle."""
        return stop_session(self.store, self.registry, record, **kwargs)

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

    def owned_session_ids(self) -> Tuple[str, ...]:
        """Exactly the sessions this controller holds a handle for. No inference."""
        return tuple(owned.session_id for owned in self.registry.sessions())

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
            expected_head=expected_head,
            alive=alive,
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
        """A loopback server holding this controller's page, rendered once.

        No host is passed, for the reason the accepted launcher passes none: the
        accepted server owns the loopback rule and stays the only place that
        decides what this surface binds.
        """
        return make_manager_server(
            run,
            view,
            details,
            agents=self.agent_count(alive=alive),
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

    The claim and the scope rules are the accepted launcher's and are called, not
    restated, so there is one place a run states what it is about.
    """
    try:
        claim, source = stated_run_inputs(list(sys.argv[1:] if argv is None else argv))
        run = resolve_run(human_exclusive_since=claim)
    except LaunchError as exc:
        print("manager-controller: {0}".format(exc), file=sys.stderr)
        return 1

    controller = ManagerController(source)

    print("allowance store: {0}".format(run.store.path))
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

    try:
        view, details = controller.queue(run)
    except QueueSourceError as exc:
        print("manager-controller: {0}".format(exc), file=sys.stderr)
        return 2

    print("queue rows: {0}".format(len(view.rows)))
    reading = controller.agent_count()
    print(
        "live occupancy: {0}".format(
            "{0} / {1}".format(reading["current"], reading["permitted"])
            if reading["current"] is not None
            else "not established ({0})".format(reading["reason"])
        )
    )

    try:
        server = controller.serve(run, view, details)
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

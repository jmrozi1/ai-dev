"""One process: the dispatch it performs, and the page drawn while it runs."""

from __future__ import annotations

# Checkpoint 45 built the controller-owned composition correctly and left one
# residue. `ManagerController` owns one `BindingStore` and one `SessionRegistry`,
# launches through them, reduces occupancy from them, and draws its page from them
# -- but the only supported process that served that page never launched anything.
# Its registry was therefore always empty, so the shipped aggregate could report a
# number only when nothing was running, and reported `ownership-unprovable` in the
# one state the ceiling exists for. The count was computable and never exposed.
#
# This module is the missing seam and nothing else: the process that performs the
# dispatch is the process that draws the page, and it dispatches through the exact
# store and the exact registry that page counts.
#
# Seven boundaries hold it honest.
#
# First, one controller. Exactly one `ManagerController` is constructed here, and
# every store, registry, reduction, queue, and page below comes from that one
# object. There is no second registry, no second store, and no second ownership
# system of any kind -- an agent admitted against one store and drawn from another
# is precisely the defect this closes.
#
# Second, ownership is still held, never discovered. Nothing here looks a process
# up by pid, adopts a durable binding, scans the host, or writes ownership down for
# a later process to claim. This process counts what it started; a binding it did
# not start stays unprovable and the page prints that reason.
#
# Third, the page is reachable across the live window, not merely rendered inside
# it. Checkpoint 46 rendered the page from within `while_running`, which really did
# establish the count from a genuinely running session -- and then stopped that
# session before the server began answering, so the only number anyone could ever
# fetch described a slot that was already empty. The order here is the whole fix:
# the surface is bound and answering first, and the dispatch happens behind it. A
# dispatched session occupies a slot from the moment it binds until the moment it
# is stopped, and for that entire window a real client can ask this page what is
# running and be told the truth.
#
# Fourth, one run stays one run, and what is not a projection of this run is
# observed rather than projected. The durable control-plane scope is resolved once
# and the allowance is projected once, because both describe state that outlives
# the render; there is no timer, no watcher, no refresh, and no request that
# reaches the coordination remote. What this run's sessions are doing is the
# exception, and it has to be: it is true only of the instant it was observed, and
# this process starts its session after the page is already answering, so a reading
# taken before the dispatch describes a moment in which this controller owns
# nothing. Both the rows and the occupancy are therefore observed while a request
# is being answered, from the same controller, the same store, and the same
# registry -- and from one observation, so a single response cannot draw a row from
# one instant and the figure beside it from another.
#
# Fifth, this adds no second service, no IPC, no polling loop, and no scheduler,
# priority model, fairness policy, or autoscaler. There is exactly one server, the
# accepted one, bound once and answering the one path it has always answered. What
# changed is when it starts answering: `start_serving` runs the accepted socket
# loop so that it spans this process's dispatch instead of beginning after it. That
# loop blocks on the socket and ends when it is shut down -- it asks nothing, polls
# nothing, schedules nothing, and knows nothing about agents, ownership, or
# occupancy. A page that only becomes reachable once the work it describes is over
# cannot describe that work, which is the defect this closes and not a trade
# available to make differently.
#
# Sixth, it decides nothing a gate already decides. Both gates, the authorization
# predicate, the reservation, the ceiling, the refusal reasons, the stop, and every
# queue-source refusal stay where they are; this module composes them and adds no
# rule of its own. A refusal is reported with the reason its owner raised.
#
# Seventh, every input is stated. There is no configuration file, no environment
# variable, no discovery step, and no default runtime policy anywhere below: the
# claim and the scope are stated exactly as the accepted entry point requires them,
# and the runtime policy a launch needs is stated in the same way, because a
# manager that inferred which prompt, which plugin, or which budget to spend would
# be inventing an authority nobody granted it.

import argparse
import http.server
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Mapping, Optional, Sequence, Tuple

from .authorization import (
    ControlPlaneObservation,
    RailObservation,
    WorkspaceObservation,
)
from .claude_allowance_view import AllowanceViewError
from .control_plane import (
    ControlPlaneError,
    RailState,
    ReadSource,
    collect_rail_states,
    rail_blob_sha,
    resolve_read_source,
)
from .decision_manager import ManagerRun, ManagerRunError
from .decision_manager_launch import (
    LaunchError,
    resolve_run,
    stated_run_inputs,
)
from .decision_manager_web import Serving, start_serving
from .manager_controller import ManagerController
from .orchestrator_invocation import InvocationOutcome, InvocationRefused
from .orchestrator_trigger import (
    OrchestratorPacket,
    ScopeSnapshot,
    TriggerError,
    WakeProposal,
    build_packet,
    build_snapshot,
    propose_wake,
)
from .queue_source import QueueSourceError
from .repository import RepositoryError, resolve_repo_root
from .tickets import TicketModelError, TicketReference
from .workspaces import (
    IdentityProblem,
    canonical_ticket_key,
    effective_worktree_id,
    verify_workspace_ticket_identity,
)

__all__ = [
    "ALLOWED_TOOL_FLAG",
    "CONTROLLER_ROOT_FLAG",
    "DispatchError",
    "DispatchInputs",
    "DispatchedRun",
    "EXPECTED_SKILL_FLAG",
    "LiveSurface",
    "MAX_BUDGET_FLAG",
    "MAX_TURNS_FLAG",
    "ORCHESTRATOR_RAIL_FLAG",
    "PLUGIN_ROOT_FLAG",
    "PROMPT_FILE_FLAG",
    "REASON_INVALID_RUNTIME",
    "REASON_RUNTIME_UNSTATED",
    "REASON_SURFACE_UNREACHABLE",
    "REASON_TICKET_UNSTATED",
    "TICKET_ID_FLAG",
    "TICKET_PROVIDER_FLAG",
    "TICKET_REPOSITORY_FLAG",
    "dispatch_behind",
    "main",
    "observe_scope",
    "open_surface",
    "prove_workspace",
    "stated_dispatch_inputs",
]

# This module's own refusals, and only its own. Everything a gate, a reader, a
# runtime validator, or the accepted entry point already refuses keeps that
# owner's reason; giving one fact a second spelling here is how two rules drift.
REASON_RUNTIME_UNSTATED = "runtime-policy-unstated"
REASON_INVALID_RUNTIME = "invalid-runtime-policy"
REASON_TICKET_UNSTATED = "ticket-reference-unstated"

# A dispatch behind a page nobody can reach is exactly the defect this module was
# reopened to close, so it is refused rather than performed and reported on. This
# is a structural precondition, not a runtime condition a supported run can meet
# by accident: `open_surface` returns a surface that is already answering.
REASON_SURFACE_UNREACHABLE = "surface-unreachable"

# The rail whose standing authorization a dispatch is decided against. Stated,
# because which rail may spend a session is a human's durable decision and not
# something a manager may pick from what happens to be running.
ORCHESTRATOR_RAIL_FLAG = "--orchestrator-rail"

# Which durable ticket this workspace must prove it owns before it may start
# anything in it. `session_binding` re-proves this at reservation; stating it here
# is what lets the authorization gate refuse before a process exists.
TICKET_PROVIDER_FLAG = "--ticket-provider"
TICKET_ID_FLAG = "--ticket-id"
TICKET_REPOSITORY_FLAG = "--ticket-repository"

# The runtime policy one launch is spent under. Every one is required and none has
# a default: an agent whose prompt, plugin, tool set, turn cap, or budget was
# inferred is an agent whose bounds nobody stated.
CONTROLLER_ROOT_FLAG = "--controller-root"
PROMPT_FILE_FLAG = "--prompt-file"
PLUGIN_ROOT_FLAG = "--plugin-root"
EXPECTED_SKILL_FLAG = "--expected-skill"
ALLOWED_TOOL_FLAG = "--allowed-tool"
MAX_TURNS_FLAG = "--max-turns"
MAX_BUDGET_FLAG = "--max-budget-usd"


class DispatchError(Exception):
    """A refusal to dispatch, carrying the exact reason."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__("{0}: {1}".format(reason, detail))
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class DispatchInputs:
    """Everything one dispatch needs that is not this controller's to decide.

    Frozen and without defaults, for the reason `QueueSourceContext` has none: a
    field that could be filled in is a field a run could fail to state and still
    spend a session under bounds nobody chose.
    """

    orchestrator_rail: str
    reference: TicketReference
    request_kwargs: Mapping
    package_root: Path


@dataclass(frozen=True)
class LiveSurface:
    """The manager page, bound and already answering, before anything runs behind it.

    Both halves are here because both are needed and neither is optional. `server`
    is the accepted loopback server, which is listening from the moment it is
    constructed; `serving` is the accepted socket loop answering on it, which is
    what turns a listening socket into a page a person can actually read. A
    dispatch performed behind only the first of those is checkpoint 46's defect.
    """

    server: http.server.HTTPServer
    serving: Serving


@dataclass(frozen=True)
class DispatchedRun:
    """What one dispatch performed behind a live surface produced.

    `reading` is this process's own record of the occupancy while its session was
    running. It is not what the page serves -- the page reduces its own reading
    when a client asks -- and it is deliberately kept separate: one is what this
    run observed at the accepted observation point, the other is what a reader was
    told at the instant they read. They agree while the session runs, and the
    second is the one that has to keep being true afterwards.
    """

    outcome: InvocationOutcome
    reading: Mapping


# --------------------------------------------------------------------------
# What the accepted predicate consumes, read from durable state
# --------------------------------------------------------------------------


def prove_workspace(
    repo_root: Path, *, reference: TicketReference
) -> WorkspaceObservation:
    """This worktree's proven identity, or the exact reason it is unproven.

    Every part is the accepted workspace authority's answer: the worktree id Git
    assigns this checkout, the canonical key the claim registry files it under, and
    the identity verdict that registry returns. Nothing here decides ownership, and
    an unproven identity is carried through verbatim rather than softened, because
    the authorization predicate refuses on exactly that text.
    """
    root = Path(repo_root)
    problem = verify_workspace_ticket_identity(root, reference=reference)
    return WorkspaceObservation(
        workspace_key=canonical_ticket_key(reference),
        worktree_id=effective_worktree_id(root),
        workspace_path=str(root),
        identity_problem=(
            problem.detail if isinstance(problem, IdentityProblem) else None
        ),
    )


def _rail_observation(source: ReadSource, state: RailState, *, project: str, ticket: str):
    return RailObservation(
        identifier=state.identifier,
        status=state.status,
        rail_blob=rail_blob_sha(
            source, project=project, ticket=ticket, rail=state.identifier
        )
        or "",
        role=state.role,
        unreconciled=state.unreconciled,
        depends_on=tuple(state.depends_on),
        shared_resource=state.shared_resource,
    )


def observe_scope(
    source: ReadSource,
    *,
    project: str,
    ticket: str,
    workspace: WorkspaceObservation,
) -> ControlPlaneObservation:
    """One scope at one resolved revision, in the shape `authorize` consumes.

    A reduction and nothing more. The rail facts are `control_plane`'s, read once
    from one revision; each rail's iteration is the blob id that same read serves,
    so a rail rewritten between the read and the decision cannot pass as the one
    that was authorized.

    `foreign_resource_holders` is deliberately left at its accepted default. This
    reader walks one scope, so the only shared-resource contention it can prove is
    contention inside that scope, which the predicate derives from the rails below.
    Reporting holders it never looked for would be a claim, not an observation.
    """
    states = collect_rail_states(source, project=project, ticket=ticket)
    return ControlPlaneObservation(
        project=project,
        ticket=ticket,
        head=source.head,
        rails=tuple(
            _rail_observation(source, state, project=project, ticket=ticket)
            for state in states
        ),
        workspace=workspace,
    )


# --------------------------------------------------------------------------
# The composition: one controller, one page already answering, one dispatch behind it
# --------------------------------------------------------------------------


def open_surface(
    controller: ManagerController,
    run: ManagerRun,
    *,
    alive: Optional[Callable] = None,
    port: int = 0,
    template_path: Optional[Path] = None,
) -> LiveSurface:
    """This run's page, answering, before anything is dispatched behind it.

    The ordering is the entire product change on this rail, so it is a function
    with a name rather than three statements inside a larger one: the surface opens
    first, and everything after it happens while a reader can already look.

    What is taken once is taken here, and it is exactly the half that cannot change
    under the page: this run's durable control-plane scope -- the revision being
    served, the rails it authorizes, the decisions it publishes -- resolved through
    this controller against the coordination remote. Resolving it again per request
    would fetch that remote per request, which is the polling loop this surface is
    not permitted to become.

    What is deliberately not taken here is anything whose subject is a running
    session. That used to mean the occupancy alone, and it was half a fix. This
    process dispatches *behind* this surface, so at the instant this function runs
    the registry is empty by construction and always will be -- a queue acquired
    here can only ever show rows for sessions this process has not started yet, and
    would keep showing them for the entire life of the page. The live-session
    reading was computable and, on this surface, permanently unreachable.

    So the rows and the occupancy are both taken per request now, and both from one
    observation, through `controller.serve_observed`. That is one change, not two:
    they are two halves of the single question "what is this controller running",
    and answering them at two instants is how one response comes to report a
    session working in a row and nothing provable in the figure beside it.

    Nothing is dispatched from here and nothing may be. This function's whole
    responsibility is that the page exists and answers before a session does.
    """
    scope = controller.queue_scope(run)
    server = controller.serve_observed(
        run, scope, alive=alive, port=port, template_path=template_path
    )
    return LiveSurface(server=server, serving=start_serving(server))


def dispatch_behind(
    controller: ManagerController,
    surface: LiveSurface,
    *,
    snapshot: ScopeSnapshot,
    proposal: Optional[WakeProposal],
    packet: OrchestratorPacket,
    observation: ControlPlaneObservation,
    inputs: DispatchInputs,
    alive: Optional[Callable] = None,
    launch_kwargs: Optional[Mapping] = None,
    stop_kwargs: Optional[Mapping] = None,
    ledger: Any = None,
) -> DispatchedRun:
    """One gated dispatch, performed while the surface above it already answers.

    The dispatch goes through the controller, so it is admitted against that
    controller's exact store, exact registry, and reconciled occupancy -- unchanged
    from checkpoint 46, which got that part right. What changed is that the page
    counting this session is reachable for the whole time the session holds its
    slot, so the count is not merely established but observable while it is true.

    The surface is required, and required to be answering, because a dispatch
    behind an unreachable page is precisely the shape that made checkpoint 46's
    count false at every instant anyone could fetch it. Checking it here makes that
    ordering a precondition rather than a convention a later edit could quietly
    reverse.

    Nothing about the dispatch's lifecycle changes. The accepted invocation still
    stops the session and terminalizes its binding as soon as the observation
    returns; no session is held open to keep a page warm, because an idle worker
    kept alive for a viewer is allowance spent on nothing -- and it is not needed,
    since the page reduces a fresh reading for whoever asks next and will report
    the empty slot as honestly as it reported the full one.

    `while_running` is still used and still means what it meant: the one instant at
    which this process can record what it itself had running. That record is this
    run's own, printed as run information; it is not the number the page serves.

    `ledger` is passed straight through and defaults to nothing, exactly as the
    accepted invocation defines it. Allowance accounting for this dispatch is a
    separate decision and is deliberately not made here.
    """
    if not surface.serving.answering():
        raise DispatchError(
            REASON_SURFACE_UNREACHABLE,
            "the manager surface is not answering yet; a dispatch performed behind "
            "a page no client can reach can only ever be counted after it ended",
        )

    observed: dict = {}

    def observe(launched) -> None:
        # Inside the live window: the process started, the handle is in this
        # controller's own registry, and the binding is nonterminal.
        observed["reading"] = controller.agent_count(alive=alive)

    outcome = controller.dispatch(
        snapshot,
        proposal,
        packet,
        observation,
        orchestrator_rail=inputs.orchestrator_rail,
        alive=alive,
        reference=inputs.reference,
        request_kwargs=inputs.request_kwargs,
        package_root=inputs.package_root,
        launch_kwargs=launch_kwargs,
        stop_kwargs=stop_kwargs,
        ledger=ledger,
        while_running=observe,
    )
    return DispatchedRun(outcome=outcome, reading=observed["reading"])


# --------------------------------------------------------------------------
# The stated runtime policy
# --------------------------------------------------------------------------


def _stated_reference(arguments: argparse.Namespace) -> TicketReference:
    """The ticket this workspace must prove it owns, exactly as stated.

    The reference's own validity is `tickets`' decision and is left to it; what is
    refused here is silence, because a workspace whose ticket nobody named cannot
    have its ownership proved against anything.
    """
    if not arguments.ticket_provider or not arguments.ticket_id:
        raise DispatchError(
            REASON_TICKET_UNSTATED,
            "state {0} and {1}; a dispatch proves this workspace owns a named "
            "ticket before it starts anything in it".format(
                TICKET_PROVIDER_FLAG, TICKET_ID_FLAG
            ),
        )
    try:
        return TicketReference(
            provider=arguments.ticket_provider,
            ticket_id=arguments.ticket_id,
            repository=arguments.ticket_repository,
        )
    except TicketModelError as exc:
        raise DispatchError(REASON_INVALID_RUNTIME, str(exc)) from exc


def _stated_bounds(arguments: argparse.Namespace) -> Tuple[int, float]:
    """Text to numbers, and nothing else.

    Whether the resulting bounds are usable stays `claude_runtime`'s decision,
    applied in one place, so a second opinion here could not drift from it.
    """
    try:
        return int(arguments.max_turns), float(arguments.max_budget_usd)
    except (TypeError, ValueError) as exc:
        raise DispatchError(
            REASON_INVALID_RUNTIME,
            "{0} takes a whole number of turns and {1} an amount, got {2!r} and "
            "{3!r}".format(
                MAX_TURNS_FLAG,
                MAX_BUDGET_FLAG,
                arguments.max_turns,
                arguments.max_budget_usd,
            ),
        ) from exc


def stated_dispatch_inputs(argv: Sequence[str]) -> Tuple[DispatchInputs, List[str]]:
    """This run's stated dispatch inputs, and the argv the accepted parser owns.

    Only this module's own flags are consumed. Everything else is handed back
    untouched so `stated_run_inputs` stays the one place that decides what the
    claim and the scope require and what silence means about either; a second
    parser for those here would be a second rule free to drift from it.
    """
    arguments, remaining = _build_parser().parse_known_args(list(argv))

    missing = [
        flag
        for flag, value in (
            (ORCHESTRATOR_RAIL_FLAG, arguments.orchestrator_rail),
            (CONTROLLER_ROOT_FLAG, arguments.controller_root),
            (PROMPT_FILE_FLAG, arguments.prompt_file),
            (PLUGIN_ROOT_FLAG, arguments.plugin_root),
            (EXPECTED_SKILL_FLAG, arguments.expected_skill),
            (MAX_TURNS_FLAG, arguments.max_turns),
            (MAX_BUDGET_FLAG, arguments.max_budget_usd),
        )
        if value is None
    ]
    if not arguments.allowed_tools:
        missing.append(ALLOWED_TOOL_FLAG)
    if missing:
        raise DispatchError(
            REASON_RUNTIME_UNSTATED,
            "state {0}; this dispatch reads no configuration file and infers no "
            "runtime policy, so a bound it cannot name is a bound it will not "
            "spend a session under".format(", ".join(missing)),
        )

    turns, budget = _stated_bounds(arguments)
    try:
        package_root = resolve_repo_root()
    except RepositoryError as exc:
        raise DispatchError(REASON_INVALID_RUNTIME, str(exc)) from exc

    return (
        DispatchInputs(
            orchestrator_rail=arguments.orchestrator_rail,
            reference=_stated_reference(arguments),
            request_kwargs={
                "controller_root": Path(arguments.controller_root),
                "prompt_file": Path(arguments.prompt_file),
                "plugin_root": Path(arguments.plugin_root),
                "expected_skill": arguments.expected_skill,
                "allowed_tools": tuple(arguments.allowed_tools),
                "max_turns": turns,
                "max_budget_usd": budget,
            },
            package_root=package_root,
        ),
        list(remaining),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manager-dispatch",
        description=(
            "Perform one gated dispatch and serve the manager page it draws from "
            "that running session."
        ),
        # Exact flags only. Abbreviation would let this parser swallow `--ticket`,
        # which belongs to the accepted scope rules below it, and a flag consumed
        # by the wrong owner is a rule silently replaced by another.
        allow_abbrev=False,
    )
    parser.add_argument(ORCHESTRATOR_RAIL_FLAG, dest="orchestrator_rail", default=None)
    parser.add_argument(TICKET_PROVIDER_FLAG, dest="ticket_provider", default=None)
    parser.add_argument(TICKET_ID_FLAG, dest="ticket_id", default=None)
    parser.add_argument(TICKET_REPOSITORY_FLAG, dest="ticket_repository", default=None)
    parser.add_argument(CONTROLLER_ROOT_FLAG, dest="controller_root", default=None)
    parser.add_argument(PROMPT_FILE_FLAG, dest="prompt_file", default=None)
    parser.add_argument(PLUGIN_ROOT_FLAG, dest="plugin_root", default=None)
    parser.add_argument(EXPECTED_SKILL_FLAG, dest="expected_skill", default=None)
    parser.add_argument(
        ALLOWED_TOOL_FLAG, dest="allowed_tools", action="append", default=[]
    )
    parser.add_argument(MAX_TURNS_FLAG, dest="max_turns", default=None)
    parser.add_argument(MAX_BUDGET_FLAG, dest="max_budget_usd", default=None)
    return parser


# --------------------------------------------------------------------------
# The human entry point
# --------------------------------------------------------------------------


def _read_scope(source_context, inputs: DispatchInputs):
    """One resolved read of the coordination repository, reduced four ways.

    All four come from the same `ReadSource`, so the snapshot a wake was proposed
    against, the packet bound to that head, and the observation a decision is made
    from cannot describe three different revisions.
    """
    read = resolve_read_source(Path(source_context.control_plane))
    snapshot = build_snapshot(
        read, project=source_context.project, ticket=source_context.ticket
    )
    observation = observe_scope(
        read,
        project=source_context.project,
        ticket=source_context.ticket,
        workspace=prove_workspace(inputs.package_root, reference=inputs.reference),
    )
    return snapshot, propose_wake(snapshot), build_packet(snapshot), observation


def _describe(reading: Mapping) -> str:
    return (
        "{0} / {1}".format(reading["current"], reading["permitted"])
        if reading["current"] is not None
        else "not established ({0})".format(reading["reason"])
    )


def main(argv: Optional[List[str]] = None) -> int:
    """One stated run, one page already answering, and one gated dispatch behind it.

    The order is the whole design. Every input is stated before anything is read;
    exactly one controller is constructed and owns the only store and the only
    registry below it; the durable scope is read once; the page is bound and starts
    answering; and only then is a session dispatched through that same controller.
    So the count beside the queue is a count of the session this process itself
    started, and it is readable for the whole time that session holds its slot.

    It keeps being readable afterwards, and keeps being true. The page reduces its
    own reading when a client asks, so once the dispatch has stopped its session
    the next fetch reports the empty slot rather than continuing to assert the full
    one. Nothing is held open to keep a number alive.

    A run with nothing material awake still serves. That is not a degraded mode: the
    scope was read and had no material wake in it, and the aggregate then reports an
    established `0 / 6`, or the reason a durable binding this controller did not
    start cannot be counted. A refused dispatch serves the same way, with the gate's
    own reason printed, because a refusal rendered as an empty page is the outcome
    this entry point must never produce.

    What is printed is bounded run information -- paths, the instant, counts, the
    reading this process observed, and the address actually bound. No decision body,
    evidence, prompt, binding, or session identity is printed.
    """
    stated = list(sys.argv[1:] if argv is None else argv)
    try:
        inputs, remaining = stated_dispatch_inputs(stated)
        claim, source = stated_run_inputs(remaining)
        run = resolve_run(human_exclusive_since=claim)
    except (DispatchError, LaunchError) as exc:
        print("manager-dispatch: {0}".format(exc), file=sys.stderr)
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
    print("orchestrator rail: {0}".format(inputs.orchestrator_rail))
    print("owned session handles: {0}".format(len(controller.owned_session_ids())))

    try:
        snapshot, proposal, packet, observation = _read_scope(source, inputs)
    except (ControlPlaneError, TriggerError) as exc:
        print("manager-dispatch: {0}".format(exc), file=sys.stderr)
        return 2

    print("control-plane head: {0}".format(snapshot.head))

    # Before the dispatch, on purpose. Everything below this line happens while a
    # real client can already fetch this page and be told what is running.
    try:
        surface = open_surface(controller, run)
    except QueueSourceError as exc:
        print("manager-dispatch: {0}".format(exc), file=sys.stderr)
        return 2
    except (AllowanceViewError, ManagerRunError) as exc:
        print("manager-dispatch: {0}".format(exc), file=sys.stderr)
        return 3

    host, port = surface.server.server_address[:2]
    print("manager: http://{0}:{1}/".format(host, port))
    try:
        try:
            dispatched = dispatch_behind(
                controller,
                surface,
                snapshot=snapshot,
                proposal=proposal,
                packet=packet,
                observation=observation,
                inputs=inputs,
            )
            print("dispatched session role: {0}".format(dispatched.outcome.role))
            print("live occupancy: {0}".format(_describe(dispatched.reading)))
        except InvocationRefused as exc:
            # A gate said no. That is a fact about this head, not a failure of the
            # manager, so the page keeps answering -- and it reports what this
            # controller can actually prove, which is that it started nothing.
            print("no dispatch this run: {0}".format(exc))
            print("live occupancy: {0}".format(_describe(controller.agent_count())))
        surface.serving.wait()
    finally:
        surface.serving.stop()
        surface.server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

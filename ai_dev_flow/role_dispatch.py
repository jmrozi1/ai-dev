"""One process: one stated role assignment, one managed session, started and stopped."""

from __future__ import annotations

# This is the supported entry point for the capability the human middle cut
# authorized: launching a managed session in the `executor` role and in the
# `reviewer` role. Before it, `manager_dispatch.main` was the only shipped `main()`
# that could start a managed session at all, and it started orchestrators only.
#
# It is a sibling of `manager_dispatch`, not a replacement for it and not a
# generalisation of it. Nothing in that module is touched, because the two entry
# points are answering different questions and the differences are the design:
#
#   * `manager_dispatch` is woken. It reads a scope, asks `propose_wake` whether
#     anything material happened, and dispatches an orchestrator only if something
#     did. That wake gate is what stops a controller authorizing itself from an
#     event it produced, and it is left exactly as it was.
#
#   * This process is stated. Nothing wakes it: a human names a rail and a role on
#     the command line, and the rail's durable `Role:` and `Status:` decide whether
#     that is allowed. Adding a wake here would mean inventing a trigger for
#     "an executor rail is ready", which is the autonomous continuation loop this
#     ticket has explicitly deferred.
#
# One process, one role, one session, and then it returns.
#
# `--role` is stated once per run, and so is every runtime-policy flag beside it,
# because the prompt file and the plugin a session runs under are per-role. That is
# not a limitation this module works around -- it is why one process runs one role.
# Running an executor and then a reviewer is two runs of this program, one after the
# other, and there is deliberately no way to ask it for both: a process that could
# hold a second role's session is the concurrent driver, and that is a different
# entry point -- `role_driver_dispatch` -- rather than a mode of this one. There is
# no loop, no pool, no thread, no scheduler, and no queue in this file, and
# `invoke_role` still stops the session it started before it returns.
#
# Checkpoint 74 removed the door-level refusal that used to enforce this a second
# time (`role_invocation._require_sequential`, `session-already-live`). This file's
# single-session shape is therefore now its own -- one `dispatch_role` call, no loop
# -- and no longer a rule the module below it imposed on every caller. Nothing about
# what this program does changed with that removal; what changed is that the
# guarantee is local rather than global, which is stated here so a reader is not
# looking for a refusal that no longer exists.
#
# No page is served, and that is a deliberate trade rather than an oversight.
# `manager_dispatch` serves one because its whole point was that a live occupancy be
# readable while the session it counts is running, and it blocks on that server
# until it is shut down. A launcher that blocked would make "one session at a time,
# bounded, foreground" harder to hold, not easier. So this process makes no page
# claim at all -- it prints the occupancy it observed at the one instant its own
# session was live, from the same controller, the same store and the same registry
# that admitted it, and the accepted surfaces stay the surfaces.
#
# Every input is stated. There is no configuration file, no environment variable,
# no discovery step, and no default runtime policy: which rail, which role, which
# ticket this workspace must prove it owns, which prompt, which plugin, which
# tools, which turn cap and which budget are all named on the command line, because
# a manager that inferred any of them would be inventing an authority nobody
# granted it.

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from .control_plane import ControlPlaneError, resolve_read_source
from .decision_manager_launch import LaunchError, stated_run_inputs
from .manager_controller import ManagerController
from .manager_dispatch import (
    ALLOWED_TOOL_FLAG,
    CONTROLLER_ROOT_FLAG,
    EXPECTED_SKILL_FLAG,
    MAX_BUDGET_FLAG,
    MAX_TURNS_FLAG,
    PLUGIN_ROOT_FLAG,
    PROMPT_FILE_FLAG,
    REASON_INVALID_RUNTIME,
    REASON_RUNTIME_UNSTATED,
    REASON_TICKET_UNSTATED,
    TICKET_ID_FLAG,
    TICKET_PROVIDER_FLAG,
    TICKET_REPOSITORY_FLAG,
    DispatchError,
    observe_scope,
    prove_workspace,
)
from .orchestrator_invocation import InvocationRefused
from .orchestrator_trigger import TriggerError, build_snapshot
from .repository import RepositoryError, resolve_repo_root
from .role_invocation import (
    LAUNCHABLE_ROLES,
    REASON_ROLE_NOT_LAUNCHABLE,
    build_role_packet,
)
from .tickets import TicketModelError, TicketReference

__all__ = [
    "RAIL_FLAG",
    "ROLE_FLAG",
    "REASON_ROLE_UNSTATED",
    "RoleRunInputs",
    "main",
    "stated_role_inputs",
]

# The rail whose standing authorization this session is decided against, and the
# role it is decided for. Both stated, because which rail may spend a session and
# what that session is permitted to be are a human's durable decisions, not
# something a manager may pick from what happens to be running.
RAIL_FLAG = "--rail"
ROLE_FLAG = "--role"

REASON_ROLE_UNSTATED = "role-unstated"

# The flag names, the missing-input rule, the ticket rule, the bounds rule and the
# refusal reasons are `manager_dispatch`'s and are imported rather than restated.
# Two spellings of "state your runtime policy" are two rules free to drift, and a
# run refused by one of them and admitted by the other is exactly the drift that
# matters.


@dataclass(frozen=True)
class RoleRunInputs:
    """Everything one role launch needs that is not this controller's to decide.

    Frozen and without defaults, for the reason `DispatchInputs` has none: a field
    that could be filled in is a field a run could fail to state and still spend a
    session under bounds and in a role nobody chose.
    """

    rail: str
    role: str
    reference: TicketReference
    request_kwargs: Mapping
    package_root: Path


def _stated_reference(arguments: argparse.Namespace) -> TicketReference:
    if not arguments.ticket_provider or not arguments.ticket_id:
        raise DispatchError(
            REASON_TICKET_UNSTATED,
            "state {0} and {1}; a launch proves this workspace owns a named ticket "
            "before it starts anything in it".format(TICKET_PROVIDER_FLAG, TICKET_ID_FLAG),
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
    try:
        return int(arguments.max_turns), float(arguments.max_budget_usd)
    except (TypeError, ValueError) as exc:
        raise DispatchError(
            REASON_INVALID_RUNTIME,
            "{0} takes a whole number of turns and {1} an amount, got {2!r} and "
            "{3!r}".format(
                MAX_TURNS_FLAG, MAX_BUDGET_FLAG, arguments.max_turns, arguments.max_budget_usd
            ),
        ) from exc


def _stated_role(arguments: argparse.Namespace) -> str:
    """The one role this run may start, refused here rather than at the provider.

    Refused by name and early, because `orchestrator` is not a value this entry
    point is permitted to carry: an orchestrator is started by `manager_dispatch`
    behind a material-wake gate, and a role flag that accepted it would be a way to
    start one without that gate. `role_invocation` refuses it again at the door; the
    refusal here is so the reason reaches a person at the command line rather than
    after a control-plane read.
    """
    if arguments.role is None:
        raise DispatchError(
            REASON_ROLE_UNSTATED,
            "state {0} as one of {1}; this process starts one session in one stated "
            "role and infers neither".format(ROLE_FLAG, ", ".join(LAUNCHABLE_ROLES)),
        )
    if arguments.role not in LAUNCHABLE_ROLES:
        # The door's own reason, imported rather than respelled, so a person told no
        # at the command line is told no in the same words the gate would use.
        raise DispatchError(
            REASON_ROLE_NOT_LAUNCHABLE,
            "{0} must be one of {1}; got {2!r}. An orchestrator is started by "
            "`manager_dispatch`, behind a material-wake gate this entry point does "
            "not have.".format(ROLE_FLAG, ", ".join(LAUNCHABLE_ROLES), arguments.role),
        )
    return arguments.role


def stated_role_inputs(argv: Sequence[str]) -> Tuple[RoleRunInputs, List[str]]:
    """This run's stated role inputs, and the argv the accepted scope parser owns.

    Only this module's own flags are consumed. Everything else is handed back
    untouched so `stated_run_inputs` stays the one place that decides what the scope
    requires and what silence means about it.
    """
    arguments, remaining = _build_parser().parse_known_args(list(argv))

    missing = [
        flag
        for flag, value in (
            (RAIL_FLAG, arguments.rail),
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
            "state {0}; this launch reads no configuration file and infers no runtime "
            "policy, so a bound it cannot name is a bound it will not spend a session "
            "under".format(", ".join(missing)),
        )

    role = _stated_role(arguments)
    turns, budget = _stated_bounds(arguments)
    try:
        package_root = resolve_repo_root()
    except RepositoryError as exc:
        raise DispatchError(REASON_INVALID_RUNTIME, str(exc)) from exc

    return (
        RoleRunInputs(
            rail=arguments.rail,
            role=role,
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
        prog="role-dispatch",
        description=(
            "Start one managed session in one stated role on one authorized rail, "
            "run its directive, and stop it."
        ),
        # Exact flags only, for the reason `manager_dispatch` gives: abbreviation
        # would let this parser swallow `--ticket`, which belongs to the accepted
        # scope rules below it, and a flag consumed by the wrong owner is a rule
        # silently replaced by another.
        allow_abbrev=False,
    )
    parser.add_argument(RAIL_FLAG, dest="rail", default=None)
    parser.add_argument(ROLE_FLAG, dest="role", default=None)
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


def _read_scope(source_context, inputs: RoleRunInputs):
    """One resolved read of the coordination repository, reduced two ways.

    Both come from the same `ReadSource`, so the snapshot the packet is bound to and
    the observation the decision is made from cannot describe two revisions.

    `propose_wake` is deliberately not called. Nothing here is woken, so proposing a
    wake and then ignoring it would be a claim this process does not make -- and
    passing one to a door that has no wake gate would be worse.
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
    return snapshot, observation


def _describe(reading: Mapping) -> str:
    return (
        "{0} / {1}".format(reading["current"], reading["permitted"])
        if reading["current"] is not None
        else "not established ({0})".format(reading["reason"])
    )


def main(argv: Optional[List[str]] = None) -> int:
    """One stated role assignment, one gated launch, one session, stopped before return.

    The order is the design. Every input is stated before anything is read; exactly
    one controller is constructed and owns the only store and the only registry
    below it; the durable scope is read once; and one session is launched through
    that same controller, in the stated role, against the standing authorization the
    named rail carries for exactly that role.

    A refusal is reported with the reason its owner raised and exits non-zero. That
    is not a degraded success: this process exists to spend one session, so a run
    that spent none has not done the thing it was asked to do, and saying so at the
    exit code is how a caller can tell without parsing prose.

    What is printed is bounded run information -- the scope, the rail, the role, the
    head, the session identity this controller itself minted, the occupancy it
    observed while its own session was live, and how the session ended. No prompt,
    no provider content, no decision body, and no evidence.
    """
    stated = list(sys.argv[1:] if argv is None else argv)
    try:
        inputs, remaining = stated_role_inputs(stated)
        _claim, source = stated_run_inputs(remaining)
    except (DispatchError, LaunchError) as exc:
        print("role-dispatch: {0}".format(exc), file=sys.stderr)
        return 1

    controller = ManagerController(source)

    print(
        "scope: {0}/{1} in {2}".format(source.project, source.ticket, source.control_plane)
    )
    print("binding root: {0}".format(source.binding_root))
    print("rail: {0}".format(inputs.rail))
    print("role: {0}".format(inputs.role))
    print("owned session handles: {0}".format(len(controller.owned_session_ids())))

    try:
        snapshot, observation = _read_scope(source, inputs)
    except (ControlPlaneError, TriggerError) as exc:
        print("role-dispatch: {0}".format(exc), file=sys.stderr)
        return 2

    print("control-plane head: {0}".format(snapshot.head))

    observed: dict = {}

    def observe(launched: Any) -> None:
        # The one instant this launch is live and provable: the process started, the
        # handle is in this controller's own registry, and the binding is
        # nonterminal. It is the only instant at which a live count exists to draw,
        # and it is drawn from the same controller that was admitted against it.
        observed["reading"] = controller.agent_count()
        observed["pid"] = launched.owned.pid
        observed["pgid"] = launched.owned.pgid
        observed["binding_role"] = launched.binding.role
        observed["request_role"] = launched.request.role

    try:
        packet = build_role_packet(snapshot, rail=inputs.rail, role=inputs.role)
        outcome = controller.dispatch_role(
            snapshot,
            packet,
            observation,
            reference=inputs.reference,
            request_kwargs=inputs.request_kwargs,
            package_root=inputs.package_root,
            while_running=observe,
        )
    except InvocationRefused as exc:
        # A gate said no. That is a fact about this head and this rail, reported with
        # the reason its owner raised, and nothing was reserved, spawned or sent.
        print("no launch this run: {0}".format(exc), file=sys.stderr)
        print("live occupancy: {0}".format(_describe(controller.agent_count())))
        return 3

    print("session: {0}".format(outcome.session_id))
    print("launched role: {0}".format(outcome.role))
    print("binding role: {0}".format(observed["binding_role"]))
    print("runtime request role: {0}".format(observed["request_role"]))
    print("worker pid/pgid: {0}/{1}".format(observed["pid"], observed["pgid"]))
    print("iteration: {0}".format(outcome.iteration_blob))
    print("live occupancy: {0}".format(_describe(observed["reading"])))
    print("binding state: {0}".format(outcome.binding_state))
    print("process group gone: {0}".format(outcome.process_group_gone))
    print("graceful: {0}".format(outcome.graceful))
    print("live occupancy after stop: {0}".format(_describe(controller.agent_count())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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
#
# The run bound -- `--command-timeout`, how long the one invocation this session is
# sent may take -- is the one exception, and it is stated as an exception rather
# than left to be discovered. It is optional, and a run that does not name it gets
# exactly what every run got before the flag existed: `claude_worker`'s own
# `DEFAULT_COMMAND_TIMEOUT_SECONDS`. Until this checkpoint it was not nameable at
# all. `session_lifecycle.launch_session` has always taken a `command_timeout` and
# plumbed it into the send, but no word of `timeout` appeared anywhere on this path,
# so every managed executor and reviewer session ran under a ten-minute cap that no
# operator could raise. It is optional rather than required because making it
# required would refuse every invocation that states everything the shipped contract
# asks for; see `_stated_run_bound`.
#
# What that cap actually did, and what this flag does NOT fix. A dogfooded executor
# session bound, worked for nine and a half minutes, committed its result, and was
# killed by the run bound before it published its handoff. The commit is real and
# durable in git; the binding is left nonterminal; and the control plane records
# neither, so an orchestrator reading only the control plane concludes nothing
# happened. That is a THIRD entry point into the recorded
# leaked-nonterminal-binding-with-no-release-mechanism family -- after the pre-spawn
# raise (checkpoint 79 s8b) and the readiness failure (checkpoint 82) -- and it is
# the first of the three that occurs *after* a successful bind, on the send in
# `session_lifecycle.launch_session`, which deliberately leaves the record `bound`
# because bound is the truth. Raising the bound makes the timeout less likely; it
# does not give that binding a release mechanism, and this rail did not authorize
# building one. It is recorded here so the next reader finds the third door named
# rather than rediscovering it from a lost session.

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from .claude_runtime import REASON_PLUGIN_ROLE_MISMATCH
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
    "COMMAND_TIMEOUT_FLAG",
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

# The run bound: how long the one invocation this session is sent may take before
# the manager stops waiting for it. It is this module's own flag rather than one
# imported from `manager_dispatch`, because it is a bound on the role-launch path
# only -- the orchestrator entry point keeps the run bound it already had, and
# nothing here raises a default for any other caller.
#
# It is stated in seconds and it is optional, which makes it the one bound on this
# command line with a fallback. See `_stated_run_bound` for why.
COMMAND_TIMEOUT_FLAG = "--command-timeout"

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
    # The one field that may be `None`, and it is still without a default: a run
    # must state it into the constructor, and `None` says "the run bound the worker
    # already applies" rather than "nobody thought about it". It is not in
    # `request_kwargs` because it is not part of the runtime request -- it bounds
    # the send, not the session's policy -- and `launch_session` takes it as its
    # own parameter.
    command_timeout: Optional[float]


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


def _stated_run_bound(arguments: argparse.Namespace) -> Optional[float]:
    """How long this session's one invocation may run, in seconds, or the shipped bound.

    The same parsing and the same refusal as `_stated_bounds` -- one conversion,
    `REASON_INVALID_RUNTIME` on failure -- because this is one more stated bound
    beside `--max-turns` and `--max-budget-usd` and not a new kind of input.

    It differs from those two in exactly one respect, deliberately: it is optional.
    Every other runtime input on this command line is required, and the module says
    why -- a bound a run cannot name is a bound it will not spend a session under.
    Requiring this one would refuse every invocation that states everything the
    shipped contract asks for, so absence keeps precisely the behaviour those runs
    already have: nothing is passed to `launch_session`, `send_arguments` carries no
    `timeout`, and `claude_worker.run_request` applies its own
    `DEFAULT_COMMAND_TIMEOUT_SECONDS`. This entry point therefore raises no default
    and lowers none; it only lets an operator say a different number.

    A non-positive or non-finite bound is refused rather than carried. `run_request`
    turns the value into `time.monotonic() + timeout`, so zero, a negative, or a NaN
    is a deadline that has already expired: the worker would bind, the record would
    turn `bound`, and the very first read would time out. That is not a bound, it is
    the leaked-nonterminal-binding failure recorded at the top of this module, with
    extra steps, and it is refused at the command line where a person can still fix
    it.

    `launch_session` also takes a `ready_timeout`, and it is deliberately NOT exposed
    beside this one. The two bound different things: this bounds the *work*, whose
    right value is a property of the assignment and which demonstrably needed to be
    larger than the shipped ten minutes; `ready_timeout` bounds how long a freshly
    spawned worker may take to say hello, which is a property of the host and the SDK
    import and not of the rail. Nothing has shown 30 seconds to be the wrong number
    for that, and raising it would only delay the discovery of a broken worker. When
    a slow host does exceed it, that is its own slice with its own evidence -- and
    its failure lands in the same leaked-binding family recorded above, at the
    readiness door checkpoint 82 already named.
    """
    if arguments.command_timeout is None:
        return None
    try:
        seconds = float(arguments.command_timeout)
    except (TypeError, ValueError) as exc:
        raise DispatchError(
            REASON_INVALID_RUNTIME,
            "{0} takes a number of seconds, got {1!r}".format(
                COMMAND_TIMEOUT_FLAG, arguments.command_timeout
            ),
        ) from exc
    if not seconds > 0.0 or seconds == float("inf"):
        raise DispatchError(
            REASON_INVALID_RUNTIME,
            "{0} must be a positive, finite number of seconds, got {1!r}; a bound "
            "that has already expired binds a session only to time it out".format(
                COMMAND_TIMEOUT_FLAG, arguments.command_timeout
            ),
        )
    return seconds


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


def _require_role_package(role: str, expected_skill: Optional[str]) -> None:
    """The stated role and the stated package must be the same role's, said early.

    `--role`, `--prompt-file`, `--plugin-root` and `--expected-skill` are four
    independent operator inputs. Until checkpoint 75 nothing compared them, so
    `--role executor` on an executor-assigned rail could be handed the reviewer
    package and every role-fidelity check in the product would agree: the packet,
    the snapshot, the observation, the `Assignment` and the durable binding would
    all say `executor`, and the provider would load the reviewer's skill.

    The gate that actually closes that is in `claude_runtime.validate_plugin_surface`,
    reached from `_build_request` with the role read off the durable binding record,
    where no caller can answer it. This is not that gate and does not replace it --
    it is the same refusal said at the command line, before a control plane is read
    or a binding reserved, in the reason the gate itself would raise. Same shape as
    `_stated_role`: the door refuses again, and a person gets told no in the door's
    own words rather than after a launch has been paid for.
    """
    if expected_skill != role:
        raise DispatchError(
            REASON_PLUGIN_ROLE_MISMATCH,
            "{0} is '{1}' but {2} is '{3}'; a session runs the package of the role "
            "it is launched in. State the package whose skill is '{1}'.".format(
                ROLE_FLAG, role, EXPECTED_SKILL_FLAG, expected_skill
            ),
        )


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
    _require_role_package(role, arguments.expected_skill)
    turns, budget = _stated_bounds(arguments)
    run_bound = _stated_run_bound(arguments)
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
            command_timeout=run_bound,
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
    parser.add_argument(COMMAND_TIMEOUT_FLAG, dest="command_timeout", default=None)
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
            command_timeout=inputs.command_timeout,
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

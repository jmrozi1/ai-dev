"""One process: several stated role assignments, held live together, then released."""

from __future__ import annotations

# The supported entry point for the concurrent driver, and a sibling of
# `role_dispatch` in exactly the way `role_dispatch` is a sibling of
# `manager_dispatch`: a separate `main()`, reachable as
# `python -m ai_dev_flow.role_driver_dispatch`, that starts managed sessions and
# nothing else. `manager_dispatch` and `orchestrator_invocation` are untouched, and
# `orchestrator` is refused here twice -- once by the per-launch flag parser this
# module borrows from `role_dispatch`, and again by the accepted door -- so the
# orchestrator's material-wake gate stays the only way an orchestrator starts.
#
# Why this is a second entry point rather than a mode of `role_dispatch`. That
# program is one process, one role, one session, and its whole shape is that claim;
# turning it into a driver by adding a repeatable flag would delete the claim while
# leaving the sentence. So the sequential launcher stays exactly what it was, and
# this stands beside it.
#
# Every input is still stated and nothing is inferred. There is no configuration
# file, no environment variable, no discovery step and no default runtime policy.
# The command line names each launch in full:
#
#   --rail <rail> --role <role> --ticket-provider ... --ticket-id ...
#     --controller-root ... --prompt-file ... --plugin-root ... --expected-skill ...
#     --allowed-tool ... --max-turns N --max-budget-usd X
#   --rail <another rail> --role <another role>  ... (the same again, in full)
#   --control-plane ... --project ... --ticket ... --binding-root ...
#
# Each `--rail` opens a launch group and every flag after it belongs to that group
# until the next `--rail`; anything a group's parser does not recognise falls through
# to the accepted scope parser, so `--project` and friends may be written anywhere.
# The per-group flag names, the missing-input rule, the ticket rule, the bounds rule,
# the role refusal and every reason string are `role_dispatch`'s, called rather than
# restated -- one group's argv is parsed by exactly the function that parses a whole
# `role_dispatch` run.
#
# The repetition is deliberate and is not a convenience gap. The prompt file, the
# plugin, the expected skill, the tool allowance, the turn cap and the budget are all
# per role, and a driver that let one launch inherit another's runtime policy would
# be a driver that could run a reviewer under the executor's role package. Stating
# each launch in full is what makes "this session ran that role's package" a fact
# about the command line rather than about a defaulting rule.
#
# What this process does NOT do, because none of it is authorized: it discovers no
# work, ranks nothing, retries nothing, waits for nothing, and adds no agent because
# a slot is free. It launches exactly the launches it was told to launch, holds them
# together, and releases them.
#
# No page is served, for the reason `role_dispatch` gives and which has not changed:
# the accepted surface blocks on its socket loop until shut down, and a blocking
# driver would make "every process group proven gone before this process exits"
# harder to hold rather than easier. This process makes no page claim; it prints the
# occupancy readings its own controller produced at the instants its own sessions
# were live, and returns. That trade is now larger than it was, because this is the
# first process in the package at which a manager page would have something worth
# looking at, and it is stated rather than hidden.

import sys
from typing import List, Mapping, Optional, Sequence, Tuple

from .control_plane import ControlPlaneError
from .decision_manager_launch import LaunchError, stated_run_inputs
from .manager_controller import ManagerController
from .manager_dispatch import DispatchError
from .orchestrator_invocation import InvocationRefused
from .orchestrator_trigger import TriggerError
from .role_dispatch import (
    RAIL_FLAG,
    RoleRunInputs,
    # The accepted single read of the coordination repository -- one `ReadSource`,
    # one snapshot, one observation, `propose_wake` deliberately not called -- taken
    # from the sequential entry point rather than respelled here. Two spellings of
    # "read the scope once" are two readings free to disagree.
    _read_scope,
    stated_role_inputs,
)
from .role_driver import (
    REASON_NO_LAUNCH_STATED,
    DriverError,
    RoleLaunch,
    drive_roles,
)

__all__ = [
    "REASON_LAUNCH_SCOPE_DISAGREEMENT",
    "main",
    "stated_launch_groups",
]

# One process proves one workspace owns one ticket, so every stated launch in a run
# must name the same ticket reference. Two references in one run would mean one
# controller spending sessions against two different ownership proofs, and the
# workspace can only prove one of them.
REASON_LAUNCH_SCOPE_DISAGREEMENT = "launch-scope-disagreement"


def _split_groups(argv: Sequence[str]) -> Tuple[List[str], List[List[str]]]:
    """Cut argv at each `--rail` into one argv per stated launch, plus the rest.

    A positional split rather than a nested syntax, because the alternative is a
    packed value (`--launch rail=...,role=...`) that would need its own parser, its
    own quoting rule and its own refusals -- a second command-line language beside
    the accepted one. Cutting at `--rail` lets each group be parsed by the accepted
    per-launch parser unchanged.
    """
    scope: List[str] = []
    groups: List[List[str]] = []
    current: Optional[List[str]] = None
    for token in argv:
        if token == RAIL_FLAG:
            current = [token]
            groups.append(current)
        elif current is None:
            scope.append(token)
        else:
            current.append(token)
    return scope, groups


def stated_launch_groups(
    argv: Sequence[str],
) -> Tuple[Tuple[RoleRunInputs, ...], List[str]]:
    """This run's stated launches, and the argv the accepted scope parser owns.

    Each group is parsed by `role_dispatch.stated_role_inputs`, which is the same
    function that parses an entire sequential run, so a launch this driver admits
    had to state exactly what a `role_dispatch` run must state -- and is refused in
    exactly the same words when it does not.
    """
    scope, groups = _split_groups(list(argv))
    if not groups:
        raise DispatchError(
            REASON_NO_LAUNCH_STATED,
            "state at least one {0} with the role and runtime policy that belong to "
            "it; this driver launches exactly what it is told to launch and discovers "
            "nothing".format(RAIL_FLAG),
        )

    stated: List[RoleRunInputs] = []
    for group in groups:
        inputs, remaining = stated_role_inputs(group)
        stated.append(inputs)
        scope.extend(remaining)

    references = {inputs.reference for inputs in stated}
    if len(references) > 1:
        raise DispatchError(
            REASON_LAUNCH_SCOPE_DISAGREEMENT,
            "every launch in one run is decided against one workspace's proof that it "
            "owns one ticket, and this run states {0} different ticket references: "
            "{1}".format(
                len(references),
                ", ".join(sorted(str(reference) for reference in references)),
            ),
        )
    return tuple(stated), scope


def _describe(reading: Mapping) -> str:
    return (
        "{0} / {1}".format(reading["current"], reading["permitted"])
        if reading.get("current") is not None
        else "not established ({0})".format(reading.get("reason"))
    )


def main(argv: Optional[List[str]] = None) -> int:
    """Several stated role assignments, admitted one at a time, held live together.

    The order is the design and it is the same order `role_dispatch` uses, extended
    by exactly one step. Every input is stated before anything is read; exactly one
    controller is constructed and owns the only store and the only registry below it;
    the durable scope is read once, so every launch in the run is decided against one
    revision of it; and then each stated launch is admitted in turn against the
    occupancy that already includes everything this run is still holding.

    A refusal on one rail is reported with the reason its owner raised and does not
    stop the run: the sessions already held were authorized on their own rails, are
    still running, and a refusal says nothing about them. The exit code is non-zero
    when any stated launch was refused, so a caller can tell without parsing prose.

    Everything opened is released before this returns, and what is printed is bounded
    run information -- the scope, each session's identity, its worker pid and pgid,
    the occupancy reading taken while it was live, the peak of those readings, each
    refusal, and each release with its process-group proof. No prompt, no provider
    content, no decision body, and no evidence.
    """
    stated_argv = list(sys.argv[1:] if argv is None else argv)
    try:
        launches, remaining = stated_launch_groups(stated_argv)
        _claim, source = stated_run_inputs(remaining)
    except (DispatchError, LaunchError) as exc:
        print("role-driver: {0}".format(exc), file=sys.stderr)
        return 1

    controller = ManagerController(source)

    print(
        "scope: {0}/{1} in {2}".format(source.project, source.ticket, source.control_plane)
    )
    print("binding root: {0}".format(source.binding_root))
    print("stated launches: {0}".format(len(launches)))
    for index, inputs in enumerate(launches, start=1):
        print("  {0}. rail {1} as {2}".format(index, inputs.rail, inputs.role))
    print("owned session handles: {0}".format(len(controller.owned_session_ids())))

    try:
        snapshot, observation = _read_scope(source, launches[0])
    except (ControlPlaneError, TriggerError) as exc:
        print("role-driver: {0}".format(exc), file=sys.stderr)
        return 2

    print("control-plane head: {0}".format(snapshot.head))

    witnessed: dict = {}

    def observe(held) -> None:
        # The one instant at which every session this run opened is live at the same
        # time: each process started, each handle is in this controller's own
        # registry, each binding is nonterminal. The reading is drawn from the same
        # controller that admitted every one of them.
        witnessed["reading"] = controller.agent_count()
        witnessed["sessions"] = tuple(session.session_id for session in held)

    try:
        outcome = drive_roles(
            snapshot,
            [
                RoleLaunch(
                    rail=inputs.rail, role=inputs.role, request_kwargs=inputs.request_kwargs
                )
                for inputs in launches
            ],
            observation,
            controller=controller,
            reference=launches[0].reference,
            package_root=launches[0].package_root,
            while_held=observe,
        )
    except InvocationRefused as exc:
        # A pre-flight gate said no to the whole run. Nothing was reserved, spawned
        # or sent for any stated launch.
        print("no launch this run: {0}".format(exc), file=sys.stderr)
        print("live occupancy: {0}".format(_describe(controller.agent_count())))
        return 3
    except DriverError as exc:
        print("role-driver: {0}".format(exc), file=sys.stderr)
        return 4

    print("occupancy on entry: {0}".format(_describe(outcome.entry_occupancy)))
    for index, session in enumerate(outcome.held, start=1):
        print(
            "held {0}: session {1} rail {2} role {3} pid/pgid {4}/{5} iteration {6} "
            "occupancy {7}".format(
                index,
                session.session_id,
                session.rail,
                session.role,
                session.pid,
                session.pgid,
                session.iteration_blob,
                _describe(session.occupancy),
            )
        )
    print(
        "all held at once: {0} session(s) -> {1}".format(
            len(witnessed.get("sessions", ())),
            _describe(witnessed["reading"]) if "reading" in witnessed else "none held",
        )
    )
    print("peak live occupancy: {0}".format(_describe(outcome.peak_occupancy)))
    for refusal in outcome.refusals:
        print(
            "refused: rail {0} as {1}: {2}".format(
                refusal.rail, refusal.role, refusal.detail
            )
        )
    for released in outcome.released:
        print(
            "released: session {0} pgid {1} binding {2} process group gone {3} "
            "graceful {4}".format(
                released.session_id,
                released.pgid,
                released.binding_state,
                released.process_group_gone,
                released.graceful,
            )
        )
    print("live occupancy after release: {0}".format(_describe(outcome.exit_occupancy)))
    return 3 if outcome.refusals else 0


if __name__ == "__main__":
    raise SystemExit(main())

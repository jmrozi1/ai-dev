"""Resolves one manager run's real runtime inputs and drives the accepted composition."""

from __future__ import annotations

# `decision_manager` composes a run but constructs nothing: its own docstring is
# explicit that `now` and `store` are the caller's, deliberately, because a clock or
# a repository read inside it would be a second instant and a second authority that
# no caller could pin. That module is therefore unusable until something owns those
# reads. This module is that something, and it owns exactly them.
#
# It is the caller half, and nothing more. It resolves four values once per run --
# the repository root, the accepted allowance store beneath it, one current epoch,
# and one explicitly stated exclusivity claim -- builds exactly one `ManagerRun` from
# them, and hands that to the accepted checkpoint-35 composition path. It computes no
# percentage, decides no availability, orders no rows, and draws nothing.
#
# Seven boundaries hold it honest.
#
# First, each input is resolved exactly once per run, in `resolve_run`, and then
# reused. There is one `resolve_repo_root` call, one `AllowanceStore` construction,
# and one clock read on the way to one frozen `ManagerRun`. A second store or a later
# instant between construction and use is the incoherence `ManagerRun` exists to make
# unrepresentable, and re-resolving either here would hand that incoherence straight
# back.
#
# Second, the exclusivity claim has no default anywhere on the path. Accepted
# decision D4 makes silence mean "unavailable", never "covered", so the claim is a
# required keyword argument with no default at all: omitting it is a `TypeError`
# rather than a quiet `None`, and silence is not something this module can represent.
# `None` is a caller saying out loud that the human affirmed nothing, and it is
# carried through to `ManagerRun` exactly as given -- never substituted, widened, or
# filled in. The command line enforces the same rule where a human states it, by
# requiring exactly one of the two flags and refusing when neither is given.
#
# Third, nothing about the claim is durable. This module opens no file for writing,
# reads no environment variable, keeps no module-level mutable state, and holds no
# cache. The claim exists for the length of one call and there is no restart across
# which it could survive, so a new run needs a new statement. That is the whole
# mechanism by which outside Claude use revokes coverage under D4: there is nothing
# here that could preserve a stale claim.
#
# Fourth, the queue is acquired, never invented. `queue_source.load_queue` is now the
# one boundary that turns durable authority into queue inputs, and this module calls
# it exactly once per run and adds nothing to it. It repeats none of that module's
# freshness, cross-checking, or reconciliation work, and it never turns a
# `QueueSourceError` into an empty page: `QueueView` carries no source-health concept,
# so a refusal drawn as zero rows would be indistinguishable from a genuinely quiet
# queue -- the exact defect this ticket rejected when it refused to show unavailable
# allowance as zero. A successful empty queue is served, because `load_queue` having
# returned is what makes the emptiness a fact rather than a silence.
#
# Fifth, the scope that queue is read from is stated, never guessed. The coordination
# repository, project, ticket, and binding-store root are four required inputs with no
# default, no environment fallback, and no config lookup, for the same reason the
# exclusivity claim has none: a manager that quietly picked a repository would answer
# confidently about a scope nobody named.
#
# Sixth, one run stays one instant. The queue's UTC timestamp is derived from the epoch
# `resolve_run` already read, so there is still exactly one clock read on the path and
# the age beside a row is the age at the same instant as the allowance beside it.
#
# Seventh, `SessionRegistry` stays controller-local and deliberately non-durable. A fresh
# process owns no handles, so durable bindings it did not start project through the
# accepted lifecycle as Disconnected. Adopting one by matching a recorded pid would be
# this module inventing ownership it cannot have.
#
# That is also why this entry point draws no manager-wide agent count and passes no
# `agents` reading to the page. A count is only as good as the ownership behind it,
# and a process that started none of the running agents has none, so the honest
# surface for one is the controller that owns the handles -- `manager_controller`.
# This remains the diagnostic, read-only view of a stated scope, and it says so
# rather than presenting an occupancy figure it cannot stand behind.
#
# The loopback rule is deliberately not restated here. `make_manager_server` and the
# accepted server beneath it own it, this module passes no host at all, and so the
# one place that decides what this surface binds stays the only place.

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import http.server
import re
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Tuple

from .claude_allowance_store import AllowanceStore, allowance_store_path
from .claude_allowance_view import AllowanceViewError
from .decision_manager import (
    ManagerRun,
    ManagerRunError,
    make_manager_server,
    render_manager_page,
)
from .decision_manager_web import serve_forever
from .decision_queue import QUEUE_STATES, DecisionQueue, QueueView, SelectedDetail
from .queue_source import (
    QueueScope,
    QueueSourceError,
    load_queue,
    project_queue,
    resolve_queue_scope,
)
from .progress_store import ProgressStore, progress_store_path
from .repository import resolve_repo_root
from .session_binding import BindingStore
from .session_lifecycle import SessionRegistry

__all__ = [
    "BINDING_ROOT_FLAG",
    "CLAIM_NONE_FLAG",
    "CLAIM_SINCE_FLAG",
    "CONTROL_PLANE_FLAG",
    "LaunchError",
    "PROJECT_FLAG",
    "QueueSourceContext",
    "REASON_CLAIM_AMBIGUOUS",
    "REASON_CLAIM_UNSTATED",
    "REASON_INVALID_CLAIM",
    "REASON_SOURCE_UNSTATED",
    "TICKET_FLAG",
    "launch_manager_server",
    "load_run_queue",
    "main",
    "project_run_queue",
    "resolve_run_scope",
    "render_launch_page",
    "resolve_run",
    "stated_run_inputs",
]

# This module's own refusals, and only its own. A repository that cannot be
# resolved already raises `RepositoryError` with its own message, and a queue that
# cannot be sourced already raises `QueueSourceError` with one of its own stable
# reasons; re-raising either under a second name here would give one fact two
# spellings that could drift.
REASON_CLAIM_UNSTATED = "exclusivity-claim-unstated"
REASON_CLAIM_AMBIGUOUS = "exclusivity-claim-ambiguous"
REASON_INVALID_CLAIM = "invalid-exclusivity-claim"
REASON_SOURCE_UNSTATED = "queue-source-unstated"

# The two ways a human may state the claim, and there is no third. One says the
# exact instant exclusivity began; the other says out loud that none is claimed.
CLAIM_SINCE_FLAG = "--human-exclusive-since"
CLAIM_NONE_FLAG = "--no-human-exclusivity"

# The four things that say which durable scope this manager is about. Every one is
# required: there is no environment variable, no config file, and no discovery step
# behind them, so a run either names its scope or does not start.
CONTROL_PLANE_FLAG = "--control-plane"
PROJECT_FLAG = "--project"
TICKET_FLAG = "--ticket"
BINDING_ROOT_FLAG = "--binding-root"

# The shape `session_lifecycle` parses. Written out because that module exposes a
# parser and no formatter, and something has to produce the text it accepts. The
# suite pins the two together by round-tripping this through the accepted parser, so
# a drift here fails rather than silently producing an unreadable instant.
_QUEUE_TIMESTAMP = "%Y-%m-%dT%H:%M:%SZ"

# Exactly an integer literal. This is a text-to-integer parse, not a second opinion
# on what a valid epoch is: whether the resulting integer is an acceptable instant
# stays the accepted projection's decision, applied in one place, so a positive
# rule here could not drift from the one the estimator enforces.
_INTEGER_TEXT = re.compile(r"\A-?[0-9]+\Z")


class LaunchError(Exception):
    """A refusal to launch a run, carrying the exact reason."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__("{0}: {1}".format(reason, detail))
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class QueueSourceContext:
    """The durable scope one run reads its queue from, stated in full.

    Frozen and without defaults, for the reason the exclusivity claim has none: a
    field that could be filled in is a field a run could fail to state and still
    start, and a manager that started against a scope nobody named would answer
    confidently about the wrong work. Omitting any of the four is a `TypeError`
    here and a stated refusal at the command line.

    These are addresses, not authority. What may be read from them, how fresh it
    must be, and when a read must refuse are all `queue_source`'s decisions.
    """

    control_plane: Path
    project: str
    ticket: str
    binding_root: Path


# --------------------------------------------------------------------------
# The runtime input boundary
# --------------------------------------------------------------------------


def resolve_run(
    *,
    human_exclusive_since: Optional[int],
    cwd: Optional[Path] = None,
) -> ManagerRun:
    """One run's five inputs, each resolved exactly once, as one frozen value.

    The repository root comes from the accepted repository helper rather than a
    path walk of this module's own, and both store paths from their own accepted
    path helpers against that root, so this module invents no second convention for
    either. The instant is read here, once, because `decision_manager` deliberately
    reads no clock and something has to.

    The progress store is resolved from the same root as the allowance store, so
    one run reports the recorded progress of the worktree it is actually serving.

    `human_exclusive_since` is keyword-only and has no default, exactly as
    `ManagerRun` and `project_window` give theirs none. A caller that omits it gets
    a `TypeError` rather than a `None` it did not choose, which is what makes
    silence unrepresentable rather than merely discouraged. `None` means the human
    affirmed nothing for this run and is carried through untouched.

    Read-only and durable-free: resolving a root, naming the store paths, and
    constructing an `AllowanceStore` and a `ProgressStore` open no file, create no
    directory, take no lock, and write nothing.
    """
    repo_root = resolve_repo_root(cwd)
    store = AllowanceStore(allowance_store_path(repo_root))
    progress = ProgressStore(progress_store_path(repo_root))
    now = int(time.time())
    return ManagerRun(
        store=store,
        now=now,
        human_exclusive_since=human_exclusive_since,
        progress=progress,
    )


# --------------------------------------------------------------------------
# The durable queue boundary
# --------------------------------------------------------------------------


def _queue_instant(now: int) -> str:
    """This run's own epoch, in the text the accepted lifecycle reads.

    A conversion, not a reading. The instant was already taken once in
    `resolve_run`, and the queue is deliberately given that same one so a row's age
    and the allowance beside it describe the same moment. Reading a clock here
    instead would be the second instant `ManagerRun` exists to rule out.
    """
    return datetime.fromtimestamp(now, timezone.utc).strftime(_QUEUE_TIMESTAMP)


def _projected(queue: DecisionQueue) -> Tuple[QueueView, Dict[str, SelectedDetail]]:
    """The complete queue and every visible row's detail, both from the queue itself.

    All three states, because the accepted payload carries every current row so its
    toggles work without a round trip, while `decision_manager_web` keeps Waiting as
    the default filter. Choosing what is visible is still not this module's call.

    Each detail is obtained by asking the queue to select that row, which is the
    accepted projection deciding what a detail contains. Building one from an item's
    fields here would be a second copy of a rule that already exists, and the two
    copies would be free to disagree about what an operational row may say.
    """
    view = queue.view(filters=QUEUE_STATES)
    details: Dict[str, SelectedDetail] = {}
    for row in view.rows:
        details[row.item_id] = queue.view(
            filters=QUEUE_STATES, selected_id=row.item_id
        ).detail
    return view, details


def load_run_queue(
    run: ManagerRun,
    source: QueueSourceContext,
    *,
    registry: SessionRegistry,
    store: Optional[BindingStore] = None,
    expected_head: Optional[str] = None,
    alive: Optional[Callable] = None,
) -> Tuple[QueueView, Dict[str, SelectedDetail]]:
    """One acquisition for one run: the accepted source, then the accepted projection.

    Exactly one `load_queue` call, against the stated scope and this run's instant.
    Everything that makes the result trustworthy -- remote freshness, the decision
    and rail cross-check, binding and lifecycle reconciliation, and the refusal
    reasons -- belongs to that call and is not repeated, second-guessed, or softened
    here. A `QueueSourceError` propagates with its reason intact.

    `registry` is the caller's because ownership cannot be discovered. The registry
    a controller holds is the only thing that knows which processes it actually
    started, so a caller that owns none passes an empty one and the accepted
    lifecycle reports the durable bindings it did not start as disconnected.
    `alive` is passed straight through for the same reason and defaults to the
    accepted prober; neither is a liveness rule of this module's own. `store` is the
    caller's for the third time over: a controller that admits against its own store
    must draw its rows from that same object rather than from a second one built
    here that merely happens to read the same files.

    An empty return is a fact rather than a silence: `load_queue` returned, so the
    scope was read and had nothing in it.
    """
    queue = load_queue(
        Path(source.control_plane),
        project=source.project,
        ticket=source.ticket,
        registry=registry,
        now=_queue_instant(run.now),
        store=_run_store(source, store),
        expected_head=expected_head,
        alive=alive,
    )
    return _projected(queue)


def _run_store(source: QueueSourceContext, store: Optional[BindingStore]) -> BindingStore:
    """The caller's store when it has one, and otherwise this run's own.

    A caller that already owns a store must be able to hand it in. Constructing a
    second one over the same root reads the same durable files today, so the two
    agree by accident rather than by construction -- and "by accident" is exactly
    what the controller-owned composition exists to remove. A controller that
    reserves against one store object and draws its rows from another is one edit
    away from the split-evidence defect checkpoint 45 closed one layer down.

    Constructing one when none is given keeps every existing caller unchanged.
    """
    return store if store is not None else BindingStore(source.binding_root)


def resolve_run_scope(
    source: QueueSourceContext,
    *,
    expected_head: Optional[str] = None,
) -> QueueScope:
    """This run's durable control-plane authority, proven once against the remote.

    The half of `load_run_queue` that reaches the coordination remote, so a caller
    that renders more than once from one run does this exactly once. The stated
    scope is the caller's, exactly as it is for `load_run_queue`; nothing about
    freshness, rail authorization, or decision validity is decided here.
    """
    return resolve_queue_scope(
        Path(source.control_plane),
        project=source.project,
        ticket=source.ticket,
        expected_head=expected_head,
    )


def project_run_queue(
    scope: QueueScope,
    run: ManagerRun,
    source: QueueSourceContext,
    *,
    registry: SessionRegistry,
    store: Optional[BindingStore] = None,
    alive: Optional[Callable] = None,
) -> Tuple[QueueView, Dict[str, SelectedDetail]]:
    """One projection of an already-proven scope, at this run's instant.

    The half of `load_run_queue` that may honestly be repeated: it re-reads the
    caller's durable store and re-observes the caller's liveness, and it fetches
    nothing. The refusal reasons are `queue_source`'s and are neither repeated nor
    softened here.

    `run.now` is still the instant, so a row's age and the allowance beside it keep
    describing one moment. Age is display, not a liveness reading -- what moves
    between two projections of one run is which sessions this controller can prove,
    which is the only thing that can have moved.
    """
    queue = project_queue(
        scope,
        registry=registry,
        now=_queue_instant(run.now),
        store=_run_store(source, store),
        alive=alive,
    )
    return _projected(queue)


# --------------------------------------------------------------------------
# Driving the accepted composition
# --------------------------------------------------------------------------


def render_launch_page(
    view: QueueView,
    details: Mapping[str, SelectedDetail],
    *,
    human_exclusive_since: Optional[int],
    cwd: Optional[Path] = None,
    template_path: Optional[Path] = None,
) -> str:
    """This run's page: inputs resolved once here, queue supplied by the caller.

    One call is one run, with one instant and one store. The queue and its details
    are the caller's because nothing in this repository can yet build them from
    durable state, and manufacturing an empty one here would present an unwired
    source as an answered one.
    """
    run = resolve_run(human_exclusive_since=human_exclusive_since, cwd=cwd)
    return render_manager_page(run, view, details, template_path=template_path)


def launch_manager_server(
    view: QueueView,
    details: Mapping[str, SelectedDetail],
    *,
    human_exclusive_since: Optional[int],
    cwd: Optional[Path] = None,
    port: int = 0,
    template_path: Optional[Path] = None,
) -> http.server.HTTPServer:
    """A loopback server for one run, rendered once at construction.

    No host is passed. `make_manager_server` already defaults to the accepted
    loopback constant and the accepted server enforces the rule, so this module
    names neither and cannot become a second place that decides what gets bound.

    Nothing is started here and nothing refreshes: the caller owns whether to serve,
    and there is no timer, poller, or watcher by which a later instant could appear
    on a page this run already rendered. A later instant is a later run.
    """
    run = resolve_run(human_exclusive_since=human_exclusive_since, cwd=cwd)
    return make_manager_server(
        run,
        view,
        details,
        port=port,
        template_path=template_path,
    )


# --------------------------------------------------------------------------
# The human entry point
# --------------------------------------------------------------------------


def _exact_claim_instant(text: str) -> int:
    """Exactly an integer literal, refused as a caller fault otherwise.

    Argument text is text, so something must turn it into an integer before the
    accepted rule can judge it. That is all this does. It deliberately does not
    decide whether the integer is a usable instant, because the estimator already
    decides that and two rules would be two rules to keep in step.
    """
    if _INTEGER_TEXT.match(text) is None:
        raise LaunchError(
            REASON_INVALID_CLAIM,
            "{0} takes an exact integer epoch, got {1!r}".format(CLAIM_SINCE_FLAG, text),
        )
    return int(text)


def _stated_claim(arguments: argparse.Namespace) -> Optional[int]:
    """The claim exactly as the human stated it, or a refusal to launch without one.

    Absence is not a claim. Under accepted decision D4 an unstated claim means
    coverage is unavailable, so it must be said in one of the two directions rather
    than fallen into: neither flag is a refusal, and both flags is a refusal too,
    because a run whose claim is ambiguous is not a run whose claim was stated.
    """
    stated_since = arguments.since is not None
    if stated_since and arguments.none_claimed:
        raise LaunchError(
            REASON_CLAIM_AMBIGUOUS,
            "state exactly one of {0} and {1}, not both".format(
                CLAIM_SINCE_FLAG, CLAIM_NONE_FLAG
            ),
        )
    if not stated_since and not arguments.none_claimed:
        raise LaunchError(
            REASON_CLAIM_UNSTATED,
            "state {0} <epoch> if the human held Claude exclusively for this run, or "
            "{1} if no exclusivity is claimed; silence is not a claim".format(
                CLAIM_SINCE_FLAG, CLAIM_NONE_FLAG
            ),
        )
    if arguments.none_claimed:
        return None
    return _exact_claim_instant(arguments.since)


def _stated_source(arguments: argparse.Namespace) -> QueueSourceContext:
    """The scope exactly as the human named it, or a refusal naming what is missing.

    Every one of the four is required and none has a fallback. Argparse could mark
    them required and exit on its own, but a stated reason is the point: a run that
    did not name its scope should say so in the same vocabulary as every other
    refusal here, rather than in the parser's.
    """
    missing = [
        flag
        for flag, value in (
            (CONTROL_PLANE_FLAG, arguments.control_plane),
            (PROJECT_FLAG, arguments.project),
            (TICKET_FLAG, arguments.ticket),
            (BINDING_ROOT_FLAG, arguments.binding_root),
        )
        if value is None
    ]
    if missing:
        raise LaunchError(
            REASON_SOURCE_UNSTATED,
            "state {0}; this manager reads no default scope and no configuration "
            "file, so a queue it cannot name is a queue it will not read".format(
                ", ".join(missing)
            ),
        )
    return QueueSourceContext(
        control_plane=Path(arguments.control_plane),
        project=arguments.project,
        ticket=arguments.ticket,
        binding_root=Path(arguments.binding_root),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="decision-manager-launch",
        description="Serve one manager run against one stated durable scope.",
    )
    parser.add_argument(CLAIM_SINCE_FLAG, dest="since", default=None)
    parser.add_argument(CLAIM_NONE_FLAG, dest="none_claimed", action="store_true")
    parser.add_argument(CONTROL_PLANE_FLAG, dest="control_plane", default=None)
    parser.add_argument(PROJECT_FLAG, dest="project", default=None)
    parser.add_argument(TICKET_FLAG, dest="ticket", default=None)
    parser.add_argument(BINDING_ROOT_FLAG, dest="binding_root", default=None)
    return parser


def stated_run_inputs(argv: List[str]) -> Tuple[Optional[int], QueueSourceContext]:
    """One run's stated claim and stated scope, parsed exactly once.

    Public because the controller-owned surface must start from the same two
    statements this entry point does. A second parser there would be a second place
    that decides what silence means and which flags are required, and the two would
    be free to drift; this is the one place, and it is called rather than copied.
    """
    arguments = _build_parser().parse_args(list(argv))
    return _stated_claim(arguments), _stated_source(arguments)


def main(argv: Optional[List[str]] = None) -> int:
    """One stated claim, one stated scope, one run, one queue, one served page.

    The order is the whole design. The claim and the scope must both be stated
    before anything is read; the run fixes the store and the instant once; the queue
    is acquired once against that same instant; and only a queue that was actually
    returned reaches a server. A source refusal exits before any server exists, with
    the reason `queue_source` raised, because a refusal rendered as an empty page is
    the one outcome this entry point must never produce.

    A genuinely empty queue is served. That is the difference this rail bought: the
    scope was read, and it had nothing in it.

    What is printed is bounded launch information -- paths, the instant, counts, and
    the address actually bound. No decision body, evidence, binding, or session
    identity is printed, because a terminal is not the surface those belong on.

    This is a diagnostic view and says so. It owns no session handles, so it serves
    no live occupancy: `manager_controller.main` is the controller-owned surface
    that does.
    """
    try:
        claim, source = stated_run_inputs(list(sys.argv[1:] if argv is None else argv))
        run = resolve_run(human_exclusive_since=claim)
    except LaunchError as exc:
        print("decision-manager-launch: {0}".format(exc), file=sys.stderr)
        return 1

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
    print(
        "live occupancy: not served; this process owns no session handles, so "
        "`manager_controller` is the surface that draws one"
    )

    try:
        view, details = load_run_queue(run, source, registry=SessionRegistry())
    except QueueSourceError as exc:
        print("decision-manager-launch: {0}".format(exc), file=sys.stderr)
        return 2

    print("queue rows: {0}".format(len(view.rows)))

    try:
        server = make_manager_server(run, view, details)
    except (AllowanceViewError, ManagerRunError) as exc:
        # The accepted projection's refusal, reported rather than raised through a
        # traceback. Its reason is preserved exactly; restating the rule that
        # produced it here would be a second copy of the estimator's judgement.
        print("decision-manager-launch: {0}".format(exc), file=sys.stderr)
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

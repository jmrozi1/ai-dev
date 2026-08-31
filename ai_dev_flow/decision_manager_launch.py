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
# Four boundaries hold it honest.
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
# Fourth, the queue is not this module's to invent. `render_manager_page` and
# `make_manager_server` both need a `QueueView`, and no production builder of
# `PendingDecision` or `OperationalAgent` exists anywhere in this repository yet.
# `QueueView` carries no source-health concept, so an empty queue and an unwired one
# are indistinguishable on the page -- exactly the defect class this ticket already
# rejected when it refused to show unavailable allowance as zero. So the queue comes
# from this module's caller, and `main` refuses to serve rather than presenting an
# empty queue as though the manager were watching durable state.
#
# The loopback rule is deliberately not restated here. `make_manager_server` and the
# accepted server beneath it own it, this module passes no host at all, and so the
# one place that decides what this surface binds stays the only place.

import argparse
import http.server
import re
import sys
import time
from pathlib import Path
from typing import List, Mapping, Optional

from .claude_allowance_store import AllowanceStore, allowance_store_path
from .decision_manager import ManagerRun, make_manager_server, render_manager_page
from .decision_queue import QueueView, SelectedDetail
from .repository import resolve_repo_root

__all__ = [
    "CLAIM_NONE_FLAG",
    "CLAIM_SINCE_FLAG",
    "LaunchError",
    "REASON_CLAIM_AMBIGUOUS",
    "REASON_CLAIM_UNSTATED",
    "REASON_INVALID_CLAIM",
    "REASON_NO_QUEUE_SOURCE",
    "launch_manager_server",
    "main",
    "render_launch_page",
    "resolve_run",
]

# This module's own refusals, and only its own. A repository that cannot be
# resolved already raises `RepositoryError` with its own message, and re-raising it
# under a second name here would give one fact two spellings that could drift.
REASON_CLAIM_UNSTATED = "exclusivity-claim-unstated"
REASON_CLAIM_AMBIGUOUS = "exclusivity-claim-ambiguous"
REASON_INVALID_CLAIM = "invalid-exclusivity-claim"
REASON_NO_QUEUE_SOURCE = "no-queue-source"

# The two ways a human may state the claim, and there is no third. One says the
# exact instant exclusivity began; the other says out loud that none is claimed.
CLAIM_SINCE_FLAG = "--human-exclusive-since"
CLAIM_NONE_FLAG = "--no-human-exclusivity"

# Exactly an integer literal. This is a text-to-integer parse, not a second opinion
# on what a valid epoch is: whether the resulting integer is an acceptable instant
# stays the accepted projection's decision, applied in one place, so a positive
# rule here could not drift from the one the estimator enforces.
_INTEGER_TEXT = re.compile(r"\A-?[0-9]+\Z")

_NO_QUEUE_SOURCE_DETAIL = (
    "no production builder of PendingDecision or OperationalAgent exists yet, so "
    "this entry point cannot obtain a truthful QueueView; an empty queue would be "
    "indistinguishable from an unwired one. Pass a view and its details to "
    "launch_manager_server or render_launch_page instead."
)


class LaunchError(Exception):
    """A refusal to launch a run, carrying the exact reason."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__("{0}: {1}".format(reason, detail))
        self.reason = reason
        self.detail = detail


# --------------------------------------------------------------------------
# The runtime input boundary
# --------------------------------------------------------------------------


def resolve_run(
    *,
    human_exclusive_since: Optional[int],
    cwd: Optional[Path] = None,
) -> ManagerRun:
    """One run's four inputs, each resolved exactly once, as one frozen value.

    The repository root comes from the accepted repository helper rather than a
    path walk of this module's own, and the store path from the accepted store-path
    helper against that root, so this module invents no second convention for
    either. The instant is read here, once, because `decision_manager` deliberately
    reads no clock and something has to.

    `human_exclusive_since` is keyword-only and has no default, exactly as
    `ManagerRun` and `project_window` give theirs none. A caller that omits it gets
    a `TypeError` rather than a `None` it did not choose, which is what makes
    silence unrepresentable rather than merely discouraged. `None` means the human
    affirmed nothing for this run and is carried through untouched.

    Read-only and durable-free: resolving a root, naming a store path, and
    constructing an `AllowanceStore` open no file, create no directory, take no
    lock, and write nothing.
    """
    repo_root = resolve_repo_root(cwd)
    store = AllowanceStore(allowance_store_path(repo_root))
    now = int(time.time())
    return ManagerRun(
        store=store,
        now=now,
        human_exclusive_since=human_exclusive_since,
    )


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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="decision-manager-launch",
        description="Resolve one manager run's runtime inputs.",
    )
    parser.add_argument(CLAIM_SINCE_FLAG, dest="since", default=None)
    parser.add_argument(CLAIM_NONE_FLAG, dest="none_claimed", action="store_true")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Resolve one run from a stated claim, then refuse to serve without a queue.

    Everything this rail owns happens and is reported: the claim is required in one
    of its two directions, and the store, instant, and claim are resolved exactly
    once into one `ManagerRun`. What does not happen is a page, because there is no
    truthful queue to draw beside the allowance and inventing one here would be the
    defect this ticket already rejected. The refusal names that gap exactly rather
    than showing an unwired source as an empty one.
    """
    arguments = _build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))

    try:
        claim = _stated_claim(arguments)
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
        "decision-manager-launch: {0}: {1}".format(
            REASON_NO_QUEUE_SOURCE, _NO_QUEUE_SOURCE_DETAIL
        ),
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

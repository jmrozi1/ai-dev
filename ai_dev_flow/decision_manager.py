"""Composes one manager run: the accepted allowance projection into the accepted page."""

from __future__ import annotations

# This is composition, and only composition. Every fact it passes on was decided
# somewhere else. `claude_allowance_view` decides what each window may honestly be
# shown as, `decision_queue` decides the rows and their details, and
# `decision_manager_web` decides how all of that is drawn. Nothing here computes a
# percentage, a bound, a health, a reset, a label, a row, or an order, and nothing
# here re-derives one that was already computed.
#
# Five boundaries hold it honest.
#
# First, one run has one set of inputs. `ManagerRun` is frozen and holds exactly
# one `AllowanceStore` and one epoch, resolved once by the caller before the run
# and reused for both windows. There is no per-window store argument and no clock
# of this module's own, so there is no code path on which the seven-day window is
# projected against a different store object or a later instant than the five-hour
# window.
#
# Second, the human exclusivity claim lives in memory for the length of one run
# and nowhere else. It is a required field with no default, exactly as
# `project_window` requires its own, because the caller is the only thing that
# knows whether Claude was used outside this manager. It is never persisted,
# defaulted, reconstructed, cached, read from an environment variable or a side
# file, or inferred: a caller that cannot assert it says `None` out loud and gets
# `unavailable`. Silence is never coverage, and nothing here writes anything
# durable at all -- there is no schema change and no new persistence of any kind.
#
# Third, per-window independence survives the composition intact. Two windows mean
# two projections, and whatever each one returns -- calibrated, provisional,
# unavailable with a reason, or an unhealthy source -- is handed on exactly as it
# was returned. Nothing is deduplicated, defaulted, synthesized, softened, or
# reordered into the other window's place. One window may be unavailable while the
# other is not, and that is the truth being shown rather than a case to repair.
#
# Fourth, the coherence unit is one window, by accepted contract rather than by
# anything invented here. `AllowanceStore.projection_inputs` derives profile,
# anchor, workload and ledger cleanliness together from one generation, and
# accepted decision D4 states that the same run-scoped exclusivity epoch "is
# compared independently inside the one-generation read against each window's own
# predecessor at reading time and anchor at projection time", so that "differing
# windows may therefore become available again at different readings". Two windows
# are therefore two one-generation reads on purpose, and what the contract fixes
# across them is the run-scoped epoch and the run-scoped assertion -- both of which
# are held exactly once here. No snapshot API, cross-window lock, cached read, or
# new store method is introduced to manufacture a coherence the contract does not
# ask for.
#
# Fifth, the queue stays dominant and arrives already accepted. The `QueueView`
# and its `SelectedDetail` values come from the caller; this module builds no
# queue from durable state, opens no repository, reads no control-plane prose, and
# has no authority over what the human answers. Allowance is a small figure beside
# the queue, and composing it must not turn it into the subject.

from dataclasses import dataclass
import http.server
from pathlib import Path
from typing import Mapping, Optional, Tuple

from .claude_allowance import WINDOW_FIVE_HOUR, WINDOW_SEVEN_DAY
from .claude_allowance_store import AllowanceStore
from .claude_allowance_view import AllowanceWindowView, project_window
from .decision_manager_web import LOOPBACK_HOST, make_server, render_page
from .decision_queue import QueueView, SelectedDetail

__all__ = [
    "MANAGER_WINDOWS",
    "ManagerRun",
    "ManagerRunError",
    "REASON_INVALID_RUN",
    "make_manager_server",
    "project_allowance",
    "render_manager_page",
]

# This module's one refusal. Everything else keeps the reason it was raised with
# by the accepted view, store, or render path; wrapping an accepted refusal would
# give one fact two names that could then drift apart.
REASON_INVALID_RUN = "invalid-manager-run"

# The windows one manager run projects, and the order it projects them in. Fixed
# here for the same reason the accepted reading order is fixed: the two windows
# are two separate reads, so a result recorded between them always lands on the
# same side of the same boundary rather than on whichever side the call order
# happened to take that day. This is not the drawing order -- the accepted page
# keys allowance by window identifier and owns its own -- so changing this changes
# which read happens first and nothing else.
MANAGER_WINDOWS = (WINDOW_FIVE_HOUR, WINDOW_SEVEN_DAY)


class ManagerRunError(Exception):
    """A refusal to compose a run, carrying the exact reason."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__("{0}: {1}".format(reason, detail))
        self.reason = reason
        self.detail = detail


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ManagerRun:
    """The one coherent set of allowance inputs one manager run projects from.

    Frozen, and constructed once before the run rather than assembled per window.
    That is the whole point of the type: the store object and the instant are
    resolved by the caller exactly once, and both windows are then projected
    against those same two values. A caller that passed a store and a clock down
    to each window separately could re-resolve either between them, which is the
    incoherence this value exists to make unrepresentable.

    `now` is the caller's, deliberately. This module reads no clock, because a
    clock read here would be a second instant no caller could pin, and the accepted
    projection is explicit that a caller's clock is the caller's to get right -- it
    refuses a malformed one as a caller fault rather than accusing a healthy store.
    The same applies to `store`: which repository's evidence this run is about is
    not this module's decision.

    `human_exclusive_since` has no default, for the same reason `project_window`
    gives its own argument none. `None` is the caller stating that the human has
    affirmed nothing for this run, and it must be said rather than fallen into. It
    is held in memory for this run only: nothing here writes it, and there is no
    restart across which it could survive.

    Not validated here. `project_window` already checks the instant and the
    assertion, with reasons this module would otherwise have to spell a second
    time, and a second spelling of one refusal is a second thing to keep in step.
    """

    store: AllowanceStore
    now: int
    human_exclusive_since: Optional[int]


def _checked_run(run: object) -> ManagerRun:
    """Exactly a `ManagerRun`, refused rather than duck-typed.

    A mapping, a tuple, or a near-miss object with the right attribute names would
    compose without complaint and could carry a per-window store or a mutable
    instant, which is precisely what the frozen value rules out.
    """
    if type(run) is not ManagerRun:
        raise ManagerRunError(
            REASON_INVALID_RUN,
            "composition consumes one ManagerRun, got {0!r}".format(type(run).__name__),
        )
    return run


# --------------------------------------------------------------------------
# Projection
# --------------------------------------------------------------------------


def project_allowance(run: ManagerRun) -> Tuple[AllowanceWindowView, ...]:
    """Both accepted window views for one run: one projection each, no more.

    Exactly one `project_window` call per window in `MANAGER_WINDOWS`, each against
    this run's own store, its own instant, and its own exclusivity assertion. The
    views come back untouched -- this returns what the accepted projection decided
    and never repairs, fills, or reconciles one window against the other.

    A caller fault propagates as the `AllowanceViewError` the accepted projection
    raised, and a store refusal arrives as the unavailable view it decided, so a
    run that asked properly can always draw something truthful.
    """
    checked = _checked_run(run)
    return tuple(
        project_window(
            checked.store,
            window=window,
            now=checked.now,
            human_exclusive_since=checked.human_exclusive_since,
        )
        for window in MANAGER_WINDOWS
    )


# --------------------------------------------------------------------------
# Composition
# --------------------------------------------------------------------------


def render_manager_page(
    run: ManagerRun,
    view: QueueView,
    details: Mapping[str, SelectedDetail],
    *,
    template_path: Optional[Path] = None,
) -> str:
    """One manager run's complete page: this run's allowance beside this queue.

    One call is one run. The two views are projected here and passed unmodified
    into the accepted render path, which owns every remaining decision about the
    page -- rounding, labels, ordering, the payload, and the policy.
    """
    return render_page(
        view,
        details,
        allowance=project_allowance(run),
        template_path=template_path,
    )


def make_manager_server(
    run: ManagerRun,
    view: QueueView,
    details: Mapping[str, SelectedDetail],
    *,
    host: str = LOOPBACK_HOST,
    port: int = 0,
    template_path: Optional[Path] = None,
) -> http.server.HTTPServer:
    """A loopback server for one manager run's page, rendered once at construction.

    The run is projected once, here, and the accepted server holds the finished
    page. Nothing refreshes: there is no timer, no poller, and no endpoint that
    could ask for a newer allowance, so the figure a viewer sees is always the one
    this run projected at this run's instant. A later instant is a later run.

    `host` defaults to the accepted loopback constant rather than to a literal, so
    one place decides what this surface will bind. That rule is the accepted
    server's and is deliberately not restated here, which means a non-loopback host
    is refused after this run has already been projected rather than before it.
    Projection is read-only -- it writes nothing, takes no lock, and leaves no
    file -- so nothing durable happens on the way to that refusal, and a second
    spelling of the loopback rule would be the worse trade.
    """
    return make_server(
        view,
        details,
        allowance=project_allowance(run),
        host=host,
        port=port,
        template_path=template_path,
    )

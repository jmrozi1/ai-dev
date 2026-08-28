"""The boundary between one dispatched Claude invocation and the allowance ledger."""

from __future__ import annotations

# The controller never holds a `RuntimeResult`. `interpret_result` runs inside the
# worker process, and everything that crosses back is the reduced JSON mapping the
# worker emitted. `AllowanceStore.record_result` accepts nothing else, so something
# has to rebuild that value on this side of the pipe, and this is it.
#
# Four rules hold the boundary honest.
#
# First, one ledger identity names one dispatched invocation, not a session. The
# accepted lifecycle continues a single owned session as many times as it likes, so
# a session UUID would collapse several results into one key and lose every one
# after the first. The ordinal may stay in memory because continuation requires the
# in-memory owned handle: after a restart that session cannot be dispatched at all,
# so no ordinal can ever be minted twice for it.
#
# Second, absence is never zero. A result the provider reported without a cost, and
# an invocation that was dispatched but produced no usable result, are both holes.
# A hole is worse for the estimate than a large cost -- it makes every calibration
# span containing it incompletely covered -- which is the point: work that cannot
# be weighed must not be quietly weighed as nothing.
#
# Third, the burden of proof runs one way. Recording nothing is only correct when
# the refusal is *proven* to have happened before any command reached the provider.
# Everything else, including a failed write to the worker's stdin whose outcome is
# genuinely unknown, records a hole. The conservative direction suppresses a number
# rather than inflating one.
#
# Fourth, contention is bounded and explicit. The store deliberately owns no retry
# policy, so this module owns a small fixed one and then fails loudly. It never
# inspects, breaks, ages out, or repairs a lock, and it never mints a fresh key to
# get past a refusal -- that is how a replay becomes a double count.

from dataclasses import dataclass
from decimal import Decimal
import time
from typing import Any, Callable, Dict, Mapping, Optional

from .claude_allowance_store import (
    REASON_LOCK_LOST,
    REASON_LOCK_MALFORMED,
    REASON_STORE_LOCKED,
    AllowanceStore,
    AllowanceStoreError,
)
from .claude_runtime import (
    MODE_LAUNCH,
    MODE_RESUME,
    REASON_RESULT_SESSION_MISMATCH,
    ClaudeRuntimeError,
    RuntimeResult,
    interpret_result,
)
from .claude_worker import (
    MESSAGE_RESULT,
    PROTOCOL_VERSION,
    REASON_BINDING_NOT_RESERVED,
    REASON_ITERATION_MISMATCH,
    REASON_READINESS_FAILED,
    REASON_SDK_UNAVAILABLE,
    REASON_SELECTOR_PRESENT,
    REASON_SPAWN_FAILED,
    ClaudeWorkerError,
)
from .session_binding import SessionBindingError, validate_session_id
from .session_lifecycle import REASON_LAUNCH_FAILED, LifecycleError

__all__ = [
    "AllowanceLedger",
    "AllowanceLedgerError",
    "DEFAULT_ATTEMPTS",
    "DEFAULT_DELAY_SECONDS",
    "INVOCATION_KINDS",
    "InvocationIdentity",
    "KIND_CONTINUE",
    "KIND_LAUNCH",
    "WORKER_PRE_DISPATCH_REASONS",
    "dispatch_occurred",
    "missing_result",
    "result_from_message",
]

# The two lifecycle operations that can dispatch a command to the provider. These
# are the lifecycle's own vocabulary, not the runtime request's: `continue_session`
# builds a request whose mode is `resume`. Keeping both names and mapping between
# them is deliberate -- the identity says which operation ran, and the
# reconstruction says which request shape the worker answered.
KIND_LAUNCH = "launch"
KIND_CONTINUE = "continue"
INVOCATION_KINDS = (KIND_LAUNCH, KIND_CONTINUE)

_RUNTIME_MODE_BY_KIND = {KIND_LAUNCH: MODE_LAUNCH, KIND_CONTINUE: MODE_RESUME}

# Small and fixed. Three attempts is enough to cross a writer that is finishing,
# and few enough that a genuinely contended store fails while someone is still
# watching. There is no exponential policy, no jitter, and no upper bound to tune.
DEFAULT_ATTEMPTS = 3
DEFAULT_DELAY_SECONDS = 0.05

# Reasons this module retries by replaying the identical key. A held or malformed
# lock left the store byte-unchanged; a lost lock may have left a durable write
# behind, and the accepted duplicate rule makes the identical replay either a
# no-op or the missing record. Both are the same action.
_RETRYABLE_REASONS = frozenset(
    {REASON_STORE_LOCKED, REASON_LOCK_MALFORMED, REASON_LOCK_LOST}
)

# The worker refusals raised before it has written anything to the provider:
# environment inspection, the binding check, the spawn, the readiness handshake,
# and the SDK verdict all precede the first command. Every other worker refusal
# happens at or after `run_request`.
WORKER_PRE_DISPATCH_REASONS = frozenset(
    {
        REASON_SELECTOR_PRESENT,
        REASON_BINDING_NOT_RESERVED,
        REASON_ITERATION_MISMATCH,
        REASON_SPAWN_FAILED,
        REASON_READINESS_FAILED,
        REASON_SDK_UNAVAILABLE,
    }
)


class AllowanceLedgerError(Exception):
    """A fail-closed ledger refusal carrying one stable machine-readable reason."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__("{0}: {1}".format(reason, detail))
        self.reason = reason
        self.detail = detail


REASON_INVALID_KIND = "invalid-invocation-kind"
REASON_INVALID_ORDINAL = "invalid-invocation-ordinal"
REASON_INVALID_MESSAGE = "invalid-result-message"
REASON_MESSAGE_MODE_MISMATCH = "result-mode-mismatch"
REASON_LEDGER_CONTENDED = "ledger-contended"
REASON_LEDGER_UNRECONCILED = "ledger-unreconciled"


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class InvocationIdentity:
    """One dispatched invocation, named so a replay cannot become a second entry."""

    session_id: str
    kind: str
    ordinal: int

    def __post_init__(self) -> None:
        # The canonical UUID check is the accepted one, so a key can never be
        # spelled two ways for one session. It also guarantees the composed key
        # matches the store's key grammar without this module restating it.
        object.__setattr__(self, "session_id", validate_session_id(self.session_id))
        if self.kind not in INVOCATION_KINDS:
            raise AllowanceLedgerError(
                REASON_INVALID_KIND,
                "kind {0!r} is not one of {1}".format(self.kind, ", ".join(INVOCATION_KINDS)),
            )
        if type(self.ordinal) is not int or self.ordinal < 1:
            raise AllowanceLedgerError(
                REASON_INVALID_ORDINAL,
                "ordinal must be a positive int, got {0!r}".format(self.ordinal),
            )

    @property
    def runtime_mode(self) -> str:
        """The request mode the worker answered for this lifecycle operation."""
        return _RUNTIME_MODE_BY_KIND[self.kind]

    @property
    def key(self) -> str:
        return "{0}:{1}:{2}".format(self.session_id, self.kind, self.ordinal)


@dataclass(frozen=True)
class _ReconstructionRequest:
    """The two fields `interpret_result` reads, and no others.

    The worker already establishes that this is all the contract needs. Building a
    real `RuntimeRequest` here would mean re-validating controller-owned
    filesystem assets to weigh a cost, which is both redundant and the wrong side
    of the boundary.
    """

    session_id: str
    mode: str


# --------------------------------------------------------------------------
# Reconstruction
# --------------------------------------------------------------------------


def _required(message: Mapping, field: str) -> Any:
    if field not in message:
        raise AllowanceLedgerError(
            REASON_INVALID_MESSAGE, "the result message has no {0!r}".format(field)
        )
    return message[field]


def result_from_message(identity: InvocationIdentity, message: Any) -> RuntimeResult:
    """Rebuild the `RuntimeResult` the worker reduced this invocation to.

    This is a deserialisation, not a second interpretation: the message is a
    serialised `RuntimeResult` plus its envelope, and `interpret_result` is stable
    over it. Running it again is still worth doing, because it is the accepted
    place where a result for the wrong session is refused, and a mapping that
    arrived here by some other path has not been through `run_request`.

    Fields the envelope carries but a workload unit has no use for -- markers
    above all -- are read by nothing here and reach no durable state.
    """
    if not isinstance(message, Mapping):
        raise AllowanceLedgerError(
            REASON_INVALID_MESSAGE,
            "a result message is a mapping, got {0!r}".format(type(message).__name__),
        )
    if _required(message, "type") != MESSAGE_RESULT:
        raise AllowanceLedgerError(
            REASON_INVALID_MESSAGE,
            "expected a {0!r} message, got {1!r}".format(MESSAGE_RESULT, message.get("type")),
        )
    if _required(message, "protocol") != PROTOCOL_VERSION:
        raise AllowanceLedgerError(
            REASON_INVALID_MESSAGE,
            "expected protocol {0}, got {1!r}".format(PROTOCOL_VERSION, message.get("protocol")),
        )
    mode = _required(message, "mode")
    if mode != identity.runtime_mode:
        # A resume answer recorded under a launch identity, or the reverse, would
        # attribute one invocation's cost to an operation that did not happen.
        raise AllowanceLedgerError(
            REASON_MESSAGE_MODE_MISMATCH,
            "identity {0} expects mode {1!r}, message reports {2!r}".format(
                identity.key, identity.runtime_mode, mode
            ),
        )
    _required(message, "session_id")
    request = _ReconstructionRequest(
        session_id=identity.session_id, mode=identity.runtime_mode
    )
    return interpret_result(request, message)


def missing_result(identity: InvocationIdentity) -> RuntimeResult:
    """What a dispatched invocation that produced no usable result is worth.

    An error with no cost: the provider may well have consumed allowance, and
    nothing here can say how much. Recording it as a hole is the only statement
    that stays true whichever it was.
    """
    return RuntimeResult(
        session_id=identity.session_id,
        mode=identity.runtime_mode,
        subtype=None,
        is_error=True,
        num_turns=None,
        total_cost_usd=None,
    )


def dispatch_occurred(error: Any) -> bool:
    """Whether a command had already reached the provider when this refusal was raised.

    Classified by the raising contract rather than by a flat list of strings,
    because that is what actually decides the answer:

    - a binding refusal never dispatches: reserving, attaching and unbinding all
      happen around the provider, never through it;
    - a runtime refusal never dispatches either, except the one reason that can
      only exist once a result came back -- an SDK probe and a request built from
      controller-owned assets both precede the first command;
    - a worker refusal dispatches unless it is one of the start-up refusals raised
      before `run_request` was ever reached;
    - a lifecycle refusal dispatches only as `launch-failed`, which is raised
      exactly around the send.

    Anything unrecognised is dispatched. A wrong `False` silently understates
    consumption forever; a wrong `True` is a hole that suppresses an estimate
    until the next human reading. Only one of those is recoverable.
    """
    reason = getattr(error, "reason", None)
    if isinstance(error, SessionBindingError):
        return False
    if isinstance(error, ClaudeRuntimeError):
        return reason == REASON_RESULT_SESSION_MISMATCH
    if isinstance(error, ClaudeWorkerError):
        return reason not in WORKER_PRE_DISPATCH_REASONS
    if isinstance(error, LifecycleError):
        return reason == REASON_LAUNCH_FAILED
    return True


# --------------------------------------------------------------------------
# The ledger
# --------------------------------------------------------------------------


class AllowanceLedger:
    """Mints one identity per dispatched invocation and records exactly one entry.

    The ordinal state is per session and lives only here, in memory. That is not a
    weaker form of a durable registry: a session that outlives this process cannot
    be continued at all, so there is no restart in which an ordinal could be
    reissued.
    """

    def __init__(
        self,
        store: AllowanceStore,
        *,
        attempts: int = DEFAULT_ATTEMPTS,
        delay: float = DEFAULT_DELAY_SECONDS,
        sleep: Optional[Callable] = None,
    ) -> None:
        if type(attempts) is not int or attempts < 1:
            raise AllowanceLedgerError(
                REASON_INVALID_ORDINAL, "attempts must be a positive int, got {0!r}".format(attempts)
            )
        self.store = store
        self.attempts = attempts
        self.delay = delay
        self._sleep = sleep if sleep is not None else time.sleep
        self._ordinals: Dict[str, int] = {}

    # -- identity ---------------------------------------------------------

    def next_identity(self, session_id: str, kind: str) -> InvocationIdentity:
        """Mint the next identity for a session. Call this before dispatching.

        Minting before dispatch is what makes a retry safe: the key exists before
        the outcome does, so the same key is replayed rather than a new one minted
        for what is really the same invocation. Ordinals are per session and
        monotonic across both kinds, so they are unique without being gapless -- a
        minted identity whose dispatch never happened simply records nothing.
        """
        if kind not in INVOCATION_KINDS:
            raise AllowanceLedgerError(
                REASON_INVALID_KIND,
                "kind {0!r} is not one of {1}".format(kind, ", ".join(INVOCATION_KINDS)),
            )
        canonical = validate_session_id(session_id)
        ordinal = self._ordinals.get(canonical, 0) + 1
        identity = InvocationIdentity(
            session_id=canonical, kind=kind, ordinal=ordinal
        )
        self._ordinals[canonical] = ordinal
        return identity

    # -- recording --------------------------------------------------------

    def record_completed(self, identity: InvocationIdentity, message: Any) -> Decimal:
        """Record the exact cost the provider reported, or a hole when it reported none.

        A provider-error result is recorded like any other. An errored turn still
        consumed allowance, and the store weighs cost, not success.
        """
        return self._record(identity, result_from_message(identity, message))

    def record_hole(self, identity: InvocationIdentity) -> Decimal:
        """Record a dispatched invocation whose cost is unknown."""
        return self._record(identity, missing_result(identity))

    def record_failure(
        self, identity: InvocationIdentity, error: Any
    ) -> Optional[Decimal]:
        """Record a hole for a dispatched failure, or nothing for a proven pre-dispatch one.

        Returns the new cumulative workload when something was recorded and `None`
        when the refusal preceded dispatch, so a caller can tell the two apart
        without re-deriving the classification.
        """
        if not dispatch_occurred(error):
            return None
        return self.record_hole(identity)

    def _record(self, identity: InvocationIdentity, result: RuntimeResult) -> Decimal:
        """One bounded, identical-key write.

        Every attempt uses the same key, so a landed-then-failed write reconciles
        itself: the accepted duplicate rule returns the current total for an
        identical replay and refuses loudly when the cost differs.
        """
        if type(identity) is not InvocationIdentity:
            raise AllowanceLedgerError(
                REASON_INVALID_KIND,
                "a ledger write needs an InvocationIdentity, got {0!r}".format(
                    type(identity).__name__
                ),
            )
        key = identity.key
        last: Optional[AllowanceStoreError] = None
        for attempt in range(self.attempts):
            try:
                return self.store.record_result(result, idempotency_key=key)
            except AllowanceStoreError as exc:
                if exc.reason not in _RETRYABLE_REASONS:
                    # Conflicts, corruption and unwritable stores are not
                    # contention and must not be retried into silence.
                    raise
                last = exc
                if attempt + 1 < self.attempts:
                    self._sleep(self.delay)

        detail = "{0} after {1} attempt(s) on key {2}: {3}".format(
            last.reason, self.attempts, key, last.detail
        )
        if last.reason == REASON_LOCK_LOST:
            # The durable write may have landed and the replay could not confirm
            # it. Nothing here may assume either way.
            raise AllowanceLedgerError(REASON_LEDGER_UNRECONCILED, detail) from last
        raise AllowanceLedgerError(REASON_LEDGER_CONTENDED, detail) from last

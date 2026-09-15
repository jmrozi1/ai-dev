"""Failure modes of the Dory-wrangler v0.1 durable store.

Contract D3 is fail-closed: nothing is coerced, defaulted, or read on a
best-effort basis. Every one of these is raised instead of degrading, and none
of them has a "repair" path that would let a detected fault become a silent one.
"""


class StoreError(Exception):
    """Base class for every refusal this store makes."""


class StoreCorrupt(StoreError):
    """A durable record on disk is unreadable, unknown, or self-inconsistent.

    Contract D3. Raised on an unknown record type or version, an unrecognized
    field, a malformed record, or a detected gap in an ordered sequence
    (contract 4.2: "A gap means a turn was lost; it is never closed silently").
    """


class NotFound(StoreError):
    """A record the caller named is not in the store."""


class ProvenanceRefused(StoreError):
    """An agent-authored message was not backed by preserved agent evidence.

    Contract D2 / 4.2 FABRICATED_AGENT_MESSAGE / 7 P2a. The store refuses at
    write time, so an unbacked agent message is not merely rejected by the
    validator afterwards -- it never reaches the disk.
    """


class UnsupportedContentType(StoreError):
    """Content richer than text/plain was offered.

    Contract 4.2: content_type is fixed at text/plain in v0.1; anything richer
    fails closed and is counted rather than silently downgraded. The count lives
    on ChatStore.rejected_content_types.
    """


class ConcurrencyRefused(StoreError):
    """A per-chat concurrency rule in contract 8.3 or 8.6 was not satisfied.

    Either the chat already owns an agent (an open binding, or a non-terminal
    session), or a session transition was appended against a state the session
    is no longer in.
    """


class TransitionRefused(StoreError):
    """A session transition is not authorized by contract 5.2."""


class ValidationRefused(StoreError):
    """A record failed the executable contract before it was written."""


class StoreInUse(StoreError):
    """Another process is serving this store (decision 0002, D1).

    v0.1 has exactly one serving process per store. The store-level lock is
    taken before anything is swept, re-attached or written, so a process that
    meets this refusal has changed nothing.
    """


class ReadOnlyStore(StoreError):
    """A write was attempted through a store opened for reading only.

    Read-only tooling -- `validate_store.py`, an offline reader -- may open a
    store another process is serving. It takes no lock, sweeps nothing and
    writes nothing, and this is what it meets if it tries.
    """


# -- refusals of the chat loop and lifecycle (#87's harness) -------------
#
# Stated refusals of an action a user or launcher asked for. Deliberately not
# StoreError subclasses: the store did not refuse a write, the lifecycle
# refused an action before anything was written.


class HarnessError(Exception):
    """Base class. Every refusal carries a reason a user can be shown."""

    def __init__(self, reason):
        Exception.__init__(self, reason)
        self.reason = reason


class ConcurrentLaunchRefused(HarnessError):
    """A second concurrent agent was requested for a chat that already has one.

    v0.1 is one agent per chat, enforced about agents rather than about
    bookkeeping: a chat with any non-terminal session already has an agent that
    may still be alive, whether or not its binding ledger looks tidy. The
    refusal is explicit and carries a stated reason; it is never a silent no-op,
    and it never becomes a queue, a retry, or a replacement.
    """


class TurnInFlightRefused(ConcurrentLaunchRefused):
    """An action met another user action already in flight on the same chat.

    Decision 0003 and the human's decisions of 2026-09-15: a turn sent while a
    turn on the chat is in flight is **refused, not queued**, and a Stop or
    Abandon that meets one is **refused, not deferred**. v0.1 lets a dispatched
    turn finish and does not interrupt it (interruption is #83's supervision
    work), so the refused action has changed nothing, and the user may act again
    once the turn in flight has ended. A subclass of `ConcurrentLaunchRefused`
    because a second turn arriving during a first is that refusal's case too.
    """


class InstructionTooLarge(HarnessError):
    """The instruction exceeds a bound this launcher declared it has measured.

    Nothing in this package asserts a bound. This fires only when a launcher
    declares `instruction_bound_bytes`, and the packet is refused before it is
    recorded, because a packet that was never sent must not be stored as though
    it had been.
    """


class NotPermitted(HarnessError):
    """The lifecycle does not authorise this action from the current state."""

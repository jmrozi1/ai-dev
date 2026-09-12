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

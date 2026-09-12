"""Refusals the harness states rather than performs silently."""

from __future__ import annotations


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


class InstructionTooLarge(HarnessError):
    """The instruction exceeds a bound this launcher declared it has measured.

    Nothing in this package asserts a bound. This fires only when a launcher
    declares `instruction_bound_bytes`, and the packet is refused before it is
    recorded, because a packet that was never sent must not be stored as though
    it had been.
    """


class NotPermitted(HarnessError):
    """The lifecycle does not authorise this action from the current state."""

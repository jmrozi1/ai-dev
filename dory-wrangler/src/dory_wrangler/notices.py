"""The harness's own words: every `author: "system"` message text there can be.

Contract 4.2 permits `author: "system"` with null provenance and places no
requirement on what it says (carried finding R6/A7). This build closes that
from its own side (decision 0006): **system text is fixed harness wording
only.** It is one of the constants below, written as they are, and never
carries anything derived from the integration -- no event bytes, types, counts,
identifiers, details or timing. `ChatStore.append_system_message` refuses any
other text, so no path in this package can write one, and phase 3 of
`tests/run_tests.py` checks every kept store's system messages against this set.

Adding a notice is adding a constant here and to `SYSTEM_TEXTS`, together with
the decision that says when it is written.
"""

# Written once for a turn that was observed to end without producing an agent
# message (decision 0006). It says two things and both are true whenever it is
# written: the turn ended, and nothing the agent sent was lost -- "whatever"
# because a turn may have sent nothing at all.
NO_SHOWABLE_REPLY = (
    "The agent's turn ended without a reply that can be shown here. "
    "Whatever it sent has been preserved."
)

SYSTEM_TEXTS = frozenset((NO_SHOWABLE_REPLY,))

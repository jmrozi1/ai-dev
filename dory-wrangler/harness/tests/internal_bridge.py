"""A faithful model of the proven internal path, written against the seam alone.

**This is test material, not a launcher this product ships.** It lives under
`tests/` and not under `harness/launchers/`, it is registered in
`launchers/registry.py` nowhere, and the package never imports it -- the
registry test that asserts `build_launcher({"launcher": "internal-bridge"})`
fails closed is deliberately still true. It starts no process, opens no socket
and reaches no bridge; it cannot, because the internal bridge is unreachable
from this VM. What it models is the *shape* of the one internal path that is
proven, so that the seam can be exercised against that shape rather than only
against the two development launchers.

It came out of the #87 checkpoint review, which wrote it to answer the release's
largest open question -- can an internal-bridge launcher drop in against contract
6.1's four operations unchanged? It can: three turns produced a transcript
byte-identical to the mode-invariance reference and a store the contract
validator accepts with zero violations, and no operation, field or capability
was added. It is preserved here, adapted to the corrected seam, so #90 starts
from a working model of the internal path rather than rebuilding one the morning
it is needed.

The shape it models, from `facts-and-assumptions.md` F10:

    ~/scripts/launch_agent.sh   <- instruction text
                                -> the agent's own response text

* one-shot: `launch` blocks for the whole agent run and the response comes back
  with it;
* prerequisites -- an active user session and an initialised bridge -- and no way
  to discover either except by attempting a turn;
* no acknowledgement separate from the response;
* **no handle issued by the script**, so the launcher synthesises one;
* **no state across a harness restart**: a fresh instance remembers nothing, and
  `events` on a handle it did not issue fails;
* no measured payload bound (U2), so `instruction_bound_bytes` is `None`;
* raw text rather than a structured transport, one event per response.

What that shape forces on a launcher author, all of it on this side of the seam
and none of it above it:

1. **Synthesise a handle, and make it unique across the launcher's own process
   lifetimes.** `LaunchResult` refuses `accepted` without one and the script
   issues none, so the launcher makes one up; handles are opaque, so that is
   legal. What is *not* legal is a per-instance counter. The harness stores the
   handle durably and hands it back to a fresh launcher process after a restart,
   so a generator that restarts with the process re-issues a live chat's address
   to a different agent -- see `AHandleMustBeUniqueAcrossRestartsNotOnlyWithinOne`
   and the obligation in `harness/README.md`.
2. **Buffer the response and serve it through `events`.** `launch` returns an
   outcome, not text, so the response cannot travel back through the call that
   obtained it.
3. **Emit a launcher-sourced `session_completed` after every response.** The
   bridge reports the agent's text and nothing about its exit. Without a
   manufactured lifecycle event the session never leaves `running` and every
   later turn is refused until the user presses Stop -- see
   `AChatThatCannotReportAnExitNeedsAStopBetweenTurns`.
4. **Honour `after_sequence`.** The harness refuses a launcher whose page does
   not advance (review finding F1), which is a stated refusal rather than a hang.
5. Register it in `launchers/registry.py` -- which this file deliberately does
   not do, because it is a model and not the launcher.
"""

from __future__ import annotations

import json
import uuid

import launch_boundary as lb


def _default_script(instruction_text):
    """Stands in for `~/scripts/launch_agent.sh`. Deliberately the same answer
    shape as every other launcher in this suite, so a transcript produced through
    the modelled internal path is comparable turn for turn with the others."""
    return "answer to: %s" % instruction_text.strip()


class InternalBridgeLauncher(lb.LaunchBoundary):

    launcher_id = "internal-bridge"

    def __init__(self, bridge_ready=True, user_session_active=True, script=None,
                 report_completion=True):
        self._ready = bridge_ready
        self._active = user_session_active
        self._script = script or _default_script
        # Handle -> buffered payloads, in this process only. A one-shot script
        # has nowhere to keep this, which is exactly the point: a fresh instance
        # is a harness restart, and it remembers nothing.
        self._buffers = {}
        self._counter = 0
        # The handle must be unique across this launcher's *process lifetimes*,
        # not merely within one. A fresh instance is a harness restart, and the
        # harness still holds the handles the previous instance issued; a counter
        # that starts again at one hands chat A's recorded address to chat B's
        # live agent, and neither the harness nor the contract validator can
        # notice, because a handle is opaque to both. Deliberately random rather
        # than derived from the process: two instances in one process are two
        # restarts as far as this seam is concerned. The cost is that a store
        # produced through this launcher is no longer byte-identical run to run,
        # which is the right trade -- a generator that is reproducible across
        # restarts is precisely the broken one.
        self._issuer = uuid.uuid4().hex[:12]
        # Whether this bridge can say anything about the agent's exit. False
        # models a bridge that can report only the agent's text.
        self._report_completion = report_completion
        # Every address this launcher was handed, so a claim about what crossed
        # the seam never has to be taken on trust.
        self.addressed = []

    @property
    def capabilities(self):
        # One-shot, and no bound has been measured internally either (U2).
        return lb.LauncherCapabilities(
            lb.CONTINUATION_FRESH_BINDING, lb.RESPONSE_SHAPE_ONE_SHOT, None)

    def launch(self, instruction):
        # The prerequisites, and the only way to discover them: attempt a turn.
        if not self._active:
            raise lb.LauncherError(lb.FAILURE_UNAVAILABLE,
                                   "there is no active user session")
        if not self._ready:
            raise lb.LauncherError(lb.FAILURE_UNAVAILABLE,
                                   "the bridge is not initialised")
        try:
            # Blocks for the whole agent run: the response comes back with the
            # call that started it.
            response = self._script(instruction.instruction_text)
        except Exception as exc:  # noqa: BLE001 - the bridge may fail any way it likes
            raise lb.LauncherError(lb.FAILURE_INTERNAL_ERROR,
                                   "the bridge call failed: %s" % exc)
        if not response:
            # On a one-shot path the response *is* the acknowledgement, so
            # nothing coming back is `no_acknowledgement` rather than a failure
            # the bridge reported.
            raise lb.LauncherError(lb.FAILURE_NO_ACKNOWLEDGEMENT,
                                   "the script returned nothing")

        self._counter += 1
        handle = "internal-bridge-%s-%04d" % (self._issuer, self._counter)

        payloads = [lb.EventPayload(
            1, lb.SOURCE_AGENT, lb.INTERPRETATION_RECOGNIZED,
            response.encode("utf-8"),
            interpreted_type=lb.PAYLOAD_ASSISTANT_TEXT, text=response)]
        if self._report_completion:
            payloads.append(lb.EventPayload(
                2, lb.SOURCE_LAUNCHER, lb.INTERPRETATION_RECOGNIZED,
                json.dumps({"type": "session_completed"}).encode("utf-8"),
                interpreted_type=lb.PAYLOAD_SESSION_COMPLETED))
        self._buffers[handle] = payloads
        return lb.LaunchResult(lb.OUTCOME_ACCEPTED, agent_handle=handle)

    def events(self, agent_handle, after_sequence):
        self.addressed.append(("events", agent_handle))
        buffered = self._buffers.get(agent_handle)
        if buffered is None:
            raise lb.LauncherError(
                lb.FAILURE_UNAVAILABLE,
                "no live record of agent %s; a one-shot bridge keeps no state "
                "across a harness restart" % agent_handle)
        # Honoured, not ignored: the page advances or the harness refuses (F1).
        return lb.EventsPage(
            [p for p in buffered if p.sequence > after_sequence], stream_ended=False)

    def stop(self, agent_handle, reason):
        self.addressed.append(("stop", agent_handle))
        if agent_handle not in self._buffers:
            raise lb.LauncherError(lb.FAILURE_UNAVAILABLE, "nothing to stop")
        return lb.StopAck(False,
                          detail="the one-shot call had already returned; the "
                                 "bridge cannot confirm the agent is gone")

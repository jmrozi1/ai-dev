"""The external Linux development launcher.

This is the environment-specific side of the seam, and it is where every host
mechanic lives: `subprocess`, argv, pipes, exit codes, and a private
newline-delimited-JSON transport. None of it crosses the launch boundary. What
crosses is instruction text plus correlation in, and abstract outcomes,
capabilities, opaque handles and preserved payloads out.

Two profiles, both real processes on this VM, because the two continuation modes
are both first-class and neither is a fallback:

======================  ==========================  ===============
`profile: one_shot`     `fresh_binding` / `one_shot`  run, answer, exit
`profile: persistent`   `persistent` / `stream`       stay alive, one instruction per turn
======================  ==========================  ===============

The `one_shot` profile is the shape of the one internal path that is proven:
`~/scripts/launch_agent.sh` takes instruction text and returns the agent's
response (facts-and-assumptions F10). It is deliberately the default here so
that the mode we can actually rely on internally is the one exercised by
default. Persistent multi-turn delivery is unproven internally (U1) and is
declared by this launcher only because *this* launcher can genuinely do it.

`instruction_bound_bytes` is `None` in both profiles. No bound has been
measured, here or internally, and asserting one would be a fabrication (U2).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading

from launch_boundary import (
    DeliveryAck,
    EventPayload,
    EventsPage,
    FAILURE_INTERNAL_ERROR,
    FAILURE_NO_ACKNOWLEDGEMENT,
    FAILURE_UNAVAILABLE,
    INTERPRETATION_MALFORMED,
    INTERPRETATION_RECOGNIZED,
    INTERPRETATION_UNRECOGNIZED,
    LaunchBoundary,
    LaunchResult,
    LauncherCapabilities,
    LauncherError,
    OUTCOME_ACCEPTED,
    PAYLOAD_ASSISTANT_TEXT,
    PAYLOAD_SESSION_COMPLETED,
    PAYLOAD_SESSION_FAILED,
    PAYLOAD_STREAM_END,
    PAYLOAD_TURN_COMPLETE,
    SOURCE_AGENT,
    SOURCE_LAUNCHER,
    StopAck,
)

DEV_AGENT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dev_agent.py")

PROFILES = {
    "one_shot": LauncherCapabilities("fresh_binding", "one_shot", None),
    "persistent": LauncherCapabilities("persistent", "stream", None),
}

# Types this build knows how to interpret. Anything else is `unrecognized`,
# which is a finding and the primary discovery output of v0.1, never an error.
AGENT_PAYLOAD_TYPES = (PAYLOAD_ASSISTANT_TEXT, PAYLOAD_TURN_COMPLETE)


def _release_pipes(process):
    for pipe in (process.stdin, process.stdout):
        try:
            if pipe is not None and not pipe.closed:
                pipe.close()
        except (OSError, ValueError):
            pass


class _Session(object):
    def __init__(self, handle, process, profile):
        self.handle = handle
        self.process = process
        self.profile = profile
        self.payloads = []
        self.next_sequence = 1
        self.stream_ended = False
        self.lock = threading.RLock()


class DevLocalLauncher(LaunchBoundary):
    launcher_id = "dev-local"

    def __init__(self, profile="one_shot", command=None):
        if profile not in PROFILES:
            raise ValueError("unknown profile %r" % (profile,))
        self._profile = profile
        self._capabilities = PROFILES[profile]
        # The launcher's own configuration, obtained from its own environment
        # rather than from any instruction packet (contract 6.2).
        self._command = list(command) if command else [
            sys.executable, DEV_AGENT, "--profile", profile,
        ]
        self._sessions = {}
        self._lock = threading.RLock()
        self._counter = 0

    @classmethod
    def from_options(cls, options):
        return cls(profile=options.get("profile", "one_shot"),
                   command=options.get("command"))

    @property
    def capabilities(self):
        return self._capabilities

    # -- launch ------------------------------------------------------------

    def launch(self, instruction):
        try:
            process = subprocess.Popen(
                self._command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                universal_newlines=True,
                bufsize=1,
            )
        except OSError as exc:
            # A launch prerequisite was not satisfied. Internally this same
            # category covers an inactive user session or an uninitialised VS
            # Code bridge; only the category crosses the seam.
            raise LauncherError(FAILURE_UNAVAILABLE,
                                "could not start the agent process: %s" % exc)

        with self._lock:
            self._counter += 1
            handle = "dev-local-agent-%04d" % self._counter
        session = _Session(handle, process, self._profile)

        if self._profile == "one_shot":
            stdout, _ = process.communicate(instruction.instruction_text)
            lines = [line for line in (stdout or "").splitlines() if line.strip()]
            if not lines:
                # The call returned without a usable acknowledgement. For a
                # one-shot launcher the response *is* the acknowledgement, so
                # nothing back means nothing was acknowledged.
                raise LauncherError(
                    FAILURE_NO_ACKNOWLEDGEMENT,
                    "the agent process exited with status %s and produced no output"
                    % process.returncode)
            for line in lines:
                session.payloads.append(self._agent_payload(session, line))
            session.payloads.append(self._lifecycle_payload(session, process.returncode))
            session.stream_ended = False  # a one-shot launcher has no stream
        else:
            try:
                process.stdin.write(instruction.instruction_text.rstrip("\n") + "\n")
                process.stdin.flush()
            except (OSError, ValueError) as exc:
                raise LauncherError(FAILURE_INTERNAL_ERROR,
                                    "could not send the instruction: %s" % exc)

        with self._lock:
            # Keyed on the handle this launcher issued, because the handle is the
            # seam's only address (contract 6.1). A launcher is never handed a
            # session id to resolve and is required to remember nothing between
            # calls; this one keeps live processes, so it does remember, but
            # nothing above the seam may depend on that.
            self._sessions[handle] = session
        return LaunchResult(OUTCOME_ACCEPTED, agent_handle=handle)

    # -- deliver -----------------------------------------------------------

    def deliver(self, agent_handle, instruction):
        if not self._capabilities.supports_delivery:
            return LaunchBoundary.deliver(self, agent_handle, instruction)
        session = self._session(agent_handle)
        with session.lock:
            try:
                session.process.stdin.write(
                    instruction.instruction_text.rstrip("\n") + "\n")
                session.process.stdin.flush()
            except (OSError, ValueError) as exc:
                return DeliveryAck(False, detail="the agent is not accepting input: %s" % exc)
        return DeliveryAck(True)

    # -- events ------------------------------------------------------------

    def events(self, agent_handle, after_sequence):
        session = self._session(agent_handle)
        with session.lock:
            if self._profile == "one_shot":
                ready = [p for p in session.payloads if p.sequence > after_sequence]
                # No stream, so never an end of stream -- not by this flag and
                # not by a payload typed `stream_end`.
                return EventsPage(ready, stream_ended=False)

            ready = [p for p in session.payloads if p.sequence > after_sequence]
            if ready:
                return EventsPage(ready, stream_ended=session.stream_ended)
            if session.stream_ended:
                return EventsPage([], stream_ended=True)
            return EventsPage(self._read_until_turn_boundary(session),
                              stream_ended=session.stream_ended)

    def _read_until_turn_boundary(self, session):
        """Block on the agent's stream until it says the turn is over or it ends.

        No timeout, no interval, no retry. If the agent says nothing at all this
        blocks, which is v0.1's honest behaviour for a quiet agent: concluding
        anything from that silence would be a timer, and timers are #83.
        """
        produced = []
        while True:
            line = session.process.stdout.readline()
            if line == "":
                session.stream_ended = True
                produced.append(self._end_of_stream_payload(session))
                return produced
            if not line.strip():
                continue
            payload = self._agent_payload(session, line)
            session.payloads.append(payload)
            produced.append(payload)
            if payload.interpreted_type == PAYLOAD_TURN_COMPLETE:
                return produced

    def _end_of_stream_payload(self, session):
        """The stream closed. Report what that actually was.

        If the process has exited, its status is a lifecycle observation and the
        honest report is `session_completed` or `session_failed`. If the stream
        closed while the agent is still alive, all the launcher observed is that
        the stream ended, which is contract 5.3 cause 2 -- an observed fact,
        unlike silence on an open stream.
        """
        # `Popen.poll` reads this launcher's own child process status. It is not
        # a boundary operation and nothing above the seam can reach it: contract
        # 6.1's prohibition is on the launch boundary exposing an interrogation
        # operation, and it does not.
        returncode = session.process.poll()
        if returncode is None:
            sequence = session.next_sequence
            session.next_sequence += 1
            payload = EventPayload(
                sequence=sequence, source=SOURCE_LAUNCHER,
                interpretation=INTERPRETATION_RECOGNIZED,
                interpreted_type=PAYLOAD_STREAM_END,
                raw=json.dumps({"type": PAYLOAD_STREAM_END,
                                "note": "stdout closed; the agent process is still alive"}
                               ).encode("utf-8"),
            )
        else:
            payload = self._lifecycle_payload(session, returncode)
        session.payloads.append(payload)
        return payload

    # -- stop --------------------------------------------------------------

    def stop(self, agent_handle, reason):
        session = self._session(agent_handle)
        with session.lock:
            if session.process.poll() is not None:
                _release_pipes(session.process)
                return StopAck(True, detail="the agent process had already exited")
            session.process.kill()
            session.process.wait()
            _release_pipes(session.process)
            return StopAck(True, detail="the agent process was stopped: %s" % reason)

    def release_all(self):
        """Close every process this launcher started.

        Not a boundary operation: it is not on `LaunchBoundary`, nothing above
        the seam can reach it, and it changes no session state. It exists so a
        test run does not leak operating-system processes.
        """
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            if session.process.poll() is None:
                session.process.kill()
                session.process.wait()
            _release_pipes(session.process)

    # -- private -----------------------------------------------------------

    def _session(self, agent_handle):
        with self._lock:
            session = self._sessions.get(agent_handle)
        if session is None:
            # A fresh launcher after a harness restart holds no agent state.
            # Re-attachment through this path therefore fails, which the harness
            # records as `reattach_failed` -- an observation, not a conclusion
            # about whether the agent is alive.
            raise LauncherError(
                FAILURE_UNAVAILABLE,
                "this launcher has no live record of agent %s; a development agent "
                "process does not survive the harness that started it" % agent_handle)
        return session

    def _agent_payload(self, session, line):
        sequence = session.next_sequence
        session.next_sequence += 1
        raw = line.rstrip("\n").encode("utf-8")
        try:
            parsed = json.loads(line)
        except ValueError:
            return EventPayload(sequence, SOURCE_AGENT, INTERPRETATION_MALFORMED, raw)
        if not isinstance(parsed, dict) or parsed.get("type") not in AGENT_PAYLOAD_TYPES:
            return EventPayload(sequence, SOURCE_AGENT, INTERPRETATION_UNRECOGNIZED, raw)
        kind = parsed["type"]
        text = parsed.get("text") if kind == PAYLOAD_ASSISTANT_TEXT else None
        if kind == PAYLOAD_ASSISTANT_TEXT and not isinstance(text, str):
            return EventPayload(sequence, SOURCE_AGENT, INTERPRETATION_MALFORMED, raw)
        return EventPayload(sequence, SOURCE_AGENT, INTERPRETATION_RECOGNIZED, raw,
                            interpreted_type=kind, text=text)

    def _lifecycle_payload(self, session, returncode):
        sequence = session.next_sequence
        session.next_sequence += 1
        kind = PAYLOAD_SESSION_COMPLETED if returncode == 0 else PAYLOAD_SESSION_FAILED
        body = json.dumps({"type": kind, "exit": returncode}).encode("utf-8")
        return EventPayload(sequence, SOURCE_LAUNCHER, INTERPRETATION_RECOGNIZED, body,
                            interpreted_type=kind)

"""A model of the internal launcher over the transport proven internally.

**This is test material, not a launcher this product ships.** It lives under
`tests/`, it is registered in `launchers/registry.py` nowhere, and the package
never imports it -- the registry test that asserts
`build_launcher({"launcher": "internal-bridge"})` fails closed is deliberately
still true. The internal bridge is unreachable from here; what this models is the
*shape* of the internal path, so that "the internal launcher drops in unchanged"
is checked against the interface the internal network actually has.

## What was proven internally, and is modelled (#87 and #88, 2026-09-14)

* `launch_agent.sh "<message>"` launches a new agent and returns its response and
  a resume ID; `launch_agent.sh --resumeID=<id> "<message>"` resumes that same
  agent. Continuity across separate invocations was demonstrated.
* Underneath it is `codex exec --json` (fresh) and `codex exec resume --json`
  (resume). **Both emit JSONL in the same shape.** The resume ID is
  `thread.started.thread_id`. The reply is an `item.completed` event whose
  `item.type == "agent_message"`, with its text in `item.text`.

`model_launch_agent.py` stands in for the script, as a real process per call.
This file is the launcher over it:

======================  =====================================================
`launch`                runs the script fresh; the `agent_handle` is the
                        `thread_id` of the `thread.started` event in its output,
                        **taken from the output and never made up**
`deliver`               runs the script with `--resumeID=<agent_handle>`
`events`                every output line of every call on that thread, in
                        order, each through the one classification below
`stop`                  unconfirmed: no stop operation has been shown, and each
                        call had already returned
capabilities            `continuation: persistent` (the normal internal case),
                        `response_shape: one_shot`, no measured bound
======================  =====================================================

## One path for launch and deliver

Neither `launch` nor `deliver` interprets what the script printed beyond finding
the handle, and `launch` finds it with the same `classify` everything else goes
through. Both append the raw output lines, byte for byte, to the thread's spool,
and `events` is the only place a line becomes a payload. There is no plain-text
resume case: a resumed turn that printed something other than JSONL is
`malformed` exactly as a launch that did would be.

`classify` recognises the two proven event types and nothing else:

* `thread.started` carrying a non-empty string `thread_id` -> `recognized`,
  typed `thread.started`; it carries the handle and nothing for the chat;
* `item.completed` whose `item.type == "agent_message"` with string `item.text`
  -> `recognized` `assistant_text`, text `item.text`;
* any other well-formed JSON -> `unrecognized` (including an `item.completed`
  of another item type, which is the honest reading until a capture shows one);
* a line that is not JSON, or one of the two proven types missing the field
  that makes it that type -> `malformed`.

## `response_shape: one_shot`, provisionally

Chosen on the only evidence there is, and provisional until a real capture
arrives: the proven interface is a call that **returns** the response together
with the resume ID, i.e. output is read after the process exits. `codex exec
--json` writing its JSONL incrementally could support `stream`, but that is
inferred, not shown for the internal wrapper, and a `stream` launcher must also
signal an end of stream distinguishably from a quiet one (contract 6.1), which
for a process per turn would read every finished turn as a closed stream.

## What the model must keep, and where

A launcher remembers nothing between calls (contract 6.1), yet `events` must
serve what `launch` and every `deliver` produced, from sequence 1, for the life
of the session. The script returns its output once and the process is gone, so
**the launcher keeps each call's raw output in a spool of its own, keyed by the
handle**, on disk under its own configuration: a fresh instance -- a restart --
serves the same thread from the same spool. That is launcher-owned durable state
(as `scripted_stub`'s `resume_handles` models), not memory in the object: every
operation re-reads the spool and nothing else.

What that shape obliges a launcher author to do, all on this side of the seam:

1. **Take the handle from the output.** The script's own `thread_id` is the
   opaque `agent_handle`; nothing is synthesised, so launcher-author obligation 6
   (uniqueness across restarts) is Codex's property here, and unproven.
2. **Keep the raw output of every call until `events` has served it**, from
   sequence 1, across restarts.
3. **Classify launch and deliver output through one path.**
4. **Honour `after_sequence`** (review finding F1).
5. **Carry the output of a launch that started no thread back with the failure**,
   sourced to the launcher. Without a handle there is no `events` call in that
   session's future, so `LauncherError.payloads` is the only channel those bytes
   have, and contract 7 P1 requires them preserved. Sourcing them to the agent
   is refused by P2a and by the store: nothing proved an agent existed.
6. Register it in `launchers/registry.py` -- which this file deliberately does
   not do, because it is a model and not the launcher.

## Not modelled, because nothing has shown it

The internal Codex version and every other event type; zero or several
`agent_message` items per turn (the model can emit two, as a probe, not a
claim); an explicit end-of-turn event distinct from process exit; error,
non-zero-exit, stderr and prerequisite-failure shapes (the model's mapping below
is a placeholder; what a failed launch *preserves* is settled, what its failure
category should be is not); stop or cancellation; `thread_id` lifetime and invalid-ID
behaviour; concurrency; any payload bound.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys

from dory_wrangler import launch_boundary as lb

MODEL_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "model_launch_agent.py")

TYPE_THREAD_STARTED = "thread.started"
TYPE_ITEM_COMPLETED = "item.completed"
ITEM_AGENT_MESSAGE = "agent_message"


def classify(sequence, raw):
    """One raw output line -> one `EventPayload`. The only interpretation there is."""
    try:
        event = json.loads(raw.decode("utf-8"))
    except ValueError:  # includes UnicodeDecodeError
        return lb.EventPayload(sequence, lb.SOURCE_AGENT, lb.INTERPRETATION_MALFORMED, raw)
    kind = event.get("type") if isinstance(event, dict) else None
    if kind == TYPE_THREAD_STARTED:
        if thread_id_of(event) is None:
            return lb.EventPayload(sequence, lb.SOURCE_AGENT, lb.INTERPRETATION_MALFORMED,
                                   raw)
        return lb.EventPayload(sequence, lb.SOURCE_AGENT, lb.INTERPRETATION_RECOGNIZED, raw,
                               interpreted_type=TYPE_THREAD_STARTED)
    if kind == TYPE_ITEM_COMPLETED:
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == ITEM_AGENT_MESSAGE:
            if not isinstance(item.get("text"), str):
                return lb.EventPayload(sequence, lb.SOURCE_AGENT,
                                       lb.INTERPRETATION_MALFORMED, raw)
            return lb.EventPayload(sequence, lb.SOURCE_AGENT, lb.INTERPRETATION_RECOGNIZED,
                                   raw, interpreted_type=lb.PAYLOAD_ASSISTANT_TEXT,
                                   text=item["text"])
    return lb.EventPayload(sequence, lb.SOURCE_AGENT, lb.INTERPRETATION_UNRECOGNIZED, raw)


def thread_id_of(event):
    thread_id = event.get("thread_id")
    return thread_id if isinstance(thread_id, str) and thread_id != "" else None


def output_lines(stdout):
    """The raw lines of one call's stdout, byte for byte. Blank lines carry no event."""
    return [line for line in stdout.split(b"\n") if line.strip()]


class InternalBridgeLauncher(lb.LaunchBoundary):

    launcher_id = "internal-bridge"

    def __init__(self, codex_home, spool_dir, continuation=lb.CONTINUATION_PERSISTENT,
                 behaviour=(), command=None):
        # Configuration only, from the launcher's own environment. Nothing here
        # is written after construction: every operation reads the spool.
        self._codex_home = codex_home
        self._spool_dir = spool_dir
        self._capabilities = lb.LauncherCapabilities(continuation, lb.RESPONSE_SHAPE_ONE_SHOT,
                                                     None)
        self._behaviour = tuple(behaviour)
        self._command = list(command) if command else [sys.executable, MODEL_SCRIPT]
        self.last_stderr = ""

    @property
    def capabilities(self):
        return self._capabilities

    # -- the four operations -------------------------------------------------

    def launch(self, instruction):
        status, lines = self._run([instruction.instruction_text])
        handle = None
        for sequence, raw in enumerate(lines, 1):
            if classify(sequence, raw).interpreted_type == TYPE_THREAD_STARTED:
                handle = thread_id_of(json.loads(raw.decode("utf-8")))
                break
        if handle is None:
            # No thread, so nothing that could be addressed again. Which category
            # the real script's failures belong to is unproven; this mapping is
            # the model's placeholder.
            #
            # The output of such a call used to have nowhere to go: no handle
            # means no `events` call can ever ask for it, so the harness saw a
            # category and a prose detail and the lines themselves were dropped
            # at the seam -- the intake gap this model exposed. They now travel
            # with the failure and the harness preserves them against the session
            # that failed to open. Every one is sourced to the **launcher**:
            # nothing here proved an agent exists, which is exactly what "no
            # thread started" means, and contract 7 P2a refuses agent-sourced
            # evidence on a session that never ran. Contract 6.1's *Known
            # residual* states that reading. The bytes are the script's own, byte
            # for byte, and `classify`'s interpretation of each line is unchanged;
            # only the source differs from the resumable path.
            payloads = self._failed_launch_payloads(lines, status)
            if not lines:
                raise lb.LauncherError(lb.FAILURE_UNAVAILABLE,
                                       "launch_agent.sh exited %d and printed nothing; "
                                       "stderr ends: %s" % (status, self.last_stderr[-300:]),
                                       payloads=payloads)
            raise lb.LauncherError(lb.FAILURE_NO_ACKNOWLEDGEMENT,
                                   "launch_agent.sh exited %d, printed %d line(s) and no "
                                   "thread started; stderr ends: %s"
                                   % (status, len(lines), self.last_stderr[-300:]),
                                   payloads=payloads)
        self._append(handle, [("agent", raw) for raw in lines])
        if not self._capabilities.supports_delivery:
            # Under fresh_binding the next turn is a new launch, so this agent is
            # done when its call returned. That is the launcher's own observation
            # of the process it ran, sourced to the launcher: not a Codex event.
            self._append(handle, [("launcher", json.dumps(
                {"model_launcher_observation": "launch_agent.sh returned",
                 "exit_status": status}).encode("utf-8"))])
        return lb.LaunchResult(lb.OUTCOME_ACCEPTED, agent_handle=handle)

    def _failed_launch_payloads(self, lines, status):
        """What a launch that started no thread produced, ready to be preserved.

        The script's own output lines first, in order, then one line of the
        launcher's own: the exit status and the tail of stderr. That last line is
        the model observing the process it ran, so it names itself as such in its
        own bytes (`model_launcher_observation`) and invents no Codex event type
        -- `classify` reads it as ordinary `unrecognized` JSON, which is the
        honest reading of a type nothing in the transport defines. It is the same
        shape the fresh-binding path already writes when a call returns.

        stderr and the exit status were previously kept only on the launcher
        object, for a failure's `detail` prose. They are evidence about what the
        integration did, so on the one path where nothing else can carry them
        they are preserved as bytes instead.
        """
        payloads = []
        for sequence, raw in enumerate(lines, 1):
            classified = classify(sequence, raw)
            payloads.append(lb.EventPayload(sequence, lb.SOURCE_LAUNCHER,
                                            classified.interpretation, raw,
                                            interpreted_type=classified.interpreted_type))
        observation = json.dumps({
            "model_launcher_observation": "launch_agent.sh started no thread",
            "exit_status": status,
            "stderr_tail": self.last_stderr[-2000:],
        }).encode("utf-8")
        payloads.append(lb.EventPayload(len(payloads) + 1, lb.SOURCE_LAUNCHER,
                                        lb.INTERPRETATION_UNRECOGNIZED, observation))
        return payloads

    def deliver(self, agent_handle, instruction):
        if not self._capabilities.supports_delivery:
            return lb.LaunchBoundary.deliver(self, agent_handle, instruction)
        self._spool_entries(agent_handle)  # an address this launcher never issued fails here
        status, lines = self._run(["--resumeID=%s" % agent_handle,
                                   instruction.instruction_text])
        self._append(agent_handle, [("agent", raw) for raw in lines])
        if status != 0:
            return lb.DeliveryAck(False, detail="launch_agent.sh --resumeID exited %d" % status)
        return lb.DeliveryAck(True)

    def events(self, agent_handle, after_sequence):
        payloads = []
        for sequence, (source, raw) in enumerate(self._spool_entries(agent_handle), 1):
            if sequence <= after_sequence:
                continue
            if source == "launcher":
                payloads.append(lb.EventPayload(
                    sequence, lb.SOURCE_LAUNCHER, lb.INTERPRETATION_RECOGNIZED, raw,
                    interpreted_type=lb.PAYLOAD_SESSION_COMPLETED))
            else:
                payloads.append(classify(sequence, raw))
        return lb.EventsPage(payloads, stream_ended=False)

    def stop(self, agent_handle, reason):
        self._spool_entries(agent_handle)
        return lb.StopAck(False, detail="launch_agent.sh has no stop operation anyone has "
                                        "shown, and every call had already returned")

    # -- private -------------------------------------------------------------

    def _run(self, args):
        env = dict(os.environ, DORY_MODEL_CODEX_HOME=self._codex_home,
                   DORY_MODEL_CODEX_BEHAVIOUR=",".join(self._behaviour))
        try:
            done = subprocess.run(self._command + list(args), stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, env=env)
        except OSError as exc:
            raise lb.LauncherError(lb.FAILURE_UNAVAILABLE,
                                   "could not run launch_agent.sh: %s" % exc)
        # stderr is not a transport event and has no shown shape, so it is never
        # classified as one. Its tail is kept on the launcher for a failure's
        # `detail`, and on the one path where nothing else can carry it -- a
        # launch that started no thread -- it is preserved as bytes inside the
        # launcher's own observation line (`_failed_launch_payloads`).
        self.last_stderr = done.stderr[-2000:].decode("utf-8", "replace")
        return done.returncode, output_lines(done.stdout)

    def _spool_path(self, agent_handle):
        # The handle is Codex's opaque string. It names a file only once encoded,
        # so no handle can reach outside the spool directory.
        name = base64.urlsafe_b64encode(agent_handle.encode("utf-8")).decode("ascii")
        return os.path.join(self._spool_dir, name + ".jsonl")

    def _append(self, agent_handle, entries):
        os.makedirs(self._spool_dir, exist_ok=True)
        with open(self._spool_path(agent_handle), "a") as spool:
            for source, raw in entries:
                spool.write(json.dumps({"source": source,
                                        "raw": base64.b64encode(raw).decode("ascii")}) + "\n")

    def _spool_entries(self, agent_handle):
        if not isinstance(agent_handle, str) or not agent_handle:
            raise lb.LauncherError(lb.FAILURE_UNAVAILABLE, "no thread is named")
        path = self._spool_path(agent_handle)
        if not os.path.isfile(path):
            raise lb.LauncherError(lb.FAILURE_UNAVAILABLE,
                                   "this launcher holds no output of thread %s" % agent_handle)
        with open(path) as spool:
            entries = [json.loads(line) for line in spool if line.strip()]
        return [(entry["source"], base64.b64decode(entry["raw"])) for entry in entries]

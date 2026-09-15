#!/usr/bin/env python3
"""A stand-in for the internal `launch_agent.sh`, over a model of `codex exec --json`.

**Test material, not product.** It is started as a separate process by the model
launcher in `internal_bridge.py`, once per call, exactly as the internal launcher
starts the real script: nothing survives a call except what the "Codex" side
keeps in its own home directory, which is the property that lets a resume ID
outlive the process that issued it.

The interface is the one proven internally (#86, #87 state, 2026-09-14):

    model_launch_agent.py "<message>"                 fresh: a new thread
    model_launch_agent.py --resumeID=<id> "<message>" resume that same thread

and the output is the JSONL shape proven internally for `codex exec --json` and
`codex exec resume --json` alike, one JSON event per line on stdout:

    {"type": "thread.started", "thread_id": "<id>"}
    {"type": "item.completed", "item": {"type": "agent_message", "text": "..."}}

Those two are the **only** Codex event types this file emits, because they are
the only two anything has shown. Nothing here is taken from Codex documentation
or from memory of it. Every other line it can be told to emit is synthetic and
says so in its own bytes, so it can never be mistaken for a real Codex event:
it exists to exercise the `unrecognized` and `malformed` paths and nothing else.

Configuration comes from this process's own environment, never from the message:

``DORY_MODEL_CODEX_HOME``
    the "Codex" home: `threads/<thread_id>.json` holds each thread's prompts,
    and `calls.jsonl` records every invocation -- its argv, the thread it ran
    on, the bytes it wrote and its exit status -- so a claim about what the
    launcher asked for is read off a log rather than taken on trust.
``DORY_MODEL_CODEX_BEHAVIOUR``
    comma-separated switches for the next invocations: `synthetic-unrecognized`,
    `synthetic-malformed`, `synthetic-not-an-object`, `synthetic-item`,
    `synthetic-typeless`, `synthetic-padded`, `thread-started-without-id-first`,
    `agent-message-without-text`, `no-thread-started`, `no-output`,
    `two-messages`, and
    `plain-text-on-resume` -- the old guess that a resume prints bare text, kept
    only to show that nothing treats it specially.
"""

import json
import os
import sys
import uuid

# The prompt that asks the agent for something only the thread can know. The
# answer draws on the "Codex" side's record of the thread and on nothing the
# launcher passed in, which is what makes a correct answer evidence of
# continuity rather than a label for it -- the nonce demonstration, modelled.
RECALL_PROMPT = "What was the first thing I said in this thread?"

# Deliberately synthetic, and named so in the bytes themselves.
SYNTHETIC_UNRECOGNIZED = {"type": "synthetic.model-only.not-a-codex-event",
                          "note": "emitted by the test model to exercise unrecognized"}
SYNTHETIC_MALFORMED = "SYNTHETIC MODEL-ONLY LINE: deliberately not JSON {{{"
SYNTHETIC_NOT_AN_OBJECT = ["synthetic", "model-only", "well-formed JSON that is not an event object"]
# A completed item of a type nothing has shown, named synthetic in its own type,
# carrying text that must never become chat.
SYNTHETIC_ITEM = {"type": "item.completed",
                  "item": {"type": "synthetic-model-only-item", "text": "SYNTHETIC: never chat"}}
# The proven event types without the field that makes each usable.
THREAD_STARTED_WITHOUT_ID = {"type": "thread.started"}
AGENT_MESSAGE_WITHOUT_TEXT = {"type": "item.completed", "item": {"type": "agent_message"}}
# A synthetic line with whitespace around it, which is part of its bytes.
SYNTHETIC_PADDED = '  {"type": "synthetic.model-only.padded-line"}\t'
# An object with no type at all, whatever else it carries.
SYNTHETIC_TYPELESS = {"note": "SYNTHETIC model-only object with no type",
                      "item": {"type": "agent_message", "text": "SYNTHETIC: never chat"}}


def reply_to(prompts):
    prompt = prompts[-1]
    if prompt.strip() == RECALL_PROMPT:
        return "you first said: %s" % prompts[0].strip()
    return "answer to: %s" % prompt.strip()


def main(argv):
    home = os.environ["DORY_MODEL_CODEX_HOME"]
    behaviour = set(b for b in os.environ.get("DORY_MODEL_CODEX_BEHAVIOUR", "").split(",") if b)
    args = list(argv)
    resume_id = None
    if args and args[0].startswith("--resumeID="):
        resume_id = args.pop(0)[len("--resumeID="):]
    if len(args) != 1:
        sys.stderr.write("usage: launch_agent.sh [--resumeID=<id>] \"<message>\"\n")
        return 64
    message = args[0]

    threads = os.path.join(home, "threads")
    os.makedirs(threads, exist_ok=True)
    lines, status, thread_id = [], 0, None

    if "no-output" in behaviour:
        status = 1
    elif resume_id is not None and not os.path.isfile(os.path.join(threads, resume_id + ".json")):
        # What an unknown resume ID does internally is unproven. The model says
        # nothing on stdout and exits non-zero; it invents no event for it.
        status = 1
    else:
        if resume_id is None:
            thread_id = uuid.uuid4().hex
            thread = {"thread_id": thread_id, "prompts": []}
        else:
            thread_id = resume_id
            with open(os.path.join(threads, resume_id + ".json")) as handle:
                thread = json.load(handle)
        thread["prompts"].append(message)
        path = os.path.join(threads, thread_id + ".json")
        with open(path + ".partial", "w") as handle:
            json.dump(thread, handle)
        os.replace(path + ".partial", path)

        if "thread-started-without-id-first" in behaviour:
            lines.append(json.dumps(THREAD_STARTED_WITHOUT_ID))
        if "no-thread-started" not in behaviour:
            lines.append(json.dumps({"type": "thread.started", "thread_id": thread_id}))
        if "synthetic-unrecognized" in behaviour:
            lines.append(json.dumps(SYNTHETIC_UNRECOGNIZED))
        if "synthetic-malformed" in behaviour:
            lines.append(SYNTHETIC_MALFORMED)
        if "synthetic-not-an-object" in behaviour:
            lines.append(json.dumps(SYNTHETIC_NOT_AN_OBJECT))
        if "synthetic-item" in behaviour:
            lines.append(json.dumps(SYNTHETIC_ITEM))
        if "agent-message-without-text" in behaviour:
            lines.append(json.dumps(AGENT_MESSAGE_WITHOUT_TEXT))
        if "synthetic-typeless" in behaviour:
            lines.append(json.dumps(SYNTHETIC_TYPELESS))
        if "synthetic-padded" in behaviour:
            lines.append(SYNTHETIC_PADDED)
        answer = reply_to(thread["prompts"])
        if resume_id is not None and "plain-text-on-resume" in behaviour:
            lines = [answer]
        else:
            lines.append(json.dumps({"type": "item.completed",
                                     "item": {"type": "agent_message", "text": answer}}))
        if "two-messages" in behaviour:
            lines.append(json.dumps({"type": "item.completed",
                                     "item": {"type": "agent_message",
                                              "text": "and a second message"}}))

    out = "".join(line + "\n" for line in lines)
    sys.stdout.write(out)
    sys.stdout.flush()
    with open(os.path.join(home, "calls.jsonl"), "a") as log:
        log.write(json.dumps({"argv": list(argv), "thread_id": thread_id,
                              "stdout": lines, "exit": status}) + "\n")
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

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
or from memory of it. No other line invents a Codex event type; every one of
them exists to exercise the `unrecognized` and `malformed` paths and nothing
else.

Most of those lines also say "synthetic" in their own bytes, so they can never
be mistaken for a real Codex event. Three deliberately do not, because saying it
would destroy the case each exists to make (re-review finding N8):

* `THREAD_STARTED_WITHOUT_ID` and `AGENT_MESSAGE_WITHOUT_TEXT` are the two proven
  types with the one field that makes each usable removed, and that missing field
  *is* the case: a real `thread.started` with no `thread_id`, a real
  `agent_message` with no `text`. A marker field would make them objects no Codex
  could emit, and the case would go untested. Neither invents a type.
* `plain-text-on-resume` prints the answer as bare text -- the old guess about
  what a resume prints, kept to show nothing treats it specially. Its bytes are
  the agent's real answer on purpose: the test
  (`test_a_resume_that_prints_plain_text_is_malformed_like_any_other_line`)
  asserts that real answer text arriving unwrapped is `malformed` and preserved
  and never becomes chat. Replacing it with a marked string would remove the
  content whose fate is the whole point.

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
    `two-messages`, `no-agent-message` (a thread starts and no `agent_message`
    follows: a turn that produces no text), `padded-message` (the answer's
    `item.text` carries leading and trailing whitespace and newlines, which are
    part of the text), `park` (before printing anything, create `parked` in the
    Codex home and wait up to a minute for `release` there, so a test can act
    while a turn is in flight), and
    `plain-text-on-resume` -- the old guess that a resume prints bare text, kept
    only to show that nothing treats it specially. Also `synthetic-not-utf8`
    (a line that is not UTF-8, saying SYNTHETIC in its own bytes), `empty-agent-message`
    (the answer's `item.text` is `""`: the proven type with the one value that
    shows nothing, which -- like the two field-less cases above -- would be
    destroyed by a marker), and `tricky-texts` (three extra answers whose text
    carries `\r\n`, a decomposed character and only whitespace, all part of
    the text).
``behaviour-once`` (a file in the Codex home)
    comma-separated switches added for the **next invocation only**; the file
    is removed when read. It lets a test change what one turn's agent does
    without restarting the process that hosts the launcher.
"""

import json
import os
import sys
import time
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
# Not UTF-8, and saying so in its own bytes.
SYNTHETIC_NOT_UTF8 = b"SYNTHETIC MODEL-ONLY LINE: \xff\xfe is not UTF-8 {{{"
# What `tricky-texts` answers with, in order: text a transcript must keep exactly.
TRICKY_TEXTS = ("line one\r\nline two\r\n", "cafe\u0301 and n\u0303", " \t\n  ")
# What `padded-message` wraps the answer in. Whitespace an agent wrote is its text.
PADDED_BEFORE = "\n  \t"
PADDED_AFTER = "  \n\n \t"


def reply_to(prompts):
    prompt = prompts[-1]
    if prompt.strip() == RECALL_PROMPT:
        return "you first said: %s" % prompts[0].strip()
    return "answer to: %s" % prompt.strip()


def main(argv):
    home = os.environ["DORY_MODEL_CODEX_HOME"]
    behaviour = set(b for b in os.environ.get("DORY_MODEL_CODEX_BEHAVIOUR", "").split(",") if b)
    once = os.path.join(home, "behaviour-once")
    if os.path.exists(once):
        with open(once) as handle:
            behaviour |= set(b for b in handle.read().strip().split(",") if b)
        os.unlink(once)
    args = list(argv)
    resume_id = None
    if args and args[0].startswith("--resumeID="):
        resume_id = args.pop(0)[len("--resumeID="):]
    if len(args) != 1:
        sys.stderr.write("usage: launch_agent.sh [--resumeID=<id>] \"<message>\"\n")
        return 64
    message = args[0]

    if "stderr-noise" in behaviour:
        # Not a claim about what the real script prints on stderr -- nothing has
        # shown that, and the module's "not modelled" list says so. It exists so
        # a probe can check that stderr is *preserved* where nothing else can
        # carry it, rather than that a key with an empty value is written.
        sys.stderr.write("model: a prerequisite check wrote this to stderr\n")

    if "park" in behaviour:
        open(os.path.join(home, "parked"), "a").close()
        deadline = time.time() + 60
        while not os.path.exists(os.path.join(home, "release")) and time.time() < deadline:
            time.sleep(0.02)

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
        if "synthetic-not-utf8" in behaviour:
            lines.append(SYNTHETIC_NOT_UTF8)
        if "synthetic-padded" in behaviour:
            lines.append(SYNTHETIC_PADDED)
        answer = reply_to(thread["prompts"])
        if "padded-message" in behaviour:
            answer = PADDED_BEFORE + answer + PADDED_AFTER
        if resume_id is not None and "plain-text-on-resume" in behaviour:
            lines = [answer]
        elif "empty-agent-message" in behaviour:
            lines.append(json.dumps({"type": "item.completed",
                                     "item": {"type": "agent_message", "text": ""}}))
        elif "no-agent-message" not in behaviour:
            lines.append(json.dumps({"type": "item.completed",
                                     "item": {"type": "agent_message", "text": answer}}))
        if "tricky-texts" in behaviour:
            for text in TRICKY_TEXTS:
                lines.append(json.dumps({"type": "item.completed",
                                         "item": {"type": "agent_message", "text": text}}))
        if "two-messages" in behaviour:
            lines.append(json.dumps({"type": "item.completed",
                                     "item": {"type": "agent_message",
                                              "text": "and a second message"}}))

    out = b"".join((line if isinstance(line, bytes) else line.encode("utf-8")) + b"\n"
                   for line in lines)
    sys.stdout.buffer.write(out)
    sys.stdout.flush()
    with open(os.path.join(home, "calls.jsonl"), "a") as log:
        log.write(json.dumps({"argv": list(argv), "thread_id": thread_id,
                              "stdout": [line.decode("utf-8", "backslashreplace")
                                         if isinstance(line, bytes) else line
                                         for line in lines],
                              "exit": status}) + "\n")
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

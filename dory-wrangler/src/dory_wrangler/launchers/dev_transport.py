r"""The development transport: its framing, its recognized set, declared once, and its one classifier.

The development agent (`dev_agent.py`) writes newline-delimited JSON, one object
per line, `{"type": ..., ...}`. `dev_local.py` reads that transport from a real
process, and `scripted_stub.py` produces payloads in exactly the same bytes
in-process, so the two launchers share one wire format -- and therefore one
framing, one declaration and one classifier, all here. Nothing else in the
package decides what a line of this transport is. The internal Codex JSONL is a
different wire format with its own declaration (`tests/internal_bridge.py`, a
model, not a shipped launcher).

**Framing (`read_line`).** A line is the agent's stdout **bytes** up to a
`b"\n"`, and nothing else ends one: not `\r`, not U+2028 or U+0085, not any
other character `str.splitlines` would split on. The line is handed to the
classifier as those bytes, without the `\n` and otherwise untouched -- a `\r`,
trailing spaces and tabs, and bytes that are not UTF-8 all stay in `raw`. Nothing
is decoded before the classifier, so a line that is not UTF-8 reaches it and is
`malformed`, with its bytes preserved (base64 in the store). A line whose bytes
are only ASCII whitespace carries no event and is skipped, as in the Codex
model's `internal_bridge.output_lines`. `dev-local` reads both of its profiles
through this one function.

This is the launcher side of the seam (contract 6.1: a launcher parses its own
transport and reports what it recognised). The harness never classifies; it
preserves what the launcher reports and declines only the one reading contract
6.1 forbids on a session (`session_manager.PRESERVE_UNATTRIBUTABLE_STREAM_END`).

**The rule (contract 7 P2), exactly:**

* a line that is not UTF-8 JSON is `malformed`;
* a JSON value whose `type` is a string naming an entry of `RECOGNIZED` is
  `recognized` as that entry's `interpreted_type` -- unless the entry names a
  text field and that field is not a string, in which case the line is a known
  type missing what makes it that type, and is `malformed`;
* every other well-formed JSON value -- another type, no type, a type that is not
  a string, or not an object at all -- is `unrecognized`.

`unrecognized` and `malformed` carry `interpreted_type: None` and no text, so
neither can become chat (`EventPayload.is_chat_text`, contract 4.2, 7 P4).

**Adding a type** is one entry in `RECOGNIZED`, and only on the evidence of real
output that carries it -- see `decisions/0004-classifying-event-types.md`.
"""

from __future__ import annotations

import collections
import json

from ..launch_boundary import (
    EventPayload,
    INTERPRETATION_MALFORMED,
    INTERPRETATION_RECOGNIZED,
    INTERPRETATION_UNRECOGNIZED,
    PAYLOAD_ASSISTANT_TEXT,
    PAYLOAD_TURN_COMPLETE,
    SOURCE_AGENT,
)

# `interpreted_type` is what the record is written as. `text_field` names the
# field carrying user-visible text, which must then be a string; None means the
# type carries no text and requires no field.
Recognized = collections.namedtuple("Recognized", ("interpreted_type", "text_field"))

# The development transport's recognized set: wire `type` -> reading. Exactly
# the two types `dev_agent.py` emits as meaning something. Anything else the
# agent says is `unrecognized`, which is a finding, never an error.
RECOGNIZED = {
    "assistant_text": Recognized(PAYLOAD_ASSISTANT_TEXT, "text"),
    "turn_complete": Recognized(PAYLOAD_TURN_COMPLETE, None),
}


def read_line(stream):
    r"""The next line of the transport from a binary `stream`, as its raw bytes
    without the terminating `b"\n"`, or None at end of stream.

    Split on `b"\n"` only (a binary `readline`), never decoded. A line of only
    ASCII whitespace -- `b""`, `b"\r"`, spaces, tabs -- carries no event and is
    skipped. The last line may lack its `b"\n"`; it is still a line.
    """
    while True:
        line = stream.readline()
        if not line:
            return None
        if line.endswith(b"\n"):
            line = line[:-1]
        if line.strip():
            return line


def interpret(raw):
    """One raw line's bytes -> `(interpretation, interpreted_type, text)`.

    A function of the bytes and of `RECOGNIZED` alone: nothing about the session,
    the launcher's profile, or whether this was a launch or a later turn.
    """
    try:
        event = json.loads(raw.decode("utf-8"))
    except ValueError:  # includes UnicodeDecodeError
        return INTERPRETATION_MALFORMED, None, None
    kind = event.get("type") if isinstance(event, dict) else None
    reading = RECOGNIZED.get(kind) if isinstance(kind, str) else None
    if reading is None:
        return INTERPRETATION_UNRECOGNIZED, None, None
    text = None
    if reading.text_field is not None:
        text = event.get(reading.text_field)
        if not isinstance(text, str):
            return INTERPRETATION_MALFORMED, None, None
    return INTERPRETATION_RECOGNIZED, reading.interpreted_type, text


def classify(sequence, raw, source=SOURCE_AGENT):
    """One raw line -> one `EventPayload`, through `interpret` and nothing else."""
    interpretation, interpreted_type, text = interpret(raw)
    return EventPayload(sequence, source, interpretation, raw,
                        interpreted_type=interpreted_type, text=text)

# Decision 0004: event types are classified from a set declared once per wire format

Status: recorded by #88 (checkpoint `classify-known-event-types`).
Scope: Dory-wrangler v0.1.

## Who classifies

The **launcher** does, on its side of the seam. It reads its own transport and
reports each payload's `interpretation` and `interpreted_type` on the
`EventPayload` it hands over (`launch_boundary.py`; `launch-boundary.md`
obligation 8). The harness never classifies. It preserves what it is given
(contract 7 P1) and declines exactly one reading: a `stream_end` from a session
whose recorded launcher has no stream is written `unrecognized` with
`interpreted_type: null` and the assertion is refused (contract 6.1 and 7 P2, the
human's decision of 2026-09-15, `session_manager.PRESERVE_UNATTRIBUTABLE_STREAM_END`).

## The recognized set, per wire format, declared once

Each wire format's set is **data in one place**, and every classifier of that
format reads it through one function. Launch and every later turn go through the
same function; there is no resumed-turn case.

**Development transport** -- newline-delimited JSON objects `{"type": ...}`,
written by `launchers/dev_agent.py`, read by `dev-local` from a real process and
produced byte-for-byte in-process by `scripted-stub`. Declared in
`launchers/dev_transport.py` as `RECOGNIZED`; classified by
`dev_transport.interpret` / `classify`.

*Framing* is part of the wire format and is also in one place,
`dev_transport.read_line`, which `dev-local` reads both profiles through. It
reads the agent's stdout as **bytes** and ends a line at `b"\n"` and nothing
else -- not `\r`, not U+2028 or U+0085 -- and hands the classifier the line's
bytes without that `\n` and otherwise untouched. Nothing is decoded first, so a
`\r`, trailing whitespace and bytes that are not UTF-8 are all in `raw`, and
the same wire bytes read the same way under `one_shot` and `persistent`. A line
of only ASCII whitespace carries no event and is skipped. The Codex model frames
the same way (`internal_bridge.output_lines`). The instruction written *to* the
agent stays text, encoded with the encoding the text-mode pipe always used; it
carries no evidence, because the harness records the instruction before the
launcher sees it. `scripted-stub` has no pipe and so no framing: it hands the
classifier each line's bytes directly.

| Wire `type` | Recorded `interpreted_type` | Required | Chat text |
| --- | --- | --- | --- |
| `assistant_text` | `assistant_text` | `text`, a string | `text` |
| `turn_complete` | `turn_complete` | nothing | none |

**Codex JSONL** -- `codex exec --json` and `codex exec resume --json`, the one
internal path proven (#87/#88, 2026-09-14). No shipped launcher reads it yet; the
real internal launcher is #90. The in-repo model `tests/internal_bridge.py`
declares it as `RECOGNIZED` and classifies with `interpret` / `classify` on
launch, resume, and a launch that started no thread (sourced to the launcher).

| Wire `type` (and `item.type`) | Recorded `interpreted_type` | Required | Chat text |
| --- | --- | --- | --- |
| `thread.started` | `thread.started` | `thread_id`, a non-empty string | none |
| `item.completed` / `agent_message` | `assistant_text` | `item.text`, a string | `item.text` |

**Launcher-synthesised records are not a wire format.** `dev-local` and
`scripted-stub` write their own observations of the agent they run --
`session_completed`, `session_failed`, `stream_end`, sourced to the launcher --
and the model writes a fresh-binding `session_completed`. They are stated, not
read, so no classifier re-reads them; their own bytes name the type recorded
(checked by `tests/reclassify_stores.py`).

## The boundary between `recognized`, `unrecognized`, and `malformed`

For every wire format, applied to the bytes of one framed line:

* **`malformed`** -- the line is not UTF-8 JSON, **or** it is a declared type
  missing the field the declaration requires. A body that looks like an
  answer -- `assistant_text` with no string `text`, `agent_message` with no string
  `item.text` -- is `malformed`. Contract 7 P2 says "could not be parsed at
  all"; this reads it as "could not be parsed *into its type*", which is the
  established reading since the JSONL model (#88).
* **`unrecognized`** -- well-formed JSON of any other shape: an undeclared type,
  no type, a type that is not a string, an `item.completed` of another item
  type, or JSON that is not an object. A finding, never an error (contract 7).
* **`recognized`** -- a declared type with its required field.

`unrecognized` and `malformed` carry `interpreted_type: null` and no text, so
neither can become chat: the harness writes an agent message only for a
recognized, agent-sourced `assistant_text` (`EventPayload.is_chat_text`), and the
store refuses a message citing anything else (contract 4.2,
`MALFORMED_EVENT_RENDERED`).

## Adding a type

Only from **real observed output**. Nothing is added from documentation or from
memory of a transport, and nothing a model or stub emits counts as evidence.

1. Cite the capture: where it came from, the transport and version, and the raw
   line, in the commit and in this record.
2. Add one entry to that format's declaration -- nothing else changes, because
   every classifier of the format reads it.
3. Pin it: the exact-set test in `tests/test_classification.py` changes with a
   stated reason, and a test shows the new line recognized and the same line
   missing its required field `malformed`.

A type recognized but not rendered is still not chat; rendering what a new type
means is its own change.

## Evidence

`tests/test_classification.py`: each format's exact set, the type string matched
exactly (no whitespace trimmed) and the model's non-empty `thread_id` meaning
non-empty rather than non-blank; the declaration changed
under both development launchers and under the model's launch, resume and
failed-launch paths, and every one follows it; the same line read identically on
launch and on resume; every event each launcher preserves re-read from
`raw.body` reproducing what was recorded; no `unrecognized` or `malformed`
payload -- the answer lookalikes included -- becoming a message, at the store and
over HTTP through `run_shell.py` with `dev-local` and `scripted-stub` chosen by
configuration; and `dev-local`'s framing -- the same wire bytes read the same way
in both profiles, a line that is not UTF-8 preserved `malformed` with its bytes
intact while the rest of its turn is answered, a `\r` and trailing whitespace
kept in `raw` -- in process and over HTTP. `tests/reclassify_stores.py` runs the
re-read over every store the suite keeps, as phase 3 of `run_tests.py`, and
fails the run on any record that does not reproduce and is not adjudicated by
name.

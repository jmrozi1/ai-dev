# Decision 0005: what the conversation renders, and the exact-text gate

Status: recorded by #88 (checkpoint `render-useful-events-in-chat`).
Scope: Dory-wrangler v0.1.

## The rule

Contract 4.2 makes `message` the only record a transcript renders, `text/plain`
its only content type, and requires every agent message to cite a `recognized`,
`source: "agent"` event of the same chat and session. So in v0.1, rendering the
useful subset means exactly this:

* **Each recognized, agent-sourced event that carries non-empty text becomes
  exactly one agent message**, whose `content.text` is exactly that event's text,
  and which cites that event. The text is the one field decision 0004's table
  names for the event's wire format -- `text` for the development transport,
  `item.text` for the Codex JSONL -- as the format's classifier extracts it from
  the preserved bytes: no strip, no newline normalisation, no decoding beyond
  JSON's own, no truncation, no join.
* **In event order.** A turn whose agent produces several text events renders
  that many messages, in the order of the events they cite. A turn that produces
  none renders no agent message, and nothing is written in its place.
* **Every other event renders nothing** and is still preserved: recognized
  non-text types (`turn_complete`, `thread.started`, launcher lifecycle and
  end-of-stream records), every `unrecognized` event, and every `malformed` one.
* **"Text-bearing" means non-empty.** A recognized `assistant_text` whose text is
  `""` is preserved and cited by nothing: it would show the user nothing, and the
  store refuses a message with no text.

No new content type, author, or record kind, and no `system` message, is
introduced for this. Nothing in the harness changed to deliver it: the one path
that writes an agent message (`SessionManager._take_payload`, reached from the
drain and from re-attachment through `_take_page`) already wrote
`EventPayload.text`, which is the classifier's output, for exactly the payloads
`EventPayload.is_chat_text` admits. What this decision adds is the rule written
down, and a gate that fails when a store breaks it.

## The gate (A6)

A6 said that nothing compared an agent message's text to the evidence it cites,
so contract 4.2 enforced attribution, not transcription. The comparison is now
made where it cannot be skipped: `tests/reclassify_stores.py`, phase 3 of
`run_tests.py`, re-reads every kept store and, for every agent message,
re-classifies the bytes of the event it cites with the classifier of that
session's recorded launcher, and requires a recognized `assistant_text` whose
text equals the message's text exactly. It also requires the converse: every
agent-sourced event whose bytes re-read as non-empty chat text is cited by
exactly one message, and a session's messages cite its events in event order.
Any disagreement fails the suite unless `ADJUDICATED_MESSAGES` names it with a
reason; that table is empty. A message on a session recorded against a launcher
no classifier in the repository reads is counted as `other launcher`, never as
exact, and fails the gate unless the launcher is adjudicated by name (below).

This is the rule #85's queued attribution wording correction waits on. The
contract is not changed here; that correction stays queued.

## What the page does with the text

The served page puts message text into the DOM only through `textContent`
(`element()` in `webapp.PAGE`), never as HTML, on both paths it takes: the
transcript bubble -- the same `renderChat` on first load, after a send and after
Abandon, each rendering the chat the server returned -- and the conversation
list's preview. The bubble is `white-space: pre-wrap`, so runs of spaces, tabs
and newlines, and leading whitespace, display as sent. Per CSS, spaces at the end
of a line hang and a single final newline opens no empty line; neither carries
anything visible. The preview is a one-line, ellipsised summary
(`white-space: nowrap`) and is not where a message is read. There is no browser
on the development host, so this is from the page's source and served bytes.

## Deferred, and where it went

Telling the user that something arrived which could not be shown -- an
`unrecognized` or `malformed` event, or a turn that produced no text -- was
deferred from here to `handle-unsupported-and-malformed-events`, and is decided
in `0006-no-showable-reply-notice.md`: a turn observed to end with no agent
message gets one `system` message in fixed words; nothing per event and no
counts. It changes nothing above: no event renders differently, and agent
messages are still exactly their events' text.

The gate described above now also fails on a record under a launcher no
classifier here reads, unless that launcher is named with a reason in
`ADJUDICATED_LAUNCHERS` (render review F1), and keys every adjudication on the
store, the session, the sequence and a hash of the record's body (classification
check L1).

## Evidence

`tests/test_rendering.py`: zero and several text events per turn in both wire
formats, in process and over HTTP (`run_shell.py` with `dev-local`, and
`model_shell.py` hosting the Codex model, which no registry selects), with the
turn completing and a second turn sent while one is in flight still refused; the
four acceptance cases -- normal text, a non-text type, an unknown type, a
malformed event -- as one group, `TheFourAcceptanceCases`; an empty-text event
rendering nothing; the gate failing on a planted store by one character, by
trailing whitespace alone, and on a text event rendered never or twice; and the
page's insertion code and markup served literally.

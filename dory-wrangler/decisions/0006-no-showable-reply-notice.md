# Decision 0006: a turn that ends with nothing to show says so, once, in fixed words

Status: recorded by #88 (checkpoint `handle-unsupported-and-malformed-events`).
The notice is the orchestrator's decision of 2026-09-23 and is **overridable by
the human**. Scope: Dory-wrangler v0.1.

## Why

Decision 0005 renders only recognized agent text. A turn whose events are all
unrecognized, malformed, of a type that carries no text, or an empty answer
therefore rendered nothing at all: the user's message sat alone and the chat
looked stuck, with no signal that the turn had ended. Two alternatives were
rejected: silence, for that reason; and a per-event or per-count display, which
is diagnostic UI and activity summarization, outside v0.1 (contract 7 P4, #82).

## The notice

When a turn is **observed to end** and it produced **no agent message**, the
harness appends exactly one message with `author: "system"`, whose text is
exactly `notices.NO_SHOWABLE_REPLY`:

> The agent's turn ended without a reply that can be shown here. Whatever it sent has been preserved.

It uses only what contract 4.2 already permits -- `author: "system"`, null
`session_id` and `source_event_id` -- and the store's existing
`append_system_message`. No record kind, author or content type is added. The
turn floor ignores system messages (contract 6.4), so the notice cannot make an
answer's instruction count come up short. The shell already renders a system
message apart from agent speech (its own `SYSTEM` label and styling).

### When a turn is observed to end

Only on something the launcher reported or the call itself did, never on time
(contract 1):

| Path | Observed end |
| --- | --- |
| drain, `one_shot` session | the launch or delivery call has returned with its whole response, and reading reached a page with nothing more on it; or the launcher reported one of the ends below |
| drain, `stream` session | a recognized `turn_complete`; a launcher's `session_completed` or `session_failed`; a launcher's `stream_end` |
| re-attachment at start | the page it read reports one of those ends; or, on a `one_shot` session, the page carries anything not yet stored -- a one-shot launcher serves only what calls that have already returned produced |

**Not a turn end:** a read that failed on our side (`stream_read_failed`), a
read that returned something that is not a page, an empty page on a session with
a stream (a quiet stream is not evidence), and an empty page at re-attachment. A
turn that never ends therefore stays in flight exactly as before, with no notice.
A launch that failed or whose outcome is `unknown` produced no agent to have a
turn, and is not given one either.

### When it is written, and when it is not

* **Once per turn, idempotent.** The turn is the chat's last user message: a turn
  is recorded only when it is offered to the chat's one live agent (decision
  0003), so the last one is the turn being answered. The notice is written only
  if no agent message and no system message follows it; a system message there
  is this notice, already written, because the harness writes no other. So the
  drain, re-attachment and any number of restarts apply one rule and write it at
  most once, from the chat's durable messages alone.
* **Never for a turn that showed a reply**, even one that also carried
  unrecognized or malformed events.
* **Never over a lost payload.** It says whatever the agent sent was preserved,
  so it is written only when every payload read was held by the store exactly
  as it arrived (checked by reading the record back). A refusal that came after
  the bytes were kept -- a declined claim, such as a one-shot launcher's
  `stream_end` -- loses nothing: the notice is written and the refusal is still
  raised. A refusal that cost a payload -- a sequence gap, a contradicting
  replay, a failed write -- leaves no notice.

The notice may be the only thing a turn shows, and it is also what the chat
list's preview shows until the next message.

## The rule it makes necessary: system text is fixed harness wording only

Contract 4.2 requires an agent message to cite evidence, and places no
requirement at all on a system message (carried finding R6/A7). Writing system
messages at all makes that gap live, so this build closes it from its own side:

* every system message's text is one of `notices.SYSTEM_TEXTS` -- today exactly
  `NO_SHOWABLE_REPLY` -- and carries **nothing derived from the integration**: no
  event bytes, types, counts, identifiers, details, or timing;
* `ChatStore.append_system_message` refuses any other text before anything is
  written, so no path in this package can write one;
* phase 3 of `run_tests.py` (`tests/reclassify_stores.py`) fails on any kept
  store whose system message is not the fixed words, precedes any user turn,
  is the second in its turn, or sits in a turn that has an agent message.

The contract is unchanged; A7 remains a contract residual (a store with
arbitrary system text still validates), now closed in this implementation twice.
Adding a notice means adding a constant to `notices.py` and the decision that
says when it is written.

## Named cases, not inferred

* A harness that dies between preserving a turn's end and writing its notice
  leaves that turn without one: re-attachment then reads an empty page and
  observes nothing. The two writes cannot be one without new store machinery.
* A harness that dies inside the call, before a one-shot launcher returned,
  leaves the turn without a notice after restart: nothing observed its end.
* Under `fresh_binding`, a launcher that never reports its agent done keeps that
  one agent `running`, so the next send is refused with the words that name
  Abandon, the exit decision D2 gives every non-terminal state. The harness does
  not conclude the agent ended; that would be lifecycle inference.

## Evidence

`tests/test_unsupported_and_malformed.py` (items B and C of the checkpoint, both
wire formats, in process and over HTTP through `run_shell.py` and
`model_shell.py`, re-attachment and restarts); `tests/test_store.py`
(`test_a_system_message_carries_the_harness_s_fixed_words_only`);
`tests/test_carried_gates.py` (the phase-3 system-message check);
`tests/test_adversarial.py` (A7, planted around the store).

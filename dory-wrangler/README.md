# Dory-wrangler

Dory-wrangler is an independent product. It lives in the `ai-dev` repository
because that repository is already mirrored onto the internal network nightly,
not because it continues the frozen AI Dev product. Nothing outside this
directory is a Dory-wrangler product path, and Coxswain does not depend on
anything here.

Release intent and the checkpoint roadmap are `jmrozi1/ai-dev` #81.

## Where this product lives

`jmrozi1/ai-dev`, path `dory-wrangler/`, is canonical, and it is the only
canonical copy. Two earlier locations still exist and neither is authoritative:

- The former `jmrozi1/dory-wrangler` repository is **archived, not deleted**.
  Its README reads "Archived -- Dory-wrangler moved to `jmrozi1/ai-dev`" and
  names this repository and this directory as canonical; its description reads
  "ARCHIVED -- superseded"; and its twelve issues are preserved read-only and
  were deliberately not migrated. New work is opened on `jmrozi1/ai-dev`.
- `relay/dory-wrangler/` on this repository's `main` branch is **superseded and
  deliberately left untouched**. It is a one-way transport of issue snapshots
  from that now-archived repository into the internal network, written before
  this product moved here, and it still names that repository as the canonical
  source of tickets -- which it no longer is. It is not a Dory-wrangler product
  path and no code here reads it; `contract/v0.1/facts-and-assumptions.md` names
  it only as the era some superseded prior observations came from. `main` is not
  edited merely to clean up a stale transport artifact.

## Contents

| Path | What it is |
| --- | --- |
| `contract/v0.1/contract.md` | the normative v0.1 chat, session, agent-binding, launch-boundary, and diagnostic contract |
| `contract/v0.1/facts-and-assumptions.md` | what is proven about the integration today versus what is assumed, as claims internal dogfood can settle |
| `fixtures/v0.1/valid/` | store snapshots the contract must accept, including both continuation modes and both restart outcomes |
| `fixtures/v0.1/invalid/` | store snapshots the contract must reject, each naming the rule it violates |
| `validator/validate_contract.py` | the executable form of the contract |
| `launch-boundary.md` | the swappable single-agent launch boundary (#87): the seam, the launchers, and the launcher-author obligations |
| `decisions/0001-runtime-and-storage.md` | the recorded runtime and storage choice, and its Rocky Linux 9 risks as claims to settle |
| `decisions/0002-one-store-one-application.md` | how #86's and #87's implementations became one store, one chat loop, one seam and one served application, per component, with the evidence |
| `decisions/0003-concurrent-turns-and-abandon.md` | a concurrent turn is refused before it is recorded (and how to flip that), Abandon as the one lifecycle action, and the refusal of both while a turn is in flight |
| `decisions/0004-classifying-event-types.md` | the recognized event types per wire format, declared once, where classification happens, the `unrecognized`/`malformed` boundary, and the rule for adding a type |
| `decisions/0005-rendering-useful-events.md` | what the conversation renders -- one agent message per recognized agent text event, its text exactly, in event order, nothing else -- and the exact-text gate over every kept store |
| `decisions/0006-no-showable-reply-notice.md` | the one notice a turn gets when it ends with nothing that can be shown, when it is and is not written, and the rule that system text is the harness's fixed words only |
| `decisions/0007-bounded-diagnostic-retrieval.md` | the one read-only command that prints a chat's preserved evidence verbatim and bounded, and the rule that it derives nothing |
| `src/dory_wrangler/` | the one product package: the durable store (`store.py`), the chat loop (`session_manager.py`), the launch seam (`launch_boundary.py`), the launchers (`launchers/`), and the served shell (`webapp.py`) |
| `skills/adversarial-guard-verification/SKILL.md` | how this product checks that a diff's guards are really pinned: the mutation-runner contract, how to enumerate and adjudicate rows, and the decision-conformance pass |
| `run_shell.py` | run the shell |
| `validate_store.py` | check a live store against the contract |
| `diagnostics.py` | print a chat's preserved raw evidence, verbatim and bounded (contract P4) |
| `tests/` | the one test suite for all of it, including both sides' adversarial probes |

## Running the shell

```
python3 dory-wrangler/run_shell.py --root ./dory-store
python3 dory-wrangler/run_shell.py --root ./dory-store --launcher scripted-stub
python3 dory-wrangler/run_shell.py --root ./dory-store --launcher dev-local \
    --launcher-options '{"profile": "persistent"}'
```

Then open the printed `http://127.0.0.1:8765`. Standard library only: nothing to
install, no build step, and the page loads no external asset. `--host`,
`--port`, and `--root` are all arguments; `--port 0` asks the kernel for a free
one.

A sent turn is offered to an agent through the configured launcher, and the send
returns once that turn's answer is durable. The default launcher is `dev-local`
with profile `one_shot`; choosing another is configuration only
(`launch-boundary.md`), and `tests/internal_bridge.py` models the internal
launcher's proven transport. A turn the chat's agent
cannot take is refused before it is recorded. When a restart finds an agent that
can no longer be reached, the refused send offers the one lifecycle action the
shell has: abandon that agent.

> **v0.1 limitation: a turn in flight cannot be stopped or interrupted.** Once a
> turn is sent, v0.1 lets it finish. While it is in flight, another send and the
> Abandon action on that chat are refused with nothing recorded -- not queued
> behind the turn and not deferred until it ends -- and the page says the answer
> must finish first. A Stop that could not reach a turn is never recorded or
> shown as `terminated`. A turn that never returns holds its chat until the shell
> process exits; after a restart the one action is the exit. Interrupting an
> active turn is later supervision work (#83). See decision 0003, section 5.

## Validating

The contract against its fixtures:

```
python3 dory-wrangler/validator/validate_contract.py dory-wrangler/fixtures/v0.1
```

A live store against the contract:

```
python3 dory-wrangler/validate_store.py ./dory-store
```

Exit `0` when the store satisfies contract v0.1, `1` when it does not, `2` when
it could not be read at all.

## Diagnostic evidence

Everything the integration handed the harness is preserved raw and correlated,
outside the chat (contract P4). One read-only command prints it, bounded:

```
python3 dory-wrangler/diagnostics.py STORE CHAT_ID [--session SESSION_ID]
    [--from N] [--to N] [--limit N] [--records events|lifecycle|messages]
```

`events` (the default) prints the chat's `diagnostic_event` records, optionally
one session's and a `sequence` range; `lifecycle` each session's
`agent_session`, `launch_result` and `session_observation` records; `messages`
the chat's `message` records. One stored record per line, as JSON, exactly as
stored -- bytes that were not UTF-8 stay base64 and nothing raw reaches the
terminal -- at most `--limit` of them (100 by default, 1000 at most). It derives
nothing: no counts, grouping, joins or interpretation (that is #82). It takes no
lock and writes nothing, so it runs while `run_shell.py` serves the same store.
Refusals are fixed words with exit `2`; an unreadable store exits `3`.

A worked example, for a chat whose turn carried a line that is not JSON, one of
a type the development transport does not know, and an answer:

```
$ python3 dory-wrangler/diagnostics.py ./dory-store cht_cbf52131823100ae8a470826 --limit 2
{"chat_id": "cht_cbf52131823100ae8a470826", "event_id": "evt_49baeb03dc4629d5e2998818", "interpretation": "malformed", "interpreted_type": null, "raw": {"body": "this is not json at all {{{", "encoding": "utf-8"}, "received_at": "2026-09-24T01:11:22.160853Z", "record_type": "diagnostic_event", "record_version": 1, "sequence": 1, "session_id": "ses_411c1409936c235e4d9a1e32", "source": "agent"}
{"chat_id": "cht_cbf52131823100ae8a470826", "event_id": "evt_b3872e2f6493b82fdff8dcca", "interpretation": "unrecognized", "interpreted_type": null, "raw": {"body": "{\"type\": \"agent_thinking\"}", "encoding": "utf-8"}, "received_at": "2026-09-24T01:11:22.166243Z", "record_type": "diagnostic_event", "record_version": 1, "sequence": 2, "session_id": "ses_411c1409936c235e4d9a1e32", "source": "agent"}
truncated: the bound of 2 record(s) was reached and more are preserved; the next is session ses_411c1409936c235e4d9a1e32 sequence 3; ask again with --session ses_411c1409936c235e4d9a1e32 --from 3; the sessions after it in this order are asked for the same way, by --session (--records lifecycle lists every session)
```

The truncation line is on standard error. Which event was rendered is the
`source_event_id` of an agent message in `--records messages`; where the stream
stopped is the last event's `sequence` together with the session's `state` and
observations in `--records lifecycle`. Putting those together is the reader's
work, deliberately (decision 0007).

## Testing

```
python3 dory-wrangler/tests/run_tests.py
python3 dory-wrangler/tests/test_adversarial.py --report
```

Stdlib `unittest`; no pytest and no test framework required. The runner has four
phases: the unit suite; every store the suite kept, handed to the contract
validator as a separate program; every event in those stores re-classified from
its bytes, every agent message checked against its cited event's text, every
system message checked to be the harness's fixed words in its place, and any
record on a launcher no classifier reads failing the run unless that launcher
is named with a reason (`tests/reclassify_stores.py`); and the contract's own
fixtures. The second
command prints #86's adversarial probe table: every guarantee the store claims,
the attack made on it, and what the attack found.

A green suite is not evidence that a guard is pinned. When a change adds,
removes, relocates or generalises one, check it by mutation under
`skills/adversarial-guard-verification/SKILL.md`, which carries the runner
contract, the adjudication rules, and the decision-conformance pass.

## Status

v0.1 has its contract (#85), its chat shell and durable persistence (#86), and
the launch boundary with its launchers (#87), converged onto one store and one
served application (#88, decision 0002), and intake that preserves what the
integration hands it raw and correlated before anything reads it, with the
exceptions named below (#88), and event classification from a recognized set
declared once per wire format (#88, decision 0004), and rendering of the useful
subset (#88, decision 0005), and handling of what cannot be shown -- the chat
survives every such turn and says so once, in fixed words (#88, decision 0006),
and bounded diagnostic access: one read-only command that prints preserved
evidence verbatim and derives nothing (#88, decision 0007). No observability (#82),
supervision (#83), or multi-agent (#84) behavior is in scope.

The last payload shape that was refused before it was preserved is preserved
now: a payload typed `stream_end` from a launcher declaring `response_shape: one_shot`,
where contract 7 P1 and contract 6.1's `STREAM_END_UNSUPPORTED` appeared to
disagree. **The human decided it on 2026-09-15: preserve the bytes.** The record
is written `unrecognized` with `interpreted_type: null`, and the `stream_end`
*assertion* is still refused exactly as 6.1 states it -- the turn fails and no
session concludes anything from an end of stream a one-shot launcher could not
have seen. What made it possible was one widened sentence of contract 7 P2,
corrected on `dory-wrangler/issue-85` and carried here by merge: `unrecognized`
now covers a type this build knows "but cannot attribute on this session". No
rule, error code, fixture or validator check changed with it.

It was never a loss of one payload. Refusing before preserving left that
`sequence` unwritten, and the gap made every later payload on that session
unpreservable for the session's life. Both answers stay implemented and pinned
behind `session_manager.PRESERVE_UNATTRIBUTABLE_STREAM_END`, so what was
decided, and what the other answer costs, is readable from the code.

The decision, and the per-payload containment around it, apply on **every**
channel that preserves: through `events` on the drain and on re-attachment, and
through the `launch` call itself for a launch that issued no handle and so has no
`events` call in its future. They did not at first. The no-handle channel was
added without them and lost exactly the shape the decision is about, which is
what the intake checkpoint's review found; they are one function now,
`_preserve_declining_what_it_cannot_attribute`, which every channel calls.
Which shape a session may attribute is read from the session's own recorded
`launcher_capabilities` -- the same record the store reads -- so a restart
configured with a launcher of a different shape cannot disagree with it.

**What a payload is, is decided once per wire format** (decision 0004). The
development transport that `dev-local` and `scripted-stub` share recognizes
`assistant_text` (with a string `text`) and `turn_complete`; the Codex JSONL the
internal path emits recognizes `thread.started` (with a `thread_id`) and
`item.completed` / `agent_message` (with a string `item.text`), declared in the
in-repo model until the real internal launcher (#90) carries it. Any other
well-formed line is `unrecognized`; a line that is not UTF-8 JSON, or a known
type missing its required field, is `malformed`; neither ever becomes chat. A
type is added only on the evidence of real output that carries it. `dev-local`
reads its agent's stdout as bytes and frames a line on `\n` alone, in both
profiles (`dev_transport.read_line`), so a line is classified from exactly the
bytes the agent wrote: a `\r`, trailing whitespace, U+2028 inside a string and
bytes that are not UTF-8 all reach `raw`, and the last is preserved `malformed`
without costing the rest of its turn. A line of only ASCII whitespace carries no
event and is not preserved. Every recorded reading is reproducible from the
preserved `raw.body`, and every suite run checks that over every store it keeps
(`tests/reclassify_stores.py`, phase 3 of `run_tests.py`).

**What the conversation shows is decided by one rule** (decision 0005). Each
recognized, agent-sourced event with non-empty text becomes exactly one agent
message whose text is exactly that event's text -- whitespace and newlines
included -- in event order; a turn with several such events shows several
messages and a turn with none shows no agent message. Every other event shows
nothing and is still preserved. The page inserts message text as text, never as
HTML, and keeps its whitespace. Every suite run checks every agent message in
every kept store against its cited event's bytes on every launcher a classifier
here reads, and fails on a record under any other launcher unless that launcher
is one of the three test probes named, with reasons, in
`reclassify_stores.ADJUDICATED_LAUNCHERS` -- so #90's real launcher fails the run
until it is given a classifier there.

**What cannot be shown does not break the conversation** (decision 0006). A turn
of only unrecognized or malformed events, a recognized type that carries no
text, an answer-shaped line missing its text, an empty answer, and events that
arrive after the turn ended are all preserved with the reading their bytes give,
the turn completes, and the next turn is answered. When a turn is **observed to
end** -- a one-shot call returned with its whole response, or the launcher
reported a turn end, a terminal lifecycle event or an end of stream -- and it
produced no agent message, the harness writes exactly one `system` message, in
fixed words:

> The agent's turn ended without a reply that can be shown here. Whatever it sent has been preserved.

It carries nothing from the integration, is never written because time passed
(a turn that never ends stays in flight, with no notice), is written at most once
per turn and never for a turn that showed a reply, and is not written over a
payload that was lost, because it says nothing was. Re-attachment after a
restart applies the same rule, from the chat's durable messages. The store
refuses any other system text: system text is the harness's fixed words only.

**What is not preserved, named rather than implied.**

* **A payload whose own `sequence` cannot be written** is preserved nowhere, and
  nothing durable records that it arrived, so that sequence can later be filled
  by different content. That is a payload arriving out of order, and a payload
  behind a store refusal that left a sequence unwritten. Contract P3 is why: a
  gap means a turn was lost and is never closed silently, so neither the payload
  that would write the gap nor the payloads behind it can be preserved.
* **A failed launch's output behind a payload that cannot cross the seam** -- one
  sourced to the agent, or out of order. What crosses is the longest prefix the
  seam accepts, with the launcher's own failure category, and the `detail` names
  what was not preserved; a gap cannot be left in a session that never ran. This
  holds whether the launcher raised its failure or returned it.
* **A line of only ASCII whitespace** on a newline-delimited transport is framing,
  not a payload (decision 0004).
* **What a launcher never hands the harness.** `dev-local` discards its agent's
  stderr (it is not a transport payload), and the Codex model keeps stderr only
  inside the observation it preserves for a launch that started no thread. A line
  `dev-local` has read but not yet returned from `events` is held in the launcher
  process and is gone if the harness process dies before the call returns.

Everything else the harness receives is preserved before anything interprets it,
on every channel that receives it.

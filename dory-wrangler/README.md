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
| `src/dory_wrangler/` | the one product package: the durable store (`store.py`), the chat loop (`session_manager.py`), the launch seam (`launch_boundary.py`), the launchers (`launchers/`), and the served shell (`webapp.py`) |
| `skills/adversarial-guard-verification/SKILL.md` | how this product checks that a diff's guards are really pinned: the mutation-runner contract, how to enumerate and adjudicate rows, and the decision-conformance pass |
| `run_shell.py` | run the shell |
| `validate_store.py` | check a live store against the contract |
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

## Testing

```
python3 dory-wrangler/tests/run_tests.py
python3 dory-wrangler/tests/test_adversarial.py --report
```

Stdlib `unittest`; no pytest and no test framework required. The runner has three
phases: the unit suite; every store the suite kept, handed to the contract
validator as a separate program; and the contract's own fixtures. The second
command prints #86's adversarial probe table: every guarantee the store claims,
the attack made on it, and what the attack found.

A green suite is not evidence that a guard is pinned. When a change adds,
removes, relocates or generalises one, check it by mutation under
`skills/adversarial-guard-verification/SKILL.md`, which carries the runner
contract, the adjudication rules, and the decision-conformance pass.

## Status

v0.1 has its contract (#85), its chat shell and durable persistence (#86), and
the launch boundary with its launchers (#87), converged onto one store and one
served application (#88, decision 0002), and intake that preserves every payload
raw and correlated before anything reads it, with the one exception named below
(#88), and event classification from a recognized set declared once per wire
format (#88, decision 0004). Rendering beyond agent text, malformed-event
handling, and bounded diagnostic access are #88's remaining checkpoints. No observability (#82), supervision
(#83), or multi-agent (#84) behavior is in scope.

Every payload shape is now preserved, including the last one that was not: a
payload typed `stream_end` from a launcher declaring `response_shape: one_shot`,
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

**The one exception, named rather than implied.** A payload whose own `sequence`
cannot be written is preserved nowhere, and nothing durable records that it
arrived, so that sequence can later be filled by different content. That is a
payload arriving out of order, and a payload behind a store refusal that left a
sequence unwritten. Contract P3 is why: a gap means a turn was lost and is never
closed silently, so neither the payload that would write the gap nor the payloads
behind it can be preserved. Everything else this harness receives is preserved
before anything interprets it, on every channel that receives it.

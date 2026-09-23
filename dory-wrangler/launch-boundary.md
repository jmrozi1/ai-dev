# The swappable single-agent launch boundary (#87)

Since #88's convergence (decision 0002) the seam, the chat loop and the
launchers live in the one package, `src/dory_wrangler/`, and the chat loop writes
every record through `ChatStore`. Paths below are relative to that package
unless they say otherwise.

The launch boundary is the only place that knows how an agent is started. It
exists so that development mechanics never leak into chat or session design, and
so that the internal VS Code/network bridge launcher can be dropped in later
without redesigning anything above it.

Normative source: [`../contract/v0.1/contract.md`](../contract/v0.1/contract.md),
sections 5 and 6. This code implements against that document and does not amend
it.

## Layout

| Path | What it is |
| --- | --- |
| `launch_boundary.py` | **the seam**: the contract's section 6 operations, the closed instruction packet, the declared capabilities, and the payload vocabulary. No host, transport, or bridge mechanics. |
| `session_manager.py` | the chat loop: binding enforcement, the lifecycle, both continuation modes, the per-chat turn lock. Imports no launcher and writes nothing itself. |
| `store.py` | `ChatStore`, #86's durable store and the only writer of every record. Imports no launcher. |
| `ids.py`, `errors.py` | opaque ids and timestamps; stated refusals. |
| `wiring.py` | `open_harness(config, root)` is the only place a store and a launcher meet. |
| `launchers/dev_local.py` | the external Linux development launcher. Real operating-system processes, two profiles. |
| `launchers/dev_agent.py` | the local development agent program `dev_local` starts. Not part of the boundary. |
| `launchers/scripted_stub.py` | a second, non-production launcher, selected only by configuration. |
| `launchers/registry.py` | configuration to launcher. Adding the internal bridge is one module and one entry here. |
| `dory-wrangler/tests/internal_bridge.py` | **test material, not a launcher this product ships**: a model of the internal launcher over the JSONL transport proven internally, with `model_launch_agent.py` standing in for `launch_agent.sh`. Registered nowhere and imported by no product module. |
| `dory-wrangler/tests/` | the one suite, including `test_launch_adversarial.py` and `test_internal_bridge.py`. |

## Running it

```
python3 dory-wrangler/tests/run_tests.py
```

Python 3 standard library only; no test framework beyond `unittest`. Three
phases: the unit suite; every store the suite produced handed to
`dory-wrangler/validator/validate_contract.py` on the command line; and the
contract's own fixtures. The fourth phase this file used to describe held the
accurate stores that the old `TURN_INSTRUCTION_MISSING` rule rejected; #85
corrected that rule at `4ff8b63`, so those stores are ordinary phase-two stores
now and live in `tests/test_turn_floor_regression.py`.

## Choosing a launcher

```python
from dory_wrangler.wiring import open_harness

harness = open_harness({"launcher": "dev-local", "options": {"profile": "one_shot"}}, root)
harness = open_harness({"launcher": "dev-local", "options": {"profile": "persistent"}}, root)
harness = open_harness({"launcher": "scripted-stub"}, root)
```

The served shell takes the same choice as `run_shell.py --launcher <id>
--launcher-options <json>`.

That dictionary is the whole selection. No chat, session, store, or transcript
code changes, and `open_harness(..., launcher=...)` takes an implementation this
repository has never seen.

| Launcher | `continuation` | `response_shape` | `instruction_bound_bytes` |
| --- | --- | --- | --- |
| `dev-local`, profile `one_shot` | `fresh_binding` | `one_shot` | `null` |
| `dev-local`, profile `persistent` | `persistent` | `stream` | `null` |
| `scripted-stub` | either, as configured | either, as configured | `null` by default |

The `one_shot` profile is the default because it is the shape of the one
internal path that is proven: `~/scripts/launch_agent.sh` takes instruction text
and returns the agent's response (facts-and-assumptions F10). Persistent
multi-turn delivery is unproven internally (U1); `dev-local`'s persistent profile
declares it because *that launcher* can genuinely do it, which is not a claim
about the bridge.

`instruction_bound_bytes` is `null` everywhere. No bound has been measured, here
or internally. The enforcement mechanism is live and tested; the value is #90's
to supply (U2).

## The handle is the address

`stop`, `events` and `deliver` each take the `agent_handle` the launcher returned
from its own `launch`, and nothing else on the seam names an agent. `session_id`
is the harness's identity for a run: it travels inside the instruction packets as
correlation and is never an address.

**A launcher is therefore required to remember nothing between calls** -- no
session-to-agent mapping, no durable state, no live process surviving the call,
no memory of having been called before. A one-shot script, started afresh for
every operation and gone when it returns, is a first-class implementation of this
boundary rather than a degraded one, and that is what makes contract 5.4
achievable: after a restart the harness holds the handle and nothing else, and
that has to be enough.

The harness enforces its own half. It refuses to issue an addressing operation
for a session that carries no handle, and the store refuses to write a record
claiming one happened unless the launcher's accepted `launch_result` issued one
-- a session whose launch outcome was `unknown` never received one, and contract
6.1 forbids an `unknown` outcome from carrying one at all. `reattach_failed` is
the one observation kind exempt from that, because the attempt fails without
being made. The user is not stuck: `abandon` is contract 5.4's exit from exactly
that state and needs no handle.

`launching` is a separate question and not a fact about the handle. In #87's own
store the handle and the `running` transition were two durable writes and a
process could die between them; `ChatStore` makes them one, so the shape a crash
now leaves is `launching` with an accepted `launch_result` and no handle on the
session, and #87's older shape still resolves. At restart the harness finishes
the interrupted transition from the
launcher's own `launch_result`, which contract 5.2 names as the precondition for
every exit out of `launching` but one, and falls back to `unknown` evidenced by a
failed re-attachment when no such record survives. Every shape therefore leaves
`launching`, which is the point: a session that no branch transitions is a chat
with no exit at all.

## What is deliberately absent

No `status`, `health`, `poll`, or `describe` operation. No timer, inactivity
inference, liveness interrogation, stall detection, retry, or automatic
recovery. Nothing here computes an elapsed duration or compares one against a
threshold. An agent that is merely quiet is unresolvable in v0.1 by design; that
is #83's gap and must not be closed here with a timeout.

Packet composition under `fresh_binding` is deliberately just the user's turn.
Carrying the prior chat by default would make an unmeasured payload bound part of
the architecture; it is an early-dogfood question (U1) and `compose_launch_instruction`
in `session_manager.py` is the one function a later answer changes.

## Adding the internal bridge launcher

1. A new module under `launchers/`, implementing `LaunchBoundary`.
2. It declares its own `continuation` and `response_shape`, and `null` for
   `instruction_bound_bytes` until #90 measures one.
3. It maps its transport onto the payload vocabulary in `launch_boundary.py`,
   reporting anything it does not know as `unrecognized` or `malformed`.
4. It maps its own failure modes onto the five abstract categories. An inactive
   user session and an uninitialised bridge are `unavailable`; the specifics stay
   inside the launcher and travel only in `detail`, which is never parsed.
   `detail` is rendered and never parsed, so passing it an exception object
   rather than `str(exc)` is coerced rather than refused.
5. **It takes the `agent_handle` from its transport, and synthesises one only if
   the transport issues none.** Internally the transport issues one: the resume
   ID is `thread.started.thread_id` in `codex exec --json` output, so the
   internal launcher takes it from that output and makes nothing up. Handles are
   opaque either way, and `LaunchResult` refuses `accepted` without one.
6. **A synthesised handle must be unique across the launcher's own process
   lifetimes, not merely within one.** A launcher remembers nothing between
   calls, so every operation after `launch` is served by a process that may be
   new; the harness holds the handles the previous process issued and hands them
   straight back to it. A generator whose state restarts with the process --- an
   instance counter, a per-run sequence --- therefore re-issues a live chat's
   address to a different agent, and one chat's `events`, `stop` and `deliver`
   reach another chat's agent. Nothing above the seam can catch this: a handle is
   opaque to the harness and to the contract validator alike, so a store in which
   two live sessions share one address validates. Draw the handle from something
   that does not restart with the process --- a UUID, or an identifier the
   transport itself guarantees unique --- and never from a counter the
   constructor initialises. A handle the transport issues, like Codex's
   `thread_id`, has whatever uniqueness the transport gives it, which internally
   is unproven.
7. **It keeps each call's raw output until `events` has served it.** `launch`
   and `deliver` return an outcome and an acknowledgement, not text, and
   `events` must serve every payload from `sequence` 1 for the life of the
   session, across a restart of the harness. A script that returns its output
   once and exits therefore needs somewhere of the launcher's own to keep it,
   keyed by the handle. Contract 6.1 permits that -- what it forbids is the
   *harness* resting on it, and the harness does not: probed by deleting the
   model's spool between a turn and a restart, every preserved event and the
   whole transcript are byte-identical afterwards, the session reaches `unknown`
   through a recorded `reattach_failed`, and the user's one action reopens the
   chat. What does not survive is the launcher's ability to address its own
   thread again, so the next turn gets a new agent. **How long a launcher must be
   able to do that is unspecified in v0.1** and is recorded here as a launcher
   property rather than a harness one (re-review N3).
8. **It classifies launch and deliver output through one path.** Internally both
   are the same JSONL; there is no plain-text resume case. Only event types a
   capture has shown are `recognized` (`thread.started`, and `item.completed`
   with `item.type == "agent_message"` as `assistant_text` from `item.text`);
   every other well-formed line is `unrecognized` and every other line
   `malformed`. The set is data in one place, read by the one classifier
   (decision 0004); the in-repo model declares it in
   `tests/internal_bridge.py`, and the real launcher should carry that
   declaration rather than a second copy of it. **It frames the output as
   bytes, split on `\n` only, before anything decodes it** -- the model's
   `output_lines`, and `dev-local`'s `dev_transport.read_line`. A text-mode pipe
   or `str.splitlines` splits a line on U+2028, U+0085 and others, drops `\r`,
   and turns a line that is not UTF-8 into an exception that loses the whole
   turn instead of one `malformed` record.
9. **Under `fresh_binding` it reports the agent's exit.** A session that never
   leaves `running` holds its chat, and under `fresh_binding` every later turn
   is then refused until the user abandons it. Under `persistent` a session
   that stays `running` between turns is the normal case, not a defect.
10. **It honours `after_sequence`.** `events` is resumable by sequence; a page
    that does not advance is refused with a stated `LaunchBoundaryError` rather
    than read again.
11. **It carries back the output of a launch that issued no handle.** `events`
    addresses an agent through the handle and nothing else, so a launch that
    started no thread has no later call through which anything it printed could
    be read -- and contract 7 P1 requires it preserved regardless. `LaunchResult`
    and `LauncherError` therefore take an optional `payloads`, and the harness
    preserves them against the session that failed to open, which is the reading
    contract 6.1 states under *Known residual* and is what fixtures
    `valid/05-launch-failure` and `valid/09-launch-outcome-unknown` already are.
    Three rules, each one the contract already states: an **accepted** launch may
    not use it, because `events` is its channel and two channels would preserve
    the same payloads twice; every payload is **launcher-sourced**, because
    nothing proved an agent exists and contract 7 P2a refuses agent-sourced
    evidence on a session that never ran; and sequences are **1..N in order**,
    because this is the whole history of a session that never ran. A launcher
    that breaks them keeps its failure category -- #90 counts those -- and the
    refusal is written into the durable `launch_result.detail`.
12. One entry in `launchers/registry.py`.

Nothing else changes. `tests/test_swappability.py` runs the full chat loop
against a launcher defined in the test file and registered nowhere, and
`tests/internal_bridge.py` models the internal launcher over the transport
proven internally -- `launch_agent.sh "<message>"` and
`launch_agent.sh --resumeID=<thread_id> "<message>"` over `codex exec --json`,
both emitting the same JSONL; `continuation: persistent`; `response_shape:
one_shot`, provisional until a real capture; no measured bound -- with
`tests/model_launch_agent.py` standing in for the script as a real process per
call. `tests/test_internal_bridge.py` runs it through the whole chat loop and
the served shell: several turns resuming one thread, a real restart of the
shell resuming the same `thread_id`, fresh binding still supported, every line
of output preserved verbatim, and contract 6.1's four operations sufficient,
with nothing in `src/` changed for it. Start there rather than from this list
alone.

What that model also establishes, as product properties rather than defects:
the user's Stop cannot reach a turn in flight, because `launch` and `deliver`
block for the agent's whole turn under the chat's turn lock; no stop operation
has been shown for the script, so a stop is recorded unconfirmed and the one
lifecycle action, `abandon`, is the exit; a dead bridge is discoverable only by
attempting a turn; and a restart that finds no output of the thread ends in
`unknown`, whose exit is the same action. Each has a named test in
`tests/test_internal_bridge.py`. Stop reaching a turn in flight remains the
human's decision.

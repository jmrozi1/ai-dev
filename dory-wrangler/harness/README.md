# The swappable single-agent launch boundary (#87)

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
| `session_manager.py` | the chat loop: binding enforcement, the lifecycle, both continuation modes. Imports no launcher. |
| `store.py` | the durable records and the section 8 queries. Imports no launcher. |
| `identity.py`, `errors.py` | opaque ids and timestamps; stated refusals. |
| `app.py` | wiring. `open_harness(config)` is the only place a store and a launcher meet. |
| `launchers/dev_local.py` | the external Linux development launcher. Real operating-system processes, two profiles. |
| `launchers/dev_agent.py` | the local development agent program `dev_local` starts. Not part of the boundary. |
| `launchers/scripted_stub.py` | a second, non-production launcher, selected only by configuration. |
| `launchers/registry.py` | configuration to launcher. Adding the internal bridge is one module and one entry here. |
| `tests/internal_bridge.py` | **test material, not a launcher this product ships**: a faithful model of the proven internal path, written against the seam alone. Registered nowhere and imported by no product module. |
| `tests/` | the suite, including `test_adversarial.py` and `test_internal_bridge.py`. |

## Running it

```
python3 dory-wrangler/harness/tests/run_tests.py
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
from app import open_harness

harness = open_harness({"launcher": "dev-local", "options": {"profile": "one_shot"}})
harness = open_harness({"launcher": "dev-local", "options": {"profile": "persistent"}})
harness = open_harness({"launcher": "scripted-stub"})
```

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

The harness enforces its own half. It refuses to issue an addressing operation,
or to write a record claiming one happened, for a session that carries no handle
-- a session whose launch outcome was `unknown` never received one, and contract
6.1 forbids an `unknown` outcome from carrying one at all. `reattach_failed` is
the one observation kind exempt from that, because the attempt fails without
being made. The user is not stuck: `abandon` is contract 5.4's exit from exactly
that state and needs no handle.

`launching` is a separate question and not a fact about the handle, because the
handle and the `running` transition are two durable writes and a process can die
between them. At restart the harness finishes the interrupted transition from the
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
5. **It synthesises an `agent_handle` if its transport issues none.** Handles are
   opaque, so this is legal, and `LaunchResult` refuses `accepted` without one.
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
   constructor initialises. `tests/internal_bridge.py` models this and
   `AHandleMustBeUniqueAcrossRestartsNotOnlyWithinOne` holds it to it.
7. **It buffers the response and serves it through `events`.** On a one-shot path
   `launch` returns an outcome, not text, so the response cannot travel back
   through the call that obtained it.
8. **It emits a launcher-sourced `session_completed` or `session_failed` after
   every response.** A bridge that reports the agent's text and nothing about its
   exit leaves the session `running` forever, and every later turn is refused
   until the user presses Stop. This is the difference between a working chat and
   one that needs a manual Stop between every turn, and it is the obligation
   easiest to miss.
9. **It honours `after_sequence`.** `events` is resumable by sequence; a page
   that does not advance is refused with a stated `LaunchBoundaryError` rather
   than read again. Whether the internal bridge can resume is recorded as unknown
   (C5), so this is the likeliest place an internal launcher first meets the seam.
10. One entry in `launchers/registry.py`.

Nothing else changes. `tests/test_swappability.py` runs the full chat loop
against a launcher defined in the test file and registered nowhere, and
`tests/internal_bridge.py` is a faithful model of the proven internal path --
one-shot, a blocking `launch` that returns the response with it, no issued
handle, no state across a restart, no measured bound -- run through the whole
chat loop by `tests/test_internal_bridge.py`. Three turns on it produce the same
transcript as every other launcher and a store the contract validator accepts,
and contract 6.1's four operations were sufficient: no operation, field, or
capability was added for it. Start there rather than from this list alone.

What that model also establishes, as product properties rather than defects:
the user's Stop cannot reach a turn in flight, so contract 5.2's
`running -> terminated` is unreachable on the one-shot shape; the agent has no
memory of the chat; a dead bridge is discoverable only by attempting a turn; and
a restart with a live session ends in `unknown`, whose only exit is the user's
`abandon`. Each has a named test in `tests/test_internal_bridge.py`, and each is
an affordance decision for #86 and #88 rather than something to fix here.

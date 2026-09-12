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
| `tests/` | the suite, including `test_adversarial.py`. |

## Running it

```
python3 dory-wrangler/harness/tests/run_tests.py
```

Python 3 standard library only; no test framework beyond `unittest`. Four phases:
the unit suite; every store the suite produced handed to
`dory-wrangler/validator/validate_contract.py` on the command line; the
contract's own fixtures; and the accurate stores that the known-defective
`TURN_INSTRUCTION_MISSING` rule currently rejects, reported rather than enforced.

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
5. One entry in `launchers/registry.py`.

Nothing else changes. `tests/test_swappability.py` runs the full chat loop
against a launcher defined in the test file and registered nowhere, which is that
claim in executable form.

# Dory-wrangler

Dory-wrangler is an independent product. It lives in the `ai-dev` repository
because that repository is already mirrored onto the internal network nightly,
not because it continues the frozen AI Dev product. Nothing outside this
directory is a Dory-wrangler product path, and Coxswain does not depend on
anything here.

Release intent and the checkpoint roadmap are `jmrozi1/ai-dev` #81.

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
| `decisions/0003-concurrent-turns-and-abandon.md` | a concurrent turn is refused before it is recorded (and how to flip that), and Abandon as the one lifecycle action |
| `src/dory_wrangler/` | the one product package: the durable store (`store.py`), the chat loop (`session_manager.py`), the launch seam (`launch_boundary.py`), the launchers (`launchers/`), and the served shell (`webapp.py`) |
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

## Status

v0.1 has its contract (#85), its chat shell and durable persistence (#86), and
the launch boundary with its launchers (#87), converged onto one store and one
served application (#88, decision 0002). Event classification, rendering beyond
agent text, and bounded diagnostic access are #88's remaining checkpoints. No
observability (#82), supervision (#83), or multi-agent (#84) behavior is in
scope.

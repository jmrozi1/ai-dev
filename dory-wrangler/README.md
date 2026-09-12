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
| `decisions/0001-runtime-and-storage.md` | the recorded runtime and storage choice, and its Rocky Linux 9 risks as claims to settle |
| `src/dory_wrangler/` | the chat shell and its durable store |
| `run_shell.py` | run the shell |
| `validate_store.py` | check a live store against the contract |
| `tests/` | the test suite, including the adversarial probes |

## Running the shell

```
python3 dory-wrangler/run_shell.py --root ./dory-store
```

Then open the printed `http://127.0.0.1:8765`. Standard library only: nothing to
install, no build step, and the page loads no external asset. `--host`,
`--port`, and `--root` are all arguments; `--port 0` asks the kernel for a free
one.

v0.1 starts no agent. The shell owns the conversation list, the active
conversation, the new-chat flow, and the durable history; the launch boundary is
#87 and event rendering is #88.

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

Stdlib `unittest`; no pytest and no test framework required. The second command
prints the adversarial probe table: every guarantee this code claims, the attack
made on it, and what the attack found.

## Status

v0.1 has its contract (#85) and its chat shell and durable persistence (#86). It
does not implement the launcher (#87) or the event pipeline (#88). No
observability (#82), supervision (#83), or multi-agent (#84) behavior is in
scope.

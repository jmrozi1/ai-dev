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
| `harness/` | the swappable single-agent launch boundary (#87), its two launcher implementations, and its tests |

## Validating

```
python3 dory-wrangler/validator/validate_contract.py dory-wrangler/fixtures/v0.1
```

Stdlib only, no test framework required. Exit `0` when every fixture matched its
declared expectation, `1` when any did not, `2` on a usage or input error.

## Status

v0.1 defines the contract (#85) and the launch boundary with its launchers
(#87). It does not implement the chat UI (#86) or the event pipeline (#88). No observability (#82), supervision
(#83), or multi-agent (#84) behavior is in scope.

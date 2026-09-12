# Contract validator

`validate_contract.py` is the executable form of
[`../contract/v0.1/contract.md`](../contract/v0.1/contract.md). The prose
document is normative; this tool exists so that "the contract holds" is a
command with an exit status rather than an opinion.

```
python3 dory-wrangler/validator/validate_contract.py <path> [<path> ...]
```

A path may be a fixture file or a directory, which is walked for `*.json`.

## What it checks

Each fixture is a store snapshot that declares its own expectation:

```json
{
  "fixture": {"name": "...", "expect": "accept" | "reject", "reason_code": "..."},
  "contract_version": "0.1",
  "records": [ ... ]
}
```

The validator checks every record in isolation (type, version, fields, id
format, timestamps, enumerations, no unrecognized fields) and then every
cross-record invariant: referential integrity; message, event, and delivery
sequencing; one agent per chat, enforced over non-terminal sessions and their
bindings rather than over bindings alone; transition replay with per-transition
owners **and executable preconditions**, where each transition's `evidence.ref`
must resolve to a real record in the state the precondition requires; message
provenance; launch and delivery packet portability and declared bounds; launch
result consistency; declared launcher capabilities; and a durable instruction
record for every answered user turn.

Two rules are worth calling out because they are what the executable form is
for:

- A precondition stated only in prose is skippable by omitting the record that
  would have been checked. Every row of contract 5.2 is enforced here, and
  `TRANSITION_PRECONDITIONS` is asserted at import to cover exactly the same
  transitions as `AUTHORIZED_TRANSITIONS`, so the two halves of that table
  cannot drift apart silently.
- This validator enforces **no instruction-payload bound of its own**. The bound
  is a measured property a launcher declares per session; `null` means nobody
  has measured one, and nothing fires.

A `reject` fixture passes only if the specific `reason_code` it names appears
among the violations actually emitted. A store that is rejected for an unrelated
reason is reported as a mismatch, so a fixture cannot pass by accident.

## Exit status

| Code | Meaning |
| --- | --- |
| 0 | every fixture matched its declared expectation |
| 1 | at least one fixture did not |
| 2 | usage or input error: unreadable path, undecodable JSON, missing `fixture.expect`, or no fixtures found |

## Properties

Deterministic: violations are emitted in a stable sorted order, directories are
walked in sorted order, and the tool reads nothing but the files named on the
command line. No clock, environment, or network input affects the result.

Dependency-free: Python 3 standard library only, verified on Python 3.9.

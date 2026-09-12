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
cross-record invariant (referential integrity, message and event sequencing,
binding cardinality and release, transition replay with per-transition owners,
message provenance, launch packet bounds and portability, launch result
consistency).

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

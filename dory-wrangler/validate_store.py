#!/usr/bin/env python3
"""Check a live Dory-wrangler store against the v0.1 contract.

    python3 dory-wrangler/validate_store.py <store-root> [--snapshot out.json]

Exports every durable record the store holds and runs the executable contract
over it -- the same function the fixture validator calls. `--snapshot` also
writes the export as a contract fixture document, which is what to attach when
reporting a store that failed.

Written for #89 and #90: "does the real store satisfy the contract" should be a
command with an exit status on the machine where the answer matters, not a claim
made on a different machine.

Exit status:
    0  the store satisfies the contract
    1  it does not; the violations are printed
    2  the store could not be read at all (fail-closed; see contract D3)
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from dory_wrangler.errors import StoreError  # noqa: E402
from dory_wrangler.store import ChatStore  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", help="the store directory")
    parser.add_argument("--snapshot", default=None,
                        help="also write the export as a contract fixture document")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.root):
        sys.stderr.write("error: %s is not a directory\n" % args.root)
        return 2

    store = ChatStore(args.root, sweep=False)
    try:
        records = store.export_records()
        violations = store.verify()
    except StoreError as exc:
        sys.stderr.write("error: the store could not be read: %s\n" % exc)
        return 2

    counts = {}
    for record in records:
        counts[record["record_type"]] = counts.get(record["record_type"], 0) + 1

    if args.snapshot:
        with open(args.snapshot, "w") as handle:
            json.dump(store.snapshot(name=os.path.basename(os.path.abspath(args.root))),
                      handle, indent=2, sort_keys=True)
            handle.write("\n")
        print("snapshot written to %s" % args.snapshot)

    print("%d record(s): %s" % (
        len(records),
        ", ".join("%s %d" % (k, counts[k]) for k in sorted(counts)) or "none",
    ))
    if not violations:
        print("the store satisfies contract v0.1")
        return 0
    print("%d violation(s):" % len(violations))
    for code, where, detail in violations:
        print("  %s at %s: %s" % (code, where, detail))
    return 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Run the #87 suite from a clean checkout, then validate every store it produced.

    python3 dory-wrangler/harness/tests/run_tests.py

Three phases, and the second is the point. Phase one runs the unit suite. Phase
two hands every store the suite produced to
`dory-wrangler/validator/validate_contract.py` on the command line -- the
executable form of the contract, run as a separate program over files on disk,
rather than a library call inside the tests that produced them.

Stdlib only; no test framework beyond `unittest`. Exit 0 when both phases pass.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.dirname(HERE)
DORY = os.path.dirname(HARNESS)
REPO = os.path.dirname(DORY)

for path in (HARNESS, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

import support  # noqa: E402


def main(argv):
    verbosity = 2 if "-v" in argv[1:] else 1
    shutil.rmtree(support.FIXTURE_OUT, ignore_errors=True)

    print("== phase 1: unit suite ==", flush=True)
    loader = unittest.TestLoader()
    suite = loader.discover(HERE, pattern="test_*.py", top_level_dir=HERE)
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    if not result.wasSuccessful():
        return 1

    print("", flush=True)
    print("== phase 2: every produced store against the contract validator ==",
          flush=True)
    if not os.path.isdir(support.FIXTURE_OUT):
        print("no stores were produced", flush=True)
        return 1
    stores = sorted(name for name in os.listdir(support.FIXTURE_OUT)
                    if name.endswith(".json"))
    print("%d store(s) in %s" % (len(stores), support.FIXTURE_OUT), flush=True)
    completed = subprocess.call(
        [sys.executable, support.VALIDATOR_PATH, support.FIXTURE_OUT], cwd=REPO)
    if completed != 0:
        return completed

    print("", flush=True)
    print("== phase 3: the contract's own fixtures still pass ==", flush=True)
    contract_fixtures = subprocess.call(
        [sys.executable, support.VALIDATOR_PATH,
         os.path.join(DORY, "fixtures", "v0.1")], cwd=REPO)
    if contract_fixtures != 0:
        return contract_fixtures

    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv))

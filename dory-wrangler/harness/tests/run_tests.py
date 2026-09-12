#!/usr/bin/env python3
"""Run the #87 suite from a clean checkout, then validate every store it produced.

    python3 dory-wrangler/harness/tests/run_tests.py

Two phases, and the second is the point. Phase one runs the unit suite. Phase two
hands every store the suite produced to
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
    shutil.rmtree(support.DIVERGENCE_OUT, ignore_errors=True)

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

    print("", flush=True)
    print("== phase 4: accurate stores the known-defective turn-floor rule rejects ==",
          flush=True)
    print("These are reported, not enforced. They are accurate records of what "
          "happened,\nrejected by contract 6.4's TURN_INSTRUCTION_MISSING, which "
          "counts user turns\npreceding the last agent message rather than turns an "
          "agent answered. The fix\nbelongs to #85; these are real cases to validate "
          "it against.", flush=True)
    if not os.path.isdir(support.DIVERGENCE_OUT):
        print("none produced", flush=True)
        return 0
    print("%d store(s) in %s" % (
        len([n for n in os.listdir(support.DIVERGENCE_OUT) if n.endswith(".json")]),
        support.DIVERGENCE_OUT), flush=True)
    divergent = subprocess.call(
        [sys.executable, support.VALIDATOR_PATH, support.DIVERGENCE_OUT], cwd=REPO)
    if divergent == 0:
        print("", flush=True)
        print("NOTE: these now validate. The turn-floor rule appears to have been "
              "fixed;\nfold them into phase 2 and delete "
              "tests/test_known_contract_divergence.py.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

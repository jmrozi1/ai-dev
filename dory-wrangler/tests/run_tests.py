#!/usr/bin/env python3
"""Run every Dory-wrangler test from a clean checkout.

    python3 dory-wrangler/tests/run_tests.py [-v] [pattern]

Standard library only: this VM has Python 3.9 and no pytest, and the internal
target is the same platform interpreter, so a test suite that needs a framework
is a test suite that may not run where it matters.

Four phases, and the second is the point of having phases at all:

1. the unit suite;
2. every store the suite kept (`support.StoreCheck.keep`) handed to
   `dory-wrangler/validator/validate_contract.py` on the command line -- the
   executable contract run as a separate program over files on disk, rather than
   a library call inside the tests that produced them;
3. every event in those same kept stores re-classified from its `raw.body`
   (`tests/reclassify_stores.py`), which exits non-zero on any record that does
   not reproduce and is not adjudicated by name;
4. the contract's own fixtures, which must still pass unchanged.

With a `pattern`, only the matching test modules run, and phase 2 validates the
stores those modules kept. Exit 0 only when every phase that ran passed.
"""

import os
import shutil
import subprocess
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PRODUCT_DIR = os.path.dirname(TESTS_DIR)
SRC_DIR = os.path.join(PRODUCT_DIR, "src")
REPO = os.path.dirname(PRODUCT_DIR)

for path in (TESTS_DIR, SRC_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

import support  # noqa: E402


def main(argv):
    verbosity = 2 if "-v" in argv else 1
    pattern = None
    for arg in argv:
        if not arg.startswith("-"):
            pattern = arg
    shutil.rmtree(support.FIXTURE_OUT, ignore_errors=True)
    shutil.rmtree(support.SCRATCH, ignore_errors=True)

    print("== phase 1: unit suite ==", flush=True)
    suite = unittest.TestLoader().discover(
        TESTS_DIR, pattern=pattern or "test_*.py", top_level_dir=TESTS_DIR
    )
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    if not result.wasSuccessful():
        return 1

    print("", flush=True)
    print("== phase 2: every kept store against the contract validator ==", flush=True)
    stores = []
    if os.path.isdir(support.FIXTURE_OUT):
        stores = sorted(n for n in os.listdir(support.FIXTURE_OUT) if n.endswith(".json"))
    print("%d store(s) in %s" % (len(stores), support.FIXTURE_OUT), flush=True)
    if not stores:
        if pattern is None:
            print("the full suite kept no store, which means phase 2 checked nothing",
                  flush=True)
            return 1
    else:
        completed = subprocess.call(
            [sys.executable, support.VALIDATOR_PATH, support.FIXTURE_OUT], cwd=REPO)
        if completed != 0:
            return completed

        print("", flush=True)
        print("== phase 3: every kept event re-classified from its bytes ==", flush=True)
        completed = subprocess.call(
            [sys.executable, os.path.join(TESTS_DIR, "reclassify_stores.py"),
             support.FIXTURE_OUT], cwd=REPO)
        if completed != 0:
            return completed

    print("", flush=True)
    print("== phase 4: the contract's own fixtures still pass ==", flush=True)
    return subprocess.call(
        [sys.executable, support.VALIDATOR_PATH,
         os.path.join(PRODUCT_DIR, "fixtures", "v0.1")], cwd=REPO)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

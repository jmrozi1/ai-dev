#!/usr/bin/env python3
"""Run every Dory-wrangler test from a clean checkout.

    python3 dory-wrangler/tests/run_tests.py [-v] [pattern]

Standard library only: this VM has Python 3.9 and no pytest, and the internal
target is the same platform interpreter, so a test suite that needs a framework
is a test suite that may not run where it matters.
"""

import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(os.path.dirname(TESTS_DIR), "src")

for path in (TESTS_DIR, SRC_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)


def main(argv):
    verbosity = 2 if "-v" in argv else 1
    pattern = None
    for arg in argv:
        if not arg.startswith("-"):
            pattern = arg
    suite = unittest.TestLoader().discover(
        TESTS_DIR, pattern=pattern or "test_*.py", top_level_dir=TESTS_DIR
    )
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

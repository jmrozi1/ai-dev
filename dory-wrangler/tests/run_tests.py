#!/usr/bin/env python3
"""Run every Dory-wrangler test from a clean checkout.

    python3 dory-wrangler/tests/run_tests.py [-v] [--category NAME ...]
                                             [--json PATH] [pattern]

Standard library only: this VM has Python 3.9 and no pytest, and the internal
target is the same platform interpreter, so a test suite that needs a framework
is a test suite that may not run where it matters.

Every test belongs to exactly one category (`tests/categories.py`, which states
each category's criterion): `portable`, `launch-boundary` or
`development-environment`. A failure's category is what tells an internal run
whether it may be environmental.

Five phases, and the third is the point of having phases at all:

0. the category guard: every test the suite discovers is assigned exactly one
   category, and every assignment names a test that exists -- any test
   unassigned fails the run before anything else runs;
1. the unit suite -- every test, or with `--category` only that category's --
   reported per category at the end;
2. every store the tests kept (`support.StoreCheck.keep`) handed to
   `dory-wrangler/validator/validate_contract.py` on the command line -- the
   executable contract run as a separate program over files on disk, rather than
   a library call inside the tests that produced them;
3. every event in those same kept stores re-classified from its `raw.body`, and
   every agent message checked to carry exactly its cited event's text
   (`tests/reclassify_stores.py`, decision 0005), which exits non-zero on any
   record that does not reproduce and is not adjudicated by name;
4. the contract's own fixtures, which must still pass unchanged.

`--category NAME` (repeatable) runs only the tests of that category; phases 2
and 3 then check the stores those tests kept. With a `pattern`, only the
matching test modules run, and phase 2 validates the stores those modules kept.
Without either, the run is the whole suite, exactly as it was before categories
existed. `--json PATH` also writes the run's result there, per category and per
phase, whatever the outcome. Exit 0 only when every phase that ran passed.
"""

import json
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

import categories  # noqa: E402
import support  # noqa: E402

USAGE = ("usage: run_tests.py [-v] [--category {%s} ...] [--json PATH] [pattern]"
         % ",".join(categories.CATEGORIES))

RANK = {"passed": 0, "skipped": 1, "failed": 2, "error": 3}


class CategoryResult(unittest.TextTestResult):
    """A text result that also records every test's outcome, by test.

    A subtest's failure, error or skip is its test's, and an error in a class or
    module fixture is recorded against that class or module.
    """

    def __init__(self, *args, **kwargs):
        unittest.TextTestResult.__init__(self, *args, **kwargs)
        self.outcomes = {}

    def _mark(self, test, outcome):
        # A skip inside `subTest` arrives here as the `_SubTest`, whose id names
        # the subtest's parameters; it is its test's outcome, like a subtest's
        # failure, so it is keyed -- and so categorised -- by that test.
        test = getattr(test, "test_case", test)
        key = categories.test_key(test)
        if RANK[outcome] >= RANK[self.outcomes.get(key, "passed")]:
            self.outcomes[key] = outcome

    def startTest(self, test):
        unittest.TextTestResult.startTest(self, test)
        self.outcomes.setdefault(categories.test_key(test), "passed")

    def addError(self, test, err):
        unittest.TextTestResult.addError(self, test, err)
        self._mark(test, "error")

    def addFailure(self, test, err):
        unittest.TextTestResult.addFailure(self, test, err)
        self._mark(test, "failed")

    def addSkip(self, test, reason):
        unittest.TextTestResult.addSkip(self, test, reason)
        self._mark(test, "skipped")

    def addUnexpectedSuccess(self, test):
        unittest.TextTestResult.addUnexpectedSuccess(self, test)
        self._mark(test, "failed")

    def addSubTest(self, test, subtest, err):
        unittest.TextTestResult.addSubTest(self, test, subtest, err)
        if err is not None:
            self._mark(test, "failed" if issubclass(err[0], test.failureException)
                       else "error")


def per_category(outcomes):
    """{category: {"tests", "passed", "failed", "error", "skipped"}} from a
    result's outcomes. A test the table does not place -- one in a module
    outside the suite, run by a pattern -- is reported as `unassigned`."""
    report = {}
    for key in sorted(outcomes):
        name = categories.category_of(key) or "unassigned"
        row = report.setdefault(name, {"tests": 0, "passed": 0, "failed": [],
                                       "error": [], "skipped": []})
        row["tests"] += 1
        if outcomes[key] == "passed":
            row["passed"] += 1
        else:
            row[outcomes[key]].append(key)
    return report


def print_categories(report):
    print("", flush=True)
    print("== phase 1, by category ==", flush=True)
    for name in list(categories.CATEGORIES) + ["unassigned"]:
        if name not in report:
            continue
        row = report[name]
        print("%-24s %4d test(s): %4d passed, %d failed, %d error(s), %d skipped"
              % (name, row["tests"], row["passed"], len(row["failed"]),
                 len(row["error"]), len(row["skipped"])), flush=True)
        for outcome in ("failed", "error"):
            for key in row[outcome]:
                print("    %-6s %s" % (outcome.upper(), key), flush=True)


def parse(argv):
    options = {"verbosity": 1, "pattern": None, "categories": [], "json": None}
    args = list(argv)
    while args:
        arg = args.pop(0)
        if arg == "-v":
            options["verbosity"] = 2
        elif arg == "--category":
            if not args or args[0] not in categories.CATEGORIES:
                raise SystemExit(USAGE)
            options["categories"].append(args.pop(0))
        elif arg == "--json":
            if not args:
                raise SystemExit(USAGE)
            options["json"] = os.path.abspath(args.pop(0))
        elif arg.startswith("-"):
            raise SystemExit(USAGE)
        else:
            options["pattern"] = arg
    return options


def main(argv):
    options = parse(argv)
    record = {"pattern": options["pattern"],
              "categories_selected": options["categories"] or list(categories.CATEGORIES),
              "python": sys.version.split()[0], "phases": {}, "categories": {},
              "exit": None}

    def finish(code):
        record["exit"] = code
        if options["json"]:
            with open(options["json"] + ".partial", "w") as out:
                json.dump(record, out, indent=2, sort_keys=True)
                out.write("\n")
            os.replace(options["json"] + ".partial", options["json"])
        return code

    shutil.rmtree(support.FIXTURE_OUT, ignore_errors=True)
    shutil.rmtree(support.SCRATCH, ignore_errors=True)

    print("== phase 0: every test is assigned exactly one category ==", flush=True)
    guard = categories.check(unittest.TestLoader().discover(
        TESTS_DIR, pattern="test_*.py", top_level_dir=TESTS_DIR))
    for problem in guard["problems"]:
        print("  " + problem, flush=True)
    print("; ".join("%s %d" % (name, guard["counts"][name])
                    for name in categories.CATEGORIES)
          + "; %d test(s) in all" % sum(guard["counts"].values()), flush=True)
    record["phases"]["0"] = {"passed": not guard["problems"],
                             "counts": guard["counts"], "problems": guard["problems"]}
    if guard["problems"]:
        print("the suite has a test with no category, or an assignment that names "
              "no test", flush=True)
        return finish(1)

    print("", flush=True)
    print("== phase 1: unit suite%s ==" % (
        " (%s)" % ", ".join(options["categories"]) if options["categories"] else ""),
        flush=True)
    suite = unittest.TestLoader().discover(
        TESTS_DIR, pattern=options["pattern"] or "test_*.py", top_level_dir=TESTS_DIR)
    if options["categories"]:
        suite = categories.select(suite, options["categories"])
    result = unittest.TextTestRunner(verbosity=options["verbosity"],
                                     resultclass=CategoryResult).run(suite)
    record["categories"] = per_category(result.outcomes)
    print_categories(record["categories"])
    record["phases"]["1"] = {"passed": result.wasSuccessful(), "ran": result.testsRun}
    if not result.wasSuccessful():
        return finish(1)

    print("", flush=True)
    print("== phase 2: every kept store against the contract validator ==", flush=True)
    stores = []
    if os.path.isdir(support.FIXTURE_OUT):
        stores = sorted(n for n in os.listdir(support.FIXTURE_OUT) if n.endswith(".json"))
    print("%d store(s) in %s" % (len(stores), support.FIXTURE_OUT), flush=True)
    record["phases"]["2"] = {"stores": len(stores)}
    if not stores:
        if options["pattern"] is None and not options["categories"]:
            print("the full suite kept no store, which means phase 2 checked nothing",
                  flush=True)
            record["phases"]["2"]["passed"] = False
            return finish(1)
        record["phases"]["2"]["passed"] = True
    else:
        completed = subprocess.call(
            [sys.executable, support.VALIDATOR_PATH, support.FIXTURE_OUT], cwd=REPO)
        record["phases"]["2"]["passed"] = completed == 0
        if completed != 0:
            return finish(completed)

        print("", flush=True)
        print("== phase 3: every kept event re-classified from its bytes, every agent "
              "message against it ==", flush=True)
        completed = subprocess.call(
            [sys.executable, os.path.join(TESTS_DIR, "reclassify_stores.py"),
             support.FIXTURE_OUT], cwd=REPO)
        record["phases"]["3"] = {"passed": completed == 0}
        if completed != 0:
            return finish(completed)

    print("", flush=True)
    print("== phase 4: the contract's own fixtures still pass ==", flush=True)
    completed = subprocess.call(
        [sys.executable, support.VALIDATOR_PATH,
         os.path.join(PRODUCT_DIR, "fixtures", "v0.1")], cwd=REPO)
    record["phases"]["4"] = {"passed": completed == 0}
    return finish(completed)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

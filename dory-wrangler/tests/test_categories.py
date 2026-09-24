"""The category guard (`tests/categories.py`, phase 0 of `run_tests.py`).

Every test is in exactly one category, and the guard that says so is itself
held: a planted test no entry places, an entry that names nothing, and an entry
whose value is not a category each fail it, and the runner exits non-zero on
them before running anything. The categories and their criteria are stated in
`categories.py`.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import categories

HERE = os.path.dirname(os.path.abspath(__file__))


class Planted(unittest.TestCase):
    """A test in a module the table has never heard of."""

    def test_planted(self):
        pass


def suite_of(*cases):
    loader = unittest.TestLoader()
    return unittest.TestSuite(loader.loadTestsFromTestCase(case) for case in cases)


class TheGuardFailsOnWhatItMustRefuse(unittest.TestCase):

    def with_entries(self, entries):
        saved = dict(categories.ASSIGNMENTS)
        categories.ASSIGNMENTS.update(entries)
        self.addCleanup(lambda: (categories.ASSIGNMENTS.clear(),
                                 categories.ASSIGNMENTS.update(saved)))

    def test_a_planted_test_no_entry_places_is_unassigned(self):
        key = "test_categories.Planted.test_planted"
        # This module's own entry would place it; take it away for the plant.
        saved = categories.ASSIGNMENTS.pop("test_categories")
        self.addCleanup(categories.ASSIGNMENTS.__setitem__, "test_categories", saved)
        self.assertIsNone(categories.category_of(key))
        found = categories.check(suite_of(Planted))
        self.assertIn("unassigned: %s" % key, found["problems"])
        self.assertEqual(sum(found["counts"].values()), 0)

    def test_an_entry_that_names_no_test_is_refused(self):
        self.with_entries({"test_categories.NoSuchClass": categories.PORTABLE})
        found = categories.check(suite_of(Planted))
        self.assertIn("names no test: test_categories.NoSuchClass", found["problems"])

    def test_an_entry_that_is_not_a_category_is_refused(self):
        self.with_entries({"test_categories.Planted": "portable-ish"})
        found = categories.check(suite_of(Planted))
        self.assertIn("not a category: test_categories.Planted.test_planted -> "
                      "'portable-ish'", found["problems"])

    def test_the_most_specific_entry_wins(self):
        self.with_entries({"test_categories.Planted": categories.LAUNCH_BOUNDARY,
                           "test_categories.Planted.test_planted":
                               categories.DEVELOPMENT_ENVIRONMENT})
        self.assertEqual(categories.category_of("test_categories.Planted.test_planted"),
                         categories.DEVELOPMENT_ENVIRONMENT)
        del categories.ASSIGNMENTS["test_categories.Planted.test_planted"]
        self.assertEqual(categories.category_of("test_categories.Planted.test_planted"),
                         categories.LAUNCH_BOUNDARY)

    def test_a_module_that_did_not_load_is_a_problem_not_a_pass(self):
        broken = unittest.TestLoader().loadTestsFromName("test_categories_no_such_module")
        loaded = [p for p in categories.check(broken)["problems"]
                  if not p.startswith("names no test: ")]
        self.assertEqual(len(loaded), 1, loaded)
        self.assertTrue(loaded[0].startswith("could not load: "), loaded)

    def test_a_class_fixture_error_is_charged_to_its_class(self):
        holder = unittest.suite._ErrorHolder("setUpClass (test_end_to_end.EachStepFailsAtItsStep)")
        self.assertEqual(categories.test_key(holder), "test_end_to_end.EachStepFailsAtItsStep")
        self.assertEqual(categories.category_of(categories.test_key(holder)),
                         categories.DEVELOPMENT_ENVIRONMENT)

    def test_selection_keeps_only_the_named_categories(self):
        self.with_entries({"test_categories.Planted": categories.LAUNCH_BOUNDARY})
        self.assertEqual(categories.select(suite_of(Planted), [categories.PORTABLE])
                         .countTestCases(), 0)
        self.assertEqual(categories.select(suite_of(Planted), [categories.LAUNCH_BOUNDARY])
                         .countTestCases(), 1)


class TheSuiteAsItStands(unittest.TestCase):

    def test_every_test_of_the_suite_has_exactly_one_category(self):
        found = categories.check(unittest.TestLoader().discover(
            HERE, pattern="test_*.py", top_level_dir=HERE))
        self.assertEqual(found["problems"], [])
        self.assertTrue(all(found["counts"][name] > 0 for name in categories.CATEGORIES))

    def test_the_runner_refuses_an_unknown_category_and_reports_under_json(self):
        """`run_tests.py`, a separate program, over this module alone: the
        category filter runs only its category, and `--json` reports it."""
        base = tempfile.mkdtemp(prefix="dory-categories-")
        self.addCleanup(shutil.rmtree, base, True)
        env = dict(os.environ, DORY_TEST_STORE_DIR=os.path.join(base, "stores"), TMPDIR=base)
        runner = os.path.join(HERE, "run_tests.py")
        refused = subprocess.run([sys.executable, runner, "--category", "nearly-portable"],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        self.assertNotEqual(refused.returncode, 0)
        report = os.path.join(base, "run.json")
        done = subprocess.run(
            [sys.executable, runner, "--category", categories.LAUNCH_BOUNDARY,
             "--json", report, "test_categories.py"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        output = done.stdout.decode("utf-8", "replace")
        self.assertEqual(done.returncode, 0, output)
        with open(report) as handle:
            result = json.load(handle)
        # Nothing in this module is launch-boundary, so nothing ran, and the
        # guard over the whole suite still did.
        self.assertEqual(result["categories"], {})
        self.assertEqual(result["phases"]["1"]["ran"], 0)
        self.assertTrue(result["phases"]["0"]["passed"])
        self.assertEqual(result["exit"], 0)


if __name__ == "__main__":
    unittest.main()

"""D11 is observability. This is the structural proof, read from the package itself."""

from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path

from ai_dev_flow import authorization, decision_manager, manager_controller
from ai_dev_flow.decision_manager import ManagerRun
from ai_dev_flow.progress_store import ProgressStore
from ai_dev_flow.progress_view import ProgressView

PACKAGE = Path(decision_manager.__file__).parent

# The progress modules themselves.
PROGRESS_MODULES = ("progress_store", "progress_view")

# Every name the progress subsystem exports that could carry a telemetry value.
PROGRESS_TYPES = (
    "ProgressStore", "ProgressView", "ProgressFacts", "ProgressStoreError",
    "ProgressViewError", "Acceptance", "NamedCompletion", "Projection",
    "project_progress", "commit_instant",
)

# The published record's schema, which the writing side must also know. It holds
# validators and constants and no measure at all -- no store, no view, no
# percentage, no facts -- so naming it grants a module nothing to decide with.
# `test_the_schema_module_carries_no_measure` proves that rather than asserting it.
SCHEMA_MODULE = "progress_record"

# The modules a progress value is permitted to reach, and why each one is here.
#
#   decision_manager        -- composition: projects the run and hands the view on
#   decision_manager_web    -- presentation: reduces the view into the page
#   decision_manager_launch -- the entry point that resolves one run's inputs
#
# Nothing else may name it. In particular no module that authorizes, admits,
# reviews, remediates, schedules, prioritizes or dispatches appears here, and the
# test below proves that by enumeration rather than by inspection of this list.
PERMITTED_CONSUMERS = frozenset(
    {"decision_manager", "decision_manager_web", "decision_manager_launch"}
)

# Every module on a path that decides something. If any of these could name a
# progress value, telemetry would be one edit away from becoming control.
CONTROL_PATH = (
    "authorization",
    "manager_controller",
    "manager_dispatch",
    "orchestrator_invocation",
    "orchestrator_outcome",
    "orchestrator_trigger",
    "blocked_workflows",
    "session_binding",
    "session_lifecycle",
    "queue_source",
    "decision_queue",
    "attention_projection",
    "workflow_state",
    "promotion_sync",
    "control_plane",
    "claude_worker",
    "claude_runtime",
    "claude_allowance_ledger",
)


def modules():
    """Every module in the product package, by name, parsed once."""
    parsed = {}
    for path in sorted(PACKAGE.glob("*.py")):
        parsed[path.stem] = ast.parse(path.read_text(encoding="utf-8"))
    return parsed


def local_imports(tree):
    """The package-local modules one module imports, by bare name."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            if node.module:
                found.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("ai_dev_flow."):
                    found.add(alias.name.split(".")[1])
    return found


def named(tree):
    """Every identifier a module names anywhere: attributes, arguments, calls."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.arg):
            found.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.add(node.name)
        elif isinstance(node, ast.keyword) and node.arg:
            found.add(node.arg)
        elif isinstance(node, ast.alias):
            found.add((node.asname or node.name).split(".")[-1])
    return found


class ImportGraphTests(unittest.TestCase):
    """Who may name a progress value, established by enumerating the package."""

    def setUp(self) -> None:
        self.modules = modules()

    def test_the_package_is_actually_being_read(self) -> None:
        """A guard on the instrument: an empty enumeration would pass everything."""
        self.assertIn("authorization", self.modules)
        self.assertIn("progress_view", self.modules)
        self.assertGreater(len(self.modules), 25)

    def test_exactly_the_render_and_composition_path_imports_progress(self) -> None:
        importers = {
            name
            for name, tree in self.modules.items()
            if name not in PROGRESS_MODULES
            and local_imports(tree) & set(PROGRESS_MODULES)
        }
        self.assertEqual(importers, set(PERMITTED_CONSUMERS))

    def test_no_module_that_decides_anything_can_name_a_progress_value(self) -> None:
        for name in CONTROL_PATH:
            with self.subTest(module=name):
                self.assertIn(name, self.modules, name)
                tree = self.modules[name]
                self.assertEqual(
                    local_imports(tree) & set(PROGRESS_MODULES), set(), name
                )
                for symbol in PROGRESS_TYPES:
                    self.assertNotIn(symbol, named(tree), "{0}.{1}".format(name, symbol))

    def test_the_schema_module_carries_no_measure(self) -> None:
        """The one progress module a writer may import defines nothing to decide on.

        `control_plane` must know what a published progress record may say, so it
        imports the schema. That import would be a hole if the schema carried a
        measure, so this enumerates what the module actually defines: validators,
        constants and one refusal, and no store, view, percentage or fact type.
        """
        tree = self.modules[SCHEMA_MODULE]
        defined = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.ClassDef))
        }
        for symbol in PROGRESS_TYPES:
            self.assertNotIn(symbol, defined, symbol)
        self.assertEqual(
            {name for name in defined if name[:1].isupper()}, {"ProgressRecordError"}
        )
        for forbidden in ("percentage", "percent", "project_progress", "facts", "view"):
            self.assertNotIn(forbidden, {name.lower() for name in defined}, forbidden)
        self.assertEqual(local_imports(tree), set())

    def test_only_the_schema_reaches_the_writing_side(self) -> None:
        """The control plane publishes progress, so it knows the shape and no more."""
        control_plane = self.modules["control_plane"]
        imported = local_imports(control_plane)
        self.assertIn(SCHEMA_MODULE, imported)
        self.assertEqual(imported & set(PROGRESS_MODULES), set())

    def test_the_progress_subsystem_cannot_reach_any_authority_at_all(self) -> None:
        """The transitive closure of what the progress modules import.

        This is the strongest form of the claim and the reason it is worth
        stating structurally: a progress value cannot be turned into a decision
        by these modules, because the code that would make the decision is not
        reachable from them. Nothing here is a rule an editor is asked to keep --
        adding the import is what would break it.
        """
        seen = set()
        frontier = list(PROGRESS_MODULES)
        while frontier:
            current = frontier.pop()
            if current in seen:
                continue
            seen.add(current)
            frontier.extend(local_imports(self.modules[current]) - seen)
        self.assertEqual(seen, set(PROGRESS_MODULES) | {SCHEMA_MODULE})
        for name in CONTROL_PATH:
            self.assertNotIn(name, seen, name)


class NoControlSignatureTests(unittest.TestCase):
    """No decision anywhere takes a progress value, or the run that carries one."""

    def _parameters(self, function):
        return {
            name: parameter.annotation
            for name, parameter in inspect.signature(function).parameters.items()
        }

    def test_the_admission_predicate_takes_no_progress_input(self) -> None:
        for function in (authorization.authorize, authorization.reconcile_agent_slots):
            parameters = self._parameters(function)
            for name, annotation in parameters.items():
                self.assertNotIn("progress", name.lower(), name)
                self.assertNotIn("Progress", str(annotation), name)
                self.assertNotIn("ManagerRun", str(annotation), name)

    def test_the_admission_decision_carries_no_progress_field(self) -> None:
        for name in dir(authorization.AuthorizationDecision):
            self.assertNotIn("progress", name.lower(), name)

    def test_nothing_that_launches_or_dispatches_receives_the_run(self) -> None:
        """`ManagerRun` is what carries the store, so where it goes matters.

        It is a projection input, and every function that takes one draws, serves,
        or projects. The controller's dispatch and launch paths take none, so the
        value that carries progress cannot arrive at the place work is admitted.
        """
        for name in ("dispatch", "launch", "agent_count", "occupancy"):
            function = getattr(manager_controller.ManagerController, name, None)
            self.assertIsNotNone(function, name)
            for parameter, annotation in self._parameters(function).items():
                self.assertNotIn("ManagerRun", str(annotation), "{0}.{1}".format(name, parameter))
                self.assertNotIn("run", parameter.lower(), "{0}.{1}".format(name, parameter))

    def test_every_consumer_of_the_run_only_draws_serves_or_projects(self) -> None:
        allowed = {
            "queue", "queue_scope", "observe", "observed_page", "serve_observed",
            "page", "serve", "project_allowance", "project_progress",
            "render_manager_page", "make_manager_server", "make_live_manager_server",
            "make_observed_manager_server", "_checked_run", "open_surface",
            "resolve_run", "load_run_queue", "project_run_queue", "_queue_instant",
        }
        found = set()
        for path in sorted(PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                arguments = node.args
                every = list(arguments.args) + list(arguments.kwonlyargs)
                for argument in every:
                    if argument.annotation is None:
                        continue
                    if "ManagerRun" in ast.dump(argument.annotation):
                        found.add(node.name)
        self.assertTrue(found)
        self.assertEqual(found - allowed, set())

    def test_the_view_offers_nothing_to_decide_with(self) -> None:
        """No ordering, no comparison, no truthiness a threshold could use."""
        for operator in ("__lt__", "__gt__", "__le__", "__ge__", "__bool__", "__int__"):
            self.assertIs(
                getattr(ProgressView, operator, None),
                getattr(object, operator, None),
                operator,
            )

    def test_a_progress_store_is_never_a_run_input_to_anything_but_projection(self) -> None:
        fields = {field.name for field in __import__("dataclasses").fields(ManagerRun)}
        self.assertIn("progress", fields)
        self.assertEqual(
            ManagerRun.__dataclass_fields__["progress"].type, ProgressStore.__name__
        )
        # And the only thing composition does with it is project it.
        source = Path(decision_manager.__file__).read_text(encoding="utf-8")
        uses = [
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Attribute) and node.attr == "progress"
        ]
        self.assertEqual(len(uses), 1)


if __name__ == "__main__":
    unittest.main()

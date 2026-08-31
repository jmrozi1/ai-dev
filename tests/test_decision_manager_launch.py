"""`decision_manager_launch` resolves one run's inputs and adds no authority of its own.

Every assertion below is about the runtime input boundary: which accepted helper was
called, how many times, with exactly which values, and that what the caller stated
reached the accepted projection untouched. Nothing here re-checks what the estimator,
the store, the view, the queue, or the renderer already decided -- those have their own
accepted suites, and a second opinion here could only drift from them.

The five invariants this suite exists to keep are the ones a mutation could quietly
break: the exclusivity claim has no default and is never inferred from silence; an
explicitly absent claim is carried through rather than substituted; exactly one epoch
and one store are resolved per run; the surface binds loopback only; and nothing about
the claim is ever made durable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict
import ast
import contextlib
import inspect
import io
import subprocess
import tempfile
import unittest
import unittest.mock

from ai_dev_flow import decision_manager as manager
from ai_dev_flow import decision_manager_launch as launch
from ai_dev_flow.claude_allowance import WINDOW_FIVE_HOUR, WINDOW_SEVEN_DAY
from ai_dev_flow.claude_allowance_store import (
    ALLOWANCE_DIRECTORY,
    ALLOWANCE_STORE_NAME,
    AllowanceStore,
)
from ai_dev_flow.claude_allowance_view import project_window
from ai_dev_flow.decision_manager import ManagerRun
from ai_dev_flow.decision_manager_launch import (
    CLAIM_NONE_FLAG,
    CLAIM_SINCE_FLAG,
    REASON_CLAIM_AMBIGUOUS,
    REASON_CLAIM_UNSTATED,
    REASON_INVALID_CLAIM,
    REASON_NO_QUEUE_SOURCE,
    LaunchError,
    launch_manager_server,
    main,
    render_launch_page,
    resolve_run,
)
from ai_dev_flow.decision_manager_web import LOOPBACK_HOST
from ai_dev_flow.decision_queue import (
    QUEUE_STATES,
    EvidenceReference,
    PendingDecision,
    SelectedDetail,
    build_queue,
)

PROJECT = "ai-dev"
TICKET = "issue-55"

SINCE = 1_700_000_000

MODULE_SOURCE = Path(launch.__file__).read_text(encoding="utf-8")
MODULE_TREE = ast.parse(MODULE_SOURCE)


def _code_only(source: str) -> str:
    """The module's code with its prose removed, so a ban anchors on what runs.

    A module that explains in its own docstring that it reads no environment
    variable would otherwise fail a ban on `environ`, which would push the next
    author to describe the boundary less clearly in order to keep a test green.
    Comments and docstrings go; every string literal the code actually evaluates
    stays, so a literal address or a smuggled key is still visible here.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body.pop(0)
            if not body:
                body.append(ast.Pass())
    return ast.unparse(ast.fix_missing_locations(tree))


MODULE_CODE = _code_only(MODULE_SOURCE)

# The store path components the accepted helper decides. Named from the accepted
# module's own constants so this suite cannot drift into asserting a second convention.
STORE_PARTS = tuple(Path(ALLOWANCE_DIRECTORY).parts) + (ALLOWANCE_STORE_NAME,)


def a_decision(**overrides) -> PendingDecision:
    base = dict(
        decision_id="d-1", project=PROJECT, ticket=TICKET, rail="issue-55-rail-one",
        raised_at="raised-1", title="Choose the credential route",
        explanation="The requirements do not say which credential the worker uses.",
        elapsed_seconds=7200,
        evidence=(EvidenceReference(label="review", locator="rails/one/handoff.md"),),
    )
    base.update(overrides)
    return PendingDecision(**base)


def a_queue(decisions=None):
    """One accepted queue view plus every row's accepted detail."""
    queue = build_queue(list(decisions if decisions is not None else [a_decision()]), [])
    view = queue.view(filters=QUEUE_STATES)
    details: Dict[str, SelectedDetail] = {}
    for row in view.rows:
        details[row.item_id] = queue.view(
            filters=QUEUE_STATES, selected_id=row.item_id
        ).detail
    return view, details


class _ProjectionSpy:
    """Records every accepted projection call and still performs it.

    A double that returned a fabricated view would prove the call count and nothing
    about the values, so the real accepted projection still runs and its result is
    what the composition receives.
    """

    def __init__(self) -> None:
        self.calls = []

    def __call__(self, store, *, window, now, human_exclusive_since):
        self.calls.append(
            {
                "store": store,
                "window": window,
                "now": now,
                "human_exclusive_since": human_exclusive_since,
            }
        )
        return project_window(
            store, window=window, now=now, human_exclusive_since=human_exclusive_since
        )


class LaunchTestCase(unittest.TestCase):
    """Every fixture gets its own repository root; none reaches into another's."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="decision-manager-launch-"))
        self.addCleanup(self._remove_root)

    def _remove_root(self) -> None:
        for item in sorted(self.root.rglob("*"), reverse=True):
            item.unlink() if item.is_file() else item.rmdir()
        self.root.rmdir()

    # -- helpers ----------------------------------------------------------

    @contextlib.contextmanager
    def rooted(self):
        """Resolve every repository lookup to this fixture's own root."""
        with unittest.mock.patch.object(
            launch, "resolve_repo_root", lambda cwd=None: self.root
        ):
            yield

    @contextlib.contextmanager
    def counted_clock(self, reads):
        real = launch.time.time

        def counting():
            reads.append(1)
            return real()

        with unittest.mock.patch.object(launch.time, "time", counting):
            yield

    @contextlib.contextmanager
    def counted_stores(self, built):
        def spy(path):
            built.append(path)
            return AllowanceStore(path)

        with unittest.mock.patch.object(launch, "AllowanceStore", spy):
            yield

    @contextlib.contextmanager
    def spied_projection(self, spy):
        with unittest.mock.patch.object(manager, "project_window", spy):
            yield

    def tree(self) -> dict:
        """Every byte under this fixture's root, so a write cannot hide."""
        return {
            str(item.relative_to(self.root)): item.read_bytes()
            for item in sorted(self.root.rglob("*"))
            if item.is_file()
        }

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()


# --------------------------------------------------------------------------
# The claim has no default and is never inferred from silence
# --------------------------------------------------------------------------


class ClaimStatementTests(LaunchTestCase):
    def test_resolve_run_gives_the_claim_no_default(self) -> None:
        parameter = inspect.signature(resolve_run).parameters["human_exclusive_since"]
        self.assertIs(parameter.default, inspect.Parameter.empty)
        self.assertIs(parameter.kind, inspect.Parameter.KEYWORD_ONLY)

    def test_every_entry_point_gives_the_claim_no_default(self) -> None:
        for function in (resolve_run, render_launch_page, launch_manager_server):
            parameter = inspect.signature(function).parameters["human_exclusive_since"]
            with self.subTest(function=function.__name__):
                self.assertIs(parameter.default, inspect.Parameter.empty)

    def test_omitting_the_claim_is_refused_by_construction(self) -> None:
        with self.rooted():
            with self.assertRaises(TypeError):
                resolve_run()

    def test_explicit_absence_is_carried_through_untouched(self) -> None:
        with self.rooted():
            run = resolve_run(human_exclusive_since=None)
        self.assertIsNone(run.human_exclusive_since)

    def test_exact_claim_is_carried_through_untouched(self) -> None:
        with self.rooted():
            run = resolve_run(human_exclusive_since=SINCE)
        self.assertEqual(run.human_exclusive_since, SINCE)
        self.assertIs(type(run.human_exclusive_since), int)

    def test_stated_absence_reaches_both_projections_as_none(self) -> None:
        """The substitution control: `None` must arrive at the accepted view as `None`."""
        view, details = a_queue()
        spy = _ProjectionSpy()
        with self.rooted(), self.spied_projection(spy):
            render_launch_page(view, details, human_exclusive_since=None)
        self.assertEqual(len(spy.calls), 2)
        for call in spy.calls:
            self.assertIsNone(call["human_exclusive_since"])

    def test_stated_claim_reaches_both_projections_exactly(self) -> None:
        view, details = a_queue()
        spy = _ProjectionSpy()
        with self.rooted(), self.spied_projection(spy):
            render_launch_page(view, details, human_exclusive_since=SINCE)
        self.assertEqual([call["human_exclusive_since"] for call in spy.calls], [SINCE, SINCE])


class CommandLineClaimTests(LaunchTestCase):
    def test_neither_direction_stated_is_refused(self) -> None:
        with self.rooted():
            code, _, err = self.run_main([])
        self.assertEqual(code, 1)
        self.assertIn(REASON_CLAIM_UNSTATED, err)

    def test_both_directions_stated_is_refused(self) -> None:
        with self.rooted():
            code, _, err = self.run_main([CLAIM_NONE_FLAG, CLAIM_SINCE_FLAG, str(SINCE)])
        self.assertEqual(code, 1)
        self.assertIn(REASON_CLAIM_AMBIGUOUS, err)

    def test_explicit_no_claim_is_accepted_and_reported_as_none(self) -> None:
        with self.rooted():
            code, out, _ = self.run_main([CLAIM_NONE_FLAG])
        self.assertEqual(code, 2)
        self.assertIn("none claimed", out)

    def test_explicit_epoch_is_accepted_and_reported_exactly(self) -> None:
        with self.rooted():
            code, out, _ = self.run_main([CLAIM_SINCE_FLAG, str(SINCE)])
        self.assertEqual(code, 2)
        self.assertIn("since {0}".format(SINCE), out)

    def test_non_integer_claim_text_is_refused(self) -> None:
        for text in ("", " ", "later", "1.0", "1e3", "0x10", "1_700", "+7", "7 "):
            with self.subTest(text=text), self.rooted():
                code, _, err = self.run_main([CLAIM_SINCE_FLAG, text])
            self.assertEqual(code, 1)
            self.assertIn(REASON_INVALID_CLAIM, err)

    def test_the_claim_is_never_read_from_the_environment(self) -> None:
        """Silence stays silence even when a plausible variable is set."""
        with unittest.mock.patch.dict(
            "os.environ",
            {
                "HUMAN_EXCLUSIVE_SINCE": str(SINCE),
                "AI_DEV_HUMAN_EXCLUSIVE_SINCE": str(SINCE),
            },
        ):
            with self.rooted():
                code, _, err = self.run_main([])
        self.assertEqual(code, 1)
        self.assertIn(REASON_CLAIM_UNSTATED, err)

    def test_unknown_arguments_are_refused_by_the_parser(self) -> None:
        with self.rooted(), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main(["--serve-anyway"])


# --------------------------------------------------------------------------
# Exactly one epoch and one store per run
# --------------------------------------------------------------------------


class SingleResolutionTests(LaunchTestCase):
    def test_the_clock_is_read_exactly_once_per_run(self) -> None:
        reads = []
        with self.rooted(), self.counted_clock(reads):
            resolve_run(human_exclusive_since=None)
        self.assertEqual(len(reads), 1)

    def test_the_store_is_constructed_exactly_once_per_run(self) -> None:
        built = []
        with self.rooted(), self.counted_stores(built):
            resolve_run(human_exclusive_since=None)
        self.assertEqual(len(built), 1)

    def test_the_repository_is_resolved_exactly_once_per_run(self) -> None:
        resolutions = []

        def spy(cwd=None):
            resolutions.append(cwd)
            return self.root

        with unittest.mock.patch.object(launch, "resolve_repo_root", spy):
            resolve_run(human_exclusive_since=None)
        self.assertEqual(len(resolutions), 1)

    def test_a_whole_render_resolves_each_input_exactly_once(self) -> None:
        view, details = a_queue()
        reads, built = [], []
        with self.rooted(), self.counted_clock(reads), self.counted_stores(built):
            render_launch_page(view, details, human_exclusive_since=None)
        self.assertEqual((len(reads), len(built)), (1, 1))

    def test_a_whole_launch_resolves_each_input_exactly_once(self) -> None:
        view, details = a_queue()
        reads, built = [], []
        with self.rooted(), self.counted_clock(reads), self.counted_stores(built):
            server = launch_manager_server(view, details, human_exclusive_since=None)
        self.addCleanup(server.server_close)
        self.assertEqual((len(reads), len(built)), (1, 1))

    def test_both_windows_project_against_one_store_and_one_instant(self) -> None:
        """The incoherence control: no second store or later instant between windows."""
        view, details = a_queue()
        spy = _ProjectionSpy()
        with self.rooted(), self.spied_projection(spy):
            render_launch_page(view, details, human_exclusive_since=SINCE)
        self.assertEqual(len(spy.calls), 2)
        first, second = spy.calls
        self.assertIs(first["store"], second["store"])
        self.assertEqual(first["now"], second["now"])
        self.assertEqual(
            [call["window"] for call in spy.calls], [WINDOW_FIVE_HOUR, WINDOW_SEVEN_DAY]
        )

    def test_two_runs_are_two_independent_resolutions(self) -> None:
        """One run's inputs never leak into the next; there is no cache to leak through."""
        with self.rooted():
            first = resolve_run(human_exclusive_since=SINCE)
            second = resolve_run(human_exclusive_since=None)
        self.assertEqual(first.human_exclusive_since, SINCE)
        self.assertIsNone(second.human_exclusive_since)
        self.assertIsNot(first.store, second.store)

    def test_the_run_is_the_accepted_frozen_value(self) -> None:
        with self.rooted():
            run = resolve_run(human_exclusive_since=None)
        self.assertIs(type(run), ManagerRun)

    def test_the_module_reads_one_clock_and_builds_one_store_structurally(self) -> None:
        """Counted from the tree, so a second read under another spelling still counts."""
        clock_reads = [
            node
            for node in ast.walk(MODULE_TREE)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "time"
        ]
        store_builds = [
            node
            for node in ast.walk(MODULE_TREE)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "AllowanceStore"
        ]
        roots = [
            node
            for node in ast.walk(MODULE_TREE)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "resolve_repo_root"
        ]
        self.assertEqual((len(clock_reads), len(store_builds), len(roots)), (1, 1, 1))


# --------------------------------------------------------------------------
# The accepted helpers, not a second convention
# --------------------------------------------------------------------------


class AcceptedHelperTests(LaunchTestCase):
    def test_the_store_path_is_the_accepted_convention(self) -> None:
        with self.rooted():
            run = resolve_run(human_exclusive_since=None)
        self.assertEqual(run.store.path.parts[-len(STORE_PARTS):], STORE_PARTS)
        self.assertEqual(run.store.path.parents[len(STORE_PARTS) - 1], self.root)

    def test_the_repository_helper_is_the_accepted_one(self) -> None:
        sources = {
            node.module
            for node in ast.walk(MODULE_TREE)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
            if alias.name == "resolve_repo_root"
        }
        self.assertEqual(sources, {"repository"})

    def test_no_bespoke_path_walk_replaces_the_helper(self) -> None:
        for forbidden in ("parents[", "getcwd", "rglob", "__file__", "chdir", "cwd()"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, MODULE_CODE)

    def test_a_real_repository_root_resolves_end_to_end(self) -> None:
        """The accepted helper, actually run, against a real repository this test makes."""
        created = subprocess.run(
            ["git", "init", "--quiet", str(self.root)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if created.returncode != 0:  # pragma: no cover - git is present in this repo
            self.skipTest("git is unavailable")
        marker = self.root / "marker.txt"
        marker.write_text("root", encoding="utf-8")

        run = resolve_run(human_exclusive_since=None, cwd=self.root)

        self.assertEqual(run.store.path.parts[-len(STORE_PARTS):], STORE_PARTS)
        self.assertTrue((run.store.path.parents[len(STORE_PARTS) - 1] / "marker.txt").is_file())


# --------------------------------------------------------------------------
# Loopback stays the accepted server's decision
# --------------------------------------------------------------------------


class LoopbackTests(LaunchTestCase):
    def test_the_launched_server_binds_loopback(self) -> None:
        view, details = a_queue()
        with self.rooted():
            server = launch_manager_server(view, details, human_exclusive_since=None)
        self.addCleanup(server.server_close)
        self.assertEqual(server.server_address[0], LOOPBACK_HOST)

    def test_the_module_passes_no_host_at_all(self) -> None:
        """A host argument here would be a second place deciding what gets bound."""
        keywords = {
            keyword.arg
            for node in ast.walk(MODULE_TREE)
            if isinstance(node, ast.Call)
            for keyword in node.keywords
        }
        self.assertNotIn("host", keywords)

    def test_the_module_names_no_address_and_no_loopback_constant(self) -> None:
        for forbidden in ("127.0.0.1", "0.0.0.0", "::1", "localhost", "LOOPBACK_HOST"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, MODULE_CODE)

    def test_nothing_is_served_or_scheduled_by_this_module(self) -> None:
        for forbidden in (
            "serve_forever", "Thread", "Timer", "sleep", "schedule", "while True",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, MODULE_CODE)


# --------------------------------------------------------------------------
# Nothing about the claim is durable
# --------------------------------------------------------------------------


class NoDurableStateTests(LaunchTestCase):
    def test_resolving_a_run_writes_nothing(self) -> None:
        before = self.tree()
        with self.rooted():
            resolve_run(human_exclusive_since=SINCE)
        self.assertEqual(self.tree(), before)
        self.assertFalse((self.root / ALLOWANCE_DIRECTORY).exists())

    def test_rendering_a_page_writes_nothing(self) -> None:
        view, details = a_queue()
        before = self.tree()
        with self.rooted():
            render_launch_page(view, details, human_exclusive_since=SINCE)
        self.assertEqual(self.tree(), before)

    def test_the_command_line_writes_nothing(self) -> None:
        before = self.tree()
        with self.rooted():
            self.run_main([CLAIM_SINCE_FLAG, str(SINCE)])
        self.assertEqual(self.tree(), before)

    def test_the_module_opens_nothing_for_writing(self) -> None:
        for forbidden in (
            "write_text", "write_bytes", "open(", "mkdir", "json.dump", "replace(",
            "environ", "getenv", "putenv",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, MODULE_CODE)

    def test_the_module_holds_no_mutable_state(self) -> None:
        """A module-level mutable is the one place a claim could survive a call."""
        mutable = [
            name
            for name, value in vars(launch).items()
            if not name.startswith("__")
            and isinstance(value, (list, dict, set, bytearray))
        ]
        self.assertEqual(mutable, [])

    def test_a_claim_cannot_survive_into_a_later_run(self) -> None:
        """Restart requires a new statement: there is no state to carry the old one."""
        with self.rooted():
            resolve_run(human_exclusive_since=SINCE)
            with self.assertRaises(TypeError):
                resolve_run()


# --------------------------------------------------------------------------
# The queue is the caller's, and its absence is stated rather than drawn
# --------------------------------------------------------------------------


class QueueSourceTests(LaunchTestCase):
    def test_the_rendered_page_carries_the_callers_queue(self) -> None:
        view, details = a_queue([a_decision(decision_id="d-9", title="Pick the seam")])
        with self.rooted():
            page = render_launch_page(view, details, human_exclusive_since=None)
        self.assertIn("Pick the seam", page)

    def test_this_module_builds_no_queue_of_its_own(self) -> None:
        for forbidden in (
            "build_queue", "PendingDecision(", "OperationalAgent(", "DecisionQueue(",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, MODULE_CODE)

    def test_the_entry_point_refuses_to_serve_without_a_queue(self) -> None:
        """An unwired source is stated, never drawn as an empty but authoritative one."""
        with self.rooted():
            code, out, err = self.run_main([CLAIM_NONE_FLAG])
        self.assertEqual(code, 2)
        self.assertIn(REASON_NO_QUEUE_SOURCE, err)
        self.assertNotIn("queue-payload", out)

    def test_the_entry_point_still_proves_the_inputs_it_resolved(self) -> None:
        with self.rooted():
            _, out, _ = self.run_main([CLAIM_SINCE_FLAG, str(SINCE)])
        self.assertIn(ALLOWANCE_STORE_NAME, out)
        self.assertIn("run instant:", out)

    def test_no_production_builder_of_queue_items_exists_yet(self) -> None:
        """The premise of the refusal above, kept honest.

        When a durable-state-to-queue projection is finally written this test fails,
        which is exactly the moment `main` must stop refusing and start using it.
        """
        repo_root = Path(launch.__file__).parents[1]
        offenders = []
        for source in sorted(repo_root.rglob("*.py")):
            relative = source.relative_to(repo_root)
            if relative.parts[0] in ("tests", ".git"):
                continue
            text = source.read_text(encoding="utf-8", errors="replace")
            if "PendingDecision(" in text or "OperationalAgent(" in text:
                offenders.append(str(relative))
        self.assertEqual(offenders, [])


# --------------------------------------------------------------------------
# The module adds no authority
# --------------------------------------------------------------------------


class NoAddedAuthorityTests(LaunchTestCase):
    def test_the_module_imports_only_accepted_neighbours(self) -> None:
        modules = set()
        for node in ast.walk(MODULE_TREE):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.add("." * node.level + (node.module or ""))
        self.assertEqual(
            modules,
            {
                "argparse", "http.server", "re", "sys", "time",
                "__future__", "pathlib", "typing",
                ".claude_allowance_store", ".decision_manager", ".decision_queue",
                ".repository",
            },
        )

    def test_the_module_recomputes_no_allowance_figure(self) -> None:
        for forbidden in (
            "Decimal", "percentage", "projection_inputs", "estimate_current",
            "HEALTH_", "resets_at", "round(",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, MODULE_CODE)

    def test_the_module_makes_no_provider_or_network_call(self) -> None:
        for forbidden in (
            "requests", "urllib", "socket", "subprocess", "anthropic", "api_key",
            "credential", "token", "http.client", "HTTPConnection",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, MODULE_CODE)

    def test_every_public_name_is_exported(self) -> None:
        public = {
            node.name
            for node in MODULE_TREE.body
            if isinstance(node, (ast.FunctionDef, ast.ClassDef))
            and not node.name.startswith("_")
        }
        self.assertTrue(public.issubset(set(launch.__all__)))


if __name__ == "__main__":
    unittest.main()

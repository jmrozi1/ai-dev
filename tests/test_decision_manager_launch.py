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
import collections
import contextlib
import inspect
import io
import json
import subprocess
import tempfile
import types
import unittest
import unittest.mock

from ai_dev_flow import decision_manager as manager
from ai_dev_flow import decision_manager_launch as launch
from ai_dev_flow.claude_allowance import (
    REASON_INVALID_EPOCH,
    WINDOW_FIVE_HOUR,
    WINDOW_SEVEN_DAY,
)
from ai_dev_flow.claude_allowance_store import (
    ALLOWANCE_DIRECTORY,
    ALLOWANCE_STORE_NAME,
    AllowanceStore,
)
from ai_dev_flow.claude_allowance_view import project_window
from ai_dev_flow.decision_manager import ManagerRun
from ai_dev_flow.decision_manager_launch import (
    BINDING_ROOT_FLAG,
    CLAIM_NONE_FLAG,
    CLAIM_SINCE_FLAG,
    CONTROL_PLANE_FLAG,
    PROJECT_FLAG,
    REASON_CLAIM_AMBIGUOUS,
    REASON_CLAIM_UNSTATED,
    REASON_INVALID_CLAIM,
    REASON_SOURCE_UNSTATED,
    TICKET_FLAG,
    LaunchError,
    QueueSourceContext,
    launch_manager_server,
    load_run_queue,
    main,
    render_launch_page,
    resolve_run,
)
from ai_dev_flow.decision_manager_web import LOOPBACK_HOST, build_payload
from ai_dev_flow import queue_source
from ai_dev_flow.queue_source import QueueSourceError
from ai_dev_flow.session_binding import (
    BINDING_STATE_BOUND,
    BINDING_STATE_UNBOUND,
    BindingStore,
    RailIteration,
    build_record,
)
from ai_dev_flow.session_lifecycle import (
    STATE_DISCONNECTED,
    STATE_RUNNING,
    STATE_WAITING,
    OwnedSession,
    SessionRegistry,
    elapsed_seconds,
)
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

# One blocked rail that can carry a decision, and one running rail that can carry a
# binding. Named after real Issue #55 rails so the fixture reads like the scope it
# stands in for.
BLOCKED_RAIL = "issue-55-durable-queue-source-adapter"
LIVE_RAIL = "issue-55-agent-sdk-worker-integration"

SESSION = "1a2b3c4d-0001-4000-8000-00000000000a"
OTHER_SESSION = "1a2b3c4d-0002-4000-8000-00000000000b"

RESERVED_AT = "2026-08-31T11:30:00Z"
STARTED_AT = "2026-08-31T11:40:00Z"

# An arbitrary fixed instant. Its only job is to be recognisable when a second clock
# read would have produced a different one.
FIXED_EPOCH = 1_772_000_000

# Where the accepted template parks this view's data. Read rather than re-derived, so
# these tests assert against the payload the page actually ships.
PAYLOAD_OPEN = '<script type="application/json" id="queue-payload">'
PAYLOAD_CLOSE = "</script>"

_Launch = collections.namedtuple("_Launch", "code out err served")


def a_record(**overrides) -> dict:
    """One durable decision record, exactly as an orchestrator publishes it."""
    payload = {
        "schemaVersion": 1,
        "decisionId": "queue-source-worktree-conflict",
        "project": PROJECT,
        "ticket": TICKET,
        "rail": BLOCKED_RAIL,
        "raisedAt": "2026-08-31T11:00:00Z",
        "title": "Decide the disposition of two untracked launcher files",
        "explanation": "The canonical worktree carries launcher work this rail does not own.",
        "evidence": [{"label": "worktree status", "locator": "git status --porcelain"}],
        "blocker": {
            "kind": "environment",
            "whatFailed": "starting-identity verification",
            "missingCapability": "an exclusively held product worktree",
            "humanChange": "decide the disposition of the two untracked paths",
            "stateChanged": False,
            "nextAction": "re-dispatch the rail to a fresh executor session",
        },
    }
    payload.update(overrides)
    return payload

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

    def test_a_stated_claim_is_accepted_and_the_run_stops_on_the_scope_instead(self) -> None:
        """The claim is judged first, so a good claim reaches the next question."""
        for stated in ([CLAIM_NONE_FLAG], [CLAIM_SINCE_FLAG, str(SINCE)]):
            with self.subTest(stated=stated), self.rooted():
                code, _, err = self.run_main(stated)
            self.assertEqual(code, 1)
            self.assertIn(REASON_SOURCE_UNSTATED, err)
            for claim_reason in (REASON_CLAIM_UNSTATED, REASON_INVALID_CLAIM):
                self.assertNotIn(claim_reason, err)

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

    def test_nothing_is_scheduled_or_looped_by_this_module(self) -> None:
        """Serving is now the entry point's job; scheduling still is not.

        The accepted server's ordinary request handling is the only loop allowed on
        this path. A timer, a thread, or a hand-rolled loop would be the periodic
        refresh this surface deliberately does not have.
        """
        for forbidden in ("Thread", "Timer", "sleep", "schedule", "while True"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, MODULE_CODE)

    def test_serving_goes_through_the_accepted_helper(self) -> None:
        """One place decides how this surface is served, and it is not this module."""
        self.assertIn("serve_forever(server)", MODULE_CODE)
        self.assertNotIn("server.serve_forever", MODULE_CODE)


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


class CallerSuppliedQueueTests(LaunchTestCase):
    """The two callable helpers still take the caller's queue, unchanged."""

    def test_the_rendered_page_carries_the_callers_queue(self) -> None:
        view, details = a_queue([a_decision(decision_id="d-9", title="Pick the seam")])
        with self.rooted():
            page = render_launch_page(view, details, human_exclusive_since=None)
        self.assertIn("Pick the seam", page)

    def test_the_launch_helper_still_takes_the_callers_queue(self) -> None:
        view, details = a_queue([a_decision(decision_id="d-8", title="Pick the port")])
        with self.rooted():
            server = launch_manager_server(view, details, human_exclusive_since=None)
        self.addCleanup(server.server_close)
        self.assertIn("Pick the port", server.RequestHandlerClass.page)

    def test_this_module_builds_no_queue_of_its_own(self) -> None:
        """Acquiring a queue is not the same as constructing one."""
        for forbidden in (
            "build_queue", "PendingDecision(", "OperationalAgent(", "DecisionQueue(",
            "QueueRow(", "SelectedDetail(",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, MODULE_CODE)

    def test_the_accepted_adapter_is_the_only_production_builder(self) -> None:
        """Exactly one module turns durable state into queue items, and it is not this one."""
        repo_root = Path(launch.__file__).parents[1]
        builders = []
        for source in sorted(repo_root.rglob("*.py")):
            relative = source.relative_to(repo_root)
            if relative.parts[0] in ("tests", ".git"):
                continue
            text = source.read_text(encoding="utf-8", errors="replace")
            if "PendingDecision(" in text or "OperationalAgent(" in text:
                builders.append(relative.as_posix())
        self.assertEqual(builders, ["ai_dev_flow/queue_source.py"])


# --------------------------------------------------------------------------
# The entry point reads one real durable scope
# --------------------------------------------------------------------------


class SourcedLaunchTestCase(LaunchTestCase):
    """A real coordination repository and a real binding store. No process, no network."""

    def setUp(self) -> None:
        super().setUp()
        self._sources = tempfile.TemporaryDirectory(prefix="launch-source-")
        self.addCleanup(self._sources.cleanup)
        self.sources = Path(self._sources.name).resolve()
        self.coordination = self._init_coordination()
        self.scope = self.coordination / PROJECT / TICKET
        self.write(self.scope / "state.md", "# Control Plane State\n")
        self.binding_root = self.sources / "controller-state"
        self.store = BindingStore(self.binding_root)
        self.registry = SessionRegistry()

    # -- fixtures ---------------------------------------------------------

    def _git(self, *args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(self.coordination), *args],
            check=True, text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return completed.stdout.strip()

    def _init_coordination(self) -> Path:
        self.coordination = self.sources / "coordination"
        self.coordination.mkdir(parents=True)
        self._git("init", "-q")
        self._git("config", "user.name", "Launch Tests")
        self._git("config", "user.email", "launch-tests@example.com")
        (self.coordination / "README.md").write_text("coordination\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git("commit", "-q", "-m", "initial commit")
        return self.coordination

    def write(self, path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def authorize(self, rail_id: str, status: str = "ready", *, handoff=None) -> Path:
        path = self.write(
            self.scope / "rails" / rail_id / "rail.md",
            "# Rail: {0}\n\nStatus: {1}\nRole: executor\nDepends on: none\n"
            "Shared resource: none\n\n## Goal\n\nbounded work\n".format(rail_id, status),
        )
        if handoff is not None:
            self.write(self.scope / "rails" / rail_id / "handoff.md", handoff)
        return path

    def decide(self, rail_id: str = BLOCKED_RAIL, payload=None, *, raw=None) -> Path:
        target = self.scope / "rails" / rail_id / "decision.json"
        if raw is not None:
            return self.write(target, raw)
        body = a_record() if payload is None else payload
        return self.write(target, json.dumps(body, indent=2, sort_keys=True) + "\n")

    def blob(self, rail_id: str) -> str:
        return self._git(
            "hash-object", "--", str(self.scope / "rails" / rail_id / "rail.md")
        )

    def bind(self, rail_id: str = LIVE_RAIL, *, session_id: str = SESSION, blob=None,
             state: str = BINDING_STATE_BOUND):
        iteration = RailIteration(
            rail=rail_id, blob=blob if blob is not None else self.blob(rail_id)
        )
        process: dict = {}
        if state == BINDING_STATE_BOUND:
            process = {
                "pid": 4242, "pid_domain": "test-host",
                "started_at": STARTED_AT, "bound_at": STARTED_AT,
            }
        record = build_record(
            project=PROJECT, ticket=TICKET,
            workspace_key="github:jmrozi1/ai-dev#55",
            worktree_id="worktree-55",
            workspace_path=str(self.sources / "workspace-55"),
            rail=rail_id, role="executor", iteration=iteration,
            session_id=session_id, launched_at_head="c" * 40,
            reserved_at=RESERVED_AT, state=state, **process,
        )
        self.store.write_new(record)
        return record

    def own(self, record, *, pgid: int = 4242) -> OwnedSession:
        return self.registry.add(
            OwnedSession(
                session_id=record.session_id,
                handle=types.SimpleNamespace(pid=record.pid, pgid=pgid),
                pid=record.pid, pid_domain=record.pid_domain, pgid=pgid,
                started_at=record.started_at, iteration=record.iteration,
                workspace_path=record.workspace_path, role=record.role,
            )
        )

    # -- invocation -------------------------------------------------------

    def source_argv(self, **overrides) -> list:
        stated = {
            CONTROL_PLANE_FLAG: str(self.coordination),
            PROJECT_FLAG: PROJECT,
            TICKET_FLAG: TICKET,
            BINDING_ROOT_FLAG: str(self.binding_root),
        }
        stated.update(overrides)
        argv = []
        for flag, value in stated.items():
            if value is not None:
                argv.extend([flag, value])
        return argv

    def context(self) -> QueueSourceContext:
        return QueueSourceContext(
            control_plane=self.coordination, project=PROJECT, ticket=TICKET,
            binding_root=self.binding_root,
        )

    def a_run(self):
        with self.rooted():
            return resolve_run(human_exclusive_since=None)

    def launch(self, argv=None, *, claim=(CLAIM_NONE_FLAG,)):
        """Run the entry point to completion, capturing the server it would serve."""
        served = []
        stated = list(claim) + (self.source_argv() if argv is None else list(argv))
        with self.rooted(), unittest.mock.patch.object(
            launch, "serve_forever", served.append
        ):
            code, out, err = self.run_main(stated)
        for server in served:
            self.addCleanup(server.server_close)
        return _Launch(code, out, err, served)

    def payload_of(self, served) -> dict:
        """The exact payload the accepted renderer put on the page it serves."""
        page = served.RequestHandlerClass.page
        opening = page.index(PAYLOAD_OPEN) + len(PAYLOAD_OPEN)
        closing = page.index(PAYLOAD_CLOSE, opening)
        return json.loads(page[opening:closing])


class SourcedEntryPointTests(SourcedLaunchTestCase):
    def test_an_authoritatively_empty_scope_is_served_rather_than_refused(self) -> None:
        """The whole point of the rail: a proven-empty queue is a page, not a stop."""
        result = self.launch()

        self.assertEqual(result.code, 0)
        self.assertEqual(len(result.served), 1)
        self.assertEqual(self.payload_of(result.served[0])["rows"], [])
        self.assertIn("queue rows: 0", result.out)

    def test_a_durable_decision_reaches_the_page_as_the_projected_waiting_row(self) -> None:
        self.authorize(BLOCKED_RAIL, "blocked")
        self.decide()

        result = self.launch()
        payload = self.payload_of(result.served[0])

        self.assertEqual(result.code, 0)
        self.assertEqual(len(payload["rows"]), 1)
        row = payload["rows"][0]
        self.assertEqual(row["state"], STATE_WAITING)
        self.assertEqual(row["title"], a_record()["title"])
        detail = payload["details"][row["itemId"]]
        self.assertEqual(detail["explanation"], a_record()["explanation"])
        self.assertEqual(
            [entry["label"] for entry in detail["evidence"]], ["worktree status"]
        )

    def test_a_durable_binding_this_process_never_started_reads_as_disconnected(self) -> None:
        """A fresh registry owns nothing, so the accepted lifecycle says so."""
        self.authorize(LIVE_RAIL, "running")
        self.bind(LIVE_RAIL)

        result = self.launch()
        payload = self.payload_of(result.served[0])

        self.assertEqual([row["state"] for row in payload["rows"]], [STATE_DISCONNECTED])

    def test_an_owned_live_session_reaches_the_view_as_running(self) -> None:
        """Running needs an injected registry that actually owns the handle."""
        self.authorize(LIVE_RAIL, "running")
        record = self.bind(LIVE_RAIL)
        self.own(record)

        view, details = load_run_queue(
            self.a_run(), self.context(), registry=self.registry,
            alive=lambda pgid: True,
        )

        self.assertEqual([row.state for row in view.rows], [STATE_RUNNING])
        self.assertIsNone(details[view.rows[0].item_id].explanation)

    def test_the_page_carries_all_three_states_while_waiting_stays_the_default(self) -> None:
        self.authorize(BLOCKED_RAIL, "blocked")
        self.decide()
        self.authorize(LIVE_RAIL, "running")
        self.bind(LIVE_RAIL)

        payload = self.payload_of(self.launch().served[0])

        self.assertEqual(payload["states"], list(QUEUE_STATES))
        self.assertEqual(
            sorted(row["state"] for row in payload["rows"]),
            sorted([STATE_DISCONNECTED, STATE_WAITING]),
        )
        # Membership is the page's job, and Waiting is still what it opens on.
        self.assertEqual(payload["defaultFilters"], [STATE_WAITING])

    def test_every_visible_row_carries_the_detail_the_queue_projected(self) -> None:
        """The renderer refuses a missing detail, so this also proves none is skipped."""
        self.authorize(BLOCKED_RAIL, "blocked")
        self.decide()
        self.authorize(LIVE_RAIL, "running")
        self.bind(LIVE_RAIL)

        run = self.a_run()

        view, details = load_run_queue(run, self.context(), registry=self.registry)

        self.assertEqual(sorted(details), sorted(row.item_id for row in view.rows))
        payload = build_payload(
            view, details, allowance=manager.project_allowance(run)
        )
        self.assertEqual(sorted(payload["details"]), sorted(details))

    def test_a_stated_claim_is_reported_exactly_as_stated(self) -> None:
        for stated, expected in (
            ((CLAIM_NONE_FLAG,), "none claimed"),
            ((CLAIM_SINCE_FLAG, str(SINCE)), "since {0}".format(SINCE)),
        ):
            with self.subTest(stated=stated):
                result = self.launch(claim=stated)
                self.assertEqual(result.code, 0)
                self.assertIn(expected, result.out)

    def test_nothing_bounded_out_of_the_launcher_leaks_a_decision_body(self) -> None:
        """A terminal is not where evidence, bindings, or session identity belong."""
        self.authorize(BLOCKED_RAIL, "blocked")
        self.decide()
        self.authorize(LIVE_RAIL, "running")
        self.bind(LIVE_RAIL)

        result = self.launch()

        for secret in (
            a_record()["explanation"], "git status --porcelain", SESSION, "4242",
        ):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, result.out)


class SourceRefusalTests(SourcedLaunchTestCase):
    """A refusal exits before a server exists. It is never drawn as an empty queue."""

    def _refused(self, expected_reason: str) -> None:
        result = self.launch()
        self.assertEqual(result.code, 2)
        self.assertEqual(result.served, [])
        self.assertIn(expected_reason, result.err)
        self.assertNotIn("manager: http://", result.out)

    def test_an_unreadable_scope_refuses(self) -> None:
        (self.scope / "state.md").unlink()
        self._refused(queue_source.REASON_SCOPE_UNKNOWN)

    def test_a_rail_with_no_authorization_refuses(self) -> None:
        (self.scope / "rails" / BLOCKED_RAIL).mkdir(parents=True)
        self.write(self.scope / "rails" / BLOCKED_RAIL / "handoff.md", "# Handoff\n")
        self._refused(queue_source.REASON_SOURCE_UNREADABLE)

    def test_a_decision_contradicting_its_rail_refuses(self) -> None:
        self.authorize(BLOCKED_RAIL, "ready")
        self.decide()
        self._refused(queue_source.REASON_DECISION_RAIL_CONTRADICTS)

    def test_an_invalid_decision_record_refuses(self) -> None:
        self.authorize(BLOCKED_RAIL, "blocked")
        self.decide(raw="{ not json")
        self._refused(queue_source.REASON_DECISION_INVALID)

    def test_a_lifecycle_refusal_refuses(self) -> None:
        self.authorize(LIVE_RAIL, "running")
        self.bind(LIVE_RAIL, blob="f" * 40)
        self._refused(queue_source.REASON_LIFECYCLE_REFUSED)

    def test_two_bindings_claiming_one_rail_refuse_as_conflicting(self) -> None:
        self.authorize(LIVE_RAIL, "running")
        self.bind(LIVE_RAIL, session_id=SESSION)
        self.bind(LIVE_RAIL, session_id=OTHER_SESSION)
        self._refused(queue_source.REASON_CONFLICTING_ITEMS)

    def test_a_moved_source_refuses_when_the_caller_pinned_one(self) -> None:
        with self.assertRaises(QueueSourceError) as caught:
            load_run_queue(
                self.a_run(), self.context(), registry=self.registry,
                expected_head="0" * 40,
            )
        self.assertEqual(caught.exception.reason, queue_source.REASON_SOURCE_STALE)

    def test_a_refusal_is_never_downgraded_into_a_served_empty_page(self) -> None:
        """The one outcome this entry point must never produce."""
        (self.scope / "state.md").unlink()

        result = self.launch()

        self.assertNotEqual(result.code, 0)
        self.assertEqual(result.served, [])

    def test_the_launcher_translates_no_source_reason_into_one_of_its_own(self) -> None:
        for forbidden in (
            "REASON_SOURCE_UNREADABLE", "REASON_CONFLICTING_ITEMS", "no-queue-source",
            "except Exception", "pass\n",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, MODULE_CODE)


class StatedScopeTests(SourcedLaunchTestCase):
    """Silence names no scope, here or anywhere behind here."""

    def test_every_source_input_is_required(self) -> None:
        for flag in (CONTROL_PLANE_FLAG, PROJECT_FLAG, TICKET_FLAG, BINDING_ROOT_FLAG):
            with self.subTest(flag=flag):
                result = self.launch(self.source_argv(**{flag: None}))
                self.assertEqual(result.code, 1)
                self.assertIn(REASON_SOURCE_UNSTATED, result.err)
                self.assertIn(flag, result.err)
                self.assertEqual(result.served, [])

    def test_stating_no_scope_at_all_is_refused(self) -> None:
        result = self.launch([])
        self.assertEqual(result.code, 1)
        self.assertIn(REASON_SOURCE_UNSTATED, result.err)

    def test_the_context_value_gives_no_field_a_default(self) -> None:
        stated = {
            "control_plane": self.coordination, "project": PROJECT,
            "ticket": TICKET, "binding_root": self.binding_root,
        }
        for omitted in sorted(stated):
            partial = {key: value for key, value in stated.items() if key != omitted}
            with self.subTest(omitted=omitted), self.assertRaises(TypeError):
                QueueSourceContext(**partial)

    def test_the_scope_is_never_read_from_the_environment_or_a_config_file(self) -> None:
        for forbidden in (
            "environ", "getenv", "config.json", ".ai-dev", "workflow.json",
            "discover", "cache", "controlPlane",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, MODULE_CODE)

    def test_a_stated_scope_that_does_not_exist_refuses_rather_than_reads_empty(self) -> None:
        result = self.launch(self.source_argv(**{TICKET_FLAG: "issue-56"}))

        self.assertEqual(result.code, 2)
        self.assertIn(queue_source.REASON_SCOPE_UNKNOWN, result.err)
        self.assertEqual(result.served, [])


class OneRunOneInstantTests(SourcedLaunchTestCase):
    def _capturing_load(self, calls):
        real = launch.load_queue

        def capturing(repo_root, **kwargs):
            calls.append(kwargs)
            return real(repo_root, **kwargs)

        return unittest.mock.patch.object(launch, "load_queue", capturing)

    def test_one_launch_acquires_the_queue_exactly_once(self) -> None:
        calls = []
        with self._capturing_load(calls):
            result = self.launch()

        self.assertEqual(result.code, 0)
        self.assertEqual(len(calls), 1)

    def test_one_launch_reads_exactly_one_clock(self) -> None:
        reads = []
        with self.counted_clock(reads):
            self.launch()
        self.assertEqual(len(reads), 1)

    def test_the_queue_instant_is_the_runs_own_epoch(self) -> None:
        """A second clock read would be visible here as a different instant."""
        calls = []
        with unittest.mock.patch.object(launch.time, "time", lambda: FIXED_EPOCH), \
                self._capturing_load(calls):
            result = self.launch()

        self.assertIn("run instant: {0}".format(FIXED_EPOCH), result.out)
        self.assertEqual(calls[0]["now"], launch._queue_instant(FIXED_EPOCH))

    def test_the_queue_instant_is_the_shape_the_accepted_lifecycle_parses(self) -> None:
        """Pins the format to the accepted parser rather than to a second literal."""
        self.assertEqual(
            elapsed_seconds(
                launch._queue_instant(FIXED_EPOCH),
                launch._queue_instant(FIXED_EPOCH + 3600),
            ),
            3600,
        )

    def test_the_binding_store_is_built_from_the_stated_root(self) -> None:
        calls = []
        with self._capturing_load(calls):
            self.launch()

        self.assertEqual(calls[0]["store"].root, BindingStore(self.binding_root).root)

    def test_the_stated_scope_reaches_the_adapter_untouched(self) -> None:
        calls = []
        with self._capturing_load(calls):
            self.launch()

        self.assertEqual(calls[0]["project"], PROJECT)
        self.assertEqual(calls[0]["ticket"], TICKET)


class NoProcessAdoptionTests(SourcedLaunchTestCase):
    def test_a_fresh_registry_never_adopts_a_durable_binding(self) -> None:
        """A standalone launch owns no handle, whatever a record says its pid was."""
        self.authorize(LIVE_RAIL, "running")
        self.bind(LIVE_RAIL)

        payload = self.payload_of(self.launch().served[0])

        self.assertEqual([row["state"] for row in payload["rows"]], [STATE_DISCONNECTED])

    def test_the_module_reconstructs_no_ownership(self) -> None:
        for forbidden in (
            "OwnedSession", "getpid", "pid_domain", "pgid", "psutil", "kill(",
            "process_group_alive",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, MODULE_CODE)

    def test_the_standalone_entry_point_builds_one_empty_registry(self) -> None:
        self.assertEqual(MODULE_CODE.count("SessionRegistry()"), 1)


class AcceptedAllowanceFailureTests(SourcedLaunchTestCase):
    def test_an_unusable_epoch_is_reported_with_the_accepted_reason(self) -> None:
        """The estimator's judgement, surfaced rather than duplicated or raised."""
        result = self.launch(claim=(CLAIM_SINCE_FLAG, "-5"))

        self.assertEqual(result.code, 3)
        self.assertEqual(result.served, [])
        self.assertIn(REASON_INVALID_EPOCH, result.err)
        self.assertNotIn("Traceback", result.err)

    def test_the_launcher_states_no_epoch_rule_of_its_own(self) -> None:
        for forbidden in ("> 0", ">= 0", "must be positive", "negative"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, MODULE_CODE)


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
                "__future__", "dataclasses", "datetime", "pathlib", "typing",
                ".authorization",
                ".claude_allowance_store", ".claude_allowance_view",
                ".decision_manager", ".decision_manager_web", ".decision_queue",
                ".queue_source", ".repository", ".session_binding",
                ".session_lifecycle",
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

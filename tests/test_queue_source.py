from __future__ import annotations

import json
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_dev_flow import queue_source
from ai_dev_flow.decision_queue import (
    DEFAULT_FILTERS,
    KIND_AGENT,
    KIND_DECISION,
    DecisionQueue,
    OperationalAgent,
    QueueError,
)
from ai_dev_flow.queue_source import QueueSourceError, load_queue
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
)


PROJECT = "ai-dev"
TICKET = "issue-55"
BLOCKED_RAIL = "issue-55-durable-queue-source-adapter"
LIVE_RAIL = "issue-55-agent-sdk-worker-integration"
SESSION = "1a2b3c4d-0001-4000-8000-00000000000a"
OTHER_SESSION = "1a2b3c4d-0002-4000-8000-00000000000b"
HEAD = "c" * 40
UNRELATED_BLOB = "f" * 40

NOW = "2026-08-31T12:00:00Z"
RAISED_AT = "2026-08-31T11:00:00Z"
RESERVED_AT = "2026-08-31T11:30:00Z"
STARTED_AT = "2026-08-31T11:40:00Z"
DECISION_AGE = 3600
SESSION_AGE = 1200


def decision_payload(**overrides: object) -> dict:
    payload = {
        "schemaVersion": 1,
        "decisionId": "queue-source-worktree-conflict",
        "project": PROJECT,
        "ticket": TICKET,
        "rail": BLOCKED_RAIL,
        "raisedAt": RAISED_AT,
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


class QueueSourceTestBase(unittest.TestCase):
    """A real coordination repository and a real binding store. No process, no network."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name).resolve()
        self.coordination = self._init_repo("coordination")
        self.scope = self.coordination / PROJECT / TICKET
        self.accept_state()
        self.store = BindingStore(self.tmp_path / "controller-state")
        self.registry = SessionRegistry()

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    # Fixtures

    def _git(self, repo_root: Path, *args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            check=True, text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return completed.stdout.strip()

    def _init_repo(self, name: str) -> Path:
        repo_root = self.tmp_path / name
        repo_root.mkdir(parents=True)
        self._git(repo_root, "init", "-q")
        self._git(repo_root, "config", "user.name", "Queue Source Tests")
        self._git(repo_root, "config", "user.email", "queue-source-tests@example.com")
        (repo_root / "README.md").write_text("coordination\n", encoding="utf-8")
        self._git(repo_root, "add", "README.md")
        self._git(repo_root, "commit", "-q", "-m", "initial commit")
        self._git(repo_root, "branch", "-M", "main")
        return repo_root

    def _write(self, path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def accept_state(self, text: str = "# Control Plane State\n\nProject: ai-dev\n") -> Path:
        return self._write(self.scope / "state.md", text)

    def authorize(self, rail_id: str, status: str = "ready", *, handoff: str | None = None) -> Path:
        path = self._write(
            self.scope / "rails" / rail_id / "rail.md",
            "# Rail: {0}\n\nStatus: {1}\nRole: executor\nDepends on: none\n"
            "Shared resource: none\n\n## Goal\n\nbounded work\n".format(rail_id, status),
        )
        if handoff is not None:
            self._write(self.scope / "rails" / rail_id / "handoff.md", handoff)
        return path

    def decide(self, rail_id: str = BLOCKED_RAIL, payload: object = None, *, raw: str | None = None) -> Path:
        target = self.scope / "rails" / rail_id / "decision.json"
        if raw is not None:
            return self._write(target, raw)
        body = decision_payload() if payload is None else payload
        return self._write(target, json.dumps(body, indent=2, sort_keys=True) + "\n")

    def blob(self, rail_id: str) -> str:
        return self._git(
            self.coordination, "hash-object", "--",
            str(self.scope / "rails" / rail_id / "rail.md"),
        )

    def bind(
        self,
        rail_id: str = LIVE_RAIL,
        *,
        session_id: str = SESSION,
        blob: str | None = None,
        state: str = BINDING_STATE_BOUND,
        project: str = PROJECT,
        ticket: str = TICKET,
    ):
        iteration = RailIteration(rail=rail_id, blob=blob if blob is not None else self.blob(rail_id))
        process: dict = {}
        if state == BINDING_STATE_BOUND:
            process = {
                "pid": 4242, "pid_domain": "test-host",
                "started_at": STARTED_AT, "bound_at": STARTED_AT,
            }
        record = build_record(
            project=project, ticket=ticket,
            workspace_key="github:jmrozi1/ai-dev#55",
            worktree_id="worktree-55",
            workspace_path=str(self.tmp_path / "workspace-55"),
            rail=rail_id, role="executor", iteration=iteration,
            session_id=session_id, launched_at_head=HEAD, reserved_at=RESERVED_AT,
            state=state, **process,
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

    # Invocation

    def load(self, **overrides: object) -> DecisionQueue:
        arguments: dict = {
            "project": PROJECT, "ticket": TICKET,
            "registry": self.registry, "now": NOW,
            "store": self.store, "alive": lambda pgid: True,
        }
        arguments.update(overrides)
        return load_queue(self.coordination, **arguments)  # type: ignore[arg-type]

    def refusal(self, **overrides: object) -> QueueSourceError:
        with self.assertRaises(QueueSourceError) as caught:
            self.load(**overrides)
        return caught.exception

    # Byte snapshots of every durable input this adapter reads

    def snapshot(self) -> dict:
        captured: dict = {}
        for root in (self.coordination, self.store.root):
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if path.is_file() and ".git" not in path.parts:
                    captured[str(path.relative_to(self.tmp_path))] = path.read_bytes()
        return captured


class WaitingDerivationTests(QueueSourceTestBase):
    def test_a_published_record_becomes_exactly_one_waiting_item(self) -> None:
        self.authorize(BLOCKED_RAIL, "blocked")
        self.decide()

        view = self.load().view()

        self.assertEqual(view.filters, DEFAULT_FILTERS)
        self.assertEqual(len(view.rows), 1)
        row = view.rows[0]
        self.assertEqual(row.state, STATE_WAITING)
        self.assertTrue(row.item_id.startswith("{0}:{1}|".format(len(KIND_DECISION), KIND_DECISION)))
        self.assertEqual(row.elapsed_seconds, DECISION_AGE)

    def test_every_row_field_is_carried_from_the_record_rather_than_inferred(self) -> None:
        self.authorize(BLOCKED_RAIL, "blocked")
        payload = decision_payload()
        self.decide(payload=payload)

        item = self.load().items[0]

        self.assertEqual(item.decision_id, payload["decisionId"])
        self.assertEqual(item.project, payload["project"])
        self.assertEqual(item.ticket, payload["ticket"])
        self.assertEqual(item.rail, payload["rail"])
        self.assertEqual(item.raised_at, payload["raisedAt"])
        self.assertEqual(item.title, payload["title"])
        # Verbatim. The blocker block is not summarised into it, and nothing about
        # the rail's own prose is appended.
        self.assertEqual(item.explanation, payload["explanation"])
        self.assertEqual([entry.label for entry in item.evidence], ["worktree status"])
        self.assertEqual([entry.locator for entry in item.evidence], ["git status --porcelain"])

    def test_the_blocker_block_is_carried_whole_beside_the_queue_input(self) -> None:
        self.authorize(BLOCKED_RAIL, "blocked")
        self.decide()
        source = queue_source._resolve_source(self.coordination, expected_head=None)
        rails = queue_source._rail_index(source, project=PROJECT, ticket=TICKET)

        found = queue_source.read_decisions(source, rails, project=PROJECT, ticket=TICKET)

        self.assertEqual(sorted(found), [BLOCKED_RAIL])
        self.assertEqual(found[BLOCKED_RAIL].blocker, decision_payload()["blocker"])
        self.assertEqual(found[BLOCKED_RAIL].rail.status, "blocked")

    def test_no_record_means_no_waiting_item_at_any_rail_status(self) -> None:
        for status in ("ready", "running", "blocked", "completed"):
            with self.subTest(status=status):
                self.authorize(BLOCKED_RAIL, status)
                queue = self.load()
                self.assertEqual(queue.items, ())
                self.assertEqual(queue.view().rows, ())

    def test_a_blocked_rail_whose_prose_reads_like_a_question_is_not_waiting(self) -> None:
        self.authorize(
            BLOCKED_RAIL, "blocked",
            handoff=(
                "# Handoff\n\nStatus: blocked\n\n## Stop Point\n\nThe launch failed with "
                "error: permission denied. Which worktree should this use? A human must "
                "decide before anything else happens.\n"
            ),
        )

        queue = self.load()

        self.assertEqual(queue.items, ())

    def test_elapsed_time_alone_never_raises_a_decision(self) -> None:
        self.authorize(BLOCKED_RAIL, "blocked")

        # Five years on, with the same durable facts, there is still nothing to
        # decide. Age orders and displays; it never promotes anything to Waiting.
        self.assertEqual(self.load(now="2031-08-31T12:00:00Z").items, ())


class OperationalDerivationTests(QueueSourceTestBase):
    def test_running_facts_project_an_operational_agent_through_observe_session(self) -> None:
        self.authorize(LIVE_RAIL, "running")
        record = self.bind(LIVE_RAIL)
        self.own(record)

        view = self.load().view(filters=(STATE_RUNNING,))

        self.assertEqual(len(view.rows), 1)
        row = view.rows[0]
        self.assertEqual(row.state, STATE_RUNNING)
        self.assertEqual(row.title, LIVE_RAIL)
        self.assertEqual(row.elapsed_seconds, SESSION_AGE)
        self.assertTrue(row.item_id.startswith("{0}:{1}|".format(len(KIND_AGENT), KIND_AGENT)))
        self.assertEqual(self.load().items[0].projection.reason, "owned-process-live")

    def test_a_binding_with_no_owned_handle_projects_disconnected(self) -> None:
        self.authorize(LIVE_RAIL, "running")
        self.bind(LIVE_RAIL)

        item = self.load().items[0]

        self.assertEqual(item.state, STATE_DISCONNECTED)
        self.assertEqual(item.projection.reason, "disconnected-no-owned-handle")

    def test_a_terminal_binding_contributes_no_row(self) -> None:
        self.authorize(LIVE_RAIL, "running")
        self.bind(LIVE_RAIL, state=BINDING_STATE_UNBOUND)

        self.assertEqual(self.load().items, ())

    def test_a_binding_in_another_scope_is_not_this_scope_s_row(self) -> None:
        self.authorize(LIVE_RAIL, "running")
        self.bind(LIVE_RAIL, project="ai-dev", ticket="issue-56")

        self.assertEqual(self.load().items, ())

    def test_a_lifecycle_waiting_projection_never_becomes_an_operational_row(self) -> None:
        self.authorize(BLOCKED_RAIL, "blocked")
        self.decide()
        record = self.bind(BLOCKED_RAIL)
        self.own(record)

        queue = self.load()

        # One item, and it is the decision. The lifecycle projected Waiting for this
        # same session; that projection answers a different question and is dropped.
        self.assertEqual(len(queue.items), 1)
        self.assertEqual(queue.items[0].state, STATE_WAITING)
        self.assertEqual(queue.items[0].decision_id, decision_payload()["decisionId"])
        self.assertEqual(queue.view(filters=(STATE_RUNNING, STATE_DISCONNECTED)).rows, ())

    def test_the_accepted_input_type_also_refuses_a_waiting_projection(self) -> None:
        """The skip above is the accepted boundary honoured early, not a substitute for it."""
        self.authorize(BLOCKED_RAIL, "blocked")
        self.decide()
        record = self.bind(BLOCKED_RAIL)
        self.own(record)
        source = queue_source._resolve_source(self.coordination, expected_head=None)
        rails = queue_source._rail_index(source, project=PROJECT, ticket=TICKET)
        decisions = queue_source.read_decisions(source, rails, project=PROJECT, ticket=TICKET)
        facts = queue_source.RailFacts(
            identifier=BLOCKED_RAIL, status="blocked", rail_blob=self.blob(BLOCKED_RAIL),
            pending_human_decision=decisions[BLOCKED_RAIL].decision_id,
        )
        projection = queue_source.observe_session(
            facts, record, self.registry, now=NOW, alive=lambda pgid: True
        )
        self.assertEqual(projection.state, STATE_WAITING)

        with self.assertRaises(QueueError) as caught:
            OperationalAgent(
                project=PROJECT, ticket=TICKET, rail=BLOCKED_RAIL,
                title=BLOCKED_RAIL, projection=projection,
            )
        self.assertEqual(caught.exception.reason, "operational-cannot-wait")


class AuthoritativeEmptyTests(QueueSourceTestBase):
    def test_a_scope_with_no_rails_reads_as_genuinely_empty(self) -> None:
        queue = self.load()
        view = queue.view()

        self.assertEqual(queue.items, ())
        self.assertEqual(view.rows, ())
        self.assertIsNone(view.selected_id)
        self.assertIsNone(view.detail)

    def test_rails_with_no_records_and_no_bindings_read_as_genuinely_empty(self) -> None:
        self.authorize(BLOCKED_RAIL, "blocked")
        self.authorize(LIVE_RAIL, "completed")

        self.assertEqual(self.load().view(filters=("waiting", "running", "disconnected")).rows, ())

    def test_a_controller_with_no_binding_store_still_reads_decisions(self) -> None:
        self.authorize(BLOCKED_RAIL, "blocked")
        self.decide()

        self.assertEqual(len(self.load(store=None).items), 1)


class SourceFailureBoundaryTests(QueueSourceTestBase):
    def test_a_scope_with_no_accepted_state_refuses_instead_of_reporting_empty(self) -> None:
        (self.scope / "state.md").unlink()

        self.assertEqual(self.refusal().reason, queue_source.REASON_SCOPE_UNKNOWN)

    def test_an_unknown_project_or_ticket_refuses(self) -> None:
        self.assertEqual(self.refusal(ticket="issue-56").reason, queue_source.REASON_SCOPE_UNKNOWN)
        self.assertEqual(self.refusal(project="Not A Slug").reason, queue_source.REASON_SCOPE_UNKNOWN)

    def test_a_head_that_moved_under_the_caller_refuses(self) -> None:
        self.authorize(BLOCKED_RAIL, "blocked")
        self.decide()

        exception = self.refusal(expected_head="0" * 40)

        self.assertEqual(exception.reason, queue_source.REASON_SOURCE_STALE)
        self.assertIn("re-read the current state", exception.detail)

    def test_a_matching_head_is_accepted(self) -> None:
        head = self._git(self.coordination, "rev-parse", "HEAD")
        self.authorize(BLOCKED_RAIL, "blocked")
        self.decide()

        self.assertEqual(len(self.load(expected_head=head).items), 1)

    def test_a_rail_directory_with_no_authorization_refuses(self) -> None:
        (self.scope / "rails" / BLOCKED_RAIL).mkdir(parents=True)
        self._write(self.scope / "rails" / BLOCKED_RAIL / "handoff.md", "# Handoff\n")

        exception = self.refusal()

        self.assertEqual(exception.reason, queue_source.REASON_SOURCE_UNREADABLE)
        self.assertIn("no orchestrator authorization", exception.detail)

    def test_a_malformed_record_refuses_before_any_row_exists(self) -> None:
        self.authorize(BLOCKED_RAIL, "blocked")
        self.decide(raw="{ not json")

        self.assertEqual(self.refusal().reason, queue_source.REASON_DECISION_INVALID)

    def test_every_schema_violation_refuses(self) -> None:
        self.authorize(BLOCKED_RAIL, "blocked")
        cases = {
            "unknown key": dict(decision_payload(), severity="high"),
            "raw content": dict(decision_payload(), transcript="everything that was said"),
            "oversized explanation": dict(decision_payload(), explanation="x" * 2001),
            "missing title": {k: v for k, v in decision_payload().items() if k != "title"},
            "wrong schema version": dict(decision_payload(), schemaVersion=2),
            "unparsable raised time": dict(decision_payload(), raisedAt="yesterday"),
            "session-shaped identity": dict(decision_payload(), decisionId=SESSION.replace("-", "")),
            "partial blocker": dict(
                decision_payload(),
                blocker={"kind": "permission", "whatFailed": "a thing"},
            ),
            "unknown blocker kind": dict(
                decision_payload(),
                blocker=dict(decision_payload()["blocker"], kind="vibes"),
            ),
            "unstated state change": dict(
                decision_payload(),
                blocker=dict(decision_payload()["blocker"], stateChanged="maybe"),
            ),
            "unbounded evidence": dict(
                decision_payload(),
                evidence=[{"label": "e{0}".format(index), "locator": "l"} for index in range(9)],
            ),
            "evidence carrying content": dict(
                decision_payload(),
                evidence=[{"label": "e", "locator": "l", "output": "the whole log"}],
            ),
        }
        for name, payload in cases.items():
            with self.subTest(case=name):
                self.decide(payload=payload)
                self.assertEqual(self.refusal().reason, queue_source.REASON_DECISION_INVALID)

    def test_a_record_for_another_scope_refuses(self) -> None:
        self.authorize(BLOCKED_RAIL, "blocked")
        self.decide(payload=dict(decision_payload(), ticket="issue-56"))

        self.assertEqual(self.refusal().reason, queue_source.REASON_DECISION_SCOPE_MISMATCH)

    def test_a_record_naming_a_rail_that_does_not_exist_refuses(self) -> None:
        self.authorize(BLOCKED_RAIL, "blocked")
        self.decide(payload=dict(decision_payload(), rail="issue-55-a-rail-nobody-authorized"))

        exception = self.refusal()

        self.assertEqual(exception.reason, queue_source.REASON_DECISION_RAIL_UNKNOWN)
        self.assertIn("does not authorize", exception.detail)

    def test_a_record_stored_under_a_different_rail_refuses(self) -> None:
        self.authorize(BLOCKED_RAIL, "blocked")
        self.authorize(LIVE_RAIL, "blocked")
        self.decide(rail_id=LIVE_RAIL, payload=decision_payload())

        exception = self.refusal()

        self.assertEqual(exception.reason, queue_source.REASON_DECISION_SCOPE_MISMATCH)
        self.assertIn("neither may be corrected from the other", exception.detail)

    def test_a_record_against_a_rail_that_is_not_blocked_refuses(self) -> None:
        for status in ("ready", "running", "completed"):
            with self.subTest(status=status):
                self.authorize(BLOCKED_RAIL, status)
                self.decide()

                exception = self.refusal()

                self.assertEqual(exception.reason, queue_source.REASON_DECISION_RAIL_CONTRADICTS)

    def test_a_rail_its_own_handoff_contradicts_refuses(self) -> None:
        self.authorize(BLOCKED_RAIL, "blocked", handoff="# Handoff\n\nStatus: completed\n")
        self.decide()

        exception = self.refusal()

        self.assertEqual(exception.reason, queue_source.REASON_RAIL_UNRECONCILED)
        self.assertIn("reconciles that", exception.detail)

    def test_an_unreadable_binding_refuses_the_whole_read(self) -> None:
        self.authorize(LIVE_RAIL, "running")
        self.bind(LIVE_RAIL)
        self._write(self.store.bindings_directory / "{0}.json".format(OTHER_SESSION), "{ broken")

        self.assertEqual(self.refusal().reason, queue_source.REASON_BINDING_UNREADABLE)

    def test_a_binding_on_an_unauthorized_rail_refuses(self) -> None:
        self.authorize(LIVE_RAIL, "running")
        blob = self.blob(LIVE_RAIL)
        (self.scope / "rails" / LIVE_RAIL / "rail.md").unlink()
        (self.scope / "rails" / LIVE_RAIL).rmdir()
        self.authorize(BLOCKED_RAIL, "blocked")
        self.bind(LIVE_RAIL, blob=blob)

        exception = self.refusal()

        self.assertEqual(exception.reason, queue_source.REASON_BINDING_RAIL_UNKNOWN)

    def test_a_session_bound_to_a_superseded_authorization_refuses(self) -> None:
        self.authorize(LIVE_RAIL, "running")
        self.bind(LIVE_RAIL, blob=UNRELATED_BLOB)

        exception = self.refusal()

        self.assertEqual(exception.reason, queue_source.REASON_LIFECYCLE_REFUSED)
        self.assertIn("bound at iteration", exception.detail)

    def test_a_live_session_on_a_rail_that_is_not_running_refuses(self) -> None:
        self.authorize(LIVE_RAIL, "ready")
        record = self.bind(LIVE_RAIL)
        self.own(record)

        exception = self.refusal()

        self.assertEqual(exception.reason, queue_source.REASON_LIFECYCLE_REFUSED)
        self.assertIn("is exactly the ambiguity this refuses", exception.detail)

    def test_a_blocked_rail_with_a_live_session_and_no_record_refuses(self) -> None:
        self.authorize(BLOCKED_RAIL, "blocked")
        record = self.bind(BLOCKED_RAIL)
        self.own(record)

        exception = self.refusal()

        self.assertEqual(exception.reason, queue_source.REASON_LIFECYCLE_REFUSED)
        self.assertIn("unexplained rather than waiting", exception.detail)

    def test_two_bindings_claiming_one_rail_refuse_as_conflicting(self) -> None:
        self.authorize(LIVE_RAIL, "running")
        self.bind(LIVE_RAIL, session_id=SESSION)
        self.bind(LIVE_RAIL, session_id=OTHER_SESSION)

        exception = self.refusal()

        self.assertEqual(exception.reason, queue_source.REASON_CONFLICTING_ITEMS)
        self.assertIn("appears twice", exception.detail)


class PurityTests(QueueSourceTestBase):
    def test_a_successful_read_mutates_no_durable_input(self) -> None:
        self.authorize(BLOCKED_RAIL, "blocked")
        self.authorize(LIVE_RAIL, "running")
        self.decide()
        record = self.bind(LIVE_RAIL)
        self.own(record)
        before = self.snapshot()

        self.assertEqual(len(self.load().items), 2)

        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self._git(self.coordination, "status", "--porcelain", "--untracked-files=no"), "")

    def test_every_refusal_mutates_no_durable_input(self) -> None:
        cases = {
            "invalid record": lambda: self.decide(raw="{ not json"),
            "contradicting rail": lambda: self.decide(),
            "unreadable binding": lambda: self._write(
                self.store.bindings_directory / "{0}.json".format(OTHER_SESSION), "{ broken"
            ),
        }
        for name, arrange in cases.items():
            with self.subTest(case=name):
                self.authorize(BLOCKED_RAIL, "ready")
                arrange()
                before = self.snapshot()

                self.refusal()

                self.assertEqual(self.snapshot(), before)

    def test_the_adapter_reaches_no_remote(self) -> None:
        self.authorize(BLOCKED_RAIL, "blocked")
        self.decide()
        with patch("ai_dev_flow.control_plane.subprocess.run", wraps=subprocess.run) as spied:
            self.load()
        invoked = [call.args[0] for call in spied.call_args_list if call.args]
        self.assertTrue(invoked)
        for argv in invoked:
            self.assertFalse(
                {"fetch", "push", "pull", "clone", "ls-remote"} & set(argv),
                msg="the adapter ran a networked command: {0}".format(argv),
            )

    def test_two_reads_of_one_state_produce_the_same_queue(self) -> None:
        self.authorize(BLOCKED_RAIL, "blocked")
        self.authorize(LIVE_RAIL, "running")
        self.decide()
        record = self.bind(LIVE_RAIL)
        self.own(record)

        first = self.load().view(filters=("waiting", "running"))
        second = self.load().view(filters=("waiting", "running"))

        self.assertEqual(first, second)


class ProjectionAuthorityTests(QueueSourceTestBase):
    def test_the_adapter_returns_the_accepted_queue_rather_than_its_own_rows(self) -> None:
        self.authorize(BLOCKED_RAIL, "blocked")
        self.decide()

        queue = self.load()

        self.assertIsInstance(queue, DecisionQueue)
        # Ordering, filtering, identity, selection, and detail all still belong to
        # the queue. The adapter exposes none of them.
        for absent in ("view", "rows", "filters", "sort", "select", "detail"):
            self.assertFalse(
                hasattr(queue_source, absent), msg="queue_source must not own '{0}'".format(absent)
            )

    def test_ordering_and_selection_come_from_the_queue(self) -> None:
        self.authorize(BLOCKED_RAIL, "blocked")
        self.authorize(LIVE_RAIL, "running")
        self.decide()
        record = self.bind(LIVE_RAIL)
        self.own(record)

        view = self.load().view(filters=("waiting", "running"))

        # Oldest first: the decision was raised an hour ago, the session started
        # twenty minutes ago. The adapter chose neither order nor selection.
        self.assertEqual([row.elapsed_seconds for row in view.rows], [DECISION_AGE, SESSION_AGE])
        self.assertEqual(view.selected_id, view.rows[0].item_id)
        self.assertEqual(view.detail.explanation, decision_payload()["explanation"])

    def test_the_detail_of_an_operational_row_carries_no_invented_explanation(self) -> None:
        self.authorize(LIVE_RAIL, "running")
        record = self.bind(LIVE_RAIL)
        self.own(record)

        view = self.load().view(filters=(STATE_RUNNING,))

        self.assertIsNone(view.detail.explanation)
        self.assertEqual(view.detail.evidence, ())


if __name__ == "__main__":
    unittest.main()

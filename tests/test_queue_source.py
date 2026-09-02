from __future__ import annotations

import ast
import json
import subprocess
import tempfile
import types
import unittest
from dataclasses import fields as dataclass_fields
from pathlib import Path
from unittest.mock import patch

from ai_dev_flow import queue_source
from ai_dev_flow.attention_projection import (
    ACTIVITY_BLOCKED,
    ACTIVITY_CONTEXT_ROTATION,
    ACTIVITY_DISCONNECTED_RECOVERY,
    ACTIVITY_EXECUTOR_WORKING,
    ACTIVITY_MANAGER_LIFECYCLE,
    ACTIVITY_ORCHESTRATOR_RECONCILING,
    ACTIVITY_REVIEWER_WORKING,
    OWNER_AGENT,
    OWNER_HUMAN,
)
from ai_dev_flow.authorization import reconcile_agent_slots
from ai_dev_flow.decision_queue import (
    DEFAULT_FILTERS,
    QUEUE_STATES,
    KIND_AGENT,
    KIND_DECISION,
    DecisionQueue,
    OperationalAgent,
    QueueError,
)
from ai_dev_flow import queue_source as queue_source_module
from ai_dev_flow.queue_source import (
    QueueScope,
    QueueSourceError,
    load_queue,
    project_queue,
    resolve_queue_scope,
)
from ai_dev_flow.session_binding import (
    BINDING_STATE_BOUND,
    BINDING_STATE_RESERVED,
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
    ownership_evidence,
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

# The three accepted states, named once so a filter list is not retyped.
QUEUE_STATES_ALL = QUEUE_STATES


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
        role: str = "executor",
        pid: int = 4242,
    ):
        iteration = RailIteration(rail=rail_id, blob=blob if blob is not None else self.blob(rail_id))
        process: dict = {}
        if state == BINDING_STATE_BOUND:
            process = {
                "pid": pid, "pid_domain": "test-host",
                "started_at": STARTED_AT, "bound_at": STARTED_AT,
            }
        record = build_record(
            project=project, ticket=ticket,
            workspace_key="github:jmrozi1/ai-dev#55",
            worktree_id="worktree-55",
            workspace_path=str(self.tmp_path / "workspace-55"),
            rail=rail_id, role=role, iteration=iteration,
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
                activity=ACTIVITY_BLOCKED, attention_owner=OWNER_AGENT,
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

    def test_two_sessions_this_controller_can_prove_are_live_fail_closed(self) -> None:
        """Contradictory ownership evidence is never merged into one plausible row."""
        self.authorize(LIVE_RAIL, "running")
        self.own(self.bind(LIVE_RAIL, session_id=SESSION))
        self.own(self.bind(LIVE_RAIL, session_id=OTHER_SESSION), pgid=4243)

        exception = self.refusal()

        self.assertEqual(exception.reason, queue_source.REASON_OWNERSHIP_CONTRADICTORY)
        self.assertIn("contradictory evidence", exception.detail)
        # Both are named, so the refusal is actionable rather than merely stern.
        self.assertIn(SESSION, exception.detail)
        self.assertIn(OTHER_SESSION, exception.detail)


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
        # Its evidence is transport, not a human-decision reference: session
        # identity is allowed here and nowhere else, and it is bounded.
        self.assertEqual([entry.label for entry in view.detail.evidence], ["live session"])
        self.assertIn(SESSION, view.detail.evidence[0].locator)
        self.assertLessEqual(len(view.detail.evidence), 8)


# --------------------------------------------------------------------------
# Named checkpoint 7: the row is the durable rail, and activity is not ownership
# --------------------------------------------------------------------------


class RailCentricIdentityTests(QueueSourceTestBase):
    """The visible operational item is the work, never the transport carrying it."""

    def a_transport_resident(self, session_id: str = OTHER_SESSION) -> OwnedSession:
        """A live worker this controller owns that no durable binding names.

        This is the `ai-dev-55-web` shape: the hidden transport may hold a session
        and may launch agents, but it is not itself a rail, a work item, or any
        kind of product authority.
        """
        return self.registry.add(
            OwnedSession(
                session_id=session_id,
                handle=types.SimpleNamespace(pid=9001, pgid=9001),
                pid=9001, pid_domain="test-host", pgid=9001,
                started_at=STARTED_AT,
                iteration=RailIteration(rail=LIVE_RAIL, blob=UNRELATED_BLOB),
                workspace_path=str(self.tmp_path / "workspace-55"),
                role="executor",
            )
        )

    def test_a_transport_resident_on_no_running_rail_produces_no_row(self) -> None:
        self.authorize(LIVE_RAIL, "running")
        self.a_transport_resident()

        queue = self.load()

        self.assertEqual(queue.items, ())
        self.assertEqual(queue.view(filters=QUEUE_STATES_ALL).rows, ())

    def test_a_transport_resident_beside_real_work_adds_no_second_row(self) -> None:
        self.authorize(LIVE_RAIL, "running")
        self.own(self.bind(LIVE_RAIL))
        self.a_transport_resident()

        view = self.load().view(filters=QUEUE_STATES_ALL)

        self.assertEqual(len(view.rows), 1)
        self.assertEqual(view.rows[0].title, LIVE_RAIL)
        for text in _every_string(view.rows):
            self.assertNotIn(OTHER_SESSION, text)

    def test_an_idle_resident_alone_never_creates_a_waiting_row(self) -> None:
        """Waiting still comes from a published record and from nothing else."""
        self.authorize(LIVE_RAIL, "running")
        self.a_transport_resident()

        self.assertEqual(self.load().view().rows, ())

    def test_one_running_rail_projects_exactly_one_running_work_item(self) -> None:
        self.authorize(LIVE_RAIL, "running")
        self.own(self.bind(LIVE_RAIL))

        view = self.load().view(filters=QUEUE_STATES_ALL)

        self.assertEqual(len(view.rows), 1)
        self.assertEqual(view.rows[0].state, STATE_RUNNING)
        self.assertEqual(view.rows[0].title, LIVE_RAIL)

    def test_rotating_the_session_keeps_one_item_with_one_unchanged_identity(self) -> None:
        """A replaced worker is the same work. The row must not split or renumber."""
        self.authorize(LIVE_RAIL, "running")
        first = self.bind(LIVE_RAIL, session_id=SESSION)
        self.own(first)
        before = self.load().view(filters=QUEUE_STATES_ALL)

        # The replacement arrives while the old record is still nonterminal: this
        # is the window the old per-binding projection turned into a second row.
        self.own(self.bind(LIVE_RAIL, session_id=OTHER_SESSION, pid=4343), pgid=4343)
        self.registry.remove(SESSION)
        after = self.load().view(filters=QUEUE_STATES_ALL)

        self.assertEqual(len(before.rows), 1)
        self.assertEqual(len(after.rows), 1)
        self.assertEqual(after.rows[0].item_id, before.rows[0].item_id)
        self.assertEqual(after.rows[0].state, STATE_RUNNING)
        self.assertEqual(
            self.load().items[0].activity, ACTIVITY_CONTEXT_ROTATION
        )

    def test_the_item_identity_survives_every_transport_fact_changing(self) -> None:
        self.authorize(LIVE_RAIL, "running")
        self.own(self.bind(LIVE_RAIL, session_id=SESSION))
        original = self.load().view(filters=QUEUE_STATES_ALL).rows[0].item_id

        self.registry.remove(SESSION)
        self._replace_state(SESSION, BINDING_STATE_UNBOUND)
        self.own(self.bind(LIVE_RAIL, session_id=OTHER_SESSION, pid=4343), pgid=4343)

        rows = self.load().view(filters=QUEUE_STATES_ALL).rows
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].item_id, original)

    def test_session_id_worker_target_and_pid_never_reach_a_row(self) -> None:
        self.authorize(LIVE_RAIL, "running")
        self.own(self.bind(LIVE_RAIL))

        view = self.load().view(filters=QUEUE_STATES_ALL)

        for secret in (SESSION, "4242", "test-host"):
            for text in _every_string(view.rows):
                self.assertNotIn(secret, text, secret)

    def test_session_id_and_pid_appear_only_in_bounded_details(self) -> None:
        self.authorize(LIVE_RAIL, "running")
        self.own(self.bind(LIVE_RAIL))

        view = self.load().view(filters=(STATE_RUNNING,))

        self.assertEqual(len(view.detail.evidence), 1)
        locator = view.detail.evidence[0].locator
        self.assertIn(SESSION, locator)
        self.assertIn("pid 4242", locator)
        self.assertIn("test-host", locator)
        self.assertLessEqual(len(view.detail.evidence), 8)

    # -- helpers ---------------------------------------------------------

    def _replace_state(self, session_id: str, state: str) -> None:
        """Rewrite one durable record's state through the store's own file layout."""
        path = self.store.bindings_directory / "{0}.json".format(session_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["state"] = state
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class ActivityAndAttentionTests(QueueSourceTestBase):
    """Two facts, read from two durable sources, and neither derived from the other."""

    def test_a_live_session_projects_its_role_and_agent_attention(self) -> None:
        """Three roles on three rails, which is how legitimate concurrency looks."""
        cases = {
            "issue-55-role-executor-rail": ("executor", ACTIVITY_EXECUTOR_WORKING),
            "issue-55-role-reviewer-rail": ("reviewer", ACTIVITY_REVIEWER_WORKING),
            "issue-55-role-orchestrator-rail": (
                "orchestrator", ACTIVITY_ORCHESTRATOR_RECONCILING
            ),
        }
        for index, (rail, (role, _)) in enumerate(sorted(cases.items())):
            self.authorize(rail, "running")
            session = "1a2b3c4d-01{0:02d}-4000-8000-00000000000a".format(index)
            self.own(
                self.bind(rail, session_id=session, role=role, pid=5000 + index),
                pgid=5000 + index,
            )

        items = {entry.rail: entry for entry in self.load().items}

        self.assertEqual(len(items), len(cases))
        for rail, (_, activity) in cases.items():
            with self.subTest(rail=rail):
                self.assertEqual(items[rail].activity, activity)
                self.assertEqual(items[rail].attention_owner, OWNER_AGENT)

    def test_a_reservation_alone_reads_as_manager_lifecycle_not_a_lost_session(self) -> None:
        self.authorize(LIVE_RAIL, "running")
        self.bind(LIVE_RAIL, state=BINDING_STATE_RESERVED)

        item = self.load().items[0]

        self.assertEqual(item.activity, ACTIVITY_MANAGER_LIFECYCLE)
        self.assertEqual(item.state, STATE_DISCONNECTED)

    def test_unprovable_ownership_stays_visible_as_recovery(self) -> None:
        """Neither vanishing nor reading as idle: the rail stays on the screen."""
        self.authorize(LIVE_RAIL, "running")
        self.bind(LIVE_RAIL)

        queue = self.load()
        item = queue.items[0]

        self.assertEqual(item.state, STATE_DISCONNECTED)
        self.assertEqual(item.activity, ACTIVITY_DISCONNECTED_RECOVERY)
        self.assertEqual(
            len(queue.view(filters=(STATE_DISCONNECTED,)).rows), 1
        )

    def test_activity_changes_while_the_attention_owner_does_not(self) -> None:
        self.authorize(BLOCKED_RAIL, "blocked")
        self.decide()
        record = self.bind(BLOCKED_RAIL)
        self.own(record)

        parked = self.load().items[0]
        self.registry.remove(record.session_id)
        lost = self.load().items[0]

        self.assertEqual(parked.activity, ACTIVITY_BLOCKED)
        self.assertEqual(lost.activity, ACTIVITY_DISCONNECTED_RECOVERY)
        self.assertEqual(parked.attention_owner, OWNER_HUMAN)
        self.assertEqual(lost.attention_owner, OWNER_HUMAN)
        # The same durable item throughout: only what it is doing moved.
        self.assertEqual(parked.item_id, lost.item_id)
        self.assertEqual(parked.state, lost.state)

    def test_the_attention_owner_changes_while_the_activity_does_not(self) -> None:
        self.authorize(BLOCKED_RAIL, "blocked")
        self.decide()
        self.bind(BLOCKED_RAIL, session_id=SESSION)
        self.authorize(LIVE_RAIL, "running")
        self.bind(LIVE_RAIL, session_id=OTHER_SESSION)

        items = {entry.rail: entry for entry in self.load().items}

        self.assertEqual(
            items[BLOCKED_RAIL].activity, items[LIVE_RAIL].activity
        )
        self.assertEqual(items[BLOCKED_RAIL].activity, ACTIVITY_DISCONNECTED_RECOVERY)
        self.assertEqual(items[BLOCKED_RAIL].attention_owner, OWNER_HUMAN)
        self.assertEqual(items[LIVE_RAIL].attention_owner, OWNER_AGENT)

    def test_the_detail_pane_carries_both_facts_and_a_row_carries_neither(self) -> None:
        self.authorize(LIVE_RAIL, "running")
        self.own(self.bind(LIVE_RAIL))

        view = self.load().view(filters=(STATE_RUNNING,))

        self.assertEqual(view.detail.activity, ACTIVITY_EXECUTOR_WORKING)
        self.assertEqual(view.detail.attention_owner, OWNER_AGENT)
        for field in ("activity", "attention_owner"):
            self.assertFalse(hasattr(view.rows[0], field), field)


class WaitingIsTheHumanOwnedSetTests(QueueSourceTestBase):
    def test_the_default_view_is_exactly_the_human_owned_items(self) -> None:
        self.authorize(BLOCKED_RAIL, "blocked")
        self.decide()
        self.authorize(LIVE_RAIL, "running")
        self.own(self.bind(LIVE_RAIL))

        queue = self.load()
        waiting = {row.item_id for row in queue.view().rows}
        human = {
            entry.item_id for entry in queue.items
            if entry.attention_owner == OWNER_HUMAN
        }

        self.assertEqual(waiting, human)
        self.assertEqual(len(waiting), 1)
        # And nothing human-owned hides behind an operational filter.
        for combination in ((STATE_RUNNING,), (STATE_DISCONNECTED,),
                            (STATE_RUNNING, STATE_DISCONNECTED)):
            with self.subTest(filters=combination):
                for row in queue.view(filters=combination).rows:
                    self.assertNotIn(row.item_id, human)

    def test_no_operational_evidence_of_any_shape_creates_a_waiting_row(self) -> None:
        self.authorize(LIVE_RAIL, "running")
        self.own(self.bind(LIVE_RAIL, session_id=SESSION))
        self.bind(LIVE_RAIL, session_id=OTHER_SESSION, state=BINDING_STATE_RESERVED)

        self.assertEqual(self.load().view().rows, ())

    def test_a_queue_whose_waiting_set_left_the_human_owned_set_is_refused(self) -> None:
        """The set property is checked over the assembled queue, not merely implied."""
        with self.assertRaises(QueueError) as caught:
            OperationalAgent(
                project=PROJECT, ticket=TICKET, rail=LIVE_RAIL, title=LIVE_RAIL,
                projection=_a_running_projection(LIVE_RAIL),
                activity=ACTIVITY_EXECUTOR_WORKING, attention_owner=OWNER_HUMAN,
            )
        self.assertEqual(
            caught.exception.reason, "waiting-is-not-the-human-owned-set"
        )


class CheckpointSixPreservationTests(QueueSourceTestBase):
    """One ownership proof feeds both the rows and the accepted concurrency ceiling."""

    def test_rows_and_the_admission_reconciler_read_the_same_ownership_evidence(self) -> None:
        self.authorize(LIVE_RAIL, "running")
        record = self.bind(LIVE_RAIL)
        self.own(record)

        item = self.load().items[0]
        slots = reconcile_agent_slots(
            self.store.records(),
            ownership=ownership_evidence(
                self.registry, self.store.records(), alive=lambda pgid: True
            ),
        )

        self.assertEqual(item.state, STATE_RUNNING)
        self.assertEqual(slots.occupants, (SESSION,))
        self.assertTrue(slots.provable)
        self.assertEqual(slots.ceiling, 6)

    def test_an_unprovable_session_is_disconnected_and_still_occupies_no_free_slot(self) -> None:
        self.authorize(LIVE_RAIL, "running")
        self.bind(LIVE_RAIL)

        item = self.load().items[0]
        slots = reconcile_agent_slots(
            self.store.records(),
            ownership=ownership_evidence(
                self.registry, self.store.records(), alive=lambda pgid: True
            ),
        )

        self.assertEqual(item.state, STATE_DISCONNECTED)
        self.assertFalse(slots.provable)

    def test_the_seam_constructs_no_second_store_registry_or_ownership_system(self) -> None:
        source = Path(queue_source.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        constructed = {
            node.func.id for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        for forbidden in ("BindingStore", "SessionRegistry", "OwnedSession",
                          "Thread", "Timer", "Popen"):
            self.assertNotIn(forbidden, constructed, forbidden)
        for forbidden in ("getpid", "process_group_alive", "psutil", "kill(",
                          "adopt", "time.sleep", "while True"):
            self.assertNotIn(forbidden, source, forbidden)


class SingleLivenessSnapshotTests(QueueSourceTestBase):
    """One queue read is one liveness instant, for every session it touches.

    Ownership evidence and the lifecycle projection both need to know whether a
    process group is still there. They used to ask separately, with a
    `rail_blob_sha` subprocess between them, so the gap was real wall time and an
    ordinary worker exit landed in it. The read then combined a session that was
    live when ownership was proved with the same session already gone when its
    state was projected, and refused the whole queue for describing a moment that
    never existed -- while the same controller's `agent_count`, which asks once,
    degraded gracefully on identical facts.

    These are the coherence assertions. A read may report live or report
    disconnected; what it may not do is report both about one session.
    """

    class Flipping:
        """A prober that would answer differently on each successive observation.

        It is not a simulation of a race: it *is* the observable difference
        between asking once and asking twice. Under one observation per read the
        later answers are never reached, which is the property under test.
        """

        def __init__(self, answers):
            self.answers = list(answers)
            self.probes = []

        def __call__(self, pgid):
            self.probes.append(pgid)
            return self.answers[min(len(self.probes) - 1, len(self.answers) - 1)]

    def a_live_rail(self):
        self.authorize(LIVE_RAIL, "running")
        self.own(self.bind(LIVE_RAIL))

    # Proofs 1 and 2: the stable readings are exactly what they were.

    def test_a_stably_live_session_still_projects_running_and_executor_working(self) -> None:
        self.a_live_rail()
        prober = self.Flipping([True])

        item = self.load(alive=prober).items[0]

        self.assertEqual(item.state, STATE_RUNNING)
        self.assertEqual(item.activity, ACTIVITY_EXECUTOR_WORKING)
        self.assertEqual(item.attention_owner, OWNER_AGENT)

    def test_a_stably_dead_session_still_projects_disconnected_recovery(self) -> None:
        self.a_live_rail()
        prober = self.Flipping([False])

        item = self.load(alive=prober).items[0]

        self.assertEqual(item.state, STATE_DISCONNECTED)
        self.assertEqual(item.activity, ACTIVITY_DISCONNECTED_RECOVERY)
        self.assertEqual(item.attention_owner, OWNER_AGENT)

    # The mechanism itself, stated rather than inferred from the outcomes.

    def test_one_read_observes_each_process_group_exactly_once(self) -> None:
        self.a_live_rail()
        prober = self.Flipping([True])

        self.load(alive=prober)

        self.assertEqual(prober.probes, [4242])

    def test_two_sessions_on_two_rails_are_each_observed_once(self) -> None:
        self.authorize(LIVE_RAIL, "running")
        self.own(self.bind(LIVE_RAIL, session_id=SESSION), pgid=4242)
        other_rail = "issue-55-agent-sdk-isolation-contract"
        self.authorize(other_rail, "running")
        self.own(
            self.bind(other_rail, session_id=OTHER_SESSION, pid=4343), pgid=4343
        )
        prober = self.Flipping([True])

        self.load(alive=prober)

        self.assertEqual(sorted(prober.probes), [4242, 4343])

    # Proofs 3 and 4: the two directions that refused the whole queue.

    def test_a_worker_that_exits_mid_read_no_longer_refuses_the_queue(self) -> None:
        """`True` then `False`: the ordinary end of a run, which used to refuse."""
        self.a_live_rail()
        prober = self.Flipping([True, False])

        item = self.load(alive=prober).items[0]

        self.assertEqual(item.state, STATE_RUNNING)
        self.assertEqual(item.activity, ACTIVITY_EXECUTOR_WORKING)
        self.assertEqual(len(prober.probes), 1)

    def test_a_worker_that_appears_mid_read_no_longer_refuses_the_queue(self) -> None:
        """`False` then `True`, the other direction, refused just as completely."""
        self.a_live_rail()
        prober = self.Flipping([False, True])

        item = self.load(alive=prober).items[0]

        self.assertEqual(item.state, STATE_DISCONNECTED)
        self.assertEqual(item.activity, ACTIVITY_DISCONNECTED_RECOVERY)
        self.assertEqual(len(prober.probes), 1)

    def test_a_rotation_survives_a_mid_read_exit_as_one_item(self) -> None:
        """The same coherence, on the rail shape that carries two sessions."""
        self.authorize(LIVE_RAIL, "running")
        self.own(self.bind(LIVE_RAIL, session_id=SESSION))
        self.bind(LIVE_RAIL, session_id=OTHER_SESSION, state=BINDING_STATE_RESERVED)
        prober = self.Flipping([True, False])

        items = self.load(alive=prober).items

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].state, STATE_RUNNING)
        self.assertEqual(items[0].activity, ACTIVITY_CONTEXT_ROTATION)

    # Proofs 5 and 6: coherence did not soften either fail-closed path.

    def test_ownership_that_cannot_be_proved_remains_fail_closed(self) -> None:
        """Unprovable is still Disconnected and still an unprovable ceiling."""
        self.authorize(LIVE_RAIL, "running")
        self.bind(LIVE_RAIL)
        prober = self.Flipping([True, True])

        item = self.load(alive=prober).items[0]
        slots = reconcile_agent_slots(
            self.store.records(),
            ownership=ownership_evidence(
                self.registry, self.store.records(), alive=prober
            ),
        )

        self.assertEqual(item.state, STATE_DISCONNECTED)
        self.assertEqual(item.activity, ACTIVITY_DISCONNECTED_RECOVERY)
        self.assertFalse(slots.provable)

    def test_contradictory_ownership_still_refuses_and_still_names_both_sessions(self) -> None:
        self.authorize(LIVE_RAIL, "running")
        self.own(self.bind(LIVE_RAIL, session_id=SESSION), pgid=4242)
        self.own(
            self.bind(LIVE_RAIL, session_id=OTHER_SESSION, pid=4343), pgid=4343
        )

        refusal = self.refusal(alive=self.Flipping([True]))

        self.assertEqual(refusal.reason, queue_source.REASON_OWNERSHIP_CONTRADICTORY)
        self.assertIn(SESSION, str(refusal))
        self.assertIn(OTHER_SESSION, str(refusal))

    # The boundary of the fix: within a read, never across reads.

    def test_the_snapshot_does_not_survive_the_read_that_took_it(self) -> None:
        """A cache would answer the second read from the first read's instant."""
        self.a_live_rail()
        prober = self.Flipping([True, False])

        first = self.load(alive=prober).items[0]
        second = self.load(alive=prober).items[0]

        self.assertEqual(first.state, STATE_RUNNING)
        self.assertEqual(second.state, STATE_DISCONNECTED)
        self.assertEqual(len(prober.probes), 2)


class DenseRowContractTests(QueueSourceTestBase):
    def test_the_row_contract_is_unchanged_by_the_new_facts(self) -> None:
        self.authorize(BLOCKED_RAIL, "blocked")
        self.decide()
        self.authorize(LIVE_RAIL, "running")
        self.own(self.bind(LIVE_RAIL))

        queue = self.load()

        # Waiting only, by default.
        self.assertEqual(queue.view().filters, DEFAULT_FILTERS)
        # Three independent filters, any nonempty combination.
        for combination in (
            (STATE_WAITING,), (STATE_RUNNING,), (STATE_DISCONNECTED,),
            (STATE_WAITING, STATE_RUNNING), QUEUE_STATES_ALL,
        ):
            with self.subTest(filters=combination):
                view = queue.view(filters=combination)
                self.assertEqual(view.filters, tuple(
                    state for state in QUEUE_STATES_ALL if state in combination
                ))
                # Oldest first, and no second ordering rule anywhere.
                ages = [row.elapsed_seconds for row in view.rows]
                self.assertEqual(ages, sorted(ages, reverse=True))
        # No sort control, no persistent sort label, no per-row marker.
        self.assertEqual(
            {f.name for f in dataclass_fields(type(queue.view().rows[0]))},
            {"item_id", "state", "title", "project", "ticket", "elapsed_seconds"},
        )


def _a_running_projection(rail: str):
    from ai_dev_flow.session_lifecycle import SessionProjection

    return SessionProjection(
        state=STATE_RUNNING, reason="owned-process-live", detail="pid 1 is live.",
        session_id=SESSION, rail=rail, elapsed_seconds=1,
    )


def _every_string(value, seen=None):
    """Every string reachable from a value, so a leak cannot hide in a nested field."""
    seen = [] if seen is None else seen
    if id(value) in seen:
        return []
    seen.append(id(value))
    if isinstance(value, str):
        return [value]
    found = []
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            found.extend(_every_string(item, seen))
    elif hasattr(value, "__dict__"):
        for item in vars(value).values():
            found.extend(_every_string(item, seen))
    elif hasattr(value, "__dataclass_fields__"):
        for name in value.__dataclass_fields__:
            found.extend(_every_string(getattr(value, name), seen))
    return found



if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# The two halves of a read, and which of them may honestly be repeated
# ---------------------------------------------------------------------------


class QueueScopeTests(QueueSourceTestBase):
    """`load_queue` split where its two halves expire, and nowhere else.

    Half of a queue read is durable control-plane authority: the revision being
    served, the rails it authorizes, the decisions it publishes. Establishing it
    reaches the coordination remote and it describes state that outlives any one
    render. The other half is what a controller can prove about its own sessions,
    and that is only ever true of the instant it was taken.

    A reader that renders once is unaffected and still calls `load_queue`. A reader
    that renders repeatedly from one run must be able to re-observe the second half
    without re-fetching the first, or re-observing becomes polling. These prove the
    split is exactly that and changes nothing else.
    """

    def a_live_rail_with_a_handle(self):
        self.authorize(LIVE_RAIL, "running")
        self.own(self.bind(LIVE_RAIL))

    def resolved_scope(self, **overrides) -> QueueScope:
        arguments = {"project": PROJECT, "ticket": TICKET}
        arguments.update(overrides)
        return resolve_queue_scope(self.coordination, **arguments)

    def projected(self, scope, **overrides):
        arguments = {
            "registry": self.registry, "now": NOW,
            "store": self.store, "alive": lambda pgid: True,
        }
        arguments.update(overrides)
        return project_queue(scope, **arguments)

    def test_the_composed_halves_produce_the_queue_load_queue_produces(self) -> None:
        """The split is a decomposition, not a second reader."""
        self.a_live_rail_with_a_handle()

        whole = self.load()
        halves = self.projected(self.resolved_scope())

        self.assertEqual(whole.view(filters=QUEUE_STATES), halves.view(filters=QUEUE_STATES))

    def test_the_scope_pins_the_revision_every_projection_is_served_from(self) -> None:
        self.a_live_rail_with_a_handle()
        scope = self.resolved_scope()

        self.assertEqual(scope.head, self._git(self.coordination, "rev-parse", "HEAD"))
        self.assertEqual(scope.project, PROJECT)
        self.assertEqual(scope.ticket, TICKET)
        self.assertIn(LIVE_RAIL, scope.rails)

    def test_projecting_again_re_observes_and_does_not_re_resolve(self) -> None:
        """The whole reason for the split, stated as two counts at once.

        Liveness is asked again -- the second projection is allowed to disagree
        with the first, and does. The coordination remote is not consulted again,
        which is what keeps re-observing from being a poll.
        """
        self.a_live_rail_with_a_handle()
        answers = [True, False]
        probes = []

        def alive(pgid):
            probes.append(pgid)
            return answers[min(len(probes) - 1, len(answers) - 1)]

        resolutions = []
        real = queue_source_module.resolve_read_source
        with patch.object(
            queue_source_module, "resolve_read_source",
            lambda root: (resolutions.append(root), real(root))[1],
        ):
            scope = self.resolved_scope()
            first = self.projected(scope, alive=alive)
            second = self.projected(scope, alive=alive)

        self.assertEqual(len(resolutions), 1)
        self.assertEqual(len(probes), 2)
        self.assertEqual(
            [row.state for row in first.view(filters=QUEUE_STATES).rows], [STATE_RUNNING]
        )
        self.assertEqual(
            [row.state for row in second.view(filters=QUEUE_STATES).rows],
            [STATE_DISCONNECTED],
        )

    def test_a_scope_holds_no_liveness_and_no_binding_of_its_own(self) -> None:
        """Structural: nothing observed may be frozen into the durable half.

        A scope outlives a response by design. A liveness reading or a binding
        record captured in one would therefore be exactly the durable cache the
        lifecycle refuses, re-served to every later response.
        """
        self.a_live_rail_with_a_handle()

        scope = self.resolved_scope()

        self.assertEqual(
            sorted(field.name for field in dataclass_fields(QueueScope)),
            ["decisions", "project", "rails", "source", "ticket"],
        )
        self.assertNotIn(SESSION, repr(scope.rails))
        with self.assertRaises(Exception):
            scope.project = "other"

    def test_the_refusals_belong_to_the_half_that_owns_them(self) -> None:
        """Source refusals resolve; projection refusals project. Neither moved."""
        with self.assertRaises(QueueSourceError) as stale:
            self.resolved_scope(expected_head="f" * 40)
        self.assertEqual(stale.exception.reason, queue_source.REASON_SOURCE_STALE)

        self.authorize(LIVE_RAIL, "running")
        self.bind(LIVE_RAIL, blob="f" * 40)
        scope = self.resolved_scope()
        with self.assertRaises(QueueSourceError) as drifted:
            self.projected(scope)
        self.assertEqual(
            drifted.exception.reason, queue_source.REASON_LIFECYCLE_REFUSED
        )

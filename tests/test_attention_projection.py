"""`attention_projection` separates what work is doing from who owes it attention."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path
import ast
import unittest

from ai_dev_flow import attention_projection as model
from ai_dev_flow.attention_projection import (
    ACTIVITIES,
    ACTIVITY_BLOCKED,
    ACTIVITY_CONTEXT_ROTATION,
    ACTIVITY_DISCONNECTED_RECOVERY,
    ACTIVITY_EXECUTOR_WORKING,
    ACTIVITY_MANAGER_LIFECYCLE,
    ACTIVITY_ORCHESTRATOR_RECONCILING,
    ACTIVITY_REVIEWER_WORKING,
    ATTENTION_OWNERS,
    DISPOSITION_LIVE,
    DISPOSITION_RESERVED,
    DISPOSITION_UNPROVABLE,
    MAX_SESSION_EVIDENCE,
    OPERATIONAL_ACTIVITY_STATES,
    OWNER_AGENT,
    OWNER_HUMAN,
    AttentionError,
    SessionEvidence,
    project_attention,
    require_activity,
    require_attention_owner,
    session_evidence,
)
from ai_dev_flow.session_binding import (
    BINDING_STATE_BOUND,
    BINDING_STATE_RESERVED,
    BINDING_STATE_UNBOUND,
    RailIteration,
    build_record,
)
from ai_dev_flow.session_lifecycle import STATE_DISCONNECTED, STATE_RUNNING

RAIL = "issue-55-attention-ownership-projection-foundation"
OTHER_RAIL = "issue-55-some-other-rail"
BLOB = "a" * 40
HEAD = "c" * 40
RESERVED_AT = "2026-08-31T11:30:00Z"
STARTED_AT = "2026-08-31T11:40:00Z"

SESSION_ONE = "1a2b3c4d-0001-4000-8000-00000000000a"
SESSION_TWO = "1a2b3c4d-0002-4000-8000-00000000000b"


def a_session(session_id=SESSION_ONE, *, role="executor",
              disposition=DISPOSITION_LIVE, pid=4242, domain="test-host"):
    return SessionEvidence(
        session_id=session_id, role=role, disposition=disposition,
        pid=pid, pid_domain=domain,
    )


def a_record(session_id=SESSION_ONE, *, rail=RAIL, role="executor",
             state=BINDING_STATE_BOUND):
    process = {}
    if state == BINDING_STATE_BOUND:
        process = {
            "pid": 4242, "pid_domain": "test-host",
            "started_at": STARTED_AT, "bound_at": STARTED_AT,
        }
    return build_record(
        project="ai-dev", ticket="issue-55",
        workspace_key="github:jmrozi1/ai-dev#55",
        worktree_id="worktree-55", workspace_path="/tmp/workspace-55",
        rail=rail, role=role, iteration=RailIteration(rail=rail, blob=BLOB),
        session_id=session_id, launched_at_head=HEAD, reserved_at=RESERVED_AT,
        state=state, **process
    )


class VocabularyTests(unittest.TestCase):
    def test_the_modeled_activities_cover_every_state_the_checkpoint_names(self) -> None:
        self.assertEqual(
            set(ACTIVITIES),
            {
                ACTIVITY_EXECUTOR_WORKING,
                ACTIVITY_REVIEWER_WORKING,
                ACTIVITY_ORCHESTRATOR_RECONCILING,
                ACTIVITY_MANAGER_LIFECYCLE,
                ACTIVITY_BLOCKED,
                ACTIVITY_DISCONNECTED_RECOVERY,
                ACTIVITY_CONTEXT_ROTATION,
            },
        )

    def test_attention_ownership_is_exactly_two_parties(self) -> None:
        self.assertEqual(ATTENTION_OWNERS, (OWNER_HUMAN, OWNER_AGENT))

    def test_every_activity_has_a_stated_operational_state_set(self) -> None:
        """No default branch anywhere: an unnamed activity is a hole, not a wildcard."""
        self.assertEqual(set(OPERATIONAL_ACTIVITY_STATES), set(ACTIVITIES))
        self.assertEqual(OPERATIONAL_ACTIVITY_STATES[ACTIVITY_BLOCKED], ())

    def test_an_unmodeled_activity_or_owner_is_refused(self) -> None:
        for value in ("thinking", "", None, "RUNNING"):
            with self.subTest(value=value):
                with self.assertRaises(AttentionError) as caught:
                    require_activity(value)
                self.assertEqual(caught.exception.reason, model.REASON_INVALID_ACTIVITY)
        for value in ("nobody", "orchestrator", None):
            with self.subTest(value=value):
                with self.assertRaises(AttentionError) as caught:
                    require_attention_owner(value)
                self.assertEqual(caught.exception.reason, model.REASON_INVALID_OWNER)


class DispositionTests(unittest.TestCase):
    def test_a_proved_handle_is_live_and_an_unproved_one_is_not_called_stopped(self) -> None:
        record = a_record()
        self.assertEqual(
            session_evidence(record, {SESSION_ONE: True}).disposition, DISPOSITION_LIVE
        )
        self.assertEqual(
            session_evidence(record, {SESSION_ONE: False}).disposition, DISPOSITION_UNPROVABLE
        )
        # Absent evidence for a bound record is the Disconnected reading too. It
        # never reads as "this session has stopped".
        self.assertEqual(session_evidence(record, {}).disposition, DISPOSITION_UNPROVABLE)

    def test_a_reservation_reads_as_reserved_rather_than_as_a_lost_handle(self) -> None:
        """`ownership_evidence` omits reservations by contract; absence is not failure."""
        reserved = a_record(state=BINDING_STATE_RESERVED)
        evidence = session_evidence(reserved, {})
        self.assertEqual(evidence.disposition, DISPOSITION_RESERVED)
        self.assertIsNone(evidence.pid)
        self.assertIn("no process attached", evidence.locator)

    def test_evidence_carries_transport_identity_and_says_what_it_is(self) -> None:
        evidence = session_evidence(a_record(), {SESSION_ONE: True})
        self.assertEqual(evidence.label, "live session")
        self.assertIn(SESSION_ONE, evidence.locator)
        self.assertIn("pid 4242", evidence.locator)
        self.assertIn("test-host", evidence.locator)

    def test_evidence_has_no_field_for_anything_a_session_said_or_did(self) -> None:
        self.assertEqual(
            {f.name for f in fields(SessionEvidence)},
            {"session_id", "role", "disposition", "pid", "pid_domain"},
        )


class ActivityTests(unittest.TestCase):
    def project(self, **overrides):
        arguments = dict(status="running", has_decision=False, sessions=())
        arguments.update(overrides)
        return project_attention(RAIL, **arguments)

    def test_a_live_session_projects_its_own_role_working(self) -> None:
        for role, activity in (
            ("executor", ACTIVITY_EXECUTOR_WORKING),
            ("reviewer", ACTIVITY_REVIEWER_WORKING),
            ("orchestrator", ACTIVITY_ORCHESTRATOR_RECONCILING),
        ):
            with self.subTest(role=role):
                found = self.project(sessions=(a_session(role=role),))
                self.assertEqual(found.activity, activity)
                self.assertEqual(found.attention_owner, OWNER_AGENT)

    def test_a_live_session_beside_a_replacement_is_a_rotation_not_a_second_item(self) -> None:
        for other in (DISPOSITION_RESERVED, DISPOSITION_UNPROVABLE):
            with self.subTest(other=other):
                found = self.project(sessions=(
                    a_session(SESSION_ONE),
                    a_session(SESSION_TWO, disposition=other),
                ))
                self.assertEqual(found.activity, ACTIVITY_CONTEXT_ROTATION)
                # One item, and both sessions are evidence beneath it.
                self.assertEqual(len(found.sessions), 2)
                self.assertEqual(len(found.live_sessions), 1)

    def test_an_unprovable_session_is_recovery_rather_than_absence(self) -> None:
        found = self.project(sessions=(a_session(disposition=DISPOSITION_UNPROVABLE),))
        self.assertEqual(found.activity, ACTIVITY_DISCONNECTED_RECOVERY)

    def test_a_reservation_alone_is_the_controller_s_own_lifecycle(self) -> None:
        found = self.project(sessions=(a_session(disposition=DISPOSITION_RESERVED),))
        self.assertEqual(found.activity, ACTIVITY_MANAGER_LIFECYCLE)

    def test_an_unprovable_session_outranks_a_reservation_beside_it(self) -> None:
        """The projection fails toward the more visible reading, never the calmer one."""
        found = self.project(sessions=(
            a_session(SESSION_ONE, disposition=DISPOSITION_UNPROVABLE),
            a_session(SESSION_TWO, disposition=DISPOSITION_RESERVED),
        ))
        self.assertEqual(found.activity, ACTIVITY_DISCONNECTED_RECOVERY)

    def test_a_blocked_rail_with_a_live_session_is_blocked_not_working(self) -> None:
        found = self.project(status="blocked", has_decision=True,
                             sessions=(a_session(),))
        self.assertEqual(found.activity, ACTIVITY_BLOCKED)

    def test_a_rail_with_no_sessions_and_no_decision_is_not_a_work_item(self) -> None:
        """A rail nobody is executing and nobody is waiting on is not a row."""
        for status in ("ready", "running", "completed"):
            with self.subTest(status=status):
                self.assertIsNone(self.project(status=status))

    def test_a_published_decision_is_a_work_item_even_with_nothing_executing(self) -> None:
        found = self.project(status="blocked", has_decision=True)
        self.assertEqual(found.activity, ACTIVITY_BLOCKED)
        self.assertEqual(found.attention_owner, OWNER_HUMAN)

    def test_a_decision_on_a_rail_with_no_modeled_activity_still_produces_an_item(self) -> None:
        """Somebody is owed an answer whether or not any process exists to answer it."""
        found = self.project(status="running", has_decision=True)
        self.assertEqual(found.activity, ACTIVITY_BLOCKED)
        self.assertEqual(found.attention_owner, OWNER_HUMAN)

    def test_an_unmodeled_role_refuses_rather_than_inventing_an_activity(self) -> None:
        with self.assertRaises(AttentionError) as caught:
            self.project(sessions=(a_session(role="janitor"),))
        self.assertEqual(caught.exception.reason, model.REASON_UNKNOWN_ROLE)


class ContradictionTests(unittest.TestCase):
    def test_two_provably_live_sessions_on_one_rail_fail_closed(self) -> None:
        with self.assertRaises(AttentionError) as caught:
            project_attention(
                RAIL, status="running", has_decision=False,
                sessions=(a_session(SESSION_ONE), a_session(SESSION_TWO)),
            )
        self.assertEqual(caught.exception.reason, model.REASON_CONTRADICTORY_OWNERSHIP)
        self.assertIn(SESSION_ONE, caught.exception.detail)
        self.assertIn(SESSION_TWO, caught.exception.detail)

    def test_the_refusal_says_concurrent_work_uses_distinct_rails(self) -> None:
        """The fix is a second rail, not a merged row, and the refusal says so."""
        with self.assertRaises(AttentionError) as caught:
            project_attention(
                RAIL, status="running", has_decision=False,
                sessions=(a_session(SESSION_ONE, role="executor"),
                          a_session(SESSION_TWO, role="reviewer")),
            )
        self.assertIn("distinct rails", caught.exception.detail)

    def test_evidence_beyond_the_bound_refuses_rather_than_truncating(self) -> None:
        sessions = tuple(
            a_session("1a2b3c4d-{0:04d}-4000-8000-00000000000a".format(index),
                      disposition=DISPOSITION_UNPROVABLE)
            for index in range(MAX_SESSION_EVIDENCE + 1)
        )
        with self.assertRaises(AttentionError) as caught:
            project_attention(RAIL, status="running", has_decision=False, sessions=sessions)
        self.assertEqual(caught.exception.reason, model.REASON_UNBOUNDED_EVIDENCE)


class IndependenceTests(unittest.TestCase):
    """Activity and attention ownership are two facts, and neither derives the other."""

    def test_activity_changes_while_the_attention_owner_does_not(self) -> None:
        parked = project_attention(
            RAIL, status="blocked", has_decision=True, sessions=(a_session(),)
        )
        lost = project_attention(
            RAIL, status="blocked", has_decision=True,
            sessions=(a_session(disposition=DISPOSITION_UNPROVABLE),),
        )
        self.assertNotEqual(parked.activity, lost.activity)
        self.assertEqual(parked.activity, ACTIVITY_BLOCKED)
        self.assertEqual(lost.activity, ACTIVITY_DISCONNECTED_RECOVERY)
        self.assertEqual(parked.attention_owner, OWNER_HUMAN)
        self.assertEqual(lost.attention_owner, OWNER_HUMAN)

    def test_the_attention_owner_changes_while_the_activity_does_not(self) -> None:
        owed_to_a_person = project_attention(
            RAIL, status="blocked", has_decision=True,
            sessions=(a_session(disposition=DISPOSITION_UNPROVABLE),),
        )
        owed_to_the_system = project_attention(
            OTHER_RAIL, status="running", has_decision=False,
            sessions=(a_session(disposition=DISPOSITION_UNPROVABLE),),
        )
        self.assertEqual(owed_to_a_person.activity, owed_to_the_system.activity)
        self.assertEqual(owed_to_a_person.activity, ACTIVITY_DISCONNECTED_RECOVERY)
        self.assertNotEqual(
            owed_to_a_person.attention_owner, owed_to_the_system.attention_owner
        )

    def test_the_owner_is_the_published_record_and_nothing_about_the_sessions(self) -> None:
        """Every session shape, one decision record: the owner never moves."""
        for sessions in (
            (),
            (a_session(),),
            (a_session(disposition=DISPOSITION_RESERVED),),
            (a_session(disposition=DISPOSITION_UNPROVABLE),),
            (a_session(SESSION_ONE), a_session(SESSION_TWO, disposition=DISPOSITION_RESERVED)),
        ):
            with self.subTest(sessions=len(sessions)):
                self.assertEqual(
                    project_attention(
                        RAIL, status="blocked", has_decision=True, sessions=sessions
                    ).attention_owner,
                    OWNER_HUMAN,
                )
                found = project_attention(
                    RAIL, status="running", has_decision=False, sessions=sessions
                )
                if found is not None:
                    self.assertEqual(found.attention_owner, OWNER_AGENT)

    def test_the_activity_function_is_never_told_whether_a_decision_exists(self) -> None:
        """Read from the signature, not from prose: it has nowhere to put the answer."""
        source = Path(model.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        activity = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_activity"
        )
        names = [arg.arg for arg in activity.args.args + activity.args.kwonlyargs]
        self.assertEqual(names, ["rail", "status", "sessions"])
        for forbidden in ("has_decision", "decision", "owner", "attention"):
            self.assertNotIn(forbidden, names)

    def test_the_owner_function_is_never_told_what_the_sessions_are_doing(self) -> None:
        source = Path(model.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        owner = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_attention_owner"
        )
        names = [arg.arg for arg in owner.args.args + owner.args.kwonlyargs]
        self.assertEqual(names, ["has_decision"])


class DeterminismAndPurityTests(unittest.TestCase):
    def test_session_order_never_changes_the_answer(self) -> None:
        forward = project_attention(
            RAIL, status="running", has_decision=False,
            sessions=(a_session(SESSION_ONE),
                      a_session(SESSION_TWO, disposition=DISPOSITION_UNPROVABLE)),
        )
        backward = project_attention(
            RAIL, status="running", has_decision=False,
            sessions=(a_session(SESSION_TWO, disposition=DISPOSITION_UNPROVABLE),
                      a_session(SESSION_ONE)),
        )
        self.assertEqual(forward, backward)

    def test_a_terminal_record_is_the_caller_s_to_exclude_not_this_module_s_to_guess(self) -> None:
        """`session_evidence` describes what it is handed; filtering is upstream."""
        terminal = a_record(state=BINDING_STATE_UNBOUND)
        self.assertEqual(
            session_evidence(terminal, {}).disposition, DISPOSITION_UNPROVABLE
        )

    def test_the_module_imports_only_accepted_pure_vocabulary(self) -> None:
        tree = ast.parse(Path(model.__file__).read_text(encoding="utf-8"))
        allowed = {"__future__", "dataclasses", "typing",
                   ".session_binding", ".session_lifecycle"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name, allowed, alias.name)
            elif isinstance(node, ast.ImportFrom):
                name = ("." * (node.level or 0)) + (node.module or "")
                self.assertIn(name, allowed, name)

    def test_the_module_touches_no_clock_process_file_or_network(self) -> None:
        tree = ast.parse(Path(model.__file__).read_text(encoding="utf-8"))
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    called.add(func.id)
                elif isinstance(func, ast.Attribute):
                    called.add(func.attr)
        for surface in ("open", "run", "Popen", "write", "read_text", "urlopen",
                        "now", "utcnow", "monotonic", "time", "sleep", "Thread",
                        "getpid", "kill", "process_group_alive", "observe_session",
                        "require_owned", "records", "publish"):
            self.assertNotIn(surface, called, surface)

    def test_the_public_surface_offers_no_action(self) -> None:
        for name in [n for n in dir(model) if not n.startswith("_")]:
            for forbidden in ("send", "write", "publish", "launch", "stop", "adopt",
                              "rotate", "schedule", "poll"):
                self.assertNotIn(forbidden, name.lower(), name)


class OperationalStateTableTests(unittest.TestCase):
    def test_working_and_rotation_describe_running_items_only(self) -> None:
        for activity in (ACTIVITY_EXECUTOR_WORKING, ACTIVITY_REVIEWER_WORKING,
                         ACTIVITY_ORCHESTRATOR_RECONCILING, ACTIVITY_CONTEXT_ROTATION):
            self.assertEqual(OPERATIONAL_ACTIVITY_STATES[activity], (STATE_RUNNING,))

    def test_recovery_and_launch_describe_disconnected_items_only(self) -> None:
        for activity in (ACTIVITY_DISCONNECTED_RECOVERY, ACTIVITY_MANAGER_LIFECYCLE):
            self.assertEqual(OPERATIONAL_ACTIVITY_STATES[activity], (STATE_DISCONNECTED,))


if __name__ == "__main__":
    unittest.main()

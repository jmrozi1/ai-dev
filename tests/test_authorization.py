from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from ai_dev_flow import authorization
from ai_dev_flow.authorization import (
    AgentSlots,
    CONCURRENCY_CEILING_DEFAULT,
    reconcile_agent_slots,
    ACTION_CONTINUE,
    ACTION_LAUNCH,
    OBSERVATION_PARTIAL,
    SOURCE_HEALTH_DIVERGED,
    SOURCE_HEALTH_UNKNOWN,
    SOURCE_HEALTH_UNPUSHED,
    ControlPlaneObservation,
    RailObservation,
    WorkspaceObservation,
    authorize,
)
from ai_dev_flow.session_binding import (
    BINDING_STATE_BOUND,
    BINDING_STATE_RESERVED,
    BINDING_STATE_UNBOUND,
    BindingRecord,
    RailIteration,
)


RAIL = "issue-55-binding-authorization-foundation"
OTHER_RAIL = "issue-55-orchestrator-role-contract"
RESOURCE = "issue-55-linux-product-worktree"
HEAD = "c" * 40
BLOB = "a" * 40
OTHER_BLOB = "b" * 40
SESSION = "1a2b3c4d-0001-4000-8000-00000000000a"
OTHER_SESSION = "1a2b3c4d-0002-4000-8000-00000000000b"
WORKSPACE_KEY = "github:owner/repo#55"
WORKTREE_ID = "ai-dev-issue-55"
WORKSPACE_PATH = "/workspaces/ai-dev-issue-55"


def rail_state(**overrides: object) -> RailObservation:
    arguments: dict = {
        "identifier": RAIL,
        "status": "running",
        "rail_blob": BLOB,
        # Stated, never defaulted: these fixtures represent an actionable rail, and
        # the durable assignment is now part of what makes it actionable.
        "role": "executor",
        "unreconciled": False,
        "depends_on": (),
        "shared_resource": RESOURCE,
    }
    arguments.update(overrides)
    return RailObservation(**arguments)  # type: ignore[arg-type]


def workspace_state(**overrides: object) -> WorkspaceObservation:
    arguments: dict = {
        "workspace_key": WORKSPACE_KEY,
        "worktree_id": WORKTREE_ID,
        "workspace_path": WORKSPACE_PATH,
        "identity_problem": None,
    }
    arguments.update(overrides)
    return WorkspaceObservation(**arguments)  # type: ignore[arg-type]


def observation(**overrides: object) -> ControlPlaneObservation:
    arguments: dict = {
        "project": "ai-dev",
        "ticket": "issue-55",
        "head": HEAD,
        "rails": (rail_state(),),
        "workspace": workspace_state(),
    }
    arguments.update(overrides)
    return ControlPlaneObservation(**arguments)  # type: ignore[arg-type]


def reserved(**overrides: object) -> BindingRecord:
    """A launch this controller committed to but has not yet attached a process to."""
    arguments: dict = {
        "project": "ai-dev",
        "ticket": "issue-55",
        "workspace_key": WORKSPACE_KEY,
        "worktree_id": WORKTREE_ID,
        "workspace_path": WORKSPACE_PATH,
        "rail": RAIL,
        "role": "executor",
        "iteration": RailIteration(rail=RAIL, blob=BLOB),
        "session_id": SESSION,
        "launched_at_head": HEAD,
        "reserved_at": "2026-08-26T12:00:00Z",
        "state": BINDING_STATE_RESERVED,
    }
    arguments.update(overrides)
    return BindingRecord(**arguments)  # type: ignore[arg-type]


def binding(**overrides: object) -> BindingRecord:
    """A reservation whose spawn succeeded and reported process identity back."""
    arguments: dict = {
        "state": BINDING_STATE_BOUND,
        "pid": 4242,
        "pid_domain": "test-host",
        "started_at": "2026-08-26T12:00:02Z",
        "bound_at": "2026-08-26T12:00:03Z",
    }
    arguments.update(overrides)
    return reserved(**arguments)


def decide(observed: ControlPlaneObservation, **overrides: object):
    arguments: dict = {
        "project": "ai-dev",
        "ticket": "issue-55",
        "rail": RAIL,
        "role": "executor",
        "expected_head": HEAD,
        "rail_blob": BLOB,
        "bindings": (),
        "in_flight_session_ids": (),
        # Room to admit, so every pre-existing case still exercises the reason it
        # was written for rather than the ceiling.
        "slots": AgentSlots(ceiling=authorization.CONCURRENCY_CEILING_DEFAULT),
    }
    arguments.update(overrides)
    return authorize(observed, **arguments)  # type: ignore[arg-type]


class AuthorizationDecisionTests(unittest.TestCase):
    # Launch versus continuation

    def test_running_rail_with_no_binding_authorizes_launch(self) -> None:
        decision = decide(observation())
        self.assertTrue(decision)
        self.assertTrue(decision.authorized)
        self.assertEqual(decision.action, ACTION_LAUNCH)
        self.assertEqual(decision.reason, authorization.REASON_LAUNCH_AUTHORIZED)
        self.assertEqual(decision.iteration, RailIteration(rail=RAIL, blob=BLOB))
        self.assertIsNone(decision.session_id)
        self.assertEqual(decision.head, HEAD)

    def test_running_rail_with_one_matching_binding_authorizes_continuation(self) -> None:
        decision = decide(observation(), bindings=(binding(),))
        self.assertTrue(decision.authorized)
        self.assertEqual(decision.action, ACTION_CONTINUE)
        self.assertEqual(decision.reason, authorization.REASON_CONTINUATION_AUTHORIZED)
        self.assertEqual(decision.session_id, SESSION)

    def test_a_reserved_binding_authorizes_nothing(self) -> None:
        # It is neither an empty rail waiting for a launch nor a session that can
        # be resumed: the spawn it names has not reported back yet.
        decision = decide(observation(), bindings=(reserved(),))
        self.assertFalse(decision.authorized)
        self.assertIsNone(decision.action)
        self.assertIsNone(decision.session_id)
        self.assertEqual(decision.reason, authorization.REASON_BINDING_NOT_READY)
        self.assertIn(SESSION, decision.detail)

    def test_a_reserved_binding_blocks_a_second_launch_with_the_same_reason(self) -> None:
        # The caller that would launch and the caller that would continue both ask
        # the same question, so both must get the same stable refusal.
        launch_attempt = decide(observation(), bindings=(reserved(),))
        continue_attempt = decide(
            observation(), bindings=(reserved(),), in_flight_session_ids=(SESSION,)
        )
        self.assertEqual(launch_attempt.reason, authorization.REASON_BINDING_NOT_READY)
        self.assertEqual(continue_attempt.reason, authorization.REASON_BINDING_NOT_READY)

    def test_a_reservation_becomes_continuable_only_once_it_is_bound(self) -> None:
        self.assertEqual(
            decide(observation(), bindings=(reserved(),)).reason,
            authorization.REASON_BINDING_NOT_READY,
        )
        promoted = decide(observation(), bindings=(binding(),))
        self.assertTrue(promoted.authorized)
        self.assertEqual(promoted.action, ACTION_CONTINUE)
        self.assertEqual(promoted.session_id, SESSION)

    def test_an_unbound_reservation_frees_the_rail_for_a_new_launch(self) -> None:
        decision = decide(
            observation(), bindings=(reserved(state=BINDING_STATE_UNBOUND),)
        )
        self.assertTrue(decision.authorized)
        self.assertEqual(decision.action, ACTION_LAUNCH)

    def test_a_reserved_binding_for_another_rail_does_not_block_this_one(self) -> None:
        decision = decide(
            observation(),
            bindings=(
                reserved(session_id=OTHER_SESSION, rail=OTHER_RAIL,
                         iteration=RailIteration(rail=OTHER_RAIL, blob=OTHER_BLOB)),
            ),
        )
        self.assertTrue(decision.authorized)
        self.assertEqual(decision.action, ACTION_LAUNCH)

    def test_a_reserved_binding_at_another_iteration_is_a_mismatch_not_a_wait(self) -> None:
        decision = decide(
            observation(),
            bindings=(reserved(iteration=RailIteration(rail=RAIL, blob=OTHER_BLOB)),),
        )
        self.assertFalse(decision.authorized)
        self.assertEqual(decision.reason, authorization.REASON_BINDING_MISMATCHED)

    def test_a_reserved_and_a_bound_binding_on_one_rail_are_duplicates(self) -> None:
        decision = decide(
            observation(),
            bindings=(reserved(), binding(session_id=OTHER_SESSION)),
        )
        self.assertFalse(decision.authorized)
        self.assertEqual(decision.reason, authorization.REASON_BINDING_DUPLICATED)

    def test_terminal_bindings_do_not_block_a_relaunch(self) -> None:
        decision = decide(
            observation(), bindings=(binding(state=BINDING_STATE_UNBOUND),)
        )
        self.assertTrue(decision.authorized)
        self.assertEqual(decision.action, ACTION_LAUNCH)

    def test_bindings_for_other_rails_or_tickets_are_ignored(self) -> None:
        decision = decide(
            observation(),
            bindings=(
                binding(session_id=OTHER_SESSION, rail=OTHER_RAIL,
                        iteration=RailIteration(rail=OTHER_RAIL, blob=OTHER_BLOB)),
            ),
        )
        self.assertTrue(decision.authorized)
        self.assertEqual(decision.action, ACTION_LAUNCH)

    # Rail status

    def test_ready_blocked_and_completed_never_authorize(self) -> None:
        for status in ("ready", "blocked", "completed"):
            with self.subTest(status=status):
                decision = decide(observation(rails=(rail_state(status=status),)))
                self.assertFalse(decision.authorized)
                self.assertIsNone(decision.action)
                self.assertEqual(decision.reason, authorization.REASON_RAIL_NOT_DISPATCHED)

    def test_ready_does_not_authorize_launch_even_with_no_binding(self) -> None:
        decision = decide(observation(rails=(rail_state(status="ready"),)), bindings=())
        self.assertFalse(decision.authorized)
        self.assertEqual(decision.reason, authorization.REASON_RAIL_NOT_DISPATCHED)

    def test_unreconciled_rail_is_refused(self) -> None:
        decision = decide(observation(rails=(rail_state(unreconciled=True),)))
        self.assertFalse(decision.authorized)
        self.assertEqual(decision.reason, authorization.REASON_RAIL_UNRECONCILED)

    def test_missing_rail_is_refused(self) -> None:
        decision = decide(observation(rails=(rail_state(identifier=OTHER_RAIL),)))
        self.assertFalse(decision.authorized)
        self.assertEqual(decision.reason, authorization.REASON_RAIL_MISSING)

    def test_duplicated_rail_is_refused(self) -> None:
        decision = decide(observation(rails=(rail_state(), rail_state())))
        self.assertFalse(decision.authorized)
        self.assertEqual(decision.reason, authorization.REASON_RAIL_DUPLICATED)

    # Freshness, completeness, and source health

    def test_partial_observation_is_refused(self) -> None:
        decision = decide(observation(completeness=OBSERVATION_PARTIAL))
        self.assertFalse(decision.authorized)
        self.assertEqual(decision.reason, authorization.REASON_OBSERVATION_INCOMPLETE)

    def test_unhealthy_source_is_refused(self) -> None:
        for health in (SOURCE_HEALTH_UNPUSHED, SOURCE_HEALTH_DIVERGED, SOURCE_HEALTH_UNKNOWN):
            with self.subTest(health=health):
                decision = decide(observation(source_health=health))
                self.assertFalse(decision.authorized)
                self.assertEqual(decision.reason, authorization.REASON_SOURCE_UNHEALTHY)

    def test_stale_head_is_refused(self) -> None:
        decision = decide(observation(head="d" * 40))
        self.assertFalse(decision.authorized)
        self.assertEqual(decision.reason, authorization.REASON_HEAD_MISMATCH)

    def test_empty_head_is_refused(self) -> None:
        decision = decide(observation(head=""), expected_head="")
        self.assertFalse(decision.authorized)
        self.assertEqual(decision.reason, authorization.REASON_HEAD_MISMATCH)

    def test_iteration_mismatch_is_refused(self) -> None:
        decision = decide(observation(rails=(rail_state(rail_blob=OTHER_BLOB),)))
        self.assertFalse(decision.authorized)
        self.assertEqual(decision.reason, authorization.REASON_ITERATION_MISMATCH)

    def test_observation_of_another_scope_is_refused(self) -> None:
        decision = decide(observation(ticket="issue-56"))
        self.assertFalse(decision.authorized)
        self.assertEqual(decision.reason, authorization.REASON_SCOPE_MISMATCH)

    # Dependencies and shared resources

    def test_unsatisfied_dependency_is_refused(self) -> None:
        decision = decide(
            observation(
                rails=(
                    rail_state(depends_on=(OTHER_RAIL,)),
                    rail_state(identifier=OTHER_RAIL, status="running",
                               shared_resource=None, rail_blob=OTHER_BLOB),
                )
            )
        )
        self.assertFalse(decision.authorized)
        self.assertEqual(decision.reason, authorization.REASON_DEPENDENCY_UNSATISFIED)

    def test_unknown_dependency_is_refused(self) -> None:
        decision = decide(observation(rails=(rail_state(depends_on=("issue-55-absent-rail",)),)))
        self.assertFalse(decision.authorized)
        self.assertEqual(decision.reason, authorization.REASON_DEPENDENCY_UNSATISFIED)

    def test_completed_dependency_is_satisfied(self) -> None:
        decision = decide(
            observation(
                rails=(
                    rail_state(depends_on=(OTHER_RAIL,)),
                    rail_state(identifier=OTHER_RAIL, status="completed",
                               shared_resource=None, rail_blob=OTHER_BLOB),
                )
            )
        )
        self.assertTrue(decision.authorized)

    def test_in_scope_resource_contention_is_refused(self) -> None:
        decision = decide(
            observation(
                rails=(
                    rail_state(),
                    rail_state(identifier=OTHER_RAIL, status="running", rail_blob=OTHER_BLOB),
                )
            )
        )
        self.assertFalse(decision.authorized)
        self.assertEqual(decision.reason, authorization.REASON_RESOURCE_CONTENDED)
        self.assertIn(OTHER_RAIL, decision.detail)

    def test_cross_scope_resource_contention_is_refused(self) -> None:
        decision = decide(
            observation(foreign_resource_holders={RESOURCE: ("ai-dev/issue-57:some-rail",)})
        )
        self.assertFalse(decision.authorized)
        self.assertEqual(decision.reason, authorization.REASON_RESOURCE_CONTENDED)
        self.assertIn("issue-57", decision.detail)

    def test_a_non_running_sibling_on_the_same_resource_does_not_contend(self) -> None:
        decision = decide(
            observation(
                rails=(
                    rail_state(),
                    rail_state(identifier=OTHER_RAIL, status="completed", rail_blob=OTHER_BLOB),
                )
            )
        )
        self.assertTrue(decision.authorized)

    # Workspace identity

    def test_absent_workspace_identity_is_refused(self) -> None:
        decision = decide(observation(workspace=None))
        self.assertFalse(decision.authorized)
        self.assertEqual(
            decision.reason, authorization.REASON_WORKSPACE_IDENTITY_AMBIGUOUS
        )

    def test_unproven_workspace_identity_is_refused_with_its_own_detail(self) -> None:
        decision = decide(
            observation(
                workspace=workspace_state(
                    identity_problem="the active ticket github:owner/repo#55 is owned by "
                    "workspace /elsewhere, not this one."
                )
            )
        )
        self.assertFalse(decision.authorized)
        self.assertEqual(
            decision.reason, authorization.REASON_WORKSPACE_IDENTITY_AMBIGUOUS
        )
        self.assertIn("/elsewhere", decision.detail)

    # Bindings

    def test_two_live_bindings_for_one_rail_are_refused(self) -> None:
        decision = decide(
            observation(),
            bindings=(binding(), binding(session_id=OTHER_SESSION)),
        )
        self.assertFalse(decision.authorized)
        self.assertEqual(decision.reason, authorization.REASON_BINDING_DUPLICATED)

    def test_duplicate_session_ids_across_bindings_are_refused(self) -> None:
        decision = decide(observation(), bindings=(binding(), binding(rail=OTHER_RAIL)))
        self.assertFalse(decision.authorized)
        self.assertEqual(decision.reason, authorization.REASON_BINDING_DUPLICATED)

    def test_binding_at_another_iteration_refuses_rather_than_rebinding_in_place(self) -> None:
        decision = decide(
            observation(),
            bindings=(binding(iteration=RailIteration(rail=RAIL, blob=OTHER_BLOB)),),
        )
        self.assertFalse(decision.authorized)
        self.assertEqual(decision.reason, authorization.REASON_BINDING_MISMATCHED)

    def test_binding_with_another_role_is_refused(self) -> None:
        decision = decide(observation(), bindings=(binding(role="reviewer"),))
        self.assertFalse(decision.authorized)
        self.assertEqual(decision.reason, authorization.REASON_BINDING_MISMATCHED)

    def test_binding_to_another_workspace_is_refused(self) -> None:
        for field, value in (
            ("workspace_key", "github:owner/repo#56"),
            ("worktree_id", "ai-dev-issue-56"),
        ):
            with self.subTest(field=field):
                decision = decide(observation(), bindings=(binding(**{field: value}),))
                self.assertFalse(decision.authorized)
                self.assertEqual(decision.reason, authorization.REASON_BINDING_MISMATCHED)

    def test_an_invocation_in_flight_refuses_continuation(self) -> None:
        decision = decide(
            observation(), bindings=(binding(),), in_flight_session_ids=(SESSION,)
        )
        self.assertFalse(decision.authorized)
        self.assertEqual(decision.reason, authorization.REASON_INVOCATION_IN_FLIGHT)

    def test_an_unrelated_in_flight_session_does_not_block(self) -> None:
        decision = decide(
            observation(), bindings=(binding(),), in_flight_session_ids=(OTHER_SESSION,)
        )
        self.assertTrue(decision.authorized)

    def test_unsupported_role_is_refused(self) -> None:
        decision = decide(observation(), role="controller")
        self.assertFalse(decision.authorized)
        self.assertEqual(decision.reason, authorization.REASON_INVALID_ROLE)


class DurableRoleTests(unittest.TestCase):
    """A caller may request a role; it may not invent one the rail never granted."""

    def test_a_rail_with_no_durable_role_authorizes_nothing(self) -> None:
        decision = decide(observation(rails=(rail_state(role=None),)))
        self.assertFalse(decision.authorized)
        self.assertEqual(decision.reason, authorization.REASON_RAIL_ROLE_MISSING)
        self.assertIsNone(decision.action)

    def test_a_non_managed_rail_role_refuses_by_mismatch_and_never_widens_the_vocabulary(self):
        decision = decide(observation(rails=(rail_state(role="evidence-worker"),)))
        self.assertFalse(decision.authorized)
        self.assertEqual(decision.reason, authorization.REASON_RAIL_ROLE_MISMATCH)
        self.assertIn("evidence-worker", decision.detail)
        self.assertNotIn("evidence-worker", authorization.BINDING_ROLES)

    def test_a_rail_assigned_to_another_managed_role_refuses(self) -> None:
        decision = decide(observation(rails=(rail_state(role="orchestrator"),)), role="executor")
        self.assertFalse(decision.authorized)
        self.assertEqual(decision.reason, authorization.REASON_RAIL_ROLE_MISMATCH)

    def test_an_exact_match_still_authorizes_launch(self) -> None:
        for role in authorization.BINDING_ROLES:
            with self.subTest(role=role):
                decision = decide(observation(rails=(rail_state(role=role),)), role=role)
                self.assertTrue(decision.authorized)
                self.assertEqual(decision.action, ACTION_LAUNCH)

    def test_role_is_checked_before_dispatch_binding_and_reservation(self) -> None:
        """Whoever a rail assigns is settled before anything the rail permits."""
        decision = decide(
            observation(rails=(rail_state(role="reviewer", status="ready", unreconciled=True),)),
            role="executor",
        )
        self.assertEqual(decision.reason, authorization.REASON_RAIL_ROLE_MISMATCH)

    def test_a_role_mismatch_refuses_continuation_too(self) -> None:
        decision = decide(
            observation(rails=(rail_state(role="reviewer"),)),
            role="executor",
            bindings=(binding(),),
        )
        self.assertFalse(decision.authorized)
        self.assertEqual(decision.reason, authorization.REASON_RAIL_ROLE_MISMATCH)

    def test_a_role_refusal_mutates_no_binding(self) -> None:
        existing = binding()
        before = existing.to_dict()
        decision = decide(observation(rails=(rail_state(role=None),)), bindings=(existing,))
        self.assertFalse(decision.authorized)
        self.assertEqual(existing.to_dict(), before)

    def test_the_requested_role_is_still_validated_against_the_managed_vocabulary(self) -> None:
        decision = decide(observation(rails=(rail_state(role="controller"),)), role="controller")
        self.assertEqual(decision.reason, authorization.REASON_INVALID_ROLE)


class AuthorizationPurityTests(unittest.TestCase):
    """A refusal -- and an approval -- must change nothing anywhere."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name).resolve()

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _tree(self) -> list:
        return sorted(
            (str(path.relative_to(self.tmp_path)), path.stat().st_mtime_ns)
            for path in self.tmp_path.rglob("*")
        )

    def test_authorize_performs_no_process_git_or_file_write(self) -> None:
        (self.tmp_path / "bindings").mkdir()
        (self.tmp_path / "bindings" / "existing.json").write_text("{}", encoding="utf-8")
        before = self._tree()

        cases = (
            {},                                                     # launch
            {"bindings": (binding(),)},                             # continuation
            {"bindings": (reserved(),)},                            # not-ready refusal
            {"rail": "issue-55-absent-rail"},                       # refusal
            {"expected_head": "d" * 40},                            # refusal
        )
        with patch.object(subprocess, "run") as run, \
                patch.object(subprocess, "Popen") as popen, \
                patch.object(os, "replace") as replace_call, \
                patch.object(Path, "write_text") as write_text, \
                patch.object(Path, "mkdir") as mkdir, \
                patch("builtins.open") as opened:
            for case in cases:
                decide(observation(), **case)

        run.assert_not_called()
        popen.assert_not_called()
        replace_call.assert_not_called()
        write_text.assert_not_called()
        mkdir.assert_not_called()
        opened.assert_not_called()
        self.assertEqual(self._tree(), before)

    def test_the_decision_is_immutable(self) -> None:
        decision = decide(observation())
        with self.assertRaises(Exception):
            decision.authorized = False  # type: ignore[misc]
        self.assertTrue(replace(decision, authorized=False).authorized is False)
        self.assertTrue(decision.authorized)

    def test_every_refusal_carries_exactly_one_stable_reason_and_no_action(self) -> None:
        refusals = (
            decide(observation(), role="controller"),
            decide(observation(ticket="issue-56")),
            decide(observation(completeness=OBSERVATION_PARTIAL)),
            decide(observation(source_health=SOURCE_HEALTH_UNPUSHED)),
            decide(observation(head="d" * 40)),
            decide(observation(rails=())),
            decide(observation(rails=(rail_state(), rail_state()))),
            decide(observation(rails=(rail_state(unreconciled=True),))),
            decide(observation(rails=(rail_state(status="ready"),))),
            decide(observation(rails=(rail_state(rail_blob=OTHER_BLOB),))),
            decide(observation(rails=(rail_state(depends_on=("issue-55-absent-rail",)),))),
            decide(observation(foreign_resource_holders={RESOURCE: ("elsewhere",)})),
            decide(observation(workspace=None)),
            decide(observation(), bindings=(binding(), binding(session_id=OTHER_SESSION))),
            decide(observation(), bindings=(binding(role="reviewer"),)),
            decide(observation(), bindings=(binding(),), in_flight_session_ids=(SESSION,)),
            decide(observation(), bindings=(reserved(),)),
        )
        seen = set()
        for decision in refusals:
            self.assertFalse(decision.authorized, msg=decision.reason)
            self.assertIsNone(decision.action)
            self.assertIsNone(decision.session_id)
            self.assertTrue(decision.detail.strip(), msg=decision.reason)
            seen.add(decision.reason)
        self.assertEqual(len(seen), len(refusals))


class ConcurrencyAdmissionTests(unittest.TestCase):
    """Accepted decision D6: a hard manager-wide admission ceiling."""

    def slots(self, **overrides):
        arguments = {"ceiling": CONCURRENCY_CEILING_DEFAULT}
        arguments.update(overrides)
        return AgentSlots(**arguments)

    def occupied(self, count, ceiling=CONCURRENCY_CEILING_DEFAULT):
        return AgentSlots(
            ceiling=ceiling,
            occupants=tuple("session-{0}".format(index) for index in range(count)),
        )

    def test_the_default_ceiling_is_six(self) -> None:
        self.assertEqual(CONCURRENCY_CEILING_DEFAULT, 6)
        self.assertEqual(reconcile_agent_slots((), ownership={}).ceiling, 6)

    def test_a_valid_explicit_ceiling_is_honored(self) -> None:
        at_two = decide(observation(), slots=self.occupied(2, ceiling=2))
        self.assertFalse(at_two.authorized)
        self.assertEqual(at_two.reason, authorization.REASON_CONCURRENCY_CEILING)
        below = decide(observation(), slots=self.occupied(1, ceiling=2))
        self.assertTrue(below.authorized)
        self.assertEqual(below.ceiling, 2)

    def test_a_malformed_ceiling_is_a_caller_fault(self) -> None:
        for bad in (0, -1, True, "6", 1.5, None):
            with self.subTest(ceiling=bad):
                decision = decide(observation(), slots=AgentSlots(ceiling=bad))
                self.assertFalse(decision.authorized)
                self.assertEqual(decision.reason, authorization.REASON_INVALID_CEILING)

    def test_every_managed_role_occupies_the_same_ceiling(self) -> None:
        records = tuple(
            binding(session_id="0000000{0}-0000-4000-8000-000000000000".format(index), role=role)
            for index, role in enumerate(("executor", "reviewer", "orchestrator"))
        )
        slots = reconcile_agent_slots(
            records, ownership={record.session_id: True for record in records}
        )
        self.assertEqual(slots.occupied, 3)
        self.assertEqual(slots.unprovable, ())

    def test_a_launch_is_admitted_below_the_ceiling(self) -> None:
        decision = decide(observation(), slots=self.occupied(5))
        self.assertTrue(decision.authorized)
        self.assertEqual(decision.action, ACTION_LAUNCH)

    def test_a_launch_is_refused_at_the_ceiling_and_names_no_action(self) -> None:
        decision = decide(observation(), slots=self.occupied(6))
        self.assertFalse(decision.authorized)
        self.assertEqual(decision.reason, authorization.REASON_CONCURRENCY_CEILING)
        # The predicate is pure, so refusing here refuses before any reservation,
        # process, or provider action can exist.
        self.assertIsNone(decision.action)
        self.assertIsNone(decision.session_id)

    def test_an_unprovable_count_refuses_a_launch_and_a_continuation_alike(self) -> None:
        unknown = self.slots(occupants=("a",), unprovable=("b",))
        launch = decide(observation(), slots=unknown)
        continuation = decide(observation(), bindings=(binding(),), slots=unknown)
        for decision in (launch, continuation):
            self.assertFalse(decision.authorized)
            self.assertEqual(decision.reason, authorization.REASON_CONCURRENCY_UNPROVABLE)
            self.assertIsNone(decision.action)

    def test_continuing_a_session_already_counted_is_admitted_at_the_ceiling(self) -> None:
        full = AgentSlots(
            ceiling=6,
            occupants=tuple(["session-{0}".format(index) for index in range(5)] + [SESSION]),
        )
        decision = decide(observation(), bindings=(binding(),), slots=full)
        self.assertTrue(decision.authorized, msg=decision.reason)
        self.assertEqual(decision.action, ACTION_CONTINUE)
        self.assertEqual(decision.session_id, SESSION)

    def test_continuing_a_session_not_among_the_occupants_is_refused_when_full(self) -> None:
        decision = decide(observation(), bindings=(binding(),), slots=self.occupied(6))
        self.assertFalse(decision.authorized)
        self.assertEqual(decision.reason, authorization.REASON_CONCURRENCY_CEILING)

    def test_a_reservation_occupies_a_slot_without_any_ownership_evidence(self) -> None:
        slots = reconcile_agent_slots((reserved(),), ownership={})
        self.assertEqual(slots.occupants, (SESSION,))
        self.assertEqual(slots.unprovable, ())

    def test_unprovable_ownership_never_frees_a_slot(self) -> None:
        record = binding()
        for ownership in ({}, {SESSION: False}, {SESSION: None}, {SESSION: "yes"}):
            with self.subTest(ownership=ownership):
                slots = reconcile_agent_slots((record,), ownership=ownership)
                self.assertEqual(slots.unprovable, (SESSION,))
                self.assertEqual(slots.occupants, ())
                self.assertFalse(slots.provable)

    def test_a_duplicated_session_is_unprovable_rather_than_counted_once(self) -> None:
        record = binding()
        slots = reconcile_agent_slots((record, record), ownership={SESSION: True})
        self.assertIn(SESSION, slots.unprovable)
        self.assertFalse(slots.provable)

    def test_terminal_bindings_occupy_nothing(self) -> None:
        slots = reconcile_agent_slots((binding(state=BINDING_STATE_UNBOUND),), ownership={})
        self.assertEqual(slots.occupants, ())
        self.assertEqual(slots.unprovable, ())

    def test_existing_reasons_still_refuse_with_room_to_admit(self) -> None:
        room = self.slots()
        for observed, expected in (
            (observation(rails=(rail_state(status="ready"),)),
             authorization.REASON_RAIL_NOT_DISPATCHED),
            (observation(rails=(rail_state(depends_on=("issue-55-absent-rail",)),)),
             authorization.REASON_DEPENDENCY_UNSATISFIED),
            (observation(foreign_resource_holders={RESOURCE: ("elsewhere",)}),
             authorization.REASON_RESOURCE_CONTENDED),
            (observation(workspace=None),
             authorization.REASON_WORKSPACE_IDENTITY_AMBIGUOUS),
        ):
            with self.subTest(expected=expected):
                decision = decide(observed, slots=room)
                self.assertFalse(decision.authorized)
                self.assertEqual(decision.reason, expected)

    def test_the_ceiling_does_not_mask_an_earlier_refusal(self) -> None:
        """A full manager still reports why the rail itself was unusable."""
        decision = decide(observation(rails=(rail_state(status="ready"),)),
                          slots=self.occupied(6))
        self.assertEqual(decision.reason, authorization.REASON_RAIL_NOT_DISPATCHED)

    def test_admission_introduces_no_scheduler_or_trigger_machinery(self) -> None:
        source = Path(authorization.__file__).read_text(encoding="utf-8").lower()
        for forbidden in ("scheduler", "priority", "fairness", "autoscal", "retry",
                          "score", "elapsed", "sleep", "backoff"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_admission_is_not_coupled_to_compaction_or_rotation(self) -> None:
        """Coupling, not vocabulary.

        The module may say out loud that the rotation threshold shares the number
        six by coincidence; what it may not do is import, reference, or branch on
        it. So this reads identifiers and imports rather than prose, which is the
        difference between naming a boundary and depending across it.
        """
        import ast

        tree = ast.parse(Path(authorization.__file__).read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id.lower())
            elif isinstance(node, ast.Attribute):
                names.add(node.attr.lower())
            elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                names.add(node.name.lower())
            elif isinstance(node, ast.arg):
                names.add(node.arg.lower())
            elif isinstance(node, ast.Import):
                names.update(alias.name.lower() for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or "").lower())
                names.update(alias.name.lower() for alias in node.names)
        for forbidden in ("compaction", "rotation", "rotate", "threshold", "compact"):
            with self.subTest(forbidden=forbidden):
                offenders = sorted(name for name in names if forbidden in name)
                self.assertEqual(offenders, [], offenders)


if __name__ == "__main__":
    unittest.main()

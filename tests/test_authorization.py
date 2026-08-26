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


def binding(**overrides: object) -> BindingRecord:
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
        "pid": 4242,
        "pid_domain": "test-host",
        "started_at": "2026-08-26T12:00:00Z",
        "launched_at_head": HEAD,
        "bound_at": "2026-08-26T12:00:01Z",
    }
    arguments.update(overrides)
    return BindingRecord(**arguments)  # type: ignore[arg-type]


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
        )
        seen = set()
        for decision in refusals:
            self.assertFalse(decision.authorized, msg=decision.reason)
            self.assertIsNone(decision.action)
            self.assertIsNone(decision.session_id)
            self.assertTrue(decision.detail.strip(), msg=decision.reason)
            seen.add(decision.reason)
        self.assertEqual(len(seen), len(refusals))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from ai_dev_flow import control_plane
from ai_dev_flow.control_plane import (
    ARTIFACT_FILENAMES,
    ARTIFACT_OWNERS,
    ControlPlaneError,
    accept_progress,
    allocate_proceed_number,
    artifact_relative,
    collect_rail_states,
    parse_proceed_sequence,
    publish,
    resolve_control_plane_config,
    resolve_read_source,
    rail_blob_sha,
    rail_handoff_publication,
    render_rail,
    render_status,
    resolve_coordination_repo,
    resolve_current_head,
    validate_decision_record,
    validate_evidence_projection,
    validate_identifier,
)
from ai_dev_flow.progress_record import progress_relative
from ai_dev_flow.progress_store import ProgressStore


SAFE_EVIDENCE = {
    "schemaVersion": 1,
    "provenance": {"source": "provider-adapter", "collectedAt": "2026-08-24T15:00:00Z", "turnId": "turn-7"},
    "sourceHealth": {"status": "partial", "detail": "one source unavailable"},
    "observations": [{"kind": "terminal-action", "count": 17, "status": "completed"}],
}


SAFE_DECISION = {
    "schemaVersion": 1,
    "decisionId": "runtime-boundary-choice",
    "project": "ai-dev",
    "ticket": "issue-51",
    "rail": "control-plane-surface",
    "raisedAt": "2026-08-24T15:00:00Z",
    "title": "Choose the runtime launch boundary",
    "explanation": "Two compositions are viable and only a person can pick between them.",
    "evidence": [{"label": "focused suite", "locator": "tests.test_control_plane"}],
    "blocker": {
        "kind": "permission",
        "whatFailed": "publishing the recovery ref",
        "missingCapability": "push access to the coordination remote",
        "humanChange": "grant push access, or publish the ref by hand",
        "stateChanged": False,
        "nextAction": "re-dispatch the rail once the ref exists",
    },
}


class ControlPlaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.coordination = self._init_repo("coordination")

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

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
        self._git(repo_root, "config", "user.name", "Control Plane Tests")
        self._git(repo_root, "config", "user.email", "control-plane-tests@example.com")
        (repo_root / "README.md").write_text("coordination\n", encoding="utf-8")
        self._git(repo_root, "add", "README.md")
        self._git(repo_root, "commit", "-q", "-m", "initial commit")
        self._git(repo_root, "branch", "-M", "main")
        return repo_root

    def _publish(self, **overrides: object) -> tuple[Path, str]:
        arguments: dict[str, object] = {
            "project": "ai-dev", "ticket": "issue-51",
            "artifact": "rail", "role": "orchestrator",
            "content": "# Rail\n\nStatus: ready\n\nbounded work\n", "rail": "control-plane-surface",
        }
        arguments.update(overrides)
        return publish(self.coordination, **arguments)  # type: ignore[arg-type]

    def _rail_dir(self) -> Path:
        return self.coordination / "ai-dev" / "issue-51" / "rails" / "control-plane-surface"

    # Ownership

    def test_executor_cannot_publish_orchestrator_accepted_state(self) -> None:
        head_before = resolve_current_head(self.coordination)
        with self.assertRaises(ControlPlaneError) as caught:
            self._publish(artifact="state", role="executor", rail=None)
        self.assertIn("owned by 'orchestrator'", str(caught.exception))
        self.assertEqual(resolve_current_head(self.coordination), head_before)
        self.assertFalse((self.coordination / "ai-dev" / "issue-51" / "state.md").exists())

    def test_orchestrator_cannot_publish_executor_handoff(self) -> None:
        with self.assertRaises(ControlPlaneError) as caught:
            self._publish(artifact="handoff", role="orchestrator")
        self.assertIn("owned by 'executor'", str(caught.exception))

    def test_each_owner_publishes_its_own_artifact(self) -> None:
        self._publish(artifact="state", role="orchestrator", rail=None, content="# Accepted\n")
        self._publish()
        self._publish(artifact="handoff", role="executor", content="# Handoff\n\nproposed fact\n")
        scope = self.coordination / "ai-dev" / "issue-51"
        self.assertTrue((scope / "state.md").is_file())
        self.assertTrue((scope / "rails" / "control-plane-surface" / "rail.md").is_file())
        self.assertTrue((scope / "rails" / "control-plane-surface" / "handoff.md").is_file())
        self.assertEqual(self._git(self.coordination, "status", "--porcelain"), "")

    # Bounded reads and multiple rails

    def test_rail_read_excludes_sibling_rails_and_accepted_state(self) -> None:
        self._publish(artifact="state", role="orchestrator", rail=None, content="# Accepted\n\nsecret-accepted-state\n")
        self._publish(rail="control-plane-surface", content="# Rail A\n\nStatus: ready\n\nmine\n")
        self._publish(rail="provider-evidence-intake", content="# Rail B\n\nStatus: ready\n\nsibling-only-content\n")
        rendered = render_rail(self.coordination, project="ai-dev", ticket="issue-51", rail="control-plane-surface")
        self.assertIn("mine", rendered)
        self.assertNotIn("sibling-only-content", rendered)
        self.assertNotIn("secret-accepted-state", rendered)

    def test_status_lists_every_rail_for_one_ticket(self) -> None:
        self._publish(artifact="state", role="orchestrator", rail=None, content="# Accepted\n")
        self._publish(rail="control-plane-surface")
        self._publish(rail="provider-evidence-intake")
        self._publish(artifact="handoff", role="executor", rail="control-plane-surface", content="# Handoff\n")
        rendered = render_status(self.coordination, project="ai-dev", ticket="issue-51")
        self.assertIn("- control-plane-surface: ready; artifacts: rail, handoff", rendered)
        self.assertIn("- provider-evidence-intake: ready; artifacts: rail", rendered)

    def test_unknown_rail_read_fails_closed(self) -> None:
        with self.assertRaises(ControlPlaneError):
            render_rail(self.coordination, project="ai-dev", ticket="issue-51", rail="never-authorized")

    # Current state, not transcript

    def test_republish_replaces_content_and_history_holds_prior_versions(self) -> None:
        self._publish(artifact="handoff", role="executor", content="# Handoff\n\nfirst finding\n")
        self._publish(artifact="handoff", role="executor", content="# Handoff\n\nsecond finding\n")
        path = self.coordination / "ai-dev" / "issue-51" / "rails" / "control-plane-surface" / "handoff.md"
        current = path.read_text(encoding="utf-8")
        self.assertIn("second finding", current)
        self.assertNotIn("first finding", current)
        revisions = self._git(self.coordination, "rev-list", "--count", "HEAD", "--", "ai-dev/issue-51/rails/control-plane-surface/handoff.md")
        self.assertEqual(revisions, "2")

    def test_identical_republish_creates_no_empty_commit(self) -> None:
        _, first = self._publish(artifact="handoff", role="executor", content="# Handoff\n")
        _, second = self._publish(artifact="handoff", role="executor", content="# Handoff\n")
        self.assertEqual(first, second)

    # Concurrency

    def test_stale_expected_head_fails_closed(self) -> None:
        _, head = self._publish(artifact="handoff", role="executor", content="# Handoff\n\none\n")
        self._publish(artifact="state", role="orchestrator", rail=None, content="# Accepted\n")
        with self.assertRaises(ControlPlaneError) as caught:
            self._publish(artifact="handoff", role="executor", content="# Handoff\n\ntwo\n", expected_head=head)
        self.assertIn("Re-read the current", str(caught.exception))

    def test_fresh_expected_head_publishes(self) -> None:
        head = resolve_current_head(self.coordination)
        _, updated = self._publish(artifact="handoff", role="executor", content="# Handoff\n", expected_head=head)
        self.assertNotEqual(updated, head)

    def _attach_shared_upstream(self, name: str) -> Path:
        """Clone the coordination repo so the upstream shares its history."""
        upstream = self.tmp_path / name
        self._git(self.tmp_path, "clone", "-q", str(self.coordination), str(upstream))
        self._git(upstream, "config", "user.name", "Control Plane Upstream")
        self._git(upstream, "config", "user.email", "control-plane-upstream@example.com")
        # A real coordination remote is bare; let this non-bare fixture accept pushes to main.
        self._git(upstream, "config", "receive.denyCurrentBranch", "updateInstead")
        self._git(self.coordination, "remote", "add", "origin", str(upstream))
        self._git(self.coordination, "fetch", "-q", "origin")
        self._git(self.coordination, "branch", "--set-upstream-to", "origin/main", "main")
        return upstream

    def _advance_upstream(self, upstream: Path) -> None:
        (upstream / "remote-change.txt").write_text("remote\n", encoding="utf-8")
        self._git(upstream, "add", "remote-change.txt")
        self._git(upstream, "commit", "-q", "-m", "remote advance")

    def test_clean_strictly_behind_clone_reconciles_itself_and_publishes(self) -> None:
        upstream = self._attach_shared_upstream("upstream-behind")
        # The remote moves after the local repository last observed it.
        self._advance_upstream(upstream)
        remote_head = self._git(upstream, "rev-parse", "HEAD")

        target, head = self._publish(artifact="handoff", role="executor", content="# Handoff\n\nbehind\n")

        self.assertEqual(target.read_text(encoding="utf-8"), "# Handoff\n\nbehind\n")
        self.assertEqual(head, resolve_current_head(self.coordination))
        # Publication landed on the freshly resolved upstream state, by fast-forward only.
        self.assertEqual(self._git(self.coordination, "rev-parse", "HEAD~1"), remote_head)
        self.assertTrue((self.coordination / "remote-change.txt").exists())

    def test_behind_publication_reads_the_state_the_remote_actually_holds(self) -> None:
        upstream = self._attach_shared_upstream("upstream-behind-content")
        self._publish_from_upstream(upstream)
        remote_head = self._git(upstream, "rev-parse", "HEAD")

        # The read reports the remote head, and publication against it now succeeds
        # without a human fast-forwarding in between.
        rendered = render_status(self.coordination, project="ai-dev", ticket="issue-51")
        self.assertIn(f"head: {remote_head}", rendered)
        self._publish(artifact="handoff", role="executor", content="# Handoff\n", expected_head=remote_head)
        self.assertEqual(self._git(self.coordination, "rev-parse", "HEAD~1"), remote_head)

    def test_dirty_behind_clone_fails_closed_without_reconciling(self) -> None:
        upstream = self._attach_shared_upstream("upstream-behind-dirty")
        self._advance_upstream(upstream)
        (self.coordination / "README.md").write_text("locally edited\n", encoding="utf-8")
        local_head = resolve_current_head(self.coordination)

        with self.assertRaises(ControlPlaneError) as caught:
            self._publish(artifact="handoff", role="executor", content="# Handoff\n")
        self.assertIn("uncommitted or untracked changes", str(caught.exception))
        self.assertEqual(resolve_current_head(self.coordination), local_head)
        self.assertFalse((self.coordination / "remote-change.txt").exists())

    def test_untracked_file_blocks_automatic_reconciliation(self) -> None:
        upstream = self._attach_shared_upstream("upstream-behind-untracked")
        self._advance_upstream(upstream)
        (self.coordination / "stray.txt").write_text("stray\n", encoding="utf-8")
        local_head = resolve_current_head(self.coordination)

        with self.assertRaises(ControlPlaneError) as caught:
            self._publish(artifact="handoff", role="executor", content="# Handoff\n")
        self.assertIn("uncommitted or untracked changes", str(caught.exception))
        self.assertEqual(resolve_current_head(self.coordination), local_head)

    def test_active_git_operation_blocks_automatic_reconciliation(self) -> None:
        upstream = self._attach_shared_upstream("upstream-behind-mid-operation")
        self._advance_upstream(upstream)
        git_dir = Path(self._git(self.coordination, "rev-parse", "--absolute-git-dir"))
        (git_dir / "MERGE_HEAD").write_text(f"{resolve_current_head(self.coordination)}\n", encoding="utf-8")
        local_head = resolve_current_head(self.coordination)

        with self.assertRaises(ControlPlaneError) as caught:
            self._publish(artifact="handoff", role="executor", content="# Handoff\n")
        self.assertIn("MERGE_HEAD", str(caught.exception))
        self.assertEqual(resolve_current_head(self.coordination), local_head)
        self.assertFalse((self.coordination / "remote-change.txt").exists())

    def test_failed_fast_forward_fails_closed_without_publishing(self) -> None:
        upstream = self._attach_shared_upstream("upstream-behind-non-ff")
        self._advance_upstream(upstream)
        local_head = resolve_current_head(self.coordination)
        real_capture = control_plane._git_capture

        def refuse_merge(repo_root, arguments, **kwargs):  # type: ignore[no-untyped-def]
            if arguments and arguments[0] == "merge":
                return subprocess.CompletedProcess(arguments, 1, "", "fatal: Not possible to fast-forward")
            return real_capture(repo_root, arguments, **kwargs)

        with patch.object(control_plane, "_git_capture", side_effect=refuse_merge):
            with self.assertRaises(ControlPlaneError) as caught:
                self._publish(artifact="handoff", role="executor", content="# Handoff\n")
        self.assertIn("could not be fast-forwarded", str(caught.exception))
        self.assertEqual(resolve_current_head(self.coordination), local_head)
        self.assertEqual(self._git(self.coordination, "status", "--porcelain"), "")

    def test_behind_publication_fails_closed_when_the_remote_cannot_be_fetched(self) -> None:
        upstream = self._attach_shared_upstream("upstream-behind-unreachable")
        self._advance_upstream(upstream)
        self._git(self.coordination, "remote", "set-url", "origin", str(self.tmp_path / "does-not-exist"))
        local_head = resolve_current_head(self.coordination)

        with self.assertRaises(ControlPlaneError) as caught:
            self._publish(artifact="handoff", role="executor", content="# Handoff\n")
        self.assertIn("could not be fetched", str(caught.exception))
        self.assertEqual(resolve_current_head(self.coordination), local_head)

    def test_local_ahead_publication_still_builds_on_local_history(self) -> None:
        self._attach_shared_upstream("upstream-behind-local-ahead")
        _, first = self._publish(artifact="handoff", role="executor", content="# Handoff\n")
        _, second = self._publish(artifact="handoff", role="executor", content="# Handoff\n\nsecond\n")
        self.assertEqual(self._git(self.coordination, "rev-parse", "HEAD~1"), first)
        self.assertEqual(resolve_current_head(self.coordination), second)

    def test_diverged_upstream_fails_closed(self) -> None:
        upstream = self._attach_shared_upstream("upstream-diverged")
        self._publish(artifact="handoff", role="executor", content="# Handoff\n")
        self._advance_upstream(upstream)
        with self.assertRaises(ControlPlaneError) as caught:
            self._publish(artifact="handoff", role="executor", content="# Handoff\n\nlocal\n")
        self.assertIn("diverged", str(caught.exception))

    def test_publication_succeeds_when_upstream_is_current(self) -> None:
        self._attach_shared_upstream("upstream-current")
        _, head = self._publish(artifact="handoff", role="executor", content="# Handoff\n")
        self.assertEqual(head, resolve_current_head(self.coordination))

    # Rail identity

    def test_semantic_rail_identifiers_are_accepted(self) -> None:
        for identifier in ("control-plane-surface", "provider-evidence-intake", "rail-2"):
            self.assertEqual(validate_identifier(identifier, label="rail identifier"), identifier)

    def test_session_shaped_rail_identifiers_are_rejected(self) -> None:
        for identifier in (
            "b1d85c9c440fdcd6ab1d85c9c440fdcd",
            "7cf8d06eeb555ba8bcf421d9cfdedc8d84f77e16",
            "a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d",
        ):
            with self.subTest(identifier=identifier), self.assertRaises(ControlPlaneError):
                validate_identifier(identifier, label="rail identifier")

    def test_non_slug_identifiers_are_rejected(self) -> None:
        for identifier in ("Rail One", "../escape", "x", "UPPER"):
            with self.subTest(identifier=identifier), self.assertRaises(ControlPlaneError):
                validate_identifier(identifier, label="rail identifier")

    # Provider-evidence intake

    def test_safe_projection_is_accepted_and_stored_separately_from_handoff(self) -> None:
        self._publish(artifact="handoff", role="executor", content="# Handoff\n\nexecutor claim\n")
        self._publish(artifact="evidence", role="evidence", content=json.dumps(SAFE_EVIDENCE))
        rail_dir = self.coordination / "ai-dev" / "issue-51" / "rails" / "control-plane-surface"
        stored = json.loads((rail_dir / "evidence.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["sourceHealth"]["status"], "partial")
        self.assertNotIn("executor claim", (rail_dir / "evidence.json").read_text(encoding="utf-8"))
        self.assertIn("executor claim", (rail_dir / "handoff.md").read_text(encoding="utf-8"))

    def test_evidence_role_cannot_publish_handoff_or_state(self) -> None:
        for artifact in ("handoff", "state"):
            with self.subTest(artifact=artifact), self.assertRaises(ControlPlaneError):
                self._publish(artifact=artifact, role="evidence", rail=None if artifact == "state" else "control-plane-surface")

    def test_raw_content_keys_are_refused(self) -> None:
        for denied in ("prompt", "response", "commands", "output", "transcript", "logs", "telemetry", "toolResults"):
            payload = dict(SAFE_EVIDENCE)
            payload[denied] = "anything"
            with self.subTest(denied=denied), self.assertRaises(ControlPlaneError) as caught:
                validate_evidence_projection(payload)
            self.assertIn("excluded raw content key", str(caught.exception))

    def test_non_allowlisted_key_is_refused(self) -> None:
        payload = dict(SAFE_EVIDENCE)
        payload["extraField"] = "value"
        with self.assertRaises(ControlPlaneError) as caught:
            validate_evidence_projection(payload)
        self.assertIn("non-allowlisted", str(caught.exception))

    def test_provenance_and_source_health_are_required(self) -> None:
        without_provenance = {key: value for key, value in SAFE_EVIDENCE.items() if key != "provenance"}
        with self.assertRaises(ControlPlaneError) as caught:
            validate_evidence_projection(without_provenance)
        self.assertIn("provenance", str(caught.exception))
        without_health = {key: value for key, value in SAFE_EVIDENCE.items() if key != "sourceHealth"}
        with self.assertRaises(ControlPlaneError) as caught:
            validate_evidence_projection(without_health)
        self.assertIn("sourceHealth", str(caught.exception))

    def test_source_health_status_must_be_a_known_state(self) -> None:
        payload = json.loads(json.dumps(SAFE_EVIDENCE))
        payload["sourceHealth"]["status"] = "probably-fine"
        with self.assertRaises(ControlPlaneError):
            validate_evidence_projection(payload)

    def test_unbounded_strings_and_observation_counts_are_refused(self) -> None:
        long_detail = json.loads(json.dumps(SAFE_EVIDENCE))
        long_detail["sourceHealth"]["detail"] = "x" * 241
        with self.assertRaises(ControlPlaneError) as caught:
            validate_evidence_projection(long_detail)
        self.assertIn("bounded projection", str(caught.exception))
        too_many = json.loads(json.dumps(SAFE_EVIDENCE))
        too_many["observations"] = [{"kind": f"action-{index}"} for index in range(51)]
        with self.assertRaises(ControlPlaneError):
            validate_evidence_projection(too_many)

    def test_malformed_evidence_json_is_refused_without_writing(self) -> None:
        with self.assertRaises(ControlPlaneError):
            self._publish(artifact="evidence", role="evidence", content="{not json")
        self.assertFalse((self.coordination / "ai-dev" / "issue-51" / "rails" / "control-plane-surface" / "evidence.json").exists())

    # The durable human-attention record

    def test_a_valid_record_is_stored_as_its_own_rail_scoped_artifact(self) -> None:
        self._publish(artifact="handoff", role="executor", content="# Handoff\n\nexecutor claim\n")
        self._publish(artifact="decision", role="orchestrator", content=json.dumps(SAFE_DECISION))
        rail_dir = self.coordination / "ai-dev" / "issue-51" / "rails" / "control-plane-surface"
        stored = json.loads((rail_dir / "decision.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["decisionId"], "runtime-boundary-choice")
        self.assertEqual(stored["blocker"]["kind"], "permission")
        self.assertNotIn("executor claim", (rail_dir / "decision.json").read_text(encoding="utf-8"))
        self.assertEqual(self._git(self.coordination, "status", "--porcelain"), "")

    def test_only_the_orchestrator_may_raise_a_human_decision(self) -> None:
        for role in ("executor", "evidence"):
            with self.subTest(role=role), self.assertRaises(ControlPlaneError) as caught:
                self._publish(artifact="decision", role=role, content=json.dumps(SAFE_DECISION))
            self.assertIn("owned by 'orchestrator'", str(caught.exception))
        self.assertFalse((self._rail_dir() / "decision.json").exists())

    def test_a_record_is_rail_scoped_and_never_scope_level(self) -> None:
        with self.assertRaises(ControlPlaneError) as caught:
            self._publish(
                artifact="decision", role="orchestrator", rail=None,
                content=json.dumps(SAFE_DECISION),
            )
        self.assertIn("requires a rail identifier", str(caught.exception))

    def test_raw_content_keys_are_refused_in_a_decision(self) -> None:
        for denied in ("prompt", "response", "output", "transcript", "logs", "telemetry"):
            payload = dict(SAFE_DECISION)
            payload[denied] = "anything"
            with self.subTest(denied=denied), self.assertRaises(ControlPlaneError) as caught:
                validate_decision_record(payload)
            self.assertIn("excluded raw content key", str(caught.exception))

    def test_a_non_allowlisted_decision_key_is_refused(self) -> None:
        with self.assertRaises(ControlPlaneError) as caught:
            validate_decision_record(dict(SAFE_DECISION, severity="high"))
        self.assertIn("non-allowlisted", str(caught.exception))

    def test_every_required_decision_field_is_required(self) -> None:
        for field in ("decisionId", "project", "ticket", "rail", "raisedAt", "title", "explanation"):
            payload = {key: value for key, value in SAFE_DECISION.items() if key != field}
            with self.subTest(field=field), self.assertRaises(ControlPlaneError):
                validate_decision_record(payload)

    def test_the_schema_version_is_exact(self) -> None:
        for version in (2, "1", True, None):
            with self.subTest(version=version), self.assertRaises(ControlPlaneError) as caught:
                validate_decision_record(dict(SAFE_DECISION, schemaVersion=version))
            self.assertIn("schemaVersion must be exactly 1", str(caught.exception))

    def test_decision_identity_and_routing_refuse_session_shaped_values(self) -> None:
        for field in ("decisionId", "project", "ticket", "rail"):
            payload = dict(SAFE_DECISION)
            payload[field] = "1a2b3c4d0001400080000000000000ab"
            with self.subTest(field=field), self.assertRaises(ControlPlaneError) as caught:
                validate_decision_record(payload)
            self.assertIn("session, agent, or process", str(caught.exception))

    def test_the_raised_time_must_be_the_shape_the_lifecycle_parses(self) -> None:
        for raised in ("yesterday", "2026-08-24", "2026-08-24T15:00:00+00:00"):
            with self.subTest(raised=raised), self.assertRaises(ControlPlaneError) as caught:
                validate_decision_record(dict(SAFE_DECISION, raisedAt=raised))
            self.assertIn("UTC timestamp", str(caught.exception))

    def test_unbounded_decision_text_is_refused(self) -> None:
        for field, limit in (("title", 120), ("explanation", 2000)):
            with self.subTest(field=field), self.assertRaises(ControlPlaneError) as caught:
                validate_decision_record(dict(SAFE_DECISION, **{field: "x" * (limit + 1)}))
            self.assertIn("bounded projection", str(caught.exception))

    def test_evidence_stays_bounded_pointers(self) -> None:
        too_many = dict(
            SAFE_DECISION,
            evidence=[{"label": f"e{index}", "locator": "l"} for index in range(9)],
        )
        with self.assertRaises(ControlPlaneError) as caught:
            validate_decision_record(too_many)
        self.assertIn("at most 8", str(caught.exception))
        with self.assertRaises(ControlPlaneError) as caught:
            validate_decision_record(
                dict(SAFE_DECISION, evidence=[{"label": "e", "locator": "l", "output": "a log"}])
            )
        self.assertIn("excluded raw content key", str(caught.exception))

    def test_a_blocker_is_complete_or_absent(self) -> None:
        without = {key: value for key, value in SAFE_DECISION.items() if key != "blocker"}
        self.assertEqual(validate_decision_record(without), without)
        with self.assertRaises(ControlPlaneError) as caught:
            validate_decision_record(dict(SAFE_DECISION, blocker={"kind": "permission"}))
        self.assertIn("missing required key(s)", str(caught.exception))

    def test_a_blocker_names_a_known_kind_and_an_explicit_state_answer(self) -> None:
        with self.assertRaises(ControlPlaneError) as caught:
            validate_decision_record(
                dict(SAFE_DECISION, blocker=dict(SAFE_DECISION["blocker"], kind="vibes"))
            )
        self.assertIn("blocker kind must be one of", str(caught.exception))
        with self.assertRaises(ControlPlaneError) as caught:
            validate_decision_record(
                dict(SAFE_DECISION, blocker=dict(SAFE_DECISION["blocker"], stateChanged="maybe"))
            )
        self.assertIn("must be true or false", str(caught.exception))

    def test_malformed_decision_json_is_refused_without_writing(self) -> None:
        with self.assertRaises(ControlPlaneError) as caught:
            self._publish(artifact="decision", role="orchestrator", content="{not json")
        self.assertIn("Human-decision record is not valid JSON", str(caught.exception))
        self.assertFalse((self._rail_dir() / "decision.json").exists())

    def test_a_rail_lists_its_decision_only_once_one_exists(self) -> None:
        self._publish(artifact="state", role="orchestrator", rail=None, content="# Accepted\n")
        self._publish()
        self._publish(artifact="handoff", role="executor", content="# Handoff\n")
        self.assertIn(
            "- control-plane-surface: ready; artifacts: rail, handoff",
            render_status(self.coordination, project="ai-dev", ticket="issue-51"),
        )
        self._publish(artifact="decision", role="orchestrator", content=json.dumps(SAFE_DECISION))
        self.assertIn(
            "- control-plane-surface: ready; artifacts: rail, handoff, decision",
            render_status(self.coordination, project="ai-dev", ticket="issue-51"),
        )

    def test_the_new_artifact_leaves_provider_evidence_intake_unchanged(self) -> None:
        self._publish(artifact="evidence", role="evidence", content=json.dumps(SAFE_EVIDENCE))
        self._publish(artifact="decision", role="orchestrator", content=json.dumps(SAFE_DECISION))
        self.assertEqual(
            json.loads((self._rail_dir() / "evidence.json").read_text(encoding="utf-8")),
            SAFE_EVIDENCE,
        )
        with self.assertRaises(ControlPlaneError) as caught:
            self._publish(artifact="evidence", role="evidence", content="{not json")
        self.assertIn("Provider evidence is not valid JSON", str(caught.exception))
        with self.assertRaises(ControlPlaneError) as caught:
            validate_evidence_projection(dict(SAFE_EVIDENCE, decisionId="borrowed"))
        self.assertIn("non-allowlisted", str(caught.exception))

    # Isolation

    def test_operations_never_touch_the_product_repository(self) -> None:
        product = self._init_repo("product")
        product_head = resolve_current_head(product)
        self._publish(artifact="state", role="orchestrator", rail=None, content="# Accepted\n")
        self._publish(artifact="handoff", role="executor", content="# Handoff\n")
        self.assertEqual(resolve_current_head(product), product_head)
        self.assertEqual(self._git(product, "status", "--porcelain"), "")
        self.assertEqual(self._git(product, "rev-parse", "--abbrev-ref", "HEAD"), "main")

    # Multiple bounded rails

    def _authorize(
        self,
        rail: str,
        status: str,
        *,
        depends_on: str = "",
        resource: str = "",
        role: str | None = None,
        extra_role: str | None = None,
    ) -> None:
        header = [f"# Rail: {rail}", "", f"Status: {status}"]
        if role is not None:
            header.append(f"Role: {role}")
        if extra_role is not None:
            header.append(f"Role: {extra_role}")
        if depends_on:
            header.append(f"Depends on: {depends_on}")
        if resource:
            header.append(f"Shared resource: {resource}")
        header.extend(["", "## Goal", "", f"bounded work for {rail}"])
        self._publish(rail=rail, content="\n".join(header) + "\n")

    def _states(self) -> dict[str, object]:
        source = resolve_read_source(self.coordination)
        return {state.identifier: state for state in collect_rail_states(source, project="ai-dev", ticket="issue-51")}

    def test_four_status_classes_are_represented_and_surfaced(self) -> None:
        for rail, status in (
            ("rail-ready", "ready"), ("rail-running", "running"),
            ("rail-blocked", "blocked"), ("rail-completed", "completed"),
        ):
            self._authorize(rail, status)
        states = self._states()
        self.assertEqual({identifier: state.status for identifier, state in states.items()}, {  # type: ignore[attr-defined]
            "rail-ready": "ready", "rail-running": "running",
            "rail-blocked": "blocked", "rail-completed": "completed",
        })
        rendered = render_status(self.coordination, project="ai-dev", ticket="issue-51")
        for rail, status in (("rail-ready", "ready"), ("rail-running", "running"),
                             ("rail-blocked", "blocked"), ("rail-completed", "completed")):
            with self.subTest(rail=rail):
                self.assertIn(f"- {rail}: {status}; artifacts: rail", rendered)

    # Durable rail role

    def test_a_rail_without_a_role_still_reads_and_surfaces_none(self) -> None:
        """38 of 81 published rails predate this header; refusing them would retire
        whole scopes for every reader, which is far worse than an unenforced field."""
        self._authorize("rail-legacy", "running")
        state = self._states()["rail-legacy"]
        self.assertIsNone(state.role)  # type: ignore[attr-defined]
        self.assertIn("rail-legacy: running", render_status(
            self.coordination, project="ai-dev", ticket="issue-51"))

    def test_an_empty_or_none_role_reads_as_absent(self) -> None:
        for value in ("", "   ", "none", "None"):
            with self.subTest(value=value):
                rail = "rail-empty-{0}".format(abs(hash(value)) % 9973)
                self._authorize(rail, "running", role=value)
                self.assertIsNone(self._states()[rail].role)  # type: ignore[attr-defined]

    def test_a_non_managed_role_stays_readable_and_normalized(self) -> None:
        """`evidence-worker` is a real assignment in Issue #55's own history. It must
        remain observable here; refusing it belongs to authorization, not the reader."""
        self._authorize("rail-evidence", "running", role="Evidence-Worker")
        self.assertEqual(self._states()["rail-evidence"].role, "evidence-worker")  # type: ignore[attr-defined]

    def test_every_managed_role_parses(self) -> None:
        for role in ("executor", "reviewer", "orchestrator"):
            with self.subTest(role=role):
                rail = "rail-{0}".format(role)
                self._authorize(rail, "running", role=role)
                self.assertEqual(self._states()[rail].role, role)  # type: ignore[attr-defined]

    def test_a_backticked_role_is_normalized(self) -> None:
        self._authorize("rail-quoted", "running", role="`orchestrator`")
        self.assertEqual(self._states()["rail-quoted"].role, "orchestrator")  # type: ignore[attr-defined]

    def test_two_role_headers_fail_the_scope_read(self) -> None:
        """Role is authorization-sensitive now, so 'last one wins' is not a safe read."""
        self._authorize("rail-ambiguous", "running", role="executor", extra_role="orchestrator")
        with self.assertRaises(ControlPlaneError) as caught:
            self._states()
        self.assertIn("Role:", str(caught.exception))
        self.assertIn("unambiguous", str(caught.exception))

    def test_two_identical_role_headers_are_still_ambiguous(self) -> None:
        self._authorize("rail-twice", "running", role="executor", extra_role="executor")
        with self.assertRaises(ControlPlaneError):
            self._states()

    def test_the_reader_never_checks_the_role_against_a_managed_vocabulary(self) -> None:
        """Three namespaces stay distinct; the reader owns none of them."""
        self._authorize("rail-odd", "running", role="release-captain")
        self.assertEqual(self._states()["rail-odd"].role, "release-captain")  # type: ignore[attr-defined]

    def test_target_rail_read_stays_bounded_with_many_rails(self) -> None:
        self._authorize("rail-alpha", "running")
        self._authorize("rail-beta", "ready")
        self._authorize("rail-gamma", "blocked")
        rendered = render_rail(self.coordination, project="ai-dev", ticket="issue-51", rail="rail-beta")
        self.assertIn("bounded work for rail-beta", rendered)
        self.assertNotIn("rail-alpha", rendered)
        self.assertNotIn("rail-gamma", rendered)

    def test_dependency_holds_one_rail_without_blocking_an_independent_rail(self) -> None:
        self._authorize("rail-prerequisite", "running")
        self._authorize("rail-dependent", "blocked", depends_on="rail-prerequisite")
        self._authorize("rail-independent", "ready")
        rendered = render_status(self.coordination, project="ai-dev", ticket="issue-51")
        self.assertIn("depends on: rail-prerequisite (running)", rendered)
        self.assertIn("dependencies satisfied: no", rendered)
        self.assertIn("- rail-independent: ready; artifacts: rail", rendered)
        independent = self._states()["rail-independent"]
        self.assertEqual(independent.depends_on, [])  # type: ignore[attr-defined]

    def test_satisfied_dependencies_are_reported_as_satisfied(self) -> None:
        self._authorize("rail-prerequisite", "completed")
        self._authorize("rail-dependent", "ready", depends_on="rail-prerequisite")
        rendered = render_status(self.coordination, project="ai-dev", ticket="issue-51")
        self.assertIn("dependencies satisfied: yes", rendered)

    def test_singleton_resource_contention_is_surfaced_but_unrelated_work_is_not(self) -> None:
        self._authorize("rail-live-runtime", "running", resource="wow-server")
        self._authorize("rail-live-second", "ready", resource="wow-server")
        self._authorize("rail-source-only", "ready")
        rendered = render_status(self.coordination, project="ai-dev", ticket="issue-51")
        self.assertIn("shared resource: wow-server", rendered)
        self.assertIn("resource in use by: rail-live-runtime", rendered)
        source_only = self._states()["rail-source-only"]
        self.assertIsNone(source_only.shared_resource)  # type: ignore[attr-defined]
        self.assertNotIn("rail-source-only: ready; artifacts: rail\n    shared resource", rendered)

    def test_helper_reports_facts_and_never_a_recommendation(self) -> None:
        self._authorize("rail-prerequisite", "running", resource="wow-server")
        self._authorize("rail-dependent", "blocked", depends_on="rail-prerequisite", resource="wow-server")
        rendered = render_status(self.coordination, project="ai-dev", ticket="issue-51").lower()
        for verdict in ("launch", "continue", "hold", "recommend", "next agent", "schedule"):
            with self.subTest(verdict=verdict):
                self.assertNotIn(verdict, rendered)

    def test_missing_or_invalid_rail_status_fails_closed(self) -> None:
        self._publish(rail="rail-no-status", content="# Rail\n\nno status header\n")
        with self.assertRaises(ControlPlaneError) as caught:
            render_status(self.coordination, project="ai-dev", ticket="issue-51")
        self.assertIn("expected one of ready, running, blocked, completed", str(caught.exception))

    def test_unknown_status_value_fails_closed(self) -> None:
        self._authorize("rail-bad-status", "almost-done")
        with self.assertRaises(ControlPlaneError):
            render_status(self.coordination, project="ai-dev", ticket="issue-51")

    def test_rail_without_orchestrator_authorization_fails_closed(self) -> None:
        self._publish(artifact="handoff", role="executor", rail="rail-unauthorized", content="# Handoff\n")
        with self.assertRaises(ControlPlaneError) as caught:
            render_status(self.coordination, project="ai-dev", ticket="issue-51")
        self.assertIn("no orchestrator authorization", str(caught.exception))

    def test_unknown_dependency_fails_closed(self) -> None:
        self._authorize("rail-dependent", "ready", depends_on="rail-that-does-not-exist")
        with self.assertRaises(ControlPlaneError) as caught:
            render_status(self.coordination, project="ai-dev", ticket="issue-51")
        self.assertIn("depends on unknown rail", str(caught.exception))

    def test_self_dependency_fails_closed(self) -> None:
        self._authorize("rail-selfish", "ready", depends_on="rail-selfish")
        with self.assertRaises(ControlPlaneError) as caught:
            render_status(self.coordination, project="ai-dev", ticket="issue-51")
        self.assertIn("dependency on itself", str(caught.exception))

    def test_contradictory_dependency_cycle_fails_closed(self) -> None:
        self._authorize("rail-one", "ready", depends_on="rail-two")
        self._authorize("rail-two", "ready", depends_on="rail-one")
        with self.assertRaises(ControlPlaneError) as caught:
            render_status(self.coordination, project="ai-dev", ticket="issue-51")
        self.assertIn("contradictory", str(caught.exception))

    # Rail iteration identity

    def test_rail_blob_sha_is_the_object_name_of_that_rails_authorization(self) -> None:
        self._authorize("rail-iterated", "running")
        source = resolve_read_source(self.coordination)
        blob = rail_blob_sha(source, project="ai-dev", ticket="issue-51", rail="rail-iterated")
        expected = self._git(
            self.coordination, "rev-parse", "HEAD:ai-dev/issue-51/rails/rail-iterated/rail.md"
        )
        self.assertEqual(blob, expected)

    def test_rail_blob_sha_changes_only_when_the_authorization_text_changes(self) -> None:
        self._authorize("rail-iterated", "running")
        first = rail_blob_sha(
            resolve_read_source(self.coordination), project="ai-dev", ticket="issue-51",
            rail="rail-iterated",
        )
        # Publishing an unrelated artifact moves the head but not the iteration.
        self._handoff("rail-iterated", "running")
        unchanged = rail_blob_sha(
            resolve_read_source(self.coordination), project="ai-dev", ticket="issue-51",
            rail="rail-iterated",
        )
        self.assertEqual(unchanged, first)

        self._authorize("rail-iterated", "blocked")
        changed = rail_blob_sha(
            resolve_read_source(self.coordination), project="ai-dev", ticket="issue-51",
            rail="rail-iterated",
        )
        self.assertNotEqual(changed, first)

    def test_a_rails_handoff_publication_is_reported_by_location_and_presence(self) -> None:
        # What a rotation boundary needs to know about durable handoff evidence:
        # exactly where a fresh agent reads it, and whether it is there yet.
        self._publish()
        source = resolve_read_source(self.coordination)
        location, published = rail_handoff_publication(
            source, project="ai-dev", ticket="issue-51", rail="control-plane-surface"
        )
        self.assertEqual(
            location, "ai-dev/issue-51/rails/control-plane-surface/handoff.md"
        )
        self.assertFalse(published)

        self._publish(artifact="handoff", role="executor", content="# Handoff\n\nnext action\n")
        location, published = rail_handoff_publication(
            resolve_read_source(self.coordination),
            project="ai-dev", ticket="issue-51", rail="control-plane-surface",
        )
        self.assertEqual(
            location, "ai-dev/issue-51/rails/control-plane-surface/handoff.md"
        )
        self.assertTrue(published)

    def test_rail_blob_sha_is_absent_for_an_unauthorized_rail(self) -> None:
        source = resolve_read_source(self.coordination)
        self.assertIsNone(
            rail_blob_sha(source, project="ai-dev", ticket="issue-51", rail="rail-absent")
        )

    def test_rail_blob_sha_reads_the_revision_the_source_serves(self) -> None:
        upstream = self._attach_shared_upstream("upstream-iteration")
        self._authorize("rail-iterated", "running")
        self._git(self.coordination, "push", "-q", "origin", "main")
        remote_blob = self._git(
            upstream, "rev-parse", "HEAD:ai-dev/issue-51/rails/rail-iterated/rail.md"
        )
        source = resolve_read_source(self.coordination)
        self.assertEqual(
            rail_blob_sha(source, project="ai-dev", ticket="issue-51", rail="rail-iterated"),
            remote_blob,
        )

    # Rail authorization versus executor-proposed status

    def _handoff(self, rail: str, status: str) -> None:
        self._publish(
            artifact="handoff", role="executor", rail=rail,
            content=f"# Handoff: {rail}\n\nStatus: {status}\nOwner: executor\n\n## Evidence\n\nbounded\n",
        )

    def test_agreeing_rail_and_handoff_are_not_flagged(self) -> None:
        self._authorize("rail-agreed", "completed")
        self._handoff("rail-agreed", "completed")
        rendered = render_status(self.coordination, project="ai-dev", ticket="issue-51")
        self.assertIn("unreconciled rails: 0", rendered)
        self.assertNotIn("UNRECONCILED", rendered)

    def test_handoff_proposing_completion_is_conspicuously_unreconciled(self) -> None:
        self._authorize("rail-drifted", "ready")
        self._handoff("rail-drifted", "completed")
        rendered = render_status(self.coordination, project="ai-dev", ticket="issue-51")
        self.assertIn("unreconciled rails: 1 (rail-drifted)", rendered)
        self.assertIn(
            "UNRECONCILED: rail authorizes 'ready' but the handoff proposes 'completed'", rendered
        )

    def test_accepted_completion_contradicted_by_a_handoff_is_flagged(self) -> None:
        self._authorize("rail-contradicted", "completed")
        self._handoff("rail-contradicted", "blocked")
        rendered = render_status(self.coordination, project="ai-dev", ticket="issue-51")
        self.assertIn("unreconciled rails: 1 (rail-contradicted)", rendered)
        self.assertIn("rail authorizes 'completed' but the handoff proposes 'blocked'", rendered)

    def test_handoff_status_is_never_promoted_into_rail_status(self) -> None:
        self._authorize("rail-drifted", "ready")
        self._handoff("rail-drifted", "completed")
        state = self._states()["rail-drifted"]
        self.assertEqual(state.status, "ready")  # type: ignore[attr-defined]
        self.assertEqual(state.proposed_status, "completed")  # type: ignore[attr-defined]
        self.assertTrue(state.unreconciled)  # type: ignore[attr-defined]
        rendered = render_status(self.coordination, project="ai-dev", ticket="issue-51")
        self.assertIn("- rail-drifted: ready; artifacts: rail, handoff", rendered)

    def test_rail_without_a_handoff_is_not_flagged(self) -> None:
        self._authorize("rail-running", "running")
        rendered = render_status(self.coordination, project="ai-dev", ticket="issue-51")
        self.assertIn("unreconciled rails: 0", rendered)

    def test_malformed_handoff_status_is_flagged_without_breaking_other_rails(self) -> None:
        self._authorize("rail-broken", "running")
        self._publish(artifact="handoff", role="executor", rail="rail-broken",
                      content="# Handoff\n\nStatus: mostly-done\n\n## Evidence\n\nbounded\n")
        self._authorize("rail-healthy", "ready")
        rendered = render_status(self.coordination, project="ai-dev", ticket="issue-51")
        self.assertIn("the handoff proposes 'unrecognized'", rendered)
        self.assertIn("- rail-healthy: ready; artifacts: rail", rendered)

    def test_one_unreconciled_rail_does_not_hide_the_others(self) -> None:
        self._authorize("rail-drifted", "ready")
        self._handoff("rail-drifted", "completed")
        self._authorize("rail-clean", "running")
        self._authorize("rail-waiting", "blocked")
        rendered = render_status(self.coordination, project="ai-dev", ticket="issue-51")
        self.assertIn("unreconciled rails: 1 (rail-drifted)", rendered)
        self.assertIn("- rail-clean: running; artifacts: rail", rendered)
        self.assertIn("- rail-waiting: blocked; artifacts: rail", rendered)

    # Fresh reads: a stale clone must never serve stale authorization

    def _publish_from_upstream(self, upstream: Path, **overrides: object) -> None:
        """Publish into the upstream clone so the shared surface moves ahead."""
        arguments: dict[str, object] = {
            "project": "ai-dev", "ticket": "issue-51",
            "artifact": "rail", "role": "orchestrator",
            "content": "# Rail\n\nStatus: ready\n\npublished remotely\n", "rail": "remote-only-rail",
        }
        arguments.update(overrides)
        publish(upstream, **arguments)  # type: ignore[arg-type]

    def test_status_reads_new_remote_state_without_moving_local_head(self) -> None:
        upstream = self._attach_shared_upstream("upstream-fresh-status")
        self._publish_from_upstream(upstream, artifact="state", role="orchestrator", rail=None,
                                    content="# Accepted\n\npublished-after-clone\n")
        local_head_before = resolve_current_head(self.coordination)
        rendered = render_status(self.coordination, project="ai-dev", ticket="issue-51")
        self.assertIn("published-after-clone", rendered)
        self.assertEqual(resolve_current_head(self.coordination), local_head_before)
        self.assertEqual(self._git(self.coordination, "status", "--porcelain"), "")

    def test_rail_discovers_a_rail_absent_from_the_stale_local_tree(self) -> None:
        upstream = self._attach_shared_upstream("upstream-fresh-rail")
        self._publish_from_upstream(upstream)
        self.assertFalse(
            (self.coordination / "ai-dev" / "issue-51" / "rails" / "remote-only-rail").exists(),
            "the local tree must still be stale for this test to mean anything",
        )
        local_head_before = resolve_current_head(self.coordination)
        rendered = render_rail(self.coordination, project="ai-dev", ticket="issue-51", rail="remote-only-rail")
        self.assertIn("published remotely", rendered)
        self.assertEqual(resolve_current_head(self.coordination), local_head_before)

    def test_reported_head_is_the_commit_actually_read(self) -> None:
        upstream = self._attach_shared_upstream("upstream-head-report")
        self._publish_from_upstream(upstream, artifact="state", role="orchestrator", rail=None, content="# Accepted\n")
        self._git(self.coordination, "fetch", "-q", "origin")
        upstream_head = self._git(self.coordination, "rev-parse", "origin/main")
        rendered = render_status(self.coordination, project="ai-dev", ticket="issue-51")
        self.assertIn(f"head: {upstream_head}", rendered)
        self.assertNotEqual(upstream_head, resolve_current_head(self.coordination))

    def test_local_ahead_read_fails_closed_without_serving_stale_content(self) -> None:
        self._attach_shared_upstream("upstream-local-ahead")
        self._publish(artifact="handoff", role="executor", content="# Handoff\n\nunpublished\n")
        with self.assertRaises(ControlPlaneError) as caught:
            render_status(self.coordination, project="ai-dev", ticket="issue-51")
        self.assertIn("unpublished local commits", str(caught.exception))

    def test_diverged_read_fails_closed(self) -> None:
        upstream = self._attach_shared_upstream("upstream-diverged-read")
        self._publish(artifact="handoff", role="executor", content="# Handoff\n\nlocal\n")
        self._advance_upstream(upstream)
        with self.assertRaises(ControlPlaneError) as caught:
            render_status(self.coordination, project="ai-dev", ticket="issue-51")
        self.assertIn("diverged", str(caught.exception))

    def test_fetch_failure_fails_closed(self) -> None:
        self._attach_shared_upstream("upstream-unreachable")
        self._git(self.coordination, "remote", "set-url", "origin", str(self.tmp_path / "does-not-exist"))
        with self.assertRaises(ControlPlaneError) as caught:
            render_status(self.coordination, project="ai-dev", ticket="issue-51")
        self.assertIn("freshness is unproven", str(caught.exception))

    def test_remote_without_tracked_upstream_fails_closed(self) -> None:
        self._attach_shared_upstream("upstream-untracked")
        self._git(self.coordination, "branch", "--unset-upstream", "main")
        with self.assertRaises(ControlPlaneError) as caught:
            render_status(self.coordination, project="ai-dev", ticket="issue-51")
        self.assertIn("no tracked upstream", str(caught.exception))

    def test_repository_without_a_remote_reads_locally(self) -> None:
        self._publish(artifact="state", role="orchestrator", rail=None, content="# Accepted\n\nlocal-only\n")
        source = resolve_read_source(self.coordination)
        self.assertIsNone(source.revision)
        rendered = render_status(self.coordination, project="ai-dev", ticket="issue-51")
        self.assertIn("local-only", rendered)

    def test_reads_do_not_mutate_branch_index_or_worktree(self) -> None:
        upstream = self._attach_shared_upstream("upstream-no-mutation")
        self._publish_from_upstream(upstream)
        before = (
            resolve_current_head(self.coordination),
            self._git(self.coordination, "status", "--porcelain"),
            self._git(self.coordination, "rev-parse", "--abbrev-ref", "HEAD"),
        )
        render_status(self.coordination, project="ai-dev", ticket="issue-51")
        render_rail(self.coordination, project="ai-dev", ticket="issue-51", rail="remote-only-rail")
        after = (
            resolve_current_head(self.coordination),
            self._git(self.coordination, "status", "--porcelain"),
            self._git(self.coordination, "rev-parse", "--abbrev-ref", "HEAD"),
        )
        self.assertEqual(before, after)

    # Handoff indicator: optimistic compare-and-swap on one mechanical counter

    def _seed_counter(self, upstream: Path, value: str = "5\n") -> None:
        target = upstream / "ai-dev" / "issue-51" / "proceed-sequence.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value, encoding="utf-8")
        self._git(upstream, "add", "--", "ai-dev/issue-51/proceed-sequence.txt")
        self._git(upstream, "commit", "-q", "-m", "seed counter")
        self._git(self.coordination, "fetch", "-q", "origin")
        self._git(self.coordination, "merge", "--ff-only", "-q", "origin/main")

    def _remote_counter(self, upstream: Path) -> str:
        return (upstream / "ai-dev" / "issue-51" / "proceed-sequence.txt").read_text(encoding="utf-8")

    def _allocate(self, **kwargs: object) -> int:
        return allocate_proceed_number(self.coordination, project="ai-dev", ticket="issue-51", **kwargs)  # type: ignore[arg-type]

    def test_counter_parsing_is_strict(self) -> None:
        self.assertEqual(parse_proceed_sequence("0\n"), 0)
        self.assertEqual(parse_proceed_sequence("42\n"), 42)
        for malformed in (None, "", "5", "abc\n", " 5\n", "5\n5\n", "-1\n", "05\n", "5 \n", "\n"):
            with self.subTest(malformed=malformed), self.assertRaises(ControlPlaneError):
                parse_proceed_sequence(malformed)

    def test_missing_counter_fails_closed(self) -> None:
        self._attach_shared_upstream("upstream-no-counter")
        with self.assertRaises(ControlPlaneError) as caught:
            self._allocate()
        self.assertIn("is missing", str(caught.exception))

    def test_malformed_remote_counter_fails_closed(self) -> None:
        upstream = self._attach_shared_upstream("upstream-bad-counter")
        self._seed_counter(upstream, "not-a-number\n")
        with self.assertRaises(ControlPlaneError) as caught:
            self._allocate()
        self.assertIn("non-negative decimal integer", str(caught.exception))

    def test_sequential_allocations_are_increasing_and_unique(self) -> None:
        upstream = self._attach_shared_upstream("upstream-sequential")
        self._seed_counter(upstream)
        allocated = [self._allocate() for _ in range(3)]
        self.assertEqual(allocated, [6, 7, 8])
        self.assertEqual(len(set(allocated)), 3)
        self.assertEqual(self._git(self.coordination, "show", "origin/main:ai-dev/issue-51/proceed-sequence.txt"), "8")

    def test_concurrent_allocators_resolve_conflict_with_distinct_values(self) -> None:
        upstream = self._attach_shared_upstream("upstream-concurrent")
        self._seed_counter(upstream)
        rival = self.tmp_path / "rival"
        self._git(self.tmp_path, "clone", "-q", str(upstream), str(rival))
        self._git(rival, "config", "user.name", "Rival")
        self._git(rival, "config", "user.email", "rival@example.com")

        original = control_plane._build_counter_commit
        raced: list[int] = []
        armed = [True]

        def race_once(repo_root: Path, **kwargs: object) -> str:
            commit = original(repo_root, **kwargs)  # type: ignore[arg-type]
            if armed[0]:
                # A rival allocator wins the counter between our build and our push.
                armed[0] = False
                raced.append(allocate_proceed_number(rival, project="ai-dev", ticket="issue-51"))
            return commit

        with patch.object(control_plane, "_build_counter_commit", side_effect=race_once):
            allocated = self._allocate()

        self.assertEqual(raced, [6], "the rival must have taken the first value")
        self.assertEqual(allocated, 7, "the loser must retry and take a distinct higher value")
        self.assertGreater(allocated, raced[0])

    def test_retry_exhaustion_fails_closed_without_claiming_a_number(self) -> None:
        upstream = self._attach_shared_upstream("upstream-exhausted")
        self._seed_counter(upstream)
        rival = self.tmp_path / "rival-always"
        self._git(self.tmp_path, "clone", "-q", str(upstream), str(rival))
        self._git(rival, "config", "user.name", "Rival")
        self._git(rival, "config", "user.email", "rival@example.com")

        original = control_plane._build_counter_commit
        racing = [False]

        def always_race(repo_root: Path, **kwargs: object) -> str:
            commit = original(repo_root, **kwargs)  # type: ignore[arg-type]
            if not racing[0]:
                racing[0] = True
                try:
                    allocate_proceed_number(rival, project="ai-dev", ticket="issue-51")
                finally:
                    racing[0] = False
            return commit

        with patch.object(control_plane, "_build_counter_commit", side_effect=always_race):
            with self.assertRaises(ControlPlaneError) as caught:
                self._allocate(attempts=2)
        self.assertIn("No number was allocated", str(caught.exception))

    def test_allocation_retry_does_not_republish_the_handoff(self) -> None:
        upstream = self._attach_shared_upstream("upstream-no-republish")
        self._seed_counter(upstream)
        self._publish(artifact="handoff", role="executor", content="# Handoff\n\npublished once\n")
        self._git(self.coordination, "push", "-q", "origin", "main")
        handoff_path = "ai-dev/issue-51/rails/control-plane-surface/handoff.md"
        revisions_before = self._git(self.coordination, "rev-list", "--count", "origin/main", "--", handoff_path)

        rival = self.tmp_path / "rival-republish"
        self._git(self.tmp_path, "clone", "-q", str(upstream), str(rival))
        self._git(rival, "config", "user.name", "Rival")
        self._git(rival, "config", "user.email", "rival@example.com")
        original = control_plane._build_counter_commit
        raced: list[int] = []
        armed = [True]

        def race_once(repo_root: Path, **kwargs: object) -> str:
            commit = original(repo_root, **kwargs)  # type: ignore[arg-type]
            if armed[0]:
                armed[0] = False
                raced.append(allocate_proceed_number(rival, project="ai-dev", ticket="issue-51"))
            return commit

        with patch.object(control_plane, "_build_counter_commit", side_effect=race_once):
            self._allocate()

        self._git(self.coordination, "fetch", "-q", "origin")
        revisions_after = self._git(self.coordination, "rev-list", "--count", "origin/main", "--", handoff_path)
        self.assertEqual(revisions_before, revisions_after)
        self.assertEqual(
            self._git(self.coordination, "show", f"origin/main:{handoff_path}").strip().splitlines()[-1],
            "published once",
        )

    def test_no_indicator_is_printed_when_allocation_fails(self) -> None:
        self._attach_shared_upstream("upstream-print-guard")
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(ControlPlaneError):
                control_plane.main(
                    ["proceed", "--repo", str(self.coordination), "--project", "ai-dev", "--ticket", "issue-51"]
                )
        self.assertNotIn("proceed ", stdout.getvalue())

    def test_successful_allocation_prints_only_the_allocated_indicator(self) -> None:
        upstream = self._attach_shared_upstream("upstream-print-success")
        self._seed_counter(upstream)
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            control_plane.main(
                ["proceed", "--repo", str(self.coordination), "--project", "ai-dev", "--ticket", "issue-51"]
            )
        self.assertEqual(stdout.getvalue().strip(), "proceed 6")

    def test_allocation_does_not_move_or_dirty_the_local_repository(self) -> None:
        upstream = self._attach_shared_upstream("upstream-no-local-move")
        self._seed_counter(upstream)
        before = (
            resolve_current_head(self.coordination),
            self._git(self.coordination, "status", "--porcelain"),
            self._git(self.coordination, "rev-parse", "--abbrev-ref", "HEAD"),
        )
        self._allocate()
        after = (
            resolve_current_head(self.coordination),
            self._git(self.coordination, "status", "--porcelain"),
            self._git(self.coordination, "rev-parse", "--abbrev-ref", "HEAD"),
        )
        self.assertEqual(before, after)
        self.assertEqual(self._remote_counter(upstream), "6\n")

    def test_allocation_without_upstream_fails_closed(self) -> None:
        with self.assertRaises(ControlPlaneError) as caught:
            self._allocate()
        self.assertIn("no tracked upstream", str(caught.exception))

    def test_unpushed_local_commits_block_allocation(self) -> None:
        upstream = self._attach_shared_upstream("upstream-unpushed")
        self._seed_counter(upstream)
        self._publish(artifact="handoff", role="executor", content="# Handoff\n\nunpushed\n")
        with self.assertRaises(ControlPlaneError) as caught:
            self._allocate()
        self.assertIn("not on", str(caught.exception))

    # Configuration discovery: a fresh agent must find its coordination repository

    def _product_repo(self, control_plane: dict[str, object] | None, *, active_issue: int | None = 51) -> Path:
        self._product_repo_count = getattr(self, "_product_repo_count", 0) + 1
        product = self._init_repo(f"configured-product-{self._product_repo_count}")
        config: dict[str, object] = {"tickets": {"provider": "github", "repository": "owner/name"}}
        if control_plane is not None:
            config["controlPlane"] = control_plane
        (product / ".ai-dev").mkdir(parents=True, exist_ok=True)
        (product / ".ai-dev" / "config.json").write_text(json.dumps(config), encoding="utf-8")
        state: dict[str, object] = {"mainBranch": "main", "scratchBranch": "scratch", "checkpoint": 0}
        if active_issue is not None:
            state["activeIssueNumber"] = active_issue
        (product / ".ai-dev" / "workflow.json").write_text(json.dumps(state), encoding="utf-8")
        return product

    def test_unconfigured_repository_reports_no_control_plane(self) -> None:
        product = self._product_repo(None)
        self.assertIsNone(resolve_control_plane_config(product))

    def test_configured_repository_resolves_scope_without_conversation(self) -> None:
        product = self._product_repo({"repository": str(self.coordination), "project": "ai-dev"})
        configured = resolve_control_plane_config(product)
        self.assertIsNotNone(configured)
        assert configured is not None
        self.assertEqual(configured.repository, self.coordination)
        self.assertEqual(configured.project, "ai-dev")
        self.assertEqual(configured.ticket, "issue-51")

    def test_explicit_ticket_overrides_the_active_issue(self) -> None:
        product = self._product_repo(
            {"repository": str(self.coordination), "project": "ai-dev", "ticket": "issue-77"}
        )
        configured = resolve_control_plane_config(product)
        assert configured is not None
        self.assertEqual(configured.ticket, "issue-77")

    def test_relative_coordination_path_resolves_against_the_product_repository(self) -> None:
        product = self._product_repo({"repository": "../coordination", "project": "ai-dev"})
        configured = resolve_control_plane_config(product)
        assert configured is not None
        self.assertEqual(configured.repository, self.coordination)

    def test_incomplete_or_unknown_configuration_fails_closed(self) -> None:
        for block in (
            {"project": "ai-dev"},
            {"repository": str(self.coordination)},
            {"repository": str(self.coordination), "project": "ai-dev", "extra": "value"},
            {"repository": "", "project": "ai-dev"},
        ):
            with self.subTest(block=block), self.assertRaises(ControlPlaneError):
                resolve_control_plane_config(self._product_repo(block))

    def test_missing_ticket_without_active_issue_fails_closed(self) -> None:
        product = self._product_repo({"repository": str(self.coordination), "project": "ai-dev"}, active_issue=None)
        with self.assertRaises(ControlPlaneError) as caught:
            resolve_control_plane_config(product)
        self.assertIn("ticket is required", str(caught.exception))

    def test_non_repository_path_is_refused(self) -> None:
        plain = self.tmp_path / "plain"
        plain.mkdir()
        with self.assertRaises(ControlPlaneError):
            resolve_coordination_repo(plain)
        with self.assertRaises(ControlPlaneError):
            resolve_coordination_repo(self.tmp_path / "missing")


# --------------------------------------------------------------------------
# The supported progress action
# --------------------------------------------------------------------------


class ProgressActionTests(unittest.TestCase):
    """Accepting a checkpoint is a durable control-plane transition, so it is tested here.

    These are the boundary properties, not the measure: who may publish a
    progress record, where it lands, that the reader looks exactly there, that a
    concurrent record cannot be lost, and that none of it touches the product
    worktree. What the published history comes to is `progress_view`'s question.
    """

    PROJECT = "ai-dev"
    TICKET = "issue-55"

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp_path = Path(self._tmpdir.name)
        self.coordination = self._repo("coordination")
        self.product = self._repo("product")
        self.checkpoints = {}
        for number in (52, 53, 54):
            self._git(self.product, "commit", "-q", "--allow-empty", "-m", str(number))
            self.checkpoints[number] = self._git(self.product, "rev-parse", "HEAD")

    def _git(self, repo_root: Path, *args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            check=True, text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return completed.stdout.strip()

    def _repo(self, name: str) -> Path:
        repo_root = self.tmp_path / name
        repo_root.mkdir(parents=True)
        self._git(repo_root, "init", "-q")
        self._git(repo_root, "config", "user.name", "Progress Action Tests")
        self._git(repo_root, "config", "user.email", "progress-action@example.com")
        (repo_root / "README.md").write_text("{0}\n".format(name), encoding="utf-8")
        self._git(repo_root, "add", "README.md")
        self._git(repo_root, "commit", "-q", "-m", "initial commit")
        return repo_root

    def accept(self, **overrides: object) -> tuple[Path, str, dict]:
        arguments: dict = {
            "project": self.PROJECT, "ticket": self.TICKET,
            "remaining": 12, "confidence": "low", "note": "",
            "state": "# Control Plane State\n\nProject: ai-dev\n",
            "product_repo": self.product,
        }
        arguments.update(overrides)
        return accept_progress(self.coordination, **arguments)  # type: ignore[arg-type]

    # -- ownership and placement ------------------------------------------

    def test_the_progress_record_is_an_orchestrator_owned_scope_artifact(self) -> None:
        self.assertEqual(ARTIFACT_OWNERS["progress"], "orchestrator")
        self.assertNotIn("progress", control_plane.RAIL_SCOPED_ARTIFACTS)
        # Stronger than ownership: no role may publish it, because it is one half
        # of an acceptance and `accept` writes both halves in a single commit.
        for role in ("executor", "orchestrator"):
            with self.assertRaises(ControlPlaneError) as caught:
                publish(
                    self.coordination, project=self.PROJECT, ticket=self.TICKET,
                    artifact="progress", role=role, content="{}\n",
                )
            self.assertIn(
                "written with the accepted state by `accept`", str(caught.exception)
            )

    def test_the_reader_looks_exactly_where_the_action_publishes(self) -> None:
        """One path, composed twice, pinned together rather than kept in step by hand."""
        target, _head, _document = self.accept(
            checkpoint=52, commit=self.checkpoints[52], remaining=12
        )
        relative = target.relative_to(self.coordination).as_posix()
        self.assertEqual(relative, progress_relative(self.PROJECT, self.TICKET))
        self.assertEqual(
            relative,
            artifact_relative(
                project=self.PROJECT, ticket=self.TICKET, artifact="progress", rail=None
            ),
        )
        store = ProgressStore.for_scope(
            self.coordination, project=self.PROJECT, ticket=self.TICKET
        )
        self.assertEqual(store.relative, relative)
        self.assertEqual(store.facts().acceptances[-1].checkpoint, 52)

    def test_the_action_commits_the_state_and_the_record_and_nothing_else(self) -> None:
        """Both halves of the acceptance, in one commit, and no third path.

        This asserted a single path while the record was separately publishable.
        Pairing them is the fix, so the property is now "exactly these two".
        """
        _target, head, _document = self.accept(
            checkpoint=52, commit=self.checkpoints[52], remaining=12
        )
        changed = sorted(self._git(
            self.coordination, "show", "--name-only", "--format=", head
        ).split())
        self.assertEqual(changed, sorted([
            progress_relative(self.PROJECT, self.TICKET),
            artifact_relative(
                project=self.PROJECT, ticket=self.TICKET, artifact="state", rail=None
            ),
        ]))
        self.assertEqual(self._git(self.coordination, "status", "--porcelain"), "")

    def test_a_record_this_action_would_refuse_cannot_be_published_by_hand(self) -> None:
        """`publish` validates the record too, so the artifact has one gate, not two."""
        for content in (
            "{}\n",
            json.dumps({"schemaVersion": 1, "accepted": None, "named": None, "projection": None}),
            json.dumps({
                "schemaVersion": 1, "accepted": None, "named": None,
                "projection": {"confidence": "urgent", "note": "", "remaining": 1},
            }),
            json.dumps({
                "schemaVersion": 1, "accepted": None, "named": None, "diary": "what happened",
                "projection": {"confidence": "low", "note": "", "remaining": 1},
            }),
        ):
            with self.subTest(content=content[:40]), self.assertRaises(ControlPlaneError):
                publish(
                    self.coordination, project=self.PROJECT, ticket=self.TICKET,
                    artifact="progress", role="orchestrator", content=content,
                )
        self.assertFalse(
            (self.coordination / progress_relative(self.PROJECT, self.TICKET)).exists()
        )

    # -- the writer model --------------------------------------------------

    def test_a_record_that_landed_in_between_refuses_this_one_rather_than_losing_it(
        self,
    ) -> None:
        """The writer model, proven rather than asserted.

        Publication carries the head the action read its current state from, so
        two writers cannot both compose from one state and have the second
        silently overwrite the first. The racing record here is a projection
        reconsideration, which passes every value check the losing writer makes --
        so the only thing that can stop it is the head it read, and it is stopped.
        Nothing is lost, the reconsideration survives, and no lock was involved:
        the coordination repository's own history is what serializes the two.
        """
        self.accept(checkpoint=52, commit=self.checkpoints[52], remaining=12)
        real = control_plane.resolve_read_source
        landed = []

        def racing(repo_root):
            source = real(repo_root)
            if not landed:
                # Marked before the nested call, so the other writer reads the
                # unpatched source and this stays one race rather than a loop.
                landed.append(True)
                self.accept(remaining=9, note="reconsidered while the other composed")
            return source

        with patch.object(control_plane, "resolve_read_source", racing):
            with self.assertRaises(ControlPlaneError) as caught:
                self.accept(checkpoint=53, commit=self.checkpoints[53], remaining=11)
        self.assertIn("expected head", str(caught.exception))

        store = ProgressStore.for_scope(
            self.coordination, project=self.PROJECT, ticket=self.TICKET
        )
        facts = store.facts()
        self.assertEqual([entry.checkpoint for entry in facts.acceptances], [52])
        self.assertEqual(facts.projections[-1].remaining, 9)
        self.assertEqual(
            facts.projections[-1].note, "reconsidered while the other composed"
        )

    def test_the_action_holds_no_lock_and_needs_none(self) -> None:
        """No lock file, no lock directory, and nothing left behind to recover."""
        self.accept(checkpoint=52, commit=self.checkpoints[52], remaining=12)
        source = Path(control_plane.__file__).read_text(encoding="utf-8")
        for forbidden in ("lock", "flock", "O_EXCL"):
            self.assertNotIn(forbidden, source.lower().split("accept_progress")[-1][:4000])

    # -- the product worktree ----------------------------------------------

    def test_accepting_a_checkpoint_writes_nothing_into_the_product_worktree(self) -> None:
        """Cleanliness by construction: the action never opens the product for writing."""
        before = self._tree(self.product)
        self.accept(checkpoint=52, commit=self.checkpoints[52], remaining=12)
        self.accept(named=6, named_total=9, remaining=12)
        self.accept(checkpoint=53, commit=self.checkpoints[53], remaining=11)
        self.assertEqual(self._tree(self.product), before)
        self.assertEqual(self._git(self.product, "status", "--porcelain"), "")
        self.assertFalse((self.product / ".ai-dev").exists())

    def test_reading_the_published_history_writes_nothing_anywhere(self) -> None:
        self.accept(checkpoint=52, commit=self.checkpoints[52], remaining=12)
        store = ProgressStore.for_scope(
            self.coordination, project=self.PROJECT, ticket=self.TICKET
        )
        before = (self._tree(self.product), self._tree(self.coordination))
        head = resolve_current_head(self.coordination)
        store.facts()
        store.facts()
        self.assertEqual((self._tree(self.product), self._tree(self.coordination)), before)
        self.assertEqual(resolve_current_head(self.coordination), head)

    def _tree(self, root: Path) -> dict:
        return {
            str(item.relative_to(root)): item.read_bytes()
            for item in sorted(root.rglob("*"))
            if item.is_file() and ".git" not in item.relative_to(root).parts
        }


if __name__ == "__main__":
    unittest.main()

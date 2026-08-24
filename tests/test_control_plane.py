from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from ai_dev_flow.control_plane import (
    ControlPlaneError,
    publish,
    resolve_control_plane_config,
    render_rail,
    render_status,
    resolve_coordination_repo,
    resolve_current_head,
    validate_evidence_projection,
    validate_identifier,
)


SAFE_EVIDENCE = {
    "schemaVersion": 1,
    "provenance": {"source": "provider-adapter", "collectedAt": "2026-08-24T15:00:00Z", "turnId": "turn-7"},
    "sourceHealth": {"status": "partial", "detail": "one source unavailable"},
    "observations": [{"kind": "terminal-action", "count": 17, "status": "completed"}],
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
            "content": "# Rail\n\nbounded work\n", "rail": "control-plane-surface",
        }
        arguments.update(overrides)
        return publish(self.coordination, **arguments)  # type: ignore[arg-type]

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
        self._publish(rail="control-plane-surface", content="# Rail A\n\nmine\n")
        self._publish(rail="provider-evidence-intake", content="# Rail B\n\nsibling-only-content\n")
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
        self.assertIn("- control-plane-surface: rail, handoff", rendered)
        self.assertIn("- provider-evidence-intake: rail", rendered)

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
        self._git(self.coordination, "remote", "add", "origin", str(upstream))
        self._git(self.coordination, "fetch", "-q", "origin")
        self._git(self.coordination, "branch", "--set-upstream-to", "origin/main", "main")
        return upstream

    def _advance_upstream(self, upstream: Path) -> None:
        (upstream / "remote-change.txt").write_text("remote\n", encoding="utf-8")
        self._git(upstream, "add", "remote-change.txt")
        self._git(upstream, "commit", "-q", "-m", "remote advance")

    def test_upstream_ahead_fails_closed_using_freshly_fetched_state(self) -> None:
        upstream = self._attach_shared_upstream("upstream-ahead")
        # The remote moves after the local repository last observed it.
        self._advance_upstream(upstream)
        with self.assertRaises(ControlPlaneError) as caught:
            self._publish(artifact="handoff", role="executor", content="# Handoff\n")
        self.assertIn("is ahead of main", str(caught.exception))

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

    # Isolation

    def test_operations_never_touch_the_product_repository(self) -> None:
        product = self._init_repo("product")
        product_head = resolve_current_head(product)
        self._publish(artifact="state", role="orchestrator", rail=None, content="# Accepted\n")
        self._publish(artifact="handoff", role="executor", content="# Handoff\n")
        self.assertEqual(resolve_current_head(product), product_head)
        self.assertEqual(self._git(product, "status", "--porcelain"), "")
        self.assertEqual(self._git(product, "rev-parse", "--abbrev-ref", "HEAD"), "main")

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


if __name__ == "__main__":
    unittest.main()

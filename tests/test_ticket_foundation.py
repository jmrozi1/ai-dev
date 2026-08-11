from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ai_dev_flow.ticket_config import (
    GitHubCurrentTicketConfiguration,
    GitHubTicketConfiguration,
    LocalTicketConfiguration,
    TicketConfigError,
    load_ticket_configuration,
    load_ticket_configuration_for_repo_root,
)
from ai_dev_flow.ticket_providers import (
    GitHubTicketProviderBinding,
    LocalTicketProviderBinding,
    TicketProviderError,
    resolve_ticket_provider,
    resolve_ticket_provider_for_reference,
)
from ai_dev_flow.tickets import (
    Ticket,
    TicketModelError,
    TicketReference,
    normalize_ticket_reference_data,
)
from ai_dev_flow.workflow_state import WorkflowState, WorkflowStateError, normalize_and_validate


class TicketConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _write_config(self, payload: object, *, name: str) -> Path:
        path = self.tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return path

    def test_valid_local_configuration(self) -> None:
        path = self._write_config(
            {
                "tickets": {
                    "provider": "local",
                    "path": ".ai-dev/tickets",
                }
            },
            name="tickets.local.json",
        )

        config = load_ticket_configuration(path)
        self.assertEqual(
            config,
            LocalTicketConfiguration(provider="local", path=".ai-dev/tickets"),
        )

    def test_valid_github_explicit_configuration(self) -> None:
        path = self._write_config(
            {
                "tickets": {
                    "provider": "github",
                    "repository": "jmrozi1/ai-dev",
                }
            },
            name="tickets.github.json",
        )

        config = load_ticket_configuration(path)
        self.assertEqual(
            config,
            GitHubTicketConfiguration(provider="github", repository="jmrozi1/ai-dev"),
        )

    def test_valid_github_current_configuration(self) -> None:
        path = self._write_config(
            {
                "tickets": {
                    "provider": "github-current",
                }
            },
            name="tickets.github-current.json",
        )

        config = load_ticket_configuration(path)
        self.assertEqual(
            config,
            GitHubCurrentTicketConfiguration(provider="github-current"),
        )

    def test_missing_configuration_file_is_error(self) -> None:
        missing_path = self.tmp_path / "missing.json"
        with self.assertRaisesRegex(TicketConfigError, "Missing ticket configuration file"):
            load_ticket_configuration(missing_path)

    def test_malformed_configuration_is_error(self) -> None:
        path = self.tmp_path / "malformed.json"
        path.write_text("{\n", encoding="utf-8")
        with self.assertRaisesRegex(TicketConfigError, "Invalid JSON"):
            load_ticket_configuration(path)

    def test_unknown_provider_is_error(self) -> None:
        path = self._write_config(
            {
                "tickets": {
                    "provider": "jira",
                }
            },
            name="tickets.unknown.json",
        )
        with self.assertRaisesRegex(TicketConfigError, "unsupported provider"):
            load_ticket_configuration(path)

    def test_local_invalid_path_is_error(self) -> None:
        path = self._write_config(
            {
                "tickets": {
                    "provider": "local",
                    "path": "../tickets",
                }
            },
            name="tickets.local.invalid-path.json",
        )
        with self.assertRaisesRegex(TicketConfigError, "path cannot traverse parent directories"):
            load_ticket_configuration(path)

    def test_github_missing_repository_is_error(self) -> None:
        path = self._write_config(
            {
                "tickets": {
                    "provider": "github",
                }
            },
            name="tickets.github.missing-repo.json",
        )
        with self.assertRaisesRegex(TicketConfigError, "requires tickets.repository"):
            load_ticket_configuration(path)

    def test_github_invalid_repository_format_is_error(self) -> None:
        path = self._write_config(
            {
                "tickets": {
                    "provider": "github",
                    "repository": "jmrozi1",
                }
            },
            name="tickets.github.invalid-repo.json",
        )
        with self.assertRaisesRegex(TicketConfigError, "owner/repo"):
            load_ticket_configuration(path)

    def test_provider_specific_conflicting_fields_are_error(self) -> None:
        local_with_repository = self._write_config(
            {
                "tickets": {
                    "provider": "local",
                    "path": ".ai-dev/tickets",
                    "repository": "jmrozi1/ai-dev",
                }
            },
            name="tickets.local.conflict.json",
        )
        with self.assertRaisesRegex(TicketConfigError, "does not allow tickets.repository"):
            load_ticket_configuration(local_with_repository)

        github_current_with_repository = self._write_config(
            {
                "tickets": {
                    "provider": "github-current",
                    "repository": "jmrozi1/ai-dev",
                }
            },
            name="tickets.github-current.conflict.json",
        )
        with self.assertRaisesRegex(TicketConfigError, "does not allow tickets.repository"):
            load_ticket_configuration(github_current_with_repository)

    def test_repo_root_loader_reads_tickets_from_existing_config_json(self) -> None:
        repo_root = self.tmp_path / "repo-root-config"
        config_path = repo_root / ".ai-dev" / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps(
                {
                    "out": "logs/out.txt",
                    "tickets": {
                        "provider": "github",
                        "repository": "jmrozi1/ai-dev",
                    },
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        config = load_ticket_configuration_for_repo_root(repo_root)
        self.assertEqual(
            config,
            GitHubTicketConfiguration(provider="github", repository="jmrozi1/ai-dev"),
        )


class TicketReferenceTests(unittest.TestCase):
    def test_valid_local_reference(self) -> None:
        reference = normalize_ticket_reference_data(
            {
                "provider": "local",
                "ticketId": "12",
                "path": ".ai-dev/tickets",
            },
            context="test local",
        )
        self.assertEqual(reference.provider, "local")
        self.assertEqual(reference.ticket_id, "12")
        self.assertIsNone(reference.repository)
        self.assertEqual(reference.path, ".ai-dev/tickets")

    def test_valid_resolved_github_reference(self) -> None:
        reference = normalize_ticket_reference_data(
            {
                "provider": "github",
                "ticketId": "10",
                "repository": "jmrozi1/ai-dev",
                "url": "https://github.com/jmrozi1/ai-dev/issues/10",
            },
            context="test github",
        )
        self.assertEqual(reference.provider, "github")
        self.assertEqual(reference.ticket_id, "10")
        self.assertEqual(reference.repository, "jmrozi1/ai-dev")

    def test_direct_github_reference_construction_requires_repository(self) -> None:
        with self.assertRaisesRegex(TicketModelError, "requires repository"):
            TicketReference(provider="github", ticket_id="10")

    def test_direct_local_reference_construction_rejects_repository(self) -> None:
        with self.assertRaisesRegex(TicketModelError, "does not allow repository"):
            TicketReference(
                provider="local",
                ticket_id="12",
                repository="jmrozi1/ai-dev",
            )

    def test_direct_local_reference_construction_preserves_path(self) -> None:
        reference = TicketReference(
            provider="local",
            ticket_id="12",
            path=".ai-dev/tickets",
        )

        self.assertEqual(reference.path, ".ai-dev/tickets")

    def test_missing_ticket_id_is_error(self) -> None:
        with self.assertRaisesRegex(TicketModelError, "ticketId"):
            normalize_ticket_reference_data(
                {
                    "provider": "local",
                },
                context="missing id",
            )

    def test_github_missing_repository_is_error(self) -> None:
        with self.assertRaisesRegex(TicketModelError, "requires repository"):
            normalize_ticket_reference_data(
                {
                    "provider": "github",
                    "ticketId": "10",
                },
                context="missing repo",
            )

    def test_unknown_provider_is_error(self) -> None:
        with self.assertRaisesRegex(TicketModelError, "unsupported provider"):
            normalize_ticket_reference_data(
                {
                    "provider": "jira",
                    "ticketId": "123",
                },
                context="unknown provider",
            )

    def test_round_trip_serialization_through_workflow_state(self) -> None:
        state = WorkflowState(
            main_branch="main",
            scratch_branch="scratch",
            checkpoint=0,
            ticket_reference=TicketReference(
                provider="github",
                ticket_id="10",
                repository="jmrozi1/ai-dev",
                url="https://github.com/jmrozi1/ai-dev/issues/10",
            ),
        )

        payload = state.to_dict()
        self.assertIn("ticket", payload)
        reloaded = normalize_and_validate(payload, context="round-trip")
        self.assertEqual(reloaded, state)

    def test_round_trip_serialization_through_workflow_state_for_local_reference(self) -> None:
        state = WorkflowState(
            main_branch="main",
            scratch_branch="scratch",
            checkpoint=0,
            ticket_reference=TicketReference(
                provider="local",
                ticket_id="12",
                path=".ai-dev/tickets",
            ),
        )

        payload = state.to_dict()
        self.assertIn("ticket", payload)
        reloaded = normalize_and_validate(payload, context="round-trip local")
        self.assertEqual(reloaded, state)

    def test_workflow_state_allows_ticket_with_active_issue_compatibility_fields(self) -> None:
        state = normalize_and_validate(
            {
                "mainBranch": "main",
                "scratchBranch": "scratch",
                "checkpoint": 0,
                "activeIssueNumber": 10,
                "activeIssueTitle": "Issue 10",
                "activeIssueUrl": "https://github.com/jmrozi1/ai-dev/issues/10",
                "ticket": {
                    "provider": "github",
                    "ticketId": "10",
                    "repository": "jmrozi1/ai-dev",
                    "url": "https://github.com/jmrozi1/ai-dev/issues/10",
                },
            },
            context="compatibility fields",
        )

        assert state.ticket_reference is not None
        self.assertEqual(state.active_issue_number, 10)
        self.assertEqual(state.ticket_reference.ticket_id, "10")

    def test_workflow_state_allows_local_ticket_with_path(self) -> None:
        state = normalize_and_validate(
            {
                "mainBranch": "main",
                "scratchBranch": "scratch",
                "checkpoint": 0,
                "activeIssueNumber": 12,
                "ticket": {
                    "provider": "local",
                    "ticketId": "12",
                    "path": ".ai-dev/tickets",
                },
            },
            context="local compatibility fields",
        )

        assert state.ticket_reference is not None
        self.assertEqual(state.ticket_reference.path, ".ai-dev/tickets")

    def test_workflow_state_rejects_mismatched_ticket_and_active_issue_id(self) -> None:
        with self.assertRaisesRegex(WorkflowStateError, "ticket.ticketId must match activeIssueNumber"):
            normalize_and_validate(
                {
                    "mainBranch": "main",
                    "scratchBranch": "scratch",
                    "checkpoint": 0,
                    "activeIssueNumber": 10,
                    "ticket": {
                        "provider": "github",
                        "ticketId": "11",
                        "repository": "jmrozi1/ai-dev",
                    },
                },
                context="mismatched ticket id",
            )

    def test_workflow_state_ticket_missing_id_is_error(self) -> None:
        with self.assertRaisesRegex(WorkflowStateError, "ticketId"):
            normalize_and_validate(
                {
                    "mainBranch": "main",
                    "scratchBranch": "scratch",
                    "checkpoint": 0,
                    "ticket": {
                        "provider": "local",
                    },
                },
                context="workflow missing ticket id",
            )

    def test_workflow_state_github_ticket_missing_repository_is_error(self) -> None:
        with self.assertRaisesRegex(WorkflowStateError, "requires repository"):
            normalize_and_validate(
                {
                    "mainBranch": "main",
                    "scratchBranch": "scratch",
                    "checkpoint": 0,
                    "ticket": {
                        "provider": "github",
                        "ticketId": "10",
                    },
                },
                context="workflow missing github repository",
            )

    def test_workflow_state_unknown_ticket_provider_is_error(self) -> None:
        with self.assertRaisesRegex(WorkflowStateError, "unsupported provider"):
            normalize_and_validate(
                {
                    "mainBranch": "main",
                    "scratchBranch": "scratch",
                    "checkpoint": 0,
                    "ticket": {
                        "provider": "jira",
                        "ticketId": "10",
                    },
                },
                context="workflow unknown provider",
            )


class TicketModelTests(unittest.TestCase):
    def _reference(self) -> TicketReference:
        return TicketReference(
            provider="github",
            ticket_id="10",
            repository="jmrozi1/ai-dev",
        )

    def test_direct_ticket_construction_rejects_empty_title(self) -> None:
        with self.assertRaisesRegex(TicketModelError, "title is required"):
            Ticket(
                reference=self._reference(),
                title="   ",
                body=None,
                acceptance_criteria=(),
                labels=(),
                lifecycle_state="open",
                workflow_state="inactive",
                block_reason=None,
                created_at=None,
                updated_at=None,
                closed_at=None,
            )

    def test_direct_ticket_construction_rejects_invalid_lifecycle_state(self) -> None:
        with self.assertRaisesRegex(TicketModelError, "lifecycle_state"):
            Ticket(
                reference=self._reference(),
                title="Valid title",
                body=None,
                acceptance_criteria=(),
                labels=(),
                lifecycle_state="pending",
                workflow_state="inactive",
                block_reason=None,
                created_at=None,
                updated_at=None,
                closed_at=None,
            )

    def test_direct_ticket_construction_rejects_invalid_workflow_state(self) -> None:
        with self.assertRaisesRegex(TicketModelError, "workflow_state"):
            Ticket(
                reference=self._reference(),
                title="Valid title",
                body=None,
                acceptance_criteria=(),
                labels=(),
                lifecycle_state="open",
                workflow_state="paused",
                block_reason=None,
                created_at=None,
                updated_at=None,
                closed_at=None,
            )


class ProviderRoutingTests(unittest.TestCase):
    def test_local_configuration_routes_to_local_provider(self) -> None:
        resolved = resolve_ticket_provider(
            LocalTicketConfiguration(provider="local", path=".ai-dev/tickets")
        )

        self.assertIsInstance(resolved, LocalTicketProviderBinding)
        self.assertEqual(resolved.tickets_path, ".ai-dev/tickets")

    def test_local_reference_routes_to_local_provider(self) -> None:
        reference = TicketReference(
            provider="local",
            ticket_id="10",
            path=".ai-dev/tickets",
        )

        provider = resolve_ticket_provider_for_reference(
            repo_root=Path("/tmp/repo"),
            reference=reference,
        )

        self.assertIsNotNone(provider)

    def test_github_configuration_routes_to_explicit_github_provider(self) -> None:
        resolved = resolve_ticket_provider(
            GitHubTicketConfiguration(provider="github", repository="jmrozi1/ai-dev")
        )

        self.assertIsInstance(resolved, GitHubTicketProviderBinding)
        self.assertEqual(resolved.repository_mode, "explicit")
        reference = resolved.resolve_reference("10")
        self.assertEqual(reference.provider, "github")
        self.assertEqual(reference.repository, "jmrozi1/ai-dev")

    def test_github_current_routes_to_github_resolution_mode(self) -> None:
        resolved = resolve_ticket_provider(
            GitHubCurrentTicketConfiguration(provider="github-current")
        )

        self.assertIsInstance(resolved, GitHubTicketProviderBinding)
        self.assertEqual(resolved.repository_mode, "current")

        with self.assertRaisesRegex(TicketProviderError, "github-current"):
            resolved.resolve_reference("10")

        class FakeResolver:
            def resolve_current_repository(self) -> str:
                return "jmrozi1/ai-dev"

        resolved_with_resolver = resolve_ticket_provider(
            GitHubCurrentTicketConfiguration(provider="github-current"),
            github_current_resolver=FakeResolver(),
        )
        self.assertIsInstance(resolved_with_resolver, GitHubTicketProviderBinding)
        reference = resolved_with_resolver.resolve_reference("10")
        self.assertEqual(reference.provider, "github")
        self.assertEqual(reference.repository, "jmrozi1/ai-dev")


if __name__ == "__main__":
    unittest.main()

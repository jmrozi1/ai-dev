from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from ai_dev_flow import cli
from ai_dev_flow.ticket_config import (
    GitHubCurrentTicketConfiguration,
    GitHubTicketConfiguration,
    LocalTicketConfiguration,
)
from ai_dev_flow.ticket_providers import (
    LocalTicketProviderBinding,
    TicketProviderError,
    instantiate_local_ticket_provider,
    instantiate_ticket_provider,
    resolve_ticket_provider,
)
from ai_dev_flow.tickets import TicketReference, normalize_ticket_data
from ai_dev_flow.workflow_state import WorkflowStateError


class _NeverResolveCurrentRepository:
    def resolve_current_repository(self) -> str:
        raise AssertionError("github-current resolver should not be called")


class _FixedCurrentRepositoryResolver:
    def __init__(self, repository: str) -> None:
        self.repository = repository

    def resolve_current_repository(self) -> str:
        return self.repository


class LocalTicketProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.repo_root = self.tmp_path / "repo"
        self.repo_root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _ticket_file(self, ticket_id: str, tickets_path: str = ".ai-dev/tickets") -> Path:
        return self.repo_root / tickets_path / f"{ticket_id}.json"

    def _write_ticket_file(
        self,
        ticket_id: str,
        payload: dict[str, object],
        *,
        tickets_path: str = ".ai-dev/tickets",
    ) -> Path:
        path = self._ticket_file(ticket_id, tickets_path=tickets_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return path

    def _ticket_payload(
        self,
        *,
        ticket_id: str,
        title: str,
        lifecycle_state: str = "open",
        workflow_state: str = "inactive",
        body: str | None = None,
        acceptance_criteria: tuple[str, ...] = (),
        labels: tuple[str, ...] = (),
        tickets_path: str = ".ai-dev/tickets",
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "reference": {
                "provider": "local",
                "ticketId": ticket_id,
                "path": tickets_path,
            },
            "title": title,
            "lifecycleState": lifecycle_state,
            "workflowState": workflow_state,
        }
        if body is not None:
            payload["body"] = body
        if acceptance_criteria:
            payload["acceptanceCriteria"] = list(acceptance_criteria)
        if labels:
            payload["labels"] = list(labels)
        return payload

    def _provider(self, tickets_path: str = ".ai-dev/tickets"):
        return instantiate_local_ticket_provider(
            repo_root=self.repo_root,
            binding=LocalTicketProviderBinding(tickets_path=tickets_path),
        )

    def test_first_and_second_create_write_separate_ticket_files(self) -> None:
        provider = self._provider()

        first = provider.create(title="First")
        second = provider.create(title="Second")

        self.assertEqual(first.ticket_id, "1")
        self.assertEqual(second.ticket_id, "2")
        self.assertTrue(self._ticket_file("1").exists())
        self.assertTrue(self._ticket_file("2").exists())
        self.assertNotEqual(
            self._ticket_file("1").read_text(encoding="utf-8"),
            self._ticket_file("2").read_text(encoding="utf-8"),
        )

    def test_existing_gap_allocates_max_plus_one_without_reuse(self) -> None:
        self._write_ticket_file("1", self._ticket_payload(ticket_id="1", title="one"))
        self._write_ticket_file("3", self._ticket_payload(ticket_id="3", title="three"))
        provider = self._provider()

        created = provider.create(title="next")

        self.assertEqual(created.ticket_id, "4")
        self.assertTrue(self._ticket_file("4").exists())
        self.assertFalse(self._ticket_file("2").exists())

    def test_create_never_overwrites_existing_ticket_file_on_collision(self) -> None:
        self._write_ticket_file("1", self._ticket_payload(ticket_id="1", title="one"))
        collision_path = self._ticket_file("2")

        provider = self._provider()

        def _simulate_midflight_collision(path: Path, data: dict[str, object]) -> bool:
            self._write_ticket_file("2", self._ticket_payload(ticket_id="2", title="concurrent"))
            return False

        with patch(
            "ai_dev_flow.ticket_providers._write_json_object_atomic_no_replace",
            side_effect=_simulate_midflight_collision,
        ):
            with self.assertRaisesRegex(TicketProviderError, "already exists"):
                provider.create(title="two")

        self.assertEqual(
            json.loads(collision_path.read_text(encoding="utf-8"))["title"],
            "concurrent",
        )

    def test_malformed_individual_ticket_file_fails_clearly(self) -> None:
        malformed = self._ticket_file("1")
        malformed.parent.mkdir(parents=True, exist_ok=True)
        malformed.write_text("{\n", encoding="utf-8")

        provider = self._provider()
        with self.assertRaisesRegex(TicketProviderError, "Invalid JSON"):
            provider.query()

    def test_persisted_ticket_round_trips_through_normalized_ticket(self) -> None:
        provider = self._provider()
        created = provider.create(
            title="Round trip",
            body="body text",
            acceptance_criteria=("a", "b"),
            labels=("x", "y"),
        )

        payload = json.loads(self._ticket_file(created.ticket_id).read_text(encoding="utf-8"))
        normalized = normalize_ticket_data(payload, context="round trip")
        loaded = provider.get(created.ticket_id)
        self.assertEqual(loaded, normalized)

    def test_query_default_returns_open_tickets_only(self) -> None:
        self._write_ticket_file("1", self._ticket_payload(ticket_id="1", title="open", lifecycle_state="open"))
        self._write_ticket_file("2", self._ticket_payload(ticket_id="2", title="closed", lifecycle_state="closed"))

        tickets = self._provider().query()
        self.assertEqual([ticket.reference.ticket_id for ticket in tickets], ["1"])

    def test_query_explicit_closed_lifecycle(self) -> None:
        self._write_ticket_file("1", self._ticket_payload(ticket_id="1", title="open", lifecycle_state="open"))
        self._write_ticket_file("2", self._ticket_payload(ticket_id="2", title="closed", lifecycle_state="closed"))

        tickets = self._provider().query(lifecycle_state="closed")
        self.assertEqual([ticket.reference.ticket_id for ticket in tickets], ["2"])

    def test_query_workflow_filter_is_exact(self) -> None:
        self._write_ticket_file("1", self._ticket_payload(ticket_id="1", title="inactive", workflow_state="inactive"))
        self._write_ticket_file("2", self._ticket_payload(ticket_id="2", title="blocked", workflow_state="blocked"))

        tickets = self._provider().query(workflow_state="blocked")
        self.assertEqual([ticket.reference.ticket_id for ticket in tickets], ["2"])

    def test_query_single_label_filter(self) -> None:
        self._write_ticket_file("1", self._ticket_payload(ticket_id="1", title="ops", labels=("ops",)))
        self._write_ticket_file("2", self._ticket_payload(ticket_id="2", title="docs", labels=("docs",)))

        tickets = self._provider().query(labels=("docs",))
        self.assertEqual([ticket.reference.ticket_id for ticket in tickets], ["2"])

    def test_query_repeated_labels_are_and_filtered(self) -> None:
        self._write_ticket_file("1", self._ticket_payload(ticket_id="1", title="a", labels=("ops", "p1")))
        self._write_ticket_file("2", self._ticket_payload(ticket_id="2", title="b", labels=("ops",)))

        tickets = self._provider().query(labels=("ops", "p1"))
        self.assertEqual([ticket.reference.ticket_id for ticket in tickets], ["1"])

    def test_query_text_matches_title_body_and_acceptance_criteria(self) -> None:
        self._write_ticket_file(
            "1",
            self._ticket_payload(ticket_id="1", title="Build cache fix", labels=("ops",)),
        )
        self._write_ticket_file(
            "2",
            self._ticket_payload(ticket_id="2", title="Doc task", body="update onboarding docs"),
        )
        self._write_ticket_file(
            "3",
            self._ticket_payload(
                ticket_id="3",
                title="Pipeline",
                acceptance_criteria=("must pass integration suite",),
            ),
        )

        provider = self._provider()
        self.assertEqual(
            [ticket.reference.ticket_id for ticket in provider.query(query_text="cache")],
            ["1"],
        )

        self.assertEqual(
            [ticket.reference.ticket_id for ticket in provider.query(query_text="ONBOARDING")],
            ["2"],
        )
        self.assertEqual(
            [ticket.reference.ticket_id for ticket in provider.query(query_text="integration suite")],
            ["3"],
        )

    def test_query_no_match_returns_empty_list(self) -> None:
        self._write_ticket_file("1", self._ticket_payload(ticket_id="1", title="First"))
        self.assertEqual(self._provider().query(query_text="does-not-match"), [])

    def test_get_existing_ticket(self) -> None:
        self._write_ticket_file("9", self._ticket_payload(ticket_id="9", title="existing"))
        ticket = self._provider().get("9")
        self.assertEqual(ticket.title, "existing")

    def test_get_missing_ticket_is_error(self) -> None:
        with self.assertRaisesRegex(TicketProviderError, "Local ticket not found"):
            self._provider().get("9")

    def test_get_invalid_id_is_error(self) -> None:
        with self.assertRaisesRegex(TicketProviderError, "ticket id must be a positive integer"):
            self._provider().get("not-a-number")

    def test_get_malformed_ticket_record_reports_useful_diagnostic(self) -> None:
        self._write_ticket_file(
            "1",
            {
                "reference": {"provider": "local", "ticketId": "1", "path": ".ai-dev/tickets"},
                "lifecycleState": "open",
                "workflowState": "inactive",
            },
        )
        with self.assertRaisesRegex(TicketProviderError, "title is required"):
            self._provider().get("1")

    def test_create_persists_minimum_title_only_ticket_defaults(self) -> None:
        created = self._provider().create(title="Minimum")
        payload = json.loads(self._ticket_file(created.ticket_id).read_text(encoding="utf-8"))
        self.assertEqual(payload["title"], "Minimum")
        self.assertEqual(payload["lifecycleState"], "open")
        self.assertEqual(payload["workflowState"], "inactive")
        self.assertIn("createdAt", payload)
        self.assertIn("updatedAt", payload)
        self.assertEqual(payload["reference"]["path"], ".ai-dev/tickets")

    def test_create_persists_body_acceptance_and_labels(self) -> None:
        created = self._provider().create(
            title="Detailed",
            body="Ticket body",
            acceptance_criteria=("criterion 1", "criterion 2"),
            labels=("label-a", "label-b"),
        )
        payload = json.loads(self._ticket_file(created.ticket_id).read_text(encoding="utf-8"))
        self.assertEqual(payload["body"], "Ticket body")
        self.assertEqual(payload["acceptanceCriteria"], ["criterion 1", "criterion 2"])
        self.assertEqual(payload["labels"], ["label-a", "label-b"])
        self.assertEqual(payload["reference"]["path"], ".ai-dev/tickets")

    def test_local_provider_create_with_non_default_path_persists_reference_path(self) -> None:
        provider = self._provider(tickets_path=".ai-dev/custom-tickets")

        created = provider.create(title="Custom path")

        self.assertEqual(created.path, ".ai-dev/custom-tickets")
        payload = json.loads(
            self._ticket_file(created.ticket_id, tickets_path=".ai-dev/custom-tickets").read_text(encoding="utf-8")
        )
        self.assertEqual(payload["reference"]["path"], ".ai-dev/custom-tickets")

class ProviderRoutingTests(unittest.TestCase):
    def test_provider_neutral_instantiation_uses_local_for_local_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            provider = instantiate_ticket_provider(
                repo_root=repo_root,
                config=LocalTicketConfiguration(provider="local", path=".ai-dev/tickets"),
            )
            reference = provider.create(title="from neutral factory")
            self.assertEqual(reference.provider, "local")
            self.assertEqual(reference.ticket_id, "1")
            self.assertEqual(reference.path, ".ai-dev/tickets")

    def test_provider_neutral_instantiation_uses_github_for_explicit_github_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            provider = instantiate_ticket_provider(
                repo_root=repo_root,
                config=GitHubTicketConfiguration(
                    provider="github",
                    repository="jmrozi1/ai-dev",
                ),
                github_current_resolver=_NeverResolveCurrentRepository(),
            )

            with patch("ai_dev_flow.ticket_providers._run_command") as run_mock:
                run_mock.return_value = subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout="https://github.com/jmrozi1/ai-dev/issues/22\n",
                    stderr="",
                )
                reference = provider.create(title="Create through explicit github")

            self.assertEqual(reference.provider, "github")
            self.assertEqual(reference.repository, "jmrozi1/ai-dev")
            self.assertEqual(reference.ticket_id, "22")

    def test_provider_neutral_instantiation_uses_github_current_resolver(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            provider = instantiate_ticket_provider(
                repo_root=repo_root,
                config=GitHubCurrentTicketConfiguration(provider="github-current"),
                github_current_resolver=_FixedCurrentRepositoryResolver("acme/current-repo"),
            )

            with patch("ai_dev_flow.ticket_providers._run_command") as run_mock:
                run_mock.return_value = subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout="https://github.com/acme/current-repo/issues/44\n",
                    stderr="",
                )
                reference = provider.create(title="Create through github-current")

            self.assertEqual(reference.provider, "github")
            self.assertEqual(reference.repository, "acme/current-repo")
            self.assertEqual(reference.ticket_id, "44")

    def test_provider_neutral_instantiation_explicit_mode_never_uses_current_resolver(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            provider = instantiate_ticket_provider(
                repo_root=repo_root,
                config=GitHubTicketConfiguration(provider="github", repository="acme/tickets"),
                github_current_resolver=_NeverResolveCurrentRepository(),
            )

            with patch("ai_dev_flow.ticket_providers._run_command") as run_mock:
                run_mock.return_value = subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "number": 3,
                            "title": "Title",
                            "url": "https://github.com/acme/tickets/issues/3",
                            "state": "OPEN",
                            "body": "Body",
                            "labels": [],
                            "createdAt": "2026-08-10T00:00:00Z",
                            "updatedAt": "2026-08-10T00:00:00Z",
                            "closedAt": None,
                        }
                    ),
                    stderr="",
                )
                ticket = provider.get("3")

            self.assertEqual(ticket.reference.repository, "acme/tickets")

    def test_github_create_renders_existing_acceptance_criteria_convention(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            provider = instantiate_ticket_provider(
                repo_root=repo_root,
                config=GitHubTicketConfiguration(provider="github", repository="acme/tickets"),
            )

            captured_commands: list[list[str]] = []

            def _run_side_effect(command: list[str]):
                cmd = list(command)
                captured_commands.append(cmd)
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="https://github.com/acme/tickets/issues/21\n",
                    stderr="",
                )

            with patch("ai_dev_flow.ticket_providers._run_command", side_effect=_run_side_effect):
                provider.create(
                    title="Acceptance format",
                    body="Body text",
                    acceptance_criteria=("criterion A", "criterion B"),
                )

            gh_create_calls = [
                call for call in captured_commands if call[:3] == ["gh", "issue", "create"]
            ]
            self.assertEqual(len(gh_create_calls), 1)
            create_call = gh_create_calls[0]
            self.assertIn("--body", create_call)
            rendered_body = create_call[create_call.index("--body") + 1]
            self.assertIn("## Acceptance criteria", rendered_body)
            self.assertIn("- [ ] criterion A", rendered_body)
            self.assertIn("- [ ] criterion B", rendered_body)

    def test_github_get_parses_existing_acceptance_section_and_preserves_other_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            provider = instantiate_ticket_provider(
                repo_root=repo_root,
                config=GitHubTicketConfiguration(provider="github", repository="acme/tickets"),
            )

            issue_body = "\n".join(
                [
                    "Problem summary.",
                    "",
                    "## Acceptance criteria",
                    "- [ ] criterion A",
                    "- [x] criterion B",
                    "",
                    "## Notes",
                    "Carry-over details.",
                    "",
                    "### Lower heading remains in body",
                    "Still part of retained body.",
                ]
            )

            with patch("ai_dev_flow.ticket_providers._run_command") as run_mock:
                run_mock.return_value = subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "number": 11,
                            "title": "Existing body parse",
                            "url": "https://github.com/acme/tickets/issues/11",
                            "state": "OPEN",
                            "body": issue_body,
                            "labels": [{"name": "active"}],
                            "createdAt": "2026-08-10T00:00:00Z",
                            "updatedAt": "2026-08-10T01:00:00Z",
                            "closedAt": None,
                        }
                    ),
                    stderr="",
                )
                ticket = provider.get("11")

            self.assertEqual(ticket.acceptance_criteria, ("criterion A", "criterion B"))
            assert ticket.body is not None
            self.assertIn("Problem summary.", ticket.body)
            self.assertIn("## Notes", ticket.body)
            self.assertIn("### Lower heading remains in body", ticket.body)
            self.assertNotIn("## Acceptance criteria", ticket.body)

    def test_local_config_uses_local_provider_binding(self) -> None:
        resolved = resolve_ticket_provider(
            LocalTicketConfiguration(provider="local", path=".ai-dev/tickets")
        )
        self.assertIsInstance(resolved, LocalTicketProviderBinding)

    def test_explicit_github_does_not_fall_back_to_local(self) -> None:
        resolved = resolve_ticket_provider(
            GitHubTicketConfiguration(provider="github", repository="jmrozi1/ai-dev")
        )
        self.assertNotIsInstance(resolved, LocalTicketProviderBinding)

    def test_github_current_does_not_fall_back_to_local(self) -> None:
        resolved = resolve_ticket_provider(
            GitHubCurrentTicketConfiguration(provider="github-current")
        )
        self.assertNotIsInstance(resolved, LocalTicketProviderBinding)


class TicketCliCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.repo_root = self.tmp_path / "repo"
        self.repo_root.mkdir(parents=True, exist_ok=True)
        self._run_git("init", "-q")
        self._run_git("config", "user.name", "Ticket CLI Tests")
        self._run_git("config", "user.email", "ticket-cli-tests@example.com")
        (self.repo_root / "README.md").write_text("repo\n", encoding="utf-8")
        self._run_git("add", "README.md")
        self._run_git("commit", "-q", "-m", "initial")
        self._run_git("branch", "-M", "main")

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _run_git(self, *args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(self.repo_root), *args],
            check=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return completed.stdout.strip()

    def _commit_all(self, message: str) -> None:
        self._run_git("add", "-A")
        self._run_git("commit", "-q", "-m", message)

    def _write_config(self, payload: dict[str, object]) -> None:
        config_path = self.repo_root / ".ai-dev" / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _set_origin_remote(self, url: str) -> None:
        self._run_git("remote", "add", "origin", url)

    def _exclude_ai_dev_from_git_status(self) -> None:
        exclude_path = self.repo_root / ".git" / "info" / "exclude"
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        if ".ai-dev/\n" not in existing:
            exclude_path.write_text(existing + ".ai-dev/\n", encoding="utf-8")

    def _workflow_path(self) -> Path:
        return self.repo_root / ".ai-dev" / "workflow.json"

    def _blocked_workflows_path(self) -> Path:
        return self.repo_root / ".ai-dev" / "blocked-workflows.json"

    def _local_ticket_path(self, ticket_id: str) -> Path:
        return self.repo_root / ".ai-dev" / "tickets" / f"{ticket_id}.json"

    def _read_local_ticket_payload(
        self,
        ticket_id: str,
        *,
        tickets_path: str = ".ai-dev/tickets",
    ) -> dict[str, object]:
        path = (
            self._local_ticket_path(ticket_id)
            if tickets_path == ".ai-dev/tickets"
            else self.repo_root / tickets_path / f"{ticket_id}.json"
        )
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_local_ticket(
        self,
        *,
        ticket_id: str,
        title: str,
        lifecycle_state: str = "open",
        tickets_path: str = ".ai-dev/tickets",
    ) -> None:
        payload = {
            "reference": {
                "provider": "local",
                "ticketId": ticket_id,
                "path": tickets_path,
            },
            "title": title,
            "lifecycleState": lifecycle_state,
            "workflowState": "inactive",
        }
        path = (
            self._local_ticket_path(ticket_id)
            if tickets_path == ".ai-dev/tickets"
            else self.repo_root / tickets_path / f"{ticket_id}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _write_workflow_state(self, payload: dict[str, object]) -> None:
        workflow_path = self._workflow_path()
        workflow_path.parent.mkdir(parents=True, exist_ok=True)
        workflow_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _invoke_direct(self, command: str, *arguments: str) -> tuple[int, str, str]:
        previous_cwd = Path.cwd()
        previous_argv = list(sys.argv)
        previous_command_name = os.environ.get("FLOW_COMMAND_NAME")
        had_command_name = "FLOW_COMMAND_NAME" in os.environ

        stdout = io.StringIO()
        stderr = io.StringIO()

        argv0 = f"flow-{command}"
        os.environ["FLOW_COMMAND_NAME"] = argv0
        sys.argv = [argv0, cli._DIRECT_FLOW_ROUTE_TOKEN, command, *arguments]
        os.chdir(self.repo_root)

        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                try:
                    cli.run()
                except SystemExit as exc:
                    code = int(exc.code) if isinstance(exc.code, int) else 1
                else:
                    code = 0
        finally:
            os.chdir(previous_cwd)
            sys.argv = previous_argv
            if had_command_name:
                assert previous_command_name is not None
                os.environ["FLOW_COMMAND_NAME"] = previous_command_name
            else:
                os.environ.pop("FLOW_COMMAND_NAME", None)

        return code, stdout.getvalue(), stderr.getvalue()

    def test_provider_switch_between_workflows_keeps_identity_bound_per_workflow(self) -> None:
        local_path = ".ai-dev/tickets-a"
        self._write_config(
            {
                "tickets": {
                    "provider": "local",
                    "path": local_path,
                }
            }
        )
        self._exclude_ai_dev_from_git_status()
        self._write_local_ticket(ticket_id="61", title="Local workflow A", tickets_path=local_path)

        start_a_code, _, start_a_stderr = self._invoke_direct("start", "61")
        self.assertEqual(start_a_code, 0)
        self.assertEqual(start_a_stderr, "")

        complete_a_code, _, complete_a_stderr = self._invoke_direct("complete")
        self.assertEqual(complete_a_code, 0)
        self.assertEqual(complete_a_stderr, "")

        local_ticket_after_complete = self._read_local_ticket_payload("61", tickets_path=local_path)
        self.assertEqual(local_ticket_after_complete["lifecycleState"], "closed")
        self.assertEqual(local_ticket_after_complete["workflowState"], "inactive")
        self.assertEqual(local_ticket_after_complete["reference"]["provider"], "local")
        self.assertEqual(local_ticket_after_complete["reference"]["path"], local_path)

        self._write_config(
            {
                "tickets": {
                    "provider": "github",
                    "repository": "acme/ticket-repo",
                }
            }
        )

        calls: list[list[str]] = []

        def _run_side_effect(command: list[str]):
            cmd = list(command)
            calls.append(cmd)
            if cmd[:3] == ["git", "-C", str(self.repo_root)]:
                raise AssertionError("explicit github mode should not resolve current project repository")
            if cmd[:3] == ["gh", "issue", "view"]:
                self.assertEqual(cmd[3], "62")
                self.assertIn("--repo", cmd)
                self.assertEqual(cmd[cmd.index("--repo") + 1], "acme/ticket-repo")
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "number": 62,
                            "title": "Github workflow B",
                            "url": "https://github.com/acme/ticket-repo/issues/62",
                            "state": "OPEN",
                            "body": "Body",
                            "labels": [{"name": "backlog"}],
                            "createdAt": "2026-08-10T00:00:00Z",
                            "updatedAt": "2026-08-10T01:00:00Z",
                            "closedAt": None,
                        }
                    ),
                    stderr="",
                )
            if cmd[:3] == ["gh", "issue", "edit"]:
                self.assertEqual(cmd[3], "62")
                self.assertIn("--repo", cmd)
                self.assertEqual(cmd[cmd.index("--repo") + 1], "acme/ticket-repo")
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="unexpected")

        with patch("ai_dev_flow.ticket_providers._run_command", side_effect=_run_side_effect):
            start_b_code, _, start_b_stderr = self._invoke_direct("start", "62")

        self.assertEqual(start_b_code, 0)
        self.assertEqual(start_b_stderr, "")

        workflow_after_start_b = json.loads(self._workflow_path().read_text(encoding="utf-8"))
        self.assertEqual(workflow_after_start_b["ticket"]["provider"], "github")
        self.assertEqual(workflow_after_start_b["ticket"]["ticketId"], "62")
        self.assertEqual(workflow_after_start_b["ticket"]["repository"], "acme/ticket-repo")

        local_ticket_still_closed = self._read_local_ticket_payload("61", tickets_path=local_path)
        self.assertEqual(local_ticket_still_closed["lifecycleState"], "closed")
        self.assertEqual(local_ticket_still_closed["workflowState"], "inactive")
        self.assertEqual(local_ticket_still_closed["reference"]["provider"], "local")
        self.assertEqual(local_ticket_still_closed["reference"]["path"], local_path)

        gh_calls = [call for call in calls if call and call[0] == "gh"]
        self.assertTrue(gh_calls)
        for call in gh_calls:
            self.assertIn("--repo", call)
            self.assertEqual(call[call.index("--repo") + 1], "acme/ticket-repo")

    def test_ticket_create_show_and_query_for_local_provider(self) -> None:
        self._write_config(
            {
                "tickets": {
                    "provider": "local",
                    "path": ".ai-dev/tickets",
                }
            }
        )

        create_code, create_stdout, create_stderr = self._invoke_direct(
            "ticket-create",
            "Implement checkpoint 2",
            "--body",
            "add local ticket commands",
            "--label",
            "feature",
            "--acceptance",
            "commands installed",
        )
        self.assertEqual(create_code, 0)
        self.assertEqual(create_stderr, "")

        created = json.loads(create_stdout)
        self.assertEqual(created["reference"]["provider"], "local")
        self.assertEqual(created["reference"]["ticketId"], "1")
        self.assertEqual(created["reference"]["path"], ".ai-dev/tickets")
        self.assertEqual(created["title"], "Implement checkpoint 2")

        show_code, show_stdout, show_stderr = self._invoke_direct("ticket-show", "1")
        self.assertEqual(show_code, 0)
        self.assertEqual(show_stderr, "")
        shown = json.loads(show_stdout)
        self.assertEqual(shown["title"], "Implement checkpoint 2")

        query_code, query_stdout, query_stderr = self._invoke_direct(
            "ticket-query",
            "--label",
            "feature",
            "--query",
            "checkpoint",
        )
        self.assertEqual(query_code, 0)
        self.assertEqual(query_stderr, "")
        queried = json.loads(query_stdout)
        self.assertEqual(len(queried["tickets"]), 1)
        self.assertEqual(queried["tickets"][0]["reference"]["ticketId"], "1")

    def test_flow_ticket_create_local_non_default_path_supports_start_block_resume(self) -> None:
        tickets_path = ".ai-dev/custom-tickets"
        self._write_config(
            {
                "tickets": {
                    "provider": "local",
                    "path": tickets_path,
                }
            }
        )
        self._exclude_ai_dev_from_git_status()

        create_code, create_stdout, create_stderr = self._invoke_direct(
            "ticket-create",
            "Custom path flow ticket",
            "--body",
            "created through flow-ticket-create",
            "--label",
            "flow",
        )
        self.assertEqual(create_code, 0)
        self.assertEqual(create_stderr, "")

        created = json.loads(create_stdout)
        self.assertEqual(created["reference"]["provider"], "local")
        self.assertEqual(created["reference"]["path"], tickets_path)
        ticket_id = created["reference"]["ticketId"]

        self._write_config(
            {
                "tickets": {
                    "provider": "local",
                    "path": tickets_path,
                }
            }
        )

        start_code, start_stdout, start_stderr = self._invoke_direct("start", ticket_id)
        self.assertEqual(start_code, 0)
        self.assertEqual(start_stderr, "")
        self.assertIn(f"Started issue {ticket_id}", start_stdout)

        block_code, _, block_stderr = self._invoke_direct("block", "blocked by flow test")
        self.assertEqual(block_code, 0)
        self.assertEqual(block_stderr, "")

        resume_code, _, resume_stderr = self._invoke_direct("resume", ticket_id)
        self.assertEqual(resume_code, 0)
        self.assertEqual(resume_stderr, "")

        created_ticket_payload = self._read_local_ticket_payload(ticket_id, tickets_path=tickets_path)
        self.assertEqual(created_ticket_payload["reference"]["path"], tickets_path)
        self.assertEqual(created_ticket_payload["workflowState"], "active")

    def test_ticket_commands_use_explicit_github_repository_even_when_code_remote_differs(self) -> None:
        self._set_origin_remote("https://github.com/acme/current-code-repo.git")
        self._write_config(
            {
                "tickets": {
                    "provider": "github",
                    "repository": "acme/ticket-repo",
                }
            }
        )

        calls: list[list[str]] = []

        def _run_side_effect(command: list[str]):
            command = list(command)
            calls.append(command)

            if command[:3] == ["gh", "issue", "create"]:
                return subprocess.CompletedProcess(
                    args=command,
                    returncode=0,
                    stdout="https://github.com/acme/ticket-repo/issues/12\n",
                    stderr="",
                )

            if command[:3] == ["gh", "issue", "view"]:
                return subprocess.CompletedProcess(
                    args=command,
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "number": 12,
                            "title": "Implement checkpoint 3",
                            "url": "https://github.com/acme/ticket-repo/issues/12",
                            "state": "OPEN",
                            "body": "Ticket body\n\n## Acceptance criteria\n- [ ] criterion A",
                            "labels": [{"name": "feature"}, {"name": "active"}],
                            "createdAt": "2026-08-10T00:00:00Z",
                            "updatedAt": "2026-08-10T01:00:00Z",
                            "closedAt": None,
                        }
                    ),
                    stderr="",
                )

            if command[:3] == ["gh", "api", "graphql"]:
                return subprocess.CompletedProcess(
                    args=command,
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "data": {
                                "repository": {
                                    "issues": {
                                        "pageInfo": {
                                            "hasNextPage": False,
                                            "endCursor": None,
                                        },
                                        "nodes": [
                                            {
                                                "number": 12,
                                                "title": "Implement checkpoint 3",
                                                "url": "https://github.com/acme/ticket-repo/issues/12",
                                                "state": "OPEN",
                                                "body": "Ticket body\n\n## Acceptance criteria\n- [ ] criterion A",
                                                "labels": {
                                                    "nodes": [{"name": "feature"}, {"name": "active"}]
                                                },
                                                "createdAt": "2026-08-10T00:00:00Z",
                                                "updatedAt": "2026-08-10T01:00:00Z",
                                                "closedAt": None,
                                            },
                                            {
                                                "number": 13,
                                                "title": "Other",
                                                "url": "https://github.com/acme/ticket-repo/issues/13",
                                                "state": "OPEN",
                                                "body": "Other body",
                                                "labels": {"nodes": [{"name": "feature"}]},
                                                "createdAt": "2026-08-10T00:00:00Z",
                                                "updatedAt": "2026-08-10T01:00:00Z",
                                                "closedAt": None,
                                            },
                                        ],
                                    }
                                }
                            }
                        }
                    ),
                    stderr="",
                )

            return subprocess.CompletedProcess(args=command, returncode=1, stdout="", stderr="unexpected")

        with patch("ai_dev_flow.ticket_providers._run_command", side_effect=_run_side_effect):
            create_code, create_stdout, create_stderr = self._invoke_direct(
                "ticket-create",
                "Implement checkpoint 3",
                "--body",
                "Ticket body",
                "--label",
                "feature",
                "--label",
                "active",
                "--acceptance",
                "criterion A",
            )
            show_code, show_stdout, show_stderr = self._invoke_direct("ticket-show", "12")
            query_code, query_stdout, query_stderr = self._invoke_direct(
                "ticket-query",
                "--label",
                "feature",
                "--label",
                "active",
                "--query",
                "criterion A",
            )

        self.assertEqual(create_code, 0)
        self.assertEqual(create_stderr, "")
        self.assertEqual(show_code, 0)
        self.assertEqual(show_stderr, "")
        self.assertEqual(query_code, 0)
        self.assertEqual(query_stderr, "")

        created_ticket = json.loads(create_stdout)
        self.assertEqual(created_ticket["reference"]["provider"], "github")
        self.assertEqual(created_ticket["reference"]["repository"], "acme/ticket-repo")
        self.assertEqual(created_ticket["reference"]["ticketId"], "12")
        self.assertEqual(created_ticket["acceptanceCriteria"], ["criterion A"])
        self.assertEqual(created_ticket["workflowState"], "active")

        shown_ticket = json.loads(show_stdout)
        self.assertEqual(shown_ticket["reference"]["provider"], "github")
        self.assertEqual(shown_ticket["reference"]["repository"], "acme/ticket-repo")
        self.assertEqual(shown_ticket["acceptanceCriteria"], ["criterion A"])

        queried = json.loads(query_stdout)
        self.assertEqual(len(queried["tickets"]), 1)
        self.assertEqual(queried["tickets"][0]["reference"]["ticketId"], "12")
        self.assertEqual(queried["tickets"][0]["reference"]["repository"], "acme/ticket-repo")

        gh_calls = [call for call in calls if call and call[0] == "gh"]
        self.assertTrue(gh_calls)
        for call in gh_calls:
            if call[:3] == ["gh", "api", "graphql"]:
                self.assertNotIn("--repo", call)
                self.assertIn("owner=acme", call)
                self.assertIn("repo=ticket-repo", call)
            else:
                self.assertIn("--repo", call)
                self.assertEqual(call[call.index("--repo") + 1], "acme/ticket-repo")

        create_call = next(call for call in gh_calls if call[:3] == ["gh", "issue", "create"])
        self.assertIn("--body", create_call)
        rendered_body = create_call[create_call.index("--body") + 1]
        self.assertIn("## Acceptance criteria", rendered_body)
        self.assertIn("- [ ] criterion A", rendered_body)

    def test_ticket_query_uses_exhaustive_graphql_pagination(self) -> None:
        self._write_config(
            {
                "tickets": {
                    "provider": "github",
                    "repository": "acme/ticket-repo",
                }
            }
        )

        graphql_calls: list[list[str]] = []

        def _run_side_effect(command: list[str]):
            cmd = list(command)

            if cmd[:3] == ["gh", "api", "graphql"]:
                graphql_calls.append(cmd)
                cursor_arg = next((item for item in cmd if item.startswith("after=")), "after=")
                # The query sends first request with null cursor and subsequent request with endCursor.
                if cursor_arg == "after=null":
                    return subprocess.CompletedProcess(
                        args=cmd,
                        returncode=0,
                        stdout=json.dumps(
                            {
                                "data": {
                                    "repository": {
                                        "issues": {
                                            "pageInfo": {
                                                "hasNextPage": True,
                                                "endCursor": "cursor-1",
                                            },
                                            "nodes": [
                                                {
                                                    "number": 1,
                                                    "title": "first page",
                                                    "url": "https://github.com/acme/ticket-repo/issues/1",
                                                    "state": "OPEN",
                                                    "body": "alpha",
                                                    "labels": {"nodes": [{"name": "feature"}]},
                                                    "createdAt": "2026-08-10T00:00:00Z",
                                                    "updatedAt": "2026-08-10T00:00:00Z",
                                                    "closedAt": None,
                                                }
                                            ],
                                        }
                                    }
                                }
                            }
                        ),
                        stderr="",
                    )

                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "data": {
                                "repository": {
                                    "issues": {
                                        "pageInfo": {
                                            "hasNextPage": False,
                                            "endCursor": None,
                                        },
                                        "nodes": [
                                            {
                                                "number": 2,
                                                "title": "second page",
                                                "url": "https://github.com/acme/ticket-repo/issues/2",
                                                "state": "OPEN",
                                                "body": "target text",
                                                "labels": {"nodes": [{"name": "feature"}, {"name": "active"}]},
                                                "createdAt": "2026-08-10T00:00:00Z",
                                                "updatedAt": "2026-08-10T00:00:00Z",
                                                "closedAt": None,
                                            }
                                        ],
                                    }
                                }
                            }
                        }
                    ),
                    stderr="",
                )

            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="unexpected")

        with patch("ai_dev_flow.ticket_providers._run_command", side_effect=_run_side_effect):
            code, stdout, stderr = self._invoke_direct(
                "ticket-query",
                "--label",
                "feature",
                "--label",
                "active",
                "--query",
                "target text",
            )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertEqual([item["reference"]["ticketId"] for item in payload["tickets"]], ["2"])
        self.assertEqual(len(graphql_calls), 2)
        self.assertTrue(all(call[:3] == ["gh", "api", "graphql"] for call in graphql_calls))
        self.assertIn("after=null", graphql_calls[0])
        self.assertIn("after=cursor-1", graphql_calls[1])
        self.assertTrue(all("--repo" not in call for call in graphql_calls))
        self.assertTrue(all("owner=acme" in call for call in graphql_calls))
        self.assertTrue(all("repo=ticket-repo" in call for call in graphql_calls))
        self.assertTrue(all("--limit" not in call for call in graphql_calls))

    def test_ticket_commands_use_github_current_repository_resolution(self) -> None:
        self._set_origin_remote("https://github.com/acme/current-code-repo.git")
        self._write_config(
            {
                "tickets": {
                    "provider": "github-current",
                }
            }
        )

        observed_repos: list[str] = []

        def _run_side_effect(command: list[str]):
            command = list(command)
            if command[:6] == ["git", "-C", str(self.repo_root), "remote", "get-url", "origin"]:
                return subprocess.CompletedProcess(
                    args=command,
                    returncode=0,
                    stdout="https://github.com/acme/current-code-repo.git\n",
                    stderr="",
                )
            if command[:3] == ["gh", "issue", "view"]:
                observed_repos.append(command[command.index("--repo") + 1])
                return subprocess.CompletedProcess(
                    args=command,
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "number": 1,
                            "title": "Current repo issue",
                            "url": "https://github.com/acme/current-code-repo/issues/1",
                            "state": "OPEN",
                            "body": "Body",
                            "labels": [],
                            "createdAt": "2026-08-10T00:00:00Z",
                            "updatedAt": "2026-08-10T01:00:00Z",
                            "closedAt": None,
                        }
                    ),
                    stderr="",
                )

            return subprocess.CompletedProcess(args=command, returncode=1, stdout="", stderr="unexpected")

        with patch("ai_dev_flow.ticket_providers._run_command", side_effect=_run_side_effect):
            code, stdout, stderr = self._invoke_direct("ticket-show", "1")

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(stdout)["reference"]["repository"], "acme/current-code-repo")
        self.assertEqual(observed_repos, ["acme/current-code-repo"])

    def test_explicit_github_mode_never_falls_back_to_current_project_repository(self) -> None:
        self._set_origin_remote("https://github.com/acme/current-code-repo.git")
        self._write_config(
            {
                "tickets": {
                    "provider": "github",
                    "repository": "acme/ticket-repo",
                }
            }
        )

        def _run_side_effect(command: list[str]):
            command = list(command)
            if command[:3] == ["git", "-C", str(self.repo_root)]:
                raise AssertionError("explicit github mode should not ask git for current repository")
            if command[:3] == ["gh", "issue", "view"]:
                return subprocess.CompletedProcess(
                    args=command,
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "number": 9,
                            "title": "Explicit",
                            "url": "https://github.com/acme/ticket-repo/issues/9",
                            "state": "OPEN",
                            "body": "Body",
                            "labels": [],
                            "createdAt": "2026-08-10T00:00:00Z",
                            "updatedAt": "2026-08-10T01:00:00Z",
                            "closedAt": None,
                        }
                    ),
                    stderr="",
                )

            return subprocess.CompletedProcess(args=command, returncode=1, stdout="", stderr="unexpected")

        with patch("ai_dev_flow.ticket_providers._run_command", side_effect=_run_side_effect):
            code, stdout, stderr = self._invoke_direct("ticket-show", "9")

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(stdout)["reference"]["repository"], "acme/ticket-repo")

    def test_github_current_mode_fails_clearly_when_project_remote_is_missing(self) -> None:
        self._write_config(
            {
                "tickets": {
                    "provider": "github-current",
                }
            }
        )

        def _run_side_effect(command: list[str]):
            command = list(command)
            if command[:6] == ["git", "-C", str(self.repo_root), "remote", "get-url", "origin"]:
                return subprocess.CompletedProcess(
                    args=command,
                    returncode=2,
                    stdout="",
                    stderr="error: No such remote 'origin'",
                )
            return subprocess.CompletedProcess(args=command, returncode=1, stdout="", stderr="unexpected")

        with patch("ai_dev_flow.ticket_providers._run_command", side_effect=_run_side_effect):
            code, stdout, stderr = self._invoke_direct("ticket-show", "1")

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Cannot resolve github-current repository from current project", stderr)

    def test_github_provider_failures_surface_useful_errors(self) -> None:
        self._write_config(
            {
                "tickets": {
                    "provider": "github",
                    "repository": "acme/ticket-repo",
                }
            }
        )

        def _run_side_effect(command: list[str]):
            command = list(command)
            if command[:3] == ["gh", "issue", "view"]:
                return subprocess.CompletedProcess(
                    args=command,
                    returncode=1,
                    stdout="",
                    stderr="HTTP 404: Not Found",
                )
            return subprocess.CompletedProcess(args=command, returncode=1, stdout="", stderr="unexpected")

        with patch("ai_dev_flow.ticket_providers._run_command", side_effect=_run_side_effect):
            code, stdout, stderr = self._invoke_direct("ticket-show", "99")

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("GitHub ticket lookup failed for acme/ticket-repo#99", stderr)
        self.assertIn("HTTP 404: Not Found", stderr)

    def test_flow_start_local_provider_persists_local_ticket_reference(self) -> None:
        self._write_config(
            {
                "tickets": {
                    "provider": "local",
                    "path": ".ai-dev/tickets",
                }
            }
        )
        self._write_local_ticket(ticket_id="7", title="Local start ticket")
        self._commit_all("add local ticket fixture")

        code, stdout, stderr = self._invoke_direct("start", "7")

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Started issue 7", stdout)

        workflow = json.loads(self._workflow_path().read_text(encoding="utf-8"))
        self.assertEqual(workflow["activeIssueNumber"], 7)
        self.assertEqual(workflow["activeIssueTitle"], "Local start ticket")
        self.assertEqual(workflow["checkpoint"], 0)
        self.assertEqual(
            workflow["ticket"],
            {
                "provider": "local",
                "ticketId": "7",
                "path": ".ai-dev/tickets",
            },
        )

        ticket_payload = self._read_local_ticket_payload("7")
        self.assertEqual(ticket_payload["workflowState"], "active")
        self.assertNotIn("blockReason", ticket_payload)

    def test_flow_block_and_resume_local_provider_updates_ticket_state_and_reason(self) -> None:
        self._write_config(
            {
                "tickets": {
                    "provider": "local",
                    "path": ".ai-dev/tickets",
                }
            }
        )
        self._write_local_ticket(ticket_id="5", title="Local block/resume")
        self._exclude_ai_dev_from_git_status()

        start_code, _, start_stderr = self._invoke_direct("start", "5")
        self.assertEqual(start_code, 0)
        self.assertEqual(start_stderr, "")

        block_code, _, block_stderr = self._invoke_direct("block", "waiting on dependency")
        self.assertEqual(block_code, 0)
        self.assertEqual(block_stderr, "")

        blocked_ticket_payload = self._read_local_ticket_payload("5")
        self.assertEqual(blocked_ticket_payload["workflowState"], "blocked")
        self.assertEqual(blocked_ticket_payload["blockReason"], "waiting on dependency")

        blocked_payload = json.loads(self._blocked_workflows_path().read_text(encoding="utf-8"))
        self.assertEqual(len(blocked_payload["blockedWorkflows"]), 1)
        blocked_record = blocked_payload["blockedWorkflows"][0]
        self.assertEqual(blocked_record["issueNumber"], 5)
        self.assertEqual(blocked_record["ticket"]["provider"], "local")
        self.assertEqual(blocked_record["ticket"]["ticketId"], "5")
        self.assertEqual(blocked_record["ticket"]["path"], ".ai-dev/tickets")

        resume_code, _, resume_stderr = self._invoke_direct("resume", "5")
        self.assertEqual(resume_code, 0)
        self.assertEqual(resume_stderr, "")

        resumed_ticket_payload = self._read_local_ticket_payload("5")
        self.assertEqual(resumed_ticket_payload["workflowState"], "active")
        self.assertNotIn("blockReason", resumed_ticket_payload)

    def test_flow_complete_local_provider_closes_bound_ticket_and_uses_persisted_path(self) -> None:
        tickets_path = ".ai-dev/custom-tickets"
        alternate_path = ".ai-dev/other-tickets"
        self._write_config(
            {
                "tickets": {
                    "provider": "local",
                    "path": tickets_path,
                }
            }
        )
        self._exclude_ai_dev_from_git_status()
        self._write_local_ticket(ticket_id="31", title="Bound local ticket", tickets_path=tickets_path)
        self._write_local_ticket(ticket_id="31", title="Wrong store ticket", tickets_path=alternate_path)

        start_code, _, start_stderr = self._invoke_direct("start", "31")
        self.assertEqual(start_code, 0)
        self.assertEqual(start_stderr, "")

        # Change current config after start to verify completion still follows persisted reference path.
        self._write_config(
            {
                "tickets": {
                    "provider": "local",
                    "path": alternate_path,
                }
            }
        )

        code, stdout, stderr = self._invoke_direct("complete")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Completed issue 31", stdout)

        self.assertEqual(
            json.loads(self._workflow_path().read_text(encoding="utf-8")),
            {
                "mainBranch": "main",
                "scratchBranch": "scratch",
                "checkpoint": 0,
            },
        )

        completed_payload = self._read_local_ticket_payload("31", tickets_path=tickets_path)
        self.assertEqual(completed_payload["lifecycleState"], "closed")
        self.assertEqual(completed_payload["workflowState"], "inactive")
        self.assertNotIn("blockReason", completed_payload)
        self.assertIn("updatedAt", completed_payload)
        self.assertIn("closedAt", completed_payload)

        alternate_payload = self._read_local_ticket_payload("31", tickets_path=alternate_path)
        self.assertEqual(alternate_payload["lifecycleState"], "open")
        self.assertEqual(alternate_payload["workflowState"], "inactive")

    def test_flow_complete_bound_provider_failure_keeps_active_workflow_and_does_not_fallback(self) -> None:
        tickets_path = ".ai-dev/custom-tickets"
        self._write_config(
            {
                "tickets": {
                    "provider": "local",
                    "path": tickets_path,
                }
            }
        )
        self._exclude_ai_dev_from_git_status()
        self._write_local_ticket(ticket_id="32", title="Bound completion failure", tickets_path=tickets_path)

        start_code, _, start_stderr = self._invoke_direct("start", "32")
        self.assertEqual(start_code, 0)
        self.assertEqual(start_stderr, "")
        workflow_before = self._workflow_path().read_text(encoding="utf-8")

        with patch(
            "ai_dev_flow.ticket_providers.LocalTicketProvider.complete",
            side_effect=TicketProviderError("cannot complete local ticket"),
        ) as complete_mock, patch(
            "ai_dev_flow.cli._resolve_ticket_provider_for_repo_root",
            side_effect=AssertionError("flow-complete should not resolve provider from current config"),
        ):
            code, stdout, stderr = self._invoke_direct("complete")

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("failed to complete bound ticket 32", stderr)
        self.assertEqual(self._workflow_path().read_text(encoding="utf-8"), workflow_before)
        complete_mock.assert_called_once()

    def test_flow_complete_bound_ticket_then_state_save_failure_reports_partial_success(self) -> None:
        tickets_path = ".ai-dev/custom-tickets"
        self._write_config(
            {
                "tickets": {
                    "provider": "local",
                    "path": tickets_path,
                }
            }
        )
        self._exclude_ai_dev_from_git_status()
        self._write_local_ticket(ticket_id="33", title="State save failure", tickets_path=tickets_path)

        start_code, _, start_stderr = self._invoke_direct("start", "33")
        self.assertEqual(start_code, 0)
        self.assertEqual(start_stderr, "")
        workflow_before = self._workflow_path().read_text(encoding="utf-8")

        with patch(
            "ai_dev_flow.cli.save_state",
            side_effect=WorkflowStateError("Cannot write workflow state to /tmp/workflow.json: denied"),
        ):
            code, stdout, stderr = self._invoke_direct("complete")

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("ticket completion succeeded but workflow state could not be cleared", stderr)
        self.assertEqual(self._workflow_path().read_text(encoding="utf-8"), workflow_before)

        completed_payload = self._read_local_ticket_payload("33", tickets_path=tickets_path)
        self.assertEqual(completed_payload["lifecycleState"], "closed")
        self.assertEqual(completed_payload["workflowState"], "inactive")

    def test_flow_complete_github_explicit_uses_persisted_repository_and_closes_issue(self) -> None:
        self._run_git("checkout", "-q", "-b", "scratch")
        self._set_origin_remote("https://github.com/acme/code-repo.git")
        self._exclude_ai_dev_from_git_status()
        self._write_workflow_state(
            {
                "mainBranch": "main",
                "scratchBranch": "scratch",
                "checkpoint": 0,
                "activeIssueNumber": 41,
                "activeIssueTitle": "Bound github ticket",
                "activeIssueUrl": "https://github.com/acme/persisted-repo/issues/41",
                "ticket": {
                    "provider": "github",
                    "ticketId": "41",
                    "repository": "acme/persisted-repo",
                    "url": "https://github.com/acme/persisted-repo/issues/41",
                },
            }
        )

        issue_state = {"value": "OPEN"}
        labels = ["active"]
        calls: list[list[str]] = []

        def _run_side_effect(command: list[str]):
            cmd = list(command)
            calls.append(cmd)
            if cmd[:3] == ["git", "-C", str(self.repo_root)]:
                raise AssertionError("bound completion should not resolve current project repository")
            if cmd[:3] == ["gh", "issue", "view"]:
                self.assertEqual(cmd[3], "41")
                self.assertEqual(cmd[cmd.index("--repo") + 1], "acme/persisted-repo")
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "number": 41,
                            "title": "Bound github ticket",
                            "url": "https://github.com/acme/persisted-repo/issues/41",
                            "state": issue_state["value"],
                            "body": "Body",
                            "labels": [{"name": label} for label in labels],
                            "createdAt": "2026-08-10T00:00:00Z",
                            "updatedAt": "2026-08-10T01:00:00Z",
                            "closedAt": "2026-08-10T02:00:00Z" if issue_state["value"] == "CLOSED" else None,
                        }
                    ),
                    stderr="",
                )
            if cmd[:3] == ["gh", "issue", "edit"]:
                self.assertEqual(cmd[3], "41")
                self.assertEqual(cmd[cmd.index("--repo") + 1], "acme/persisted-repo")
                self.assertIn("--remove-label", cmd)
                to_remove = {
                    label
                    for label in cmd[cmd.index("--remove-label") + 1].split(",")
                    if label
                }
                labels[:] = [label for label in labels if label not in to_remove]
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
            if cmd[:3] == ["gh", "issue", "close"]:
                self.assertEqual(cmd[3], "41")
                self.assertEqual(cmd[cmd.index("--repo") + 1], "acme/persisted-repo")
                issue_state["value"] = "CLOSED"
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="closed\n", stderr="")
            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="unexpected")

        with patch("ai_dev_flow.ticket_providers._run_command", side_effect=_run_side_effect), patch(
            "ai_dev_flow.cli._resolve_ticket_provider_for_repo_root",
            side_effect=AssertionError("flow-complete should not resolve provider from current config"),
        ):
            code, stdout, stderr = self._invoke_direct("complete")

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Completed issue 41", stdout)
        close_calls = [call for call in calls if call[:3] == ["gh", "issue", "close"]]
        self.assertEqual(len(close_calls), 1)
        self.assertEqual(
            json.loads(self._workflow_path().read_text(encoding="utf-8")),
            {
                "mainBranch": "main",
                "scratchBranch": "scratch",
                "checkpoint": 0,
            },
        )

    def test_flow_complete_github_current_uses_repository_bound_at_start_after_remote_change(self) -> None:
        initial_remote = "https://github.com/acme/current-code-repo.git"
        changed_remote = "https://github.com/acme/changed-code-repo.git"
        self._set_origin_remote(initial_remote)
        self._exclude_ai_dev_from_git_status()
        self._write_config({"tickets": {"provider": "github-current"}})

        issue_state = {"state": "OPEN", "labels": []}
        remote_resolution_calls = 0
        calls: list[list[str]] = []

        def _run_side_effect(command: list[str]):
            nonlocal remote_resolution_calls
            cmd = list(command)
            calls.append(cmd)

            if cmd[:6] == ["git", "-C", str(self.repo_root), "remote", "get-url", "origin"]:
                remote_resolution_calls += 1
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=f"{initial_remote}\n",
                    stderr="",
                )

            if cmd[:3] == ["gh", "issue", "view"]:
                self.assertEqual(cmd[3], "50")
                self.assertEqual(cmd[cmd.index("--repo") + 1], "acme/current-code-repo")
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "number": 50,
                            "title": "github-current start+complete",
                            "url": "https://github.com/acme/current-code-repo/issues/50",
                            "state": issue_state["state"],
                            "body": "Body",
                            "labels": [{"name": label} for label in issue_state["labels"]],
                            "createdAt": "2026-08-10T00:00:00Z",
                            "updatedAt": "2026-08-10T01:00:00Z",
                            "closedAt": "2026-08-10T02:00:00Z" if issue_state["state"] == "CLOSED" else None,
                        }
                    ),
                    stderr="",
                )

            if cmd[:3] == ["gh", "issue", "edit"]:
                self.assertEqual(cmd[3], "50")
                self.assertEqual(cmd[cmd.index("--repo") + 1], "acme/current-code-repo")
                if "--add-label" in cmd:
                    label = cmd[cmd.index("--add-label") + 1]
                    if label not in issue_state["labels"]:
                        issue_state["labels"].append(label)
                if "--remove-label" in cmd:
                    to_remove = [item for item in cmd[cmd.index("--remove-label") + 1].split(",") if item]
                    issue_state["labels"] = [label for label in issue_state["labels"] if label not in to_remove]
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

            if cmd[:3] == ["gh", "issue", "close"]:
                self.assertEqual(cmd[3], "50")
                self.assertEqual(cmd[cmd.index("--repo") + 1], "acme/current-code-repo")
                issue_state["state"] = "CLOSED"
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="closed\n", stderr="")

            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="unexpected")

        with patch("ai_dev_flow.ticket_providers._run_command", side_effect=_run_side_effect):
            start_code, _, start_stderr = self._invoke_direct("start", "50")
            self.assertEqual(start_code, 0)
            self.assertEqual(start_stderr, "")
            remote_resolution_calls_before_complete = remote_resolution_calls

            self._run_git("remote", "set-url", "origin", changed_remote)
            self._write_config({"tickets": {"provider": "github", "repository": "acme/other-repo"}})

            complete_code, complete_stdout, complete_stderr = self._invoke_direct("complete")

        self.assertEqual(complete_code, 0)
        self.assertEqual(complete_stderr, "")
        self.assertIn("Completed issue 50", complete_stdout)
        self.assertEqual(remote_resolution_calls, remote_resolution_calls_before_complete)
        close_calls = [call for call in calls if call[:3] == ["gh", "issue", "close"]]
        self.assertEqual(len(close_calls), 1)
        self.assertEqual(close_calls[0][close_calls[0].index("--repo") + 1], "acme/current-code-repo")

    def test_flow_complete_github_provider_failure_keeps_active_workflow_and_avoids_fallback(self) -> None:
        self._run_git("checkout", "-q", "-b", "scratch")
        self._exclude_ai_dev_from_git_status()
        self._write_workflow_state(
            {
                "mainBranch": "main",
                "scratchBranch": "scratch",
                "checkpoint": 0,
                "activeIssueNumber": 42,
                "activeIssueTitle": "Bound github failure",
                "activeIssueUrl": "https://github.com/acme/persisted-repo/issues/42",
                "ticket": {
                    "provider": "github",
                    "ticketId": "42",
                    "repository": "acme/persisted-repo",
                    "url": "https://github.com/acme/persisted-repo/issues/42",
                },
            }
        )
        workflow_before = self._workflow_path().read_text(encoding="utf-8")

        calls: list[list[str]] = []
        labels = ["active"]

        def _run_side_effect(command: list[str]):
            cmd = list(command)
            calls.append(cmd)
            if cmd[:3] == ["git", "-C", str(self.repo_root)]:
                raise AssertionError("bound completion should not resolve current project repository")
            if cmd[:3] == ["gh", "issue", "view"]:
                self.assertEqual(cmd[3], "42")
                self.assertEqual(cmd[cmd.index("--repo") + 1], "acme/persisted-repo")
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "number": 42,
                            "title": "Bound github failure",
                            "url": "https://github.com/acme/persisted-repo/issues/42",
                            "state": "OPEN",
                            "body": "Body",
                            "labels": [{"name": label} for label in labels],
                            "createdAt": "2026-08-10T00:00:00Z",
                            "updatedAt": "2026-08-10T01:00:00Z",
                            "closedAt": None,
                        }
                    ),
                    stderr="",
                )
            if cmd[:3] == ["gh", "issue", "edit"]:
                self.assertEqual(cmd[3], "42")
                self.assertEqual(cmd[cmd.index("--repo") + 1], "acme/persisted-repo")
                self.assertIn("--remove-label", cmd)
                to_remove = {
                    label
                    for label in cmd[cmd.index("--remove-label") + 1].split(",")
                    if label
                }
                labels[:] = [label for label in labels if label not in to_remove]
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
            if cmd[:3] == ["gh", "issue", "close"]:
                self.assertEqual(cmd[3], "42")
                self.assertEqual(cmd[cmd.index("--repo") + 1], "acme/persisted-repo")
                return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="close denied")
            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="unexpected")

        with patch("ai_dev_flow.ticket_providers._run_command", side_effect=_run_side_effect), patch(
            "ai_dev_flow.cli._resolve_ticket_provider_for_repo_root",
            side_effect=AssertionError("flow-complete should not resolve provider from current config"),
        ):
            code, stdout, stderr = self._invoke_direct("complete")

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("failed to complete bound ticket 42", stderr)
        self.assertEqual(self._workflow_path().read_text(encoding="utf-8"), workflow_before)
        close_calls = [call for call in calls if call[:3] == ["gh", "issue", "close"]]
        self.assertEqual(len(close_calls), 1)
        edit_calls = [call for call in calls if call[:3] == ["gh", "issue", "edit"]]
        self.assertEqual(len(edit_calls), 0)
        self.assertEqual(labels, ["active"])

    def test_flow_complete_github_close_then_label_cleanup_failure_is_recoverable(self) -> None:
        self._run_git("checkout", "-q", "-b", "scratch")
        self._exclude_ai_dev_from_git_status()
        self._write_workflow_state(
            {
                "mainBranch": "main",
                "scratchBranch": "scratch",
                "checkpoint": 0,
                "activeIssueNumber": 43,
                "activeIssueTitle": "Bound github cleanup failure",
                "activeIssueUrl": "https://github.com/acme/persisted-repo/issues/43",
                "ticket": {
                    "provider": "github",
                    "ticketId": "43",
                    "repository": "acme/persisted-repo",
                    "url": "https://github.com/acme/persisted-repo/issues/43",
                },
            }
        )
        workflow_before = self._workflow_path().read_text(encoding="utf-8")

        calls: list[list[str]] = []
        issue_state = {
            "state": "OPEN",
            "labels": ["active", "needs-triage"],
        }
        fail_cleanup_once = {"value": True}

        def _run_side_effect(command: list[str]):
            cmd = list(command)
            calls.append(cmd)

            if cmd[:3] == ["git", "-C", str(self.repo_root)]:
                raise AssertionError("bound completion should not resolve current project repository")

            if cmd[:3] == ["gh", "issue", "view"]:
                self.assertEqual(cmd[3], "43")
                self.assertEqual(cmd[cmd.index("--repo") + 1], "acme/persisted-repo")
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "number": 43,
                            "title": "Bound github cleanup failure",
                            "url": "https://github.com/acme/persisted-repo/issues/43",
                            "state": issue_state["state"],
                            "body": "Body",
                            "labels": [{"name": label} for label in issue_state["labels"]],
                            "createdAt": "2026-08-10T00:00:00Z",
                            "updatedAt": "2026-08-10T01:00:00Z",
                            "closedAt": "2026-08-10T02:00:00Z" if issue_state["state"] == "CLOSED" else None,
                        }
                    ),
                    stderr="",
                )

            if cmd[:3] == ["gh", "issue", "close"]:
                self.assertEqual(cmd[3], "43")
                self.assertEqual(cmd[cmd.index("--repo") + 1], "acme/persisted-repo")
                issue_state["state"] = "CLOSED"
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="closed\n", stderr="")

            if cmd[:3] == ["gh", "issue", "edit"]:
                self.assertEqual(cmd[3], "43")
                self.assertEqual(cmd[cmd.index("--repo") + 1], "acme/persisted-repo")
                self.assertIn("--remove-label", cmd)
                if fail_cleanup_once["value"]:
                    fail_cleanup_once["value"] = False
                    return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="edit denied")
                to_remove = {
                    label
                    for label in cmd[cmd.index("--remove-label") + 1].split(",")
                    if label
                }
                issue_state["labels"] = [
                    label for label in issue_state["labels"] if label not in to_remove
                ]
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="unexpected")

        with patch("ai_dev_flow.ticket_providers._run_command", side_effect=_run_side_effect), patch(
            "ai_dev_flow.cli._resolve_ticket_provider_for_repo_root",
            side_effect=AssertionError("flow-complete should not resolve provider from current config"),
        ):
            first_code, first_stdout, first_stderr = self._invoke_direct("complete")

            self.assertEqual(first_code, 1)
            self.assertEqual(first_stdout, "")
            self.assertIn("failed to complete bound ticket 43", first_stderr)
            self.assertEqual(self._workflow_path().read_text(encoding="utf-8"), workflow_before)
            self.assertEqual(issue_state["state"], "CLOSED")
            self.assertIn("active", issue_state["labels"])

            retry_code, retry_stdout, retry_stderr = self._invoke_direct("complete")

        self.assertEqual(retry_code, 0)
        self.assertEqual(retry_stderr, "")
        self.assertIn("Completed issue 43", retry_stdout)
        self.assertNotIn("active", issue_state["labels"])
        self.assertEqual(issue_state["labels"], ["needs-triage"])
        self.assertEqual(
            json.loads(self._workflow_path().read_text(encoding="utf-8")),
            {
                "mainBranch": "main",
                "scratchBranch": "scratch",
                "checkpoint": 0,
            },
        )

        close_calls = [call for call in calls if call[:3] == ["gh", "issue", "close"]]
        self.assertEqual(len(close_calls), 1)
        edit_calls = [call for call in calls if call[:3] == ["gh", "issue", "edit"]]
        self.assertEqual(len(edit_calls), 2)

    def test_github_provider_complete_clears_workflow_labels_and_is_idempotent(self) -> None:
        provider = instantiate_ticket_provider(
            repo_root=self.repo_root,
            config=GitHubTicketConfiguration(provider="github", repository="acme/persisted-repo"),
        )
        reference = TicketReference(
            provider="github",
            ticket_id="77",
            repository="acme/persisted-repo",
        )

        issue_state = {
            "state": "OPEN",
            "labels": ["active", "blocked", "backlog", "needs-triage", "docs"],
        }
        calls: list[list[str]] = []

        def _run_side_effect(command: list[str]):
            cmd = list(command)
            calls.append(cmd)

            if cmd[:3] == ["gh", "issue", "view"]:
                self.assertEqual(cmd[3], "77")
                self.assertEqual(cmd[cmd.index("--repo") + 1], "acme/persisted-repo")
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "number": 77,
                            "title": "Completion label cleanup",
                            "url": "https://github.com/acme/persisted-repo/issues/77",
                            "state": issue_state["state"],
                            "body": "Body",
                            "labels": [{"name": name} for name in issue_state["labels"]],
                            "createdAt": "2026-08-10T00:00:00Z",
                            "updatedAt": "2026-08-10T01:00:00Z",
                            "closedAt": "2026-08-10T02:00:00Z" if issue_state["state"] == "CLOSED" else None,
                        }
                    ),
                    stderr="",
                )

            if cmd[:3] == ["gh", "issue", "edit"]:
                self.assertEqual(cmd[3], "77")
                self.assertEqual(cmd[cmd.index("--repo") + 1], "acme/persisted-repo")
                self.assertIn("--remove-label", cmd)
                to_remove = {
                    label
                    for label in cmd[cmd.index("--remove-label") + 1].split(",")
                    if label
                }
                self.assertTrue(to_remove.issubset({"active", "blocked", "backlog"}))
                issue_state["labels"] = [
                    label for label in issue_state["labels"] if label not in to_remove
                ]
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

            if cmd[:3] == ["gh", "issue", "close"]:
                self.assertEqual(cmd[3], "77")
                self.assertEqual(cmd[cmd.index("--repo") + 1], "acme/persisted-repo")
                issue_state["state"] = "CLOSED"
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="closed\n", stderr="")

            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="unexpected")

        with patch("ai_dev_flow.ticket_providers._run_command", side_effect=_run_side_effect):
            completed_once = provider.complete(reference)
            fetched_after_complete = provider.get("77")
            completed_twice = provider.complete(reference)

        self.assertEqual(completed_once.lifecycle_state, "closed")
        self.assertEqual(completed_once.workflow_state, "inactive")
        self.assertEqual(fetched_after_complete.lifecycle_state, "closed")
        self.assertEqual(fetched_after_complete.workflow_state, "inactive")
        self.assertEqual(completed_twice.lifecycle_state, "closed")
        self.assertEqual(completed_twice.workflow_state, "inactive")

        self.assertNotIn("active", completed_once.labels)
        self.assertNotIn("blocked", completed_once.labels)
        self.assertNotIn("backlog", completed_once.labels)
        self.assertEqual(set(completed_once.labels), {"needs-triage", "docs"})

        close_calls = [call for call in calls if call[:3] == ["gh", "issue", "close"]]
        self.assertEqual(len(close_calls), 1)

        edit_calls = [call for call in calls if call[:3] == ["gh", "issue", "edit"]]
        self.assertEqual(len(edit_calls), 1)

        gh_calls = [call for call in calls if call and call[0] == "gh"]
        self.assertTrue(gh_calls)
        for call in gh_calls:
            self.assertIn("--repo", call)
            self.assertEqual(call[call.index("--repo") + 1], "acme/persisted-repo")

    def test_flow_start_provider_mutation_failure_leaves_no_active_workflow(self) -> None:
        self._write_config(
            {
                "tickets": {
                    "provider": "local",
                    "path": ".ai-dev/tickets",
                }
            }
        )
        self._write_local_ticket(ticket_id="6", title="Start failure")
        self._commit_all("add local ticket fixture")

        with patch(
            "ai_dev_flow.ticket_providers.LocalTicketProvider.mark_active",
            side_effect=TicketProviderError("failed to mark active"),
        ):
            code, stdout, stderr = self._invoke_direct("start", "6")

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("failed to mark ticket 6 active", stderr)
        self.assertFalse(self._workflow_path().exists())

        ticket_payload = self._read_local_ticket_payload("6")
        self.assertEqual(ticket_payload["workflowState"], "inactive")
        self.assertNotIn("blockReason", ticket_payload)

    def test_flow_block_provider_failure_keeps_active_workflow_and_blocked_registry_unchanged(self) -> None:
        self._write_config(
            {
                "tickets": {
                    "provider": "local",
                    "path": ".ai-dev/tickets",
                }
            }
        )
        self._write_local_ticket(ticket_id="14", title="Block failure")
        self._exclude_ai_dev_from_git_status()
        start_code, _, start_stderr = self._invoke_direct("start", "14")
        self.assertEqual(start_code, 0)
        self.assertEqual(start_stderr, "")

        workflow_before = self._workflow_path().read_text(encoding="utf-8")

        with patch(
            "ai_dev_flow.ticket_providers.LocalTicketProvider.block",
            side_effect=TicketProviderError("cannot block"),
        ):
            code, stdout, stderr = self._invoke_direct("block", "blocked for failure test")

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("failed to transition ticket 14 to blocked", stderr)
        self.assertEqual(self._workflow_path().read_text(encoding="utf-8"), workflow_before)
        self.assertFalse(self._blocked_workflows_path().exists())

    def test_flow_resume_provider_failure_keeps_blocked_record_and_inactive_workflow(self) -> None:
        self._write_config(
            {
                "tickets": {
                    "provider": "local",
                    "path": ".ai-dev/tickets",
                }
            }
        )
        self._write_local_ticket(ticket_id="15", title="Resume failure")
        self._exclude_ai_dev_from_git_status()

        start_code, _, start_stderr = self._invoke_direct("start", "15")
        self.assertEqual(start_code, 0)
        self.assertEqual(start_stderr, "")
        block_code, _, block_stderr = self._invoke_direct("block", "waiting")
        self.assertEqual(block_code, 0)
        self.assertEqual(block_stderr, "")

        workflow_before = self._workflow_path().read_text(encoding="utf-8")
        blocked_before = self._blocked_workflows_path().read_text(encoding="utf-8")

        with patch(
            "ai_dev_flow.ticket_providers.LocalTicketProvider.resume",
            side_effect=TicketProviderError("cannot resume"),
        ):
            code, stdout, stderr = self._invoke_direct("resume", "15")

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("failed to transition ticket 15 to active", stderr)
        self.assertEqual(self._workflow_path().read_text(encoding="utf-8"), workflow_before)
        self.assertEqual(self._blocked_workflows_path().read_text(encoding="utf-8"), blocked_before)

    def test_flow_block_and_resume_github_use_persisted_repository_and_preserve_unrelated_labels(self) -> None:
        self._set_origin_remote("https://github.com/acme/current-code-repo.git")
        self._write_config(
            {
                "tickets": {
                    "provider": "github-current",
                }
            }
        )
        self._commit_all("add github-current config")

        calls: list[list[str]] = []
        labels_by_issue = {
            "20": ["active", "needs-triage", "docs"],
        }

        def _run_side_effect(command: list[str]):
            cmd = list(command)
            calls.append(cmd)

            if cmd[:6] == ["git", "-C", str(self.repo_root), "remote", "get-url", "origin"]:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="https://github.com/acme/current-code-repo.git\n",
                    stderr="",
                )

            if cmd[:3] == ["gh", "issue", "view"]:
                issue_number = cmd[3]
                repository = cmd[cmd.index("--repo") + 1]
                self.assertEqual(repository, "acme/current-code-repo")
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "number": int(issue_number),
                            "title": f"Issue {issue_number}",
                            "url": f"https://github.com/acme/current-code-repo/issues/{issue_number}",
                            "state": "OPEN",
                            "body": "Body",
                            "labels": [{"name": item} for item in labels_by_issue.get(issue_number, [])],
                            "createdAt": "2026-08-10T00:00:00Z",
                            "updatedAt": "2026-08-10T01:00:00Z",
                            "closedAt": None,
                        }
                    ),
                    stderr="",
                )

            if cmd[:3] == ["gh", "issue", "edit"]:
                issue_number = cmd[3]
                repository = cmd[cmd.index("--repo") + 1]
                self.assertEqual(repository, "acme/current-code-repo")
                add_label = cmd[cmd.index("--add-label") + 1] if "--add-label" in cmd else None
                remove_labels = []
                if "--remove-label" in cmd:
                    remove_labels = [
                        item for item in cmd[cmd.index("--remove-label") + 1].split(",") if item
                    ]
                existing = [item for item in labels_by_issue.get(issue_number, []) if item not in remove_labels]
                if add_label is not None and add_label not in existing:
                    existing.append(add_label)
                labels_by_issue[issue_number] = existing
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="unexpected")

        with patch("ai_dev_flow.ticket_providers._run_command", side_effect=_run_side_effect):
            start_code, _, start_stderr = self._invoke_direct("start", "20")
            self.assertEqual(start_code, 0)
            self.assertEqual(start_stderr, "")

            self._run_git("remote", "set-url", "origin", "https://github.com/acme/changed-remote.git")

            block_code, _, block_stderr = self._invoke_direct("block", "waiting")
            self.assertEqual(block_code, 0)
            self.assertEqual(block_stderr, "")
            self.assertIn("blocked", labels_by_issue["20"])
            self.assertNotIn("active", labels_by_issue["20"])
            self.assertIn("needs-triage", labels_by_issue["20"])
            self.assertIn("docs", labels_by_issue["20"])

            resume_code, _, resume_stderr = self._invoke_direct("resume", "20")
            self.assertEqual(resume_code, 0)
            self.assertEqual(resume_stderr, "")
            self.assertIn("active", labels_by_issue["20"])
            self.assertNotIn("blocked", labels_by_issue["20"])
            self.assertIn("needs-triage", labels_by_issue["20"])
            self.assertIn("docs", labels_by_issue["20"])

        gh_calls = [call for call in calls if call and call[0] == "gh"]
        self.assertTrue(gh_calls)
        for call in gh_calls:
            self.assertIn("--repo", call)
            self.assertEqual(call[call.index("--repo") + 1], "acme/current-code-repo")

    def test_flow_block_and_resume_local_provider_uses_persisted_tickets_path_over_config_changes(self) -> None:
        tickets_path_a = ".ai-dev/tickets-a"
        tickets_path_b = ".ai-dev/tickets-b"
        ticket_a_path = self.repo_root / tickets_path_a / "30.json"
        ticket_b_path = self.repo_root / tickets_path_b / "30.json"

        self._write_config(
            {
                "tickets": {
                    "provider": "local",
                    "path": tickets_path_a,
                }
            }
        )
        self._write_local_ticket(ticket_id="30", title="Path A ticket", tickets_path=tickets_path_a)
        self._write_local_ticket(ticket_id="30", title="Path B ticket", tickets_path=tickets_path_b)
        self._exclude_ai_dev_from_git_status()

        start_code, _, start_stderr = self._invoke_direct("start", "30")
        self.assertEqual(start_code, 0)
        self.assertEqual(start_stderr, "")

        self._write_config(
            {
                "tickets": {
                    "provider": "local",
                    "path": tickets_path_b,
                }
            }
        )

        block_code, _, block_stderr = self._invoke_direct("block", "waiting on path A")
        self.assertEqual(block_code, 0)
        self.assertEqual(block_stderr, "")

        path_a_blocked = json.loads(ticket_a_path.read_text(encoding="utf-8"))
        self.assertEqual(path_a_blocked["workflowState"], "blocked")
        self.assertEqual(path_a_blocked["blockReason"], "waiting on path A")

        path_b_payload = json.loads(ticket_b_path.read_text(encoding="utf-8"))
        self.assertEqual(path_b_payload["workflowState"], "inactive")
        self.assertNotIn("blockReason", path_b_payload)

        resume_code, _, resume_stderr = self._invoke_direct("resume", "30")
        self.assertEqual(resume_code, 0)
        self.assertEqual(resume_stderr, "")

        path_a_resumed = json.loads(ticket_a_path.read_text(encoding="utf-8"))
        self.assertEqual(path_a_resumed["workflowState"], "active")
        self.assertNotIn("blockReason", path_a_resumed)

        path_b_after_resume = json.loads(ticket_b_path.read_text(encoding="utf-8"))
        self.assertEqual(path_b_after_resume["workflowState"], "inactive")
        self.assertNotIn("blockReason", path_b_after_resume)

        blocked_record = json.loads(self._blocked_workflows_path().read_text(encoding="utf-8"))["blockedWorkflows"]
        self.assertEqual(blocked_record, [])

    def test_flow_start_explicit_github_persists_configured_repository_even_when_remote_differs(self) -> None:
        self._set_origin_remote("https://github.com/acme/current-code-repo.git")
        self._write_config(
            {
                "tickets": {
                    "provider": "github",
                    "repository": "acme/ticket-repo",
                }
            }
        )
        self._commit_all("add explicit github config")

        calls: list[list[str]] = []

        def _run_side_effect(command: list[str]):
            cmd = list(command)
            calls.append(cmd)

            if cmd[:6] == ["git", "-C", str(self.repo_root), "remote", "get-url", "origin"]:
                raise AssertionError("explicit github mode should not resolve repository from git remote")

            if cmd[:3] == ["gh", "issue", "view"]:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "number": 12,
                            "title": "Explicit start",
                            "url": "https://github.com/acme/ticket-repo/issues/12",
                            "state": "OPEN",
                            "body": "Body",
                            "labels": [{"name": "active"}],
                            "createdAt": "2026-08-10T00:00:00Z",
                            "updatedAt": "2026-08-10T01:00:00Z",
                            "closedAt": None,
                        }
                    ),
                    stderr="",
                )

            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="unexpected")

        with patch("ai_dev_flow.ticket_providers._run_command", side_effect=_run_side_effect):
            code, stdout, stderr = self._invoke_direct("start", "12")

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Started issue 12", stdout)

        workflow = json.loads(self._workflow_path().read_text(encoding="utf-8"))
        self.assertEqual(workflow["ticket"]["provider"], "github")
        self.assertEqual(workflow["ticket"]["ticketId"], "12")
        self.assertEqual(workflow["ticket"]["repository"], "acme/ticket-repo")
        self.assertEqual(workflow["ticket"]["url"], "https://github.com/acme/ticket-repo/issues/12")

        gh_view_calls = [call for call in calls if call[:3] == ["gh", "issue", "view"]]
        self.assertGreaterEqual(len(gh_view_calls), 1)
        for call in gh_view_calls:
            self.assertIn("--repo", call)
            self.assertEqual(call[call.index("--repo") + 1], "acme/ticket-repo")

    def test_flow_start_github_current_persists_repository_resolved_from_current_project(self) -> None:
        self._set_origin_remote("https://github.com/acme/current-code-repo.git")
        self._write_config(
            {
                "tickets": {
                    "provider": "github-current",
                }
            }
        )
        self._commit_all("add github-current config")

        def _run_side_effect(command: list[str]):
            cmd = list(command)
            if cmd[:6] == ["git", "-C", str(self.repo_root), "remote", "get-url", "origin"]:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="https://github.com/acme/current-code-repo.git\n",
                    stderr="",
                )
            if cmd[:3] == ["gh", "issue", "view"]:
                self.assertIn("--repo", cmd)
                self.assertEqual(cmd[cmd.index("--repo") + 1], "acme/current-code-repo")
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "number": 3,
                            "title": "Current repo start",
                            "url": "https://github.com/acme/current-code-repo/issues/3",
                            "state": "OPEN",
                            "body": "Body",
                            "labels": [{"name": "active"}],
                            "createdAt": "2026-08-10T00:00:00Z",
                            "updatedAt": "2026-08-10T01:00:00Z",
                            "closedAt": None,
                        }
                    ),
                    stderr="",
                )
            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="unexpected")

        with patch("ai_dev_flow.ticket_providers._run_command", side_effect=_run_side_effect):
            code, stdout, stderr = self._invoke_direct("start", "3")

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Started issue 3", stdout)

        workflow = json.loads(self._workflow_path().read_text(encoding="utf-8"))
        self.assertEqual(workflow["ticket"]["provider"], "github")
        self.assertEqual(workflow["ticket"]["ticketId"], "3")
        self.assertEqual(workflow["ticket"]["repository"], "acme/current-code-repo")

    def test_flow_start_persisted_reference_is_stable_after_config_or_remote_changes(self) -> None:
        self._set_origin_remote("https://github.com/acme/current-code-repo.git")
        self._write_config(
            {
                "tickets": {
                    "provider": "github-current",
                }
            }
        )
        self._commit_all("add github-current config")

        def _run_side_effect(command: list[str]):
            cmd = list(command)
            if cmd[:6] == ["git", "-C", str(self.repo_root), "remote", "get-url", "origin"]:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="https://github.com/acme/current-code-repo.git\n",
                    stderr="",
                )
            if cmd[:3] == ["gh", "issue", "view"]:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "number": 31,
                            "title": "Stable identity",
                            "url": "https://github.com/acme/current-code-repo/issues/31",
                            "state": "OPEN",
                            "body": "Body",
                            "labels": [{"name": "active"}],
                            "createdAt": "2026-08-10T00:00:00Z",
                            "updatedAt": "2026-08-10T01:00:00Z",
                            "closedAt": None,
                        }
                    ),
                    stderr="",
                )
            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="unexpected")

        with patch("ai_dev_flow.ticket_providers._run_command", side_effect=_run_side_effect):
            code, _, _ = self._invoke_direct("start", "31")
        self.assertEqual(code, 0)

        before = json.loads(self._workflow_path().read_text(encoding="utf-8"))
        self.assertEqual(before["ticket"]["repository"], "acme/current-code-repo")

        self._write_config(
            {
                "tickets": {
                    "provider": "github",
                    "repository": "acme/other-repo",
                }
            }
        )
        self._run_git("remote", "set-url", "origin", "https://github.com/acme/changed-remote.git")

        status_code, _, status_stderr = self._invoke_direct("status")
        self.assertEqual(status_code, 0)
        self.assertEqual(status_stderr, "")

        after = json.loads(self._workflow_path().read_text(encoding="utf-8"))
        self.assertEqual(after["ticket"], before["ticket"])

    def test_flow_start_closed_ticket_fails_without_creating_active_workflow(self) -> None:
        self._write_config(
            {
                "tickets": {
                    "provider": "local",
                    "path": ".ai-dev/tickets",
                }
            }
        )
        self._write_local_ticket(ticket_id="8", title="Closed local ticket", lifecycle_state="closed")
        self._commit_all("add closed local ticket fixture")

        workflow_path = self._workflow_path()
        self.assertFalse(workflow_path.exists())

        code, stdout, stderr = self._invoke_direct("start", "8")

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Cannot start workflow: ticket 8 is closed.", stderr)
        self.assertFalse(workflow_path.exists())

    def test_flow_start_provider_resolution_failure_leaves_workflow_state_unchanged(self) -> None:
        self._write_config(
            {
                "tickets": {
                    "provider": "github-current",
                }
            }
        )
        workflow_path = self._workflow_path()
        workflow_path.parent.mkdir(parents=True, exist_ok=True)
        workflow_path.write_text(
            json.dumps(
                {
                    "mainBranch": "main",
                    "scratchBranch": "scratch",
                    "checkpoint": 0,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        self._commit_all("add workflow and config fixtures")
        before = workflow_path.read_text(encoding="utf-8")

        def _run_side_effect(command: list[str]):
            cmd = list(command)
            if cmd[:6] == ["git", "-C", str(self.repo_root), "remote", "get-url", "origin"]:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=2,
                    stdout="",
                    stderr="error: No such remote 'origin'",
                )
            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="unexpected")

        with patch("ai_dev_flow.ticket_providers._run_command", side_effect=_run_side_effect):
            code, stdout, stderr = self._invoke_direct("start", "4")

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Cannot resolve github-current repository from current project", stderr)
        self.assertEqual(workflow_path.read_text(encoding="utf-8"), before)

    def test_flow_start_keeps_existing_git_safety_checks(self) -> None:
        self._write_config(
            {
                "tickets": {
                    "provider": "local",
                    "path": ".ai-dev/tickets",
                }
            }
        )
        self._write_local_ticket(ticket_id="2", title="Git safety ticket")
        self._commit_all("add local ticket fixture")
        (self.repo_root / "README.md").write_text("dirty\n", encoding="utf-8")

        code, stdout, stderr = self._invoke_direct("start", "2")

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Working tree is not clean", stderr)


if __name__ == "__main__":
    unittest.main()

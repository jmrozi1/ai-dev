from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from ai_dev_flow.skill_installation import install_skill_packages
from ai_dev_flow.ticket_status import TicketStatusError, main, render_active_ticket_status


class TicketStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmpdir.name) / "repo"
        self.repo_root.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo_root, check=True)
        subprocess.run(["git", "config", "user.name", "Ticket Status Tests"], cwd=self.repo_root, check=True)
        subprocess.run(["git", "config", "user.email", "ticket-status@example.com"], cwd=self.repo_root, check=True)
        (self.repo_root / "README.md").write_text("status\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo_root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=self.repo_root, check=True)
        subprocess.run(["git", "branch", "-M", "main"], cwd=self.repo_root, check=True)

        ai_dev = self.repo_root / ".ai-dev"
        tickets = ai_dev / "tickets"
        tickets.mkdir(parents=True)
        (ai_dev / "config.json").write_text(
            json.dumps({"tickets": {"provider": "local", "path": ".ai-dev/tickets"}}),
            encoding="utf-8",
        )
        (tickets / "39.json").write_text(
            json.dumps(
                {
                    "reference": {"provider": "local", "ticketId": "39", "path": ".ai-dev/tickets"},
                    "title": "Ticket-oriented status",
                    "body": """## Checkpoints

- [x] **Define status contract**
  Document the project-progress surface.
- [ ] **Render active roadmap**
  Use the first incomplete named checkpoint.
- [ ] **Validate output**
  Cover normal and verbose output.

## Acceptance Criteria

- [ ] This must never appear in normal status.

## Full Description

Detailed implementation context belongs here.
""",
                    "lifecycleState": "open",
                    "workflowState": "active",
                }
            ),
            encoding="utf-8",
        )
        (ai_dev / "workflow.json").write_text(
            json.dumps(
                {
                    "mainBranch": "main",
                    "scratchBranch": "scratch",
                    "checkpoint": 9,
                    "activeIssueNumber": 39,
                    "activeIssueTitle": "Ticket-oriented status",
                    "ticket": {"provider": "local", "ticketId": "39", "path": ".ai-dev/tickets"},
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_normal_status_uses_named_roadmap_not_flow_checkpoint(self) -> None:
        output = render_active_ticket_status(self.repo_root)

        self.assertEqual(
            output,
            "\n".join(
                (
                    "Active ticket: #39 Ticket-oriented status",
                    "Checkpoints: 1/3 completed",
                    "Current checkpoint: Render active roadmap",
                )
            ),
        )
        self.assertNotIn("Acceptance Criteria", output)
        self.assertNotIn("Executive Summary", output)
        self.assertNotIn("checkpoint: 9", output)
        self.assertNotIn("branch", output.lower())

    def test_normal_status_ignores_legacy_executive_summary(self) -> None:
        ticket_path = self.repo_root / ".ai-dev" / "tickets" / "39.json"
        ticket = json.loads(ticket_path.read_text(encoding="utf-8"))
        ticket["body"] = """## Executive Summary

Legacy content to ignore.

## Checkpoints

- [ ] **Render active roadmap**: Use the first incomplete named checkpoint.

## Full Description

Detailed implementation context belongs here.
"""
        ticket_path.write_text(json.dumps(ticket), encoding="utf-8")

        output = render_active_ticket_status(self.repo_root)

        self.assertNotIn("Executive Summary", output)
        self.assertNotIn("Legacy content", output)
        self.assertEqual(output.splitlines()[0], "Active ticket: #39 Ticket-oriented status")

    def test_verbose_status_requires_full_description_only_when_verbose(self) -> None:
        ticket_path = self.repo_root / ".ai-dev" / "tickets" / "39.json"
        ticket = json.loads(ticket_path.read_text(encoding="utf-8"))
        ticket["body"] = """## Checkpoints

- [ ] **Render active roadmap**: Use the first incomplete named checkpoint.
"""
        ticket_path.write_text(json.dumps(ticket), encoding="utf-8")

        self.assertIn("Current checkpoint: Render active roadmap", render_active_ticket_status(self.repo_root))
        with self.assertRaisesRegex(
            TicketStatusError,
            "missing its Full Description section",
        ):
            render_active_ticket_status(self.repo_root, verbose=True)

    def test_verbose_status_adds_full_description_and_roadmap_detail(self) -> None:
        output = render_active_ticket_status(self.repo_root, verbose=True)

        self.assertEqual(
            output,
            "\n".join(
                (
                    "Active ticket: #39 Ticket-oriented status",
                    "Checkpoints: 1/3 completed",
                    "Current checkpoint: Render active roadmap",
                    "Full Description:",
                    "Detailed implementation context belongs here.",
                    "Checkpoints:",
                    "- [x] Define status contract: Document the project-progress surface.",
                    "- [ ] Render active roadmap: Use the first incomplete named checkpoint.",
                    "- [ ] Validate output: Cover normal and verbose output.",
                )
            ),
        )
        self.assertNotIn("Executive Summary", output)
        self.assertNotIn("Acceptance Criteria", output)

    def test_inactive_status_behavior_is_unchanged(self) -> None:
        workflow_path = self.repo_root / ".ai-dev" / "workflow.json"
        workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
        workflow.pop("activeIssueNumber")
        workflow.pop("activeIssueTitle", None)
        workflow.pop("ticket")
        workflow_path.write_text(json.dumps(workflow), encoding="utf-8")

        with self.assertRaisesRegex(TicketStatusError, "No active ticket workflow"):
            render_active_ticket_status(self.repo_root)

    def test_invalid_status_arguments_are_unchanged(self) -> None:
        with self.assertRaisesRegex(TicketStatusError, r"Usage: /status \[verbose\]"):
            main(["unexpected"])

    def test_installed_copilot_flow_package_services_status_without_path_launchers(self) -> None:
        source_repo = Path(__file__).resolve().parents[1]
        installed_skills = self.repo_root.parent / "installed-skills"
        home = self.repo_root.parent / "home"
        home.mkdir()
        install_skill_packages(
            repo_root=source_repo,
            destination_root=installed_skills,
            home=home,
            audience="copilot",
        )
        path_without_flow_launchers = os.pathsep.join(("/usr/bin", "/bin"))
        self.assertIsNone(shutil.which("flow-status", path=path_without_flow_launchers))
        environment = {**os.environ, "PATH": path_without_flow_launchers}
        helper = installed_skills / "flow" / "scripts" / "ticket-status"
        powershell_helper = installed_skills / "flow" / "scripts" / "ticket-status.ps1"

        self.assertTrue(powershell_helper.exists())
        self.assertIn("ai_dev_flow.ticket_status", powershell_helper.read_text(encoding="utf-8"))

        normal = subprocess.run(
            [str(helper)],
            cwd=self.repo_root,
            env=environment,
            check=True,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
        )
        verbose = subprocess.run(
            [str(helper), "verbose"],
            cwd=self.repo_root,
            env=environment,
            check=True,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
        )

        self.assertEqual(normal.stdout.strip(), render_active_ticket_status(self.repo_root))
        self.assertEqual(verbose.stdout.strip(), render_active_ticket_status(self.repo_root, verbose=True))


if __name__ == "__main__":
    unittest.main()
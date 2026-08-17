from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from ai_dev_flow.skill_installation import install_skill_packages
from ai_dev_flow.ticket_status import render_active_ticket_status


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
                    "body": """## Executive Summary

Show progress without repository diagnostics.

## Checkpoints

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
                    "Executive Summary:",
                    "Show progress without repository diagnostics.",
                )
            ),
        )
        self.assertNotIn("Acceptance Criteria", output)
        self.assertNotIn("checkpoint: 9", output)
        self.assertNotIn("branch", output.lower())

    def test_verbose_status_adds_full_description_and_roadmap_detail(self) -> None:
        output = render_active_ticket_status(self.repo_root, verbose=True)

        self.assertIn("Full Description:\nDetailed implementation context belongs here.", output)
        self.assertIn("Checkpoints:\n- [x] Define status contract: Document the project-progress surface.", output)
        self.assertIn("- [ ] Render active roadmap: Use the first incomplete named checkpoint.", output)
        self.assertNotIn("Acceptance Criteria", output)

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
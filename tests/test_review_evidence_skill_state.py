from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER = REPO_ROOT / "skills" / "copilot" / "auto-review" / "scripts" / "review-evidence"


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


class ReviewEvidenceTicketSkillStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.root = Path(self.tmp_dir.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()

        _git(self.repo, "init", "--quiet")
        _git(self.repo, "symbolic-ref", "HEAD", "refs/heads/main")
        _git(self.repo, "config", "user.email", "review@example.test")
        _git(self.repo, "config", "user.name", "Review Test")
        (self.repo / "README.md").write_text("# fixture\n", encoding="utf-8")
        _git(self.repo, "add", "README.md")
        _git(self.repo, "commit", "--quiet", "-m", "base")
        _git(self.repo, "checkout", "--quiet", "-b", "scratch")

        ai_dev = self.repo / ".ai-dev"
        tickets = ai_dev / "tickets"
        tickets.mkdir(parents=True)
        (ai_dev / "workflow.json").write_text(
            json.dumps(
                {
                    "mainBranch": "main",
                    "scratchBranch": "scratch",
                    "checkpoint": 1,
                    "activeIssueNumber": 7,
                    "activeIssueTitle": "Fixture issue",
                    "ticket": {
                        "provider": "local",
                        "ticketId": "7",
                        "path": ".ai-dev/tickets",
                    },
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        self.ticket_path = tickets / "7.json"

    def _write_ticket(self, body: str) -> None:
        self.ticket_path.write_text(
            json.dumps(
                {
                    "reference": {
                        "provider": "local",
                        "ticketId": "7",
                        "path": ".ai-dev/tickets",
                    },
                    "title": "Fixture issue",
                    "lifecycleState": "open",
                    "workflowState": "active",
                    "body": body,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def _run_helper(self) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["HOME"] = str(self.root / "home")
        return subprocess.run(
            [str(HELPER), "--mode", "checkpoint"],
            cwd=self.repo,
            capture_output=True,
            text=True,
            env=environment,
        )

    def test_active_ticket_skill_sections_are_rendered_compactly(self) -> None:
        self._write_ticket(
            "## Checkpoints\n\n"
            "- [x] first: completed\n\n"
            "## Skill Candidates\n\n"
            "- pet-battle lookup: candidate from checkpoint M\n\n"
            "## Skills\n\n"
            "- investigate-pet-battles: in progress\n\n"
            "## Acceptance Criteria\n\n"
            "- done\n\n"
            "## Full Description\n\n"
            "fixture\n"
        )

        result = self._run_helper()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("## Ticket Skill State", result.stdout)
        self.assertIn("### Skill Candidates", result.stdout)
        self.assertIn("pet-battle lookup: candidate from checkpoint M", result.stdout)
        self.assertIn("### Skills", result.stdout)
        self.assertIn("investigate-pet-battles: in progress", result.stdout)
        self.assertNotIn("## Acceptance Criteria\n\n- done", result.stdout)

    def test_legacy_ticket_missing_sections_is_reported_not_treated_as_empty(self) -> None:
        self._write_ticket(
            "## Checkpoints\n\n"
            "- [x] first: completed\n\n"
            "## Acceptance Criteria\n\n"
            "- done\n\n"
            "## Full Description\n\n"
            "fixture\n"
        )

        result = self._run_helper()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("missing: ## Skill Candidates", result.stdout)
        self.assertIn("missing: ## Skills", result.stdout)


if __name__ == "__main__":
    unittest.main()

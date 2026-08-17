from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from ai_dev_flow.skill_installation import install_skill_packages


class SkillLocalLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        source_repo = Path(__file__).resolve().parents[1]
        self.installed_skills = self.tmp_path / "installed-skills"
        home = self.tmp_path / "home"
        home.mkdir()
        install_skill_packages(
            repo_root=source_repo,
            destination_root=self.installed_skills,
            home=home,
            audience="copilot",
        )
        self.scripts = self.installed_skills / "flow" / "scripts"
        self.environment = {**os.environ, "PATH": os.pathsep.join(("/usr/bin", "/bin"))}
        self.assertIsNone(shutil.which("flow-start", path=self.environment["PATH"]))

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _repo(self, name: str, *, ticket: bool = False) -> Path:
        repo = self.tmp_path / name
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Skill Lifecycle Test"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "skill-lifecycle@example.com"], cwd=repo, check=True)
        (repo / ".gitignore").write_text(".ai-dev/\n", encoding="utf-8")
        (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", ".gitignore", "tracked.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)
        subprocess.run(["git", "branch", "-M", "main"], cwd=repo, check=True)
        if ticket:
            ai_dev = repo / ".ai-dev"
            tickets = ai_dev / "tickets"
            tickets.mkdir(parents=True)
            (ai_dev / "config.json").write_text(
                json.dumps({"tickets": {"provider": "local", "path": ".ai-dev/tickets"}}),
                encoding="utf-8",
            )
            (tickets / "1.json").write_text(
                json.dumps(
                    {
                        "reference": {"provider": "local", "ticketId": "1", "path": ".ai-dev/tickets"},
                        "title": "Installed skill lifecycle",
                        "body": """## Executive Summary

Exercise the installed helper package.

## Checkpoints

- [x] **Started**
  Begin the workflow.
- [ ] **Validate**
  Validate local lifecycle behavior.

## Acceptance Criteria

- [ ] Hidden from status.

## Full Description

Detailed lifecycle test context.
""",
                        "lifecycleState": "open",
                        "workflowState": "inactive",
                    }
                ),
                encoding="utf-8",
            )
        return repo

    def _run(self, repo: Path, helper: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.scripts / helper), *arguments],
            cwd=repo,
            env=self.environment,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )

    def test_installed_helpers_cover_ticket_patch_and_local_completion_lifecycle(self) -> None:
        ticket_repo = self._repo("ticket", ticket=True)
        started = self._run(ticket_repo, "flow-start", "1")
        self.assertEqual(started.returncode, 0, started.stderr)
        self.assertEqual(subprocess.run(["git", "branch", "--show-current"], cwd=ticket_repo, capture_output=True, text=True, check=True).stdout.strip(), "scratch")
        normal_status = self._run(ticket_repo, "ticket-status")
        verbose_status = self._run(ticket_repo, "ticket-status", "verbose")
        self.assertIn("Current checkpoint: Validate", normal_status.stdout)
        self.assertNotIn("Acceptance Criteria", normal_status.stdout)
        self.assertIn("Full Description:", verbose_status.stdout)
        self.assertEqual(self._run(ticket_repo, "flow-block", "waiting").returncode, 0)
        self.assertEqual(self._run(ticket_repo, "flow-resume", "1").returncode, 0)

        patch_repo = self._repo("patch")
        self.assertEqual(self._run(patch_repo, "flow-patch", "Scoped patch").returncode, 0)
        (patch_repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
        self.assertEqual(self._run(patch_repo, "flow-commit").returncode, 0)
        self.assertEqual(self._run(patch_repo, "flow-reset").returncode, 0)
        self.assertEqual((patch_repo / "tracked.txt").read_text(encoding="utf-8"), "base\n")

        adopt_repo = self._repo("adopt")
        subprocess.run(["git", "checkout", "-q", "-b", "scratch"], cwd=adopt_repo, check=True)
        (adopt_repo / "tracked.txt").write_text("adopted\n", encoding="utf-8")
        adopted = self._run(adopt_repo, "flow-patch", "--adopt", "Adopted patch")
        self.assertEqual(adopted.returncode, 0, adopted.stderr)
        self.assertEqual((adopt_repo / "tracked.txt").read_text(encoding="utf-8"), "adopted\n")

        promote_repo = self._repo("promote")
        self.assertEqual(self._run(promote_repo, "flow-patch", "Promote locally").returncode, 0)
        (promote_repo / "promoted.txt").write_text("done\n", encoding="utf-8")
        self.assertEqual(self._run(promote_repo, "flow-commit").returncode, 0)
        (promote_repo / ".ai-dev" / "config.json").write_text(json.dumps({"review": {"promotionGate": False}}), encoding="utf-8")
        promoted = self._run(promote_repo, "flow-promote", "Ship local patch")
        self.assertEqual(promoted.returncode, 0, promoted.stderr)
        completed = self._run(promote_repo, "flow-complete")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        workflow = json.loads((promote_repo / ".ai-dev" / "workflow.json").read_text(encoding="utf-8"))
        self.assertNotIn("patchDescription", workflow)
        self.assertEqual(workflow["checkpoint"], 0)
        heads = subprocess.run(["git", "rev-parse", "main", "scratch"], cwd=promote_repo, capture_output=True, text=True, check=True).stdout.splitlines()
        self.assertEqual(heads[0], heads[1])


if __name__ == "__main__":
    unittest.main()
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class FlowLauncherStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    @unittest.skipUnless(os.name == "nt", "Windows-only launcher test")
    def test_flow_ps1_launcher_uses_caller_working_directory(self) -> None:
        outside = self.tmp_path / "outside-launcher"
        outside.mkdir(parents=True, exist_ok=True)

        repo_root = Path(__file__).resolve().parents[1]
        launcher_path = repo_root / "scripts" / "flow.ps1"
        self.assertTrue(launcher_path.exists(), "flow.ps1 is required")

        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"& '{launcher_path}' status",
            ],
            cwd=str(outside),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(completed.returncode, 1)
        combined_output = completed.stdout + completed.stderr
        self.assertIn(
            "Not inside a Git repository. Run this command from within a repository.",
            combined_output,
        )

    @unittest.skipUnless(os.name == "nt", "Windows-only bootstrap test")
    def test_bootstrap_flow_shim_targets_canonical_launcher(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        bootstrap_path = repo_root / "scripts" / "bootstrap-flow.ps1"
        launcher_path = repo_root / "scripts" / "flow.ps1"
        self.assertTrue(bootstrap_path.exists(), "bootstrap-flow.ps1 is required")
        self.assertTrue(launcher_path.exists(), "flow.ps1 is required")

        home_root = self.tmp_path / "home"
        home_root.mkdir(parents=True, exist_ok=True)
        user_bin = home_root / ".local" / "bin"
        user_bin.mkdir(parents=True, exist_ok=True)

        shim_path = user_bin / "flow.ps1"
        path_value = os.environ.get("Path", "")
        if path_value:
            path_value = f"{user_bin};{path_value}"
        else:
            path_value = str(user_bin)
        outside = self.tmp_path / "outside-launcher"
        outside.mkdir(parents=True, exist_ok=True)

        def run_bootstrap() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(bootstrap_path),
                ],
                cwd=str(repo_root),
                env={
                    **os.environ,
                    "HOME": str(home_root),
                    "UserProfile": str(home_root),
                    "FLOW_BOOTSTRAP_HOME": str(home_root),
                    "Path": path_value,
                    "FLOW_BOOTSTRAP_SKIP_USER_PATH_UPDATE": "1",
                },
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        first = run_bootstrap()
        self.assertEqual(first.returncode, 0)
        self.assertTrue(shim_path.exists())
        first_text = shim_path.read_text(encoding="utf-8")
        self.assertIn("scripts\\flow.ps1", first_text)

        shim_run = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"& '{shim_path}' status",
            ],
            cwd=str(outside),
            env={
                **os.environ,
                "HOME": str(home_root),
                "UserProfile": str(home_root),
                "FLOW_BOOTSTRAP_HOME": str(home_root),
                "Path": path_value,
                "FLOW_BOOTSTRAP_SKIP_USER_PATH_UPDATE": "1",
            },
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(shim_run.returncode, 1)
        self.assertIn("Not inside a Git repository", shim_run.stdout + shim_run.stderr)

        second = run_bootstrap()
        self.assertEqual(second.returncode, 0)
        self.assertEqual(shim_path.read_text(encoding="utf-8"), first_text)
        self.assertIn("Shim path:", first.stdout)
        self.assertEqual(second.stdout, first.stdout)


if __name__ == "__main__":
    unittest.main()

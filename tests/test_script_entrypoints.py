from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class ScriptEntrypointTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.repo_root = Path(__file__).resolve().parents[1]

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _write_fake_python(self, path: Path, *, version: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "if [[ ${1:-} == '-c' ]]; then\n"
            f"  printf '%s\\n' '{version}'\n"
            "  exit 0\n"
            "fi\n"
            "if [[ ${1:-} == '-m' ]]; then\n"
            "  printf '%s %s\\n' \"$0\" \"$*\" >> \"$AI_DEV_TEST_SELECTED_LOG\"\n"
            "  exit 0\n"
            "fi\n",
            encoding="utf-8",
        )
        path.chmod(0o755)

    def test_install_sh_help_describes_cleanup_only(self) -> None:
        completed = subprocess.run(
            ["bash", str(self.repo_root / "scripts" / "install.sh"), "--help"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=self.repo_root,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertIn("Safely remove AI Dev-managed legacy Flow launchers", completed.stdout)
        self.assertIn("--home <path>", completed.stdout)
        self.assertNotIn("--force", completed.stdout)
        self.assertNotIn("flow-ticket-create", completed.stdout)

    def test_install_sh_uses_compatible_interpreter_for_cleanup(self) -> None:
        fake_bin = self.tmp_path / "fake-bin"
        selected_log = self.tmp_path / "selected.log"
        self._write_fake_python(fake_bin / "python3", version="3.6.15")
        self._write_fake_python(fake_bin / "python3.11", version="3.11.9")
        completed = subprocess.run(
            ["bash", str(self.repo_root / "scripts" / "install.sh")],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=self.repo_root,
            env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}", "AI_DEV_TEST_SELECTED_LOG": str(selected_log), "HOME": str(self.tmp_path / "home")},
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        self.assertIn("python3.11 -m ai_dev_flow.bootstrap --platform posix", selected_log.read_text(encoding="utf-8"))

    def test_test_sh_help_and_list_keep_supported_suites(self) -> None:
        script = self.repo_root / "scripts" / "test.sh"
        for argument in ("--help", "--list"):
            with self.subTest(argument=argument):
                completed = subprocess.run(
                    ["bash", str(script), argument],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    cwd=self.repo_root,
                )
                self.assertEqual(completed.returncode, 0)
                for suite in ("unit", "bootstrap", "flow", "integration", "all"):
                    self.assertIn(suite, completed.stdout)

    def test_test_sh_rejects_unknown_suite(self) -> None:
        completed = subprocess.run(
            ["bash", str(self.repo_root / "scripts" / "test.sh"), "unknown-suite"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=self.repo_root,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("unknown suite", completed.stderr)

    def test_install_ps1_help_when_available(self) -> None:
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("pwsh is not available")
        completed = subprocess.run(
            [pwsh, "-NoProfile", "-File", str(self.repo_root / "scripts" / "install.ps1"), "--help"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=self.repo_root,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertIn("Safely remove AI Dev-managed legacy Flow launchers", completed.stdout)


if __name__ == "__main__":
    unittest.main()

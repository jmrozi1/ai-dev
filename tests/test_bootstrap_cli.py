from __future__ import annotations

from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_dev_flow import bootstrap


class BootstrapCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.repo_root = Path(__file__).resolve().parents[1]
        self.config_path = self.tmp_path / "cfg" / "config.yaml"

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_parse_args_defaults(self) -> None:
        parsed = bootstrap._parse_args(["--repo-root", str(self.repo_root)])
        self.assertEqual(parsed.prefix, "flow")
        self.assertEqual(parsed.repo_root, str(self.repo_root))

    def test_parse_args_accepts_prefix(self) -> None:
        parsed = bootstrap._parse_args(["--repo-root", str(self.repo_root), "--prefix", "flow"])
        self.assertEqual(parsed.prefix, "flow")

    def test_parse_args_accepts_force(self) -> None:
        parsed = bootstrap._parse_args(["--repo-root", str(self.repo_root), "--force"])
        self.assertTrue(parsed.force)

    def test_main_success_posix(self) -> None:
        install_dir = self.tmp_path / "install"
        stdout = StringIO()
        stderr = StringIO()
        with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            rc = bootstrap.main(
                [
                    "--platform",
                    "posix",
                    "--repo-root",
                    str(self.repo_root),
                    "--install-dir",
                    str(install_dir),
                    "--home",
                    str(self.tmp_path / "home"),
                    "--config-path",
                    str(self.config_path),
                    "--path-value",
                    "/usr/bin:/bin",
                ]
            )

        self.assertEqual(rc, 0)
        text = stdout.getvalue()
        self.assertIn("Bootstrap complete.", text)
        self.assertNotIn("AI Dev installation completed successfully.", text)
        self.assertIn("Command name: flow-*", text)
        self.assertIn("flow-status", text)
        self.assertIn("PATH status: install directory is not on PATH", text)
        self.assertEqual(stderr.getvalue(), "")

    def test_main_success_concise_installer_output_mode(self) -> None:
        install_dir = self.tmp_path / "install-concise"
        stdout = StringIO()
        stderr = StringIO()
        with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            rc = bootstrap.main(
                [
                    "--platform",
                    "posix",
                    "--repo-root",
                    str(self.repo_root),
                    "--install-dir",
                    str(install_dir),
                    "--home",
                    str(self.tmp_path / "home-concise"),
                    "--config-path",
                    str(self.config_path),
                    "--path-value",
                    str(install_dir),
                    "--installer-output",
                    "concise",
                ]
            )

        self.assertEqual(rc, 0)
        self.assertEqual(stdout.getvalue().strip(), "AI Dev installation completed successfully.")
        self.assertEqual(stderr.getvalue(), "")

    def test_main_success_with_prefix(self) -> None:
        install_dir = self.tmp_path / "install-prefix"
        stdout = StringIO()
        stderr = StringIO()
        with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            rc = bootstrap.main(
                [
                    "--platform",
                    "posix",
                    "--repo-root",
                    str(self.repo_root),
                    "--prefix",
                    "flow",
                    "--install-dir",
                    str(install_dir),
                    "--home",
                    str(self.tmp_path / "home-prefix"),
                    "--config-path",
                    str(self.config_path),
                    "--path-value",
                    "/usr/bin:/bin",
                ]
            )

        self.assertEqual(rc, 0)
        text = stdout.getvalue()
        self.assertIn("Command name: flow-*", text)
        self.assertIn("flow-status", text)
        self.assertEqual(stderr.getvalue(), "")

    def test_main_failure_on_invalid_platform(self) -> None:
        stdout = StringIO()
        stderr = StringIO()
        with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            rc = bootstrap.main(
                [
                    "--platform",
                    "unknown",
                    "--repo-root",
                    str(self.repo_root),
                ]
            )

        self.assertNotEqual(rc, 0)
        self.assertIn("invalid choice", stderr.getvalue())

    def test_main_failure_missing_repo(self) -> None:
        stdout = StringIO()
        stderr = StringIO()
        with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            rc = bootstrap.main(
                [
                    "--platform",
                    "posix",
                    "--repo-root",
                    str(self.tmp_path / "missing"),
                ]
            )

        self.assertEqual(rc, 1)
        self.assertIn("bootstrap:", stderr.getvalue())

    def test_main_windows_guidance_avoids_setx(self) -> None:
        install_dir = self.tmp_path / "install-win"
        stdout = StringIO()
        stderr = StringIO()
        with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            rc = bootstrap.main(
                [
                    "--platform",
                    "windows",
                    "--repo-root",
                    str(self.repo_root),
                    "--install-dir",
                    str(install_dir),
                    "--home",
                    str(self.tmp_path / "home-win"),
                    "--config-path",
                    str(self.config_path),
                    "--path-value",
                    "C:/Windows/System32",
                ]
            )

        self.assertEqual(rc, 0)
        text = stdout.getvalue()
        self.assertIn("Environment Variables", text)
        self.assertNotIn("setx ", text.lower())
        self.assertEqual(stderr.getvalue(), "")

    def test_main_rejects_invalid_prefix(self) -> None:
        invalid_prefixes = ["", " flow", "flow ", "flow/status", "flow*", "flow?", "flow.", "flow_"]
        for invalid_prefix in invalid_prefixes:
            with self.subTest(prefix=invalid_prefix):
                stdout = StringIO()
                stderr = StringIO()
                with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                    rc = bootstrap.main(
                        [
                            "--platform",
                            "posix",
                            "--repo-root",
                            str(self.repo_root),
                            "--prefix",
                            invalid_prefix,
                        ]
                    )

                self.assertEqual(rc, 1)
                self.assertIn("Invalid prefix", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
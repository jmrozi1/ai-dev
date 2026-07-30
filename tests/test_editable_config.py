from __future__ import annotations

import os
from pathlib import Path, PosixPath
import tempfile
import unittest
from unittest.mock import patch

import yaml

from ai_dev_flow.editable_config import EditableConfigError, ensure_editable_user_config
from ai_dev_flow.task_config import load_task_config, resolve_user_config_path


class EditableConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_creates_missing_default_config_and_parent(self) -> None:
        config_path = self.tmp_path / "cfg" / "ai-dev" / "config.yaml"

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(config_path)}, clear=False):
            state = ensure_editable_user_config()

        self.assertTrue(state.created)
        self.assertEqual(state.config_path, config_path)
        self.assertTrue(config_path.exists())
        self.assertTrue(config_path.parent.exists())

        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        self.assertIsInstance(loaded, dict)
        assert isinstance(loaded, dict)
        self.assertEqual(loaded.get("aliases"), {})

    def test_existing_file_is_preserved_byte_for_byte(self) -> None:
        config_path = self.tmp_path / "cfg" / "existing.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        original = "# keep me\nai:\n  delivery: file-only\n"
        config_path.write_text(original, encoding="utf-8")

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(config_path)}, clear=False):
            state = ensure_editable_user_config()

        self.assertFalse(state.created)
        self.assertEqual(config_path.read_text(encoding="utf-8"), original)

    def test_simulated_create_race_reports_created_false_and_preserves_raced_file(self) -> None:
        config_path = self.tmp_path / "cfg" / "raced.yaml"
        raced_text = "ai:\n  delivery: file-only\n"

        original_open = os.open

        def race_open(path: str, flags: int, mode: int) -> int:
            if flags & os.O_EXCL:
                Path(path).write_text(raced_text, encoding="utf-8")
                raise FileExistsError("raced create")
            return original_open(path, flags, mode)

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(config_path)}, clear=False),
            patch("ai_dev_flow.editable_config.os.open", side_effect=race_open),
        ):
            state = ensure_editable_user_config()

        self.assertFalse(state.created)
        self.assertEqual(state.config_path, config_path)
        self.assertEqual(config_path.read_text(encoding="utf-8"), raced_text)

    def test_raced_non_regular_target_raises_and_is_not_removed(self) -> None:
        config_path = self.tmp_path / "cfg" / "raced-non-regular"

        original_open = os.open

        def race_to_directory(path: str, flags: int, mode: int) -> int:
            if flags & os.O_EXCL:
                Path(path).mkdir(parents=True, exist_ok=True)
                raise FileExistsError("raced create")
            return original_open(path, flags, mode)

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(config_path)}, clear=False),
            patch("ai_dev_flow.editable_config.os.open", side_effect=race_to_directory),
        ):
            with self.assertRaises(EditableConfigError) as context:
                ensure_editable_user_config()

        self.assertIn("not a regular file", str(context.exception))
        self.assertTrue(config_path.exists())
        self.assertTrue(config_path.is_dir())

    def test_generated_config_passes_existing_validation(self) -> None:
        repo_root = self.tmp_path / "repo"
        repo_root.mkdir(parents=True)
        config_path = self.tmp_path / "cfg" / "validated.yaml"

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(config_path)}, clear=False):
            ensure_editable_user_config()
            config = load_task_config(repo_root)

        self.assertEqual(config.delivery, "stdout")
        self.assertEqual(config.invocation, "Read and execute {task_file}")
        self.assertEqual(config.report_presentation, "path-only")
        self.assertIsNone(config.editor_command)

    def test_atomic_write_failure_leaves_no_partial_file(self) -> None:
        config_path = self.tmp_path / "cfg" / "atomic-failure.yaml"

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(config_path)}, clear=False),
            patch("ai_dev_flow.editable_config.os.fdopen", side_effect=OSError("write failed")),
        ):
            with self.assertRaises(EditableConfigError):
                ensure_editable_user_config()

        self.assertFalse(config_path.exists())

    def test_environment_override_is_authoritative(self) -> None:
        override = self.tmp_path / "override.yaml"
        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(override)}, clear=False):
            resolved = resolve_user_config_path()

        self.assertEqual(resolved, override)

    def test_linux_xdg_path_resolution(self) -> None:
        xdg_home = self.tmp_path / "xdg-home"
        with (
            patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg_home), "AI_DEV_CONFIG": ""}, clear=False),
            patch("ai_dev_flow.task_config.os.name", "posix"),
        ):
            resolved = resolve_user_config_path()

        self.assertEqual(resolved, xdg_home / "ai-dev" / "config.yaml")

    def test_windows_appdata_path_resolution(self) -> None:
        appdata = self.tmp_path / "AppData" / "Roaming"
        with (
            patch.dict(os.environ, {"APPDATA": str(appdata), "AI_DEV_CONFIG": ""}, clear=False),
            patch("ai_dev_flow.task_config.os.name", "nt"),
            patch("ai_dev_flow.task_config.Path", PosixPath),
        ):
            resolved = resolve_user_config_path()

        self.assertEqual(resolved, appdata / "ai-dev" / "config.yaml")


if __name__ == "__main__":
    unittest.main()

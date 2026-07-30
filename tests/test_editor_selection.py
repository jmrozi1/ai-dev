from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_dev_flow.editor_selection import (
    EditorCandidate,
    EditorSelectionResult,
    launch_selected_editor,
    select_editor_candidate,
)


class _FakeProcess:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.wait_calls: list[float | None] = []

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        return self.returncode


class EditorSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_configured_editor_beats_visual(self) -> None:
        with patch(
            "ai_dev_flow.editor_selection.shutil.which",
            side_effect=lambda name: "/usr/bin/code" if name in {"code", "vim"} else None,
        ):
            selected = select_editor_candidate(
                "code --wait",
                env={"VISUAL": "vim", "EDITOR": "nano"},
                system_name="Linux",
            )

        self.assertIsNotNone(selected.candidate)
        assert selected.candidate is not None
        self.assertEqual(selected.candidate.command_argv, ["code", "--wait"])
        self.assertEqual(selected.candidate.source, "editor.command")

    def test_visual_beats_editor(self) -> None:
        with patch(
            "ai_dev_flow.editor_selection.shutil.which",
            side_effect=lambda name: "/usr/bin/vim" if name in {"vim", "nano"} else None,
        ):
            selected = select_editor_candidate(
                None,
                env={"VISUAL": "vim", "EDITOR": "nano"},
                system_name="Linux",
            )

        self.assertEqual(selected.candidate.command_argv if selected.candidate else None, ["vim"])

    def test_editor_beats_platform_default(self) -> None:
        with patch(
            "ai_dev_flow.editor_selection.shutil.which",
            side_effect=lambda name: "/usr/bin/nano" if name == "nano" else None,
        ):
            selected = select_editor_candidate(
                None,
                env={"VISUAL": "", "EDITOR": "nano"},
                system_name="Linux",
            )

        self.assertEqual(selected.candidate.command_argv if selected.candidate else None, ["nano"])

    def test_unix_default_vi(self) -> None:
        with patch(
            "ai_dev_flow.editor_selection.shutil.which",
            side_effect=lambda name: "/usr/bin/vi" if name == "vi" else None,
        ):
            selected = select_editor_candidate(None, env={}, system_name="Linux")

        self.assertEqual(selected.candidate.command_argv if selected.candidate else None, ["vi"])

    def test_windows_default_notepad_exe(self) -> None:
        selected = select_editor_candidate(None, env={}, system_name="Windows")
        self.assertEqual(selected.candidate.command_argv if selected.candidate else None, ["notepad.exe"])

    def test_windows_quoted_executable_path_with_spaces_is_parsed_without_quotes(self) -> None:
        with (
            patch("ai_dev_flow.editor_selection.Path.exists", return_value=True),
            patch("ai_dev_flow.editor_selection.Path.is_file", return_value=True),
        ):
            selected = select_editor_candidate(
                '"C:\\Program Files\\Microsoft VS Code\\Code.exe" --wait',
                env={},
                system_name="Windows",
            )

        self.assertIsNotNone(selected.candidate)
        assert selected.candidate is not None
        self.assertEqual(
            selected.candidate.command_argv,
            [r"C:\Program Files\Microsoft VS Code\Code.exe", "--wait"],
        )
        self.assertFalse(selected.candidate.command_argv[0].startswith('"'))
        self.assertFalse(selected.candidate.command_argv[0].endswith('"'))

    def test_windows_quoted_executable_path_preserves_multiple_arguments(self) -> None:
        with (
            patch("ai_dev_flow.editor_selection.Path.exists", return_value=True),
            patch("ai_dev_flow.editor_selection.Path.is_file", return_value=True),
        ):
            selected = select_editor_candidate(
                '"C:\\Program Files\\Notepad++\\notepad++.exe" -multiInst --wait',
                env={},
                system_name="Windows",
            )

        self.assertEqual(
            selected.candidate.command_argv if selected.candidate else None,
            [r"C:\Program Files\Notepad++\notepad++.exe", "-multiInst", "--wait"],
        )

    def test_windows_unquoted_command_name_with_args(self) -> None:
        with patch(
            "ai_dev_flow.editor_selection.shutil.which",
            side_effect=lambda name: r"C:\\Tools\\code.exe" if name == "code" else None,
        ):
            selected = select_editor_candidate("code --wait", env={}, system_name="Windows")

        self.assertEqual(selected.candidate.command_argv if selected.candidate else None, ["code", "--wait"])

    def test_windows_malformed_quotes_warn_and_fall_back(self) -> None:
        with patch(
            "ai_dev_flow.editor_selection.shutil.which",
            side_effect=lambda name: "/usr/bin/nano" if name == "nano" else None,
        ):
            selected = select_editor_candidate(
                '"C:\\Program Files\\Microsoft VS Code\\Code.exe --wait',
                env={"EDITOR": "nano"},
                system_name="Windows",
            )

        self.assertEqual(selected.candidate.command_argv if selected.candidate else None, ["nano"])
        self.assertTrue(any("unbalanced double quotes" in warning for warning in selected.warnings))

    def test_explicit_executable_path_directory_is_rejected(self) -> None:
        editor_dir = self.tmp_path / "Program Files" / "Editor"
        editor_dir.mkdir(parents=True)

        with patch(
            "ai_dev_flow.editor_selection.shutil.which",
            side_effect=lambda name: "/usr/bin/nano" if name == "nano" else None,
        ):
            selected = select_editor_candidate(
                f'"{editor_dir}" --wait',
                env={"EDITOR": "nano"},
                system_name="Windows",
            )

        self.assertEqual(selected.candidate.command_argv if selected.candidate else None, ["nano"])
        self.assertTrue(any("not a regular file" in warning for warning in selected.warnings))

    def test_path_only_fallback_when_no_candidates(self) -> None:
        with patch("ai_dev_flow.editor_selection.shutil.which", return_value=None):
            selected = select_editor_candidate(None, env={"VISUAL": "", "EDITOR": ""}, system_name="Linux")

        self.assertIsNone(selected.candidate)
        self.assertTrue(any("No editor candidate is available" in warning for warning in selected.warnings))

    def test_empty_environment_values_ignored(self) -> None:
        with patch(
            "ai_dev_flow.editor_selection.shutil.which",
            side_effect=lambda name: "/usr/bin/vi" if name == "vi" else None,
        ):
            selected = select_editor_candidate(None, env={"VISUAL": "   ", "EDITOR": ""}, system_name="Linux")

        self.assertEqual(selected.candidate.command_argv if selected.candidate else None, ["vi"])

    def test_malformed_configured_editor_command_falls_back(self) -> None:
        with patch(
            "ai_dev_flow.editor_selection.shutil.which",
            side_effect=lambda name: "/usr/bin/nano" if name == "nano" else None,
        ):
            selected = select_editor_candidate('"', env={"EDITOR": "nano"}, system_name="Linux")

        self.assertEqual(selected.candidate.command_argv if selected.candidate else None, ["nano"])
        self.assertTrue(any("Invalid editor.command editor command" in warning for warning in selected.warnings))

    def test_launch_appends_config_path_once_and_no_shell(self) -> None:
        popen_calls: list[tuple[list[str], dict[str, object]]] = []
        fake_process = _FakeProcess(returncode=0)

        def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
            popen_calls.append((command, kwargs))
            return fake_process

        selection = EditorSelectionResult(
            candidate=EditorCandidate(command_argv=["code", "--wait"], source="editor.command"),
            warnings=(),
        )

        target = Path("/tmp/with space/config.yaml")
        with patch("ai_dev_flow.editor_opening.subprocess.Popen", side_effect=fake_popen):
            result = launch_selected_editor(target, selection)

        self.assertTrue(result.opened)
        called_command, called_kwargs = popen_calls[0]
        self.assertEqual(called_command[-1], str(target))
        self.assertNotIn(str(target), called_command[:-1])
        self.assertIn("--wait", called_command)
        self.assertIn(called_kwargs.get("shell"), (None, False))

    def test_launch_preserves_argv_and_appends_target_once(self) -> None:
        popen_calls: list[tuple[list[str], dict[str, object]]] = []
        fake_process = _FakeProcess(returncode=0)

        def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
            popen_calls.append((command, kwargs))
            return fake_process

        selection = EditorSelectionResult(
            candidate=EditorCandidate(
                command_argv=[r"C:\Program Files\Microsoft VS Code\Code.exe", "--wait", "--reuse-window"],
                source="editor.command",
            ),
            warnings=(),
        )

        target = self.tmp_path / "config.yaml"
        with patch("ai_dev_flow.editor_opening.subprocess.Popen", side_effect=fake_popen):
            result = launch_selected_editor(target, selection)

        self.assertTrue(result.opened)
        called_command, called_kwargs = popen_calls[0]
        self.assertEqual(
            called_command[:-1],
            [r"C:\Program Files\Microsoft VS Code\Code.exe", "--wait", "--reuse-window"],
        )
        self.assertEqual(called_command.count(str(target)), 1)
        self.assertIn(called_kwargs.get("shell"), (None, False))

    def test_launch_failure_distinguishes_missing_executable(self) -> None:
        selection = EditorSelectionResult(
            candidate=EditorCandidate(command_argv=["missing-editor"], source="EDITOR"),
            warnings=(),
        )

        with patch(
            "ai_dev_flow.editor_opening.subprocess.Popen",
            side_effect=OSError("[Errno 2] No such file or directory"),
        ):
            result = launch_selected_editor(Path("/tmp/config.yaml"), selection)

        self.assertFalse(result.opened)
        self.assertEqual(result.status, "command-unavailable")


if __name__ == "__main__":
    unittest.main()

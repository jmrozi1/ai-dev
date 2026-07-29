from __future__ import annotations

from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from ai_dev_flow.editor_opening import (
    SubprocessEditorOpener,
    build_editor_opener,
    resolve_editor_command,
)


class _FakeProcess:
    def __init__(self, *, returncode: int = 0, timeout: bool = False, stderr_text: str = "") -> None:
        self.returncode = returncode
        self.timeout = timeout
        self.wait_calls: list[float | None] = []
        self.stderr = _FakeStderr(stderr_text)

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        if self.timeout:
            raise subprocess.TimeoutExpired(cmd=["editor"], timeout=timeout if timeout is not None else 0.0)
        return self.returncode


class _FakeStderr:
    def __init__(self, text: str) -> None:
        self.text = text

    def read(self) -> str:
        return self.text


class EditorOpeningTests(unittest.TestCase):
    def test_configured_command_precedence(self) -> None:
        with patch(
            "ai_dev_flow.editor_opening.shutil.which",
            side_effect=lambda name: "/usr/bin/code" if name == "code" else "/usr/bin/vim",
        ):
            resolved = resolve_editor_command(
                "code --wait",
                env={"VISUAL": "vim", "EDITOR": "nano"},
                system_name="Linux",
            )

        self.assertEqual(resolved.command_argv, ["code", "--wait"])
        self.assertEqual(resolved.source, "editor.command")
        self.assertEqual(resolved.warnings, ())

    def test_configured_unavailable_falls_back_to_visual_with_warning(self) -> None:
        with patch(
            "ai_dev_flow.editor_opening.shutil.which",
            side_effect=lambda name: "/usr/bin/vim" if name == "vim" else None,
        ):
            resolved = resolve_editor_command(
                "missing-editor",
                env={"VISUAL": "vim", "EDITOR": "nano"},
                system_name="Linux",
            )

        self.assertEqual(resolved.command_argv, ["vim"])
        self.assertEqual(resolved.source, "VISUAL")
        self.assertTrue(any("Configured editor command is not available" in warning for warning in resolved.warnings))

    def test_configured_malformed_falls_back_to_editor_with_warning(self) -> None:
        with patch(
            "ai_dev_flow.editor_opening.shutil.which",
            side_effect=lambda name: "/usr/bin/nano" if name == "nano" else None,
        ):
            resolved = resolve_editor_command(
                '"',
                env={"VISUAL": "", "EDITOR": "nano"},
                system_name="Linux",
            )

        self.assertEqual(resolved.command_argv, ["nano"])
        self.assertEqual(resolved.source, "EDITOR")
        self.assertTrue(any("Invalid configured editor.command" in warning for warning in resolved.warnings))

    def test_visual_precedence_over_editor(self) -> None:
        with patch(
            "ai_dev_flow.editor_opening.shutil.which",
            side_effect=lambda name: "/usr/bin/vim" if name in {"vim", "nano"} else None,
        ):
            resolved = resolve_editor_command(
                None,
                env={"VISUAL": "vim", "EDITOR": "nano"},
                system_name="Linux",
            )

        self.assertEqual(resolved.command_argv, ["vim"])
        self.assertEqual(resolved.source, "VISUAL")

    def test_visual_unavailable_falls_back_to_editor_and_preserves_warning(self) -> None:
        with patch(
            "ai_dev_flow.editor_opening.shutil.which",
            side_effect=lambda name: "/usr/bin/nano" if name == "nano" else None,
        ):
            resolved = resolve_editor_command(
                None,
                env={"VISUAL": "missing", "EDITOR": "nano"},
                system_name="Linux",
            )

        self.assertEqual(resolved.command_argv, ["nano"])
        self.assertEqual(resolved.source, "EDITOR")
        self.assertTrue(any("VISUAL command is not available" in warning for warning in resolved.warnings))

    def test_all_user_and_env_candidates_fail_then_platform_fallback_succeeds(self) -> None:
        with patch(
            "ai_dev_flow.editor_opening.shutil.which",
            side_effect=lambda name: "/usr/bin/vi" if name == "vi" else None,
        ):
            resolved = resolve_editor_command(
                '"',
                env={"VISUAL": "missing-visual", "EDITOR": "missing-editor"},
                system_name="Linux",
            )

        self.assertEqual(resolved.command_argv, ["vi"])
        self.assertEqual(resolved.source, "platform-fallback")
        self.assertTrue(any("Invalid configured editor.command" in warning for warning in resolved.warnings))
        self.assertTrue(any("VISUAL command is not available" in warning for warning in resolved.warnings))
        self.assertTrue(any("EDITOR command is not available" in warning for warning in resolved.warnings))

    def test_windows_platform_fallback_to_notepad(self) -> None:
        with patch(
            "ai_dev_flow.editor_opening.shutil.which",
            side_effect=lambda name: "C:/Windows/notepad.exe" if name == "notepad" else None,
        ):
            resolved = resolve_editor_command(None, env={}, system_name="Windows")

        self.assertEqual(resolved.command_argv, ["notepad"])
        self.assertEqual(resolved.source, "platform-fallback")

    def test_all_candidates_fail_returns_null_opener(self) -> None:
        with patch("ai_dev_flow.editor_opening.shutil.which", return_value=None):
            opener = build_editor_opener("missing", env={"VISUAL": "bad", "EDITOR": "also-bad"}, system_name="Linux")

        result = opener.open_path(Path("/tmp/missing"))
        self.assertFalse(result.opened)
        self.assertIn("No editor command is available", result.warning or "")
        self.assertIn("Configured editor command is not available", result.warning or "")

    def test_vi_inherits_stdio_and_waits(self) -> None:
        popen_calls: list[tuple[list[str], dict[str, object]]] = []
        fake_process = _FakeProcess(returncode=0)

        def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
            popen_calls.append((command, kwargs))
            return fake_process

        with patch("ai_dev_flow.editor_opening.subprocess.Popen", side_effect=fake_popen):
            result = SubprocessEditorOpener(["vi"]).open_path(Path("/tmp/a"))

        self.assertTrue(result.opened)
        called_command, called_kwargs = popen_calls[0]
        self.assertEqual(called_command, ["vi", "/tmp/a"])
        self.assertNotIn("stdin", called_kwargs)
        self.assertNotIn("stdout", called_kwargs)
        self.assertNotIn("stderr", called_kwargs)
        self.assertEqual(fake_process.wait_calls, [None])

    def test_vi_nonzero_exit_returns_warning(self) -> None:
        with patch("ai_dev_flow.editor_opening.subprocess.Popen", return_value=_FakeProcess(returncode=3)):
            result = SubprocessEditorOpener(["vi"]).open_path(Path("/tmp/a"))

        self.assertFalse(result.opened)
        self.assertIn("failed: exit code 3", result.warning or "")

    def test_nano_is_terminal_editor(self) -> None:
        with patch("ai_dev_flow.editor_opening.subprocess.Popen", return_value=_FakeProcess(returncode=0)) as popen_mock:
            result = SubprocessEditorOpener(["nano"]).open_path(Path("/tmp/a"))

        self.assertTrue(result.opened)
        self.assertNotIn("stdin", popen_mock.call_args.kwargs)

    def test_notepad_remains_nonblocking_graphical(self) -> None:
        fake_process = _FakeProcess(returncode=0)

        with patch("ai_dev_flow.editor_opening.subprocess.Popen", return_value=fake_process) as popen_mock:
            result = SubprocessEditorOpener(["notepad"]).open_path(Path("/tmp/a"))

        self.assertTrue(result.opened)
        self.assertEqual(fake_process.wait_calls, [])
        self.assertEqual(popen_mock.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(popen_mock.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(popen_mock.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertNotIn("text", popen_mock.call_args.kwargs)

    def test_code_wait_keeps_bounded_wait_semantics(self) -> None:
        wait_process = _FakeProcess(returncode=0)

        with patch("ai_dev_flow.editor_opening.subprocess.Popen", return_value=wait_process):
            result = SubprocessEditorOpener(["code", "--wait"], wait_timeout_seconds=2.0).open_path(Path("/tmp/a"))

        self.assertTrue(result.opened)
        self.assertEqual(wait_process.wait_calls, [2.0])

    def test_code_wait_timeout_is_successful_launch(self) -> None:
        wait_process = _FakeProcess(returncode=0, timeout=True)

        with patch("ai_dev_flow.editor_opening.subprocess.Popen", return_value=wait_process):
            result = SubprocessEditorOpener(["code", "--wait"], wait_timeout_seconds=2.0).open_path(Path("/tmp/a"))

        self.assertTrue(result.opened)

    def test_target_path_appended_as_separate_argument_and_no_shell(self) -> None:
        popen_calls: list[tuple[list[str], dict[str, object]]] = []

        def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
            popen_calls.append((command, kwargs))
            return _FakeProcess(returncode=0)

        target_path = Path("/tmp/with space/report.md")
        with patch("ai_dev_flow.editor_opening.subprocess.Popen", side_effect=fake_popen):
            result = SubprocessEditorOpener(["code", "--wait"], wait_timeout_seconds=1.5).open_path(target_path)

        self.assertTrue(result.opened)
        called_command, called_kwargs = popen_calls[0]
        self.assertEqual(called_command[-1], str(target_path))
        self.assertNotIn(str(target_path), called_command[:-1])
        self.assertIn("--wait", called_command)
        shell_value = called_kwargs.get("shell")
        self.assertIn(shell_value, (None, False))
        self.assertEqual(called_kwargs["stderr"], subprocess.DEVNULL)

    def test_graphical_wait_nonzero_exit_reports_exit_code(self) -> None:
        wait_process = _FakeProcess(returncode=7)

        with patch("ai_dev_flow.editor_opening.subprocess.Popen", return_value=wait_process):
            result = SubprocessEditorOpener(["code", "--wait"], wait_timeout_seconds=2.0).open_path(Path("/tmp/a"))

        self.assertFalse(result.opened)
        self.assertIn("failed: exit code 7", result.warning or "")

    def test_launch_failure_returns_fallback_signal(self) -> None:
        with patch("ai_dev_flow.editor_opening.subprocess.Popen", side_effect=OSError("boom")):
            result = SubprocessEditorOpener(["code"]).open_path(Path("/tmp/a"))

        self.assertFalse(result.opened)
        self.assertIn("Failed to launch editor command", result.warning or "")


if __name__ == "__main__":
    unittest.main()

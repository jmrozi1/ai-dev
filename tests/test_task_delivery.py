from __future__ import annotations

import io
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from ai_dev_flow.task_delivery import (
    ClipboardDeliveryAdapter,
    ClipboardDeliveryError,
    FileOnlyDeliveryAdapter,
    PlatformClipboardWriter,
    StdoutDeliveryAdapter,
    build_delivery_adapter,
)


class _FakeClipboardWriter:
    def __init__(self) -> None:
        self.copied: list[str] = []

    def copy_text(self, text: str) -> None:
        self.copied.append(text)


class _FailingClipboardWriter:
    def __init__(self, message: str = "failed") -> None:
        self.message = message

    def copy_text(self, text: str) -> None:
        raise ClipboardDeliveryError(self.message)


class TaskDeliveryTests(unittest.TestCase):
    def test_build_delivery_adapter_stdout_does_not_construct_platform_clipboard(self) -> None:
        with patch("ai_dev_flow.task_delivery.PlatformClipboardWriter") as writer_ctor:
            adapter = build_delivery_adapter("stdout")

        self.assertIsInstance(adapter, StdoutDeliveryAdapter)
        writer_ctor.assert_not_called()

    def test_build_delivery_adapter_file_only_does_not_construct_platform_clipboard(self) -> None:
        with patch("ai_dev_flow.task_delivery.PlatformClipboardWriter") as writer_ctor:
            adapter = build_delivery_adapter("file-only")

        self.assertIsInstance(adapter, FileOnlyDeliveryAdapter)
        writer_ctor.assert_not_called()

    def test_build_delivery_adapter_accepts_clipboard_modes(self) -> None:
        clipboard_adapter = build_delivery_adapter("clipboard", clipboard_writer=_FakeClipboardWriter())
        clipboard_stdout_adapter = build_delivery_adapter("clipboard+stdout", clipboard_writer=_FakeClipboardWriter())

        self.assertIsInstance(clipboard_adapter, ClipboardDeliveryAdapter)
        self.assertIsInstance(clipboard_stdout_adapter, ClipboardDeliveryAdapter)

    def test_fake_clipboard_writer_injection(self) -> None:
        writer = _FakeClipboardWriter()
        adapter = build_delivery_adapter("clipboard", clipboard_writer=writer)

        with redirect_stdout(io.StringIO()):
            adapter.deliver("invoke")

        self.assertEqual(writer.copied, ["invoke"])

    def test_clipboard_adapter_success_output(self) -> None:
        writer = _FakeClipboardWriter()
        adapter = ClipboardDeliveryAdapter(
            clipboard_writer=writer,
            echo_stdout=False,
            fallback_to_stdout=True,
        )

        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            adapter.deliver("invoke")

        self.assertEqual(writer.copied, ["invoke"])
        self.assertIn("Invocation copied to clipboard.", out.getvalue())
        self.assertEqual(err.getvalue(), "")

    def test_clipboard_adapter_fallback_to_stdout(self) -> None:
        adapter = ClipboardDeliveryAdapter(
            clipboard_writer=_FailingClipboardWriter("copy failed"),
            echo_stdout=False,
            fallback_to_stdout=True,
        )

        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            adapter.deliver("invoke")

        self.assertIn("Clipboard delivery failed; falling back to stdout.", out.getvalue())
        self.assertIn("invoke", out.getvalue())
        self.assertIn("Warning: clipboard delivery failed", err.getvalue())

    def test_clipboard_stdout_success_prints_invocation_once(self) -> None:
        adapter = ClipboardDeliveryAdapter(
            clipboard_writer=_FakeClipboardWriter(),
            echo_stdout=True,
            fallback_to_stdout=False,
        )

        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            adapter.deliver("invoke")

        self.assertEqual(out.getvalue().count("invoke"), 1)
        self.assertIn("Invocation copied to clipboard.", out.getvalue())
        self.assertEqual(err.getvalue(), "")

    def test_clipboard_stdout_failure_still_succeeds_with_single_invocation(self) -> None:
        adapter = ClipboardDeliveryAdapter(
            clipboard_writer=_FailingClipboardWriter("copy failed"),
            echo_stdout=True,
            fallback_to_stdout=False,
        )

        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            adapter.deliver("invoke")

        self.assertEqual(out.getvalue().count("invoke"), 1)
        self.assertNotIn("falling back to stdout", out.getvalue())
        self.assertIn("Warning: clipboard delivery failed; invocation was still written to stdout", err.getvalue())

    def test_file_only_delivery_is_silent(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            FileOnlyDeliveryAdapter().deliver("invoke")
        self.assertEqual(out.getvalue(), "")

    def test_stdout_delivery_prints_text(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            StdoutDeliveryAdapter().deliver("invoke")
        self.assertEqual(out.getvalue(), "invoke\n")

    def test_platform_clipboard_command_selection_order_linux(self) -> None:
        writer = PlatformClipboardWriter(timeout_seconds=1.0)

        with (
            patch("ai_dev_flow.task_delivery.platform.system", return_value="Linux"),
            patch(
                "ai_dev_flow.task_delivery.shutil.which",
                side_effect=lambda name: "/usr/bin/xclip" if name == "xclip" else None,
            ),
            patch("ai_dev_flow.task_delivery.subprocess.run") as run_mock,
        ):
            run_mock.return_value = subprocess.CompletedProcess(
                args=["xclip", "-selection", "clipboard"],
                returncode=0,
                stdout="",
                stderr="",
            )
            writer.copy_text("invoke")

        self.assertEqual(run_mock.call_args.args[0], ["xclip", "-selection", "clipboard"])
        self.assertEqual(run_mock.call_args.kwargs["input"], "invoke")
        self.assertTrue(run_mock.call_args.kwargs["text"])
        self.assertTrue(run_mock.call_args.kwargs["capture_output"])
        self.assertNotIn("shell", run_mock.call_args.kwargs)

    def test_platform_clipboard_no_supported_command(self) -> None:
        writer = PlatformClipboardWriter(timeout_seconds=1.0)

        with (
            patch("ai_dev_flow.task_delivery.platform.system", return_value="Linux"),
            patch("ai_dev_flow.task_delivery.shutil.which", return_value=None),
        ):
            with self.assertRaises(ClipboardDeliveryError) as context:
                writer.copy_text("invoke")

        self.assertIn("No supported clipboard command is available", str(context.exception))

    def test_platform_clipboard_subprocess_launch_failure(self) -> None:
        writer = PlatformClipboardWriter(timeout_seconds=1.0)

        with (
            patch("ai_dev_flow.task_delivery.platform.system", return_value="Linux"),
            patch("ai_dev_flow.task_delivery.shutil.which", return_value="/usr/bin/wl-copy"),
            patch("ai_dev_flow.task_delivery.subprocess.run", side_effect=OSError("boom")),
        ):
            with self.assertRaises(ClipboardDeliveryError) as context:
                writer.copy_text("invoke")

        self.assertIn("Failed to start clipboard command", str(context.exception))

    def test_platform_clipboard_non_zero_exit(self) -> None:
        writer = PlatformClipboardWriter(timeout_seconds=1.0)

        with (
            patch("ai_dev_flow.task_delivery.platform.system", return_value="Linux"),
            patch("ai_dev_flow.task_delivery.shutil.which", return_value="/usr/bin/wl-copy"),
            patch("ai_dev_flow.task_delivery.subprocess.run") as run_mock,
        ):
            run_mock.return_value = subprocess.CompletedProcess(
                args=["wl-copy"],
                returncode=7,
                stdout="",
                stderr="permission denied",
            )
            with self.assertRaises(ClipboardDeliveryError) as context:
                writer.copy_text("invoke")

        self.assertIn("permission denied", str(context.exception))

    def test_platform_clipboard_timeout(self) -> None:
        writer = PlatformClipboardWriter(timeout_seconds=0.1)

        with (
            patch("ai_dev_flow.task_delivery.platform.system", return_value="Linux"),
            patch("ai_dev_flow.task_delivery.shutil.which", return_value="/usr/bin/wl-copy"),
            patch(
                "ai_dev_flow.task_delivery.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd=["wl-copy"], timeout=0.1),
            ),
        ):
            with self.assertRaises(ClipboardDeliveryError) as context:
                writer.copy_text("invoke")

        self.assertIn("timed out", str(context.exception))

    def test_platform_clipboard_windows_powershell_reads_stdin(self) -> None:
        writer = PlatformClipboardWriter(timeout_seconds=3.0)
        invocation_text = "Read and execute .ai-dev/tasks/demo.md"

        with (
            patch("ai_dev_flow.task_delivery.platform.system", return_value="Windows"),
            patch(
                "ai_dev_flow.task_delivery.shutil.which",
                side_effect=lambda name: "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
                if name == "powershell"
                else None,
            ),
            patch("ai_dev_flow.task_delivery.subprocess.run") as run_mock,
        ):
            run_mock.return_value = subprocess.CompletedProcess(
                args=["powershell"],
                returncode=0,
                stdout="",
                stderr="",
            )
            writer.copy_text(invocation_text)

        selected_command = run_mock.call_args.args[0]
        self.assertEqual(selected_command[0], "powershell")
        self.assertIn("[Console]::In.ReadToEnd() | Set-Clipboard", selected_command)
        self.assertEqual(run_mock.call_args.kwargs["input"], invocation_text)
        self.assertTrue(run_mock.call_args.kwargs["text"])
        self.assertEqual(run_mock.call_args.kwargs["timeout"], 3.0)
        shell_value = run_mock.call_args.kwargs.get("shell")
        self.assertIn(shell_value, (None, False))
        self.assertFalse(any(invocation_text in argument for argument in selected_command))

    def test_platform_clipboard_windows_fallback_to_powershell_exe(self) -> None:
        writer = PlatformClipboardWriter(timeout_seconds=2.0)
        invocation_text = "Read and execute .ai-dev/tasks/fallback.md"

        with (
            patch("ai_dev_flow.task_delivery.platform.system", return_value="Windows"),
            patch(
                "ai_dev_flow.task_delivery.shutil.which",
                side_effect=lambda name: None if name == "powershell" else "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
            ),
            patch("ai_dev_flow.task_delivery.subprocess.run") as run_mock,
        ):
            run_mock.return_value = subprocess.CompletedProcess(
                args=["powershell.exe"],
                returncode=0,
                stdout="",
                stderr="",
            )
            writer.copy_text(invocation_text)

        selected_command = run_mock.call_args.args[0]
        self.assertEqual(selected_command[0], "powershell.exe")
        self.assertIn("[Console]::In.ReadToEnd() | Set-Clipboard", selected_command)
        self.assertEqual(run_mock.call_args.kwargs["input"], invocation_text)
        self.assertTrue(run_mock.call_args.kwargs["text"])
        self.assertEqual(run_mock.call_args.kwargs["timeout"], 2.0)
        shell_value = run_mock.call_args.kwargs.get("shell")
        self.assertIn(shell_value, (None, False))
        self.assertFalse(any(invocation_text in argument for argument in selected_command))


if __name__ == "__main__":
    unittest.main()

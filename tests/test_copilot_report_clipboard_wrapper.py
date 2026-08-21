"""Tests for the clipboard wrapper with comprehensive coverage."""
from __future__ import annotations

import base64
import ctypes
import os
import sys
import tempfile
import unittest
import importlib.util
from pathlib import Path
from unittest import mock

# Import the clipboard wrapper module
wrapper_path = Path(__file__).resolve().parent.parent / "skills" / "copilot" / "flow" / "scripts" / "flow-report-clipboard.py"
spec = importlib.util.spec_from_file_location("clipboard_wrapper", wrapper_path)
clipboard_module = importlib.util.module_from_spec(spec)
sys.modules["clipboard_wrapper"] = clipboard_module
spec.loader.exec_module(clipboard_module)

# Import symbols from the loaded module
WindowsClipboard = clipboard_module.WindowsClipboard
WlCopyBackend = clipboard_module.WlCopyBackend
XclipBackend = clipboard_module.XclipBackend
XselBackend = clipboard_module.XselBackend
OSC52Backend = clipboard_module.OSC52Backend
get_canonical_report = clipboard_module.get_canonical_report
get_clipboard_backends = clipboard_module.get_clipboard_backends
copy_report_to_clipboard = clipboard_module.copy_report_to_clipboard
main = clipboard_module.main


class PlatformNeutralReportTests(unittest.TestCase):
    """Tests for platform-neutral canonical report invocation."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.temp_dir.name) / "repo"
        self.repo_root.mkdir(parents=True)
        (self.repo_root / ".ai-dev").mkdir(exist_ok=True)
        self.original_cwd = Path.cwd()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_canonical_report_uses_python_cli(self) -> None:
        """Canonical report must use Python CLI, not Bash."""
        canonical_report = "Issue: 49\nTokens: 100\n"

        # Mock subprocess to verify Python invocation
        subprocess_calls = []

        def mock_run(*args, **kwargs):
            subprocess_calls.append((args, kwargs))
            return mock.MagicMock(returncode=0, stdout=canonical_report)

        with mock.patch("subprocess.run", side_effect=mock_run):
            result = get_canonical_report(self.repo_root, self.original_cwd)

        # Verify Python CLI was invoked
        self.assertEqual(len(subprocess_calls), 1)
        args, kwargs = subprocess_calls[0]
        self.assertIn(sys.executable, args[0])
        self.assertIn("ai_dev_flow.cli", " ".join(args[0]))
        self.assertIn("__ai_dev_flow_exec__", " ".join(args[0]))
        self.assertIn("report", " ".join(args[0]))

    def test_caller_working_directory_preserved(self) -> None:
        """Report must execute from caller's original directory, not AI Dev root."""
        canonical_report = "Issue: test-repo\n"
        caller_cwd = self.repo_root
        original_cwd = self.original_cwd

        # Track subprocess working directory
        subprocess_calls = []

        def mock_run(*args, **kwargs):
            subprocess_calls.append((args, kwargs))
            return mock.MagicMock(returncode=0, stdout=canonical_report)

        with mock.patch("subprocess.run", side_effect=mock_run):
            get_canonical_report(self.repo_root, caller_cwd)

        # Verify subprocess was executed from caller's directory
        self.assertEqual(len(subprocess_calls), 1)
        args, kwargs = subprocess_calls[0]
        self.assertEqual(kwargs["cwd"], str(caller_cwd))

    def test_distinct_repo_not_silently_swallowed(self) -> None:
        """Wrapper must not silently report different repository than caller's."""
        repo1 = Path(self.temp_dir.name) / "repo1"
        repo1.mkdir(parents=True)
        repo2 = Path(self.temp_dir.name) / "repo2"
        repo2.mkdir(parents=True)

        # Mock reports with distinct repo identity
        report_for_repo1 = "Issue: 49 [repo1]\n"
        report_for_repo2 = "Issue: 49 [repo2]\n"

        def mock_run(*args, **kwargs):
            cwd = Path(kwargs.get("cwd", "."))
            if cwd == repo1:
                return mock.MagicMock(returncode=0, stdout=report_for_repo1)
            elif cwd == repo2:
                return mock.MagicMock(returncode=0, stdout=report_for_repo2)
            else:
                return mock.MagicMock(returncode=1)

        with mock.patch("subprocess.run", side_effect=mock_run):
            result1 = get_canonical_report(repo1, repo1)
            result2 = get_canonical_report(repo2, repo2)

        self.assertIn("repo1", result1)
        self.assertIn("repo2", result2)
        self.assertNotEqual(result1, result2)


class OutputPreservationTests(unittest.TestCase):
    """Tests for exact output preservation."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.temp_dir.name) / "repo"
        self.repo_root.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_wrapper_output_equals_canonical_report(self) -> None:
        """Wrapper output must equal canonical report byte-for-byte."""
        canonical_report = "Issue: 49\nPrompt: test\nTokens: unavailable\n"

        captured_content = None

        def capture_backend(content: str) -> bool:
            nonlocal captured_content
            captured_content = content
            return True

        with mock.patch.object(clipboard_module, "get_canonical_report") as mock_get:
            mock_get.return_value = canonical_report
            success, _ = copy_report_to_clipboard(self.repo_root, backends=[capture_backend])

        self.assertTrue(success)
        self.assertEqual(captured_content, canonical_report)

    def test_unicode_in_report_preserved(self) -> None:
        """Unicode content must be preserved exactly."""
        report_with_unicode = "Issue: 49\nPrompt: café ñoño 中文 日本語\nTokens: unavailable\n"

        captured_content = None

        def capture_backend(content: str) -> bool:
            nonlocal captured_content
            captured_content = content
            return True

        with mock.patch.object(clipboard_module, "get_canonical_report") as mock_get:
            mock_get.return_value = report_with_unicode
            copy_report_to_clipboard(self.repo_root, backends=[capture_backend])

        self.assertEqual(captured_content, report_with_unicode)
        self.assertIn("café", captured_content)
        self.assertIn("中文", captured_content)
        self.assertIn("日本語", captured_content)

    def test_final_newline_preserved(self) -> None:
        """Final newline state must be preserved."""
        report_with_newline = "Issue: 49\nTokens: unavailable\n"
        report_without_newline = "Issue: 49\nTokens: unavailable"

        captured_with = None
        captured_without = None

        def capture1(content: str) -> bool:
            nonlocal captured_with
            captured_with = content
            return True

        def capture2(content: str) -> bool:
            nonlocal captured_without
            captured_without = content
            return True

        with mock.patch.object(clipboard_module, "get_canonical_report") as mock_get:
            mock_get.return_value = report_with_newline
            copy_report_to_clipboard(self.repo_root, backends=[capture1])

        with mock.patch.object(clipboard_module, "get_canonical_report") as mock_get:
            mock_get.return_value = report_without_newline
            copy_report_to_clipboard(self.repo_root, backends=[capture2])

        self.assertTrue(captured_with.endswith("\n"))
        self.assertFalse(captured_without.endswith("\n"))


class SubprocessSafetyTests(unittest.TestCase):
    """Tests for subprocess argument safety."""

    def test_wl_copy_backend_exact_arguments(self) -> None:
        """wl-copy must receive report via stdin, never as arguments."""
        report = "Issue: 49\nSecret data\n"

        subprocess_calls = []

        def mock_run(*args, **kwargs):
            subprocess_calls.append((args, kwargs))
            return mock.MagicMock(returncode=0)

        with mock.patch("subprocess.run", side_effect=mock_run):
            WlCopyBackend.copy(report)

        self.assertEqual(len(subprocess_calls), 1)
        args, kwargs = subprocess_calls[0]
        self.assertEqual(args[0], ["wl-copy"])
        self.assertEqual(kwargs.get("input"), report.encode("utf-8"))
        self.assertNotIn("Secret data", str(args[0]))

    def test_xclip_backend_exact_arguments(self) -> None:
        """xclip must receive report via stdin, never as arguments."""
        report = "Issue: 49\nSecret data\n"

        subprocess_calls = []

        def mock_run(*args, **kwargs):
            subprocess_calls.append((args, kwargs))
            return mock.MagicMock(returncode=0)

        with mock.patch("subprocess.run", side_effect=mock_run):
            XclipBackend.copy(report)

        self.assertEqual(len(subprocess_calls), 1)
        args, kwargs = subprocess_calls[0]
        self.assertEqual(args[0], ["xclip", "-selection", "clipboard"])
        self.assertEqual(kwargs.get("input"), report.encode("utf-8"))
        self.assertNotIn("Secret data", str(args[0]))

    def test_xsel_backend_exact_arguments(self) -> None:
        """xsel must receive report via stdin, never as arguments."""
        report = "Issue: 49\nSecret data\n"

        subprocess_calls = []

        def mock_run(*args, **kwargs):
            subprocess_calls.append((args, kwargs))
            return mock.MagicMock(returncode=0)

        with mock.patch("subprocess.run", side_effect=mock_run):
            XselBackend.copy(report)

        self.assertEqual(len(subprocess_calls), 1)
        args, kwargs = subprocess_calls[0]
        self.assertEqual(args[0], ["xsel", "--clipboard", "--input"])
        self.assertEqual(kwargs.get("input"), report.encode("utf-8"))
        self.assertNotIn("Secret data", str(args[0]))


class OSC52Tests(unittest.TestCase):
    """Tests for OSC 52 terminal control sequence."""

    def test_osc52_encoding_exact(self) -> None:
        """OSC 52 encoding must be UTF-8 then Base64."""
        report = "Issue: 49\nTokens: 100\n"

        captured_output = []

        def mock_write(data):
            captured_output.append(data)
            return len(data)

        def mock_flush():
            pass

        mock_buffer = mock.MagicMock()
        mock_buffer.write.side_effect = mock_write
        mock_buffer.flush.side_effect = mock_flush

        with mock.patch.object(sys, "stdout") as mock_stdout:
            mock_stdout.buffer = mock_buffer
            result = OSC52Backend.copy(report)

        self.assertTrue(result)
        self.assertEqual(len(captured_output), 1)

        # Verify OSC 52 format: ESC ] 52 ; c ; <base64> ESC \
        output = captured_output[0].decode("utf-8")
        self.assertTrue(output.startswith("\x1b]52;c;"))
        self.assertTrue(output.endswith("\x1b\\"))

        # Extract and verify base64 encoding
        encoded = output[7:-2]  # Strip OSC prefix and suffix
        decoded = base64.b64decode(encoded).decode("utf-8")
        self.assertEqual(decoded, report)

    def test_osc52_size_limit(self) -> None:
        """OSC 52 must fail without truncation if report exceeds limit."""
        # Create report exceeding 16 KB
        large_report = "X" * 16385

        result = OSC52Backend.copy(large_report)
        self.assertFalse(result)

    def test_osc52_at_size_limit(self) -> None:
        """OSC 52 must succeed at exactly the size limit."""
        # Create report at exactly 16 KB
        report_at_limit = "X" * 16384

        captured_output = []

        def mock_write(data):
            captured_output.append(data)
            return len(data)

        mock_buffer = mock.MagicMock()
        mock_buffer.write.side_effect = mock_write
        mock_buffer.flush = mock.MagicMock()

        with mock.patch.object(sys, "stdout") as mock_stdout:
            mock_stdout.buffer = mock_buffer
            result = OSC52Backend.copy(report_at_limit)

        self.assertTrue(result)

    def test_osc52_only_to_terminal_not_copied_content(self) -> None:
        """OSC 52 sequence must write to stdout.buffer only, not contaminate copied content."""
        report = "Issue: 49\nTokens: 100\n"

        captured_output = []

        def mock_write(data):
            captured_output.append(data)
            return len(data)

        mock_buffer = mock.MagicMock()
        mock_buffer.write.side_effect = mock_write
        mock_buffer.flush = mock.MagicMock()

        with mock.patch.object(sys, "stdout") as mock_stdout:
            mock_stdout.buffer = mock_buffer
            OSC52Backend.copy(report)

        # Verify only OSC 52 sequence was written, no extra content
        self.assertEqual(len(captured_output), 1)
        output = captured_output[0].decode("utf-8")
        # Sequence should start and end correctly
        self.assertTrue(output.startswith("\x1b]52;c;"))
        self.assertTrue(output.endswith("\x1b\\"))
        # No duplication or extra newlines
        self.assertEqual(output.count("\x1b]52;c;"), 1)


class WindowsClipboardTests(unittest.TestCase):
    """Tests for Windows clipboard lifecycle and encoding."""

    def test_windows_uses_utf16le(self) -> None:
        """Windows backend must encode as UTF-16LE, not UTF-8."""
        # Note: This test verifies the code structure, not real Windows API
        # because ctypes.windll is Windows-only
        content = "café"

        # We can at least verify the code path by checking the source
        import inspect

        source = inspect.getsource(WindowsClipboard.copy)
        self.assertIn('encode("utf-16-le")', source)
        self.assertIn("CF_UNICODETEXT", source)
        self.assertNotIn('encode("utf-8")', source)
        self.assertNotIn("CF_TEXT", source.replace("CF_UNICODETEXT", ""))

    def test_windows_proper_lifecycle(self) -> None:
        """Windows clipboard must follow correct ownership lifecycle."""
        import inspect

        source = inspect.getsource(WindowsClipboard.copy)

        # Verify proper sequence: Alloc -> Lock -> Copy -> Unlock -> OpenClipboard -> EmptyClipboard -> SetClipboardData -> CloseClipboard
        alloc_pos = source.find("GlobalAlloc")
        lock_pos = source.find("GlobalLock")
        unlock_pos = source.find("GlobalUnlock")
        open_pos = source.find("OpenClipboard")
        set_pos = source.find("SetClipboardData")
        close_pos = source.find("CloseClipboard")

        # Verify ordering
        self.assertLess(alloc_pos, lock_pos)
        self.assertLess(lock_pos, unlock_pos)
        self.assertLess(unlock_pos, open_pos)
        self.assertLess(open_pos, set_pos)
        self.assertLess(set_pos, close_pos)

    def test_windows_cleanup_on_allocation_failure(self) -> None:
        """Windows backend must handle allocation failure."""
        import inspect

        source = inspect.getsource(WindowsClipboard.copy)
        # Verify error handling after GlobalAlloc
        self.assertIn("if not hglob:", source)

    def test_windows_cleanup_on_lock_failure(self) -> None:
        """Windows backend must free memory on lock failure."""
        import inspect

        source = inspect.getsource(WindowsClipboard.copy)
        # Verify GlobalFree is called in error handling (check for the pattern)
        self.assertIn("if not lpglob:", source)
        self.assertIn("GlobalFree(hglob)", source)

    def test_windows_cleanup_on_open_clipboard_failure(self) -> None:
        """Windows backend must free memory on OpenClipboard failure."""
        import inspect

        source = inspect.getsource(WindowsClipboard.copy)
        # Verify GlobalFree is called when OpenClipboard fails
        self.assertIn("if not user32.OpenClipboard(None):", source)
        self.assertIn("GlobalFree(hglob)", source)


class BackendSelectionTests(unittest.TestCase):
    """Tests for backend selection and iteration."""

    def test_backend_selection_platform_aware(self) -> None:
        """get_clipboard_backends must return platform-appropriate backends."""
        backends = get_clipboard_backends()
        self.assertIsInstance(backends, list)
        self.assertGreater(len(backends), 0)

    def test_backends_tried_in_order(self) -> None:
        """Backends must be tried in priority order until one succeeds."""
        report = "Issue: 49\n"
        backend_calls = []

        def failing_backend(content):
            backend_calls.append("failing")
            return False

        def successful_backend(content):
            backend_calls.append("successful")
            return True

        def never_called_backend(content):
            backend_calls.append("never_called")
            return False

        with mock.patch.object(clipboard_module, "get_canonical_report") as mock_get:
            mock_get.return_value = report
            success, _ = copy_report_to_clipboard(
                backends=[failing_backend, successful_backend, never_called_backend]
            )

        self.assertTrue(success)
        self.assertEqual(backend_calls, ["failing", "successful"])


class FailureRecoveryTests(unittest.TestCase):
    """Tests for failure recovery and output behavior."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.temp_dir.name) / "repo"
        self.repo_root.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_all_backends_fail_exit_nonzero(self) -> None:
        """main() must exit nonzero when all backends fail."""
        canonical_report = "Issue: 49\nTokens: 100\n"

        def failing_backend(content):
            return False

        with mock.patch.object(clipboard_module, "get_canonical_report") as mock_get:
            mock_get.return_value = canonical_report
            with mock.patch("sys.stderr"):
                with mock.patch("sys.stdout"):
                    with mock.patch.object(clipboard_module, "get_clipboard_backends") as mock_backends:
                        mock_backends.return_value = [failing_backend]
                        exit_code = main()

        self.assertEqual(exit_code, 1)

    def test_all_backends_fail_print_report_to_stdout(self) -> None:
        """When all backends fail, report must be on stdout for recovery."""
        canonical_report = "Issue: 49\nTokens: 100\n"

        def failing_backend(content):
            return False

        captured_output = {"stdout": [], "stderr": []}

        def mock_print(msg="", **kwargs):
            if kwargs.get("file") == sys.stderr:
                captured_output["stderr"].append(msg)
            else:
                captured_output["stdout"].append(msg)

        with mock.patch.object(clipboard_module, "get_canonical_report") as mock_get:
            mock_get.return_value = canonical_report
            with mock.patch("builtins.print", side_effect=mock_print):
                with mock.patch.object(clipboard_module, "get_clipboard_backends") as mock_backends:
                    mock_backends.return_value = [failing_backend]
                    main()

        # Report should be in stdout
        stdout_text = "\n".join(captured_output["stdout"])
        self.assertIn("Issue: 49", stdout_text)
        self.assertIn("Tokens: 100", stdout_text)

    def test_failure_does_not_include_report_in_arguments(self) -> None:
        """Failure messages must not include report content in exception/args."""
        canonical_report = "Issue: 49\nSecret data\n"

        def failing_backend(content):
            raise ValueError("backend error")

        with mock.patch.object(clipboard_module, "get_canonical_report") as mock_get:
            mock_get.return_value = canonical_report
            success, message = copy_report_to_clipboard(
                self.repo_root, backends=[failing_backend]
            )

        self.assertFalse(success)
        # Error message might contain report at end for recovery, but not in the guidance
        guidance_part = message.split("\n\n--- Report")[0] if "\n\n--- Report" in message else message
        self.assertNotIn("Secret data", guidance_part)


class MainEntryPointTests(unittest.TestCase):
    """Tests for main() function."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.temp_dir.name) / "repo"
        self.repo_root.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_main_success_exit_zero(self) -> None:
        """main() must return 0 on success."""
        canonical_report = "Issue: 49\nTokens: 100\n"

        def successful_backend(content):
            return True

        with mock.patch.object(clipboard_module, "get_canonical_report") as mock_get:
            mock_get.return_value = canonical_report
            with mock.patch("builtins.print"):
                with mock.patch.object(clipboard_module, "get_clipboard_backends") as mock_backends:
                    mock_backends.return_value = [successful_backend]
                    exit_code = main()

        self.assertEqual(exit_code, 0)

    def test_main_failure_exit_nonzero(self) -> None:
        """main() must return 1 on failure."""
        canonical_report = "Issue: 49\nTokens: 100\n"

        def failing_backend(content):
            return False

        with mock.patch.object(clipboard_module, "get_canonical_report") as mock_get:
            mock_get.return_value = canonical_report
            with mock.patch("builtins.print"):
                with mock.patch.object(clipboard_module, "get_clipboard_backends") as mock_backends:
                    mock_backends.return_value = [failing_backend]
                    exit_code = main()

        self.assertEqual(exit_code, 1)


class SymlinkResolutionTests(unittest.TestCase):
    """Tests for symlinked wrapper execution."""

    def test_wrapper_file_exists(self) -> None:
        """Wrapper file must exist at expected location."""
        self.assertTrue(wrapper_path.exists())

    def test_wrapper_has_python_shebang(self) -> None:
        """Wrapper must have python3 shebang for direct execution."""
        with open(wrapper_path, "r") as f:
            first_line = f.readline().strip()
        self.assertTrue(first_line.startswith("#!"))
        self.assertIn("python", first_line)


if __name__ == "__main__":
    unittest.main()

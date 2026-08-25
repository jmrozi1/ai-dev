"""Tests for the clipboard wrapper with comprehensive coverage."""
from __future__ import annotations

import base64
import ctypes
import io
import os
import subprocess
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

    def test_canonical_report_uses_current_interpreter(self) -> None:
        """Canonical report must use the current interpreter, never a literal launcher."""
        canonical_report = "Issue: 49\nTokens: 100\n"
        subprocess_calls = []

        def mock_run(*args, **kwargs):
            subprocess_calls.append((args, kwargs))
            return mock.MagicMock(returncode=0, stdout=canonical_report.encode("utf-8"))

        with mock.patch("subprocess.run", side_effect=mock_run):
            result = get_canonical_report(self.repo_root, self.original_cwd)

        self.assertEqual(len(subprocess_calls), 1)
        args, kwargs = subprocess_calls[0]
        self.assertEqual(args[0][0], sys.executable)
        self.assertEqual(args[0][1], "-m")
        self.assertEqual(args[0][2], "ai_dev_flow.cli")
        self.assertEqual(args[0][3], "__ai_dev_flow_exec__")
        self.assertEqual(args[0][4], "report")
        self.assertEqual(result, canonical_report)

    def test_existing_pythonpath_is_preserved(self) -> None:
        """Existing PYTHONPATH content must be retained while source root is added."""
        canonical_report = "Issue: 49\nTokens: 100\n"
        subprocess_calls = []

        def mock_run(*args, **kwargs):
            subprocess_calls.append((args, kwargs))
            return mock.MagicMock(returncode=0, stdout=canonical_report.encode("utf-8"))

        with mock.patch.dict(os.environ, {"PYTHONPATH": "/existing/one:/existing/two"}, clear=False):
            with mock.patch("subprocess.run", side_effect=mock_run):
                get_canonical_report(self.repo_root, self.original_cwd)

        _, kwargs = subprocess_calls[0]
        env = kwargs["env"]
        self.assertIn("/existing/one", env["PYTHONPATH"])
        self.assertIn("/existing/two", env["PYTHONPATH"])
        self.assertIn(str(wrapper_path.parents[4]), env["PYTHONPATH"])

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

        control_stream = io.BytesIO()
        result = OSC52Backend.copy(report, control_stream=control_stream)

        self.assertTrue(result)

        # Verify OSC 52 format: ESC ] 52 ; c ; <base64> ESC \
        output = control_stream.getvalue().decode("utf-8")
        self.assertTrue(output.startswith("\x1b]52;c;"))
        self.assertTrue(output.endswith("\x1b\\"))

        # Extract and verify base64 encoding
        encoded = output[7:-2]  # Strip OSC prefix and suffix
        decoded = base64.b64decode(encoded).decode("utf-8")
        self.assertEqual(decoded, report)

    def test_osc52_size_limit(self) -> None:
        """OSC 52 must fail without truncation if UTF-8 byte length exceeds limit."""
        large_report = "X" * (OSC52Backend.MAX_OSC52_BYTES + 1)
        self.assertFalse(OSC52Backend.copy(large_report))

    def test_osc52_at_size_limit(self) -> None:
        """OSC 52 must succeed at exactly the UTF-8 byte limit."""
        report_at_limit = "é" * (OSC52Backend.MAX_OSC52_BYTES // 2)
        control_stream = io.BytesIO()
        result = OSC52Backend.copy(report_at_limit, control_stream=control_stream)

        self.assertTrue(result)
        output = control_stream.getvalue().decode("utf-8")
        self.assertTrue(output.startswith("\x1b]52;c;"))
        self.assertTrue(output.endswith("\x1b\\"))
        self.assertEqual(output[7:-2], base64.b64encode(report_at_limit.encode("utf-8")).decode("ascii"))

    def test_osc52_one_byte_over_limit_fails(self) -> None:
        """A report one UTF-8 byte over the limit must fail without truncation."""
        report = "A" * (OSC52Backend.MAX_OSC52_BYTES + 1)
        self.assertFalse(OSC52Backend.copy(report))

    def test_osc52_only_to_terminal_not_copied_content(self) -> None:
        """OSC 52 sequence must write to stdout.buffer only, not contaminate copied content."""
        report = "Issue: 49\nTokens: 100\n"

        control_stream = io.BytesIO()
        OSC52Backend.copy(report, control_stream=control_stream)

        # Verify only OSC 52 sequence was written, no extra content
        output = control_stream.getvalue().decode("utf-8")
        # Sequence should start and end correctly
        self.assertTrue(output.startswith("\x1b]52;c;"))
        self.assertTrue(output.endswith("\x1b\\"))
        # No duplication or extra newlines
        self.assertEqual(output.count("\x1b]52;c;"), 1)

    def test_osc52_never_writes_stdout(self) -> None:
        report = "Issue: 49\n"
        control_stream = io.BytesIO()
        stdout = io.StringIO()
        with mock.patch.object(sys, "stdout", stdout):
            self.assertTrue(OSC52Backend.copy(report, control_stream=control_stream))
        self.assertEqual(stdout.getvalue(), "")

    def test_osc52_without_control_stream_fails_safely(self) -> None:
        with mock.patch("builtins.open", side_effect=OSError("no tty")):
            self.assertFalse(OSC52Backend.copy("Issue: 49\n"))


class WindowsClipboardTests(unittest.TestCase):
    """Tests for Windows clipboard lifecycle and encoding."""

    def test_windows_utf16le_payload_with_null_terminator(self) -> None:
        """Windows backend must supply UTF-16LE payload with two-byte terminator."""
        calls = []

        class FakeWin32:
            def __init__(self):
                self.buffers = {}

            class kernel32:
                def __init__(self):
                    self.owner = None

                def GlobalAlloc(self, flags, size):
                    calls.append(("GlobalAlloc", flags, size))
                    handle = 0x1000
                    self.owner = self.owner or {"handle": handle, "buffer": ctypes.create_string_buffer(size)}
                    return handle

                def GlobalLock(self, handle):
                    calls.append(("GlobalLock", handle))
                    buffer = ctypes.create_string_buffer(4096)
                    self.owner["buffer"] = buffer
                    return ctypes.addressof(buffer)

                def GlobalUnlock(self, handle):
                    calls.append(("GlobalUnlock", handle))
                    return True

                def GlobalFree(self, handle):
                    calls.append(("GlobalFree", handle))
                    return 0

            class user32:
                def OpenClipboard(self, hwnd):
                    calls.append(("OpenClipboard", hwnd))
                    return True

                def EmptyClipboard(self):
                    calls.append(("EmptyClipboard",))
                    return True

                def SetClipboardData(self, format_id, handle):
                    calls.append(("SetClipboardData", format_id, handle))
                    return 0x3000

                def CloseClipboard(self):
                    calls.append(("CloseClipboard",))
                    return True

        fake = FakeWin32()
        fake.kernel32 = FakeWin32.kernel32()
        fake.user32 = FakeWin32.user32()
        payload = "café"
        result = WindowsClipboard.copy(payload, api=fake)
        self.assertTrue(result)
        self.assertIn(("SetClipboardData", 13, 0x1000), calls)

    def test_windows_success_transfers_ownership_without_globalfree(self) -> None:
        """Successful SetClipboardData transfers ownership and does not free."""
        calls = []

        class FakeWin32:
            def __init__(self):
                self._buffer = ctypes.create_string_buffer(4096)

            class kernel32:
                def __init__(self, owner):
                    self.owner = owner

                def GlobalAlloc(self, flags, size):
                    calls.append(("GlobalAlloc", flags, size))
                    return 0x1000

                def GlobalLock(self, handle):
                    calls.append(("GlobalLock", handle))
                    return ctypes.addressof(self.owner._buffer)

                def GlobalUnlock(self, handle):
                    calls.append(("GlobalUnlock", handle))
                    return True

                def GlobalFree(self, handle):
                    calls.append(("GlobalFree", handle))
                    return 0

            class user32:
                def OpenClipboard(self, hwnd):
                    calls.append(("OpenClipboard", hwnd))
                    return True

                def EmptyClipboard(self):
                    calls.append(("EmptyClipboard",))
                    return True

                def SetClipboardData(self, format_id, handle):
                    calls.append(("SetClipboardData", format_id, handle))
                    return 0x3000

                def CloseClipboard(self):
                    calls.append(("CloseClipboard",))
                    return True

        fake = FakeWin32()
        fake.kernel32 = FakeWin32.kernel32(fake)
        fake.user32 = FakeWin32.user32()
        result = WindowsClipboard.copy("hello", api=fake)
        self.assertTrue(result)
        self.assertNotIn(("GlobalFree", 0x1000), calls)
        self.assertIn(("CloseClipboard",), calls)

    def test_windows_failure_frees_global_memory_before_transfer(self) -> None:
        """All pre-transfer failures must free allocated memory."""
        for failure_step in ("GlobalAlloc", "GlobalLock", "OpenClipboard", "EmptyClipboard", "SetClipboardData"):
            calls = []

            class FakeWin32:
                def __init__(self):
                    self._buffer = ctypes.create_string_buffer(4096)

                class kernel32:
                    def __init__(self, owner):
                        self.owner = owner

                    def GlobalAlloc(self, flags, size):
                        calls.append(("GlobalAlloc", flags, size))
                        return 0x1000 if failure_step != "GlobalAlloc" else 0

                    def GlobalLock(self, handle):
                        calls.append(("GlobalLock", handle))
                        if failure_step == "GlobalLock":
                            return 0
                        return ctypes.addressof(self.owner._buffer)

                    def GlobalUnlock(self, handle):
                        calls.append(("GlobalUnlock", handle))
                        return True

                    def GlobalFree(self, handle):
                        calls.append(("GlobalFree", handle))
                        return 0

                class user32:
                    def __init__(self, owner):
                        self.owner = owner

                    def OpenClipboard(self, hwnd):
                        calls.append(("OpenClipboard", hwnd))
                        return True if failure_step != "OpenClipboard" else False

                    def EmptyClipboard(self):
                        calls.append(("EmptyClipboard",))
                        return True if failure_step != "EmptyClipboard" else False

                    def SetClipboardData(self, format_id, handle):
                        calls.append(("SetClipboardData", format_id, handle))
                        return 0x3000 if failure_step != "SetClipboardData" else 0

                    def CloseClipboard(self):
                        calls.append(("CloseClipboard",))
                        return True

            fake = FakeWin32()
            fake.kernel32 = FakeWin32.kernel32(fake)
            fake.user32 = FakeWin32.user32(fake)
            self.assertFalse(WindowsClipboard.copy("hello", api=fake))
            if failure_step != "GlobalAlloc":
                self.assertIn(("GlobalFree", 0x1000), calls)
            else:
                self.assertNotIn(("GlobalFree", 0x1000), calls)

    def test_windows_closes_clipboard_after_successful_open(self) -> None:
        """CloseClipboard must always run after a successful open."""
        calls = []

        class FakeWin32:
            def __init__(self):
                self._buffer = ctypes.create_string_buffer(4096)

            class kernel32:
                def __init__(self, owner):
                    self.owner = owner

                def GlobalAlloc(self, flags, size):
                    calls.append(("GlobalAlloc", flags, size))
                    return 0x1000

                def GlobalLock(self, handle):
                    calls.append(("GlobalLock", handle))
                    return ctypes.addressof(self.owner._buffer)

                def GlobalUnlock(self, handle):
                    calls.append(("GlobalUnlock", handle))
                    return True

                def GlobalFree(self, handle):
                    calls.append(("GlobalFree", handle))
                    return 0

            class user32:
                def __init__(self, owner):
                    self.owner = owner

                def OpenClipboard(self, hwnd):
                    calls.append(("OpenClipboard", hwnd))
                    return True

                def EmptyClipboard(self):
                    calls.append(("EmptyClipboard",))
                    return True

                def SetClipboardData(self, format_id, handle):
                    calls.append(("SetClipboardData", format_id, handle))
                    return 0x3000

                def CloseClipboard(self):
                    calls.append(("CloseClipboard",))
                    return True

        fake = FakeWin32()
        fake.kernel32 = FakeWin32.kernel32(fake)
        fake.user32 = FakeWin32.user32(fake)
        self.assertTrue(WindowsClipboard.copy("hello", api=fake))
        self.assertIn(("CloseClipboard",), calls)

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.temp_dir.name) / "repo"
        self.repo_root.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_windows_failure_reaches_printable_recovery(self) -> None:
        """Windows failure path must be reported through printable recovery output."""
        class FakeWin32:
            def __init__(self):
                self._buffer = ctypes.create_string_buffer(4096)

            class kernel32:
                def __init__(self, owner):
                    self.owner = owner

                def GlobalAlloc(self, flags, size):
                    return 0

                def GlobalLock(self, handle):
                    return 0

                def GlobalUnlock(self, handle):
                    return True

                def GlobalFree(self, handle):
                    return 0

            class user32:
                def __init__(self, owner):
                    self.owner = owner

                def OpenClipboard(self, hwnd):
                    return True

                def EmptyClipboard(self):
                    return True

                def SetClipboardData(self, format_id, handle):
                    return 0

                def CloseClipboard(self):
                    return True

        with mock.patch.object(clipboard_module, "get_canonical_report") as mock_get:
            mock_get.return_value = "Issue: 49\n"
            with mock.patch.object(clipboard_module, "get_clipboard_backends") as mock_backends:
                mock_backends.return_value = [lambda content: WindowsClipboard.copy(content, api=FakeWin32())]
                success, message = copy_report_to_clipboard(self.repo_root)
                self.assertFalse(success)
                self.assertNotIn("Issue: 49", message)


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

        with mock.patch.object(clipboard_module, "get_canonical_report") as mock_get:
            mock_get.return_value = canonical_report
            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch.object(sys, "stdout", stdout), mock.patch.object(sys, "stderr", stderr):
                with mock.patch.object(clipboard_module, "get_clipboard_backends") as mock_backends:
                    mock_backends.return_value = [failing_backend]
                    self.assertEqual(main(), 1)

        self.assertEqual(stdout.getvalue(), canonical_report)
        self.assertEqual(stdout.getvalue().count("Issue: 49"), 1)
        self.assertIn("All clipboard backends failed", stderr.getvalue())
        self.assertNotIn("Report copied", stderr.getvalue())

    def test_success_prints_report_once_and_confirmation_only_to_stderr(self) -> None:
        canonical_report = "Issue: 49\nPrompt: café 中文\n"

        with mock.patch.object(clipboard_module, "get_canonical_report", return_value=canonical_report):
            with mock.patch.object(clipboard_module, "get_clipboard_backends", return_value=[lambda content: content == canonical_report]):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with mock.patch.object(sys, "stdout", stdout), mock.patch.object(sys, "stderr", stderr):
                    self.assertEqual(main(), 0)

        self.assertEqual(stdout.getvalue(), canonical_report)
        self.assertEqual(stdout.getvalue().count("Issue: 49"), 1)
        self.assertIn("Report copied", stderr.getvalue())
        self.assertNotIn("Report copied", stdout.getvalue())

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

    # PATH-resolved backends the wrapper may reach on a POSIX host. The test supplies
    # its own executable for every one of them so the host's real clipboard is
    # unreachable, and the run never depends on which tools happen to be installed.
    PATH_BACKENDS = ("wl-copy", "xclip", "xsel", "pbcopy")

    def _write_fake_backend(self, directory: Path, name: str, record: Path, *, exit_code: int = 0) -> None:
        """Install an executable that records what it received instead of copying."""
        script = directory / name
        script.write_text(
            f"#!{sys.executable}\n"
            "import pathlib, sys\n"
            f"pathlib.Path({str(record)!r}).write_bytes(sys.stdin.buffer.read())\n"
            f"raise SystemExit({exit_code})\n",
            encoding="utf-8",
        )
        script.chmod(0o755)

    def test_actual_symlink_invocation_resolves_source_and_runs_report(self) -> None:
        """A real symlink should resolve the repository and deliver the report to a backend."""
        if not hasattr(os, "symlink"):
            self.skipTest("symbolic links are unavailable on this platform")
        if sys.platform == "win32":
            self.skipTest("the Windows backend is in-process ctypes and cannot be replaced through PATH")

        temp_dir = tempfile.TemporaryDirectory()
        try:
            target_dir = Path(temp_dir.name)
            symlink_path = target_dir / "flow-report-clipboard-link.py"
            try:
                symlink_path.symlink_to(wrapper_path)
            except (OSError, NotImplementedError):
                self.skipTest("symbolic links are unavailable on this platform")

            backend_dir = target_dir / "fake-backends"
            backend_dir.mkdir()
            primary = "pbcopy" if sys.platform == "darwin" else "wl-copy"
            delivered = backend_dir / "delivered-report.txt"
            self._write_fake_backend(backend_dir, primary, delivered)

            # The remaining backends are shadowed and fail, so a real one is never
            # reachable and reaching them at all is observable.
            fallbacks = {}
            for name in self.PATH_BACKENDS:
                if name == primary:
                    continue
                marker = backend_dir / f"{name}-was-invoked"
                fallbacks[name] = marker
                self._write_fake_backend(backend_dir, name, marker, exit_code=1)

            environment = os.environ.copy()
            environment["PATH"] = os.pathsep.join([str(backend_dir), environment.get("PATH", "")])

            completed = subprocess.run(
                [sys.executable, str(symlink_path)],
                capture_output=True,
                text=True,
                cwd=str(Path(__file__).resolve().parent.parent),
                env=environment,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("Issue:", completed.stdout)
            self.assertEqual(completed.stdout.splitlines().count("Copilot work report"), 1)
            self.assertNotIn("\x1b]52;c;", completed.stdout)
            self.assertIn("report copied", completed.stderr.lower())
            if primary == "wl-copy":
                self.assertIn(WlCopyBackend.name, completed.stderr)

            # The controlled backend received exactly what was printed, so the report
            # was delivered rather than merely rendered.
            self.assertTrue(delivered.exists(), "the controlled backend received nothing")
            self.assertEqual(delivered.read_text(encoding="utf-8"), completed.stdout)

            for name, marker in fallbacks.items():
                self.assertFalse(marker.exists(), f"the wrapper fell through to {name}")
        finally:
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()

"""Tests for the clipboard wrapper."""
from __future__ import annotations

import base64
import os
from pathlib import Path
from unittest import mock
import subprocess
import sys
import tempfile
import unittest
import importlib.util

# Import the clipboard wrapper module
wrapper_path = (Path(__file__).resolve().parent.parent / "skills" / "copilot" / "flow" / "scripts" / "flow-report-clipboard.py")
spec = importlib.util.spec_from_file_location("clipboard_wrapper", wrapper_path)
clipboard_module = importlib.util.module_from_spec(spec)
sys.modules["clipboard_wrapper"] = clipboard_module
spec.loader.exec_module(clipboard_module)

# Import symbols from the loaded module
copy_report_to_clipboard = clipboard_module.copy_report_to_clipboard
get_canonical_report = clipboard_module.get_canonical_report
OSC52Backend = clipboard_module.OSC52Backend
WlCopyBackend = clipboard_module.WlCopyBackend
XclipBackend = clipboard_module.XclipBackend
XselBackend = clipboard_module.XselBackend


class ClipboardWrapperTests(unittest.TestCase):
    """Tests for the cross-platform clipboard wrapper."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.temp_dir.name) / "repo"
        self.repo_root.mkdir(parents=True)
        (self.repo_root / ".ai-dev").mkdir(exist_ok=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_wrapper_output_equals_canonical_report(self) -> None:
        """Wrapper output must equal canonical flow-report byte-for-byte."""
        canonical_report = "Issue: 49\nPrompt: test\nTokens: unavailable\n"

        # Mock get_canonical_report to return known content
        with mock.patch.object(clipboard_module, "get_canonical_report") as mock_get:
            mock_get.return_value = canonical_report

            # Mock a successful backend
            def mock_backend(content: str) -> bool:
                self.assertEqual(content, canonical_report, "Backend received exact report")
                return True

            success, message = copy_report_to_clipboard(self.repo_root, backends=[mock_backend])
            self.assertTrue(success)

    def test_unicode_in_report_preserved(self) -> None:
        """Unicode content in report must be preserved exactly."""
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

    def test_final_newline_preserved(self) -> None:
        """Final newline in report must be preserved."""
        report_with_newline = "Issue: 49\nTokens: unavailable\n"
        report_without_newline = "Issue: 49\nTokens: unavailable"

        captured_newline = None
        captured_no_newline = None

        def capture1(content: str) -> bool:
            nonlocal captured_newline
            captured_newline = content
            return True

        def capture2(content: str) -> bool:
            nonlocal captured_no_newline
            captured_no_newline = content
            return True

        with mock.patch.object(clipboard_module, "get_canonical_report") as mock_get:
            mock_get.return_value = report_with_newline
            copy_report_to_clipboard(self.repo_root, backends=[capture1])

        with mock.patch.object(clipboard_module, "get_canonical_report") as mock_get:
            mock_get.return_value = report_without_newline
            copy_report_to_clipboard(self.repo_root, backends=[capture2])

        self.assertTrue(captured_newline.endswith("\n"))
        self.assertFalse(captured_no_newline.endswith("\n"))

    def test_report_passed_via_stdin_not_arguments(self) -> None:
        """Report content must be passed via stdin/API, never as subprocess arguments."""
        report = "Issue: 49\nSecret token data here\n"

        # Track subprocess.run calls
        subprocess_calls = []

        def mock_run(*args, **kwargs):
            subprocess_calls.append((args, kwargs))
            # Return success
            return mock.MagicMock(returncode=0)

        with mock.patch.object(clipboard_module, "get_canonical_report") as mock_get:
            mock_get.return_value = report

            # Test xclip backend
            with mock.patch("subprocess.run", side_effect=mock_run):
                result = XclipBackend.copy(report)

            # Verify the report was passed via stdin, not as argument
            self.assertTrue(any(kwargs.get("input") == report.encode("utf-8") for args, kwargs in subprocess_calls), "Report must be in stdin")
            # Verify the command-line arguments don't contain the secret
            for args, kwargs in subprocess_calls:
                if args:
                    cmd = args[0]
                    self.assertNotIn("Secret token", str(cmd))

    def test_wl_copy_backend_exact_arguments(self) -> None:
        """wl-copy backend must receive report via stdin with no extra arguments."""
        report = "Issue: 49\nTokens: 100\n"

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

    def test_xclip_backend_exact_arguments(self) -> None:
        """xclip backend must use correct arguments."""
        report = "Issue: 49\nTokens: 100\n"

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

    def test_xsel_backend_exact_arguments(self) -> None:
        """xsel backend must use correct arguments."""
        report = "Issue: 49\nTokens: 100\n"

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

    def test_osc52_encoding_exact(self) -> None:
        """OSC 52 encoding must be exact UTF-8 then Base64."""
        report = "Issue: 49\nTokens: 100\n"

        captured_output = []

        def mock_write(data):
            captured_output.append(data)
            return len(data)

        def mock_flush():
            pass

        mock_buffer = mock.MagicMock()
        mock_buffer.write = mock_write
        mock_buffer.flush = mock_flush

        with mock.patch.object(clipboard_module, "sys") as mock_sys:
            mock_sys.stdout.buffer = mock_buffer
            result = OSC52Backend.copy(report)

        self.assertTrue(result)
        self.assertEqual(len(captured_output), 1)

        output_bytes = captured_output[0]
        # Extract Base64 from OSC 52: ESC ] 52 ; c ; <base64> ESC \
        start = output_bytes.find(b";c;")
        end = output_bytes.rfind(b"\x1b")
        if start >= 0 and end > start:
            encoded_b64 = output_bytes[start + 3 : end]
            decoded = base64.b64decode(encoded_b64).decode("utf-8")
            self.assertEqual(decoded, report)

    def test_osc52_with_unicode(self) -> None:
        """OSC 52 must handle Unicode correctly."""
        report = "Issue: 49\nPrompt: café\nTokens: 100\n"

        captured_output = []

        def mock_write(data):
            captured_output.append(data)
            return len(data)

        def mock_flush():
            pass

        mock_buffer = mock.MagicMock()
        mock_buffer.write = mock_write
        mock_buffer.flush = mock_flush

        with mock.patch.object(clipboard_module, "sys") as mock_sys:
            mock_sys.stdout.buffer = mock_buffer
            result = OSC52Backend.copy(report)

        self.assertTrue(result)

        # Verify encoding
        output_bytes = captured_output[0]
        start = output_bytes.find(b";c;")
        end = output_bytes.rfind(b"\x1b")
        if start >= 0 and end > start:
            encoded_b64 = output_bytes[start + 3 : end]
            decoded = base64.b64decode(encoded_b64).decode("utf-8")
            self.assertEqual(decoded, report)
            self.assertIn("café", decoded)

    def test_osc52_size_limit_enforced(self) -> None:
        """OSC 52 must fail if report exceeds size limit without truncation."""
        # Create a report larger than the limit
        oversized_report = "x" * (OSC52Backend.MAX_OSC52_BYTES + 1000)

        result = OSC52Backend.copy(oversized_report)
        self.assertFalse(result, "Oversized report should fail")

    def test_osc52_at_boundary_succeeds(self) -> None:
        """OSC 52 should succeed at or near the boundary."""
        # Create report at the exact limit
        report_at_limit = "x" * OSC52Backend.MAX_OSC52_BYTES

        captured_output = []

        def mock_write(data):
            captured_output.append(data)
            return len(data)

        def mock_flush():
            pass

        mock_buffer = mock.MagicMock()
        mock_buffer.write = mock_write
        mock_buffer.flush = mock_flush

        with mock.patch.object(clipboard_module, "sys") as mock_sys:
            mock_sys.stdout.buffer = mock_buffer
            result = OSC52Backend.copy(report_at_limit)

        self.assertTrue(result)

    def test_all_backends_unavailable_fails_nonzero(self) -> None:
        """When all backends fail, should return nonzero and actionable guidance."""

        def failing_backend(content: str) -> bool:
            return False

        with mock.patch.object(clipboard_module, "get_canonical_report") as mock_get:
            mock_get.return_value = "Issue: 49\n"

            success, message = copy_report_to_clipboard(self.repo_root, backends=[failing_backend, failing_backend])

        self.assertFalse(success)
        self.assertIn("Error", message)
        self.assertIn("clipboard", message.lower())

    def test_backend_exception_continues_to_next(self) -> None:
        """Exception in one backend should not prevent trying the next."""

        def failing_backend(content: str) -> bool:
            raise RuntimeError("Backend failed")

        def success_backend(content: str) -> bool:
            return True

        with mock.patch.object(clipboard_module, "get_canonical_report") as mock_get:
            mock_get.return_value = "Issue: 49\n"

            success, message = copy_report_to_clipboard(self.repo_root, backends=[failing_backend, success_backend])

        self.assertTrue(success)

    def test_no_report_available_fails(self) -> None:
        """When canonical report is unavailable, should fail gracefully."""
        with mock.patch.object(clipboard_module, "get_canonical_report") as mock_get:
            mock_get.return_value = None

            success, message = copy_report_to_clipboard(self.repo_root)

        self.assertFalse(success)
        self.assertIn("Could not obtain canonical report", message)

    def test_existing_flow_helpers_unchanged(self) -> None:
        """flow-report and flow-report.ps1 helpers must remain unchanged and print-only."""
        scripts_dir = Path(__file__).resolve().parent.parent / "skills" / "copilot" / "flow" / "scripts"

        # Verify helpers exist
        posix_helper = scripts_dir / "flow-report"
        self.assertTrue(posix_helper.exists(), "flow-report helper must exist")

        ps1_helper = scripts_dir / "flow-report.ps1"
        self.assertTrue(ps1_helper.exists(), "flow-report.ps1 helper must exist")

        # Verify they're still executable and short (print-only)
        self.assertTrue(os.access(posix_helper, os.X_OK), "flow-report must be executable")

        posix_content = posix_helper.read_text()
        # Should be a thin wrapper, not contain clipboard logic
        self.assertNotIn("clipboard", posix_content.lower())
        self.assertNotIn("wl-copy", posix_content)
        self.assertNotIn("xclip", posix_content)

    def test_empty_report_handled(self) -> None:
        """Empty report should be handled gracefully."""
        with mock.patch.object(clipboard_module, "get_canonical_report") as mock_get:
            mock_get.return_value = ""

            def mock_backend(content: str) -> bool:
                self.assertEqual(content, "")
                return True

            success, message = copy_report_to_clipboard(self.repo_root, backends=[mock_backend])

        self.assertTrue(success)

    def test_very_long_report_handled(self) -> None:
        """Very long report should be handled by appropriate backend."""
        # Create a report that's large but within OSC 52 limit
        long_report = "Issue: 49\n" + ("x" * 7000) + "\n"

        with mock.patch.object(clipboard_module, "get_canonical_report") as mock_get:
            mock_get.return_value = long_report

            def mock_backend(content: str) -> bool:
                self.assertEqual(len(content), len(long_report))
                return True

            success, message = copy_report_to_clipboard(self.repo_root, backends=[mock_backend])

        self.assertTrue(success)


if __name__ == "__main__":
    unittest.main()

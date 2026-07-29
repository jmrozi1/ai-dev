from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ai_dev_flow.editor_opening import EditorOpenResult
from ai_dev_flow.report_presentation import (
    EditorReportPresenter,
    PathOnlyReportPresenter,
    ReportPresentationError,
    StdoutReportPresenter,
)


class _FakeOpener:
    def __init__(self, opened: bool, warning: str | None = None) -> None:
        self.opened = opened
        self.warning = warning
        self.calls: list[Path] = []

    def open_path(self, target_path: Path) -> EditorOpenResult:
        self.calls.append(target_path)
        return EditorOpenResult(opened=self.opened, warning=self.warning)


class ReportPresentationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_stdout_prints_contents_once(self) -> None:
        report = self.tmp_path / "report.md"
        report.write_text("report body\n", encoding="utf-8")

        out = io.StringIO()
        with redirect_stdout(out):
            StdoutReportPresenter().present(report)

        self.assertEqual(out.getvalue(), "report body\n")

    def test_editor_mode_opens_correct_path(self) -> None:
        report = self.tmp_path / "report.md"
        report.write_text("report body\n", encoding="utf-8")

        opener = _FakeOpener(opened=True)
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            EditorReportPresenter(editor_opener=opener).present(report)

        self.assertEqual(opener.calls, [report])
        self.assertIn(f"Opened report in editor: {report}", out.getvalue())
        self.assertEqual(err.getvalue(), "")

    def test_editor_mode_success_with_warning_prints_warning_once(self) -> None:
        report = self.tmp_path / "report.md"
        report.write_text("report body\n", encoding="utf-8")

        opener = _FakeOpener(opened=True, warning="configured editor unavailable; using VISUAL")
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            EditorReportPresenter(editor_opener=opener).present(report)

        self.assertIn(f"Opened report in editor: {report}", out.getvalue())
        self.assertEqual(out.getvalue().count("Opened report in editor"), 1)
        self.assertIn("configured editor unavailable; using VISUAL", err.getvalue())
        self.assertEqual(err.getvalue().count("configured editor unavailable; using VISUAL"), 1)
        self.assertNotIn("report body", out.getvalue())

    def test_editor_failure_falls_back_to_path(self) -> None:
        report = self.tmp_path / "report.md"
        report.write_text("report body\n", encoding="utf-8")

        opener = _FakeOpener(opened=False, warning="launch failed")
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            EditorReportPresenter(editor_opener=opener).present(report)

        self.assertIn(f"Report path: {report}", out.getvalue())
        self.assertIn("Warning: launch failed", err.getvalue())
        self.assertNotIn("Opened report in editor", out.getvalue())

    def test_path_only_output(self) -> None:
        report = self.tmp_path / "report.md"
        report.write_text("report body\n", encoding="utf-8")

        out = io.StringIO()
        with redirect_stdout(out):
            PathOnlyReportPresenter().present(report)

        self.assertEqual(out.getvalue(), f"Report path: {report}\n")

    def test_missing_report_error(self) -> None:
        missing = self.tmp_path / "missing.md"
        with self.assertRaises(ReportPresentationError) as context:
            StdoutReportPresenter().present(missing)

        self.assertIn("Report file does not exist", str(context.exception))

    def test_unreadable_report_error(self) -> None:
        report = self.tmp_path / "report.md"
        report.write_text("report body\n", encoding="utf-8")

        with patch.object(Path, "read_text", side_effect=OSError("permission denied")):
            with self.assertRaises(ReportPresentationError) as context:
                StdoutReportPresenter().present(report)

        self.assertIn("Cannot read report file", str(context.exception))


if __name__ == "__main__":
    unittest.main()

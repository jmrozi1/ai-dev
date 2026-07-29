from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
import sys

from .editor_opening import EditorOpener


SUPPORTED_REPORT_PRESENTATION_VALUES = (
    "stdout",
    "editor",
    "path-only",
)


class ReportPresentationError(Exception):
    """Raised for report presentation failures."""


class ReportPresenter(Protocol):
    def present(self, report_path: Path) -> None:
        pass


@dataclass(frozen=True)
class StdoutReportPresenter:
    def present(self, report_path: Path) -> None:
        if not report_path.exists():
            raise ReportPresentationError(f"Report file does not exist: {report_path}")

        try:
            content = report_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ReportPresentationError(f"Cannot read report file {report_path}: {exc}") from exc

        print(content, end="")


@dataclass(frozen=True)
class EditorReportPresenter:
    editor_opener: EditorOpener

    def present(self, report_path: Path) -> None:
        if not report_path.exists():
            raise ReportPresentationError(f"Report file does not exist: {report_path}")

        result = self.editor_opener.open_path(report_path)
        if result.opened:
            if result.warning:
                print(f"Warning: {result.warning}", file=sys.stderr)
            print(f"Opened report in editor: {report_path}")
            return

        if result.warning:
            print(f"Warning: {result.warning}", file=sys.stderr)
        print(f"Report path: {report_path}")


@dataclass(frozen=True)
class PathOnlyReportPresenter:
    def present(self, report_path: Path) -> None:
        if not report_path.exists():
            raise ReportPresentationError(f"Report file does not exist: {report_path}")
        print(f"Report path: {report_path}")


def build_report_presenter(
    presentation_mode: str,
    *,
    editor_opener: EditorOpener,
) -> ReportPresenter:
    if presentation_mode == "stdout":
        return StdoutReportPresenter()

    if presentation_mode == "editor":
        return EditorReportPresenter(editor_opener=editor_opener)

    if presentation_mode == "path-only":
        return PathOnlyReportPresenter()

    raise ValueError(
        "Unsupported report presentation mode: "
        f"{presentation_mode}. Supported modes: {', '.join(SUPPORTED_REPORT_PRESENTATION_VALUES)}."
    )

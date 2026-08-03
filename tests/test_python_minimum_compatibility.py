from __future__ import annotations

import ast
from pathlib import Path
import unittest


class PythonMinimumCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.package_root = self.repo_root / "ai_dev_flow"

    def test_package_parses_with_python38_grammar(self) -> None:
        failures: list[str] = []
        for path in sorted(self.package_root.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            try:
                ast.parse(source, filename=str(path), feature_version=8)
            except SyntaxError as exc:
                failures.append(f"{path}:L{exc.lineno}: {exc.msg}")
        self.assertEqual(
            failures,
            [],
            msg="Package source failed to parse with Python 3.8 grammar:\n" + "\n".join(failures),
        )

    def test_pep604_annotations_are_postponed_in_package_modules(self) -> None:
        missing_future: list[str] = []
        for path in sorted(self.package_root.rglob("*.py")):
            if path.name == "__init__.py":
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
            header = "\n".join(lines[:5])
            if "from __future__ import annotations" not in header:
                missing_future.append(str(path))

        self.assertEqual(
            missing_future,
            [],
            msg=(
                "Expected postponed annotation semantics in package modules to keep"
                " PEP 604 and built-in generic hints compatible with Python 3.8 runtime."
            ),
        )

    def test_no_known_stdlib_dependency_requires_python39_plus(self) -> None:
        banned_tokens = (
            "import tomllib",
            "from tomllib",
            ".removeprefix(",
            ".removesuffix(",
            ".is_relative_to(",
        )
        hits: list[str] = []
        for path in sorted(self.package_root.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for token in banned_tokens:
                if token in text:
                    hits.append(f"{path}: contains {token}")

        self.assertEqual(
            hits,
            [],
            msg="Found stdlib APIs associated with newer minimums:\n" + "\n".join(hits),
        )


if __name__ == "__main__":
    unittest.main()

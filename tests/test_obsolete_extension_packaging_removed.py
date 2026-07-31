from __future__ import annotations

from pathlib import Path
import unittest


class ObsoleteExtensionPackagingRemovedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]

    def test_obsolete_vsix_packaging_scripts_are_removed(self) -> None:
        for relative_path in [
            "scripts/build-vscode-plugin.cjs",
            "scripts/build-vsix.sh",
            "scripts/update-vscode-plugin.ps1",
        ]:
            with self.subTest(path=relative_path):
                self.assertFalse(
                    (self.repo_root / relative_path).exists(),
                    f"obsolete packaging script still present: {relative_path}",
                )

    def test_extension_package_is_removed(self) -> None:
        self.assertFalse(
            (self.repo_root / "ai-dev-vscode").exists(),
            "obsolete VS Code extension package is still present",
        )

    def test_readmes_do_not_document_vsix_or_extension_installation(self) -> None:
        checks = {
            "README.md": [
                "build-vsix.sh",
                "install-extension",
                ".vsix",
                "ai-dev-vscode",
            ],
        }

        for relative_path, forbidden_terms in checks.items():
            text = (self.repo_root / relative_path).read_text(encoding="utf-8")
            for forbidden in forbidden_terms:
                with self.subTest(path=relative_path, forbidden=forbidden):
                    self.assertNotIn(forbidden, text)

    def test_cleanup_report_records_removed_extension_surface(self) -> None:
        report = (self.repo_root / "docs/obsolete-plugin-cleanup-final.md").read_text(
            encoding="utf-8"
        )

        required_markers = [
            "Preferred outcome selected: remove the extension package completely.",
            "| Extension source files (`ai-dev-vscode/src`, excluding `src/test`) | 35 | 0 |",
            "| Extension LOC (`ai-dev-vscode/src`, excluding `src/test`) | 17,788 | 0 |",
            "| VS Code contributed commands | 2 | 0 |",
            "| Activation events | 2 | 0 |",
            "| Installation steps for supported extension path | 2 | 0 |",
        ]

        for marker in required_markers:
            with self.subTest(marker=marker):
                self.assertIn(marker, report)


if __name__ == "__main__":
    unittest.main()

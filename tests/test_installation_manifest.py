from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from ai_dev_flow.installation_manifest import (
    InstallationManifest,
    InstallationManifestError,
    MANIFEST_VERSION,
    load_manifest,
    save_manifest,
)


class InstallationManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.manifest_path = self.tmp_path / "manifest.json"

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_round_trip(self) -> None:
        manifest = InstallationManifest(
            version=MANIFEST_VERSION,
            profile_path=Path("/tmp/profile"),
            profile_sha256="a" * 64,
            alias_file_path=Path("/tmp/aliases.sh"),
            alias_file_sha256="b" * 64,
            aliases={"gs": "status"},
        )
        save_manifest(self.manifest_path, manifest)

        loaded = load_manifest(self.manifest_path)

        self.assertEqual(loaded, manifest)

    def test_missing_file_returns_none(self) -> None:
        loaded = load_manifest(self.manifest_path)
        self.assertIsNone(loaded)

    def test_invalid_version_fails(self) -> None:
        self.manifest_path.write_text(
            "{\"version\": 999}",
            encoding="utf-8",
        )

        with self.assertRaises(InstallationManifestError):
            load_manifest(self.manifest_path)

    def test_rejects_non_absolute_path(self) -> None:
        self.manifest_path.write_text(
            "{"
            '"version": 1,'
            '"profile_path": "relative/path",'
            '"profile_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
            '"alias_file_path": "/tmp/aliases.sh",'
            '"alias_file_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
            '"aliases": {"gs": "status"}'
            "}",
            encoding="utf-8",
        )

        with self.assertRaises(InstallationManifestError):
            load_manifest(self.manifest_path)

    def test_rejects_non_normalized_path(self) -> None:
        self.manifest_path.write_text(
            "{"
            '"version": 1,'
            '"profile_path": "/tmp/a/../profile",'
            '"profile_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
            '"alias_file_path": "/tmp/aliases.sh",'
            '"alias_file_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
            '"aliases": {"gs": "status"}'
            "}",
            encoding="utf-8",
        )

        with self.assertRaises(InstallationManifestError):
            load_manifest(self.manifest_path)

    def test_rejects_invalid_digest(self) -> None:
        self.manifest_path.write_text(
            "{"
            '"version": 1,'
            '"profile_path": "/tmp/profile",'
            '"profile_sha256": "XYZ",'
            '"alias_file_path": "/tmp/aliases.sh",'
            '"alias_file_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
            '"aliases": {"gs": "status"}'
            "}",
            encoding="utf-8",
        )

        with self.assertRaises(InstallationManifestError):
            load_manifest(self.manifest_path)


if __name__ == "__main__":
    unittest.main()

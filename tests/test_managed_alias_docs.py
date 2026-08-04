from __future__ import annotations

from pathlib import Path
import unittest


class ManagedAliasDocumentationTests(unittest.TestCase):
    def test_readme_has_managed_alias_add_section_and_migration_guidance(self) -> None:
        text = Path("README.md").read_text(encoding="utf-8")
        stale_phrase = "Reserved for future managed command " + "aliases"

        self.assertIn("## Adding managed command aliases", text)
        self.assertIn("installation:\n  aliases:\n", text)
        self.assertIn("my-alias: \"ai-dev some-command\"", text)
        self.assertIn("- `my-alias` -> `ai-dev some-command`", text)
        self.assertIn("After saving the config, run:", text)
        self.assertIn("This creates and reconciles managed launcher files.", text)
        self.assertIn("aliases: {}", text)
        self.assertIn(
            "Top-level `aliases` is an obsolete configuration field retained in some older user config files. "
            "It is not used for managed launchers, and `ai-dev apply` ignores it.",
            text,
        )
        self.assertIn(
            "Unsupported targets still install their root launcher, but do not receive generated descendants because no authoritative command model is available.",
            text,
        )
        self.assertIn("preserves existing config files byte-for-byte", text)
        self.assertNotIn(stale_phrase, text)
        self.assertNotIn("compatibility config", text)

    def test_managed_alias_doc_has_edit_location_and_migration_guidance(self) -> None:
        text = Path("docs/managed-aliases-slice2.md").read_text(encoding="utf-8")
        stale_phrase = "Reserved for future managed command " + "aliases"

        self.assertIn("## Adding managed command aliases", text)
        self.assertIn("Open the file with `ai-dev config`", text)
        self.assertIn("installation:\n  aliases:\n", text)
        self.assertIn("my-alias: \"ai-dev some-command\"", text)
        self.assertIn("- `my-alias` -> `ai-dev some-command`", text)
        self.assertIn("After saving the config, run:", text)
        self.assertIn("This creates and reconciles managed launcher files.", text)
        self.assertIn("aliases: {}", text)
        self.assertIn(
            "Top-level `aliases` is an obsolete configuration field retained in some older user config files. "
            "It is not used for managed launchers, and `ai-dev apply` ignores it.",
            text,
        )
        self.assertIn(
            "Unsupported targets still install their root launcher, but do not receive generated descendants because no authoritative command model is available.",
            text,
        )
        self.assertIn("preserves an existing config file byte-for-byte", text)
        self.assertNotIn(stale_phrase, text)
        self.assertNotIn("compatibility config", text)


if __name__ == "__main__":
    unittest.main()

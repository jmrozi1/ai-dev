from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ai_dev_flow.summarize_config import SummarizeConfigError, load_repository_summarize_config


class SummarizeConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _run_git(self, repo_root: Path, *args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            check=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return completed.stdout.strip()

    def _init_repo(self, name: str) -> Path:
        repo_root = self.tmp_path / name
        repo_root.mkdir(parents=True)

        self._run_git(repo_root, "init", "-q")
        self._run_git(repo_root, "config", "user.name", "Summarize Config Tests")
        self._run_git(repo_root, "config", "user.email", "summarize-config-tests@example.com")
        (repo_root / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._run_git(repo_root, "add", "tracked.txt")
        self._run_git(repo_root, "commit", "-q", "-m", "initial")

        return repo_root

    def test_absent_summarize_config_uses_empty_rules(self) -> None:
        repo_root = self._init_repo("repo-empty")
        config = load_repository_summarize_config(repo_root)

        self.assertEqual(config.rules, ())
        self.assertIsNone(config.repository_config_path)

    def test_valid_rules_and_order_preserved(self) -> None:
        repo_root = self._init_repo("repo-valid-rules")
        config_path = repo_root / ".ai-dev.yaml"
        config_path.write_text(
            "summarize:\n"
            "  rules:\n"
            "    - match: \"**/*\"\n"
            "      instructions: |\n"
            "        Generic summary line one.\n"
            "        Generic summary line two.\n"
            "    - match: \"**/config.xml\"\n"
            "      instructions: |\n"
            "        Specific configuration guidance.\n",
            encoding="utf-8",
        )

        config = load_repository_summarize_config(repo_root)
        self.assertEqual(config.repository_config_path, config_path)
        self.assertEqual(len(config.rules), 2)
        self.assertEqual(config.rules[0].index, 0)
        self.assertEqual(config.rules[1].index, 1)
        self.assertEqual(config.rules[0].match, "**/*")
        self.assertEqual(config.rules[1].match, "**/config.xml")
        self.assertIn("Generic summary line one.", config.rules[0].instructions)

    def test_summarize_must_be_mapping(self) -> None:
        repo_root = self._init_repo("repo-summarize-type")
        path = repo_root / ".ai-dev.yaml"
        path.write_text("summarize: not-a-mapping\n", encoding="utf-8")

        with self.assertRaises(SummarizeConfigError) as context:
            load_repository_summarize_config(repo_root)

        message = str(context.exception)
        self.assertIn(str(path), message)
        self.assertIn("at summarize", message)

    def test_rules_must_be_list(self) -> None:
        repo_root = self._init_repo("repo-rules-type")
        path = repo_root / ".ai-dev.yaml"
        path.write_text(
            "summarize:\n"
            "  rules: bad\n",
            encoding="utf-8",
        )

        with self.assertRaises(SummarizeConfigError) as context:
            load_repository_summarize_config(repo_root)

        message = str(context.exception)
        self.assertIn(str(path), message)
        self.assertIn("at summarize.rules", message)

    def test_unknown_summarize_key_rejected(self) -> None:
        repo_root = self._init_repo("repo-unknown-summarize-key")
        path = repo_root / ".ai-dev.yaml"
        path.write_text(
            "summarize:\n"
            "  rules: []\n"
            "  unknown: true\n",
            encoding="utf-8",
        )

        with self.assertRaises(SummarizeConfigError) as context:
            load_repository_summarize_config(repo_root)

        self.assertIn(str(path), str(context.exception))
        self.assertIn("unknown key(s): unknown", str(context.exception))

    def test_unknown_rule_key_rejected(self) -> None:
        repo_root = self._init_repo("repo-unknown-rule-key")
        path = repo_root / ".ai-dev.yaml"
        path.write_text(
            "summarize:\n"
            "  rules:\n"
            "    - match: \"**/*\"\n"
            "      instructions: \"base\"\n"
            "      unknown: value\n",
            encoding="utf-8",
        )

        with self.assertRaises(SummarizeConfigError) as context:
            load_repository_summarize_config(repo_root)

        self.assertIn(str(path), str(context.exception))
        self.assertIn("summarize.rules[0]", str(context.exception))
        self.assertIn("unknown key(s): unknown", str(context.exception))

    def test_rule_match_brace_syntax_rejected(self) -> None:
        repo_root = self._init_repo("repo-rule-braces")
        path = repo_root / ".ai-dev.yaml"
        path.write_text(
            "summarize:\n"
            "  rules:\n"
            "    - match: \"**/*.{py,txt}\"\n"
            "      instructions: \"base\"\n",
            encoding="utf-8",
        )

        with self.assertRaises(SummarizeConfigError) as context:
            load_repository_summarize_config(repo_root)

        message = str(context.exception)
        self.assertIn(str(path), message)
        self.assertIn("summarize.rules[0].match", message)
        self.assertIn("brace expansion", message)

    def test_rule_match_unclosed_character_class_rejected(self) -> None:
        repo_root = self._init_repo("repo-rule-unclosed-class")
        path = repo_root / ".ai-dev.yaml"
        path.write_text(
            "summarize:\n"
            "  rules:\n"
            "    - match: \"**/[ab.py\"\n"
            "      instructions: \"base\"\n",
            encoding="utf-8",
        )

        with self.assertRaises(SummarizeConfigError) as context:
            load_repository_summarize_config(repo_root)

        message = str(context.exception)
        self.assertIn(str(path), message)
        self.assertIn("summarize.rules[0].match", message)
        self.assertIn("unclosed character class", message)

    def test_rule_match_empty_character_class_rejected(self) -> None:
        repo_root = self._init_repo("repo-rule-empty-class")
        path = repo_root / ".ai-dev.yaml"
        path.write_text(
            "summarize:\n"
            "  rules:\n"
            "    - match: \"**/[].py\"\n"
            "      instructions: \"base\"\n",
            encoding="utf-8",
        )

        with self.assertRaises(SummarizeConfigError) as context:
            load_repository_summarize_config(repo_root)

        message = str(context.exception)
        self.assertIn(str(path), message)
        self.assertIn("summarize.rules[0].match", message)
        self.assertIn("empty character class", message)

    def test_empty_match_rejected(self) -> None:
        repo_root = self._init_repo("repo-empty-match")
        path = repo_root / ".ai-dev.yaml"
        path.write_text(
            "summarize:\n"
            "  rules:\n"
            "    - match: \"   \"\n"
            "      instructions: \"ok\"\n",
            encoding="utf-8",
        )

        with self.assertRaises(SummarizeConfigError) as context:
            load_repository_summarize_config(repo_root)

        self.assertIn(str(path), str(context.exception))
        self.assertIn("summarize.rules[0].match", str(context.exception))

    def test_empty_instructions_rejected(self) -> None:
        repo_root = self._init_repo("repo-empty-instructions")
        path = repo_root / ".ai-dev.yaml"
        path.write_text(
            "summarize:\n"
            "  rules:\n"
            "    - match: \"**/*\"\n"
            "      instructions: \"  \"\n",
            encoding="utf-8",
        )

        with self.assertRaises(SummarizeConfigError) as context:
            load_repository_summarize_config(repo_root)

        self.assertIn(str(path), str(context.exception))
        self.assertIn("summarize.rules[0].instructions", str(context.exception))

    def test_user_config_cannot_override_repository_summarize_rules(self) -> None:
        repo_root = self._init_repo("repo-user-cannot-override")
        (repo_root / ".ai-dev.yaml").write_text(
            "summarize:\n"
            "  rules:\n"
            "    - match: \"**/tracked.txt\"\n"
            "      instructions: \"repository rules\"\n",
            encoding="utf-8",
        )

        user_config = self.tmp_path / "user-config.yaml"
        user_config.write_text(
            "summarize:\n"
            "  rules:\n"
            "    - match: \"**/*\"\n"
            "      instructions: \"user override\"\n",
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False):
            config = load_repository_summarize_config(repo_root)

        self.assertEqual(len(config.rules), 1)
        self.assertEqual(config.rules[0].instructions, "repository rules")


if __name__ == "__main__":
    unittest.main()

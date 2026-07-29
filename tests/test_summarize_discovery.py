from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

from ai_dev_flow.summarize_discovery import SummarizeDiscoveryError, discover_source_paths
from ai_dev_flow.summarize_glob import matches_glob


class SummarizeDiscoveryTests(unittest.TestCase):
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
        self._run_git(repo_root, "config", "user.name", "Summarize Discovery Tests")
        self._run_git(repo_root, "config", "user.email", "summarize-discovery-tests@example.com")

        return repo_root

    def test_nested_glob_and_stable_lexical_order(self) -> None:
        repo_root = self._init_repo("repo-nested-order")
        (repo_root / "zeta.py").write_text("z\n", encoding="utf-8")
        (repo_root / "alpha.py").write_text("a\n", encoding="utf-8")
        (repo_root / "nested").mkdir(parents=True)
        (repo_root / "nested" / "beta.py").write_text("b\n", encoding="utf-8")

        discovered = discover_source_paths(repo_root, "**/*.py")
        self.assertEqual(discovered, ("alpha.py", "nested/beta.py", "zeta.py"))

    def test_files_only_and_no_duplicates(self) -> None:
        repo_root = self._init_repo("repo-files-only")
        (repo_root / "folder").mkdir(parents=True)
        (repo_root / "folder" / "item.txt").write_text("x\n", encoding="utf-8")

        discovered = discover_source_paths(repo_root, "**/*")
        self.assertIn("folder/item.txt", discovered)
        self.assertNotIn("folder", discovered)
        self.assertEqual(len(discovered), len(set(discovered)))

    def test_ignored_files_excluded(self) -> None:
        repo_root = self._init_repo("repo-ignore")
        (repo_root / ".gitignore").write_text("ignored.tmp\n", encoding="utf-8")
        (repo_root / "ignored.tmp").write_text("tmp\n", encoding="utf-8")
        (repo_root / "kept.tmp").write_text("tmp\n", encoding="utf-8")

        discovered = discover_source_paths(repo_root, "**/*.tmp")
        self.assertEqual(discovered, ("kept.tmp",))

    def test_generated_directories_excluded(self) -> None:
        repo_root = self._init_repo("repo-generated")
        (repo_root / ".ai-dev" / "tasks").mkdir(parents=True)
        (repo_root / ".ai-dev" / "tasks" / "x.md").write_text("x\n", encoding="utf-8")
        (repo_root / "ai-docs" / "src").mkdir(parents=True)
        (repo_root / "ai-docs" / "src" / "summary.md").write_text("doc\n", encoding="utf-8")
        (repo_root / "artifacts").mkdir(parents=True)
        (repo_root / "artifacts" / "a.txt").write_text("artifact\n", encoding="utf-8")
        (repo_root / "src.txt").write_text("ok\n", encoding="utf-8")

        discovered = discover_source_paths(repo_root, "**/*")
        self.assertEqual(discovered, ("src.txt",))

    def test_spaces_and_unicode_paths(self) -> None:
        repo_root = self._init_repo("repo-spaces-unicode")
        (repo_root / "dir with spaces").mkdir(parents=True)
        (repo_root / "dir with spaces" / "naive.txt").write_text("space\n", encoding="utf-8")
        (repo_root / "unicodé.txt").write_text("u\n", encoding="utf-8")

        discovered = discover_source_paths(repo_root, "**/*.txt")
        self.assertEqual(discovered, ("dir with spaces/naive.txt", "unicodé.txt"))

    def test_preserves_leading_and_trailing_spaces_in_filenames(self) -> None:
        repo_root = self._init_repo("repo-filename-spaces")
        leading = " leading.txt"
        trailing = "trailing.txt "
        (repo_root / leading).write_text("lead\n", encoding="utf-8")
        (repo_root / trailing).write_text("trail\n", encoding="utf-8")

        discovered = discover_source_paths(repo_root, "*")
        self.assertEqual(discovered, (leading, trailing))

    def test_character_class_glob_support(self) -> None:
        repo_root = self._init_repo("repo-class-support")
        (repo_root / "a.py").write_text("a\n", encoding="utf-8")
        (repo_root / "b.py").write_text("b\n", encoding="utf-8")
        (repo_root / "c.py").write_text("c\n", encoding="utf-8")
        (repo_root / "nested").mkdir(parents=True)
        (repo_root / "nested" / "a.py").write_text("a\n", encoding="utf-8")
        (repo_root / "nested" / "b.py").write_text("b\n", encoding="utf-8")
        (repo_root / "nested" / "c.py").write_text("c\n", encoding="utf-8")

        self.assertEqual(
            discover_source_paths(repo_root, "**/[ab].py"),
            ("a.py", "b.py", "nested/a.py", "nested/b.py"),
        )
        self.assertEqual(
            discover_source_paths(repo_root, "**/[a-c].py"),
            ("a.py", "b.py", "c.py", "nested/a.py", "nested/b.py", "nested/c.py"),
        )
        self.assertEqual(
            discover_source_paths(repo_root, "**/[!a].py"),
            ("b.py", "c.py", "nested/b.py", "nested/c.py"),
        )

    def test_discovery_and_rule_matching_are_consistent_for_same_patterns(self) -> None:
        repo_root = self._init_repo("repo-discovery-rule-consistency")
        files = (
            "x/a.py",
            "x/b.py",
            "x/c.py",
            "y/a.py",
            "y/c.py",
            "y/z.txt",
        )
        for relative in files:
            full = repo_root / relative
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text("x\n", encoding="utf-8")

        patterns = ("**/[ab].py", "**/[a-c].py", "**/[!a].py")
        for pattern in patterns:
            discovered = discover_source_paths(repo_root, pattern)
            by_rule_match = tuple(sorted(path for path in files if matches_glob(path, pattern)))
            self.assertEqual(discovered, by_rule_match)

    def test_no_matches_raises_clear_error(self) -> None:
        repo_root = self._init_repo("repo-no-matches")
        (repo_root / "a.py").write_text("a\n", encoding="utf-8")

        with self.assertRaises(SummarizeDiscoveryError) as context:
            discover_source_paths(repo_root, "**/*.xml")

        self.assertIn("No source files matched summarize glob", str(context.exception))

    def test_absolute_glob_rejected(self) -> None:
        repo_root = self._init_repo("repo-absolute")
        (repo_root / "a.txt").write_text("a\n", encoding="utf-8")

        with self.assertRaises(SummarizeDiscoveryError) as context:
            discover_source_paths(repo_root, "/tmp/*.txt")

        self.assertIn("repository-relative", str(context.exception))

    def test_parent_traversal_glob_rejected(self) -> None:
        repo_root = self._init_repo("repo-parent-traversal")
        (repo_root / "a.txt").write_text("a\n", encoding="utf-8")

        with self.assertRaises(SummarizeDiscoveryError) as context:
            discover_source_paths(repo_root, "../*.txt")

        self.assertIn("cannot include '..'", str(context.exception))

    def test_brace_syntax_rejected(self) -> None:
        repo_root = self._init_repo("repo-brace-rejected")
        (repo_root / "a.py").write_text("a\n", encoding="utf-8")

        with self.assertRaises(SummarizeDiscoveryError) as context:
            discover_source_paths(repo_root, "**/*.{py,txt}")

        self.assertIn("brace expansion", str(context.exception))

    def test_unclosed_character_class_rejected(self) -> None:
        repo_root = self._init_repo("repo-unclosed-class")
        (repo_root / "a.py").write_text("a\n", encoding="utf-8")

        with self.assertRaises(SummarizeDiscoveryError) as context:
            discover_source_paths(repo_root, "**/[ab.py")

        self.assertIn("unclosed character class", str(context.exception))

    def test_empty_character_class_rejected(self) -> None:
        repo_root = self._init_repo("repo-empty-class")
        (repo_root / "a.py").write_text("a\n", encoding="utf-8")

        with self.assertRaises(SummarizeDiscoveryError) as context:
            discover_source_paths(repo_root, "**/[].py")

        self.assertIn("empty character class", str(context.exception))

    def test_invalid_negated_character_class_rejected(self) -> None:
        repo_root = self._init_repo("repo-invalid-negated-class")
        (repo_root / "a.py").write_text("a\n", encoding="utf-8")

        with self.assertRaises(SummarizeDiscoveryError) as context:
            discover_source_paths(repo_root, "**/[!].py")

        self.assertIn("invalid negated character class", str(context.exception))


if __name__ == "__main__":
    unittest.main()

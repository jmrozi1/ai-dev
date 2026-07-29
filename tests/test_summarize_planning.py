from __future__ import annotations

import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from ai_dev_flow import cli
from ai_dev_flow.summarize_planning import (
    build_summarize_plan,
    resolve_matching_rules,
    summary_output_path_for_source,
)


class SummarizePlanningTests(unittest.TestCase):
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
        self._run_git(repo_root, "config", "user.name", "Summarize Planning Tests")
        self._run_git(repo_root, "config", "user.email", "summarize-planning-tests@example.com")
        (repo_root / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._run_git(repo_root, "add", "tracked.txt")
        self._run_git(repo_root, "commit", "-q", "-m", "initial")

        return repo_root

    def _invoke(self, cwd: Path, *arguments: str) -> tuple[int, str, str]:
        previous_cwd = Path.cwd()
        previous_argv = list(sys.argv)
        had_command_name = "FLOW_COMMAND_NAME" in os.environ
        previous_command_name = os.environ.get("FLOW_COMMAND_NAME")

        stdout = io.StringIO()
        stderr = io.StringIO()

        os.environ["FLOW_COMMAND_NAME"] = "flow"
        sys.argv = ["flow", *arguments]
        os.chdir(cwd)

        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                try:
                    cli.run()
                except SystemExit as exc:
                    code = int(exc.code) if isinstance(exc.code, int) else 1
                else:
                    code = 0
        finally:
            os.chdir(previous_cwd)
            sys.argv = previous_argv
            if had_command_name:
                assert previous_command_name is not None
                os.environ["FLOW_COMMAND_NAME"] = previous_command_name
            else:
                os.environ.pop("FLOW_COMMAND_NAME", None)

        return code, stdout.getvalue(), stderr.getvalue()

    def test_output_path_convention_variants(self) -> None:
        self.assertEqual(summary_output_path_for_source("src/app.py"), ".ai-dev/summaries/src/app.py.md")
        self.assertEqual(summary_output_path_for_source("config/settings.prod.xml"), ".ai-dev/summaries/config/settings.prod.xml.md")
        self.assertEqual(summary_output_path_for_source("scripts/no_suffix"), ".ai-dev/summaries/scripts/no_suffix.md")
        self.assertEqual(summary_output_path_for_source("dir with spaces/a.txt"), ".ai-dev/summaries/dir with spaces/a.txt.md")

    def test_rule_precedence_generic_before_specific(self) -> None:
        repo_root = self._init_repo("repo-precedence")
        (repo_root / ".ai-dev.yaml").write_text(
            "summarize:\n"
            "  rules:\n"
            "    - match: \"**/*\"\n"
            "      instructions: \"generic\"\n"
            "    - match: \"**/config.xml\"\n"
            "      instructions: \"specific\"\n",
            encoding="utf-8",
        )

        (repo_root / "services").mkdir(parents=True)
        (repo_root / "services" / "config.xml").write_text("<x/>\n", encoding="utf-8")

        plan = build_summarize_plan(repo_root, "**/*.xml")
        self.assertEqual(plan.source_count, 1)
        self.assertEqual(plan.entries[0].instructions, ("generic", "specific"))
        self.assertEqual(plan.entries[0].matched_rule_indexes, (0, 1))

    def test_rule_tie_uses_declaration_order(self) -> None:
        repo_root = self._init_repo("repo-tie-order")
        (repo_root / ".ai-dev.yaml").write_text(
            "summarize:\n"
            "  rules:\n"
            "    - match: \"**/*.py\"\n"
            "      instructions: \"first\"\n"
            "    - match: \"src/*.py\"\n"
            "      instructions: \"second\"\n"
            "    - match: \"src/*.py\"\n"
            "      instructions: \"third\"\n",
            encoding="utf-8",
        )

        (repo_root / "src").mkdir(parents=True)
        (repo_root / "src" / "file.py").write_text("print('x')\n", encoding="utf-8")

        plan = build_summarize_plan(repo_root, "src/*.py")
        self.assertEqual(plan.entries[0].instructions, ("first", "second", "third"))
        self.assertEqual(plan.entries[0].matched_rule_indexes, (0, 1, 2))

    def test_unmatched_rules_produce_empty_instruction_list(self) -> None:
        repo_root = self._init_repo("repo-unmatched")
        (repo_root / ".ai-dev.yaml").write_text(
            "summarize:\n"
            "  rules:\n"
            "    - match: \"**/*.xml\"\n"
            "      instructions: \"xml\"\n",
            encoding="utf-8",
        )

        (repo_root / "a.py").write_text("a\n", encoding="utf-8")

        plan = build_summarize_plan(repo_root, "**/*.py")
        self.assertEqual(plan.entries[0].instructions, ())
        self.assertEqual(plan.entries[0].matched_rule_indexes, ())

    def test_platform_separator_normalization_for_matching(self) -> None:
        repo_root = self._init_repo("repo-separator")
        (repo_root / ".ai-dev.yaml").write_text(
            "summarize:\n"
            "  rules:\n"
            "    - match: \"src/**/*.py\"\n"
            "      instructions: \"python\"\n",
            encoding="utf-8",
        )

        from ai_dev_flow.summarize_config import load_repository_summarize_config

        rules = load_repository_summarize_config(repo_root).rules
        matched = resolve_matching_rules("src\\pkg\\file.py", rules)
        self.assertEqual(tuple(rule.instructions for rule in matched), ("python",))

    def test_platform_separator_normalization_with_character_class(self) -> None:
        repo_root = self._init_repo("repo-separator-class")
        (repo_root / ".ai-dev.yaml").write_text(
            "summarize:\n"
            "  rules:\n"
            "    - match: \"src/**/[ab].py\"\n"
            "      instructions: \"python\"\n",
            encoding="utf-8",
        )

        from ai_dev_flow.summarize_config import load_repository_summarize_config

        rules = load_repository_summarize_config(repo_root).rules
        matched = resolve_matching_rules("src\\pkg\\a.py", rules)
        self.assertEqual(tuple(rule.instructions for rule in matched), ("python",))

    def test_planning_preserves_filename_whitespace_exactly(self) -> None:
        repo_root = self._init_repo("repo-planning-filename-spaces")
        leading = " leading.txt"
        trailing = "trailing.txt "
        (repo_root / leading).write_text("lead\n", encoding="utf-8")
        (repo_root / trailing).write_text("trail\n", encoding="utf-8")

        plan_one = build_summarize_plan(repo_root, "*")
        plan_two = build_summarize_plan(repo_root, "*")

        self.assertEqual(plan_one, plan_two)
        by_source = {entry.source_path: entry.output_path for entry in plan_one.entries}
        self.assertEqual(by_source[leading], ".ai-dev/summaries/ leading.txt.md")
        self.assertEqual(by_source[trailing], ".ai-dev/summaries/trailing.txt .md")
        self.assertIn(leading, by_source)
        self.assertIn(trailing, by_source)
        self.assertEqual(len({entry.output_path for entry in plan_one.entries}), len(plan_one.entries))

    def test_full_deterministic_plan_and_repeatability(self) -> None:
        repo_root = self._init_repo("repo-full-plan")
        (repo_root / ".ai-dev.yaml").write_text(
            "summarize:\n"
            "  rules:\n"
            "    - match: \"**/*\"\n"
            "      instructions: \"generic\"\n"
            "    - match: \"**/*.xml\"\n"
            "      instructions: \"xml\"\n"
            "    - match: \"**/config.xml\"\n"
            "      instructions: \"config\"\n",
            encoding="utf-8",
        )

        (repo_root / "src").mkdir(parents=True)
        source_one = repo_root / "src" / "config.xml"
        source_two = repo_root / "src" / "other.xml"
        source_one.write_text("<config/>\n", encoding="utf-8")
        source_two.write_text("<other/>\n", encoding="utf-8")

        before_one = source_one.read_text(encoding="utf-8")
        before_two = source_two.read_text(encoding="utf-8")

        plan_one = build_summarize_plan(repo_root, "src/*.xml")
        plan_two = build_summarize_plan(repo_root, "src/*.xml")

        self.assertEqual(plan_one, plan_two)
        self.assertEqual(plan_one.source_count, 2)
        self.assertEqual(plan_one.rule_count, 3)
        self.assertEqual(plan_one.entries[0].source_path, "src/config.xml")
        self.assertEqual(plan_one.entries[0].output_path, ".ai-dev/summaries/src/config.xml.md")
        self.assertEqual(plan_one.entries[0].instructions, ("generic", "xml", "config"))
        self.assertEqual(plan_one.entries[1].source_path, "src/other.xml")
        self.assertEqual(plan_one.entries[1].output_path, ".ai-dev/summaries/src/other.xml.md")
        self.assertEqual(plan_one.entries[1].instructions, ("generic", "xml"))

        self.assertEqual(before_one, source_one.read_text(encoding="utf-8"))
        self.assertEqual(before_two, source_two.read_text(encoding="utf-8"))

    def test_cli_summarize_prepares_tasks(self) -> None:
        repo_root = self._init_repo("repo-cli-preparation")
        (repo_root / ".ai-dev.yaml").write_text(
            "summarize:\n"
            "  batch:\n"
            "    max_files: 10\n"
            "  rules:\n"
            "    - match: \"**/*.py\"\n"
            "      instructions: \"python summary\"\n",
            encoding="utf-8",
        )
        (repo_root / "pkg").mkdir(parents=True)
        (repo_root / "pkg" / "a.py").write_text("print('a')\n", encoding="utf-8")

        code, out, err = self._invoke(repo_root, "summarize", "pkg/*.py")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Prepared summarize tasks for plan", out)
        self.assertIn("Coordinator task: .ai-dev/tasks/summarize-", out)
        self.assertIn("Task file: .ai-dev/tasks/summarize-", out)
        self.assertNotIn("Execution task generation is not implemented in this slice.", out)
        self.assertTrue((repo_root / ".ai-dev" / "tasks").exists())


if __name__ == "__main__":
    unittest.main()

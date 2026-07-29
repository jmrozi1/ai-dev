from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ai_dev_flow.summarize_batching import build_summarize_batches
from ai_dev_flow.summarize_planning import SummarizePlanEntry, build_summarize_plan
from ai_dev_flow.summarize_task_generation import (
    SummarizeTaskGenerationError,
    plan_summarize_task_artifacts,
    prepare_summarize_task_artifacts,
    render_summarize_batch_task_markdown,
    render_expected_output_manifest_json,
    summarize_manifest_path,
)


class SummarizeTaskGenerationTests(unittest.TestCase):
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
        self._run_git(repo_root, "config", "user.name", "Summarize Task Generation Tests")
        self._run_git(repo_root, "config", "user.email", "summarize-task-generation-tests@example.com")
        (repo_root / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._run_git(repo_root, "add", "tracked.txt")
        self._run_git(repo_root, "commit", "-q", "-m", "initial")

        return repo_root

    def _extract_expected_output_manifest(self, markdown_text: str) -> dict[str, object]:
        if "## Expected Output Manifest" not in markdown_text:
            self.fail("Expected output manifest section missing")

        block = markdown_text.split("## Expected Output Manifest", 1)[1]
        if "```json\n" not in block:
            self.fail("Expected output manifest json fence missing")

        json_text = block.split("```json\n", 1)[1].split("\n```", 1)[0]
        return json.loads(json_text)

    def test_batch_task_markdown_contains_canonical_sections(self) -> None:
        repo_root = self._init_repo("repo-render-canonical")
        (repo_root / ".ai-dev.yaml").write_text(
            "summarize:\n"
            "  rules:\n"
            "    - match: \"**/*\"\n"
            "      instructions: \"generic instruction\"\n"
            "    - match: \"src/[ab].py\"\n"
            "      instructions: \"specific instruction\"\n",
            encoding="utf-8",
        )
        (repo_root / "src").mkdir(parents=True)
        (repo_root / "src" / "a.py").write_text("print('a')\n", encoding="utf-8")

        plan = build_summarize_plan(repo_root, "src/*.py")
        batches = build_summarize_batches(plan, max_files=10)
        prepared = prepare_summarize_task_artifacts(repo_root=repo_root, plan=plan, batches=batches)

        batch_path = repo_root / prepared.batch_task_paths[0]
        task_text = batch_path.read_text(encoding="utf-8")

        self.assertIn("# AI Dev Generated Task:", task_text)
        self.assertIn("- Task-Type: summarize", task_text)
        self.assertIn("- Requested-Glob: src/*.py", task_text)
        self.assertIn("## Execution Instructions", task_text)
        self.assertIn("Do not modify source files.", task_text)
        self.assertIn("## Per-Source Manifest", task_text)
        self.assertIn("- Source-Path: src/a.py", task_text)
        self.assertIn("- Output-Path: .ai-dev/summaries/src/a.py.md", task_text)
        self.assertIn("- Matched-Rule-Indexes: 0, 1", task_text)
        self.assertIn("Source: src/a.py", task_text)
        self.assertIn("Generated-By: ai-dev summarize", task_text)
        self.assertIn("Plan-ID:", task_text)
        self.assertIn("## Expected Output Manifest", task_text)
        self.assertIn('"source_path": "src/a.py"', task_text)
        self.assertIn('"output_path": ".ai-dev/summaries/src/a.py.md"', task_text)
        self.assertIn("## Completion Report Requirements", task_text)
        self.assertNotIn("print('a')", task_text)
        self.assertNotIn("click", task_text.lower())
        self.assertNotIn("chatgpt", task_text.lower())

    def test_rendering_is_deterministic(self) -> None:
        repo_root = self._init_repo("repo-render-deterministic")
        (repo_root / ".ai-dev.yaml").write_text(
            "summarize:\n"
            "  rules:\n"
            "    - match: \"**/*\"\n"
            "      instructions: \"generic\"\n",
            encoding="utf-8",
        )
        (repo_root / "src").mkdir(parents=True)
        (repo_root / "src" / "a.py").write_text("a\n", encoding="utf-8")

        plan = build_summarize_plan(repo_root, "src/*.py")
        batches = build_summarize_batches(plan, max_files=5)

        from ai_dev_flow.task_artifacts import plan_generated_task

        planned = plan_generated_task(
            repo_root=repo_root,
            task_id=batches[0].task_id,
            task_type="summarize",
            requested_command="flow summarize src/*.py",
        )

        first = render_summarize_batch_task_markdown(
            batch=batches[0],
            planned_task=planned,
            requested_glob=plan.requested_glob,
        )
        second = render_summarize_batch_task_markdown(
            batch=batches[0],
            planned_task=planned,
            requested_glob=plan.requested_glob,
        )
        self.assertEqual(first, second)
        first_json = render_expected_output_manifest_json(batches[0])
        second_json = render_expected_output_manifest_json(batches[0])
        self.assertEqual(first_json, second_json)

    def test_artifact_creation_single_batch_and_pointer(self) -> None:
        repo_root = self._init_repo("repo-artifact-single")
        (repo_root / "src.py").write_text("x\n", encoding="utf-8")

        plan = build_summarize_plan(repo_root, "*.py")
        batches = build_summarize_batches(plan, max_files=10)
        prepared = prepare_summarize_task_artifacts(repo_root=repo_root, plan=plan, batches=batches)

        self.assertEqual(prepared.batch_count, 1)
        self.assertTrue((repo_root / prepared.coordinator_task_path).exists())
        self.assertTrue((repo_root / prepared.batch_task_paths[0]).exists())
        self.assertTrue((repo_root / prepared.manifest_path).exists())

        pointer = (repo_root / ".ai-dev" / "current-task.md").read_text(encoding="utf-8")
        self.assertIn(f"Task-ID: {prepared.coordinator_task_id}", pointer)
        self.assertIn(f"Task-File: {prepared.coordinator_task_path}", pointer)

    def test_artifact_creation_multiple_batches_and_manifest(self) -> None:
        repo_root = self._init_repo("repo-artifact-multi")
        for index in range(5):
            (repo_root / f"src{index}.py").write_text("x\n", encoding="utf-8")

        plan = build_summarize_plan(repo_root, "*.py")
        batches = build_summarize_batches(plan, max_files=2)
        prepared = prepare_summarize_task_artifacts(repo_root=repo_root, plan=plan, batches=batches)

        self.assertEqual(prepared.batch_count, 3)
        self.assertEqual(len(prepared.batch_task_paths), 3)

        coordinator_text = (repo_root / prepared.coordinator_task_path).read_text(encoding="utf-8")
        self.assertIn("## Ordered Batch Tasks", coordinator_text)
        for task_path in prepared.batch_task_paths:
            self.assertIn(task_path, coordinator_text)

        manifest_data = json.loads((repo_root / prepared.manifest_path).read_text(encoding="utf-8"))
        self.assertEqual(manifest_data["plan_id"], plan.plan_id)
        self.assertEqual(manifest_data["coordinator_task"], prepared.coordinator_task_path)
        self.assertEqual(tuple(manifest_data["batch_tasks"]), prepared.batch_task_paths)
        self.assertEqual(len(manifest_data["batches"]), 3)

    def test_existing_task_collision_rejected(self) -> None:
        repo_root = self._init_repo("repo-artifact-collision")
        (repo_root / "a.py").write_text("a\n", encoding="utf-8")
        plan = build_summarize_plan(repo_root, "*.py")
        batches = build_summarize_batches(plan, max_files=10)

        first = prepare_summarize_task_artifacts(repo_root=repo_root, plan=plan, batches=batches)
        self.assertTrue((repo_root / first.coordinator_task_path).exists())

        with self.assertRaises(SummarizeTaskGenerationError) as context:
            prepare_summarize_task_artifacts(repo_root=repo_root, plan=plan, batches=batches)

        self.assertIn("Cannot overwrite immutable task file", str(context.exception))

    def test_existing_manifest_collision_rejected_without_writes(self) -> None:
        repo_root = self._init_repo("repo-artifact-manifest-collision")
        (repo_root / "a.py").write_text("a\n", encoding="utf-8")
        plan = build_summarize_plan(repo_root, "*.py")
        batches = build_summarize_batches(plan, max_files=10)
        planned = plan_summarize_task_artifacts(repo_root=repo_root, plan=plan, batches=batches)

        pointer_path = repo_root / ".ai-dev" / "current-task.md"
        pointer_path.parent.mkdir(parents=True, exist_ok=True)
        pointer_path.write_text("old pointer\n", encoding="utf-8")

        manifest_path = repo_root / summarize_manifest_path(plan.plan_id)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text('{"existing": true}\n', encoding="utf-8")

        with self.assertRaises(SummarizeTaskGenerationError) as context:
            prepare_summarize_task_artifacts(
                repo_root=repo_root,
                plan=plan,
                batches=batches,
                planned_artifacts=planned,
            )

        self.assertIn("Cannot overwrite immutable summarize manifest", str(context.exception))
        self.assertIn(summarize_manifest_path(plan.plan_id), str(context.exception))
        self.assertEqual(pointer_path.read_text(encoding="utf-8"), "old pointer\n")
        self.assertFalse((repo_root / planned.coordinator_planned.repository_relative_path).exists())
        for planned_batch in planned.batch_plans:
            self.assertFalse((repo_root / planned_batch.repository_relative_path).exists())

    def test_pointer_write_failure_rolls_back_all_created_artifacts(self) -> None:
        repo_root = self._init_repo("repo-artifact-pointer-rollback")
        for index in range(3):
            (repo_root / f"file-{index}.py").write_text("x\n", encoding="utf-8")

        plan = build_summarize_plan(repo_root, "*.py")
        batches = build_summarize_batches(plan, max_files=2)

        pointer_path = repo_root / ".ai-dev" / "current-task.md"
        pointer_path.parent.mkdir(parents=True, exist_ok=True)
        pointer_path.write_text("old pointer\n", encoding="utf-8")

        from ai_dev_flow import summarize_task_generation as module

        original_write = module.write_text_atomic

        def fail_pointer_write(path: Path, text: str) -> None:
            if path.name == "current-task.md":
                raise module.JsonFileError("Cannot write pointer")
            original_write(path, text)

        with patch("ai_dev_flow.summarize_task_generation.write_text_atomic", side_effect=fail_pointer_write):
            with self.assertRaises(SummarizeTaskGenerationError) as context:
                prepare_summarize_task_artifacts(repo_root=repo_root, plan=plan, batches=batches)

        self.assertIn("Rolled back summarize task preparation", str(context.exception))
        self.assertEqual(pointer_path.read_text(encoding="utf-8"), "old pointer\n")
        for batch in batches:
            self.assertFalse((repo_root / f".ai-dev/tasks/{batch.task_id}.md").exists())
        self.assertFalse((repo_root / f".ai-dev/tasks/summarize-{plan.plan_id}-coordinator.md").exists())
        self.assertFalse((repo_root / f".ai-dev/summarize/{plan.plan_id}/manifest.json").exists())

    def test_paths_with_spaces_unicode_and_trailing_whitespace_preserved(self) -> None:
        repo_root = self._init_repo("repo-artifact-special-paths")
        names = (
            " leading.txt",
            "trailing.txt ",
            "unicodé file.txt",
            "dir with spaces/child.txt",
        )
        for name in names:
            path = repo_root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x\n", encoding="utf-8")

        plan = build_summarize_plan(repo_root, "*")
        batches = build_summarize_batches(plan, max_files=10)
        prepared = prepare_summarize_task_artifacts(repo_root=repo_root, plan=plan, batches=batches)

        batch_text = (repo_root / prepared.batch_task_paths[0]).read_text(encoding="utf-8")
        self.assertIn("Source-Path:  leading.txt", batch_text)
        self.assertIn("Source-Path: trailing.txt ", batch_text)
        self.assertIn("Output-Path: .ai-dev/summaries/ leading.txt.md", batch_text)
        self.assertIn("Output-Path: .ai-dev/summaries/trailing.txt .md", batch_text)
        self.assertIn("Source-Path: unicodé file.txt", batch_text)

    def test_expected_output_manifest_json_parses_and_preserves_special_filenames(self) -> None:
        repo_root = self._init_repo("repo-artifact-manifest-json-special")
        names = (
            'src/weird"name.py',
            "src/unicodé.py",
            "src/back\\slash.py",
        )
        for name in names:
            path = repo_root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x\n", encoding="utf-8")

        (repo_root / ".ai-dev.yaml").write_text(
            "summarize:\n"
            "  rules:\n"
            "    - match: \"src/*\"\n"
            "      instructions: \"test rule\"\n",
            encoding="utf-8",
        )

        plan = build_summarize_plan(repo_root, "src/*")
        batches = build_summarize_batches(plan, max_files=10)

        from ai_dev_flow.task_artifacts import plan_generated_task

        planned = plan_generated_task(
            repo_root=repo_root,
            task_id=batches[0].task_id,
            task_type="summarize",
            requested_command="flow summarize src/*",
        )

        first = render_summarize_batch_task_markdown(
            batch=batches[0],
            planned_task=planned,
            requested_glob=plan.requested_glob,
        )
        second = render_summarize_batch_task_markdown(
            batch=batches[0],
            planned_task=planned,
            requested_glob=plan.requested_glob,
        )
        self.assertEqual(first, second)

        parsed = self._extract_expected_output_manifest(first)
        self.assertEqual(parsed["plan_id"], plan.plan_id)
        self.assertEqual(parsed["batch_index"], batches[0].batch_index)

        expected_pairs = [
            {
                "source_path": entry.source_path,
                "output_path": entry.output_path,
            }
            for entry in batches[0].entries
        ]
        self.assertEqual(parsed["outputs"], expected_pairs)
        self.assertIn('src/weird"name.py', [item["source_path"] for item in parsed["outputs"]])
        self.assertIn("src/unicodé.py", [item["source_path"] for item in parsed["outputs"]])

    def test_expected_output_manifest_json_preserves_backslashes_in_direct_render(self) -> None:
        repo_root = self._init_repo("repo-artifact-manifest-json-backslash-direct")

        from ai_dev_flow.summarize_batching import SummarizeBatch
        from ai_dev_flow.task_artifacts import plan_generated_task

        entry = SummarizePlanEntry(
            source_path="src\\win\\name.py",
            output_path=".ai-dev/summaries/src\\win\\name.py.md",
            instructions=("rule",),
            matched_rule_indexes=(0,),
        )
        batch = SummarizeBatch(
            plan_id="plan123",
            batch_index=1,
            batch_count=1,
            entries=(entry,),
            batch_id="batch123",
            task_id="summarize-plan123-batch-001",
            expected_output_paths=(entry.output_path,),
            source_count=1,
        )
        planned = plan_generated_task(
            repo_root=repo_root,
            task_id=batch.task_id,
            task_type="summarize",
            requested_command="flow summarize src/*",
        )

        markdown = render_summarize_batch_task_markdown(
            batch=batch,
            planned_task=planned,
            requested_glob="src/*",
        )
        parsed = self._extract_expected_output_manifest(markdown)
        self.assertEqual(
            parsed["outputs"],
            [
                {
                    "source_path": "src\\win\\name.py",
                    "output_path": ".ai-dev/summaries/src\\win\\name.py.md",
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()

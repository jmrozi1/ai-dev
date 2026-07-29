from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ai_dev_flow.summarize_batching import SummarizeBatchingError, build_summarize_batches
from ai_dev_flow.summarize_config import (
    DEFAULT_SUMMARIZE_BATCH_MAX_FILES,
    SummarizeConfigError,
    load_repository_summarize_config,
)
from ai_dev_flow.summarize_planning import build_summarize_plan


class SummarizeBatchingTests(unittest.TestCase):
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
        self._run_git(repo_root, "config", "user.name", "Summarize Batching Tests")
        self._run_git(repo_root, "config", "user.email", "summarize-batching-tests@example.com")
        (repo_root / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._run_git(repo_root, "add", "tracked.txt")
        self._run_git(repo_root, "commit", "-q", "-m", "initial")

        return repo_root

    def _write_sources(self, repo_root: Path, file_count: int) -> tuple[str, ...]:
        names: list[str] = []
        for index in range(file_count):
            name = f"src/file-{index:02d}.py"
            target = repo_root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"print({index})\n", encoding="utf-8")
            names.append(name)
        return tuple(names)

    def test_batch_config_default_when_absent(self) -> None:
        repo_root = self._init_repo("repo-batch-default")

        config = load_repository_summarize_config(repo_root)
        self.assertEqual(config.batch_max_files, DEFAULT_SUMMARIZE_BATCH_MAX_FILES)

    def test_batch_config_valid_max_files(self) -> None:
        repo_root = self._init_repo("repo-batch-valid")
        (repo_root / ".ai-dev.yaml").write_text(
            "summarize:\n"
            "  batch:\n"
            "    max_files: 7\n"
            "  rules: []\n",
            encoding="utf-8",
        )

        config = load_repository_summarize_config(repo_root)
        self.assertEqual(config.batch_max_files, 7)

    def test_batch_config_zero_rejected(self) -> None:
        repo_root = self._init_repo("repo-batch-zero")
        path = repo_root / ".ai-dev.yaml"
        path.write_text("summarize:\n  batch:\n    max_files: 0\n", encoding="utf-8")

        with self.assertRaises(SummarizeConfigError) as context:
            load_repository_summarize_config(repo_root)

        self.assertIn(str(path), str(context.exception))
        self.assertIn("summarize.batch.max_files", str(context.exception))

    def test_batch_config_negative_rejected(self) -> None:
        repo_root = self._init_repo("repo-batch-negative")
        path = repo_root / ".ai-dev.yaml"
        path.write_text("summarize:\n  batch:\n    max_files: -1\n", encoding="utf-8")

        with self.assertRaises(SummarizeConfigError) as context:
            load_repository_summarize_config(repo_root)

        self.assertIn(str(path), str(context.exception))
        self.assertIn("summarize.batch.max_files", str(context.exception))

    def test_batch_config_bool_rejected(self) -> None:
        repo_root = self._init_repo("repo-batch-bool")
        path = repo_root / ".ai-dev.yaml"
        path.write_text("summarize:\n  batch:\n    max_files: true\n", encoding="utf-8")

        with self.assertRaises(SummarizeConfigError) as context:
            load_repository_summarize_config(repo_root)

        self.assertIn(str(path), str(context.exception))
        self.assertIn("expected integer greater than zero", str(context.exception))

    def test_batch_config_non_integer_rejected(self) -> None:
        repo_root = self._init_repo("repo-batch-string")
        path = repo_root / ".ai-dev.yaml"
        path.write_text("summarize:\n  batch:\n    max_files: bad\n", encoding="utf-8")

        with self.assertRaises(SummarizeConfigError) as context:
            load_repository_summarize_config(repo_root)

        self.assertIn(str(path), str(context.exception))
        self.assertIn("summarize.batch.max_files", str(context.exception))

    def test_batch_config_unknown_key_rejected(self) -> None:
        repo_root = self._init_repo("repo-batch-unknown")
        path = repo_root / ".ai-dev.yaml"
        path.write_text("summarize:\n  batch:\n    max_files: 5\n    extra: 1\n", encoding="utf-8")

        with self.assertRaises(SummarizeConfigError) as context:
            load_repository_summarize_config(repo_root)

        self.assertIn(str(path), str(context.exception))
        self.assertIn("summarize.batch", str(context.exception))
        self.assertIn("unknown key(s): extra", str(context.exception))

    def test_user_config_cannot_override_batch_behavior(self) -> None:
        repo_root = self._init_repo("repo-batch-user-override")
        (repo_root / ".ai-dev.yaml").write_text(
            "summarize:\n"
            "  batch:\n"
            "    max_files: 3\n",
            encoding="utf-8",
        )

        user_config = self.tmp_path / "user-config.yaml"
        user_config.write_text(
            "summarize:\n"
            "  batch:\n"
            "    max_files: 99\n",
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False):
            config = load_repository_summarize_config(repo_root)

        self.assertEqual(config.batch_max_files, 3)

    def test_batching_single_file(self) -> None:
        repo_root = self._init_repo("repo-batch-single")
        self._write_sources(repo_root, 1)

        plan = build_summarize_plan(repo_root, "src/*.py")
        batches = build_summarize_batches(plan, max_files=5)

        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0].source_count, 1)

    def test_batching_below_limit(self) -> None:
        repo_root = self._init_repo("repo-batch-below")
        self._write_sources(repo_root, 3)

        plan = build_summarize_plan(repo_root, "src/*.py")
        batches = build_summarize_batches(plan, max_files=10)

        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0].source_count, 3)

    def test_batching_exact_limit(self) -> None:
        repo_root = self._init_repo("repo-batch-exact")
        self._write_sources(repo_root, 4)

        plan = build_summarize_plan(repo_root, "src/*.py")
        batches = build_summarize_batches(plan, max_files=4)

        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0].source_count, 4)

    def test_batching_one_over_limit(self) -> None:
        repo_root = self._init_repo("repo-batch-one-over")
        self._write_sources(repo_root, 5)

        plan = build_summarize_plan(repo_root, "src/*.py")
        batches = build_summarize_batches(plan, max_files=4)

        self.assertEqual(len(batches), 2)
        self.assertEqual(batches[0].source_count, 4)
        self.assertEqual(batches[1].source_count, 1)

    def test_batching_multiple_batches_repeatable_ids_and_order(self) -> None:
        repo_root = self._init_repo("repo-batch-repeatable")
        sources = self._write_sources(repo_root, 9)

        plan = build_summarize_plan(repo_root, "src/*.py")
        first = build_summarize_batches(plan, max_files=3)
        second = build_summarize_batches(plan, max_files=3)

        self.assertEqual(first, second)
        self.assertEqual(len(first), 3)

        flattened_sources = tuple(entry.source_path for batch in first for entry in batch.entries)
        self.assertEqual(flattened_sources, tuple(sorted(sources)))
        self.assertEqual(len(flattened_sources), len(set(flattened_sources)))

        ids = {batch.batch_id for batch in first}
        task_ids = {batch.task_id for batch in first}
        self.assertEqual(len(ids), len(first))
        self.assertEqual(len(task_ids), len(first))

    def test_changing_only_batch_size_changes_boundaries_deterministically(self) -> None:
        repo_root = self._init_repo("repo-batch-size-change")
        self._write_sources(repo_root, 8)

        plan = build_summarize_plan(repo_root, "src/*.py")
        by_three = build_summarize_batches(plan, max_files=3)
        by_four = build_summarize_batches(plan, max_files=4)

        self.assertEqual(tuple(batch.source_count for batch in by_three), (3, 3, 2))
        self.assertEqual(tuple(batch.source_count for batch in by_four), (4, 4))

        by_three_repeat = build_summarize_batches(plan, max_files=3)
        by_four_repeat = build_summarize_batches(plan, max_files=4)
        self.assertEqual(by_three, by_three_repeat)
        self.assertEqual(by_four, by_four_repeat)

    def test_invalid_max_files_rejected_by_batcher(self) -> None:
        repo_root = self._init_repo("repo-batch-invalid-runtime")
        self._write_sources(repo_root, 2)
        plan = build_summarize_plan(repo_root, "src/*.py")

        with self.assertRaises(SummarizeBatchingError):
            build_summarize_batches(plan, max_files=0)

        with self.assertRaises(SummarizeBatchingError):
            build_summarize_batches(plan, max_files=True)


if __name__ == "__main__":
    unittest.main()

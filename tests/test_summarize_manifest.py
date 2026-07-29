from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

from ai_dev_flow.summarize_batching import build_summarize_batches
from ai_dev_flow.summarize_manifest import SummarizeManifestError, load_summarize_manifest, parse_summarize_manifest
from ai_dev_flow.summarize_planning import build_summarize_plan
from ai_dev_flow.summarize_task_generation import prepare_summarize_task_artifacts


class SummarizeManifestTests(unittest.TestCase):
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
        self._run_git(repo_root, "config", "user.name", "Summarize Manifest Tests")
        self._run_git(repo_root, "config", "user.email", "summarize-manifest-tests@example.com")
        (repo_root / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._run_git(repo_root, "add", "tracked.txt")
        self._run_git(repo_root, "commit", "-q", "-m", "initial")

        return repo_root

    def _valid_manifest_data(self, *, plan_id: str = "plan-123") -> dict[str, object]:
        return {
            "schema_version": 2,
            "plan_id": plan_id,
            "requested_glob": "src/*.py",
            "coordinator_task": f".ai-dev/tasks/summarize-{plan_id}-coordinator.md",
            "batch_tasks": [f".ai-dev/tasks/summarize-{plan_id}-batch-001.md"],
            "batches": [
                {
                    "batch_index": 1,
                    "batch_count": 1,
                    "batch_id": "batchid",
                    "task_id": f"summarize-{plan_id}-batch-001",
                    "task_file": f".ai-dev/tasks/summarize-{plan_id}-batch-001.md",
                    "source_count": 1,
                    "entries": [
                        {
                            "source_path": "src/a.py",
                            "output_path": ".ai-dev/summaries/src/a.py.md",
                            "source_digest_sha256": "a" * 64,
                            "source_size_bytes": 10,
                            "batch_index": 1,
                            "matched_rule_indexes": [0],
                        }
                    ],
                }
            ],
        }

    def test_manifest_contains_source_digest_size_and_special_paths(self) -> None:
        repo_root = self._init_repo("repo-manifest-snapshots")
        (repo_root / ".ai-dev.yaml").write_text(
            "summarize:\n"
            "  batch:\n"
            "    max_files: 10\n"
            "  rules:\n"
            "    - match: \"**/*\"\n"
            "      instructions: \"rule\"\n",
            encoding="utf-8",
        )

        names = (
            'src/weird"name.py',
            "src/unicodé.py",
            "src/space file.py",
        )
        for name in names:
            target = repo_root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"{name}\n", encoding="utf-8")

        plan = build_summarize_plan(repo_root, "src/*")
        batches = build_summarize_batches(plan, max_files=10)
        prepare_summarize_task_artifacts(repo_root=repo_root, plan=plan, batches=batches)

        manifest = load_summarize_manifest(repo_root, plan.plan_id)

        self.assertEqual(manifest.plan_id, plan.plan_id)
        self.assertEqual(len(manifest.entries), 3)
        source_paths = [entry.source_path for entry in manifest.entries]
        self.assertIn('src/weird"name.py', source_paths)
        self.assertIn("src/unicodé.py", source_paths)

        for entry in manifest.entries:
            self.assertEqual(len(entry.source_digest_sha256), 64)
            source_bytes = (repo_root / entry.source_path).read_bytes()
            self.assertEqual(entry.source_size_bytes, len(source_bytes))
            self.assertTrue(entry.output_path.startswith(".ai-dev/summaries/"))

    def test_load_manifest_rejects_invalid_json(self) -> None:
        repo_root = self._init_repo("repo-manifest-invalid-json")
        plan_id = "deadbeefcafebabe"
        path = repo_root / ".ai-dev" / "summarize" / plan_id / "manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{bad json\n", encoding="utf-8")

        with self.assertRaises(SummarizeManifestError) as context:
            load_summarize_manifest(repo_root, plan_id)

        self.assertIn("Invalid JSON", str(context.exception))

    def test_manifest_rejects_path_traversal(self) -> None:
        data = self._valid_manifest_data()
        batch = data["batches"][0]
        assert isinstance(batch, dict)
        entries = batch["entries"]
        assert isinstance(entries, list)
        entry = entries[0]
        assert isinstance(entry, dict)
        entry["output_path"] = "../escape.md"

        with self.assertRaises(SummarizeManifestError) as context:
            parse_summarize_manifest(
                manifest_data=data,
                expected_plan_id="plan-123",
                manifest_relative_path=".ai-dev/summarize/plan-123/manifest.json",
                manifest_absolute_path=Path("/tmp/manifest.json"),
            )

        self.assertIn("must be repository-relative", str(context.exception))

    def test_manifest_rejects_duplicate_source_or_output(self) -> None:
        data = self._valid_manifest_data()
        batch = data["batches"][0]
        assert isinstance(batch, dict)
        entries = batch["entries"]
        assert isinstance(entries, list)
        duplicate = dict(entries[0])
        duplicate["output_path"] = ".ai-dev/summaries/src/b.py.md"
        entries.append(duplicate)
        batch["source_count"] = 2

        with self.assertRaises(SummarizeManifestError) as context:
            parse_summarize_manifest(
                manifest_data=data,
                expected_plan_id="plan-123",
                manifest_relative_path=".ai-dev/summarize/plan-123/manifest.json",
                manifest_absolute_path=Path("/tmp/manifest.json"),
            )

        self.assertIn("duplicate source_path", str(context.exception))

        entries[1]["source_path"] = "src/b.py"
        entries[1]["output_path"] = entries[0]["output_path"]

        with self.assertRaises(SummarizeManifestError) as context_two:
            parse_summarize_manifest(
                manifest_data=data,
                expected_plan_id="plan-123",
                manifest_relative_path=".ai-dev/summarize/plan-123/manifest.json",
                manifest_absolute_path=Path("/tmp/manifest.json"),
            )

        self.assertIn("duplicate output_path", str(context_two.exception))

    def test_manifest_rejects_mismatched_plan_id(self) -> None:
        data = self._valid_manifest_data(plan_id="plan-a")

        with self.assertRaises(SummarizeManifestError) as context:
            parse_summarize_manifest(
                manifest_data=data,
                expected_plan_id="plan-b",
                manifest_relative_path=".ai-dev/summarize/plan-b/manifest.json",
                manifest_absolute_path=Path("/tmp/manifest.json"),
            )

        self.assertIn("does not match requested plan", str(context.exception))

    def test_manifest_rejects_incoherent_batch_metadata(self) -> None:
        data = self._valid_manifest_data(plan_id="plan-z")
        batch = data["batches"][0]
        assert isinstance(batch, dict)
        batch["batch_count"] = 2

        with self.assertRaises(SummarizeManifestError) as context:
            parse_summarize_manifest(
                manifest_data=data,
                expected_plan_id="plan-z",
                manifest_relative_path=".ai-dev/summarize/plan-z/manifest.json",
                manifest_absolute_path=Path("/tmp/manifest.json"),
            )

        self.assertIn("batch_count", str(context.exception))


if __name__ == "__main__":
    unittest.main()

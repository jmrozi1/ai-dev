from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from ai_dev_flow.summarize_batching import build_summarize_batches
from ai_dev_flow.summarize_manifest import load_summarize_manifest
from ai_dev_flow.summarize_planning import build_summarize_plan
from ai_dev_flow.summarize_task_generation import prepare_summarize_task_artifacts
from ai_dev_flow.summarize_verification import (
    BATCH_STATUS_PARTIAL,
    BATCH_STATUS_UNTOUCHED,
    OUTPUT_STATUS_EMPTY,
    OUTPUT_STATUS_MISSING_GENERATOR_MARKER,
    OUTPUT_STATUS_STALE,
    OUTPUT_STATUS_UNREADABLE,
    OUTPUT_STATUS_WRONG_PLAN_MARKER,
    OUTPUT_STATUS_WRONG_SOURCE_MARKER,
    OVERALL_STATUS_COMPLETE,
    OVERALL_STATUS_PARTIAL,
    OVERALL_STATUS_STALE,
    SOURCE_STATUS_CHANGED,
    SOURCE_STATUS_MISSING,
    SOURCE_STATUS_NOT_REGULAR,
    render_verification_markdown,
    verification_result_json,
    verify_summarize_plan,
    write_verification_artifacts,
)


class SummarizeVerificationTests(unittest.TestCase):
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
        self._run_git(repo_root, "config", "user.name", "Summarize Verification Tests")
        self._run_git(repo_root, "config", "user.email", "summarize-verification-tests@example.com")
        (repo_root / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._run_git(repo_root, "add", "tracked.txt")
        self._run_git(repo_root, "commit", "-q", "-m", "initial")

        return repo_root

    def _prepare_manifest(self, repo_root: Path, *, source_count: int, max_files: int = 2):
        (repo_root / ".ai-dev.yaml").write_text(
            "summarize:\n"
            "  batch:\n"
            f"    max_files: {max_files}\n"
            "  rules:\n"
            "    - match: \"src/*.py\"\n"
            "      instructions: \"rule\"\n",
            encoding="utf-8",
        )

        src = repo_root / "src"
        src.mkdir(parents=True, exist_ok=True)
        for index in range(source_count):
            (src / f"file-{index:02d}.py").write_text(f"print({index})\n", encoding="utf-8")

        plan = build_summarize_plan(repo_root, "src/*.py")
        batches = build_summarize_batches(plan, max_files=max_files)
        prepare_summarize_task_artifacts(repo_root=repo_root, plan=plan, batches=batches)
        return load_summarize_manifest(repo_root, plan.plan_id)

    def _write_valid_output(self, repo_root: Path, *, source_path: str, output_path: str, plan_id: str) -> None:
        target = repo_root / output_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "# Summary\n\n"
            f"Source: {source_path}\n"
            "Generated-By: ai-dev summarize\n"
            f"Plan-ID: {plan_id}\n\n"
            "Content: ok\n",
            encoding="utf-8",
        )

    def test_verify_complete_and_deterministic_markdown(self) -> None:
        repo_root = self._init_repo("repo-verify-complete")
        manifest = self._prepare_manifest(repo_root, source_count=2)

        for entry in manifest.entries:
            self._write_valid_output(
                repo_root,
                source_path=entry.source_path,
                output_path=entry.output_path,
                plan_id=manifest.plan_id,
            )

        result = verify_summarize_plan(repo_root, manifest)
        self.assertEqual(result.overall_status, OVERALL_STATUS_COMPLETE)
        self.assertEqual(result.valid_output_count, 2)
        self.assertEqual(result.unexpected_output_count, 0)

        first = render_verification_markdown(result)
        second = render_verification_markdown(result)
        self.assertEqual(first, second)
        self.assertIn("Overall-Status: complete", first)

    def test_partial_and_untouched_batch_accounting(self) -> None:
        repo_root = self._init_repo("repo-verify-partial-untouched")
        manifest = self._prepare_manifest(repo_root, source_count=3, max_files=2)

        first_entry = manifest.entries[0]
        self._write_valid_output(
            repo_root,
            source_path=first_entry.source_path,
            output_path=first_entry.output_path,
            plan_id=manifest.plan_id,
        )

        result = verify_summarize_plan(repo_root, manifest)
        self.assertEqual(result.overall_status, OVERALL_STATUS_PARTIAL)
        statuses = {batch.batch_index: batch.status for batch in result.batch_states}
        self.assertEqual(statuses[1], BATCH_STATUS_PARTIAL)
        self.assertEqual(statuses[2], BATCH_STATUS_UNTOUCHED)

    def test_malformed_output_statuses(self) -> None:
        repo_root = self._init_repo("repo-verify-malformed")
        manifest = self._prepare_manifest(repo_root, source_count=4, max_files=4)

        # empty
        (repo_root / manifest.entries[0].output_path).parent.mkdir(parents=True, exist_ok=True)
        (repo_root / manifest.entries[0].output_path).write_text("\n\n", encoding="utf-8")

        # invalid utf-8
        output_two = repo_root / manifest.entries[1].output_path
        output_two.parent.mkdir(parents=True, exist_ok=True)
        output_two.write_bytes(b"\xff\xfe\xfa")

        # wrong source marker and missing generator
        output_three = repo_root / manifest.entries[2].output_path
        output_three.parent.mkdir(parents=True, exist_ok=True)
        output_three.write_text(
            "# Summary\n\n"
            "Source: src/not-the-source.py\n"
            f"Plan-ID: {manifest.plan_id}\n",
            encoding="utf-8",
        )

        # wrong plan marker
        output_four = repo_root / manifest.entries[3].output_path
        output_four.parent.mkdir(parents=True, exist_ok=True)
        output_four.write_text(
            "# Summary\n\n"
            f"Source: {manifest.entries[3].source_path}\n"
            "Generated-By: ai-dev summarize\n"
            "Plan-ID: some-other-plan\n",
            encoding="utf-8",
        )

        result = verify_summarize_plan(repo_root, manifest)
        status_by_output = {item.output_path: item.status for item in result.output_states}

        self.assertEqual(status_by_output[manifest.entries[0].output_path], OUTPUT_STATUS_EMPTY)
        self.assertEqual(status_by_output[manifest.entries[1].output_path], OUTPUT_STATUS_UNREADABLE)
        self.assertEqual(status_by_output[manifest.entries[2].output_path], OUTPUT_STATUS_WRONG_SOURCE_MARKER)
        self.assertEqual(status_by_output[manifest.entries[3].output_path], OUTPUT_STATUS_WRONG_PLAN_MARKER)

    def test_source_changed_marks_stale(self) -> None:
        repo_root = self._init_repo("repo-verify-source-changed")
        manifest = self._prepare_manifest(repo_root, source_count=1)
        entry = manifest.entries[0]

        self._write_valid_output(
            repo_root,
            source_path=entry.source_path,
            output_path=entry.output_path,
            plan_id=manifest.plan_id,
        )

        (repo_root / entry.source_path).write_text("changed\n", encoding="utf-8")

        result = verify_summarize_plan(repo_root, manifest)
        self.assertEqual(result.overall_status, OVERALL_STATUS_STALE)
        self.assertEqual(result.source_states[0].status, SOURCE_STATUS_CHANGED)
        self.assertEqual(result.output_states[0].status, OUTPUT_STATUS_STALE)

    def test_source_missing_and_source_replaced_directory(self) -> None:
        repo_root = self._init_repo("repo-verify-source-missing")
        manifest = self._prepare_manifest(repo_root, source_count=2, max_files=2)

        for entry in manifest.entries:
            self._write_valid_output(
                repo_root,
                source_path=entry.source_path,
                output_path=entry.output_path,
                plan_id=manifest.plan_id,
            )

        first_source = repo_root / manifest.entries[0].source_path
        first_source.unlink()

        second_source = repo_root / manifest.entries[1].source_path
        second_source.unlink()
        second_source.mkdir(parents=True)

        result = verify_summarize_plan(repo_root, manifest)
        self.assertEqual(result.overall_status, OVERALL_STATUS_STALE)
        states = {item.source_path: item.status for item in result.source_states}
        self.assertEqual(states[manifest.entries[0].source_path], SOURCE_STATUS_MISSING)
        self.assertEqual(states[manifest.entries[1].source_path], SOURCE_STATUS_NOT_REGULAR)

    def test_unexpected_output_detection(self) -> None:
        repo_root = self._init_repo("repo-verify-unexpected")
        manifest = self._prepare_manifest(repo_root, source_count=1)
        entry = manifest.entries[0]

        self._write_valid_output(
            repo_root,
            source_path=entry.source_path,
            output_path=entry.output_path,
            plan_id=manifest.plan_id,
        )

        unexpected = repo_root / ".ai-dev" / "summaries" / "extra.md"
        unexpected.parent.mkdir(parents=True, exist_ok=True)
        unexpected.write_text(
            "# Summary\n\nSource: src/x.py\nGenerated-By: ai-dev summarize\nPlan-ID: other\n",
            encoding="utf-8",
        )

        result = verify_summarize_plan(repo_root, manifest)
        self.assertEqual(len(result.unexpected_outputs), 1)
        self.assertEqual(result.unexpected_outputs[0].output_path, ".ai-dev/summaries/extra.md")
        self.assertEqual(result.unexpected_outputs[0].detected_plan_id, "other")
        self.assertEqual(result.unexpected_output_count, 1)
        self.assertEqual(result.overall_status, OVERALL_STATUS_PARTIAL)
        self.assertIn("unexpected summary outputs", result.recommended_next_action)

        payload = verification_result_json(result)
        self.assertEqual(payload["overall_status"], OVERALL_STATUS_PARTIAL)

        markdown = render_verification_markdown(result)
        self.assertIn("Overall-Status: partial", markdown)

        unexpected.unlink()
        complete_result = verify_summarize_plan(repo_root, manifest)
        self.assertEqual(complete_result.unexpected_output_count, 0)
        self.assertEqual(complete_result.overall_status, OVERALL_STATUS_COMPLETE)

    def test_generator_marker_variants(self) -> None:
        repo_root = self._init_repo("repo-verify-generator-markers")
        manifest = self._prepare_manifest(repo_root, source_count=1)
        entry = manifest.entries[0]
        output_path = repo_root / entry.output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # exact single marker passes
        output_path.write_text(
            "# Summary\n\n"
            f"Source: {entry.source_path}\n"
            "Generated-By: ai-dev summarize\n"
            f"Plan-ID: {manifest.plan_id}\n",
            encoding="utf-8",
        )
        result_single = verify_summarize_plan(repo_root, manifest)
        self.assertEqual(result_single.output_states[0].status, "valid")

        # duplicated identical markers pass
        output_path.write_text(
            "# Summary\n\n"
            f"Source: {entry.source_path}\n"
            "Generated-By: ai-dev summarize\n"
            "Generated-By: ai-dev summarize\n"
            f"Plan-ID: {manifest.plan_id}\n",
            encoding="utf-8",
        )
        result_dupe = verify_summarize_plan(repo_root, manifest)
        self.assertEqual(result_dupe.output_states[0].status, "valid")

        # required marker plus conflicting marker fails
        output_path.write_text(
            "# Summary\n\n"
            f"Source: {entry.source_path}\n"
            "Generated-By: ai-dev summarize\n"
            "Generated-By: something-else\n"
            f"Plan-ID: {manifest.plan_id}\n",
            encoding="utf-8",
        )
        result_conflict_after = verify_summarize_plan(repo_root, manifest)
        self.assertEqual(result_conflict_after.output_states[0].status, OUTPUT_STATUS_MISSING_GENERATOR_MARKER)
        self.assertEqual(
            result_conflict_after.output_states[0].reason_codes,
            ("inconsistent-generator-marker",),
        )

        # conflicting marker before required marker fails
        output_path.write_text(
            "# Summary\n\n"
            f"Source: {entry.source_path}\n"
            "Generated-By: other\n"
            "Generated-By: ai-dev summarize\n"
            f"Plan-ID: {manifest.plan_id}\n",
            encoding="utf-8",
        )
        result_conflict_before = verify_summarize_plan(repo_root, manifest)
        self.assertEqual(result_conflict_before.output_states[0].status, OUTPUT_STATUS_MISSING_GENERATOR_MARKER)
        self.assertEqual(
            result_conflict_before.output_states[0].reason_codes,
            ("inconsistent-generator-marker",),
        )

        # only incorrect generator marker fails
        output_path.write_text(
            "# Summary\n\n"
            f"Source: {entry.source_path}\n"
            "Generated-By: someone-else\n"
            f"Plan-ID: {manifest.plan_id}\n",
            encoding="utf-8",
        )
        result_only_wrong = verify_summarize_plan(repo_root, manifest)
        self.assertEqual(result_only_wrong.output_states[0].status, OUTPUT_STATUS_MISSING_GENERATOR_MARKER)
        self.assertEqual(result_only_wrong.output_states[0].reason_codes, ("missing-generator-marker",))

    def test_write_verification_artifacts_json_and_markdown(self) -> None:
        repo_root = self._init_repo("repo-verify-artifacts")
        manifest = self._prepare_manifest(repo_root, source_count=1)
        entry = manifest.entries[0]
        self._write_valid_output(
            repo_root,
            source_path=entry.source_path,
            output_path=entry.output_path,
            plan_id=manifest.plan_id,
        )

        result = verify_summarize_plan(repo_root, manifest)
        markdown_path, json_path = write_verification_artifacts(repo_root=repo_root, result=result)

        self.assertTrue((repo_root / markdown_path).exists())
        self.assertTrue((repo_root / json_path).exists())

        payload = json.loads((repo_root / json_path).read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["plan_id"], manifest.plan_id)
        self.assertEqual(payload["overall_status"], OVERALL_STATUS_COMPLETE)

        rendered = verification_result_json(result)
        self.assertEqual(rendered["plan_id"], manifest.plan_id)


if __name__ == "__main__":
    unittest.main()

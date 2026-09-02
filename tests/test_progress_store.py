"""The durable half of D11: what may be recorded, and what may never get in."""

from __future__ import annotations

import ast
import inspect
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from ai_dev_flow import progress_store as store_module
from ai_dev_flow.progress_store import (
    CONFIDENCES,
    MAX_PROJECTION_NOTE,
    PROGRESS_DIRECTORY,
    PROGRESS_STORE_NAME,
    ProgressStore,
    ProgressStoreError,
    REASON_CHECKPOINT_REGRESSED,
    REASON_INVALID_CHECKPOINT,
    REASON_INVALID_COMMIT,
    REASON_INVALID_CONFIDENCE,
    REASON_INVALID_NAMED_TOTAL,
    REASON_INVALID_NOTE,
    REASON_INVALID_REMAINING,
    REASON_MALFORMED_STORE,
    REASON_NAMED_OUT_OF_ORDER,
    REASON_TIMESTAMP_UNAVAILABLE,
    SCHEMA_VERSION,
    commit_instant,
    progress_store_path,
)


class ProgressStoreTestCase(unittest.TestCase):
    """Every fixture builds its own repository and its own store file."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)
        self.repo = self._init_repo()
        self.path = self.tmp_path / "progress.json"
        self.store = ProgressStore(self.path)

    # -- fixtures ---------------------------------------------------------

    def _git(self, *args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True, text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return completed.stdout.strip()

    def _init_repo(self) -> Path:
        self.repo = self.tmp_path / "coordination"
        self.repo.mkdir(parents=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "init", "-q"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self._git("config", "user.name", "Progress Tests")
        self._git("config", "user.email", "progress-tests@example.com")
        return self.repo

    def a_commit(self, message: str = "orchestrator: state", when: str = None) -> str:
        """One real commit in the coordination repository, at a stated instant."""
        arguments = ["commit", "-q", "--allow-empty", "-m", message]
        if when is None:
            self._git(*arguments)
        else:
            completed = subprocess.run(
                ["git", "-C", str(self.repo), *arguments],
                check=True, text=True, encoding="utf-8", errors="replace",
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=dict(**_environment(), GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when),
            )
            self.assertEqual(completed.returncode, 0)
        return self._git("rev-parse", "HEAD")

    def written(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def refused(self, callable_, *args, **kwargs) -> ProgressStoreError:
        with self.assertRaises(ProgressStoreError) as caught:
            callable_(*args, **kwargs)
        return caught.exception


def _environment() -> dict:
    import os

    return dict(os.environ)


# --------------------------------------------------------------------------
# The deterministic instant
# --------------------------------------------------------------------------


class DeterministicInstantTests(ProgressStoreTestCase):
    """Every timestamp comes from the control-plane mechanism, or from nowhere."""

    def test_the_recorded_instant_is_exactly_what_git_log_reports(self) -> None:
        commit = self.a_commit()
        recorded = self.store.record_acceptance(
            repo_root=self.repo, commit=commit, checkpoint=52
        )
        independent = self._git("log", "-1", "--format=%cI", commit)
        self.assertEqual(recorded.accepted_at, independent)
        self.assertEqual(self.store.facts().acceptances[0].accepted_at, independent)

    def test_no_recording_function_accepts_an_instant_at_all(self) -> None:
        """The structural half of "never from Claude prose".

        A rule that timestamps must come from git is a rule an agent can forget.
        A recording interface with nowhere to put a timestamp is not.
        """
        for name in ("record_acceptance", "record_named_completion", "record_projection"):
            parameters = set(inspect.signature(getattr(ProgressStore, name)).parameters)
            for forbidden in (
                "accepted_at", "completed_at", "recorded_at", "at", "when",
                "instant", "timestamp", "now",
            ):
                self.assertNotIn(forbidden, parameters, "{0}.{1}".format(name, forbidden))
            self.assertIn("repo_root", parameters, name)
            self.assertIn("commit", parameters, name)

    def test_the_module_derives_no_instant_of_its_own(self) -> None:
        source = Path(store_module.__file__).read_text(encoding="utf-8")
        for forbidden in ("time.time", "datetime.now", "utcnow", "strftime", "monotonic"):
            self.assertNotIn(forbidden, source, forbidden)

    def test_the_only_git_command_is_the_accepted_convention(self) -> None:
        """One git invocation exists, and it is the one D11 names."""
        source = Path(store_module.__file__).read_text(encoding="utf-8")
        runs = [
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "run"
        ]
        self.assertEqual(len(runs), 1)
        literals = [
            element.value
            for element in runs[0].args[0].elts
            if isinstance(element, ast.Constant)
        ]
        self.assertEqual(literals, ["git", "-C", "log", "-1", "--format=%cI", "--"])

    def test_only_a_full_object_name_is_a_source(self) -> None:
        """An abbreviation or a ref resolves differently on a different day."""
        commit = self.a_commit()
        for ambiguous in (commit[:12], "HEAD", "main", "HEAD~0", commit.upper(), ""):
            error = self.refused(commit_instant, self.repo, ambiguous)
            self.assertEqual(error.reason, REASON_INVALID_COMMIT, ambiguous)

    def test_a_commit_the_repository_does_not_have_is_refused(self) -> None:
        error = self.refused(commit_instant, self.repo, "0" * 40)
        self.assertEqual(error.reason, REASON_TIMESTAMP_UNAVAILABLE)

    def test_a_repository_that_is_not_there_is_refused(self) -> None:
        commit = self.a_commit()
        error = self.refused(commit_instant, self.tmp_path / "absent", commit)
        self.assertEqual(error.reason, REASON_TIMESTAMP_UNAVAILABLE)

    def test_both_forms_git_actually_emits_are_accepted(self) -> None:
        """`%cI` prints `Z` for UTC and an offset otherwise; both are real output."""
        utc = self.a_commit(when="2026-08-25T10:00:00+00:00")
        offset = self.a_commit(when="2026-08-26T10:00:00+02:00")
        self.assertTrue(commit_instant(self.repo, utc).endswith("Z"))
        self.assertTrue(commit_instant(self.repo, offset).endswith("+02:00"))

    def test_a_hand_written_instant_is_refused_on_read(self) -> None:
        commit = self.a_commit()
        self.store.record_acceptance(repo_root=self.repo, commit=commit, checkpoint=52)
        document = self.written()
        document["acceptances"][0]["acceptedAt"] = "yesterday evening"
        self.path.write_text(json.dumps(document), encoding="utf-8")
        self.assertEqual(self.refused(self.store.facts).reason, REASON_TIMESTAMP_UNAVAILABLE)


# --------------------------------------------------------------------------
# Acceptance is the only way in
# --------------------------------------------------------------------------


class AcceptanceTests(ProgressStoreTestCase):
    def test_accepted_checkpoints_are_recorded_strictly_increasing(self) -> None:
        first = self.a_commit()
        second = self.a_commit()
        self.store.record_acceptance(repo_root=self.repo, commit=first, checkpoint=51)
        self.store.record_acceptance(repo_root=self.repo, commit=second, checkpoint=52)
        self.assertEqual(
            [entry.checkpoint for entry in self.store.facts().acceptances], [51, 52]
        )

    def test_a_repeated_or_regressed_checkpoint_is_refused(self) -> None:
        first = self.a_commit()
        second = self.a_commit()
        self.store.record_acceptance(repo_root=self.repo, commit=first, checkpoint=52)
        for regressive in (52, 51, 1):
            error = self.refused(
                self.store.record_acceptance,
                repo_root=self.repo, commit=second, checkpoint=regressive,
            )
            self.assertEqual(error.reason, REASON_CHECKPOINT_REGRESSED, regressive)
        self.assertEqual(len(self.store.facts().acceptances), 1)

    def test_a_refused_recording_leaves_the_store_byte_identical(self) -> None:
        """A bad call fails; it does not take the recorded history down with it.

        The whole new document is validated before the file is replaced, so a
        regressed checkpoint, an unusable confidence or an oversized note is a
        refusal and nothing more. Writing first and validating afterwards would
        leave a store that no longer reads at all, which would remove the
        percentage as collateral for a call nobody accepted.
        """
        self.store.record_acceptance(
            repo_root=self.repo, commit=self.a_commit(), checkpoint=52
        )
        before = self.path.read_bytes()
        commit = self.a_commit()
        self.refused(
            self.store.record_acceptance,
            repo_root=self.repo, commit=commit, checkpoint=52,
        )
        self.refused(
            self.store.record_projection,
            repo_root=self.repo, commit=commit,
            remaining=1, confidence="certain", note="",
        )
        self.refused(
            self.store.record_named_completion,
            repo_root=self.repo, commit=commit, checkpoint=4, total=9,
        )
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(self.store.facts().acceptances), 1)

    def test_a_checkpoint_that_is_not_a_whole_number_is_refused(self) -> None:
        commit = self.a_commit()
        for bad in (0, -1, 1.0, "52", True, None):
            error = self.refused(
                self.store.record_acceptance,
                repo_root=self.repo, commit=commit, checkpoint=bad,
            )
            self.assertEqual(error.reason, REASON_INVALID_CHECKPOINT, repr(bad))

    def test_there_is_no_record_kind_for_a_published_checkpoint(self) -> None:
        """A published-but-unaccepted checkpoint has no shape to occupy.

        This is the structural half of "a published checkpoint never advances the
        numerator". There is no pending state, no publication verb, and no field
        on the persisted document a not-yet-accepted checkpoint could be written
        into, so the numerator cannot be moved by anything but an acceptance.
        """
        verbs = [
            name
            for name in dir(ProgressStore)
            if not name.startswith("_") and callable(getattr(ProgressStore, name))
        ]
        self.assertEqual(
            sorted(verbs),
            ["facts", "record_acceptance", "record_named_completion", "record_projection"],
        )
        commit = self.a_commit()
        self.store.record_acceptance(repo_root=self.repo, commit=commit, checkpoint=52)
        document = self.written()
        self.assertEqual(sorted(document), ["acceptances", "named", "projections", "version"])
        self.assertEqual(
            sorted(document["acceptances"][0]), ["acceptedAt", "checkpoint", "commit"]
        )

    def test_an_added_field_makes_the_store_unreadable_rather_than_ignored(self) -> None:
        commit = self.a_commit()
        self.store.record_acceptance(repo_root=self.repo, commit=commit, checkpoint=52)
        document = self.written()
        document["acceptances"][0]["published"] = 53
        self.path.write_text(json.dumps(document), encoding="utf-8")
        self.assertEqual(self.refused(self.store.facts).reason, REASON_MALFORMED_STORE)


# --------------------------------------------------------------------------
# Projections
# --------------------------------------------------------------------------


class ProjectionTests(ProgressStoreTestCase):
    def test_the_basis_is_derived_from_evidence_and_cannot_be_supplied(self) -> None:
        """The basis is what makes a revision distinguishable from progress.

        It is read from this store's own acceptance records rather than taken as
        an argument, so no caller can make a revised denominator look like an
        unchanged one -- or the reverse -- by misstating what its estimate was
        measured against.
        """
        parameters = set(
            inspect.signature(ProgressStore.record_projection).parameters
        )
        self.assertNotIn("basis", parameters)
        self.store.record_acceptance(
            repo_root=self.repo, commit=self.a_commit(), checkpoint=51
        )
        first = self.store.record_projection(
            repo_root=self.repo, commit=self.a_commit(),
            remaining=11, confidence="low", note="initial",
        )
        self.assertEqual(first.basis, 51)
        self.store.record_acceptance(
            repo_root=self.repo, commit=self.a_commit(), checkpoint=52
        )
        second = self.store.record_projection(
            repo_root=self.repo, commit=self.a_commit(),
            remaining=12, confidence="low", note="revised",
        )
        self.assertEqual(second.basis, 52)

    def test_a_projection_before_any_acceptance_has_a_basis_of_zero(self) -> None:
        entry = self.store.record_projection(
            repo_root=self.repo, commit=self.a_commit(),
            remaining=64, confidence="low", note="nothing accepted yet",
        )
        self.assertEqual(entry.basis, 0)

    def test_confidence_is_exactly_low_medium_or_high(self) -> None:
        self.assertEqual(CONFIDENCES, ("low", "medium", "high"))
        for accepted in CONFIDENCES:
            ProgressStore(self.tmp_path / "{0}.json".format(accepted)).record_projection(
                repo_root=self.repo, commit=self.a_commit(),
                remaining=1, confidence=accepted, note="",
            )
        for rejected in ("LOW", "Low", "medium ", "very-high", "unknown", "", 1, None, True):
            error = self.refused(
                self.store.record_projection,
                repo_root=self.repo, commit=self.a_commit(),
                remaining=1, confidence=rejected, note="",
            )
            self.assertEqual(error.reason, REASON_INVALID_CONFIDENCE, repr(rejected))

    def test_a_remaining_count_is_a_whole_count_of_at_least_zero(self) -> None:
        for bad in (-1, 1.5, "12", None, True):
            error = self.refused(
                self.store.record_projection,
                repo_root=self.repo, commit=self.a_commit(),
                remaining=bad, confidence="low", note="",
            )
            self.assertEqual(error.reason, REASON_INVALID_REMAINING, repr(bad))

    def test_a_preserved_estimate_is_recorded_rather_than_skipped(self) -> None:
        """D11 asks the orchestrator to reconsider; having reconsidered is a fact."""
        self.store.record_acceptance(
            repo_root=self.repo, commit=self.a_commit(), checkpoint=52
        )
        for note in ("initial", "reconsidered, unchanged", "reconsidered again"):
            self.store.record_projection(
                repo_root=self.repo, commit=self.a_commit(),
                remaining=12, confidence="low", note=note,
            )
        self.assertEqual(len(self.store.facts().projections), 3)


class ProjectionNoteTests(ProgressStoreTestCase):
    """The note says why. It is not permitted to become a diary."""

    def _record(self, note):
        return self.store.record_projection(
            repo_root=self.repo, commit=self.a_commit(),
            remaining=1, confidence="low", note=note,
        )

    def test_one_bounded_line_is_accepted(self) -> None:
        self.assertEqual(self._record("scope grew: D8 needed a remediation").note,
                         "scope grew: D8 needed a remediation")
        self.assertEqual(self._record("").note, "")
        self.assertEqual(self._record("x" * MAX_PROJECTION_NOTE).note, "x" * MAX_PROJECTION_NOTE)

    def test_a_multi_line_or_oversized_note_is_refused(self) -> None:
        for diary in (
            "line one\nline two",
            "carriage\rreturn",
            "tab\tseparated",
            " leading space",
            "trailing space ",
            "x" * (MAX_PROJECTION_NOTE + 1),
        ):
            self.assertEqual(self.refused(self._record, diary).reason, REASON_INVALID_NOTE)

    def test_a_note_that_is_not_text_is_refused(self) -> None:
        for bad in (None, 12, ["why"], {"why": "scope"}):
            self.assertEqual(self.refused(self._record, bad).reason, REASON_INVALID_NOTE)


# --------------------------------------------------------------------------
# Named checkpoints
# --------------------------------------------------------------------------


class NamedCompletionTests(ProgressStoreTestCase):
    def test_completions_are_the_contiguous_prefix_of_the_roadmap(self) -> None:
        for number in range(1, 7):
            self.store.record_named_completion(
                repo_root=self.repo, commit=self.a_commit(), checkpoint=number, total=9
            )
        self.assertEqual(
            [entry.checkpoint for entry in self.store.facts().named], [1, 2, 3, 4, 5, 6]
        )

    def test_a_gap_in_the_prefix_is_refused(self) -> None:
        self.store.record_named_completion(
            repo_root=self.repo, commit=self.a_commit(), checkpoint=1, total=9
        )
        error = self.refused(
            self.store.record_named_completion,
            repo_root=self.repo, commit=self.a_commit(), checkpoint=3, total=9,
        )
        self.assertEqual(error.reason, REASON_NAMED_OUT_OF_ORDER)

    def test_a_completion_beyond_the_roadmap_is_refused(self) -> None:
        error = self.refused(
            self.store.record_named_completion,
            repo_root=self.repo, commit=self.a_commit(), checkpoint=1, total=0,
        )
        self.assertIn(error.reason, (REASON_INVALID_NAMED_TOTAL, REASON_INVALID_CHECKPOINT))
        self.store.record_named_completion(
            repo_root=self.repo, commit=self.a_commit(), checkpoint=1, total=9
        )
        error = self.refused(
            self.store.record_named_completion,
            repo_root=self.repo, commit=self.a_commit(), checkpoint=2, total=1,
        )
        self.assertEqual(error.reason, REASON_INVALID_NAMED_TOTAL)

    def test_the_roadmap_size_is_restated_with_each_completion(self) -> None:
        """A roadmap may honestly grow; one stored figure would rewrite history."""
        self.store.record_named_completion(
            repo_root=self.repo, commit=self.a_commit(), checkpoint=1, total=8
        )
        self.store.record_named_completion(
            repo_root=self.repo, commit=self.a_commit(), checkpoint=2, total=9
        )
        self.assertEqual([entry.total for entry in self.store.facts().named], [8, 9])


# --------------------------------------------------------------------------
# What this store is not
# --------------------------------------------------------------------------


class RetainedHistoryTests(ProgressStoreTestCase):
    """Acceptance and projection facts only. There is no room for anything else."""

    def setUp(self) -> None:
        super().setUp()
        self.source = Path(store_module.__file__).read_text(encoding="utf-8")

    def test_the_persisted_document_has_exactly_three_kinds_of_fact(self) -> None:
        self.store.record_acceptance(
            repo_root=self.repo, commit=self.a_commit(), checkpoint=52
        )
        self.store.record_named_completion(
            repo_root=self.repo, commit=self.a_commit(), checkpoint=1, total=9
        )
        self.store.record_projection(
            repo_root=self.repo, commit=self.a_commit(),
            remaining=12, confidence="low", note="why",
        )
        document = self.written()
        self.assertEqual(document["version"], SCHEMA_VERSION)
        self.assertEqual(sorted(document), ["acceptances", "named", "projections", "version"])
        self.assertEqual(
            sorted(document["projections"][0]),
            ["basis", "commit", "confidence", "note", "recordedAt", "remaining"],
        )
        self.assertEqual(
            sorted(document["named"][0]), ["checkpoint", "commit", "completedAt", "total"]
        )

    def test_no_diary_transcript_log_or_analytics_surface_exists(self) -> None:
        """Named surfaces, not prose.

        Read from the AST rather than from the file's text, so the module may say
        in a comment what it refuses to *be* -- and so this checks the thing that
        matters: no attribute, argument, function, class or persisted key exists
        that a diary, a transcript, a log, a session, a token figure or a
        duration could be written into.
        """
        names = set()
        for node in ast.walk(ast.parse(self.source)):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.arg):
                names.add(node.arg)
            elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.keyword) and node.arg:
                names.add(node.arg)
        names.update(store_module._STORE_KEYS)
        names.update(store_module._ACCEPTANCE_KEYS)
        names.update(store_module._NAMED_KEYS)
        names.update(store_module._PROJECTION_KEYS)
        lowered = {name.lower().replace("_", "") for name in names}
        for forbidden in (
            "transcript", "diary", "logging", "analytics", "telemetryevent",
            "sessionid", "pid", "token", "duration", "elapsed",
            "velocity", "handoff", "wallclock", "appendevent",
        ):
            self.assertNotIn(forbidden, lowered, forbidden)
            for name in lowered:
                self.assertNotIn(forbidden, name, "{0} in {1}".format(forbidden, name))

    def test_the_module_reaches_no_product_authority(self) -> None:
        """It holds facts. It cannot ask anything, and nothing here decides."""
        imported = set()
        for node in ast.walk(ast.parse(self.source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add("." * (node.level or 0) + (node.module or ""))
        self.assertEqual(
            imported,
            {"__future__", "json", "re", "subprocess", "pathlib", "typing", ".json_files"},
        )

    def test_a_store_path_is_named_beside_the_accepted_allowance_store(self) -> None:
        self.assertEqual(PROGRESS_DIRECTORY, ".ai-dev/progress")
        self.assertEqual(PROGRESS_STORE_NAME, "progress.json")
        self.assertEqual(
            progress_store_path(self.tmp_path),
            self.tmp_path / ".ai-dev" / "progress" / "progress.json",
        )

    def test_naming_a_store_opens_nothing_and_reading_an_absent_one_is_empty(self) -> None:
        absent = ProgressStore(self.tmp_path / "nowhere" / "progress.json")
        facts = absent.facts()
        self.assertEqual((facts.acceptances, facts.named, facts.projections), ((), (), ()))
        self.assertFalse((self.tmp_path / "nowhere").exists())


class MalformedStoreTests(ProgressStoreTestCase):
    def _corrupt(self, text: str):
        self.path.write_text(text, encoding="utf-8")
        return self.refused(self.store.facts)

    def test_a_store_that_is_not_json_is_refused(self) -> None:
        self.assertEqual(self._corrupt("{not json").reason, REASON_MALFORMED_STORE)

    def test_a_store_of_an_unsupported_version_is_refused(self) -> None:
        self.assertEqual(
            self._corrupt(json.dumps(
                {"version": 2, "acceptances": [], "named": [], "projections": []}
            )).reason,
            REASON_MALFORMED_STORE,
        )

    def test_a_store_missing_a_section_is_refused(self) -> None:
        self.assertEqual(
            self._corrupt(json.dumps({"version": 1, "acceptances": []})).reason,
            REASON_MALFORMED_STORE,
        )

    def test_a_projection_measured_against_an_unrecorded_checkpoint_is_refused(self) -> None:
        commit = self.a_commit()
        self.store.record_acceptance(repo_root=self.repo, commit=commit, checkpoint=52)
        document = self.written()
        document["projections"] = [{
            "basis": 99, "commit": commit, "confidence": "low",
            "note": "", "recordedAt": commit_instant(self.repo, commit), "remaining": 3,
        }]
        self.assertEqual(
            self._corrupt(json.dumps(document)).reason, REASON_INVALID_CHECKPOINT
        )

    def test_a_refused_read_leaves_a_later_recording_refused_too(self) -> None:
        """A record is never appended to a document this store could not validate."""
        self.path.write_text("{not json", encoding="utf-8")
        self.refused(
            self.store.record_acceptance,
            repo_root=self.repo, commit=self.a_commit(), checkpoint=52,
        )
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{not json")


if __name__ == "__main__":
    unittest.main()

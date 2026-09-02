"""Durable progress facts: checkpoint acceptances and projection estimates, only."""

from __future__ import annotations

# This is the durable half of D11, and it is deliberately the smallest durable
# thing that can answer the accepted measure. It holds three kinds of fact and no
# fourth: which numeric Flow checkpoints were accepted and when, which named
# checkpoints were completed and when, and what the orchestrator projected as
# remaining and how confident it was. There is no event log, no execution diary,
# no transcript, no session, no token figure, no wall-clock duration, and no
# analytics framework -- and there is no field through which one could arrive,
# because the persisted key set is closed and a record carrying anything else is
# refused rather than trimmed.
#
# Four boundaries hold it honest.
#
# First, no timestamp in this store can be prose. There is no `accepted_at`,
# `completed_at` or `recorded_at` parameter on any recording function. Each one
# takes a coordination repository and a full commit object name and derives the
# instant itself, through `git log -1 --format=%cI`, which is the deterministic
# control-plane mechanism accepted decision D11 names. A caller that wants to
# state a different instant has nowhere to put it, and a caller whose commit
# cannot be resolved gets a refusal rather than a plausible time.
#
# Second, the numerator can only be an acceptance. `record_acceptance` is the one
# way a numeric checkpoint enters this store, and numeric checkpoints must arrive
# strictly increasing. There is no "published" record kind, no pending state, and
# no field a not-yet-accepted checkpoint could occupy -- so a published but
# unaccepted checkpoint cannot advance anything here, for the same reason an
# unwritten fact cannot: there is no shape for it.
#
# Third, a projection is measured against evidence rather than against a claim.
# `record_projection` takes the projected remaining count and a confidence, and it
# does *not* take the checkpoint that projection was made against. It derives that
# basis from this store's own acceptance records, so the basis cannot be
# misstated, and so a later reader can tell a denominator that was revised from a
# denominator that was merely consumed by progress.
#
# Fourth, reading is one generation and refuses rather than repairs. `facts()`
# loads the whole object once and validates it whole: unknown keys, a regressed
# checkpoint, a confidence outside the accepted three, a note carrying more than
# one bounded line, or a timestamp that is not what the deterministic mechanism
# emits are all refusals carrying an exact reason. Nothing is defaulted, filled,
# reordered or silently dropped, because a store that quietly repairs itself is a
# store whose figures cannot be checked against the commits they came from.

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from .json_files import JsonFileError, write_json_object_atomic

__all__ = [
    "Acceptance",
    "CONFIDENCES",
    "MAX_PROJECTION_NOTE",
    "NamedCompletion",
    "PROGRESS_DIRECTORY",
    "PROGRESS_STORE_NAME",
    "Projection",
    "ProgressFacts",
    "ProgressStore",
    "ProgressStoreError",
    "REASON_CHECKPOINT_REGRESSED",
    "REASON_INVALID_CHECKPOINT",
    "REASON_INVALID_COMMIT",
    "REASON_INVALID_CONFIDENCE",
    "REASON_INVALID_NAMED_TOTAL",
    "REASON_INVALID_NOTE",
    "REASON_INVALID_REMAINING",
    "REASON_MALFORMED_STORE",
    "REASON_NAMED_OUT_OF_ORDER",
    "REASON_STORE_WRITE_FAILED",
    "REASON_TIMESTAMP_UNAVAILABLE",
    "REASON_UNREADABLE_STORE",
    "SCHEMA_VERSION",
    "progress_store_path",
]

SCHEMA_VERSION = 1

# Beside the accepted allowance store, under the same product-local directory
# convention, because both are durable manager evidence about one worktree.
PROGRESS_DIRECTORY = ".ai-dev/progress"
PROGRESS_STORE_NAME = "progress.json"

# Exactly the three accepted decision D11 permits. Not an ordering, not a score,
# and deliberately not a number: a confidence that could be compared numerically
# is a confidence something could threshold on.
CONFIDENCES = ("low", "medium", "high")

# One bounded line saying why the projection changed. Bounded and single-line on
# purpose: the "why" D11 asks for is a sentence a person reads beside a
# percentage, and anything longer is the execution diary D11 forbids.
MAX_PROJECTION_NOTE = 240

REASON_MALFORMED_STORE = "malformed-progress-store"
REASON_UNREADABLE_STORE = "unreadable-progress-store"
REASON_STORE_WRITE_FAILED = "progress-store-write-failed"
REASON_INVALID_CHECKPOINT = "invalid-checkpoint"
REASON_CHECKPOINT_REGRESSED = "checkpoint-regressed"
REASON_INVALID_CONFIDENCE = "invalid-confidence"
REASON_INVALID_REMAINING = "invalid-projected-remaining"
REASON_INVALID_NOTE = "invalid-projection-note"
REASON_INVALID_COMMIT = "invalid-commit"
REASON_TIMESTAMP_UNAVAILABLE = "acceptance-timestamp-unavailable"
REASON_NAMED_OUT_OF_ORDER = "named-checkpoint-out-of-order"
REASON_INVALID_NAMED_TOTAL = "invalid-named-total"

_STORE_KEYS = ("version", "acceptances", "named", "projections")
_ACCEPTANCE_KEYS = ("checkpoint", "commit", "acceptedAt")
_NAMED_KEYS = ("checkpoint", "total", "commit", "completedAt")
_PROJECTION_KEYS = ("basis", "commit", "confidence", "note", "recordedAt", "remaining")

# A full object name and nothing else. An abbreviation, a branch, `HEAD`, or a
# reflog expression would each resolve to a different commit on a different day,
# which is the opposite of a deterministic source.
_COMMIT = re.compile(r"\A[0-9a-f]{40}\Z")

# Exactly what `git log -1 --format=%cI` emits, which is strict ISO 8601 in
# either of the two forms git actually produces: an explicit numeric offset, and
# the bare `Z` it prints instead of `+00:00` for UTC. Both are accepted because
# both are observed output of the named mechanism -- accepting only the offset
# form would refuse every commit made on a UTC host. Validated on the way in and
# on the way out, so a hand-edited file cannot pass a plausible-looking instant
# off as a derived one.
_GIT_ISO = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})\Z")

_EMPTY: Dict[str, Any] = {
    "version": SCHEMA_VERSION,
    "acceptances": [],
    "named": [],
    "projections": [],
}


class ProgressStoreError(Exception):
    """A refusal to read or record a progress fact, carrying one stable reason."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__("{0}: {1}".format(reason, detail))
        self.reason = reason
        self.detail = detail


def progress_store_path(repo_root: Path) -> Path:
    """Where one worktree's progress facts live. Naming a path opens nothing."""
    return Path(repo_root) / PROGRESS_DIRECTORY / PROGRESS_STORE_NAME


# --------------------------------------------------------------------------
# The facts
# --------------------------------------------------------------------------


class Acceptance(NamedTuple):
    """One numeric Flow checkpoint that was accepted, and when it was."""

    checkpoint: int
    commit: str
    accepted_at: str


class NamedCompletion(NamedTuple):
    """One named ticket checkpoint that was completed, and the roadmap size then."""

    checkpoint: int
    total: int
    commit: str
    completed_at: str


class Projection(NamedTuple):
    """One reconsideration of the remaining count, and what it was measured from.

    `basis` is the last accepted numeric checkpoint at the instant this projection
    was recorded, derived from the acceptance records rather than supplied. It is
    what makes `basis + remaining` -- the projected final this entry implied --
    comparable against the previous entry's, which is the whole mechanism by which
    a revised denominator is told apart from a denominator progress consumed.
    """

    remaining: int
    basis: int
    confidence: str
    note: str
    commit: str
    recorded_at: str


class ProgressFacts(NamedTuple):
    """Everything one read of this store returns, from one generation of the file.

    One call, one load. Assembling these from three separate reads would let a
    projection recorded midway through be paired with an older acceptance list,
    and the basis arithmetic above is exactly what that would corrupt.
    """

    acceptances: Tuple[Acceptance, ...]
    named: Tuple[NamedCompletion, ...]
    projections: Tuple[Projection, ...]


# --------------------------------------------------------------------------
# Exact values
# --------------------------------------------------------------------------


def _exact_keys(payload: object, expected: Tuple[str, ...], *, label: str) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ProgressStoreError(
            REASON_MALFORMED_STORE,
            "{0} must be an object, got {1!r}".format(label, type(payload).__name__),
        )
    present = tuple(sorted(payload))
    if present != tuple(sorted(expected)):
        raise ProgressStoreError(
            REASON_MALFORMED_STORE,
            "{0} must carry exactly {1}, got {2}".format(
                label, ", ".join(sorted(expected)), ", ".join(present) or "nothing"
            ),
        )
    return payload


def _exact_checkpoint(value: object, *, label: str) -> int:
    """A whole positive checkpoint number, refusing every near-miss.

    `type(...) is not int` rather than `isinstance`, because `True` is an `int`
    and a boolean checkpoint is a bug that would otherwise number itself 1.
    """
    if type(value) is not int or value < 1:
        raise ProgressStoreError(
            REASON_INVALID_CHECKPOINT,
            "{0} must be a whole checkpoint number of at least 1, got {1!r}".format(label, value),
        )
    return value


def _exact_commit(value: object) -> str:
    if type(value) is not str or not _COMMIT.match(value):
        raise ProgressStoreError(
            REASON_INVALID_COMMIT,
            "a progress fact is sourced from one full 40-character commit object "
            "name, got {0!r}".format(value),
        )
    return value


def _exact_instant(value: object, *, label: str) -> str:
    if type(value) is not str or not _GIT_ISO.match(value):
        raise ProgressStoreError(
            REASON_TIMESTAMP_UNAVAILABLE,
            "{0} must be the instant `git log -1 --format=%cI` emits, got {1!r}".format(
                label, value
            ),
        )
    return value


def _exact_confidence(value: object) -> str:
    if value not in CONFIDENCES:
        raise ProgressStoreError(
            REASON_INVALID_CONFIDENCE,
            "confidence is exactly one of {0}, got {1!r}".format(
                ", ".join(CONFIDENCES), value
            ),
        )
    return value


def _exact_remaining(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ProgressStoreError(
            REASON_INVALID_REMAINING,
            "projected remaining must be a whole count of at least 0, got {0!r}".format(value),
        )
    return value


def _exact_note(value: object) -> str:
    """One bounded line, or nothing. Never a paragraph and never a log line."""
    if type(value) is not str:
        raise ProgressStoreError(
            REASON_INVALID_NOTE,
            "a projection note is one bounded line of text, got {0!r}".format(
                type(value).__name__
            ),
        )
    if len(value) > MAX_PROJECTION_NOTE:
        raise ProgressStoreError(
            REASON_INVALID_NOTE,
            "a projection note is at most {0} characters, got {1}".format(
                MAX_PROJECTION_NOTE, len(value)
            ),
        )
    if value != value.strip() or any(character in value for character in "\r\n\t"):
        raise ProgressStoreError(
            REASON_INVALID_NOTE,
            "a projection note is a single trimmed line; a multi-line note is the "
            "execution diary this store does not keep",
        )
    return value


# --------------------------------------------------------------------------
# The deterministic instant
# --------------------------------------------------------------------------


def commit_instant(repo_root: Path, commit: str) -> str:
    """One commit's instant, from the deterministic control-plane mechanism.

    This is the only place any instant in this store is produced. It runs the
    convention accepted decision D11 names -- `git log -1 --format=%cI` -- against
    an explicit coordination repository and a full object name, and it validates
    what came back before returning it.

    `--` ends the revision list so a commit name can never be read as a path, and
    a full object name is required so the answer cannot depend on which refs
    happen to exist. A repository that is missing, a commit that is not present,
    and output that is not the expected instant are all one refusal: this function
    never returns an approximate time, and there is no caller-supplied fallback it
    could return instead.
    """
    checked = _exact_commit(commit)
    root = Path(repo_root)
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "log", "-1", "--format=%cI", checked, "--"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise ProgressStoreError(
            REASON_TIMESTAMP_UNAVAILABLE,
            "cannot run git in {0}: {1}".format(root, exc),
        ) from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "exit code {0}".format(completed.returncode)
        raise ProgressStoreError(
            REASON_TIMESTAMP_UNAVAILABLE,
            "git log -1 --format=%cI {0} in {1} failed: {2}".format(checked, root, detail),
        )
    return _exact_instant(completed.stdout.strip(), label="the acceptance instant")


# --------------------------------------------------------------------------
# The store
# --------------------------------------------------------------------------


class ProgressStore:
    """One worktree's progress facts, in one small JSON object replaced whole.

    Constructing this opens nothing, creates nothing and reads nothing: it names a
    path, exactly as the accepted allowance store does, so a run may hold one
    whether or not any progress has ever been recorded.

    Nothing is cached between calls. Every read loads the file again, so a page
    served after a new acceptance shows that acceptance, and two readers of one
    path always see the same evidence.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    # -- reading ----------------------------------------------------------

    def facts(self) -> ProgressFacts:
        """Every recorded fact, from one load, validated whole.

        A file that is not there is not a failure: it is a worktree where nothing
        has been recorded yet, and it reads as three empty tuples. Everything else
        wrong with the file is a refusal carrying its exact reason, because a
        store that repaired itself would produce a numerator nobody could check
        against the commit it claims to come from.
        """
        return self._validated(self._load())

    def _validated(self, payload: object) -> ProgressFacts:
        """One whole document, checked whole. The single validator both paths use.

        Reading runs this against the file, and recording runs it against the
        document it is about to write. That is deliberately the same code: a
        record this store would refuse to read is a record it must refuse to
        write, and running two similar checks in two places is how those two
        answers drift apart.
        """
        document = _exact_keys(payload, _STORE_KEYS, label="the progress store")
        version = document.get("version")
        if version != SCHEMA_VERSION:
            raise ProgressStoreError(
                REASON_MALFORMED_STORE,
                "progress store version {0!r} is not the supported {1}".format(
                    version, SCHEMA_VERSION
                ),
            )
        acceptances = self._acceptances(document["acceptances"])
        named = self._named(document["named"])
        projections = self._projections(document["projections"], acceptances)
        return ProgressFacts(acceptances=acceptances, named=named, projections=projections)

    def _load(self) -> Dict[str, Any]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return dict(_EMPTY)
        except OSError as exc:
            raise ProgressStoreError(
                REASON_UNREADABLE_STORE, "cannot read {0}: {1}".format(self.path, exc)
            ) from exc
        try:
            return json.loads(text)
        except ValueError as exc:
            raise ProgressStoreError(
                REASON_MALFORMED_STORE, "{0} is not valid JSON: {1}".format(self.path, exc)
            ) from exc

    @staticmethod
    def _sequence(value: object, *, label: str) -> List[Any]:
        if not isinstance(value, list):
            raise ProgressStoreError(
                REASON_MALFORMED_STORE,
                "{0} must be a list, got {1!r}".format(label, type(value).__name__),
            )
        return value

    def _acceptances(self, value: object) -> Tuple[Acceptance, ...]:
        """Accepted numeric checkpoints, in order, strictly increasing.

        Strictly increasing is the numerator's whole guarantee here. A repeated or
        regressed checkpoint is refused rather than sorted or deduplicated,
        because either repair would silently choose which of two contradictory
        records about one checkpoint the percentage is drawn from.
        """
        entries: List[Acceptance] = []
        for index, raw in enumerate(self._sequence(value, label="acceptances")):
            record = _exact_keys(raw, _ACCEPTANCE_KEYS, label="acceptance {0}".format(index))
            acceptance = Acceptance(
                checkpoint=_exact_checkpoint(record["checkpoint"], label="an accepted checkpoint"),
                commit=_exact_commit(record["commit"]),
                accepted_at=_exact_instant(record["acceptedAt"], label="an acceptance instant"),
            )
            if entries and acceptance.checkpoint <= entries[-1].checkpoint:
                raise ProgressStoreError(
                    REASON_CHECKPOINT_REGRESSED,
                    "accepted checkpoints are recorded strictly increasing; {0} "
                    "follows {1}".format(acceptance.checkpoint, entries[-1].checkpoint),
                )
            entries.append(acceptance)
        return tuple(entries)

    def _named(self, value: object) -> Tuple[NamedCompletion, ...]:
        """Completed named checkpoints, as the contiguous prefix they are.

        Named ticket checkpoints are an ordered roadmap, so completions form a
        prefix 1, 2, 3 ... and a gap means a record is missing or wrong. Refusing
        is the honest answer: the current named checkpoint is derived from the
        highest completed one, and deriving it across a gap would name a
        checkpoint nobody said was reached.
        """
        entries: List[NamedCompletion] = []
        for index, raw in enumerate(self._sequence(value, label="named completions")):
            record = _exact_keys(raw, _NAMED_KEYS, label="named completion {0}".format(index))
            completion = NamedCompletion(
                checkpoint=_exact_checkpoint(record["checkpoint"], label="a named checkpoint"),
                total=_exact_checkpoint(record["total"], label="the named roadmap size"),
                commit=_exact_commit(record["commit"]),
                completed_at=_exact_instant(
                    record["completedAt"], label="a named completion instant"
                ),
            )
            if completion.checkpoint != index + 1:
                raise ProgressStoreError(
                    REASON_NAMED_OUT_OF_ORDER,
                    "named completions are the contiguous prefix of the roadmap; "
                    "position {0} records checkpoint {1}".format(index + 1, completion.checkpoint),
                )
            if completion.checkpoint > completion.total:
                raise ProgressStoreError(
                    REASON_INVALID_NAMED_TOTAL,
                    "named checkpoint {0} cannot be completed on a roadmap of {1}".format(
                        completion.checkpoint, completion.total
                    ),
                )
            entries.append(completion)
        return tuple(entries)

    def _projections(
        self, value: object, acceptances: Tuple[Acceptance, ...]
    ) -> Tuple[Projection, ...]:
        """Recorded projections, each still tied to the acceptance it was made at.

        A basis naming a checkpoint this store never accepted is refused. It is
        the one cross-check that keeps `basis + remaining` meaningful: an entry
        measured against a checkpoint no acceptance record supports could imply
        any projected final at all.
        """
        accepted = {entry.checkpoint for entry in acceptances}
        entries: List[Projection] = []
        for index, raw in enumerate(self._sequence(value, label="projections")):
            record = _exact_keys(raw, _PROJECTION_KEYS, label="projection {0}".format(index))
            basis = record["basis"]
            if type(basis) is not int or basis < 0:
                raise ProgressStoreError(
                    REASON_INVALID_CHECKPOINT,
                    "a projection basis is a whole checkpoint number or 0, got {0!r}".format(basis),
                )
            if basis != 0 and basis not in accepted:
                raise ProgressStoreError(
                    REASON_INVALID_CHECKPOINT,
                    "projection {0} is measured against checkpoint {1}, which this "
                    "store has no acceptance record for".format(index, basis),
                )
            entries.append(
                Projection(
                    remaining=_exact_remaining(record["remaining"]),
                    basis=basis,
                    confidence=_exact_confidence(record["confidence"]),
                    note=_exact_note(record["note"]),
                    commit=_exact_commit(record["commit"]),
                    recorded_at=_exact_instant(
                        record["recordedAt"], label="a projection instant"
                    ),
                )
            )
        return tuple(entries)

    # -- recording --------------------------------------------------------

    def record_acceptance(self, *, repo_root: Path, commit: str, checkpoint: int) -> Acceptance:
        """Record that one numeric Flow checkpoint was accepted at one commit.

        There is no instant parameter. The instant is derived here from that
        commit through the deterministic mechanism, which is what makes "never
        from Claude prose" a property of the interface rather than a rule a caller
        is asked to follow.

        This is also the only way a numeric checkpoint reaches the numerator, and
        it records acceptance and nothing else. There is no publication record
        kind, so a checkpoint that was pushed but not accepted has no way in.
        """
        number = _exact_checkpoint(checkpoint, label="an accepted checkpoint")
        instant = commit_instant(repo_root, commit)
        document = self._document()
        entry = Acceptance(checkpoint=number, commit=_exact_commit(commit), accepted_at=instant)
        document["acceptances"].append(
            {"checkpoint": entry.checkpoint, "commit": entry.commit, "acceptedAt": entry.accepted_at}
        )
        self._replace(document)
        return entry

    def record_named_completion(
        self, *, repo_root: Path, commit: str, checkpoint: int, total: int
    ) -> NamedCompletion:
        """Record that one named ticket checkpoint was completed at one commit.

        `total` is the roadmap size as it stood at that completion, restated each
        time rather than held once, because a roadmap may honestly grow and a
        single stored figure would silently rewrite what earlier completions meant.
        """
        entry = NamedCompletion(
            checkpoint=_exact_checkpoint(checkpoint, label="a named checkpoint"),
            total=_exact_checkpoint(total, label="the named roadmap size"),
            commit=_exact_commit(commit),
            completed_at=commit_instant(repo_root, commit),
        )
        document = self._document()
        document["named"].append(
            {
                "checkpoint": entry.checkpoint,
                "total": entry.total,
                "commit": entry.commit,
                "completedAt": entry.completed_at,
            }
        )
        self._replace(document)
        return entry

    def record_projection(
        self, *, repo_root: Path, commit: str, remaining: int, confidence: str, note: str
    ) -> Projection:
        """Record one reconsideration of the remaining numeric checkpoint count.

        The basis is not a parameter. It is read from this store's own acceptance
        records at the instant of recording, so the checkpoint a projection was
        measured against is evidence rather than an assertion, and no caller can
        make a revised denominator look like an unchanged one by misstating it.

        Recording the same remaining count again is not a no-op and must not be:
        D11 asks that the orchestrator reconsider after every acceptance and *may*
        preserve the prior estimate, and a preserved estimate is a fact about the
        projection having been looked at again.
        """
        instant = commit_instant(repo_root, commit)
        facts = self.facts()
        entry = Projection(
            remaining=_exact_remaining(remaining),
            basis=facts.acceptances[-1].checkpoint if facts.acceptances else 0,
            confidence=_exact_confidence(confidence),
            note=_exact_note(note),
            commit=_exact_commit(commit),
            recorded_at=instant,
        )
        document = self._document()
        document["projections"].append(
            {
                "basis": entry.basis,
                "commit": entry.commit,
                "confidence": entry.confidence,
                "note": entry.note,
                "recordedAt": entry.recorded_at,
                "remaining": entry.remaining,
            }
        )
        self._replace(document)
        return entry

    def _document(self) -> Dict[str, Any]:
        """The current file as a plain document, proven readable before it is added to.

        `facts()` runs first and its refusal propagates, so a record is never
        appended to a file this module could not fully validate -- which would
        turn one malformed entry into a store that is malformed and longer.
        """
        self.facts()
        payload = self._load()
        return {
            "version": SCHEMA_VERSION,
            "acceptances": list(payload["acceptances"]),
            "named": list(payload["named"]),
            "projections": list(payload["projections"]),
        }

    def _replace(self, document: Dict[str, Any]) -> None:
        """Validate the whole new document, then replace the file with it.

        Validation happens before the write and never after it. A refusal must
        leave the store byte-unchanged, so that a caller which recorded a
        regressed checkpoint has simply failed rather than left behind a file
        that no longer reads at all -- which would take the percentage away as
        collateral for a bad call nobody accepted.
        """
        self._validated(document)
        try:
            write_json_object_atomic(self.path, document)
        except JsonFileError as exc:
            raise ProgressStoreError(
                REASON_STORE_WRITE_FAILED, "cannot write {0}: {1}".format(self.path, exc)
            ) from exc

"""Durable progress facts, derived from the coordination repository's own history."""

from __future__ import annotations

# This is the durable half of D11, and it is deliberately the smallest durable
# thing that can answer the accepted measure. It holds three kinds of fact and no
# fourth: which numeric Flow checkpoints were accepted and when, which named
# checkpoints were completed and when, and what the orchestrator projected as
# remaining and how confident it was. There is no event log, no execution diary,
# no transcript, no session, no token figure, no wall-clock duration, and no
# analytics framework -- and there is no field through which one could arrive,
# because the published key set is closed and a record carrying anything else is
# refused rather than trimmed.
#
# It is a reader and only a reader. Nothing here writes, and there is no
# recording function to call: progress is *published* by the orchestrator through
# the supported control-plane action, into the coordination repository, and this
# module derives the facts back out of the versions that publication left behind.
# That is what makes the durable transition and the telemetry one thing rather
# than two -- there is no second store to keep in step, and no way to record a
# progress fact except by making the durable transition that is the fact.
#
# Five boundaries hold it honest.
#
# First, no timestamp anywhere can be prose, because no timestamp is ever
# written. Each published record is dated by the commit that published it, and
# every instant here is read from that commit through `git log -1 --format=%cI`,
# the deterministic control-plane mechanism accepted decision D11 names. The
# published record has no instant field at all, so there is nowhere for a stated
# time to enter, and a commit that cannot be resolved is a refusal rather than a
# plausible time.
#
# Second, the numerator can only be an acceptance. A checkpoint enters these
# facts exactly when a published record says it is the accepted one, which
# happens only when the orchestrator publishes that record; the act of pushing a
# checkpoint to the product remote touches nothing here. Accepted checkpoints
# must arrive strictly increasing, and there is no published shape a not-yet-
# accepted checkpoint could occupy. One published version may advance the
# accepted checkpoint by more than one, and every checkpoint that advance makes
# accepted is derived out of it -- see `_newly_accepted` -- so a checkpoint the
# product accepted cannot go missing from these facts just because one event
# accepted several.
#
# Third, a projection is measured against evidence rather than against a claim.
# The basis of a projection is the accepted checkpoint standing in the very
# record that carries it, so the checkpoint an estimate was measured against is
# read from the same durable version rather than asserted by a caller -- which is
# what lets a later reader tell a denominator that was revised from a denominator
# that was merely consumed by progress.
#
# Fourth, every published version is one reconsideration. The published record
# cannot express an acceptance without restating the projection, so "reconsidered
# and preserved" is a fact rather than an inference, and "did not reconsider" is
# not representable.
#
# Fifth, reading refuses rather than repairs. Every version is validated whole,
# and unknown keys, a regressed checkpoint, a confidence outside the accepted
# three, a note carrying more than one bounded line, or an instant that is not
# what the deterministic mechanism emits are all refusals carrying an exact
# reason. Nothing is defaulted, filled, reordered or silently dropped, because a
# reader that quietly repaired itself would produce a figure nobody could check
# against the commit it claims to come from.

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from .progress_record import (
    CONFIDENCES,
    MAX_PROJECTION_NOTE,
    PROGRESS_FILENAME,
    ProgressRecordError,
    SCHEMA_VERSION,
    progress_relative,
    validate_document,
)

__all__ = [
    "Acceptance",
    "CONFIDENCES",
    "MAX_PROJECTION_NOTE",
    "NamedCompletion",
    "PROGRESS_FILENAME",
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
    "REASON_TIMESTAMP_UNAVAILABLE",
    "REASON_UNREADABLE_STORE",
    "SCHEMA_VERSION",
    "commit_instant",
    "progress_relative",
]

REASON_MALFORMED_STORE = "malformed-progress-store"
REASON_UNREADABLE_STORE = "unreadable-progress-store"
REASON_INVALID_CHECKPOINT = "invalid-checkpoint"
REASON_CHECKPOINT_REGRESSED = "checkpoint-regressed"
REASON_INVALID_CONFIDENCE = "invalid-confidence"
REASON_INVALID_REMAINING = "invalid-projected-remaining"
REASON_INVALID_NOTE = "invalid-projection-note"
REASON_INVALID_COMMIT = "invalid-commit"
REASON_TIMESTAMP_UNAVAILABLE = "acceptance-timestamp-unavailable"
REASON_NAMED_OUT_OF_ORDER = "named-checkpoint-out-of-order"
REASON_INVALID_NAMED_TOTAL = "invalid-named-total"

# Exactly what `git log -1 --format=%cI` emits, which is strict ISO 8601 in
# either of the two forms git actually produces: an explicit numeric offset, and
# the bare `Z` it prints instead of `+00:00` for UTC. Both are accepted because
# both are observed output of the named mechanism -- accepting only the offset
# form would refuse every commit made on a UTC host.
_GIT_ISO = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})\Z")

_OBJECT_NAME = re.compile(r"\A[0-9a-f]{40}\Z")


class ProgressStoreError(Exception):
    """A refusal to read a progress fact, carrying one stable reason."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__("{0}: {1}".format(reason, detail))
        self.reason = reason
        self.detail = detail


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

    `basis` is the accepted numeric checkpoint standing in the same published
    record, so the checkpoint an estimate was measured against is read from
    durable state rather than supplied. It is what makes `basis + remaining` --
    the projected final this entry implied -- comparable against the previous
    entry's, which is the whole mechanism by which a revised denominator is told
    apart from a denominator progress consumed.
    """

    remaining: int
    basis: int
    confidence: str
    note: str
    commit: str
    recorded_at: str


class ProgressFacts(NamedTuple):
    """Everything one read of the published history returns, from one enumeration.

    One call, one derivation. Assembling these from three separate reads would
    let a projection published midway through be paired with an older acceptance
    list, and the basis arithmetic above is exactly what that would corrupt.
    """

    acceptances: Tuple[Acceptance, ...]
    named: Tuple[NamedCompletion, ...]
    projections: Tuple[Projection, ...]


def _exact_instant(value: object, *, label: str) -> str:
    if type(value) is not str or not _GIT_ISO.match(value):
        raise ProgressStoreError(
            REASON_TIMESTAMP_UNAVAILABLE,
            "{0} must be the instant `git log -1 --format=%cI` emits, got {1!r}".format(
                label, value
            ),
        )
    return value


# --------------------------------------------------------------------------
# The deterministic instant
# --------------------------------------------------------------------------


def commit_instant(repo_root: Path, commit: str) -> str:
    """One commit's instant, from the deterministic control-plane mechanism.

    This is the only place any instant in these facts is produced. It runs the
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
    checked = _exact_object_name(commit)
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


def _exact_object_name(value: object) -> str:
    if type(value) is not str or not _OBJECT_NAME.match(value):
        raise ProgressStoreError(
            REASON_INVALID_COMMIT,
            "a progress fact is sourced from one full 40-character commit object "
            "name, got {0!r}".format(value),
        )
    return value


# --------------------------------------------------------------------------
# The store
# --------------------------------------------------------------------------


def _newly_accepted(checkpoint: int, acceptances: List["Acceptance"]) -> range:
    """Which numeric checkpoints one published record newly makes accepted.

    The record is a compact current-state artifact: it names the checkpoint that
    stands accepted, not the list of every checkpoint that ever did. The list is
    the difference between consecutive published versions, which is why there is
    no event log here to keep in step with anything.

    A version that advances the accepted checkpoint from N to M makes every
    checkpoint in N+1 .. M accepted, because one acceptance event is what made
    each of them accepted; they therefore share that event's commit and its
    instant, which is the honest source for all of them. Deriving only M would
    lose the ones stepped over -- they would be accepted in the product and
    absent from every figure computed here.

    Nothing is invented by that. The supported action proves the whole range
    against the product lineage before publishing a record that implies it, and
    refuses a jump product history does not carry. The very first acceptance has
    no predecessor to measure from, so it stands for itself alone and claims no
    earlier checkpoint. A republished record that restates the standing
    checkpoint accepts nothing new; one that goes backwards is a refusal.
    """
    if not acceptances:
        return range(checkpoint, checkpoint + 1)
    standing = acceptances[-1].checkpoint
    if checkpoint < standing:
        raise ProgressStoreError(
            REASON_CHECKPOINT_REGRESSED,
            "accepted checkpoints are published strictly increasing; {0} "
            "follows {1}".format(checkpoint, standing),
        )
    return range(standing + 1, checkpoint + 1)


class ProgressStore:
    """One ticket's published progress record, and every version of it.

    Constructing this opens nothing, creates nothing and reads nothing: it names
    a coordination repository and one path inside it, exactly as the accepted
    allowance store names a file, so a run may hold one whether or not any
    progress has ever been published.

    Nothing is cached between calls. Every read enumerates the history again, so
    a page served after a new acceptance shows that acceptance, and two readers
    of one repository always see the same evidence.

    This class cannot write. Progress is published by the orchestrator through
    the supported control-plane action; there is no recording method here to call
    instead, and therefore no way for a fact to exist in these figures without
    the durable transition that produced it.
    """

    def __init__(self, repo_root: Path, relative: str) -> None:
        self.repo_root = Path(repo_root)
        self.relative = str(relative)

    @classmethod
    def for_scope(cls, repo_root: Path, *, project: str, ticket: str) -> "ProgressStore":
        """The published record of one project/ticket scope in one repository."""
        return cls(repo_root, progress_relative(project, ticket))

    # -- reading ----------------------------------------------------------

    def facts(self) -> ProgressFacts:
        """Every published fact, derived from every version, validated whole.

        A record that was never published is not a failure: it is a ticket
        nothing has been said about yet, and it reads as three empty tuples.
        Everything else wrong with the history is a refusal carrying its exact
        reason.
        """
        versions = self._versions()
        acceptances: List[Acceptance] = []
        named: List[NamedCompletion] = []
        projections: List[Projection] = []

        accepted_checkpoints = set()
        for commit, instant, document in versions:
            accepted = document["accepted"]
            if accepted is not None:
                for number in _newly_accepted(accepted["checkpoint"], acceptances):
                    acceptances.append(
                        Acceptance(checkpoint=number, commit=commit, accepted_at=instant)
                    )
                    accepted_checkpoints.add(number)

            completion = document["named"]
            if completion is not None and (
                not named or completion["checkpoint"] != named[-1].checkpoint
            ):
                entry = NamedCompletion(
                    checkpoint=completion["checkpoint"],
                    total=completion["total"],
                    commit=commit,
                    completed_at=instant,
                )
                if named and entry.checkpoint != named[-1].checkpoint + 1:
                    raise ProgressStoreError(
                        REASON_NAMED_OUT_OF_ORDER,
                        "completed named checkpoints are the contiguous prefix of the "
                        "roadmap; {0} follows {1}".format(
                            entry.checkpoint, named[-1].checkpoint
                        ),
                    )
                named.append(entry)

            projection = document["projection"]
            basis = 0 if accepted is None else accepted["checkpoint"]
            if basis != 0 and basis not in accepted_checkpoints:
                raise ProgressStoreError(
                    REASON_INVALID_CHECKPOINT,
                    "a projection is measured against checkpoint {0}, which this "
                    "history has no acceptance for".format(basis),
                )
            projections.append(
                Projection(
                    remaining=projection["remaining"],
                    basis=basis,
                    confidence=projection["confidence"],
                    note=projection["note"],
                    commit=commit,
                    recorded_at=instant,
                )
            )

        return ProgressFacts(
            acceptances=tuple(acceptances),
            named=tuple(named),
            projections=tuple(projections),
        )

    # -- the published history --------------------------------------------

    def _versions(self) -> List[Tuple[str, str, Dict[str, Any]]]:
        """Every published version of this record, oldest first, validated whole.

        Three read-only git reads and no more: one enumeration of the commits
        that changed this path, one batch that hands back every one of those
        versions' content, and the accepted instant convention per commit. None
        of them moves a ref, an index or a worktree.
        """
        commits = self._commits()
        if not commits:
            return []
        blobs = self._blobs(commits)
        versions: List[Tuple[str, str, Dict[str, Any]]] = []
        for commit in commits:
            instant = commit_instant(self.repo_root, commit)
            versions.append((commit, instant, self._document(commit, blobs[commit])))
        return versions

    def _commits(self) -> List[str]:
        """The commits that changed this record, oldest first.

        A repository with no history at all is not a failure: it is a scope
        nothing has been published into yet, and it enumerates as nothing. A
        coordination repository that is not there, or is not a repository, is a
        different fact and is refused as one -- "nothing has been published" and
        "the evidence could not be read" must not look alike on the page.
        """
        try:
            completed = self._run(["log", "--reverse", "--format=%H", "--", self.relative])
        except ProgressStoreError:
            if self._has_history() or not self._is_repository():
                raise
            return []
        commits = []
        for line in completed.splitlines():
            text = line.strip()
            if text:
                commits.append(_exact_object_name(text))
        return commits

    def _has_history(self) -> bool:
        return self._succeeds(["rev-parse", "--verify", "--quiet", "HEAD"])

    def _is_repository(self) -> bool:
        return self._succeeds(["rev-parse", "--git-dir"])

    def _succeeds(self, arguments: List[str]) -> bool:
        try:
            completed = subprocess.run(
                ["git", "-C", str(self.repo_root), *arguments],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except OSError:
            return False
        return completed.returncode == 0

    def _blobs(self, commits: List[str]) -> Dict[str, str]:
        """Every named version's content, from one batch read of the object store.

        Read as bytes and decoded here, because `git cat-file --batch` measures
        each entry in bytes and a decode applied to the whole stream would let a
        multi-byte character shift where the next entry starts. Undecodable bytes
        become replacement characters, so a corrupted version fails the JSON and
        schema checks below and is stated as malformed rather than raised through
        the page.
        """
        request = "".join("{0}:{1}\n".format(commit, self.relative) for commit in commits)
        try:
            completed = subprocess.run(
                ["git", "-C", str(self.repo_root), "cat-file", "--batch"],
                input=request.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except OSError as exc:
            raise ProgressStoreError(
                REASON_UNREADABLE_STORE,
                "cannot run git in {0}: {1}".format(self.repo_root, exc),
            ) from exc
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", "replace").strip() or "exit code {0}".format(
                completed.returncode
            )
            raise ProgressStoreError(
                REASON_UNREADABLE_STORE,
                "cannot read {0} in {1}: {2}".format(self.relative, self.repo_root, detail),
            )
        return self._batched(completed.stdout, commits)

    def _batched(self, output: bytes, commits: List[str]) -> Dict[str, str]:
        """Split one `cat-file --batch` response into one version per commit.

        The response is self-describing -- each entry announces its own byte
        length -- so it is walked by that length rather than by looking for a
        separator, which content could otherwise contain.
        """
        blobs: Dict[str, str] = {}
        position = 0
        for commit in commits:
            end = output.find(b"\n", position)
            if end == -1:
                raise ProgressStoreError(
                    REASON_UNREADABLE_STORE,
                    "git cat-file did not describe the version of {0} at {1}".format(
                        self.relative, commit
                    ),
                )
            header = output[position:end].split()
            if len(header) != 3 or header[1] != b"blob":
                raise ProgressStoreError(
                    REASON_MALFORMED_STORE,
                    "{0} at {1} is not a published file: git reported {2!r}".format(
                        self.relative, commit, output[position:end].decode("utf-8", "replace")
                    ),
                )
            try:
                size = int(header[2])
            except ValueError as exc:
                raise ProgressStoreError(
                    REASON_UNREADABLE_STORE,
                    "git cat-file described {0} at {1} unusably".format(self.relative, commit),
                ) from exc
            start = end + 1
            blobs[commit] = output[start : start + size].decode("utf-8", "replace")
            position = start + size + 1
        return blobs

    def _document(self, commit: str, text: str) -> Dict[str, Any]:
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise ProgressStoreError(
                REASON_MALFORMED_STORE,
                "{0} at {1} is not valid JSON: {2}".format(self.relative, commit, exc),
            ) from exc
        try:
            return validate_document(payload)
        except ProgressRecordError as exc:
            raise ProgressStoreError(_store_reason(exc.reason), exc.detail) from exc

    def _run(self, arguments: List[str]) -> str:
        try:
            completed = subprocess.run(
                ["git", "-C", str(self.repo_root), *arguments],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except OSError as exc:
            raise ProgressStoreError(
                REASON_UNREADABLE_STORE, "cannot run git in {0}: {1}".format(self.repo_root, exc)
            ) from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip() or "exit code {0}".format(completed.returncode)
            raise ProgressStoreError(
                REASON_UNREADABLE_STORE,
                "cannot read the published progress of {0} in {1}: {2}".format(
                    self.relative, self.repo_root, detail
                ),
            )
        return completed.stdout


# The published record and these facts refuse the same things; the reason a
# reader states is its own, so one vocabulary reaches the page.
_RECORD_REASONS = {
    "malformed-progress-record": REASON_MALFORMED_STORE,
    "invalid-checkpoint": REASON_INVALID_CHECKPOINT,
    "invalid-confidence": REASON_INVALID_CONFIDENCE,
    "invalid-projected-remaining": REASON_INVALID_REMAINING,
    "invalid-projection-note": REASON_INVALID_NOTE,
    "invalid-commit": REASON_INVALID_COMMIT,
    "invalid-named-total": REASON_INVALID_NAMED_TOTAL,
}


def _store_reason(reason: str) -> str:
    return _RECORD_REASONS.get(reason, REASON_MALFORMED_STORE)

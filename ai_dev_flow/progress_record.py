"""The published shape of one ticket's current progress state, and nothing else.

This module is the one definition of what an orchestrator may durably say about
progress, shared by the two sides that must agree about it: `control_plane`,
which validates a record on the way into the coordination repository, and
`progress_store`, which validates every version it reads back out. Two copies of
these rules in those two modules would be two rules free to drift, and a reader
that accepted a record its writer would have refused is exactly how a percentage
stops being checkable against the commit it claims to come from.

It holds values and refusals only. There is no store, no view, no measure, no
percentage and no clock here -- nothing a decision could be made from -- which is
why the writing side may import it without importing telemetry.

Four properties are structural rather than asked for.

First, the record is *current state*, not a log. It says what the last accepted
numeric checkpoint is, which named checkpoint is complete, and what the
orchestrator now projects as remaining. It carries no history and no instant at
all, because the coordination repository already dates every version of it: the
commit that published a record is when that record became true, and a timestamp
written *into* the record would be a second answer to a question Git has already
answered deterministically.

Second, a projection is not optional. Accepted decision D11 asks the orchestrator
to reconsider the remaining count whenever a checkpoint is accepted, and this
record cannot express an acceptance without one -- so "reconsidered and
preserved" and "reconsidered and revised" are both facts, and "did not
reconsider" is not representable.

Third, the key set is closed and a record carrying anything else is refused
rather than trimmed. There is no field through which a transcript, a session, a
token figure or a wall-clock duration could arrive.

Fourth, the two facts are not independent. A named completion is carried by the
numeric checkpoints that completed it, so a record may not claim one while
claiming that nothing has been accepted at all. That is a cross-check between
fields rather than a rule about either, which is why it lives here with them and
not in the writer: a record with a named claim standing on nothing is one this
package refuses to read for the same reason it refuses to publish it.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

__all__ = [
    "ACCEPTED_KEYS",
    "CONFIDENCES",
    "DOCUMENT_KEYS",
    "MAX_PROJECTION_NOTE",
    "NAMED_KEYS",
    "PROGRESS_FILENAME",
    "PROJECTION_KEYS",
    "ProgressRecordError",
    "REASON_INVALID_CHECKPOINT",
    "REASON_INVALID_COMMIT",
    "REASON_INVALID_CONFIDENCE",
    "REASON_INVALID_NAMED_TOTAL",
    "REASON_INVALID_NOTE",
    "REASON_INVALID_REMAINING",
    "REASON_MALFORMED_RECORD",
    "REASON_UNANCHORED_NAMED",
    "SCHEMA_VERSION",
    "empty_document",
    "exact_checkpoint",
    "exact_commit",
    "exact_confidence",
    "exact_keys",
    "exact_note",
    "exact_remaining",
    "progress_relative",
    "validate_document",
]

SCHEMA_VERSION = 1

# The published artifact's filename. `control_plane` composes its path from this
# and so does `progress_relative` below, so the writer and the reader cannot end
# up naming two different files.
PROGRESS_FILENAME = "progress.json"

# Exactly the three accepted decision D11 permits. Not an ordering, not a score,
# and deliberately not a number: a confidence that could be compared numerically
# is a confidence something could threshold on.
CONFIDENCES = ("low", "medium", "high")

# One bounded line saying why the projection changed. Bounded and single-line on
# purpose: the "why" D11 asks for is a sentence a person reads beside a
# percentage, and anything longer is the execution diary D11 forbids.
MAX_PROJECTION_NOTE = 240

DOCUMENT_KEYS: Tuple[str, ...] = ("accepted", "named", "projection", "schemaVersion")
ACCEPTED_KEYS: Tuple[str, ...] = ("checkpoint", "commit")
NAMED_KEYS: Tuple[str, ...] = ("checkpoint", "total")
PROJECTION_KEYS: Tuple[str, ...] = ("confidence", "note", "remaining")

REASON_MALFORMED_RECORD = "malformed-progress-record"
REASON_INVALID_CHECKPOINT = "invalid-checkpoint"
REASON_INVALID_CONFIDENCE = "invalid-confidence"
REASON_INVALID_REMAINING = "invalid-projected-remaining"
REASON_INVALID_NOTE = "invalid-projection-note"
REASON_INVALID_COMMIT = "invalid-commit"
REASON_INVALID_NAMED_TOTAL = "invalid-named-total"
REASON_UNANCHORED_NAMED = "unanchored-named-completion"

# A full object name and nothing else. An abbreviation, a branch, `HEAD`, or a
# reflog expression would each resolve to a different commit on a different day,
# which is the opposite of a deterministic source.
_COMMIT = re.compile(r"\A[0-9a-f]{40}\Z")


class ProgressRecordError(Exception):
    """A refusal to accept one progress record, carrying one stable reason."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__("{0}: {1}".format(reason, detail))
        self.reason = reason
        self.detail = detail


def progress_relative(project: str, ticket: str) -> str:
    """Where one ticket's published progress record lives, relative to the repo."""
    return "{0}/{1}/{2}".format(project, ticket, PROGRESS_FILENAME)


def exact_keys(payload: object, expected: Tuple[str, ...], *, label: str) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ProgressRecordError(
            REASON_MALFORMED_RECORD,
            "{0} must be an object, got {1}".format(label, type(payload).__name__),
        )
    present = tuple(sorted(payload))
    if present != tuple(sorted(expected)):
        raise ProgressRecordError(
            REASON_MALFORMED_RECORD,
            "{0} must carry exactly {1}, got {2}".format(
                label, ", ".join(sorted(expected)), ", ".join(present) or "nothing"
            ),
        )
    return payload


def exact_checkpoint(value: object, *, label: str) -> int:
    """A whole positive checkpoint number, refusing every near-miss.

    `type(...) is not int` rather than `isinstance`, because `True` is an `int`
    and a boolean checkpoint is a bug that would otherwise number itself 1.
    """
    if type(value) is not int or value < 1:
        raise ProgressRecordError(
            REASON_INVALID_CHECKPOINT,
            "{0} must be a whole checkpoint number of at least 1, got {1!r}".format(label, value),
        )
    return value


def exact_commit(value: object) -> str:
    if type(value) is not str or not _COMMIT.match(value):
        raise ProgressRecordError(
            REASON_INVALID_COMMIT,
            "a progress fact is sourced from one full 40-character commit object "
            "name, got {0!r}".format(value),
        )
    return value


def exact_confidence(value: object) -> str:
    if value not in CONFIDENCES:
        raise ProgressRecordError(
            REASON_INVALID_CONFIDENCE,
            "confidence is exactly one of {0}, got {1!r}".format(", ".join(CONFIDENCES), value),
        )
    return value


def exact_remaining(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ProgressRecordError(
            REASON_INVALID_REMAINING,
            "projected remaining must be a whole count of at least 0, got {0!r}".format(value),
        )
    return value


def exact_note(value: object) -> str:
    """One bounded line, or nothing. Never a paragraph and never a log line."""
    if type(value) is not str:
        raise ProgressRecordError(
            REASON_INVALID_NOTE,
            "a projection note is one bounded line of text, got {0}".format(type(value).__name__),
        )
    if len(value) > MAX_PROJECTION_NOTE:
        raise ProgressRecordError(
            REASON_INVALID_NOTE,
            "a projection note is at most {0} characters, got {1}".format(
                MAX_PROJECTION_NOTE, len(value)
            ),
        )
    if value != value.strip() or any(character in value for character in "\r\n\t"):
        raise ProgressRecordError(
            REASON_INVALID_NOTE,
            "a projection note is a single trimmed line; a multi-line note is the "
            "execution diary this record does not keep",
        )
    return value


def empty_document() -> Dict[str, Any]:
    """The record a ticket has before anything has been published about it."""
    return {
        "schemaVersion": SCHEMA_VERSION,
        "accepted": None,
        "named": None,
        "projection": None,
    }


def validate_document(payload: object) -> Dict[str, Any]:
    """One whole record, checked whole, returned in its canonical shape.

    The same function runs on the way in and on the way out. A record this
    package would refuse to read is a record it must refuse to publish, and two
    similar checks in two places is how those two answers drift apart.
    """
    document = exact_keys(payload, DOCUMENT_KEYS, label="the progress record")
    version = document.get("schemaVersion")
    if version != SCHEMA_VERSION:
        raise ProgressRecordError(
            REASON_MALFORMED_RECORD,
            "progress record version {0!r} is not the supported {1}".format(
                version, SCHEMA_VERSION
            ),
        )

    accepted: Optional[Dict[str, Any]] = None
    if document["accepted"] is not None:
        record = exact_keys(document["accepted"], ACCEPTED_KEYS, label="the accepted checkpoint")
        accepted = {
            "checkpoint": exact_checkpoint(record["checkpoint"], label="an accepted checkpoint"),
            "commit": exact_commit(record["commit"]),
        }

    named: Optional[Dict[str, Any]] = None
    if document["named"] is not None:
        record = exact_keys(document["named"], NAMED_KEYS, label="the named checkpoint")
        completed = exact_checkpoint(record["checkpoint"], label="a named checkpoint")
        total = exact_checkpoint(record["total"], label="the named roadmap size")
        if completed > total:
            raise ProgressRecordError(
                REASON_INVALID_NAMED_TOTAL,
                "named checkpoint {0} cannot be completed on a roadmap of {1}".format(
                    completed, total
                ),
            )
        named = {"checkpoint": completed, "total": total}

    if named is not None and accepted is None:
        # A named checkpoint is completed *by* accepted numeric checkpoints, so a
        # record asserting one while asserting that nothing has been accepted at
        # all is internally incoherent -- and it is the one state in which a
        # named claim rests on nothing durable. The reader derives the checkpoint
        # now in progress from the completions it finds, so an unanchored first
        # completion of 7 would make the served surface assert named checkpoint 8
        # with no accepted checkpoint behind it.
        #
        # This is deliberately not an anchor at named checkpoint 1. The record may
        # be adopted part-way through a roadmap, and requiring the first recorded
        # completion to be 1 would mean publishing the earlier ones with commits
        # and instants that would have to be invented. The anchor asked for here
        # is the one thing this package already proves against product history.
        raise ProgressRecordError(
            REASON_UNANCHORED_NAMED,
            "named checkpoint {0} of {1} is completed with no accepted numeric "
            "checkpoint in the same record; a named completion is carried by the "
            "checkpoints that completed it, never by the claim alone".format(
                named["checkpoint"], named["total"]
            ),
        )

    if document["projection"] is None:
        raise ProgressRecordError(
            REASON_MALFORMED_RECORD,
            "a progress record always restates the projection; D11 asks that the "
            "estimate be reconsidered at every acceptance, so an absent one is not "
            "a record this package can publish",
        )
    record = exact_keys(document["projection"], PROJECTION_KEYS, label="the projection")
    projection = {
        "confidence": exact_confidence(record["confidence"]),
        "note": exact_note(record["note"]),
        "remaining": exact_remaining(record["remaining"]),
    }

    return {
        "schemaVersion": SCHEMA_VERSION,
        "accepted": accepted,
        "named": named,
        "projection": projection,
    }

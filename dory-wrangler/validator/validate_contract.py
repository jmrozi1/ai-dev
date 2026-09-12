#!/usr/bin/env python3
"""Deterministic validator for the Dory-wrangler v0.1 chat/session/agent contract.

Normative source: dory-wrangler/contract/v0.1/contract.md

This validator is the executable form of that contract. It reads fixture files
(store snapshots), validates every record and every cross-record invariant, and
compares the result against the fixture's declared expectation.

Stdlib only. Deterministic: violations are emitted in a stable sorted order and
the tool takes no input other than the fixture files given on the command line.

Usage:
    python3 dory-wrangler/validator/validate_contract.py <path> [<path> ...]

Exit status:
    0  every fixture matched its declared expectation
    1  at least one fixture did not match its declared expectation
    2  usage or input error (unreadable path, undecodable JSON, no fixtures)
"""

from __future__ import annotations

import json
import os
import re
import sys

CONTRACT_VERSION = "0.1"

# ---------------------------------------------------------------------------
# Contract constants
# ---------------------------------------------------------------------------

ID_PREFIXES = {
    "chat_id": "cht",
    "message_id": "msg",
    "session_id": "ses",
    "binding_id": "bnd",
    "event_id": "evt",
    "request_id": "req",
    "delivery_id": "dlv",
    "observation_id": "obs",
}

ID_RE = re.compile(r"^(cht|msg|ses|bnd|evt|req|dlv|obs)_[0-9a-z]{8,32}$")
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")
LAUNCHER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

SUPPORTED_RECORD_VERSION = 1

RECORD_TYPES = (
    "chat",
    "message",
    "agent_session",
    "agent_binding",
    "launch_request",
    "launch_result",
    "delivery_request",
    "session_observation",
    "diagnostic_event",
)

SESSION_STATES = (
    "pending",
    "launching",
    "running",
    "completed",
    "failed",
    "launch_failed",
    "terminated",
    "unknown",
    "abandoned",
)

TERMINAL_SESSION_STATES = frozenset(
    ("completed", "failed", "launch_failed", "terminated", "abandoned")
)

TRANSITION_OWNERS = ("user", "harness", "launcher")

# (from_state, to_state) -> sole authorized owner.
AUTHORIZED_TRANSITIONS = {
    (None, "pending"): "user",
    ("pending", "launching"): "harness",
    ("pending", "launch_failed"): "harness",
    ("launching", "running"): "launcher",
    ("launching", "launch_failed"): "launcher",
    ("launching", "unknown"): "launcher",
    ("running", "completed"): "launcher",
    ("running", "failed"): "launcher",
    ("running", "terminated"): "user",
    ("running", "unknown"): "launcher",
    ("unknown", "running"): "launcher",
    ("unknown", "completed"): "launcher",
    ("unknown", "failed"): "launcher",
    ("unknown", "terminated"): "user",
    ("unknown", "abandoned"): "user",
}

EVIDENCE_KINDS = (
    "event",
    "stream_end",
    "launch_result",
    "observation",
    "user_action",
    "harness_action",
)

# Evidence kinds that count as an explicit observation of the integration.
OBSERVED_EVIDENCE_KINDS = frozenset(("event", "stream_end", "launch_result", "observation"))

# session_observation kinds. Each records the outcome of an attempted boundary
# interaction. None of them is an inference from elapsed time or from silence.
OBSERVATION_KINDS = (
    "stop_confirmed",
    "stop_unconfirmed",
    "reattached",
    "reattach_failed",
    "stream_read_failed",
)

# Observations that leave the agent's liveness undeterminable.
UNRESOLVED_OBSERVATION_KINDS = frozenset(
    ("stop_unconfirmed", "reattach_failed", "stream_read_failed")
)

# Observation kinds that can only be produced by an operation that addressed an
# already-launched agent. Contract 6.1 gives 'stop', 'events' and 'deliver' the
# agent_handle as their only address, so the harness cannot have issued any of
# these without holding one.
#
# 'reattach_failed' is deliberately absent. A session interrupted in 'launching'
# never received a handle, so there is nothing to address and a failed
# re-attachment is the honest record of exactly that (contract 5.4). Requiring a
# handle for it would reject a true history, which is the failure mode this
# contract has already made once.
ADDRESSING_OBSERVATION_KINDS = frozenset(
    ("stop_confirmed", "stop_unconfirmed", "reattached", "stream_read_failed")
)

CONTINUATION_MODES = ("persistent", "fresh_binding")

RESPONSE_SHAPES = ("stream", "one_shot")

# The interpreted_type a launcher-sourced event must carry to be usable as the
# end-of-stream signal section 6.1 requires.
STREAM_END_TYPE = "stream_end"

LAUNCH_OUTCOMES = ("accepted", "failed", "unknown")

LAUNCH_FAILURE_CATEGORIES = (
    "invalid_request",
    "unavailable",
    "rejected",
    "no_acknowledgement",
    "internal_error",
)

EVENT_INTERPRETATIONS = ("recognized", "unrecognized", "malformed")

EVENT_SOURCES = ("launcher", "agent", "harness")

MESSAGE_AUTHORS = ("user", "agent", "system")

# Contract section 5.2: the precondition column, in executable form.
#
# (from_state, to_state) -> {admissible evidence kind: what evidence.ref must
# resolve to}. An evidence kind absent from the mapping is not admissible for
# that transition; a ref that does not satisfy the named requirement means the
# precondition was not met. The requirement tokens are handled in
# _check_evidence_requirement.
TRANSITION_PRECONDITIONS = {
    (None, "pending"): {"user_action": "user_message"},
    ("pending", "launching"): {"harness_action": "launch_request"},
    ("pending", "launch_failed"): {"harness_action": "no_ref"},
    ("launching", "running"): {"launch_result": "result_accepted"},
    ("launching", "launch_failed"): {"launch_result": "result_failed"},
    ("launching", "unknown"): {
        "launch_result": "result_unknown",
        "observation": "observation_unresolved",
    },
    ("running", "completed"): {"event": "event_recognized"},
    ("running", "failed"): {"event": "event_recognized"},
    ("running", "terminated"): {"observation": "observation_stop_confirmed"},
    ("running", "unknown"): {
        "event": "event_any",
        "stream_end": "event_stream_end",
        "observation": "observation_unresolved",
    },
    ("unknown", "running"): {
        "event": "event_recognized",
        "observation": "observation_reattached",
    },
    ("unknown", "completed"): {"event": "event_recognized"},
    ("unknown", "failed"): {"event": "event_recognized"},
    ("unknown", "terminated"): {"observation": "observation_stop_confirmed"},
    ("unknown", "abandoned"): {"user_action": "no_ref"},
}

# Anti-drift guard. The authorized-transition table and the precondition table
# are two halves of contract section 5.2's table; a row present in one and not
# the other is exactly the prose/executable divergence this contract exists to
# prevent, and it is a programming error rather than a store violation.
assert set(TRANSITION_PRECONDITIONS) == set(AUTHORIZED_TRANSITIONS), (
    "section 5.2 owner table and precondition table disagree: %r"
    % sorted(set(TRANSITION_PRECONDITIONS) ^ set(AUTHORIZED_TRANSITIONS))
)

# There is deliberately no MAX_INSTRUCTION_BYTES constant here. No
# instruction-payload bound is asserted by this contract: the bound is a measured
# property of a launcher, declared per session in
# agent_session.launcher_capabilities.instruction_bound_bytes, and its only
# honest value today is null. An earlier revision hard-coded 65536, a number
# invented in the contract document and enforced as though measured.
# See facts-and-assumptions.md, known-unproven claim U2.
MAX_TITLE_CHARS = 200

# Field names that leak host, transport, or bridge mechanics into the launch
# boundary. The launch packet must be hostable unchanged by both the external
# Linux development launcher and the internal VS Code/network bridge launcher.
BRIDGE_SPECIFIC_FIELDS = frozenset(
    (
        "argv",
        "command",
        "container",
        "cwd",
        "display",
        "endpoint",
        "env",
        "environment",
        "extension_id",
        "host",
        "hostname",
        "path",
        "pid",
        "port",
        "script_path",
        "shell",
        "socket",
        "ssh",
        "terminal_id",
        "url",
        "vscode_workspace",
        "workspace_path",
        "working_directory",
    )
)

# ---------------------------------------------------------------------------
# Record field specifications
# ---------------------------------------------------------------------------
# Each entry: field -> (kind, required)
#   kind is one of: str, int, bool, obj, list, id:<prefix>, id?:<prefix>,
#                   ts, ts?, enum:<a|b|c>, str?

RECORD_SPECS = {
    "chat": {
        "chat_id": ("id:cht", True),
        "title": ("str", True),
        "created_at": ("ts", True),
        "updated_at": ("ts", True),
        "state": ("enum:open|archived", True),
    },
    "message": {
        "message_id": ("id:msg", True),
        "chat_id": ("id:cht", True),
        "sequence": ("int", True),
        "author": ("enum:" + "|".join(MESSAGE_AUTHORS), True),
        "created_at": ("ts", True),
        "content": ("obj", True),
        "session_id": ("id?:ses", True),
        "source_event_id": ("id?:evt", True),
    },
    "agent_session": {
        "session_id": ("id:ses", True),
        "chat_id": ("id:cht", True),
        "created_at": ("ts", True),
        "launcher_id": ("str", True),
        "launcher_capabilities": ("obj", True),
        "state": ("enum:" + "|".join(SESSION_STATES), True),
        "agent_handle": ("str?", False),
        "transitions": ("list", True),
    },
    "agent_binding": {
        "binding_id": ("id:bnd", True),
        "chat_id": ("id:cht", True),
        "session_id": ("id:ses", True),
        "bound_at": ("ts", True),
        "released_at": ("ts?", True),
    },
    "launch_request": {
        "request_id": ("id:req", True),
        "chat_id": ("id:cht", True),
        "session_id": ("id:ses", True),
        "created_at": ("ts", True),
        "instruction_encoding": ("enum:utf-8", True),
        "instruction_text": ("str", True),
    },
    "launch_result": {
        "request_id": ("id:req", True),
        "session_id": ("id:ses", True),
        "observed_at": ("ts", True),
        "outcome": ("enum:" + "|".join(LAUNCH_OUTCOMES), True),
        "agent_handle": ("str?", False),
        "failure_category": ("str?", False),
        "detail": ("str?", False),
    },
    "delivery_request": {
        "delivery_id": ("id:dlv", True),
        "chat_id": ("id:cht", True),
        "session_id": ("id:ses", True),
        "sequence": ("int", True),
        "created_at": ("ts", True),
        "instruction_encoding": ("enum:utf-8", True),
        "instruction_text": ("str", True),
        "acknowledged": ("bool?", True),
    },
    "session_observation": {
        "observation_id": ("id:obs", True),
        "chat_id": ("id:cht", True),
        "session_id": ("id:ses", True),
        "observed_at": ("ts", True),
        "kind": ("enum:" + "|".join(OBSERVATION_KINDS), True),
        "detail": ("str?", True),
    },
    "diagnostic_event": {
        "event_id": ("id:evt", True),
        "chat_id": ("id:cht", True),
        "session_id": ("id:ses", True),
        "sequence": ("int", True),
        "received_at": ("ts", True),
        "source": ("enum:" + "|".join(EVENT_SOURCES), True),
        "interpretation": ("enum:" + "|".join(EVENT_INTERPRETATIONS), True),
        "interpreted_type": ("str?", True),
        "raw": ("obj", True),
    },
}

CONTENT_SPEC = {
    "content_type": ("enum:text/plain", True),
    "text": ("str", True),
}

# The capabilities a launcher declares for the session it served. These are
# declared properties of an implementation, never assumptions of this contract.
CAPABILITIES_SPEC = {
    "continuation": ("enum:" + "|".join(CONTINUATION_MODES), True),
    "response_shape": ("enum:" + "|".join(RESPONSE_SHAPES), True),
    "instruction_bound_bytes": ("int?", True),
}

RAW_SPEC = {
    "encoding": ("enum:utf-8|base64", True),
    "body": ("str", True),
}

TRANSITION_SPEC = {
    "from": ("str?", True),
    "to": ("str", True),
    "owner": ("enum:" + "|".join(TRANSITION_OWNERS), True),
    "at": ("ts", True),
    "evidence": ("obj", True),
}

EVIDENCE_SPEC = {
    "kind": ("enum:" + "|".join(EVIDENCE_KINDS), True),
    "ref": ("str?", True),
}


# ---------------------------------------------------------------------------
# Violation collection
# ---------------------------------------------------------------------------


class Report(object):
    def __init__(self):
        self.violations = []

    def add(self, code, where, detail):
        self.violations.append((code, where, detail))

    @property
    def codes(self):
        return set(v[0] for v in self.violations)

    def sorted(self):
        return sorted(self.violations)

    def ok(self):
        return not self.violations


# ---------------------------------------------------------------------------
# Primitive field checks
# ---------------------------------------------------------------------------


def _id_ok(value, prefix):
    return isinstance(value, str) and bool(ID_RE.match(value)) and value.startswith(prefix + "_")


def check_fields(report, where, obj, spec, allow_unknown=False):
    """Validate a mapping against a field spec. Returns True if shape is usable."""
    if not isinstance(obj, dict):
        report.add("BAD_FIELD_TYPE", where, "expected an object")
        return False

    usable = True
    for field in sorted(spec):
        kind, required = spec[field]
        if field not in obj:
            if required:
                report.add("MISSING_FIELD", where, "missing required field '%s'" % field)
                usable = False
            continue
        value = obj[field]

        if kind == "str":
            if not isinstance(value, str) or value == "":
                report.add("BAD_FIELD_TYPE", where, "'%s' must be a non-empty string" % field)
                usable = False
        elif kind == "str?":
            if value is not None and not isinstance(value, str):
                report.add("BAD_FIELD_TYPE", where, "'%s' must be a string or null" % field)
                usable = False
        elif kind == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                report.add("BAD_FIELD_TYPE", where, "'%s' must be an integer" % field)
                usable = False
        elif kind == "int?":
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                report.add("BAD_FIELD_TYPE", where, "'%s' must be an integer or null" % field)
                usable = False
        elif kind == "bool?":
            if value is not None and not isinstance(value, bool):
                report.add("BAD_FIELD_TYPE", where, "'%s' must be true, false, or null" % field)
                usable = False
        elif kind == "obj":
            if not isinstance(value, dict):
                report.add("BAD_FIELD_TYPE", where, "'%s' must be an object" % field)
                usable = False
        elif kind == "list":
            if not isinstance(value, list):
                report.add("BAD_FIELD_TYPE", where, "'%s' must be an array" % field)
                usable = False
        elif kind == "ts":
            if not isinstance(value, str) or not TS_RE.match(value):
                report.add("BAD_TIMESTAMP", where, "'%s' must be RFC3339 UTC ending in Z" % field)
                usable = False
        elif kind == "ts?":
            if value is not None and (not isinstance(value, str) or not TS_RE.match(value)):
                report.add("BAD_TIMESTAMP", where, "'%s' must be RFC3339 UTC ending in Z or null" % field)
                usable = False
        elif kind.startswith("enum:"):
            allowed = kind[len("enum:"):].split("|")
            if value not in allowed:
                report.add(
                    "BAD_ENUM_VALUE",
                    where,
                    "'%s' must be one of %s" % (field, "/".join(allowed)),
                )
                usable = False
        elif kind.startswith("id:"):
            prefix = kind[len("id:"):]
            if not _id_ok(value, prefix):
                report.add("BAD_ID_FORMAT", where, "'%s' must match %s_<8-32 lowercase alnum>" % (field, prefix))
                usable = False
        elif kind.startswith("id?:"):
            prefix = kind[len("id?:"):]
            if value is not None and not _id_ok(value, prefix):
                report.add("BAD_ID_FORMAT", where, "'%s' must be null or match %s_<8-32 lowercase alnum>" % (field, prefix))
                usable = False
        else:  # pragma: no cover - programming error
            raise AssertionError("unknown field kind %r" % kind)

    if not allow_unknown:
        for field in sorted(obj):
            if field in spec or field in ("record_type", "record_version"):
                continue
            if field in BRIDGE_SPECIFIC_FIELDS:
                report.add(
                    "BRIDGE_SPECIFIC_FIELD",
                    where,
                    "field '%s' encodes host, transport, or bridge mechanics" % field,
                )
            else:
                report.add("UNKNOWN_FIELD", where, "unrecognized field '%s'" % field)
            usable = False

    return usable


# ---------------------------------------------------------------------------
# Per-record validation
# ---------------------------------------------------------------------------


# The identifying field of each record type, preferred when naming a location.
OWN_ID_FIELD = {
    "chat": "chat_id",
    "message": "message_id",
    "agent_session": "session_id",
    "agent_binding": "binding_id",
    "launch_request": "request_id",
    "launch_result": "request_id",
    "delivery_request": "delivery_id",
    "session_observation": "observation_id",
    "diagnostic_event": "event_id",
}


def record_where(index, record):
    if isinstance(record, dict):
        rtype = record.get("record_type")
        keys = []
        own = OWN_ID_FIELD.get(rtype)
        if own:
            keys.append(own)
        keys.extend([
            "chat_id", "message_id", "session_id", "binding_id",
            "request_id", "delivery_id", "observation_id", "event_id",
        ])
        for key in keys:
            if isinstance(record.get(key), str):
                return "records[%d] %s %s" % (index, rtype, record[key])
        return "records[%d] %s" % (index, rtype)
    return "records[%d]" % index


def validate_record(report, index, record):
    """Validate one record in isolation. Returns the record type if usable."""
    where = record_where(index, record)

    if not isinstance(record, dict):
        report.add("BAD_FIELD_TYPE", where, "record must be an object")
        return None

    rtype = record.get("record_type")
    if rtype not in RECORD_TYPES:
        report.add(
            "UNKNOWN_RECORD_TYPE",
            where,
            "record_type %r is not defined by contract v%s" % (rtype, CONTRACT_VERSION),
        )
        return None

    rversion = record.get("record_version")
    if isinstance(rversion, bool) or not isinstance(rversion, int):
        report.add("UNKNOWN_RECORD_VERSION", where, "record_version must be an integer")
        return None
    if rversion != SUPPORTED_RECORD_VERSION:
        report.add(
            "UNKNOWN_RECORD_VERSION",
            where,
            "record_version %r is not supported (supported: %d)" % (rversion, SUPPORTED_RECORD_VERSION),
        )
        return None

    # diagnostic_event: raw evidence is checked before generic shape so that a
    # dropped raw body reports as lost evidence rather than a generic omission.
    if rtype == "diagnostic_event":
        raw = record.get("raw")
        if not isinstance(raw, dict) or not isinstance(raw.get("body"), str):
            report.add(
                "RAW_EVIDENCE_MISSING",
                where,
                "raw integration evidence must be preserved verbatim in raw.body",
            )

    check_fields(report, where, record, RECORD_SPECS[rtype])

    if rtype == "chat":
        title = record.get("title")
        if isinstance(title, str) and len(title) > MAX_TITLE_CHARS:
            report.add("FIELD_OUT_OF_RANGE", where, "title exceeds %d characters" % MAX_TITLE_CHARS)
        _check_order(report, where, record.get("created_at"), record.get("updated_at"),
                     "updated_at precedes created_at")

    elif rtype == "message":
        if isinstance(record.get("sequence"), int) and not isinstance(record.get("sequence"), bool):
            if record["sequence"] < 1:
                report.add("FIELD_OUT_OF_RANGE", where, "sequence must be >= 1")
        if isinstance(record.get("content"), dict):
            check_fields(report, where + " content", record["content"], CONTENT_SPEC)

    elif rtype == "agent_session":
        launcher_id = record.get("launcher_id")
        if isinstance(launcher_id, str) and not LAUNCHER_ID_RE.match(launcher_id):
            report.add("BAD_FIELD_TYPE", where, "launcher_id must match [a-z0-9][a-z0-9-]{0,63}")
        capabilities = record.get("launcher_capabilities")
        if isinstance(capabilities, dict):
            check_fields(report, where + " launcher_capabilities", capabilities, CAPABILITIES_SPEC)
            bound = capabilities.get("instruction_bound_bytes")
            if isinstance(bound, int) and not isinstance(bound, bool) and bound < 1:
                report.add(
                    "FIELD_OUT_OF_RANGE",
                    where + " launcher_capabilities",
                    "instruction_bound_bytes must be null (not measured) or >= 1",
                )
        _validate_transitions(report, where, record)

    elif rtype in ("launch_request", "delivery_request"):
        if rtype == "delivery_request":
            seq = record.get("sequence")
            if isinstance(seq, int) and not isinstance(seq, bool) and seq < 1:
                report.add("FIELD_OUT_OF_RANGE", where, "sequence must be >= 1")
        text = record.get("instruction_text")
        if isinstance(text, str):
            try:
                text.encode("utf-8")
            except UnicodeEncodeError:
                report.add("INSTRUCTION_TEXT_NOT_UTF8", where, "instruction_text is not encodable as UTF-8")

    elif rtype == "launch_result":
        _validate_launch_result(report, where, record)

    elif rtype == "diagnostic_event":
        if isinstance(record.get("raw"), dict):
            check_fields(report, where + " raw", record["raw"], RAW_SPEC)
        if isinstance(record.get("sequence"), int) and not isinstance(record.get("sequence"), bool):
            if record["sequence"] < 1:
                report.add("FIELD_OUT_OF_RANGE", where, "sequence must be >= 1")
        interpretation = record.get("interpretation")
        interpreted_type = record.get("interpreted_type")
        if interpretation == "recognized" and not interpreted_type:
            report.add(
                "EVENT_INTERPRETATION_INCONSISTENT",
                where,
                "a recognized event must name its interpreted_type",
            )
        if interpretation in ("unrecognized", "malformed") and interpreted_type is not None:
            report.add(
                "EVENT_INTERPRETATION_INCONSISTENT",
                where,
                "interpreted_type must be null when interpretation is %r" % interpretation,
            )

    return rtype


def _check_order(report, where, earlier, later, message):
    if isinstance(earlier, str) and isinstance(later, str):
        if TS_RE.match(earlier) and TS_RE.match(later) and later < earlier:
            report.add("TIME_REGRESSION", where, message)


def _validate_launch_result(report, where, record):
    outcome = record.get("outcome")
    handle = record.get("agent_handle")
    category = record.get("failure_category")

    if outcome == "accepted":
        if not handle:
            report.add(
                "LAUNCH_RESULT_INCONSISTENT", where,
                "outcome 'accepted' requires a non-empty agent_handle",
            )
        if category is not None:
            report.add(
                "LAUNCH_RESULT_INCONSISTENT", where,
                "outcome 'accepted' must not carry a failure_category",
            )
    elif outcome == "failed":
        if category not in LAUNCH_FAILURE_CATEGORIES:
            report.add(
                "LAUNCH_RESULT_INCONSISTENT", where,
                "outcome 'failed' requires failure_category in %s"
                % "/".join(LAUNCH_FAILURE_CATEGORIES),
            )
        if handle is not None:
            report.add(
                "LAUNCH_RESULT_INCONSISTENT", where,
                "outcome 'failed' must not carry an agent_handle",
            )
    elif outcome == "unknown":
        if handle is not None or category is not None:
            report.add(
                "LAUNCH_RESULT_INCONSISTENT", where,
                "outcome 'unknown' must carry neither agent_handle nor failure_category",
            )


def _validate_transitions(report, where, record):
    transitions = record.get("transitions")
    if not isinstance(transitions, list):
        return
    if not transitions:
        report.add("TRANSITION_CHAIN_BROKEN", where, "a session must record at least its creation transition")
        return

    previous_state = None
    previous_at = None
    usable_chain = True

    for i, transition in enumerate(transitions):
        twhere = "%s transitions[%d]" % (where, i)
        if not check_fields(report, twhere, transition, TRANSITION_SPEC):
            usable_chain = False
            continue

        evidence = transition.get("evidence")
        if isinstance(evidence, dict):
            check_fields(report, twhere + " evidence", evidence, EVIDENCE_SPEC)

        frm = transition.get("from")
        to = transition.get("to")

        if frm is not None and frm not in SESSION_STATES:
            report.add("BAD_ENUM_VALUE", twhere, "'from' must be null or a defined session state")
            usable_chain = False
            continue
        if to not in SESSION_STATES:
            report.add("BAD_ENUM_VALUE", twhere, "'to' must be a defined session state")
            usable_chain = False
            continue

        if usable_chain and frm != previous_state:
            report.add(
                "TRANSITION_CHAIN_BROKEN",
                twhere,
                "'from' is %r but the session is in %r" % (frm, previous_state),
            )

        key = (frm, to)
        if key not in AUTHORIZED_TRANSITIONS:
            report.add(
                "UNAUTHORIZED_TRANSITION",
                twhere,
                "%s -> %s is not an authorized transition" % (frm, to),
            )
        else:
            expected_owner = AUTHORIZED_TRANSITIONS[key]
            if transition.get("owner") != expected_owner:
                report.add(
                    "TRANSITION_OWNER_MISMATCH",
                    twhere,
                    "%s -> %s is owned by %r, not %r"
                    % (frm, to, expected_owner, transition.get("owner")),
                )

        if to == "unknown":
            kind = evidence.get("kind") if isinstance(evidence, dict) else None
            if kind not in OBSERVED_EVIDENCE_KINDS:
                report.add(
                    "UNKNOWN_INFERRED_WITHOUT_EVIDENCE",
                    twhere,
                    "'unknown' requires an explicit integration observation "
                    "(evidence.kind in %s), never absence or chat text"
                    % "/".join(sorted(OBSERVED_EVIDENCE_KINDS)),
                )

        at = transition.get("at")
        if previous_at is not None:
            _check_order(report, twhere, previous_at, at, "transition time regresses")
        previous_at = at if isinstance(at, str) else previous_at
        previous_state = to

    if usable_chain and previous_state is not None and record.get("state") != previous_state:
        report.add(
            "SESSION_STATE_MISMATCH",
            where,
            "state %r does not match the final transition target %r"
            % (record.get("state"), previous_state),
        )


# ---------------------------------------------------------------------------
# Cross-record validation
# ---------------------------------------------------------------------------


def validate_store(report, records):
    by_type = dict((t, []) for t in RECORD_TYPES)
    index = {}

    for i, record in enumerate(records):
        rtype = validate_record(report, i, record)
        if rtype is None:
            continue
        by_type[rtype].append(record)

        id_field = OWN_ID_FIELD[rtype]
        rid = record.get(id_field)
        if isinstance(rid, str):
            key = (rtype, rid)
            if key in index:
                report.add("DUPLICATE_ID", record_where(i, record), "%s %s is defined twice" % (rtype, rid))
            else:
                index[key] = record

    chats = dict((c["chat_id"], c) for c in by_type["chat"] if isinstance(c.get("chat_id"), str))
    sessions = dict((s["session_id"], s) for s in by_type["agent_session"] if isinstance(s.get("session_id"), str))
    events = dict((e["event_id"], e) for e in by_type["diagnostic_event"] if isinstance(e.get("event_id"), str))
    requests = dict((r["request_id"], r) for r in by_type["launch_request"] if isinstance(r.get("request_id"), str))
    messages = dict((m["message_id"], m) for m in by_type["message"] if isinstance(m.get("message_id"), str))
    observations = dict(
        (o["observation_id"], o) for o in by_type["session_observation"]
        if isinstance(o.get("observation_id"), str)
    )
    results_by_request = {}
    for result in by_type["launch_result"]:
        rid = result.get("request_id")
        if isinstance(rid, str) and rid not in results_by_request:
            results_by_request[rid] = result

    def ref(where, kind, value, table, label):
        if value is None:
            return None
        if not isinstance(value, str):
            return None
        target = table.get(value)
        if target is None:
            report.add("DANGLING_REFERENCE", where, "%s %r has no %s record" % (kind, value, label))
        return target

    # Handles the launcher actually issued, per session. Contract 4.3: agent_handle
    # "is the opaque handle the launcher returned", and 5.4 re-attaches through it.
    # A handle is a fact about the launch boundary, so its provenance is the
    # accepted launch_result that returned it -- never the session's own assertion.
    issued_handles = {}
    # When each handle became available to the harness: the earliest accepted
    # launch_result that returned it. Contract 4.3 -- the harness cannot address
    # an agent before the launcher has told it how.
    handle_issued_at = {}
    for result in by_type["launch_result"]:
        if result.get("outcome") != "accepted":
            continue
        sid = result.get("session_id")
        handle = result.get("agent_handle")
        if isinstance(sid, str) and isinstance(handle, str) and handle:
            issued_handles.setdefault(sid, set()).add(handle)
            at = result.get("observed_at")
            if isinstance(at, str) and TS_RE.match(at):
                seen_at = handle_issued_at.get((sid, handle))
                if seen_at is None or at < seen_at:
                    handle_issued_at[(sid, handle)] = at

    # --- sessions -------------------------------------------------------
    for i, session in enumerate(by_type["agent_session"]):
        where = record_where(i, session)
        ref(where, "chat_id", session.get("chat_id"), chats, "chat")
        # Keyed on having *reached* running, not on sitting in it. Keying on the
        # current state lets a session run with no handle at all and then move to a
        # terminal state, which dodges this rule and SESSION_HANDLE_NOT_ISSUED
        # together -- the latter has nothing to check when the field is absent.
        if "running" in _transition_targets(session) and not session.get("agent_handle"):
            report.add(
                "SESSION_HANDLE_MISSING",
                where,
                "session %s reached 'running' but carries no agent_handle; a session that "
                "ran must record the handle the launcher returned, or nothing can stop it "
                "or re-attach to it"
                % session.get("session_id"),
            )
        handle = session.get("agent_handle")
        if isinstance(handle, str) and handle:
            issued = issued_handles.get(session.get("session_id"), set())
            if handle not in issued:
                report.add(
                    "SESSION_HANDLE_NOT_ISSUED",
                    where,
                    "session %s carries agent_handle %r, but no accepted launch_result for "
                    "this session returned it (issued: %s). A handle nobody issued cannot be "
                    "stopped and cannot be re-attached to"
                    % (session.get("session_id"), handle,
                       ", ".join(sorted(issued)) if issued else "none"),
                )
        _validate_preconditions(
            report, where, session,
            {
                "messages": messages,
                "events": events,
                "requests": requests,
                "results_by_request": results_by_request,
                "observations": observations,
            },
        )

    # --- messages -------------------------------------------------------
    seen_msg_seq = {}
    for i, message in enumerate(by_type["message"]):
        where = record_where(i, message)
        ref(where, "chat_id", message.get("chat_id"), chats, "chat")
        session = ref(where, "session_id", message.get("session_id"), sessions, "agent_session")
        event = ref(where, "source_event_id", message.get("source_event_id"), events, "diagnostic_event")

        author = message.get("author")
        if author == "agent":
            if message.get("session_id") is None or message.get("source_event_id") is None:
                report.add(
                    "FABRICATED_AGENT_MESSAGE",
                    where,
                    "an agent-authored message must cite the session and the preserved "
                    "diagnostic event it was derived from",
                )
            if event is not None and event.get("interpretation") != "recognized":
                report.add(
                    "MALFORMED_EVENT_RENDERED",
                    where,
                    "user-visible history may only be derived from a recognized event; "
                    "event %s is %r"
                    % (message.get("source_event_id"), event.get("interpretation")),
                )
            # Contract 7 P2a, message side. AGENT_OUTPUT_WITHOUT_AGENT keeps a
            # never-launched session from sourcing an event to the agent; without
            # this, the same text arrives as chat by being sourced to the launcher
            # instead, on a session no cardinality or turn rule can see.
            if event is not None and event.get("source") != "agent":
                report.add(
                    "NON_AGENT_EVENT_RENDERED",
                    where,
                    "an agent-authored message must be transcribed from an event the agent "
                    "produced; event %s has source %r. Launcher and harness output is "
                    "diagnostic, never chat"
                    % (message.get("source_event_id"), event.get("source")),
                )
        elif author in ("user", "system"):
            if message.get("source_event_id") is not None:
                report.add(
                    "MESSAGE_PROVENANCE_INVALID",
                    where,
                    "only agent-authored messages may cite a diagnostic event",
                )
            if message.get("session_id") is not None:
                report.add(
                    "MESSAGE_PROVENANCE_INVALID",
                    where,
                    "only agent-authored messages may cite an agent session",
                )

        if event is not None:
            if event.get("chat_id") != message.get("chat_id") or event.get("session_id") != message.get("session_id"):
                report.add(
                    "CORRELATION_MISMATCH",
                    where,
                    "cited diagnostic event belongs to a different chat or session",
                )
        if session is not None and session.get("chat_id") != message.get("chat_id"):
            report.add("CORRELATION_MISMATCH", where, "cited session belongs to a different chat")

        chat_id = message.get("chat_id")
        seq = message.get("sequence")
        if isinstance(chat_id, str) and isinstance(seq, int) and not isinstance(seq, bool):
            seen_msg_seq.setdefault(chat_id, []).append((seq, where))

    _check_sequences(report, seen_msg_seq, "chat", "message")

    # --- diagnostic events ----------------------------------------------
    seen_event_seq = {}
    for i, event in enumerate(by_type["diagnostic_event"]):
        where = record_where(i, event)
        ref(where, "chat_id", event.get("chat_id"), chats, "chat")
        session = ref(where, "session_id", event.get("session_id"), sessions, "agent_session")
        if session is not None and session.get("chat_id") != event.get("chat_id"):
            report.add("CORRELATION_MISMATCH", where, "event session belongs to a different chat")
        session_id = event.get("session_id")
        seq = event.get("sequence")
        if isinstance(session_id, str) and isinstance(seq, int) and not isinstance(seq, bool):
            seen_event_seq.setdefault(session_id, []).append((seq, where))

    _check_sequences(report, seen_event_seq, "session", "diagnostic event")

    # --- bindings -------------------------------------------------------
    open_by_chat = {}
    open_by_session = {}
    for i, binding in enumerate(by_type["agent_binding"]):
        where = record_where(i, binding)
        ref(where, "chat_id", binding.get("chat_id"), chats, "chat")
        session = ref(where, "session_id", binding.get("session_id"), sessions, "agent_session")
        if session is not None and session.get("chat_id") != binding.get("chat_id"):
            report.add("CORRELATION_MISMATCH", where, "binding session belongs to a different chat")

        released = binding.get("released_at")
        state = session.get("state") if session is not None else None
        if released is None:
            chat_id = binding.get("chat_id")
            if isinstance(chat_id, str):
                open_by_chat.setdefault(chat_id, []).append((binding.get("binding_id"), where))
            session_id = binding.get("session_id")
            if isinstance(session_id, str):
                open_by_session.setdefault(session_id, []).append((binding.get("binding_id"), where))
            if state in TERMINAL_SESSION_STATES:
                report.add(
                    "BINDING_OPEN_ON_TERMINAL_SESSION",
                    where,
                    "binding is still open but session %s is terminal (%s)"
                    % (binding.get("session_id"), state),
                )
        else:
            if session is not None and state not in TERMINAL_SESSION_STATES:
                report.add(
                    "BINDING_RELEASED_BEFORE_TERMINAL",
                    where,
                    "binding was released but session %s is in non-terminal state %r"
                    % (binding.get("session_id"), state),
                )
            _check_order(report, where, binding.get("bound_at"), released, "released_at precedes bound_at")

    for chat_id in sorted(open_by_chat):
        entries = sorted(open_by_chat[chat_id])
        if len(entries) > 1:
            report.add(
                "CONCURRENT_BINDING",
                entries[1][1],
                "chat %s has %d open agent bindings (%s); v0.1 permits at most one"
                % (chat_id, len(entries), ", ".join(str(e[0]) for e in entries)),
            )

    # --- one agent per chat (contract 1, 4.4) ----------------------------
    # The binding cardinality rule above is bookkeeping about records. The
    # defining v0.1 rule is about agents, so it is enforced directly: a session
    # that is not terminal denotes an agent that may still be alive, every such
    # session must be held by exactly one open binding, and a chat may have at
    # most one of them.
    active_by_chat = {}
    for i, session in enumerate(by_type["agent_session"]):
        where = record_where(i, session)
        session_id = session.get("session_id")
        if session.get("state") in TERMINAL_SESSION_STATES:
            continue
        chat_id = session.get("chat_id")
        if isinstance(chat_id, str) and isinstance(session_id, str):
            active_by_chat.setdefault(chat_id, []).append((session_id, where))
        held = open_by_session.get(session_id, []) if isinstance(session_id, str) else []
        if not held:
            report.add(
                "UNBOUND_ACTIVE_SESSION",
                where,
                "session %s is in non-terminal state %r but no open agent_binding holds it; "
                "an agent that may still be alive must be bound to its chat"
                % (session_id, session.get("state")),
            )
        elif len(held) > 1:
            report.add(
                "UNBOUND_ACTIVE_SESSION",
                where,
                "session %s is held by %d open bindings (%s); exactly one is permitted"
                % (session_id, len(held), ", ".join(str(h[0]) for h in sorted(held))),
            )

    for chat_id in sorted(active_by_chat):
        entries = sorted(active_by_chat[chat_id])
        if len(entries) > 1:
            report.add(
                "CONCURRENT_SESSION",
                entries[1][1],
                "chat %s has %d non-terminal agent sessions (%s); v0.1 is one agent per chat"
                % (chat_id, len(entries), ", ".join(str(e[0]) for e in entries)),
            )

    # --- launch requests and results -------------------------------------
    request_by_session = {}
    for i, request in enumerate(by_type["launch_request"]):
        where = record_where(i, request)
        ref(where, "chat_id", request.get("chat_id"), chats, "chat")
        session = ref(where, "session_id", request.get("session_id"), sessions, "agent_session")
        if session is not None and session.get("chat_id") != request.get("chat_id"):
            report.add("CORRELATION_MISMATCH", where, "launch request session belongs to a different chat")
        session_id = request.get("session_id")
        if isinstance(session_id, str):
            if session_id in request_by_session:
                report.add(
                    "DUPLICATE_LAUNCH_REQUEST",
                    where,
                    "session %s already has a launch request; one launch attempt per session"
                    % session_id,
                )
            else:
                request_by_session[session_id] = request

    seen_result_for = set()
    for i, result in enumerate(by_type["launch_result"]):
        where = record_where(i, result)
        request = ref(where, "request_id", result.get("request_id"), requests, "launch_request")
        session = ref(where, "session_id", result.get("session_id"), sessions, "agent_session")
        if request is not None and request.get("session_id") != result.get("session_id"):
            report.add("CORRELATION_MISMATCH", where, "launch result and request name different sessions")
        rid = result.get("request_id")
        if isinstance(rid, str):
            if rid in seen_result_for:
                report.add("DUPLICATE_LAUNCH_RESULT", where, "request %s already has a result" % rid)
            seen_result_for.add(rid)

        if session is not None:
            state = session.get("state")
            targets = set()
            if isinstance(session.get("transitions"), list):
                for t in session["transitions"]:
                    if isinstance(t, dict) and isinstance(t.get("to"), str):
                        targets.add(t["to"])
            if result.get("outcome") == "failed" and state != "launch_failed":
                report.add(
                    "LAUNCH_OUTCOME_MISMATCH",
                    where,
                    "launcher reported a launch failure but session %s is in state %r"
                    % (result.get("session_id"), state),
                )
            if result.get("outcome") == "accepted" and "running" not in targets:
                report.add(
                    "LAUNCH_OUTCOME_MISMATCH",
                    where,
                    "launcher accepted the launch but session %s never entered 'running'"
                    % result.get("session_id"),
                )

    # --- deliveries ------------------------------------------------------
    seen_delivery_seq = {}
    for i, delivery in enumerate(by_type["delivery_request"]):
        where = record_where(i, delivery)
        ref(where, "chat_id", delivery.get("chat_id"), chats, "chat")
        session = ref(where, "session_id", delivery.get("session_id"), sessions, "agent_session")
        if session is not None and session.get("chat_id") != delivery.get("chat_id"):
            report.add("CORRELATION_MISMATCH", where, "delivery session belongs to a different chat")
        if session is not None:
            capabilities = session.get("launcher_capabilities")
            mode = capabilities.get("continuation") if isinstance(capabilities, dict) else None
            if mode != "persistent":
                report.add(
                    "DELIVERY_NOT_SUPPORTED",
                    where,
                    "session %s declares continuation %r; delivering to an already-running "
                    "agent requires a launcher that declares 'persistent'"
                    % (delivery.get("session_id"), mode),
                )
            if "running" not in _transition_targets(session):
                report.add(
                    "DELIVERY_NOT_SUPPORTED",
                    where,
                    "session %s never entered 'running'; there was no agent to deliver to"
                    % delivery.get("session_id"),
                )
            exited_at = _terminal_at(session)
            created_at = delivery.get("created_at")
            if (exited_at is not None and isinstance(created_at, str)
                    and TS_RE.match(created_at) and created_at > exited_at):
                report.add(
                    "DELIVERY_AFTER_AGENT_EXIT",
                    where,
                    "delivery was created at %s, after session %s reached terminal state %r "
                    "at %s; 'deliver' sends text to an agent that is already running"
                    % (created_at, delivery.get("session_id"), session.get("state"), exited_at),
                )
        session_id = delivery.get("session_id")
        seq = delivery.get("sequence")
        if isinstance(session_id, str) and isinstance(seq, int) and not isinstance(seq, bool):
            seen_delivery_seq.setdefault(session_id, []).append((seq, where))

    _check_sequences(report, seen_delivery_seq, "session", "delivery")

    # --- agent output presupposes an agent -------------------------------
    # A launcher may report before anything ran; an agent may not. An event
    # sourced to the agent on a session that never reached 'running' is output
    # from an agent that was never started.
    for i, event in enumerate(by_type["diagnostic_event"]):
        if event.get("source") != "agent":
            continue
        session = sessions.get(event.get("session_id"))
        if session is None:
            continue
        if "running" not in _transition_targets(session):
            report.add(
                "AGENT_OUTPUT_WITHOUT_AGENT",
                record_where(i, event),
                "event is sourced to the agent, but session %s never entered 'running'; "
                "there was no agent to produce it"
                % event.get("session_id"),
            )

    # --- a one-shot launcher has no stream -------------------------------
    # Contract 6.1: a launcher declaring response_shape 'one_shot' returns the
    # agent's response from the call and "cannot observe a stream ending". The
    # evidence-kind guard covers the citation channel; this covers the fact, so
    # the same assertion cannot be smuggled in as an ordinary 'event'.
    for i, event in enumerate(by_type["diagnostic_event"]):
        if event.get("interpreted_type") != STREAM_END_TYPE:
            continue
        session = sessions.get(event.get("session_id"))
        if session is None:
            continue
        capabilities = session.get("launcher_capabilities")
        shape = capabilities.get("response_shape") if isinstance(capabilities, dict) else None
        if shape == "one_shot":
            report.add(
                "STREAM_END_UNSUPPORTED",
                record_where(i, event),
                "event is typed %r, but session %s declares response_shape 'one_shot', "
                "which has no stream to end"
                % (STREAM_END_TYPE, event.get("session_id")),
            )

    # --- one user turn opens at most one agent ---------------------------
    # The user sending a turn is the user action that opens the next binding.
    # Two sessions that both ran on the strength of the same turn means one of
    # them was opened by something other than a user turn.
    served_by_turn = {}
    for i, session in enumerate(by_type["agent_session"]):
        transitions = session.get("transitions")
        if not isinstance(transitions, list) or not transitions:
            continue
        first = transitions[0]
        if not isinstance(first, dict) or first.get("from") is not None:
            continue
        evidence = first.get("evidence")
        opener = evidence.get("ref") if isinstance(evidence, dict) else None
        if not isinstance(opener, str) or opener not in messages:
            continue
        if "running" not in _transition_targets(session):
            continue  # a session that never ran is a retry of the same turn
        if opener in served_by_turn:
            report.add(
                "TURN_ALREADY_SERVED",
                record_where(i, session),
                "message %s already opened session %s, which ran; a later binding is "
                "opened by a later user turn, never by the harness on its own"
                % (opener, served_by_turn[opener]),
            )
        else:
            served_by_turn[opener] = session.get("session_id")

    # --- session observations --------------------------------------------
    for i, observation in enumerate(by_type["session_observation"]):
        where = record_where(i, observation)
        ref(where, "chat_id", observation.get("chat_id"), chats, "chat")
        session = ref(where, "session_id", observation.get("session_id"), sessions, "agent_session")
        if session is not None and session.get("chat_id") != observation.get("chat_id"):
            report.add("CORRELATION_MISMATCH", where, "observation session belongs to a different chat")

    # --- addressing an agent requires a handle the harness recorded -------
    # Contract 6.1: 'stop', 'events' and 'deliver' take the agent_handle the
    # launcher returned, and nothing else on the seam names an agent. A launcher
    # is therefore required to remember nothing between calls, and the harness is
    # forbidden to depend on one that does.
    #
    # The signature is an interface property: no store holds an argument list, so
    # nothing here can show that an implementation passed the handle rather than
    # the session id. What a store can show is the fact the signature exists to
    # guarantee -- that every operation which addressed an agent was issued by a
    # harness that had the handle in hand at the time. A record of such an
    # operation on a session with no issued handle means the agent was reached by
    # some other route, and the only other route is launcher-side memory.
    addressing_records = []
    for i, observation in enumerate(by_type["session_observation"]):
        if observation.get("kind") in ADDRESSING_OBSERVATION_KINDS:
            addressing_records.append(
                (record_where(i, observation), observation, observation.get("observed_at"),
                 "a %r observation" % observation.get("kind"))
            )
    for i, delivery in enumerate(by_type["delivery_request"]):
        addressing_records.append(
            (record_where(i, delivery), delivery, delivery.get("created_at"), "a delivery")
        )

    for where, record, when, label in addressing_records:
        sid = record.get("session_id")
        session = sessions.get(sid) if isinstance(sid, str) else None
        if session is None:
            continue  # DANGLING_REFERENCE already reported
        # Stated over the handle the launcher *issued*, not over the field being
        # populated. Keyed on the field alone this rule would test the label on
        # the claim -- a store could satisfy it by writing any string -- and would
        # be standing on SESSION_HANDLE_NOT_ISSUED to supply the fact behind it.
        # A rule that goes vacuous when a neighbouring rule's keying shifts is the
        # defect this ticket family has now produced seven times, so this one
        # carries its own fact.
        handle = session.get("agent_handle")
        issued = issued_handles.get(sid, set())
        if not (isinstance(handle, str) and handle and handle in issued):
            report.add(
                "ADDRESSED_WITHOUT_HANDLE",
                where,
                "%s is recorded on session %s, for which the launcher issued no handle "
                "(carried: %r; issued: %s). 'stop', 'events' and 'deliver' address the "
                "agent through the handle the launcher returned (6.1), so the harness had "
                "nothing to pass and could only have asked the launcher to resolve the "
                "session from state of its own"
                % (label, sid, handle,
                   ", ".join(sorted(issued)) if issued else "none"),
            )
            continue
        issued_at = handle_issued_at.get((sid, handle))
        if issued_at is None:
            continue  # the issuing result has no usable timestamp; BAD_TIMESTAMP covers it
        if not (isinstance(when, str) and TS_RE.match(when)):
            continue
        # Whole-second granularity, ties accepted: a launch_result and the first
        # operation on its handle can honestly share a timestamp, and rejecting
        # the tie would fail a true store to no purpose. Comparing the fixed-width
        # second prefix also keeps an optional fractional part from sorting wrong.
        if when[:19] < issued_at[:19]:
            report.add(
                "ADDRESSED_BEFORE_HANDLE_ISSUED",
                where,
                "%s on session %s is dated %s, before the accepted launch_result issued "
                "handle %r at %s; the harness cannot have addressed an agent whose handle "
                "it did not yet have"
                % (label, sid, when, handle, issued_at),
            )

    # --- the declared instruction bound, where one was measured -----------
    for i, packet in enumerate(by_type["launch_request"] + by_type["delivery_request"]):
        where = record_where(i, packet)
        session = sessions.get(packet.get("session_id"))
        if session is None:
            continue
        capabilities = session.get("launcher_capabilities")
        if not isinstance(capabilities, dict):
            continue
        bound = capabilities.get("instruction_bound_bytes")
        if not isinstance(bound, int) or isinstance(bound, bool) or bound < 1:
            continue  # not measured; this contract asserts no bound of its own
        text = packet.get("instruction_text")
        if not isinstance(text, str):
            continue
        size = len(text.encode("utf-8"))
        if size > bound:
            report.add(
                "INSTRUCTION_TEXT_TOO_LARGE",
                where,
                "instruction_text is %d bytes; session %s declares a measured bound of %d"
                % (size, packet.get("session_id"), bound),
            )

    # --- every turn the agent answered has a durable instruction record ---
    # Contract 6.2/6.4: the exact text sent to the agent is preserved for every
    # turn, not only the first. Without this, a chat served by one persistent
    # agent records what was sent at launch and nothing afterwards.
    #
    # Only a packet on a session that actually reached 'running' can have reached an
    # agent. A launch_request on a session that never ran is the packet that was not
    # sent, which is exactly what a launch failure is; counting it lets a chat pad
    # the floor with decoy sessions and leave every later turn integration-blind.
    #
    # What the floor counts on the other side is the number of times the agent
    # *answered*, not the number of messages the user typed. A user message is not
    # itself evidence that anything was sent to an agent: the user may send twice
    # before an answer arrives, a launch may fail so the turn reached nobody, and a
    # turn may reach no agent at all. Counting user messages treated all three as
    # missing packets and rejected accurate stores.
    #
    # An occasion is a maximal run of consecutive agent messages in the chat's own
    # ordered history. Each run is one occasion on which the agent demonstrably
    # produced output, and every such occasion required a packet to have been sent.
    # A run is counted once however many messages it contains, because one
    # instruction can produce several recognized events and therefore several
    # transcribed messages. 'system' messages are neither user nor agent and are
    # ignored here exactly as before (carried finding R6). A run that precedes every
    # user message is still an occasion: no honest chat has the agent speaking
    # first, so writing an answer at the front of the history must not lower the
    # floor.
    #
    # This is still a count and not a per-turn pairing: it reads the recorded order
    # of the chat's own messages and computes no duration, compares no message to a
    # packet, and consults no clock.
    instructions_by_chat = {}
    for packet in by_type["launch_request"] + by_type["delivery_request"]:
        chat_id = packet.get("chat_id")
        session = sessions.get(packet.get("session_id"))
        if session is None or "running" not in _transition_targets(session):
            continue
        if isinstance(chat_id, str):
            instructions_by_chat[chat_id] = instructions_by_chat.get(chat_id, 0) + 1

    turns_by_chat = {}
    for message in by_type["message"]:
        chat_id = message.get("chat_id")
        seq = message.get("sequence")
        author = message.get("author")
        if not isinstance(chat_id, str) or not isinstance(seq, int) or isinstance(seq, bool):
            continue
        if author not in ("user", "agent"):
            continue
        turns_by_chat.setdefault(chat_id, []).append((seq, author))

    for chat_id in sorted(turns_by_chat):
        ordered = sorted(turns_by_chat[chat_id], key=lambda item: item[0])
        answered = 0
        previous = None
        for _, author in ordered:
            if author == "agent" and previous != "agent":
                answered += 1
            previous = author
        if answered == 0:
            continue
        recorded = instructions_by_chat.get(chat_id, 0)
        if recorded < answered:
            report.add(
                "TURN_INSTRUCTION_MISSING",
                "chat %s" % chat_id,
                "chat %s shows %d occasion(s) on which the agent produced an answer "
                "but preserves only %d instruction packet(s); every turn sent to an "
                "agent must be durably recorded"
                % (chat_id, answered, recorded),
            )


def _terminal_at(session):
    """When the session entered its current terminal state, else None.

    This is an ordering of two recorded timestamps, of the same kind the record
    shapes already require (TIME_REGRESSION). It is not an elapsed-time rule and
    infers nothing from silence: no duration is computed and no threshold exists.
    """
    if session.get("state") not in TERMINAL_SESSION_STATES:
        return None
    transitions = session.get("transitions")
    if not isinstance(transitions, list) or not transitions:
        return None
    last = transitions[-1]
    if not isinstance(last, dict) or last.get("to") != session.get("state"):
        return None
    at = last.get("at")
    if isinstance(at, str) and TS_RE.match(at):
        return at
    return None


def _transition_targets(session):
    targets = set()
    if isinstance(session.get("transitions"), list):
        for transition in session["transitions"]:
            if isinstance(transition, dict) and isinstance(transition.get("to"), str):
                targets.add(transition["to"])
    return targets


def _validate_preconditions(report, where, session, tables):
    """Contract 5.2 precondition column and 5.3 evidence rules, executably.

    Each transition names an evidence kind that must be admissible for that
    transition, and a reference that must resolve to a real record in the store
    establishing that the precondition actually held. A claim of an observation
    is not an observation.
    """
    transitions = session.get("transitions")
    if not isinstance(transitions, list):
        return

    session_id = session.get("session_id")
    chat_id = session.get("chat_id")
    capabilities = session.get("launcher_capabilities")
    response_shape = capabilities.get("response_shape") if isinstance(capabilities, dict) else None

    for i, transition in enumerate(transitions):
        if not isinstance(transition, dict):
            continue
        twhere = "%s transitions[%d]" % (where, i)
        frm = transition.get("from")
        to = transition.get("to")
        key = (frm, to)
        admissible = TRANSITION_PRECONDITIONS.get(key)
        if admissible is None:
            continue  # unauthorized pairs are already reported as such

        evidence = transition.get("evidence")
        if not isinstance(evidence, dict):
            continue  # shape failure already reported
        kind = evidence.get("kind")
        ref_value = evidence.get("ref")

        if kind == "stream_end" and response_shape == "one_shot":
            report.add(
                "EVIDENCE_KIND_UNSUPPORTED",
                twhere,
                "session %s declares response_shape 'one_shot', which returns a response "
                "rather than a stream; 'stream_end' is not an observation it can make"
                % session_id,
            )

        if kind not in admissible:
            report.add(
                "PRECONDITION_NOT_MET",
                twhere,
                "%s -> %s admits evidence.kind in %s, not %r"
                % (frm, to, "/".join(sorted(admissible)), kind),
            )
            if to == "unknown":
                report.add(
                    "UNKNOWN_INFERRED_WITHOUT_EVIDENCE",
                    twhere,
                    "'unknown' was concluded without an admissible integration observation",
                )
            continue

        failure = _check_evidence_requirement(
            admissible[kind], ref_value, session_id, chat_id, tables
        )
        if failure is None:
            continue

        code, detail = failure
        report.add(code, twhere, "%s -> %s: %s" % (frm, to, detail))
        if to == "unknown":
            report.add(
                "UNKNOWN_INFERRED_WITHOUT_EVIDENCE",
                twhere,
                "'unknown' cites evidence %r that does not resolve to a record in this "
                "store, so it rests on the claim of an observation rather than one"
                % ref_value,
            )


def _check_evidence_requirement(requirement, ref_value, session_id, chat_id, tables):
    """Return None when satisfied, else (violation code, detail)."""
    if requirement == "no_ref":
        if ref_value is not None:
            return ("EVIDENCE_REF_INVALID", "no record applies, so evidence.ref must be null")
        return None

    if ref_value is None:
        return ("EVIDENCE_REF_INVALID", "evidence.ref is required and must name a record")
    if not isinstance(ref_value, str) or not ID_RE.match(ref_value):
        return (
            "EVIDENCE_REF_INVALID",
            "evidence.ref %r is not a well-formed identifier" % (ref_value,),
        )

    if requirement == "user_message":
        message = tables["messages"].get(ref_value)
        if not ref_value.startswith("msg_") or message is None:
            return ("EVIDENCE_REF_INVALID", "evidence.ref %r names no message record" % ref_value)
        if message.get("author") != "user":
            return (
                "PRECONDITION_NOT_MET",
                "a session is opened by a user turn; message %s is authored by %r"
                % (ref_value, message.get("author")),
            )
        if message.get("chat_id") != chat_id:
            return (
                "CORRELATION_MISMATCH",
                "message %s belongs to a different chat than the session it opens" % ref_value,
            )
        return None

    if requirement.startswith("result_"):
        request = tables["requests"].get(ref_value)
        if not ref_value.startswith("req_") or request is None:
            return (
                "EVIDENCE_REF_INVALID",
                "evidence.ref %r names no launch_request record" % ref_value,
            )
        if request.get("session_id") != session_id:
            return (
                "CORRELATION_MISMATCH",
                "launch_request %s belongs to a different session" % ref_value,
            )
        result = tables["results_by_request"].get(ref_value)
        if result is None:
            return (
                "PRECONDITION_NOT_MET",
                "the launcher's report is what authorizes this transition, and request %s "
                "has no launch_result" % ref_value,
            )
        expected = {
            "result_accepted": "accepted",
            "result_failed": "failed",
            "result_unknown": "unknown",
        }[requirement]
        if result.get("outcome") != expected:
            return (
                "PRECONDITION_NOT_MET",
                "this transition requires launch outcome %r; request %s reports %r"
                % (expected, ref_value, result.get("outcome")),
            )
        if expected == "accepted" and not result.get("agent_handle"):
            return (
                "PRECONDITION_NOT_MET",
                "an accepted launch must return a handle before a session may run",
            )
        if expected == "failed" and not result.get("failure_category"):
            return (
                "PRECONDITION_NOT_MET",
                "a failed launch must name a failure category #90 can count",
            )
        return None

    if requirement == "launch_request":
        request = tables["requests"].get(ref_value)
        if not ref_value.startswith("req_") or request is None:
            return (
                "EVIDENCE_REF_INVALID",
                "evidence.ref %r names no launch_request record; a session may not be "
                "launched without the bounded packet that was sent" % ref_value,
            )
        if request.get("session_id") != session_id:
            return (
                "CORRELATION_MISMATCH",
                "launch_request %s belongs to a different session" % ref_value,
            )
        return None

    if requirement.startswith("event_"):
        event = tables["events"].get(ref_value)
        if not ref_value.startswith("evt_") or event is None:
            return (
                "EVIDENCE_REF_INVALID",
                "evidence.ref %r names no diagnostic_event record" % ref_value,
            )
        if event.get("session_id") != session_id:
            return (
                "CORRELATION_MISMATCH",
                "diagnostic_event %s belongs to a different session" % ref_value,
            )
        if event.get("source") not in ("agent", "launcher"):
            return (
                "PRECONDITION_NOT_MET",
                "a transition authorized by an observation of the integration must cite an "
                "event the integration produced; %s has source %r. What the harness itself "
                "observed is a session_observation, not an event it received"
                % (ref_value, event.get("source")),
            )
        if requirement == "event_recognized" and event.get("interpretation") != "recognized":
            return (
                "PRECONDITION_NOT_MET",
                "this transition requires a recognized event; %s is %r"
                % (ref_value, event.get("interpretation")),
            )
        if requirement == "event_stream_end":
            if event.get("source") != "launcher" or event.get("interpreted_type") != STREAM_END_TYPE:
                return (
                    "PRECONDITION_NOT_MET",
                    "end-of-stream must be signalled by the launcher and preserved as a "
                    "launcher-sourced %r event; %s is source %r, type %r. A reader-side "
                    "read failure is a session_observation, not an end of stream"
                    % (STREAM_END_TYPE, ref_value, event.get("source"),
                       event.get("interpreted_type")),
                )
            if event.get("interpretation") != "recognized":
                return (
                    "PRECONDITION_NOT_MET",
                    "the end-of-stream signal must be recognized; %s is %r"
                    % (ref_value, event.get("interpretation")),
                )
        return None

    if requirement.startswith("observation_"):
        observation = tables["observations"].get(ref_value)
        if not ref_value.startswith("obs_") or observation is None:
            return (
                "EVIDENCE_REF_INVALID",
                "evidence.ref %r names no session_observation record" % ref_value,
            )
        if observation.get("session_id") != session_id:
            return (
                "CORRELATION_MISMATCH",
                "session_observation %s belongs to a different session" % ref_value,
            )
        kind = observation.get("kind")
        if requirement == "observation_stop_confirmed" and kind != "stop_confirmed":
            return (
                "PRECONDITION_NOT_MET",
                "terminating requires a confirmed stop; observation %s is %r"
                % (ref_value, kind),
            )
        if requirement == "observation_reattached" and kind != "reattached":
            return (
                "PRECONDITION_NOT_MET",
                "resuming requires a successful re-attachment; observation %s is %r"
                % (ref_value, kind),
            )
        if requirement == "observation_unresolved" and kind not in UNRESOLVED_OBSERVATION_KINDS:
            return (
                "PRECONDITION_NOT_MET",
                "'unknown' requires an observation that left liveness undeterminable "
                "(one of %s); observation %s is %r"
                % ("/".join(sorted(UNRESOLVED_OBSERVATION_KINDS)), ref_value, kind),
            )
        return None

    raise AssertionError("unknown evidence requirement %r" % requirement)  # pragma: no cover


def _check_sequences(report, grouped, owner_label, item_label):
    for owner_id in sorted(grouped):
        entries = sorted(grouped[owner_id])
        seen = set()
        for seq, where in entries:
            if seq in seen:
                report.add(
                    "DUPLICATE_SEQUENCE",
                    where,
                    "%s sequence %d occurs twice in %s %s" % (item_label, seq, owner_label, owner_id),
                )
            seen.add(seq)
        expected = 1
        for seq in sorted(seen):
            if seq != expected:
                report.add(
                    "SEQUENCE_GAP",
                    "%s %s" % (owner_label, owner_id),
                    "%s sequence jumps from %d to %d; ordered history must be contiguous from 1"
                    % (item_label, expected - 1, seq),
                )
                break
            expected += 1


# ---------------------------------------------------------------------------
# Fixture driving
# ---------------------------------------------------------------------------


def validate_fixture(path, document):
    report = Report()

    if not isinstance(document, dict):
        report.add("BAD_FIXTURE", path, "fixture must be a JSON object")
        return report

    version = document.get("contract_version")
    if version != CONTRACT_VERSION:
        report.add(
            "UNSUPPORTED_CONTRACT_VERSION",
            path,
            "contract_version %r is not supported (supported: %r)" % (version, CONTRACT_VERSION),
        )
        return report

    records = document.get("records")
    if not isinstance(records, list):
        report.add("BAD_FIXTURE", path, "'records' must be an array")
        return report

    validate_store(report, records)
    return report


def load_fixture(path):
    with open(path, "r") as handle:
        return json.load(handle)


def collect_fixture_paths(targets):
    paths = []
    for target in targets:
        if os.path.isdir(target):
            for root, dirs, files in os.walk(target):
                dirs.sort()
                for name in sorted(files):
                    if name.endswith(".json"):
                        paths.append(os.path.join(root, name))
        else:
            paths.append(target)
    return sorted(paths)


def main(argv):
    targets = argv[1:]
    if not targets:
        sys.stderr.write(__doc__)
        return 2

    paths = collect_fixture_paths(targets)
    if not paths:
        sys.stderr.write("error: no .json fixtures found under %s\n" % ", ".join(targets))
        return 2

    failures = 0
    accepted = 0
    rejected = 0

    for path in paths:
        try:
            document = load_fixture(path)
        except (IOError, OSError) as exc:
            sys.stderr.write("error: cannot read %s: %s\n" % (path, exc))
            return 2
        except ValueError as exc:
            sys.stderr.write("error: %s is not valid JSON: %s\n" % (path, exc))
            return 2

        meta = document.get("fixture") if isinstance(document, dict) else None
        if not isinstance(meta, dict) or meta.get("expect") not in ("accept", "reject"):
            sys.stderr.write(
                "error: %s has no fixture.expect of 'accept' or 'reject'\n" % path
            )
            return 2

        expect = meta["expect"]
        reason_code = meta.get("reason_code")
        if expect == "reject" and not reason_code:
            sys.stderr.write("error: %s expects rejection but names no reason_code\n" % path)
            return 2

        report = validate_fixture(path, document)
        name = meta.get("name") or os.path.basename(path)

        if expect == "accept":
            if report.ok():
                accepted += 1
                print("PASS  accept  %s" % name)
            else:
                failures += 1
                print("FAIL  accept  %s -- expected acceptance, got %d violation(s)"
                      % (name, len(report.violations)))
                for code, where, detail in report.sorted():
                    print("        %s at %s: %s" % (code, where, detail))
        else:
            if report.ok():
                failures += 1
                print("FAIL  reject  %s -- expected rejection for %s, but the store validated"
                      % (name, reason_code))
            elif reason_code not in report.codes:
                failures += 1
                print("FAIL  reject  %s -- rejected, but not for %s (got: %s)"
                      % (name, reason_code, ", ".join(sorted(report.codes))))
                for code, where, detail in report.sorted():
                    print("        %s at %s: %s" % (code, where, detail))
            else:
                rejected += 1
                matching = [v for v in report.sorted() if v[0] == reason_code]
                print("PASS  reject  %s -- %s" % (name, reason_code))
                for code, where, detail in matching:
                    print("        %s at %s: %s" % (code, where, detail))

    print("")
    print("%d fixture(s): %d accepted as valid, %d rejected for the stated reason, %d mismatched"
          % (len(paths), accepted, rejected, failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

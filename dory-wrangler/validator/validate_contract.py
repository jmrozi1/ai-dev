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
}

ID_RE = re.compile(r"^(cht|msg|ses|bnd|evt|req)_[0-9a-z]{8,32}$")
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

EVIDENCE_KINDS = ("event", "stream_end", "launch_result", "user_action", "harness_action")

# Evidence kinds that count as an explicit observation of the integration.
OBSERVED_EVIDENCE_KINDS = frozenset(("event", "stream_end", "launch_result"))

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

MAX_INSTRUCTION_BYTES = 65536
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
    "diagnostic_event": "event_id",
}


def record_where(index, record):
    if isinstance(record, dict):
        rtype = record.get("record_type")
        keys = []
        own = OWN_ID_FIELD.get(rtype)
        if own:
            keys.append(own)
        keys.extend(["chat_id", "message_id", "session_id", "binding_id", "request_id", "event_id"])
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
        _validate_transitions(report, where, record)

    elif rtype == "launch_request":
        text = record.get("instruction_text")
        if isinstance(text, str):
            try:
                size = len(text.encode("utf-8"))
            except UnicodeEncodeError:
                report.add("INSTRUCTION_TEXT_NOT_UTF8", where, "instruction_text is not encodable as UTF-8")
                size = 0
            if size > MAX_INSTRUCTION_BYTES:
                report.add(
                    "INSTRUCTION_TEXT_TOO_LARGE",
                    where,
                    "instruction_text is %d bytes; the bounded packet limit is %d"
                    % (size, MAX_INSTRUCTION_BYTES),
                )

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

        id_field = {
            "chat": "chat_id",
            "message": "message_id",
            "agent_session": "session_id",
            "agent_binding": "binding_id",
            "launch_request": "request_id",
            "launch_result": "request_id",
            "diagnostic_event": "event_id",
        }[rtype]
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

    def ref(where, kind, value, table, label):
        if value is None:
            return None
        if not isinstance(value, str):
            return None
        target = table.get(value)
        if target is None:
            report.add("DANGLING_REFERENCE", where, "%s %r has no %s record" % (kind, value, label))
        return target

    # --- sessions -------------------------------------------------------
    for i, session in enumerate(by_type["agent_session"]):
        where = record_where(i, session)
        ref(where, "chat_id", session.get("chat_id"), chats, "chat")
        if session.get("state") == "running" and not session.get("agent_handle"):
            report.add(
                "SESSION_HANDLE_MISSING",
                where,
                "a running session must carry the agent_handle the launcher returned",
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

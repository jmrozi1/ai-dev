"""The durable chat store: contract section 8, on the filesystem.

This is the only writer of durable records, and every guarantee the shell makes
about history is a guarantee about this module.

## Layout

    <root>/chats/<chat_id>/chat.json
    <root>/chats/<chat_id>/.lock
    <root>/chats/<chat_id>/messages/00000001.json
    <root>/chats/<chat_id>/sessions/<session_id>.json
    <root>/chats/<chat_id>/packets/<request|delivery|observation|result id>.json
    <root>/diagnostics/<chat_id>/<session_id>/00000001.json

Two layout choices are load-bearing rather than cosmetic.

**Diagnostics live outside the chat tree.** Contract P4 requires diagnostic
events to be stored separately from chat history and to be reachable only
through a bounded out-of-band retrieval. Here that is structural: the transcript
reader walks `chats/<id>/messages` and has no path that reaches `diagnostics/`
at all, so "diagnostics stay out of the chat" is not a rule the renderer has to
remember.

**A session and its binding share one file.** They remain two records (contract
4.4 keeps the binding separate so that "which agent is on this chat" has one
answer in one place), but they are written together. The contract requires a
binding to be released *once* its session reaches a terminal state
(`BINDING_OPEN_ON_TERMINAL_SESSION`) and requires a non-terminal session to be
held by an open binding (`UNBOUND_ACTIVE_SESSION`). Both are rules about two
records at the same instant, so storing them in two files would make every
lifecycle change a two-write operation with an on-disk state in between that the
contract rejects. One file makes every such change a single atomic replace.

## What a reader may assume

Nothing, until it has checked. Reads are fail-closed (contract D3): every record
is run through the executable contract before it is returned, a message sequence
that is not contiguous from 1 raises rather than being renumbered, and a file
whose name disagrees with the identity inside it raises rather than being
trusted. No read path repairs anything.
"""

import json
import os
import re

from . import atomic
from . import contract
from . import ids
from .errors import (
    ConcurrencyRefused,
    NotFound,
    ProvenanceRefused,
    StoreCorrupt,
    TransitionRefused,
    UnsupportedContentType,
    ValidationRefused,
)

RECORD_VERSION = contract.RECORD_VERSION

SEQ_NAME_RE = re.compile(r"^(\d{8})\.json$")

MAX_TITLE_CHARS = 200

# Contract P4: the out-of-band diagnostic retrieval returns "a bounded number of
# events". The bound is the store's, not the caller's, so a caller cannot ask
# for everything and turn a retrieval into a feed.
DIAGNOSTIC_PAGE_DEFAULT = 100
DIAGNOSTIC_PAGE_MAX = 1000

# Observation kinds that can only be produced by an operation which addressed an
# already-launched agent. Contract 6.1 gives `stop`, `events` and `deliver` the
# `agent_handle` as their only address and requires a launcher to remember
# nothing between calls, so the harness cannot have issued any of these without
# holding a handle.
#
# `reattach_failed` is deliberately absent, for the same reason the contract
# exempts it: a session interrupted in `launching` never received a handle, so
# failing to re-attach is the true record of exactly that.
ADDRESSING_OBSERVATION_KINDS = frozenset(
    ("stop_confirmed", "stop_unconfirmed", "reattached", "stream_read_failed")
)

_MAX_SEQUENCE_RETRIES = 64


def _dumps(record):
    return (json.dumps(record, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _loads(path):
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except (IOError, OSError) as exc:
        raise StoreCorrupt("cannot read %s: %s" % (path, exc))
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise StoreCorrupt("%s is not a readable record: %s" % (path, exc))


def _seq_name(sequence):
    return "%08d.json" % sequence


def _json_names(directory):
    """Record file names in a directory, temp files excluded by construction."""
    if not os.path.isdir(directory):
        return []
    names = []
    for name in os.listdir(directory):
        if name.startswith(atomic.TEMP_PREFIX):
            continue
        if name.endswith(".json"):
            names.append(name)
    return sorted(names)


class ChatStore(object):
    """Durable storage for contract v0.1 records."""

    def __init__(self, root, sweep=True):
        self.root = os.path.abspath(root)
        self.chats_dir = os.path.join(self.root, "chats")
        self.diagnostics_dir = os.path.join(self.root, "diagnostics")
        os.makedirs(self.chats_dir, exist_ok=True)
        os.makedirs(self.diagnostics_dir, exist_ok=True)
        # Contract 4.2: richer content fails closed and is *counted* rather than
        # silently downgraded. This is that count.
        self.rejected_content_types = {}
        if sweep:
            atomic.sweep_temp_files(self.root)

    # -- paths ---------------------------------------------------------

    def _chat_dir(self, chat_id):
        if not ids.is_id(chat_id, "cht"):
            raise NotFound("%r is not a chat identifier" % (chat_id,))
        return os.path.join(self.chats_dir, chat_id)

    def _require_chat_dir(self, chat_id):
        path = self._chat_dir(chat_id)
        if not os.path.isdir(path):
            raise NotFound("no chat %s" % chat_id)
        return path

    def _lock(self, chat_id):
        return atomic.ChatLock(os.path.join(self._require_chat_dir(chat_id), ".lock"))

    # -- record validation ---------------------------------------------

    @staticmethod
    def _check_record(record, origin):
        violations = contract.record_violations(record)
        if violations:
            detail = "; ".join("%s: %s" % (code, text) for code, _, text in violations)
            raise ValidationRefused("%s violates the contract: %s" % (origin, detail))
        return record

    @staticmethod
    def _check_read(record, path):
        violations = contract.record_violations(record)
        if violations:
            detail = "; ".join("%s: %s" % (code, text) for code, _, text in violations)
            raise StoreCorrupt("%s is not a contract-valid record: %s" % (path, detail))
        return record

    # -- chats ---------------------------------------------------------

    def create_chat(self, title, chat_id=None):
        """Create a chat. The chat exists complete or it does not exist."""
        if not isinstance(title, str) or not title.strip():
            raise ValidationRefused("a chat title must be a non-empty string")
        title = title.strip()
        if len(title) > MAX_TITLE_CHARS:
            raise ValidationRefused(
                "a chat title is at most %d characters" % MAX_TITLE_CHARS
            )
        chat_id = chat_id or ids.new_id("cht")
        stamp = ids.now()
        record = self._check_record(
            {
                "record_type": "chat",
                "record_version": RECORD_VERSION,
                "chat_id": chat_id,
                "title": title,
                "created_at": stamp,
                "updated_at": stamp,
                "state": "open",
            },
            "new chat",
        )

        def build(staging):
            os.mkdir(os.path.join(staging, "messages"), 0o700)
            os.mkdir(os.path.join(staging, "sessions"), 0o700)
            os.mkdir(os.path.join(staging, "packets"), 0o700)
            with open(os.path.join(staging, "chat.json"), "wb") as handle:
                handle.write(_dumps(record))

        try:
            atomic.create_tree_exclusive(self.chats_dir, chat_id, build)
        except FileExistsError:
            raise ConcurrencyRefused("chat %s already exists" % chat_id)
        return record

    def read_chat(self, chat_id):
        path = os.path.join(self._require_chat_dir(chat_id), "chat.json")
        if not os.path.isfile(path):
            raise NotFound("no chat %s" % chat_id)
        record = self._check_read(_loads(path), path)
        if record.get("record_type") != "chat":
            raise StoreCorrupt("%s does not hold a chat record" % path)
        if record.get("chat_id") != chat_id:
            raise StoreCorrupt(
                "%s holds chat %r but is stored under %r"
                % (path, record.get("chat_id"), chat_id)
            )
        return record

    def list_chats(self, include_archived=False):
        """Every chat, most recently updated first.

        Ordering is by the record's own `updated_at`, with the opaque chat_id as
        a tiebreak so the order is total and stable. Nothing is parsed out of an
        identifier to produce it.
        """
        chats = []
        for name in sorted(os.listdir(self.chats_dir)):
            if name.startswith(atomic.TEMP_PREFIX):
                continue
            if not os.path.isdir(os.path.join(self.chats_dir, name)):
                continue
            try:
                chat = self.read_chat(name)
            except NotFound:
                # A chat directory is published by one atomic rename, so a
                # directory with no readable chat record inside it cannot be an
                # interrupted create. It is damage, and the list fails closed
                # rather than quietly showing the chats it can still read.
                raise StoreCorrupt(
                    "%s is a chat directory with no readable chat record"
                    % os.path.join(self.chats_dir, name)
                )
            if chat["state"] == "archived" and not include_archived:
                continue
            chats.append(chat)
        chats.sort(key=lambda c: (c["updated_at"], c["chat_id"]), reverse=True)
        return chats

    def archive_chat(self, chat_id):
        with self._lock(chat_id):
            chat = self.read_chat(chat_id)
            chat["state"] = "archived"
            chat["updated_at"] = ids.now()
            self._check_record(chat, "archived chat")
            atomic.replace(
                os.path.join(self._chat_dir(chat_id), "chat.json"), _dumps(chat)
            )
        return chat

    def set_title(self, chat_id, title, only_if_titled=None):
        """Rename a chat, optionally only while it still carries a given title.

        `only_if_titled` is checked under the chat lock against what is on disk,
        so a rename cannot overwrite a title someone else set in between.
        """
        if not isinstance(title, str) or not title.strip():
            raise ValidationRefused("a chat title must be a non-empty string")
        title = title.strip()[:MAX_TITLE_CHARS]
        with self._lock(chat_id):
            chat = self.read_chat(chat_id)
            if only_if_titled is not None and chat["title"] != only_if_titled:
                return chat
            chat["title"] = title
            chat["updated_at"] = ids.now()
            self._check_record(chat, "renamed chat")
            atomic.replace(
                os.path.join(self._chat_dir(chat_id), "chat.json"), _dumps(chat)
            )
        return chat

    def _touch_chat(self, chat_id, stamp):
        with self._lock(chat_id):
            chat = self.read_chat(chat_id)
            if chat["updated_at"] < stamp:
                chat["updated_at"] = stamp
                self._check_record(chat, "chat")
                atomic.replace(
                    os.path.join(self._chat_dir(chat_id), "chat.json"), _dumps(chat)
                )

    # -- messages ------------------------------------------------------

    def _messages_dir(self, chat_id):
        return os.path.join(self._require_chat_dir(chat_id), "messages")

    def read_messages(self, chat_id):
        """The chat's ordered user-visible history, from durable records alone.

        Fails closed on a duplicate or a gap. Contract 4.2: a gap means a turn
        was lost, and it is never closed silently -- so this does not renumber,
        skip, or return what it has.
        """
        directory = self._messages_dir(chat_id)
        messages = []
        for name in _json_names(directory):
            match = SEQ_NAME_RE.match(name)
            if not match:
                raise StoreCorrupt("%s is not a message file name" % os.path.join(directory, name))
            path = os.path.join(directory, name)
            record = self._check_read(_loads(path), path)
            if record.get("record_type") != "message":
                raise StoreCorrupt("%s does not hold a message record" % path)
            if record.get("chat_id") != chat_id:
                raise StoreCorrupt(
                    "%s holds a message for chat %r but is stored under %r"
                    % (path, record.get("chat_id"), chat_id)
                )
            # The ordering is taken from the record's own `sequence`. The file
            # name is checked against it rather than trusted in place of it, so
            # a store whose names look contiguous while its records are not is a
            # detected fault instead of a plausible-looking history.
            if int(match.group(1)) != record["sequence"]:
                raise StoreCorrupt(
                    "%s holds sequence %d; the file name says %d"
                    % (path, record["sequence"], int(match.group(1)))
                )
            messages.append(record)

        messages.sort(key=lambda m: m["sequence"])
        expected = 1
        for message in messages:
            if message["sequence"] == expected - 1:
                raise StoreCorrupt(
                    "chat %s has two messages at sequence %d" % (chat_id, message["sequence"])
                )
            if message["sequence"] != expected:
                raise StoreCorrupt(
                    "chat %s message sequence jumps from %d to %d; a lost turn is a "
                    "detected fault, never a closed gap"
                    % (chat_id, expected - 1, message["sequence"])
                )
            expected += 1
        return messages

    def _next_sequence(self, directory):
        highest = 0
        for name in _json_names(directory):
            match = SEQ_NAME_RE.match(name)
            if match:
                highest = max(highest, int(match.group(1)))
        return highest + 1

    def _append_message(self, chat_id, author, text, session_id, source_event_id,
                        content_type="text/plain"):
        if content_type != "text/plain":
            self.rejected_content_types[content_type] = (
                self.rejected_content_types.get(content_type, 0) + 1
            )
            raise UnsupportedContentType(
                "content_type %r is not carried by v0.1; text/plain only. "
                "Rejected and counted, never downgraded." % (content_type,)
            )
        if not isinstance(text, str) or text == "":
            raise ValidationRefused("message text must be a non-empty string")

        self.read_chat(chat_id)  # fail closed if the chat is not readable
        directory = self._messages_dir(chat_id)

        last_error = None
        for _ in range(_MAX_SEQUENCE_RETRIES):
            sequence = self._next_sequence(directory)
            stamp = ids.now()
            record = self._check_record(
                {
                    "record_type": "message",
                    "record_version": RECORD_VERSION,
                    "message_id": ids.new_id("msg"),
                    "chat_id": chat_id,
                    "sequence": sequence,
                    "author": author,
                    "created_at": stamp,
                    "content": {"content_type": "text/plain", "text": text},
                    "session_id": session_id,
                    "source_event_id": source_event_id,
                },
                "new message",
            )
            try:
                atomic.create_exclusive(
                    os.path.join(directory, _seq_name(sequence)), _dumps(record)
                )
            except FileExistsError as exc:
                # Another writer took this sequence between our read and our
                # write. `os.link` is the arbiter, so exactly one of us has it.
                last_error = exc
                continue
            atomic.fault("post_message_pre_chat_update")
            self._touch_chat(chat_id, stamp)
            return record
        raise ConcurrencyRefused(
            "could not claim a message sequence in chat %s after %d attempts (%s)"
            % (chat_id, _MAX_SEQUENCE_RETRIES, last_error)
        )

    def append_user_message(self, chat_id, text):
        """A user turn. Carries null provenance: it is not derived from the integration."""
        return self._append_message(chat_id, "user", text, None, None)

    def append_system_message(self, chat_id, text):
        """A harness-authored notice.

        Contract 4.2 permits `author: "system"` with null provenance. It is not
        agent speech and the shell renders it distinctly; see the residual noted
        in the handoff, because the contract places no provenance requirement on
        system text at all.
        """
        return self._append_message(chat_id, "system", text, None, None)

    def append_agent_message(self, chat_id, session_id, source_event_id, text):
        """A user-visible agent turn, transcribed from preserved evidence.

        Contract D2 and 4.2. Every condition is checked against the records that
        are actually in the store, not against what the caller asserts: the
        cited event must exist, be on this chat and this session, be
        `recognized`, and be `agent`-sourced. A message that cannot point at
        such an event is refused here and so never reaches the disk.
        """
        session = self.read_session(chat_id, session_id)
        event = self.read_diagnostic_event(chat_id, session_id, event_id=source_event_id)
        if event is None:
            raise ProvenanceRefused(
                "no preserved diagnostic event %s on session %s; an agent message "
                "must cite evidence that is in the store"
                % (source_event_id, session_id)
            )
        if event["chat_id"] != chat_id or event["session_id"] != session_id:
            raise ProvenanceRefused(
                "event %s belongs to chat %s session %s, not chat %s session %s"
                % (source_event_id, event["chat_id"], event["session_id"], chat_id, session_id)
            )
        if event["interpretation"] != "recognized":
            raise ProvenanceRefused(
                "event %s is %r; user-visible history is derived only from a "
                "recognized event" % (source_event_id, event["interpretation"])
            )
        if event["source"] != "agent":
            raise ProvenanceRefused(
                "event %s has source %r; launcher and harness output is diagnostic, "
                "never chat" % (source_event_id, event["source"])
            )
        if session["chat_id"] != chat_id:
            raise ProvenanceRefused(
                "session %s belongs to a different chat" % session_id
            )
        return self._append_message(chat_id, "agent", text, session_id, source_event_id)

    # -- sessions and bindings ------------------------------------------

    def _sessions_dir(self, chat_id):
        return os.path.join(self._require_chat_dir(chat_id), "sessions")

    def _session_path(self, chat_id, session_id):
        if not ids.is_id(session_id, "ses"):
            raise NotFound("%r is not a session identifier" % (session_id,))
        return os.path.join(self._sessions_dir(chat_id), session_id + ".json")

    def _read_session_file(self, path):
        document = _loads(path)
        if not isinstance(document, dict) or "session" not in document or "binding" not in document:
            raise StoreCorrupt("%s is not a session file" % path)
        session = self._check_read(document["session"], path)
        binding = self._check_read(document["binding"], path)
        if session.get("record_type") != "agent_session" or binding.get("record_type") != "agent_binding":
            raise StoreCorrupt("%s does not hold a session and its binding" % path)
        expected = os.path.basename(path)[: -len(".json")]
        if session.get("session_id") != expected:
            raise StoreCorrupt(
                "%s holds session %r but is stored under %r"
                % (path, session.get("session_id"), expected)
            )
        if binding.get("session_id") != session.get("session_id"):
            raise StoreCorrupt("%s pairs a binding with a different session" % path)
        return session, binding

    def _write_session_file(self, chat_id, session, binding, create=False):
        self._check_record(session, "agent session")
        self._check_record(binding, "agent binding")
        path = self._session_path(chat_id, session["session_id"])
        data = _dumps({"session": session, "binding": binding})
        if create:
            atomic.create_exclusive(path, data)
        else:
            atomic.replace(path, data)

    def read_session(self, chat_id, session_id):
        path = self._session_path(chat_id, session_id)
        if not os.path.isfile(path):
            raise NotFound("no session %s on chat %s" % (session_id, chat_id))
        session, _binding = self._read_session_file(path)
        return session

    def read_binding(self, chat_id, session_id):
        path = self._session_path(chat_id, session_id)
        if not os.path.isfile(path):
            raise NotFound("no session %s on chat %s" % (session_id, chat_id))
        _session, binding = self._read_session_file(path)
        return binding

    def list_sessions(self, chat_id):
        directory = self._sessions_dir(chat_id)
        out = []
        for name in _json_names(directory):
            session, binding = self._read_session_file(os.path.join(directory, name))
            out.append((session, binding))
        out.sort(key=lambda pair: (pair[0]["created_at"], pair[0]["session_id"]))
        return out

    def chat_agent_status(self, chat_id):
        """Contract 8.3, as a query.

        Returns which bindings on this chat are open and which of its sessions
        are non-terminal. Both are read from the records; neither is cached.
        """
        terminal = contract.terminal_states()
        open_bindings = []
        non_terminal = []
        for session, binding in self.list_sessions(chat_id):
            if binding.get("released_at") is None:
                open_bindings.append(binding["binding_id"])
            if session["state"] not in terminal:
                non_terminal.append(session["session_id"])
        return {"open_bindings": open_bindings, "non_terminal_sessions": non_terminal}

    def create_session(self, chat_id, opening_message_id, launcher_id, capabilities):
        """Open the chat's next agent: one session, one binding, one write.

        Contract 8.3 permits this only when the chat has no open binding and no
        non-terminal session. Both answers are read under the chat lock, and the
        session and its binding are published together, because a session that
        exists without its binding is a state the contract rejects
        (`UNBOUND_ACTIVE_SESSION`).

        The creation transition cites the `msg_` user message that caused it
        (contract 5.2), and that message is checked to exist, to be on this
        chat, and to be user-authored. Opening the next agent is a user action;
        nothing here opens one on its own, and no elapsed time is consulted.
        """
        chat_dir = self._require_chat_dir(chat_id)
        opener = None
        for message in self.read_messages(chat_id):
            if message["message_id"] == opening_message_id:
                opener = message
                break
        if opener is None:
            raise ValidationRefused(
                "message %s is not in chat %s; a session is opened by a user turn "
                "that is actually in this chat's history" % (opening_message_id, chat_id)
            )
        if opener["author"] != "user":
            raise ValidationRefused(
                "message %s is authored by %r; a session is opened by a user turn"
                % (opening_message_id, opener["author"])
            )

        with atomic.ChatLock(os.path.join(chat_dir, ".lock")):
            status = self.chat_agent_status(chat_id)
            if status["open_bindings"]:
                raise ConcurrencyRefused(
                    "chat %s already has an open agent binding (%s); v0.1 is one agent "
                    "per chat" % (chat_id, ", ".join(status["open_bindings"]))
                )
            if status["non_terminal_sessions"]:
                raise ConcurrencyRefused(
                    "chat %s already has a non-terminal agent session (%s); an agent "
                    "that may still be alive holds this chat"
                    % (chat_id, ", ".join(status["non_terminal_sessions"]))
                )
            stamp = ids.now()
            session_id = ids.new_id("ses")
            session = {
                "record_type": "agent_session",
                "record_version": RECORD_VERSION,
                "session_id": session_id,
                "chat_id": chat_id,
                "created_at": stamp,
                "launcher_id": launcher_id,
                "launcher_capabilities": dict(capabilities),
                "state": "pending",
                "transitions": [
                    {
                        "from": None,
                        "to": "pending",
                        "owner": "user",
                        "at": stamp,
                        "evidence": {"kind": "user_action", "ref": opening_message_id},
                    }
                ],
            }
            binding = {
                "record_type": "agent_binding",
                "record_version": RECORD_VERSION,
                "binding_id": ids.new_id("bnd"),
                "chat_id": chat_id,
                "session_id": session_id,
                "bound_at": stamp,
                "released_at": None,
            }
            self._write_session_file(chat_id, session, binding, create=True)
        return session, binding

    def _issued_handles(self, chat_id, session_id):
        """The handles an accepted `launch_result` actually returned for a session.

        One derivation, used by every rule that needs it. Contract 4.3 and 6.1
        both turn on which handles a launcher genuinely issued, and two copies of
        that computation is how a rule and its enforcement drift apart.
        """
        issued = set()
        for result in self.read_launch_results(chat_id, session_id):
            if result.get("outcome") == "accepted" and result.get("agent_handle"):
                issued.add(result["agent_handle"])
        return issued

    def set_agent_handle(self, chat_id, session_id, handle):
        """Record the handle the launcher returned.

        Refused unless an accepted `launch_result` for this session actually
        returned that handle. Contract 4.3: a handle nobody issued makes `stop`
        and re-attachment target nothing, and does so silently.
        """
        if not isinstance(handle, str) or handle == "":
            raise ValidationRefused("an agent handle must be a non-empty string")
        with self._lock(chat_id):
            path = self._session_path(chat_id, session_id)
            if not os.path.isfile(path):
                raise NotFound("no session %s on chat %s" % (session_id, chat_id))
            session, binding = self._read_session_file(path)
            issued = self._issued_handles(chat_id, session_id)
            if handle not in issued:
                raise ValidationRefused(
                    "no accepted launch_result for session %s returned handle %r "
                    "(issued: %s)"
                    % (session_id, handle, ", ".join(sorted(issued)) or "none")
                )
            session["agent_handle"] = handle
            self._write_session_file(chat_id, session, binding)
        return session

    def _require_addressable(self, chat_id, session_id, what):
        """Refuse an operation that addresses an agent nothing gave us a handle for.

        Contract 6.1: `stop`, `events` and `deliver` take the `agent_handle` the
        launcher returned, and a launcher is required to remember nothing between
        calls. An operation recorded against a session with no issued handle
        therefore reached the agent by some other route, and the only other route
        is launcher-side memory -- the exact arrangement the seam exists to
        forbid.

        Stated over the handle the launcher *issued*, not over the field being
        populated. Keyed on the session's own field alone this would test the
        label on the claim: it would pass for any string written there, and it
        would be leaning on `set_agent_handle` to supply the fact behind it. That
        is the defect this ticket family has produced repeatedly, so this check
        carries its own fact and re-derives issuance from the packets.
        """
        session = self.read_session(chat_id, session_id)
        handle = session.get("agent_handle")
        issued = self._issued_handles(chat_id, session_id)
        # Membership is the whole test, and deliberately so. A type or
        # emptiness check alongside it would be unreachable: `issued` holds
        # only non-empty strings, a session whose `agent_handle` is neither a
        # string nor null is rejected as BAD_FIELD_TYPE by `read_session` above
        # before this line runs, and `None not in issued` is already true. A
        # clause that cannot fail is a clause no test can prove, and the
        # mechanical mutation probe on this rail found exactly that -- removing
        # it changed nothing anywhere.
        if handle not in issued:
            raise ValidationRefused(
                "%s addresses session %s, for which the launcher issued no handle "
                "(carried: %r; issued: %s); contract 6.1 addresses an agent through "
                "the handle the launcher returned, so there is nothing to pass"
                % (what, session_id, handle, ", ".join(sorted(issued)) or "none")
            )
        return session

    def append_transition(self, chat_id, session_id, expected_state, to_state, owner,
                          evidence, at=None):
        """Contract 8.6: append a transition atomically with respect to the state.

        `expected_state` is a compare-and-swap against what the session is
        actually in, so two concurrent writers cannot both append from the same
        state. The pair and its owner are checked against contract 5.2, and a
        transition into a terminal state releases the binding in the same write,
        because the contract requires a binding to be released once its session
        is terminal and a store may not sit in between.
        """
        with self._lock(chat_id):
            path = self._session_path(chat_id, session_id)
            if not os.path.isfile(path):
                raise NotFound("no session %s on chat %s" % (session_id, chat_id))
            session, binding = self._read_session_file(path)
            if session["state"] != expected_state:
                raise ConcurrencyRefused(
                    "session %s is in state %r, not the expected %r"
                    % (session_id, session["state"], expected_state)
                )
            authorized = contract.authorized_transitions()
            key = (session["state"], to_state)
            if key not in authorized:
                raise TransitionRefused(
                    "%s -> %s is not an authorized transition"
                    % (session["state"], to_state)
                )
            if authorized[key] != owner:
                raise TransitionRefused(
                    "%s -> %s is owned by %r, not %r"
                    % (session["state"], to_state, authorized[key], owner)
                )
            stamp = at or ids.now()
            session["transitions"] = list(session["transitions"]) + [
                {
                    "from": session["state"],
                    "to": to_state,
                    "owner": owner,
                    "at": stamp,
                    "evidence": dict(evidence),
                }
            ]
            session["state"] = to_state
            if to_state in contract.terminal_states() and binding.get("released_at") is None:
                binding = dict(binding)
                binding["released_at"] = stamp
            self._write_session_file(chat_id, session, binding)
        return session, binding

    # -- packets, results, observations ---------------------------------

    def _packets_dir(self, chat_id):
        return os.path.join(self._require_chat_dir(chat_id), "packets")

    def _append_packet(self, chat_id, record, name):
        self._check_record(record, record["record_type"])
        atomic.create_exclusive(
            os.path.join(self._packets_dir(chat_id), name + ".json"), _dumps(record)
        )
        return record

    def _read_packets(self, chat_id, record_type, session_id=None):
        directory = self._packets_dir(chat_id)
        out = []
        for name in _json_names(directory):
            path = os.path.join(directory, name)
            record = self._check_read(_loads(path), path)
            if record.get("record_type") != record_type:
                continue
            if session_id is not None and record.get("session_id") != session_id:
                continue
            out.append(record)
        out.sort(key=lambda r: (r.get("created_at") or r.get("observed_at"), json.dumps(r, sort_keys=True)))
        return out

    def append_launch_request(self, chat_id, session_id, instruction_text):
        if self.read_launch_requests(chat_id, session_id):
            raise ValidationRefused(
                "session %s already has a launch request; one launch attempt, one "
                "session, one run identity" % session_id
            )
        self.read_session(chat_id, session_id)
        record = {
            "record_type": "launch_request",
            "record_version": RECORD_VERSION,
            "request_id": ids.new_id("req"),
            "chat_id": chat_id,
            "session_id": session_id,
            "created_at": ids.now(),
            "instruction_encoding": "utf-8",
            "instruction_text": instruction_text,
        }
        return self._append_packet(chat_id, record, "launch_request-" + record["request_id"])

    def append_launch_result(self, chat_id, request_id, session_id, outcome,
                             agent_handle=None, failure_category=None, detail=None):
        record = {
            "record_type": "launch_result",
            "record_version": RECORD_VERSION,
            "request_id": request_id,
            "session_id": session_id,
            "observed_at": ids.now(),
            "outcome": outcome,
        }
        if agent_handle is not None:
            record["agent_handle"] = agent_handle
        if failure_category is not None:
            record["failure_category"] = failure_category
        if detail is not None:
            record["detail"] = detail
        return self._append_packet(chat_id, record, "launch_result-" + request_id)

    def append_delivery_request(self, chat_id, session_id, instruction_text):
        self._require_addressable(chat_id, session_id, "a delivery")
        existing = self._read_packets(chat_id, "delivery_request", session_id)
        record = {
            "record_type": "delivery_request",
            "record_version": RECORD_VERSION,
            "delivery_id": ids.new_id("dlv"),
            "chat_id": chat_id,
            "session_id": session_id,
            "sequence": len(existing) + 1,
            "created_at": ids.now(),
            "instruction_encoding": "utf-8",
            "instruction_text": instruction_text,
            "acknowledged": None,
        }
        return self._append_packet(chat_id, record, "delivery_request-" + record["delivery_id"])

    def append_session_observation(self, chat_id, session_id, kind, detail=None):
        """Record what an attempted boundary interaction actually produced.

        Contract 4.7. Every kind records the outcome of something the harness
        did. Nothing here is created by elapsed time, by silence, or by an
        absent record, and this store has no code path that creates one without
        a caller naming the interaction it attempted.

        A kind that could only have come from addressing a live agent is refused
        unless the launcher issued a handle for the session (contract 6.1). The
        exemptions are the kinds that record something other than a reached
        agent: `launch_timeout`, and `reattach_failed` on a session that never
        got a handle to re-attach with.
        """
        if kind in ADDRESSING_OBSERVATION_KINDS:
            self._require_addressable(chat_id, session_id, "a %r observation" % kind)
        self.read_session(chat_id, session_id)
        record = {
            "record_type": "session_observation",
            "record_version": RECORD_VERSION,
            "observation_id": ids.new_id("obs"),
            "chat_id": chat_id,
            "session_id": session_id,
            "observed_at": ids.now(),
            "kind": kind,
            "detail": detail,
        }
        return self._append_packet(
            chat_id, record, "session_observation-" + record["observation_id"]
        )

    def read_launch_requests(self, chat_id, session_id=None):
        return self._read_packets(chat_id, "launch_request", session_id)

    def read_launch_results(self, chat_id, session_id=None):
        return self._read_packets(chat_id, "launch_result", session_id)

    def read_delivery_requests(self, chat_id, session_id=None):
        return self._read_packets(chat_id, "delivery_request", session_id)

    def read_session_observations(self, chat_id, session_id=None):
        return self._read_packets(chat_id, "session_observation", session_id)

    # -- diagnostic events ----------------------------------------------

    def _events_dir(self, chat_id, session_id):
        if not ids.is_id(session_id, "ses"):
            raise NotFound("%r is not a session identifier" % (session_id,))
        return os.path.join(self.diagnostics_dir, chat_id, session_id)

    def next_event_sequence(self, chat_id, session_id):
        directory = self._events_dir(chat_id, session_id)
        return self._next_sequence(directory)

    def append_diagnostic_event(self, chat_id, session_id, sequence, source,
                                interpretation, interpreted_type, body,
                                encoding="utf-8", received_at=None):
        """Preserve one unit of raw integration evidence.

        Contract 8.4: a replayed `(session_id, sequence)` is already stored
        rather than a new record, so `events` may be re-read from any point
        without breaking contiguity. The replay check compares the preserved
        bytes, not only the sequence number: a *different* payload at a sequence
        that is already taken is a contradiction rather than a replay, and is
        refused.

        Contract 7 P2a: an event sourced to the agent requires its session to
        have reached `running`. The launcher may report before anything was
        started; the agent may not.
        """
        self.read_chat(chat_id)
        session = self.read_session(chat_id, session_id)
        if source == "agent":
            reached = set(
                t["to"] for t in session["transitions"] if isinstance(t, dict)
            )
            if "running" not in reached:
                raise ProvenanceRefused(
                    "event is sourced to the agent, but session %s never entered "
                    "'running'; there was no agent to produce it" % session_id
                )
        directory = self._events_dir(chat_id, session_id)
        os.makedirs(directory, exist_ok=True)
        record = self._check_record(
            {
                "record_type": "diagnostic_event",
                "record_version": RECORD_VERSION,
                "event_id": ids.new_id("evt"),
                "chat_id": chat_id,
                "session_id": session_id,
                "sequence": sequence,
                "received_at": received_at or ids.now(),
                "source": source,
                "interpretation": interpretation,
                "interpreted_type": interpreted_type,
                "raw": {"encoding": encoding, "body": body},
            },
            "diagnostic event",
        )
        path = os.path.join(directory, _seq_name(sequence))
        try:
            atomic.create_exclusive(path, _dumps(record))
        except FileExistsError:
            existing = self._check_read(_loads(path), path)
            if existing["raw"] != record["raw"] or existing["source"] != record["source"]:
                raise StoreCorrupt(
                    "session %s sequence %d is already preserved with different "
                    "evidence; a replay carries the same payload"
                    % (session_id, sequence)
                )
            return existing, False
        return record, True

    def read_diagnostic_event(self, chat_id, session_id, event_id):
        """One preserved event, located by its opaque identifier.

        The identifier is opaque, so this reads the events of the session and
        compares the record's own `event_id`. Nothing is parsed out of the
        identifier and no filename is derived from it.
        """
        if not ids.is_id(event_id, "evt"):
            return None
        directory = self._events_dir(chat_id, session_id)
        for name in _json_names(directory):
            path = os.path.join(directory, name)
            record = self._check_read(_loads(path), path)
            if record.get("event_id") == event_id:
                return record
        return None

    def read_diagnostic_events(self, chat_id, session_id=None, sequence_from=None,
                               sequence_to=None, limit=DIAGNOSTIC_PAGE_DEFAULT):
        """Contract P4: the bounded out-of-band diagnostic retrieval.

        Addressed by `chat_id`, optionally narrowed by `session_id` and a
        `sequence` range, returning a bounded number of preserved records. It
        derives nothing: no counting, no grouping, no interpretation, no
        ranking. A view that did any of those would be #82.
        """
        if not isinstance(limit, int) or limit < 1:
            raise ValidationRefused("limit must be a positive integer")
        limit = min(limit, DIAGNOSTIC_PAGE_MAX)
        # Contract section 3: an identifier is opaque and "no ordering,
        # timestamp, or filename may substitute for it". `session_id` becomes a
        # path component below, so it is checked here exactly as `_events_dir`
        # checks it. Without this a relative `session_id` addresses another
        # chat's preserved events, or a directory outside the store root
        # entirely, while the caller believes it named the chat it passed in --
        # which also defeats correlation rule P3 at the retrieval layer.
        if session_id is not None and not ids.is_id(session_id, "ses"):
            raise NotFound("%r is not a session identifier" % (session_id,))
        self.read_chat(chat_id)
        base = os.path.join(self.diagnostics_dir, chat_id)
        if not os.path.isdir(base):
            return []
        session_ids = sorted(os.listdir(base)) if session_id is None else [session_id]
        out = []
        for sid in session_ids:
            if sid.startswith(atomic.TEMP_PREFIX):
                continue
            directory = os.path.join(base, sid)
            if not os.path.isdir(directory):
                continue
            for name in _json_names(directory):
                path = os.path.join(directory, name)
                record = self._check_read(_loads(path), path)
                if record.get("record_type") != "diagnostic_event":
                    raise StoreCorrupt("%s does not hold a diagnostic event" % path)
                if sequence_from is not None and record["sequence"] < sequence_from:
                    continue
                if sequence_to is not None and record["sequence"] > sequence_to:
                    continue
                out.append(record)
        out.sort(key=lambda e: (e["session_id"], e["sequence"]))
        return out[:limit]

    def read_all_events_of_session(self, chat_id, session_id):
        """Every preserved event on a session, contiguity enforced.

        Used by export and by contiguity checking. Contract P3 requires event
        `sequence` to be contiguous from 1 within its session, so a gap here is
        a detected fault in the same way a message gap is.
        """
        directory = self._events_dir(chat_id, session_id)
        events = []
        for name in _json_names(directory):
            match = SEQ_NAME_RE.match(name)
            path = os.path.join(directory, name)
            if not match:
                raise StoreCorrupt("%s is not an event file name" % path)
            record = self._check_read(_loads(path), path)
            if int(match.group(1)) != record["sequence"]:
                raise StoreCorrupt(
                    "%s holds sequence %d; the file name says %d"
                    % (path, record["sequence"], int(match.group(1)))
                )
            events.append(record)
        events.sort(key=lambda e: e["sequence"])
        expected = 1
        for event in events:
            if event["sequence"] != expected:
                raise StoreCorrupt(
                    "session %s event sequence jumps from %d to %d"
                    % (session_id, expected - 1, event["sequence"])
                )
            expected += 1
        return events

    # -- export and verification ----------------------------------------

    def export_records(self):
        """Every durable record in the store, in a deterministic order.

        This is the input to the executable contract. It raises rather than
        skipping anything it cannot read: an exporter that quietly dropped an
        unreadable record would make "this store validates" a statement about
        what the exporter chose to emit rather than about what is on disk.
        """
        records = []
        for name in sorted(os.listdir(self.chats_dir)):
            if name.startswith(atomic.TEMP_PREFIX):
                continue
            chat_dir = os.path.join(self.chats_dir, name)
            if not os.path.isdir(chat_dir):
                raise StoreCorrupt("%s is not a chat directory" % chat_dir)
            chat_id = name
            records.append(self.read_chat(chat_id))
            records.extend(self.read_messages(chat_id))
            for session, binding in self.list_sessions(chat_id):
                records.append(session)
                records.append(binding)
                records.extend(self.read_all_events_of_session(chat_id, session["session_id"]))
            for record_type in ("launch_request", "launch_result",
                                "delivery_request", "session_observation"):
                records.extend(self._read_packets(chat_id, record_type))

        # Diagnostics for sessions that are not in any chat would otherwise go
        # unexported and therefore unchecked, so they are found from the
        # diagnostics tree rather than only from the chats tree.
        exported = set(
            (r["session_id"], r["sequence"]) for r in records
            if r.get("record_type") == "diagnostic_event"
        )
        for chat_id in sorted(os.listdir(self.diagnostics_dir)):
            if chat_id.startswith(atomic.TEMP_PREFIX):
                continue
            base = os.path.join(self.diagnostics_dir, chat_id)
            if not os.path.isdir(base):
                raise StoreCorrupt("%s is not a diagnostics directory" % base)
            for session_id in sorted(os.listdir(base)):
                if session_id.startswith(atomic.TEMP_PREFIX):
                    continue
                for name in _json_names(os.path.join(base, session_id)):
                    path = os.path.join(base, session_id, name)
                    record = self._check_read(_loads(path), path)
                    if (record.get("session_id"), record.get("sequence")) in exported:
                        continue
                    records.append(record)
        return records

    def snapshot(self, name="store", expect="accept", description=None):
        """The store as a contract fixture document."""
        meta = {"name": name, "expect": expect}
        if description:
            meta["description"] = description
        return {
            "fixture": meta,
            "contract_version": contract.contract_version(),
            "records": self.export_records(),
        }

    def verify(self):
        """Run the executable contract over everything on disk.

        Returns the list of violations; empty means the store satisfies the
        contract. This is the same function the fixture validator calls.
        """
        return contract.store_violations(self.export_records())

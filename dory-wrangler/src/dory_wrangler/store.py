"""The durable chat store: contract section 8, on the filesystem.

This is the only writer of durable records, and every guarantee the shell makes
about history is a guarantee about this module.

## Layout

    <root>/.store-lock                         held by the one serving process (D1)
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
import weakref

from . import atomic
from . import contract
from . import ids
from . import notices
from .errors import (
    ConcurrencyRefused,
    NotFound,
    ProvenanceRefused,
    ReadOnlyStore,
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


def _component(value, what):
    """One path component, or a refusal.

    Contract section 3: an identifier is opaque, and "no ordering, timestamp, or
    filename may substitute for it". A value that becomes a path component and
    is not a single component is a value being used as a *path* -- it addresses
    a record the caller did not name, and it does so while the caller believes
    it named the one it passed in.

    This is the store's single answer to that, and it is deliberately about the
    shape of the string rather than about which kind of identifier it is. The
    kind check (`ids.is_id(x, "cht")`) states what the caller must have meant;
    this states what the filesystem will be asked to do. Both are needed and
    neither implies the other: a kind check that is weakened, removed, or simply
    never written for one argument of a two-argument join is exactly how this
    defect has now reached the store twice.

    Four clauses, and no more than four. `os.path.isabs(value)`,
    `value != os.path.basename(value)` and an `os.altsep` clause were all here
    and are all gone: the first two are implied by `os.sep in value` on this
    platform and the third can never fire on it, so the mechanical mutation
    probe removed each of them with every test still green. A clause that cannot
    fail is a clause no test can prove, which is the argument
    `_require_addressable` already makes about its own missing type check. Each
    clause that remains is individually held (N2, N3, N9-N11, N7).
    """
    if not isinstance(value, str):
        raise NotFound("%r is not a %s" % (value, what))
    if value in ("", ".", ".."):
        raise NotFound("%r is not a usable %s" % (value, what))
    if os.sep in value or "\0" in value:
        raise NotFound("%r is not a usable %s" % (value, what))
    return value


def _under(base, *components):
    """Join `components` beneath `base`, each checked as a single component.

    Every path this store builds from a value it did not itself derive goes
    through here. `os.path.join` is not called on a caller-supplied value
    anywhere else, so "a caller cannot address outside the tree it named" is a
    property of one function rather than of every call site remembering to
    check.
    """
    checked = [_component(c, "path component") for c in components]
    return os.path.join(base, *checked)


def _listdir(directory):
    """Names in a directory that may not exist yet.

    A store is created by its first write, and a reader that opens it before
    then -- read-only tooling does not create anything -- sees an empty store
    rather than an error.
    """
    if not os.path.isdir(directory):
        return []
    return os.listdir(directory)


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
    """Durable storage for contract v0.1 records.

    ## One serving process per store (decision 0002, D1)

    Nothing sweeps, creates, publishes or locks anything in a store without
    first holding the store-level lock (`atomic.own_store`). Every write in this
    class goes through one of the gated primitives below -- `_publish_new`,
    `_publish_replace`, `_publish_tree`, `_make_dirs`, `_lock` -- and each takes
    the lock before it touches the filesystem, so a second process that tries to
    write is refused with `StoreInUse` before it has changed anything. The lock
    is taken on the first write (or by `acquire`, which the served application
    calls before re-attaching), held for the life of this object, and released
    by `close` or when the object is collected.

    Temp files are swept when this process takes the lock, not when an object
    is constructed: that is the only moment at which no write of this process
    can be in flight, and the lock means no other process has one either.

    `read_only=True` is for tooling that must work while a server runs
    (`validate_store.py`): it takes no lock, sweeps nothing, creates nothing,
    and refuses every write with `ReadOnlyStore`.
    """

    def __init__(self, root, sweep=True, read_only=False):
        self.root = os.path.abspath(root)
        self.chats_dir = os.path.join(self.root, "chats")
        self.diagnostics_dir = os.path.join(self.root, "diagnostics")
        self.read_only = bool(read_only)
        # Contract 4.2: richer content fails closed and is *counted* rather than
        # silently downgraded. This is that count.
        self.rejected_content_types = {}
        self._sweep = sweep
        self._release = None

    # -- the store-level lock (D1) --------------------------------------

    def acquire(self):
        """Hold this store for writing, or raise `StoreInUse` having changed nothing."""
        if self.read_only:
            raise ReadOnlyStore(
                "this store was opened for reading only; it takes no lock and writes "
                "nothing")
        if self._release is not None and self._release.alive:
            return
        key, first = atomic.own_store(self.root)
        # A store dropped without `close` gives its hold back when collected,
        # through the finalizer-safe release (it never waits for the guard).
        self._release = weakref.finalize(self, atomic.disown_collected_store, key)
        os.makedirs(self.chats_dir, exist_ok=True)
        os.makedirs(self.diagnostics_dir, exist_ok=True)
        if first and self._sweep:
            atomic.sweep_temp_files(self.root)

    def close(self):
        """Give the store-level lock back. A later write takes it again."""
        if self._release is not None:
            detached = self._release.detach()
            self._release = None
            if detached is not None:
                atomic.disown_store(*detached[2])

    @property
    def held(self):
        return self._release is not None and self._release.alive

    def _publish_new(self, path, data):
        self.acquire()
        atomic.create_exclusive(path, data)

    def _publish_replace(self, path, data):
        self.acquire()
        atomic.replace(path, data)

    def _publish_tree(self, parent, final_name, builder):
        self.acquire()
        atomic.create_tree_exclusive(parent, final_name, builder)

    def _make_dirs(self, path):
        self.acquire()
        os.makedirs(path, exist_ok=True)

    # -- paths ---------------------------------------------------------

    def _chat_dir(self, chat_id):
        if not ids.is_id(chat_id, "cht"):
            raise NotFound("%r is not a chat identifier" % (chat_id,))
        return _under(self.chats_dir, chat_id)

    def _require_chat_dir(self, chat_id):
        path = self._chat_dir(chat_id)
        if not os.path.isdir(path):
            raise NotFound("no chat %s" % chat_id)
        return path

    def _lock(self, chat_id):
        path = os.path.join(self._require_chat_dir(chat_id), ".lock")
        self.acquire()
        return atomic.ChatLock(path)

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
            self._publish_tree(self.chats_dir, chat_id, build)
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
        for name in sorted(_listdir(self.chats_dir)):
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

    def chat_ids(self):
        """The name of every chat directory, without reading any chat.

        For callers that must go on past one unreadable chat -- start-up
        re-attachment and the conversation list (review finding R8). `list_chats`
        still fails closed for the whole store, because it promises every chat
        and cannot keep that promise past one it cannot read; a caller of this
        reads each chat itself and decides what one it cannot read means.
        """
        return sorted(
            name for name in _listdir(self.chats_dir)
            if not name.startswith(atomic.TEMP_PREFIX)
            and os.path.isdir(os.path.join(self.chats_dir, name)))

    def archive_chat(self, chat_id):
        with self._lock(chat_id):
            chat = self.read_chat(chat_id)
            chat["state"] = "archived"
            chat["updated_at"] = ids.now()
            self._check_record(chat, "archived chat")
            self._publish_replace(
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
            self._publish_replace(
                os.path.join(self._chat_dir(chat_id), "chat.json"), _dumps(chat)
            )
        return chat

    def _touch_chat(self, chat_id, stamp):
        with self._lock(chat_id):
            chat = self.read_chat(chat_id)
            if chat["updated_at"] < stamp:
                chat["updated_at"] = stamp
                self._check_record(chat, "chat")
                self._publish_replace(
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

    @staticmethod
    def _message_record(chat_id, author, text, session_id, source_event_id, sequence, stamp):
        """The one composition of a `message` record: the write and its pre-flight."""
        return {
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
        }

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
            record = self._check_record(
                self._message_record(chat_id, author, text, session_id, source_event_id,
                                     sequence, ids.now()),
                "new message",
            )
            try:
                self._publish_new(
                    os.path.join(directory, _seq_name(sequence)), _dumps(record)
                )
            except FileExistsError as exc:
                # Another writer took this sequence between our read and our
                # write. `os.link` is the arbiter, so exactly one of us has it.
                last_error = exc
                continue
            atomic.fault("post_message_pre_chat_update")
            self._touch_chat(chat_id, record["created_at"])
            return record
        raise ConcurrencyRefused(
            "could not claim a message sequence in chat %s after %d attempts (%s)"
            % (chat_id, _MAX_SEQUENCE_RETRIES, last_error)
        )

    def append_user_message(self, chat_id, text):
        """A user turn. Carries null provenance: it is not derived from the integration."""
        return self._append_message(chat_id, "user", text, None, None)

    def append_system_message(self, chat_id, text):
        """A harness-authored notice, in the harness's fixed words only.

        Contract 4.2 permits `author: "system"` with null provenance and places
        no requirement on what it says (carried finding R6/A7), so a system
        message could carry anything -- including text taken from the
        integration and shown with no evidence behind it. This store closes
        that from its own side (decision 0006): the text must be exactly one of
        `notices.SYSTEM_TEXTS`, and anything else is refused before it is
        written. The shell renders a system message distinctly from agent
        speech.
        """
        if text not in notices.SYSTEM_TEXTS:
            raise ValidationRefused(
                "a system message carries the harness's fixed words only "
                "(notices.SYSTEM_TEXTS, decision 0006); nothing derived from the "
                "integration is ever system text")
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
        self._require_instruction_recorded(chat_id)
        return self._append_message(chat_id, "agent", text, session_id, source_event_id)

    def _require_instruction_recorded(self, chat_id):
        """Refuse an answer this chat has no preserved instruction for.

        Contract 6.2/6.4: the exact text sent to the agent is preserved for
        every turn, not only the first. The contract states that as a count over
        the finished store -- occasions on which the agent produced output must
        not exceed the instruction packets preserved on sessions that ran
        (`TURN_INSTRUCTION_MISSING`) -- and the same count is decidable at the
        moment the answer is written, which is the only moment at which refusing
        it keeps the store valid.

        An occasion is a maximal run of consecutive agent messages, counted
        exactly as the contract counts it, so the message about to be written
        opens a new occasion only when the chat's last user-or-agent message was
        not itself the agent's. `system` messages are neither, and are skipped
        here for the same reason the contract skips them.
        """
        previous = None
        answered = 0
        for message in self.read_messages(chat_id):
            author = message.get("author")
            if author not in ("user", "agent"):
                continue
            if author == "agent" and previous != "agent":
                answered += 1
            previous = author
        if previous != "agent":
            answered += 1  # the message about to be written opens a new occasion
        ran = set(
            session["session_id"]
            for session, _binding in self.list_sessions(chat_id)
            if "running" in set(
                t["to"] for t in session["transitions"] if isinstance(t, dict)
            )
        )
        recorded = len([
            packet
            for packet in (self.read_launch_requests(chat_id)
                           + self.read_delivery_requests(chat_id))
            if packet.get("session_id") in ran
        ])
        if recorded < answered:
            raise ProvenanceRefused(
                "chat %s would show %d occasion(s) of agent output against %d "
                "preserved instruction packet(s); the text sent to an agent is "
                "recorded before the answer to it is"
                % (chat_id, answered, recorded)
            )

    # -- sessions and bindings ------------------------------------------

    def _sessions_dir(self, chat_id):
        return os.path.join(self._require_chat_dir(chat_id), "sessions")

    def _session_path(self, chat_id, session_id):
        if not ids.is_id(session_id, "ses"):
            raise NotFound("%r is not a session identifier" % (session_id,))
        return _under(self._sessions_dir(chat_id), session_id + ".json")

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
            self._publish_new(path, data)
        else:
            self._publish_replace(path, data)

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

        The creation transition cites the `msg_` user message that caused it,
        and contract 5.2's precondition column says what that reference must
        resolve to: a message record, in this chat, authored by the user.
        Opening the next agent is a user action; nothing here opens one on its
        own, and no elapsed time is consulted.

        Those three facts used to be restated here as two explicit checks. They
        are not restated any more: `_require_precondition` puts the creation
        transition to the contract's own precondition check, exactly as
        `append_transition` does for every other row of the same table. The
        restatement had made the shared check unreachable on this route -- the
        mechanical enumeration removed it with every test still green -- which is
        the same "a repair can leave a guard nothing can prove" shape the last
        rail reported one function over.
        """
        with self._lock(chat_id):
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
            # Contract 7: one user turn opens at most one agent that runs. A
            # later binding is opened by a later user turn, never by the harness
            # replaying an earlier one, so a turn whose session actually reached
            # 'running' has been served and cannot open a second.
            for served in self._sessions_opened_by(chat_id, opening_message_id):
                if "running" in set(
                    t["to"] for t in served["transitions"] if isinstance(t, dict)
                ):
                    raise ValidationRefused(
                        "message %s already opened session %s, which ran; a later "
                        "agent is opened by a later user turn"
                        % (opening_message_id, served["session_id"])
                    )
            session, binding = self._session_records(
                chat_id, opening_message_id, launcher_id, capabilities, ids.now())
            self._require_precondition(
                chat_id, session, session["transitions"][0], error=ValidationRefused)
            self._write_session_file(chat_id, session, binding, create=True)
        return session, binding

    @staticmethod
    def _session_records(chat_id, opening_message_id, launcher_id, capabilities, stamp):
        """The one composition of a new session and its binding: the write and its pre-flight."""
        session_id = ids.new_id("ses")
        session = {
            "record_type": "agent_session",
            "record_version": RECORD_VERSION,
            "session_id": session_id,
            "chat_id": chat_id,
            "created_at": stamp,
            "launcher_id": launcher_id,
            "launcher_capabilities": (dict(capabilities) if isinstance(capabilities, dict)
                                      else capabilities),
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
        return session, binding

    # -- pre-flight: a user turn is recorded only if what it opens is acceptable --

    def preflight_launch(self, chat_id, user_text, launcher_id, capabilities,
                         instruction_text):
        """Refuse, having written nothing, a turn whose records the store would refuse.

        Review finding R4 (decision 0003's invariant): a user turn is recorded
        only if the session and packet it would open would themselves be
        accepted. `_launch_turn` used to write the user's message and only then
        discover that `create_session` or `append_launch_request` refused --
        a malformed `launcher_id`, capabilities changed after construction, a
        lone surrogate -- leaving a turn in the chat that was never offered to an
        agent. This composes the would-be message, session, binding and launch
        packet with the **same functions the writes use**, and puts each to the
        same checks the writes make: the contract's record check, contract 5.2's
        precondition for the creation transition (with the would-be message as
        its evidence), and the instruction bound the would-be session records.
        It reads; it writes nothing.
        """
        self.read_chat(chat_id)
        stamp = ids.now()
        message = self._check_record(
            self._message_record(chat_id, "user", user_text, None, None,
                                 self._next_sequence(self._messages_dir(chat_id)), stamp),
            "new message")
        session, binding = self._session_records(
            chat_id, message["message_id"], launcher_id, capabilities, stamp)
        self._check_record(session, "agent session")
        self._check_record(binding, "agent binding")
        self._require_precondition(chat_id, session, session["transitions"][0],
                                   error=ValidationRefused, would_be=message)
        request = self._launch_request_record(chat_id, session["session_id"],
                                              instruction_text, stamp)
        self._check_packet(chat_id, request, session=session)

    def preflight_delivery(self, chat_id, session_id, user_text, instruction_text):
        """The delivery path's pre-flight: the would-be message and delivery packet.

        `_require_deliverable` is the store's own delivery precondition, and the
        bound is the one the *session* recorded -- not the current launcher's,
        which after a restart may declare a different one (review finding R4).
        """
        self.read_chat(chat_id)
        message = self._check_record(
            self._message_record(chat_id, "user", user_text, None, None,
                                 self._next_sequence(self._messages_dir(chat_id)), ids.now()),
            "new message")
        session, stamp = self._require_deliverable(chat_id, session_id)
        existing = self._read_packets(chat_id, "delivery_request", session_id)
        packet = self._delivery_request_record(
            chat_id, session_id, max([d["sequence"] for d in existing] or [0]) + 1,
            instruction_text, stamp)
        self._check_packet(chat_id, packet, session=session)
        return message

    def _sessions_opened_by(self, chat_id, message_id):
        """Sessions whose creation transition cites this user message.

        Matched the way the contract matches it: the first transition, the one
        with no prior state, and the identifier its evidence names.
        """
        out = []
        for session, _binding in self.list_sessions(chat_id):
            transitions = session.get("transitions")
            if not isinstance(transitions, list) or not transitions:
                continue
            first = transitions[0]
            if not isinstance(first, dict) or first.get("from") is not None:
                continue
            evidence = first.get("evidence")
            ref = evidence.get("ref") if isinstance(evidence, dict) else None
            if ref == message_id:
                out.append(session)
        return out

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
        # The stamp the addressing record will carry is produced *here*, by the
        # same function that checks it, rather than by each caller afterwards.
        # Contract 6.1 also forbids addressing an agent before its handle
        # existed (`ADDRESSED_BEFORE_HANDLE_ISSUED`), and the store's clock is a
        # wall clock: a backwards step between the launch result and this write
        # dates the operation before the handle it used. There is no honest
        # record of that, so it is refused rather than written or adjusted.
        # Two callers write addressing records and a third would be added with
        # neither check if the stamp were theirs to make.
        stamp = ids.now()
        issued_at = self._handle_issued_at(chat_id, session_id, handle)
        if issued_at is not None and stamp[:19] < issued_at[:19]:
            raise ValidationRefused(
                "%s on session %s would be dated %s, before the accepted "
                "launch_result issued handle %r at %s; the store's clock has "
                "gone backwards and there is no true time to record"
                % (what, session_id, stamp, handle, issued_at)
            )
        return session, stamp

    def _handle_issued_at(self, chat_id, session_id, handle):
        """When an accepted launch_result first returned this handle."""
        earliest = None
        for result in self.read_launch_results(chat_id, session_id):
            if result.get("outcome") != "accepted" or result.get("agent_handle") != handle:
                continue
            at = result.get("observed_at")
            if isinstance(at, str) and (earliest is None or at < earliest):
                earliest = at
        return earliest

    def _require_deliverable(self, chat_id, session_id):
        """Refuse a delivery the contract would reject once it is on disk.

        Contract 6.5 and 8.x state four preconditions for a `delivery_request`,
        and they live within twenty lines of each other in the validator. Three
        of them are about the *session*, and each is refused here:

        * the launcher must have declared `continuation: persistent`, because
          delivering to an already-running agent is the capability that word
          names (`DELIVERY_NOT_SUPPORTED`);
        * the session must have *reached* `running`, because otherwise there was
          no agent to deliver to (`DELIVERY_NOT_SUPPORTED`, second clause --
          the same code, a different fact, and a guard that stopped at the first
          would be the defect this rail exists to close);
        * the session must not have exited, because `deliver` sends text to an
          agent that is already running (`DELIVERY_AFTER_AGENT_EXIT`).

        The fourth, `ADDRESSED_WITHOUT_HANDLE`, is `_require_addressable`.

        The exit check is stated over the session being terminal rather than
        over comparing this delivery's timestamp to the terminal one. The
        validator compares timestamps because a fixture is a history it did not
        watch being made; this store is writing *now*, so "the agent has already
        exited" is a fact it can read directly, and reading it directly is what
        keeps the rule from turning into a clock comparison that a one-second
        tie can slip through.
        """
        session, stamp = self._require_addressable(chat_id, session_id, "a delivery")
        capabilities = session.get("launcher_capabilities")
        mode = capabilities.get("continuation") if isinstance(capabilities, dict) else None
        if mode != "persistent":
            raise ValidationRefused(
                "session %s declares continuation %r; delivering to an "
                "already-running agent requires a launcher that declares "
                "'persistent'" % (session_id, mode)
            )
        reached = set(
            t["to"] for t in session["transitions"] if isinstance(t, dict)
        )
        if "running" not in reached:
            raise ValidationRefused(
                "session %s never entered 'running'; there is no agent to "
                "deliver to" % session_id
            )
        if session["state"] in contract.terminal_states():
            raise ValidationRefused(
                "session %s is in terminal state %r; 'deliver' sends text to an "
                "agent that is already running, and this one has exited"
                % (session_id, session["state"])
            )
        return session, stamp

    def _handle_entering_running(self, chat_id, session_id, session, offered):
        """The handle this session enters `running` with, or a refusal.

        Keyed on the state being *entered*, which is how contract 4.3 is keyed.
        Both authorized routes into `running` come through here -- the launch
        (`launching -> running`) and the re-attachment (`unknown -> running`) --
        because a guard written for the route a reproduction printed is the
        defect this rail exists to close, one table over.
        """
        issued = self._issued_handles(chat_id, session_id)
        handle = offered if offered is not None else session.get("agent_handle")
        if not (isinstance(handle, str) and handle and handle in issued):
            raise ValidationRefused(
                "session %s cannot enter 'running' without the handle the "
                "launcher issued (offered: %r; carried: %r; issued: %s); "
                "contract 4.3 requires a session that ever reached 'running' to "
                "carry one, so recording the transition first would put a "
                "rejected store on disk and leave it there if the handle never "
                "arrived"
                % (session_id, offered, session.get("agent_handle"),
                   ", ".join(sorted(issued)) or "none")
            )
        carried = session.get("agent_handle")
        if carried and carried != handle:
            raise ValidationRefused(
                "session %s already carries handle %r; a session has one agent, "
                "and %r is not it" % (session_id, carried, handle)
            )
        return handle

    # -- contract 5.2's precondition table -------------------------------

    def _find_event_in_chat(self, chat_id, event_id):
        """The preserved event with this identifier anywhere in the chat.

        Evidence may cite an event of another session in the same chat. The
        contract resolves such a reference and then reports it as a
        `CORRELATION_MISMATCH`; resolving only within the named session would
        report it as an unresolvable reference instead, which is a different
        (and less accurate) answer to the same question.
        """
        base = self._chat_events_dir(chat_id)
        if not os.path.isdir(base):
            return None
        for name in sorted(os.listdir(base)):
            if name.startswith(atomic.TEMP_PREFIX) or not ids.is_id(name, "ses"):
                continue
            event = self.read_diagnostic_event(chat_id, name, event_id)
            if event is not None:
                return event
        return None

    def _evidence_tables(self, chat_id, ref, would_be=None):
        """The records contract 5.2's precondition column resolves `ref` against.

        The contract supplies the rule and the store supplies the records. Only
        the referenced record is looked up: the precondition column resolves one
        reference per transition, so loading the chat's whole history to check
        one of them would make every transition cost the size of the chat.

        The lookup is keyed on the reference's own prefix, which is contract
        section 3's only legible part of an identifier. A reference whose prefix
        does not match the kind the transition requires therefore resolves to
        nothing, and the contract reports it as an invalid reference -- the same
        answer it gives for the same reference in a fixture.
        """
        tables = dict((name, {}) for name in contract.evidence_table_names())
        if not isinstance(ref, str):
            return tables
        if ref.startswith("msg_"):
            for message in self.read_messages(chat_id):
                if message["message_id"] == ref:
                    tables["messages"][ref] = message
            # A pre-flight asks about a message it has composed and not written.
            if would_be is not None and would_be.get("message_id") == ref:
                tables["messages"][ref] = would_be
        elif ref.startswith("evt_"):
            event = self._find_event_in_chat(chat_id, ref)
            if event is not None:
                tables["events"][ref] = event
        elif ref.startswith("req_"):
            for request in self.read_launch_requests(chat_id):
                if request["request_id"] == ref:
                    tables["requests"][ref] = request
            for result in self.read_launch_results(chat_id):
                if (result.get("request_id") == ref
                        and ref not in tables["results_by_request"]):
                    tables["results_by_request"][ref] = result
        elif ref.startswith("obs_"):
            for observation in self.read_session_observations(chat_id):
                if observation["observation_id"] == ref:
                    tables["observations"][ref] = observation
        return tables

    def _require_precondition(self, chat_id, session, transition,
                              error=TransitionRefused, would_be=None):
        """Refuse a transition contract 5.2's *precondition* column rejects.

        `AUTHORIZED_TRANSITIONS` and `TRANSITION_PRECONDITIONS` are the two
        halves of one table in contract section 5.2. The store checked the owner
        half and not the precondition half, so evidence of an admissible *kind*
        carrying the wrong *content* -- a `harness_action` naming a message where
        a launch request is required, an `unknown` inferred from a resolved
        observation -- wrote a transition the contract rejects.

        This is stated over the table rather than over the rows a reproduction
        printed, and it is the contract's own function that states it: the store
        hands the referenced records to `_validate_preconditions` and refuses
        whatever it reports. A row added to section 5.2, a requirement token
        given a new meaning, or a new violation code raised from that block is
        therefore enforced here on the day the contract gains it, without this
        module changing.

        Every route that appends a transition goes through here, including the
        creation transition `create_session` writes, because a guard installed
        on the route a finding named is the defect this rail exists to close.
        """
        probe = dict(session)
        probe["transitions"] = [transition]
        evidence = transition.get("evidence")
        ref = evidence.get("ref") if isinstance(evidence, dict) else None
        violations = contract.transition_precondition_violations(
            probe, self._evidence_tables(chat_id, ref, would_be=would_be)
        )
        if violations:
            detail = "; ".join("%s: %s" % (code, text) for code, _, text in violations)
            raise error(
                "%s -> %s does not satisfy contract 5.2: %s"
                % (transition.get("from"), transition.get("to"), detail)
            )
        return transition

    def append_transition(self, chat_id, session_id, expected_state, to_state, owner,
                          evidence, at=None, agent_handle=None):
        """Contract 8.6: append a transition atomically with respect to the state.

        `expected_state` is a compare-and-swap against what the session is
        actually in, so two concurrent writers cannot both append from the same
        state. The pair and its owner are checked against contract 5.2, and a
        transition into a terminal state releases the binding in the same write,
        because the contract requires a binding to be released once its session
        is terminal and a store may not sit in between.

        **The handle enters `running` in this same write.** Contract 4.3: a
        session that ever reached `running` must carry the handle an accepted
        `launch_result` returned (`SESSION_HANDLE_MISSING`), and that rule is
        stated over having *reached* the state, not over sitting in it -- so it
        is not a rule a later write can satisfy. A store that recorded the
        transition first and the handle afterwards would be rejected by the
        contract in between, and would stay rejected forever if the second write
        never came: nothing in the seam requires a harness to make it, and a
        harness that crashes between the two leaves a permanently invalid store
        with no fault recorded anywhere. That is a window, not an event, so it is
        closed the same way the session/binding pair is -- by making the two
        facts one write.

        `agent_handle` is therefore accepted here and refused anywhere else: it
        is meaningful only on the write that enters `running`. Both authorized
        transitions into `running` (`launching -> running` and
        `unknown -> running`) go through this one check; the second is how a
        re-attached session gets back, and it carries the handle it already had.

        The handle is checked against what the launcher actually issued, not
        against what the caller passed: `_issued_handles` is the same derivation
        `set_agent_handle` and `_require_addressable` use, so a handle nobody
        issued cannot enter the store through this door either.
        """
        if agent_handle is not None and to_state != "running":
            raise ValidationRefused(
                "agent_handle belongs to the write that enters 'running'; this "
                "transition goes to %r" % (to_state,)
            )
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
            if not isinstance(evidence, dict):
                raise ValidationRefused(
                    "a transition's evidence is an object naming what authorized "
                    "it; %r is not one" % (evidence,)
                )
            stamp = at or ids.now()
            transition = {
                "from": session["state"],
                "to": to_state,
                "owner": owner,
                "at": stamp,
                "evidence": dict(evidence),
            }
            self._require_precondition(chat_id, session, transition)
            session["transitions"] = list(session["transitions"]) + [transition]
            if to_state == "running":
                session["agent_handle"] = self._handle_entering_running(
                    chat_id, session_id, session, agent_handle
                )
            session["state"] = to_state
            if to_state in contract.terminal_states() and binding.get("released_at") is None:
                binding = dict(binding)
                binding["released_at"] = stamp
            self._write_session_file(chat_id, session, binding)
        return session, binding

    # -- packets, results, observations ---------------------------------

    def _packets_dir(self, chat_id):
        return os.path.join(self._require_chat_dir(chat_id), "packets")

    def _check_packet(self, chat_id, record, session=None):
        """The checks every packet is put to before it is written, in this order.

        The contract's record check comes first: it refuses text that is not
        UTF-8 (`INSTRUCTION_TEXT_NOT_UTF8`) before the bound check measures that
        text in UTF-8 bytes, which would otherwise raise out of the store as a
        `UnicodeEncodeError` rather than as a refusal.
        """
        self._check_record(record, record["record_type"])
        self._require_within_instruction_bound(chat_id, record, session=session)
        return record

    def _append_packet(self, chat_id, record, name):
        self._check_packet(chat_id, record)
        try:
            self._publish_new(
                _under(self._packets_dir(chat_id), name + ".json"), _dumps(record)
            )
        except FileExistsError as exc:
            # Exclusive creation is the arbiter between concurrent writers, and
            # a packet file that already exists means another writer took this
            # identity first. Raised as a refusal rather than as a bare OS error
            # so that every public write of this store fails with a StoreError.
            raise ConcurrencyRefused(
                "packet %s already exists in chat %s (%s)" % (name, chat_id, exc)
            )
        return record

    def _require_within_instruction_bound(self, chat_id, record, session=None):
        """Refuse a packet larger than the bound its session declared.

        Contract 6.3: this contract asserts no instruction bound of its own. The
        only bound is the one a launcher measured and the session recorded in
        `launcher_capabilities.instruction_bound_bytes`, and a packet over it
        would be a record of text that could not have been delivered.

        Stated over every packet that carries `instruction_text` rather than
        over the two methods that write one, because the contract states it over
        `launch_request` *and* `delivery_request` together and a guard on one of
        a pair is the defect this rail exists to close.
        """
        text = record.get("instruction_text")
        if not isinstance(text, str):
            # Either the record carries no instruction (a launch result, an
            # observation) or it carries one of the wrong type, which
            # `_check_record` reports as BAD_FIELD_TYPE a line later. A separate
            # "has the field at all" test would be a clause nothing could fail.
            return
        if session is None:
            session = self.read_session(chat_id, record["session_id"])
        capabilities = session.get("launcher_capabilities")
        bound = (capabilities.get("instruction_bound_bytes")
                 if isinstance(capabilities, dict) else None)
        if not isinstance(bound, int) or isinstance(bound, bool) or bound < 1:
            return  # no measured bound; this contract asserts none of its own
        size = len(text.encode("utf-8"))
        if size > bound:
            raise ValidationRefused(
                "instruction_text is %d bytes; session %s declares a measured "
                "bound of %d, so this text could not have been delivered"
                % (size, record["session_id"], bound)
            )

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
        record = self._launch_request_record(chat_id, session_id, instruction_text, ids.now())
        return self._append_packet(chat_id, record, "launch_request-" + record["request_id"])

    @staticmethod
    def _launch_request_record(chat_id, session_id, instruction_text, stamp):
        """The one composition of a `launch_request`: the write and its pre-flight."""
        return {
            "record_type": "launch_request",
            "record_version": RECORD_VERSION,
            "request_id": ids.new_id("req"),
            "chat_id": chat_id,
            "session_id": session_id,
            "created_at": stamp,
            "instruction_encoding": "utf-8",
            "instruction_text": instruction_text,
        }

    @staticmethod
    def _delivery_request_record(chat_id, session_id, sequence, instruction_text, stamp):
        """The one composition of a `delivery_request`: the write and its pre-flight."""
        return {
            "record_type": "delivery_request",
            "record_version": RECORD_VERSION,
            "delivery_id": ids.new_id("dlv"),
            "chat_id": chat_id,
            "session_id": session_id,
            "sequence": sequence,
            "created_at": stamp,
            "instruction_encoding": "utf-8",
            "instruction_text": instruction_text,
            "acknowledged": None,
        }

    def append_launch_result(self, chat_id, request_id, session_id, outcome,
                             agent_handle=None, failure_category=None, detail=None):
        """Record what the launcher reported about one launch request.

        A result is a report *about a request this store actually sent*, so the
        request has to be in this chat and has to name this session, and it may
        be reported once. Without those three, the public API could write a
        result naming no request at all (`DANGLING_REFERENCE`), a result naming
        another session's request (`CORRELATION_MISMATCH`), or a second result
        for one request (`DUPLICATE_LAUNCH_RESULT`, and `DUPLICATE_ID` with it,
        because contract section 8 identifies a launch result by the request it
        reports on).

        The first two are one lookup. The second two are the packet's *file
        name*, which is the request it reports on: exclusive creation refuses the
        second report of one launch, atomically and without a read-then-check
        that two writers could both pass. A read-then-check was written here
        first; the mechanical enumeration removed it with everything still green,
        because the file name was already doing the work.

        There is deliberately no separate "does this session exist" check either.
        A request is only ever written for a session that does, and the result
        must name the request's own session, so such a check could not fail --
        and the enumeration removed one from here, green, before it was deleted.
        """
        request = None
        for candidate in self.read_launch_requests(chat_id):
            if candidate["request_id"] == request_id:
                request = candidate
                break
        if request is None:
            raise ValidationRefused(
                "no launch request %s in chat %s; a launch result reports on a "
                "request this store sent" % (request_id, chat_id)
            )
        if request["session_id"] != session_id:
            raise ValidationRefused(
                "launch request %s belongs to session %s, not %s"
                % (request_id, request["session_id"], session_id)
            )
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
        """Send further text to an already-running agent.

        The `sequence` is claimed by exclusive creation, not by counting. A
        delivery used to be stored under its own opaque identifier, so two
        writers that both read "no deliveries yet" both wrote sequence 1 and
        neither file collided: four concurrent deliveries produced four records
        at sequence 1 and a store the contract rejects with
        `DUPLICATE_SEQUENCE`. Naming the file after the sequence makes the
        filesystem the arbiter, exactly as it already is for message sequences,
        so the loser retries against what the winner wrote.
        """
        session, stamp = self._require_deliverable(chat_id, session_id)
        last_error = None
        for _ in range(_MAX_SEQUENCE_RETRIES):
            existing = self._read_packets(chat_id, "delivery_request", session_id)
            sequence = max([d["sequence"] for d in existing] or [0]) + 1
            record = self._delivery_request_record(chat_id, session_id, sequence,
                                                   instruction_text, stamp)
            try:
                return self._append_packet(
                    chat_id, record,
                    "delivery_request-%s-%s" % (session_id, _seq_name(sequence)[:-len(".json")]),
                )
            except ConcurrencyRefused as exc:
                last_error = exc
                continue
        raise ConcurrencyRefused(
            "could not claim a delivery sequence on session %s after %d attempts (%s)"
            % (session_id, _MAX_SEQUENCE_RETRIES, last_error)
        )

    def record_delivery_acknowledgement(self, chat_id, session_id, delivery_id,
                                        acknowledged):
        """Record what `deliver` answered, on the delivery it answered.

        Contract 6.4: a `delivery_request` is written *before* the call, with
        `acknowledged: null`, because a turn sent to an agent and not recorded is
        a turn nobody can investigate. The answer arrives afterwards, so it is the
        one field of the one packet type that is filled in later -- and it is
        filled in once. `null` means "unknown", which is what the record already
        says; an acknowledgement already recorded is history and is not
        rewritten. Everything else in the packet is carried over unchanged and
        the whole record is re-checked by the contract before it is published.

        Located by the record's own `delivery_id` within the named session, never
        by a file name derived from the caller's arguments: the file replaced is
        the one named after the record that was found, and it must still hold
        that record.
        """
        if not isinstance(acknowledged, bool):
            raise ValidationRefused(
                "an acknowledgement is recorded as true or false; %r is not one, and "
                "null is what an unanswered delivery already says" % (acknowledged,)
            )
        # Review finding R9: the delivery is located *within the named session*,
        # so a session must be named. `read_delivery_requests` treats a missing
        # session as "every session", which answered a delivery with no session
        # named at all.
        if not ids.is_id(session_id, "ses"):
            raise ValidationRefused(
                "an acknowledgement names the session its delivery belongs to; %r is "
                "not a session identifier" % (session_id,)
            )
        with self._lock(chat_id):
            found = None
            for packet in self.read_delivery_requests(chat_id, session_id):
                if packet["delivery_id"] == delivery_id:
                    found = packet
                    break
            if found is None:
                raise ValidationRefused(
                    "no delivery %r on session %s in chat %s"
                    % (delivery_id, session_id, chat_id)
                )
            if found["acknowledged"] is not None:
                raise ValidationRefused(
                    "delivery %s already records acknowledged=%r; an answer that "
                    "was received is not rewritten"
                    % (delivery_id, found["acknowledged"])
                )
            updated = dict(found)
            updated["acknowledged"] = acknowledged
            self._check_record(updated, "delivery_request")
            path = _under(
                self._packets_dir(chat_id),
                "delivery_request-%s-%s.json"
                % (found["session_id"], _seq_name(found["sequence"])[:-len(".json")]),
            )
            on_disk = self._check_read(_loads(path), path) if os.path.isfile(path) else None
            if on_disk is None or on_disk.get("delivery_id") != delivery_id:
                raise StoreCorrupt(
                    "delivery %s is not stored under the name its session and "
                    "sequence give it" % delivery_id
                )
            self._publish_replace(path, _dumps(updated))
        return updated

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
            _session, stamp = self._require_addressable(
                chat_id, session_id, "a %r observation" % kind)
        else:
            self.read_session(chat_id, session_id)
            stamp = ids.now()
        record = {
            "record_type": "session_observation",
            "record_version": RECORD_VERSION,
            "observation_id": ids.new_id("obs"),
            "chat_id": chat_id,
            "session_id": session_id,
            "observed_at": stamp,
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

    def _chat_events_dir(self, chat_id):
        """The diagnostics directory of one chat.

        Contract section 3 again, and the reason this exists at all: the
        diagnostics tree is addressed by `chat_id` from five public methods, and
        until this rail three of them validated the *other* argument of the same
        join and not this one. Every diagnostics path is built from here, so the
        check cannot be present on some of them and absent on others.
        """
        if not ids.is_id(chat_id, "cht"):
            raise NotFound("%r is not a chat identifier" % (chat_id,))
        return _under(self.diagnostics_dir, chat_id)

    def _events_dir(self, chat_id, session_id):
        if not ids.is_id(session_id, "ses"):
            raise NotFound("%r is not a session identifier" % (session_id,))
        return _under(self._chat_events_dir(chat_id), session_id)

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
        capabilities = session.get("launcher_capabilities")
        shape = (capabilities.get("response_shape")
                 if isinstance(capabilities, dict) else None)
        if interpreted_type == contract.stream_end_type() and shape == "one_shot":
            raise ValidationRefused(
                "session %s declares response_shape 'one_shot', which returns a "
                "response rather than a stream; there is no end of stream for it "
                "to have observed" % session_id
            )
        directory = self._events_dir(chat_id, session_id)
        self._make_dirs(directory)
        # Contract P3: event `sequence` is contiguous from 1 within its session,
        # and "a gap means a turn was lost; it is never closed silently". A
        # caller naming a sequence beyond the next one writes that gap, and the
        # reader then fails closed on a store that is already invalid. Refusing
        # here keeps the gap out of the store instead of detecting it afterwards.
        next_sequence = self._next_sequence(directory)
        if (isinstance(sequence, int) and not isinstance(sequence, bool)
                and sequence > next_sequence):
            raise ValidationRefused(
                "session %s is at event sequence %d; preserving %d would leave a "
                "gap, and a gap means a turn was lost"
                % (session_id, next_sequence - 1, sequence)
            )
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
        path = _under(directory, _seq_name(sequence))
        try:
            self._publish_new(path, _dumps(record))
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
        base = self._chat_events_dir(chat_id)
        if not os.path.isdir(base):
            return []
        session_ids = sorted(os.listdir(base)) if session_id is None else [session_id]
        out = []
        for sid in session_ids:
            if sid.startswith(atomic.TEMP_PREFIX):
                continue
            directory = _under(base, sid)
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
        for name in sorted(_listdir(self.chats_dir)):
            if name.startswith(atomic.TEMP_PREFIX):
                continue
            chat_dir = _under(self.chats_dir, name)
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
        for chat_id in sorted(_listdir(self.diagnostics_dir)):
            if chat_id.startswith(atomic.TEMP_PREFIX):
                continue
            base = _under(self.diagnostics_dir, chat_id)
            if not os.path.isdir(base):
                raise StoreCorrupt("%s is not a diagnostics directory" % base)
            for session_id in sorted(os.listdir(base)):
                if session_id.startswith(atomic.TEMP_PREFIX):
                    continue
                for name in _json_names(_under(base, session_id)):
                    path = _under(base, session_id, name)
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

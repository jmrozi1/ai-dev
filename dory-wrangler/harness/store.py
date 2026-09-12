"""The durable store: contract sections 2 (durability) and 8 (what a store must support).

Durable records are canonical (D1). A chat and its ordered user-visible history
are readable from these records alone, with no live agent, no live launcher, and
no provider-side conversation. Reopening a store after a restart reads the same
records back.

The engine is deliberately dull -- an append-only JSON-lines log with
last-write-wins per (record_type, id), materialised into memory on open. The
contract mandates no storage engine; this one exists so that #87's guarantees
are exercised against something that really survives a process ending, and #86
may replace it wholesale.

Nothing here knows what a launcher is.
"""

from __future__ import annotations

import json
import os
import threading

# Identity of a record, per contract 4.
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

TERMINAL_SESSION_STATES = frozenset(
    ("completed", "failed", "launch_failed", "terminated", "abandoned")
)

DEFAULT_EVENT_PAGE_LIMIT = 200


class StoreError(Exception):
    """The store refused a write. Fail closed (contract D3)."""


class Store(object):
    """An append-only durable record store.

    Concurrency control is per chat, which is all contract section 8 requires:
    requirements 3 (open a binding only when no open binding and no non-terminal
    session exist) and 6 (read and append transitions atomically with respect to
    state) are the only ones needing it, and both are per chat. No global lock
    and no persistent coordinator.
    """

    def __init__(self, path=None):
        self._path = path
        self._records = {}          # (record_type, id) -> record
        self._order = []            # insertion order of keys, for stable snapshots
        self._event_keys = set()    # (session_id, sequence) already stored
        self._lock = threading.RLock()
        self._chat_locks = {}
        if path is not None:
            self._load()

    # -- persistence --------------------------------------------------------

    def _load(self):
        if not os.path.exists(self._path):
            return
        with open(self._path, "r") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                self._index(record)

    def _index(self, record):
        key = (record["record_type"], record[OWN_ID_FIELD[record["record_type"]]])
        if key not in self._records:
            self._order.append(key)
        self._records[key] = record
        if record["record_type"] == "diagnostic_event":
            self._event_keys.add((record["session_id"], record["sequence"]))

    def _append_durably(self, record):
        self._index(record)
        if self._path is None:
            return
        directory = os.path.dirname(os.path.abspath(self._path))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        with open(self._path, "a") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    # -- locking ------------------------------------------------------------

    def chat_lock(self, chat_id):
        with self._lock:
            lock = self._chat_locks.get(chat_id)
            if lock is None:
                lock = threading.RLock()
                self._chat_locks[chat_id] = lock
            return lock

    # -- writes -------------------------------------------------------------

    def put(self, record):
        """Insert or replace a record. Mutable records (a session's state and
        transitions, a binding's release, a launch result) are rewritten in
        place; the log keeps every version and the last one wins on reload."""
        rtype = record.get("record_type")
        if rtype not in OWN_ID_FIELD:
            raise StoreError("unknown record_type %r" % (rtype,))
        if record.get("record_version") != 1:
            raise StoreError("record_version must be 1 in contract v0.1")
        with self._lock:
            self._append_durably(json.loads(json.dumps(record)))

    def append_event(self, record):
        """Append a diagnostic event, treating a replayed (session_id, sequence)
        as already stored rather than as a new record (contract 8, item 4).

        This is what makes `events` resumable at-least-once without a replayed
        payload becoming a DUPLICATE_SEQUENCE violation. Returns True when the
        record was newly stored."""
        if record.get("record_type") != "diagnostic_event":
            raise StoreError("append_event takes a diagnostic_event")
        key = (record["session_id"], record["sequence"])
        with self._lock:
            if key in self._event_keys:
                return False
            self.put(record)
            return True

    def append_message(self, record):
        """Append a message with a sequence unique within its chat (contract 8, item 2)."""
        if record.get("record_type") != "message":
            raise StoreError("append_message takes a message")
        chat_id = record["chat_id"]
        with self.chat_lock(chat_id):
            taken = set(m["sequence"] for m in self.messages(chat_id))
            if record["sequence"] in taken:
                raise StoreError(
                    "message sequence %d is already used in chat %s"
                    % (record["sequence"], chat_id)
                )
            self.put(record)

    # -- reads --------------------------------------------------------------

    def get(self, record_type, record_id):
        with self._lock:
            return self._records.get((record_type, record_id))

    def all_of(self, record_type):
        with self._lock:
            return [
                self._records[key] for key in self._order if key[0] == record_type
            ]

    def snapshot(self):
        """Every current record, in insertion order. This is the store, and it is
        what the contract validator is run against."""
        with self._lock:
            return [json.loads(json.dumps(self._records[key])) for key in self._order]

    # -- contract section 8 queries ----------------------------------------

    def messages(self, chat_id):
        """Item 1: a chat's messages in `sequence` order, with no live process."""
        rows = [m for m in self.all_of("message") if m["chat_id"] == chat_id]
        rows.sort(key=lambda m: m["sequence"])
        return rows

    def next_message_sequence(self, chat_id):
        rows = self.messages(chat_id)
        return (max(m["sequence"] for m in rows) + 1) if rows else 1

    def sessions_of(self, chat_id):
        return [s for s in self.all_of("agent_session") if s["chat_id"] == chat_id]

    def open_bindings(self, chat_id):
        """Item 3, first half: which bindings on this chat have released_at null."""
        return [
            b
            for b in self.all_of("agent_binding")
            if b["chat_id"] == chat_id and b["released_at"] is None
        ]

    def non_terminal_sessions(self, chat_id):
        """Item 3, second half: which of this chat's sessions are non-terminal.

        This is the query the one-agent-per-chat rule actually turns on. A
        binding ledger that reads as tidy while a non-terminal session sits
        outside it is the exact shape contract 4.4 calls out, so the answer that
        matters is about sessions, not about bindings.
        """
        return [s for s in self.sessions_of(chat_id) if s["state"] not in TERMINAL_SESSION_STATES]

    def binding_for_session(self, session_id):
        for b in self.all_of("agent_binding"):
            if b["session_id"] == session_id:
                return b
        return None

    def events_of(self, session_id):
        rows = [e for e in self.all_of("diagnostic_event") if e["session_id"] == session_id]
        rows.sort(key=lambda e: e["sequence"])
        return rows

    def last_event_sequence(self, session_id):
        rows = self.events_of(session_id)
        return rows[-1]["sequence"] if rows else 0

    def retrieve_diagnostics(self, chat_id, session_id=None, sequence_from=None,
                             sequence_to=None, limit=DEFAULT_EVENT_PAGE_LIMIT):
        """Item 5, and contract rule P4's bounded out-of-band retrieval.

        A retrieval, not a dashboard: it returns preserved records and derives
        nothing. Anything that interprets, aggregates, ranks, or summarises them
        is #82.
        """
        rows = [e for e in self.all_of("diagnostic_event") if e["chat_id"] == chat_id]
        if session_id is not None:
            rows = [e for e in rows if e["session_id"] == session_id]
        if sequence_from is not None:
            rows = [e for e in rows if e["sequence"] >= sequence_from]
        if sequence_to is not None:
            rows = [e for e in rows if e["sequence"] <= sequence_to]
        rows.sort(key=lambda e: (e["session_id"], e["sequence"]))
        if limit is not None:
            rows = rows[:limit]
        return rows

    def launch_request_of(self, session_id):
        """Item 7: read an instruction packet back by session, since transition
        preconditions resolve against these records."""
        for r in self.all_of("launch_request"):
            if r["session_id"] == session_id:
                return r
        return None

    def deliveries_of(self, session_id):
        rows = [d for d in self.all_of("delivery_request") if d["session_id"] == session_id]
        rows.sort(key=lambda d: d["sequence"])
        return rows

    def observations_of(self, session_id):
        return [o for o in self.all_of("session_observation") if o["session_id"] == session_id]

    def write_snapshot_fixture(self, path, name, expect="accept", reason_code=None,
                               description=None):
        """Write the store in the validator's fixture form, so that every store a
        test produces can be handed to dory-wrangler/validator/validate_contract.py."""
        meta = {"name": name, "expect": expect}
        if description:
            meta["description"] = description
        if reason_code:
            meta["reason_code"] = reason_code
        document = {
            "fixture": meta,
            "contract_version": "0.1",
            "records": self.snapshot(),
        }
        directory = os.path.dirname(os.path.abspath(path))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        # Written next to the target and renamed, rather than through tempfile:
        # this module is imported by the chat and session layers, and it must
        # not drag process, shell, or archive machinery in behind it.
        tmp = path + ".partial"
        with open(tmp, "w") as out:
            json.dump(document, out, indent=2, sort_keys=False)
            out.write("\n")
        os.replace(tmp, path)
        return path

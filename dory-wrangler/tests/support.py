"""Shared test support: paths, the contract validator, and store checking.

Every store any test in this suite produces is checked against
`dory-wrangler/validator/validate_contract.py` -- the executable form of the
contract -- rather than against a local opinion of what the contract says.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DORY = os.path.dirname(HERE)
SRC = os.path.join(DORY, "src")
REPO = os.path.dirname(DORY)
VALIDATOR_PATH = os.path.join(DORY, "validator", "validate_contract.py")
# Produced stores are written outside the repository on purpose: they are test
# output, not source, and nothing is added outside `dory-wrangler/`.
FIXTURE_OUT = os.environ.get(
    "DORY_TEST_STORE_DIR",
    os.path.join(tempfile.gettempdir(), "dory-wrangler-stores"))
SCRATCH = os.path.join(tempfile.gettempdir(), "dory-wrangler-scratch")
# There is no separate divergence directory any more. #85 corrected the
# turn-instruction floor at `4ff8b63`, so the stores that used to be exported
# there as `expect: accept` regression material are now ordinary stores in
# phase 2 -- see `test_turn_floor_regression.py`. Keeping a second directory
# would have been the workaround, not the fix.

if SRC not in sys.path:
    sys.path.insert(0, SRC)


def load_validator():
    spec = importlib.util.spec_from_file_location("dory_validate_contract", VALIDATOR_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VALIDATOR = load_validator()


def violations(records):
    """Every violation the contract validator finds in a store, as (code, detail)."""
    report = VALIDATOR.Report()
    VALIDATOR.validate_store(report, records)
    return [(code, detail) for code, where, detail in report.sorted()]


def codes(records):
    return sorted(set(code for code, _ in violations(records)))


def _records_of(store):
    """The records of a `ChatStore`, or of a `StoreView` built from one."""
    return store.snapshot() if isinstance(store, StoreView) else store.export_records()


class StoreCheck(object):
    """Mixin: assert a store validates, and keep it for the CLI validator run."""

    def assert_store_valid(self, store, name, description=None):
        found = violations(_records_of(store))
        if found:
            self.fail(
                "store %r was expected to satisfy the contract but the validator "
                "reported:\n%s" % (name, "\n".join("  %s: %s" % v for v in found)))
        self.keep(store, name, description)

    def assert_store_rejected_for(self, store, code, name=None):
        found = codes(_records_of(store))
        if code not in found:
            self.fail("expected the validator to emit %s; it emitted %s"
                      % (code, ", ".join(found) or "nothing"))
        return found

    def keep(self, store, name, description=None):
        """Write the store out in fixture form so the CLI validator can be run
        over every store this suite produced, as independent evidence."""
        keep_records(_records_of(store), name, description)


def keep_records(records, name, description=None):
    if not os.path.isdir(FIXTURE_OUT):
        os.makedirs(FIXTURE_OUT)
    meta = {"name": name, "expect": "accept"}
    if description:
        meta["description"] = description
    path = os.path.join(FIXTURE_OUT, "%s.json" % name)
    if os.path.exists(path):
        raise AssertionError("two tests kept a store under the name %r" % name)
    with open(path + ".partial", "w") as out:
        json.dump({"fixture": meta, "contract_version": "0.1", "records": records},
                  out, indent=2)
        out.write("\n")
    os.replace(path + ".partial", path)


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


class StoreView(object):
    """#87's store queries, answered from one snapshot of a `ChatStore`.

    Read-only with respect to the store: it holds the records `export_records`
    read -- every one of them run through the contract on the way out -- and
    answers #87's query names over that list, so #87's tests keep asking the
    questions they asked of #87's store. `put` edits **the snapshot and nothing
    on disk**. That is exactly what the forging probes need: they take a store
    the product really wrote, change one record the way an attacker or a bug
    would, and hand the result to the contract validator. A probe that needs the
    *product* to meet a planted shape writes it to disk with `plant` instead.
    """

    def __init__(self, store):
        self._records = store.export_records()

    def snapshot(self):
        return json.loads(json.dumps(self._records))

    def put(self, record):
        record = json.loads(json.dumps(record))
        key = (record["record_type"], record[OWN_ID_FIELD[record["record_type"]]])
        for i, existing in enumerate(self._records):
            if (existing["record_type"],
                    existing[OWN_ID_FIELD[existing["record_type"]]]) == key:
                self._records[i] = record
                return
        self._records.append(record)

    def remove(self, record_type, record_id):
        self._records = [
            r for r in self._records
            if (r["record_type"], r[OWN_ID_FIELD[r["record_type"]]])
            != (record_type, record_id)]

    def all_of(self, record_type):
        return [json.loads(json.dumps(r)) for r in self._records
                if r["record_type"] == record_type]

    def get(self, record_type, record_id):
        for r in self.all_of(record_type):
            if r[OWN_ID_FIELD[record_type]] == record_id:
                return r
        return None

    def messages(self, chat_id):
        return sorted((m for m in self.all_of("message") if m["chat_id"] == chat_id),
                      key=lambda m: m["sequence"])

    def sessions_of(self, chat_id):
        return sorted((s for s in self.all_of("agent_session") if s["chat_id"] == chat_id),
                      key=lambda s: (s["created_at"], s["session_id"]))

    def open_bindings(self, chat_id):
        return [b for b in self.all_of("agent_binding")
                if b["chat_id"] == chat_id and b["released_at"] is None]

    def non_terminal_sessions(self, chat_id):
        terminal = VALIDATOR.TERMINAL_SESSION_STATES
        return [s for s in self.sessions_of(chat_id) if s["state"] not in terminal]

    def binding_for_session(self, session_id):
        for b in self.all_of("agent_binding"):
            if b["session_id"] == session_id:
                return b
        return None

    def events_of(self, session_id):
        return sorted((e for e in self.all_of("diagnostic_event")
                       if e["session_id"] == session_id), key=lambda e: e["sequence"])

    def last_event_sequence(self, session_id):
        rows = self.events_of(session_id)
        return rows[-1]["sequence"] if rows else 0

    def observations_of(self, session_id):
        return sorted((o for o in self.all_of("session_observation")
                       if o["session_id"] == session_id),
                      key=lambda o: (o["observed_at"], o["observation_id"]))

    def launch_request_of(self, session_id):
        for r in self.all_of("launch_request"):
            if r["session_id"] == session_id:
                return r
        return None

    def launch_result_of(self, session_id):
        for r in self.all_of("launch_result"):
            if r["session_id"] == session_id:
                return r
        return None

    def deliveries_of(self, session_id):
        return sorted((d for d in self.all_of("delivery_request")
                       if d["session_id"] == session_id), key=lambda d: d["sequence"])


def view(harness_or_store):
    """A fresh `StoreView` of a harness's store, or of a `ChatStore`."""
    store = getattr(harness_or_store, "store", harness_or_store)
    return StoreView(store)


def now():
    from dory_wrangler import ids
    return ids.now()


def plant(harness_or_store, record, chat_id=None):
    """Write one record straight onto disk, around the store.

    For the probes whose subject is what the *product* does when it meets a
    shape its own writer would never produce -- a crash window, a corrupted
    file, a record from an older writer. Nothing in the product calls this, and
    every record it writes is written where `ChatStore` itself would look for it,
    so the product reads it exactly as it reads everything else.
    """
    store = getattr(harness_or_store, "store", harness_or_store)
    rtype = record["record_type"]
    chat_dir = os.path.join(store.chats_dir, chat_id or record["chat_id"])

    def write(path, document):
        with open(path, "w") as out:
            json.dump(document, out, indent=2, sort_keys=True)
            out.write("\n")

    if rtype == "chat":
        write(os.path.join(chat_dir, "chat.json"), record)
    elif rtype == "message":
        write(os.path.join(chat_dir, "messages", "%08d.json" % record["sequence"]), record)
    elif rtype in ("agent_session", "agent_binding"):
        path = os.path.join(chat_dir, "sessions", record["session_id"] + ".json")
        with open(path) as handle:
            pair = json.load(handle)
        pair["session" if rtype == "agent_session" else "binding"] = record
        write(path, pair)
    elif rtype == "diagnostic_event":
        directory = os.path.join(store.diagnostics_dir, record["chat_id"],
                                 record["session_id"])
        if not os.path.isdir(directory):
            os.makedirs(directory)
        write(os.path.join(directory, "%08d.json" % record["sequence"]), record)
    else:
        name = {
            "launch_request": lambda r: "launch_request-" + r["request_id"],
            "launch_result": lambda r: "launch_result-" + r["request_id"],
            "delivery_request": lambda r: "delivery_request-%s-%08d"
                                          % (r["session_id"], r["sequence"]),
            "session_observation": lambda r: "session_observation-" + r["observation_id"],
        }[rtype](record)
        write(os.path.join(chat_dir, "packets", name + ".json"), record)


THREE_TURNS = (
    "What does the launch boundary do?",
    "And what does it deliberately not do?",
    "Who may stop a running agent in v0.1?",
)


def release(harness):
    """Close any operating-system processes a launcher still holds.

    Test plumbing only. It reaches a concrete launcher's own cleanup, never a
    boundary operation, and it changes no durable record.
    """
    releaser = getattr(harness._boundary, "release_all", None)
    if releaser is not None:
        releaser()


def run_three_turns(harness, title="Continuation"):
    """The same conversation, whatever the launcher underneath is.

    This function is the mode-invariance experiment: it is written once, knows
    nothing about continuation mode or response shape, and is run against every
    launcher and every capability combination.
    """
    chat_id = harness.create_chat(title)
    for text in THREE_TURNS:
        harness.send_turn(chat_id, text)
    return chat_id


def end_chat(harness, chat_id, reason="the user is done"):
    """Release whatever the chat still holds, the way a user would."""
    from dory_wrangler.errors import NotPermitted
    try:
        harness.stop_agent(chat_id, reason)
    except NotPermitted:
        return
    session = harness._active_session(chat_id)
    if session is not None and session["state"] == "unknown":
        harness.abandon(chat_id)


# Every capability combination this repository can actually produce. Both
# continuation modes and both response shapes, across two independent launcher
# implementations, one of which starts real operating-system processes.
CONFIGURATIONS = [
    ("dev-local-one-shot",
     {"launcher": "dev-local", "options": {"profile": "one_shot"}}),
    ("dev-local-persistent",
     {"launcher": "dev-local", "options": {"profile": "persistent"}}),
    ("stub-fresh-binding-one-shot",
     {"launcher": "scripted-stub",
      "options": {"continuation": "fresh_binding", "response_shape": "one_shot"}}),
    ("stub-fresh-binding-stream",
     {"launcher": "scripted-stub",
      "options": {"continuation": "fresh_binding", "response_shape": "stream"}}),
    ("stub-persistent-one-shot",
     {"launcher": "scripted-stub",
      "options": {"continuation": "persistent", "response_shape": "one_shot"}}),
    ("stub-persistent-stream",
     {"launcher": "scripted-stub",
      "options": {"continuation": "persistent", "response_shape": "stream"}}),
]


def expected_transcript():
    rows = []
    for i, text in enumerate(THREE_TURNS):
        rows.append((2 * i + 1, "user", text))
        rows.append((2 * i + 2, "agent", "answer to: %s" % text))
    return rows


def harness(config, store_path=None, **kwargs):
    """A harness over a `ChatStore` at `store_path`, or at a fresh scratch root.

    Reopening one over an existing root is how these tests restart: a new
    `SessionManager`, a new launcher instance, and nothing carried over but what
    is on disk.
    """
    from dory_wrangler.wiring import open_harness
    if store_path is None:
        if not os.path.isdir(SCRATCH):
            os.makedirs(SCRATCH)
        store_path = tempfile.mkdtemp(prefix="store-", dir=SCRATCH)
    return open_harness(config, store_path, **kwargs)


def scratch_root(prefix="root-"):
    if not os.path.isdir(SCRATCH):
        os.makedirs(SCRATCH)
    return tempfile.mkdtemp(prefix=prefix, dir=SCRATCH)

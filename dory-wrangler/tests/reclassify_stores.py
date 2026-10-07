#!/usr/bin/env python3
"""Re-classify every preserved event in a directory of kept stores, from its bytes.

    python3 dory-wrangler/tests/reclassify_stores.py [STORE_DIR] [--json]

**Test tooling, not product, and not a retrieval tool.** It reads the stores the
suite kept (`support.FIXTURE_OUT`, the files phase 2 of `run_tests.py` hands to
the validator) and asks one question of each `diagnostic_event`: does running
its `raw.body`, decoded per `raw.encoding`, back through the classifier of the
launcher that session recorded reproduce the recorded `interpretation` and
`interpreted_type` exactly? That is the claim that classification is a function
of the preserved bytes, measured over every kept store rather than a sample.

Each event lands in exactly one row:

``reclassified``
    a line of a wire format some launcher in this repository reads: agent output
    of `dev-local` and `scripted-stub` (the development transport,
    `launchers/dev_transport.py`), and of the `internal-bridge` model (Codex
    JSONL, `tests/internal_bridge.py`) -- including that model's failed-launch
    lines, which are the same format sourced to the launcher. Reported as
    reproduced or not.
``synthesised``
    a launcher's own observation of the agent it runs, which it writes rather
    than reads: `dev-local`'s and `scripted-stub`'s lifecycle and end-of-stream
    records, and the model's fresh-binding `session_completed`. There is no
    wire format to re-read them against, so they are not re-classifiable; what
    *is* checked is that the `type` in their own bytes is the type recorded, or
    that the harness declined it (a `stream_end` on a session with no stream is
    written `unrecognized`, the human's decision of 2026-09-15).
``other launcher``
    a session recorded against a launcher id no classifier here belongs to.
    **This fails the gate** unless `ADJUDICATED_LAUNCHERS` names that id, with
    the reason its records cannot be re-read (render review F1): today only the
    three test probes written against the interface alone. A new launcher --
    #90's real one included -- is unchecked until it is given a classifier
    here, so it fails here until then rather than passing unread.

A record that does not reproduce is printed with its store, session and
sequence. Probes that keep a shipped launcher's id and hand the harness readings
no classifier would produce, on purpose, show up here as non-reproducing or
inconsistent and are adjudicated by name in `ADJUDICATED`, not hidden.

**Rendering (A6, decision 0005).** The same pass then asks one question of each
`message` with `author: "agent"`: does re-classifying the bytes of the event it
cites, with that session's launcher's classifier, yield a recognized
`assistant_text` whose text is the message's `content.text` exactly -- byte for
byte, no strip, no newline normalisation, no join? And, the other way round,
is every agent-sourced event whose bytes re-read as non-empty chat text cited by
exactly one message, with a session's messages citing its events in event
order? A message on a session whose launcher no classifier here reads is
counted as `other launcher` and cannot be checked; it is never counted as
exact, and it fails the gate unless its launcher is adjudicated by name.

**System messages (decision 0006).** Every `author: "system"` message must be
exactly one of the harness's fixed words (`notices.SYSTEM_TEXTS`) -- so no
system message carries anything derived from the integration -- and must follow
a user turn, at most once per turn, in a turn that has no agent message.

**Adjudication keys** (classify check L1) name one record and nothing else: the
store file, the session -- by its chat's `chat_id` and the sequence of the user
message that opened it, which is unique within a store (malformed review F2: a
chat's title is not, so a same-titled twin chat was covered by its twin's
entry), and which a test that keeps an adjudicated record makes stable from run
to run by creating that chat under a fixed id -- the record's sequence, and a
SHA-256 of the record's body (`raw.body` for an event, `content.text` for a
message). A different record at the same place, or the same record with
different bytes, is not covered.

**Exit status** is 0 only when every re-classified record reproduced, every
synthesised record is consistent with its own bytes, every agent message
renders its cited event's text exactly, every system message is the harness's
fixed words in its place, and no record sits on a launcher no classifier reads,
apart from the records `ADJUDICATED` and `ADJUDICATED_MESSAGES` name and the
launchers `ADJUDICATED_LAUNCHERS` names; any other disagreement exits 1, with
or without `--json`. `run_tests.py`
runs this over every kept store as its own phase, so the re-read is a gate, not
a one-time measurement.
"""

import base64
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(os.path.dirname(HERE), "src")]

import support  # noqa: E402
import internal_bridge  # noqa: E402
from dory_wrangler.launchers import dev_transport  # noqa: E402
from dory_wrangler.notices import SYSTEM_TEXTS  # noqa: E402

DEV_TRANSPORT_LAUNCHERS = ("dev-local", "scripted-stub")
CODEX_LAUNCHERS = ("internal-bridge",)

def body_hash(text):
    """SHA-256 of a record's body as stored, the last element of every key."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# Records that disagree with their bytes on purpose, each by store file, session
# key (`session_key`), sequence, recorded reading and the hash of its `raw.body`,
# with the reason. Nothing else may disagree.
ADJUDICATED = {
    ("page-preserved-past-a-clock-refusal.json", "cht_c10c4ref05a1m1dpage0001/1", 3,
     ("unrecognized", None),
     body_hash('{"type": "exit_report"}')):
        "test_convergence.PagedLauncher, a probe that keeps the scripted-stub id, "
        "hands the harness a launcher-sourced {\"type\": \"exit_report\"} stated "
        "unrecognized; no launcher synthesises it, so there is no reading to match",
}


# Agent messages that disagree with their cited event's bytes on purpose, each by
# store file, session key, message sequence and the hash of its text, with the
# reason. Empty: no store the suite keeps has one, and a new one must be named
# here to pass.
ADJUDICATED_MESSAGES = {}

# Launcher ids no classifier here reads, each named with the reason its records
# cannot be re-read. Every record on any other unread launcher fails the gate.
ADJUDICATED_LAUNCHERS = {
    "intake-probe":
        "test_intake.PageLauncher: a probe written against the interface alone, "
        "handing the harness pages whose readings it states itself, to exercise "
        "preservation; it has no wire format to re-read",
    "out-of-tree":
        "test_swappability's out-of-tree launcher: the point of the probe is that "
        "it is defined outside this package and registered nowhere; its readings "
        "are its own and have no wire format here",
    "replaying":
        "test_launch_adversarial's replaying launcher: replays pages it built "
        "itself to probe at-least-once delivery; no wire format",
}

# The reading a cited event's bytes must re-read as for its text to be chat.
CHAT_TEXT_READING = ("recognized", "assistant_text")


def session_keys(records):
    """session_id -> "<chat_id>/<sequence of the user message that opened it>".

    Unique within a store: a chat id names one chat, and a user message opens at
    most one session. A chat's title named it before, and two chats may share a
    title (malformed review F2). A kept store's ids are random from run to run,
    so a test whose record is adjudicated creates its chat under a fixed id."""
    opened = dict((r["message_id"], r["sequence"]) for r in records
                  if r.get("record_type") == "message")
    keys = {}
    for session in (r for r in records if r.get("record_type") == "agent_session"):
        transitions = session.get("transitions") or [{}]
        ref = (transitions[0].get("evidence") or {}).get("ref")
        keys[session["session_id"]] = "%s/%s" % (session["chat_id"], opened.get(ref))
    return keys


def adjudicated(row):
    """`row` is a disagreement: (store, launcher, session, sequence, source,
    recorded, ..., session key, body hash) -- the last two always."""
    return (row[0], row[-2], row[3], tuple(row[5]), row[-1]) in ADJUDICATED


def raw_bytes(event):
    raw = event["raw"]
    if raw["encoding"] == "base64":
        return base64.b64decode(raw["body"])
    return raw["body"].encode("utf-8")


def declined(reading, session):
    """What the harness writes for a reading: `stream_end` on a session whose
    recorded launcher has no stream is kept `unrecognized` / null."""
    shape = (session.get("launcher_capabilities") or {}).get("response_shape")
    if reading == ("recognized", "stream_end") and shape != "stream":
        return ("unrecognized", None)
    return reading


def row_for(event, session):
    """-> (row, expected reading or None)."""
    launcher = session.get("launcher_id")
    source = event["source"]
    if launcher in DEV_TRANSPORT_LAUNCHERS:
        if source == "agent":
            return "reclassified", dev_transport.interpret(raw_bytes(event))[:2]
        return "synthesised", None
    if launcher in CODEX_LAUNCHERS:
        if source == "agent" or not session.get("agent_handle"):
            # agent output, or a failed launch's lines: the same format either way
            return "reclassified", internal_bridge.interpret(raw_bytes(event))[:2]
        return "synthesised", None
    return "other launcher", None


def reread(event, session):
    """-> `(interpretation, interpreted_type, text)` of the event's bytes through
    the classifier of the launcher its session recorded, or None when no
    classifier here reads that launcher's wire format."""
    launcher = session.get("launcher_id")
    if launcher in DEV_TRANSPORT_LAUNCHERS:
        return dev_transport.interpret(raw_bytes(event))
    if launcher in CODEX_LAUNCHERS:
        return internal_bridge.interpret(raw_bytes(event))
    return None


def rendering_rows(name, records, sessions, events, counts):
    """The A6 check over one store. Returns the disagreements, each a tuple
    `(store, launcher, session, message sequence or None, event sequence or None,
    what, recorded text, re-read text)`."""
    found = []
    messages = sorted((r for r in records
                       if r.get("record_type") == "message" and r.get("author") == "agent"),
                      key=lambda m: (m["chat_id"], m["sequence"]))
    cited = Counter()
    last_cited = {}
    for message in messages:
        session = sessions[message["session_id"]]
        launcher = session.get("launcher_id")
        event = events[message["source_event_id"]]
        cited[event["event_id"]] += 1
        text = message["content"]["text"]
        counts[launcher]["agent messages"] += 1
        got = reread(event, session)
        if got is None:
            counts[launcher]["agent messages, other launcher"] += 1
            continue
        if tuple(got[:2]) == CHAT_TEXT_READING and got[2] == text:
            counts[launcher]["agent messages exact"] += 1
        else:
            counts[launcher]["agent messages not exact"] += 1
            found.append((name, launcher, message["session_id"], message["sequence"],
                          event["sequence"], "text differs from its cited event", text,
                          got[2] if tuple(got[:2]) == CHAT_TEXT_READING else got[:2]))
        previous = last_cited.get(message["session_id"])
        if previous is not None and event["sequence"] <= previous:
            found.append((name, launcher, message["session_id"], message["sequence"],
                          event["sequence"], "cites an event out of event order", text,
                          previous))
        last_cited[message["session_id"]] = event["sequence"]
    for event in sorted(events.values(), key=lambda e: (e["session_id"], e["sequence"])):
        if event["source"] != "agent":
            continue
        session = sessions[event["session_id"]]
        got = reread(event, session)
        if got is None or tuple(got[:2]) != CHAT_TEXT_READING or not got[2]:
            continue
        launcher = session.get("launcher_id")
        counts[launcher]["chat-text events"] += 1
        times = cited[event["event_id"]]
        if times == 1:
            counts[launcher]["chat-text events rendered once"] += 1
        else:
            counts[launcher]["chat-text events rendered %s" % (
                "never" if times == 0 else "%d times" % times)] += 1
            found.append((name, launcher, event["session_id"], None, event["sequence"],
                          "chat-text event rendered %d time(s)" % times, None, got[2]))
    return found


def message_adjudicated(row, keys):
    text = row[6] if isinstance(row[6], str) else ""
    return (row[0], keys.get(row[2]), row[3], body_hash(text)) in ADJUDICATED_MESSAGES


def unread_launcher_rows(name, records, sessions):
    """Every record on a session whose launcher no classifier here reads and no
    adjudication names: one row per such session, with what it carries."""
    found = []
    carried = defaultdict(Counter)
    for record in records:
        if record.get("record_type") == "diagnostic_event" or (
                record.get("record_type") == "message" and record.get("author") == "agent"):
            carried[record["session_id"]][record["record_type"]] += 1
    for session_id, what in sorted(carried.items()):
        launcher = sessions[session_id].get("launcher_id")
        if (launcher in DEV_TRANSPORT_LAUNCHERS or launcher in CODEX_LAUNCHERS
                or launcher in ADJUDICATED_LAUNCHERS):
            continue
        found.append((name, launcher, session_id, dict(what)))
    return found


def system_rows(name, records):
    """Decision 0006 over one store: every system message is the harness's fixed
    words, follows a user turn, is the only one in its turn, and is in a turn
    with no agent message."""
    found = []
    by_chat = defaultdict(list)
    for record in records:
        if record.get("record_type") == "message":
            by_chat[record["chat_id"]].append(record)
    for chat_id, messages in sorted(by_chat.items()):
        turn = None  # authors since the last user message; None before the first
        for message in sorted(messages, key=lambda m: m["sequence"]):
            author = message["author"]
            if author == "user":
                turn = []
                continue
            if author == "system":
                text = message["content"]["text"]
                if text not in SYSTEM_TEXTS:
                    found.append((name, chat_id, message["sequence"],
                                  "system text is not the harness's fixed words", text))
                if turn is None:
                    found.append((name, chat_id, message["sequence"],
                                  "system message before any user turn", text))
                elif "system" in turn:
                    found.append((name, chat_id, message["sequence"],
                                  "a second system message in one turn", text))
                elif "agent" in turn:
                    found.append((name, chat_id, message["sequence"],
                                  "a system notice in a turn that has an answer", text))
            elif author == "agent" and turn is not None and "system" in turn:
                found.append((name, chat_id, message["sequence"],
                              "an answer after the turn's notice", None))
            if turn is not None:
                turn.append(author)
    return found


def synthesised_consistent(event, session):
    try:
        own_type = json.loads(raw_bytes(event).decode("utf-8")).get("type")
    except (ValueError, AttributeError):
        own_type = None
    recorded = (event["interpretation"], event["interpreted_type"])
    if session.get("launcher_id") in CODEX_LAUNCHERS:
        # The model's fresh-binding line names itself, not a type.
        return recorded == ("recognized", "session_completed")
    return recorded == declined(("recognized", own_type), session)


def main(argv):
    as_json = "--json" in argv
    args = [a for a in argv if not a.startswith("--")]
    store_dir = args[0] if args else support.FIXTURE_OUT
    names = sorted(n for n in os.listdir(store_dir) if n.endswith(".json"))
    counts = defaultdict(Counter)
    readings = defaultdict(Counter)
    mismatches = []
    inconsistent = []
    message_counts = defaultdict(Counter)
    rendering = []
    rendering_failing = []
    unread = []
    system = []
    for name in names:
        with open(os.path.join(store_dir, name)) as handle:
            records = json.load(handle)["records"]
        sessions = dict((r["session_id"], r) for r in records
                        if r.get("record_type") == "agent_session")
        keys = session_keys(records)
        rows = rendering_rows(
            name, records, sessions,
            dict((r["event_id"], r) for r in records
                 if r.get("record_type") == "diagnostic_event"),
            message_counts)
        rendering.extend(rows)
        rendering_failing.extend(r for r in rows if not message_adjudicated(r, keys))
        unread.extend(unread_launcher_rows(name, records, sessions))
        system.extend(system_rows(name, records))
        for event in (r for r in records if r.get("record_type") == "diagnostic_event"):
            session = sessions[event["session_id"]]
            launcher = session.get("launcher_id")
            row, expected = row_for(event, session)
            recorded = (event["interpretation"], event["interpreted_type"])
            counts[launcher]["events"] += 1
            counts[launcher][row] += 1
            readings[launcher][recorded] += 1
            identity = (keys.get(event["session_id"]), body_hash(event["raw"]["body"]))
            if row == "reclassified":
                expected = declined(tuple(expected), session)
                if tuple(expected) == recorded:
                    counts[launcher]["reproduced"] += 1
                else:
                    counts[launcher]["not reproduced"] += 1
                    mismatches.append((name, launcher, event["session_id"], event["sequence"],
                                       event["source"], recorded, tuple(expected)) + identity)
            elif row == "synthesised":
                if not synthesised_consistent(event, session):
                    inconsistent.append((name, launcher, event["session_id"],
                                         event["sequence"], event["source"], recorded)
                                        + identity)
    failing = [m for m in mismatches + inconsistent if not adjudicated(m)]
    result = {
        "stores": len(names),
        "per_launcher": dict((k, dict(v)) for k, v in sorted(counts.items(), key=str)),
        "readings": dict((k, dict(("%s/%s" % r, n) for r, n in v.items()))
                         for k, v in sorted(readings.items(), key=str)),
        "not_reproduced": mismatches,
        "synthesised_inconsistent": inconsistent,
        "unadjudicated": failing,
        "rendering_per_launcher": dict((k, dict(v)) for k, v in
                                       sorted(message_counts.items(), key=str)),
        "rendering_disagreements": rendering,
        "rendering_unadjudicated": rendering_failing,
        "unread_launchers": unread,
        "system_messages": system,
    }
    failed = bool(failing or rendering_failing or unread or system)
    if as_json:
        print(json.dumps(result, indent=1, default=str))
        return 1 if failed else 0
    print("%d store(s) in %s" % (len(names), store_dir))
    for launcher, c in result["per_launcher"].items():
        print("%-16s %s" % (launcher, ", ".join("%s %d" % kv for kv in sorted(c.items()))))
    print("not reproduced: %d" % len(mismatches))
    for m in mismatches:
        print("  %s %s %s #%s %s recorded %s re-read %s" % m[:7])
    print("synthesised and inconsistent with their own bytes: %d" % len(inconsistent))
    for m in inconsistent:
        print("  %s %s %s #%s %s recorded %s" % m[:6])
    print("adjudicated by name: %d; unadjudicated: %d"
          % (len(mismatches) + len(inconsistent) - len(failing), len(failing)))
    for m in failing:
        print("  UNADJUDICATED %s %s %s #%s %s recorded %s" % m[:6])
    print("rendering (A6): every agent message against its cited event's bytes")
    for launcher, c in result["rendering_per_launcher"].items():
        print("%-16s %s" % (launcher, ", ".join("%s %d" % kv for kv in sorted(c.items()))))
    print("rendering disagreements: %d; adjudicated by name: %d; unadjudicated: %d"
          % (len(rendering), len(rendering) - len(rendering_failing), len(rendering_failing)))
    for r in rendering_failing:
        print("  UNADJUDICATED %s %s %s message #%s event #%s %s: recorded %r re-read %r" % r)
    print("launchers no classifier reads: adjudicated by name %s; unadjudicated sessions: %d"
          % (", ".join(sorted(ADJUDICATED_LAUNCHERS)), len(unread)))
    for u in unread:
        print("  UNREAD LAUNCHER %s %r session %s carries %s" % u)
    print("system messages (decision 0006): disagreements: %d" % len(system))
    for m in system:
        print("  SYSTEM %s %s message #%s %s: %r" % m)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

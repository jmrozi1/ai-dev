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
    a session recorded against a launcher id no classifier here belongs to --
    test probes written against the interface alone.

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
exact.

**Exit status** is 0 only when every re-classified record reproduced, every
synthesised record is consistent with its own bytes, and every agent message
renders its cited event's text exactly, apart from the records `ADJUDICATED`
and `ADJUDICATED_MESSAGES` name; any other disagreement exits 1. `run_tests.py`
runs this over every kept store as its own phase, so the re-read is a gate, not
a one-time measurement.
"""

import base64
import json
import os
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(os.path.dirname(HERE), "src")]

import support  # noqa: E402
import internal_bridge  # noqa: E402
from dory_wrangler.launchers import dev_transport  # noqa: E402

DEV_TRANSPORT_LAUNCHERS = ("dev-local", "scripted-stub")
CODEX_LAUNCHERS = ("internal-bridge",)

# Records that disagree with their bytes on purpose, each by store file,
# sequence and recorded reading, with the reason. Nothing else may disagree.
ADJUDICATED = {
    ("page-preserved-past-a-clock-refusal.json", 3, ("unrecognized", None)):
        "test_convergence.PagedLauncher, a probe that keeps the scripted-stub id, "
        "hands the harness a launcher-sourced {\"type\": \"exit_report\"} stated "
        "unrecognized; no launcher synthesises it, so there is no reading to match",
}


# Agent messages that disagree with their cited event's bytes on purpose, each by
# store file, message sequence and recorded text, with the reason. Empty: no
# store the suite keeps has one, and a new one must be named here to pass.
ADJUDICATED_MESSAGES = {}

# The reading a cited event's bytes must re-read as for its text to be chat.
CHAT_TEXT_READING = ("recognized", "assistant_text")


def adjudicated(row):
    """`row` is a printed disagreement: (store, launcher, session, sequence, source, recorded, ...)."""
    return (row[0], row[3], tuple(row[5])) in ADJUDICATED


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


def message_adjudicated(row):
    return (row[0], row[3], row[6]) in ADJUDICATED_MESSAGES


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
    for name in names:
        with open(os.path.join(store_dir, name)) as handle:
            records = json.load(handle)["records"]
        sessions = dict((r["session_id"], r) for r in records
                        if r.get("record_type") == "agent_session")
        rendering.extend(rendering_rows(
            name, records, sessions,
            dict((r["event_id"], r) for r in records
                 if r.get("record_type") == "diagnostic_event"),
            message_counts))
        for event in (r for r in records if r.get("record_type") == "diagnostic_event"):
            session = sessions[event["session_id"]]
            launcher = session.get("launcher_id")
            row, expected = row_for(event, session)
            recorded = (event["interpretation"], event["interpreted_type"])
            counts[launcher]["events"] += 1
            counts[launcher][row] += 1
            readings[launcher][recorded] += 1
            if row == "reclassified":
                expected = declined(tuple(expected), session)
                if tuple(expected) == recorded:
                    counts[launcher]["reproduced"] += 1
                else:
                    counts[launcher]["not reproduced"] += 1
                    mismatches.append((name, launcher, event["session_id"], event["sequence"],
                                       event["source"], recorded, tuple(expected)))
            elif row == "synthesised":
                if not synthesised_consistent(event, session):
                    inconsistent.append((name, launcher, event["session_id"],
                                         event["sequence"], event["source"], recorded))
    failing = [m for m in mismatches + inconsistent if not adjudicated(m)]
    rendering_failing = [r for r in rendering if not message_adjudicated(r)]
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
    }
    failed = bool(failing or rendering_failing)
    if as_json:
        print(json.dumps(result, indent=1, default=str))
        return 1 if failed else 0
    print("%d store(s) in %s" % (len(names), store_dir))
    for launcher, c in result["per_launcher"].items():
        print("%-16s %s" % (launcher, ", ".join("%s %d" % kv for kv in sorted(c.items()))))
    print("not reproduced: %d" % len(mismatches))
    for m in mismatches:
        print("  %s %s %s #%s %s recorded %s re-read %s" % m)
    print("synthesised and inconsistent with their own bytes: %d" % len(inconsistent))
    for m in inconsistent:
        print("  %s %s %s #%s %s recorded %s" % m)
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
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

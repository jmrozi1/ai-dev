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
sequence. Probes that subclass `ScriptedStubLauncher` keep its launcher id and
hand the harness readings no classifier would produce, on purpose; they show up
here as non-reproducing and are adjudicated by name, not hidden.

Exit status 0 when every row was computed; the verdict is the printed counts.
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
    for name in names:
        with open(os.path.join(store_dir, name)) as handle:
            records = json.load(handle)["records"]
        sessions = dict((r["session_id"], r) for r in records
                        if r.get("record_type") == "agent_session")
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
    result = {
        "stores": len(names),
        "per_launcher": dict((k, dict(v)) for k, v in sorted(counts.items(), key=str)),
        "readings": dict((k, dict(("%s/%s" % r, n) for r, n in v.items()))
                         for k, v in sorted(readings.items(), key=str)),
        "not_reproduced": mismatches,
        "synthesised_inconsistent": inconsistent,
    }
    if as_json:
        print(json.dumps(result, indent=1, default=str))
        return 0
    print("%d store(s) in %s" % (len(names), store_dir))
    for launcher, c in result["per_launcher"].items():
        print("%-16s %s" % (launcher, ", ".join("%s %d" % kv for kv in sorted(c.items()))))
    print("not reproduced: %d" % len(mismatches))
    for m in mismatches:
        print("  %s %s %s #%s %s recorded %s re-read %s" % m)
    print("synthesised and inconsistent with their own bytes: %d" % len(inconsistent))
    for m in inconsistent:
        print("  %s %s %s #%s %s recorded %s" % m)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

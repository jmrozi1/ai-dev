"""Shared test support: paths, the contract validator, and store checking.

Every store any test in this suite produces is checked against
`dory-wrangler/validator/validate_contract.py` -- the executable form of the
contract -- rather than against a local opinion of what the contract says.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.dirname(HERE)
DORY = os.path.dirname(HARNESS)
REPO = os.path.dirname(DORY)
VALIDATOR_PATH = os.path.join(DORY, "validator", "validate_contract.py")
# Produced stores are written outside the repository on purpose: they are test
# output, not source, and #87 adds nothing outside `dory-wrangler/`.
FIXTURE_OUT = os.environ.get(
    "DORY_TEST_STORE_DIR",
    os.path.join(tempfile.gettempdir(), "dory-wrangler-issue-87-stores"))
SCRATCH = os.path.join(tempfile.gettempdir(), "dory-wrangler-issue-87-scratch")
# Stores #87 produced that are accurate records of what happened and that the
# contract validator currently rejects under the known-defective
# TURN_INSTRUCTION_MISSING rule. Kept apart so the #85 fix can be validated
# against real cases from this work rather than invented ones. They declare
# `expect: accept` because that is #87's position on them.
DIVERGENCE_OUT = os.environ.get(
    "DORY_DIVERGENCE_STORE_DIR",
    os.path.join(tempfile.gettempdir(), "dory-wrangler-issue-87-divergent-stores"))

if HARNESS not in sys.path:
    sys.path.insert(0, HARNESS)


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


class StoreCheck(object):
    """Mixin: assert a store validates, and keep it for the CLI validator run."""

    def assert_store_valid(self, store, name, description=None):
        found = violations(store.snapshot())
        if found:
            self.fail(
                "store %r was expected to satisfy the contract but the validator "
                "reported:\n%s" % (name, "\n".join("  %s: %s" % v for v in found)))
        self.keep(store, name, description)

    def assert_store_rejected_for(self, store, code, name=None):
        found = codes(store.snapshot())
        if code not in found:
            self.fail("expected the validator to emit %s; it emitted %s"
                      % (code, ", ".join(found) or "nothing"))
        return found

    def keep_divergent(self, store, name, description):
        """An accurate store the known-defective turn-floor rule rejects."""
        if not os.path.isdir(DIVERGENCE_OUT):
            os.makedirs(DIVERGENCE_OUT)
        store.write_snapshot_fixture(
            os.path.join(DIVERGENCE_OUT, "%s.json" % name), name,
            expect="accept", description=description)

    def keep(self, store, name, description=None):
        """Write the store out in fixture form so the CLI validator can be run
        over every store this suite produced, as independent evidence."""
        if not os.path.isdir(FIXTURE_OUT):
            os.makedirs(FIXTURE_OUT)
        store.write_snapshot_fixture(
            os.path.join(FIXTURE_OUT, "%s.json" % name), name,
            expect="accept", description=description)


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
    from errors import NotPermitted
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


def deterministic(config, salt, store_path=None, start=None, **kwargs):
    """A harness on a deterministic clock and id source, so a produced store is
    byte-comparable run to run.

    `start` moves the clock forward, which a harness reopened over an existing
    store needs: a real restart's clock has advanced, and a fixture clock that
    silently rewound would manufacture a TIME_REGRESSION the product never has.
    """
    from app import open_harness
    from identity import FixedClock, SequentialIdFactory
    clock = FixedClock(start) if start else FixedClock()
    return open_harness(config, store_path=store_path,
                        ids=SequentialIdFactory(salt), clock=clock, **kwargs)


RESTARTED = "2026-09-12T12:00:00.000000Z"

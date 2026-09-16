"""Intake: everything received is preserved raw and correlated, before interpretation.

Contract 7 P1 and P3, and contract 6.1. This is the `intake-and-persist-raw-events`
checkpoint's own evidence. Each test here is one of the event drops the
convergence review classified as intake scope, pinned as a property rather than
as a description:

* a page whose payloads are refused mid-way still preserves the payloads before
  and after the refusal (the sequence-gap cause, and the page-level end-of-stream
  cause);
* a read that comes back unusable leaves a record of the read;
* a launch that issues no handle preserves what it produced anyway;
* after a real multi-turn run, the durable records **alone** answer what event
  types occurred -- which is a property of what intake preserved, derived here by
  reading the store and nothing else. Classifying event types is a later
  checkpoint and nothing here does it.

The one shape intake left open -- where P1 and 6.1's `STREAM_END_UNSUPPORTED`
appeared to disagree -- was answered by the human on 2026-09-15: preserve the
bytes, decline the reading. Both values of
`session_manager.PRESERVE_UNATTRIBUTABLE_STREAM_END` are still tested below, so
the older accepted behaviour stays pinned alongside the shipped one, and the
test that asserts the shipped value says which it is.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

import support
from support import StoreCheck

from dory_wrangler import launch_boundary as lb
from dory_wrangler import session_manager as sm
from dory_wrangler.launch_boundary import LaunchBoundaryError
from dory_wrangler.store import ChatStore
from dory_wrangler.session_manager import SessionManager
from internal_bridge import InternalBridgeLauncher


def agent_text(sequence, text):
    """One `item.completed` / `agent_message` line, in the proven JSONL shape."""
    raw = json.dumps({"type": "item.completed",
                      "item": {"type": "agent_message", "text": text}}).encode("utf-8")
    return lb.EventPayload(sequence, lb.SOURCE_AGENT, lb.INTERPRETATION_RECOGNIZED,
                           raw, interpreted_type=lb.PAYLOAD_ASSISTANT_TEXT, text=text)


def unparseable(sequence, raw):
    """A payload the product could not parse at all, as the launcher reports it."""
    return lb.EventPayload(sequence, lb.SOURCE_AGENT, lb.INTERPRETATION_MALFORMED, raw)


def stream_end(sequence, source=lb.SOURCE_LAUNCHER):
    return lb.EventPayload(sequence, source, lb.INTERPRETATION_RECOGNIZED,
                           b'{"type":"stream_end"}',
                           interpreted_type=lb.PAYLOAD_STREAM_END)


class PageLauncher(lb.LaunchBoundary):
    """A launcher whose every answer this test dictates.

    It is the smallest thing that can put a chosen page in front of the drain or
    in front of re-attachment. It invents no event type: every payload a test
    hands it is built above from the shapes the internal transport is proven to
    emit, or is explicitly unparseable bytes.
    """

    launcher_id = "intake-probe"

    def __init__(self, pages=(), shape=lb.RESPONSE_SHAPE_ONE_SHOT,
                 continuation=lb.CONTINUATION_PERSISTENT, launch_error=None):
        self._capabilities = lb.LauncherCapabilities(continuation, shape, None)
        self.pages = list(pages)
        self.launch_error = launch_error

    @property
    def capabilities(self):
        return self._capabilities

    def launch(self, instruction):
        if self.launch_error is not None:
            raise self.launch_error
        return lb.LaunchResult(lb.OUTCOME_ACCEPTED, agent_handle="thread-1")

    def events(self, agent_handle, after_sequence):
        if not self.pages:
            return lb.EventsPage([])
        page = self.pages.pop(0)
        return page() if callable(page) else page

    def stop(self, agent_handle, reason):
        return lb.StopAck(False, detail="this model has no stop operation")

    def deliver(self, agent_handle, instruction):
        return lb.DeliveryAck(True)


class IntakeCase(unittest.TestCase, StoreCheck):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="dory-intake-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def harness(self, launcher, root=None):
        return SessionManager(ChatStore(root or self.root), launcher)

    def preserved(self, store, chat_id):
        """Every preserved event of every session of a chat, read back from disk."""
        out = []
        for session, _binding in store.list_sessions(chat_id):
            out.extend(store.read_all_events_of_session(chat_id, session["session_id"]))
        return out

    def bodies(self, store, chat_id):
        return [event["raw"]["body"] for event in self.preserved(store, chat_id)]

    def sequences(self, store, chat_id):
        return [event["sequence"] for event in self.preserved(store, chat_id)]

    def observation_kinds(self, store, chat_id):
        return [o["kind"] for o in store.read_session_observations(chat_id)]


class APageIsPreservedWhateverRefusesPartOfIt(IntakeCase):
    """Contract 7 P1. A refusal met at one payload does not cost the others.

    The R5 repair did this for the refusals the *store* raises. These are the two
    the *seam* raises, which review classified as intake scope and which still
    dropped whole pages at `fc3baeb`.
    """

    def test_a_payload_behind_a_sequence_gap_is_still_preserved(self):
        # Page [1, 2, 5, 3]: 5 leaves a gap and cannot be preserved at all --
        # contiguity is contract P3 and the store refuses to write the hole. 3 is
        # contiguous and honest, and at `fc3baeb` it was lost because the gap
        # raised straight out of the page loop.
        launcher = PageLauncher(pages=[lb.EventsPage(
            [agent_text(1, "one"), agent_text(2, "two"),
             agent_text(5, "five"), agent_text(3, "three")])])
        harness = self.harness(launcher)
        chat_id = harness.create_chat("A gap mid-page")

        with self.assertRaises(LaunchBoundaryError) as raised:
            harness.send_turn(chat_id, "hello")
        self.assertIn("sequence 5", str(raised.exception),
                      "the gap is still refused, and still names the sequence")
        self.assertEqual(self.sequences(harness.store, chat_id), [1, 2, 3],
                         "every payload the store could accept was preserved; only "
                         "the one that would have written a gap was not")

    def test_the_whole_page_survives_an_end_of_stream_a_one_shot_launcher_cannot_have(self):
        # `EventsPage.stream_ended` from a launcher declaring `one_shot`. The
        # claim is refused exactly as before; the payloads on the page are
        # ordinary and say nothing about a stream, and at `fc3baeb` all of them
        # were discarded before anything was read.
        launcher = PageLauncher(pages=[lb.EventsPage(
            [agent_text(1, "one"), agent_text(2, "two")], stream_ended=True)])
        harness = self.harness(launcher)
        chat_id = harness.create_chat("Page-level end of stream")

        with self.assertRaises(LaunchBoundaryError) as raised:
            harness.send_turn(chat_id, "hello")
        self.assertIn("signalled that a stream ended", str(raised.exception))
        self.assertEqual(self.sequences(harness.store, chat_id), [1, 2],
                         "the launcher's claim about its stream is refused; the "
                         "payloads it had already produced are not")

    def test_a_refusal_met_inside_that_page_is_the_one_reported(self):
        # Both refusals are real on this page: a payload the store cannot take,
        # and a launcher claiming a stream ended that it has none of. The one met
        # while reading the page is the earlier fact and is the one a launcher
        # author needs to see; found by mutating the re-raise away, which left
        # the page's own refusal swallowed and only the page-level one reported.
        launcher = PageLauncher(pages=[lb.EventsPage(
            [agent_text(1, "one"), agent_text(5, "gapped")], stream_ended=True)])
        harness = self.harness(launcher)
        chat_id = harness.create_chat("Two refusals on one page")

        with self.assertRaises(LaunchBoundaryError) as raised:
            harness.send_turn(chat_id, "hello")
        self.assertIn("sequence 5", str(raised.exception),
                      "the refusal met while reading the page is reported, not the "
                      "page-level one that was met after it")
        self.assertEqual(self.sequences(harness.store, chat_id), [1],
                         "and what could be preserved still was")

    def test_a_payload_that_could_not_be_parsed_at_all_is_preserved_verbatim(self):
        # Contract 7 P1: preservation "is *especially* required when structured
        # parsing fails, because those are the cases v0.1 exists to discover".
        # The bytes are not valid UTF-8, so the store keeps them base64 -- still
        # exactly as received, which the round-trip below is the proof of.
        raw = b"\xff\xfe not json, not even text"
        launcher = PageLauncher(pages=[lb.EventsPage(
            [agent_text(1, "before"), unparseable(2, raw), agent_text(3, "after")])])
        harness = self.harness(launcher)
        chat_id = harness.create_chat("Unparseable bytes")
        harness.send_turn(chat_id, "hello")

        events = self.preserved(harness.store, chat_id)
        self.assertEqual([e["sequence"] for e in events], [1, 2, 3])
        kept = events[1]
        self.assertEqual(kept["interpretation"], "malformed")
        self.assertIsNone(kept["interpreted_type"],
                          "a malformed payload names no type (contract 7 P2)")
        self.assertEqual(kept["raw"]["encoding"], "base64")
        import base64 as _b64
        self.assertEqual(_b64.b64decode(kept["raw"]["body"]), raw,
                         "the bytes come back exactly as the launcher produced them")
        messages = harness.store.read_messages(chat_id)
        self.assertEqual([m["author"] for m in messages], ["user", "agent", "agent"],
                         "the two honest payloads became chat, and nothing else did")
        self.assertNotIn(kept["event_id"], [m.get("source_event_id") for m in messages],
                         "an unparseable payload is preserved and never rendered "
                         "as chat (contract 7 P4, MALFORMED_EVENT_RENDERED)")
        self.assert_store_valid(harness.store, "intake-unparseable-payload-preserved",
                                "A payload the product cannot parse, preserved "
                                "verbatim between two it can.")


class AReadThatComesBackUnusableLeavesARecord(IntakeCase):
    """Review drop 4. A non-page carries no bytes; the read still happened.

    4.7's `stream_read_failed` is "reading the event stream failed on *our* side",
    which is exactly this, and is deliberately not an end of stream: it says
    nothing about whether the agent is alive, and carries the session nowhere.
    """

    class NotAPage(PageLauncher):
        def events(self, agent_handle, after_sequence):
            return {"payloads": []}

    def test_the_drain_records_that_a_read_returned_a_non_page(self):
        harness = self.harness(self.NotAPage())
        chat_id = harness.create_chat("A non-page from the drain")
        with self.assertRaises(LaunchBoundaryError):
            harness.send_turn(chat_id, "hello")
        self.assertEqual(self.observation_kinds(harness.store, chat_id),
                         ["stream_read_failed"])
        session = support.view(harness).sessions_of(chat_id)[0]
        self.assertEqual(session["state"], "running",
                         "a failed read is not evidence about the agent")

    def test_reattachment_records_that_a_read_returned_a_non_page(self):
        launcher = PageLauncher(pages=[lb.EventsPage([agent_text(1, "one")])])
        harness = self.harness(launcher)
        chat_id = harness.create_chat("A non-page at re-attachment")
        harness.send_turn(chat_id, "hello")

        launcher.events = lambda handle, after: "not a page"
        restarted = self.harness(launcher)
        restarted.reattach_on_start()

        self.assertEqual(self.observation_kinds(restarted.store, chat_id),
                         ["reattached", "stream_read_failed"],
                         "the page was dropped in silence at `fc3baeb`")
        self.assertEqual(self.sequences(restarted.store, chat_id), [1],
                         "nothing preserved is lost, and a non-page adds nothing")


class ReattachmentPreservesWhatItReads(IntakeCase):
    """Review drop 1. A re-attachment page is the drain's page and is taken the same way."""

    def test_a_refused_payload_at_reattachment_does_not_cost_the_honest_one(self):
        launcher = PageLauncher(pages=[lb.EventsPage([agent_text(1, "one")])])
        harness = self.harness(launcher)
        chat_id = harness.create_chat("A gapped re-attachment page")
        harness.send_turn(chat_id, "hello")

        launcher.pages = [lb.EventsPage([agent_text(9, "gapped"), agent_text(2, "honest")])]
        restarted = self.harness(launcher)
        restarted.reattach_on_start()

        events = self.preserved(restarted.store, chat_id)
        self.assertEqual([e["sequence"] for e in events], [1, 2],
                         "at `fc3baeb` the honest payload at 2 was lost behind the gap")
        self.assertIn("honest", events[1]["raw"]["body"])


class ALaunchThatIssuedNoHandlePreservesWhatItProduced(IntakeCase):
    """The intake gap the JSONL model exposed (contract 7 P1; 6.1 *Known residual*).

    `events` addresses an agent through the handle and nothing else, so a launch
    that started no thread has no later channel: what it printed crosses the seam
    with the failure or not at all. Its correlation is the chat and the session
    that failed to open, which is what contract 6.1 says such records are.
    """

    def setUp(self):
        IntakeCase.setUp(self)
        base = tempfile.mkdtemp(prefix="dory-intake-bridge-")
        self.addCleanup(shutil.rmtree, base, True)
        self.codex_home = os.path.join(base, "codex-home")
        self.spool = os.path.join(base, "spool")
        os.makedirs(self.codex_home)

    def test_the_model_s_output_and_exit_status_are_preserved_on_the_failed_session(self):
        launcher = InternalBridgeLauncher(
            self.codex_home, self.spool,
            behaviour=("no-thread-started", "stderr-noise"))
        harness = self.harness(launcher)
        chat_id = harness.create_chat("A launch that started no thread")
        harness.send_turn(chat_id, "hello")

        sessions = support.view(harness).sessions_of(chat_id)
        self.assertEqual([s["state"] for s in sessions], ["launch_failed"])
        self.assertIsNone(sessions[0].get("agent_handle"))

        events = self.preserved(harness.store, chat_id)
        self.assertTrue(events, "at `fc3baeb` this preserved nothing at all")
        self.assertEqual([e["sequence"] for e in events], list(range(1, len(events) + 1)),
                         "contiguous from 1 within the session (contract P3)")
        self.assertEqual(set(e["source"] for e in events), {"launcher"},
                         "nothing proved an agent existed, so nothing is sourced to "
                         "one (contract 7 P2a)")
        self.assertEqual(set(e["session_id"] for e in events), {sessions[0]["session_id"]},
                         "correlated to the session that failed to open")
        self.assertEqual(set(e["chat_id"] for e in events), {chat_id})

        observation = json.loads(events[-1]["raw"]["body"])
        self.assertIn("model_launcher_observation", observation,
                      "the launcher's own line names itself as one in its own bytes")
        self.assertIn("exit_status", observation)
        self.assertIn("a prerequisite check wrote this to stderr",
                      observation.get("stderr_tail", ""),
                      "stderr is preserved with its content, not as an empty key; "
                      "on this path nothing else can carry it")
        self.assertIsNone(events[-1]["interpreted_type"],
                          "no event type is invented for it")

        results = support.view(harness).all_of("launch_result")
        self.assertEqual([r["outcome"] for r in results], ["failed"])
        self.assertEqual([r["failure_category"] for r in results], ["no_acknowledgement"],
                         "the launcher's own category survives; #90 counts them")
        self.assert_store_valid(harness.store, "intake-launch-with-no-handle-preserved",
                                "A launch that started no thread, with the output it "
                                "produced preserved against the session that failed "
                                "to open.")

    def test_a_launcher_that_returns_a_failure_rather_than_raising_keeps_its_output(self):
        # `launch` may *return* a failed or unknown `LaunchResult` instead of
        # raising, and the harness re-states every result through the
        # constructor rather than trusting the launcher's object. Found by
        # mutating that re-statement: it dropped `payloads` and nothing failed,
        # because every other test here reaches this through `LauncherError`.
        line = lb.EventPayload(1, lb.SOURCE_LAUNCHER, lb.INTERPRETATION_MALFORMED,
                               b"prerequisite check failed")

        class ReturnsAFailure(PageLauncher):
            def launch(self, instruction):
                return lb.LaunchResult(lb.OUTCOME_FAILED,
                                       failure_category=lb.FAILURE_UNAVAILABLE,
                                       detail="no active user session",
                                       payloads=[line])

        harness = self.harness(ReturnsAFailure())
        chat_id = harness.create_chat("A returned failure")
        harness.send_turn(chat_id, "hello")

        events = self.preserved(harness.store, chat_id)
        self.assertEqual([(e["sequence"], e["source"], e["interpretation"]) for e in events],
                         [(1, "launcher", "malformed")])
        self.assertEqual(events[0]["raw"]["body"], "prerequisite check failed",
                         "preserved verbatim whether the launcher raised or returned")

    def test_an_unknown_outcome_also_preserves_what_the_launch_produced(self):
        # `unknown` carries no handle either, so it has the same problem and the
        # same channel. Contract fixture `valid/09-launch-outcome-unknown` is
        # exactly this shape.
        line = lb.EventPayload(1, lb.SOURCE_LAUNCHER, lb.INTERPRETATION_UNRECOGNIZED,
                               b'{"note":"the call returned nothing usable"}')

        class OutcomeUnknown(PageLauncher):
            def launch(self, instruction):
                return lb.LaunchResult(lb.OUTCOME_UNKNOWN, detail="no acknowledgement",
                                       payloads=[line])

        harness = self.harness(OutcomeUnknown())
        chat_id = harness.create_chat("An unknown outcome")
        harness.send_turn(chat_id, "hello")

        sessions = support.view(harness).sessions_of(chat_id)
        self.assertEqual([s["state"] for s in sessions], ["unknown"])
        self.assertEqual([e["sequence"] for e in self.preserved(harness.store, chat_id)], [1])
        harness.abandon(chat_id)
        self.assert_store_valid(harness.store, "intake-unknown-launch-output-preserved",
                                "A launch whose outcome is `unknown`, with what it "
                                "produced preserved on the session, then abandoned.")

    def test_a_launch_that_printed_nothing_at_all_still_leaves_evidence(self):
        # The other branch of the model's failure: the script exits non-zero and
        # prints nothing, so there is no transport output to preserve. The
        # launcher's own observation of the process it ran is the whole record,
        # and without it this failure leaves no `diagnostic_event` at all --
        # found by mutating that branch's payloads away.
        launcher = InternalBridgeLauncher(
            self.codex_home, self.spool, behaviour=("no-output", "stderr-noise"))
        harness = self.harness(launcher)
        chat_id = harness.create_chat("A launch that printed nothing")
        harness.send_turn(chat_id, "hello")

        events = self.preserved(harness.store, chat_id)
        self.assertEqual([(e["sequence"], e["source"], e["interpretation"])
                          for e in events],
                         [(1, "launcher", "unrecognized")])
        observation = json.loads(events[0]["raw"]["body"])
        self.assertEqual(observation["exit_status"], 1)
        self.assertIn("a prerequisite check wrote this to stderr",
                      observation["stderr_tail"])
        results = support.view(harness).all_of("launch_result")
        self.assertEqual([r["failure_category"] for r in results], ["unavailable"])

    def test_the_seam_refuses_output_that_could_not_honestly_be_preserved(self):
        # Agent-sourced output on a launch that never ran is refused by contract
        # 7 P2a, and the seam says so where the launcher can see it rather than
        # letting the store refuse it mid-launch. The failure keeps its category.
        payload = agent_text(1, "an agent nobody launched")
        error = lb.LauncherError(lb.FAILURE_UNAVAILABLE, "prerequisite missing",
                                 payloads=[payload])
        harness = self.harness(PageLauncher(launch_error=error))
        chat_id = harness.create_chat("Mis-sourced launch output")
        harness.send_turn(chat_id, "hello")

        self.assertEqual(self.preserved(harness.store, chat_id), [])
        result = support.view(harness).all_of("launch_result")[0]
        self.assertEqual(result["failure_category"], "unavailable",
                         "a packing mistake does not downgrade an honest category")
        self.assertIn("could not cross the seam", result["detail"],
                      "and the loss is written down where it happened")

    def test_output_that_is_not_an_event_payload_is_refused_at_the_seam(self):
        # Without this check the next one reads `.source` off whatever the
        # launcher handed over and raises `AttributeError`, which is not a
        # `LaunchBoundaryError`, so it escapes the fallback and the launcher's
        # honest `unavailable` becomes an unclassified `internal_error` that #90
        # cannot count. Found by mutating the isinstance check away.
        error = lb.LauncherError(lb.FAILURE_UNAVAILABLE, "prerequisite missing",
                                 payloads=[{"sequence": 1, "raw": b"not a payload"}])
        harness = self.harness(PageLauncher(launch_error=error))
        chat_id = harness.create_chat("Output that is not a payload")
        harness.send_turn(chat_id, "hello")

        result = support.view(harness).all_of("launch_result")[0]
        self.assertEqual(result["failure_category"], "unavailable",
                         "the launcher's own category survives whatever it packed "
                         "its output in")
        self.assertIn("could not cross the seam", result["detail"])
        self.assertEqual(self.preserved(harness.store, chat_id), [])

    def test_an_accepted_launch_may_not_carry_output_beside_its_events_channel(self):
        with self.assertRaises(LaunchBoundaryError) as raised:
            lb.LaunchResult(lb.OUTCOME_ACCEPTED, agent_handle="thread-1",
                            payloads=[stream_end(1)])
        self.assertIn("through `events`", str(raised.exception))

    def test_launch_output_must_be_contiguous_from_one(self):
        launcher_line = lb.EventPayload(2, lb.SOURCE_LAUNCHER,
                                        lb.INTERPRETATION_UNRECOGNIZED, b"{}")
        with self.assertRaises(LaunchBoundaryError) as raised:
            lb.LaunchResult(lb.OUTCOME_FAILED, failure_category=lb.FAILURE_UNAVAILABLE,
                            payloads=[launcher_line])
        self.assertIn("contiguous from 1", str(raised.exception))


class TheOneShotEndOfStreamPayloadIsWhereP1AndSixOneDisagree(IntakeCase):
    """The escalated question, answered, and still pinned at both answers.

    Contract 7 P1 requires the bytes. Contract 6.1 rejects
    `interpreted_type: "stream_end"` on a session whose launcher declares
    `one_shot`, outright, "whichever evidence channel cites it or whether
    anything cites it at all". **The human answered it on 2026-09-15: preserve
    the bytes and decline the reading**, which contract 7 P2 now covers -- a type
    this build knows "but cannot attribute on this session" is `unrecognized`
    with `interpreted_type: null`. `PRESERVE_UNATTRIBUTABLE_STREAM_END` is
    `True` accordingly.

    The older accepted behaviour stays pinned below so what was given up is
    readable, and so the assertion refusal -- which did **not** change -- is held
    under both answers. A change that preserved the `stream_end` *assertion* as
    well as its bytes would break every test in this class.
    """

    def page(self):
        return lb.EventsPage([agent_text(1, "before"), stream_end(2),
                              agent_text(3, "after")])

    def run_it(self, preserve):
        original = sm.PRESERVE_UNATTRIBUTABLE_STREAM_END
        sm.PRESERVE_UNATTRIBUTABLE_STREAM_END = preserve
        self.addCleanup(setattr, sm, "PRESERVE_UNATTRIBUTABLE_STREAM_END", original)
        harness = self.harness(PageLauncher(pages=[self.page()]))
        chat_id = harness.create_chat("One-shot end of stream")
        with self.assertRaises(LaunchBoundaryError) as raised:
            harness.send_turn(chat_id, "hello")
        self.assertIn("no stream to end", str(raised.exception),
                      "the claim is refused under either answer")
        return harness, chat_id

    def test_the_shipped_answer_is_the_one_the_orchestrator_has_not_changed(self):
        # The two tests below each set the constant themselves, so neither pins
        # the value this build actually ships with -- found by mutating it. This
        # one touches nothing and asserts the behaviour, not the name.
        #
        # **The expectation changed from `[1]` to `[1, 2, 3]` deliberately.** The
        # human answered the one-shot `stream_end` question on 2026-09-15 with
        # option A -- preserve the bytes, decline the reading -- and the enabling
        # widening of contract 7 P2 was carried onto this branch from
        # `dory-wrangler/issue-85`. This is what that flip looks like, recorded
        # exactly as decision 0003's flip was. Nothing was weakened: the refusal
        # of the *assertion* is still asserted here and in every test below.
        harness = self.harness(PageLauncher(pages=[self.page()]))
        chat_id = harness.create_chat("The shipped answer")
        with self.assertRaises(LaunchBoundaryError) as raised:
            harness.send_turn(chat_id, "hello")
        self.assertIn("no stream to end", str(raised.exception),
                      "the assertion is still refused; only the bytes are kept")
        self.assertEqual(
            self.sequences(harness.store, chat_id), [1, 2, 3],
            "this build preserves the bytes and declines only the reading, which "
            "is the human's decision of 2026-09-15 and the whole reason the "
            "payloads behind it are no longer lost")
        kept = self.preserved(harness.store, chat_id)[1]
        self.assertEqual(kept["interpretation"], "unrecognized")
        self.assertIsNone(kept["interpreted_type"],
                          "preserving the bytes must never become recording the "
                          "assertion; `interpreted_type` stays null so 6.1's "
                          "STREAM_END_UNSUPPORTED is unreachable, not suppressed")

    def test_the_preserved_payload_leaves_no_gap_behind_it_on_that_session(self):
        # The point of the decision, and the thing the older behaviour really
        # cost. Refusing before preserving left sequence 2 unwritten, so the
        # store refused 3, and 4, and everything after it for the life of the
        # session -- the loss was never one payload. A fourth payload offered on
        # the *same* session after the page is the proof that the history is
        # contiguous again.
        launcher = PageLauncher(pages=[
            self.page(),
            lb.EventsPage([agent_text(4, "later, on the same session")])])
        harness = self.harness(launcher)
        chat_id = harness.create_chat("No gap left behind")
        with self.assertRaises(LaunchBoundaryError):
            harness.send_turn(chat_id, "hello")
        self.assertEqual(self.sequences(harness.store, chat_id), [1, 2, 3])

        harness.send_turn(chat_id, "are you still there")
        self.assertEqual(
            self.sequences(harness.store, chat_id), [1, 2, 3, 4],
            "at b871aa1 this second turn was refused as a gap -- 'sequence 4 "
            "while 1 is stored' -- and stayed refused for the session's life")
        sessions = support.view(harness).sessions_of(chat_id)
        self.assertEqual(len(sessions), 1,
                         "and it is the same session, not a new one opened "
                         "around the damage")
        # Kept from the *shipped* configuration -- this test sets no constant --
        # so what the contract validator accepts is what this build produces.
        self.assert_store_valid(
            harness.store, "intake-one-shot-stream-end-no-gap-left",
            "A one-shot launcher's `stream_end` payload preserved as bytes with "
            "its reading declined, and a later payload on the same session "
            "preserved behind it -- the contiguous history the refusal used to "
            "destroy, accepted by the contract validator unchanged.")

    def test_refused_before_preservation_is_the_accepted_behaviour_and_costs_the_rest(self):
        # Kept under its original name: this is what #86 and #87 were accepted
        # doing, and it is no longer what ships. It stays so that the answer the
        # human did not take is still measured rather than described, and so the
        # decision remains a one-line change in either direction.
        harness, chat_id = self.run_it(False)
        self.assertEqual(self.sequences(harness.store, chat_id), [1],
                         "this is what #86 and #87 were both accepted doing")
        session = support.view(harness).sessions_of(chat_id)[0]
        self.assertEqual(session["state"], "running",
                         "and no session concludes anything from an end of stream "
                         "a one-shot launcher could not have seen")

    def test_preserving_the_bytes_keeps_the_whole_page_and_records_no_stream_end(self):
        harness, chat_id = self.run_it(True)
        events = self.preserved(harness.store, chat_id)
        self.assertEqual([e["sequence"] for e in events], [1, 2, 3],
                         "refusing before preserving opens a gap at 2, which then "
                         "makes 3 and everything after it unpreservable for the "
                         "life of the session")
        kept = events[1]
        self.assertEqual(kept["raw"]["body"], '{"type":"stream_end"}',
                         "the bytes are preserved exactly as received (P1)")
        self.assertEqual(kept["interpretation"], "unrecognized")
        self.assertIsNone(kept["interpreted_type"],
                          "the reading 6.1 forbids is not recorded, so "
                          "STREAM_END_UNSUPPORTED is never reachable")
        session = support.view(harness).sessions_of(chat_id)[0]
        self.assertEqual(session["state"], "running")
        self.assert_store_valid(
            harness.store, "intake-one-shot-stream-end-bytes-preserved",
            "A one-shot launcher's `stream_end` payload preserved as bytes with "
            "its reading declined -- the contract validator accepts it unchanged.")

    def test_only_the_launcher_may_say_its_own_stream_ended_and_the_bytes_survive_saying_so(self):
        # A *stream* launcher, so there is nothing unattributable about the type;
        # what is wrong is who said it. Preserved as sent, and still refused as a
        # reading (contract 6.1).
        launcher = PageLauncher(pages=[lb.EventsPage(
            [agent_text(1, "before"), stream_end(2, source=lb.SOURCE_AGENT)])],
            shape=lb.RESPONSE_SHAPE_STREAM)
        harness = self.harness(launcher)
        chat_id = harness.create_chat("An agent claiming the stream ended")
        with self.assertRaises(LaunchBoundaryError) as raised:
            harness.send_turn(chat_id, "hello")
        self.assertIn("must be signalled by the launcher", str(raised.exception))
        self.assertEqual(self.sequences(harness.store, chat_id), [1, 2],
                         "at `fc3baeb` the claim cost the bytes as well")
        session = support.view(harness).sessions_of(chat_id)[0]
        self.assertEqual(session["state"], "running",
                         "nothing concluded an end of stream from it")


class TheEventTypesThatOccurredAreDerivableFromTheStoreAlone(IntakeCase):
    """The checkpoint's own requirement, over a real multi-turn run.

    Nothing here classifies anything: the derivation reads `interpretation` and
    `interpreted_type` off preserved records and groups them. That those fields
    are there, on every payload, correlated to a session, is what intake
    delivers; what to *do* with an unrecognized type is a later checkpoint.

    The store is reopened from disk with a fresh `ChatStore` and no launcher at
    all, so what answers the question is the durable evidence and nothing else.
    """

    def setUp(self):
        IntakeCase.setUp(self)
        base = tempfile.mkdtemp(prefix="dory-intake-types-")
        self.addCleanup(shutil.rmtree, base, True)
        self.codex_home = os.path.join(base, "codex-home")
        self.spool = os.path.join(base, "spool")
        os.makedirs(self.codex_home)

    @staticmethod
    def derive(root):
        """What event types occurred in this store, from the store alone."""
        store = ChatStore(root)
        found = {}
        for chat_id in store.chat_ids():
            for session, _binding in store.list_sessions(chat_id):
                for event in store.read_all_events_of_session(chat_id,
                                                              session["session_id"]):
                    key = (event["interpretation"], event["interpreted_type"])
                    found[key] = found.get(key, 0) + 1
        return found

    def test_a_multi_turn_run_against_the_jsonl_model_answers_it(self):
        harness = self.harness(InternalBridgeLauncher(
            self.codex_home, self.spool,
            behaviour=("synthetic-unrecognized", "synthetic-malformed")))
        chat_id = harness.create_chat("What types occurred")
        for text in ("first", "second", "third"):
            harness.send_turn(chat_id, text)

        derived = self.derive(self.root)
        self.assertEqual(
            derived,
            self.derive(self.root),
            "the derivation is a read; running it twice changes nothing")
        recognized = set(t for (interpretation, t) in derived
                         if interpretation == "recognized")
        self.assertEqual(recognized, {"thread.started", "assistant_text"},
                         "every recognized type the transport actually emitted, "
                         "named by the evidence rather than by this test; derived "
                         "%r" % (derived,))
        self.assertIn(("unrecognized", None), derived,
                      "a well-formed line of a type this build does not know is a "
                      "finding the evidence keeps (contract 7 P2)")
        self.assertIn(("malformed", None), derived,
                      "and so is a line that could not be parsed at all")
        self.assertEqual(sum(derived.values()),
                         len(self.preserved(harness.store, chat_id)),
                         "every preserved event is accounted for, and nothing else is")
        self.assert_store_valid(harness.store, "intake-event-types-derivable",
                                "Three turns against the internal JSONL model, "
                                "emitting recognized, unrecognized and malformed "
                                "lines; the set of types that occurred is derived "
                                "from these records alone.")


class TheHarnessRestsOnNoLauncherDurableState(IntakeCase):
    """Item E / re-review N3. Contract 6.1: no correct harness behaviour may rest
    on a launcher that keeps durable state of its own.

    The JSONL model keeps a spool, which 6.1 explicitly permits a launcher to do.
    What it forbids is the harness depending on one. These two tests measure the
    difference: everything the harness owns survives the spool's loss, and what
    does not survive is the launcher's ability to address its own thread, which
    is a launcher capability and not a harness behaviour.
    """

    def setUp(self):
        IntakeCase.setUp(self)
        base = tempfile.mkdtemp(prefix="dory-intake-spool-")
        self.addCleanup(shutil.rmtree, base, True)
        self.codex_home = os.path.join(base, "codex-home")
        self.spool = os.path.join(base, "spool")
        os.makedirs(self.codex_home)

    def launcher(self):
        return InternalBridgeLauncher(self.codex_home, self.spool)

    def test_losing_the_spool_loses_nothing_the_harness_preserved(self):
        harness = self.harness(self.launcher())
        chat_id = harness.create_chat("The spool goes away")
        harness.send_turn(chat_id, "remember this")
        before = [(e["sequence"], e["interpreted_type"], e["raw"]["body"])
                  for e in self.preserved(harness.store, chat_id)]
        transcript = harness.transcript(chat_id)

        shutil.rmtree(self.spool, ignore_errors=True)
        restarted = self.harness(self.launcher())
        restarted.reattach_on_start()

        after = [(e["sequence"], e["interpreted_type"], e["raw"]["body"])
                 for e in self.preserved(restarted.store, chat_id)]
        self.assertEqual(after, before,
                         "durable records are canonical (D1); the launcher's own "
                         "state is not part of them")
        self.assertEqual(restarted.transcript(chat_id), transcript)
        self.assertIn("reattach_failed", self.observation_kinds(restarted.store, chat_id),
                      "the failed re-attachment is recorded, which is what 5.4 "
                      "requires and is not an inference from silence")

    def test_a_chat_whose_thread_is_gone_still_has_an_exit_and_a_next_turn(self):
        harness = self.harness(self.launcher())
        chat_id = harness.create_chat("A thread nobody can address")
        harness.send_turn(chat_id, "first")

        shutil.rmtree(self.spool, ignore_errors=True)
        restarted = self.harness(self.launcher())
        restarted.reattach_on_start()
        self.assertEqual(
            [s["state"] for s in support.view(restarted).sessions_of(chat_id)],
            ["unknown"])

        restarted.abandon(chat_id)
        restarted.send_turn(chat_id, "second")
        states = [s["state"] for s in support.view(restarted).sessions_of(chat_id)]
        self.assertEqual(states, ["abandoned", "running"],
                         "the chat is usable again without the launcher's state; "
                         "what is lost is the agent's own memory of the thread, "
                         "which is Codex's and was never the harness's")
        self.assert_store_valid(restarted.store, "intake-spool-gone-chat-still-usable",
                                "A chat whose launcher lost its durable spool: every "
                                "preserved event survives, the session reaches "
                                "`unknown`, and the user's one action reopens the chat.")


if __name__ == "__main__":
    unittest.main()

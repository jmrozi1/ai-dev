"""An interrupted write must not expose partially updated chat state.

The claim is not "the final state is fine". It is that *every* state a reader
could observe after a crash is a complete, contract-valid state. So these tests
kill the real write path at every point it can be killed, in a separate process
that dies without unwinding, and then inspect the store as it stands -- before
anything has had a chance to repair it.

`atomic.FAULT_POINTS` is enumerated rather than sampled, and one test asserts
that every point in it was actually exercised. A crash test that quietly stopped
covering a point would otherwise keep passing.
"""

import json
import os
import subprocess
import sys
import threading
import unittest

from helpers import StoreCase, TESTS_DIR

from dory_wrangler import atomic
from dory_wrangler.store import ChatStore

CRASH_WRITER = os.path.join(TESTS_DIR, "_crashwriter.py")

# Points reachable from a message append. `post_message_pre_chat_update` is
# specific to the message path; the rest are in the shared write primitives.
MESSAGE_FAULT_POINTS = atomic.FAULT_POINTS


def crash_write(root, fault, *args):
    """Run one store write in a child killed at `fault`. Returns the exit code."""
    env = dict(os.environ)
    if fault:
        env[atomic.FAULT_ENV] = fault
    else:
        env.pop(atomic.FAULT_ENV, None)
    completed = subprocess.run(
        [sys.executable, CRASH_WRITER, root] + list(args),
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return completed.returncode, completed.stderr.decode("utf-8", "replace")


class TestInterruptedWrites(StoreCase):
    def setUp(self):
        StoreCase.setUp(self)
        self.chat_id = self.store.create_chat("Crash target")["chat_id"]
        self.store.append_user_message(self.chat_id, "turn one")
        self.store.append_user_message(self.chat_id, "turn two")
        self.before = self.store.read_messages(self.chat_id)

    def read_fresh(self):
        """Read the store as a process that has never seen a write would."""
        return ChatStore(self.root, sweep=False)

    def test_every_fault_point_leaves_a_whole_store(self):
        exercised = []
        for point in MESSAGE_FAULT_POINTS:
            code, stderr = crash_write(
                self.root, point, "message", self.chat_id, "interrupted turn"
            )
            self.assertEqual(
                code, atomic.FAULT_EXIT_CODE,
                "fault point %r was never reached (exit %s)\n%s" % (point, code, stderr),
            )
            exercised.append(point)

            fresh = self.read_fresh()
            messages = fresh.read_messages(self.chat_id)

            # Whatever the crash left, the history is complete and contiguous,
            # and every message that is there is a whole record.
            self.assertIn(
                len(messages), (2, 3),
                "crash at %r left %d messages" % (point, len(messages)),
            )
            for message in messages:
                self.assertEqual(message["content"]["content_type"], "text/plain")
                self.assertTrue(message["content"]["text"])
            self.assertEqual(
                [m["sequence"] for m in messages], list(range(1, len(messages) + 1))
            )
            # The two turns that were durable before the crash are untouched.
            self.assertEqual(messages[:2], self.before)

            violations = fresh.verify()
            self.assertEqual(
                violations, [],
                "crash at %r left a store the contract rejects: %s" % (point, violations),
            )

            # Reset to the pre-crash history for the next point.
            if len(messages) == 3:
                os.unlink(os.path.join(
                    self.root, "chats", self.chat_id, "messages", "00000003.json"
                ))
            atomic.sweep_temp_files(self.root)

        self.assertEqual(
            sorted(exercised), sorted(atomic.FAULT_POINTS),
            "not every declared fault point was exercised",
        )

    def test_a_crash_before_publication_leaves_no_message_at_all(self):
        for point in ("mid_tmp_write", "tmp_written", "tmp_fsynced", "pre_publish"):
            code, _ = crash_write(
                self.root, point, "message", self.chat_id, "never published"
            )
            self.assertEqual(code, atomic.FAULT_EXIT_CODE)
            messages = self.read_fresh().read_messages(self.chat_id)
            self.assertEqual(
                len(messages), 2,
                "crash at %r published a message it had not finished writing" % point,
            )
            atomic.sweep_temp_files(self.root)

    def test_a_crash_after_publication_leaves_the_whole_message(self):
        code, _ = crash_write(
            self.root, "post_publish", "message", self.chat_id, "published and whole"
        )
        self.assertEqual(code, atomic.FAULT_EXIT_CODE)
        messages = self.read_fresh().read_messages(self.chat_id)
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[2]["content"]["text"], "published and whole")
        self.assertEqual(self.read_fresh().verify(), [])

    def test_a_crash_between_the_message_and_the_chat_timestamp(self):
        """The one two-record window in the write path, ordered so it cannot lie.

        The message is published before the chat's `updated_at` is advanced. A
        crash in between leaves a chat whose timestamp is behind its history --
        stale, which is contract-valid and understates activity. The opposite
        order would leave a chat claiming activity with no message behind it.
        """
        before_chat = self.read_fresh().read_chat(self.chat_id)
        code, _ = crash_write(
            self.root, "post_message_pre_chat_update", "message", self.chat_id, "orphaned bump"
        )
        self.assertEqual(code, atomic.FAULT_EXIT_CODE)
        fresh = self.read_fresh()
        messages = fresh.read_messages(self.chat_id)
        chat = fresh.read_chat(self.chat_id)
        self.assertEqual(len(messages), 3)
        self.assertEqual(chat["updated_at"], before_chat["updated_at"])
        self.assertLess(chat["updated_at"], messages[-1]["created_at"])
        self.assertEqual(fresh.verify(), [])
        # And the next successful append brings it forward again.
        fresh.append_user_message(self.chat_id, "the next turn")
        self.assertGreater(
            fresh.read_chat(self.chat_id)["updated_at"], before_chat["updated_at"]
        )

    def test_an_interrupted_chat_creation_leaves_no_half_chat(self):
        before = set(c["chat_id"] for c in self.store.list_chats())
        reached = []
        for point in atomic.FAULT_POINTS:
            code, _ = crash_write(self.root, point, "chat", "Half a chat")
            if code != atomic.FAULT_EXIT_CODE:
                continue  # this point is not on the chat-creation path
            reached.append(point)
            fresh = self.read_fresh()
            after = set(c["chat_id"] for c in fresh.list_chats())
            new = after - before
            for chat_id in new:
                # If a chat became visible, it is whole: readable record,
                # readable (empty) history, and a contract-valid store.
                self.assertEqual(fresh.read_chat(chat_id)["title"], "Half a chat")
                self.assertEqual(fresh.read_messages(chat_id), [])
            self.assertEqual(fresh.verify(), [])
            for chat_id in new:
                _remove_chat(self.root, chat_id)
            atomic.sweep_temp_files(self.root)
        # Chat creation publishes a whole directory, so it has no temp-file
        # points; naming the expected set keeps a silently-unreached point from
        # turning this into a test that asserts nothing.
        self.assertEqual(
            reached, ["pre_publish", "post_publish", "pre_parent_fsync"],
            "the chat-creation crash points actually reached changed",
        )

    def test_an_interrupted_transition_never_splits_a_session_from_its_binding(self):
        """A session and its binding change in one write, so a crash cannot split them."""
        chat_id = self.store.create_chat("Lifecycle")["chat_id"]
        user = self.store.append_user_message(chat_id, "go")
        session, _b = self.store.create_session(
            chat_id, user["message_id"], "dev-local",
            {"continuation": "fresh_binding", "response_shape": "one_shot",
             "instruction_bound_bytes": None},
        )
        sid = session["session_id"]
        reached = []
        for point in atomic.FAULT_POINTS:
            code, _ = crash_write(self.root, point, "transition", chat_id, sid)
            if code != atomic.FAULT_EXIT_CODE:
                continue
            reached.append(point)
            fresh = self.read_fresh()
            state = fresh.read_session(chat_id, sid)["state"]
            released = fresh.read_binding(chat_id, sid)["released_at"]
            # Either the transition happened and the binding is released, or
            # neither did. There is no third reading.
            self.assertEqual(
                state in ("launch_failed",), released is not None,
                "crash at %r left state %r with released_at %r" % (point, state, released),
            )
            self.assertEqual(fresh.verify(), [])
            if state == "launch_failed":
                break
            atomic.sweep_temp_files(self.root)
        self.assertEqual(
            reached,
            ["mid_tmp_write", "tmp_written", "tmp_fsynced", "pre_publish", "post_publish"],
            "the transition crash points actually reached changed",
        )


class TestConcurrentSenders(StoreCase):
    """Sequence allocation needs no lock: `os.link` decides who gets a name.

    Contract 8.2 requires a message to be appended atomically with a sequence
    unique within the chat, and contract 8 says only the binding and transition
    operations need concurrency control. Uniqueness here therefore comes from
    exclusive creation rather than from a lock, and these are the tests that hold
    it to that -- one across processes, one across threads, both requiring that
    no writer's turn is silently overwritten by another's.
    """

    def test_threads_racing_for_a_sequence_lose_nothing(self):
        chat_id = self.store.create_chat("Thread race")["chat_id"]
        writers, per_writer = 6, 15
        failures = []

        def write(worker):
            store = ChatStore(self.root, sweep=False)
            for i in range(per_writer):
                try:
                    store.append_user_message(chat_id, "w%d-%d" % (worker, i))
                except Exception as exc:  # pragma: no cover - reported, not swallowed
                    failures.append("%s: %s" % (worker, exc))

        threads = [threading.Thread(target=write, args=(w,)) for w in range(writers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(failures, [])

        messages = self.store.read_messages(chat_id)
        expected = sorted("w%d-%d" % (w, i) for w in range(writers) for i in range(per_writer))
        self.assertEqual(
            sorted(m["content"]["text"] for m in messages), expected,
            "a turn was overwritten by a concurrent writer",
        )
        self.assertEqual(
            [m["sequence"] for m in messages], list(range(1, writers * per_writer + 1))
        )
        self.assertStoreValid()

    def test_two_processes_racing_for_a_sequence_lose_nothing(self):
        chat_id = self.store.create_chat("Race")["chat_id"]
        children = [
            subprocess.Popen(
                [sys.executable, CRASH_WRITER, self.root, "message", chat_id, "racer %d" % i],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            for i in range(8)
        ]
        for child in children:
            _out, err = child.communicate()
            self.assertEqual(child.returncode, 0, err.decode())
        messages = self.store.read_messages(chat_id)
        self.assertEqual([m["sequence"] for m in messages], list(range(1, 9)))
        self.assertEqual(
            sorted(m["content"]["text"] for m in messages),
            sorted("racer %d" % i for i in range(8)),
        )
        self.assertStoreValid()

    def test_the_second_writer_of_a_name_is_refused(self):
        """The primitive itself: exclusive creation, not last-writer-wins."""
        target = os.path.join(self.root, "exclusive.json")
        atomic.create_exclusive(target, b"first")
        with self.assertRaises(FileExistsError):
            atomic.create_exclusive(target, b"second")
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), b"first")


def _remove_chat(root, chat_id):
    path = os.path.join(root, "chats", chat_id)
    for base, dirs, files in os.walk(path, topdown=False):
        for name in files:
            os.unlink(os.path.join(base, name))
        for name in dirs:
            os.rmdir(os.path.join(base, name))
    os.rmdir(path)


if __name__ == "__main__":
    unittest.main()

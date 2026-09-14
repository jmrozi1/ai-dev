"""Reopen after a full restart, with the complete history and no agent running.

This is the central requirement of #86: the agent's transient context is never
the only copy of the conversation. The proof has to rule out three ways of
appearing to hold that do not:

1. *Cached history.* A shell that answered from memory would pass a restart test
   that only ever asked the same process. Here the process is killed with
   SIGKILL and a new one is started, and one test additionally edits a message
   on disk while nothing is running and requires the edit to show up -- which it
   can only do if the answer is being read from disk.
2. *History with nothing in it worth surviving.* A chat of user turns proves
   very little, since the user's own text was never in the agent's context in
   the first place. These tests restart a chat whose history contains *agent*
   turns -- answered by the served application's own chat loop through the
   configured launcher, not written in by the test -- and check that text, its
   provenance, and the preserved event it was transcribed from.
3. *An agent quietly still running.* "No agent running" is checked against the
   operating system -- the old pid is gone, and the new process has no children
   at any point -- rather than against a flag in the store.
"""

import json
import os
import shutil
import tempfile
import unittest

from shellproc import ShellProcess, children_of, is_alive

from dory_wrangler.store import ChatStore


class RestartCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="dory-restart-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.shell = ShellProcess(self.root)
        self.addCleanup(self.shell.kill)


class TestRestartRecovery(RestartCase):
    def test_a_chat_reopens_with_its_complete_user_visible_history(self):
        self.shell.start()
        first_pid = self.shell.process.pid

        _status, chat = self.shell.post("/api/chats", {})
        chat_id = chat["chat_id"]
        _status, sent = self.shell.post(
            "/api/chats/%s/messages" % chat_id, {"text": "what does the seam hide?"}
        )

        # The served application answered that turn itself: a session opened on
        # the user's message, a launch, a preserved agent-sourced event, a
        # transcribed message, and a session that ends.
        store = ChatStore(self.root)
        user_record = store.read_messages(chat_id)[0]
        self.assertEqual(user_record["message_id"], sent["messages"][0]["message_id"])
        session = store.list_sessions(chat_id)[0][0]
        self.assertEqual(session["transitions"][0]["evidence"]["ref"],
                         user_record["message_id"])
        self.shell.post("/api/chats/%s/messages" % chat_id, {"text": "thanks"})

        _status, before = self.shell.get("/api/chats/" + chat_id)
        _status, list_before = self.shell.get("/api/chats")
        self.assertEqual(len(before["messages"]), 4)
        self.assertEqual(
            [m["author"] for m in before["messages"]], ["user", "agent", "user", "agent"]
        )

        # --- full restart ------------------------------------------------
        killed = self.shell.kill()
        self.assertFalse(is_alive(killed), "the shell survived SIGKILL")
        self.assertIsNone(self.shell.port)

        self.shell.start()
        second_pid = self.shell.process.pid
        self.assertNotEqual(first_pid, second_pid, "the shell was not actually restarted")

        _status, after = self.shell.get("/api/chats/" + chat_id)
        _status, list_after = self.shell.get("/api/chats")

        self.assertEqual(after, before, "the reopened history is not the history that was there")
        self.assertEqual(list_after, list_before)

        # No agent is running, and none was started by the restarted shell.
        self.assertEqual(children_of(second_pid), [])
        reopened = ChatStore(self.root)
        self.assertEqual(
            reopened.chat_agent_status(chat_id),
            {"open_bindings": [], "non_terminal_sessions": []},
        )
        self.assertIn(
            reopened.read_session(chat_id, session["session_id"])["state"],
            ("completed", "failed", "launch_failed", "terminated", "abandoned"),
        )

        # The agent's turn survived, and it still points at the preserved event
        # it was transcribed from (contract D2).
        agent_turn = [m for m in after["messages"] if m["author"] == "agent"][0]
        self.assertEqual(agent_turn["text"], "answer to: what does the seam hide?")
        durable = [m for m in reopened.read_messages(chat_id) if m["author"] == "agent"][0]
        self.assertEqual(durable["session_id"], session["session_id"])
        preserved = reopened.read_diagnostic_event(
            chat_id, durable["session_id"], durable["source_event_id"]
        )
        self.assertIsNotNone(preserved, "the evidence the agent turn was derived from is gone")
        self.assertEqual(preserved["source"], "agent")
        self.assertEqual(preserved["interpretation"], "recognized")
        self.assertEqual(reopened.verify(), [])

    def test_the_reopened_history_is_read_from_disk_and_not_from_memory(self):
        """A cache would pass a plain restart test. This one it cannot pass."""
        self.shell.start()
        _status, chat = self.shell.post("/api/chats", {})
        chat_id = chat["chat_id"]
        self.shell.post("/api/chats/%s/messages" % chat_id, {"text": "original text"})
        self.shell.kill()

        path = os.path.join(self.root, "chats", chat_id, "messages", "00000001.json")
        with open(path) as handle:
            record = json.load(handle)
        record["content"]["text"] = "edited while nothing was running"
        with open(path, "w") as handle:
            json.dump(record, handle)

        self.shell.start()
        _status, after = self.shell.get("/api/chats/" + chat_id)
        self.assertEqual(after["messages"][0]["text"], "edited while nothing was running")

    def test_the_history_survives_the_store_being_moved_between_restarts(self):
        """The durable records are the whole of it: nothing else has to come along."""
        self.shell.start()
        _status, chat = self.shell.post("/api/chats", {})
        chat_id = chat["chat_id"]
        self.shell.post("/api/chats/%s/messages" % chat_id, {"text": "portable"})
        _status, before = self.shell.get("/api/chats/" + chat_id)
        self.assertEqual([m["author"] for m in before["messages"]], ["user", "agent"])
        self.shell.kill()

        elsewhere = tempfile.mkdtemp(prefix="dory-moved-")
        self.addCleanup(shutil.rmtree, elsewhere, True)
        moved = os.path.join(elsewhere, "store")
        shutil.copytree(self.root, moved)

        relocated = ShellProcess(moved)
        self.addCleanup(relocated.kill)
        relocated.start()
        _status, after = relocated.get("/api/chats/" + chat_id)
        self.assertEqual(after, before)
        self.assertEqual(children_of(relocated.process.pid), [])

    def test_sending_continues_the_history_after_a_restart(self):
        self.shell.start()
        _status, chat = self.shell.post("/api/chats", {})
        chat_id = chat["chat_id"]
        self.shell.post("/api/chats/%s/messages" % chat_id, {"text": "before"})
        self.shell.kill()

        self.shell.start()
        _status, body = self.shell.post("/api/chats/%s/messages" % chat_id, {"text": "after"})
        self.assertEqual([m["text"] for m in body["messages"]],
                         ["before", "answer to: before", "after", "answer to: after"])
        self.assertEqual([m["sequence"] for m in body["messages"]], [1, 2, 3, 4])
        self.assertEqual(ChatStore(self.root).verify(), [])

    def test_a_restart_in_the_middle_of_a_send_loses_nothing_already_durable(self):
        """The shell is killed while it is serving; what was durable stays durable."""
        self.shell.start()
        _status, chat = self.shell.post("/api/chats", {})
        chat_id = chat["chat_id"]
        for i in range(6):
            self.shell.post("/api/chats/%s/messages" % chat_id, {"text": "turn %d" % i})
        self.shell.kill()

        self.shell.start()
        _status, after = self.shell.get("/api/chats/" + chat_id)
        self.assertEqual([m["text"] for m in after["messages"] if m["author"] == "user"],
                         ["turn %d" % i for i in range(6)])
        self.assertEqual([m["sequence"] for m in after["messages"]], list(range(1, 13)))


class TestRestartWithNoShellAtAll(RestartCase):
    def test_history_is_readable_with_nothing_running(self):
        """Contract D1: the read needs no live process of any kind.

        The shell is killed and never restarted. The history is then read by a
        program that is not the shell, and validated by the contract validator
        run as its own process.
        """
        import subprocess
        import sys

        from helpers import VALIDATOR

        self.shell.start()
        _status, chat = self.shell.post("/api/chats", {})
        chat_id = chat["chat_id"]
        self.shell.post("/api/chats/%s/messages" % chat_id, {"text": "a question"})
        self.shell.post("/api/chats/%s/messages" % chat_id, {"text": "and a follow-up"})
        killed = self.shell.kill()
        self.assertFalse(is_alive(killed))

        # Nothing is running. Read the history.
        offline = ChatStore(self.root)
        messages = offline.read_messages(chat_id)
        self.assertEqual(
            [m["content"]["text"] for m in messages],
            ["a question", "answer to: a question",
             "and a follow-up", "answer to: and a follow-up"],
        )

        # And hand the store to the contract validator as a separate process.
        snapshot = offline.snapshot(
            name="restart-with-no-agent",
            description="History read with no shell and no agent process running.",
        )
        path = os.path.join(tempfile.mkdtemp(prefix="dory-snap-"), "store.json")
        self.addCleanup(shutil.rmtree, os.path.dirname(path), True)
        with open(path, "w") as handle:
            json.dump(snapshot, handle, indent=2)
        completed = subprocess.run(
            [sys.executable, VALIDATOR, path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertEqual(
            completed.returncode, 0,
            completed.stdout.decode("utf-8") + completed.stderr.decode("utf-8"),
        )
        self.assertIn(b"PASS  accept", completed.stdout)


if __name__ == "__main__":
    unittest.main()


class TestARestartWithALiveAgentLeavesTheUserAWayOut(RestartCase):
    """Contract 5.4 through the served application: the Abandon affordance.

    A chat whose agent was live when the shell died is re-attached once at start.
    A launcher that cannot resume it -- which is every launcher in this
    repository and the modelled internal bridge -- leaves the session `unknown`,
    and `unknown` refuses every new turn. Without a user action that exits it,
    the chat is stranded for the life of the store: that is the release's reopen
    requirement failing, so it is proven here over HTTP, across a real SIGKILL.
    """

    PERSISTENT = {"continuation": "persistent", "response_shape": "stream",
                  "end_of_turn": "turn_complete"}

    def test_the_user_abandons_the_unreachable_agent_and_the_chat_continues(self):
        self.shell = ShellProcess(self.root, launcher_options=self.PERSISTENT)
        self.addCleanup(self.shell.kill)
        self.shell.start()
        _status, chat = self.shell.post("/api/chats", {})
        chat_id = chat["chat_id"]
        _status, body = self.shell.post("/api/chats/%s/messages" % chat_id,
                                        {"text": "stay with me"})
        self.assertEqual([m["author"] for m in body["messages"]], ["user", "agent"])
        live = ChatStore(self.root).list_sessions(chat_id)[0][0]
        self.assertEqual(live["state"], "running", "the agent must be live at the kill")

        self.shell.kill()
        self.shell.start()

        reopened = ChatStore(self.root)
        session = reopened.read_session(chat_id, live["session_id"])
        self.assertEqual(session["state"], "unknown",
                         "re-attachment at start did not resolve the live session")
        self.assertEqual([o["kind"] for o in reopened.read_session_observations(chat_id)],
                         ["reattach_failed"])

        # The history is all there, and a new turn is refused *before* it is
        # recorded, with the affordance offered.
        _status, opened = self.shell.get("/api/chats/" + chat_id)
        self.assertEqual([m["text"] for m in opened["messages"]],
                         ["stay with me", "answer to: stay with me"])
        status, refused = self.shell.raw("POST", "/api/chats/%s/messages" % chat_id,
                                         {"text": "are you still there?"})
        self.assertEqual(status, 409)
        self.assertTrue(json.loads(refused)["refused"])
        self.assertEqual(len(ChatStore(self.root).read_messages(chat_id)), 2,
                         "a refused turn was recorded")

        # The one action.
        status, abandoned = self.shell.raw("POST", "/api/chats/%s/abandon" % chat_id, {})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(abandoned)["messages"], opened["messages"])
        self.assertEqual(
            ChatStore(self.root).read_session(chat_id, live["session_id"])["state"],
            "abandoned")

        # And the chat is usable again, through a new agent the user's turn opens.
        status, continued = self.shell.raw("POST", "/api/chats/%s/messages" % chat_id,
                                           {"text": "are you still there?"})
        self.assertEqual(status, 201)
        self.assertEqual([m["text"] for m in json.loads(continued)["messages"]][-2:],
                         ["are you still there?", "answer to: are you still there?"])
        self.assertEqual(ChatStore(self.root).verify(), [])

    def test_abandon_is_refused_when_there_is_nothing_to_abandon(self):
        self.shell.start()
        _status, chat = self.shell.post("/api/chats", {})
        chat_id = chat["chat_id"]
        status, _body = self.shell.raw("POST", "/api/chats/%s/abandon" % chat_id, {})
        self.assertEqual(status, 409)
        self.shell.post("/api/chats/%s/messages" % chat_id, {"text": "hello"})
        status, _body = self.shell.raw("POST", "/api/chats/%s/abandon" % chat_id, {})
        self.assertEqual(status, 409, "a finished agent is not abandoned")
        self.assertEqual(self.shell.status_of("POST", "/api/chats/cht_nothinghere0/abandon"),
                         404)
        self.assertEqual(ChatStore(self.root).verify(), [])


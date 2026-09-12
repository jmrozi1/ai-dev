"""Shared test scaffolding.

`sys.path` is bootstrapped here because `dory-wrangler` is not a legal Python
package name, so the tests cannot simply import a top-level package.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PRODUCT_DIR = os.path.dirname(TESTS_DIR)
SRC_DIR = os.path.join(PRODUCT_DIR, "src")
RUN_SHELL = os.path.join(PRODUCT_DIR, "run_shell.py")
VALIDATOR = os.path.join(PRODUCT_DIR, "validator", "validate_contract.py")

if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from dory_wrangler.store import ChatStore  # noqa: E402

ONE_SHOT = {
    "continuation": "fresh_binding",
    "response_shape": "one_shot",
    "instruction_bound_bytes": None,
}

PERSISTENT_STREAM = {
    "continuation": "persistent",
    "response_shape": "stream",
    "instruction_bound_bytes": None,
}


class StoreCase(unittest.TestCase):
    """A test with a fresh store on disk."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="dory-test-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.store = ChatStore(self.root)

    def assertStoreValid(self, store=None):
        """The store on disk satisfies the executable contract."""
        store = store or self.store
        violations = store.verify()
        if violations:
            self.fail(
                "store violates the contract:\n"
                + "\n".join("  %s at %s: %s" % v for v in violations)
            )


def answered_turn(store, chat_id, user_text, agent_text,
                  capabilities=None, launcher_id="dev-local"):
    """Drive one complete user turn through to a preserved, rendered answer.

    This is what #87 and #88 will do at runtime. It is written here so that the
    persistence tests exercise real agent history rather than a chat containing
    only user text -- the whole claim under test is that agent history survives
    without the agent.

    Returns (session, event, agent_message).
    """
    user = store.append_user_message(chat_id, user_text)
    return answer_turn(store, chat_id, user, agent_text,
                       capabilities=capabilities, launcher_id=launcher_id)


def answer_turn(store, chat_id, user_message, agent_text,
                capabilities=None, launcher_id="dev-local"):
    """Answer a user turn that is already in the chat's durable history.

    This is the shape the integrated product has: #86's shell writes the user
    turn, and #87 opens a session on it. Returns (session, event, agent_message).
    """
    capabilities = capabilities or ONE_SHOT
    user = user_message
    user_text = user["content"]["text"]
    session, _binding = store.create_session(
        chat_id, user["message_id"], launcher_id, capabilities
    )
    sid = session["session_id"]
    request = store.append_launch_request(chat_id, sid, user_text)
    store.append_transition(
        chat_id, sid, "pending", "launching", "harness",
        {"kind": "harness_action", "ref": request["request_id"]},
    )
    handle = "agent-" + sid[-6:]
    store.append_launch_result(
        chat_id, request["request_id"], sid, "accepted", agent_handle=handle
    )
    store.append_transition(
        chat_id, sid, "launching", "running", "launcher",
        {"kind": "launch_result", "ref": request["request_id"]},
    )
    store.set_agent_handle(chat_id, sid, handle)
    event, _created = store.append_diagnostic_event(
        chat_id, sid, store.next_event_sequence(chat_id, sid),
        "agent", "recognized", "assistant_text",
        '{"type":"assistant_text","text":%s}' % json.dumps(agent_text),
    )
    message = store.append_agent_message(chat_id, sid, event["event_id"], agent_text)
    done, _created = store.append_diagnostic_event(
        chat_id, sid, store.next_event_sequence(chat_id, sid),
        "launcher", "recognized", "session_completed", '{"type":"session_completed"}',
    )
    session, _binding = store.append_transition(
        chat_id, sid, "running", "completed", "launcher",
        {"kind": "event", "ref": done["event_id"]},
    )
    return session, event, message

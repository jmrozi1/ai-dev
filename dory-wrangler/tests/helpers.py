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
    # The handle enters `running` in the same write as the transition. A store
    # that recorded the transition first would be SESSION_HANDLE_MISSING until
    # the next write and permanently so if that write never came, which is the
    # window the store now refuses to open.
    store.append_transition(
        chat_id, sid, "launching", "running", "launcher",
        {"kind": "launch_result", "ref": request["request_id"]},
        agent_handle=handle,
    )
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


# ---------------------------------------------------------------------------
# The contract's violation vocabulary, read out of the contract
# ---------------------------------------------------------------------------
#
# The store must account for every violation code the executable contract can
# emit. A list of those codes written alongside the accounting would be a second
# copy of the contract's vocabulary, and a second copy that silently falls
# behind is precisely the defect the accounting exists to end -- so the list is
# derived from the validator's own source every time it is asked for.

import ast  # noqa: E402  (kept beside the code that uses it)
import re  # noqa: E402

CODE_SHAPE = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")


def _constants(node):
    return set(
        n.value for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and CODE_SHAPE.match(n.value)
    )


def _functions(tree):
    return dict(
        (n.name, n) for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    )


def _calls(node):
    names = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            if isinstance(n.func, ast.Name):
                names.add(n.func.id)
            elif isinstance(n.func, ast.Attribute):
                names.add(n.func.attr)
    return names


def _reachable(functions, start):
    seen, stack = set(), [start]
    while stack:
        name = stack.pop()
        if name in seen or name not in functions:
            continue
        seen.add(name)
        stack.extend(_calls(functions[name]))
    return seen


def contract_violation_vocabulary(path=None):
    """(every code, record-level codes, cross-record codes) of the contract.

    Read structurally from the validator's source rather than by running it: a
    code that only fires on an input nobody has thought of is exactly the one an
    accounting must not miss, and no execution-based enumeration can find it.

    The shape test over-approximates on purpose. Every screaming-snake-case
    string literal in the validator is treated as a code; today all 53 of them
    are one, 52 of which appear as a `report.add` argument and the 53rd of which
    is returned as a pair. If the contract ever gains an upper-case literal that
    is not a code, this demands that it be classified -- a false alarm a person
    resolves, rather than a missed code nobody sees, which is the direction to
    fail in.

    A code is *record-level* when it is emitted somewhere in `validate_record`'s
    call graph, which is the check every record passes through `_check_record`
    before this store writes it, and *cross-record* when it is emitted outside
    that graph. A code can be both. A code in neither is emitted by the fixture
    driver about a fixture document rather than about any record.
    """
    # The same resolution the store itself uses, so the accounting is read from
    # whichever contract the code under test is actually running against.
    path = path or os.environ.get("DORY_WRANGLER_VALIDATOR") or VALIDATOR
    with open(path) as handle:
        tree = ast.parse(handle.read())
    functions = _functions(tree)
    every = _constants(tree)
    record_fns = _reachable(functions, "validate_record")
    store_fns = _reachable(functions, "validate_store") - record_fns
    record = set()
    for name in record_fns:
        record |= _constants(functions[name])
    cross = set()
    for name in store_fns:
        cross |= _constants(functions[name])
    return every, record & every, cross & every

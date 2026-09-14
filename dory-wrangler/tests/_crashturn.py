#!/usr/bin/env python3
"""A child process that serves one real turn and is killed at one write of it.

Run as:

    python3 _crashturn.py <root> <chat_id> <launcher-options-json> <k> <point> <warmup-json>

The whole converged path runs -- `SessionManager.send_turn` over `ChatStore`
with the scripted launcher -- and the process dies at fault point `point` of
the `k`-th store write the turn makes. The fault hook is #86's own, in the
product's write path, armed by setting its environment variable immediately
before that write; the death is `os._exit`, with no unwinding. Exit 97 means the
fault fired; exit 0 means the turn made fewer than `k` writes that reach
`point` (with `k` 0 it never fires, and the number of writes the turn made is
printed). The turns in `warmup` are served first by the same loop and launcher,
unarmed, so a delivery to a live agent is a real one: the scripted launcher is
in-process and a fresh process could not reach an agent another one started.
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

from dory_wrangler import atomic  # noqa: E402
from dory_wrangler.launchers.scripted_stub import ScriptedStubLauncher  # noqa: E402
from dory_wrangler.session_manager import SessionManager  # noqa: E402
from dory_wrangler.store import ChatStore  # noqa: E402

WRITES = (
    "append_user_message", "create_session", "append_launch_request",
    "append_launch_result", "append_transition", "append_diagnostic_event",
    "append_agent_message", "append_delivery_request",
    "record_delivery_acknowledgement", "append_session_observation",
)


def main(argv):
    root, chat_id, options, k, point = argv[0], argv[1], json.loads(argv[2]), int(argv[3]), argv[4]
    warmup = json.loads(argv[5])
    store = ChatStore(root, sweep=False)
    manager = SessionManager(store, ScriptedStubLauncher(options))
    for text in warmup:
        manager.send_turn(chat_id, text)
    count = [0]

    for name in WRITES:
        real = getattr(store, name)

        def armed(*args, _real=real, **kwargs):
            count[0] += 1
            if count[0] == k:
                os.environ[atomic.FAULT_ENV] = point
            elif count[0] > k:
                # Only the k-th write may die, so a point the k-th write never
                # reaches does not fire at some later write instead.
                os.environ.pop(atomic.FAULT_ENV, None)
            return _real(*args, **kwargs)

        setattr(store, name, armed)

    manager.send_turn(chat_id, "crash me")
    sys.stdout.write("%d\n" % count[0])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

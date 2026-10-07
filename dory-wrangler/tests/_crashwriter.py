#!/usr/bin/env python3
"""A child process that performs one real store write and is killed partway.

Run as:

    DORY_WRANGLER_FAULT=<point> python3 _crashwriter.py <root> <what> [args...]

`what` is one of:

    message <chat_id> <text>     append a user message
    chat <title>                 create a chat
    transition <chat_id> <ses>   append pending -> launch_failed

The point of running this out of process is that the write path being killed is
the product's own, in a process that genuinely dies with no unwinding, no atexit
handler, and no buffer flush. A test that simulated a crash inside its own copy
of the write path would be testing the copy.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

from dory_wrangler.store import ChatStore  # noqa: E402


def main(argv):
    root, what = argv[0], argv[1]
    store = ChatStore(root, sweep=False)
    if what == "message":
        store.append_user_message(argv[2], argv[3])
    elif what == "chat":
        store.create_chat(argv[2])
    elif what == "transition":
        store.append_transition(
            argv[2], argv[3], "pending", "launch_failed", "harness",
            {"kind": "harness_action", "ref": None},
        )
    else:
        raise SystemExit("unknown action %r" % what)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

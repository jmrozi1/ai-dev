#!/usr/bin/env python3
"""A local development agent program, for the external Linux development launcher.

This is a stand-in for a real agent, not part of the launch boundary. It exists
so that `DevLocalLauncher` starts a genuine operating-system process, writes
instruction text to it, and reads what comes back -- which is what makes the
seam worth having. The internal path starts a real agent through the VS Code /
network bridge instead; nothing above the boundary can tell the difference.

Transport (private to `dev_local.py`, never crosses the seam): newline-delimited
JSON on stdout, one object per line, `{"type": ..., ...}`.

    --profile one_shot    read all of stdin as one instruction, answer, exit
    --profile persistent  one instruction per stdin line, answer each, exit on EOF

    --garbage             emit one unparseable line, to exercise preservation of
                          evidence that could not be interpreted (contract P1)
    --unknown-type        emit one well-formed line of a type this build does not
                          know, which is a finding rather than an error (P2)
    --fail-exit           exit non-zero after answering
    --silent              answer nothing at all and exit 0
    --close-stdout        close stdout after the first answer and then block on
                          stdin, so the stream ends while the process is alive
"""

import argparse
import json
import os
import sys


def emit(handle, obj):
    handle.write(json.dumps(obj) + "\n")
    handle.flush()


def answer(instruction):
    return "answer to: %s" % instruction.strip()


def respond(out, instruction, args):
    if args.garbage:
        out.write("this is not json at all {{{\n")
        out.flush()
    if args.unknown_type:
        emit(out, {"type": "agent_thinking", "detail": "a type this build does not know"})
    emit(out, {"type": "assistant_text", "text": answer(instruction)})
    emit(out, {"type": "turn_complete"})


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("one_shot", "persistent"), required=True)
    parser.add_argument("--garbage", action="store_true")
    parser.add_argument("--unknown-type", action="store_true")
    parser.add_argument("--fail-exit", action="store_true")
    parser.add_argument("--silent", action="store_true")
    parser.add_argument("--close-stdout", action="store_true")
    args = parser.parse_args(argv[1:])

    out = sys.stdout

    if args.profile == "one_shot":
        instruction = sys.stdin.read()
        if not args.silent:
            respond(out, instruction, args)
        return 1 if args.fail_exit else 0

    answered = 0
    for line in sys.stdin:
        if not line.strip():
            continue
        if not args.silent:
            respond(out, line, args)
        answered += 1
        if args.close_stdout and answered == 1:
            # os.close rather than out.close(): CPython builds sys.stdout with
            # closefd=False, so closing the wrapper leaves file descriptor 1
            # open and the reader never sees an end of stream.
            out.flush()
            os.close(out.fileno())
            # Block on stdin with the stream closed: the launcher observes an end
            # of stream while the agent is still alive, which is exactly the fact
            # contract 5.3 cause 2 distinguishes from a merely quiet stream.
            sys.stdin.read()
            return 0
    return 1 if args.fail_exit else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

#!/usr/bin/env python3
"""The served application, hosting the internal-bridge model, as its own process.

**Test material.** `run_shell.py` selects a launcher by registry id, and the model
is deliberately registered nowhere, so this is the smallest program that hands
the model to the product's own `build_server` and serves -- nothing else. It is
what lets a test restart the shell for real (SIGKILL, then a new process) with
the chat resuming on the same thread.

    model_shell.py --root <store> --port-file <file> --codex-home <dir> --spool <dir>
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

from dory_wrangler.webapp import build_server  # noqa: E402
from internal_bridge import InternalBridgeLauncher  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--port-file", required=True)
    parser.add_argument("--codex-home", required=True)
    parser.add_argument("--spool", required=True)
    args = parser.parse_args(argv)
    server = build_server(args.root, port=0, quiet=True,
                          launcher=InternalBridgeLauncher(args.codex_home, args.spool))
    with open(args.port_file + ".partial", "w") as handle:
        handle.write(str(server.server_address[1]))
    os.replace(args.port_file + ".partial", args.port_file)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Run the chat shell.

    python3 dory-wrangler/run_shell.py --root <store> [--host H] [--port P]
        [--launcher dev-local|scripted-stub] [--launcher-options JSON]

Binds to 127.0.0.1 by default: v0.1 has no authentication of any kind, so the
shell is a local surface until someone decides otherwise, and that decision is
not this ticket's to make by default.
"""

import argparse
import json
import os
import sys

from .errors import StoreInUse
from .service import DEFAULT_LAUNCHER
from .webapp import build_server

# A second shell on a store another process is serving.
EXIT_STORE_IN_USE = 3


def main(argv=None):
    parser = argparse.ArgumentParser(description="Dory-wrangler v0.1 chat shell")
    parser.add_argument("--root", default=os.environ.get("DORY_WRANGLER_ROOT", "./dory-store"),
                        help="directory holding the durable store")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765,
                        help="0 asks the kernel for a free port")
    parser.add_argument("--port-file", default=None,
                        help="write the bound port here once listening (used by tests)")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--launcher", default=DEFAULT_LAUNCHER["launcher"],
                        help="the configured launcher's id (launchers/registry.py)")
    parser.add_argument("--launcher-options", default=None,
                        help="the launcher's own options, as a JSON object")
    args = parser.parse_args(argv)

    if args.launcher_options is not None:
        options = json.loads(args.launcher_options)
    elif args.launcher == DEFAULT_LAUNCHER["launcher"]:
        options = dict(DEFAULT_LAUNCHER["options"])
    else:
        options = {}
    config = {"launcher": args.launcher, "options": options}
    try:
        server = build_server(args.root, host=args.host, port=args.port, quiet=args.quiet,
                              launcher_config=config)
    except StoreInUse:
        # Decision 0002, D1: one serving process per store. Refused before
        # anything was swept, re-attached or written, so there is nothing to
        # undo and nothing to report but the refusal itself.
        sys.stderr.write(
            "dory-wrangler: another shell is already serving the store at %s; "
            "this one changed nothing and is exiting\n" % os.path.abspath(args.root))
        sys.stderr.flush()
        return EXIT_STORE_IN_USE
    bound = server.server_address[1]
    if args.port_file:
        with open(args.port_file, "w") as handle:
            handle.write(str(bound))
            handle.flush()
            os.fsync(handle.fileno())
    if not args.quiet:
        sys.stderr.write(
            "dory-wrangler shell on http://%s:%d  store=%s\n"
            % (args.host, bound, os.path.abspath(args.root))
        )
        sys.stderr.flush()
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0

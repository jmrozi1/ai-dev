"""Run the chat shell.

    python3 dory-wrangler/run_shell.py --root <store> [--host H] [--port P]

Binds to 127.0.0.1 by default: v0.1 has no authentication of any kind, so the
shell is a local surface until someone decides otherwise, and that decision is
not this ticket's to make by default.
"""

import argparse
import os
import sys

from .webapp import build_server


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
    args = parser.parse_args(argv)

    server = build_server(args.root, host=args.host, port=args.port, quiet=args.quiet)
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

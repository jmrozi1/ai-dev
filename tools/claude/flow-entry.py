"""Deterministic entry point for the installed Flow lifecycle launchers.

Running a script by absolute path makes ``sys.path[0]`` the script's own
directory, so the caller's working directory is never consulted for imports.
That is what an installed launcher needs: it is a copy living outside any
checkout, so it cannot find the runtime by walking parents from wherever it
landed. Walking parents is exactly how the skill-directory launchers used to
resolve, which put them in the user's home instead of the AI Dev runtime.

Runtime ownership and the product repository are separate concerns here. The
runtime is fixed by this file's own location; the working directory is left
untouched, because that is what the lifecycle commands resolve the product
repository from.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

RUNTIME_ROOT = Path(__file__).resolve().parents[2]

if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from ai_dev_flow.cli import DIRECT_FLOW_ROUTE_TOKEN, run  # noqa: E402


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print(
            "flow-entry: missing lifecycle command. This entry point is invoked by the "
            "installed flow-* launchers, not directly.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    command = sys.argv[1]
    # Usage strings name the command the caller actually typed.
    os.environ["FLOW_COMMAND_NAME"] = f"flow-{command}"
    # run() reads sys.argv itself and owns the shared error handling, so routing
    # happens by rewriting argv rather than by duplicating that handling here.
    sys.argv = [f"flow-{command}", DIRECT_FLOW_ROUTE_TOKEN, command, *sys.argv[2:]]
    run()


if __name__ == "__main__":
    main()

"""Deterministic entry point for the Claude AI Dev launcher.

Running a script by absolute path makes ``sys.path[0]`` the script's own
directory, so the caller's working directory is never consulted for imports.
That is what keeps this entry point correct when the caller happens to be
standing in a different, older ai-dev checkout -- the failure mode the generic
workspace launchers still have. This file owns only its own resolution; it does
not modify those launchers.
"""

from __future__ import annotations

import sys
from pathlib import Path

RUNTIME_ROOT = Path(__file__).resolve().parents[2]

if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from ai_dev_flow.claude_activation import run  # noqa: E402

if __name__ == "__main__":
    run()

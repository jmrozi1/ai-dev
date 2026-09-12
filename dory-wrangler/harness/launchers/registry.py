"""Choosing a launcher is configuration, and only configuration.

    build_launcher({"launcher": "dev-local", "options": {"profile": "one_shot"}})
    build_launcher({"launcher": "scripted-stub", "options": {...}})

The internal VS Code/network bridge launcher is not reachable from this
development VM and is not implemented here. Adding it is a new module under this
package plus one entry in the table below; nothing in the chat, session, or
store layers changes, and that is the swappability claim this file exists to
make checkable rather than asserted.
"""

from __future__ import annotations

import re

from launch_boundary import LAUNCHER_ID_PATTERN, LaunchBoundary
from launchers.dev_local import DevLocalLauncher
from launchers.scripted_stub import ScriptedStubLauncher

BUILDERS = {
    DevLocalLauncher.launcher_id: DevLocalLauncher.from_options,
    ScriptedStubLauncher.launcher_id: ScriptedStubLauncher.from_options,
}


class UnknownLauncher(Exception):
    pass


def build_launcher(config, builders=None):
    """Build the launcher a configuration names.

    `config` is `{"launcher": <launcher_id>, "options": {...}}`. Options are the
    launcher's own configuration and never travel in an instruction packet
    (contract 6.2): a launcher that needs configuration obtains it from its own
    environment.
    """
    table = BUILDERS if builders is None else builders
    name = config.get("launcher")
    if name not in table:
        raise UnknownLauncher(
            "no launcher %r is configured; known launchers are %s"
            % (name, ", ".join(sorted(table)))
        )
    launcher = table[name](dict(config.get("options") or {}))
    if not isinstance(launcher, LaunchBoundary):
        raise UnknownLauncher(
            "launcher %r did not build a LaunchBoundary" % (name,))
    if not re.match(LAUNCHER_ID_PATTERN, launcher.launcher_id or ""):
        raise UnknownLauncher(
            "launcher_id %r does not match the shape contract 4.3 fixes"
            % (launcher.launcher_id,))
    return launcher

"""Wiring. The only place that knows both a store and a launcher exist.

    harness = open_harness({"launcher": "dev-local", "options": {"profile": "one_shot"}}, root)
    harness = open_harness({"launcher": "scripted-stub"}, root)

Choosing an implementation is this dictionary and nothing else. No chat, session,
store, or transcript code changes, which is the swappability claim #87 owes
evidence for. The store is always `ChatStore` over `root`: there is one store
implementation (decision 0002), and this is where the served application and the
test suite both get it.
"""

from __future__ import annotations

from .launchers.registry import build_launcher
from .session_manager import SessionManager
from .store import ChatStore


def open_harness(config, root, launcher=None, builders=None, compose=None):
    """Open the chat loop over the durable store at `root`, with the configured launcher.

    `launcher` accepts an already-built `LaunchBoundary` so that an
    implementation living outside this repository can be dropped in without
    being registered anywhere. That is the strongest form of the swappability
    claim, and the test suite uses it.
    """
    boundary = launcher if launcher is not None else build_launcher(config, builders)
    return SessionManager(ChatStore(root), boundary, compose=compose)

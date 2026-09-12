"""Wiring. The only place that knows both a store and a launcher exist.

    harness = open_harness({"launcher": "dev-local", "options": {"profile": "one_shot"}})
    harness = open_harness({"launcher": "scripted-stub"})

Choosing an implementation is this dictionary and nothing else. No chat, session,
store, or transcript code changes, which is the swappability claim #87 owes
evidence for.
"""

from __future__ import annotations

from launchers.registry import build_launcher
from session_manager import SessionManager
from store import Store


def open_harness(config, store_path=None, ids=None, clock=None, compose=None,
                 launcher=None, builders=None):
    """Open a harness over a durable store, with the configured launcher.

    `launcher` accepts an already-built `LaunchBoundary` so that an
    implementation living outside this repository can be dropped in without
    being registered anywhere. That is the strongest form of the swappability
    claim, and the test suite uses it.
    """
    boundary = launcher if launcher is not None else build_launcher(config, builders)
    return SessionManager(Store(store_path), boundary, ids=ids, clock=clock,
                          compose=compose)

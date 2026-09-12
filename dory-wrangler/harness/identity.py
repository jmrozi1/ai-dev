"""Opaque identifiers and timestamps, per contract section 3.

Identifiers carry no meaning: nothing is parsed out of one, and no ordering,
timestamp, or filename substitutes for one. Timestamps order events for display
and are checked for regression; no rule in this package fires because of elapsed
time, and nothing here computes a duration or compares one against a threshold.
"""

from __future__ import annotations

import binascii
import datetime
import os
import re
import threading

ID_PREFIXES = ("cht", "msg", "ses", "bnd", "req", "dlv", "obs", "evt")

ID_RE = re.compile(r"^(cht|msg|ses|bnd|evt|req|dlv|obs)_[0-9a-z]{8,32}$")
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")


class IdFactory(object):
    """Opaque identifier source. Deterministic variants exist for tests only."""

    def __call__(self, prefix):
        if prefix not in ID_PREFIXES:
            raise ValueError("unknown identifier prefix %r" % (prefix,))
        # os.urandom rather than uuid: `uuid` drags process machinery in behind
        # it, and this module is imported by the chat and session layers, which
        # must stay free of it. 16 hex characters of randomness, opaque by
        # construction and matching the contract's identifier shape.
        return "%s_%s" % (prefix, binascii.hexlify(os.urandom(8)).decode("ascii"))


class SequentialIdFactory(object):
    """Deterministic ids, so a produced store can be diffed run to run.

    Still opaque: the counter is zero-padded into the same 16-character opaque
    body every other identifier uses, and nothing anywhere parses it back out.
    """

    def __init__(self, salt="t"):
        self._salt = re.sub(r"[^0-9a-z]", "", salt.lower()) or "t"
        self._counters = {}
        self._lock = threading.Lock()

    def __call__(self, prefix):
        if prefix not in ID_PREFIXES:
            raise ValueError("unknown identifier prefix %r" % (prefix,))
        with self._lock:
            n = self._counters.get(prefix, 0) + 1
            self._counters[prefix] = n
        body = ("%s%09d" % (self._salt, n))[-16:]
        while len(body) < 8:
            body = "0" + body
        return "%s_%s" % (prefix, body)


class Clock(object):
    """A non-decreasing UTC clock emitting RFC 3339 with six fractional digits.

    Six digits always, so lexicographic comparison of two of these strings is
    chronological comparison -- which is the only thing the contract does with
    them (TIME_REGRESSION, and 6.4's strictly-after delivery rule). It is not a
    timer: no code reads this clock to decide that something has taken too long.
    """

    def __init__(self):
        self._last = None
        self._lock = threading.Lock()

    def now(self):
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        with self._lock:
            if self._last is not None and stamp < self._last:
                stamp = self._last
            self._last = stamp
        return stamp


class FixedClock(object):
    """A deterministic clock for reproducible stores. Advances only when called."""

    def __init__(self, start="2026-09-12T10:00:00.000000Z", step_micros=1000000):
        if not TS_RE.match(start):
            raise ValueError("start must be RFC 3339 UTC with a Z suffix")
        self._value = datetime.datetime.strptime(
            start, "%Y-%m-%dT%H:%M:%S.%fZ"
        ).replace(tzinfo=datetime.timezone.utc)
        self._step = datetime.timedelta(microseconds=step_micros)
        self._lock = threading.Lock()

    def now(self):
        with self._lock:
            stamp = self._value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            self._value = self._value + self._step
            return stamp

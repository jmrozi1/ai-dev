"""Opaque identifiers and timestamps.

Contract section 3: every identifier is `<prefix>_<8-32 lowercase alphanumerics>`
and carries no meaning. Nothing may be parsed out of one, and no ordering,
timestamp, or filename may substitute for one.

Two consequences this module is responsible for:

* Identifiers are drawn from `secrets`, not from a counter, a clock, or a hash
  of the record. Nothing is recoverable from one, so no caller can be tempted to
  read one.
* `new_id` is the only source of identifiers in this package. The store reads a
  record's identity from the record's own field and never from the filename it
  happened to be stored under, so a filename cannot substitute for an id.
"""

import datetime
import re
import secrets

ID_RE = re.compile(r"^(cht|msg|ses|bnd|evt|req|dlv|obs)_[0-9a-z]{8,32}$")

PREFIXES = ("cht", "msg", "ses", "bnd", "evt", "req", "dlv", "obs")

# 24 hex characters: inside the contract's 8-32 window, drawn from [0-9a-f]
# which is a subset of the permitted lowercase alphanumerics, and 96 bits of
# entropy so that collisions are not a thing the store has to reason about.
_ID_HEX_BYTES = 12


def new_id(prefix):
    """Return a fresh opaque identifier with the given contract prefix."""
    if prefix not in PREFIXES:
        raise ValueError("unknown identifier prefix %r" % (prefix,))
    return "%s_%s" % (prefix, secrets.token_hex(_ID_HEX_BYTES))


def is_id(value, prefix=None):
    if not isinstance(value, str) or not ID_RE.match(value):
        return False
    if prefix is not None and not value.startswith(prefix + "_"):
        return False
    return True


def now():
    """Current time as an RFC 3339 UTC timestamp with microseconds.

    Microsecond resolution is deliberate. Contract 6.4 notes that at one-second
    granularity a delivery that genuinely preceded an agent's exit can share its
    timestamp; recording more resolution costs nothing and removes that tie for
    records this store writes.

    No rule in this package fires because of elapsed time. Timestamps are
    recorded for display and for the contract's regression checks only.
    """
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

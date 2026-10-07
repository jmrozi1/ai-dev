#!/usr/bin/env python3
"""Print a chat's preserved raw evidence, verbatim and bounded (contract P4).

    python3 dory-wrangler/diagnostics.py STORE CHAT_ID [--session SESSION_ID]
        [--from N] [--to N] [--limit N] [--records events|lifecycle|messages]
        [--resume-at SESSION_ID:N]

The bounded out-of-band diagnostic retrieval of contract P4 and 8.5, as a
command. It prints preserved records exactly as the store holds them, one JSON
object per line on standard output, and **derives nothing**: no counts, no
grouping, no per-type tallies, no joining of one record kind to another, no
ranking, no interpretation. A view that does any of that is #82 (decision 0007).
What a reader concludes from the records is the reader's.

What it prints, chosen by `--records`:

``events`` (the default)
    the chat's `diagnostic_event` records -- addressed by `CHAT_ID`, optionally
    narrowed by `--session` and a `sequence` range `--from`/`--to` -- in the
    store's order (session id, then sequence). `raw.body` is printed as stored,
    so bytes that were not UTF-8 stay base64, and every character outside
    printable ASCII is a JSON escape: nothing raw reaches the terminal.
    `--resume-at SESSION_ID:N` continues a chat-wide retrieval at that record:
    the records at and after it in that same order, `--from`/`--to` still
    applying in every session.
``lifecycle``
    the preserved `agent_session`, `launch_result` and `session_observation`
    records -- of `--session`, or of every session of the chat -- each session's
    in that order and in the store's order; `--from`/`--to` number the lines of
    this listing from 1.
``messages``
    the chat's `message` records, `--from`/`--to` being message sequences.

Every mode returns at most `--limit` records (default 100, at most 1000: the
store's own `DIAGNOSTIC_PAGE_DEFAULT` and `DIAGNOSTIC_PAGE_MAX`). When the bound
held records back, a line on standard error says so, names the next record's
address, and gives the arguments that ask for the rest. Followed literally and
repeatedly, those arguments return every preserved record once, in one order: a
chat-wide `events` retrieval continues by `--resume-at`, never by a listing
ordered some other way (checkpoint review finding G1).

It opens the store **read-only** (decision 0002, D1): it takes no lock, sweeps
nothing, creates nothing and writes nothing, so it can run while `run_shell.py`
serves the same store, or while nothing does. It is not part of the served
application, which neither imports it nor has a route to it.

Exit status:
    0  the records were printed (possibly none; see standard error for a bound)
    2  refused: an argument, an identifier, a bound, or an address the store
       does not hold -- in fixed words that never carry a path or an error text
    3  the store could not be read (fail-closed, contract D3); `validate_store.py`
       says why
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from dory_wrangler import ids  # noqa: E402
from dory_wrangler.errors import NotFound, StoreError  # noqa: E402
from dory_wrangler.store import (  # noqa: E402
    DIAGNOSTIC_PAGE_DEFAULT,
    DIAGNOSTIC_PAGE_MAX,
    ChatStore,
)

USAGE = (
    "diagnostics.py STORE CHAT_ID [--session SESSION_ID] [--from N] [--to N] "
    "[--limit N] [--records events|lifecycle|messages] [--resume-at SESSION_ID:N]"
)

# Fixed words. Nothing a refusal prints comes from the store, the filesystem or
# an exception: the served boundary makes the same promise (review finding R3).
REFUSED_ARGUMENTS = "refused: the arguments were not understood; usage: " + USAGE
REFUSED_STORE = "refused: that is not a store directory"
REFUSED_CHAT_ID = "refused: that is not a chat identifier"
REFUSED_SESSION_ID = "refused: that is not a session identifier"
REFUSED_NO_CHAT = "refused: that store holds no such chat"
REFUSED_NO_SESSION = "refused: that chat holds no such session"
REFUSED_LIMIT = "refused: --limit must be a whole number of at least 1"
REFUSED_RANGE = "refused: --from and --to must be whole numbers of at least 1"
REFUSED_MESSAGES_BY_SESSION = (
    "refused: messages are addressed by chat and sequence only, not by --session")
REFUSED_RESUME = (
    "refused: --resume-at must be a session identifier, a colon and a whole number "
    "of at least 1")
REFUSED_RESUME_SCOPE = (
    "refused: --resume-at continues a chat-wide events retrieval, so it takes no "
    "--session and no other --records")
UNREADABLE = (
    "error: the store could not be read, so nothing was printed; nothing was "
    "changed. validate_store.py says why.")

MODES = ("events", "lifecycle", "messages")


class Refused(Exception):
    """A refusal, carrying its fixed words and exit status."""

    def __init__(self, words, status=2):
        Exception.__init__(self, words)
        self.words = words
        self.status = status


class _Parser(argparse.ArgumentParser):
    """argparse, except that its errors are fixed words too: its own messages
    echo whatever was typed back."""

    def error(self, message):
        raise Refused(REFUSED_ARGUMENTS)


def _whole(text, words):
    """A whole number of at least 1, from exactly its decimal digits, or a refusal."""
    if text is None:
        return None
    if not (isinstance(text, str) and text.isascii() and text.isdigit()):
        raise Refused(words)
    value = int(text)
    if value < 1:
        raise Refused(words)
    return value


def _line(record):
    """One record, verbatim, as one line of printable ASCII."""
    return json.dumps(record, sort_keys=True, ensure_ascii=True)


def _resumption(text):
    """`SESSION_ID:N` -> (session id, N), or a refusal."""
    session_id, colon, sequence = text.partition(":")
    if not (colon and ids.is_id(session_id, "ses")):
        raise Refused(REFUSED_RESUME)
    return session_id, _whole(sequence, REFUSED_RESUME)


def _resumed(store, chat_id, position, first, last, limit):
    """A chat-wide retrieval continued at `position`: the preserved records at
    and after it in the order the chat-wide retrieval itself uses -- session id,
    then sequence -- at most `limit`, and the address of the first the bound
    held back. Each session is read through the store's own bounded retrieval,
    so addressing, the cap and the temp-file filter are the store's."""
    at_session, at_sequence = position
    later = sorted(session["session_id"] for session, _binding in
                   store.list_sessions(chat_id) if session["session_id"] > at_session)
    records = []
    at_lower = at_sequence if first is None else max(at_sequence, first)
    for session_id, lower in [(at_session, at_lower)] + [(s, first) for s in later]:
        room = limit - len(records)
        page, following = store.read_diagnostic_page(
            chat_id, session_id=session_id, sequence_from=lower, sequence_to=last,
            limit=max(room, 1))
        if room == 0:
            if page:
                return records, {"session_id": session_id, "sequence": page[0]["sequence"]}
            continue
        records.extend(page)
        if following is not None:
            return records, following
    return records, None


def _events(store, chat_id, session_id, first, last, limit, resume_at):
    if resume_at is None:
        records, following = store.read_diagnostic_page(
            chat_id, session_id=session_id, sequence_from=first, sequence_to=last,
            limit=limit)
    else:
        records, following = _resumed(store, chat_id, resume_at, first, last, limit)
    more = None
    if following is not None:
        # One address, in one order. Within a session it is that session's next
        # sequence; for the whole chat it is the position to resume the same
        # chat-wide retrieval from, which crosses into the next session by
        # itself (checkpoint review finding G1).
        if session_id is not None:
            ask = "--session %s --from %d" % (following["session_id"], following["sequence"])
        else:
            ask = "--resume-at %s:%d%s" % (
                following["session_id"], following["sequence"],
                "" if first is None else " --from %d" % first)
        more = "the next is session %s sequence %d; ask again with %s%s" % (
            following["session_id"], following["sequence"], ask,
            "" if last is None else " --to %d" % last)
    return records, more


def _positioned(listing, first, last, limit, what):
    """Lines `first`..`last` of `listing`, numbered from 1, at most `limit`."""
    start = (first or 1) - 1
    chosen = listing[start:last]
    more = None
    if len(chosen) > limit:
        more = ("the next is %s %d; ask again with --from %d%s" % (
            what, start + limit + 1, start + limit + 1,
            "" if last is None else " --to %d" % last))
    return chosen[:limit], more


def _lifecycle(store, chat_id, session_id, first, last, limit):
    if session_id is None:
        sessions = [session for session, _binding in store.list_sessions(chat_id)]
    else:
        sessions = [store.read_session(chat_id, session_id)]
    listing = []
    for session in sessions:
        sid = session["session_id"]
        listing.append(session)
        listing.extend(store.read_launch_results(chat_id, sid))
        listing.extend(store.read_session_observations(chat_id, sid))
    return _positioned(listing, first, last, limit, "line")


def _messages(store, chat_id, first, last, limit):
    listing = [m for m in store.read_messages(chat_id)
               if (first is None or m["sequence"] >= first)
               and (last is None or m["sequence"] <= last)]
    records = listing[:limit]
    more = None
    if len(listing) > limit:
        following = listing[limit]["sequence"]
        more = "the next is message sequence %d; ask again with --from %d%s" % (
            following, following, "" if last is None else " --to %d" % last)
    return records, more


def retrieve(argv):
    """(records, truncation statement or None), or raise `Refused`."""
    parser = _Parser(prog="diagnostics.py", usage=USAGE, add_help=True,
                     description=__doc__.splitlines()[0])
    parser.add_argument("store")
    parser.add_argument("chat_id")
    parser.add_argument("--session", dest="session_id", default=None)
    parser.add_argument("--from", dest="first", default=None)
    parser.add_argument("--to", dest="last", default=None)
    parser.add_argument("--limit", default=None)
    parser.add_argument("--records", default="events")
    parser.add_argument("--resume-at", dest="resume_at", default=None)
    args = parser.parse_args(argv)

    if args.records not in MODES:
        raise Refused(REFUSED_ARGUMENTS)
    limit = _whole(args.limit, REFUSED_LIMIT)
    limit = DIAGNOSTIC_PAGE_DEFAULT if limit is None else min(limit, DIAGNOSTIC_PAGE_MAX)
    first = _whole(args.first, REFUSED_RANGE)
    last = _whole(args.last, REFUSED_RANGE)
    if not ids.is_id(args.chat_id, "cht"):
        raise Refused(REFUSED_CHAT_ID)
    if args.session_id is not None and not ids.is_id(args.session_id, "ses"):
        raise Refused(REFUSED_SESSION_ID)
    if args.records == "messages" and args.session_id is not None:
        raise Refused(REFUSED_MESSAGES_BY_SESSION)
    resume_at = None
    if args.resume_at is not None:
        if args.session_id is not None or args.records != "events":
            raise Refused(REFUSED_RESUME_SCOPE)
        resume_at = _resumption(args.resume_at)
    if not os.path.isdir(args.store):
        raise Refused(REFUSED_STORE)

    # Read-only (decision 0002, D1): no lock, no sweep, no write, ever.
    store = ChatStore(args.store, read_only=True)
    try:
        try:
            store.read_chat(args.chat_id)
        except NotFound:
            raise Refused(REFUSED_NO_CHAT)
        # A session is addressed by --session or by --resume-at, never both.
        addressed = args.session_id if resume_at is None else resume_at[0]
        if addressed is not None:
            try:
                store.read_session(args.chat_id, addressed)
            except NotFound:
                raise Refused(REFUSED_NO_SESSION)
        if args.records == "events":
            return _events(store, args.chat_id, args.session_id, first, last, limit,
                           resume_at)
        if args.records == "lifecycle":
            return _lifecycle(store, args.chat_id, args.session_id, first, last, limit)
        return _messages(store, args.chat_id, first, last, limit)
    except (StoreError, OSError, ValueError, LookupError, TypeError):
        # Whatever the reason, it is not printed: an exception's text names
        # paths and records (contract P4's boundary, and review finding R3's).
        raise Refused(UNREADABLE, status=3)


def main(argv=None, out=None, err=None):
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    try:
        records, more = retrieve(sys.argv[1:] if argv is None else argv)
    except Refused as refused:
        err.write(refused.words + "\n")
        return refused.status
    for record in records:
        out.write(_line(record) + "\n")
    out.flush()
    if more is not None:
        err.write("truncated: the bound of %d record(s) was reached and more are "
                  "preserved; %s\n" % (len(records), more))
    return 0


if __name__ == "__main__":
    sys.exit(main())

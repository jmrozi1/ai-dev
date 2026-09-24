# Decision 0007: one read-only command prints preserved evidence, and derives nothing

Status: recorded by #88 (checkpoint `expose-bounded-diagnostic-access`). The
scoping is the orchestrator's decision of 2026-09-23. Scope: Dory-wrangler v0.1.

## Why

Contract 8.5 requires a store to "retrieve diagnostic events by `chat_id`,
optionally by `session_id` and `sequence` range, with a bounded result count",
and P4 makes that bounded out-of-band retrieval the only diagnostic surface v0.1
has: "a retrieval, not a dashboard, and it derives nothing: it returns preserved
records. A view that interprets, aggregates, or summarizes them is #82."

`ChatStore.read_diagnostic_events` already satisfied 8.5 as a library, but only
a program could reach it. #88's question -- after a session, what event types
occurred, which were rendered, which were unknown, and where the stream
stopped -- had no answer a person could get without writing code.

## The command

```
python3 dory-wrangler/diagnostics.py STORE CHAT_ID [--session SESSION_ID]
    [--from N] [--to N] [--limit N] [--records events|lifecycle|messages]
```

It prints preserved records, one JSON object per line on standard output, each
exactly the record the store holds:

| `--records` | What | `--from`/`--to` |
| --- | --- | --- |
| `events` (default) | the chat's `diagnostic_event` records, optionally of one `--session`, in the store's order (session id, then sequence) | event `sequence` |
| `lifecycle` | each session's `agent_session`, then its `launch_result`, then its `session_observation` records, in the store's order; of `--session` or of every session of the chat | line of this listing, from 1 |
| `messages` | the chat's `message` records | message `sequence` |

`events` is P4's retrieval itself. The other two exist because two of #88's four
answers live in other record kinds: "which were rendered" in a `message`'s
`source_event_id`, and "where the stream stopped" in the session's `state`, its
transitions and its observations. Each mode prints one record kind's records
(`lifecycle`, three kinds, one after another per session) and **no mode joins
one kind to another**.

### The derive-nothing rule

The tool prints what is preserved and nothing it computed about it: no counts,
no grouping, no per-type tallies, no "rendered by" join, no ranking, no
interpretation, no summary line, no filtering by reading or type. What a reader
concludes is the reader's. `tests/test_diagnostic_access.py`
(`TheMinimumFromTheToolAlone`) is that reader: it runs the tool as a separate
program, reads only its standard output, and derives #88's four answers for the
development transport in both profiles and for the Codex model -- including
`unrecognized`, `malformed`, non-UTF-8, non-text and rendered events, a one-shot
and a stream session, and sessions that ended `completed` and `unknown` -- and
compares them with what the launchers were given. The derivation is in the
test, never in the tool.

### Verbatim, and safe on a terminal

Each line is the record as `json.dumps(record, sort_keys=True,
ensure_ascii=True)`: parsing a line gives exactly the stored record. `raw.body`
is as stored, so bytes that were not UTF-8 stay base64; every character outside
printable ASCII -- an escape sequence an agent wrote included -- is a JSON
escape, so nothing raw reaches the terminal.

### Bounded, and saying so

Every mode returns at most `--limit` records: default
`store.DIAGNOSTIC_PAGE_DEFAULT` (100), capped at `store.DIAGNOSTIC_PAGE_MAX`
(1000), the store's existing constants, so a caller cannot turn a retrieval into
a feed. When the bound held records back, one line on standard error says so,
names the address of the next record, and gives the arguments that ask for it:

```
truncated: the bound of 100 record(s) was reached and more are preserved; the next is session ses_... sequence 101; ask again with --session ses_... --from 101
```

The statement is on standard error so that standard output stays records only.
The library and the tool share one implementation: `read_diagnostic_events` is
`ChatStore.read_diagnostic_page(...)[0]`, and the page's second value is the
address of the first record the bound held back. So addressing, the bound and
the temp-file filter cannot differ between the two.

### Read-only, and not served

It opens `ChatStore(root, read_only=True)` (decision 0002, D1): no store lock,
no temp sweep, no directory created, no write. It therefore works while
`run_shell.py` serves the same store and while nothing does; the suite hashes
the whole store tree before and after every invocation, against a live server
and a stopped store. It lives beside `validate_store.py`, outside the package:
nothing in `src/dory_wrangler/` imports it, the served modules load without it,
and no route serves diagnostics (all pinned).

### Refusals

In fixed words on standard error, exit `2`, never carrying a path, a record or
an exception's text (the served boundary's rule, review finding R3): not a store
directory; not a chat identifier; no such chat; not a session identifier; no
such session; a `--limit` that is not a whole number of at least 1; a
`--from`/`--to` that is not; `--session` with `messages`; any other argument
error. A store that cannot be read exits `3`, saying so and that
`validate_store.py` gives the reason.

## Alternatives rejected

* **An HTTP route or a page.** P4 keeps diagnostics out of the chat, and the
  shell's promise that nothing it serves carries worker internals (#86, R3)
  would have to be broken for it.
* **A summary mode** -- counts per type, a "rendered" column, a last-state line.
  That is the view #82 owns; the minimum is shown answerable without it.
* **Truncation as a record on standard output.** It would put something that is
  not a preserved record among the records.

## Residuals, named

* A chat-wide `events` retrieval orders sessions by session id, not by time, and
  continues one session at a time: the statement names the next session and
  sequence, and the sessions after it are asked for by `--session`; `lifecycle`
  lists every session id.
* `lifecycle` lines are positions in the store's order of the moment. A record
  written between two invocations -- or one dated earlier by a clock that stepped
  back -- moves the lines after it.
* Run while a server writes, one invocation reads each file once; a record
  published a moment later is simply not in that answer.

## Evidence

`tests/test_diagnostic_access.py`: B (`TheCommandLineRetrieval`,
`WhileTheShellServesTheStore`, `NotReachableFromTheServedApplication`), C
(`TheMinimumFromTheToolAlone`), D6 (`AStagedDirectoryIsNeverReturned`).

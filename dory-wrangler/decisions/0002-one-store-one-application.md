# Decision 0002: one store, one chat loop, one seam, one served application

Status: recorded by #88 (checkpoint `converge-the-two-implementations`).
Scope: Dory-wrangler v0.1. Supersedes nothing in 0001; it narrows "the store"
and "the shell" in 0001 to the converged ones.

#86 and #87 were built in parallel from the same contract and each shipped its
own durable store and its own entry point. This records, per component, which
side is kept, the evidence for it, which accepted properties the choice keeps,
and which properties the *other* side had proven that therefore had to be
proven again on what was kept. Neither side is discarded wholesale: the store
and the served application are #86's, the seam, the launchers and the chat loop
are #87's, and the loop was rewritten onto #86's store.

The re-proof of every accepted property, test by test, is in the rail handoff
`issue-88-converge-implementations`; this file carries the decisions and the
reasons, not the table.

## What exists now

| Component | Module | From |
| --- | --- | --- |
| Durable store, the only writer of every record type | `src/dory_wrangler/store.py` (`ChatStore`) with `atomic.py`, `contract.py`, `ids.py` | #86 |
| Chat loop and lifecycle: binding rule, launch, delivery, drain, stop, abandon, restart | `src/dory_wrangler/session_manager.py` (`SessionManager`) | #87, rewritten onto `ChatStore` |
| Launch seam | `src/dory_wrangler/launch_boundary.py` | #87, unchanged |
| Launchers and their registry | `src/dory_wrangler/launchers/` | #87, unchanged |
| Wiring of a store to a launcher | `src/dory_wrangler/wiring.py` (`open_harness`) | #87's `app.py`, now over `ChatStore` |
| Served application | `src/dory_wrangler/webapp.py`, `service.py`, `serve.py`, `run_shell.py` | #86, now sending turns through the chat loop |
| Refusals | `src/dory_wrangler/errors.py` | #86's store errors plus #87's four lifecycle refusals |
| Tests | `tests/`, one runner `tests/run_tests.py` | both suites, merged |

Removed: #87's `harness/store.py`, `harness/identity.py`, `harness/app.py`,
`harness/errors.py`, `harness/tests/run_tests.py`, and #86's `turn_listener`.
The structural check that exactly one store class and one HTTP server remain is
`tests/test_convergence.py`.

## 1. The store: #86's `ChatStore`

**Kept because** the durable-store weight and the durable-store evidence are
both on #86's side, and #87's store said so of itself.

* #87's `Store` was an append-only JSON-lines log with last-write-wins per
  record identity, materialised into memory on open. Its docstring: *"this one
  exists so that #87's guarantees are exercised against something that really
  survives a process ending, and #86 may replace it wholesale."* It validated a
  record's type and version and nothing else, fsynced one append, and had no
  crash-atomicity argument for a torn line.
* #86's `ChatStore` is the accepted durability design of decision 0001 and
  carries the evidence for it: atomic publication at seven named fault points
  proven by real kills; every record put through the executable contract on
  write *and* on read; every path component validated module-wide, asserted
  over the module's own AST; every one of the contract's 53 violation codes
  accounted for with an unaccounted-code detector; a 75-mutation sweep with
  every green adjudicated.

**Accepted properties this keeps** without re-deriving them: #86's atomicity
under kill, restart recovery, message and event contiguity, diagnostics
unreachable from the transcript reader, path-component validation, the code
accounting. They are re-proved by #86's own tests running on the converged tree,
where the store is the same module plus one method.

**What #87's store proved that had to be proven again on this one**:

* *A replayed `(session_id, sequence)` is already stored, not a new record.*
  `ChatStore.append_diagnostic_event` does this by exclusive creation and
  returns `created=False`, and #87's replay tests now run against it. It is
  **stricter**: a replay whose `raw` or `source` differs from what is stored is a
  contradiction and raises `StoreCorrupt`, where #87's store silently kept the
  first. Recorded as a behaviour change.
* *A second `Store` over the same path reads the same chat back (D1).* #87's
  reopen tests now open a second `SessionManager` over the same `ChatStore` root.
* *The section 8 queries the lifecycle turns on* -- non-terminal sessions, open
  bindings, a session's launch request and launch result, a session's last event
  sequence -- are answered by `list_sessions`, `read_launch_requests`,
  `read_launch_results` and `next_event_sequence`.
* *A delivery's acknowledgement is recorded after the call.* #87 rewrote the
  whole `delivery_request`; #86's packets are created once and never replaced,
  so the answer had nowhere to go. **The store gained one write**,
  `record_delivery_acknowledgement`: the one field of the one packet type that is
  filled in later, filled in once, `true` or `false` only, located by the
  record's own id, and published by atomic replace of the file that holds that
  record. It is in the public-write enumeration (four variants crossed with all
  fourteen reachable session shapes) and in the publisher list, and each of its
  refusals has a test.

**Behaviour that changes with the store** and is listed in the handoff: a
`launch_result` no longer stores `null` optional fields (contract-equivalent);
a binding is released in the same write as the terminal transition, so #87's
crash window between the two is gone; a handle enters a session only in the
write that enters `running`, so #87's N1 window -- `launching` *with* a handle --
is structurally unwritable, and the shape a crash leaves instead is `launching`
with an accepted `launch_result`; a record the contract rejects on its own is
unreadable, so a hand-damaged packet stops re-attachment of that one chat
instead of being routed.

## 2. The chat loop: #87's `SessionManager`, rewritten onto `ChatStore`

**Kept because** #86 has no lifecycle manager at all -- its shell recorded a
user turn and notified a listener -- and #87's is the one reviewed and accepted
for one agent per chat, both continuation modes, F1, F2 and N1.

**Rewritten, not wrapped.** It writes nothing itself: every record goes through
a public `ChatStore` method, so no record has two writers. Consequences:

* **Merged guards.** #87's loop kept its own copies of contract 5.2's owner
  table and of the addressing observation kinds, and refused with `NotPermitted`
  / `LaunchBoundaryError` before writing. Both copies are gone. The store's
  `append_transition` checks the owner table *and* the precondition table *and*
  compares against the state the caller read; the store's
  `append_session_observation` refuses an addressing kind unless the launcher
  *issued* a handle, which is stronger than #87's "the session carries one".
* **Relocated writes.** Binding release, handle-with-`running`, and session plus
  binding creation are each one store write instead of two or three.
* **Added guards**, each enumerated in the handoff: `_classify_launch` restates
  a `LaunchResult` through its constructor, because a launcher can mutate the
  object after building it; `_deliver_turn` asks the store's own delivery
  preconditions *before* recording the user's turn, so a turn the store would not
  deliver is never left in the chat unoffered; a launcher-mutated `DeliveryAck`
  records nothing; re-attachment at start skips a chat whose turn lock another
  live process holds, and a chat whose records cannot be read.
* **A per-chat turn lock.** #87 held a per-chat in-process lock for a whole
  turn. The loop now holds an `flock` on `chats/<id>/.turn-lock` for each user
  action, re-entrant within a thread, so threads of the one serving process are
  serialised per chat and a launcher that calls back into the loop is refused
  rather than deadlocked. A process that dies releases it; nothing measures
  time. *Corrected in place:* this bullet used to say "#86's store is shared
  safely by more than one process" and presented the lock as what made that
  safe. That was false; see section 5.
* **Ids and clock are the store's.** #87 injected an id factory and a clock so
  its stores were byte-comparable run to run; the store mints both, so those
  parameters are gone rather than accepted and ignored. Nothing asserted
  byte-stability except #87's helper name, and mode invariance compares
  transcripts, which are unaffected.

**What #86 proved that had to be proven again with the loop in the path**:
restart recovery through the served application with the history *answered by
the loop* rather than written in by the test, and the transcript and list
payloads carrying no worker internals when a real session, binding, handle and
preserved output sit behind them.

## 3. The seam and the launchers: #87's, unchanged

Only #87 has a seam. `launch_boundary.py` and `launchers/` moved into the package
with their imports changed and nothing else. Swappability, stateless handle
addressing, the internal-bridge model and the launcher-author obligations are
#87's accepted evidence and are re-proved by #87's tests on the converged loop
and store. The launcher-author obligations are in `launch-boundary.md`.

One import-graph probe changed: the chat and session layers' sources may not
read the environment, and the converged core brings two reads with it -- #86's
fault hook and its validator-path override. Neither is launcher configuration.
The probe removes exactly those two reads and a new test holds the set of
environment reads in the core closed at those two.

## 4. The served application: #86's shell, over the chat loop

**Kept because** only #86 serves anything: #87's `app.py` built a harness and
served nothing. #86's shell carries the accepted route-set boundary, the page
with no external asset, the fail-closed rendering, and the restart tests over a
real process.

**Changed**: `POST /api/chats/<id>/messages` sends the turn through
`SessionManager.send_turn` and returns once the agent's answer for that turn is
durable; `POST /api/chats/<id>/abandon` is new (decision 0003); the chat loop's
refusals reach the browser as fixed sentences with no identifier, state name or
lifecycle word in them, flagged `"refused": true`; a launcher that misuses the
seam answers 502; `serve.py` takes `--launcher` and `--launcher-options`; and
re-attachment runs once when the server is built, before the first request --
after the store-level lock is taken and the port is bound (section 5), so a
shell that cannot serve re-attaches nothing.

The default launcher is `dev-local` with profile `one_shot`, the shape of the
one proven internal path. The test shell runs `scripted-stub`, so no test starts
an operating-system process through the shell.

## 5. One serving process per store (corrected in place: decision D1)

**The claim that was here, and why it was false.** This decision said that
#86's store "is shared safely by more than one process", and convergence's
`TheTurnLockHoldsAcrossProcesses` tests were offered as the evidence. The
independent review of convergence (finding R1) showed otherwise, on the
product, with real processes:

* a second `run_shell.py` started on the same store re-attached before it bound
  its port, so it turned the first server's live, idle `running` agents
  `unknown` and then exited on "address already in use"; with the real
  `dev-local` launcher the user's Abandon then orphaned the first agent process
  and the next turn started a second agent for the same chat;
* opening a `ChatStore` swept `.tmp-` files across the whole root, deleting
  another live process's writes in flight: in one 40-turn run 33 turns failed,
  sessions were stranded in `running`, `launching` and `pending`, and one chat
  directory lost its chat record, after which the application would not start.

The per-chat turn lock serialised *turns*; it never covered a second process's
start-up, its sweep, or an idle live agent. The two-process tests were green
because they opened the second store while the first was parked with no write
in flight. v0.1 never required a multi-writer store -- the release is one user
and one shell -- so the truthful fix is to enforce that the store is not
shared, not to build the property the sentence claimed.

**The rule that replaces it.**

* **Exactly one process serves a store.** It holds an exclusive, non-blocking
  `flock` on `<root>/.store-lock` (`atomic.own_store`), taken **before** anything
  is swept, re-attached or written, and held for the life of the server. The
  kernel releases it when the process dies, so a killed server leaves no stale
  lock and nothing measures time.
* **Nothing sweeps or writes without it.** Every write `ChatStore` makes goes
  through five gated primitives (`_publish_new`, `_publish_replace`,
  `_publish_tree`, `_make_dirs`, `_lock`), each of which takes the store-level
  lock first; the chat loop's turn-lock file is created only after the same
  check. Temp files are swept when a process *takes* the lock, which is the one
  moment no write can be in flight. A second process that tries to serve or
  write fails closed with `StoreInUse` having changed nothing; `run_shell.py`
  exits with status 3 and says so.
* **The served application takes the store, binds, then re-attaches**
  (`build_server`), so a shell refused the store or the port changes nothing.
* **Read-only tooling still works.** `ChatStore(root, read_only=True)` takes no
  lock, sweeps nothing, creates nothing and refuses every write with
  `ReadOnlyStore`; `validate_store.py` opens the store that way and runs against
  a live server.
* **Within the one process** the store-level hold is shared by every
  `ChatStore` over that root and counted, and the per-chat turn lock remains,
  for threads and for a launcher that re-enters the loop.
* Decision 0001's remark that `os.link` makes message-sequence allocation safe
  "between processes with no lock" remains true of the primitive; the product
  no longer relies on it across processes, and it is proven between threads.

Tests whose expectation changed with this rule, each deliberately and not as a
weakening, are listed in the rail handoff `issue-88-convergence-remediation`.
The rule is surfaced to the human as overridable: making the store genuinely
multi-writer would be new architecture for a property v0.1 does not require.

## Not decided here

* Whether Stop must reach a turn in flight -- unchanged, still the human's.
* Whether the shell needs an exit from a `running` session it can no longer
  drain. See decision 0003's last section: it is reachable, it is measured, and
  providing it would be a second lifecycle action, which the convergence rail did
  not authorise.

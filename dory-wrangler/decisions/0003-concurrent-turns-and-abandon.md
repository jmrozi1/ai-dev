# Decision 0003: a concurrent turn is refused before it is recorded, and Abandon is the one lifecycle action

Status: recorded by #88 (checkpoint `converge-the-two-implementations`). Both
behaviours were decided by the orchestrator before convergence; this records how
they are implemented, what proves them, and how to change the first. Corrected in
place by the in-flight refusal rail (re-review N1, N2): a turn or Abandon that
meets a turn **in flight** is refused, not queued or deferred, and v0.1 does not
interrupt a turn in flight -- section 5.
Scope: Dory-wrangler v0.1.

> **v0.1 limitation.** Once a turn is dispatched, v0.1 lets it finish. While it
> is in flight, a second send and the Abandon action (and the loop's Stop) on
> that chat are refused at once with nothing recorded. Nothing interrupts the
> turn in flight; stopping or interrupting an active turn is later supervision
> work (#83). A Stop that could not reach a turn is never recorded or shown as
> `terminated`.

## 1. Concurrent turns: refuse before recording

**The two sides disagreed.** #86's shell recorded a user turn unconditionally and
refused nothing. #87's chat loop refuses a turn the chat's agent cannot take --
a second agent under `fresh_binding`, or any agent not `running` under
`persistent` -- **before** the user's message is written. Since the turn-floor
correction (#85, `4ff8b63`) both produce contract-valid stores; #87's re-review
verified its refusal precedes every write at code level and by measurement.

**Default kept: #87's.** Nothing is recorded as a user turn that was never
offered to an agent. A turn is "offered" when a session is opened on it (a
launch attempt, including one that fails and is recorded as `launch_failed`) or
when a delivery is recorded for it. The user sees the refusal and their text is
still in the composer; the chat's history is unchanged.

**How it is implemented.**

* The served application records a user turn only through
  `SessionManager.send_turn`; `ChatService.send_user_message` no longer writes a
  message itself.
* The decision point is one method, `SessionManager._refuse_concurrent_turn`,
  called from the two places a turn can be refused for concurrency.
* The check that decides it asks exactly what `ChatStore.create_session` would
  refuse on -- a non-terminal session or an open binding -- so the loop cannot
  proceed to record a turn the store would then refuse to open a session for.
  On the delivery path the loop asks the store's own delivery preconditions
  (`ChatStore._require_deliverable`, read-only) before recording the turn.
* The check and the writes that follow run under the chat's turn lock, an
  `flock` held for the action, so a second thread or a second process serving the
  same store cannot interleave between "is this chat served?" and "record the
  turn and open its session". A turn that finds the lock held is **refused**
  there, before anything is written -- it does not wait for the lock (section 5).

**Evidence.** `tests/test_binding.py` (a second turn to a live `fresh_binding`
agent, ten concurrent threads resolving to one agent, a re-entrant launcher),
`tests/test_adversarial.py::TestClosedContractFindings::test_a_second_turn_sent_while_the_agent_is_running`
(through `ChatService`, the refusal leaves `export_records()` unchanged),
`tests/test_convergence.py` (two processes over one store; the refused turn
through HTTP leaves no record).

**How to flip it to #86's record-then-refuse.** Change
`SessionManager._refuse_concurrent_turn` to

```python
self._store.append_user_message(chat_id, text)
raise ConcurrentLaunchRefused(self._refusal_reason(session))
```

and nothing else. The refusal still reaches the browser as a 409 with
`"refused": true`; the shell would then re-render the chat to show the recorded
turn. The resulting history -- user, user, agent against one instruction packet
-- is contract-valid, which `test_a_second_turn_sent_while_the_agent_is_running`
keeps proving by writing exactly that shape through the store, and
`tests/test_convergence.py::TheConcurrentTurnPolicyIsOnePoint` runs the flipped
policy end to end and validates the store. Tests that assert "a refused turn
leaves nothing behind" are the ones that would then change expectation, and
they would be the visible record of the flip.

## 2. Abandon: the one lifecycle action the shell exposes

**Why it is in scope.** A restart that finds a live or `unknown` session must
leave the user a way out through the shell. Contract 5.4 re-attaches once at
start; a launcher that cannot resume -- every launcher this product ships -- leaves
the session `unknown`, and `unknown` refuses every new turn. Contract 5.2 makes
the exit from `unknown` a user action, `unknown -> abandoned`. Without it the
chat is stranded for the life of the store, which fails the release's reopen
requirement.

**What is provided, and nothing more.**

* `POST /api/chats/<id>/abandon` calls `SessionManager.abandon`, which takes the
  turn lock -- or, if a turn is in flight, is refused (section 5) -- and takes the chat's non-terminal session out of whatever state it
  is in, by contract-legal transitions only (decision D2, section 4 below). From
  `unknown` that is `unknown -> abandoned`, owned by the user with `user_action`
  evidence, as it always was. The store releases the binding in the same write
  as the terminal state. The response is the reopened chat.
* On the page, the action appears only as a button inside the notice that
  explains a refused send. There is no status indicator, no lifecycle panel, no
  session list, no poll: the page learns nothing about sessions except that the
  user's own send was refused.
* A refusal is a fixed sentence with no identifier, state name, or lifecycle
  vocabulary in it, so the boundary tests over every served body still hold.
* Re-attachment happens once, when the server is built, before the first request.
  No timer, no retry, no automatic recovery: the harness never abandons.

**Evidence.** `tests/test_restart.py::TestARestartWithALiveAgentLeavesTheUserAWayOut`
(a live persistent agent, a real SIGKILL, a restart, the session found `unknown`,
a refused send that records nothing, the abandon, and a new turn answered),
`tests/test_shell.py` (the route set now includes `/abandon` and nothing else
new; every served body, including both abandon responses and the refused send,
carries no worker internals), and `test_internal_bridge.py`, whose modelled internal launcher
resumes across a real restart and ends in `unknown` only when it cannot.

## 3. Resolved by decision D2: a `running` session the shell can no longer drain

*This section recorded an open question at convergence; it is kept, corrected,
because the measurement still describes how the state is reached. The
orchestrator's decision D2 resolved it, and section 4 is the rule.*

Measured during convergence and not resolved then, because resolving it would
have added a second lifecycle action and the convergence rail authorised exactly
one.

Some ways a turn ends leave the session `running` with the chat lock released
and nothing left reading it: a launcher that misuses the seam mid-turn (#87's F1
non-advancing page, a sequence gap, an end of stream from a one-shot launcher, a
replay that contradicts what is stored), and a restart on a launcher that *can*
resume, whose re-attachment succeeds and leaves `running` without draining. On a
`fresh_binding` launcher every new turn is then refused and `abandon` is refused
too, because contract 5.2 lets the user abandon only `unknown`.

#87's accepted exit from this state is the user's Stop (`stop_agent`), which is
still there and still tested. The shell does not expose it. What the served
application does today, pinned by
`tests/test_convergence.py::ARunningSessionTheShellCannotDrain`:

* the chat refuses turns until the application restarts;
* a restart on any launcher that cannot resume carries it to `unknown`, and
  Abandon then works;
* on a launcher that can resume, the chat stays refused after the restart too.

The choices were a shell Stop affordance (a second action), making the one
affordance stop a `running` agent before abandoning it, or accepting the restart
as the exit. The orchestrator chose the second, and then generalised it (D2):
the three bullets above no longer describe the product, and
`tests/test_convergence.py::ARunningSessionTheShellCannotDrain` now pins the
exit instead of its absence.

## 4. Every non-terminal state has the one action as its exit (decision D2)

The independent review of convergence (finding R2) found the same stranding in
two more states in a live process: `pending` and `launching`, left by a wall
clock stepping back between the writes of a launch (or by any store or OS error
there), with send, stop and abandon all refused until a restart. The rule now:

* **Exactly one lifecycle action**, `Abandon`, and it exits every non-terminal
  state. No second button, no timer, no inactivity inference, no automatic
  recovery; the harness never takes an exit on its own.
* **Contract-legal transitions only**, each checked by the store against
  contract 5.2's owner and precondition tables as it is written:

  | State | Route out | Transitions (owner, evidence) |
  | --- | --- | --- |
  | `unknown` | abandon | `unknown -> abandoned` (user, `user_action`) |
  | `running` | the user's `stop`, confirmed | `running -> terminated` (user, `stop_confirmed` observation); nothing is left to abandon, and that is success |
  | `running` | the user's `stop`, not confirmed -- including a launcher that raises anything at all or answers with something that is not a `StopAck` | `running -> unknown` (launcher, `stop_unconfirmed` observation, 5.3 cause 4), then `unknown -> abandoned` |
  | `launching` with an accepted `launch_result` | 5.4's resolution from the durable result, then the `running` rows | `launching -> running` (launcher, `launch_result`) |
  | `launching` with a failed `launch_result` | 5.4's resolution | `launching -> launch_failed` (launcher, `launch_result`) |
  | `launching` with an unknown `launch_result` | 5.4's resolution, then abandon | `launching -> unknown` (launcher, `launch_result`), `unknown -> abandoned` |
  | `launching` with no usable `launch_result` | 5.4's `reattach_failed`, then abandon | `launching -> unknown` (launcher, `reattach_failed` observation), `unknown -> abandoned` |
  | `pending` | 5.4's resolution of a launch never issued | `pending -> launch_failed` (harness, `harness_action`, `ref: null`) |

* **The action never calls `events`.** On a live agent that is quiet `events`
  blocks by design, and the exit must not.
* **A turn in flight is not interrupted, and the action is not deferred behind
  it.** *Corrected:* this bullet used to say the action waits for the turn in
  flight to finish; it did, and then recorded its `terminated` after the answer
  (re-review N2). The action is now refused while a turn is in flight (section 5),
  and the human decided Stop does not reach a turn in flight in v0.1. A launcher
  that calls the action back from inside an action on the same chat, on the same
  thread, is refused every route but `unknown -> abandoned`, which it always had.
* **Re-attachment preserves what it reads.** On a launcher that can resume, the
  page re-attachment reads may carry the agent's answer and its completion; it
  is preserved and acted on exactly as the drain would, so the chat is not later
  recorded as a user terminating an agent that had completed. This partly
  satisfies #88's later intake checkpoint.
* **Residual, stated.** A store record damaged by hand (for example a
  `launch_result` the contract cannot read) still holds its chat: the action
  reads it and fails closed (contract D3). No product write can produce it.
  And a wall clock that stays behind the records already written refuses every
  write on that session until it passes them; each retry of the action makes
  whatever progress the clock allows.

Evidence: `tests/test_convergence.py::EveryNonTerminalStateHasTheOneActionAsItsExit`
(one test per row, each checking the transitions written and that the chat takes
a new turn), `ReAttachmentPreservesWhatItReads`, and
`ARunningSessionTheShellCannotDrain`.

## 5. A turn in flight: refused, not queued or deferred, and not interrupted

**The decisions.** The human decided on 2026-09-15 that a concurrent turn while
an agent is running is *refused, not queued* -- a queue would bring ordering,
cancellation, editing and assumed-context semantics v0.1 does not need -- and
that *Stop does not reach a turn in flight in v0.1*: once dispatched, a turn is
let finish, and a Stop that could not reach it must never be recorded or shown as
`terminated`. The orchestrator decided that an Abandon meeting a turn in flight
is refused with fixed words and nothing recorded, rather than deferred.

**What was wrong.** The focused re-review of convergence found both violated by
one mechanism. Every user action took the chat's turn lock *blocking*, and the
served application runs each request on its own thread. A second send during a
turn waited for the lock and was then recorded and delivered after the first
answer -- queued, in both continuation modes (N1). An Abandon during a turn waited
too, and on a launcher that confirms stops then recorded `running -> terminated`
dated after the answer (N2).

**The rule now.**

* Every user action -- `send_turn`, `abandon`, `stop_agent` -- takes the chat's
  turn lock **without waiting**, in one place, `SessionManager._user_action`. If
  another thread or process holds it, the action raises `TurnInFlightRefused` (a
  `ConcurrentLaunchRefused`) before anything durable is written, in every session
  state and whether or not the chat has a session. The launcher is asked nothing.
* A send refused this way writes nothing at all, not even the chat's name from
  its first turn: that turn belongs to the request still in flight, which names
  the chat when it ends.
* Over HTTP both routes answer `409` with `"refused": true` and the fixed words
  `webapp.REFUSED_IN_FLIGHT`, which tell the user the answer in progress must
  finish first and that nothing was sent or changed. No identifier, state name or
  exception text.
* On the page, Enter does not submit while Send is disabled -- the same gate as
  the button -- so one tab does not provoke the refusal. The server's refusal is
  the guarantee; the key behaviour is not browser-verified.
* The same thread re-entering its own hold -- a launcher calling back into the
  loop during an action on the chat -- still acquires it, and meets the loop's
  own rules rather than this refusal. Those rules are **not the same for all
  three actions**, and an earlier wording of this bullet read as though they
  were (check-rail finding F1). What the code does:
  * a nested send reaches `_continue_turn`, which refuses anything that is not a
    `running` session on a `persistent` launcher and otherwise delivers;
  * `abandon` tests `held.nested` and is refused every route but
    `unknown -> abandoned`;
  * `stop_agent` has no `held.nested` test, so a nested Stop runs the ordinary
    Stop and, on a launcher that confirms stops, records `running -> terminated`.

  The `stop_agent` asymmetry is a recorded finding carried to #90 with the nested
  launcher-callback path it belongs to, and is not fixed here. It is not
  user-reachable: it needs an out-of-tree launcher that holds the
  `SessionManager` and calls back from inside its own `stop`, which contract 6.1
  puts outside the seam, and the outer action then fails closed.
* **Nothing interrupts the turn in flight.** No queue, retry, timer, polling or
  cancellation was added.

**Decision D2 still holds.** Every non-terminal state keeps the one action as its
exit (section 4): the turn ending makes the action available again. A turn that
never returns holds its chat until its process exits -- the kernel then releases
the lock with the descriptor -- and contract 5.4 re-attachment at the next start
carries the session to a state whose exit is the action: a `running` session on a
launcher that cannot resume becomes `unknown`, then `abandoned`; on one that can,
the user's confirmed stop gives `terminated`; a `launching` one resolves as in
section 4.

**Evidence.** `tests/test_convergence.py`:
`TheTurnLockHoldsWithinOneProcess` (a second send, and an Abandon and Stop, while
the first turn is parked; both changed expectation from waiting to refusal),
`ATurnInFlightRefusesEveryOtherUserAction` (every action in `pending`,
`launching`, `running` and `unknown`, with the lock held by another thread and by
another descriptor; a turn parked in `deliver`; both continuation modes over HTTP,
including that the refused send does not name the chat), and
`EveryNonTerminalStateHasTheOneActionAsItsExit::test_a_turn_that_never_returns_is_exited_by_process_exit_restart_and_the_action`.
`tests/test_shell.py::TestShellFlows::test_enter_does_not_send_while_a_send_is_in_flight`
holds the page's Enter gate from the served source.

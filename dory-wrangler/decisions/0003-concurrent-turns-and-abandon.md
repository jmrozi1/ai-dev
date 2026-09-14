# Decision 0003: a concurrent turn is refused before it is recorded, and Abandon is the one lifecycle action

Status: recorded by #88 (checkpoint `converge-the-two-implementations`). Both
behaviours were decided by the orchestrator before convergence; this records how
they are implemented, what proves them, and how to change the first.
Scope: Dory-wrangler v0.1.

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
  turn and open its session".

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
start; a launcher that cannot resume -- every launcher in this repository, and
the modelled internal bridge, which remembers nothing across a restart -- leaves
the session `unknown`, and `unknown` refuses every new turn. Contract 5.2 makes
the exit from `unknown` a user action, `unknown -> abandoned`. Without it the
chat is stranded for the life of the store, which fails the release's reopen
requirement.

**What is provided, and nothing more.**

* `POST /api/chats/<id>/abandon` calls `SessionManager.abandon`, which takes the
  turn lock, requires the chat's active session to be `unknown`, and appends
  `unknown -> abandoned` owned by the user with `user_action` evidence. The store
  releases the binding in the same write. The response is the reopened chat.
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
carries no worker internals), and #87's own `test_internal_bridge.py` restart
tests on the converged loop.

## 3. Open: a `running` session the shell can no longer drain

Measured during convergence and **not** resolved here, because resolving it would
add a second lifecycle action and the convergence rail authorised exactly one.

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

The choices are a shell Stop affordance (a second action), making the one
affordance stop a `running` agent before abandoning it (Abandon then sends `stop`
to a live agent), or accepting the restart as the exit for v0.1 because no
in-tree launcher resumes. That is a product decision, not a convergence one.

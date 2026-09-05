# Issue #55 — checkpoint 69: rotation dogfood

The accepted rotation surface, driven end to end against a **real provider session**.
This checkpoint adds **no mechanism**. It is the evidence, and the evidence includes a
defect that the mechanism's unit tests could not have found.

**Verdict: the rotation did NOT work end to end.** Steps A–E and G held exactly as
accepted. **Step F — the replacement continuing from durable state — failed against
the real provider**, for a structural reason recorded below.

---

## What was driven

A harness (not product code, not committed) composed the accepted production routes
exactly as a manager would: `ManagerController.launch` → `continue_session` ×8 →
`terminal_finalizer` → `evaluate_rotation_readiness` →
`ManagerController.replace_old_context` → `ManagerController.continue_from_durable_state`
→ `ManagerController.release_continued_context`.

Every authorization came from the accepted `authorize` over an `observe_scope` reading of
a real Git control plane; every session ran in the accepted `ai_dev_flow.claude_worker`
worker process against `claude-agent-sdk 0.2.152` on `/usr/bin/python3.12`. Nothing was
stubbed, faked, injected or simulated.

Scope: a purpose-made control-plane scope `dogfood/rotation`, rail `rotation-dogfood`
(`Status: running`, `Role: executor`), workspace a small real Git repository holding a
copied subset of this package's own modules. Rotation threshold configured to **2**;
D6 ceiling at its default **6**.

Total real provider spend: **USD 2.17** across 9 completed invocations.

## A. A real managed session, launched and bound

- predecessor session id: **`ba89a611-89db-43f6-96b9-509305c92e21`**
- pid/pgid **485341**, `bound`, SDK `0.2.152`, launch invocation `subtype: success`

## B. Real observed compactions, reaching the threshold

Two `compact_boundary` events, counted by `context_lifecycle.decode_lifecycle_event`
from `message.data` and deduplicated by `(session_id, uuid)`:

| # | session_id | uuid | trigger | pre_tokens | post_tokens |
|---|---|---|---|---|---|
| 1 | `ba89a611-89db-43f6-96b9-509305c92e21` | `856a7216-b154-4099-b075-498d8f8fceb5` | `manual` | 68260 | 4538 |
| 2 | `ba89a611-89db-43f6-96b9-509305c92e21` | `b0397fd9-d621-4678-90cb-1d31996decb1` | `manual` | 38916 | 5047 |

Reading at the threshold: `health: healthy-complete-from-session-start`, `observed: 2`,
`count: 2`, `rotationMarked: true` — *"2 observed compactions reach the rotation
threshold of 2; this session is marked for graceful rotation at a later safe boundary."*

**Two prerequisite scope limits are now closed.** The compaction prerequisite observed
its boundaries through the VS Code extension's CLI, outside the accepted worker
configuration. These two were observed **through the accepted worker**, over the worker
protocol, from the SDK the product actually uses.

**On the trigger.** `decode_lifecycle_event` deliberately drops `trigger` at the protocol
boundary, so the product never sees it. The values above are read from the **provider's
own transcript** (`~/.claude/projects/…/ba89a611….jsonl`), whose `compactMetadata` carries
`trigger`, and whose `uuid` matches the pair the product counted, event for event. That
cross-read is corroboration of the product's identity pairs, not their source.

### `trigger: "auto"` was NOT observed. It remains unproven.

What was attempted: a bounded, realistic workload — five real turns in which the agent
read four real modules (3,042 lines) in full and answered genuine comprehension questions
about them, which is the shape of work an executor actually does. No synthetic context was
manufactured; no artificial padding was used.

What was seen: the session reached **68,260 pre-compaction tokens** after those five
turns, and the provider raised **no automatic boundary**. Only the two controller-issued
`/compact` commands produced boundaries, both `manual`.

What would be needed: automatic compaction fires near the model's context limit, so
reaching it from 68k after five turns implies roughly **three times that workload —
fifteen-plus turns of full-file reading — in one session**. That is a soak, and at this
scale it is indistinguishable from the large artificial workload the compaction
prerequisite forbade and this rail forbids. It was therefore **not attempted**.

**This is an honest gap, recorded rather than worked around.** The manual trigger is
proven twice over; nothing in the counting, threshold, marking or rotation path reads
`trigger` at all, so no accepted behaviour depends on it.

## C. Safe boundary and a current terminal handoff

The finalizing turn ran with `terminal_finalizer(publish=…, bookkeeping=…)`. Its terminal
result carried exactly one handoff envelope; the controller published those bytes verbatim
**after `sender` returned**, i.e. after the provider turn had ended.

- publication (blob): **`970e58eca549d9c176e3c63c3661b64b3ff8f1fc`**, 2,775 bytes
- coordination commit `079710c60e5ef9253d3e4869ac2ee707f3bb36be`, trailer
  `Work-State: a93162953a9f75df5b3da37243b982679c408515`
- finalization state `terminal-handoff-established`, at **work boundary 8**

**Proof it was current for the boundary it was credited to.** The work boundary was 7
before the invocation and 8 after; the recorded finalization names work boundary **8**,
the session was still standing at 8 when readiness was projected, and the publication the
control plane served (`970e58ec…`) was byte-identical in identity to the one the
controller finalized. `evaluate_rotation_readiness` returned **`rotation-ready`** and said
so itself.

**The carried residual did not bite.** The bounded finalization retry was never built, so
a rail that cannot finalize cannot rotate. Finalization succeeded on its first attempt
here, so that residual was not exercised — neither closed nor worsened.

## D. Retirement, with the process group proven gone

`retire_old_context`, reached through `replace_old_context`: predecessor **`ba89a611…`**
stopped gracefully, exit code 0, `process_group_gone: true`, binding `unbound`.

Independently re-proven after the fact: pgid **485341** answers no liveness probe,
`/proc/485341` is absent, zero processes carry that pgid, and the controller holds no
handle for the session.

## E. Replacement launched and bound

- successor session id: **`323fbf58-3694-42f2-ad0f-3f6226d2c955`** — distinct from the
  predecessor, pid **486127**, binding `bound`
- predecessor's binding terminal (`unbound`) before the successor was reserved

## F. Continuation from durable state — **FAILED**

`continue_from_durable_state` resolved a correct brief and sent it, and the provider
refused the invocation:

```
ClaudeWorkerError: worker-fatal: ResultError: Claude Code returned an error result:
No conversation found with session ID: 323fbf58-3694-42f2-ad0f-3f6226d2c955 (exit code: 1)
```

### The cause, established rather than guessed

A replacement is bound by `_reserve_and_bind`, which builds a **launch** request
(`session_lifecycle.py:898`) and, by design, **sends nothing** — *"It sends nothing and
returns no result, because sending is not part of coming into existence."*
`replace_old_context` deliberately has no `send`, `prompt` or `markers` parameter, so the
successor's launch request is returned in `BoundReplacement.request` and **no production
route ever sends it**.

A provider conversation is created by a **launch** invocation, which sets
`session_id=<minted>` and `resume=None` (`claude_runtime.py:636-638`). The only route that
continues a replacement is `continue_from_durable_state` → `continue_session`, which
builds a **resume** request (`session_lifecycle.py:1067`) setting `session_id=None` and
`resume=<minted>` (`claude_runtime.py:640-641`).

So the successor's session id exists in the binding store and in the manager's registry,
and a worker **process** genuinely exists for it — but **no provider conversation with
that id was ever created**, and the only continuation route can only resume one.

Corroborated: the provider wrote a transcript for the predecessor
(`ba89a611….jsonl`, 494 KB) and for the launch smoke session, and **none for
`323fbf58…`** — the conversation never existed.

**Consequence.** On the shipped composition, D9's *"the replacement must continue without
the predecessor's transcript"* is **unreachable**: every replacement is bound into a state
from which the only available continuation must fail. This is a composition gap between
two individually accepted routes, which is precisely the class of defect a dogfood exists
to find and a unit test of either route in isolation cannot.

**No remediation is attempted here.** Recording it and stopping is what this rail
authorizes.

### The failure path itself behaved exactly as accepted

The failed continuation left the session `bound`, owned and continuable; nothing was
launched or stopped; no handoff was credited from a turn with no terminal result; and the
observation correctly degraded to `unhealthy-partial` with `rotationMarked: null` —
*"0 observed compactions is a floor rather than a count … so whether the rotation threshold
of 2 was reached is undetermined."*

`release_continued_context` then read the category **at the instant it acted**, found
`category-unprovable`, routed to `supervised_teardown`, and published a durable D8
human-attention record before returning — `stateChanged: true`, `explanation` carrying
`human_action` verbatim beside all six blocker keys. **This is checkpoint 68's
`category-unprovable` route firing for real, in production, on a genuine ambiguity rather
than a constructed one.**

## F′. Fresh-reader resolution — held

A separate process, holding a **new `BindingStore`, no registry and no predecessor
memory**, resolved from durable state alone (control plane + workspace + binding record):

- rail iteration `2619caab79e6c01118dfc4336e5931d823d5b30b`
- handoff `dogfood/rotation/rails/rotation-dogfood/handoff.md`, publication
  `970e58eca549d9c176e3c63c3661b64b3ff8f1fc`, work state
  `a93162953a9f75df5b3da37243b982679c408515`
- workspace head `a93162953a9f75df5b3da37243b982679c408515`

**All twelve `ContinuationBrief` fields identical to the brief actually sent, and the
749-character prompt byte-identical.** The fresh reader also read the published handoff
back and found a substantive executor handoff — outcome, evidence, unresolved work, exact
next action — written by the real agent.

*Limit, stated:* `continuation_brief` itself could not be re-invoked at check time, because
the successor's binding is now terminal and the function correctly refuses one. The
reproduction is therefore field-by-field from the same durable reads, not a second call of
the function.

## G. D6 accounting across the whole swap

| step | occupied / ceiling | occupants |
|---|---|---|
| before launch | 0 / 6 | — |
| after launch | 1 / 6 | `ba89a611…` |
| before rotation | 1 / 6 | `ba89a611…` |
| after rotation | 1 / 6 | `323fbf58…` |
| after continuation | 1 / 6 | `323fbf58…` |
| after release | 0 / 6 | — |

**Never above the ceiling, never transiently at two, never permanently consuming two
slots.** The predecessor's slot was released by the terminalization before the successor's
reservation was written, so the swap passed through N−1 and not N+1. `unprovable` was
empty at every step.

## Process accounting

Three worker process groups were started, all by this run, all proven gone:

| pid = pgid | role | escalation | in `/proc` | processes in group |
|---|---|---|---|---|
| 485244 | launch smoke | none — graceful | absent | 0 |
| 485341 | predecessor | none — graceful, exit 0 | absent | 0 |
| 486127 | successor | none — graceful | absent | 0 |

Independently verified after the run: no `ai_dev_flow.claude_worker` process exists on the
host, and no process anywhere has a working directory inside the fixture. No background
process was left alive and none outlives the run.

## Deliberately not done

No mechanism was changed, redesigned or remediated. `main` was not moved. No checkpoint
was accepted. Nothing under `skills/**` was read into scope, modified or activated. No
synthetic context was manufactured to force an automatic compaction. The two pre-existing
failures verified at checkpoint 56 were neither repaired nor counted.

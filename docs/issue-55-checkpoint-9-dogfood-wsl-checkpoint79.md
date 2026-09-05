# Issue #55 — checkpoint 79: the named checkpoint 9 dogfood, on WSL

The first dogfood on this ticket run on a host where `process_group_alive` is actually
evaluable, and the first that has held **more than one** managed provider session live at
the same instant. It adds **no mechanism** and changes **no product code**. Every route
driven is the shipped one; the driver that composes them is not product code and is not
committed.

**Read the limits section first if you read only one thing.** The result that most
undermines this document is that the D6 ceiling was proven against *fixture* records and
never against six live agents, and that the largest number of real managed sessions this
package has ever held at once is **two**.

---

## Environment, established first-hand

| | |
|---|---|
| host | Windows 11 + WSL Ubuntu, every invocation as `-u root` |
| Claude CLI | `/root/.local/bin/claude` **2.1.261** |
| worker interpreter | **`/root/aidev/host/venv/bin/python`**, Python **3.14.4**, `claude-agent-sdk` **0.2.152** |
| `PATH` hygiene | `/mnt/` stripped before every measurement; with it stripped, **there is no Linux `node` and no Linux `npm`** on this host. The `npm 10.9.2` a naive probe reports is Windows npm reaching through PATH interop. |
| workspace | WSL-native standalone clone `/root/aidev/cp79/ws` detached at **`910523378f86ae00a557a0dac272b12cfcf438d0`** (checkpoint 78), clean throughout, primary worktree, no active ticket claim |
| controller root | `/root/aidev/cp79/controller-root`, `packages/ai-dev-{executor,reviewer,orchestrator}/{.claude-plugin/plugin.json, skills/<role>/SKILL.md}` plus one prompt per role |

The worker spawns with `executable or sys.executable`, so running the driver under the
venv interpreter is what puts the SDK in the worker. That is stated rather than assumed.

## Provider budget, spent in full

**Eight real provider sessions, the hard bound, all eight spent. Never more than two
concurrent.** Every one is listed, with its process group and how it ended.

| # | session id | pgid | what it was | outcome |
|---|---|---|---|---|
| 1 | `3cf157ed-9eda-4b96-a323-d2fab9b46d08` | 15817 | run A attempt 1, launch 1 | held, then released when launch 2 failed |
| 2 | `5f07d2a4-4963-439e-9c94-01fb0bfd144c` | 15879 | run A attempt 1, launch 2 | **spent on my own bound**: `Reached maximum budget ($0.25)` |
| 3 | `130dc766-9ccf-4a04-bb1d-372f5a0a32de` | 16211 | run A attempt 2, launch 1 | **spent on my own bound**: `Reached maximum number of turns (3)` |
| 4 | `31c3ea85-a59b-487b-9e82-be056c79a30c` | 16630 | run A, **executor** on `dogfood-exec-1` | held live, released, `process_group_gone true`, graceful |
| 5 | `84890f98-2c32-4a48-a0f8-5a78a42139e6` | 16688 | run A, **reviewer** on `dogfood-rev-1` | held live, released, `process_group_gone true`, graceful |
| 6 | `325bc2bd-37fc-41b1-976a-d3781290618c` | 17075 | the condition-4 marker run | continued against an unreachable brief, stopped |
| 7 | `1d233750-d431-4be1-9435-5eadc59a55a3` | 17723 | rotation predecessor | **spent on a harness defect of mine**; see item 6 |
| 8 | `97dff1e9-0d32-4ea3-b97a-42cfd29a01f7` | 18500 | the reachable-brief continuation | continued, stopped |

Three of eight — **numbers 2, 3 and 7** — were consumed by my own bounds and my own
harness, not by the product. That is the direct cause of item 6 being incomplete, and it
is my defect, not the package's.

Each session wrote a provider transcript on disk under **its own minted id and no other**
(25.9 KB – 74.4 KB, 16 – 41 records). That is what makes them real rather than asserted.

### Termination, proven for every process

Proven twice for every one of the eight process groups: by the shipped
`process_group_alive`, and by an **independent `/proc` walk in a separate process that
holds no registry and no binding store and was never told a pid by the driver**.

```
pgid 15817 alive=False /proc=absent    pgid 16688 alive=False /proc=absent
pgid 15879 alive=False /proc=absent    pgid 17075 alive=False /proc=absent
pgid 16211 alive=False /proc=absent    pgid 17723 alive=False /proc=absent
pgid 16630 alive=False /proc=absent    pgid 18500 alive=False /proc=absent
independent /proc walk for anything under /root/aidev/cp79 or running ai_dev_flow: []
```

**One process outlived its owner and it was mine.** When the rotation harness raised
(item 6), worker pgid **17723** was left running with no owner. Nothing further was run
until it was proven gone: ownership was established from `/proc/17723/cwd` =
`/root/aidev/cp79/ws` and its parent chain before anything was signalled, other lanes'
Claude processes were enumerated and left alone, and the group was then confirmed absent
by `process_group_alive` **and** an independent walk. No timeout, no exit 137/143 and no
lost process contact occurred anywhere else.

---

## 1. Concurrent real sessions — PROVEN, peak 2

`python -m ai_dev_flow.role_driver_dispatch`, unmodified, three stated launches:

```
held 1: session 31c3ea85… rail dogfood-exec-1 role executor pid/pgid 16630/16630 occupancy 1 / 6
held 2: session 84890f98… rail dogfood-rev-1  role reviewer pid/pgid 16688/16688 occupancy 2 / 6
all held at once: 2 session(s) -> 2 / 6
peak live occupancy: 2 / 6
refused: rail dogfood-rev-1 as reviewer: continuation-refused: authorization answers
         'continue' for rail 'dogfood-rev-1'; this door may only start a fresh session
released: session 84890f98… pgid 16688 binding unbound process group gone True graceful True
released: session 31c3ea85… pgid 16630 binding unbound process group gone True graceful True
live occupancy after release: 0 / 6
```

An **executor-role** and a **reviewer-role** managed session, live at the same instant,
each under its own role package, counted by the controller that admitted both.

### The independent walk saw them simultaneously, by pid

A separate watcher process sampling `/proc` every 200 ms recorded **16 consecutive
samples across a 3.0-second window** in which both process groups were live at once:

```json
{"t": 1788604789.136,
 "procs": [{"pid":16630,"ppid":16399,"pgid":16630,"cwd":"/root/aidev/cp79/ws",
            "cmdline":"/root/aidev/host/venv/bin/python -m ai_dev_flow.claude_worker"},
           {"pid":16688,"ppid":16399,"pgid":16688,"cwd":"/root/aidev/cp79/ws",
            "cmdline":"/root/aidev/host/venv/bin/python -m ai_dev_flow.claude_worker"}],
 "process_group_alive": {"16630": true, "16688": true}}
```

Two **distinct** process groups, the **same** parent (the one driver process, 16399), the
**same** workspace, both answering `process_group_alive` true. Sample census across the
run: 22 samples at zero workers, 18 at one, **16 at two**.

The third stated launch was refused *while the other two kept running*, which is what
makes the driver's "a refusal on one rail says nothing about another" observable rather
than merely written down. The refusal reason is worth recording: the rail already carried
a live binding, so the authorizer answered `continue`, and this door refuses to continue —
`continuation-refused`, not `binding-duplicated`.

## 2. The checkpoint-49 obligation — CLOSED

`process_group_alive` was called on every session's process group **before** termination
(true, from the driver's own controller *and* from the independent watcher process) and
**after** termination (false, from both), and corroborated by a `/proc` walk that found
zero processes in each group and no `/proc/<pgid>` directory.

This is the first time on this ticket that the reading has been taken over **more than one
dispatched session**, and the first time it has been taken on a host where the function is
evaluable at all. On Windows it was unevaluable by construction. **It is settled on this
host, at a multiplicity of two.**

## 3. The D6 ceiling — PROVEN, and the counter-intuitive shape confirmed

Both shapes were driven. The rail's warning is exactly right and is now evidence.

**3a — six `reserved` records, each on a DISTINCT rail.** Occupancy read straight off the
store: `occupied 6, ceiling 6, occupants [6 ids], unprovable []`. A seventh launch stated
on the distinct rail `d6-rail-7`:

```
occupancy on entry: 6 / 6
refused: rail d6-rail-7 as executor: not-authorized: the accepted authorization predicate
         refuses this launch: concurrency-ceiling-reached
```

**Nothing was reserved, spawned or sent** — zero worker processes, and the store still held
exactly the six records it started with.

**3b — six `bound` foreign records, the obvious fixture, proves the wrong thing.** The same
six rails, the same seventh launch, the records `bound` instead of `reserved`:

```
occupied 0, occupants [], unprovable [all six ids]
occupancy on entry: not established (ownership-unprovable)
refused: … concurrency-count-unprovable
```

`slots.unprovable` short-circuits ahead of the ceiling test at `authorization.py:453-468`,
and an unprovable session is never added to `occupants`, so six bound foreign records read
as `occupied == 0`. A dogfood built on that fixture would have reported a ceiling it never
reached. Reusing one rail six times trips `binding-duplicated` first and never reaches
either test.

**A live instance of 3b, produced by accident and worth more than the fixture.** My
rotation harness crashed leaving session `1d233750…` `bound` in `bindings-D` with its
process gone and no controller holding its handle. That store now reads
`concurrency-count-unprovable` for real. The fail-closed direction is the one it fails in.

## 4. THE CONDITION-4 MARKER — **POSITIVE, live, and it did not fire spuriously**

Checkpoint 72's enforcement had never caught anything real. **It has now**, and both
polarities were driven against the live provider.

### 4a — a brief that genuinely could not be reached

The rail `marker-rail` and its handoff live in a control plane **outside** the workspace.
The worker's options set `cwd` to the workspace and `add_dirs: []`, so a workspace-rooted
tool set has no route to it. Nothing was engineered to force the outcome: the handoff was
published by the product's own `publish`, at publication
`9ad443778d48280194602a60aa6489403f6d77b6`, work-state
`910523378f86ae00a557a0dac272b12cfcf438d0`, and the brief named it.

```
state:  continuation-brief-unreachable
reason: continuation-brief-reported-unreachable
markers: {"AI-DEV-BRIEF-UNREACHABLE": true}
num_turns: 3   is_error: false   cost: USD 0.183547
```

What the session actually replied, verbatim:

> `AI-DEV-BRIEF-UNREACHABLE`
>
> I read the authorized rail at `.cp79/dogfood/cp79/rails/marker-rail/rail.md` (it exists,
> status running, role executor), then tried the published handoff at the sibling path
> `.cp79/dogfood/cp79/rails/marker-rail/handoff.md` — the read failed with "File does not
> exist," so publication `9ad4437…` is not reachable from this workspace and I am
> continuing nothing and stopping here.

That is a **report**, not an echo: it names the path it tried and the error it got. The
route refused to credit the turn, left the session bound, owned and continuable, and
emitted the next action a manager would act on.

### 4b — the discriminating half: a brief that IS reachable

The same route, the same marker in the same prompt, with the handoff inside the
replacement's tool root at `.cp79/dogfood/cp79/rails/rot-rail/handoff.md`:

```
state:  continuation-continued
markers: {"AI-DEV-BRIEF-UNREACHABLE": false}
num_turns: 3   is_error: false   cost: USD 0.0703265
```

and the reply proves it actually read the bytes rather than merely not complaining:

> This is rail **rot-rail** in dogfood/cp79, and it assigns me the **executor** role (the
> handoff's "next action" is stale — it presumed no rail identifier had been supplied, but
> this invocation supplies one).

**Both halves matter.** 4a shows the check catches a real failure. 4b shows the marker —
which is handed to every replacement in its own prompt, and which a quoting replacement
could trivially echo — did **not** fire on a session that had every opportunity to echo it.
Checkpoint 72's R2 asked how often a real replacement trips the false-positive path. On
these two live invocations: **once out of two, and correctly both times.**

The shipped detail sentence still states its own narrowness, and it is right to:
*"this route does not establish that the replacement read or understood the handoff, only
that it did not say it could not reach it."* 4b's reply happens to establish more than the
route claims; the route is not entitled to that and does not claim it.

## 5. Ambiguous binding — PROVEN, nothing routed and nothing executed

Two live `reserved` records on one rail (`amb-rail`), then a launch stated on that rail:

```
occupancy on entry: 2 / 6
refused: rail amb-rail as executor: not-authorized: … binding-duplicated
```

Zero worker processes started, nothing sent, the store unchanged. The controller **stops**;
it does not pick one of the two.

## 6. Rotation — **NOT COMPLETED. Reported as a negative, not engineered around.**

The rotation was attempted end to end and stopped at its **first** gate, on a real
observation about this host.

**A controller-issued `/compact` produced no compaction here.** Through the shipped
`continue_session` route, against the live provider on this host and SDK:

```
B. controller-issued /compact
{"subtype": "success", "is_error": false, "num_turns": 0, "cost": 0.0, "events": []}

lifecycle reading: {"health": "healthy-complete-from-session-start", "observed": 0,
                    "count": 0, "threshold": 1, "rotationMarked": false,
                    "detail": "0 of 1 observed compactions, counted from this session's start."}
rotation-marked sessions: []
```

Zero turns, zero cost, **no `compact_boundary` event**. With `rotation_threshold` set to 1
— D9 policy, human-owned and configurable — the session was never marked, so no rotation
boundary ever existed, so no retirement and no replacement could be authorized. This
differs from checkpoint 71, where controller-issued `/compact` on a Linux host produced
`manual` boundaries at 68k pre-compaction tokens. **What changed between the two is not
established here** — the host, the CLI version and the workload all differ — and guessing
would be worse than recording the observation.

Two further things went wrong, and both are mine:

1. My finalizer read the control plane between the publish and the push, so the shipped
   freshness rule correctly refused it: *"'main' has unpublished local commits ahead of
   origin/main."* The product reported `durable-publication-failed`, credited nothing, and
   left the context alive — **exactly the fail-closed behaviour `terminal_finalizer` is
   written for.** That is the product working; the ordering error was in my harness.
2. My harness then raised on the next read and exited without stopping the predecessor,
   leaking pgid 17723 (proven gone above) and a `bound` record with no owner.

That cost the seventh session and left one in the budget — not two — so no replacement
could be launched at all.

**What was salvaged, and what it is worth.** Item 4b above is the *continuation* half of
item 6, driven for real: a session resolved its work from **durable state alone** —
`continuation_brief` built from the binding, the rail observation, the published handoff
and a fresh workspace read, with **no predecessor transcript read, held or required** — and
the shipped route reported `continuation-continued`.

**What is therefore NOT established on this host:** that a session reaching the rotation
threshold hands off safely and is *replaced*. Retirement, the replacement launch, the
successor's distinct minted id, and the swap's D6 accounting were **not exercised here at
all**. Checkpoint 71 proved them on a Linux host; this checkpoint does not re-prove them,
and no fixture stands in for them.

## 7. Elapsed state and allowance status — observed from the real surfaces

Occupancy was read only from `ManagerController.agent_count` / `reconcile_agent_slots`,
never computed anywhere else, and every figure in this document is one of those readings
recorded verbatim:

| moment | reading |
|---|---|
| driver entry, empty store | `0 / 6` |
| first session live | `1 / 6` |
| second session live | `2 / 6` |
| peak, chosen by `_peak` over the controller's own readings | `2 / 6` |
| after release | `0 / 6` |
| six reserved fixture records | `6 / 6` |
| six bound foreign records | `not established (ownership-unprovable)` |
| the marker run, live | `1 / 6` → `0 / 6` after release |

The binding records carry their own elapsed state: `reservedAt`, `startedAt`, `boundAt`,
`pid`, `pidDomain` (`Jeffs-Omen`), and the terminal `unbound`. Every session that this
slice stopped cleanly reached `unbound` with `process_group_gone true` and `graceful true`;
no SIGKILL escalation was needed anywhere.

**No manager page was served.** `role_driver_dispatch` makes no page claim, states why, and
this dogfood did not invent one.

## 8. Role/package fidelity on the real launch path — PROVEN, and it found something

**8a — the early report.** `--role executor` with `--expected-skill reviewer` and the
reviewer package:

```
role-driver: plugin-role-mismatch: --role is 'executor' but --expected-skill is 'reviewer';
a session runs the package of the role it is launched in.
exit 1 | binding records written: 0 | worker processes started: 0
```

Refused before a control plane is read. **No binding consumed, no process started.**

**8b — the same mismatch disguised past the command line, and a NEW finding.** Stating
`--role executor --expected-skill executor` while pointing `--plugin-root` at the
**reviewer** package passes every command-line check. The gate still catches it, from the
durable binding's own role:

```
ai_dev_flow.claude_runtime.ClaudeRuntimeError: plugin …/ai-dev-reviewer exposes skill(s)
reviewer; exactly [executor] was expected.     (claude_runtime.py:331, via _build_request)
worker processes started: 0
```

**But the binding is consumed.** `_reserve_and_bind` reserves *before* it builds the
request (`session_lifecycle.py:938-955`), so this path leaves a `reserved` record behind —
one is sitting in `bindings-RM2` now — occupying a D6 slot that nothing will release. And
the refusal escapes as an uncaught `ClaudeRuntimeError` with a traceback rather than as an
`InvocationRefused` the driver would record as a stated refusal, so the whole run dies at
exit 1 instead of continuing to the next stated launch.

**"No binding consumed" is true of the command-line refusal and false of the gate.** No
process spawns on either path, which is the safety-critical half. This is reported, not
fixed: it is adjacent to the recorded residual about the orchestrator entry point's missing
early refusal, and closing residuals is not this rail's authority.

---

## The seven unreachable properties — named boundaries, none fixture-proved

Under the accepted middle cut these cannot be exercised. **None was simulated, and no
fixture stands in for any of them.**

| property | why it is unreachable here |
|---|---|
| **7 — out-of-order responses** | No response channel exists. Nothing in the package reads what came back and decides what to say next. |
| **8 — routing back through the orchestrator** | The orchestrator is refused twice on this path — by the per-launch parser and by the accepted door — so no orchestrator session exists to route to. |
| **3, the loop half** | `role_driver` launches exactly what it is told and discovers nothing. There is no continuation loop to close. |
| **14, the arising half** | Nothing here can cause work to arise; there is no work discovery and nothing to discover it with. |
| **9, the recovery half** | No session failed in a way that required recovery. `recover_session` was never reached. Driving one deliberately would have cost a session I did not have and would have proved a fixture, not a recovery. |
| **13, the "runnable" half** | Nothing computes what is runnable. A free slot is a permission, never a reason to find work. |
| **4** | A reviewer session carries no question and a verdict has nowhere to go. Session 5 ran the reviewer role and its package correctly and had nothing to return a verdict *to*. |

### The live false-green trap was not touched

`ai_dev_flow/decision_manager.html` ships a working-looking response composer whose
`submit()` splices the item out of memory. **It was not opened, not served, not clicked,
and nothing in this document rests on it.** It would produce apparent evidence for exactly
properties 7 and 8 above.

---

## What was FIXTURE and what was REAL

**Fixture, with the boundary each one tests:**

- The control-plane scope `dogfood/cp79` and its fourteen rails, in two purpose-made real
  Git repositories with a real published upstream (a snapshot requires a resolved
  revision, so a remoteless control plane is refused — that refusal was hit and respected,
  not worked around). Tests the rail-authorization and iteration-blob path; does **not**
  test Issue #55's own live scope, which was deliberately not used as a dogfood target.
- The six `reserved` D6 records and the six `bound` ones (item 3), and the two duplicate
  records on `amb-rail` (item 5). These test the **admission predicate** with no session
  spent. They prove what the predicate does with a given store; they do **not** prove the
  ceiling holds against six live agents.
- `rotation_threshold = 1` instead of the default 6 — D9 human-owned policy. The D6
  ceiling stayed at 6 throughout.
- The reachable-brief arrangement: the control plane sits at `.cp79` inside the workspace,
  excluded via `.git/info/exclude` so the workspace stays clean, and `read_handoff`
  returns the location relative to the workspace root. Tests that a replacement with a
  granted route reads its handoff; the *unreachable* case (item 4a) needed no arrangement
  at all — the control plane simply sits outside the tool root.
- The harness itself: `ManagerController`, `open_role`, `continue_session`,
  `continue_from_durable_state`, `evaluate_rotation_readiness`, `terminal_finalizer` and
  the six durable readers, composed as a manager would. Not product code, not committed.

**Real:** the worker processes, `claude-agent-sdk 0.2.152`, the provider itself, all eight
sessions and their on-disk transcripts, every authorization from the shipped `authorize`
over a real Git control-plane read, both handoff publications through the product's own
`publish` with a real `Work-State` trailer, every binding record, every occupancy reading,
the condition-4 marker on both polarities, the continuation and its brief, and every
process termination.

---

## Suite

Run on a **pristine WSL-native clone** at checkpoint 78 (`910523378f86ae00a557a0dac272b12cfcf438d0`),
never in the dogfood workspace, so the dogfood's own artefacts could not touch the figures.

**Interpreter: `/root/aidev/host/venv/bin/python`, Python 3.14.4, `claude-agent-sdk 0.2.152`.**

```
Ran 2842 tests in 115.229s
FAILED (failures=1, errors=1, skipped=4)
```

Same clone, **system `python3` 3.14.4, no SDK**: `FAILED (failures=1, errors=1, skipped=6)`.

The two non-passing outcomes are the two the baseline names, unchanged and by the same ids:

- FAIL `tests.test_python_minimum_compatibility.PythonMinimumCompatibilityTests.test_pep604_annotations_are_postponed_in_package_modules` — `ai_dev_flow/progress_record.py`
- ERROR `tests.test_orchestrator_outcome.FreshReviewLoopTests.test_the_checkpoint_three_loop_closes_without_a_provider` — `authorize() missing 1 required keyword-only argument: 'slots'`

**No new failure and no new error. Nothing here is mine.**

**The skip delta is new information, and it is 6 → 4, not 6 → 3.** Three skips became real
passing results, and one *new* skip appeared that the baseline could not have:

| skip under `python3` | under the worker interpreter |
|---|---|
| `test_a_real_compacting_status_message_is_still_not_countable` — *sdk not installed* | **passes** |
| `test_the_real_system_message_carries_identity_only_in_its_data` — *sdk not installed* | **passes** |
| *"Why the two reductions cannot share a shape, stated against the real types"* — *sdk not installed* | **passes** |
| `test_an_unreadable_prompt_is_refused` — *root bypasses the read permission bit* | still skipped |
| `test_install_ps1_help_when_available` — *pwsh is not available* | still skipped |
| `test_python_selection_ps1_parses_in_powershell` — *powershell is not available* | still skipped |
| — | **new:** `test_a_command_without_a_usable_sdk_returns_a_compact_error` — *"the SDK is installed; the absent-SDK path cannot be observed"* |

The absent-SDK path is now unobservable on the interpreter that runs the worker. That is
the correct consequence of provisioning the host and is recorded rather than hidden.

---

## Trades

- **Peak concurrency two rather than six.** The first two runs spent three sessions on my
  own bounds (`max_budget_usd 0.25`, then `max_turns 3`) before I understood the cost of a
  turn on this host. With five left, holding six live would have consumed the entire
  remainder and left nothing for items 4 and 6. I chose breadth over depth: both roles
  live at once, then the marker, then the rotation attempt.
- **D6 proven by fixture.** Directly instructed by the rail, and correct given the budget:
  the fixture reaches the ceiling for zero sessions, and the counter-intuitive `bound`
  shape could not have been reached with live sessions at all, since a controller that
  owns its sessions can always prove them.
- **No manager page.** `role_driver_dispatch` serves none by design, and this dogfood did
  not add one.
- **The rotation was attempted last** because it was the most expensive and the most
  likely to fail. It failed, and the ordering meant items 1–5, 7 and 8 were already
  banked. Had I run it first, a `/compact` that produces nothing would have consumed the
  budget before the marker was ever tried.
- **The condition-4 negative half cost the last session.** Given one session and a choice
  between a second attempt at rotation (impossible — it needs two) and the reachable-brief
  discriminator, the discriminator was the only thing that could still be proven.

---

## Limits — leading with the one most likely to undermine this result

1. **The D6 ceiling has still never been reached by six live agents, on any host.** Every
   ceiling proof on this ticket, including this one, is a proof about the admission
   predicate reading a store. The largest number of real managed sessions this package has
   ever held at once is **two**, established here. If concurrency at the ceiling has a
   failure mode — contention on the binding store's exclusive boundary, provider-side
   throttling, host resource limits — **nothing in this document would have found it.**
2. **Rotation is not proven on this host.** Item 6 is incomplete for the reasons above.
   Retirement, replacement and the swap's D6 accounting were not exercised here.
3. **`/compact` produced no compaction and I do not know why.** One host, one CLI version,
   one SDK version, one workload, one attempt. The observation is real; its cause is not
   established, and no synthetic context was manufactured to force a boundary.
4. **Three of eight sessions were spent on my own errors.** A better-calibrated first run
   would have left budget for a full rotation. The bounds were guesses and two of them
   were wrong.
5. **The peak-concurrency window is 3.0 seconds.** Real, independently observed, and short
   — because `launch_session` sends the directive synchronously, so admissions are serial
   and only *liveness* overlaps. The driver says this about itself; this run measures it.
6. **`pidDomain` is `Jeffs-Omen`**, the WSL hostname. Every liveness proof in this document
   is valid inside that one domain. It says nothing about a pid on the Windows side.
7. **The workspace was a clone, not the canonical worktree**, and it holds no ticket claim,
   so `verify_workspace_ticket_identity` returned no problem for the ordinary
   single-worktree reason rather than by proving a claim. The claim-conflict path is
   untested here.
8. **One `bound` record with no owner and one `reserved` record from a refused launch are
   sitting in the fixture binding roots** (`bindings-D`, `bindings-RM2`). They are left in
   place as evidence for items 3 and 8b. In a production store either would consume a D6
   slot indefinitely.
9. **Cost is under-reported.** The driver prints no per-invocation cost, and the transcripts
   as written do not carry a cost field this reader could total. Only four figures are
   directly observed: USD 0.183547 (marker continuation), USD 0.0703265 (reachable
   continuation), USD 0.058014 (rotation finalizing turn), USD 0.0 (`/compact`).

## Deliberately not done

No mechanism was changed, added or remediated, and **no product code was modified by this
checkpoint**. `manager_dispatch.py` and `orchestrator_invocation.py` were not touched.
Response routing and the autonomous continuation loop were **not** implemented — the
middle-cut boundary stands exactly where it stood. No scheduler, queue, priority model,
fairness policy, autoscaler or work discovery exists. No recorded residual was closed. No
credential was copied, scraped or transplanted, no authentication check was weakened, and
no product code was added to work around credentials. Nothing was ported to Windows.
`main` was not moved, no checkpoint was accepted, `state.md` and every rail were left
alone, and nothing under `skills/**` was read into scope, modified or activated.
**Named checkpoint 9 is not complete and is not claimed complete.**

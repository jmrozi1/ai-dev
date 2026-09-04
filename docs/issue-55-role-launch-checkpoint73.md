# Issue #55 — checkpoint 73: sequential role launch, proved against the real provider

The capability checkpoint 50 ruled could not be added by any rail, and that the human
middle cut authorized by name: **a production path that launches a managed session in the
`executor` role and in the `reviewer` role, one at a time.**

**Verdict: it works, and it is the real path.** Two real provider sessions were launched
sequentially through `python -m ai_dev_flow.role_dispatch`, one per role, each under its
own product-minted id, each running the role's own directive and the role's own plugin,
each stopped with its process group proven gone. Three refusals were driven on the same
path: a role the rail did not grant, the `orchestrator` role, and the D6 ceiling.

---

## What was built

| File | What it is |
|---|---|
| `ai_dev_flow/role_invocation.py` | the gate and the enactment: `RolePacket`, `build_role_packet`, `invoke_role` |
| `ai_dev_flow/role_dispatch.py` | the entry point: `main()`, one stated role assignment, one session |
| `ai_dev_flow/manager_controller.py` | one added pass-through, `ManagerController.dispatch_role` |
| `tests/test_role_invocation.py` | 28 tests |

`role_dispatch.main` -> `ManagerController.dispatch_role` -> `role_invocation.invoke_role`
-> `authorize` -> `launch_session` -> `run_request` -> `stop_session`.

### Why it is a production path and not a test-only door

- It is reachable from a `main()` and from `python -m ai_dev_flow.role_dispatch`, exactly
  as `manager_dispatch` is. Every run below was started that way from a shell.
- It states every input and reads no configuration file: rail, role, ticket reference,
  controller root, prompt file, plugin root, expected skill, allowed tools, turn cap and
  budget are all named on the command line, reusing `manager_dispatch`'s own flag names,
  missing-input rule and refusal reasons rather than a second copy of them.
- The scope it decides against is a real Git control plane, read through
  `resolve_read_source` -> `build_snapshot` -> `observe_scope`, the same three calls
  `manager_dispatch` makes.
- It has **no injection points**. There is no `decision` parameter, no `authorized` flag,
  no launcher parameter and no test hook: `invoke_role` always calls the accepted
  `authorize` predicate itself. The provider process boundary is injectable only through
  the accepted `launch_kwargs`/`stop_kwargs` the lifecycle already defines, and neither
  real run below used them.

## The established facts: which moved and which did not

| Established at the boundary measurement | Moved? |
|---|---|
| `manager_dispatch.main` hard-binds `orchestrator` in three independent places, and is the only shipped entry point that starts a managed session | **Left exactly alone.** `manager_dispatch.py` and `orchestrator_invocation.py` are byte-unchanged. The new path is a sibling entry point, and it refuses `orchestrator` by name in two places, so it is not a second door onto the orchestrator. |
| The second wake gate: `manager_dispatch` calls `propose_wake(snapshot)` with no `lifecycle_facts`, so only an unreconciled handoff can ever wake an orchestrator | **Left exactly alone.** The new path calls `propose_wake` not at all, because nothing wakes it: a human states a rail and a role. Inventing a wake kind for "an executor rail is ready" would be the deferred autonomous loop. Refusing the `orchestrator` role is what keeps the orchestrator's wake gate whole while this door exists beside it. |
| `authorize` admits a launch only on a `running` rail | **Left exactly alone**, and now exercised for two more roles. |
| `ManagerController.launch` and the rotation methods have zero production call sites; the only driven lifecycle call is `controller.dispatch(` | **Moved, deliberately and narrowly.** There is now a second driven lifecycle path: `controller.dispatch_role(` -> `invoke_role` -> `launch_session`. **`ManagerController.launch` itself still has zero production callers**, and `replace_old_context`, `continue_from_durable_state` and `release_continued_context` still have none. `invoke_role` calls `launch_session` with the controller's own store and registry, which is exactly what `ManagerController.launch` does; spelling it through that method instead would have changed a name and nothing else. |

## Role fidelity is structural in four places

The role a session is launched in is the role its binding records and the role its
authorization was granted for, because the two would have to disagree at a point where
the product compares them and fails closed:

1. `RolePacket.__post_init__` refuses any role but `executor` or `reviewer`, and
   `_require_launchable_role` refuses again at the enactment boundary, from the field that
   would actually be carried into the binding;
2. `_require_standing_authorization` refuses when the rail's durable `Role:` read from the
   **snapshot** is not the requested role;
3. `authorize` re-checks the rail's role from the **observation** -- an independent source
   -- and refuses `rail-role-mismatch` before authorizing anything;
4. `Assignment(role=...)` carries it into `session_lifecycle._require_decision`, which
   refuses unless it equals the role the decision was granted for; `reserve_binding` writes
   it into the durable record; `launch_request` reads it back onto the `RuntimeRequest` the
   provider is invoked with.

Both runs printed `launched role`, `binding role` and `runtime request role`, and all
three agreed with the rail in both runs.

## Sequential only

- One process, one role, one session. `main()` contains no loop, no thread, no pool, no
  scheduler and no queue, and `invoke_role` stops its session before returning.
- `_require_sequential` additionally **refuses at the door** when the registry it is about
  to launch into already holds a session (`session-already-live`). It reads that registry,
  never a parameter, so a caller cannot answer the question on the door's behalf.
  Concurrency is the next authorized slice; whatever builds it must delete this refusal
  deliberately.
- The two real runs shared one binding store, one after the other, and the second saw
  `owned session handles: 0` on entry.

---

## The real runs

Both from a shell, `cwd` = the fixture workspace, `/usr/bin/python3.12`,
`claude-agent-sdk 0.2.152`, provider CLI `2.1.259`, model `claude-opus-5`, bounded
`--max-turns 2 --max-budget-usd 0.50`, foreground, under `timeout`, **one at a time**.

| | run E (executor) | run R (reviewer) |
|---|---|---|
| session id | `67b9b9af-49c8-4caa-8b23-82059f16c3b7` | `d81955f5-9234-475c-a60c-9d9311db3f6d` |
| rail | `role-launch-executor-proof` | `role-launch-reviewer-proof` |
| rail `Role:` | `executor` | `reviewer` |
| launched / binding / request role | `executor` / `executor` / `executor` | `reviewer` / `reviewer` / `reviewer` |
| iteration blob | `4744523522b4129dd9785961dca5e130f22e70c4` | `b040f610a7084555c4108f051bd6b3532b8674e3` |
| worker pid = pgid | 605274 | 606445 |
| live occupancy while running | **1 / 6** | **1 / 6** |
| occupancy after stop | 0 / 6 | 0 / 6 |
| binding state at exit | `unbound` | `unbound` |
| `process_group_gone` / `graceful` | True / True | True / True |
| exit code | 0 | 0 |

### The provider-written artifacts

The provider wrote its own transcript for each session, under the id the product minted,
at `~/.claude/projects/<workspace slug>/<session id>.jsonl`. Nothing in the product writes
these files.

**Run E -- `67b9b9af-49c8-4caa-8b23-82059f16c3b7.jsonl`**

- `sessionId: 67b9b9af-49c8-4caa-8b23-82059f16c3b7`, `cwd` = the fixture workspace,
  `version: 2.1.259`
- the first user message is the product's **executor** directive constant, verbatim:
  *"Read your authorized rail in the control plane fresh and continue it."*
- the assistant's own reply, `model: claude-opus-5`, `stop_reason: end_turn`:

  > `ROLE=executor SKILLS=ai-dev-executor:executor, ...`

**Run R -- `d81955f5-9234-475c-a60c-9d9311db3f6d.jsonl`**

- `sessionId: d81955f5-9234-475c-a60c-9d9311db3f6d`, same `cwd`, `version: 2.1.259`
- the first user message is the product's **reviewer** directive constant, verbatim and
  **different from run E's**: *"Read your authorized rail in the control plane fresh and
  return its verdict."*
- the assistant's own reply:

  > `ROLE=reviewer SKILLS=ai-dev-reviewer:reviewer, ...`

**What that proves, and what it does not.** The `ROLE=` half is an echo of the role's
system prompt and proves only which prompt file was loaded. The `SKILLS=` half is stronger:
`ai-dev-executor:executor` and `ai-dev-reviewer:reviewer` are the plugin name and skill
name the product selected from the per-role `--plugin-root` / `--expected-skill`, validated
by `validate_plugin_surface` and passed into the SDK options -- **the session is reporting
which role package it is actually running**, and the two sessions report different ones.
The differing directive is a third, independent provider-written discriminator, since the
directive is a per-role constant an operator cannot supply.

Neither is the primary role-fidelity proof; that is structural and lives in code. These are
the proof that **a real session existed, under its own id, in the role claimed**.

## Refusals driven on the same real path

| # | Command | Result |
|---|---|---|
| 1 | `--role reviewer --rail role-launch-executor-proof` | exit **3**, `role-rail-role-mismatch: rail 'role-launch-executor-proof' is assigned to 'executor'`. The binding store directory was **never created**: nothing was reserved, spawned or sent. |
| 2 | `--role orchestrator` | exit **1**, `role-not-launchable: --role must be one of executor, reviewer ... An orchestrator is started by manager_dispatch, behind a material-wake gate this entry point does not have.` Refused before any control-plane read. |
| 3 | six **reserved** fixture occupants in the binding root | exit **3**, `not-authorized: ... concurrency-ceiling-reached`, `live occupancy: 6 / 6`, and the store still held exactly 6 records afterwards. |

## D6 accounting

- The ceiling is evaluated **at launch, before a binding is consumed**, by the accepted
  `authorize` predicate, against `ManagerController.occupancy(records)` -- the same
  reduction, from the same single store read, that the manager page draws. `invoke_role`
  adds no count of its own.
- It is evaluated a **second** time inside the store lock by `reserve_binding`, from the
  ceiling the decision carried rather than a re-read one.
- Refusal 3 used the exact fixture shape the accepted state warns about: **six `reserved`
  records, not six bound ones**, because `slots.unprovable` is never subtracted and bound
  foreign records answer `concurrency-count-unprovable` instead.
- The refusal discriminates in both directions on the real path: the *identical* command
  with an empty binding root is run E, which launched. Only the store differed.
- Peak real occupancy across this whole checkpoint was **1 of 6**. At no point did two
  managed sessions exist at once.

## Discriminating fixture

The checkpoint-72 technique, applied to the two properties whose fixtures could otherwise
decide their own answer. Baseline 28 tests OK; each mutation applied alone to the shipped
implementation and then reverted byte-identically.

| Mutation | Direction | Result |
|---|---|---|
| `_require_sequential` call removed | blind | FAILED (2 failures) |
| `_require_sequential` always fires | always-refuse | FAILED (10 failures, 8 errors) |
| `authorize(role=...)` given a constant role | wrong role | FAILED (4 errors) |
| `Assignment(role=...)` given a constant role | wrong role | FAILED (4 errors) |
| snapshot rail-role check removed | blind | FAILED (2 failures) |
| restored | -- | OK |

The fixture moves in both directions with the product's real implementation, so it has no
freedom to decide its own answer.

## What was fixture and what was real

**Fixture** -- a disposable, purpose-made control plane (`ai-dev/role-launch`, two rails,
one `Role: executor` and one `Role: reviewer`, both `running`, no dependencies, no shared
resource); a disposable workspace holding a **byte-identical** copy of the package under
test (the `sha256` of the sorted per-file digests matches `/home/jtmrozi/src/ai-dev`
exactly); two controller-owned per-role prompt files and plugins; the six reserved binding
records in refusal 3.

**Real** -- the entry point, the gate, the accepted predicate, the binding store and its
lock, the `ai_dev_flow.claude_worker` subprocess, the readiness handshake, `run_request`,
`claude-agent-sdk 0.2.152`, the provider itself, the two sessions, the two transcripts, the
stop and the process-group proof. Nothing was stubbed, faked, injected or simulated in
either run: no `start=`, no `send=`, no `stop=`, no `alive=`.

## Process accounting

Three worker process groups were started on this rail, all by me, all proven gone by
`process_group_alive` **and** an independent `/proc` walk over every pid and pgid:

| pgid | run | proven gone |
|---|---|---|
| 604567 | interrupted first attempt (below) | yes |
| 605274 | run E | yes |
| 606445 | run R | yes |

**One incident, and it was mine, not the product's.** The first attempt was killed by my
own harness's two-minute command limit while the provider turn was still in flight. Nothing
further was run until the process group was proven gone by both methods. It left exactly
what the accepted design says it should: a **`bound`** binding record for session
`d6e931ef-7828-4f39-8661-59509a0bc03f` whose process no longer exists -- the truthful
record that later projects Disconnected, rather than a cleaner story. **That record lives in
the disposable fixture binding store, not in any product or accepted state.** It was set
aside as evidence and the two real runs used a fresh binding root. The provider had already
written that session's transcript, carrying the executor directive, which corroborates that
the launch was real before it was interrupted.

## The trade this checkpoint most likely loses on

**This entry point serves no manager page.** `manager_dispatch` serves one, and checkpoint
47's accepted standard is precisely that occupancy be *readable by a real client while the
session it counts is running* -- a page reachable only after the work is over cannot
describe that work. This process instead makes no page claim at all: it prints the
occupancy it observed at its own live instant, from the same controller, store and registry
that admitted the session, and returns.

The reason was that the accepted surface blocks on its socket loop until shut down, and a
blocking launcher makes "one session at a time, bounded, foreground, proven dead" harder to
hold rather than easier. That is a real reason and it is not a defence: a reviewer may
reasonably judge that a launch door with no reachable live surface is a step back from
checkpoint 47, and that judgment belongs to the review rather than to me. It is stated here
so it is decided rather than discovered.

## Deliberately not done

No concurrent driver and nothing that can hold two sessions live at once; no response
routing; no autonomous continuation loop; no follow-on ticket; no rotation dogfood; no
manager surface on this entry point; no change to `manager_dispatch`,
`orchestrator_invocation`, `authorization`, `session_lifecycle`, `session_binding`,
`claude_runtime` or `claude_worker`; no change to `skills/**`; neither checkpoint-72
residual closed; no checkpoint accepted; product `main` unmoved.

---

## CORRECTION NOTE — appended at checkpoint 75, 2026-09-04

**Nothing above this line has been edited.** The text stands as it was published, and this
note says which part of it was wrong when it was written.

**The claim: "Role fidelity is structural in four places" (the section above at lines
53–72).** That heading and its four numbered items were **wrong as a statement about what
they covered**, and they were wrong when published, not made wrong by later work.

The four checks are real and each does what it says. What they do not do — and what the
heading implied — is bind the role to the **runtime package** the session actually runs.
Every one of the four compares a role to another statement of the *same* role: the packet's
role, the rail's `Role:` in the snapshot, the rail's role in the observation, and the role
on the `Assignment`, the binding and the `RuntimeRequest`. None of them looked at
`--prompt-file`, `--plugin-root` or `--expected-skill`, which are three further independent
operator inputs, and `claude_runtime.validate_plugin_surface` was never told which role was
being launched.

The consequence, demonstrated by an independent review of 72→73→74: a run stating
`--role executor` on a rail durably assigned `executor`, handed the **reviewer** plugin and
`--expected-skill reviewer`, **passed all four checks**, wrote `executor` into the durable
binding, and ran the reviewer's package. Nothing failed closed.

That is also why the run-E / run-R evidence above is weaker than it reads. The `SKILLS=`
half of each transcript reply is described there as "the session reporting which role
package it is actually running", and it is — but at the time it agreed with the role only
because the operator (me) stated a matching package on the command line. It was a
convention, not a structural fact.

**What makes it true now.** Checkpoint 75 adds a fifth check, in the product:
`claude_runtime._build_request` passes `role=record.role` — the role off the durable binding
record, not any argument it was given — into `validate_plugin_surface`, which refuses
`plugin-role-mismatch` unless the single skill that package exposes is that role's. It is on
the one path every launch, resume and creating-launch request in the package is built
through, and it has no injection point. `role_dispatch._require_role_package` says the same
no at the command line, in the same reason, before a control plane is read.

The rest of this document is unaffected: the two real runs happened, the three refusals were
driven, and `manager_dispatch.py` and `orchestrator_invocation.py` are still byte-identical
to their accepted blobs.

**Also corrected at checkpoint 75:** the mutation table above ("Discriminating fixture")
reports bare failure counts with no named oracle per row, which the accepted
`feedback-loop-design` skill forbids — a suite that merely turns red can do so through an
unrelated structural or setup failure. Checkpoint 74's table names an oracle per row;
checkpoint 75's does too. The checkpoint-73 rows are **not** re-derived here and should be
read as unproven per-partition kill power rather than as proven.

See `docs/issue-55-role-package-fidelity-checkpoint75.md`.

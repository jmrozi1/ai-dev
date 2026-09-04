# Issue #55 — checkpoint 74: a concurrent driver, proved against the real provider

The second of the three capabilities named checkpoint 9's accepted middle cut authorized:
**a driver that can hold more than one managed executor/reviewer session live at once,
bounded by the D6 ceiling.** Checkpoint 73 built the sequential launcher and placed a
refusal at its door that this slice had to remove deliberately. It is removed.

**Verdict: it works, and it is the real path.** Three real provider sessions were held
alive, owned and counted at the same instant through
`python -m ai_dev_flow.role_driver_dispatch` — peak occupancy **3 of 6**, corroborated by
an independent `/proc` walk that saw all three worker process groups simultaneously by
pid. A fourth real session was launched to occupancy **6 of 6** and the driver then
refused the seventh with the accepted `concurrency-ceiling-reached`. All four process
groups were proven gone by `process_group_alive` **and** by an independent `/proc` walk.
Three refusals were driven on the same path.

---

## What was built

| File | What it is |
|---|---|
| `ai_dev_flow/role_driver.py` | the driver: `RoleLaunch`, `HeldSession`, `StatedRefusal`, `ReleasedSession`, `DriverOutcome`, `drive_roles` |
| `ai_dev_flow/role_driver_dispatch.py` | the entry point: `main()`, `python -m ai_dev_flow.role_driver_dispatch`, one launch group per `--rail` |
| `ai_dev_flow/role_invocation.py` | `_require_sequential` **deleted**; `invoke_role` factored into `open_role_session` + the stop it already did |
| `ai_dev_flow/manager_controller.py` | one added pass-through, `ManagerController.open_role` |
| `ai_dev_flow/role_dispatch.py` | comments only — the paragraph that described the deleted refusal |
| `tests/test_role_driver.py` | 28 tests |
| `tests/test_role_invocation.py` | 28 → 30 tests; `SequentialOnlyTests` replaced by `ConcurrencyIsNoLongerRefusedTests` |

`role_driver_dispatch.main` -> `drive_roles` -> `ManagerController.open_role` ->
`role_invocation.open_role_session` -> `authorize` -> `launch_session` -> `run_request`,
repeated per stated launch with nothing stopped in between, then
`ManagerController.stop` -> `stop_session` for every held session in reverse order.

### Why it is a production path and not a test-only door

- It is reachable from a `main()` and from `python -m ai_dev_flow.role_driver_dispatch`,
  exactly as `role_dispatch` and `manager_dispatch` are. Every run below was started that
  way from a shell.
- It states every input and reads no configuration file. Each `--rail` opens a launch
  group, and every flag after it belongs to that group until the next `--rail`: role,
  ticket reference, controller root, prompt file, plugin root, expected skill, allowed
  tools, turn cap and budget. Each group is parsed by **`role_dispatch.stated_role_inputs`
  itself** — the same function that parses an entire sequential run — so a launch this
  driver admits had to state exactly what a `role_dispatch` run must state, and is refused
  in exactly the same words when it does not. Anything a group's parser does not recognise
  falls through to the accepted scope parser.
- The scope it decides against is a real Git control plane, read once through
  `role_dispatch._read_scope` (`resolve_read_source` -> `build_snapshot` -> `observe_scope`),
  imported rather than respelled, so every launch in a run is decided against one revision.
- It has **no injection points**. There is no `decision` parameter, no `authorized` flag,
  no launcher parameter and no test hook; `drive_roles`'s signature is asserted to contain
  none of `decision`, `authorized`, `authorize`, `launcher`, `gate`, and `role_driver` is
  asserted (by AST walk, not substring) never to use `authorize`, `AuthorizationDecision`
  or `launch_session`. The provider process boundary is injectable only through the
  accepted `launch_kwargs`/`stop_kwargs` the lifecycle already defines, and none of the
  four real runs used them.

---

## The established facts: which moved and which did not

| Established at checkpoint 73 | Moved? |
|---|---|
| `role_invocation._require_sequential` refuses at the door when the registry already holds a session; "concurrency is the next authorized slice; whatever builds it must delete this refusal deliberately" | **Moved — deliberately, and it is the point of this checkpoint.** `_require_sequential` and `REASON_SESSION_ALREADY_LIVE` are deleted. `role_invocation` names the deletion in its own comments so a reader of a checkpoint-73 transcript finds out what happened to the reason rather than assuming it merely became unreachable, and `tests.test_role_invocation.ConcurrencyIsNoLongerRefusedTests.test_the_sequential_refusal_and_its_reason_are_gone` pins it by name so it cannot come back by accident. |
| `manager_dispatch.main` hard-binds `orchestrator` in three independent places and is the only entry point that starts an orchestrator; the second wake gate (`propose_wake` with no `lifecycle_facts`) | **Left exactly alone.** `manager_dispatch.py` and `orchestrator_invocation.py` are **byte-unchanged**, asserted in the suite against the committed blobs. `propose_wake` is not called by this driver. `orchestrator` is refused twice: at the command line by the borrowed `role_dispatch._stated_role`, and pre-flight in `drive_roles` by the accepted `_require_launchable_role` — before anything is spent, not after two sessions have been. |
| The launchable-role refusal, the rail-role match in **both** snapshot and observation, rail running, rail reconciled, head currency, workspace proof, `Assignment(role=...)` fidelity | **Left exactly alone**, and now reached by both doors, because `invoke_role` was factored into `open_role_session` + the stop it already performed rather than copied. A second admission path with its own copy of these checks would be two policies free to drift, and the one that drifts is always the one that admits more. |
| `role_dispatch` is one process, one role, one session | **Unchanged in behaviour, weakened in enforcement, and that is stated in the file.** Its single-session shape was previously guaranteed twice: by its own shape and by the door's refusal. The door's half is gone. The file's half — one `dispatch_role` call, no loop, no thread, no pool — remains and is pinned by `test_the_sequential_entry_point_is_still_sequential`. |
| `ManagerController.launch` and the rotation methods have zero production call sites; the driven lifecycle calls are `controller.dispatch(` and `controller.dispatch_role(` | **Moved, narrowly.** There are now two more: `controller.open_role(` and `controller.stop(`. `ManagerController.launch` itself **still has zero production callers**, and `replace_old_context`, `continue_from_durable_state` and `release_continued_context` still have none. |
| Peak real occupancy across checkpoint 73 was 1 of 6; at no point did two managed sessions exist at once | **Moved. Peak real occupancy this checkpoint is 3 of 6**, and separately 6 of 6 with fixture occupants. |

---

## How D6 is accounted

The ceiling is evaluated **at admission, before a launch consumes a binding**, by the
accepted `authorize` predicate, against `ManagerController.occupancy(records)` —
`reconcile_agent_slots` over the durable records and the ownership this controller can
prove. `role_driver` adds **no count of its own**: it never adds, subtracts or caches an
occupancy number, and an AST walk asserts it never names `reconcile_agent_slots`,
`AgentSlots`, `CONCURRENCY_CEILING_DEFAULT`, `occupied` or `ceiling`. The only occupancy
values it holds are readings `ManagerController.agent_count` produced.

**Serial admission is forced, not chosen.** A slot is consumed by `reserve_binding`, which
lives inside `launch_session`; nothing else in the accepted design marks a slot taken. So
the only way to admit launch *n+1* against an occupancy that already includes launch *n* is
to have reserved *n* already. The driver therefore admits and enacts one launch at a time,
re-reading the store and re-reconciling occupancy through the controller immediately before
the predicate sees it:

```
admit #1 -> occupancy 0/6 -> reserve+bind #1 (slot consumed, session held live)
admit #2 -> occupancy 1/6 -> reserve+bind #2 (slot consumed, session held live)
admit #3 -> occupancy 2/6 -> reserve+bind #3 (slot consumed, session held live)
...
admit #7 -> occupancy 6/6 -> concurrency-ceiling-reached, nothing reserved
```

The alternative — admitting a batch and then launching it — would require the driver to
count pending admissions that no durable record yet describes, which is exactly the second
count the accepted state forbids and exactly how a seventh agent gets in.

- Evaluated a **second** time inside the store lock by `reserve_binding`, from the ceiling
  the decision carried rather than a re-read one.
- **Fails closed.** If a held session's ownership cannot be proved, `reconcile_agent_slots`
  reports it `unprovable`, `authorize` never subtracts that, and the next admission is
  refused `concurrency-count-unprovable` rather than admitted against a smaller total.
  Driven in the suite by a bound record this controller does not hold: **nothing** is
  launched, both stated launches are refused.
- **A limit, not a target.** Nothing in `role_driver` launches an agent because a slot is
  free. It is told which rails to launch in which roles, launches those and nothing else,
  in the order stated, and never asks what else might be runnable. There is no work
  discovery in the file and nothing to discover it with. Pinned by
  `test_a_free_slot_is_never_a_reason_to_launch_anything`: one stated launch with six free
  slots starts exactly one session.
- **No scheduler, queue, priority model, fairness policy or autoscaler.** Asserted by AST
  walk over both new modules: no `threading`, `multiprocessing`, `futures`, `asyncio`,
  `Thread`, `Pool`, `heapq`, `sched`, `PriorityQueue`, `Queue`.

---

## The real runs

All from a shell, `cwd` = the fixture workspace, `/usr/bin/python3.12`,
`claude-agent-sdk 0.2.152`, provider CLI `2.1.259`, model `claude-opus-5`, each session
bounded `--max-turns 2 --max-budget-usd 0.50`, each run under `timeout` and started in the
background per the checkpoint-73 harness hazard.

### Run H — three sessions held live at once (`bindings-hold`, exit 0)

Control-plane head `1f8e4def6797fca057c0ffdcbe2b4191190c0c8b`, scope
`ai-dev/concurrent-driver`, three stated launches in one process.

| | held 1 | held 2 | held 3 |
|---|---|---|---|
| rail | `cd-executor-one` | `cd-reviewer-one` | `cd-executor-two` |
| rail `Role:` / launched role | `executor` | `reviewer` | `executor` |
| session id | `f9216ea7-e69d-4e3e-866a-64dac1f5b682` | `d057d9d0-2004-4a52-ba32-309a2d640332` | `de0bb95e-3beb-41e6-a4a8-3a7fd15c129e` |
| iteration blob | `e261a9174f435244fc309a71df925dc0d0e928be` | `8400ac434d0b241002f5bc9ca0c89039ced4a1a3` | `e633972941065e12f9f54c293b41019eb7e94eca` |
| worker pid = pgid | 677181 | 677247 | 677362 |
| occupancy **at this admission** | **1 / 6** | **2 / 6** | **3 / 6** |
| `process_group_gone` / `graceful` | True / True | True / True | True / True |
| binding state at exit | `unbound` | `unbound` | `unbound` |

- occupancy on entry `0 / 6`; **all held at once: 3 session(s) -> 3 / 6**;
  **peak live occupancy 3 / 6**; live occupancy after release `0 / 6`.
- released in reverse order: `de0bb95e…`, then `d057d9d0…`, then `f9216ea7…`.

**The independent corroboration.** A separate process walked `/proc` every 0.2 s for the
whole run, recording every pid whose cmdline names `ai_dev_flow.claude_worker` together
with its pgid. Nothing in the product writes that file. Its transitions:

```
1788551724.694 count=1 [(677172, 677172)]
1788551725.309 count=2 [(677181, 677181), (677197, 677197)]
1788551725.513 count=1 [(677181, 677181)]
1788551727.777 count=2 [(677181, 677181), (677238, 677238)]
1788551727.981 count=1 [(677181, 677181)]
1788551729.415 count=2 [(677181, 677181), (677247, 677247)]
1788551733.951 count=3 [(677181, 677181), (677247, 677247), (677362, 677362)]
1788551739.319 count=2 [(677181, 677181), (677247, 677247)]
1788551739.523 count=1 [(677181, 677181)]
```

For **5.37 seconds — 26 consecutive samples** — exactly the three process groups the
product reported (677181, 677247, 677362) were simultaneously alive. That is the whole
claim of this slice, observed from outside the product.

**An honest wrinkle in that trace.** Three other short-lived `claude_worker` pids appear
(677172, 677197, 677238), each for under half a second, each in its own process group and
none of them ever reported by this run. They are not this run's sessions — the product
minted and named 677181, 677247 and 677362 and no others — and they are most likely other
activity on this shared host. This is stated rather than smoothed over, and it is why the
poller records **pids**, not just a count: the identification is exact regardless of what
else was on the machine. It has no bearing on the product's own occupancy, which is
reconciled from this controller's binding store and registry and never from a `/proc` scan.

### Run C — the ceiling refusing a launch (`bindings-ceiling`, exit 3)

Five **reserved** fixture occupants seeded into the binding root, then two stated launches.

| | |
|---|---|
| occupancy on entry | `5 / 6` |
| held 1 | session `ed754c66-7856-4a47-93b3-16f5fa0cac2a`, rail `cd-executor-one` as `executor`, pid/pgid `682029/682029` |
| occupancy with it live | **`6 / 6`** — peak live occupancy `6 / 6` |
| stated launch 2 | **refused**: `not-authorized: the accepted authorization predicate refuses this launch: concurrency-ceiling-reached` |
| after release | `5 / 6`; store holds exactly 6 records — 5 `reserved`, 1 `unbound` |
| `process_group_gone` / `graceful` | True / True |

The refused rail had **nothing** reserved, spawned or sent, and the held session kept
running while the refusal was recorded. The fixture shape is the one the accepted state
warns about: **five `reserved` records, not bound ones**, because `slots.unprovable` is
never subtracted and bound foreign records answer `concurrency-count-unprovable` instead.

### The provider-written artifacts

The provider wrote its own transcript for each of the four sessions, under the id the
product minted, at `~/.claude/projects/<workspace slug>/<session id>.jsonl`. Nothing in the
product writes these files. All four carry `cwd` = the fixture workspace and
`version: 2.1.259`.

| session | first user message (the product's per-role directive constant) | assistant reply, `claude-opus-5`, `stop_reason: end_turn` |
|---|---|---|
| `f9216ea7…` | *"Read your authorized rail in the control plane fresh and continue it."* | `ROLE=executor SKILLS=ai-dev-executor:executor, …` |
| `d057d9d0…` | *"Read your authorized rail in the control plane fresh and return its verdict."* | `ROLE=reviewer SKILLS=ai-dev-reviewer:reviewer, …` |
| `de0bb95e…` | *"…continue it."* | `ROLE=executor SKILLS=ai-dev-executor:executor, …` |
| `ed754c66…` | *"…continue it."* | `ROLE=executor SKILLS=ai-dev-executor:executor, …` |

The `SKILLS=` half is the load-bearing one: `ai-dev-executor:executor` and
`ai-dev-reviewer:reviewer` are the plugin name and skill name the product selected from
each launch's own `--plugin-root` / `--expected-skill`, validated by
`validate_plugin_surface` and passed into the SDK options. Two of the three sessions held
together were running **different role packages at the same time**, which is what makes
"more than one role, concurrently" a fact about the sessions rather than about the command
line. The differing directive is a second, independent provider-written discriminator,
since the directive is a per-role constant an operator cannot supply.

Turn timestamps from those transcripts — `19:55:27.0–28.9`, `19:55:31.4–33.3`,
`19:55:36.6–38.6` — show the provider **turns did not overlap**. That is true, expected,
and discussed under the trade below.

### Process accounting

Four worker process groups were started on this rail, all by me, all proven gone by
`process_group_alive` **and** an independent `/proc` walk over every pid and pgid:

| pgid | run | `process_group_alive` | independent `/proc` walk |
|---|---|---|---|
| 677181 | run H, held 1 | False | nothing |
| 677247 | run H, held 2 | False | nothing |
| 677362 | run H, held 3 | False | nothing |
| 682029 | run C, held 1 | False | nothing |

No orphan binding record was produced and no run was interrupted. Every provider launch was
run with an explicit long `timeout` and in the background, per the checkpoint-73 hazard.

---

## Refusals driven on the same real path

| # | Command | Result |
|---|---|---|
| 1 | two stated launches, five reserved occupants (run C) | exit **3**, `not-authorized: … concurrency-ceiling-reached` on the second, with the first still running at `6 / 6`. Store afterwards: 5 `reserved` + 1 `unbound`. |
| 2 | `--rail cd-executor-one --role reviewer` | exit **3**, `role-rail-role-mismatch: rail 'cd-executor-one' is assigned to 'executor'`. The binding store directory was **never created**: nothing reserved, spawned or sent. |
| 3 | a valid executor group **plus** `--role orchestrator` | exit **1**, `role-not-launchable: --role must be one of executor, reviewer … An orchestrator is started by manager_dispatch, behind a material-wake gate this entry point does not have.` Refused before any control-plane read; the binding store directory was never created, and **the valid launch beside it was not spent either**. |

Refusal 1 discriminates in both directions on the real path: the *same* two-launch command
against an empty binding root is run H's first two launches, which both launched. Only the
store differed.

---

## Discriminating fixture

Per `feedback-loop-design`: the partitions were enumerated first, and each mutation was
applied **alone** to the shipped implementation with a **named oracle** stated in advance,
rather than merely turning the suite red. Every mutation was reverted byte-identically —
the harness asserts the file's `sha256` matches its pre-mutation digest before moving on.
Baseline: 58 tests OK (`tests.test_role_driver` 28 + `tests.test_role_invocation` 30).

| Mutation | Partition it attacks | Named oracle | Result |
|---|---|---|---|
| M1 each session is stopped as soon as it is opened | concurrent holding | `ConcurrentHoldingTests.test_two_sessions_are_held_live_at_the_same_instant`, `…test_occupancy_grows_with_each_admission` | **DIED** (2 errors) |
| M2 `open_role` admits against an empty store | D6 at admission | `CeilingTests.test_the_ceiling_refuses_the_launch_that_would_exceed_it`, `…test_a_total_that_cannot_be_established_fails_closed` | **DIED** (1 failure, 1 error) |
| M3 a held session reports the entry occupancy | occupancy grows per admission | `…test_occupancy_grows_with_each_admission`, `…test_two_sessions_are_held_live_at_the_same_instant` | **DIED** (2 failures) |
| M4 the pre-flight launchable-role refusal is skipped | orchestrator refused *before* anything is spent | `DriverRefusalTests.test_the_orchestrator_role_refuses_the_whole_run_before_anything_is_spent` | **DIED** (1 failure) |
| M5 nothing is released | teardown | `…test_every_held_session_is_released_in_reverse_order_and_proven_gone`, `TeardownTests.test_a_while_held_that_raises_releases_every_held_session` | **DIED** (2 failures) |
| M6 release in launch order rather than reverse | teardown order | `…test_every_held_session_is_released_in_reverse_order_and_proven_gone` | **DIED** (1 failure) |
| M7 a refusal on one rail aborts the whole run | a refusal is about one rail only | `DriverRefusalTests.test_a_rail_assigned_another_role_is_refused_and_the_rest_still_run`, `CeilingTests.test_the_ceiling_refuses_the_launch_that_would_exceed_it` | **DIED** (2 errors) |
| M8 peak is the first reading rather than the largest | peak reporting | `…test_two_sessions_are_held_live_at_the_same_instant`, `…test_three_sessions_are_held_at_once_on_three_rails` | **DIED** (2 failures) |
| M9 nothing is released when something raises | no leak on failure | `TeardownTests.test_a_while_held_that_raises…`, `…test_a_launch_failure_releases_what_was_already_held` | **DIED** (2 failures) |
| M10 release ignores the stop boundary it was given | teardown uses the accepted stop | `…test_every_held_session_is_released_in_reverse_order_and_proven_gone` | **DIED** (1 error) |
| M11 the launchable-role refusal is removed from `open_role_session` | orchestrator refused at the door | `ConcurrencyIsNoLongerRefusedTests.test_the_orchestrator_role_is_still_refused_by_name_on_the_open_path` | **DIED** (1 failure) |
| M12 the snapshot rail-role check is removed | role fidelity from the snapshot | `RoleFidelityRefusalTests.test_a_rail_assigned_another_role_is_refused_from_the_snapshot`, `DriverRefusalTests.test_a_rail_assigned_another_role_is_refused_and_the_rest_still_run` | **DIED** (2 failures) |
| restored | — | full 58 | **OK** |

The fixture also moves in the *admitting* direction, so it has no freedom to decide its own
answer: `CeilingTests.test_one_slot_lower_the_same_two_launches_are_both_admitted` runs the
identical two launches with four occupants instead of five and both are admitted, and
`ConcurrencyIsNoLongerRefusedTests.test_a_second_session_opens_into_a_registry_that_already_holds_one`
is precisely the call checkpoint 73 refused, now admitted with nothing else changed.

One methodological note: the structural assertions (no scheduler, no injection point, no
second count) are **AST walks over the module's identifiers**, not substring searches over
the file. Both new modules argue at length in comments about the schedulers and second
counts they do not contain, and a test that could be failed by explaining itself would push
the reasoning out of the code.

---

## What was fixture and what was real

**Fixture** — a disposable, purpose-made control plane (`ai-dev/concurrent-driver`, its own
bare origin, three rails: `cd-executor-one` and `cd-executor-two` as `Role: executor`,
`cd-reviewer-one` as `Role: reviewer`, all `running`, no dependencies, no shared resource);
a disposable workspace holding a **byte-identical** copy of the package under test (the
`sha256` of the sorted per-file digests is
`278c5de23ad32f79fb3948312791569edd42f7c719c8d8dbfbf2c95f35eac3b9` for both
`/home/jtmrozi/src/ai-dev` and the fixture); two controller-owned per-role prompt files and
plugins carried forward from checkpoint 73's fixture; the five reserved binding records in
run C; the disposable ticket reference `local:concurrent-driver-proof`. **Issue #55's own
live scope was never the dogfood target**, per checkpoint 69.

**Real** — the entry point, the driver, the gate, the accepted predicate, the binding store
and its lock, four `ai_dev_flow.claude_worker` subprocesses, the readiness handshake,
`run_request`, `claude-agent-sdk 0.2.152`, the provider itself, the four sessions, the four
transcripts, every stop and every process-group proof. Nothing was stubbed, faked, injected
or simulated in any of the four runs: no `start=`, no `send=`, no `stop=`, no `alive=`.

---

## Validation

- `python3 -m unittest tests.test_role_driver tests.test_role_invocation` — **58 tests, OK**
  (28 new + 30, up from checkpoint 73's 28).
- Four real provider runs and three real refusals, above.
- Twelve mutations, each applied alone and reverted byte-identically, each killed by a
  named oracle.
- `manager_dispatch.py` and `orchestrator_invocation.py` verified **byte-identical** to
  their committed blobs by a test in the suite, not only by inspection.
- No file under `skills/**` was touched.

---

## The trade this checkpoint most likely loses on

**What is concurrent here is session liveness, not provider turns.** Three sessions were
alive, owned and counted at the same instant; their turns ran one after another. The
transcript timestamps say so plainly and this document does not hide it.

The reason is structural rather than an omission: a slot is consumed only by
`reserve_binding`, which is inside `launch_session`, and `launch_session` also performs the
launch invocation. So admitting launch *n+1* against an occupancy that includes *n* requires
*n* to have been reserved — and reserving *n* means having already sent it its directive.
Overlapping the turns would need either a second count of pending admissions (forbidden), a
split of the accepted `launch_session` (a change to accepted lifecycle contracts this slice
was not authorized to make), or threads (a concurrency-control apparatus D6's "no scheduler"
sits very close to). I judged holding sessions live to be the authorized capability and
overlapping turns to be the thing that needs response routing and a continuation loop —
both explicitly deferred — to be worth anything.

That is a real reason and it is not a defence. A reviewer may reasonably judge that a
driver whose agents take turns one at a time has not yet delivered "six agents working at
once", and that judgment belongs to the review rather than to me. It is stated here so it is
decided rather than discovered.

Two smaller trades in the same family:

- **This entry point still serves no manager page.** Checkpoint 73 stated this and the cost
  has grown: this is the first process in the package at which a live occupancy page would
  have something worth looking at — three concurrent agents — and it still prints readings
  and returns rather than serving one. The accepted surface blocks on its socket loop until
  shut down, and a blocking driver would make "every process group proven gone before this
  process exits" harder to hold, not easier.
- **The command line is long.** Every launch states its runtime policy in full, with no
  inheritance between groups, because the prompt file, plugin, expected skill, tool
  allowance, turn cap and budget are per role and a driver that let one launch inherit
  another's could run a reviewer under the executor's role package. That is deliberate, but
  it is a real ergonomic cost and a reviewer may want a different answer.

---

## Deliberately not done

No human-response routing; no autonomous continuation loop; no follow-on ticket filed; no
scheduler, queue, priority model, fairness policy or autoscaling; no work discovery; no
retry; no wake kind for "an executor rail is ready"; no manager surface on this entry point;
no overlap of provider turns; no rotation dogfood; no change to `manager_dispatch`,
`orchestrator_invocation`, `authorization`, `session_lifecycle`, `session_binding`,
`claude_runtime` or `claude_worker`; no change to `skills/**`; no checkpoint-72 residual
closed; no checkpoint accepted; product `main` unmoved.

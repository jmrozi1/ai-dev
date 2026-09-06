# Checkpoint 81 — six managed sessions live at the D6 ceiling, on the real path

Blocker 2, the last one open for named checkpoint 9: **the six-agent ceiling holds against six
real managed sessions that were alive at the same instant**, not against a store arranged to
look like six.

Every D6 ceiling proof this ticket had produced before this one — checkpoints 74 and 79 and
their fixtures — was a predicate reading a store. Six *records* had been arranged; six real
managed sessions had never been alive together, and the most ever held at once was two. This
run holds six, reads `6 / 6` from the controller that admitted them, is refused
`concurrency-ceiling-reached` on a seventh while they are still running, and proves every one
of the six gone afterwards.

**Named checkpoint 9 is not claimed complete.** The orchestrator decides that after review.

**Lead with this limit:** what was simultaneous is the **liveness, ownership and accounting**
of six managed sessions — not six provider turns. The independent walk never saw more than one
provider child process at a time, because the shipped driver admits and enacts launches
strictly one at a time and says so in its own header. Section *Limits* opens with this.

---

## Environment, established first-hand

| | |
|---|---|
| host | Windows 11 + WSL Ubuntu, every invocation as `-u root` |
| worker interpreter | `/root/aidev/host/venv/bin/python`, Python **3.14.4**, `claude_agent_sdk` **0.2.152** |
| CLI actually executed | the SDK's **bundled** binary — every one of the six transcripts records `"version": "2.1.259"` |
| CLI on `PATH` | `/root/.local/bin/claude` **2.1.261**, which no managed session runs. Recorded, not closed: the product still neither pins nor reports which CLI binary a session executed |
| `PATH` hygiene | `/mnt/` stripped for the run and for every measurement; with it stripped there is **no Linux `node`** (`command -v node` → none) |
| model | `claude-opus-5` on all six sessions, from the transcripts |
| workspace | WSL-native clone `/root/aidev/cp81/ws`, detached at **`916c739dde00d9e71764678bcf0b947fd0f95002`** (checkpoint 80), clean before and after |
| controller root | `/root/aidev/cp81/controller-root` — one plugin package per role, one prompt file per launch |
| dogfood control plane | `dogfood/cp81`, a real Git repo at `ws/.cp81` with a real bare origin at `/root/aidev/cp81/cp.git`; `.cp81/` is in `.git/info/exclude` |
| binding root | `/root/aidev/cp81/bindings` |

The entry point is the shipped one and nothing was wrapped around it:

```
/root/aidev/host/venv/bin/python -m ai_dev_flow.role_driver_dispatch \
  --no-human-exclusivity --control-plane .../ws/.cp81 --project dogfood --ticket cp81 \
  --binding-root /root/aidev/cp81/bindings \
  --rail ceiling-lane-one   --role executor  ... \
  --rail ceiling-lane-two   --role reviewer  ... \
  --rail ceiling-lane-three --role executor  ... \
  --rail ceiling-lane-four  --role reviewer  ... \
  --rail ceiling-lane-five  --role executor  ... \
  --rail ceiling-lane-six   --role reviewer  ... \
  --rail ceiling-lane-seven --role executor  ...
```

Each group states its own ticket reference, controller root, prompt file, plugin root, expected
skill, tool allowance, `--max-turns 100` and `--max-budget-usd 10.0`. The bounds are deliberately
generous: checkpoints 79 and 80 lost four provider sessions between them to the executor's own
cost caps, and **no session was lost to a cap here**.

### Six distinct rails, because one rail cannot carry six records

`authorization.authorize` evaluates `len(live) > 1 -> binding-duplicated` before it reaches the
`if not live:` block that owns the ceiling, so six records on one rail trip the wrong refusal.
Seven rails were therefore written, `ceiling-lane-one` … `ceiling-lane-seven`, each `Status:
running`, each naming its own `Role:`, none sharing a resource, none depending on another.

### Ownership had to be the driver's

`concurrency-count-unprovable` comes from ownership *absence*, not from boundness: six bound
foreign records reconcile to `occupied 0, unprovable 6` and would **authorize** a seventh. The
six here are the driver's own — launched by one `ManagerController`, held in its own
`SessionRegistry` — which is why the reading is `6 / 6` and not `not established`.

### A pre-flight that spends nothing

Before any session was paid for, a rehearsal ran the whole gate stack — `resolve_read_source`,
`build_snapshot`, `prove_workspace`, `observe_scope`, `authorize` for all seven lanes,
`validate_plugin_surface` for both packages — and printed `launch-authorized` seven times with
an empty store. That is the pure predicate; it spends no provider session and it is not the
proof. It is why the paid run did not have to discover a control-plane typo with six live
agents on the host.

---

## Provider budget — six sessions spent, six concurrent, seventh never spawned

| # | rail | role | session id | pid/pgid | provider evidence |
|---|---|---|---|---|---|
| 1 | `ceiling-lane-one` | executor | `7b57d4f0-1865-4d4c-9e75-7b987ca7635f` | 737398 | 31,094 B / 21 records |
| 2 | `ceiling-lane-two` | reviewer | `055f137c-1aee-4dc8-a535-709d9a7748bc` | 737456 | 31,093 B / 21 records |
| 3 | `ceiling-lane-three` | executor | `746477f5-b9e0-4749-a9b2-802d23a36b2e` | 737522 | 31,072 B / 21 records |
| 4 | `ceiling-lane-four` | reviewer | `3eae5992-a4e3-4b43-9552-6ee9ee93e14b` | 737586 | 31,097 B / 21 records |
| 5 | `ceiling-lane-five` | executor | `f49650a0-ed5f-40b8-98a6-ba9148af914e` | 737650 | 31,093 B / 21 records |
| 6 | `ceiling-lane-six` | reviewer | `d5ca53c7-c55e-4b44-9ce3-0c23d4da0193` | 737717 | 74,132 B / 28 records |
| 7 | `ceiling-lane-seven` | executor | — none minted — | — none — | **refused, nothing spawned** |

Three executor-role and three reviewer-role sessions. Each wrote a provider transcript on disk
**under its own minted id and no other**, at `cwd /root/aidev/cp81/ws`, and each one's tool calls
name **its own rail and only its own rail**:

```
7b57d4f0 -> Read .../rails/ceiling-lane-one/rail.md
055f137c -> Read .../rails/ceiling-lane-two/rail.md
746477f5 -> Read .../rails/ceiling-lane-three/rail.md
3eae5992 -> Read .../rails/ceiling-lane-four/rail.md
f49650a0 -> Read .../rails/ceiling-lane-five/rail.md
d5ca53c7 -> Read .../rails/ceiling-lane-six/rail.md , Read ws/ai_dev_flow/role_driver.py
```

That is what makes them six real sessions rather than six records: six separate provider
conversations, each of which read the rail its own launch was authorized against and answered
it. Sessions 1–5 answered `ACKNOWLEDGED`; session 6 answered `421` and the first line of
`role_driver.py`, which is that file's actual first line. Total across the six: 294,125
input+cache tokens, 1,432 output tokens.

Cost was kept off the critical path deliberately: **occupancy is the product here, not
throughput**, so each rail carried one trivial bounded instruction. Session 6's was slightly
larger on purpose — it is the last one admitted, so its invocation is what holds the all-six
window open.

---

## 1. Six real provider sessions alive at the same instant — PROVEN

The shipped driver's own output, verbatim:

```
occupancy on entry: 0 / 6
held 1: session 7b57d4f0… rail ceiling-lane-one   role executor pid/pgid 737398/737398 occupancy 1 / 6
held 2: session 055f137c… rail ceiling-lane-two   role reviewer pid/pgid 737456/737456 occupancy 2 / 6
held 3: session 746477f5… rail ceiling-lane-three role executor pid/pgid 737522/737522 occupancy 3 / 6
held 4: session 3eae5992… rail ceiling-lane-four  role reviewer pid/pgid 737586/737586 occupancy 4 / 6
held 5: session f49650a0… rail ceiling-lane-five  role executor pid/pgid 737650/737650 occupancy 5 / 6
held 6: session d5ca53c7… rail ceiling-lane-six   role reviewer pid/pgid 737717/737717 occupancy 6 / 6
all held at once: 6 session(s) -> 6 / 6
peak live occupancy: 6 / 6
```

Each line carries the occupancy reading `ManagerController.agent_count()` produced at the
instant that session became live, from the same controller, store and registry that admitted
it. The climb is monotone and never returns to zero, which is the whole difference from
`role_dispatch`: a session is handed back **running** and is still running when the next one is
admitted against it.

## 2. Occupancy reads `6 / 6`, ownership-provable — PROVEN

`all held at once: 6 session(s) -> 6 / 6`, printed by `role_driver_dispatch`'s `while_held`
observer, which asks the controller for a fresh reading at the one instant every session this
run opened is live.

That it is `6 / 6` and not `not established (ownership-unprovable)` is the load-bearing half,
and it is not a matter of formatting. `agent_count` sets `current` to `slots.occupied` **only
when `slots.provable`**, and `provable` is `not unprovable`. So `6 / 6` entails six occupants
and zero unprovable sessions. Those six occupants were reached through
`reconcile_agent_slots`' *bound* branch — `ownership.get(session) is True` — and not through
its reservation shortcut, because `launch_session` returns a binding only once it is bound and
a pgid is attached, and all six had pgids. **Six bound records, each with ownership proved by
the controller holding its handle.**

## 3. The seventh was refused `concurrency-ceiling-reached`, and nothing was spawned — PROVEN

```
refused: rail ceiling-lane-seven as executor: not-authorized: the accepted authorization
predicate refuses this launch: concurrency-ceiling-reached
```

Refused *while the six were still live* — the refusal is printed between the six `held` lines
and the six `released` lines, and the driver's exit code was 3 (refusals present).

Nothing reserved: the binding store contains **exactly six records** and
`ceiling-lane-seven has a record: False`. Nothing spawned: the independent walk below never
saw a seventh process group, at any sample, at any point in the run.

This is the accepted predicate refusing, not a second rule. `open_role_session` calls
`authorize` itself and raises before `launch_session` is reached, so no reservation, no spawn
and no provider message exists for lane seven.

## 4. An independent `/proc` walk saw all six groups simultaneously — PROVEN

A separate process, started before the driver, holding **no `SessionRegistry` and no
`BindingStore`**, **told no pid and no pgid**. It discovers what is running by walking `/proc`
and deciding ownership from `/proc/<pid>/cwd` plus the parent chain — never from a pid it was
handed. It samples every ~0.2 s and writes one JSON record per sample.

```
samples: 381        span 77.674 s
sampling interval: min 0.198 s  mean 0.204 s  max 0.214 s
peak simultaneous owned process groups: 6
samples by owned-group count: {0: 222, 1: 27, 2: 24, 3: 24, 4: 25, 5: 23, 6: 36}
```

**36 consecutive samples showed six distinct owned process groups.**

> **THE SIMULTANEITY WINDOW: `12:24:54.138Z` → `12:25:01.291Z`, `7.153 s`,**
> **36 samples at ~0.2 s, contiguous (no sample inside it dropped below six),**
> **and exactly one pgid set observed throughout: `[737398, 737456, 737522, 737586, 737650, 737717]`.**

Those six pgids are the six the driver independently reported — the walk was not told them and
arrived at the same set. The climb and the teardown, sampled:

```
12:24:29.228Z  0 groups   []
12:24:29.637Z  1 group    [737398]
12:24:34.942Z  2 groups   [737398, 737456]
12:24:39.847Z  3 groups   [737398, 737456, 737522]
12:24:44.538Z  4 groups   [737398, 737456, 737522, 737586]
12:24:49.446Z  5 groups   [737398, 737456, 737522, 737586, 737650]
12:24:54.138Z  6 groups   [737398, 737456, 737522, 737586, 737650, 737717]   <-- window opens
12:25:01.494Z  4 groups   [737398, 737456, 737522, 737586]                   <-- window closes
12:25:01.696Z  3 groups   [737398, 737456, 737522]
12:25:01.898Z  1 group    [737398]
12:25:02.103Z  0 groups   []
```

Ownership, established structurally rather than asserted — every one of the six, from the walk
itself:

```
pgid 737398 -> 737398 python -m ai_dev_flow.claude_worker
            <- 737260 python -m ai_dev_flow.role_driver_dispatch
            <- 737243 bash /root/aidev/cp81/run.sh  <- /init <- /init <- systemd
   cwd /root/aidev/cp81/ws
(identical chain and cwd for 737456, 737522, 737586, 737650, 737717)
```

`foreign claude processes seen (never signalled): [0]` — no other lane was running Claude on
this host during the window; every process the walk classified as anybody else's was counted
and left alone, and none existed.

## 5. `process_group_alive` true for all six at one instant, false after teardown — PROVEN

The sampler calls the **shipped** `claude_worker.process_group_alive` on the groups it found
itself:

```
process_group_alive true for every discovered group in every one of the 36 window samples: True
```

After teardown, on the same six groups, from a separate process:

```
pgid 737398 alive=False    pgid 737586 alive=False
pgid 737456 alive=False    pgid 737650 alive=False
pgid 737522 alive=False    pgid 737717 alive=False
every group gone: True
```

Corroborated by the independent walk, which is the same evidence obtained without the
predicate: after `12:25:02.103Z`, **220 further samples spanning 44.8 s all show zero owned
groups**.

## 6. Every one of the six proven gone; no slot leaked — PROVEN

Four independent means, for all six groups:

1. the shipped `process_group_alive` — `False` for all six;
2. the driver's own teardown report — `process group gone True graceful True` for all six,
   `binding unbound` for all six;
3. direct `/proc/<pid>` presence — all six pids and all six pgids **absent**;
4. the independent `/proc` walk and a `ps` sweep for `claude_worker|_bundled/claude|ai_dev_flow`
   — walk returns only the proof script itself, sweep returns `none`.

No timeout, no exit 137/143 and no lost process contact occurred anywhere in this run, so the
"prove the prior process gone before running anything further" rule never had to be invoked.

And the accounting:

```
live occupancy after release: 0 / 6           (the driver's own exit reading)

a fresh controller that owns no handles, reading the same store afterwards:
  owned session handles: 0
  agent_count():  {'permitted': 6, 'current': 0, 'reason': None}
  occupancy():    AgentSlots(ceiling=6, occupants=(), unprovable=())
  records: 6, all state=unbound, all terminal=True
```

`current` is `0` with `reason None` — an established count of nothing running, not an
unestablished one. **No slot leaked.**

---

## Fixture vs real

**Real.** Six live provider sessions, each with its own on-disk transcript under its own minted
id, its own tool calls against its own rail, and its own worker process group. A real Git
control plane with a real bare origin. Every occupancy figure an `agent_count()` reading taken
by the controller that admitted the sessions. Every liveness answer either the shipped
`process_group_alive` or an independent `/proc` walk. The refusal is the accepted
`authorization.authorize` predicate's own, raised through the shipped door. The entry point,
the driver, the admission, the launch, the teardown and the reconciliation are all shipped code,
unmodified.

**Fixture — the scope only.** `dogfood/cp81`, the seven rails and their trivial assignments are
mine, and so is the choice of workload. The rail *shape*, the authorization rules, the binding
mechanics and the launch path are the product's.

**Neither product nor fixture.** The pre-flight rehearsal, the independent sampler, the analysis
and the post-check are harness scripts: not product code, not committed, and no product decision
was taken from any of them. They only read.

**Nothing here is fixture-proved.** No store was arranged, no record was written by hand, no
liveness was asserted, and no predicate was handed a fabricated `AgentSlots`.

---

## Suite

Pristine WSL-native clone at `916c739dde00d9e71764678bcf0b947fd0f95002`, clean, **never the
dogfood workspace**.

| interpreter | result |
|---|---|
| `/root/aidev/host/venv/bin/python`, Python **3.14.4**, `claude_agent_sdk` **0.2.152** | `Ran 2842 tests in 117.786s` — `FAILED (failures=1, errors=1, skipped=4)` |
| system `python3` **3.14.4**, no SDK (`ModuleNotFoundError: No module named 'claude_agent_sdk'`) | `Ran 2842 tests in 113.117s` — `FAILED (failures=1, errors=1, skipped=6)` |

**Both are exactly the stated baseline, including the 4 / 6 skip split.** The two non-passing
outcomes are the baseline's, by id, on both interpreters:

- `FAIL: test_pep604_annotations_are_postponed_in_package_modules (tests.test_python_minimum_compatibility.PythonMinimumCompatibilityTests…)`
- `ERROR: test_the_checkpoint_three_loop_closes_without_a_provider (tests.test_orchestrator_outcome.FreshReviewLoopTests…)`

**No new failure and no new error.** An earlier pass of the same two suites on the same clone
gave 114.884 s and 113.140 s with identical counts, so the figures are stable across two runs.

---

## Trades

- **Occupancy over throughput.** Each rail carried one trivial instruction, so the run cost six
  cheap sessions and about 33 s of driver wall clock. The alternative — six agents doing real
  work — would have proved the same ceiling at many times the cost and with many more ways to
  lose a session. Stated, not hidden: this proves the ceiling holds, not that six *working*
  agents are sustainable.
- **Seven rails rather than one.** Forced by `binding-duplicated` preceding the ceiling check.
  The cost is that the scope is visibly a fixture; the benefit is that the refusal that fires is
  the one being tested.
- **One prompt file per launch.** The directive is a product constant that does not name a rail,
  so with seven rails in one scope each session had to be told which rail is its own through the
  one per-launch operator input that can say it. That input is the system prompt file — which is
  the recorded residual "system prompt file not bound to role", used here, not closed.
- **A pre-flight that spends nothing.** One extra script, in exchange for not discovering a
  configuration fault with six paid sessions on the host.
- **Control plane inside the workspace** (`ws/.cp81`, git-excluded) so the rails are reachable by
  a workspace-rooted tool set without granting the worker any directory outside its `cwd`. The
  cost is that this is not a production layout.

---

## Limits — leading with the one most likely to undermine this result

1. **Six managed sessions were simultaneously live; six provider turns were not.** The
   independent walk's owned-process count peaked at **7** — six workers plus **one** provider
   child. Across the 36 window samples, **32 showed exactly seven processes** (six workers plus
   one child) and **4 showed six** (no child at all), so at no sampled instant were even two
   provider CLI processes executing, let alone six. This is the shipped
   driver's documented shape, not an artefact of my run: `role_driver` admits and enacts
   strictly one launch at a time, because a slot is consumed by `reserve_binding` inside
   `launch_session` and admitting a batch would require a second count the accepted state
   forbids. What is proven simultaneous is **liveness, ownership and D6 accounting**. If
   "six agents at once" is read as six concurrent provider turns, **this run does not prove
   that**, and nothing in the package can currently produce it.
2. **The all-six window is 7.153 s and its length is not mine to choose.** The shipped
   `while_held` observer returns immediately and `_release` follows, so the window is
   essentially the duration of the sixth launch's provider invocation. Holding six longer is
   not reachable through the shipped entry point without changing product code, which this rail
   forbids.
3. **The load was trivial by design, so the host was never stressed.** Peak owned RSS 0.67 GiB
   across all six groups; `MemAvailable` never dropped below 14.02 GiB of 15.50 GiB; owned file
   descriptors 18–48; system-wide open file handles 2,368–2,496; 1-minute load 0.28–0.34. The
   host sustained six comfortably — but six *idle* sessions and one working provider child. This
   says nothing about six agents each doing real work.
4. **N = 1.** One run, one host, one ceiling value (the default 6), one ordering of roles. No
   repeat, no different ceiling, no interleaving other than the one the command line stated.
5. **Sampling is 0.2 s, so the window's edges are ±1 interval** and a sub-interval dropout
   inside it would not have been seen. It was contiguous across 36 samples, which bounds but
   does not eliminate that.
6. **The scope is a dogfood fixture.** No production ticket rail was driven, and the six
   assignments were written to be answered in one turn.
7. **"Nothing reserved for the seventh" is proven by absence** — no lane-seven record in the
   store, no seventh group in the walk — not by instrumenting `reserve_binding`. The code path
   makes it structural (the refusal is raised before `launch_session`), but the evidence here is
   observational.
8. **`pidDomain` is the WSL hostname**; every liveness proof is valid inside that domain only.
9. **No ticket claim exists in the workspace**, so `verify_workspace_ticket_identity` returned
   no problem for the ordinary single-worktree reason. The claim-conflict path is still untested.
10. **The bundled-CLI residual is unchanged**: the product still neither pins nor reports which
    CLI binary a managed session executed. The version here is read off the transcripts
    (`2.1.259`), not off a product record.

---

## Boundaries recorded, not proved

The seven properties this ticket has recorded as unreachable — 7, 8, the 3-loop, 14-arising,
9-recovery, 13-runnable, and 4 — remain unreachable and **none of them was fixture-proved
here**. The condition-4 echo surface was not touched: it is real and fail-closed, and both its
polarities were already observed live at checkpoints 79 and 80. `decision_manager.html`'s
response composer was neither opened nor clicked.

Every recorded residual is still open and none was closed: the system prompt file not bound to
role; role fidelity resting on the skill directory name; the alias hole; the orchestrator entry
point's missing early refusal; the leaked-`reserved`-on-disguised-mismatch defect; the dead
`claude_worker` shims; and the bundled-CLI pinning/reporting gap.

---

## Unresolved work and the exact next action

Blocker 1 (rotation end to end) closed at checkpoint 80. Blocker 2 (this) is closed as far as
the shipped path can close it, with limit 1 above stated rather than absorbed.

**Exact next action:** the orchestrator reviews checkpoint 81 and decides whether the six-agent
ceiling requirement of named checkpoint 9 is satisfied by *simultaneous liveness at the
ceiling*, given that simultaneous provider execution is not reachable in the package as it
stands. That decision is the orchestrator's; nothing here completes named checkpoint 9, and the
middle-cut boundary — no deferred response routing, no autonomous continuation loop — is
intact.

---

## Verification

`manager_dispatch.py` and `orchestrator_invocation.py` are byte-for-byte as pinned to `c0b6a3a`:
`1501bf2dbd0a8e680e56f452fb5239e09d9ec75a` and `e63a79586eb56a6610adc8657810a2a34c775750`.
No product code was changed. `main` was not moved, nothing was promoted or accepted, `state.md`
and every other rail were left alone, `skills/**` was neither modified nor activated, and Issues
#74 and #76 and WoW/Coxswain were not touched. The dogfood workspace under `/root/aidev/cp81`
is deleted after the run.

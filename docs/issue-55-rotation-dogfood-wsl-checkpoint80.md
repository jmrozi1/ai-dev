# Issue #55 — checkpoint 80: rotation, end to end, on WSL

The first **complete** graceful rotation this ticket has driven on this host: a genuine
compaction, the threshold mark it earned, a safe boundary with the agent's own handoff made
durable, retirement through the accepted gate, a replacement bound under its own minted id,
and that replacement continuing the rail from durable state alone. It adds **no mechanism**
and changes **no product code**. Every route driven is the shipped one; the driver that
composes them is not product code and is not committed.

**Read the limits section first if you read only one thing.** The result that most
undermines this document is that the whole of it rests on **exactly one observed
compaction**, with D9's rotation threshold lowered from its default of six to **one**. The
counter was never watched accumulating, and a defect in accumulation would not have been
found here.

This closes **blocker 1 of the 2 remaining** for named checkpoint 9. **The second blocker —
six live agents at the D6 ceiling — was not attempted and is not claimed.** Named
checkpoint 9 is **not** complete and is not claimed complete.

---

## What checkpoint 79 got wrong, and why this slice was reachable

Checkpoint 79 attempted this rotation and failed honestly at its first gate: a
controller-issued `/compact` returned `num_turns: 0`, emitted no `compact_boundary`, left
`rotationMarked: false`, and the checkpoint reported the cause as unknown.

The cause was in an artifact checkpoint 79 itself cites. In its rotation predecessor's
transcript, at record 21:

```
{"type":"system","subtype":"local_command",
 "content":"<local-command-stdout>Not enough messages to compact.</local-command-stdout>"}
```

`/compact` was delivered at record 20, with **one** user turn, **one** assistant reply and
the `/compact` turn itself ahead of it. **The session had two turns.** The variable was
workload size — not the host, not the CLI version, not the product.

**So the sequencing was the whole slice**, and this run is built around it: drive the
session with real work, **measure the context and prove it is large enough**, and only then
compact. The correction note appended to
[`issue-55-checkpoint-9-dogfood-wsl-checkpoint79.md`](issue-55-checkpoint-9-dogfood-wsl-checkpoint79.md)
records both refutations against that document without editing its narrative.

### `num_turns: 0` was a red herring, and this run proves it

The successful compaction here **also** returned `num_turns: 0`. That field says nothing
about whether a compaction happened. The two discriminators are the **`compact_boundary`
event** and the **absence of the `local_command` refusal**, and both are recorded below.

---

## Environment, established first-hand

| | |
|---|---|
| host | Windows 11 + WSL Ubuntu, every invocation as `-u root` |
| CLI **actually executed** | **`…/site-packages/claude_agent_sdk/_bundled/claude`, `2.1.259`** — the SDK's bundled binary, observed in `/proc` on a live managed session |
| CLI on `PATH` | `/root/.local/bin/claude` `2.1.261` — **a different binary that no managed session ever runs** |
| worker interpreter | **`/root/aidev/host/venv/bin/python`**, Python **3.14.4**, `claude-agent-sdk` **0.2.152** |
| `PATH` hygiene | `/mnt/` stripped for every measurement and for the run itself; with it stripped there is **no Linux `node`** on this host (`command -v node` → none) |
| workspace | WSL-native standalone **primary** clone `/root/aidev/cp80/ws`, detached at **`9e566b72d8d3531858fb15584242dbb82766a1e8`** (checkpoint 79), **clean before, during and after**, no active ticket claim |
| controller root | `/root/aidev/cp80/controller-root`, one plugin package and one prompt per role |
| dogfood control plane | `dogfood/cp80`, a real Git repo at `ws/.cp80` with a bare origin; `.cp80/` is in `.git/info/exclude`, which is what keeps the workspace clean while the handoff is reachable by a workspace-rooted tool set |

The exact request the worker built is not asserted — it was read off the live process:

```
…/claude_agent_sdk/_bundled/claude --output-format stream-json --verbose
  --system-prompt-file /root/aidev/cp80/controller-root/prompts/executor.md
  --allowedTools Read,Glob,Grep --max-turns 60 --max-budget-usd 8.0
  --permission-mode dontAsk --resume=fb95abf8-6b8b-4b6e-882a-c9eb9d7057a1
  --strict-mcp-config --setting-sources=
  --plugin-dir /root/aidev/cp80/controller-root/packages/ai-dev-executor
```

**The bundled-CLI finding is new and it corrects checkpoint 79.** Its environment table
records `2.1.261` from `/root/.local/bin/claude`; that binary is not what runs. All eight
of checkpoint 79's transcripts and all three of this checkpoint's record **`2.1.259`**, so
the CLI version is a **controlled** variable across the two runs rather than a candidate
cause of anything.

---

## Provider budget — 3 of 6 spent

| # | session id | pgid | what it was | outcome |
|---|---|---|---|---|
| 1 | `1f35f5ce-c95e-444e-bf1e-9deeb197792a` | 582183 | first launch attempt | **spent on my own bound**: `Reached maximum budget ($2)` |
| 2 | `fb95abf8-6b8b-4b6e-882a-c9eb9d7057a1` | 582560 | **rotation predecessor** | 4 invocations, compacted, published its handoff, **retired** |
| 3 | `9ea831c5-d0dc-4567-83a9-15c3ccfa94f5` | 583279 | **replacement** | continued from durable state, stopped |

Each wrote a provider transcript on disk under **its own minted id and no other** (47.3 KB
/ 386.7 KB / 600.3 KB; 27 / 117 / 115 records). That is what makes them real rather than
asserted.

**One of three was spent on my own bound, and that is my defect, not the package's** — the
same failure family checkpoint 79 recorded three times. My `max_budget_usd` of $2 cut the
launch invocation mid-work and `launch_session` correctly refused to hand back a session
whose launch invocation failed. What it cost was one session and one leaked `bound` record;
what it bought was the measurement that fixed the run: **that dead session had already
reached 65,425 context tokens**, which is what told me a single invocation could carry the
context past the compaction floor.

### Termination, proven four independent ways for every process group

By the shipped `process_group_alive`; by an **independent `/proc` walk in a separate
process that holds no registry and no binding store and was never told a pid**; by direct
`/proc/<pid>` presence for every pid; and by a `ps` sweep for any `claude_worker`,
`_bundled/claude` or `ai_dev_flow` process anywhere on the host.

```
pgid 582129 alive=False  driver run 1              pgid 582560 alive=False  worker fb95abf8 predecessor
pgid 582183 alive=False  worker 1f35f5ce           pgid 583279 alive=False  worker 9ea831c5 successor
pgid 582506 alive=False  driver run 2

pid 582129 absent   pid 582183 absent   pid 582186 absent   pid 582506 absent
pid 582560 absent   pid 582815 absent   pid 583279 absent

independent /proc walk for anything under /root/aidev or running ai_dev_flow: [] (only the proof script itself)
ps sweep for claude_worker | _bundled/claude | ai_dev_flow: none
```

**No process outlived its owner.** No timeout, no exit 137/143 and no lost process contact
occurred anywhere. When session 1's launch failed, nothing further was run until its group
was proven gone by all four means. Other lanes' Claude processes were enumerated before and
after and none was touched; ownership was established from `/proc/<pid>/cwd` =
`/root/aidev/cp80/ws` and the parent chain, never from a pid.

One detail worth recording: the worker spawns a **fresh** CLI child per invocation (pids
582186, 582815 on the same pgid 582560), so the **process group**, not the pid, is the
stable identity — which is exactly what `process_group_alive` takes.

---

## 1. A GENUINE COMPACTION — **PROVEN**, and here is the size it succeeded at

The context was built by real work and **measured before the compaction was issued**, which
is the whole of what checkpoint 79 got wrong. The harness gate is `max` over every assistant
turn's `input_tokens + cache_creation_input_tokens + cache_read_input_tokens` in the CLI's
own transcript:

```
tokens after launch                : 27,928
tokens after work invocation 1     : 99,840
pre-compaction-size gate           : tokens=99,840  floor=40,000  target=60,000   -> PASS
```

Only then was `/compact` issued, through the shipped `continue_session` route. The event the
product observed:

```json
{"event": "compaction-observed",
 "session_id": "fb95abf8-6b8b-4b6e-882a-c9eb9d7057a1",
 "uuid": "b31cb5b3-1c44-43a5-a2d9-10c923b536d1"}
```

and the CLI's **own** record of the same instant, written to its transcript:

```json
{"type":"system","subtype":"compact_boundary",
 "uuid":"b31cb5b3-1c44-43a5-a2d9-10c923b536d1",
 "sessionId":"fb95abf8-6b8b-4b6e-882a-c9eb9d7057a1",
 "timestamp":"2026-09-05T11:48:24.542Z","cwd":"/root/aidev/cp80/ws","entrypoint":"sdk-py",
 "compactMetadata":{"trigger":"manual","preTokens":105543,"postTokens":7114,
                    "durationMs":147404,"cumulativeDroppedTokens":98429}}
```

**The uuid and session id match exactly**, which is what makes the product's count a
reading of that boundary rather than a coincidence beside it. `trigger: "manual"` is the
controller-issued compaction, not an automatic one.

> ### THE TOKEN SIZE AT WHICH COMPACTION SUCCEEDED
>
> **`preTokens: 105543`** — the CLI's own figure for the context it compacted, down to
> `postTokens: 7114`, taking **147.4 seconds**. My harness independently measured
> **99,840** immediately before issuing the command. Checkpoint 71 compacted at 68,387 and
> 68,257; checkpoint 79's failed attempt had **two turns**.

The result payload, for completeness and to kill the red herring:

```json
{"subtype":"success","is_error":false,"num_turns":0,"total_cost_usd":0.515436,
 "events":[{"event":"compaction-observed", …}]}
local_command records in this session: []
```

`num_turns: 0`, exactly as at checkpoint 79 — **and a real compaction happened**. The
`local_command` list is empty: no `Not enough messages to compact.` this time. Those two
facts together are the discriminator; the turn count never was.

## 2. THRESHOLD MARKING — **PROVEN**, at a stated threshold, and the count is not faked

`ManagerController(source, rotation_threshold=1)` — D9's policy, human-owned and stated at
the controller, sharing nothing with D6's ceiling, which stayed at its own default of 6 in
the same object. One genuine compaction reaches it:

```json
{"sessionId":"fb95abf8-6b8b-4b6e-882a-c9eb9d7057a1","role":"executor",
 "contextPolicy":"persistent","health":"healthy-complete-from-session-start",
 "observed":1,"count":1,"threshold":1,"rotationMarked":true,
 "detail":"1 observed compactions reach the rotation threshold of 1; this session is
           marked for graceful rotation at a later safe boundary."}
rotation-marked sessions: ["fb95abf8-6b8b-4b6e-882a-c9eb9d7057a1"]
```

`observed` and `count` are both 1 and the health is `healthy-complete-from-session-start`,
which is the only state under which `count` exists at all. **Nothing was injected into the
counter**: the single increment came from the `compaction-observed` event above, carrying
the identity pair the CLI wrote, and the mark survived the *next* invocation unchanged —
the reading after the handoff turn is byte-identical — which is the count carrying across
an exact resume.

## 3. THE SAFE BOUNDARY — **PROVEN**, and the ordering is structural rather than promised

The next bounded command asked the predecessor for its handoff. It answered in **one turn**
(`$0.19365`), ending its final message with exactly one handoff envelope. The controller —
**not the agent** — then made those bytes durable, from
`finalize_terminal_handoff`, which runs *after* `sender` has returned and therefore after
the provider turn has provably ended:

```json
{"state":"terminal-handoff-established",
 "detail":"session fb95abf8…'s terminal handoff was published as
           cdefaa6c5e01b663277d287a40bba0dfbe486895 at work boundary 3,
           after its provider turn ended.",
 "publication":"cdefaa6c5e01b663277d287a40bba0dfbe486895"}
```

The finalizer is `terminal_finalizer(publish=…, bookkeeping=push)`: publish first, push
second, and **a push failure is a finalization failure**. At checkpoint 79 this fired
`durable-publication-failed` because the control plane was read between the two steps; here
the two steps are adjacent inside the finalizer and nothing reads between them, so it did
not fire. That is the harness fixed, not the product changed.

The published bytes are the agent's own, byte for byte, and they carry what the boundary
requires — worktree/repository state, current outcome and evidence, unresolved work, and
the exact next action. The last of those, verbatim from the publication:

> **Exact next action for a fresh agent on this rail:** Do not re-run either completed
> command. Read `.cp80/dogfood/cp80/rails/rot-rail/rail.md` fresh, confirm Status is still
> `running` and that the stated assignment is unchanged from the one recorded above, then
> stop and wait for the next bounded command from the orchestrator. If and only if
> `rail.md` has been amended with a new assignment, execute that amended assignment
> exactly as written, within its stated bound, reading only what it names and writing
> nothing.

The commit that carries it records the product state it was written against:

```
78bce44d906ad054ca2bc8dd869017c757c20d25  executor: handoff rot-rail (dogfood/cp80)
Work-State: 9e566b72d8d3531858fb15584242dbb82766a1e8
HEAD:…/handoff.md        -> cdefaa6c5e01b663277d287a40bba0dfbe486895
origin/main:…/handoff.md -> cdefaa6c5e01b663277d287a40bba0dfbe486895
```

Readiness was then projected from **fresh** reads taken at that instant, and only then was
anything terminated:

```
state:  rotation-ready
reason: durable-handoff-established
detail: session fb95abf8… is marked at 1 of 1 observed compactions, is between invocations,
        has a coherent workspace, and its rail carries handoff publication cdefaa6c…,
        established at the work boundary 3 this session is still standing at and written
        against product state 9e566b72…, which is where the workspace still stands.
        Nothing is terminated or replaced by this.
facts:  rail   status=running blob=3d10518b…
        handoff published=true publication=cdefaa6c… work_state=9e566b72…
        worktree clean=true operation=None head=9e566b72…
```

All six of `evaluate_rotation_readiness`'s conditions were satisfied by facts, not by
assertion: marked; no invocation in flight; a clean workspace with no active Git operation;
a published handoff; **a publication this controller finalized itself at the work boundary
the session is still standing at**; and a product repository that has not moved since.

## 4. RETIREMENT — **PROVEN**, through the accepted gate

```
state:  old-context-retired
detail: session fb95abf8… was rotation-ready at the instant of retirement on handoff
        publication cdefaa6c…, and this controller's own handle for pid 582560 was proven
        to be the process the binding names; process group 582560 is gone (acknowledged
        shutdown, exit code 0), the binding is unbound, and nothing was launched or bound
        in its place.
stopped: pgid=582560 process_group_gone=true graceful=true binding_state=unbound
```

The gate took its **own** readiness projection immediately before acting — there is no
`readiness=` parameter through which the one from item 3 could have authorized this — and
it proved ownership before it asked about readiness at all. The retirement route is also
the *only* way this session could have been stopped: `_stop_owned_process` re-reads the
category from the registry and refuses a rotation-marked session to ordinary teardown,
which is why a route that forgot to ask still cannot terminate one.

## 5. REPLACEMENT LAUNCH WITH ITS OWN MINTED ID — **PROVEN, and distinct**

```
predecessor  fb95abf8-6b8b-4b6e-882a-c9eb9d7057a1   pgid 582560   binding: unbound
successor    9ea831c5-d0dc-4567-83a9-15c3ccfa94f5   pgid 583279   binding: bound
distinct_from_predecessor: true
```

Distinct in the durable records, not merely in memory. The two binding files on disk are
separate records with different session ids, different pids, different `reservedAt`
(`11:44:40Z` and `11:48:48Z` — the successor reserved **after** the retirement) and even
different `launchedAtHead` (`3347898b…` and `78bce44d…`, because publishing the handoff
moved the coordination head between them). The successor also wrote its own transcript
under its own id and no other.

Reuse is not merely avoided, it is unrepresentable: `replace_old_context` wraps the minter
in `successor_id()`, which raises `successor-identity-reused` if the mint returns the
predecessor's id.

Nothing was sent to it by this route. The product says so itself:

> The successor is a distinct session that holds none of the predecessor's context, one
> slot was released before one was occupied, and nothing has been sent to it: continuing
> from the durable handoff is a separate act.

## 6. CONTINUING FROM DURABLE STATE ALONE — **PROVEN**, and structurally so

`continuation_brief(rail, record, handoff, worktree)` **takes no registry**. The one object
holding this session's results, events, work boundary and finalization is not in its
signature, so no field of the brief can be derived from any of them. What it resolved
carries locators and nothing else:

```json
{"project":"dogfood","ticket":"cp80","rail":"rot-rail","role":"executor",
 "iteration":"3d10518b58f0dcafbc5844da944eec8ebc4eebd2",
 "workspaceKey":"github:jmrozi1/ai-dev#55","worktreeId":":primary:",
 "workspacePath":"/root/aidev/cp80/ws",
 "worktreeHead":"9e566b72d8d3531858fb15584242dbb82766a1e8",
 "handoff":".cp80/dogfood/cp80/rails/rot-rail/handoff.md",
 "handoffPublication":"cdefaa6c5e01b663277d287a40bba0dfbe486895"}
```

Not one sentence of the handoff, no result, no terminal payload, no provider message. The
rendered prompt is locators plus the one non-locator sentence the route is entitled to: the
protocol for reporting that the brief did not arrive.

`continue_from_durable_state` reported:

```
state:  continuation-continued
detail: … No transcript of the predecessor was read, held, or required, and the replacement
        did not report the brief unreachable on the channel it was asked to report it on.
        That is the whole of what is established: this route does not establish that the
        replacement read or understood the handoff, only that it did not say it could not
        reach it.
result: subtype=success is_error=false num_turns=3 cost=USD 0.0923225
```

## 7. THE SWAP'S D6 ACCOUNTING — **PROVEN**: no slot leaked, none double-counted

Every figure below is a `ManagerController.agent_count()` reading recorded verbatim at the
instant named. Nothing was computed anywhere else.

| instant | reading |
|---|---|
| entry | `1 permitted 6 / current 0` |
| predecessor live | `current 1 / 6` |
| before the swap | `current 1 / 6` |
| **after the swap** | **`current 1 / 6`** |
| successor continued | `current 1 / 6` |
| exit | `current 0 / 6` |

`current` is never `None`, so ownership was provable at every one of those instants — the
count is an established number, not a zero standing in for an unknown.

**One in, one out, and the order is the product's.** Inside `replace_old_context` the
predecessor's binding is terminalized *first*, the store is then re-read, and occupancy is
reconciled from those records **before** the successor's authorization is decided — so the
released slot is already released when the replacement is admitted. The durable evidence of
that ordering is in the records themselves: `unbound` for the predecessor at the moment the
successor's reservation timestamp was written. A leaked slot would show as `current 2 / 6`
after the swap; a double-count would show as `current 2 / 6` during it. Neither appears,
and the run ends at `0 / 6` with both bindings terminal.

**What this does not show, and the limits repeat it:** occupancy never exceeded **one**.
This is a rotation of a single agent, not a rotation at the ceiling.

## 8. THE CONDITION-4 MARKER ON THE REPLACEMENT — **NEGATIVE, and that is a real result**

```
markers: {"AI-DEV-BRIEF-UNREACHABLE": false}
state:   continuation-continued
```

**It did not fire.** The brief the replacement was handed contains the literal
`AI-DEV-BRIEF-UNREACHABLE` — it must, because a replacement cannot emit a word it was never
given — and `_scan_markers` is a bare substring test (`if marker and marker in text`), so
any reply that quoted its own instructions back would have tripped it. This reply did not.

The negative is the correct answer here, because the handoff genuinely *was* reachable: it
was published into the replacement's own tool root at
`.cp80/dogfood/cp80/rails/rot-rail/handoff.md`, a 3,686-byte file inside the workspace the
worker's `cwd` names. Checkpoint 79 drove the positive half against a brief outside the
tool root; this is the discriminating half against a brief inside it, on a different run,
a different session and a different control plane. **Both polarities are now observed
live, and neither fired spuriously.**

The echo surface remains real and remains the accepted polarity: every false positive lands
fail-closed on `continuation-brief-unreachable`, which refuses to credit the turn, so an
echo costs a turn and never buys a false credit. **Nothing was done about it.**

### What the replacement actually did — offered as observation, not as a claim of the route

The route is entitled to say only that the replacement did not report the brief
unreachable, and it says exactly that. Its reply happens to establish more, and it is worth
recording that a real replacement on a real rail did the useful thing:

> **Confirmation against `rail.md` (read fresh, just now):** Status: `running` — unchanged.
> … That assignment is the one the handoff records as already answered … `rail.md` has
> **not** been amended with a new assignment, so the conditional branch in the handoff's
> next action does not fire, and I did not re-run either completed command.

It read both durable artifacts, executed the handoff's exact next action, and correctly
declined the conditional branch. **The route does not claim this and is not entitled to
it**; a replacement that read nothing and said nothing would have been reported green too,
which the product disclaims in its own comment beside the marker. This paragraph is an
observation about one session, not a property.

---

## The seven unreachable properties — named boundaries, none fixture-proved

Unchanged from checkpoint 79 and under the same accepted middle cut. **None was simulated
and no fixture stands in for any of them:** 7 (out-of-order responses), 8 (routing back
through the orchestrator), 3's loop half, 14's arising half, 9's recovery half, 13's
"runnable" half, and 4. Nothing here reaches a response channel, work discovery, a
continuation loop, or a verdict destination.

`ai_dev_flow/decision_manager.html`'s response composer — which manufactures apparent
evidence for exactly properties 7 and 8 while only splicing an item out of memory — **was
not opened, not served, not clicked, and nothing here rests on it.**

## What was FIXTURE and what was REAL

**Real:** three provider sessions against the live provider under a max subscription, each
with its own on-disk transcript; a real 105,543-token compaction with its own
`compact_boundary` record; a real Git control plane with real commits, a real bare origin
and a real push; the product's own publication of the agent's own bytes; a real retirement
with a real process group proven gone; a real second worker process; every occupancy figure
a `ManagerController.agent_count()` reading; every liveness answer either
`process_group_alive` or an independent `/proc` walk.

**Fixture:** the *scope* only — `dogfood/cp80` is a control plane I created, `rot-rail` is a
rail I authored, and the workload (which files the executor was told to read) was chosen by
me to build context. The rail's shape, the publication mechanics and the authorization it
carries are the shipped ones; what is fixture is that this is not a production ticket rail.

**Neither:** the driver. It is not product code, is not committed, and composes only
shipped routes — `open_role`, `continue_session`, `evaluate_rotation_readiness`,
`terminal_finalizer`, `replace_old_context`, `continue_from_durable_state`, `stop`. It reads
the CLI's own transcript to size the context, which is an out-of-band measurement and is
labelled as one; **no product decision was taken from it** — the product's compaction count
comes only from the `compact_boundary` event.

## Suite

Run on a **pristine WSL-native clone** at checkpoint 79
(`9e566b72d8d3531858fb15584242dbb82766a1e8`), clean, **never the dogfood workspace**, so the
dogfood's own artefacts could not touch the figures.

**Interpreter: `/root/aidev/host/venv/bin/python`, Python 3.14.4, `claude-agent-sdk 0.2.152`.**

```
Ran 2842 tests in 114.870s
FAILED (failures=1, errors=1, skipped=4)
```

Same clone, **system `python3` 3.14.4, no SDK** (`find_spec('claude_agent_sdk') is None`):

```
Ran 2842 tests in 110.488s
FAILED (failures=1, errors=1, skipped=6)
```

**Both figures are exactly the stated baseline**, including the 4/6 skip split. The two
non-passing outcomes are the two the baseline names, unchanged and by the same ids:

- FAIL `tests.test_python_minimum_compatibility.PythonMinimumCompatibilityTests.test_pep604_annotations_are_postponed_in_package_modules` — `ai_dev_flow/progress_record.py`
- ERROR `tests.test_orchestrator_outcome.FreshReviewLoopTests.test_the_checkpoint_three_loop_closes_without_a_provider` — `authorize() missing 1 required keyword-only argument: 'slots'`

**No new failure and no new error. Nothing here is mine.**

## Trades

- **One rotation, driven properly, over many properties driven thinly.** The budget was
  spent on making a single compaction genuinely happen rather than on breadth. Six sessions
  were authorized and three were spent.
- **The rotation threshold was lowered to 1.** The alternative — six real compactions —
  would have cost roughly six times the context-building and most of the budget, to observe
  the same mark. What it would additionally have proved is exactly what the leading limit
  says is unproven, and that trade is stated rather than hidden.
- **The control plane was put *inside* the workspace** (`ws/.cp80`, excluded from the
  workspace's own Git). That makes the published handoff reachable by a workspace-rooted
  tool set without granting the worker any directory outside its `cwd`, and it removes a
  whole class of staleness between what the controller publishes and what the replacement
  reads. The cost is that this is not how a production checkout is laid out.
- **The workload was reading, never writing.** A writing workload would have dirtied the
  worktree and been refused at readiness condition 3, which is the product working; proving
  that refusal was not this slice's job, so the workload was chosen to keep the boundary
  reachable.
- **The failed first run was kept and reported rather than deleted.** Its leaked `bound`
  record is preserved in a separate binding root as evidence.

## Limits — leading with the one most likely to undermine this result

1. **Everything here rests on ONE observed compaction, with the threshold lowered from six
   to one.** `observed: 1` reaching `threshold: 1` is the smallest possible instance of the
   mechanism. The counter was never watched accumulating across two boundaries, the dedup
   of a replayed identity pair was never exercised against a real replay, and the
   `unhealthy-partial` and `unavailable-prior-history-unknown` readings were never reached.
   **If accumulation is broken, nothing in this document would have found it.**
2. **The swap's D6 accounting never exceeded one occupied slot.** "No slot leaked, none
   double-counted" is proven at `1 → 1` on a manager whose ceiling is 6. A rotation at the
   ceiling — the case the ordering inside `replace_old_context` exists for — is untested
   here, and is the second blocker, which is **not** mine.
3. **The scope is a dogfood fixture.** `dogfood/cp80` and `rot-rail` are mine; the workload
   is mine. Nothing here was driven on a production ticket rail, and a rail whose handoff
   convention or dependency graph is more complex could fail at gates this rail cannot
   reach.
4. **`continuation-continued` is a narrow claim and the product says so.** It establishes
   only that the replacement did not report the brief unreachable. A replacement that read
   nothing and said nothing would be reported identically. The reply quoted in item 8 is
   one session's behaviour, not a property.
5. **One of three sessions was spent on my own bound** — the same defect family checkpoint
   79 recorded three times. A better-calibrated first bound would have made this a two
   session run.
6. **N = 1 for the rotation itself.** One predecessor, one replacement, one swap, one host.
   Nothing here speaks to flakiness, to repeated rotation of the same rail, or to a
   rotation interrupted partway.
7. **`pidDomain` is the WSL hostname.** Every liveness proof here is valid inside that one
   domain and says nothing about a pid on the Windows side.
8. **The workspace holds no ticket claim**, so `verify_workspace_ticket_identity` returned
   no problem for the ordinary single-worktree reason rather than by proving a claim. The
   claim-conflict path is untested here.
9. **The compaction took 147 seconds** and `postTokens` was 7,114 against `preTokens`
   105,543. Whether a compaction that aggressive preserves what a *working* agent needs is
   not a question this checkpoint asked, and the replacement never resumed the compacted
   context — it was a fresh session reading durable state, which is the point of rotation.
10. **A new residual, recorded not closed:** nothing in the product pins, records or
    reports which CLI binary a managed session actually executed. `claude --version` on
    `PATH` reports a binary the product never runs.

## Deliberately not done

No mechanism was changed, added or remediated, and **no product code was modified by this
checkpoint**. `manager_dispatch.py` and `orchestrator_invocation.py` were verified
byte-for-byte identical to their pinned hashes (`1501bf2dbd0a8e680e56f452fb5239e09d9ec75a`,
`e63a79586eb56a6610adc8657810a2a34c775750`). The echo surface in `_scan_markers` was **not**
"fixed"; it is the accepted polarity. **The second blocker — six live agents at the D6
ceiling — was not attempted and is not claimed.** Response routing and the autonomous
continuation loop were **not** implemented; the middle-cut boundary stands exactly where it
stood. No scheduler, queue, priority model, fairness policy, autoscaler or work discovery
exists. No recorded residual was closed: the system prompt file is still not bound to role,
fidelity still rests on the skill directory name, the alias hole, the orchestrator entry
point's missing early refusal, the leaked-`reserved`-on-disguised-mismatch defect and the
dead `claude_worker` shims all stand. No credential was copied, scraped or transplanted and
no authentication check was weakened. Nothing was ported to Windows. `main` was not moved,
no checkpoint was accepted, `state.md` and every rail were left alone, and nothing under
`skills/**` was read into scope, modified or activated. Issues #74 and #76 and WoW/Coxswain
were not touched. **Named checkpoint 9 is not complete and is not claimed complete.**

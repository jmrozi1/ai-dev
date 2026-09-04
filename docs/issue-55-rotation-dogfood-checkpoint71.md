# Issue #55 — checkpoint 71: rotation dogfood, re-run against the checkpoint-70 fix

The accepted rotation surface, driven end to end against a **real provider session**,
a second time. This checkpoint adds **no mechanism** and changes **no product code**.
It is the evidence for the one question checkpoint 70 could not answer about itself.

**Verdict: the rotation now completes end to end. Step F works.** The replacement's
provider conversation is created by the continuation's first invocation, under the
successor's own minted id; the continuation actually ran; the second invocation
resumes that same conversation rather than re-creating it; and a fresh reader holding
no registry resolves the successor's work from durable state alone.

At checkpoint 69 this step failed with
`No conversation found with session ID: 323fbf58-…`. It does not fail now.

---

## What was driven, and how it was real

Two runs. Both composed the accepted production routes exactly as a manager would:
`ManagerController.launch` → `continue_session` ×N → `terminal_finalizer` →
`evaluate_rotation_readiness` → `ManagerController.replace_old_context` →
`ManagerController.continue_from_durable_state` →
`ManagerController.release_continued_context`.

Every authorization came from the accepted `authorize` over an `observe_scope` read of
a real Git control plane. Every session ran in the accepted `ai_dev_flow.claude_worker`
worker process against `claude-agent-sdk 0.2.152` on `/usr/bin/python3.12`. Nothing was
stubbed, faked, injected or simulated. The driver is not product code and is not
committed.

| | run A (the dogfood) | run B (supplementary) |
|---|---|---|
| fixture | `dg2` | `dg3` |
| rotation threshold | **2** | **1** |
| D6 ceiling | 6 (default) | 6 (default) |
| priced provider turns | 9 | 7 |
| real spend | **USD 2.81** | **USD 1.58** |
| purpose | A–G, the whole rotation | close the two gaps run A left |

**Total real provider spend: USD 4.39 over 16 completed invocations.**

Run B exists because run A left two things unproven — one through a bug in my own
harness, one through a timing race. Both are named in full below.

## A. A real managed session, launched and bound

- run A predecessor: **`79f58bcf-609f-40b3-9684-30a6d4410098`**, pid = pgid **518878**,
  `bound`, SDK `0.2.152`, launch invocation `subtype: success`
- run B predecessor: **`1c5b23c6-0f2f-4552-bcff-e92a166f3bec`**, pid = pgid **520106**,
  `bound`, SDK `0.2.152`

Authorization in both: `launch-authorized`, *"rail 'rotation-dogfood' is running at
iteration `2619caab79e6c01118dfc4336e5931d823d5b30b` with no live binding."*

## B. Real observed compactions, reaching the threshold

Counted by `context_lifecycle.decode_lifecycle_event` from real `compact_boundary`
events in `message.data`, deduplicated by `(session_id, uuid)`. Never
`status: "compacting"`, never `compact_progress`, never an injected count.

**Run A — two boundaries, threshold 2:**

| # | session_id | uuid | trigger | pre_tokens | post_tokens |
|---|---|---|---|---|---|
| 1 | `79f58bcf-609f-40b3-9684-30a6d4410098` | `9683726e-c839-454c-a29b-84021ef2229c` | `manual` | 68387 | 5193 |
| 2 | `79f58bcf-609f-40b3-9684-30a6d4410098` | `8181c8d3-fb93-4946-85ae-e0f075507866` | `manual` | 28895 | 4522 |

Reading at the threshold: `health: healthy-complete-from-session-start`,
`observed: 2`, `count: 2`, `rotationMarked: true` — *"2 observed compactions reach the
rotation threshold of 2; this session is marked for graceful rotation at a later safe
boundary."*

**Run B — one boundary, threshold 1:**

| # | session_id | uuid | trigger | pre_tokens | post_tokens |
|---|---|---|---|---|---|
| 1 | `1c5b23c6-0f2f-4552-bcff-e92a166f3bec` | `f72b4615-5ca0-4275-a181-ee4df6c01817` | `manual` | 68257 | 4778 |

`rotationMarked: true`, `health: healthy-complete-from-session-start`.

**On the trigger field.** `decode_lifecycle_event` deliberately drops `trigger` at the
protocol boundary, so the product never sees it. The triggers above are read from the
**provider's own transcript**, whose `compactMetadata.trigger` and `uuid` match the
product's counted pairs event for event. That is corroboration of the product's
identity pairs, not their source.

### `trigger: "auto"` was NOT observed, again

The same bounded, realistic workload as checkpoint 69: real turns in which the managed
agent read four real modules in full and answered genuine comprehension questions about
them. No padding, no synthetic context, no artificial inflation.

Run A reached **68,387** pre-compaction tokens and run B **68,257** — both within 130
tokens of checkpoint 69's 68,260 — and in neither run did the provider raise an
automatic boundary. Every boundary observed across all three dogfoods came from a
controller-issued `/compact` and read `manual`.

**Recorded plainly as an observability limit, not a mechanism dependency.** Nothing in
the counting, threshold, marking or rotation path reads `trigger`. No synthetic context
was manufactured to force one.

## C. Safe boundary and a CURRENT terminal handoff

Finalized by the controller **after `sender` returned**, i.e. after the provider turn
had ended.

**Run A:**
- publication (blob) **`cd1180e247ca26d395ad69a0a43acb07e2b9ff7b`**, 3,208 bytes
- coordination head `7dc5c4de95679eff6cfb8c9ca0add8b6e518b1fb`
- finalization state `terminal-handoff-established`, at **work boundary 8**

**Run B:** publication **`48d072cec0a570bd51661e8e1298eb49025964d0`**, 3,267 bytes.

**Proof it was CURRENT for the boundary it was credited to.** In run A the work
boundary was 7 before the finalizing invocation and 8 after; the recorded finalization
names **8**; and `evaluate_rotation_readiness` said so itself, returning
**`rotation-ready`**:

> session `79f58bcf…` is marked at 2 of 2 observed compactions, **is between
> invocations**, has a coherent workspace, and its rail carries handoff publication
> `cd1180e2…`, **established at the work boundary 8 this session is still standing
> at** and written against product state `a93162953a9f75df5b3da37243b982679c408515`,
> **which is where the workspace still stands**.

**The finalization-retry residual did NOT bite, in either run.** Finalization succeeded
on its first attempt both times, so that residual was again neither exercised, closed,
nor made worse.

## D. Retirement, with the process group PROVEN GONE

| run | predecessor | pgid | graceful | exit | `process_group_gone` | binding |
|---|---|---|---|---|---|---|
| A | `79f58bcf…` | 518878 | yes | 0 | **true** | `unbound` |
| B | `1c5b23c6…` | 520106 | yes | 0 | **true** | `unbound` |

Re-proven independently after each run: `/proc/<pgid>` absent, **zero** processes carry
the pgid, `process_group_alive` returns `False`, and the controller holds no handle
(`handle_held: false`, `is_terminal: true`).

## E. Replacement launched and bound

| run | successor | pid | binding | distinct from predecessor |
|---|---|---|---|---|
| A | **`a1d64a84-9a91-4867-bca8-7cdcd4852d01`** | 519603 | `bound` | yes |
| B | **`b9c47477-e7f7-461d-917f-e309685a2872`** | 520622 | `bound` | yes |

The predecessor's binding was terminal (`unbound`) **before** the successor was
reserved, in both runs, as the route's own detail states: *"was retired — process group
518878 proven gone and its binding unbound — and only then was replacement session
`a1d64a84…` reserved and bound … one slot was released before one was occupied, and
nothing has been sent to it."*

## F. Continuation from durable state — **IT WORKS**

### F.1 The precondition, observed: the conversation genuinely did not exist

Immediately before the continuation, with the successor minted, reserved, started and
`bound`:

| run | successor | `conversation_established` | provider transcripts on disk |
|---|---|---|---|
| A | `a1d64a84…` | **false** | **0** |
| B | `b9c47477…` | **false** | **0** |

This is exactly the state checkpoint 69 failed from. The defect's precondition is
reproduced, not assumed.

### F.2 What the route was about to ask the provider for

Built by calling the same public builders `continue_session` calls, on the same durable
record, with the same `request_kwargs` — a read, sending nothing:

```
create_conversation_request  mode=launch  session_id=a1d64a84-9a91-4867-bca8-7cdcd4852d01  resume=None
resume_request               mode=resume  session_id=None                                  resume=a1d64a84-9a91-4867-bca8-7cdcd4852d01
```

`continue_conversation=False`, `fork_session=False` on both. The binding state is
`bound`, and `create_conversation_request` accepts it — which is the checkpoint-70 seam.

### F.3 The continuation ran, against the real provider

| run | state | reason | subtype | turns | is_error | cost |
|---|---|---|---|---|---|---|
| A | `continuation-continued` | `continuation-continued` | `success` | 11 | false | USD 0.4151 |
| B | `continuation-continued` | `continuation-continued` | `success` | 7 | false | USD 0.1436 |

The route's own detail: *"session `a1d64a84…` continued the work of rail
rotation-dogfood **from durable state alone**: the rail authorization at iteration
`2619caab…` and handoff publication `cd1180e2…` … **No transcript of the predecessor
was read, held, or required.**"*

### F.4 The successor's conversation exists, UNDER ITS OWN ID

The decisive, provider-side evidence — the same artifact whose **absence** proved the
checkpoint-69 failure:

| run | transcript file | bytes | lines | every `sessionId` inside |
|---|---|---|---|---|
| A | `…/a1d64a84-9a91-4867-bca8-7cdcd4852d01.jsonl` | 144,406 | 60 | `a1d64a84-9a91-4867-bca8-7cdcd4852d01` |
| B | `…/b9c47477-e7f7-461d-917f-e309685a2872.jsonl` | 46,417 | 39 | `b9c47477-e7f7-461d-917f-e309685a2872` |

The provider wrote a transcript **named for the successor's own minted id**, containing
**that id and no other**, where before the continuation there was none. Its first
record is the continuation prompt enqueued under `"sessionId":"a1d64a84-…"`.
`registry.conversation_established` flipped `false → true` on the same act.

### F.5 The SECOND invocation RESUMES — it does not create again (run B)

Checkpoint 70's claim has two halves. F.4 is the first. This is the second, and it is
the half that would fail loudly if the conversation fact were recorded wrongly: the
provider refuses a create for an id it already holds.

- second `continue_from_durable_state` on `b9c47477…`: **`continuation-continued`**,
  `subtype: success`, `is_error: false`, 3 turns
- `conversation_established` **stayed `true`**; the reconstruction at that moment shows
  `resume_request` → `session_id=None, resume=b9c47477-…`
- **the same transcript grew**: 46,417 → **62,749** bytes, 39 → **55** lines, still
  carrying that one session id. No second file appeared.
- the brief resolved identically both times (`prompt_equal_to_first: true`)

The strongest evidence is what the agent itself said on the second invocation:

> *"My answer is unchanged … Repeating the same instruction won't change that —
> nothing in the workspace has changed **between the two attempts**."*

It remembered the first attempt. That is one continuous provider conversation, resumed —
not a fresh one.

### F′. Fresh-reader resolution — and checkpoint 69's stated limit, CLOSED

Checkpoint 69 could only reproduce the brief field by field, because by check time the
successor's binding was terminal and `continuation_brief` correctly refuses one.

**Run B ran the fresh reader as a separate OS process while the successor was still
`bound`, and called the real production `continuation_brief`.** A new `BindingStore`,
**no registry**, no memory of the run:

- `binding_state_seen: bound`, `continuation_brief_called: **true**`, `refusal: null`
- **`brief_identical_to_sent: true`**
- **`prompt_byte_identical_to_sent: true`**, 750 characters
- resolved fresh: rail iteration `2619caab79e6c01118dfc4336e5931d823d5b30b`, rail status
  `running`, handoff publication `48d072cec0a570bd51661e8e1298eb49025964d0`, work state
  and workspace head both `a93162953a9f75df5b3da37243b982679c408515`, handoff first line
  `Status: running`, 3,267 bytes

**This is an independent execution of the production function, not a reproduction of its
fields.** The limit checkpoint 69 had to state is closed.

Run A's fresh reader fired **after** the release and was therefore refused —
*"session `a1d64a84…` is unbound; a terminal binding has no work to continue"* — while
still resolving every durable fact fresh and reading back the predecessor's substantive
handoff. That refusal is correct behaviour and is why run B exists.

## G. D6 accounting across the whole swap

| step | run A | run B |
|---|---|---|
| before launch | 0 / 6 | 0 / 6 |
| after launch | 1 / 6 `79f58bcf…` | 1 / 6 `1c5b23c6…` |
| before rotation | 1 / 6 `79f58bcf…` | 1 / 6 `1c5b23c6…` |
| after rotation | 1 / 6 `a1d64a84…` | 1 / 6 `b9c47477…` |
| after continuation | 1 / 6 `a1d64a84…` | 1 / 6 `b9c47477…` |
| after 2nd continuation | — | 1 / 6 `b9c47477…` |
| after release | 0 / 6 | 0 / 6 |

**Never above the ceiling, never transiently at two, never permanently consuming two
slots.** `unprovable` was empty at every step of both runs. The predecessor's slot was
released by the terminalization before the successor's reservation was written, so the
swap passed through N−1, not N+1. Creating the conversation reserved nothing and
started nothing: occupancy is unchanged across it.

## The release: a different, better outcome than checkpoint 69

At checkpoint 69 the failed continuation left the category unprovable, so
`release_continued_context` routed to `supervised_teardown` and published a durable D8
human-attention record. **With the continuation succeeding, both runs released as
ordinary teardown:**

> state `released-ordinary-teardown`, category **`non-rotation-teardown`**,
> `attention: null` — *"session … is provably below the rotation threshold, so its stop
> is ordinary teardown: process group … is gone and the binding is unbound. **This is
> routine work and raises no human-attention item.**"*

That is D8 behaving exactly as written: routine work creates no human-attention item.
Checkpoint 68's `category-unprovable` route is not exercised by a rotation that works,
which is the correct outcome, not a regression.

## Process accounting, termination PROVEN

Four worker process groups were started, all by these runs, all proven gone:

| pid = pgid | run | role | escalation | `/proc` | processes in group | `process_group_alive` |
|---|---|---|---|---|---|---|
| 518878 | A | predecessor | none — graceful, exit 0 | absent | 0 | `False` |
| 519603 | A | successor | none — graceful | absent | 0 | `False` |
| 520106 | B | predecessor | none — graceful, exit 0 | absent | 0 | `False` |
| 520622 | B | successor | none — graceful | absent | 0 | `False` |

**No SIGTERM or SIGKILL escalation was needed for any process.** Verified after the runs
by `process_group_alive` **and** an independent `/proc` walk: no `ai_dev_flow.claude_worker`
process exists on the host, no driver or fresh-reader process survives, and **no process
anywhere has a working directory inside either fixture**. No timeout, no exit 137/143,
and no lost process contact occurred at any point.

**Ownership discipline.** A host process baseline was taken before anything started. No
`unittest discover` process in `/tmp` was seen at any point during either run; had one
appeared it would have been left alone as the Issue #79 lane's.

## What did NOT work

**1. A bug in my own harness, not in the product.** Run A guarded the second-invocation
test on `continuation.state == "continued"`. The actual state is
**`continuation-continued`**, so the guard was false and run A silently skipped the
create-then-resume test entirely. The continuation itself had succeeded. This was my
defect; run B exists to correct it and does.

**2. A timing race in my own harness, not in the product.** Run A's fresh reader was
armed to fire when the continuation was logged, but with the second continuation skipped
the driver went from continuation to release in seconds, so the fresh reader arrived
after the binding was terminal and `continuation_brief` correctly refused it. Run B
invokes the fresh reader from inside the driver, while bound, and closes it.

**3. A real observation about brief reachability — recorded, not remediated.** In run B
the replacement could not **read** the handoff its brief named:
`dogfood/rotation/rails/rotation-dogfood/handoff.md` lives in the control-plane
repository, outside the workspace, and the successor's tools are `Read`/`Grep`/`Glob`
rooted at the workspace. It said so honestly and asked to be unblocked. In run A the
same agent *did* find it, by traversing to the sibling `dg2/cp` tree from the shared
parent directory — an accident of fixture layout, not a property of the mechanism.

**The rotation mechanism is unaffected**: the continuation route reported
`continuation-continued` in both cases, because what it continues is the *invocation*,
and the brief resolved, was sent, and was acted on. But it is worth stating that
**`ContinuationBrief` names a control-plane location the replacement is not guaranteed
to have a route to read.** In production the control plane is a real repository the
agent can reach; in this fixture it was not always. **No remediation was attempted and
none is claimed to be needed** — this is recorded for the reviewer's judgment.

**4. `trigger: "auto"` still unobserved** — see B above. Third dogfood, third time.

## Which parts were FIXTURES and which were REAL

**Fixtures:**
- the control-plane scope `dogfood/rotation` and its rail `rotation-dogfood`
  (`Status: running`, `Role: executor`), in a purpose-made real Git repository
- the workspace: a small real Git repository holding a copied subset of this package's
  own modules
- the rotation threshold, configured to **2** (run A) and **1** (run B) rather than the
  default 6 — D9 makes it human-owned configurable policy; the D6 ceiling stayed at 6
- `terminal_finalizer`'s `bookkeeping` step: the fixture control plane has no remote, so
  bookkeeping verified the coordination checkout was clean and the publication committed
  — the same *shape* as the supported push-and-receipt, and a real check, but not the
  same act. A bookkeeping **failure** was therefore not exercised.

**Real:** the worker process, `claude-agent-sdk 0.2.152`, the provider itself, every
compaction, the finalization and its Git publication, the retirement and every process
termination, the replacement launch and binding, the creation of the successor's
provider conversation, both continuations, the provider's own transcripts on disk, the
fresh reader's execution of `continuation_brief`, and every production route driven.

## Validation

Focused, one command: `tests.test_session_lifecycle` → **Ran 301 tests, OK**. No full
suite, no `unittest discover`, no mutation campaign, no broad re-certification of 57–70.
The two pre-existing failures verified at checkpoint 56 were neither run, repaired, nor
counted. **No product code was changed by this checkpoint.**

## Deliberately not done

No mechanism was changed, redesigned or remediated. Named checkpoint 8 was **not**
completed and no checkpoint was accepted. `main` was not moved. Nothing under `skills/**`
was read into scope, modified or activated. No synthetic context was manufactured to
force an automatic compaction. Issue #55's own live scope was not used as the dogfood
target, for the reason checkpoint 69 recorded and this rail carries forward.

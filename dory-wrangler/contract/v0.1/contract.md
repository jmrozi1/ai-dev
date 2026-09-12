# Dory-wrangler v0.1 chat, session, and agent-binding contract

Status: normative for release v0.1 (`jmrozi1/ai-dev` #81), owned by #85.
Contract version: `0.1`. Record version: `1`.

This document is the model that #86 (chat shell and persistence), #87 (launch
boundary), and #88 (event rendering and raw diagnostics) build against. Where
this document and an implementation disagree, this document is wrong or the
implementation is wrong, but they are not both right: change this document
first.

The executable form of this contract is
[`../../validator/validate_contract.py`](../../validator/validate_contract.py).
Every rule stated here with a violation code in `SMALL_CAPS` is enforced there.
**Not all of them are demonstrated by a fixture.** Of the 44 violation codes the
validator enforces, 24 are exercised by a rejection fixture under
[`../../fixtures/v0.1`](../../fixtures/v0.1) and 20 are not. All 20 have been
probed and fire correctly, so this is a regression-coverage gap rather than a
correctness one; closing it is known carried work, tracked as review finding F8.
An earlier revision of this paragraph claimed every code was fixture-demonstrated
when 13 of 28 were not, which is the same class of error as the rules it
describes. A rule with no code is a design constraint that the record shapes make
structurally impossible to violate.

Enforcement is the point. A rule that this document states and the validator
checks only the shape of is not a rule, and finding such gaps is a first-class
review obligation on every artifact built against this contract.

## 1. Scope and non-goals

v0.1 is one agent per chat. This contract deliberately defines no agent team, no
reviewer, no supervisor, no stall detection, no health polling, and no
time-based state change. Those belong to #82, #83, and #84 and are named here
only as boundaries.

Consequences that later tickets must not quietly undo:

- **No transition happens because time passed.** Every session state change is
  caused by a user action, a harness action, or an explicit launcher
  observation. Inactivity is not evidence. Stall detection is #83.
- **No automatic relaunch or replacement.** If a chat's single agent is
  exhausted or confused, that is the expected v0.1 outcome and useful evidence.
  A replacement agent requires an explicit user action.
- **One chat, one agent, enforced about agents and not only about bookkeeping.**
  A chat may have at most one `agent_session` that is not in a terminal state
  (`CONCURRENT_SESSION`), every such session must be held by exactly one open
  binding (`UNBOUND_ACTIVE_SESSION`), and at most one binding per chat may be
  open (`CONCURRENT_BINDING`). All three are needed: the first two are what make
  the rule bite on agents, and without them a second live agent can speak into a
  chat while the binding ledger looks tidy. #84 relaxes these three together.
- **No record type exists for an agent team, reviewer, or supervisor.** A store
  containing one is rejected (`UNKNOWN_RECORD_TYPE`).

The contract mandates no UI technology and no storage engine. It constrains only
the records, their identities, their relationships, and the invariants below. Any
store that can hold these records and answer the queries in section 8 satisfies
it.

## 2. Durability invariants

**D1. Durable records are canonical.** A chat and its ordered user-visible
history are readable and appendable from durable records alone. No read of chat
history may require a live agent, a live launcher, or any provider-side
conversation. Fixture `02-reopen-after-restart` is the easy proof obligation: it
contains no live session and no open binding and must still validate.

The hard one is a restart with a session still live. That is section 5.4, and
fixtures `10-restart-reattach-failed` and `11-restart-reattach-recovered` are its
proof obligation. D1 is what makes restart re-attachment a v0.1 problem rather
than #82's: a durable record that survives a restart into a state with no legal
exit is not durable, it is a chat nobody can use.

**D2. The agent's transient conversation is never the only copy.** Every
user-visible agent message is derived from a preserved diagnostic event that
remains in the store after the agent is gone (`FABRICATED_AGENT_MESSAGE`).

**D3. Fail closed.** An unknown record version (`UNKNOWN_RECORD_VERSION`), an
unknown record type (`UNKNOWN_RECORD_TYPE`), an unrecognized field
(`UNKNOWN_FIELD`), an unauthorized transition (`UNAUTHORIZED_TRANSITION`), or a
malformed record is a hard rejection. Nothing is coerced, defaulted, or read on
a best-effort basis.

**D4. `unknown` is a state, not an absence.** `unknown` is representable, is
entered only on an explicit integration observation, and is never inferred from
silence or reconstructed from chat text (`UNKNOWN_INFERRED_WITHOUT_EVIDENCE`).

## 3. Identities

Every identifier is an opaque string `<prefix>_<8-32 lowercase alphanumerics>`.
Identifiers carry no meaning: nothing may be parsed out of one, and no ordering,
timestamp, or filename may substitute for one (`BAD_ID_FORMAT`).

| Prefix | Names |
| --- | --- |
| `cht_` | a chat |
| `msg_` | a message |
| `ses_` | an agent session |
| `bnd_` | an agent binding |
| `req_` | a launch request |
| `dlv_` | a delivery request |
| `obs_` | a session observation |
| `evt_` | a diagnostic event |

Timestamps are RFC 3339 UTC with a `Z` suffix and optional microseconds
(`BAD_TIMESTAMP`). Timestamps order events for display and are checked for
regression (`TIME_REGRESSION`), but no rule in this contract fires because of
elapsed time.

Every record carries `record_type` and `record_version`. `record_version` is `1`
for every type in contract version `0.1`.

## 4. Records

### 4.1 `chat`

The durable container for one user-visible conversation.

| Field | Type |
| --- | --- |
| `chat_id` | `cht_` id |
| `title` | string, 1-200 characters |
| `created_at`, `updated_at` | timestamp |
| `state` | `open` \| `archived` |

A chat holds **no** pointer to its agent. The binding is a separate record so
that "which agent is on this chat" has exactly one answer in exactly one place,
and so that a second simultaneous agent is representable-and-rejectable rather
than silently unrepresentable.

### 4.2 `message`

One ordered, user-visible turn. Messages are the **only** records a chat
transcript renders.

| Field | Type |
| --- | --- |
| `message_id` | `msg_` id |
| `chat_id` | `cht_` id |
| `sequence` | integer >= 1 |
| `author` | `user` \| `agent` \| `system` |
| `created_at` | timestamp |
| `content` | `{ "content_type": "text/plain", "text": string }` |
| `session_id` | `ses_` id or `null` |
| `source_event_id` | `evt_` id or `null` |

- `sequence` is unique and contiguous from 1 within a chat (`DUPLICATE_SEQUENCE`,
  `SEQUENCE_GAP`). A gap means a turn was lost; it is never closed silently.
- `author: "agent"` **requires** both `session_id` and `source_event_id`
  (`FABRICATED_AGENT_MESSAGE`). This is the structural guarantee that agent
  history is transcribed evidence rather than narration.
- The cited event must be `interpretation: "recognized"`
  (`MALFORMED_EVENT_RENDERED`), and must belong to the same chat and session
  (`CORRELATION_MISMATCH`).
- `author: "user"` and `author: "system"` must carry `null` in both fields
  (`MESSAGE_PROVENANCE_INVALID`). A user turn is not derived from the
  integration.

`content_type` is fixed at `text/plain` in v0.1. Richer content is an additive
v0.2 concern; a store using another value fails closed today.

### 4.3 `agent_session`

The durable record of one agent engagement with one chat. **One launch attempt
produces exactly one agent session, and the session id is the run identity**
referred to as "run" elsewhere in #81/#85. There is deliberately no separate run
record: two ids for one thing is the kind of drift this ticket exists to prevent.

A session is created *before* the launch is attempted, so launch failures are
attributable to a durable record rather than being lost.

| Field | Type |
| --- | --- |
| `session_id` | `ses_` id |
| `chat_id` | `cht_` id |
| `created_at` | timestamp |
| `launcher_id` | opaque slug, `[a-z0-9][a-z0-9-]{0,63}` |
| `launcher_capabilities` | see below |
| `state` | see 5.1 |
| `agent_handle` | string or absent |
| `transitions` | ordered array, see 5.2 |

`launcher_id` names which launch boundary implementation served this session
(for example `dev-local` or `internal-bridge`). It is recorded so that #89 and
#90 can partition evidence by environment. It is opaque: no behavior may branch
on its value.

`agent_handle` is the opaque handle the launcher returned. A session in state
`running` must carry one (`SESSION_HANDLE_MISSING`). It is meaningful only to
the launcher that issued it.

**`launcher_capabilities` records what the launcher that served this session
declared it can do.** These are declared properties of an implementation. This
contract asserts none of them and assumes none of them, which is the whole point:
the chat and session model must be the same under every combination.

| Field | Type | Meaning |
| --- | --- | --- |
| `continuation` | `persistent` \| `fresh_binding` | whether this launcher can deliver a further instruction to an agent it already started |
| `response_shape` | `stream` \| `one_shot` | whether the launcher exposes an ordered event stream, or returns the agent's response from the call |
| `instruction_bound_bytes` | integer >= 1, or `null` | the largest instruction packet this launcher is *measured* to accept; `null` means nobody has measured one |

The chat records, the user-visible transcript, and reopen behavior are identical
under every combination. Fixtures `12-continuation-persistent` and
`13-continuation-fresh-binding` are the proof obligation: one agent across three
turns and three agents across three turns produce the same transcript, turn for
turn, in the same record shapes.

A launcher declaring `fresh_binding` is not a degraded launcher. It is one of two
first-class modes, and a store may not use a capability its launcher did not
declare (`DELIVERY_NOT_SUPPORTED`, `EVIDENCE_KIND_UNSUPPORTED`).

### 4.4 `agent_binding`

The relation "this chat currently owns this agent".

| Field | Type |
| --- | --- |
| `binding_id` | `bnd_` id |
| `chat_id` | `cht_` id |
| `session_id` | `ses_` id |
| `bound_at` | timestamp |
| `released_at` | timestamp or `null` |

**The one-agent-per-chat rule.** It has three parts, and all three are enforced:

1. At most one binding per chat may have `released_at: null`
   (`CONCURRENT_BINDING`).
2. A chat may have at most one `agent_session` in a non-terminal state
   (`CONCURRENT_SESSION`).
3. Every non-terminal `agent_session` must be held by exactly one open binding
   (`UNBOUND_ACTIVE_SESSION`).

Part 1 alone is a rule about records. A session that is not terminal denotes an
agent that may still be alive, so parts 2 and 3 are what make the rule a rule
about agents: without them two live agents can serve one chat, and an agent no
binding holds can have its output rendered as user-visible chat while the binding
ledger still reads as one-agent-per-chat. Fixtures
`16-two-running-sessions-one-binding` and `17-unbound-running-session` are those
two stores.

This is a concurrency rule, not a lifetime rule. A chat may have many bindings
over its life, one after another, each released before the next is opened
(fixture `07-sequential-rebinding-after-release`).

**Opening the next binding is a user action, and the user sending a turn is that
action.** No harness autonomy, no timer, and no inactivity inference is involved
or permitted. A session's creation transition must cite the `msg_` user message
that caused it (5.2), so a session opened without a user turn behind it is not
representable. One turn opens at most one agent that actually ran
(`TURN_ALREADY_SERVED`); a session that never reached `running` does not consume
its turn, so retrying a failed launch is legal while quietly serving one turn
with two agents is not. This is what makes the `fresh_binding` continuation mode work
without any #83 behavior: under it, each user turn opens the next binding because
the user sent it.

- A binding may be released only when its session is in a terminal state
  (`BINDING_RELEASED_BEFORE_TERMINAL`).
- A binding **must** be released once its session reaches a terminal state
  (`BINDING_OPEN_ON_TERMINAL_SESSION`), so a dead agent cannot hold a chat
  hostage.
- A binding, its session, and its chat must agree (`CORRELATION_MISMATCH`).

### 4.5 `diagnostic_event`

One preserved unit of raw integration evidence.

| Field | Type |
| --- | --- |
| `event_id` | `evt_` id |
| `chat_id` | `cht_` id |
| `session_id` | `ses_` id |
| `sequence` | integer >= 1, contiguous from 1 within the session |
| `received_at` | timestamp |
| `source` | `launcher` \| `agent` \| `harness` |
| `interpretation` | `recognized` \| `unrecognized` \| `malformed` |
| `interpreted_type` | string, or `null` |
| `raw` | `{ "encoding": "utf-8" \| "base64", "body": string }` |

See section 7 for the preservation contract these obey.

### 4.6 `launch_request`, `delivery_request`, and `launch_result`

See section 6.

### 4.7 `session_observation`

One durable record of what an attempted interaction with the launch boundary
actually produced. This is the record type that makes crash recovery expressible
without a timer.

| Field | Type |
| --- | --- |
| `observation_id` | `obs_` id |
| `chat_id` | `cht_` id |
| `session_id` | `ses_` id |
| `observed_at` | timestamp |
| `kind` | see below |
| `detail` | free string or `null`, never parsed |

| Kind | What was attempted, and what came back |
| --- | --- |
| `stop_confirmed` | `stop` was called and the boundary confirmed it |
| `stop_unconfirmed` | `stop` was called and the boundary did not confirm it |
| `reattached` | after a restart, the stored handle was re-attached successfully |
| `reattach_failed` | after a restart, re-attachment through the stored handle did not succeed |
| `stream_read_failed` | reading the event stream failed on *our* side |

Every kind records the outcome of something the harness **did**. None of them is
an inference from elapsed time, from silence, or from an absent record, and
nothing in this contract creates one. That distinction is the entire reason these
are admissible where a timeout is not: a `stop` that returned nothing is an
observation, and #83's absence from v0.1 does not make it one.

`stream_read_failed` is deliberately not an end-of-stream. A failure on the
reader's side says nothing about whether the agent is alive; only the launcher
can signal that its stream ended (6.1).

## 5. Agent lifecycle

### 5.1 States

| State | Meaning | Terminal |
| --- | --- | --- |
| `pending` | the session exists; no launch attempted | no |
| `launching` | a launch was issued; no outcome yet | no |
| `running` | the launcher acknowledged a live agent | no |
| `completed` | the agent finished normally | yes |
| `failed` | the agent ran and ended in failure | yes |
| `launch_failed` | no live agent was ever produced | yes |
| `terminated` | the user stopped the agent | yes |
| `unknown` | the integration reported nothing usable | no |
| `abandoned` | the user gave up on an `unknown` session | yes |

`failed` and `launch_failed` are deliberately distinct: "the agent ran and went
wrong" and "we never got an agent" are different product problems, and #90 needs
to count them separately.

`unknown` is not terminal. A later explicit observation may resolve it in either
direction. It is the only state from which the user may choose `abandoned`,
which exists so a chat stuck behind a silent agent can be freed by a human
without the harness inventing an outcome.

### 5.2 Transitions

`transitions` is the ordered, replayable history of the session. The first entry
has `from: null`; each subsequent entry's `from` equals the previous entry's `to`
(`TRANSITION_CHAIN_BROKEN`); the final `to` equals `state`
(`SESSION_STATE_MISMATCH`).

Each entry is `{ "from", "to", "owner", "at", "evidence": { "kind", "ref" } }`.

**Every transition has exactly one authorized owner, and stated preconditions.**
Any other owner is a violation (`TRANSITION_OWNER_MISMATCH`), and any pair not in
this table is a violation (`UNAUTHORIZED_TRANSITION`).

**The precondition column is executable.** Each row names the evidence kinds that
are admissible for that transition and the record `evidence.ref` must resolve to.
Using an inadmissible kind, or naming a record that is not there or is not in the
state the precondition requires, is `PRECONDITION_NOT_MET`; a reference that is
missing, malformed, or dangling is `EVIDENCE_REF_INVALID`. A precondition stated
only in prose is a precondition the store can skip by omitting the record that
would have been checked, which is how a bounded-packet rule becomes optional.

| From | To | Sole owner | Admissible `evidence.kind` | `evidence.ref` must resolve to |
| --- | --- | --- | --- | --- |
| *(none)* | `pending` | `user` | `user_action` | the `msg_` user message on this chat that opened the session |
| `pending` | `launching` | `harness` | `harness_action` | the session's `req_` launch packet (6.2) |
| `pending` | `launch_failed` | `harness` | `harness_action` | nothing; `ref` is `null`, because nothing was launched |
| `launching` | `running` | `launcher` | `launch_result` | a `req_` whose result is `accepted` **with a handle** |
| `launching` | `launch_failed` | `launcher` | `launch_result` | a `req_` whose result is `failed` **with a category** |
| `launching` | `unknown` | `launcher` | `launch_result`, `observation` | a `req_` whose result is `unknown`, or an `obs_` that left liveness undeterminable |
| `running` | `completed` | `launcher` | `event` | a `recognized` `evt_` on this session |
| `running` | `failed` | `launcher` | `event` | a `recognized` `evt_` on this session |
| `running` | `terminated` | `user` | `observation` | an `obs_` of kind `stop_confirmed` on this session |
| `running` | `unknown` | `launcher` | `event`, `stream_end`, `observation` | see 5.3 |
| `unknown` | `running` | `launcher` | `event`, `observation` | a `recognized` `evt_`, or an `obs_` of kind `reattached` |
| `unknown` | `completed` | `launcher` | `event` | a `recognized` `evt_` on this session |
| `unknown` | `failed` | `launcher` | `event` | a `recognized` `evt_` on this session |
| `unknown` | `terminated` | `user` | `observation` | an `obs_` of kind `stop_confirmed` on this session |
| `unknown` | `abandoned` | `user` | `user_action` | nothing; `ref` is `null`. This is a decision, not an observation |

Every referenced record must belong to this session, or to this session's chat in
the case of the opening user message (`CORRELATION_MISMATCH`). A transition
authorized by an observation of the integration must cite an event the
integration produced: a cited `evt_` must have `source` `agent` or `launcher`,
never `harness` (`PRECONDITION_NOT_MET`). What the harness itself observed is a
`session_observation` (4.7), and routing it through `evidence.kind: "event"`
would let the harness authorize its own conclusions under the launcher's name.

No transition leaves a terminal state. Attempting one is
`UNAUTHORIZED_TRANSITION`.

Owner is *authority*, not authorship: the harness is always the only writer of
durable records. `owner: "launcher"` means the launch boundary's report is what
authorizes the change, and the harness may not make it without one.
`owner: "user"` means no component may make it without an explicit human action;
this is why `running -> terminated` is owned by `user` and why v0.1 grants the
harness no authority to stop an agent on its own judgement.

### 5.3 Evidence, and what `unknown` requires

`evidence.kind` is one of `event`, `stream_end`, `launch_result`, `observation`,
`user_action`, `harness_action`.

**`evidence.ref` must resolve.** It names the record the precondition in 5.2
requires, it must be a well-formed identifier of the right prefix, and it must
resolve to a record that is actually in the store and on this session
(`EVIDENCE_REF_INVALID`, `CORRELATION_MISMATCH`, `PRECONDITION_NOT_MET`). An
unresolvable reference is not a weaker citation; it is no citation at all.

The first four kinds are **observations of the integration**. A transition to
`unknown` requires one of them, and requires it to resolve
(`UNKNOWN_INFERRED_WITHOUT_EVIDENCE`). Without resolution the rule reads as "a
transition to `unknown` requires the *claim* of an observation", which is
precisely the fabrication D4 exists to prevent. The concrete admissible causes
are:

1. the launcher explicitly returned outcome `unknown` (`launch_result`);
2. the launcher signalled that its event stream ended without a terminal
   lifecycle event (`stream_end`) — the stream *closing* is an observed fact,
   unlike silence on an open stream. The signal must be preserved as a
   launcher-sourced `recognized` event of `interpreted_type: "stream_end"`; a
   failure to read the stream on our side is not one (4.7, 6.1);
3. an event was received that contradicts or cannot be reconciled with the
   current state (`event`);
4. a `stop` was attempted and the launch boundary did not confirm it
   (`observation`, kind `stop_unconfirmed`);
5. re-attachment to a session's agent after a restart was attempted and did not
   succeed (`observation`, kind `reattach_failed`); or
6. reading the event stream failed on our side, leaving what the agent is doing
   undeterminable (`observation`, kind `stream_read_failed`).

Causes 4 to 6 are what keep a chat from bricking, and none of them is a timer.
Each records something the harness attempted and what came back. **A timeout, an
inactivity inference, or any conclusion drawn from elapsed time is not on this
list**, adding one is #83's decision rather than #86-#88's, and nothing in this
contract fires because time passed.

### 5.4 Restart, and how a chat stops being usable

D1 makes durable records canonical across a restart, so what happens to a live
session at restart is this contract's obligation and not #82's. Declining to
define it is what produces a chat nobody can use.

A `running` session with an open binding is a legal durable state. After a
restart the harness must, for each non-terminal session, attempt to re-attach to
the agent through the stored `agent_handle` and record what came back as a
`session_observation` (4.7). Both outcomes are representable:

- `reattached` — the session continues as it was. If it was driven to `unknown`
  first, `unknown -> running` returns it (fixture
  `11-restart-reattach-recovered`).
- `reattach_failed` — the session goes to `unknown` (fixture
  `10-restart-reattach-failed`). The binding stays open, because `unknown` is not
  terminal and the agent may still be out there.

From `unknown` the user always has an exit: `abandoned`. That is terminal, so the
binding is released, and the chat can bind a new agent. **The user is never
stuck**, and the harness never resolves this on its own. The same applies when
`stop` itself fails: an unconfirmed stop is cause 4 above, not a dead end.

A session interrupted in `launching` is the second case of this shape. If the
launch outcome cannot be recovered, an `obs_` of kind `reattach_failed` carries
it to `unknown`, and the user may abandon it. **A retry is a new session with its
own launch packet, never a second packet on the old one** — one launch attempt,
one session, one run identity (4.3, 6.2), and a double-launch stays visible as
two sessions rather than hiding inside one.

The harness does none of this on a schedule. Re-attachment happens when the
harness starts; everything after it is a user action.

## 6. The launch boundary

The launch boundary is the only place that knows how an agent is started. It is
specified here as an interface. #87 supplies implementations; this ticket
supplies none.

**Portability rule.** No operation, input, or output may contain host,
transport, or bridge mechanics: no command line, argv, working directory,
environment, shell, path, hostname, port, URL, socket, pid, terminal id,
workspace, container, or extension identifier. The external Linux development
launcher and the internal VS Code/network bridge launcher must both sit behind
this interface unchanged.

**What actually guarantees this is that the instruction packet is a closed
schema.** Any field not in the packet's table is rejected (`UNKNOWN_FIELD`),
whatever it is called, so a name nobody thought to forbid fails exactly as
firmly as one that was. The denylist of known mechanics names
(`BRIDGE_SPECIFIC_FIELD`) improves the diagnosis for the likeliest mistakes and
guarantees nothing on its own; it is a better error message, not the rule. Do not
extend the denylist believing it is what holds the seam closed, and do not open
the schema believing the denylist will catch what comes through.

Mechanics hidden inside the *value* of an allowed field — a handle that happens
to encode a host, a `detail` string naming a script — are accepted by design.
`agent_handle` is opaque and `detail` is never parsed, so neither can influence
behavior on this side of the seam.

### 6.1 Operations

| Operation | Input | Output | Always available |
| --- | --- | --- | --- |
| `launch` | `launch_request` (6.2) | `launch_result` (6.3) | yes |
| `stop` | `session_id`, reason string | acknowledgement, or a failure category | yes |
| `events` | `session_id`, `after_sequence` | see *Reading what the agent produced* below | yes |
| `deliver` | `session_id`, delivery packet (6.4) | `launch_result`-shaped acknowledgement | **only if declared** |

There is **no** `status`, `health`, `poll`, or `describe` operation. v0.1 learns
what the agent is doing only from what the boundary reports. Adding an
interrogation operation is #82's decision.

#### `deliver` is a declared capability, not an assumption

`deliver` sends further text to an agent that is already `running`. **A launcher
is not assumed to have it.** A launcher declares `continuation: "persistent"` if
delivering to a running agent works, and `continuation: "fresh_binding"` if a
later turn requires a new launch. Both are first-class; neither is a fallback.

- Under `persistent`, a chat may be served by one agent across many turns, and
  **every delivery produces a durable record** (6.4). Without that, everything
  after the first turn is integration-blind: the bounded packet, the portability
  rule, and diagnostic preservation would all apply to turn one and to nothing
  after it.
- Under `fresh_binding`, each later turn is a new session with its own launch
  packet, bound and released in order. A `delivery_request` on such a session is
  rejected (`DELIVERY_NOT_SUPPORTED`). Because the user sending a turn is itself
  the user action that opens the next binding (4.4), this needs no harness
  autonomy, no timer, and no #83 behavior.

**What the fresh-binding instruction packet contains is deliberately undecided
here.** It is specifically *not* "the whole prior chat", because that would make
an unmeasured payload limit part of the architecture. Packet composition is an
integration question for early internal dogfood, and #86 and #87 must not settle
it by default.

#### Reading what the agent produced

`events(session_id, after_sequence)` returns raw payloads from the session,
ordered, each carrying the `sequence` under which it is preserved (4.5).
`after_sequence: 0` means from the beginning.

- **It is resumable and may be called any number of times.** A caller that was
  interrupted resumes by passing the last `sequence` it stored. Delivery is
  at-least-once with respect to `sequence`; the store writes each `(session_id,
  sequence)` once, so a replayed payload is a no-op rather than a
  `DUPLICATE_SEQUENCE` violation, and the contiguity rule survives resumption.
- **Nothing emitted after the launch was accepted is lost.** A launcher must be
  able to replay from `sequence` 1 for the life of the session, so events
  produced before the first `events` call are still retrievable.
- **After end-of-stream it keeps working**, returning the end-of-stream signal
  and no new payloads.
- **A launcher declaring `response_shape: "stream"` must signal end-of-stream
  distinguishably from a quiet stream**, because 5.3 cause 2 depends on that
  distinction. The signal is preserved as a launcher-sourced `recognized` event
  of `interpreted_type: "stream_end"`.
- **A reader-side failure is not an end-of-stream.** If the call itself fails,
  that is `stream_read_failed` (4.7) and it says nothing about the agent. Two
  implementations that disagree on this drive healthy sessions to `unknown` on a
  transport hiccup.

**A launcher declaring `response_shape: "one_shot"` has no stream.** It returns
the agent's response from the call, which is the only internal path proven to
work today. Its response is preserved as one or more `diagnostic_event` records
exactly as a stream's payloads are; if it cannot be segmented at all, the honest
record is a single event, and `unrecognized` or `malformed` is a finding rather
than an error (7). Such a launcher cannot observe a stream ending, so
`stream_end` evidence on its sessions is rejected
(`EVIDENCE_KIND_UNSUPPORTED`) — concluding `unknown` from an end-of-stream that
could not have been seen is the one-shot form of inferring from silence. It
reaches `unknown` through causes 1, 3, 4, 5 and 6 instead.

This contract does not require event granularity that may not exist. It requires
that whatever arrives is preserved, ordered, and attributable.

### 6.2 The bounded instruction packet (`launch_request`)

| Field | Type |
| --- | --- |
| `request_id` | `req_` id |
| `chat_id` | `cht_` id |
| `session_id` | `ses_` id |
| `created_at` | timestamp |
| `instruction_encoding` | `"utf-8"` |
| `instruction_text` | UTF-8 string, at least 1 byte |

**The bound is a measured property of a launcher, and this contract asserts
none.** `instruction_text` is checked against
`launcher_capabilities.instruction_bound_bytes` for the session
(`INSTRUCTION_TEXT_TOO_LARGE`). When that is `null` — its only honest value today
— no size check fires, because no bound has been measured. An earlier revision of
this document asserted 65536 bytes and enforced it. That number was a guess, and
enforcing a guess as though it were a bound is worse than enforcing nothing: it
looks like knowledge. The mechanism stays, live and tested (fixtures
`06-instruction-text-over-limit` and `14-unmeasured-instruction-bound`); the
value is #90's to supply, as known-unproven claim U2.

Note that this only means anything because the packet must exist at all. While
the precondition column was prose, a store could send an instruction of any size
and simply omit the record that would have been checked.

The packet is text plus correlation, and nothing else. No unlisted field is
permitted (`UNKNOWN_FIELD`, or `BRIDGE_SPECIFIC_FIELD` for the named mechanics).
A launcher that needs configuration obtains it from its own environment, never
from the packet.

At most one `launch_request` exists per session (`DUPLICATE_LAUNCH_REQUEST`):
one launch attempt, one session, one run identity. A retry after an ambiguous
launch is a new session (5.4), not a second packet on the old one.

### 6.3 `launch_result` and failure categories

| Field | Type |
| --- | --- |
| `request_id` | `req_` id |
| `session_id` | `ses_` id |
| `observed_at` | timestamp |
| `outcome` | `accepted` \| `failed` \| `unknown` |
| `agent_handle` | string; required iff `accepted`, forbidden otherwise |
| `failure_category` | required iff `failed`, forbidden otherwise |
| `detail` | free string or `null`, never parsed |

Violations of the conditional fields are `LAUNCH_RESULT_INCONSISTENT`; a result
that disagrees with its session's state is `LAUNCH_OUTCOME_MISMATCH`.

Failure categories are closed and are the distinctions #90 must be able to count:

| Category | Meaning |
| --- | --- |
| `invalid_request` | the packet was rejected; nothing was launched |
| `unavailable` | a launch prerequisite was not satisfied |
| `rejected` | the launch path was reachable and refused the request |
| `no_acknowledgement` | the call returned without a usable acknowledgement |
| `internal_error` | the launcher failed in an unclassified way |

`unavailable` is the abstract form of the internal operating prerequisites (an
active user session, an initialized bridge). Those specifics stay inside the
internal launcher; only the category crosses the seam. `detail` may carry the
human-readable specifics and is never parsed for control flow.

### 6.4 The delivery packet (`delivery_request`)

Every `deliver` call produces one of these. A turn the harness sent to an agent
and did not record is a turn nobody can investigate.

| Field | Type |
| --- | --- |
| `delivery_id` | `dlv_` id |
| `chat_id` | `cht_` id |
| `session_id` | `ses_` id |
| `sequence` | integer >= 1, contiguous from 1 within the session |
| `created_at` | timestamp |
| `instruction_encoding` | `"utf-8"` |
| `instruction_text` | UTF-8 string, at least 1 byte |
| `acknowledged` | `true` \| `false` \| `null` when the acknowledgement is unknown |

It is bounded, closed, and portability-checked **identically to the launch
packet**, against the same declared `instruction_bound_bytes`. The seam's
guarantees are not allowed to be weaker on turn four than on turn one.

A `delivery_request` is permitted only on a session whose launcher declared
`continuation: "persistent"` and which actually reached `running`
(`DELIVERY_NOT_SUPPORTED`).

**Every answered user turn has a durable instruction record.** For each chat, the
number of instruction packets — `launch_request` plus `delivery_request` — must
be at least the number of user messages the agent went on to answer
(`TURN_INSTRUCTION_MISSING`). This holds under both continuation modes and is how
they are made equivalent: `persistent` records one launch and N-1 deliveries,
`fresh_binding` records N launches, and either way what was sent is preserved for
every turn.

## 7. Diagnostic preservation

**P1. Everything received is preserved verbatim.** Every payload the integration
produces becomes a `diagnostic_event` whose `raw.body` holds it exactly as
received (`RAW_EVIDENCE_MISSING`). Preservation is unconditional and is
*especially* required when structured parsing fails, because those are the cases
v0.1 exists to discover. `raw.encoding` is `base64` when the payload is not
valid UTF-8.

**P2. Interpretation is recorded, never assumed.**

- `recognized` — parsed into a known type; `interpreted_type` names it.
- `unrecognized` — well-formed but of a type this build does not know;
  `interpreted_type` is `null`.
- `unrecognized` and `malformed` must have `interpreted_type: null`; `recognized`
  must name one (`EVENT_INTERPRETATION_INCONSISTENT`).
- `malformed` — could not be parsed at all; `interpreted_type` is `null`.

An unrecognized event is a **finding**, not an error. Its accumulation is the
primary discovery output of v0.1 and the direct input to #89 and #90.

**P2a. An agent's output presupposes an agent.** A `diagnostic_event` with
`source: "agent"` requires its session to have reached `running`
(`AGENT_OUTPUT_WITHOUT_AGENT`). The launcher may report before anything was
started — that is what a launch failure is — but the agent may not. Without this,
a session that never launched can still emit recognized events and have them
rendered as user-visible chat, which routes around the lifecycle entirely.

**P3. Correlation.** Every event carries `chat_id`, `session_id`, and a
`sequence` that is contiguous from 1 within its session. Those three answer
"what did this chat's agent emit, in what order". Cross-correlation is a
violation (`CORRELATION_MISMATCH`).

**P4. Diagnostics stay out of the chat.** The transcript renders `message`
records only. `diagnostic_event` records are never rendered in a transcript, are
stored separately from chat history, and are reachable only through a bounded
out-of-band retrieval addressed by `chat_id`, optionally narrowed by
`session_id` and a `sequence` range, returning a bounded number of events.
A `malformed` or `unrecognized` event can never reach the user as chat content
(`MALFORMED_EVENT_RENDERED`).

The out-of-band retrieval is the only diagnostic surface v0.1 has. It is a
retrieval, not a dashboard, and it derives nothing: it returns preserved records.
A view that interprets, aggregates, or summarizes them is #82.

## 8. What a store must support

Any storage engine satisfies this contract if it can:

1. read a chat and its messages in `sequence` order without any live process;
2. append a message atomically with a `sequence` that is unique within the chat;
3. answer "which binding on this chat has `released_at: null`, and which of this
   chat's sessions are non-terminal", and open a new binding only if both
   answers are empty;
4. append a `diagnostic_event` with a `sequence` unique within its session,
   separately from chat history, and treat a replayed `(session_id, sequence)`
   as already stored rather than as a new record (6.1);
5. retrieve diagnostic events by `chat_id`, optionally by `session_id` and
   `sequence` range, with a bounded result count;
6. read and append session transitions atomically with respect to the session's
   `state`;
7. append a `launch_request`, `delivery_request`, or `session_observation` and
   read it back by `session_id`, since transition preconditions resolve against
   these records (5.2).

Requirements 3 and 6 are the only ones needing concurrency control, and both are
per-chat. No global lock and no persistent coordinator is required.

## 9. Validating a store

```
python3 dory-wrangler/validator/validate_contract.py dory-wrangler/fixtures/v0.1
```

Exit `0` when every fixture matched its declared expectation, `1` when any did
not, `2` on a usage or input error. A rejection fixture passes only when the
violation code it names is actually among the codes emitted, so a fixture cannot
pass by being wrong in some unrelated way.

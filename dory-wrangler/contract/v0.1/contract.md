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
Every rule stated here with a violation code in `SMALL_CAPS` is enforced there
and demonstrated by a fixture under [`../../fixtures/v0.1`](../../fixtures/v0.1).
A rule with no code is a design constraint that the record shapes make
structurally impossible to violate.

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
- **No record type exists for a second simultaneous agent.** A store containing
  one is rejected (`UNKNOWN_RECORD_TYPE`, `CONCURRENT_BINDING`).

The contract mandates no UI technology and no storage engine. It constrains only
the records, their identities, their relationships, and the invariants below. Any
store that can hold these records and answer the queries in section 8 satisfies
it.

## 2. Durability invariants

**D1. Durable records are canonical.** A chat and its ordered user-visible
history are readable and appendable from durable records alone. No read of chat
history may require a live agent, a live launcher, or any provider-side
conversation. Fixture `02-reopen-after-restart` is the proof obligation: it
contains no live session and no open binding and must still validate.

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

### 4.4 `agent_binding`

The relation "this chat currently owns this agent".

| Field | Type |
| --- | --- |
| `binding_id` | `bnd_` id |
| `chat_id` | `cht_` id |
| `session_id` | `ses_` id |
| `bound_at` | timestamp |
| `released_at` | timestamp or `null` |

**The one-agent-per-chat rule.** At most one binding per chat may have
`released_at: null` (`CONCURRENT_BINDING`).

This is a concurrency rule, not a lifetime rule. A chat may have many bindings
over its life, one after another, each released before the next is opened
(fixture `07-sequential-rebinding-after-release`). Opening the next one is a
user action; the harness never does it on its own.

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

### 4.6 `launch_request` and `launch_result`

See section 6.

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

**Every transition has exactly one authorized owner.** Any other owner is a
violation (`TRANSITION_OWNER_MISMATCH`), and any pair not in this table is a
violation (`UNAUTHORIZED_TRANSITION`).

| From | To | Sole owner | Precondition |
| --- | --- | --- | --- |
| *(none)* | `pending` | `user` | the chat exists and has no open binding |
| `pending` | `launching` | `harness` | a valid bounded launch packet exists (6.2) |
| `pending` | `launch_failed` | `harness` | the packet failed validation; nothing was launched |
| `launching` | `running` | `launcher` | the launcher returned `accepted` with a handle |
| `launching` | `launch_failed` | `launcher` | the launcher returned `failed` with a category |
| `launching` | `unknown` | `launcher` | the launcher returned `unknown` |
| `running` | `completed` | `launcher` | a recognized terminal completion event was received |
| `running` | `failed` | `launcher` | a recognized terminal failure event was received |
| `running` | `terminated` | `user` | the user requested a stop and it was confirmed |
| `running` | `unknown` | `launcher` | an explicit observation left liveness undeterminable |
| `unknown` | `running` | `launcher` | a later explicit observation established liveness |
| `unknown` | `completed` | `launcher` | a later recognized terminal completion event |
| `unknown` | `failed` | `launcher` | a later recognized terminal failure event |
| `unknown` | `terminated` | `user` | the user requested a stop and it was confirmed |
| `unknown` | `abandoned` | `user` | the user explicitly abandoned the session |

No transition leaves a terminal state. Attempting one is
`UNAUTHORIZED_TRANSITION`.

Owner is *authority*, not authorship: the harness is always the only writer of
durable records. `owner: "launcher"` means the launch boundary's report is what
authorizes the change, and the harness may not make it without one.
`owner: "user"` means no component may make it without an explicit human action;
this is why `running -> terminated` is owned by `user` and why v0.1 grants the
harness no authority to stop an agent on its own judgement.

### 5.3 Evidence, and what `unknown` requires

`evidence.kind` is one of `event`, `stream_end`, `launch_result`, `user_action`,
`harness_action`. `evidence.ref` names the `evt_` or `req_` record when one
applies.

The first three are **observations of the integration**. A transition to
`unknown` requires one of them (`UNKNOWN_INFERRED_WITHOUT_EVIDENCE`). The
concrete admissible causes are:

1. the launcher explicitly returned outcome `unknown` (`launch_result`);
2. the event stream terminated without a terminal lifecycle event
   (`stream_end`) — the stream *closing* is an observed fact, unlike silence on
   an open stream;
3. an event was received that contradicts or cannot be reconciled with the
   current state (`event`).

A timeout is not on this list, and adding one is #83's decision, not #86-#88's.

## 6. The launch boundary

The launch boundary is the only place that knows how an agent is started. It is
specified here as an interface. #87 supplies implementations; this ticket
supplies none.

**Portability rule.** No operation, input, or output may contain host,
transport, or bridge mechanics: no command line, argv, working directory,
environment, shell, path, hostname, port, URL, socket, pid, terminal id,
workspace, container, or extension identifier (`BRIDGE_SPECIFIC_FIELD`). The
external Linux development launcher and the internal VS Code/network bridge
launcher must both sit behind this interface unchanged.

### 6.1 Operations

| Operation | Input | Output |
| --- | --- | --- |
| `launch` | `launch_request` | `launch_result` |
| `deliver` | `session_id`, instruction packet (6.2) | `launch_result`-shaped acknowledgement |
| `stop` | `session_id`, reason string | acknowledgement, or a failure category |
| `events` | `session_id` | an ordered stream of raw event payloads, and an end-of-stream signal |

`deliver` sends further text to an already-`running` agent; it is how a chat
continues past its first turn.

There is **no** `status`, `health`, `poll`, or `describe` operation. v0.1 learns
what the agent is doing only from `events`. Adding an interrogation operation is
#82's decision.

`events` must signal end-of-stream distinguishably from a quiet stream, because
section 5.3 depends on that distinction.

### 6.2 The bounded instruction packet (`launch_request`)

| Field | Type |
| --- | --- |
| `request_id` | `req_` id |
| `chat_id` | `cht_` id |
| `session_id` | `ses_` id |
| `created_at` | timestamp |
| `instruction_encoding` | `"utf-8"` |
| `instruction_text` | string, 1 to 65536 UTF-8 bytes |

The packet is **bounded**: `instruction_text` is at most 65536 UTF-8 bytes
(`INSTRUCTION_TEXT_TOO_LARGE`). This limit is a provisional assumption, not a
measured one — see the fact/assumption record, claim A4.

The packet is text plus correlation, and nothing else. No unlisted field is
permitted (`UNKNOWN_FIELD`, or `BRIDGE_SPECIFIC_FIELD` for the named mechanics).
A launcher that needs configuration obtains it from its own environment, never
from the packet.

At most one `launch_request` exists per session (`DUPLICATE_LAUNCH_REQUEST`):
one launch attempt, one session, one run identity.

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
3. answer "which binding on this chat has `released_at: null`" and open a new
   binding only if that answer is empty;
4. append a `diagnostic_event` with a `sequence` unique within its session,
   separately from chat history;
5. retrieve diagnostic events by `chat_id`, optionally by `session_id` and
   `sequence` range, with a bounded result count;
6. read and append session transitions atomically with respect to the session's
   `state`.

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

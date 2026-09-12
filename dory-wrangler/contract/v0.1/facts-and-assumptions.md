# What is known, and what is assumed

Companion to [`contract.md`](contract.md). Written by #85 so that #89 (Linux
validation) and #90 (internal Rocky 9 dogfood) inherit a register of claims to
settle rather than a contract that quietly pretends to know things.

The contract is designed so that every assumption below can be wrong without the
chat, message, session, or binding model changing. If settling one of these
forces a change to sections 2-5 of the contract, that is a finding worth
escalating, not a routine edit.

## Facts

Verified in this worktree at commit `38282bdb09c70523d10215f7a7fcc9f8a73f99b1`:

- **F1.** No Dory-wrangler source existed in `ai-dev` before this ticket. The
  `dory-wrangler/` tree is new.
- **F2.** No internal bridge, launcher, or agent-integration implementation is
  present in this repository. A repository-wide search for VS Code or bridge
  invocation machinery returns only prose. Nothing about the internal launch
  path can be verified from this checkout.
- **F3.** This development VM runs Python 3.9.25 and has no `pytest`. The
  validator is therefore stdlib-only and self-driving. #89 should confirm the
  internal interpreter version before assuming a test framework is available.

Established by the control plane and #81/#85 rather than by inspection here.
They are treated as settled, and contradicting evidence is an escalation:

- **F4.** Development happens on an external Linux VM; the target is internal
  Rocky Linux 9. The internal environment is the compatibility authority and
  changes reach it through one nightly mirror.
- **F5.** Agent launch differs between the two environments. The internal path
  ultimately goes through an existing VS Code/network bridge and a script that
  can launch an agent when given instruction text. That mechanism is not
  reachable from this VM and could not be exercised.
- **F6.** v0.1 is one agent per chat, and an exhausted or confused single agent
  is an expected outcome rather than a defect to engineer around.

Prior observations from the superseded earlier planning era
(`relay/dory-wrangler/`). They are useful and were designed for, but they carry
no authority and #90 should re-confirm them internally:

- **F7.** Raw agent output must be preserved even when structured parsing fails.
  This is the origin of contract rule P1.
- **F8.** An active user session and an initialized VS Code bridge are explicit
  internal operating prerequisites. These are the concrete cases behind the
  abstract `unavailable` launch failure category.
- **F9.** Token counting is not required. Nothing in the contract counts tokens.

## Assumptions

Each is a claim internal dogfood can settle by observation. "Contract exposure"
names what would have to change if the claim is false.

- **A1. The launch call reports an outcome distinguishable from the agent's own
  output.** *Claim to test:* a single internal launch invocation yields an
  acknowledgement that an agent was started, separately from anything the agent
  later emits. *Exposure:* if launch success can only be inferred from the first
  agent output, `launching -> running` has no owner-authorized evidence and the
  `launch_failed` state becomes unreachable, collapsing into `unknown`.

- **A2. There is a distinguishable end-of-stream for one agent's output.**
  *Claim to test:* the internal path signals that no further output will arrive
  for a given agent, distinguishably from a stream that is merely quiet.
  *Exposure:* contract rule 5.3 case 2 is the only non-launcher, non-event route
  into `unknown`. Without it, a silent internal agent is indistinguishable from a
  running one, and v0.1 has no honest way to leave `running` — which is
  precisely the gap #83 exists to close, and must not be closed here with a
  timer.

- **A3. A started agent can later be stopped through an opaque handle.**
  *Claim to test:* the handle the internal launch returns is sufficient to stop
  that agent, without the caller supplying host or process detail.
  *Exposure:* the `stop` operation and the `running -> terminated` transition.

- **A4. A 65536-byte UTF-8 instruction text is accepted in one launch.**
  *Claim to test:* the internal script accepts an instruction of that size
  without truncation, and report the size at which it actually truncates or
  fails. *Exposure:* one constant in contract section 6.2. This number is a
  guess and is expected to move.

- **A5. Instruction text plus correlation is the whole launch input.**
  *Claim to test:* no per-launch host-specific parameter beyond instruction text
  is required internally. *Exposure:* the portability rule in section 6, which
  is the entire point of the seam. If a per-launch host parameter is required,
  it must be absorbed into the internal launcher's own configuration rather than
  added to the packet.

- **A6. Additional text can be delivered to an already-running agent.**
  *Claim to test:* the internal bridge can send a second and subsequent
  instruction to an agent it already started, without a new launch. *Exposure:*
  this is the largest open question and is discussed separately below.

- **A7. Concurrent agents' events are attributable to their own session.**
  *Claim to test:* with two chats active internally, each event can be
  attributed to the agent that produced it. *Exposure:* correlation rule P3 and
  every `CORRELATION_MISMATCH` guarantee. If attribution is not possible
  internally, v0.1 is effectively single-concurrent-chat and #90 should say so.

- **A8. Output can be segmented into discrete events at all.**
  *Claim to test:* internal agent output arrives in units that can be recorded
  as individually preserved events, rather than as one undelimited byte stream.
  *Exposure:* `diagnostic_event.sequence` and the whole preservation model. If
  output is undelimited, the honest v0.1 record is one event per stream chunk
  with `interpretation: "malformed"`, which the contract already permits and
  which would itself be a valuable finding.

- **A9. Agent output worth showing a user is adequately represented as plain
  text.** *Claim to test:* record which event types actually occur internally
  and whether any carries user-relevant content that `text/plain` loses.
  *Exposure:* `content.content_type`, deliberately fixed at `text/plain` so that
  anything else fails closed and gets counted rather than silently downgraded.

## The one question that would most change this design

**Can the internal bridge deliver further instruction text to an agent it has
already started, or does every user turn require a new launch?** (A6)

It could not be determined here: F2 means there is nothing to inspect, and F5
means there is nothing to run.

*Provisional assumption taken:* yes. The contract defines a `deliver` operation
(section 6.1), and the fixtures assume one agent spans many turns.

*If the answer is no*, the contract degrades without a redesign, because it
already separates the durable chat from the session that serves it: a chat would
accumulate one session per turn, each bound and released in order, exactly as
fixture `07-sequential-rebinding-after-release` shows. Two consequences would
then need an explicit decision rather than an implementation guess:

1. the rule that a new binding requires an explicit user action would have to
   allow the harness to open the next binding automatically on a user turn; and
2. each launch would have to carry enough prior chat history in its instruction
   text to be useful, which makes the A4 size limit load-bearing rather than
   incidental.

Both are product decisions. Neither is taken here.

## Pressure points recorded and not acted on

Places where this contract felt the pull of a later release and deliberately
stopped. Each is a live design tension, not an oversight:

- **Toward #83.** Leaving `running` honestly requires either an explicit
  observation or a timer. The contract admits only observations (rule 5.3) and
  states that no transition occurs because time passed. If A2 fails internally,
  a silent agent is unresolvable in v0.1 by design, and that pressure belongs to
  #83.
- **Toward #82.** The launch boundary has no `status` or `health` operation, so
  "is it alive" is answerable only from events. The bounded out-of-band
  diagnostic retrieval (rule P4) is a retrieval and derives nothing; the moment
  it aggregates, interprets, or ranks, it has become #82.
- **Toward #84.** `agent_binding` exists as a separate record partly so a second
  concurrent agent is representable and therefore rejectable. That is the shape
  #84 will relax, and it should relax the binding cardinality rule rather than
  the chat, message, or session records.

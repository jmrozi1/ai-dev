# What is known, and what is assumed

Companion to [`contract.md`](contract.md). Written by #85 so that #89 (Linux
validation) and #90 (internal Rocky 9 dogfood) inherit a register of claims to
settle rather than a contract that quietly pretends to know things.

The contract is designed so that every assumption below can be wrong without the
chat, message, session, or binding model changing. If settling one of these
forces a change to sections 2-5 of the contract, that is a finding worth
escalating, not a routine edit.

This register distinguishes three things, and the distinction is load-bearing:

- a **fact** — verified, or established by #81/#85 and treated as settled;
- a **known-unproven** claim — something we specifically know we have not
  established, where the contract must work either way and asserting an answer
  would be a fabrication;
- an **assumption** — a claim the contract proceeds on and internal dogfood can
  settle.

The known-unproven group exists because the previous revision of this contract
had none, and put an unmeasured guess where a measurement belonged.

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

- **F10.** `~/scripts/launch_agent.sh` accepts instruction text, uses the
  existing VS Code/network bridge, launches an agent, and **returns that agent's
  response**. It has been used successfully as a one-shot launch/request path.
  This is the one internal launch path that is proven, and it is human-supplied
  rather than verified here. It is the `fresh_binding` + `one_shot` combination
  in contract 4.3, and fixture `13-continuation-fresh-binding` is the store shape
  it produces.
Prior observations from the superseded earlier planning era
(`relay/dory-wrangler/`). They are useful and were designed for, but they carry
no authority and #90 should re-confirm them internally:

- **F7.** Raw agent output must be preserved even when structured parsing fails.
  This is the origin of contract rule P1.
- **F8.** An active user session and an initialized VS Code bridge are explicit
  internal operating prerequisites. These are the concrete cases behind the
  abstract `unavailable` launch failure category.
- **F9.** Token counting is not required. Nothing in the contract counts tokens.

## Known-unproven

Not assumptions. These are claims we know we have **not** established, and that
must not be assumed anywhere in v0.1. The contract is built so that each can go
either way without the chat, message, session, or binding model changing, and
each states what would settle it.

- **U1. Whether the bridge can deliver a further prompt to an already-running
  agent.** Persistent multi-turn delivery is unproven; only the one-shot
  launch/request path (F10) has been demonstrated.
  *Claim #90 can settle:* send a second instruction to an agent the bridge
  already started, and record whether it arrives and whether the agent's reply
  is attributable to that session.
  *How the contract survives either answer:* continuation is a declared launcher
  capability (`launcher_capabilities.continuation`), not a contract-wide
  guarantee. `persistent` and `fresh_binding` are both first-class, and fixtures
  `12-continuation-persistent` and `13-continuation-fresh-binding` show the same
  three-turn chat producing an identical user-visible transcript under each.
  Nothing outside the launcher branches on the answer.
  *What is deliberately still open:* what the instruction packet contains under
  `fresh_binding`. It is specifically **not** "the whole prior chat by default",
  because that would make U2 part of the architecture. #86 and #87 must not
  settle this by default; it is an early-dogfood question.

- **U2. Any trustworthy instruction-payload size or truncation limit.** No bound
  has been measured. The previous revision asserted 65536 bytes and enforced it;
  that number was invented here and has been removed.
  *Claim #90 can settle:* send instruction packets of increasing size through
  the internal path and report the size at which truncation or failure actually
  begins, and whether truncation is silent.
  *How the contract survives either answer:* the bound is a per-launcher declared
  quantity, `launcher_capabilities.instruction_bound_bytes`, whose only honest
  value today is `null`, meaning not measured. The enforcement mechanism is live
  and fixture-tested in both directions
  (`06-instruction-text-over-limit` with a declared bound,
  `14-unmeasured-instruction-bound` without one). Supplying the number is an
  edit to one field in a store, not to this contract.
  *Why this matters more than it looks:* until transition preconditions became
  executable, the bound was skippable anyway — a store could omit the
  `launch_request` record entirely and no size rule fired.

- **U3. The shape in which an agent's output arrives.** The proven internal path
  *returns a response*; the contract's original `events` seam assumed a stream of
  discrete payloads with a distinguishable end. Those may not be the same shape,
  and which one the internal bridge offers is not established.
  *Claim #90 can settle:* record whether the internal path exposes an ordered,
  resumable stream with an end-of-stream signal, or returns one response per
  call; and if a stream, whether a reader-side failure is distinguishable from
  the agent's stream closing.
  *How the contract survives either answer:* `response_shape` is declared per
  launcher. A `one_shot` launcher has no stream, so `stream_end` evidence is
  rejected for its sessions and it reaches `unknown` through explicit
  observations instead; a `stream` launcher must make end-of-stream
  distinguishable and must not let a reader-side failure masquerade as it.
  Preservation, ordering, and correlation are identical under both.
  *Direct consequence for #88:* it must render from preserved
  `diagnostic_event` records without assuming event granularity that may not
  exist. One event per response is a permitted and honest recording.

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
  for a given agent, distinguishably from a stream that is merely quiet; and,
  separately, **whether a failure to read the stream on our side is
  distinguishable from the agent's stream closing**. Record both, because they
  are different facts and only the second tells us whether the agent is gone.
  *Exposure:* contract rule 5.3 case 2. It is no longer the only non-launcher
  route into `unknown` — causes 4 to 6 (unconfirmed stop, failed re-attachment,
  failed stream read) are explicit observations that do not depend on A2. If A2
  is false, a merely quiet agent is still unresolvable in v0.1 by design, and
  that remains #83's gap and must not be closed here with a timer. See also U3,
  which asks whether there is a stream at all.

- **A3. A started agent can later be stopped through an opaque handle.** This is
  the load-bearing assumption of the whole lifecycle, and it is flagged as such:
  sections 5 and 6 treat `stop` throughout as though it holds.
  *Claim to test:* the handle the internal launch returns is sufficient to stop
  that agent, without the caller supplying host or process detail; and whether
  the handle survives a harness restart well enough to re-attach through it.
  *Narrowed, 2026-09-12:* A3 is now the whole of what the seam asks of a
  launcher's memory. Contract 6.1 was corrected so that `stop`, `events` and
  `deliver` take the handle rather than the `session_id`, because the earlier
  signatures silently assumed a second and much larger capability — that a
  launcher can map a harness session id to an agent from state of its own that
  outlives a harness restart. The only internal path proven to work is a
  one-shot script with no process and no storage, which cannot, and the
  contract now says in 6.1 that no launcher is required to. What #90 must test
  is therefore A3 exactly as written and nothing beyond it: hand the launcher
  back the handle it returned and see whether that is enough. Reported as
  contract defect C1 by #87.
  *Exposure:* the `stop` operation and the `running -> terminated` transition.
  The design now has a fallback if A3 is false: an unconfirmed stop and a failed
  re-attachment are recorded observations that carry the session to `unknown`,
  from which the user can always reach `abandoned` and free the chat (contract
  5.4). A3 being false costs the ability to stop an agent; it no longer costs the
  chat.

- **A4. Superseded by U2 above.** This entry previously read "a 65536-byte UTF-8
  instruction text is accepted in one launch". No such measurement exists, and
  the number was invented in this document. Stating a guessed quantity among
  assumptions understated the problem: it was not a claim awaiting confirmation,
  it was an unmeasured value being enforced as a bound. It has been removed from
  the contract and restated as known-unproven U2, where the obligation is to
  measure rather than to confirm.

- **A5. Instruction text plus correlation is the whole launch input.**
  *Claim to test:* no per-launch host-specific parameter beyond instruction text
  is required internally. *Exposure:* the portability rule in section 6, which
  is the entire point of the seam. If a per-launch host parameter is required,
  it must be absorbed into the internal launcher's own configuration rather than
  added to the packet.

- **A6. Superseded by U1 above.** This entry previously carried a provisional
  assumption that `deliver` works. Human direction of 2026-09-12 settles the
  handling rather than the answer: it may not be assumed, and continuation is now
  a declared launcher capability. U1 additionally covers what A6 did not — the
  bound on a delivery packet, and its preservation. Both now hold identically to
  the launch packet (contract 6.4).

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

- **A10. A retry after an ambiguous launch is safe to express as a new session.**
  *Claim to test:* when the harness dies mid-`launch`, whether the internal path
  leaves an agent running that a retry would duplicate, and whether that
  duplicate is detectable.
  *Exposure:* contract 5.4. A retry is a new session with its own packet, so a
  double launch stays visible as two sessions rather than hiding inside one. If
  the internal path can leave an undetectable orphan, #90 should say so; v0.1
  deliberately does not reap it, because automatic recovery is #83.

## The question that most changed this design, and how it was settled

**Can the internal bridge deliver further instruction text to an agent it has
already started, or does every user turn require a new launch?**

An earlier revision of this contract took a provisional assumption that it can,
defined `deliver` as an operation every launcher provides, and wrote fixtures in
which one agent spans many turns. Human direction of 2026-09-12 replaced that:
the question is still open, and holding it as a fixed capability was the error.

**It is now settled as a matter of design rather than of fact** (U1). Continuation
is a declared capability of a launcher. The chat and session model is provably
invariant across both modes rather than assumed to be, and the two consequences
the earlier revision flagged as needing a product decision are both resolved:

1. *"the harness would have to open the next binding automatically"* — it does
   not. **The user sending a turn is itself the user action that opens the next
   binding.** No harness autonomy, no timer, and no #83 behavior is required.
   Contract 4.4 states it and the creation transition's citation of a `msg_` user
   message enforces it.
2. *"each launch would have to carry enough prior chat history to be useful"* —
   deliberately undecided, and explicitly not the default. Carrying the whole
   prior chat would make an unmeasured payload limit (U2) part of the
   architecture. Packet composition under `fresh_binding` is an early-dogfood
   question, and #86 and #87 must not settle it by default.

Continuation semantics are a compatibility assumption and a test, not a blocker
on current development.

## Pressure points recorded and not acted on

Places where this contract felt the pull of a later release and deliberately
stopped. Each is a live design tension, not an oversight:

- **Toward #83.** Leaving `running` honestly requires either an explicit
  observation or a timer. The contract admits only observations (rule 5.3) and
  states that no transition occurs because time passed. The earlier revision drew
  that line one notch too far in: it excluded a failed `stop` and a failed
  re-attachment, which are observations of attempted interactions rather than
  timers, and excluding them left a chat with no legal exit. They are now causes
  4 to 6. The line itself is unchanged — an agent that is merely quiet is still
  unresolvable in v0.1 by design, and that pressure belongs to #83.
- **Toward #82.** The launch boundary still has no `status` or `health`
  operation, and should not gain one: interrogation-for-monitoring is squarely
  #82. That boundary was, however, doing double duty in the earlier revision. It
  was also being used to avoid defining what happens to a live session across a
  restart, which D1 makes a v0.1 obligation rather than #82's. That is now
  contract 5.4, closed inside `events` and the durable record model rather than
  by adding an operation. The bounded out-of-band diagnostic retrieval (rule P4)
  is a retrieval and derives nothing; the moment it aggregates, interprets, or
  ranks, it has become #82.
- **Toward #84.** `agent_binding` exists as a separate record partly so a second
  concurrent agent is representable and therefore rejectable. That is the shape
  #84 will relax, and it should relax the binding cardinality rule rather than
  the chat, message, or session records.

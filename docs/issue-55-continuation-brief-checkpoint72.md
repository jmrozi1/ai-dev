# Issue #55 — checkpoint 72: the continuation brief's reachability report

**This document was written at checkpoint 75, not at checkpoint 72.** Checkpoint 72 was
published — commit `381689d6e3f235efb047a5921811941426d95875`, whose entire message is the
single character sequence `72` — with no document of its own. The session that built it was
lost in a VS Code crash, and a chat transcript is not canonical state in this project
anyway. Checkpoints 73 and 74 both carried "neither checkpoint-72 residual closed" forward
as a live obligation against a statement that existed nowhere durable.

So this is a **reconstruction from durable evidence**, and it says which parts are which:

- **What was built** and **what the evidence is** are read directly off the commit, the
  shipped code and the shipped tests. They are facts.
- **The residuals** are marked `RECONSTRUCTED` throughout. The original statement is gone.
  What is below is grounded in named files and lines; nothing is inferred from memory,
  because there is no memory to infer from.

---

## What checkpoint 72 built

Two files, and nothing else:

```
ai_dev_flow/session_lifecycle.py | 162 +++++++++++++++++++-
tests/test_session_lifecycle.py  | 310 ++++++++++++++++++++++++++++++++++++++-
2 files changed, 461 insertions(+), 11 deletions(-)
```

It implemented, in the product, the accepted **checkpoint-71 deployment condition 4**: a
replacement that reports it cannot reach its brief is not to be treated as having
continued, notwithstanding the reported state.

| Added at `381689d` | What it is |
|---|---|
| `CONTINUATION_UNREACHABLE_MARKER = "AI-DEV-BRIEF-UNREACHABLE"` | the one string the replacement is asked to emit, and the whole of condition 4 expressed in code |
| `CONTINUATION_BRIEF_UNREACHABLE`, `CONTINUATION_UNENFORCEABLE` | two new continuation states beside `continued` / `refused` / `failed` |
| `REASON_CONTINUATION_BRIEF_UNREACHABLE`, `REASON_CONTINUATION_REPORT_UNSCORED` | their reasons |
| `_reachability_report(result)` | three answers, not two: `True` reported, `False` not reported, `None` the channel returned no score at all |
| one sentence added to `ContinuationBrief.prompt` | the report protocol: what to say if the handoff named in the brief cannot be read |
| the marker prepended to `markers` inside `continue_from_durable_state` | the route asks for it on every invocation and a caller cannot take it away or double it |
| the `continuation-continued` detail sentence, rewritten | it now states what it does *not* establish |

### The polarity, which is the design

The marker literal is in the brief the replacement is handed — a replacement cannot emit a
word it was never given — and the worker scans the **reply**, so any replacement can put
the string in its answer by quoting its instructions back. A marker whose presence meant
*success* would therefore be manufacturable by a replacement that read nothing. A marker
whose presence means *failure* inherits the same echo and lands on the safe side: the worst
a quoting replacement can do is have its own turn refused. The route asks for a report of
failure and never for a proof of success. (`session_lifecycle.py:2956-2985`.)

## The evidence checkpoint 72 has

- `tests/test_session_lifecycle.py` went from **301** test methods at checkpoint 71
  (`c0b6a3a`) to **308** at `381689d`. The seven are the case-H block, now at
  `tests/test_session_lifecycle.py:6012-6218`.
- The discriminating fixture is inside the suite rather than beside it:
  `test_case_h_the_fixture_itself_discriminates_and_here_is_the_proof` drives **one**
  sender construction with **two** replies and asserts the marker score, the state and the
  `continued` verdict all move together — and it scores them through
  `claude_worker._scan_markers` itself (`ai_dev_flow/claude_worker.py:677-682`), reached via
  the test module's `_score_markers`, so the fixture cannot agree with the product about
  scanning while the real worker's scan does something else.
- The third state is asserted to be distinguishable from the unrelated-failure case:
  `test_case_h_a_channel_that_scores_nothing_is_not_a_continuation` puts
  `CONTINUATION_UNENFORCEABLE` and `CONTINUATION_FAILED` side by side on the same bound
  successor.
- The echo hazard is asserted in **both** directions by
  `test_case_h_the_marker_is_a_failure_report_and_cannot_manufacture_a_green`: a replacement
  that echoes the whole brief is refused, and a replacement that says nothing at all is
  green.
- The brief is asserted still to carry only locators plus the one protocol line, and still
  to be resolvable by a fresh reader holding the same three durable reads
  (`test_case_h_the_brief_still_carries_only_locators_and_this_one_protocol_line`).

**No real provider session was run by checkpoint 72.** The commit touches no runner, no
worker and no entry point; the whole of it is `session_lifecycle` and its suite.

---

## The residuals — RECONSTRUCTED at checkpoint 75

Checkpoint 73 wrote "neither checkpoint-72 residual closed" and checkpoint 74 wrote "no
checkpoint-72 residual closed". **The count "two" comes from checkpoint 73's prose and from
nowhere else.** Two residuals are reconstructable from durable evidence, and they are the
two below. Whether they are the same two the lost session wrote down cannot be established.

### R1 — only the *reported* unreachable case is caught. `RECONSTRUCTED`, and grounded.

`ai_dev_flow/session_lifecycle.py:2956-2985` says it in the shipped code:

> What that buys, exactly, and no more: condition 4 is a rule about a replacement that
> *reports* it cannot reach its brief … This enforces the reported case. A replacement that
> silently ignores its brief is not caught here, and no state below claims it is.

and the suite asserts the consequence rather than hiding it — in
`test_case_h_the_marker_is_a_failure_report_and_cannot_manufacture_a_green`, a replacement
that reads nothing and says nothing is **green**, and the detail sentence is asserted to
contain "does not establish".

**This is correct against the accepted condition, not a shortfall of it.** Checkpoint 71's
own record (`docs/issue-55-rotation-dogfood-checkpoint71.md`, "What did NOT work", item 3)
observed a replacement that "said so honestly and asked to be unblocked" — the reported
case. The residual is that the route establishes only that the replacement did not say it
could not reach the brief, and never that it read or understood one.

### R2 — `continue_from_durable_state` still has zero production callers. `RECONSTRUCTED`, and grounded.

Grounded by search over the shipped package: outside `tests/`, `continue_from_durable_state`
appears at its own definition (`ai_dev_flow/session_lifecycle.py:3269`), in
`manager_controller`'s import, and in `ManagerController.continue_from_durable_state`
(`ai_dev_flow/manager_controller.py:328,363`) — a pass-through that nothing calls. No
`main()`, no entry point and no driver reaches it. Checkpoints 73 and 74 both restate the
same fact from their own side: "`replace_old_context`, `continue_from_durable_state` and
`release_continued_context` still have none."

The consequence, which is the residual: the new check has **never met a real provider**.
Both of its refusing paths are reachable only through the suite. In particular the
false-positive path is real and unexercised outside tests — the marker is in the brief, so a
replacement that quotes its instructions, or that discusses the protocol while doing the
work, trips `continuation-brief-unreachable` and has a genuine turn thrown away. The suite
asserts that this happens (`…cannot_manufacture_a_green` drives the echo deliberately); what
no durable evidence establishes is how often a real replacement does it.

### What is deliberately NOT claimed as a residual

The finalization-retry residual named at `docs/issue-55-rotation-dogfood-checkpoint71.md:126`
belongs to checkpoint 71 and was carried forward by it, not opened by checkpoint 72. It is
not counted here.

Nothing else is listed. There is no third residual grounded in a file, and inventing one to
match a remembered shape would be worse than the gap it filled.

---

## Status of this record

This is a reconstruction, published at checkpoint 75 so that checkpoints 73 and 74 stop
carrying an obligation nobody could read. **It does not close either residual, and it is not
a checkpoint-72 acceptance.** R1 and R2 both stand exactly as they stood at `381689d`:
checkpoint 75 changed nothing in `continue_from_durable_state`, added no production caller
for it, and ran no provider session.

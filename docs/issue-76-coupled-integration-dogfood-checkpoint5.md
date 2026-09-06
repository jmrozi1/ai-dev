# Issue #76 execution checkpoint 5 — coupled development/integration dogfood

Checkpoint 5 dogfoods the model checkpoint 4 wrote down: `skills/module-development`
for how a slice is developed, `skills/integration-signal` for how it is validated.
This document is the measured evidence, not a narrative of the session.

**Every change dogfooded here is a previously recorded finding from this ticket's own
checkpoint-4 review.** No new requirement was introduced to create something to test.

## What was developed, and how it was gated

Four candidates were submitted in order on `flow/github/jmrozi1/ai-dev/76` from the
published checkpoint-4 tip `4282c71`.

| # | SHA | slice | author | local gate | runtime |
|---|---|---|---|---|---|
| S1 | `e52c069` | catalogue row restored the `module-development` negative trigger | executor | `test_skill_catalog_audience_coverage` 4, `test_module_development_skill` 10, `test_integration_signal_skill` 10 — all OK | 0.010 / 0.007 / 0.006 s |
| S2 | `3ecd4ab` | three obligations asserted by contract rather than by wording | executor | `test_module_development_skill` 11 OK, `test_integration_signal_skill` 11 OK | 0.009 / 0.006 s |
| S3 | `5afd141` | remaining catalogue rows agreed with their skills' canon | orchestrator | 24 tests OK | 0.020 s |
| S4 | `c764922` | integrated provider `main` `db6583c` into the candidate | orchestrator | `test_role_invocation` 49 OK; four skill modules 35 OK | 84.202 s / 0.028 s |

Each gate is **the module tests plus the tests for the contracts the slice affects**, and
nothing else. No slice ran the full suite. That is `integration-signal`'s rule that the
synchronous gate is author-owned and cheap, and integration is not that gate.

## The measurement that makes the async model load-bearing

| what | tests | runtime |
|---|---|---|
| all four skill modules together | 35 | **0.028 s** |
| `tests.test_role_invocation` alone (S4's affected contract) | 49 | **84.202 s** |
| full suite (integration), Windows | 3067 | **2753 s under concurrent load** |
| full suite (integration), WSL | 3067 | **136 s** |

**Corrected after re-review (N1).** This originally read *"roughly three thousandfold between a
slice's own gate and the integration suite"*, computed as 84.202 / 0.028. That is wrong: 84.202 s is
`test_role_invocation`, which the same table labels S4's **affected contract** — another *local*
gate, not the integration suite. Against the integration suite the spread is **~4,750x** in WSL
(133.1 s / 0.028 s) and **~98,000x** on Windows (2753 s / 0.028 s). The error understated its own case, which is how it
survived.

## Coalescing — three superseded candidates dropped, none re-run

Policy: keep at most the active run plus the newest pending candidate.

- S1 dropped when S2 arrived — S2's range `4282c71..3ecd4ab` contains S1.
- S2 dropped when S3 arrived — S3's range contains S2.
- S3 dropped when S4 arrived — S4's range contains S3.

Three queued candidates were coalesced away and **no separate integration answer was ever
produced for them**. Per the skill, that is a scheduling decision and not a validation
claim: it does not assert S1-S3 were good, only that nobody needed a separate answer about
them once S4 subsumed their range.

## Continued independent work

Development never blocked on an integration result:

- the executor authored S1 and S2 in the candidate worktree **while run 1 was executing**
  in a separate worktree;
- the orchestrator authored S3, then integrated `main` as S4 and ran its 84 s gate,
  **while runs 1 and 2 were still executing**;
- no submission was held open awaiting an integration result, and no slice waited for a
  green before starting.

## Findings about the policy itself

A dogfood that only confirms the policy has not tested it. **Eleven findings**, none of them
remediated here — remediation would be the scope expansion this ticket forbids. F1-F4 came from
using the policy; F5-F7 from the acceptance gate; F8-F9 from the independent review; F10-F11 from
the re-review, which ran the suite in both environments and twice at the accepted tip.

### F1 — exact-SHA binding implies workspace isolation, and the skill does not say so

The first integration run was started in the **shared development worktree**. Dispatching
the executor into that same worktree would have mutated files underneath a run whose entire
claim is to describe one exact commit. The run was stopped and restarted in a worktree
checked out detached at the tested SHA.

`integration-signal` states the recording obligation — tested SHA, range, suites, runtime,
outcome, last green — but never says the run must execute against an **immutable checkout**
of that SHA. Recording a SHA while executing against a mutable tree produces a result that
is precisely as wrong as an unlabeled one, and nothing in the current text catches it.

### F2 — the queue model assumes one run role, and its single-active-run rule has an unstated resource basis

Two distinct problems surfaced here, and the second was found by getting it wrong.

**The vocabulary gap.** The policy's "at most the active run plus the newest pending
candidate" treats all runs as interchangeable candidate validations. This checkpoint needed
runs with **different roles**: an acceptance run on the commit actually being accepted, and
reference runs (the published checkpoint-4 tip, and the merge-base) whose purpose is failure
attribution rather than candidate validation. Reference runs are not candidates and must not
be coalesced against candidates, but the skill has no vocabulary for the distinction.
Applied literally, the queue rule would either forbid the attribution references that the
policy's own attribution section requires, or force them to masquerade as candidate runs.

**The resource basis, measured.** Reasoning from that gap, this checkpoint started the
acceptance run while two reference runs were still executing, rather than idling ~40
minutes. **That was wrong, and the cost was measurable:**

| concurrent full-suite runs | run 1 throughput |
|---|---|
| 1 | ~**82 tests/min** |
| 3 | ~**3 tests/min** |
| 2 (after the third run was stopped) | ~**33 tests/min** |

Throughput collapsed by more than an order of magnitude and projected completion went from
tens of minutes to hours, so the third run was stopped. Throughput then recovered roughly
tenfold **on the same suite region, with the only change being one fewer concurrent run** —
which is what separates contention from the suite's own slowness. The suite does contain a
genuinely slow process-spawning section, so these figures are throughput under load rather
than a controlled contention benchmark, but the recovery makes contention the dominant term
rather than a conjecture.

**Provenance limit:** the partial logs carry no timestamps and no summary lines, so these
throughput figures are **not reproducible from any retained artifact** — they were observed live.
They corroborate against `runA`'s recoverable average of ~67 tests/min over 2753 s, but they are not
verifiable after the fact, and nothing in this finding should be rested on their exact values.

**The sharper fact, surfaced by the independent review, is that the concurrency bought nothing at
all.** All three reference runs were killed mid-execution and none produced a summary line, so they
contributed **zero evidence** to this checkpoint. The attribution that actually worked was the
single-threaded 81 s focused replay, which needed no concurrency whatever. The error was therefore
not merely under-warned-against by the skill — it was unnecessary **on its own stated terms**, since
the runs it raced were never required. That is the honest account, and it is less flattering than
the one this section originally gave.

The finding is that **"at most the active run plus the newest pending candidate" is not only
a bookkeeping rule about which answers are worth having — it is also a concurrency limit
with a resource justification**, and the skill states only the former. A reader who
internalizes the stated rationale ("a backlog produces results about commits no one is
waiting on") will conclude, as this checkpoint did, that runs somebody *is* waiting on may
safely proceed in parallel. The text gives them nothing to catch the error.

### F3 — a negative-trigger gap in the catalogue was systematic, not a single row

The checkpoint-4 review found the `module-development` catalogue row dropped its
frontmatter negative trigger. The same gap held for `integration-signal` and for
`change-validation` — the latter **accepted at checkpoint 3** — so the defect predates the
finding that surfaced it. Corrected in S3 because checkpoint 6 installs this catalogue and
a row reading broader than the skill it describes would otherwise be activated. No
`SKILL.md` was changed; only the catalogue's description of them.

### F4 — the policy requires recording a run's outcome but says nothing about observing one

`integration-signal` requires each run to record its runtime and outcome, and it assumes those
facts are simply available. Obtaining them from a long-running background run turned out to be
the hard part, and naive observation produced **two false conclusions in a single session**:

1. **Buffered output read as stalled progress.** Python block-buffers stdout when redirected to a
   file, so the log lagged far behind actual execution. Progress counts derived from the log moved
   erratically and even appeared to go backwards. Runs must be launched unbuffered (`python -u`)
   for their logs to be a usable progress signal at all.
2. **A broken liveness check read as a dead run.** The process check used a shell whose path
   translation rewrote the `/FI` filter argument into a filesystem path, so the command errored and
   the "no matching processes" branch was taken. Three healthy runs were declared dead on that
   basis. **Nothing had died**; the check had.

Both failures share a shape the skill does not warn about: **an integration signal's absence of
news is not news.** A missing outcome can mean still running, crashed, or unobservable, and these
must be distinguished before any of them is recorded. A monitor that watches only for success
cannot tell them apart, and silence looks identical to progress.

This is a genuine gap in deliverable 13 as written. The policy defines what to record and what a
result binds to, but not how a run's liveness and completion are established — and an
asynchronous model rests entirely on that.

## Failure attribution — a real red, attributed to the environment rather than the range

The acceptance run went **red**, which gave this checkpoint the genuine failure its
attribution requirement needs. It was attributed by the policy's own cheap path.

| run | tested SHA | environment | result | runtime |
|---|---|---|---|---|
| candidate run | `c764922` | Windows 11, CPython 3.10 | **FAILED** — failures=30, errors=77, skipped=19, of 3067 | 2753 s (45.9 min) |
| focused replay of the non-passing set | `4282c71` | Windows 11, CPython 3.10 | **failures=30, errors=77** | 81.033 s |
| baseline | `4282c71` | WSL2 Linux 6.6, CPython 3.14.4 | failures=8, errors=1, skipped=6, of 3054 | 134.2 s |
| acceptance | `c764922` | WSL2 Linux 6.6, CPython 3.14.4 | failures=8, errors=1, skipped=6, of 3067 | 135.9 s |
| acceptance at tip | `4804621` | WSL2 Linux 6.6, CPython 3.14.4 | failures=8, errors=1, skipped=6, of 3067 | 136.4 s |

**The non-passing sets of all three are identical by id**, verified by sorted `comm`/`diff` with
zero lines in either direction. The candidate introduces **zero new failures** over the accepted
checkpoint-4 tip, and the +13 tests it adds all pass. All three ran in the **same** environment,
which is what makes the comparison mean anything — F5 applied, not merely stated.

The same environment also settles the skip question: **both** `4282c71` and the candidate show
**6** skips here, so checkpoint 4's recorded *5* is an artifact of its environment, not a code
difference. The skips are `claude-agent-sdk is not installed` x3, `pwsh`/`powershell not
available` x2, and one Windows-only junction test.

The environment column is not decoration: F5 below is the finding that the policy does not
require it, and every row above is uninterpretable without it.

The 101 distinct non-passing tests were extracted from the acceptance run and replayed
**only they** against the published checkpoint-4 tip. The counts match exactly, so the whole
non-passing set reproduces on a commit that predates every change in this checkpoint.
`integration-signal` states the rule directly: *if it reproduces on the last green SHA, **or on an unrelated
commit**, it is environmental, flaky, or older than the range.* Since `4282c71` is **not** a green
SHA, it is the second clause that licenses this conclusion, not the first. **The range `4282c71..c764922` is
exonerated.** Corroborating: none of the 101 tests lie in the four modules these slices touch.

Root causes, all host-environmental:

| cause | count |
|---|---|
| `OSError [WinError 1314]` — required privilege not held (symlink creation) | 31 |
| `OSError [WinError 193]` — not a valid Win32 application (POSIX scripts) | 18 |
| `AttributeError: module 'os' has no attribute 'getpgid'` | 18 |
| `OSError [WinError 10038]` — operation on a non-socket | 7 |
| exit `9009` — *"Python was not found"* | 10 |
| remainder — `os.fork`, `os.geteuid`, assorted assertions | ~17 |

Attribution cost **81.033 s** against the ~90 minutes a whole-suite re-run across the range would
have taken — the saving the policy's escalation order exists to produce.

**This figure was reported as "1 second" until the independent review caught it (B1).** That number
came from the *broken* replay's wrapper output, not from the corrected run — the same discredited
instrument F6 exists to warn about, misread inside the document that reports F6. The conclusion is
unchanged (~65x rather than ~6500x); the measurement was wrong and is corrected here rather than
quietly amended.

### Last known green SHA — there is none, and that is the honest entry

`integration-signal` requires every run to record the **last known green SHA**. This checkpoint
records it as **none exists anywhere in this ticket**. Checkpoint 4's accepted record is itself not
green (8 failures, 1 error), and every run here reproduces that same set. The project's integration
suite has no green commit to point at, so the policy's mandated field can only be filled honestly
with an absence.

This is not a defect introduced here, but the deliverable originally omitted the field rather than
recording the absence — and "not recorded" and "none exists" are exactly the two states the policy
elsewhere insists on distinguishing.

**And the plain consequence, which this document previously stopped one sentence short of stating
(N7).** `integration-signal` requires *"an integration run that is **green on the latest relevant
candidate**"* before a named checkpoint is accepted. The run at `644a3c8` is
`FAILED (failures=8, errors=1)`. **The acceptance precondition as written is unsatisfiable in this
project**, and this checkpoint substitutes a different criterion — parity with the accepted
non-passing set — without the policy authorizing the substitution.

Parity is defensible and is what checkpoint 4 also used. But F8 names exactly this shape as a defect
when it appears elsewhere: *a sound engineering argument substituted for a clause the policy does not
actually offer.* The same move is being made here, at the gate itself, and it should be recorded as
such rather than performed silently. **Checkpoint 6 inherits it**, since "merge and confirm
fresh-session discovery" rests on the same gate.

Together with F11 — parity is not even a stable property run-to-run — this is the checkpoint's
strongest evidence that deliverable 13's acceptance gate needs a definition it does not have.

### What the attribution did and did not demonstrate

The attribution ladder has three rungs: affected tests, focused replay, then bisect. Only **focused
replay** was exercised, and it was used to **exonerate a range**, not to localize a failure to a
change.

- **Demonstrated:** range exoneration — a red whose entire non-passing set reproduces on an earlier
  commit is not attributable to the range between them.
- **NOT demonstrated:** failure localization. No failure was ever traced *to* a change, because no
  change in this checkpoint caused one. Rungs 1 and 3 were never needed and are therefore untested.

Recorded explicitly so that a convenient absence of introduced failures is not read as evidence that
localization works. It was not tried.

### F10 — the cost case for asynchronous integration is largely a Windows artifact

F5 concludes that the acceptance gate must run in WSL. Measured in that same authoritative
environment at `644a3c8`:

| what | WSL | Windows |
|---|---|---|
| full integration suite | **~133 s** | 2753 s |
| S4's affected-contract gate (`test_role_invocation`, 49 tests) | **1.677-1.729 s** | 53.7 s unloaded |

A **133-second** synchronous integration gate is not obviously intolerable. `integration-signal`'s
opening argument — that a synchronous gate *"serializes development behind the slowest suite in the
project"* — keeps its logic but loses most of its measured force in the environment this checkpoint
declares authoritative for outcomes.

**The checkpoint had been declaring one environment authoritative for correctness and quietly
retaining the other for costs.** Surfaced by the re-review, which ran both. The async model may
still be right for larger suites or slower hosts; what is not supportable is citing Windows numbers
to justify it while ruling Windows results inadmissible.

### F11 — parity-as-green is not deterministic, and the ticket has no flake provision

The re-reviewer ran the full suite twice in WSL at the accepted tip `644a3c8`:

| run | result |
|---|---|
| first | `failures=9, errors=1` — **one extra failure** |
| second | `failures=8, errors=1` — non-passing set identical by id to the accepted one |

The extra failure was `test_nothing_bounded_out_of_the_launcher_leaks_a_decision_body`, which
asserts the literal `"4242"` is absent from launcher output while the launcher prints the real epoch
second — and that minute contained `4242`. A genuine time-dependent flake in Issue #55 code on
`main`, **not introduced by this candidate**.

The consequence is sharp: under the criterion this checkpoint operates by — *green means parity with
the accepted non-passing set* — **one of two runs at the accepted tip is a red, and the gate would
have rejected its own tip.** F7 says to record membership rather than counts; the missing half is
that **membership is not deterministic**, and neither `integration-signal` nor this ticket has any
provision for flakes: no re-run rule, no quarantine, no distinction between a failure and an
unstable test. Six runs at that SHA now exist and five agree. That is an observation, not the
property the acceptance gate assumes.

### F5 — a result binds to an exact SHA but not to an exact environment

This is the most consequential finding of the checkpoint.

Checkpoint 4's accepted record reports **8 failures, 1 error, 5 skips**. This checkpoint's
acceptance run reports **30 failures, 77 errors, 19 skips**. Both are honest, both are
correctly labelled with their tested SHA, and **they describe the same code**. Checkpoint 4's
suite ran under WSL; this one ran on Windows.

`integration-signal` requires recording the tested SHA, included range, selected suites,
runtime, outcome, and last known green SHA. **It does not require recording the
environment.** Every field the policy demands can be filled in correctly and the result still
be uninterpretable — worse, silently comparable against a green produced on a different
platform. The skill's own prohibition on carrying a stale green forward has an unguarded
sibling: **carrying a green sideways, across environments.**

Concretely, this makes checkpoint 6's acceptance gate a WSL run, not a Windows one. A
Windows "red" here is not evidence against the candidate, and a Windows "green" would not
have been evidence for it.

### F6 — a replay's summary line cannot distinguish a real result from a broken instrument

The first batch replay reported `Ran 101 tests … FAILED (errors=101)` — a clean, plausible
summary that appeared to confirm the attribution. It was worthless: every test had failed
with `ModuleNotFoundError: No module named 'tests'` because the runner placed its own
directory on `sys.path` instead of the worktree. The corrected replay produced
failures=30/errors=77, matching the acceptance run exactly.

**A summary line is not a result.** "101 of 101 reproduce" and "101 of 101 failed to load"
are indistinguishable at the summary, and only the root-cause breakdown separated them. Had
the causes not been checked, a false attribution would have been recorded with complete
confidence. `integration-signal` requires recording an outcome but never requires evidence
that the run measured what it claims to measure.

### F7 — an outcome recorded as counts cannot support the comparison the acceptance gate needs

Checkpoint 4 recorded its result as *"8 failures, 1 error, 5 skips"* and characterised it as
*"four of those pre-existing failures concern skill discovery over the real repository."*

Both statements are true of a **count**. Neither is a **set**. This checkpoint observed the same
totals — 8 failures, 1 error — but **seven** of the eight failures fall in the skill-installation
discovery family rather than four. A matching total is not a matching set, and the record as kept
could not distinguish "identical non-passing set" from "same size, different membership". The
acceptance gate rests entirely on that distinction.

It could not be resolved by reading; the missing evidence had to be regenerated by running the
checkpoint-4 tip again and diffing sorted identities. That was cheap **here** only because the run
costs ~134 s in the right environment.

`integration-signal` requires recording an outcome. It should require recording the outcome's
**membership**, because a count cannot answer the question the next checkpoint will ask of it.

### A note on the regress this exposes

The policy requires the latest relevant candidate to be green before acceptance. When the
acceptance evidence is itself committed to the candidate, recording a run creates a newer commit
than the run describes, and re-running to cover it creates another. This checkpoint terminates the
regress deliberately: the **deliverable** carries the method, the findings, and the three-run
comparison, and the **final acceptance run against the final tip is recorded in the control plane**,
which is not part of the candidate lineage. Recorded as an observation, not remediated.

### F8 — the policy has no provision for a delta no suite can observe

The acceptance run initially bound to `c764922` while the tip was `4804621`, a delta of exactly one
added `docs/` file. The reviewer verified mechanically that **no test in the repository can observe
anything under `docs/`**: repo-wide walks cover `*.py`, `skills/**/SKILL.md` and `prompts/*.prompt.md`,
and every other `docs/` reference names a specific file. The engineering risk was nil.

But `integration-signal` says without carve-out: *do not accept a named checkpoint on a green that
predates the commit being accepted.* Its escape clause covers a *"descendant-free equivalent whose
change range covers it"* — and an **ancestor** is not that. The original reasoning here substituted a
sound engineering argument for a clause the policy does not actually offer, which is the shape of
rationalization the rule exists to prevent.

Rather than rest on it, the suite was simply re-run at each true tip; in the correct environment that
costs ~136 s, so **rigour was cheaper than the argument for skipping it**. The finding stands on its
own: the policy has no provision for a delta that no selected suite can observe, and forces either a
re-run or a rationalization. Recorded, not remediated.

### F9 — a catalogue obligation was fixed with no test that can hold it

S1 and S3 restored negative triggers to three catalogue rows. `test_skill_catalog_audience_coverage`
ties rows only to package **names and paths**; it does not constrain description text at all.
**Nothing prevents those triggers drifting out again.**

That sits in tension with this ticket's own accepted standard — checkpoint 4's blocking finding was
precisely that *an obligation with no test is not remembered*. Whether a catalogue blurb carries a
substantive obligation is a fair question, and it is **not settled here**: the same question is
already open as checkpoint 4's residual 3 for `## ChatGPT Interaction`. It is surfaced rather than
answered, because answering it is an instrument question and instrument redesign is a separate
checkpoint.

The tension is sharpened by F3's own stated reason for making the fix: that checkpoint 6 installs
this catalogue. If the row matters enough to correct before activation, the argument that it is too
trivial to protect is weaker than it looks.

## Orchestrator verification, and a claim it did not support

The widened assertions in S2 were checked against the **live** skill file rather than the executor's
fixtures:

| step | result |
|---|---|
| unmutated `skills/module-development/SKILL.md` | `test_module_development_skill` **OK 11** |
| injection section excised (awk, 7064 -> 5964 bytes) | **FAILED** |
| section restored | **OK 11**, working tree clean |

**The conclusion originally drawn from this was wrong, and the independent review caught it (B2).**
The document claimed the widened wordings "still fail when the obligation is deleted." They did not.
The module failed on a **neighbouring pre-existing narrow assertion**; the widened set itself still
returned `True`, because `assert_covers` reads the whole file **including YAML frontmatter** and

- `module-development`'s frontmatter says *"injected clocks/filesystems/…"*, and
- `integration-signal`'s frontmatter contains the literal *"stale green"*.

Two of the three widened assertions were therefore **inert**: they could not fail while the
frontmatter stood, whatever the guidance body said. A green module was read as proof of protection
that a green module could not provide — the same mistake as B1, in the section written to guard
against exactly it.

**This was not a regression from S2.** The reviewer verified at `e52c069` that the *narrow* sets were
equally frontmatter-satisfiable, and the obligations stayed protected by neighbouring assertions
throughout. Nothing was lost; a claim was made that the evidence never supported.

### Remediated at `c8ee178`, and proved in the direction that matters

Both assertions are now scoped to the guidance body via the `_guidance_body` / `assert_body_covers`
instrument the executor had already built for this purpose — whose message states the principle
outright: *naming it in the frontmatter description does not carry it*. The instrument was ported
verbatim into `integration-signal`, not redesigned. `git diff --name-only e5ccf6d c8ee178 -- skills/`
is **empty**; the change is two test files, +23/-2.

Orchestrator-verified independently, with the frontmatter deliberately left intact:

| step | result |
|---|---|
| staleness section excised, frontmatter still carrying *"stale green"* | **FAILED on its own assertion** — *"integration-signal's guidance body does not cover the staleness prohibition; naming it in the frontmatter description does not carry it"* |
| section restored | **OK 11**, `skills/` diff empty |

**A correction, made after the re-review caught it (B3).** This section previously closed with
*"Before remediation this same mutation returned green."* **That was false.** Excising the whole
section also removes the text the *neighbouring* assertions match, so the module failed
pre-remediation too — on a different message (*"does not cover refusing an ancestor's green for a
descendant"*). Reproduced both ways. The module-level outcome is **unchanged** by the remediation;
what changed is **which assertion inside the method fires**, and whether the widened one is inert
under frontmatter or live on the body.

A mutation that does isolate the difference at module level exists, and it is not a section
excision — delete **lines 57-60 only**, the heading and lead paragraph, keeping the bullets:

| mutation: delete `integration-signal` lines 57-60 | result |
|---|---|
| **pre**-remediation test module (`e5ccf6d`) | **OK 11** — green, obligation gone, nothing noticed |
| **post**-remediation test module (`644a3c8`) | **FAILED** on the widened assertion's own message |

That is the real before/after, and it shows the remediation is load-bearing. The earlier claim
was a punchier version of a true finding, and the instrument does not produce it.

### A disclosure from the remediation, worth more than the fix

The executor reported that the **injection** obligation could not be falsified by excising its
section alone: `## Develop Behavior And Its Tests Together` closes with the incidental clause *"which
is the real reason to keep the world injected."* Section-only excision therefore left the body still
matching, and the module still failed on the neighbour — the reviewer's exact symptom, reproduced
for a second, independent reason.

So the obligation is stated in the body **twice**, and a section-excision instrument cannot falsify
an obligation that is restated incidentally elsewhere. The re-review confirmed this independently and
found a **further instance neither the executor nor this document had cited**: at `4282c71`, excising
`## Shape Modules As Independently Constructible Units` still fails on a *different* assertion,
because *"independently constructible"* also appears at line 8, the skill's own opening paragraph.

Every mutation result in this ticket is therefore an **upper bound** on the protection it
demonstrates, not a measurement of it — **at clause granularity**.

**That qualifier matters and an earlier draft omitted it.** It does *not* impeach checkpoint 4's
acceptance: the re-reviewer re-ran that proof at `4282c71` and it still fails on **its own test
method's own assertion** (`failures=1`, that exact test; restored `OK 10`). Checkpoint 4's accepted
result survives at test-method granularity. Stated without the qualifier, this finding reads as
self-impeachment of accepted work with no defect behind it, which would be false.

Recorded, not remediated: fixing it is instrument design, and instrument design is a separate
checkpoint.

## The pattern in this checkpoint's own reporting

Three blocking findings across two independent reviews, and **all three are the same error**:

| finding | the claim | what the evidence showed |
|---|---|---|
| **B1** | attribution cost "1 second" | 81.033 s; the 1 s came from the **broken** replay this document discredits |
| **B2** | the mutation proved the widened assertions still fail on deletion | it failed on a **neighbouring** assertion; the widened sets were inert |
| **B3** | "before remediation this same mutation returned green" | it failed pre-remediation too, on a different message |

Every one converted an accurate underlying finding into a sharper before/after that the instrument
does not produce. None was a fabrication and none changed a conclusion — the attribution still holds,
the remediation is still load-bearing, the range is still exonerated. But **the deliverable that
reports "a summary line is not a result" (F6) made the summary-line mistake three times**, and each
was caught by a reviewer rather than by the author.

That is worth recording as bluntly as the policy findings. This checkpoint's own evidence is the
strongest argument in it for adversarial review being non-optional: the errors were not in the code,
which was checked mechanically at every step, but in the **prose asserting what the code had shown**
— the one artifact no test covers.

Two further reporting errors, both corrected above rather than defended: the headline cost ratio was
computed against the wrong denominator (N1), and the cost case for the whole async model rested on
numbers from the environment this checkpoint rules inadmissible (F10).

## Findings surfaced by the re-review and recorded without remediation

- **N4 — frontmatter-satisfiability is systemic.** An AST sweep found at least seven further
  whole-file-scoped assertions satisfied by frontmatter alone: `integration-signal` L134, L156, L157,
  L216 and the required-fields `assertIn`s; `module-development` L256, L293. Outside the remediation
  rail's scope, correctly. But F3 made exactly this "systematic, not a single instance"
  generalization for the catalogue rows, and it was available here and not made.
- **N5 — the falsifiability proof no longer exercises the path it certifies.** The
  proof-of-non-vacuity tests still call `_covers(_normalized_text(fixture), ...)` while the live
  assertions now call `assert_body_covers`. Equivalent today, because the fixtures carry no
  frontmatter — but that is the same seam that let B2 through.
- **N8 — run artifacts do not carry their SHA or environment.** Five WSL logs are distinguishable
  only by filename and mtime; the SHA lives in a disposable checkout's `HEAD`, not in the record.
  `runA.status` does it correctly for the Windows run. Given F1 and F5, that field belongs in the
  artifact.
- **N9 — "continued independent work" is weaker than presented.** Committer timestamps put all four
  slices at 08:13-08:17; `runA` started 08:55. The substantive claim holds — no submission blocked on
  an integration answer — but the specific claim that S1-S4 were authored *while runs executed* rests
  on the three reference runs this document concedes contributed zero evidence, whose logs carry no
  timestamps. Not refutable, not verifiable.
- **N10 — coalescing has no retained artifact.** Checkpoint 5 names superseded-run coalescing as a
  thing to dogfood, and the evidence for it is prose. A two-line queue log would have been the proof,
  and its absence is conspicuous in the document that raised the reference-run vocabulary gap (F2).
- **N11 — the suite invocation is not recorded.** "Selected suites" is a required field of the
  policy's own record, and the command determines it. Two different invocations were used across
  these runs; immaterial here (same 3067 tests), but unrecorded.

## Recorded residuals — carried, not closed

The semantic-blindness residual from checkpoint 4 is unchanged: a section rewritten to say
the **opposite** of its obligation still passes, as does a vocabulary stub. It is shared by
every accepted skill test module here, was not introduced by checkpoint 4 or 5, and its
remediation is an instrument redesign that is explicitly a separate checkpoint.

Residual 3 from checkpoint 4 — whether `## ChatGPT Interaction` carries a substantive
obligation at all — remains an **open decision**, deliberately not taken here.

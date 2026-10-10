---
name: integration-signal
description: Run integration validation as an asynchronous signal bound to an exact candidate commit rather than a synchronous gate after every change — coalesce superseded queued runs, record the tested SHA and last known green, never carry a stale green onto a newer candidate, attribute a failure from the green-to-red change range, and decide what a red run actually blocks. Do not use to choose which tests a change needs.
---

# Integration Signal

Integration validation is a **signal attached to an exact candidate commit**,
not a turnstile every change walks through. Treating it as a synchronous gate
serializes development behind the slowest suite in the project and still proves
nothing about the commit that is finally accepted.

## Gate Submission Locally; Do Not Gate It On Integration

Before a coherent changeset is submitted, its **module tests and the tests for
every contract it affects** must pass. That is the synchronous gate, it is owned
by the author, and it is cheap enough to run in the inner loop.

Integration is not that gate. It runs **after** submission, in parallel with
continued development. Do not hold a submission open waiting for an integration
result, and do not require an integration pass before starting the next
independent slice.

## Run Against The Latest Submitted Candidate

Start each integration run against the **latest submitted candidate commit**,
not against every commit in order.

Keep at most **the active run plus the newest pending candidate**. When a newer
candidate is submitted while one is already pending, **coalesce**: drop the
superseded queued candidate and let the newer one stand for it, because the
newer candidate's change range already contains the superseded one. Never let
the queue grow one run per commit — a backlog produces results about commits no
one is waiting on while the current tip stays unknown.

Superseding a queued candidate is a scheduling decision, not a validation claim.
It does not assert the dropped commit was good; it asserts nobody needs a
separate answer about it.

## Bind Every Result To Its Exact Commit

Record, for each integration run:

- the **tested SHA** — the exact commit the run executed against;
- the **included change range** — from the previously tested commit to the
  tested SHA, so the run's coverage is explicit;
- the **selected suites** — which integration surfaces this run actually
  exercised;
- the **runtime** — how long the run took;
- the **outcome** — pass, fail, or did not complete; and
- the **last known green SHA** — the most recent commit that has passed.

A result is a statement about its tested SHA and nothing else. An unlabeled
"integration passed" is not a usable signal, because the thing it passed on
cannot be recovered.

## Never Apply A Stale Green To A Newer Candidate

Green at one commit says nothing about a later commit containing changes that
commit did not include. Do not carry a green forward:

- do not report the tip as validated because an ancestor was green;
- do not accept a named checkpoint on a green that predates the commit being
  accepted; and
- do not let a coalesced-away candidate inherit a neighbor's result.

When the latest relevant candidate has no green of its own, the honest state is
**not yet known**, not green. Say which commit is green and which commit is
being asked about.

## Publish Through A Moving Main Without Restarting The Finish Line

**The finished track publishes its own work.** Once a candidate's review and
selected validation pass, the track that produced it, or the one publication owner
it names, publishes it through the supported route. Publication is not a task
queued behind another track's development: a release owner's queue allocates
development effort, not access to main. Serialize only the publication
transaction itself -- Flow's promotion lock or one conditional push -- and never
development, review or a test run. Real dependencies, unresolved conflicts and
missing permissions stay blockers; name the blocker and its owner, not "waiting
for promotion".

A track validates against a measured main revision. When main advances while
that validation or its review runs, the track does not start over:

- A **conflict-free** merge with the newer main may be published without another
  synchronous integration pass or another review. Flow's workspace refresh
  carries a promotion-review pass through such a merge and records where it was
  earned. A clean merge is not proof of semantic compatibility; that residual
  risk is deliberately accepted and the asynchronous run below is what reports
  it.
- A **conflicted** merge is new work. Resolve it deliberately and validate the
  resolution proportionately: the conflicted boundary's tests and its review, not
  a full re-certification of the whole track.
- Publication stays conditional and non-force. A push rejected because main
  moved again means refresh and retry; never overwrite another track's commits.
- Once published, later main movement does not reopen the completed track's
  obligation to chase the newest tip.

Report the published commit as **merged, integration pending**, then passed,
failed, or incomplete as observed. Never report it green because the track's own
pre-merge commit was green.

## Own Every Pending Run Until It Has A Result

The track that published a commit owns its integration run and the result. The
run binds to that exact commit and is coalesced like any other candidate.

- Record the pending run and its outcome on that track's durable ticket surface:
  the control-plane rail evidence or state for an orchestrated lane, the ticket
  itself otherwise. A session-local note is not a durable result.
- A session that ends before its run finishes hands the run back as
  **incomplete** with the exact SHA. Pending is never silently dropped and never
  reported as passed.
- On failure, the publishing track coordinates attribution from the change range
  below. That does not decide which author repairs the root cause, but someone is
  named before the track moves on.

In a Coxswain-managed lane the run is a specialized job, so it survives the
session that published the commit:

- The lane's orchestrator keeps one integration rail: `Role: specialized`,
  operation `coxswain.integration.v1`, `input.projectRevision` the exact published
  commit and `input.testFiles` the selected `tests/test_*.py` modules. It publishes
  or advances that rail in the same pass that reconciles a handoff reporting
  `integration: pending at <sha>`.
- The controller publishes exactly one job per `attempt`, a subscriber runs it,
  and the terminal result wakes that orchestrator. Read it with
  `ai-dev specialized show`.
- Coalesce by moving the rail to the newest published commit, incrementing
  `attempt`, only once the current attempt's job has started or finished. The
  commits in between are superseded, not individually tested.
- Read the result by its class. `succeeded` is a green for that exact commit.
  `failed` with primary `failed` is a product failure: attribute it, open repair or
  a revert, and block dependent rails. `not-attempted` is an environment failure,
  and `timed-out` or `ambiguous` is an incomplete run: repair the environment or
  re-run with a new `attempt`. Until then the commit stays pending, never green.

## Decide What A Red Run Actually Blocks

A red run is a known defect on main. It triggers repair or a supported revert;
it is never dismissed as the accepted risk. Beyond that, blocking anything more
than the following is the cost this policy exists to prevent:

- it **blocks dependent work** whose boundary the failure shows to be
  unreliable;
- it **blocks deployment, release acceptance, and named-checkpoint acceptance**
  of the change it is attributed to until that change is repaired or reverted;
  and
- it **does not stop unrelated development or promotion**, which continue on
  their own local gate.

Name which of these applies. "Integration is red, so everything stops" is a
scheduling failure, not caution. Work whose correctness does not rest on the
unreliable boundary keeps moving and keeps submitting.

## Attribute From The Last Green-To-Red Change Range

Search the **change range between the last known green SHA and the red tested
SHA**, and only that range. In increasing cost:

1. **affected tests** — map the failing behavior to the changes in the range
   that touch its boundary;
2. **focused replay** — re-run the failing case alone against candidates in the
   range; then
3. **bisect** across the range when the first two do not localize it.

Before attributing, confirm the failure is caused by a change in the range: if
it reproduces on the last green SHA, or on an unrelated commit, it is
environmental, flaky, or older than the range, and attributing it to the range
will send the fix to the wrong place. Do not re-run the whole suite on every
commit in the range as a first move.

## Require A Current Green For Deployment And Release Acceptance

Development promotion and checkpoint advancement do not wait on the
asynchronous run. A named checkpoint is judged on the evidence for its own exact
commit; accepting it does not claim that the later merged main commit is green.

Deployment authorization and final release or product acceptance are the
durable claims. Before either, require an integration run that is **green on the
exact commit being deployed or accepted**, or on a descendant-free equivalent
whose change range covers it. This is where waiting on the asynchronous signal is
correct, and those gates keep their own explicit evidence and permission
requirements.

Use `change-validation` to decide which suites that run should select. This
skill owns when the run happens, which commit it binds to, and what its outcome
blocks.

## ChatGPT Interaction

When ChatGPT intentionally activates this shared skill, announce
`Skill: integration-signal`, or include it in a responsibility-ordered composed
chain, and continue without extra gating.

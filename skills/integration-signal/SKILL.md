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

## Decide What A Red Run Actually Blocks

An integration failure blocks exactly three things, and blocking anything more
is the cost this policy exists to prevent:

- it **blocks named-checkpoint acceptance and promotion** of the affected
  lineage;
- it **blocks dependent work** whose boundary the failure shows to be
  unreliable; and
- it **does not stop unrelated development**, which continues on its own local
  gate.

Name which of the three applies. "Integration is red, so everything stops" is a
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

## Require A Current Green Before Accepting A Named Checkpoint

Before a named checkpoint is accepted or a lineage is promoted, require an
integration run that is **green on the latest relevant candidate** — the commit
actually being accepted, or a descendant-free equivalent whose change range
covers it. This is the one place where waiting on the asynchronous signal is
correct, because acceptance is the durable claim.

Use `change-validation` to decide which suites that run should select. This
skill owns when the run happens, which commit it binds to, and what its outcome
blocks.

## ChatGPT Interaction

When ChatGPT intentionally activates this shared skill, announce
`Skill: integration-signal`, or include it in a responsibility-ordered composed
chain, and continue without extra gating.

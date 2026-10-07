---
name: adversarial-guard-verification
description: Verify that a diff's guards are real and tested, by mutating them and requiring the suite to catch each mutation, under a runner contract that cannot fake its own evidence.
---

# Adversarial Guard Verification

Use this when a change adds, removes, relocates, merges or generalises a guard,
and the question is whether the suite actually holds it. A green suite over
unmutated code is evidence that the code passes its tests, not that its guards
are pinned. This skill turns "the tests pass" into "here is the mutation each
guard survives being broken by, and the named test that caught it".

It is local to Dory-wrangler. It is not a shared AI Dev skill.

## The verdicts

Exactly four, and every row gets one:

- **CAUGHT** -- the mutation is applied, the suite fails, and the *same named
  test* fails again on a re-run while the unmutated control passes.
- **GREEN** -- the mutation is applied, is valid, and nothing fails. A green is
  never "fine". Adjudicate it (below).
- **HANG** -- the mutated tree does not terminate. A hang is a behaviour change
  to adjudicate, not a catch.
- **INVALID** -- the mutant did not compile, did not import, or the patch did
  not apply as intended. An invalid row proves nothing, in either direction.

State, with every verdict, **which tests it was run against**. A verdict without
its target set is not a result.

## The runner contract

The apparatus is the part most likely to lie to you. Every one of these rules
exists because a runner without it produced false evidence here.

1. **The pattern applies exactly once.** Count the occurrences before patching
   and refuse the row if the count is not 1. A pattern that matched twice
   mutates something you did not mean; one that matched zero times silently
   tests nothing.
2. **Verify the patched content after writing it.** Read the file back and
   assert both that the old text is gone and that the new text is present.
   *Why:* a runner whose write path truncated the files reported **11 catches
   that were really empty modules**. Compilation alone would not have caught it;
   an empty file compiles.
3. **Compile-gate every mutant.** Parse the patched file before running
   anything. A mutant that does not parse is INVALID.
   *Why:* a preserved mutation that never compiled had been recorded as CAUGHT,
   so a real gap sat behind a fake catch until it was re-run.
4. **A load error is INVALID, never CAUGHT.** An `ImportError`, a collection
   error, or a failure before the first test body is apparatus, not a guard.
   Distinguish it in the runner, not by eye.
5. **A fresh `TMPDIR` per run.** Point every run at its own empty temporary
   directory.
   *Why:* one runner re-used a single `TMPDIR`; a re-run into a directory that
   already held a kept store would have **faked a confirmation on every row**.
6. **Confirm every CAUGHT by re-running its named tests**, and require *that
   same test* to fail under the mutation while the unmutated control passes on
   the same tree. Match the test by identity, not by parsing a line of output.
   Confirm on a **small named subset** -- the one to three tests you claim catch
   the row -- not on every test the mutation broke.
   *Why:* a test-name parse that did not match the runner's output format made
   **every confirmation read as unconfirmed**; and on a host whose sandbox can
   refuse `exec`, a failed process can fake a CAUGHT but never a GREEN, so the
   confirmation is the only thing separating the two. *Why the subset:* six rows
   here broke 141-200 tests each and every one confirmed first time on its named
   subset, while a confirmation run over everything a mutation breaks fails for
   reasons unrelated to the guard and turns a real catch into an unconfirmed row.
7. **Run hang-sensitive rows against the full suite**, not the subset you think
   is relevant.
   *Why:* subset-only runs hid a full-suite hang: a waiter with no deadline in a
   module nobody thought was in scope. "It now fails instead of hanging" is a
   claim about the whole suite and must be measured on the whole suite.

A runner that satisfies 1-7 can still be wrong, so keep a **control row**: an
unmutated run in the same harness, in the same sweep. A sweep whose control is
not GREEN is void.

## Enumerating the mutations

- **Enumerate mechanically from the diff**, not from intuition about what
  matters. Walk the changed hunks and produce a row per mutable element.
- **Element granularity.** One mutation changes one element: one comparison, one
  boundary, one operand, one call, one branch. A row that changes two things
  cannot be adjudicated.
- **Every value in an expression a finding names.** If a finding says
  `depth > 1`, there are rows for `depth`, for `>`, and for `1`.
- **For every guard the diff removes, generalises, relocates, merges or adds**:
  enumerate the inputs the old code accepted and the new one does not, and the
  inputs the new code accepts and the old one did not, and show that each one
  still has a legal exit. A relocated guard is two rows, not one: the place it
  left and the place it arrived.
- **Symmetry: for every channel, path or call site the diff *adds* that carries
  the same kind of data as an existing one, enumerate the rules the existing path
  applies and show the new one applies each, or say why it must not.** Write the
  enumeration out, rule by rule, and answer each; a diff that adds no such path
  answers this in one line.
  *Why:* **mutation cannot reach a guard a new channel was never given.** There
  is nothing there to mutate, so the rows all pass and the sweep says nothing.
  Here, 66 rows across two sweeps returned 61 catches and no unadjudicated greens
  on a revision that shipped a new preservation channel missing both of the rules
  the existing one applied -- losing, on that channel, exactly the payload shape
  the release had just escalated to a human to decide. No future sweep would have
  found it. When the answer is "it must not", the honest ones are usually that
  the new path has no agent to attribute output to, or that a lifecycle
  transition belongs to the caller; say which, and the enumeration is the
  evidence either way.
- **Scope the sweep to what the diff changed.** A sweep costs roughly one full
  suite per row. Mutating untouched code re-measures the last release.

## Adjudicating what is not CAUGHT

Every GREEN and every HANG is resolved to one of these, with its evidence:

- **a real gap** -- pin it with a test that fails under the mutation and passes
  without it, and show both;
- **an equivalence** -- the mutant's behaviour is identical; say why, in terms of
  the code, not of intent;
- **unreachable** -- no input reaches the mutated element; name what blocks it;
- **deliberate behaviour** -- the mutated behaviour is a choice the product has
  recorded; cite where.

Two rules on whose word you take:

- **A reviewer's prescribed fix is a proposal to test,** not an instruction to
  apply. Mutate the prescribed fix like anything else.
- **An author's assessment of its own greens is a claim to verify.** Re-run the
  rows, do not read the adjudication table.

## The decision-conformance pass

Per-guard mutation cannot reach a property that lives in the *composition* of
correct parts. Two defects here were of exactly that kind: a second turn was
queued rather than refused, and an Abandon during a turn recorded a state change
dated after it -- both against confirmed decisions, and both invisible to every
mutation of every guard involved.

So, after the sweep and separately from it: for each confirmed human or
orchestrator decision the change touches, **drive the served application through
that scenario** and record what it did. Through the real served boundary, not an
in-process call, because that is where the decision is promised.

## Reporting

Per row: the identifier, the file and element, the exact patch, the verdict, the
target test set, and for a CAUGHT the confirming test's full name. Per sweep:
the control's result, the counts, every adjudication with its evidence, the
symmetry enumeration, the decision-conformance results, and the apparatus rules
this runner implemented. The symmetry enumeration is reported whether or not it
found anything -- it is the only part of this skill that can speak about a guard
the sweep could not reach.

Publish an interim result before a long run. A sweep here can take hours, and an
unpublished sweep that is interrupted is a sweep that did not happen.

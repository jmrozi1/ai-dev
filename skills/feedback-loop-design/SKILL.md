---
name: feedback-loop-design
description: Design discovery, prototyping, implementation, and validation loops when builds, live environments, screenshots, human relay, or other feedback are materially slow or costly. Choose the cheapest sufficiently faithful loop, increase the evidence payload of expensive passes, size execution rails to the next genuine decision branch, and avoid unnecessary human involvement. Do not use for routine work whose direct feedback loop is already fast.
---

# Feedback Loop Design

Optimize for useful ticket progress across elapsed time, provider credits, human
attention, and rework risk while preserving correctness and safety. Use
proportional judgment rather than a scoring formula or fixed priority between
time and credits. Treat human involvement as a high-cost boundary when the
human is only relaying state or confirming a reasonable default.

## Identify The Current Uncertainty

Before choosing an execution loop, identify what is still unknown:

- product or design intent;
- implementation behavior or system configuration;
- correctness of a bounded change; or
- integrated behavior that only the real environment can establish.

Describe the current feedback loop only far enough to identify its materially
costly boundaries, such as a build, deployment, login, live scenario, screenshot
handoff, provider transition, or human review. Do not optimize a loop that is
already direct and cheap.

## Use The Cheapest Sufficiently Faithful Loop

Resolve each uncertainty at the least expensive level that can answer it
reliably:

- use text layouts, examples, or other low-cost representations to refine
  product and interface structure before implementation;
- use repository inspection, focused diagnostics, fixtures, and non-live tests
  to discover implementation behavior;
- use builds, live environments, rendered interfaces, and end-to-end tests for
  evidence that cheaper loops cannot establish.

Move to a higher-cost loop only for the fidelity it uniquely provides. Do not
treat a cheap representation as final integrated proof, and do not use an
integrated environment to answer questions that a focused lower-cost loop can
answer objectively.

## Increase Payload Before Paying The Outer-Loop Cost

Before entering a materially expensive loop:

- enumerate the currently knowable questions and assertions it should answer;
- batch compatible independent observations when doing so preserves isolation;
- prepare the focused instrumentation and result shape needed to report those
  observations;
- reuse an expensive build, runtime, authenticated session, or rendered state
  only when ownership, isolation, restoration, and cleanup remain valid;
- continue independent cases after one case fails, while blocking only work
  whose prerequisite was invalidated; and
- return one concise evidence package covering the bounded pass.

Maximize useful evidence, not raw output. Do not add broad state dumps,
speculative instrumentation, a generic scenario language, or unnecessary
abstractions merely to make one pass larger.

An unexpected result is not a reason for an automatic retry. Diagnose what the
result changed, revise the hypothesis or implementation, and perform another
expensive pass only when it can establish newly useful evidence.

## Prove The Apparatus Before Paying The Cost

A prepared pass can fail without producing any evidence when the harness,
driver, or command it depends on was never checked against the place it will
actually run. Before the first materially expensive boundary — a download or
install, a build or deployment, an authentication step, a provider call, a live
environment, a rendered or relayed observation, or a human handoff — establish
that the apparatus can reach that boundary at all:

- prove the target runtime, interpreter, and tool versions the apparatus
  requires, including the interface features it actually uses;
- validate the exact options, arguments, and input shapes the target will
  accept, from authoritative interface documentation or a bounded direct probe;
- compile, parse, or otherwise construct the driver through the last
  deterministic boundary before the expensive action, including the state and
  ordering preconditions that action requires;
- when host and target differ materially, run the cheapest faithful self-test on
  the target itself rather than trusting local behavior.

Non-exhaustive help output, a local host's version of a tool, and a neighboring
interface's behavior are not capability evidence. Absence from an abbreviated
listing does not establish that an option is unsupported, and working locally
does not establish that it works on the target.

Dry construction covers ordering as well as syntax. Build the request, command,
or payload in the exact state the real call will see, so a precondition that can
only hold earlier in the sequence fails deterministically and cheaply instead of
after the cost is paid.

Preflight iteration is cheap and repairable. Correct a failed compatibility
check, an invalid option, or a broken dry construction and continue; that is
what the preflight is for. Once the expensive attempt begins, that latitude ends
and the no-automatic-retry rule above governs. Keep the boundary honest in both
directions: an authoring defect corrected before a declared pass is not a
failure of that pass, and a declared validation or expensive attempt does not
become repeatable by describing its failure as a typo.

The same discipline governs what a pass declares it will affect. When a negative
control, mutation, or other deliberate-breakage pass states an exact set of tests
it expects to fail, derive that set two ways and take both: from the prohibited
behavioral invariant, and from searching the tests themselves for the exact text
or construct the mutation will change, together with the assertions structurally
coupled to it — those that index on, split around, or compare against that same
text. Recalling which test names sound related to the behavior is not an
enumeration method, because a test can assert the mutated construct without
naming the idea at all.

A failure caused directly by such a coupled assertion is inside the pass rather
than outside it, but it still proves the declaration incomplete. Disclose the
omission plainly instead of relabelling the extra failure unrelated or widening
the original claim after seeing the result.

Keep this proportional. A pass that claims no exact set owes no enumeration, and
nothing here asks for a dependency analyzer, a mutation harness, a registry of
controls, or any scoring of declarations.

## Size The Execution Rail To The Next Branch Point

Build a rail only across work whose route is currently knowable.

Use a short rail when each result may materially determine the next action, such
as discovering a framework configuration before test design can begin. Stop at
the next genuine product, scope, architecture, permission, or evidence-strategy
branch rather than predicting work beyond it.

Use a longer rail when the foundation is stable and the remaining work contains
independent or predictably ordered units, such as running many explicit test
cases through one established harness. The executor should complete all useful
work inside that boundary and report once rather than stopping after each fact.

Do not create a dependency graph, planning framework, or task-history system to
represent the rail. Use the project's existing ticket checkpoints and current
tasking state.

## Spend Human Attention Deliberately

Do not ask the user to confirm reasonable defaults already supported by current
requirements, accepted decisions, and project conventions.

Let the executor gather evidence and resolve bounded implementation details.
Let the orchestrator judge that evidence and make decisions within established
intent. Escalate to the user only when their product preference, permission, or
a material unresolved product, scope, or architecture decision can change the
correct route.

When human judgment is required, consolidate the smallest decision-complete
evidence surface instead of serializing several confirmations. Never avoid a
necessary human decision merely to reduce interaction count.

## Separate Discovery From Integrated Verification

During discovery or prototyping, favor the fastest faithful loop and postpone
expensive integrated confirmation until the bounded direction is stable. During
verification, use the real outer loop to prove the production or user-visible
behavior that cheaper evidence cannot prove.

Record the evidence obtained, remaining uncertainty, and any decision that
changes the next rail. Do not preserve a transcript or build a performance
history.

## ChatGPT Interaction

When ChatGPT intentionally activates this shared skill, announce
`Skill: feedback-loop-design`, or include it in a responsibility-ordered
composed chain, and continue without extra gating. Re-announce the active skill
or chain only when the invocation or composition changes materially.

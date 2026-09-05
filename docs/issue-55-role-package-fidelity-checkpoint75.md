# Issue #55 — checkpoint 75: role and runtime package, bound

An independent review of the published-but-unaccepted unit 72→73→74 returned
**`product: fail`**. This checkpoint clears it. It is remediation inside accepted
requirements — no new capability, no redesign, and no deferred capability touched.

**No real provider session was run.** The capability evidence from checkpoints 73 and 74
stands; this is a correctness-and-record checkpoint, and none of the four findings needed a
provider to clear. The whole of the material finding lives in code that runs before any
process is spawned, and its kill power is provable from a fixture. Paying for a real run to
watch a refusal happen earlier would have bought a screenshot, not a fact.

---

## What was built

| File | What changed |
|---|---|
| `ai_dev_flow/claude_runtime.py` | **the gate.** `REASON_PLUGIN_ROLE_MISMATCH`; `validate_plugin_surface` takes a required `role`; `_build_request` supplies it from `record.role` |
| `ai_dev_flow/role_dispatch.py` | `_require_role_package` — the same refusal, said at the command line, in the same reason |
| `ai_dev_flow/role_invocation.py` | the module's "role fidelity is structural in four places" comment corrected, and what item 5 does *not* cover stated |
| `ai_dev_flow/role_driver.py` | `RoleLaunch`'s false claim about `validate_plugin_surface` corrected |
| `ai_dev_flow/manager_controller.py` | `dispatch_role`'s stale docstring — `_require_sequential` was deleted at checkpoint 74 |
| `tests/test_claude_runtime.py` | `RolePackageBindingTests`, 6 tests; 19 existing call sites given the required `role` |
| `tests/test_role_invocation.py` | `RolePackageFidelityTests`, 6 tests |
| `tests/test_role_driver.py` | the gate-integrity oracle pinned to the accepted baseline `c0b6a3a`, and its silent-skip removed |
| `tests/test_session_lifecycle.py` | `_package_for(role)` — the fixture states each role's own package rather than the gate being relaxed for it |
| `docs/issue-55-continuation-brief-checkpoint72.md` | checkpoint 72 recorded, its residuals reconstructed from durable evidence |
| `docs/issue-55-role-launch-checkpoint73.md`, `…-checkpoint74.md` | correction notes **appended**; their published text is untouched |

---

## (a) MATERIAL — role and runtime package are now unable to disagree silently

### What was wrong

`--role`, `--prompt-file`, `--plugin-root` and `--expected-skill` were four independent
operator inputs with no cross-check. `claude_runtime._build_request` set `role=record.role`
on the `RuntimeRequest` and took the package inputs from its caller, comparing nothing.
`validate_plugin_surface` was never given `role`: it validated the plugin against the skill
name it was handed.

The reviewer's demonstration: `--role executor` on an `executor` rail, with the **reviewer**
plugin and `--expected-skill reviewer`, passed all four claimed structural role-fidelity
checks, wrote `executor` into the durable binding, and ran the reviewer package. Nothing
failed closed. All four published checks compare a role to another statement of the *same*
role; none of them looked at the package.

### The choice: refuse, not derive — and why

The finding allowed either refusing fail-closed or deriving the package from the role.
**Refusing is smaller and is the shape every neighbouring gate already has.** Deriving would
mean replacing four stated flags with a package root plus a layout convention, which changes
the command-line contract of `role_dispatch` *and* `role_driver_dispatch`, abandons the
deliberate reuse of `manager_dispatch`'s flag names, invents a directory layout nobody has
accepted, and would leave the orchestrator path spelling the same thing a second way. The
accepted design's whole idiom is "two independent statements of the same fact must agree at
a point where the product compares them and fails closed" — the rail role in snapshot vs
observation, the workspace on the record vs the one passed in, the head on the packet vs the
snapshot. This is one more of those.

### Where it lives

```
launch_request / create_conversation_request / resume_request
    -> _build_request(record, ...)
         -> validate_plugin_surface(resolved_plugin,
                                    expected_skill=expected_skill,
                                    role=record.role)   <- refuses plugin-role-mismatch
```

`validate_plugin_surface` already established the single skill the package exposes, by
listing the directory. The new refusal compares **that** — `skills[0]`, read off the
filesystem — against the role, so it is a statement about the package's content rather than
about the command line.

**It is a gate, so:**

- `role` is **required and has no default**. A defaulted parameter is skippable by anything
  that forgets it, which is the same hole.
- `_build_request` supplies it from `record.role` — the role `reserve_binding` wrote into the
  durable binding from the `Assignment` the accepted `authorize` decision was granted for.
  `_build_request` has **no `role` parameter**, so the role side is not answerable by the
  caller that chose the package.
- It is on the one path every runtime request in the package is built through, so no builder
  can reach the SDK around it. Asserted structurally: the module contains exactly one call to
  the validator.
- Nothing was added to the injection surface. No new parameter, flag, hook or override.

`role_dispatch._require_role_package` refuses the same disagreement at parse time, in the
same reason, before a control plane is read or a binding reserved. **It is not the gate** and
does not replace it; it is the same shape as `_stated_role`, which refuses `orchestrator` at
the command line while `_require_launchable_role` refuses it again at the door. Because
`role_driver_dispatch` parses each launch group through `stated_role_inputs` itself, the
concurrent driver inherits it without a second spelling.

### What this does NOT bind, stated plainly

**The system prompt file.** It is a controller-owned free-text file with no structural
declaration of the role it was written for, so `--role executor --prompt-file
<a reviewer's prompt>` is still accepted. Binding it would mean imposing a filename
convention on operator-chosen paths, which is a convention I would be inventing rather than
enforcing — the exact substitution for a structural fact this checkpoint exists to remove.

What *is* bound beside the package: the **directive** — the first user message — is
`DIRECTIVES[role]`, a per-role constant an operator cannot state at all. So the role's
instruction and the role's skill are both structural; the role's system prompt is not. This
is the honest edge and it is recorded in `role_invocation`'s own comments, not only here.

---

## (b) MATERIAL — the gate-integrity oracle can now fail

`tests/test_role_driver.py` compared the working tree against `git show HEAD:<file>`. HEAD is
the commit under review, so it passed for any committed change and detected only uncommitted
edits — and checkpoint 74's document offered it as the reason a reviewer need not take gate
integrity on trust.

It is now pinned to the **accepted baseline `c0b6a3a`** (checkpoint 71) and to the literal
blob names that baseline carries, and it asserts three separable things:

1. the file is still **at its accepted path** (a move fails here);
2. its bytes hash to the recorded literal (`manager_dispatch.py` =
   `1501bf2dbd0a8e680e56f452fb5239e09d9ec75a`, `orchestrator_invocation.py` =
   `e63a79586eb56a6610adc8657810a2a34c775750`);
3. the accepted baseline really carries that blob at that path, so the literal is anchored to
   accepted state rather than to a number typed into a test.

It also **no longer skips** when an object fails to resolve. It skips only when there is no
git checkout at all; past that, an unresolvable object is the finding, not a reason to stand
down. That change was made because the first attempt at the move control skipped instead of
failing — the mutation found a second, quieter version of the same defect.

The substance was always true and the test still passes: both files are byte-identical to
their `c0b6a3a` blobs at HEAD and in the working tree.

---

## (c) MATERIAL — checkpoint 72 is now on the record

`docs/issue-55-continuation-brief-checkpoint72.md`. Checkpoint 72's commit message is the
single token `72` and it had no document; the session that built it was lost in a VS Code
crash. What it built and what its evidence is are read off the commit, the shipped code and
the shipped tests. **Its residuals are marked `RECONSTRUCTED` throughout**, with the original
statement recorded as lost.

Two residuals are grounded in files, and they are the reviewer's own reading — **confirmed,
not copied**:

- **R1** — only the *reported* unreachable case is caught. Grounded in
  `session_lifecycle.py:2956-2985` ("A replacement that silently ignores its brief is not
  caught here, and no state below claims it is") and in
  `test_case_h_the_marker_is_a_failure_report_and_cannot_manufacture_a_green`, which asserts
  that a replacement saying nothing at all is green. This correctly matches the checkpoint-71
  condition, whose observed replacement *did* say so.
- **R2** — `continue_from_durable_state` still has zero production callers. Confirmed by
  search: outside `tests/`, it appears at its definition, in `manager_controller`'s import,
  and in a pass-through nothing calls. The new check has never met a real provider, and its
  false-positive path — the echo, which the suite drives deliberately — is real and
  unmeasured outside the fixture.

**The count "two" comes from checkpoint 73's prose** ("neither checkpoint-72 residual
closed") and from nowhere else. Whether these are the two the lost session wrote down cannot
be established. No third residual is invented to fill a remembered shape.

Corroborating evidence recorded there: `tests/test_session_lifecycle.py` went 301 → 308 test
methods between `c0b6a3a` and `381689d`, and the seven are the case-H block.

---

## (d) ROUTINE — the stale docstring

`ManagerController.dispatch_role` claimed it "refuses to start anything while this controller
already holds a session". `_require_sequential` was deleted at checkpoint 74. The sentence is
removed and the deletion named, so a reader of a checkpoint-73 transcript finds out what
happened to it rather than hunting for a refusal that is gone.

---

## The established facts: which moved and which did not

| Established at 73/74 | Moved? |
|---|---|
| `manager_dispatch.py` and `orchestrator_invocation.py` byte-unchanged; the orchestrator wake gate whole; the `orchestrator` role refused | **Left exactly alone.** Both files still hash to their `c0b6a3a` blobs, now asserted against that baseline rather than against HEAD. No `propose_wake` call was added; `orchestrator` is still refused at the command line and at the door. |
| "Role fidelity is structural in four places and conventional in none" | **Moved, because it was wrong.** It is five places, and the fifth is the one that binds the role to the package. The four were real and each did what it said; the claim about what they *covered* was false when published. Corrected in `role_invocation`, and appended as a correction note to checkpoint 73's document rather than edited into its body. |
| `RoleLaunch`: a shared runtime policy across roles "is the exact failure `validate_plugin_surface` exists to catch" | **Moved, because it was false.** That function was never given the role. It is now, and the docstring says what actually catches it. |
| `ManagerController.launch`, `replace_old_context`, `continue_from_durable_state`, `release_continued_context` have zero production callers | **Left exactly alone.** None was added. R2 above depends on this still being true. |
| `_require_sequential` deleted; concurrency bounded by D6 alone | **Left exactly alone.** No refusal came back, no scheduler, queue, priority model, fairness policy or autoscaler was added. |
| The `SKILLS=` half of the checkpoint 73/74 transcripts shows "which role package the session is actually running" | **Reinterpreted, not moved.** The observation is real; what it proved was weaker than stated, because the package agreed with the role only by operator convention. It is now a structural fact. |
| Peak real occupancy 3 of 6 (cp74), 1 of 6 (cp73) | **Unmoved. No provider session was run at all this checkpoint.** |

---

## Discriminating fixture

Per `feedback-loop-design`: partitions enumerated and oracles named **in advance** — the
declaration is the `Named oracle(s)` column below, fixed in a JSON file outside the
repository before any mutation ran and driven from it rather than read back afterwards —
each mutation applied **alone** to the shipped implementation, each restored and the restore
proved by `sha256`, and both directions covered.

Baseline: **372 tests OK** (`tests.test_role_driver` 28 + `tests.test_role_invocation` 36 +
`tests.test_session_lifecycle` 308), plus `tests.test_claude_runtime` **65 OK**.

| # | Mutation | Partition it attacks | Named oracle(s) | Result |
|---|---|---|---|---|
| M1 | the `skills[0] != role` refusal never fires | role→package binding, **refusing** direction | `RolePackageBindingTests.test_the_package_of_another_role_is_refused_for_this_binding`; `…test_the_executor_package_is_refused_for_a_reviewer_binding`; `…test_the_gate_reaches_every_request_this_boundary_builds`; `RolePackageFidelityTests.test_an_executor_launch_handed_the_reviewer_package_is_refused`; `…test_a_reviewer_launch_handed_the_executor_package_is_refused` | **DIED** — all 5 |
| M2 | the refusal always fires | role→package binding, **admitting** direction | `RolePackageBindingTests.test_the_same_package_is_admitted_for_the_role_it_belongs_to`; `RolePackageFidelityTests.test_the_same_launch_with_its_own_package_is_admitted` | **DIED** — both |
| M3 | `_build_request` passes `role=expected_skill` instead of `role=record.role` | the role side comes off the **durable record**, not a caller argument | `RolePackageBindingTests.test_the_role_comes_off_the_record_and_not_off_an_argument`; the four refusal oracles of M1; `RolePackageFidelityTests.test_the_gate_reads_the_role_off_the_durable_binding_not_an_argument` | **DIED** — all 7 |
| M4 | `_require_role_package` never called | the command-line early report | `RolePackageFidelityTests.test_the_command_line_says_the_same_no_in_the_same_reason`; `…test_the_whole_entry_point_exits_non_zero_on_the_mismatch` | **DIED** — both |
| M5 | a **committed** change to `manager_dispatch.py` (disposable clone) | gate integrity, content | `StructuralTests.test_the_orchestrator_entry_points_are_byte_unchanged`, at `assertEqual(observed, blob, name)` | **DIED**; the checkpoint-74 oracle **PASSED** on the same tree |
| M6 | `manager_dispatch.py` **moved** to `manager_gateway.py`, imports updated, committed (disposable clone) | gate integrity, path | same test, at `assertTrue(path.is_file(), …)` | **DIED**; the checkpoint-74 oracle **SKIPPED** on the same tree |

**The kill path in both directions for the new gate** is M1 (it must refuse a mismatch) and
M2 (it must not refuse a match). Neither alone is sufficient: M1 alone is satisfied by a
route that refuses everything, and M2 alone by a route that refuses nothing.

**Restoration.** M1–M4 were applied to the canonical worktree one at a time. Each file's
`sha256` was taken before the mutation and again after the restore, and each pair is
identical:

- `ai_dev_flow/claude_runtime.py` — `418ddb5821a1b12a57f9baacb1cb04c9359a4301e329f31183bb4e2be642d3db`, before and after M1, M2 and M3
- `ai_dev_flow/role_dispatch.py` — `7884309f84b3c8a30c13db2387c4fd24ba92db88321cc47e8daf994c2a6bfda8`, before and after M4

M5 and M6 were run in a **disposable git clone** outside the repository and never touched the
canonical worktree — `manager_dispatch.py` and `orchestrator_invocation.py` were required to
stay byte-identical to `c0b6a3a`, and they are (`git hash-object` confirms both blobs at the
end of this checkpoint). The clone was deleted.

### The declaration was incomplete twice, and both were my tests' fault

Disclosed rather than relabelled, per `feedback-loop-design`. On the first pass:

- **M3** left `test_the_role_comes_off_the_record_and_not_off_an_argument` and
  `test_the_gate_reads_the_role_off_the_durable_binding_not_an_argument` **passing**. Both
  asserted `assertIn("role=record.role", source)` — and `role=record.role` also appears in
  the `RuntimeRequest` construction a few lines below, so the substring survived the
  mutation. The assertion is now on the whole call
  (`"expected_skill=expected_skill, role=record.role"`).
- **M4** left `test_the_whole_entry_point_exits_non_zero_on_the_mismatch` **passing**,
  because it asserted only `code == 1` and `main` reports several unrelated refusals as
  exit 1. It now captures stderr and asserts the reason.

Both were authoring defects in the new oracles, corrected before the declared pass, and the
table above is the re-run. The mutation campaign found them, which is what it is for; the
oracles as first written would have reported kill power they did not have.

### What is *not* claimed

The corrected prose — `role_driver`'s docstring, `manager_controller`'s docstring,
`role_invocation`'s module comment, and the two appended correction notes — has **no
mutation control and no oracle**. It is prose. A test that could be failed by explaining
itself would push the reasoning out of the code, which is checkpoint 74's own methodological
note and it applies here.

---

## Validation

- `python3 -m unittest tests.test_role_driver tests.test_role_invocation tests.test_session_lifecycle`
  — **372 tests, OK** (up from 366: +6 in `test_role_invocation`).
- `python3 -m unittest tests.test_claude_runtime` — **65 tests, OK** (up from 59: +6).
- `python3 -m unittest tests.test_decision_queue tests.test_claude_worker
  tests.test_orchestrator_invocation tests.test_claude_allowance_ledger
  tests.test_manager_controller tests.test_manager_dispatch tests.test_context_lifecycle`
  — **433 tests, OK (3 skipped)**. Run because the gate is in `claude_runtime`, which those
  modules reach; the three skips are pre-existing.
- Six mutations, each applied alone with its oracle named in advance, each killed by that
  oracle; four restored byte-exact in the canonical tree, two run in a disposable clone.
- `git hash-object ai_dev_flow/manager_dispatch.py ai_dev_flow/orchestrator_invocation.py`
  → `1501bf2d…` and `e63a7958…`, identical to `c0b6a3a`.
- No file under `skills/**` was touched.
- **No provider session was run and no provider budget was spent.**

---

## The trade this checkpoint most likely loses on

**The role↔package binding rests on a naming equality — the package's single skill directory
must be named for the role — and that is a convention promoted to a rule rather than an
independent declaration.** The plugin does not *say* which role it is for; it is inferred
from the one name it already had to have. A package could be renamed and the gate would then
refuse a launch that is substantively correct, or two roles could one day want to share a
skill name and the gate would admit a wrong pairing.

The alternative was a role field in the plugin manifest — a genuinely independent
declaration, checked against `record.role`. I did not take it because it changes an accepted
file format (`ALLOWED_MANIFEST_KEYS` is deliberately four keys, and every one of them is
descriptive), it would invalidate every existing controller-owned plugin until rewritten, and
it puts the authority for "what role is this" inside a file the same operator writes — which
is a second copy of the same input rather than an independent source. Comparing against the
skill the provider will actually load has the advantage that it is the thing that matters.

That is a real reason and it is not a defence. A reviewer may reasonably judge that a gate
resting on a filename is weaker than a gate resting on a declaration, and that the system
prompt file being unbound (above) leaves half the role package unchecked. Both judgments
belong to the review rather than to me, and both are stated here so they are decided rather
than discovered.

**A second, smaller trade.** This checkpoint spent no provider credits and ran no session, so
the new refusal has never been driven against a real provider launch. Checkpoints 73 and 74
each drove their refusals on the real path; this one drove its refusal on a fixture whose
lifecycle, store, predicate and request boundary are real but whose worker is injected. I
judged that a real run could only have shown a refusal happening before the process starts —
which the fixture shows exactly, and cheaper. A reviewer may want it driven for real anyway.

---

## Deliberately not done

No human-response routing; no autonomous continuation loop; no work discovery; no scheduler,
queue, priority model, fairness policy or autoscaling; no wake kind for "an executor rail is
ready"; no manager surface on either role entry point; no new injection point, parameter,
flag or hook; no change to `manager_dispatch`, `orchestrator_invocation`, `authorization`,
`session_binding` or `claude_worker`; no change to `skills/**`; no real provider session and
no provider budget spent; **neither checkpoint-72 residual closed** — both stand exactly as
they stood at `381689d`, and are now written down; checkpoint 73's and checkpoint 74's
published text not edited, only appended to; no follow-on ticket filed; no checkpoint
accepted; product `main` unmoved.

---

## Correction, appended at checkpoint 76 — two claims above were overstated

Checkpoint 75's narrative above is left as it was published. These two sentences in it
were wrong when they were written, and are corrected here rather than edited there.

**1. "Asserted structurally: the module contains exactly one call to the validator."**
(under *It is a gate, so*, third bullet). **False as published.** The assertion was not
structural. It was

```python
module = inspect.getsource(claude_runtime)
self.assertEqual(module.count("validate_plugin_surface(
"), 1)
```

— a count of the *substring* `validate_plugin_surface(` immediately followed by a line
break. It therefore counted calls whose open paren happened to sit at the end of a line.
A genuine second call to the validator, written on one line, satisfies it. Review
demonstrated exactly that: a second call was added, the count stayed at 1, and **no
shipped test failed**. The single-chokepoint property this boundary is built on was, in
practice, guarded by a formatting convention.

**What it is now.** `tests/test_claude_runtime.py` parses every module in `ai_dev_flow/`
and counts `ast.Call` nodes by resolved callee name, via `tests/source_oracles.py`. It
asserts both halves of the property, and the same site for each:

- exactly one call to `validate_plugin_surface`, in `claude_runtime._build_request`;
- exactly one construction of `RuntimeRequest`, in `claude_runtime._build_request`.

The second half was never asserted at all before. One validator call says the gate is not
bypassed by a laxer second call; one request construction says no builder can assemble a
request *beside* the gate. Neither implies the other. The scan is over the whole package,
not one module, because a second call anywhere in `ai_dev_flow` defeats the property.
Comments, whitespace and line breaks are not in the parse tree, so the oracle cannot be
satisfied or broken by formatting. Three sibling oracles that had the mirror-image defect
— `controller.agent_count(`, `drive_roles(` and `dispatch_role(` substring counts, each
breakable by a *comment* that merely mentioned the call — were converted with it.

**2. "so it is a statement about the package's content rather than about the command
line"** (under *Where it lives*), and the same claim in the `validate_plugin_surface`
comment. **An overclaim.** The check immediately above raises unless
`skills == [expected_skill]`, so at the role comparison `skills[0] == expected_skill`
already holds and `skills[0] != role` and `expected_skill != role` are provably the same
predicate. Choosing `skills[0]` cannot be what turns the comparison into a statement about
the package, because the alternative would have said precisely the same thing.

What actually makes it a statement about the binding rather than about the command line is
the *other* operand: `role` comes from `record.role`, which `_build_request` supplies from
the durable binding and refuses to take as a parameter. That part of checkpoint 75's
argument stands unchanged and is what the gate rests on. `skills[0]` is kept for a
narrower and honest reason, now written in the code: it is the value read off the
filesystem, so the comparison and the refusal message quote one source, and the line stays
correct if the equality check above is ever loosened.

**Neither correction changes behaviour.** `ai_dev_flow/claude_runtime.py` was edited only
inside a comment; its parse tree at checkpoint 76 is identical to its parse tree at
`62e3d68`. The gate's behaviour, signature and reason semantics are untouched, and the
refusal reason `plugin-role-mismatch` is now pinned as a literal by a test, which nothing
did before.

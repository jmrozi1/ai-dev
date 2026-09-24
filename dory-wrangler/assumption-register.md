# The v0.1 portability assumption register

#89 checkpoint `record-portability-assumption-register`. This register lists every
portability assumption that external validation cannot prove. Each entry is
written so that internal dogfood (#90) can mark it **held** or **failed**, with
evidence, and without needing any conversation. #89 calls it "the real
deliverable alongside the tests".

This register records assumptions and fixes nothing. #89's acceptance criteria
forbid any speculative compatibility fix before internal evidence exists. So an
entry says what to run and what each answer looks like. It never says what to
change in advance.

## How to read an entry

Every entry has the same fields:

- **Claim** -- one sentence, stated so that it can be true or false.
- **Status** -- exactly one of:
  - `held (internal evidence)` -- observed on the internal network, with its source cited;
  - `held (external evidence only)` -- proven here, and only for a claim about the product itself, never about the internal environment;
  - `unproven` -- nobody has established it, and nothing may assume it;
  - `known limitation` -- a limit that is recorded and not closed.
- **Evidence** -- the source for any held entry: a GitHub comment id on `jmrozi1/ai-dev`, a control-plane state section (`ai-dev-control-plane`, project `dory-wrangler`), or a commit.
- **Why external validation cannot settle it**.
- **How internal dogfood settles it** -- the command or procedure to run, and what "held" and "failed" each look like. For a held entry, this is how to re-confirm it on the release tree, because the internal evidence was gathered on an earlier tree or on the proof-of-concept script.
- **Depends on it** -- what in the product rests on the claim, and whether a failure is a v0.1 compatibility gap for #90 or later work.
- **Owner** -- #90 for anything internal dogfood settles. #89 only for a known limitation of external validation itself.

Entries marked **[human-required]** were required by the human on #89. The
direction came in comment 5648269596 (2026-09-12) and three comments on
2026-09-14: 5668368963, 5668684450 and 5669592360. Each such entry is first in
its group.

Commands are written with `python3`, which is Python 3.9.25 on the internal
Rocky Linux 9 host (RT-1). On the external development desktop, `python3` is
3.6; use `python3.11` there. Run commands from the repository root with a fresh
`TMPDIR`. The tests put their stores under `tempfile.gettempdir()`, so `TMPDIR`
also chooses which filesystem the stores live on.

**The means of settling entries.** These tools already exist. This register
adds none.

- **The test categories.** `python3 dory-wrangler/tests/run_tests.py --category portable|launch-boundary|development-environment [--json PATH]`. `--json` takes a path; given alone it is a usage error. The categories and their dependency groups are declared in `tests/categories.py`. The README section "Test categories, and telling an environmental failure from a product one" says how to read a failure in each. `run_tests.py` deletes its kept-store directory before it starts and checks only the stores its own tests write there, so it cannot check a dogfood store (LO-3).
- **The end-to-end path.** `python3 dory-wrangler/tests/e2e_loop.py --launcher <id> [--launcher-options <json>] [--work <dir>] [--request-timeout <seconds>]` runs create, send, launch, events, render, restart and reopen, a third turn, `validate_store.py` and `diagnostics.py`. It prints PASS or FAIL per step (README "The whole loop, end to end"). `--launcher` takes any launcher registered in `launchers/registry.py`, so it works for #90's internal launcher once that launcher is registered (TA-6).
  - Each request waits at most `--request-timeout` seconds, **120 by default**, and a send waits for the whole turn. Against a real launcher, pass a value well above the longest real turn recorded (TR-20), for example `--request-timeout 1800`. A slower turn otherwise fails its step with `the shell gave no answer to POST /api/chats/<chat>/messages within 120s`. That line says nothing about the entry being settled: it is neither held nor failed, so re-run with a larger value.
  - A PASS line's words matter as well as the PASS. `third-turn` passes both when the agent is re-attached and when the path takes Abandon; TR-9 quotes the two lines.
- **The store checks.** `python3 dory-wrangler/validate_store.py STORE [--snapshot FILE]` checks a store against the contract, and `--snapshot` also writes the store's export as one JSON document. `python3 dory-wrangler/diagnostics.py STORE CHAT_ID [--records events|lifecycle|messages] [...]` prints the preserved raw evidence, verbatim and bounded (decision 0007). It answers "which event types occurred, what rendered, where the stream stopped" from durable evidence alone. `STORE` and `CHAT_ID` are always required; a chat's id is its directory name under `STORE/chats/`. Where an entry below says `diagnostics.py --records lifecycle`, it means `python3 dory-wrangler/diagnostics.py STORE CHAT_ID --records lifecycle`, once for each chat concerned.
- **The whole-store re-classification.** Take a snapshot with `validate_store.py STORE --snapshot DIR/dogfood.json`, then run `python3 dory-wrangler/tests/reclassify_stores.py DIR` over that snapshot. This is how a dogfood store gets the check that phase 3 of `run_tests.py` gives the suite's own stores (LO-3).

Every command in this register was run once on the external desktop, from a clean archive of the tree it describes, with `python3.11` in place of `python3`. A command that needs a store was run against a store written by `e2e_loop.py`. Some commands could not run there:
- the internal tools (`codex`, `launch_agent.sh`, a registered internal launcher id) do not exist on the desktop;
- `ausearch` needs root; its arguments were checked against `ausearch --help`;
- this host's policy refuses to execute `rpm`.

The log is in the `issue-89-register-procedures-fix` handoff.

**How #90 records a result.** #90's acceptance criteria require every entry to be
recorded internally as held, failed or untested, with evidence. For each entry
id, record:

- that outcome;
- the release commit it was run against;
- the command or procedure;
- its output, captured and redacted, or a pointer to where it is kept durably.

A failed entry is a successful result for #90 (#90 "Full Description").

## Group 1. Runtime and platform

### RT-1 [human-required] Python 3.9.25 on internal Rocky Linux 9 runs #86's accepted tree unchanged

- **Claim.** The internal Rocky Linux 9 host has a usable Python 3.9 (3.9.25), and it runs #86's accepted tree unchanged.
- **Status.** `held (internal evidence)`.
- **Evidence.** Reported by the human from internal dogfood on 2026-09-14:
  - #86's work at `f519eec` ran on Rocky Linux 9 with Python 3.9.25;
  - 225 tests passed, which is #86's accepted count;
  - the contract validator reported 59 fixtures, 19 accepted and 40 rejected;
  - the UI worked well.

  Sources: #89 comment 5668368963, #86 comment 5668368596 and #90 comment 5668369261; control-plane `issue-85/state.md` "Internal Compatibility Findings". #86 comment 5668368596 states that this settles the runtime-availability claim of `decisions/0001-runtime-and-storage.md` (risk R1) for the internal environment.
- **Scope of what is held.** It covers #86's tree only, at `f519eec`. It does not cover the converged release tree (RT-2).
- **Why external validation cannot settle it.** The external VM that had the same platform package (RHEL 9.8, `python3-3.9.25`) was retired on 2026-09-15. The desktop has no Python 3.9 (control-plane `issue-89/state.md`, "The Development Environment, As It Now Is").
- **How internal dogfood settles it.** Re-confirm with `python3 --version` and `rpm -q python3` on the internal host.
  - **Held:** 3.9.x from the platform RPM.
  - **Failed:** absent, or older than 3.9. `decisions/0001` R1 says the fallbacks for an older interpreter are not written.
- **Depends on it.** Everything: the product, the validator and the tests are Python 3.9 standard library only (decision 0001). A failure would be a v0.1 gap.
- **Owner.** #90.

### RT-2 The release tree executes under the internal Python 3.9

- **Claim.** The whole suite passes under the internal Python 3.9 from a clean checkout of the release commit: 699 tests, all phases, exit 0.
- **Status.** `unproven`.
- **Why external validation cannot settle it.**
  - The desktop has Python 3.6 (unusable), 3.11 and 3.12, and no 3.9. Installing one needs a system change the orchestrator does not make on its own (`issue-89/state.md`).
  - External evidence is execution under 3.11 and 3.12, plus every changed `.py` parsed with `ast.parse(..., feature_version=(3, 9))`. Parsing proves syntax only. It does not prove standard-library behaviour or that APIs exist.
  - The only internal execution so far is #86 alone (RT-1). #87's launcher suite and #88's converged product have not been run internally (#86 comment 5668368596).
- **How internal dogfood settles it.** From a clean `git archive` of the release commit, run `python3 dory-wrangler/tests/run_tests.py --json run.json`.
  - **Held:** exit 0, and `Ran 699 tests ... OK` (the count at `48f1412`). Portable 439, launch-boundary 165, development-environment 95, each with 0 failed. Every kept store accepted. Phase 3 has 0 disagreements. Fixtures are `59 fixture(s): 19 accepted as valid, 40 rejected for the stated reason, 0 mismatched`.
  - **Failed:** any `SyntaxError`, `AttributeError`, `ImportError` or `TypeError` from the standard library, or any portable or launch-boundary failure that the named re-run reproduces.
  - Read a development-environment failure first against the entries in groups 2 and 3.
- **Depends on it.** Everything. A failure in portable or launch-boundary is a v0.1 gap (#90).
- **Owner.** #90.

### RT-3 The real internal launcher needs nothing beyond Python 3.9's standard library and the internal `codex` CLI

- **Claim.** The internal launcher can be written in Python 3.9 standard library only, invoking the internal `codex` executable (or `launch_agent.sh`). It needs no third-party package on the internal host.
- **Status.** `unproven`.
- **Why external validation cannot settle it.** No real internal launcher exists (`contract/v0.1/facts-and-assumptions.md` F2 is still true). The in-repo model, `tests/internal_bridge.py` with `tests/model_launch_agent.py`, is stdlib-only by construction. `decisions/0001` R5 records this as "a guess about someone else's ticket".
- **How internal dogfood settles it.** When `implement-internal-bridge-launcher` is written, list its imports. Then run it on the internal host with only the platform interpreter.
  - **Held:** it runs, and every import is standard library.
  - **Failed:** a package is needed. Contract section 6 keeps that dependency on the launcher's side of the seam.
- **Depends on it.** The internal launcher only. If a package is needed, that is a #90 deployment finding, not a change to chat or session code.
- **Owner.** #90.

### RT-4 SELinux does not refuse the store location or the loopback bind

- **Claim.** On the internal host, with SELinux enforcing, `run_shell.py` can bind its loopback port, and it can create, link, rename, lock and fsync under the chosen `--root`.
- **Status.** `unproven`.
- **Why external validation cannot settle it.** The external hosts' SELinux policy and store paths are not the internal ones (`decisions/0001` R4, a guess).
  - #86's internal run (RT-1) is bearing evidence, but it does not settle this. It did not record where its store lived, or any `ausearch` result.
- **How internal dogfood settles it.** Run `python3 dory-wrangler/run_shell.py --root <the intended store path>`, send one turn, restart, and reopen. Then run `ausearch -m avc -ts recent` (as root).
  - **Held:** the turn and the reopen succeed, with no AVC denial naming the process.
  - **Failed:** a denial, or a refused write. `decisions/0001` says the remedy is a label or a different store path, not a code change.
- **Depends on it.** The store and the served shell. A failure is a #90 deployment finding.
- **Owner.** #90.

### RT-5 The internal host's locale lets the launcher hand any turn text to `codex` as a command-line argument

- **Claim.** On the internal host, the process locale lets turn text be passed to `codex exec` / `codex exec resume` as a command-line argument. This covers any text a user can type, including characters outside Latin-1.
- **Status.** `unproven`.
- **Evidence behind the concern.**
  - The internal resume shape passes the prompt as an argument (TR-8).
  - Under `LC_ALL=en_US.iso88591`, `subprocess` has been measured refusing an argument that the locale cannot encode, before the program runs (`tests/categories.py`, "the locale's encoding").
  - That behaviour carrying over to the real launcher is an inference. The launcher has not been written or measured.
- **Why external validation cannot settle it.** The internal locale is unknown, and the real launcher does not exist.
- **How internal dogfood settles it.** Record `locale` and `python3 -c 'import sys; print(sys.getfilesystemencoding())'` as the launcher's process sees them. Then send, through the real launcher, a turn that contains non-ASCII text outside Latin-1.
  - **Held:** the turn reaches the agent, and the reply renders.
  - **Failed:** the launch or deliver is refused locally, or the text arrives altered. Preserve the evidence with `diagnostics.py`.
- **Depends on it.** Every turn through the internal launcher. A failure would be a v0.1 gap.
- **Owner.** #90.

### RT-6 The internal host's wall clock does not step backwards during a turn

- **Claim.** The wall clock on the internal host does not step backwards between a turn's writes.
- **Status.** `unproven`.
- **Why external validation cannot settle it.**
  - The product refuses a backwards clock rather than patching it. The refusal leaves an exit (#88 convergence review R2 and its fix; control-plane `issue-88/state.md`). Clock steps are driven only by scripted clocks in the tests.
  - The internal host's time synchronisation is unknown.
- **How internal dogfood settles it.** Record the time-sync configuration (`timedatectl`, or `chronyc tracking`).
  - **Held:** no step backwards observed during dogfood. A slewing configuration is supporting evidence.
  - **Failed:** a turn is refused because the clock went back. Preserve it with `python3 dory-wrangler/diagnostics.py STORE CHAT_ID --records lifecycle`.
- **Depends on it.** Store ordering checks (contract F10 is carried unresolved; see Appendix B). A refusal costs one turn, not the chat. It is later work unless dogfood shows it happens.
- **Owner.** #90.

## Group 2. Filesystem and store

### FS-1 The store's filesystem supports hard links, and `link` refuses an existing name

- **Claim.** On the filesystem that holds the internal store:
  - `os.link` works;
  - `os.link` fails with `FileExistsError` when the target name exists.
- **Status.** `unproven`.
- **Why external validation cannot settle it.** `decisions/0001` R3 builds every durability guarantee on POSIX `link` exclusivity. Two measurements from the #89 portable-labels check show what happens without it:
  - **No hard links** (`os.link` gives `EPERM`, as on some FUSE and SMB mounts): nothing can be stored, and `test_chat_loop` shows 20 errors.
  - **A `link` that replaces an existing name:** the product's drain never terminates. The portable suite then **hangs** at `test_diagnostic_access.OneDefinitionOfAOneShotTurnEnd.test_a_page_that_does_not_advance_is_not_a_turn_end` instead of failing (TA-2).

  Sources: `issue-89/rails/issue-89-portable-labels-check/handoff.md`, and #89 comment 5819502876. External hosts use local filesystems. The internal store's filesystem is unknown.
- **How internal dogfood settles it.**
  1. `findmnt -T <store path>`, and record `FSTYPE`.
  2. In an empty scratch directory on that filesystem, run `python3 -c "import os; open('a','w').close(); os.link('a','b'); os.link('a','b')"`.
     - **Held:** the second `link` raises `FileExistsError`.
     - **Failed:** it succeeds silently, or the first raises `PermissionError`/`OSError`.
     - The directory must be empty. Where `b` is left over from an earlier run, the *first* `link` raises `FileExistsError`, and that proves nothing.
  3. With `TMPDIR` on that filesystem, run `timeout 1800 python3 dory-wrangler/tests/run_tests.py --category portable`. The outer `timeout` is there because the suite has no per-test deadline (TA-2).
     - **Held:** exit 0.
     - **Failed:** errors, or exit 124, which is a hang.
- **Depends on it.** Every durable write: record publication, sequence claims and exclusive creation. A failure makes the store unusable on that filesystem. `decisions/0001` names the remedy as placing the store on local disk (`--root`), which is a deployment note and not a redesign. It is a #90 finding either way.
- **Owner.** #90.

### FS-2 `flock` on the store's filesystem conflicts between processes and between descriptors

- **Claim.** On the internal store's filesystem, `fcntl.flock` taken by one process, or on one descriptor, is refused to another.
- **Status.** `unproven`.
- **Why external validation cannot settle it.**
  - The product relies on `flock` for three things:
    - one serving process per store (D1, human-confirmed; #81 comment 5687495740);
    - the per-chat turn lock that refuses a concurrent turn or action (decision 0003);
    - the store lock.
  - The portable and launch-boundary categories pass whole with `flock` emulated by POSIX `lockf`, as the Linux NFS client does, and with `flock` a no-op (#89 comment 5819502876; `issue-89/state.md` "Portable Tests Accepted"). So every contention property rests on 22 named development-environment tests, listed under "flock, or exclusive creation" in `tests/categories.py`.
  - Under a no-op `flock`, 13 of the 19 lock and exclusive-creation tests fail and one errors, which proves those tests are live (portable-labels check).
  - External hosts are local filesystems. `flock` over NFS depends on the mount and the server (`decisions/0001` R3; #86 comment 5668368596 names it as more load-bearing since convergence).
- **How internal dogfood settles it.** Run `findmnt -T <store path>`. Then, with `TMPDIR` on the store's filesystem, run `python3 dory-wrangler/tests/run_tests.py --category development-environment --json dev.json`, and read the tests in the `flock` group of `tests/categories.py`.
  - **Held:** that group passes. A second `run_shell.py --root <same store>` is refused with the fixed store-in-use words and changes nothing.
  - **Failed:** failures in that group, or a second serving process that is not refused. That is a finding about the host (README "On an internal run", item 2), not an environmental excuse.
- **Depends on it.** D1's single-server guarantee, refuse-not-queue, and one-agent-per-chat across processes. A failure is a v0.1 gap on that filesystem. `decisions/0001` names moving the store to local disk as the deployment remedy.
- **Owner.** #90.

### FS-3 The store's filesystem is local, so `fsync`, `rename` and `replace` give the durability the store assumes

- **Claim.** The internal store lives on a local filesystem (the Rocky 9 defaults are xfs or ext4). On it, `os.replace` and directory `os.rename` are atomic, and `fsync` reaches stable storage.
- **Status.** `unproven`.
- **Why external validation cannot settle it.**
  - #86 recorded that "whether `fsync` is honoured to stable storage" is unestablished (control-plane `issue-86/state.md`, "Delivered").
  - Kill-based atomicity is tested externally; power loss is not.
  - `decisions/0001` R3 says NFS weakens `link` exclusivity and `fsync` ordering.
- **How internal dogfood settles it.** Run `findmnt -T <store path>` (and `stat -f`).
  - **Held:** a local xfs or ext4 mount. Loss of power is not tested here or internally, so that part stays unproven by both.
  - **Failed:** a network or FUSE filesystem. Report the type; do not work around it.
- **Depends on it.** "Writes cannot expose partially updated chat state" (#86), and reopen after restart. On a non-local filesystem, the store should be moved, per decision 0001. It is a #90 finding.
- **Owner.** #90.

## Group 3. Process and launch mechanics

### PR-1 [human-required] Internal operating prerequisites fail in a shape the launcher can report as `unavailable`

- **Claim.** When the internal prerequisites are not met -- an inactive user session, or an uninitialised VS Code bridge -- the internal call fails in a shape the launcher can recognise and report as the abstract `unavailable` category. It does not hang or fake success.
- **Status.** `unproven`.
- **Sources.**
  - The prerequisites are a prior observation that carries no authority (`contract/v0.1/facts-and-assumptions.md` F8).
  - Their failure shapes are "still unproven -- do not assert" (#89 comment 5668368963; #90 comment 5668369261).
  - `launch-boundary.md` obligation 4 maps both to `unavailable`.
  - `launchers/dev_local.py`'s `launch` OSError comment claims the same. It was deliberately left unverified (control-plane `issue-88/state.md`, "Carried Cleanups Delivered").
- **Why external validation cannot settle it.** Neither prerequisite exists externally.
- **How internal dogfood settles it.** With the real launcher registered, run one turn in each of three states: with the user session inactive; with the bridge not initialised; with both in order. Capture exit status, stdout and stderr, and the stored `launch_result` (`python3 dory-wrangler/diagnostics.py STORE CHAT_ID --records lifecycle`).
  - **Held:** both failures return promptly, and are recorded `launch_failed` with category `unavailable`. The next turn, once the prerequisite is fixed, is answered.
  - **Failed:** a hang, an `accepted` result, a category other than `unavailable`, or a lost turn.
- **Depends on it.** The launch-failure path (contract 5.2). The user learns that the bridge is dead only by attempting a turn; that is a product property (control-plane `issue-87/state.md`, "Review Outcome"). A wrong category is a v0.1 gap.
- **Owner.** #90.

### PR-2 [human-required] Several agents run concurrently, and each one's output is attributable to its own session

- **Claim.** Two chats with agents active at once on the internal path each receive only their own agent's output. One `thread_id` is never resumed by two calls at once. (The product's per-chat turn lock prevents that within one chat.)
- **Status.** `unproven`.
- **Sources.**
  - The human listed "whether one ID can be resumed concurrently, and whether several agents can run concurrently" as still unproven (#89 comments 5668368963 and 5669592360).
  - #90 comment 5648267936 asks whether concurrent agents' output is attributable.
  - `facts-and-assumptions.md` A7.
- **Why external validation cannot settle it.** The external launchers are in-repo processes with known isolation. Codex's concurrent behaviour cannot be observed here.
- **How internal dogfood settles it.** Open two chats, and send a turn in each so that the two turns are in flight together. Each turn should ask for a distinct nonce to be stored. Then ask each chat to recall its nonce.
  - **Held:** each chat recalls only its own nonce. `validate_store.py` accepts the store with no `CORRELATION_MISMATCH`.
  - **Failed:** a crossed nonce, a refused or failed concurrent call, or mis-correlated events.
- **Depends on it.** Correlation rule P3. If attribution fails, #90 should report that v0.1 is effectively single-concurrent-chat on the internal path (`facts-and-assumptions.md` A7), and triage it.
- **Owner.** #90.

### PR-3 A harness death mid-launch leaves no agent that a retry would silently duplicate

- **Claim.** If `run_shell.py` dies while an internal launch is in flight, the internal path either leaves no agent running, or leaves one that is detectable. A retry, which is a new session under contract 5.4, therefore does not create a hidden duplicate.
- **Status.** `unproven`.
- **Source.** `facts-and-assumptions.md` A10.
- **Why external validation cannot settle it.** What `codex` or the bridge does when its caller dies is not observable externally.
- **How internal dogfood settles it.**
  1. Start a turn whose answer takes a while.
  2. `kill -9` the `run_shell.py` process.
  3. Look for the `codex` process and the thread with `ps`, and through whatever the bridge exposes.
  4. Restart, abandon, and resend.

  - **Held:** no orphan, or an orphan that is visible and whose thread is not resumed by the retry.
  - **Failed:** an undetectable orphan that keeps working.
- **Depends on it.** Contract 5.4. v0.1 deliberately reaps no orphan (automatic recovery is #83). An orphan is recorded as evidence and deferred, not fixed here.
- **Owner.** #90.

## Group 4. The internal transport and event stream

### TR-1 [human-required] Continuation: the internal path delivers a further prompt to an already-running agent

- **Claim.** The internal path can deliver a later turn to the same agent. `continuation: persistent` is the normal internal case.
- **Status.** `held (internal evidence)`.
- **Evidence.**
  - `launch_agent.sh "<message>"` returns the response and a resume ID.
  - `launch_agent.sh --resumeID=<id> "<message>"` resumes the same agent.
  - A nonce stored in one turn (`nonce-12345`) was recalled in a later invocation using only the resume ID.

  Sources: #87 comment 5668368252, #89 comment 5668368963 and #90 comment 5668369261; control-plane `issue-85/state.md` "Internal Compatibility Findings". The mapping onto the contract needs no change: the resume ID is `agent_handle`, and `--resumeID` is `deliver`.
- **History.** The human required this entry as **unproven** on 2026-09-12 (#89 comment 5648269596). The human's comments of 2026-09-14 explicitly supersede that: "This supersedes the 2026-09-12 direction" (#87 comment 5668368252).
- **Why external validation cannot settle it.** The bridge is not reachable externally.
- **How internal dogfood settles it.** Re-confirm on the release tree with the real launcher: `python3 dory-wrangler/tests/e2e_loop.py --launcher <internal id> --request-timeout 1800`. The default request timeout is 120 s. A real turn slower than that fails a step for a reason that is neither held nor failed (TR-20). The path's first turn is `hello`, and its second asks `What was the first thing I said in this thread?`. Separately, in a chat of your own, store a nonce in turn 1 and ask for it in turn 2.
  - **Held:** `PASS continue: the second turn is delivered to the same agent session, as continuation persistent declares`. The answer quoted on that line names `hello`, and the nonce is recalled.
  - **Failed:** `FAIL continue`, with the second turn answered by a new session or refused, or a second answer that does not know the first turn.
- **Depends on it.** The internal launcher declares `continuation: persistent`. Fresh binding stays a supported declared capability, but nothing may be designed around it as the normal path. A failure would reopen instruction-packet composition under `fresh_binding`, which is still deliberately undecided (TR-2).
- **Owner.** #90.
- **Caveat.** Continuity has been shown by one nonce round-trip only (TR-16).

### TR-2 [human-required] Instruction-payload bound

- **Claim.** There is some size at which an internal instruction payload starts to truncate or fail, and the behaviour there can be observed.
- **Status.** `unproven`. **No bound may be stated or enforced as though measured.** Every launcher declares `instruction_bound_bytes: null`, and a test defends that on both packet types (control-plane `issue-86/state.md`, "Sweep Completed And Adjudicated", B4).
- **Sources.**
  - #89 comment 5648269596 (human-required);
  - #89 comments 5668368963 and 5669592360 (still unproven);
  - #90 comment 5648267936, item 2;
  - `facts-and-assumptions.md` U2 (A4 superseded).
- **Why external validation cannot settle it.** It is a property of the internal bridge, `codex` and the host. The mechanism is tested externally; the value is not.
- **How internal dogfood settles it.** Follow #90 comment 5648267936 item 2. Send instruction text of increasing size through **the same invocation the real launcher uses**; the internal resume shape passes the prompt as a command-line argument (TR-8). Ask the agent to report the length and the last characters it received. Record the smallest size at which the text arrives altered or the call fails, and which of these happened: silent truncation, explicit rejection, or failure.
  - The outcome is a measured number with its behaviour, not held or failed.
  - Until it exists, the declared bound stays `null`.
- **Depends on it.** `launcher_capabilities.instruction_bound_bytes`, and the undecided packet composition under `fresh_binding` (`facts-and-assumptions.md` U1, "deliberately still open"). Supplying the number is one field in the launcher, not a contract change. It is v0.1 evidence for #90.
- **Owner.** #90.

### TR-3 [human-required] Response shape: output arrives as discrete events, the same on launch and resume

- **Claim.** Internal agent output arrives as discrete, individually preservable events: JSONL, one JSON event per line. Fresh launch and resume use the same shape.
- **Status.** `held (internal evidence)`.
- **Evidence.**
  - Fresh `codex exec --json` and `codex exec resume ... --json "<thread_id>" "<prompt>"` both emit JSONL, one JSON event per line, in the same shape.
  - Sources: #87 comment 5669591294, #89 comment 5669592360 and #90 comment 5669593061; control-plane `issue-87/state.md` "Internal Transport Established".
  - This settles `facts-and-assumptions.md` A8 and the discrete-events half of U3 for the internal path.
- **History.** The human required this entry as **unproven** on 2026-09-12 (#89 comment 5648269596): "whether the internal path can be observed as discrete events at all".
- **Why external validation cannot settle it.** The internal Codex output cannot be produced externally. The in-repo model reproduces only the two recorded shapes.
- **How internal dogfood settles it.** Re-confirm with a redacted capture of one real launch and one real resume, which the human has asked for more than once (#90 comment 5669593061). Also run `diagnostics.py STORE CHAT_ID` after a real turn.
  - **Held:** one `diagnostic_event` per line, every line preserved verbatim.
  - **Failed:** multi-line JSON documents, or undelimited output. Those would be preserved as `malformed`, as the contract permits.
- **Depends on it.** Intake, classification and preservation (contract 7). A failure would be a v0.1 gap in the launcher's framing (LO-3), not in the harness.
- **Owner.** #90.

### TR-4 [human-required] Each internal call returns once with its output; the adapter's declared `response_shape` is #90's decision

- **Claim.** Each internal call -- `launch_agent.sh`, or a `codex exec` / `codex exec resume` process -- returns once, with that turn's output: one response per call.
- **Status.** `held (internal evidence)`, for exactly the claim above.
- **Evidence.** "Response shape internally: one response per call, i.e. `response_shape: one_shot` with `continuation: persistent`" (#89 comment 5668368963; #87 comment 5668368252: "since each call returns the response").
- **Two readings, both recorded.**
  - **2026-09-14 17:58.** #89 comment 5668368963 records `response_shape: one_shot` as held.
  - **Later the same day.** Comments at 18:24 and 19:28 say that whether the adapter declares `stream` or `one_shot` is **not decided**. They call it "an adapter design decision to make on evidence, not a transport limitation", because the raw JSONL stream is available. Sources: #90 comments 5668684764 and 5669593061; #87 comments 5668684014 and 5669591294; #88 comments 5668684239 and 5669591807.
  - **Resolution, from the durable records.** The 19:28 comments state that they supersede the earlier comments where they conflict. #89 comment 5669592360 does the same for the format entries, and control-plane `issue-87/state.md` "Internal Transport Established" lists the stream-versus-one-shot choice as an adapter design decision.
  - What is held is the call's behaviour. The adapter's declared `response_shape` is **not** an assumption. It is an open #90 design decision, and the in-repo model declares `one_shot` "provisional until a real capture" (`launch-boundary.md`).
- **Why external validation cannot settle it.** Only the internal script and CLI have this behaviour.
- **How internal dogfood settles it.** Time a real call, and observe whether any output line is available before the process exits.
  - **Held:** the call returns once, with the whole turn.
  - **Failed:** output continues after the call returns.

  Record the evidence #90 uses to choose `response_shape`.
- **Depends on it.** The choice between LO-4 (one-shot obligations) and LO-5 (the paging-stream limitation). The user's Stop cannot reach a turn in flight in v0.1 either way. The human decided that on 2026-09-15 (#81 comment 5687495740).
- **Owner.** #90.

### TR-5 [human-required] An end of turn is distinguishable from a quiet agent

- **Claim.** The internal output carries an explicit end-of-turn event that is distinguishable from the process exiting. Separately, a failure to read the output on our side is distinguishable from the agent's output ending.
- **Status.** `unproven`.
- **Sources.**
  - #89 comment 5648269596, the second half of the response-shape entry;
  - #89 comment 5669592360 ("any explicit end-of-turn event", unproven);
  - #90 comment 5669593061;
  - `facts-and-assumptions.md` A2, and the end-of-stream half of U3.
- **Why external validation cannot settle it.** Only `thread.started` and `item.completed`/`agent_message` are known internally. No other event type may be assumed (TR-12).
- **How internal dogfood settles it.** From the captures (TR-3), list the last line of every turn, and whether any event type recurs exactly once at a turn's end.
  - **Held:** such a type exists in every captured turn, and never mid-turn.
  - **Failed:** the turn's end is known only from process exit.

  Report it; do not add a recognized type without the capture.
- **Depends on it.**
  - Under `one_shot`, process return is the turn end (contract 6.1).
  - Under `stream`, an end-of-stream must be distinguishable.
  - A merely quiet agent stays unresolvable in v0.1 by design; that is #83's gap and is not closed with a timer.
- **Owner.** #90.

### TR-6 [human-required] The handle is `thread.started.thread_id`, issued by Codex

- **Claim.** The internal `agent_handle` is the `thread_id` field of the `thread.started` event in `codex exec --json` output. It is issued by Codex, not synthesised.
- **Status.** `held (internal evidence)`.
- **Evidence.** #89 comments 5668684450 and 5669592360; #87 comments 5668684014 and 5669591294; #90 comments 5668684764 and 5669593061. Launcher-author obligation 6 therefore does not apply to it (`launch-boundary.md`).
- **Why external validation cannot settle it.** Internal CLI output.
- **How internal dogfood settles it.** In the capture, and in `python3 dory-wrangler/diagnostics.py STORE CHAT_ID --records lifecycle` for a real session:
  - **Held:** the session's `agent_handle` equals the captured `thread.started.thread_id`.
  - **Failed:** there is no `thread.started`, or the `thread_id` is elsewhere.
- **Depends on it.** Every addressing operation (`deliver`, `events`, `stop`) and re-attachment. A failure is a v0.1 gap in the launcher.
- **Owner.** #90.

### TR-7 [human-required] The reply is `item.completed` with `item.type == "agent_message"`, text in `item.text`, the same on resume

- **Claim.** The assistant's reply arrives as an `item.completed` event whose `item.type` is `agent_message`, with its text in `item.text`. Resumed turns use the same shape.
- **Status.** `held (internal evidence)`.
- **Evidence.** #89 comment 5669592360; #87 comment 5669591294; #90 comment 5669593061. The recognized set is declared once in `tests/internal_bridge.RECOGNIZED` (decision 0004).
- **Why external validation cannot settle it.** Internal CLI output.
- **How internal dogfood settles it.** After real turns (launch and resume), run LO-3's procedure over the dogfood store: snapshot it, then run `reclassify_stores.py`. Phase 3 of `run_tests.py` reads only the suite's own stores. Also compare each agent message with its cited event's `item.text`, using `python3 dory-wrangler/diagnostics.py STORE CHAT_ID --records messages` and `... --records events`.
  - **Held:** every rendered message is exactly an `item.text`.
  - **Failed:** the reply text arrives in another event or field. It would then be preserved as `unrecognized` and nothing would render; the no-showable-reply notice would say so (decision 0006).
- **Depends on it.** Rendering (decision 0005). A failure is a v0.1 gap in the classifier declaration (LO-3).
- **Owner.** #90.

### TR-8 [human-required] A `thread_id` resumes across separate launcher processes

- **Claim.** Resume by `thread_id` works across separate `codex exec` processes. The handle survives the launcher process ending, which is the property contract 6.1 needs ("a launcher remembers nothing between calls").
- **Status.** `held (internal evidence)`.
- **Evidence.** "Each `codex exec` invocation is a separate process, and resume by `thread_id` works across them" (#87 comment 5669591294; #90 comment 5669593061; #89 comment 5669592360).
  - The working resume shape is `<codex> exec resume -c "model_provider=<provider>" -c "model=<internal model>" --skip-git-repo-check --json "<thread_id>" "<prompt>"`.
- **Why external validation cannot settle it.** Internal CLI behaviour.
- **How internal dogfood settles it.** Re-confirmed by TR-1's procedure, because every call is a new process.
  - **Held:** turn 2 resumes turn 1's thread.
  - **Failed:** the resume is refused, or starts a new thread.
- **Depends on it.** Contract 6.1 C1 ("what a launcher must remember between calls: nothing"). A failure would be a v0.1 gap.
- **Owner.** #90.

### TR-9 [human-required] A `thread_id` still resumes after a Dory-wrangler restart, and after a bridge, VS Code or VM restart

- **Claim.** A `thread_id` issued before a restart still resumes the same agent after each of these:
  - a restart of Dory-wrangler (`run_shell.py`);
  - a restart of the bridge or VS Code;
  - a VM or host restart.
- **Status.** `unproven`, for all three.
- **Sources.**
  - #89 comment 5668368963 lists all three as unproven.
  - #89 comment 5669592360 keeps "restart survival beyond process boundaries" unproven.
  - #87 comment 5669591294 names bridge, VS Code and VM as unproven.
  - TR-8 bears on a Dory-wrangler restart, because each call is its own process, but no restart has been demonstrated.
- **Why external validation cannot settle it.** The Codex JSONL model re-attaches and resumes after a real shell restart (`tests/test_internal_bridge.ARestartOfTheShellResumesTheSameThread`; the `e2e_loop.py --codex-model` third turn). The model is not the internal CLI.
- **How internal dogfood settles it.**
  - **Dory-wrangler restart.** Run `python3 dory-wrangler/tests/e2e_loop.py --launcher <internal id> --request-timeout 1800`. The default of 120 s can fail a real turn on time alone (TR-20). `third-turn` **passes in both outcomes**, so read its words: PASS alone settles nothing. The path's third turn asks `What was the first thing I said in this thread?`, and its first turn was `hello`.
    - **Held:** `PASS third-turn: the third turn is answered after the restart: the agent live before the restart was re-attached and answered it, [...]`, and the answer quoted in the brackets names `hello`.
    - **Failed:** `PASS third-turn: the third turn is answered after the restart: a newly launched agent answered it, [...]; the restart could not re-attach the agent, so the refused turn was sent again after the one action, abandon`. The thread did not survive, and the path took Abandon, as dev-local `persistent` does by design. `FAIL third-turn: continuation is declared persistent, but the agent live before the restart did not answer the third turn` is also failed.
    - Both lines were seen on this tree. `e2e_loop.py --codex-model` prints the held line. `e2e_loop.py --launcher dev-local --launcher-options '{"profile": "persistent"}'` prints the failed line.
  - **Bridge or VS Code restart, and VM restart.** Store a nonce, restart that component, then send a turn in the same chat.
    - **Held:** the nonce is recalled.
    - **Failed:** refused, `reattach_failed`, or a new thread.
- **Depends on it.** Re-attachment (contract 5.4). A failure does **not** strand the chat: `unknown`, then Abandon, is always the exit (D2, human-confirmed), and the history reopens byte-identical from durable state. What a failure costs is the agent's memory across that restart. #90 triages it; it is not automatically a v0.1 gap.
- **Owner.** #90.

### TR-10 [human-required] `thread_id` lifetime, expiry, and invalid-ID behaviour

- **Claim.** How long a `thread_id` remains resumable, and what `codex exec resume` returns for an expired or invalid ID.
- **Status.** `unproven`.
- **Sources.** #89 comments 5668368963 and 5669592360; #87 comment 5669591294; `launch-boundary.md` obligation 7 ("how long a launcher must be able to [address its own thread] is unspecified in v0.1").
- **Why external validation cannot settle it.** Codex property.
- **How internal dogfood settles it.** Resume a thread after progressively longer idle periods over the dogfood days, and record the oldest resume that worked. Resume a syntactically valid but never-issued ID, and capture exit status, stdout and stderr.
  - **Held:** the invalid ID fails promptly and distinguishably, and is recorded without fabricating an answer (`tests/test_internal_bridge.TheHandleIsWhatSelectsTheThread` is the modelled shape).
  - **Failed:** a hang, or a silently new thread.
- **Depends on it.** Re-attachment and `deliver`. The exit for an expired handle is the same as TR-9's. A silently new thread would be a v0.1 gap, because the transcript would imply continuity it does not have.
- **Owner.** #90.

### TR-11 [human-required] `thread_id` uniqueness

- **Claim.** Codex never issues the same `thread_id` to two agents that the store holds, whether within one run or across restarts.
- **Status.** `unproven`.
- **Sources.** #87 comment 5668684014 ("its uniqueness, lifetime, and survival across restarts are Codex's properties and remain unproven"); #89 comment 5668684450; control-plane `issue-87/state.md` N2.
- **Why external validation cannot settle it.** Nothing above the seam can detect it. A handle is opaque to the harness and to the validator, so a store in which two live sessions share one address validates (`launch-boundary.md` obligation 6). External N2 evidence concerns synthesised handles only (TA-11).
- **How internal dogfood settles it.** Over the whole dogfood store, list every `agent_handle`: run `python3 dory-wrangler/diagnostics.py STORE CHAT_ID --records lifecycle` for each chat directory under `STORE/chats/`, and read the `agent_handle` of every `agent_session` record.
  - **Held:** no handle appears on two sessions.
  - **Failed:** any repeat. The procedure catches one: on the store of `e2e_loop.py --launcher dev-local --launcher-options '{"profile": "persistent"}'`, two sessions carry `dev-local-agent-0001` (TA-11).
- **Depends on it.** Correct addressing of `deliver`, `events` and `stop`. A repeat would route one chat's turn to another's agent: a v0.1 gap.
- **Owner.** #90.

### TR-12 [human-required] The internal Codex CLI version, and every event type other than the two known

- **Claim.** Which Codex CLI version runs internally, and which event types it emits besides `thread.started` and `item.completed`/`agent_message`.
- **Status.** `unproven`.
- **Sources.** #89 comments 5668684450 and 5669592360; #90 comment 5669593061; `facts-and-assumptions.md` A9 (agent output worth showing is plain text).
- **Why external validation cannot settle it.** No Codex event schema may be hard-coded from documentation or memory (#88 comment 5668684239).
- **How internal dogfood settles it.** `codex --version`. Then, from real sessions, list the preserved types with `diagnostics.py`. Every type other than the two is stored `unrecognized` and renders nothing.
  - **Held/settled:** the set is recorded. A type that carries user-relevant content is reported as a finding, with its capture (A9).
  - A type is added to the recognized set only on the evidence of real output that carries it (decision 0004).
- **Depends on it.** Rendering and the notice. Adding types is #90 or later work, decided on captures.
- **Owner.** #90.

### TR-13 [human-required] Zero, one or several `agent_message` items per turn

- **Claim.** How many `agent_message` items one internal turn produces: whether a turn can produce none, or several.
- **Status.** `unproven` internally. The product already handles zero and several without fabricating or dropping (decision 0005; `tests/test_internal_bridge...test_two_agent_messages_in_one_turn_are_both_the_chat_and_neither_is_invented`).
- **Sources.** #89 comment 5669592360; #90 comment 5669593061; #88 comment 5669591807.
- **How internal dogfood settles it.** Count `agent_message` events per turn in the dogfood store (`diagnostics.py`).
  - **Held:** counts are recorded, and each renders as that many messages. A turn with none gets exactly one no-showable-reply notice (decision 0006).
  - **Failed:** a rendered count that differs from the preserved count.
- **Depends on it.** Rendering. A mismatch would be a v0.1 gap.
- **Owner.** #90.

### TR-14 [human-required] Error, failure, partial-output and stderr shapes

- **Claim.** What the internal path produces when a turn fails: failure events, a non-zero exit status, stderr, partial output.
- **Status.** `unproven`.
- **Sources.** #89 comments 5668368963 and 5669592360; #87 comment 5669591294; #90 comment 5669593061.
- **Why external validation cannot settle it.** Internal CLI and bridge behaviour. The model's failure lines are clearly synthetic (#88 N8, closed).
- **How internal dogfood settles it.** Provoke each failure you can reach -- invalid ID (TR-10), prerequisite failures (PR-1), a killed `codex` process mid-turn -- and capture exit status, stdout and stderr.
  - **Held:** each failure is preserved and correlated, and recorded with an honest category. The chat takes the next turn.
  - **Failed:** output lost, a hang, or a wrong category.

  The real launcher decides whether stderr is preserved, and how. `dev-local` discards its agent's stderr, and the README names that.
- **Depends on it.** Launch-failure and agent-failure classification (contract 5.2). #90 counts failure categories. A lost payload is a v0.1 gap (contract 7 P1).
- **Owner.** #90.

### TR-15 [human-required] A stop or cancel operation exists

- **Claim.** The internal path offers an operation that stops or cancels an agent through its handle.
- **Status.** `unproven`. None has been reported, so the internal launcher **must be assumed to have none** (#89 comment 5668368963).
- **Sources.** #89 comments 5668368963 and 5669592360; #90 comment 5669593061; `facts-and-assumptions.md` A3, the stop half.
- **Why external validation cannot settle it.** Internal CLI and bridge.
- **How internal dogfood settles it.** Report whether any stop or cancel exists. With none, the real launcher records a stop as unconfirmed (`stop_unconfirmed`).
  - **Held:** the operation exists, and a stopped agent is confirmed stopped.
  - **Not held:** Abandon moves the session `running -> unknown -> abandoned`, and it is never shown as `terminated` (the human decision, #81 comment 5687495740).
- **Depends on it.** The `running -> terminated` transition only. Without a stop, the chat still has its exit (contract 5.4, D2), so its absence is not a v0.1 gap. Stopping a turn in flight is #83's work.
- **Owner.** #90.

### TR-16 [human-required] Continuity holds beyond one demonstration

- **Claim.** A resumed agent remembers earlier turns reliably across a real chat, not only in the single nonce round-trip demonstrated.
- **Status.** `unproven`.
- **Source.** "The proof is one nonce round-trip" (#89 comment 5668368963).
- **How internal dogfood settles it.** Over the dogfood days, keep one chat going for many turns. Periodically ask for a fact stated several turns earlier.
  - **Held:** every recall correct.
  - **Failed:** any turn that answers as a fresh agent while the store shows the same handle.
- **Depends on it.** The persistent-continuation assumption (TR-1). A long chat exhausting or confusing its single agent is **expected** v0.1 behaviour (#81), not a failure of this entry. Only loss of the thread is.
- **Owner.** #90.

### TR-17 The launch outcome is distinguishable from the agent's own output

- **Claim.** An internal launch yields an acknowledgement that an agent started, separately from what the agent then says.
- **Status.** `unproven`. `thread.started` carries the handle, which bears on it, but no durable source records it as the launch acknowledgement.
- **Source.** `facts-and-assumptions.md` A1.
- **Why external validation cannot settle it.** Internal output.
- **How internal dogfood settles it.** From the captures, check whether `thread.started` always precedes any agent output, and appears even when the agent then fails.
  - **Held:** it does.
  - **Failed:** a launch that starts an agent emits no `thread.started` first. The harness then preserves the output (`launch-boundary.md` obligation 11) and records no accepted launch.
- **Depends on it.** `launching -> running` and the reachability of `launch_failed` (contract 5.2). #90 triages.
- **Owner.** #90.

### TR-18 Instruction text plus correlation is the whole per-call input

- **Claim.** No per-launch host-specific parameter is needed beyond the instruction text. For resume, the handle is needed too. Provider, model and `--skip-git-repo-check` are launcher configuration, read from the launcher's own environment.
- **Status.** `held (internal evidence)`.
- **Evidence.** The launch form is `launch_agent.sh "<message>"`. The resume form is `launch_agent.sh --resumeID=<id> "<message>"`, or `codex exec resume -c "model_provider=..." -c "model=..." --skip-git-repo-check --json "<thread_id>" "<prompt>"`. The human states that the provider, the model and the flag are launcher configuration (#87 comments 5668368252 and 5669591294; #90 comment 5669593061).
  - What is held rests on two things: the human's statement of that mapping, and the invocation shapes that have been shown to work. Nobody has observed that no other per-launch input is ever needed. The procedure below checks that for the real launcher.
- **Source.** `facts-and-assumptions.md` A5.
- **Why external validation cannot settle it.** Internal invocation.
- **How internal dogfood settles it.** Check that the real launcher builds each call from the packet's text, the handle, and its own configuration only.
  - **Held:** `launch_request` and `delivery_request` hold the text alone (contract 6.4).
  - **Failed:** a per-turn host parameter has to enter the packet. Absorb it into launcher configuration instead (A5's exposure).
- **Depends on it.** The portability rule of contract section 6. A failure would be a contract-level finding for #90 to stop on.
- **Owner.** #90.

### TR-19 Agent output worth showing is adequately represented as plain text

- **Claim.** Nothing the internal agent emits that is worth showing a user is lost by rendering `text/plain` only.
- **Status.** `unproven`.
- **Source.** `facts-and-assumptions.md` A9.
- **How internal dogfood settles it.** Settled together with TR-12. For each non-rendered type, record whether it carried user-relevant content.
  - **Held:** none did.
  - **Failed:** a type carried content the user needed.
- **Depends on it.** `content.content_type` is fixed at `text/plain`. Anything more is later work, decided on captures.
- **Owner.** #90.

### TR-20 A real internal turn finishes within what a synchronous send will wait for

- **Claim.** A real internal turn, run through the real launcher, finishes before anything that waits for the send gives up. That includes the browser, anything between the browser and the loopback server, and `e2e_loop.py`. So the answer to a long turn is shown where it was asked for.
- **Status.** `unproven`.
- **What the source shows.** This part is read from this tree:
  - The served send is synchronous. `webapp.py`'s `do_POST` calls `service.send_user_message`, which runs the whole turn, launch or deliver and then the drain, before it answers.
  - The page's `fetch` sets no timeout of its own.
  - `e2e_loop.py` waits the same way, and bounds each request by `--request-timeout`, 120 s by default. A turn slower than that fails its step with `the shell gave no answer to POST /api/chats/<chat>/messages within 120s`. That line is about waiting, not about the entry under test.
- **What is inferred.** This is an **inference, not an observation** (checkpoint review F3):
  - that a real turn can take longer than the browser, a proxy or `e2e_loop.py`'s default will wait;
  - that a client giving up leaves the turn to finish on the server and be recorded. That is what the source implies, but it has not been measured against a client timeout.

  No real internal turn's duration has been recorded anywhere.
- **Why external validation cannot settle it.** The in-repo agents answer in well under a second. The internal CLI, the model, and the internal workstation's browser and network policy are not reachable externally.
- **How internal dogfood settles it.**
  1. Record every real turn's duration. Either time the send in the browser, or, for each turn, subtract the user message's `created_at` from the answering agent message's `created_at` in `python3 dory-wrangler/diagnostics.py STORE CHAT_ID --records messages`.
  2. In the browser, send a turn that asks for several minutes of work, and watch whether the page shows the answer without a reload.
  3. Whenever `e2e_loop.py` runs against the real launcher, pass `--request-timeout` well above the longest duration recorded (TR-1, TR-9, LO-6).
  - **Held:** durations are recorded, and the long turn's answer appears in the page as it would for a short one.
  - **Failed:** the page shows an error, or no answer, although the store then holds the answer. That means the client or something in between gave up. Record the duration at which it happened, and preserve the chat with `diagnostics.py`.
- **Depends on it.** Whether a user sees a long turn's answer where they asked for it. A client that gives up does not lose the turn: the server still finishes it and records it, so a reload should show it (inferred, above). The cost is the live display. #90 triages whether that is a v0.1 gap. Stopping or timing out a turn in flight stays out of v0.1 (#81 comment 5687495740; #83).
- **Owner.** #90.

## Group 5. Launcher obligations

`launch-boundary.md` "Adding the internal bridge launcher" lists twelve
obligations. Each entry here asks whether the **real** internal launcher meets
one of them on the internal transport. No such launcher exists yet
(`facts-and-assumptions.md` F2), so every entry is `unproven` until
`implement-internal-bridge-launcher`. The in-repo model meets them against a
modelled transport only.

### LO-1 The handle is taken from the transport and never synthesised

- **Claim.** The real launcher takes `agent_handle` from `thread.started.thread_id` (obligation 5). It synthesises nothing, so obligation 6's cross-restart uniqueness rule has nothing to bind.
- **Status.** `unproven`.
- **Sources.** `launch-boundary.md` obligations 5 and 6; control-plane `issue-87/state.md` N2 and "Internal Transport Established"; #90 comment 5668684764.
- **How internal dogfood settles it.** Code reading, plus TR-6's check.
  - **Held:** every session handle equals a captured `thread_id`.
  - **Failed:** any made-up handle. A counter-based handle would re-issue a live chat's address after a restart; that is N2, measured on the old model.
- **Depends on it.** Addressing, and TR-11. A failure is a v0.1 gap.
- **Owner.** #90.

### LO-2 The launcher keeps each call's raw output until `events` has served it

- **Claim.** The real launcher keeps each call's output, keyed by handle, until `events` has served it, including across a restart of the harness (obligation 7).
- **Status.** `unproven`.
- **Sources.** `launch-boundary.md` obligation 7; #88 re-review N3, resolved without a contract change (control-plane `issue-88/state.md`, "Intake Delivered", item E).
- **What is already known.** No harness behaviour rests on the launcher keeping state. With the model's spool deleted, the transcript is byte-identical and the chat degrades to `reattach_failed -> unknown -> abandon`.
- **What is unspecified.** How long a launcher must be able to address its thread is **unspecified in v0.1**. This register does not specify it.
- **How internal dogfood settles it.** Run TR-9's Dory-wrangler-restart procedure with a turn completed before the restart.
  - **Held:** after the restart, `events` serves from sequence 1 with nothing missing, and the store holds each line once.
  - **Failed:** payloads missing or duplicated.
- **Depends on it.** Re-attachment. If the spool is lost, the chat still exits through Abandon, and the cost is the agent's memory. #90 triages.
- **Owner.** #90.

### LO-3 One classification path, the declared recognized set, `\n`-only byte framing, and a classifier the whole-store gate reads

- **Claim.** The real launcher meets four obligations:
  - it classifies launch and resume output through one path;
  - it carries the recognized set declared in `tests/internal_bridge.RECOGNIZED` rather than a second copy of it;
  - it frames output as bytes split on `\n` only, before anything decodes it;
  - it registers its classifier with `tests/reclassify_stores.py`, so the phase-3 gate reads its stores.
- **Status.** `unproven`.
- **Sources.**
  - `launch-boundary.md` obligation 8;
  - #88 comments 5798973522 ("#90's real internal launcher inherits the Codex declaration and `\n`-only byte framing") and 5805392291 ("#90's real launcher must register one");
  - #88 comment 5811719649 ("frame bytes on `\n` only, and register a classifier");
  - #90 comment 5669593061's directive: raw JSONL for both, one path, no plain-text resume case.
- **Why it matters.** A text-mode pipe or `splitlines()` splits on U+2028 and U+0085, drops `\r`, and loses a whole turn on a non-UTF-8 line. That was measured on `dev-local` before `507f505`.
- **How internal dogfood settles it.** Snapshot the dogfood store, then re-classify the snapshot. Take the snapshot with no turn in flight; `validate_store.py` is read-only and may run while the shell serves.
  1. `mkdir DIR`. Use an empty directory, because `reclassify_stores.py` reads every `*.json` file in it.
  2. `python3 dory-wrangler/validate_store.py STORE --snapshot DIR/dogfood.json`. This should exit 0 with `the store satisfies contract v0.1`, and write the snapshot.
  3. `python3 dory-wrangler/tests/reclassify_stores.py DIR`.
  - **Held.** All of the following:
    - exit 0;
    - the first line is `1 store(s) in DIR`, so at least one store was read;
    - the real launcher's sessions are counted under **its own id**:
      - a line `<its id>  events N, reclassified N, reproduced N`, with N at least 1;
      - under "rendering (A6)", a line `<its id>  agent messages M, agent messages exact M`, with M at least 1;
    - `not reproduced: 0`;
    - `adjudicated by name: 0; unadjudicated: 0`;
    - `rendering disagreements: 0; adjudicated by name: 0; unadjudicated: 0`;
    - `unadjudicated sessions: 0`;
    - `system messages (decision 0006): disagreements: 0`.
  - **Failed.** Exit 1, for any of these:
    - a record not reproduced;
    - an unadjudicated rendering or system-message disagreement;
    - `UNREAD LAUNCHER dogfood.json '<id>' session ... carries ...`, which means a session under a launcher id that no classifier in `reclassify_stores.py` reads.

    The gate **fails closed** on that last one. It stays failed until #90 registers the real launcher's classifier in `reclassify_stores.py` (obligation 8). Note that `not reproduced` and `rendering disagreements` both still read 0 in this case, so do not read held from those two lines alone.
  - **Not evidence.** `0 store(s)` means nothing was read, which is neither held nor failed. It is what `reclassify_stores.py` prints, with exit 0, when pointed at a store directory instead of a directory of snapshots.
  - **Why not `run_tests.py`.** It cannot check a dogfood store. Before its first phase it deletes its kept-store directory, `support.FIXTURE_OUT`: `$DORY_TEST_STORE_DIR`, or else `$TMPDIR/dory-wrangler-stores` (`run_tests.py`, `shutil.rmtree(support.FIXTURE_OUT)`). Phase 3 then reads only the stores the suite's own tests wrote there. A dogfood store placed there is deleted unread.
  - **Run on this tree** (`issue-89-register-procedures-fix` handoff):
    - The `e2e_loop.py --codex-model` store gives `1 store(s)`, `internal-bridge  events 6, reclassified 6, reproduced 6` and `internal-bridge  agent messages 3, agent messages exact 3 ...`, exit 0.
    - The same store, with its session's `launcher_id` changed to `internal-codex`, a launcher id no classifier reads, still validates. It then gives `internal-codex  events 6, other launcher 6`, `unadjudicated sessions: 1`, `UNREAD LAUNCHER dogfood.json 'internal-codex' session ...`, exit 1.
  - A line of only ASCII whitespace carries no event and is not preserved (decision 0004). If real output ever carries meaning in such a line, report it.
- **Depends on it.** Preservation (contract 7 P1), classification, and A6 exact-text rendering. A failure is a v0.1 gap.
- **Owner.** #90.

### LO-4 A one-shot launcher serves output only from calls that have returned

- **Claim.** If the real launcher declares `response_shape: one_shot`, its `events` serves only output from calls that have already returned.
- **Status.** `unproven`.
- **Sources.** The diagnostic-access review judged the reading of contract 6.1 sound, on this assumption, and recorded it as a #90 obligation. See control-plane `issue-88/state.md`, "Diagnostic Access Review Outcome"; #88 comment 5811719649.
- **Why external validation cannot settle it.** It is a property of a launcher not yet written.
- **How internal dogfood settles it.** Code reading, plus a real turn whose reply is long.
  - **Held:** the drain decides the turn's end once, and the reply renders under the turn it answers.
  - **Failed:** a reply that renders under a later turn, or a no-showable-reply notice written over a turn that did answer.
- **Depends on it.** The one-shot turn-end rule (`SessionManager._one_shot_turn_ended`; decision 0006). A failure is a v0.1 gap.
- **Owner.** #90.

### LO-5 A paging `stream` launcher gets a false notice and a delayed reply after re-attachment

- **Claim.** If the real launcher declares `response_shape: stream` and returns a turn's output over several pages, then after re-attachment:
  - the next turn's drain writes a false no-showable-reply notice under turn 2;
  - turn 2's reply shows only after a later restart.
- **Status.** `known limitation`. It is a recorded product residual, conditional on the launcher's shape.
- **Sources.** G3 (#88 diagnostic review), carried to #90 (control-plane `issue-88/state.md`, "Diagnostic Access Accepted"; #88 comment 5811719649).
- **Why external validation cannot settle it.** Whether it arises depends on #90's `response_shape` decision (TR-4). No shipped launcher pages a stream.
- **How internal dogfood settles it.** Record the declared shape.
  - **Does not arise:** the launcher is `one_shot`, or a `stream` launcher that returns whole turns.
  - **Arises:** it pages, in which case reproduce and triage.
- **Depends on it.** The notice's honesty (decision 0006). It is a v0.1 gap only if #90 chooses a paging stream.
- **Owner.** #90.

### LO-6 The remaining seam obligations hold for the real launcher

- **Claim.** The real launcher meets obligations 9 to 12:
  - under `fresh_binding`, it reports the agent's exit (9);
  - `events` honours `after_sequence`, and a non-advancing page is refused rather than re-read (10);
  - a launch that issues no handle carries its output back through `payloads` under the three stated rules (11);
  - it has one entry in `launchers/registry.py` (12).
- **Status.** `unproven`.
- **Sources.** `launch-boundary.md` obligations 9 to 12. #87 F1 (`_drain` spinning against a launcher that ignores `after_sequence`) is closed in the harness. The pre-existing deliver-then-blocking-drain hang needs an inconsistent launcher (control-plane `issue-87/state.md`, "Current Decision").
- **How internal dogfood settles it.** Run `python3 dory-wrangler/tests/e2e_loop.py --launcher <internal id> --request-timeout 1800` (every step), plus a provoked launch with no `thread.started` (TR-17). With the default of 120 s, a slow real turn fails a step on time alone, and that is neither held nor failed here (TR-20).
  - **Held:** all 11 steps pass, and the no-handle output is preserved against the failed session.
  - **Failed:** any step for a reason other than the request timeout, or a lost payload.
  - **After any run that was killed or timed out,** look with `ps` for a `codex` process the run left behind, and stop it by its PID. The suite's deadline kills the path's whole process group (`tests/test_end_to_end.run_loop`, `os.killpg`). A launcher that detaches its agent with `setsid`, or into a new session, leaves that group and survives the kill. No shipped launcher does this (`issue-89/state.md`, "End-To-End Path Accepted"). Whether the real launcher does is for code reading.

  TA-6 lists what running the launch-boundary tests against it would additionally need.
- **Depends on it.** Swappability (#87). A failure is a v0.1 gap, and a contract defect if the seam itself cannot host the launcher (#90 "Full Description").
- **Owner.** #90.

### LO-7 The real launcher never calls back into the chat loop from inside its own call

- **Claim.** The real launcher does not call back into `SessionManager` from inside its own `launch`, `deliver`, `events` or `stop`.
- **Status.** `unproven`.
- **What happens if it does.** Two paths are known, carried to #90 as launcher misuse:
  - `stop_agent` called back from inside the one action's own `stop` records `terminated` and can launch a second agent;
  - a nested send on a running persistent session is **delivered**, not refused.

  Sources: control-plane `issue-88/state.md`, "Convergence Accepted" (F1) and "Carried Cleanups Delivered"; #88 comment 5691403824.
- **Why external validation cannot settle it.** No shipped launcher does it. Contract 6.1 places the behaviour outside the seam.
- **How internal dogfood settles it.** Code reading of the real launcher.
  - **Held:** it holds no reference to the loop.
  - **Failed:** it holds one, in which case #90 triages F1 and the nested-send rule.
- **Depends on it.** One-agent-per-chat and the human's Stop decision. A failure is a v0.1 gap.
- **Owner.** #90.

## Group 6. The served UI

### UI-1 The loopback shell is reachable and usable from a browser on the internal workstation

- **Claim.** `ThreadingHTTPServer` binds `127.0.0.1` on the internal workstation, and a browser there can reach and use the page.
- **Status.** `held (internal evidence)`, for #86's tree at `f519eec`.
- **Evidence.** "The UI works well" (#86 comment 5668368596; #89 comment 5668368963). This settles `decisions/0001` R2 for that tree.
- **Why external validation cannot settle it.** The internal workstation's networking, proxy and browser policy are unknown externally (R2 was a guess).
- **How internal dogfood settles it.** Re-confirm on the release tree: run `python3 dory-wrangler/run_shell.py --root ./dory-store`, open the printed URL, and send a turn.
  - **Held:** the page loads and answers.
  - **Failed:** unreachable. `--host` and `--port` exist for this. The fix is at the web front only; no durable record changes.
- **Depends on it.** The whole user experience. A failure is a v0.1 gap.
- **Owner.** #90.

### UI-2 The release's served page draws as intended in a real browser

- **Claim.** In a browser on the internal workstation, the release's page does what its source says:
  - the composer grows as text wraps, to about seven visible lines, then scrolls internally;
  - message text is shown literally, with its whitespace;
  - Enter does not submit while Send is disabled;
  - the refusal and notice wording shows as a system message, and the Abandon affordance appears after a refused send.
- **Status.** `unproven`.
- **Sources.**
  - Composer auto-grow was "not browser-verified" (control-plane `issue-88/state.md`, item N).
  - The Enter gating is "source-proven only, no browser".
  - `textContent` insertion was proven from source and served bytes (#88 comment 5800445965).
  - "Browser rendering is not verified on this host" (#89 comment 5815745216).
- **Why external validation cannot settle it.** Headless Chrome and DevTools are disallowed by host policy on the external desktop. `tests/pagemodel.py` derives rendering from the page's stylesheet and script, not from a real browser.
- **How internal dogfood settles it.** Follow README "The whole loop, end to end", "By hand", in a browser:
  - type a long prompt;
  - send a message containing markup and runs of spaces;
  - press Enter while a turn is in flight;
  - produce a turn with nothing showable (for example with `scripted-stub`).
  - **Held:** each renders as described.
  - **Failed:** any difference. Capture a screenshot.
- **Depends on it.** #86's and #88's UI criteria, and the human's composer request (#86 comment 5668368596). A failure is a v0.1 gap if it breaks use; otherwise #90 triages.
- **Owner.** #90.

### UI-3 The page loads nothing from outside the shell

- **Claim.** The served page loads no external asset: no CDN, no font host, no remote script.
- **Status.** `held (external evidence only)`. It is a claim about the product's page, not the environment.
- **Evidence.** `tests/test_shell.TestShellFlows.test_the_page_serves_with_no_external_asset` (portable), and `decisions/0001` "Standard library only". It passed in the clean run at `48f1412` (`issue-89/state.md`, "Portable Tests Accepted").
- **Why it is here.** The internal network may have no CDN or outbound access (decision 0001). This entry records that the product does not need them, so that an internal rendering failure is not blamed on a missing asset.
- **How internal dogfood settles it.** Re-confirmed by RT-2's portable run.
- **Depends on it.** UI-1 and UI-2.
- **Owner.** #90 (re-confirmation only).

## Group 7. Test apparatus

Everything in this group is about external validation itself. It tells #90 how
far the external evidence reaches, and how to read an internal failure.

### TA-1 The portable and launch-boundary categories rest on nothing the development host alone provides

- **Claim.** The 604 portable and launch-boundary tests depend on none of the following: `dev-local`, same-process or cross-process `flock` conflict, a UTF-8 locale, `/proc`, running as a non-root user, or a writable checkout. An internal failure there is therefore a product or boundary failure.
- **Status.** `held (external evidence only)`. The claim is about the tests; it makes no claim about the internal environment.
- **Evidence.** 604/604 passed under each of these perturbations:
  - dev-local unavailable;
  - `python3.12` with a Latin-1 locale;
  - a read-only checkout and a read-only `TMPDIR` parent;
  - four CPU hogs;
  - `env -i` with loopback only, as root in a user namespace;
  - `flock` emulated by `lockf`;
  - `flock` a no-op;
  - `O_EXCL` stripped.

  Sources: #89 comment 5819502876; control-plane `issue-89/state.md`, "Portable Tests Accepted".
- **Two exceptions, stated.**
  - A filesystem without POSIX `link` semantics breaks portable tests. That is a product need (FS-1), not a mislabel.
  - Heavier load than four hogs is untested (TA-10).
- **How internal dogfood uses it.** Run `python3 dory-wrangler/tests/run_tests.py --category portable --category launch-boundary --json portable.json`. A failure there that reproduces on a named re-run is a product or boundary finding (README "On an internal run", item 1).
- **Owner.** #89.

### TA-2 The suite has no per-test deadline

- **Claim.** A product hang hangs the suite instead of failing a test.
- **Status.** `known limitation`.
- **Evidence.** On a filesystem whose `link` replaces an existing name, `test_diagnostic_access.OneDefinitionOfAOneShotTurnEnd.test_a_page_that_does_not_advance_is_not_a_turn_end` hangs (the portable-labels check; #89 comment 5819502876). Only some store-lock waiters and the end-to-end path carry deadlines (control-plane `issue-88/state.md`, "In-Flight Refusal Fix").
- **How internal dogfood works around it.** Wrap internal runs in an outer `timeout` with a generous limit, for example `timeout 3600 python3 dory-wrangler/tests/run_tests.py ...`; the suite takes about 9 to 12 minutes on the external desktop. Exit 124 is a timeout. Treat it as a hang to investigate with `faulthandler` or a named re-run. Do not treat it as a pass.
- **Owner.** #89.

### TA-3 "No child process" assertions pass vacuously without `/proc` child lists

- **Claim.** `shellproc.children_of` and `e2e_loop.children_of` return `[]` when `/proc/<pid>/task/*/children` is absent. On such a host, "no child process" assertions pass without checking anything.
- **Status.** `known limitation`.
- **Evidence.**
  - The affected assertions are in development-environment tests only:
    - `test_shell.TestShellBoundaries.test_the_shell_starts_no_process`;
    - two `test_restart.TestRestartRecovery` tests;
    - `test_convergence.ASecondShellAgainstALiveOneChangesNothing.test_a_real_second_shell_against_a_live_dev_local_one`.

    Source: `issue-89/rails/issue-89-portable-labels-fix/handoff.md`.
  - Those `test_restart` tests are also the only place where store relocation across a restart is asserted (portable checkpoint review, information).
- **How internal dogfood reads it.** Check `ls /proc/self/task/*/children`. If it is absent, those assertions prove nothing on that host. If a `test_restart` test fails, confirm where the host differs before dismissing it (README item 2), because a portable reopen property sits inside it.
- **Owner.** #89.

### TA-4 Running as root skips a permission-bits test

- **Claim.** As root, `test_diagnostic_access.TheCommandLineRetrieval.test_an_unreadable_directory_is_refused_without_its_reason` skips, because a mode of 0 does not stop root reading (its own `os.geteuid() == 0` guard). It is one of the two permission-bits tests in `tests/categories.py`.
- **Status.** `known limitation`.
- **Evidence.** One root-guard skip in the user-namespace run (`issue-89/state.md`, "Portable Tests Delivered"; `issue-89-build-repeatable-portable-tests` handoff).
- **How internal dogfood reads it.** Run the suite as the ordinary user. As root, that skip is expected and proves nothing. The other permission-bits test has no root guard; a failure there as root is environmental for the same reason.
- **Owner.** #89.

### TA-5 A new test silently inherits its module's or class's category

- **Claim.** Phase 0 fails on an unassigned test or a stale entry. It cannot judge what a test does, so a new test added to a portable module is portable, whatever it depends on.
- **Status.** `known limitation`.
- **Evidence.** The portable checkpoint review and the labels fix (`issue-89-portable-labels-fix` handoff, "For the register rail").
- **How internal dogfood reads it.** When an internal portable failure names a test added after `48f1412`, check its dependencies before calling it a product failure.
- **Owner.** #89.

### TA-6 The launch-boundary tests cannot be run against #90's launcher by configuration alone

- **Claim.** Registering the internal launcher makes no launch-boundary test run against it. Those tests compare answers with the in-repo agents' fixed text.
- **Status.** `known limitation` (item D).
- **Evidence.** README "Running the launch-boundary tests against a new launcher" lists the five things that would be needed; they are deliberately not built. #89 comment 5819502876 and the portable review judged #89's launch-boundary criterion met for v0.1.
- **How internal dogfood works with it.** Exercise the real launcher through `e2e_loop.py --launcher <id>`, and match it against the shape `tests/test_internal_bridge.py` holds for the model (LO-6).
- **Owner.** #89. Building the conformance module is later work, or #90's call.

### TA-7 The `fstat` descriptor scan's run time grows with `RLIMIT_NOFILE`

- **Claim.** Two portable tests count open descriptors by calling `fstat` on every number below the soft `RLIMIT_NOFILE`. They are `test_convergence...test_a_failed_flock_leaves_nothing_held_and_leaks_no_descriptor` and `...test_a_store_lock_flock_that_fails_otherwise_leaks_nothing_and_holds_nothing`.
- **Status.** `known limitation` (L1 of the portable-labels check).
- **Measured cost.**
  - 0.39 s per scan at 262144, and about 25 minutes per scan at 1,073,741,816, which would look like a hang.
  - With an infinite limit and no fallback, the scan would check nothing and pass vacuously. That is impossible on Linux.

  Source: `issue-89/state.md`, "Portable Tests Accepted".
- **How internal dogfood reads it.** Record `ulimit -n`. At an ordinary soft limit this is irrelevant. At a very large one, expect those tests to be slow.
- **Owner.** #89.

### TA-8 The contract validator accepts a byte-copy of a session's events under a directory with no session record

- **Claim.** `validate_store.py` accepts a store in which a session's events are copied byte-for-byte under a directory that has no session record. Its export de-duplicates by content.
- **Status.** `known limitation`: a validator-coverage gap (C1 of #88's last check).
- **Evidence.** #88 comment 5811719649 ("To #89"); control-plane `issue-88/state.md`, "Diagnostic Access Accepted"; `issue-89/state.md`, "Carried Into #89".
  - The validator is final for v0.1, so this is recorded and not changed.
  - The product never writes this shape. It arises only from tampering or copying a store by hand, and `diagnostics.py` then repeats up to 9 records but never skips.
- **How internal dogfood reads it.** `validate_store.py` exit 0 does not rule out a hand-copied orphan events directory. Do not copy session directories by hand.
- **Owner.** #89.

### TA-9 Two tests are sensitive to a non-UTF-8 locale

- **Claim.** Under a Latin-1 locale:
  - `test_classification.DevLocalFramesTheWireBytesTheSameInBothProfiles.test_the_instruction_reaches_the_agent_as_it_always_did` fails 2 subtests (L4). `dev-local` writes the instruction in the pipe's locale encoding;
  - `test_diagnostic_access.TheCommandLineRetrieval.test_a_non_ascii_digit_int_reads_is_refused_as_a_limit` cannot pass its argument, because `subprocess` refuses it.

  Product behaviour is unchanged in both.
- **Status.** `known limitation`. Both tests are labelled development-environment.
- **Evidence.** #88 comment 5798973522 (L4 to #89); `issue-89/state.md`, "Carried Into #89"; `tests/categories.py`.
- **How internal dogfood reads it.** Record `locale`. Under a non-UTF-8 locale these two are environmental. The product question for the real launcher is RT-5.
- **Owner.** #89.

### TA-10 The served boundary's closed world is closed over routes, not responses; heavier load is untested

- **Claim.** Two limits on the external evidence.
  - **Route versus response.** The served-boundary tests require the route set to equal the intended routes. They do not inspect every response's content, so agent output behind a query string, or session ids behind a header, would leave the tests green.
  - **Load.** The load perturbation was one level (four hogs on four CPUs). Timing-sensitive development-environment tests may fail under heavier internal load, and this host's sandbox intermittently refuses `exec`.
    - One named case is `test_end_to_end.AHungRunLeavesNoServer.test_a_run_killed_at_its_deadline_leaves_no_process_behind` (development-environment). It samples the running processes 15 s into a 20 s deadline. Under heavy load the path may not yet have started its shell and agent by then, and the test fails with `the hung run never had a shell and an agent`. It passed 6 of 6 times in the end-to-end check (`issue-89/state.md`, "End-To-End Path Accepted").
- **Status.** `known limitation`.
- **Evidence.**
  - The route-versus-response limit was carried from #86's re-review (control-plane `issue-86/state.md`, "Re-review Outcome"), and routed "to #89/#90" by #88 (`issue-88/state.md`, "Carried Findings That Land On #88").
  - The load limit is from the portable checkpoint review, "Residual risk". The sandbox note is from `issue-88/state.md`, "Remediation Delivered".
- **How internal dogfood reads it.**
  - Route versus response is not a portability assumption. Internal dogfood cannot settle it; it is a test-coverage limit, placed here because it was routed to #89/#90.
  - For load, re-run a named test before calling it a failure (README item 3).
- **Owner.** #89.

### TA-11 `dev-local` re-issues its handles after a restart

- **Claim.** After `run_shell.py` restarts, `dev-local` issues handles from the start again. `dev-local-agent-0001` was issued twice in one store: once to the abandoned session and once to the new agent.
- **Status.** `known limitation` of the external development launcher. It is the N2 obligation observed on `dev-local`.
- **Evidence.** The end-to-end checkpoint review (`issue-89-e2e-checkpoint-review` handoff, item (c)); `issue-89/state.md`, "End-To-End Path Accepted".
- **Why it does not transfer.** `dev-local`'s agents die with the shell, so it cannot re-attach by design (the orchestrator's ruling on dev-local `persistent` after restart, `issue-89/state.md`). It is not shipped internally.
  - `dev-local` itself does **not** meet obligation 6, which requires a synthesised handle to be unique across process lifetimes. That is harmless today for one reason only: its agents die with the shell, so re-attachment fails at start-up, before any new launch can reuse a live chat's address.
  - The internal handle is Codex-issued, so obligation 6 does not bind it (TR-11 and LO-1 are the internal questions).
  - The handle reuse is recorded here and not fixed, because #89 changes no product code.
- **Owner.** #89.

## What this register supersedes, and why

`contract/v0.1/` is final for v0.1 and is not edited. Where internal evidence
has overtaken an entry in `contract/v0.1/facts-and-assumptions.md`, the
correction is made here (#89 comment 5668368963; control-plane
`issue-85/state.md`). The same applies to two sentences in `launch-boundary.md`
and one risk in `decisions/0001`. Read them through this table:

| Entry | What it says | What now holds | Where |
| --- | --- | --- | --- |
| `facts-and-assumptions.md` **U1** (and superseded **A6**) | persistent delivery through the bridge is known-unproven | **held**: persistent continuation is proven internally, and is the normal case | TR-1 |
| `facts-and-assumptions.md` **F10** | the one-shot `launch_agent.sh` path, `fresh_binding` + `one_shot`, is the one proven internal path | superseded as the *only* proven path; the script now resumes by ID; each call still returns one response | TR-1, TR-4 |
| `facts-and-assumptions.md` **U3** | the shape in which output arrives is not established | discrete JSONL events, the same on launch and resume: **held**; one response per call: **held**; an explicit end-of-turn event: still **unproven**; the declared `response_shape` is #90's design decision | TR-3, TR-4, TR-5 |
| `facts-and-assumptions.md` **A8** | output can be segmented into discrete events | **held** internally | TR-3 |
| `facts-and-assumptions.md` **A3** | a started agent can be stopped through its handle, and the handle survives a restart | the handle survives launcher processes: **held**; stop: **unproven** and assumed absent; restart survival: **unproven** | TR-8, TR-15, TR-9 |
| `facts-and-assumptions.md` **A5** | instruction text plus correlation is the whole launch input | **held** internally | TR-18 |
| `facts-and-assumptions.md` **F3** | "this development VM runs Python 3.9.25" | stale: that VM was retired on 2026-09-15; internal Python 3.9.25 is held for #86's tree; the release tree under 3.9 is unproven | RT-1, RT-2 |
| `launch-boundary.md`, "Choosing a launcher" | "Persistent multi-turn delivery is unproven internally (U1)", and `one_shot` is the default "because it is the shape of the one internal path that is proven" | persistence is proven internally; `dev-local`'s default profile is a development choice, not a claim about the bridge | TR-1 |
| `decisions/0001` **R1** | the platform interpreter is present on the internal host (a guess) | **held** (internal evidence) | RT-1 |
| `decisions/0001` **R2** | loopback HTTP is reachable from a browser (a guess) | **held** for #86's tree | UI-1 |

Every other entry in `facts-and-assumptions.md` still stands as written, and is
carried here where it is an assumption. See Appendix A.

## Counts

| Group | held (internal evidence) | held (external evidence only) | unproven | known limitation | total |
| --- | --- | --- | --- | --- | --- |
| 1. Runtime and platform (RT) | 1 | 0 | 5 | 0 | 6 |
| 2. Filesystem and store (FS) | 0 | 0 | 3 | 0 | 3 |
| 3. Process and launch mechanics (PR) | 0 | 0 | 3 | 0 | 3 |
| 4. Internal transport and event stream (TR) | 7 | 0 | 13 | 0 | 20 |
| 5. Launcher obligations (LO) | 0 | 0 | 6 | 1 | 7 |
| 6. Served UI (UI) | 1 | 1 | 1 | 0 | 3 |
| 7. Test apparatus (TA) | 0 | 1 | 0 | 10 | 11 |
| **Total** | **9** | **2** | **31** | **11** | **53** |

There are 19 entries marked [human-required]: RT-1, PR-1, PR-2 and TR-1 to
TR-16. The owners are #90 for the 42 entries in groups 1 to 6, and #89 for the
11 in group 7.

## Appendix A. Traceability

Every source item the rail names, and where it went. An item that is not a
portability assumption says why. "Fixed" cites the accepted commit or state
section.

### GitHub #89 (human direction and acceptance records)

| Item | Placed |
| --- | --- |
| #89 description: timing is among the things external validation cannot prove | TR-20: a real turn's duration against the synchronous send and `e2e_loop.py --request-timeout`. Added after checkpoint review F3 |
| 5648269596: continuation capability, required unproven | TR-1 (since held; superseded explicitly by 5668368963) |
| 5648269596: instruction-payload bound, no bound stated | TR-2 |
| 5648269596: response shape: discrete events, and end distinguishable from quiet | TR-3, TR-5 |
| 5668368963 held: persistent continuation | TR-1 |
| 5668368963 held: one response per call (`one_shot`) | TR-4, with the two readings recorded |
| 5668368963 held: Rocky 9 / Python 3.9.25 runs #86; 59 at 19/40 | RT-1 |
| 5668368963: correct the stale continuation entry in `facts-and-assumptions.md` here | "What this register supersedes" |
| 5668368963 unproven: how `launch_agent.sh` separates the response from the resume ID | superseded as an open question by 5669592360: the output is Codex JSONL and the handle is `thread.started.thread_id` (TR-3, TR-6). The redacted capture that is still wanted is in TR-3 and TR-12 |
| 5668368963 unproven: resume after a Dory-wrangler, bridge, VS Code or VM restart | TR-9 |
| 5668368963 unproven: ID lifetime and invalid-ID behaviour | TR-10 |
| 5668368963 unproven: concurrent resumes and concurrent agents | PR-2 |
| 5668368963 unproven: any stop or terminate operation (assume none) | TR-15 |
| 5668368963 unproven: any payload bound | TR-2 |
| 5668368963 unproven: partial-output, error and prerequisite-failure shapes | TR-14, PR-1 |
| 5668368963 unproven: continuity beyond one nonce | TR-16 |
| 5668684450 held: the handle is `thread_id` from `codex exec --json` | TR-6 |
| 5668684450 unproven: Codex CLI version | TR-12 |
| 5668684450 unproven: one JSON document or JSONL, and event types | JSONL held (TR-3), per 5669592360; event types TR-12 |
| 5668684450 unproven: which event carries `thread_id` and which the response | held (TR-6, TR-7), per 5669592360 |
| 5668684450 unproven: whether `launch_agent.sh` exposes the raw stream (bears on `response_shape`) | the raw stream is available (#90 5669593061); `response_shape` is #90's decision (TR-4) |
| 5668684450 unproven: uniqueness, lifetime and restart survival of `thread_id` | TR-11, TR-10, TR-9 |
| 5669592360 held: fresh launch and resume emit the same JSONL | TR-3 |
| 5669592360 held: handle `thread.started.thread_id` | TR-6 |
| 5669592360 held: reply `item.completed`/`agent_message`/`item.text`, same on resume | TR-7 |
| 5669592360 held: `thread_id` resumes across launcher processes | TR-8 |
| 5669592360 unproven: CLI version, other event types, zero or several `agent_message`, end-of-turn event, error shapes, stop, lifetime and restart survival, concurrency, payload bound | TR-12, TR-13, TR-5, TR-14, TR-15, TR-10, TR-9, PR-2, TR-2 |
| 5815745216: handle reuse across a restart, Python 3.9 execution, browser rendering go to the register | TA-11 (and TR-11/LO-1 for the internal handle), RT-2, UI-2 |
| 5815745216: the unpinned read-back after the third turn | not a portability assumption; fixed (pinned at `4830a68`, accepted at `48f1412`) |
| 5819502876: POSIX hard-link semantics; a replacing `link` hangs the drain | FS-1, TA-2 |
| 5819502876: `flock` on the internal store filesystem | FS-2 |
| 5819502876: no per-test deadline | TA-2 |
| 5819502876: Python 3.9 execution | RT-2 |
| 5819502876: item D, launch-boundary tests not runnable against #90's launcher by configuration | TA-6 |

### GitHub #90

| Item | Placed |
| --- | --- |
| 5648267936 test 1: continuation capability | TR-1 |
| 5648267936 test 2: instruction-payload bound and the behaviour at it | TR-2 |
| 5648267936 test 3: discrete events; end distinguishable from quiet; concurrent agents' output attributable | TR-3, TR-5, PR-2 |
| 5648267936: packet composition under `fresh_binding` is a product decision from these results | not an assumption; an open product decision, dependent on TR-1 and TR-2 (noted in both) |
| 5668369261: early evidence (persistent, `one_shot`, #86 on Rocky 9 / 3.9.25) and the still-unproven list | TR-1, TR-4, RT-1; the list is as in #89 5668368963 above |
| 5668684764: handle is `thread_id`; obligation 6 does not apply; version unknown; format unknown; `launch_agent.sh` presentation; the Codex command behind `--resumeID` | TR-6, LO-1, TR-12, TR-3, TR-4; the command is given by 5669593061 (TR-8) |
| 5669593061: resume shape, JSONL, handle, reply, directive (raw JSONL for both, one path), contract mapping, survival across processes, still-unproven list | TR-3, TR-6, TR-7, TR-8, LO-3; the still-unproven list is as for #89 5669592360 |
| #90 acceptance: every register entry recorded internally as held, failed or untested | "How #90 records a result" |

### GitHub #81, #86, #87, #88

| Item | Placed |
| --- | --- |
| #81 5653447749 / 5670359711: internal compatibility baseline | TR-1, TR-3, TR-6, TR-7, RT-1 |
| #81 5653447749: contract P1 versus a one-shot `stream_end` payload | not an assumption; decided by the human on 2026-09-15 and implemented (`7fab5c5`, contract 7 P2 wording at `532520f`) |
| #81 5687495740: Stop does not reach a turn in flight; D1; D2; refuse-not-queue | not assumptions; confirmed human decisions (product properties). D1 rests on FS-2; TR-15 notes Stop |
| #86 5668368596: #86 runs unchanged; R1 settled; UI works well; composer finding | RT-1, UI-1, UI-2 |
| #86 5668368596: not yet run internally: #87's suite, #88's product, the store's `link`/`fsync`/`flock` behaviour | RT-2, FS-1, FS-2, FS-3 |
| #87 5668368252: persistent proven; contract mapping; still-unproven list | TR-1, TR-4, TR-18, and the #89 list above |
| #87 5668684014 / 5669591294: handle source, transport, directive, still-unproven list | TR-3, TR-6, TR-7, TR-8, TR-18, LO-1, LO-3; the list as for #89 5669592360 |
| #88 5668684239 / 5669591807: preserve raw first; classify only observed types; `response_shape` open; zero or several items; A6 rule | TR-4, TR-12, TR-13, LO-3; A6 fixed (decision 0005, `0315019`) |
| #88 5670341638 / 5687110513: convergence-remediation mutation sweeps | read; mutation definitions and adjudications only; no item routed to #89/#90 or to the register |
| #88 5691403824: nested `stop_agent` to #90 as launcher misuse | LO-7 |
| #88 5702152189: two named non-preservations; `internal_error` over-counted | not portability assumptions; the returned mis-packed case was fixed in `1591a1d` (item D, category `unavailable`, prefix kept); the sequence-gap exception is contract P3, named in the README |
| #88 5798973522: L4 Latin-1 test to #89; `dev-local` stderr to #90; Codex declaration and `\n` framing for #90's launcher | TA-9, TR-14 (stderr is now named in the README), LO-3 |
| #88 5800445965: the gate skips unlisted launchers | fixed: the gate fails closed (`1591a1d`); LO-3 requires the classifier |
| #88 5805392291: one-shot turn end decided differently by drain and re-attachment | fixed (`0b08c6f`, accepted at `ac5d7cd`); LO-4 is the residual obligation |
| #88 5811719649 to #89: `validate_store.py` accepts a byte-copy under an orphan directory | TA-8 |
| #88 5811719649 to #90: paging stream launcher false notice; one-shot output only from returned calls; `\n` framing; classifier | LO-5, LO-4, LO-3 |
| #88 5811719649 residuals: a walk misses sessions created mid-walk (C2); an unreadable events directory exits 0 (C3); unpinned refusal details (C4) | not portability assumptions; diagnostic-tool residuals named in decision 0007 and state, carried as low |

### Control-plane state, #85 to #89

| Item | Placed |
| --- | --- |
| #85 "Internal launch path (2026-09-12)": one-shot proven; persistent, bound and packet composition not established | superseded (TR-1); TR-2; packet composition as under #90 |
| #85 "Internal Compatibility Findings": the whole list | TR-1, TR-4, RT-1, and the still-unproven list as under #89 |
| #85: `facts-and-assumptions.md` continuation entry stale; correct it in #89's register | "What this register supersedes" |
| #85 F10 (timestamp and sequence coherence unchecked) | not a portability assumption; a contract-level carried finding (it gates per-turn pairing). The environmental side, a clock stepping back, is RT-6 |
| #85 F7 to F14, R5 to R7, the `TURN_INSTRUCTION_MISSING` counting floor, C1 residuals | not portability assumptions; contract residuals accepted or carried in #85 state, not routed to the register |
| #86 stack decision: CPython 3.9 from the platform RPM, and "this fact belongs in #89's" register | RT-1 |
| #86 five stack risks (decision 0001 R1 to R5; R6 is a fact) | R1: RT-1; R2: UI-1; R3: FS-1, FS-2, FS-3; R4: RT-4; R5: RT-3; R6: not an assumption (the reason entries are commands) |
| #86: `fsync` to stable storage unestablished | FS-3 |
| #86: route-versus-response boundary limit | TA-10 |
| #86: the store's duplicated contract constant (`ADDRESSING_OBSERVATION_KINDS`) | not a portability assumption; a drift risk between two copies in the tree. `tests/test_addressing.py` asserts that the store's and validator's sets are equal |
| #86: D1 (`read_diagnostic_events` limit validation untested) and D2/G16 (the `DIAGNOSTIC_PAGE_MAX` bound untested) | not portability assumptions; untested-guard items carried to #90's triage list. `tests/test_diagnostic_access.py` now exercises the bound. This register makes no claim that either is pinned by mutation |
| #86: D6, X14 | not portability assumptions; #88 records D6 as holding and X14 as pinned (`issue-88/state.md`, "Diagnostic Access Delivered") |
| #86: A6, A7/R6, B7, H1 | A6 fixed (decision 0005); A7/R6: system text is fixed words, refused otherwise (decision 0006); B7: not a portability assumption, a store/validator coverage item of the same family as TA-8; H1 fixed (`7afe8df`) |
| #86: the `PRECONDITION_NOT_MET` and `UNKNOWN_INFERRED_WITHOUT_EVIDENCE` pinned residuals | not portability assumptions; pinned residuals of the store |
| #87: internal-bridge accommodations (synthesise handle, buffer, `session_completed`, `after_sequence`, registry) | superseded by the JSONL transport (handle issued: TR-6, LO-1); `after_sequence` and registry: LO-6 |
| #87 product properties: Stop cannot reach a turn in flight; the agent has no memory; a dead bridge is found only by attempting a turn; restart with a live session needs Abandon | decided (human, 2026-09-15); "no memory" superseded by persistent continuation (TR-1); dead bridge: PR-1, TR-14; Abandon: D2 |
| #87 N2: synthesised handle uniqueness across restarts | LO-1, TR-11 (internal); TA-11 (`dev-local`) |
| #87: F3, F6, R4 and the `deliver`-then-blocking-drain hang, carried to #90 | not portability assumptions. F3 (`open_harness(launcher=...)` skipping `launcher_id` validation): #88 now pre-flights the whole would-be session record before any durable write (`issue-88/state.md`, "Decisions After Review"; R4 closed at `aa7096d`). F6 (whitespace-only turns accepted) is product behaviour. #87's R4 (an agent-sourced `stream_end` refused with nothing durable written): launcher-misuse evidence; this register does not claim it closed, and it stays on #90's triage list. The drain hang needs an inconsistent launcher: LO-6 |
| #87: a mis-addressed `deliver` deadlocks `dev-local` on a real process | not a portability assumption; `dev-local` only, which is not shipped internally; stays on #90's triage list |
| #87 "Internal Transport Established": the whole section, including the still-unproven list | TR-3 to TR-16, TR-18 |
| #88 "Carried to #89/#90": route-versus-response, N2, duplicated constant, D1, D2/G16 | TA-10; LO-1/TR-11/TA-11; constant, D1 and D2/G16 as above |
| #88: the dev_local OSError comment (inactive user session, uninitialised bridge are `unavailable`) left unverified for #90 | PR-1 |
| #88: nested send delivered on a running persistent session; nested `stop_agent`; `stop_agent` lacking `abandon`'s nested guard (F1) | LO-7 |
| #88 C1 validator coverage (to #89) | TA-8 |
| #88 L4 (to #89) | TA-9 |
| #88 G3; the one-shot "output only from returned calls" obligation; `\n`-only framing; classifier registration | LO-5, LO-4, LO-3, LO-3 |
| #88: N3 (a launcher's durable spool; how long a launcher can address its thread) | LO-2 |
| #88: `dev-local` stderr not preserved (L5) | not an internal assumption; named in the README; the real launcher's stderr is TR-14 |
| #88: N4 to N7, F4 probe launcher ids, the crash-window residual, C2 to C4 | not portability assumptions; recorded low product or test residuals |
| #88 desktop facts: no 3.9; sandbox `EPERM`; headless Chrome disallowed | RT-2; TA-10; UI-2 |
| #88 "Internal Transport -- Consequences": response shape chosen on evidence; the P1 `stream_end` tension may not arise internally | TR-4; the tension was decided by the human and implemented |
| #89 "The Development Environment, As It Now Is": 3.9 not provable externally; the human could install `python39` | RT-2 (the stronger external evidence is available only through that human action) |
| #89 "Carried Into #89": C1, L4, the "#89/#90" items, the stale contract record, the GitHub #89 entries | TA-8, TA-9, as above, "What this register supersedes", and the #89 table |
| #89 end-to-end: N2 handle reuse (`dev-local-agent-0001` twice) | TA-11 |
| #89 end-to-end: the stale `test_restart` docstring | not an assumption; fixed at `b216430` (accepted at `48f1412`) |
| #89 end-to-end: README `python3` is 3.6 on the desktop | not an internal assumption; internally `python3` is 3.9.25 (RT-1); the README's Testing section already says to use `python3.11` on the desktop |
| #89 end-to-end: browser rendering unverified; Python 3.9 execution unproven | UI-2; RT-2 |
| #89 end-to-end accepted: a future launcher detaching with `setsid` would escape the group kill | not an assumption about the host. It is a property of #90's launcher under the suite's deadline, which kills the path's process group. LO-6 says to look for a `codex` process left behind after a killed run, and to stop it by PID |
| #89 end-to-end accepted: the hung-run test samples at 15 s of a 20 s deadline and could flake under heavy load | TA-10 (load), naming `test_end_to_end.AHungRunLeavesNoServer.test_a_run_killed_at_its_deadline_leaves_no_process_behind` |
| #89 end-to-end: the persistent-restart ruling (dev-local cannot re-attach) | not a portability assumption; a `dev-local` design property ruled as completing the loop; the internal question is TR-9 |
| #89 portable: locale reaches the test apparatus (argv) | TA-9; the product side is RT-5 |
| #89 portable: `flock`/exclusive creation isolated to 22 named tests | FS-2 |
| #89 portable: running as root skips the permission-bits test | TA-4 |
| #89 portable review: `children_of` vacuity; store relocation asserted only in development-environment tests | TA-3 |
| #89 portable review: new tests inherit categories | TA-5 |
| #89 portable review: heavier internal load untested | TA-10 |
| #89 labels check: hard links absent; `link` replacing an existing name hangs the drain; no per-test deadline; no-op `flock` | FS-1, TA-2, FS-2 |
| #89 labels check: L1, the `fstat` scan and `RLIMIT_NOFILE` | TA-7 |
| #89 labels check: `O_EXCL` stripped is inert (names are claimed with `os.link`) | not an assumption; recorded in FS-1's rationale (the claim is on `link`, not `O_EXCL`) |

## Appendix B. The contract's own records, entry by entry

### `contract/v0.1/facts-and-assumptions.md`

| Entry | Placed |
| --- | --- |
| F1 no Dory-wrangler source existed before #85 | not an assumption; history |
| F2 no internal bridge or launcher in this repository | still true; the reason group 5 is unproven |
| F3 the development VM runs Python 3.9.25; no `pytest` | superseded (VM retired); RT-1, RT-2; stdlib `unittest` needs no framework |
| F4 external development, internal authority, one nightly mirror | fact; the reason this register exists |
| F5 launch differs between environments | fact; refined by TR-1 to TR-18 |
| F6 one agent per chat; an exhausted single agent is expected | fact; noted in TR-16 |
| F7 raw output preserved even when parsing fails | a product rule (contract 7 P1), implemented and gated; LO-3 is the real launcher's side |
| F8 active user session and initialised bridge are prerequisites | PR-1 |
| F9 token counting not required | not a portability assumption |
| F10 the one-shot script is the one proven path | superseded; TR-1, TR-4 |
| U1 continuation | held; TR-1 |
| U2 payload bound | TR-2 |
| U3 output shape | TR-3, TR-4, TR-5 |
| A1 launch outcome distinguishable | TR-17 |
| A2 distinguishable end-of-stream; reader failure versus stream close | TR-5 |
| A3 stop through the handle; handle survives restart | TR-15, TR-9 (the process-boundary half is held: TR-8) |
| A4 superseded by U2 | TR-2 |
| A5 instruction text plus correlation is the whole input | TR-18 |
| A6 superseded by U1 | TR-1 |
| A7 concurrent agents' events attributable | PR-2 |
| A8 output segmentable into discrete events | held; TR-3 |
| A9 plain text adequate | TR-19 |
| A10 retry after an ambiguous launch is safe as a new session | PR-3 |
| Pressure points toward #83, #82 and #84 | not assumptions; design tensions deliberately left to later releases |

### `launch-boundary.md`, "Adding the internal bridge launcher"

| Obligation | Placed |
| --- | --- |
| 1 a module implementing `LaunchBoundary` | structural; exercised by LO-6 |
| 2 declares its own capabilities, `null` bound | TR-4 (shape), TR-1 (continuation), TR-2 (bound) |
| 3 maps its transport onto the payload vocabulary | LO-3 |
| 4 maps its failures onto the five categories (`unavailable` for the prerequisites) | PR-1, TR-14 |
| 5 handle from the transport | LO-1, TR-6 |
| 6 a synthesised handle unique across process lifetimes | LO-1; TA-11 for `dev-local` |
| 7 keeps each call's output until served; how long is unspecified | LO-2, TR-10 |
| 8 one classification path; declared set; `\n`-only bytes | LO-3 |
| 9 under `fresh_binding`, reports the agent's exit | LO-6 |
| 10 honours `after_sequence` | LO-6 |
| 11 carries back the output of a no-handle launch | LO-6, TR-17 |
| 12 one registry entry | LO-6, TA-6 |
| The model's product properties (Stop cannot reach a turn in flight; no confirmed stop; dead bridge found by attempting; restart without output ends in `unknown`) | decided or carried: TR-15, PR-1, TR-9 |

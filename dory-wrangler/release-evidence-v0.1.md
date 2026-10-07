# Dory-wrangler v0.1 release evidence

#89 checkpoint `produce-mirror-ready-release-evidence`. This is the evidence and
the instructions internal dogfood (#90) needs to start on the internal Rocky
Linux 9 network the morning after the mirror, without GitHub, the control plane,
or any conversation. It records:

- the exact commit that was tested;
- every command that was run on it, and what each printed;
- the procedure to follow internally, in order;
- what to bring back, in a form #90 can reconcile.

It is evidence, not a compatibility verdict. The internal Rocky Linux 9
environment is the compatibility authority (#89 "Full Description").

## 1. Identity: which tree was tested

| What | Value |
| --- | --- |
| Release-candidate commit (the tree tested) | `e591ced642900d1204e5b6b9a9627fc1b5e85aea` |
| Branch | `dory-wrangler/issue-89` on `jmrozi1/ai-dev` |
| Root tree of the commit | `961bae0db5feca711350219f18aeff13ad40f747` |
| Tree of `dory-wrangler/` | `c7e097606d31bd51e2d99669ce3461b81e87f72e` |
| Tree of `dory-wrangler/contract` | `97037d35e38b36930b0428e45bf187b8fdcd18e5` |
| Tree of `dory-wrangler/fixtures` | `b2d3667a8be1631549d0842fee9c8bc6bf6e37f7` |
| Tree of `dory-wrangler/validator` | `b1478bac9b4d329687066a126e2286a01cb92185` |
| Tree of `dory-wrangler/src` | `201a0ba3b2914422b4edd59ce96d0777615f84ac` |
| Tree of `dory-wrangler/tests` | `dc59af43bae86b28968fe145a3b4226b38ed1284` |

The contract, fixtures and validator trees are identical to those on branch
`dory-wrangler/issue-85` (#85's accepted contract with its editorial corrections,
`2625906`), so the release names one contract.

**Every later commit on this branch touches only this document and one row of
the README's Contents table.** The commit that added them sits directly on the
release candidate. One command confirms that the code you run is the code that
was tested:

```
git diff --name-only e591ced642900d1204e5b6b9a9627fc1b5e85aea <the branch head you have>
```

It must print exactly these two lines:

```
dory-wrangler/README.md
dory-wrangler/release-evidence-v0.1.md
```

Any other path means the tree you have is not the tree tested: stop, and report
that output (section 3, step 1).

The `dory-wrangler/` tree at the branch head differs from the one above only by
this document and that README row. The `src`, `tests`, `contract`, `fixtures`
and `validator` trees are the same at both commits.

No merge, pull request or promotion is part of this release. #85 to #89 stay on
their own branches, unmerged, by human direction. Nothing here changes `main`.

## 2. External results

Every command below ran on 2026-09-24 against a clean `git archive` of the
release candidate, unpacked into a directory outside any clone, with
`python3.11` standing in for the internal `python3` and a fresh, empty
`TMPDIR` per run. No run was retried, and no test was re-run.

### The host

| Fact | Value |
| --- | --- |
| Operating system | Red Hat Enterprise Linux 8.10 (Ootpa) |
| Kernel | Linux 4.18.0-553.157.1.el8_10.x86_64, x86_64 |
| Interpreter used | Python 3.11.13 (`python3.11`). Also present: `python3` 3.6.8 (unusable), `python3.12` 3.12.14. **No Python 3.9.** |
| Filesystem of the checkout and of `TMPDIR` | xfs, local (`/dev/nvme0n1p3`, `rw,relatime,seclabel`) |
| Locale | `LANG=en_US.utf8`, `LC_ALL` unset; file system encoding `utf-8` |
| Cores | 4 |
| `ulimit -n` | 262144 |
| User | an ordinary user, not root |

This desktop is #89's Linux development environment. The RHEL 9.8 development
VM, which had the platform `python3-3.9.25`, was retired on 2026-09-15. On-access
virus scanning makes the suite I/O-bound here.

### The commands and what they printed

| Command | Result | Wall time |
| --- | --- | --- |
| `python3.11 dory-wrangler/tests/run_tests.py --json full.json` | exit 0. Phase 0: `portable 439; launch-boundary 165; development-environment 95; 699 test(s) in all`. Phase 1: `Ran 699 tests in 573.000s`, `OK`; each category all passed, 0 failed, 0 errors, 0 skipped. Phase 2: `255 fixture(s): 255 accepted as valid, 0 rejected for the stated reason, 0 mismatched`. Phase 3: `not reproduced: 0`, `adjudicated by name: 1; unadjudicated: 0`, `rendering disagreements: 0; adjudicated by name: 0; unadjudicated: 0`, `unadjudicated sessions: 0`, `system messages (decision 0006): disagreements: 0`. Phase 4: `59 fixture(s): 19 accepted as valid, 40 rejected for the stated reason, 0 mismatched` | 9 min 37 s |
| `python3.11 dory-wrangler/tests/run_tests.py --category portable --json portable.json` | exit 0. `portable 439 test(s): 439 passed, 0 failed, 0 error(s), 0 skipped`; 123 kept stores all accepted; phase 3 0 not reproduced, 0 unadjudicated; fixtures 59 at 19/40/0 | 5 min 36 s |
| `python3.11 dory-wrangler/tests/run_tests.py --category launch-boundary --json launch-boundary.json` | exit 0. `launch-boundary 165 test(s): 165 passed, 0 failed, 0 error(s), 0 skipped`; 68 kept stores all accepted; phase 3 clean; fixtures 59 at 19/40/0 | 59 s |
| `python3.11 dory-wrangler/tests/run_tests.py --category development-environment --json development-environment.json` | exit 0. `development-environment 95 test(s): 95 passed, 0 failed, 0 error(s), 0 skipped`; 64 kept stores all accepted; phase 3 clean; fixtures 59 at 19/40/0 | 2 min 57 s |
| `python3.11 dory-wrangler/tests/e2e_loop.py --launcher dev-local --launcher-options '{"profile": "one_shot"}'` | exit 0, `e2e: all 11 steps held`. `continue` and `third-turn` each answered by a newly launched agent, as `fresh_binding` declares; the reopened transcript byte-identical (862 bytes) | 3 s |
| `python3.11 dory-wrangler/tests/e2e_loop.py --launcher dev-local --launcher-options '{"profile": "persistent"}'` | exit 0, all 11 steps held. `continue`: the same agent session. `third-turn`: `a newly launched agent answered it, [...]; the restart could not re-attach the agent, so the refused turn was sent again after the one action, abandon` -- `dev-local`'s agent dies with its shell, by design | 3 s |
| `python3.11 dory-wrangler/tests/e2e_loop.py --launcher scripted-stub` | exit 0, all 11 steps held, `fresh_binding` | 3 s |
| `python3.11 dory-wrangler/tests/e2e_loop.py --codex-model` | exit 0, all 11 steps held. `continue`: `you first said: hello`. `third-turn`: `the agent live before the restart was re-attached and answered it, ["you first said: hello"]` | 2 s |
| `python3.11 dory-wrangler/validate_store.py <the --codex-model loop's store>` | exit 0: `20 record(s): agent_binding 1, agent_session 1, chat 1, delivery_request 2, diagnostic_event 6, launch_request 1, launch_result 1, message 6, session_observation 1`, `the store satisfies contract v0.1` | 0.1 s |
| `python3.11 dory-wrangler/validator/validate_contract.py dory-wrangler/fixtures/v0.1` | exit 0: `59 fixture(s): 19 accepted as valid, 40 rejected for the stated reason, 0 mismatched` | 1 s |
| every `.py` under `dory-wrangler/` parsed with `ast.parse(source, feature_version=(3, 9))` | `parsed 62 .py file(s) at feature_version=(3, 9); 0 failed` -- every tracked `.py` file | 0.4 s |

The one record phase 3 adjudicates by name is a test probe's deliberate
`exit_report`, named with its reason in `tests/reclassify_stores.ADJUDICATED`.
The three launcher ids phase 3 does not read (`intake-probe`, `out-of-tree`,
`replaying`) are test probes, named with reasons in
`ADJUDICATED_LAUNCHERS`. No `run_shell.py`, `model_shell.py` or agent process
was left running after the runs.

The four loops' step lines are in Appendix A, verbatim. An internal run should
print the same lines, apart from times, paths and ids.

The same runs, and item A's LO-3 runs, were made on this commit for the
assumption register's LO-3 wording: a `--codex-model` store reads **held**; a
copy with two `DUPLICATE_ID` violations reads **failed** at `validate_store.py`
exit 1; a store whose resumed reply was preserved `malformed` and never
rendered reads **failed** at step 4 (`user agent user system`). See the
register, LO-3, "Run again with steps 2 and 4".

### What was not proven externally

Each of these is an entry in the register, with the procedure that settles it
internally:

| Not proven externally | Why | Register |
| --- | --- | --- |
| The release tree executing under Python 3.9 | this host has no 3.9; only syntax was checked at 3.9. The one internal run so far was #86's tree alone, 225 tests on Rocky 9 / Python 3.9.25 | RT-2 (RT-1 for #86's tree) |
| The internal store filesystem's `link`, `flock`, `fsync` and `rename` | external runs used local xfs | FS-1, FS-2, FS-3 |
| SELinux on the store path and the loopback bind; the internal locale; the clock | internal host configuration | RT-4, RT-5, RT-6 |
| Rendering in a real browser | no browser here; headless Chrome is disallowed by host policy. The page's rendering is derived from its own stylesheet and script by `tests/pagemodel.py` | UI-2 (UI-1 held for #86's tree) |
| The real internal launcher, and everything about the real transport beyond the four held facts | it does not exist yet (#90 `implement-internal-bridge-launcher`); `--codex-model` is a model of the proven transport, not the internal CLI | group 4 (TR-1 to TR-20) and group 5 (LO-1 to LO-7); PR-1 to PR-3 |
| The launch-boundary tests run against #90's launcher | they compare with the in-repo agents' fixed answers; registering a launcher does not make them run against it | TA-6 |

## 3. The morning procedure, on the internal network

Follow the steps in order. Each step says what to run, what a good result looks
like, and what to keep. Everything you keep goes into one results directory,
described in step 6.

- **Interpreter.** Every command uses `python3`, which is Python 3.9.25 on the
  internal Rocky Linux 9 host (register RT-1). The external runs in section 2
  used `python3.11` in its place, because the external desktop has no 3.9.
- **Where to run.** Run from a `git archive` of the branch head (step 1), never
  from a working clone, so that nothing left over in a clone is tested.
- **`TMPDIR`.** Give every suite run a fresh, empty `TMPDIR`, on the same
  filesystem the dogfood store will live on. The tests put their stores under
  `TMPDIR`, so it also decides which filesystem is tested (register FS-1, FS-2).
- **The user.** Run as your ordinary user, not root (register TA-4).
- **Changing nothing.** No step here edits, patches, merges or copies code into
  the tree. If a step seems to need that, stop and report what you saw. #89
  forbids any speculative compatibility fix before internal evidence exists.
- **Commands marked *internal-only*** need something that exists only on the
  internal network: `codex`, `~/scripts/launch_agent.sh`, the bridge, a browser
  on the internal workstation, or #90's internal launcher. They were not run
  externally. Where a tool exists externally, its arguments were checked there.

The examples use two shell variables. Set them first:

```
RC=e591ced642900d1204e5b6b9a9627fc1b5e85aea
DW=~/dw-dogfood-$(date +%Y%m%d)        # one directory for this morning's work
mkdir -p $DW/results $DW/tmp
```

### Step 1. Confirm the exact commit is on the mirror, and unpack it

In your internal clone of `ai-dev`, fetch as you normally fetch the nightly
mirror. `MIRROR` below is the name of the remote that fetches it, often
`origin`.

```
MIRROR=origin
git fetch $MIRROR
git ls-remote $MIRROR 'refs/heads/dory-wrangler/*'   > $DW/results/mirror.txt
git cat-file -t $RC                                  >> $DW/results/mirror.txt 2>&1
```

- **Present.** `mirror.txt` has a line ending `refs/heads/dory-wrangler/issue-89`,
  and `git cat-file -t` printed `commit`. Continue:

  ```
  HEAD_SHA=$(git rev-parse $MIRROR/dory-wrangler/issue-89)
  {
    echo "head $HEAD_SHA"
    git merge-base --is-ancestor $RC $HEAD_SHA && echo "the release candidate is an ancestor of the head"
    echo "changed since the release candidate:"; git diff --name-only $RC $HEAD_SHA
    for p in src tests contract fixtures validator; do
      echo "dory-wrangler/$p $(git rev-parse $HEAD_SHA:dory-wrangler/$p)"
    done
  } >> $DW/results/mirror.txt
  cat $DW/results/mirror.txt
  ```

  Check that the ancestor line is printed, that the only paths changed since
  the release candidate are `dory-wrangler/README.md` and
  `dory-wrangler/release-evidence-v0.1.md`, and that
  the five tree hashes equal those in section 1. Then unpack the head and
  work only there:

  ```
  mkdir -p $DW/tree && git archive $HEAD_SHA | tar -x -C $DW/tree
  cd $DW/tree
  ```

- **Not present.** It is **not established** that the nightly mirror carries
  branches other than `main`. #85 to #89 are deliberately unmerged, and no
  record says outright that their branches are mirrored. If `mirror.txt` has no
  `dory-wrangler/issue-89` line, or its last line is anything other than
  `commit` (git 2.x prints, for example,
  `fatal: git cat-file: could not get object info`), **stop here**. (If you are reading this document from the mirrored
  tree, the branch has arrived. This case matters when these instructions
  reached you another way, or when the head or the hashes differ.)
  - **The one fact to report back:** whether `refs/heads/dory-wrangler/issue-89`
    reached the internal mirror, and at which SHA. That is the contents of
    `mirror.txt`.
  - Do not work around it. Do not cherry-pick, merge into `main`, copy files
    from another checkout, or retype anything: each of those produces a tree
    that is not the one tested. Getting the branch mirrored is a human decision
    outside this procedure.
  - Any other path in the `git diff` list, or tree hashes that differ from
    section 1, is the same stop. Report `mirror.txt`.

### Step 2. Record the interpreter and host facts

From `$DW/tree`:

```
{
  cat /etc/os-release; uname -srvmo
  command -v python3; python3 --version; rpm -q python3
  python3 -c 'import sys; print(sys.version); print("fsencoding", sys.getfilesystemencoding())'
  locale; nproc; ulimit -n; id -u
  findmnt -T $DW; stat -f -c 'filesystem %T' $DW
  ls /proc/$$/task/*/children
  timedatectl
} > $DW/results/host.txt 2>&1
```

- `python3 --version` must be 3.9.x, from the platform RPM (RT-1). If it is
  missing or older than 3.9, stop and report `host.txt`: nothing else here can
  run (decision 0001, R1).
- `findmnt` gives the store filesystem's type for FS-1 and FS-3. `id -u` should
  not be 0 (TA-4). `ls /proc/$$/task/*/children` failing means the "no child
  process" checks prove nothing on this host (TA-3). **Use this form, not the
  register's.** TA-3 in the register writes `ls /proc/self/task/*/children`,
  which reports the files absent even where they exist: the shell expands the
  glob for its own process, and `ls`, a different process, then looks under its
  own `/proc/self`. On the external desktop that form printed `No such file or
  directory` while `/proc/$$/task/$$/children` listed the shell's child. The
  register is part of the tested tree, so it is corrected here rather than
  edited. A non-UTF-8 `locale`
  explains two development-environment tests (TA-9). A very large `ulimit -n`
  makes two portable tests slow (TA-7).

### Step 3. Run the test categories, portable first

From `$DW/tree`, one category at a time, each with a fresh `TMPDIR`, and each
wrapped in `timeout` because the suite has no per-test deadline (TA-2). Then the
whole suite, which is what RT-2 asks for.

```
for c in portable launch-boundary development-environment; do
  export TMPDIR=$DW/tmp/$c; mkdir -p $TMPDIR
  ( time timeout 3600 python3 dory-wrangler/tests/run_tests.py --category $c \
      --json $DW/results/$c.json ) > $DW/results/$c.log 2>&1
  echo "exit $?" >> $DW/results/$c.log
done
export TMPDIR=$DW/tmp/full; mkdir -p $TMPDIR
( time timeout 3600 python3 dory-wrangler/tests/run_tests.py \
    --json $DW/results/full.json ) > $DW/results/full.log 2>&1
echo "exit $?" >> $DW/results/full.log
```

What each should print is in section 2: the same counts and `exit 0`. How to
read anything else:

- **Exit 124** is `timeout` stopping a hang, not a result. The known hang is a
  filesystem whose `link` replaces an existing name (FS-1). Record it; never
  read it as a pass.
- **A failure under `portable` or `launch-boundary`** is a product or boundary
  failure on this host (README, "On an internal run", item 1). These categories
  passed externally with the development launcher removed, under a non-UTF-8
  locale, with `flock` emulated or disabled, as root, and from a read-only
  checkout (TA-1).
- **A failure under `development-environment`** may be environmental. Find its
  dependency group in `dory-wrangler/tests/categories.py` (the `dev-local`
  launcher; `flock` or exclusive creation; process mechanics; the locale's
  encoding; permission bits), and confirm the host really differs there before
  calling it environmental. A difference the product relies on -- `flock` on
  the store's filesystem above all -- is still a finding about the host (README
  item 2; FS-2).
- **`SyntaxError`, `ImportError`, `AttributeError` or `TypeError` from the
  standard library** means the release does not run under 3.9: RT-2 failed.
- **Before calling any test a failure, run it again by name** (README item 3):

  ```
  cd $DW/tree/dory-wrangler/tests
  TMPDIR=$(mktemp -d -p $DW/tmp) python3 -m unittest -v test_module.TestClass.test_name
  cd $DW/tree
  ```

  Keep both outputs. A failure that does not reproduce is recorded as such, with
  both runs.

### Step 4. Run the end-to-end loop

From `$DW/tree`. The four configurations below use only in-repository launchers
and the platform `python3`, so all of them run internally:

```
python3 dory-wrangler/tests/e2e_loop.py --launcher dev-local --launcher-options '{"profile": "one_shot"}' \
    --work $DW/e2e-one-shot    > $DW/results/e2e-one-shot.log 2>&1;    echo "exit $?" >> $DW/results/e2e-one-shot.log
python3 dory-wrangler/tests/e2e_loop.py --launcher dev-local --launcher-options '{"profile": "persistent"}' \
    --work $DW/e2e-persistent  > $DW/results/e2e-persistent.log 2>&1;  echo "exit $?" >> $DW/results/e2e-persistent.log
python3 dory-wrangler/tests/e2e_loop.py --launcher scripted-stub \
    --work $DW/e2e-scripted    > $DW/results/e2e-scripted.log 2>&1;    echo "exit $?" >> $DW/results/e2e-scripted.log
python3 dory-wrangler/tests/e2e_loop.py --codex-model \
    --work $DW/e2e-codex-model > $DW/results/e2e-codex-model.log 2>&1; echo "exit $?" >> $DW/results/e2e-codex-model.log
python3 dory-wrangler/validate_store.py $DW/e2e-codex-model/store > $DW/results/validate-e2e-store.log 2>&1
echo "exit $?" >> $DW/results/validate-e2e-store.log
```

Each should end `e2e: all 11 steps held` and `exit 0`, with the same step lines
as section 2. The `--work` directory must not exist yet, or be empty.

**Internal-only, and not possible at this release:** the loop through #90's
own launcher,
`python3 dory-wrangler/tests/e2e_loop.py --launcher <its id> --request-timeout 1800`.
That launcher does not exist yet; it is #90's checkpoint
`implement-internal-bridge-launcher`. Until it is registered in
`launchers/registry.py`, the loop stops at its first step with
`FAIL start: the shell process exited before it was listening (exit status 1)`,
and `shell.log` in its `--work` directory ends with
`UnknownLauncher: no launcher '<id>' is configured; known launchers are dev-local, scripted-stub`.
The larger request timeout is there because a real turn may take longer than
the default of 120 seconds (TR-20).

### Step 5. Work through the assumption register

`dory-wrangler/assumption-register.md` has 53 entries. Each says how to settle
it and what "held" and "failed" look like; that text is authoritative, and this
step only sets the order. Record every entry as **held**, **failed** or
**untested**, with its evidence (step 6). A failed entry is a successful
result for #90 (#90 "Full Description"). Work in this order, because each group
needs only what the groups before it set up:

1. **Settled by steps 2 to 4 already.** RT-1 (step 2), RT-2 (step 3, the whole
   suite), TA-1 and UI-3 (step 3, portable and launch-boundary). For the
   test-apparatus entries TA-2 to TA-11, record what each entry's "How
   internal dogfood reads it" asks for, from steps 2 and 3: whether the
   limitation showed on this host.
2. **The store's host, with the in-repository launchers.** FS-1, FS-3, FS-2
   (its `flock` group is in step 3's development-environment run; also start a
   second `run_shell.py` on the same `--root` and see it refused), RT-6. Then
   the served shell: RT-4 (*internal-only* for `ausearch`, which needs root),
   UI-1 and UI-2 (*internal-only*: a browser on the internal workstation,
   following README "By hand").
3. **The internal CLI and script directly, before any launcher exists**
   (*internal-only*). These need captured output, not Dory-wrangler: TR-12
   (`codex --version`, event types), TR-3 (one real launch and one real resume,
   captured raw), TR-5, TR-4, TR-17 and TR-19 (read from those captures), TR-10
   (an invalid ID), TR-14 (failure shapes), TR-15 (whether any stop exists),
   TR-2 (the payload bound, through the same invocation a launcher would use),
   and the prerequisite failures of PR-1 as the script shows them. TR-6, TR-7
   and TR-13 can be read from the captures now; their store half waits for the
   launcher.
4. **Needs #90's internal launcher, registered** (*internal-only*). TR-1,
   TR-8, TR-9, TR-11, TR-16, TR-18, TR-20, RT-3, RT-5, PR-1 (through the
   launcher), PR-2, PR-3, the store halves of TR-6, TR-7 and TR-13, and LO-1 to
   LO-7. LO-3 is the whole-store check: validate, snapshot, re-classify, and
   check every turn was answered. Until the launcher exists, record each of
   these **untested**, reason "no internal launcher at this release".

Entries already `held (internal evidence)` (RT-1, TR-1, TR-3, TR-4, TR-6, TR-7,
TR-8, TR-18, UI-1) were observed on an earlier tree or on the proof-of-concept
script. Each has a re-confirmation procedure; record its result on this
release like any other.

### Step 6. What to capture and bring back

Everything goes in `$DW/results/`. Bring it back the way internal results
normally reach #90. #90 requires internal evidence to be preserved durably, not
only reported in conversation.

| File | From | What it answers |
| --- | --- | --- |
| `mirror.txt` | step 1 | whether the branch reached the mirror, and at which SHA; that the tree is the tested one |
| `host.txt` | step 2 | interpreter, OS, filesystem, locale, cores, limits (RT-1, FS-1, FS-3, TA-3, TA-4, TA-7, TA-9) |
| `portable.log/.json`, `launch-boundary.log/.json`, `development-environment.log/.json`, `full.log/.json` | step 3 | the category and whole-suite results, with wall time and exit status (RT-2, TA-1, FS-2) |
| any named re-run's output | step 3 | whether a failure reproduces |
| `e2e-*.log`, `validate-e2e-store.log` | step 4 | the end-to-end loop in four configurations |
| `captures/` | step 5, group 3 | redacted raw output of one `codex exec --json` launch and one `codex exec resume ... --json`, `launch_agent.sh` output for one launch and one resume, each with its exit status and stderr, and `codex --version` (TR-3 to TR-7, TR-12 to TR-15, TR-17, TR-19) |
| `stores/` | step 5, group 4 | for each launcher store: `validate_store.py STORE --snapshot` output and the snapshot; `diagnostics.py` output for the chats an entry cites |
| `verdicts.md` | step 5 | one row per register entry, as below |

**Redacting a capture.** Replace only values -- a secret, an internal host
name, a prompt's private text -- in place, keeping each line a line and each
field where it was. Do not reformat, re-indent, join or split lines: which event
types occur, their field names, the line boundaries and the `thread_id`'s shape
are the evidence.

**`verdicts.md`** has one row per entry id, all 53, in this form:

| Entry | Outcome | Release commit | Command or procedure | Evidence | Note |
| --- | --- | --- | --- | --- | --- |
| RT-1 | held / failed / untested | `e591ced642900d1204e5b6b9a9627fc1b5e85aea` | as in the register, or what you ran instead | a file in `results/`, or where it is kept | why, if untested or unexpected |

The entry ids are RT-1 to RT-6; FS-1 to FS-3; PR-1 to PR-3; TR-1 to TR-20; LO-1
to LO-7; UI-1 to UI-3; TA-1 to TA-11. The release commit column is the release
candidate for every run of this tree. If a later tree is run, name it instead,
with its own `mirror.txt`.

## 4. Links

All paths are relative to `dory-wrangler/`, and all are in the mirrored tree.

| What | Where |
| --- | --- |
| The assumption register: 53 entries, each with how to settle it | [`assumption-register.md`](assumption-register.md) |
| How an internal run reads a failure by category | [README, "Test categories, and telling an environmental failure from a product one"](README.md#test-categories-and-telling-an-environmental-failure-from-a-product-one), and its "On an internal run" list |
| Every test's category, grouped by the dependency it rests on | [`tests/categories.py`](tests/categories.py) |
| The end-to-end loop, its steps, and the same loop by hand in a browser | [README, "The whole loop, end to end"](README.md#the-whole-loop-end-to-end) and "By hand" |
| Running the shell; validating a store; diagnostic evidence | README, ["Running the shell"](README.md#running-the-shell), ["Validating"](README.md#validating), ["Diagnostic evidence"](README.md#diagnostic-evidence) |
| What the launch-boundary tests need to run against a new launcher | [README, "Running the launch-boundary tests against a new launcher"](README.md#running-the-launch-boundary-tests-against-a-new-launcher) |
| The launch boundary and the twelve obligations #90's launcher must meet | [`launch-boundary.md`](launch-boundary.md), "Adding the internal bridge launcher" |
| The contract, and what was proven or assumed when it was written | [`contract/v0.1/contract.md`](contract/v0.1/contract.md), [`contract/v0.1/facts-and-assumptions.md`](contract/v0.1/facts-and-assumptions.md) (read its stale entries through the register's "What this register supersedes") |
| Runtime and storage choice, and its Rocky Linux 9 risks | [`decisions/0001-runtime-and-storage.md`](decisions/0001-runtime-and-storage.md) |
| One store, one application; concurrent turns and Abandon; event classification; rendering; the no-showable-reply notice; bounded diagnostics | [`decisions/0002`](decisions/0002-one-store-one-application.md), [`0003`](decisions/0003-concurrent-turns-and-abandon.md), [`0004`](decisions/0004-classifying-event-types.md), [`0005`](decisions/0005-rendering-useful-events.md), [`0006`](decisions/0006-no-showable-reply-notice.md), [`0007`](decisions/0007-bounded-diagnostic-retrieval.md) |

Records outside the tree, cited by id for #90 and not needed to follow section 3:

- The human decisions of 2026-09-15 (Stop does not reach a turn in flight in
  v0.1; one serving process per store; Abandon is the exit from every
  non-terminal state; a concurrent turn is refused, not queued): `jmrozi1/ai-dev`
  #81 comment 5687495740, and decision 0003.
- The internal evidence the register holds: #89 comments 5668368963,
  5668684450 and 5669592360; #90 comments 5668369261, 5668684764 and
  5669593061.
- #89's checkpoint acceptances: #89 comments 5815745216, 5819502876 and
  5820849403. The control plane (`ai-dev-control-plane`, project
  `dory-wrangler`, `issue-89/state.md`) holds the reviews behind them.

## Appendix A. The end-to-end loop's lines on the release candidate

Verbatim, apart from the work directory's path, shortened to `$E`.

`--launcher dev-local --launcher-options '{"profile": "one_shot"}'`:

```
PASS start: the shell is served over HTTP by process run_shell.py, a process of its own
PASS create: a new chat is created (HTTP 201) and it is the one chat listed
PASS send: the first turn is accepted (HTTP 201) and recorded as the user's
PASS launch: an agent session opened on that turn, launched by dev-local with an accepted launch; it declares continuation fresh_binding
PASS events: 3 event(s) preserved for the session, 2 of them recognized and agent-sourced
PASS render: the answer ["answer to: hello"] is in the served transcript, read back from disk, and the served page renders it as the agent's
PASS continue: the second turn is answered by a newly launched agent session, the first one completed, as continuation fresh_binding declares; it answered ["answer to: What was the first thing I said in this thread?"]
PASS reopen: the shell was stopped by its PID (SIGKILL; 0 agent process(es) it had started exited with it) and started again on the same store; the reopened transcript is byte-identical (862 bytes) and is the whole conversation so far, 4 message(s) of 2 turn(s)
PASS third-turn: the third turn is answered after the restart: a newly launched agent answered it, ["answer to: What was the first thing I said in this thread?"]
PASS validate: the shell stopped; validate_store.py, a separate program, accepts the store: 28 record(s): agent_binding 3, agent_session 3, chat 1, diagnostic_event 9, launch_request 3, launch_result 3, message 6
PASS diagnostics: diagnostics.py, followed through its bound of 3, returns all 9 preserved event(s) of 3 session(s), gap-free; the 6 served message(s) are the preserved ones and all 3 agent message(s) cite a retrieved event
e2e: all 11 steps held
e2e: work directory (store, logs) kept at $E/e2e-one-shot
```

`--launcher dev-local --launcher-options '{"profile": "persistent"}'`:

```
PASS start: the shell is served over HTTP by process run_shell.py, a process of its own
PASS create: a new chat is created (HTTP 201) and it is the one chat listed
PASS send: the first turn is accepted (HTTP 201) and recorded as the user's
PASS launch: an agent session opened on that turn, launched by dev-local with an accepted launch; it declares continuation persistent
PASS events: 2 event(s) preserved for the session, 2 of them recognized and agent-sourced
PASS render: the answer ["answer to: hello"] is in the served transcript, read back from disk, and the served page renders it as the agent's
PASS continue: the second turn is delivered to the same agent session, as continuation persistent declares; it answered ["answer to: What was the first thing I said in this thread?"]
PASS reopen: the shell was stopped by its PID (SIGKILL; 1 agent process(es) it had started exited with it) and started again on the same store; the reopened transcript is byte-identical (862 bytes) and is the whole conversation so far, 4 message(s) of 2 turn(s)
PASS third-turn: the third turn is answered after the restart: a newly launched agent answered it, ["answer to: What was the first thing I said in this thread?"]; the restart could not re-attach the agent, so the refused turn was sent again after the one action, abandon
PASS validate: the shell stopped; validate_store.py, a separate program, accepts the store: 23 record(s): agent_binding 2, agent_session 2, chat 1, delivery_request 1, diagnostic_event 6, launch_request 2, launch_result 2, message 6, session_observation 1
PASS diagnostics: diagnostics.py, followed through its bound of 3, returns all 6 preserved event(s) of 2 session(s), gap-free; the 6 served message(s) are the preserved ones and all 3 agent message(s) cite a retrieved event
e2e: all 11 steps held
e2e: work directory (store, logs) kept at $E/e2e-persistent
```

`--launcher scripted-stub`:

```
PASS start: the shell is served over HTTP by process run_shell.py, a process of its own
PASS create: a new chat is created (HTTP 201) and it is the one chat listed
PASS send: the first turn is accepted (HTTP 201) and recorded as the user's
PASS launch: an agent session opened on that turn, launched by scripted-stub with an accepted launch; it declares continuation fresh_binding
PASS events: 3 event(s) preserved for the session, 2 of them recognized and agent-sourced
PASS render: the answer ["answer to: hello"] is in the served transcript, read back from disk, and the served page renders it as the agent's
PASS continue: the second turn is answered by a newly launched agent session, the first one completed, as continuation fresh_binding declares; it answered ["answer to: What was the first thing I said in this thread?"]
PASS reopen: the shell was stopped by its PID (SIGKILL; 0 agent process(es) it had started exited with it) and started again on the same store; the reopened transcript is byte-identical (862 bytes) and is the whole conversation so far, 4 message(s) of 2 turn(s)
PASS third-turn: the third turn is answered after the restart: a newly launched agent answered it, ["answer to: What was the first thing I said in this thread?"]
PASS validate: the shell stopped; validate_store.py, a separate program, accepts the store: 28 record(s): agent_binding 3, agent_session 3, chat 1, diagnostic_event 9, launch_request 3, launch_result 3, message 6
PASS diagnostics: diagnostics.py, followed through its bound of 3, returns all 9 preserved event(s) of 3 session(s), gap-free; the 6 served message(s) are the preserved ones and all 3 agent message(s) cite a retrieved event
e2e: all 11 steps held
e2e: work directory (store, logs) kept at $E/e2e-scripted
```

`--codex-model`:

```
PASS start: the shell is served over HTTP by process model_shell.py, a process of its own
PASS create: a new chat is created (HTTP 201) and it is the one chat listed
PASS send: the first turn is accepted (HTTP 201) and recorded as the user's
PASS launch: an agent session opened on that turn, launched by internal-bridge with an accepted launch; it declares continuation persistent
PASS events: 2 event(s) preserved for the session, 2 of them recognized and agent-sourced
PASS render: the answer ["answer to: hello"] is in the served transcript, read back from disk, and the served page renders it as the agent's
PASS continue: the second turn is delivered to the same agent session, as continuation persistent declares; it answered ["you first said: hello"]
PASS reopen: the shell was stopped by its PID (SIGKILL; 0 agent process(es) it had started exited with it) and started again on the same store; the reopened transcript is byte-identical (825 bytes) and is the whole conversation so far, 4 message(s) of 2 turn(s)
PASS third-turn: the third turn is answered after the restart: the agent live before the restart was re-attached and answered it, ["you first said: hello"]
PASS validate: the shell stopped; validate_store.py, a separate program, accepts the store: 20 record(s): agent_binding 1, agent_session 1, chat 1, delivery_request 2, diagnostic_event 6, launch_request 1, launch_result 1, message 6, session_observation 1
PASS diagnostics: diagnostics.py, followed through its bound of 3, returns all 6 preserved event(s) of 1 session(s), gap-free; the 6 served message(s) are the preserved ones and all 3 agent message(s) cite a retrieved event
e2e: all 11 steps held
e2e: work directory (store, logs) kept at $E/e2e-codex-model
```

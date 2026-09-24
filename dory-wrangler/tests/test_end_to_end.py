"""The single-agent chat loop, end to end, for every launcher configuration (#89).

Separable on purpose: this is the whole create, send, launch, events, render,
reopen loop driven by `e2e_loop.py` against the served application as a separate
process, not a replacement for any test that pins a part of it. Run alone with

    python3 dory-wrangler/tests/run_tests.py test_end_to_end.py

Two things are held here:

* **The loop runs.** For `dev-local` in both profiles and for the internal-shape
  Codex JSONL model, `e2e_loop.py` is run as its own process and must exit 0
  with every step held, in order. Each store it leaves is kept, so the runner's
  later phases hand it to the contract validator as a separate program and
  re-classify every event in it from its bytes.
* **Each step checks what it claims.** In a copy of this product tree -- never
  the product itself -- one fault is injected per step, and the path must fail
  *at that step*, with every earlier step having held. A fault is a patch that
  must apply exactly once, is read back, and must compile, or the row is an
  error and proves nothing; the copy is restored and checked after every row,
  and an unfaulted run of the same copy is the control.

A run that outlives its deadline is killed with its whole process group -- the
path, the shell it started, and any agent that shell started -- so a hung run
cannot leave a server running (a SIGKILL of the path alone runs none of its
own clean-up).
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import unittest

import support

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PRODUCT_DIR = os.path.dirname(TESTS_DIR)
E2E_LOOP = os.path.join("tests", "e2e_loop.py")

STEPS = ("start", "create", "send", "launch", "events", "render", "continue", "reopen",
         "third-turn", "validate", "diagnostics")

ONE_SHOT = ["--launcher", "dev-local", "--launcher-options", '{"profile": "one_shot"}']
PERSISTENT = ["--launcher", "dev-local", "--launcher-options", '{"profile": "persistent"}']
CODEX_MODEL = ["--codex-model"]

# What every configuration's agent answers, turn by turn. The development agent
# answers every turn the same way; the Codex model answers the recall prompt
# from its thread's first prompt, so a resumed thread shows in the transcript.
RECALL = "What was the first thing I said in this thread?"
DEV_ANSWERS = ["answer to: hello", "answer to: " + RECALL, "answer to: " + RECALL]
CODEX_ANSWERS = ["answer to: hello", "you first said: hello", "you first said: hello"]

# A whole run of the loop, and a fault row, each get a deadline of their own.
RUN_DEADLINE = 900
FAULT_DEADLINE = 600


class LoopDeadline(AssertionError):
    """A run did not finish within its deadline; its process group was killed."""

    def __init__(self, deadline, group):
        AssertionError.__init__(
            self, "e2e_loop.py did not finish within %ds; its process group %d was killed"
            % (deadline, group))
        self.group = group


def run_loop(product_dir, config, work, extra=(), deadline=RUN_DEADLINE):
    """Run `e2e_loop.py` from `product_dir` as its own process: (status, lines).

    The path is the leader of a process group of its own, which the shell it
    starts, and every agent the shell starts, inherit. If the run outlives
    `deadline`, or the wait is interrupted, the whole group is killed by its id,
    not the path alone, and a deadline raises `LoopDeadline`.
    """
    process = subprocess.Popen(
        [sys.executable, os.path.join(product_dir, E2E_LOOP)] + list(config)
        + ["--work", work] + list(extra),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True)
    try:
        out, err = process.communicate(timeout=deadline)
    except subprocess.TimeoutExpired:
        kill_group(process)
        raise LoopDeadline(deadline, process.pid)
    except BaseException:
        kill_group(process)
        raise
    return (process.returncode, out.decode("utf-8").splitlines(),
            err.decode("utf-8", "replace"))


def kill_group(process):
    """SIGKILL the process group `process` leads, and the leader itself, and reap it.

    The pipes are closed rather than drained: a process that left the group
    could hold them open, and the wait must not outlive the kill.
    """
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.kill()
    process.wait()
    for pipe in (process.stdout, process.stderr):
        pipe.close()


def processes_naming(text):
    """PIDs of processes, not yet exited, whose argv names `text`, from /proc."""
    found = []
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) == os.getpid():
            continue
        try:
            with open("/proc/%s/cmdline" % name, "rb") as handle:
                argv = handle.read().decode("utf-8", "replace")
            with open("/proc/%s/stat" % name) as handle:
                state = handle.read().rsplit(")", 1)[-1].split()[0]
        except (IOError, OSError, IndexError):
            continue
        if text in argv and state != "Z":
            found.append(int(name))
    return found


def silent_launcher(product_dir, marker=None):
    """The development agent told to stay silent, chosen by configuration alone.

    `marker`, if given, is carried in the agent's own argv as an interpreter
    `-X` option the agent never reads, so the agent can be found by it.
    """
    agent = os.path.join(product_dir, "src", "dory_wrangler", "launchers", "dev_agent.py")
    tag = ["-X", "dory_e2e_run=" + marker] if marker else []
    return ["--launcher", "dev-local", "--launcher-options", json.dumps(
        {"profile": "persistent",
         "command": [sys.executable] + tag + [agent, "--profile", "persistent", "--silent"]})]


def verdicts(lines):
    """[(PASS|FAIL, step)] from the path's step lines, in order."""
    out = []
    for line in lines:
        head = line.split(":", 1)[0].split(" ")
        if len(head) == 2 and head[0] in ("PASS", "FAIL"):
            out.append((head[0], head[1]))
    return out


def answers_in(lines):
    """The answer each turn's step line reports, as the JSON list it prints."""
    found = []
    for step in ("render", "continue", "third-turn"):
        line = [l for l in lines if l.startswith("PASS %s:" % step)][0]
        start = line.index("[")
        found.append(json.JSONDecoder().raw_decode(line[start:])[0])
    return found


class TheLoopRunsEndToEnd(unittest.TestCase):
    """Every launcher configuration completes the loop over HTTP."""

    def run_configuration(self, name, config, declared, answers, third):
        work = os.path.join(support.scratch_root("e2e-"), "work")
        status, lines, err = run_loop(PRODUCT_DIR, config, work)
        report = "\n".join(lines) + "\n" + err
        self.assertEqual(status, 0, report)
        self.assertEqual(verdicts(lines), [("PASS", step) for step in STEPS], report)
        self.assertEqual(lines[-1], "e2e: all %d steps held" % len(STEPS), report)
        self.assertIn("it declares continuation %s" % declared, report)
        self.assertEqual(answers_in(lines), [[a] for a in answers], report)
        self.assertIn(third, [l for l in lines if l.startswith("PASS third-turn:")][0], report)
        for line in lines:
            self.assertNotIn(work, line, "a step line carries a path")
            self.assertNotIn("Traceback", line)
        with open(os.path.join(work, "snapshot.json")) as handle:
            records = json.load(handle)["records"]
        support.keep_records(records, "e2e-" + name,
                             "the end-to-end loop's store, %s" % name)

    def test_dev_local_one_shot(self):
        self.run_configuration("dev-local-one-shot", ONE_SHOT, "fresh_binding", DEV_ANSWERS,
                               "a newly launched agent answered it")

    def test_dev_local_persistent(self):
        # dev-local's agent does not survive the shell that started it, so the
        # restart finds it unreachable and the path takes the one action.
        self.run_configuration("dev-local-persistent", PERSISTENT, "persistent", DEV_ANSWERS,
                               "sent again after the one action, abandon")

    def test_codex_jsonl_model(self):
        self.run_configuration("codex-jsonl-model", CODEX_MODEL, "persistent", CODEX_ANSWERS,
                               "the agent live before the restart was re-attached")


class AHungRunLeavesNoServer(unittest.TestCase):
    """A run killed at its deadline takes the shell and its agents with it."""

    def test_a_run_killed_at_its_deadline_leaves_no_process_behind(self):
        # The silent agent never answers, so the path waits on its first send
        # for far longer than this deadline, with a shell and an agent running.
        # Every process of this run names `work` in its argv: the path and the
        # shell by their own arguments, the agent by its marker.
        work = os.path.join(support.scratch_root("e2e-hung-"), "work")
        deadline = 20
        seen = []
        watch = threading.Timer(deadline - 5, lambda: seen.extend(processes_naming(work)))
        watch.start()
        started = time.time()
        try:
            with self.assertRaises(LoopDeadline):
                run_loop(PRODUCT_DIR, silent_launcher(PRODUCT_DIR, marker=work), work,
                         ("--request-timeout", "300"), deadline=deadline)
        finally:
            watch.cancel()
        # The deadline ended the run; it did not wait for the run to end itself.
        self.assertLess(time.time() - started, deadline + 60)
        # Before the deadline the path, its shell and the shell's agent ran.
        self.assertGreaterEqual(len(seen), 3, "the hung run never had a shell and an agent")
        left = processes_naming(work)
        for _ in range(100):
            if not left:
                break
            time.sleep(0.1)
            left = processes_naming(work)
        for pid in left:
            os.kill(pid, signal.SIGKILL)  # never leave one running, even on failure
        self.assertEqual(left, [], "a process of the killed run is still running")


# ---------------------------------------------------------------------------
# One fault per step, in a copy of the product tree
# ---------------------------------------------------------------------------

SERVE = "src/dory_wrangler/serve.py"
SERVE_ARGS_PARSED = "    args = parser.parse_args(argv)\n"

FAULTS = {
    # The shell never says it is listening.
    "start": ("start", ONE_SHOT, (), SERVE,
              "    if args.port_file:\n        with open(args.port_file, \"w\") as handle:",
              "    if False:\n        with open(args.port_file, \"w\") as handle:"),
    # A chat is created and the conversation list never shows it.
    "create": ("create", ONE_SHOT, (), "src/dory_wrangler/webapp.py",
               "return self._respond(200, self.service.list_chats())",
               "return self._respond(200, [])"),
    # A launcher that never answers: the development agent told to stay silent,
    # chosen by configuration alone.
    "send": ("send", ["--launcher", "dev-local", "--launcher-options", "SILENT"],
             ("--request-timeout", "10"), None, None, None),
    # The configured launcher id builds a different launcher.
    "launch": ("launch", ONE_SHOT, (), "src/dory_wrangler/launchers/registry.py",
               "DevLocalLauncher.launcher_id: DevLocalLauncher.from_options,",
               "DevLocalLauncher.launcher_id: ScriptedStubLauncher.from_options,"),
    # Nothing the agent writes is recognized, so no event can carry an answer.
    "events": ("events", ONE_SHOT, (), "src/dory_wrangler/launchers/dev_transport.py",
               "    reading = RECOGNIZED.get(kind) if isinstance(kind, str) else None\n",
               "    reading = None\n"),
    # A render that drops a message: the served chat loses the agent's turns.
    "render": ("render", ONE_SHOT, (), "src/dory_wrangler/service.py",
               "                for m in messages\n",
               "                for m in messages if m[\"author\"] != \"agent\"\n"),
    # A persistent chat whose live agent is never found, so the second turn
    # tries to open a second agent.
    "continue": ("continue", PERSISTENT, (), "src/dory_wrangler/session_manager.py",
                 "        pairs = self._store.list_sessions(chat_id)\n"
                 "        for session, _binding in pairs:\n"
                 "            if session[\"state\"] not in self._terminal:\n",
                 "        pairs = []\n"
                 "        for session, _binding in pairs:\n"
                 "            if session[\"state\"] not in self._terminal:\n"),
    # A server that loses the store: a restarted shell serves somewhere else.
    "reopen": ("reopen", ONE_SHOT, (), SERVE, SERVE_ARGS_PARSED,
               SERVE_ARGS_PARSED
               + "    if os.path.isdir(os.path.join(args.root, \"chats\")):\n"
               + "        args.root = args.root + \"-lost\"\n"),
    # A served chat that has lost turns: every read of a chat serves only its
    # first two messages, while the sends still return the whole chat.
    "reopen-lost-turns": ("reopen", ONE_SHOT, (), "src/dory_wrangler/webapp.py",
                          "return self._respond(200, self.service.open_chat(chat_id))",
                          "return self._respond(200, dict(self.service.open_chat(chat_id), "
                          "messages=self.service.open_chat(chat_id)[\"messages\"][:2]))"),
    # A render that labels the agent's answer as the user's.
    "render-label": ("render", ONE_SHOT, (), "src/dory_wrangler/webapp.py",
                     'agent: "AGENT"', 'agent: "YOU"'),
    # A reopened transcript that differs from the one served before the restart.
    "reopen-bytes": ("reopen", ONE_SHOT, (), "src/dory_wrangler/service.py",
                     '"state": chat["state"],',
                     '"state": "%s-%d" % (chat["state"], __import__("os").getpid()),'),
    # After a restart the agent program cannot be started.
    "third-turn": ("third-turn", ONE_SHOT, (), SERVE, SERVE_ARGS_PARSED,
                   SERVE_ARGS_PARSED
                   + "    if os.path.isdir(os.path.join(args.root, \"chats\")):\n"
                   + "        args.launcher_options = json.dumps({\"profile\": \"one_shot\", "
                   + "\"command\": [\"/nonexistent/dory-e2e-agent\"]})\n"),
    # A restarted shell that leaves a preserved event out of sequence on disk.
    "validate": ("validate", ONE_SHOT, (), SERVE, SERVE_ARGS_PARSED,
                 SERVE_ARGS_PARSED
                 + "    for _where, _dirs, _files in os.walk(os.path.join(args.root, "
                 + "\"diagnostics\")):\n"
                 + "        for _name in [n for n in _files if n == \"00000001.json\"]:\n"
                 + "            import shutil\n"
                 + "            shutil.copyfile(os.path.join(_where, _name), "
                 + "os.path.join(_where, \"00000009.json\"))\n"),
    # A bounded retrieval that loses the record it resumes at.
    "diagnostics": ("diagnostics", ONE_SHOT, (), "diagnostics.py",
                    "    for record in records:\n",
                    "    for record in (records[1:] if \"--resume-at\" in sys.argv else "
                    "records):\n"),
}


class EachStepFailsAtItsStep(unittest.TestCase):
    """A fault per step, and the path fails at exactly that step.

    `FAULTS` maps a row to the step it must fail at, the configuration it runs,
    and the one patch it makes in the copy (none for the launcher that never
    answers, which is configuration alone).
    """

    @classmethod
    def setUpClass(cls):
        cls.copy = os.path.join(support.scratch_root("e2e-faults-"), "dory-wrangler")
        shutil.copytree(PRODUCT_DIR, cls.copy,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    def run_copy(self, config, extra=()):
        work = os.path.join(support.scratch_root("e2e-fault-"), "work")
        return run_loop(self.copy, config, work,
                        ("--start-timeout", "30") + tuple(extra), deadline=FAULT_DEADLINE)

    def apply(self, relative, old, new):
        path = os.path.join(self.copy, relative)
        with open(path) as handle:
            original = handle.read()
        self.assertEqual(original.count(old), 1,
                         "INVALID: the fault's pattern must occur exactly once in %s"
                         % relative)
        patched = original.replace(old, new)
        with open(path, "w") as handle:
            handle.write(patched)
        with open(path) as handle:
            written = handle.read()
        self.assertEqual(written, patched, "INVALID: the fault was not written as meant")
        self.assertIn(new, written)
        compile(written, path, "exec")
        self.addCleanup(self.restore, path, original)

    def restore(self, path, original):
        with open(path, "w") as handle:
            handle.write(original)
        with open(path) as handle:
            if handle.read() != original:
                raise AssertionError("the copy was not restored after a fault")

    def assert_fails_at(self, fault):
        step, config, extra, relative, old, new = FAULTS[fault]
        if "SILENT" in config:
            config = silent_launcher(self.copy)
        if relative is not None:
            self.apply(relative, old, new)
        status, lines, err = self.run_copy(config, extra)
        report = "\n".join(lines) + "\n" + err
        self.assertEqual(status, 1, report)
        index = STEPS.index(step)
        self.assertEqual(verdicts(lines),
                         [("PASS", s) for s in STEPS[:index]] + [("FAIL", step)], report)
        self.assertEqual(lines[-1], "e2e: the loop failed at step %s" % step, report)
        for line in lines:
            self.assertNotIn("Traceback", line)

    def test_every_step_has_a_fault_and_every_fault_a_test(self):
        self.assertEqual(sorted(set(row[0] for row in FAULTS.values())), sorted(STEPS))
        tested = [name for name in dir(self)
                  if name.startswith("test_") and "_fails_at_" in name]
        self.assertEqual(len(tested), len(FAULTS))

    def test_the_unfaulted_copy_passes(self):
        """The control: the same copy, with nothing injected, holds every step."""
        for config in (ONE_SHOT, PERSISTENT):
            status, lines, err = self.run_copy(config)
            self.assertEqual(status, 0, "\n".join(lines) + "\n" + err)
            self.assertEqual(verdicts(lines), [("PASS", s) for s in STEPS])

    def test_a_shell_that_never_listens_fails_at_start(self):
        self.assert_fails_at("start")

    def test_a_chat_the_list_never_shows_fails_at_create(self):
        self.assert_fails_at("create")

    def test_a_launcher_that_never_answers_fails_at_send(self):
        self.assert_fails_at("send")

    def test_a_different_launcher_than_configured_fails_at_launch(self):
        self.assert_fails_at("launch")

    def test_a_turn_whose_events_carry_no_recognized_answer_fails_at_events(self):
        self.assert_fails_at("events")

    def test_a_render_that_drops_the_answer_fails_at_render(self):
        self.assert_fails_at("render")

    def test_a_page_that_labels_the_answer_as_the_users_fails_at_render(self):
        self.assert_fails_at("render-label")

    def test_a_persistent_agent_that_is_not_continued_fails_at_continue(self):
        self.assert_fails_at("continue")

    def test_a_restarted_shell_that_loses_the_store_fails_at_reopen(self):
        self.assert_fails_at("reopen")

    def test_a_served_chat_that_has_lost_turns_fails_at_reopen(self):
        self.assert_fails_at("reopen-lost-turns")

    def test_a_reopened_transcript_that_is_not_byte_identical_fails_at_reopen(self):
        self.assert_fails_at("reopen-bytes")

    def test_an_agent_that_cannot_start_after_the_restart_fails_at_third_turn(self):
        self.assert_fails_at("third-turn")

    def test_a_store_left_out_of_sequence_fails_at_validate(self):
        self.assert_fails_at("validate")

    def test_a_retrieval_that_loses_records_fails_at_diagnostics(self):
        self.assert_fails_at("diagnostics")


if __name__ == "__main__":
    unittest.main()

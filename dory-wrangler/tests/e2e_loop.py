#!/usr/bin/env python3
"""The single-agent chat loop, end to end, as one runnable path (#89).

    python3 dory-wrangler/tests/e2e_loop.py --launcher dev-local \\
        --launcher-options '{"profile": "one_shot"}'
    python3 dory-wrangler/tests/e2e_loop.py --launcher dev-local \\
        --launcher-options '{"profile": "persistent"}'
    python3 dory-wrangler/tests/e2e_loop.py --codex-model

**Test material, not product.** It builds no behaviour: it drives the served
application the way a browser does -- over HTTP, to a shell started as a separate
process -- and reads what was preserved only through the two read-only programs
that ship beside it, `validate_store.py` and `diagnostics.py`. It imports nothing
from the product package; the one module it imports is `pagemodel`, which reads
the page the shell actually served.

Which launcher answers is configuration and nothing else. `--launcher` and
`--launcher-options` are handed to `run_shell.py` unchanged, so any launcher the
registry builds can be driven. `--codex-model` serves the internal-shape Codex
JSONL model through `tests/model_shell.py`, which hands the model to the
product's own `build_server` because the model is deliberately registered
nowhere. Nothing below that choice branches on which launcher it is: what the
path expects of continuation is read from the continuation the launcher
*declared*, as the store recorded it.

Against a work directory it creates (`--work`, or a new temporary one), it runs
these steps, printing one line per step and stopping at the first that does not
hold:

    start        the shell is served over HTTP by a process of its own
    create       a new chat is created and listed
    send         a user turn is accepted and recorded
    launch       an agent session opened on that turn, by the configured
                 launcher, with an accepted launch
    events       that session's events were preserved, agent-sourced
    render       the answer is in the served transcript, read back from disk,
                 and the served page renders it as the agent's text
    continue     a second turn is answered as the declared continuation says:
                 `persistent` by the same agent, `fresh_binding` by a new one
    reopen       the shell is stopped by its PID and started again on the same
                 store; the reopened transcript is byte-identical to before, and
                 both are the whole conversation so far
    third-turn   a third turn is answered after the restart, and the chat then
                 served is the whole conversation, all three turns
    validate     `validate_store.py`, a separate program, accepts the store
    diagnostics  `diagnostics.py`, followed through its own bound, returns every
                 preserved event of the chat, and every agent message cites one

The conversation the path expects is built only from what the served application
returned after each send: every send's answer must carry the whole conversation
held so far, unchanged, followed by the new user turn and its answer. Every step
that reads the served chat back requires it to be that whole conversation, so no
step can pass on a chat that has lost turns.

Exit 0 only if every step held; 1 at the first that did not, naming it and why;
2 for arguments it cannot use. The lines say what was checked about the product
and never carry a path or an exception's text. The work directory -- the store,
and the model's Codex home and spool -- is named on standard error at the end
and kept, so `diagnostics.py` can be run over it by hand.

Every wait here has a deadline, and every deadline is this program's own. The
product is given no timer: a turn that never returns is observed as a request
that did not answer within `--request-timeout`, and the shell is then stopped by
the PID this program started. Standard library only; Python 3.9.
"""

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PRODUCT_DIR = os.path.dirname(TESTS_DIR)
RUN_SHELL = os.path.join(PRODUCT_DIR, "run_shell.py")
MODEL_SHELL = os.path.join(TESTS_DIR, "model_shell.py")
VALIDATE_STORE = os.path.join(PRODUCT_DIR, "validate_store.py")
DIAGNOSTICS = os.path.join(PRODUCT_DIR, "diagnostics.py")

if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

import pagemodel  # noqa: E402

# The launcher id `model_shell.py` serves; the model's own `launcher_id`.
MODEL_LAUNCHER_ID = "internal-bridge"

STEPS = ("start", "create", "send", "launch", "events", "render", "continue", "reopen",
         "third-turn", "validate", "diagnostics")

FIRST_TURN = "hello"
# Any agent may be asked this. The Codex model answers it from its thread's first
# prompt, which is what makes a resumed thread visible in the transcript; the
# development agent simply answers it.
RECALL_TURN = "What was the first thing I said in this thread?"

# The contract's terminal session states (validator/validate_contract.py).
TERMINAL = frozenset(("completed", "failed", "launch_failed", "terminated", "abandoned"))

# The page's avatar label for each author, as a reader sees it.
AGENT_LABEL = "AGENT"


class StepFailed(Exception):
    """A step did not hold. Carries fixed words, never an exception's text."""

    def __init__(self, words):
        Exception.__init__(self, words)
        self.words = words


# ---------------------------------------------------------------------------
# The shell, as a process of its own
# ---------------------------------------------------------------------------

class Shell(object):
    """One served application, started as a separate process and stopped by PID."""

    def __init__(self, work, command, start_timeout, request_timeout):
        self.work = work
        self.command = command
        self.port_file = os.path.join(work, "port")
        self.log = os.path.join(work, "shell.log")
        self.start_timeout = start_timeout
        self.request_timeout = request_timeout
        self.process = None
        self.port = None

    def start(self):
        if os.path.exists(self.port_file):
            os.unlink(self.port_file)
        with open(self.log, "ab") as log:
            self.process = subprocess.Popen(self.command, stdin=subprocess.DEVNULL,
                                            stdout=log, stderr=log)
        deadline = time.time() + self.start_timeout
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise StepFailed("the shell process exited before it was listening "
                                 "(exit status %d)" % self.process.returncode)
            text = _read_text(self.port_file)
            if text and text.strip().isdigit():
                self.port = int(text.strip())
                break
            time.sleep(0.05)
        else:
            raise StepFailed("the shell was not listening within %ds" % self.start_timeout)
        status, body = self.request("GET", "/healthz")
        if status != 200 or _json(body) != {"status": "ok"}:
            raise StepFailed("the shell did not answer its health check (HTTP %s)" % status)

    @property
    def pid(self):
        return self.process.pid if self.process is not None else None

    def stop(self):
        """SIGKILL by the PID this program started: no shutdown hook runs."""
        if self.process is None:
            return
        process, self.process, self.port = self.process, None, None
        if process.poll() is None:
            try:
                os.kill(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass

    def request(self, method, path, payload=None):
        """(status, body bytes). An error status is data; no answer is a failure."""
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.request_timeout) as response:
                return response.status, response.read()
        except HTTPError as exc:
            return exc.code, exc.read()
        except (URLError, OSError, ValueError):
            raise StepFailed("the shell gave no answer to %s %s within %ds"
                             % (method, _route(path), self.request_timeout))


def _route(path):
    """A request path with its chat identifier replaced, for a failure line."""
    parts = path.split("/")
    return "/".join("<chat>" if part.startswith("cht_") else part for part in parts)


def children_of(pid):
    """Direct children of a process, from /proc; empty where /proc is not."""
    found = []
    task_dir = "/proc/%d/task" % pid
    if not os.path.isdir(task_dir):
        return found
    for task in os.listdir(task_dir):
        text = _read_text(os.path.join(task_dir, task, "children"))
        if text:
            found.extend(int(p) for p in text.split())
    return found


def is_alive(pid):
    """Whether `pid` is a process that has not exited (a zombie has)."""
    status = _read_text("/proc/%d/stat" % pid)
    if status is not None:
        return status.rsplit(")", 1)[-1].split()[0] != "Z"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_text(path):
    try:
        with open(path) as handle:
            return handle.read()
    except (IOError, OSError):
        return None


def _json(body):
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# The read-only programs
# ---------------------------------------------------------------------------

def run_program(argv, timeout, what):
    try:
        done = subprocess.run([sys.executable] + argv, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        raise StepFailed("%s did not finish within %ds" % (what, timeout))
    except OSError:
        raise StepFailed("%s could not be run" % what)
    return done


def diagnostics(store, chat_id, mode, page, timeout):
    """Every record `diagnostics.py` prints in `mode`, following its bound literally.

    Asked `page` records at a time; while standard error names the next record,
    the arguments it gives are appended to the same base and asked again, which
    is exactly what a person at a terminal does (decision 0007).
    """
    base = [DIAGNOSTICS, store, chat_id, "--records", mode, "--limit", str(page)]
    extra, records, asked = [], [], 0
    while True:
        asked += 1
        if asked > 10000:
            raise StepFailed("diagnostics.py never stopped naming a next record")
        done = run_program(base + extra, timeout, "diagnostics.py")
        if done.returncode != 0:
            raise StepFailed("diagnostics.py refused the %s retrieval (exit %d)"
                             % (mode, done.returncode))
        lines = [line for line in done.stdout.decode("utf-8").split("\n") if line]
        try:
            batch = [json.loads(line) for line in lines]
        except ValueError:
            raise StepFailed("diagnostics.py printed a line that is not one JSON record")
        if len(batch) > page:
            raise StepFailed("diagnostics.py printed more than the bound it was given")
        records.extend(batch)
        err = done.stderr.decode("utf-8")
        marker = "; ask again with "
        if "truncated:" not in err:
            return records
        if marker not in err:
            raise StepFailed("diagnostics.py held records back without naming the next")
        extra = shlex.split(err.split(marker, 1)[1].strip().split("\n")[0])


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

class Loop(object):
    def __init__(self, args, work):
        self.args = args
        self.work = work
        self.store = os.path.join(work, "store")
        if args.codex_model:
            self.launcher_id = MODEL_LAUNCHER_ID
            codex_home = os.path.join(work, "codex-home")
            spool = os.path.join(work, "spool")
            for path in (self.store, codex_home, spool):
                os.makedirs(path)
            command = [sys.executable, MODEL_SHELL, "--root", self.store,
                       "--port-file", os.path.join(work, "port"),
                       "--codex-home", codex_home, "--spool", spool]
        else:
            self.launcher_id = args.launcher
            os.makedirs(self.store)
            command = [sys.executable, RUN_SHELL, "--root", self.store, "--port", "0",
                       "--port-file", os.path.join(work, "port"), "--quiet",
                       "--launcher", args.launcher]
            if args.launcher_options is not None:
                command += ["--launcher-options", args.launcher_options]
        self.shell = Shell(work, command, args.start_timeout, args.request_timeout)
        self.chat_id = None
        # The whole conversation so far, as the sends returned it: every user
        # turn and every agent message, in order.
        self.conversation = []
        self.turns = 0

    # -- helpers ------------------------------------------------------------

    def lifecycle(self):
        return diagnostics(self.store, self.chat_id, "lifecycle", self.args.page,
                           self.args.program_timeout)

    def sessions(self):
        return [r for r in self.lifecycle() if r.get("record_type") == "agent_session"]

    def message_records(self):
        return diagnostics(self.store, self.chat_id, "messages", self.args.page,
                           self.args.program_timeout)

    def send(self, text):
        status, body = self.shell.request("POST", "/api/chats/%s/messages" % self.chat_id,
                                          {"text": text})
        return status, _json(body)

    def answered(self, chat, text, what):
        """The agent messages that follow the last user turn, which must be `text`."""
        if not isinstance(chat, dict) or not isinstance(chat.get("messages"), list):
            raise StepFailed("the served chat is not a chat")
        messages = chat["messages"]
        users = [i for i, m in enumerate(messages) if m.get("author") == "user"]
        if not users or messages[users[-1]].get("text") != text:
            raise StepFailed("the %s is not the last user turn in the served transcript" % what)
        replies = [m for m in messages[users[-1] + 1:] if m.get("author") == "agent"]
        if not replies or not all(isinstance(m.get("text"), str) and m["text"]
                                  for m in replies):
            raise StepFailed("the %s has no answer from the agent in the served transcript"
                             % what)
        return replies

    def hold(self, chat, what):
        """Take a send's answer as the conversation so far, if it extends the last.

        The messages the send returned must begin with the whole conversation
        held before it, unchanged, and must carry one user turn per turn sent.
        """
        messages = chat["messages"]
        held = len(self.conversation)
        if messages[:held] != self.conversation or len(messages) <= held:
            raise StepFailed("the %s's answer does not carry the whole conversation so far"
                             % what)
        self.turns += 1
        if len([m for m in messages if m.get("author") == "user"]) != self.turns:
            raise StepFailed("the %s's answer does not carry every user turn sent" % what)
        self.conversation = list(messages)

    def whole(self, chat):
        """Whether a served chat is this chat with the whole conversation so far."""
        return isinstance(chat, dict) and chat.get("chat_id") == self.chat_id and \
            chat.get("messages") == self.conversation

    def answering_session(self, message_id):
        for record in self.message_records():
            if record.get("message_id") == message_id:
                return record.get("session_id")
        raise StepFailed("the served answer has no preserved message record")

    # -- the steps -----------------------------------------------------------

    def step_start(self):
        self.shell.start()
        return "the shell is served over HTTP by process %s, a process of its own" % (
            "model_shell.py" if self.args.codex_model else "run_shell.py")

    def step_create(self):
        status, body = self.shell.request("POST", "/api/chats", {})
        chat = _json(body)
        if status != 201 or not isinstance(chat, dict) or not chat.get("chat_id"):
            raise StepFailed("creating a chat answered HTTP %s, not a new chat" % status)
        if chat.get("messages") != []:
            raise StepFailed("a new chat was served with messages already in it")
        self.chat_id = chat["chat_id"]
        status, body = self.shell.request("GET", "/api/chats")
        listed = _json(body)
        if status != 200 or not isinstance(listed, list) or \
                [c.get("chat_id") for c in listed] != [self.chat_id]:
            raise StepFailed("the conversation list does not show exactly the new chat")
        return "a new chat is created (HTTP 201) and it is the one chat listed"

    def step_send(self):
        status, chat = self.send(FIRST_TURN)
        if status != 201:
            raise StepFailed("the first turn was not accepted (HTTP %s)" % status)
        first = (chat or {}).get("messages") or [{}]
        if first[0].get("author") != "user" or first[0].get("text") != FIRST_TURN:
            raise StepFailed("the first turn is not the chat's first message")
        self.first_user_id = first[0].get("message_id")
        self.sent = chat
        return "the first turn is accepted (HTTP 201) and recorded as the user's"

    def step_launch(self):
        records = self.lifecycle()
        sessions = [r for r in records if r.get("record_type") == "agent_session"]
        if len(sessions) != 1:
            raise StepFailed("the first turn opened %d agent sessions, not one" % len(sessions))
        session = sessions[0]
        if session.get("launcher_id") != self.launcher_id:
            raise StepFailed("the session was launched by a launcher other than the "
                             "configured one")
        opened = (session.get("transitions") or [{}])[0].get("evidence") or {}
        if opened.get("ref") != self.first_user_id:
            raise StepFailed("the session was not opened on the user's first turn")
        results = [r for r in records if r.get("record_type") == "launch_result"
                   and r.get("session_id") == session.get("session_id")]
        if len(results) != 1 or results[0].get("outcome") != "accepted" or \
                not results[0].get("agent_handle") or \
                results[0].get("agent_handle") != session.get("agent_handle"):
            raise StepFailed("the session has no accepted launch that issued its agent handle")
        self.first_session = session
        self.declared = (session.get("launcher_capabilities") or {}).get("continuation")
        return ("an agent session opened on that turn, launched by %s with an accepted "
                "launch; it declares continuation %s" % (self.launcher_id, self.declared))

    def step_events(self):
        sid = self.first_session["session_id"]
        events = [e for e in diagnostics(self.store, self.chat_id, "events", self.args.page,
                                         self.args.program_timeout)
                  if e.get("session_id") == sid]
        if [e.get("sequence") for e in events] != list(range(1, len(events) + 1)) or not events:
            raise StepFailed("the session's preserved events are not a gap-free sequence "
                             "from 1")
        agent = [e for e in events if e.get("source") == "agent"
                 and e.get("interpretation") == "recognized"]
        if not agent:
            raise StepFailed("no recognized agent-sourced event was preserved for the turn")
        return "%d event(s) preserved for the session, %d of them recognized and " \
               "agent-sourced" % (len(events), len(agent))

    def step_render(self):
        replies = self.answered(self.sent, FIRST_TURN, "first turn")
        self.hold(self.sent, "first turn")
        status, body = self.shell.request("GET", "/api/chats/%s" % self.chat_id)
        if status != 200 or _json(body) != self.sent or not self.whole(_json(body)):
            raise StepFailed("the chat read back from disk is not what the send returned")
        status, page = self.shell.request("GET", "/")
        if status != 200:
            raise StepFailed("the page was not served (HTTP %s)" % status)
        page = page.decode("utf-8")
        for reply in replies:
            try:
                seen = pagemodel.render_turn(page, "agent", reply["text"])
            except pagemodel.PageModelError:
                raise StepFailed("the served page no longer renders a turn in a shape the "
                                 "page model can read")
            if seen["avatar_text"] != AGENT_LABEL or seen["body_text"] != reply["text"]:
                raise StepFailed("the served page does not render the answer as the "
                                 "agent's text")
        return "the answer %s is in the served transcript, read back from disk, and the " \
               "served page renders it as the agent's" % json.dumps(
                   [r["text"] for r in replies])

    def step_continue(self):
        status, chat = self.send(RECALL_TURN)
        if status != 201:
            raise StepFailed("the second turn was not answered (HTTP %s)" % status)
        replies = self.answered(chat, RECALL_TURN, "second turn")
        self.hold(chat, "second turn")
        answering = self.answering_session(replies[-1]["message_id"])
        sessions = self.sessions()
        first = self.first_session["session_id"]
        if self.declared == "persistent":
            if len(sessions) != 1 or answering != first:
                raise StepFailed("continuation is declared persistent, but the second turn "
                                 "was not answered by the first turn's agent")
            how = "delivered to the same agent session"
        elif self.declared == "fresh_binding":
            before = [s for s in sessions if s["session_id"] == first]
            if len(sessions) != 2 or answering == first or not before or \
                    before[0].get("state") not in TERMINAL:
                raise StepFailed("continuation is declared fresh_binding, but the second turn "
                                 "was not answered by a newly launched agent")
            how = "answered by a newly launched agent session, the first one %s" % (
                before[0]["state"])
        else:
            raise StepFailed("the launcher declared a continuation this path does not know")
        return "the second turn is %s, as continuation %s declares; it answered %s" % (
            how, self.declared, json.dumps([r["text"] for r in replies]))

    def step_reopen(self):
        status, before = self.shell.request("GET", "/api/chats/%s" % self.chat_id)
        if status != 200:
            raise StepFailed("the chat could not be read before the restart (HTTP %s)" % status)
        if not self.whole(_json(before)):
            raise StepFailed("the chat served before the restart is not the whole "
                             "conversation so far")
        old_pid = self.shell.pid
        agents = children_of(old_pid)
        self.shell.stop()
        deadline = time.time() + self.args.start_timeout
        while time.time() < deadline and any(is_alive(p) for p in [old_pid] + agents):
            time.sleep(0.05)
        if any(is_alive(p) for p in [old_pid] + agents):
            raise StepFailed("the stopped shell, or an agent process it started, is still "
                             "running")
        self.shell.start()
        if self.shell.pid == old_pid:
            raise StepFailed("the restarted shell is the same process")
        status, after = self.shell.request("GET", "/api/chats/%s" % self.chat_id)
        if status != 200:
            raise StepFailed("the chat could not be reopened after the restart (HTTP %s)"
                             % status)
        if not self.whole(_json(after)):
            raise StepFailed("the reopened chat is not the whole conversation so far")
        if after != before:
            raise StepFailed("the reopened transcript is not byte-identical to the one "
                             "served before the restart")
        self.reopened = before
        return ("the shell was stopped by its PID (SIGKILL; %d agent process(es) it had "
                "started exited with it) and started again on the same store; the "
                "reopened transcript is byte-identical (%d bytes) and is the whole "
                "conversation so far, %d message(s) of %d turn(s)"
                % (len(agents), len(before), len(self.conversation), self.turns))

    def step_third_turn(self):
        existing = self.sessions()
        before = [s["session_id"] for s in existing]
        live = [s["session_id"] for s in existing if s.get("state") not in TERMINAL]
        status, chat = self.send(RECALL_TURN)
        taken = ""
        if status == 409 and (chat or {}).get("refused") is True:
            # The chat's agent could not be re-attached after the restart, and the
            # send was refused before it was recorded. The shell's one lifecycle
            # action is the way out (contract 5.4, decision 0003); it is taken
            # only when the store says re-attachment of the live agent failed.
            status_now, now = self.shell.request("GET", "/api/chats/%s" % self.chat_id)
            if status_now != 200 or now != self.reopened:
                raise StepFailed("a refused third turn changed the chat")
            failed = [r for r in self.lifecycle()
                      if r.get("record_type") == "session_observation"
                      and r.get("kind") == "reattach_failed"
                      and r.get("session_id") in live]
            if not live or not failed:
                raise StepFailed("the third turn was refused with no failed re-attachment "
                                 "to explain it")
            status, _body = self.shell.request(
                "POST", "/api/chats/%s/abandon" % self.chat_id, {})
            if status != 200:
                raise StepFailed("the one lifecycle action was refused (HTTP %s)" % status)
            status, chat = self.send(RECALL_TURN)
            taken = ("; the restart could not re-attach the agent, so the refused turn was "
                     "sent again after the one action, abandon")
        if status != 201:
            raise StepFailed("the third turn was not answered (HTTP %s)" % status)
        replies = self.answered(chat, RECALL_TURN, "third turn")
        self.hold(chat, "third turn")
        answering = self.answering_session(replies[-1]["message_id"])
        if not taken and self.declared == "persistent":
            if live != [answering]:
                raise StepFailed("continuation is declared persistent, but the agent live "
                                 "before the restart did not answer the third turn")
            how = "the agent live before the restart was re-attached and answered it"
        else:
            if answering in before:
                raise StepFailed("the third turn was not answered by a newly launched agent")
            how = "a newly launched agent answered it"
        status, body = self.shell.request("GET", "/api/chats/%s" % self.chat_id)
        if status != 200 or not self.whole(_json(body)):
            raise StepFailed("the chat read back after the third turn is not the whole "
                             "conversation")
        return "the third turn is answered after the restart: %s, %s%s" % (
            how, json.dumps([r["text"] for r in replies]), taken)

    def step_validate(self):
        self.shell.stop()
        self.snapshot = os.path.join(self.work, "snapshot.json")
        done = run_program([VALIDATE_STORE, self.store, "--snapshot", self.snapshot],
                           self.args.program_timeout, "validate_store.py")
        if done.returncode != 0 or b"the store satisfies contract v0.1" not in done.stdout:
            raise StepFailed("validate_store.py did not accept the store (exit %d)"
                             % done.returncode)
        counts = [line for line in done.stdout.decode("utf-8").split("\n")
                  if " record(s): " in line]
        return "the shell stopped; validate_store.py, a separate program, accepts the " \
               "store: %s" % (counts[0].strip() if counts else "")

    def step_diagnostics(self):
        try:
            with open(self.snapshot) as handle:
                exported = json.load(handle)["records"]
        except (IOError, OSError, ValueError, KeyError, TypeError):
            raise StepFailed("validate_store.py wrote no readable export")
        preserved = [r for r in exported if r.get("record_type") == "diagnostic_event"
                     and r.get("chat_id") == self.chat_id]
        preserved.sort(key=lambda r: (r["session_id"], r["sequence"]))
        sessions = sorted(set(r["session_id"] for r in exported
                              if r.get("record_type") == "agent_session"
                              and r.get("chat_id") == self.chat_id))
        retrieved = diagnostics(self.store, self.chat_id, "events", self.args.page,
                                self.args.program_timeout)
        if retrieved != preserved:
            raise StepFailed("diagnostics.py did not return every preserved event of the "
                             "chat exactly once, in order")
        for sid in sessions:
            sequences = [r["sequence"] for r in retrieved if r["session_id"] == sid]
            if not sequences or sequences != list(range(1, len(sequences) + 1)):
                raise StepFailed("a session's retrieved events are not a gap-free sequence "
                                 "from 1")
        by_id = dict((r["event_id"], r) for r in retrieved)
        messages = self.message_records()
        served = self.conversation
        if [(m.get("message_id"), m.get("author"), (m.get("content") or {}).get("text"))
                for m in messages] != [(m["message_id"], m["author"], m["text"])
                                        for m in served]:
            raise StepFailed("the preserved messages are not the served transcript")
        cited = 0
        for message in messages:
            if message.get("author") != "agent":
                continue
            event = by_id.get(message.get("source_event_id"))
            if event is None or event["session_id"] != message.get("session_id") or \
                    event.get("source") != "agent" or \
                    event.get("interpretation") != "recognized":
                raise StepFailed("an agent message does not cite a retrieved, recognized, "
                                 "agent-sourced event of its own session")
            cited += 1
        return ("diagnostics.py, followed through its bound of %d, returns all %d preserved "
                "event(s) of %d session(s), gap-free; the %d served message(s) are the "
                "preserved ones and all %d agent message(s) cite a retrieved event"
                % (self.args.page, len(retrieved), len(sessions), len(messages), cited))

    # -- running -------------------------------------------------------------

    def run(self, out):
        try:
            for name in STEPS:
                try:
                    said = getattr(self, "step_" + name.replace("-", "_"))()
                except StepFailed as failed:
                    out.write("FAIL %s: %s\n" % (name, failed.words))
                    out.write("e2e: the loop failed at step %s\n" % name)
                    out.flush()
                    return 1
                except Exception as exc:  # noqa: BLE001 - reported by kind, never text
                    out.write("FAIL %s: the step could not be completed (%s)\n"
                              % (name, type(exc).__name__))
                    out.write("e2e: the loop failed at step %s\n" % name)
                    out.flush()
                    return 1
                out.write("PASS %s: %s\n" % (name, said))
                out.flush()
            out.write("e2e: all %d steps held\n" % len(STEPS))
            out.flush()
            return 0
        finally:
            self.shell.stop()


def main(argv=None, out=None):
    out = sys.stdout if out is None else out
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--launcher", help="a launcher id run_shell.py builds")
    which.add_argument("--codex-model", action="store_true",
                       help="serve the internal-shape Codex JSONL model via model_shell.py")
    parser.add_argument("--launcher-options", default=None,
                        help="the launcher's options, as JSON, handed to run_shell.py")
    parser.add_argument("--work", default=None,
                        help="an empty or absent directory to hold the store")
    parser.add_argument("--page", type=int, default=3,
                        help="the --limit every diagnostics.py call is given")
    parser.add_argument("--start-timeout", type=int, default=60)
    parser.add_argument("--request-timeout", type=int, default=120)
    parser.add_argument("--program-timeout", type=int, default=120)
    args = parser.parse_args(argv)
    if args.codex_model and args.launcher_options is not None:
        parser.error("--launcher-options configures a run_shell.py launcher only")
    if args.page < 1:
        parser.error("--page must be at least 1")

    if args.work is None:
        work = tempfile.mkdtemp(prefix="dory-e2e-")
    else:
        work = os.path.abspath(args.work)
        if os.path.exists(work) and (not os.path.isdir(work) or os.listdir(work)):
            parser.error("--work must be an empty or absent directory")
        if not os.path.isdir(work):
            os.makedirs(work)
    status = Loop(args, work).run(out)
    sys.stderr.write("e2e: work directory (store, logs) kept at %s\n" % work)
    sys.stderr.flush()
    return status


if __name__ == "__main__":
    sys.exit(main())

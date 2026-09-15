"""The chat shell: conversation list, active conversation, new-chat flow, send, abandon.

An HTTP server from the standard library, serving one page with no external
asset of any kind. Nothing is fetched from a CDN, a package registry, or a font
host, because the environment this has to run in eventually is an internal
network and a page that needs the internet to render is a page that does not
render there.

## What this surface deliberately does not expose

Contract P4 and the ticket's boundary with #82. There is no endpoint and no
pixel for a session, a binding, a transition, a launch result, an observation,
or a diagnostic event. The JSON the browser receives contains chat metadata and
message text and nothing else, so the shell could not display worker internals
even if its markup tried to. The bounded out-of-band diagnostic retrieval
required by contract 8.5 exists on `ChatStore`, reachable by a program, and is
not wired to a route.

There are no agent-launch mechanics here either. A sent turn goes to the chat
loop (`session_manager`), which reaches an agent only through the configured
launcher; this module never names one.

## The one lifecycle action

`POST /api/chats/<id>/abandon` is the user's exit from an agent nobody can reach
(contract 5.4). It is offered on the page only after a send was refused, and a
refusal says why in words that carry no identifier, state name, or lifecycle
vocabulary: the shell still shows one assistant conversation and no status.
"""

import json
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .errors import (
    ConcurrentLaunchRefused,
    HarnessError,
    InstructionTooLarge,
    NotFound,
    StoreError,
)
from .launch_boundary import LaunchBoundaryError
from .service import ChatService

MAX_BODY_BYTES = 1 << 20  # a user turn is text; a megabyte is already generous

# What the browser is told when the chat loop refuses an action. Fixed words,
# deliberately: the refusal a harness raises names sessions and states, and
# none of that crosses onto the chat surface (contract P4).
REFUSED_BUSY = (
    "This chat's agent has not finished, or whether it has cannot be determined, "
    "so your message was not sent. If the agent cannot be reached, abandon it and "
    "send again."
)
REFUSED_TOO_LARGE = "This message is larger than the agent here accepts; it was not sent."
REFUSED_OTHER = "That is not possible for this chat right now; nothing was changed."
INTEGRATION_FAILED = (
    "The agent integration returned something this build cannot use. Reload the "
    "chat to see what it holds now."
)

# What the browser is told on every other error path. Fixed words, and the only
# words any error body carries (review finding R3): the store's refusals and
# the chat loop's exceptions name sessions, handles, sequences and states, and
# since convergence every one of those writes can raise into a request handler.
# Nothing an exception says is ever copied into a body.
NO_SUCH_ROUTE = "no such route"
NOT_FOUND = "There is no such chat."
UNREADABLE = "This chat could not be read, so nothing from it is shown; nothing was changed."
STORE_REFUSED = (
    "This could not be recorded as asked. Reload the chat to see what it holds now."
)
FAILED = "Something went wrong in the shell. Reload the chat to see what it holds now."
BAD_LENGTH = "Content-Length is not a number"
TOO_LARGE = "request body is too large"
NOT_JSON = "request body is not JSON"
NOT_AN_OBJECT = "request body must be a JSON object"
NEEDS_TEXT = "a message needs text"

# Every sentence an error body may carry. A test holds every served error body
# to this set, so adding a path that answers with anything else fails there.
ERROR_WORDS = frozenset((
    REFUSED_BUSY, REFUSED_TOO_LARGE, REFUSED_OTHER, INTEGRATION_FAILED, NO_SUCH_ROUTE,
    NOT_FOUND, UNREADABLE, STORE_REFUSED, FAILED, BAD_LENGTH, TOO_LARGE, NOT_JSON,
    NOT_AN_OBJECT, NEEDS_TEXT,
))

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dory-wrangler</title>
<style>
  :root {
    --bg: #ffffff;
    --sidebar: #f7f7f8;
    --line: #e3e3e6;
    --ink: #1f1f22;
    --muted: #6b6b73;
    --user: #edf1ff;
    --accent: #2f5bd8;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #191a1c;
      --sidebar: #131416;
      --line: #2c2e32;
      --ink: #ececf1;
      --muted: #9a9aa4;
      --user: #23304f;
      --accent: #7ea0ff;
    }
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    display: flex;
    font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto,
          "Helvetica Neue", Arial, sans-serif;
    background: var(--bg);
    color: var(--ink);
  }
  #sidebar {
    width: 272px;
    flex: 0 0 272px;
    background: var(--sidebar);
    border-right: 1px solid var(--line);
    display: flex;
    flex-direction: column;
    min-height: 0;
  }
  #brand {
    padding: 14px 16px 10px;
    font-weight: 650;
    letter-spacing: .2px;
  }
  #brand small { display: block; font-weight: 400; color: var(--muted); font-size: 12px; }
  #newchat {
    margin: 0 12px 10px;
    padding: 9px 12px;
    border: 1px solid var(--line);
    border-radius: 8px;
    background: var(--bg);
    color: var(--ink);
    font: inherit;
    cursor: pointer;
    text-align: left;
  }
  #newchat:hover { border-color: var(--accent); }
  #chats { overflow-y: auto; flex: 1 1 auto; padding: 0 8px 12px; min-height: 0; }
  .chat-item {
    display: block;
    width: 100%;
    text-align: left;
    border: 0;
    background: transparent;
    color: inherit;
    font: inherit;
    padding: 8px 10px;
    border-radius: 8px;
    cursor: pointer;
  }
  .chat-item:hover { background: rgba(127,127,127,.13); }
  .chat-item[aria-current="true"] { background: rgba(127,127,127,.2); }
  .chat-title { display: block; font-weight: 550; }
  .chat-preview {
    display: block;
    color: var(--muted);
    font-size: 12.5px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  #main { flex: 1 1 auto; display: flex; flex-direction: column; min-width: 0; min-height: 0; }
  #header {
    padding: 13px 22px;
    border-bottom: 1px solid var(--line);
    font-weight: 600;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  #transcript { flex: 1 1 auto; overflow-y: auto; padding: 22px; min-height: 0; }
  .turn { max-width: 760px; margin: 0 auto 18px; display: flex; gap: 12px; }
  .who {
    flex: 0 0 34px; height: 34px; width: 34px;
    border-radius: 50%;
    display: grid; place-items: center;
    font-size: 11px; font-weight: 700; letter-spacing: .4px;
    background: rgba(127,127,127,.18);
    color: var(--muted);
  }
  .bubble { white-space: pre-wrap; word-wrap: break-word; overflow-wrap: anywhere; padding-top: 6px; }
  .turn.user .bubble { background: var(--user); border-radius: 10px; padding: 9px 13px; }
  .turn.system .bubble {
    border-left: 3px solid var(--muted);
    padding-left: 11px;
    color: var(--muted);
    font-style: italic;
  }
  .turn.system .who { background: transparent; border: 1px dashed var(--line); }
  #composer { border-top: 1px solid var(--line); padding: 14px 22px 20px; }
  #composer form { max-width: 760px; margin: 0 auto; display: flex; gap: 10px; }
  #text {
    flex: 1 1 auto;
    resize: none;
    /* One line to start; grows with its text to about seven lines, then scrolls
       inside (fitComposer below sets the height). */
    line-height: 1.55;
    min-height: calc(1.55em + 26px);
    max-height: calc(7 * 1.55em + 26px);
    overflow-y: auto;
    padding: 12px 14px;
    border: 1px solid var(--line);
    border-radius: 10px;
    background: var(--bg);
    color: var(--ink);
    font: inherit;
  }
  #send {
    flex: 0 0 auto;
    padding: 0 18px;
    border: 0;
    border-radius: 10px;
    background: var(--accent);
    color: #fff;
    font: inherit;
    font-weight: 600;
    cursor: pointer;
  }
  #send:disabled { opacity: .5; cursor: default; }
  #empty, #problem { max-width: 760px; margin: 60px auto; color: var(--muted); text-align: center; }
  #refusal { max-width: 760px; margin: 0 auto 10px; color: var(--muted); }
  #refusal button {
    margin-left: 8px; padding: 4px 10px; border: 1px solid var(--line); border-radius: 8px;
    background: var(--bg); color: var(--ink); font: inherit; cursor: pointer;
  }
  #problem { color: #c0392b; white-space: pre-wrap; text-align: left; }
</style>
</head>
<body>
<nav id="sidebar">
  <div id="brand">Dory-wrangler<small>v0.1 &middot; one agent per chat</small></div>
  <button id="newchat" type="button">+ New chat</button>
  <div id="chats"></div>
</nav>
<main id="main">
  <div id="header">Dory-wrangler</div>
  <div id="transcript"><div id="empty">Start a new chat, or pick one on the left.</div></div>
  <div id="composer">
    <div id="refusal" hidden><span id="refusal-text"></span><button id="abandon" type="button">Abandon the agent</button></div>
    <form id="form">
      <textarea id="text" rows="1" placeholder="Send a message" autocomplete="off"></textarea>
      <button id="send" type="submit">Send</button>
    </form>
  </div>
</main>
<script>
"use strict";
var activeChatId = null;

function api(method, path, body) {
  return fetch(path, {
    method: method,
    headers: body ? {"Content-Type": "application/json"} : {},
    body: body ? JSON.stringify(body) : undefined
  }).then(function (response) {
    return response.text().then(function (text) {
      var payload = null;
      try { payload = text ? JSON.parse(text) : null; } catch (e) { payload = null; }
      if (!response.ok) {
        var message = payload && payload.error ? payload.error : (text || response.statusText);
        var failure = new Error(message);
        failure.refused = !!(payload && payload.refused);
        throw failure;
      }
      return payload;
    });
  });
}

function element(tag, className, text) {
  var node = document.createElement(tag);
  if (className) { node.className = className; }
  if (text !== undefined && text !== null) { node.textContent = text; }
  return node;
}

function showProblem(err) {
  var transcript = document.getElementById("transcript");
  transcript.textContent = "";
  var panel = element("div", null,
    "This chat could not be read, and nothing has been changed.\n\n" + err.message);
  panel.id = "problem";
  transcript.appendChild(panel);
}

function hideRefusal() {
  document.getElementById("refusal").hidden = true;
}

function showRefusal(err) {
  document.getElementById("refusal-text").textContent = err.message;
  document.getElementById("refusal").hidden = false;
}

function renderChatList(chats) {
  var list = document.getElementById("chats");
  list.textContent = "";
  chats.forEach(function (chat) {
    var item = element("button", "chat-item");
    item.type = "button";
    item.setAttribute("aria-current", chat.chat_id === activeChatId ? "true" : "false");
    item.appendChild(element("span", "chat-title", chat.title));
    item.appendChild(element("span", "chat-preview", chat.preview || "No messages yet"));
    item.addEventListener("click", function () { openChat(chat.chat_id); });
    list.appendChild(item);
  });
}

var LABEL = {user: "YOU", agent: "AGENT", system: "SYSTEM"};

function renderChat(chat) {
  document.getElementById("header").textContent = chat.title;
  var transcript = document.getElementById("transcript");
  transcript.textContent = "";
  if (!chat.messages.length) {
    var blank = element("div", null, "No messages in this chat yet.");
    blank.id = "empty";
    transcript.appendChild(blank);
    return;
  }
  chat.messages.forEach(function (message) {
    var turn = element("div", "turn " + message.author);
    turn.appendChild(element("div", "who", LABEL[message.author] || message.author));
    turn.appendChild(element("div", "bubble", message.text));
    transcript.appendChild(turn);
  });
  transcript.scrollTop = transcript.scrollHeight;
}

function refreshList() {
  return api("GET", "/api/chats").then(renderChatList);
}

function openChat(chatId) {
  return api("GET", "/api/chats/" + encodeURIComponent(chatId))
    .then(function (chat) {
      if (chat.chat_id !== activeChatId) { hideRefusal(); }
      activeChatId = chat.chat_id;
      renderChat(chat);
      return refreshList();
    })
    .catch(showProblem);
}

document.getElementById("newchat").addEventListener("click", function () {
  api("POST", "/api/chats", {}).then(function (chat) {
    return openChat(chat.chat_id);
  }).catch(showProblem);
});

document.getElementById("form").addEventListener("submit", function (event) {
  event.preventDefault();
  var box = document.getElementById("text");
  var text = box.value.trim();
  if (!text) { return; }
  var send = function (chatId) {
    return api("POST", "/api/chats/" + encodeURIComponent(chatId) + "/messages", {text: text})
      .then(function (chat) {
        activeChatId = chat.chat_id;
        box.value = "";
        fitComposer();
        renderChat(chat);
        return refreshList();
      });
  };
  document.getElementById("send").disabled = true;
  var started = activeChatId
    ? send(activeChatId)
    : api("POST", "/api/chats", {}).then(function (chat) { activeChatId = chat.chat_id; return send(chat.chat_id); });
  hideRefusal();
  started.catch(function (err) {
    if (err.refused) { showRefusal(err); } else { showProblem(err); }
  }).then(function () {
    document.getElementById("send").disabled = false;
  });
});

document.getElementById("abandon").addEventListener("click", function () {
  if (!activeChatId) { hideRefusal(); return; }
  api("POST", "/api/chats/" + encodeURIComponent(activeChatId) + "/abandon", {})
    .then(function (chat) {
      hideRefusal();
      renderChat(chat);
      return refreshList();
    })
    .catch(function (err) {
      if (err.refused) { showRefusal(err); } else { showProblem(err); }
    });
});

// The composer grows with its text as it wraps, up to the max-height in the
// stylesheet (about seven lines), then scrolls inside; it shrinks as text is
// removed and when a sent message clears it.
function fitComposer() {
  var box = document.getElementById("text");
  box.style.height = "auto";
  var limit = parseFloat(window.getComputedStyle(box).maxHeight);
  var wanted = box.scrollHeight + box.offsetHeight - box.clientHeight;
  box.style.height = Math.min(wanted, limit) + "px";
  box.style.overflowY = wanted > limit ? "auto" : "hidden";
}

document.getElementById("text").addEventListener("input", fitComposer);
window.addEventListener("resize", fitComposer);
fitComposer();

document.getElementById("text").addEventListener("keydown", function (event) {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    document.getElementById("form").dispatchEvent(new Event("submit", {cancelable: true}));
  }
});

refreshList().then(function () {
  return api("GET", "/api/chats");
}).then(function (chats) {
  if (chats.length) { return openChat(chats[0].chat_id); }
}).catch(showProblem);
</script>
</body>
</html>
"""


class ShellHandler(BaseHTTPRequestHandler):
    server_version = "dory-wrangler/0.1"
    protocol_version = "HTTP/1.1"

    # -- plumbing -------------------------------------------------------

    @property
    def service(self):
        return self.server.service

    def log_message(self, fmt, *args):
        if self.server.quiet:
            return
        BaseHTTPRequestHandler.log_message(self, fmt, *args)

    def _respond(self, status, payload=None, content_type="application/json", raw=None):
        if raw is None:
            body = json.dumps(payload).encode("utf-8") if payload is not None else b""
        else:
            body = raw
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _read_json(self):
        length = self.headers.get("Content-Length")
        if not length:
            return {}
        try:
            length = int(length)
        except ValueError:
            raise _BadRequest(BAD_LENGTH)
        if length > MAX_BODY_BYTES:
            raise _BadRequest(TOO_LARGE)
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise _BadRequest(NOT_JSON)
        if not isinstance(body, dict):
            raise _BadRequest(NOT_AN_OBJECT)
        return body

    def _failed(self):
        """An exception no route anticipated: 500, fixed words, detail to stderr."""
        if not self.server.quiet:
            traceback.print_exc(file=sys.stderr)
        return self._respond(500, {"error": FAILED})

    # -- routes ---------------------------------------------------------

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
        path = urlparse(self.path).path
        try:
            if path == "/" or path == "/index.html":
                return self._respond(200, raw=PAGE.encode("utf-8"),
                                     content_type="text/html; charset=utf-8")
            if path == "/healthz":
                return self._respond(200, {"status": "ok"})
            if path == "/api/chats":
                return self._respond(200, self.service.list_chats())
            if path.startswith("/api/chats/"):
                chat_id = path[len("/api/chats/"):]
                if "/" in chat_id:
                    return self._respond(404, {"error": NO_SUCH_ROUTE})
                return self._respond(200, self.service.open_chat(chat_id))
            return self._respond(404, {"error": NO_SUCH_ROUTE})
        except NotFound:
            return self._respond(404, {"error": NOT_FOUND})
        except StoreError:
            # Fail closed, and say so. Nothing partial is rendered and nothing
            # is repaired on the way out.
            return self._respond(409, {"error": UNREADABLE})
        except Exception:  # noqa: BLE001 - answered with fixed words, never its text
            return self._failed()

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        try:
            body = self._read_json()
        except _BadRequest as exc:
            return self._respond(400, {"error": exc.words})
        try:
            if path == "/api/chats":
                title = body.get("title")
                chat = self.service.create_chat(title if isinstance(title, str) and title.strip() else None)
                return self._respond(201, self.service.open_chat(chat["chat_id"]))
            if path.startswith("/api/chats/") and path.endswith("/messages"):
                chat_id = path[len("/api/chats/"):-len("/messages")]
                if "/" in chat_id:
                    return self._respond(404, {"error": NO_SUCH_ROUTE})
                text = body.get("text")
                if not isinstance(text, str) or not text.strip():
                    return self._respond(400, {"error": NEEDS_TEXT})
                return self._respond(201, self.service.send_user_message(chat_id, text.strip()))
            if path.startswith("/api/chats/") and path.endswith("/abandon"):
                chat_id = path[len("/api/chats/"):-len("/abandon")]
                if "/" in chat_id:
                    return self._respond(404, {"error": NO_SUCH_ROUTE})
                return self._respond(200, self.service.abandon(chat_id))
            return self._respond(404, {"error": NO_SUCH_ROUTE})
        except NotFound:
            return self._respond(404, {"error": NOT_FOUND})
        except StoreError:
            return self._respond(409, {"error": STORE_REFUSED})
        except HarnessError as exc:
            return self._respond(409, {"error": _refusal_words(exc), "refused": True})
        except LaunchBoundaryError:
            return self._respond(502, {"error": INTEGRATION_FAILED})
        except Exception:  # noqa: BLE001 - answered with fixed words, never its text
            return self._failed()


class _BadRequest(Exception):
    """A request the shell cannot parse. Carries one of the fixed sentences above."""

    def __init__(self, words):
        Exception.__init__(self, words)
        self.words = words


def _refusal_words(exc):
    if isinstance(exc, ConcurrentLaunchRefused):
        return REFUSED_BUSY
    if isinstance(exc, InstructionTooLarge):
        return REFUSED_TOO_LARGE
    return REFUSED_OTHER


class ShellServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, service, quiet=False):
        self.service = service
        self.quiet = quiet
        ThreadingHTTPServer.__init__(self, address, ShellHandler)

    def server_close(self):
        try:
            ThreadingHTTPServer.server_close(self)
        finally:
            # The server was the store's one serving process; closing it gives
            # the store-level lock back (decision 0002, D1).
            self.service.store.close()


def build_server(root, host="127.0.0.1", port=8765, quiet=False, launcher_config=None,
                 launcher=None):
    """The one served application: the shell, over the chat loop, over the store.

    In this order, and the order is the point (decision 0002, D1):

    1. **take the store.** The store-level lock is taken before anything is
       swept, re-attached or written. A second shell on a store another process
       serves is refused here, with `StoreInUse`, having changed nothing;
    2. **bind.** A shell that cannot serve does not re-attach anyone's agents;
    3. **re-attach**, once, before the first request is served (contract 5.4),
       so a chat a restart interrupted has already been carried to a state its
       user can act on by the time anyone opens it.
    """
    service = ChatService.open(root, launcher_config=launcher_config, launcher=launcher)
    try:
        service.store.acquire()
        server = ShellServer((host, port), service, quiet=quiet)
    except BaseException:
        service.store.close()
        raise
    try:
        service.sessions.reattach_on_start()
    except BaseException:
        server.server_close()
        raise
    return server

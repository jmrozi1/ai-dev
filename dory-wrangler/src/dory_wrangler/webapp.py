"""The chat shell: conversation list, active conversation, new-chat flow.

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

There is also no agent-launch mechanics here. #86 starts nothing.
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .errors import NotFound, StoreError
from .service import ChatService

MAX_BODY_BYTES = 1 << 20  # a user turn is text; a megabyte is already generous

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
    min-height: 46px;
    max-height: 180px;
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
    <form id="form">
      <textarea id="text" placeholder="Send a message" autocomplete="off"></textarea>
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
        throw new Error(message);
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
        renderChat(chat);
        return refreshList();
      });
  };
  document.getElementById("send").disabled = true;
  var started = activeChatId
    ? send(activeChatId)
    : api("POST", "/api/chats", {}).then(function (chat) { activeChatId = chat.chat_id; return send(chat.chat_id); });
  started.catch(showProblem).then(function () {
    document.getElementById("send").disabled = false;
  });
});

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
            raise ValueError("Content-Length is not a number")
        if length > MAX_BODY_BYTES:
            raise ValueError("request body is too large")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ValueError("request body is not JSON")
        if not isinstance(body, dict):
            raise ValueError("request body must be a JSON object")
        return body

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
                    return self._respond(404, {"error": "no such route"})
                return self._respond(200, self.service.open_chat(chat_id))
            return self._respond(404, {"error": "no such route"})
        except NotFound as exc:
            return self._respond(404, {"error": str(exc)})
        except StoreError as exc:
            # Fail closed, and say so. Nothing partial is rendered and nothing
            # is repaired on the way out.
            return self._respond(409, {"error": str(exc)})

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        try:
            body = self._read_json()
        except ValueError as exc:
            return self._respond(400, {"error": str(exc)})
        try:
            if path == "/api/chats":
                title = body.get("title")
                chat = self.service.create_chat(title if isinstance(title, str) and title.strip() else None)
                return self._respond(201, self.service.open_chat(chat["chat_id"]))
            if path.startswith("/api/chats/") and path.endswith("/messages"):
                chat_id = path[len("/api/chats/"):-len("/messages")]
                if "/" in chat_id:
                    return self._respond(404, {"error": "no such route"})
                text = body.get("text")
                if not isinstance(text, str) or not text.strip():
                    return self._respond(400, {"error": "a message needs text"})
                return self._respond(201, self.service.send_user_message(chat_id, text.strip()))
            return self._respond(404, {"error": "no such route"})
        except NotFound as exc:
            return self._respond(404, {"error": str(exc)})
        except StoreError as exc:
            return self._respond(409, {"error": str(exc)})


class ShellServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, service, quiet=False):
        self.service = service
        self.quiet = quiet
        ThreadingHTTPServer.__init__(self, address, ShellHandler)


def build_server(root, host="127.0.0.1", port=8765, quiet=False, turn_listener=None):
    service = ChatService.open(root, turn_listener=turn_listener)
    return ShellServer((host, port), service, quiet=quiet)

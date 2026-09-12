"""Drive the real chat shell over HTTP, as a separate process.

Used by the restart tests. Everything here talks to the shell the way a browser
would: over a socket, to a process that shares no memory with the test.
"""

import json
import os
import signal
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from helpers import RUN_SHELL


class ShellProcess(object):
    def __init__(self, root):
        self.root = root
        self.port_file = os.path.join(root, ".port")
        self.process = None
        self.port = None

    def start(self, timeout=20.0):
        if os.path.exists(self.port_file):
            os.unlink(self.port_file)
        self.process = subprocess.Popen(
            [sys.executable, RUN_SHELL, "--root", self.root,
             "--port", "0", "--port-file", self.port_file, "--quiet"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(self.port_file):
                with open(self.port_file) as handle:
                    text = handle.read().strip()
                if text:
                    self.port = int(text)
                    break
            if self.process.poll() is not None:
                _out, err = self.process.communicate()
                raise RuntimeError(
                    "shell exited immediately: %s" % err.decode("utf-8", "replace")
                )
            time.sleep(0.05)
        else:
            raise RuntimeError("shell did not start within %.1fs" % timeout)
        self.get("/healthz")
        return self

    def kill(self):
        """SIGKILL: no shutdown hook, no flush, no chance to write anything."""
        if self.process is None:
            return None
        pid = self.process.pid
        os.kill(pid, signal.SIGKILL)
        self.process.communicate()
        self.process = None
        self.port = None
        return pid

    # -- HTTP ----------------------------------------------------------

    def _request(self, method, path, payload=None):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        request = Request(url, data=data, headers=headers, method=method)
        with urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8")
            return response.status, json.loads(body) if body else None

    def get(self, path):
        return self._request("GET", path)

    def post(self, path, payload=None):
        return self._request("POST", path, payload if payload is not None else {})

    def raw(self, method, path, payload=None):
        """Status and undecoded body, for routes that do not serve JSON."""
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=15) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    def status_of(self, method, path, payload=None):
        """Return only the status code, treating an error response as data."""
        try:
            status, _body = self._request(method, path, payload)
            return status
        except HTTPError as exc:
            return exc.code

    def get_page(self):
        url = "http://127.0.0.1:%d/" % self.port
        with urlopen(url, timeout=15) as response:
            return response.status, response.read().decode("utf-8")


def children_of(pid):
    """Direct child pids of a process, from /proc. Empty means nothing was started."""
    found = []
    task_dir = "/proc/%d/task" % pid
    if not os.path.isdir(task_dir):
        return found
    for task in os.listdir(task_dir):
        path = os.path.join(task_dir, task, "children")
        try:
            with open(path) as handle:
                found.extend(int(p) for p in handle.read().split())
        except (IOError, OSError):
            continue
    return found


def is_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - not expected for our own child
        return True
    return True

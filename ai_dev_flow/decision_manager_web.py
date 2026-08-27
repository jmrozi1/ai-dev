"""Renders the accepted decision-queue projection as one self-contained desktop page."""

from __future__ import annotations

# This is presentation. Every fact it shows was decided by `decision_queue`, and
# nothing here re-derives one.
#
# Four boundaries hold it honest.
#
# First, it consumes accepted types only. A `QueueView` supplies the rows, their
# order, and the filter set; `SelectedDetail` supplies the right pane. This module
# reads no control-plane prose, opens no repository, inspects no session, derives
# no age, and cannot produce a Waiting item -- there is no code path that
# constructs one.
#
# Second, the page carries every item's detail up front. The server is read-only
# and has no endpoint to ask for more, so selecting a different row must not need
# a round trip. The caller supplies those details from the accepted queue; this
# module refuses any it did not receive rather than synthesizing one.
#
# Third, fixture data is hostile until proved otherwise. Titles, explanations,
# evidence, and identities are published prose that a person wrote, so they are
# delivered as JSON inside a non-executable block with the characters that could
# end that block escaped, and the page inserts them as text nodes only. The
# content-security policy is emitted with the exact hash of the one inline script
# and the one inline stylesheet, so no other script can run and no external
# request can be made.
#
# Fourth, a fixture submission is presentation, not success. It removes the
# selected item from the page's own memory and moves on. Nothing is stored,
# nothing is transmitted, no endpoint exists to receive it, and the page never
# claims otherwise. Real response routing is a later seam.

import base64
import hashlib
import http.server
import json
import re
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

from .decision_queue import (
    QUEUE_STATES,
    STATE_WAITING,
    QueueView,
    SelectedDetail,
)

__all__ = [
    "LOOPBACK_HOST",
    "PAGE_PATH",
    "RenderError",
    "build_payload",
    "make_server",
    "render_page",
    "serialize_payload",
]

# The only interface this module will bind. Not a default a caller can widen:
# a development surface that answers off-host is a different product decision.
LOOPBACK_HOST = "127.0.0.1"

# The one path the server owns.
PAGE_PATH = "/"

TEMPLATE_NAME = "decision_manager.html"

# Placeholders the template reserves for rendered content.
CSP_PLACEHOLDER = "<!--CONTENT-SECURITY-POLICY-->"
PAYLOAD_PLACEHOLDER = "<!--QUEUE-PAYLOAD-->"

# Characters that could end the JSON block early or be read as a line terminator
# by a JavaScript parser. Escaped rather than stripped, so hostile text still
# displays exactly as written.
_JSON_HTML_ESCAPES = (
    ("<", "\\u003c"),
    (">", "\\u003e"),
    ("&", "\\u0026"),
    ("\u2028", "\\u2028"),
    ("\u2029", "\\u2029"),
)

REASON_INVALID_VIEW = "invalid-view"
REASON_INVALID_DETAIL = "invalid-detail"
REASON_DETAIL_MISSING = "detail-missing"
REASON_DETAIL_UNKNOWN = "detail-unknown-item"
REASON_TEMPLATE_MALFORMED = "template-malformed"
REASON_NOT_LOOPBACK = "host-not-loopback"


class RenderError(Exception):
    """A refusal to render or serve, carrying the exact reason."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__("{0}: {1}".format(reason, detail))
        self.reason = reason
        self.detail = detail


# --------------------------------------------------------------------------
# Payload
# --------------------------------------------------------------------------


def build_payload(
    view: QueueView, details: Mapping[str, SelectedDetail]
) -> "dict":
    """Reduce accepted projection objects to the exact data the page draws.

    The row order is the view's own -- oldest first, already decided -- and this
    module never sorts. Filtering in the page is membership against that ordered
    list, so "the oldest visible row" stays the first visible entry without any
    second ordering rule existing anywhere.
    """
    if type(view) is not QueueView:
        raise RenderError(
            REASON_INVALID_VIEW,
            "rendering consumes an accepted QueueView, got {0!r}".format(type(view).__name__),
        )

    row_ids = [row.item_id for row in view.rows]
    for item_id, detail in details.items():
        if type(detail) is not SelectedDetail:
            raise RenderError(
                REASON_INVALID_DETAIL,
                "detail for '{0}' must be an accepted SelectedDetail, got {1!r}".format(
                    item_id, type(detail).__name__
                ),
            )
        if item_id != detail.item_id:
            raise RenderError(
                REASON_INVALID_DETAIL,
                "detail keyed '{0}' carries identity '{1}'".format(item_id, detail.item_id),
            )
        if item_id not in row_ids:
            raise RenderError(
                REASON_DETAIL_UNKNOWN,
                "detail for '{0}' has no row in this view".format(item_id),
            )

    missing = [item_id for item_id in row_ids if item_id not in details]
    if missing:
        # Selecting a row must never need a request. A page missing one item's
        # detail would either fetch it or invent it, and neither is authorized.
        raise RenderError(
            REASON_DETAIL_MISSING,
            "no detail supplied for {0}; every row needs one because the page "
            "cannot ask for more".format(", ".join(missing)),
        )

    return {
        "states": list(QUEUE_STATES),
        "defaultFilters": [STATE_WAITING],
        "rows": [
            {
                "itemId": row.item_id,
                "state": row.state,
                "title": row.title,
                "project": row.project,
                "ticket": row.ticket,
                "elapsedSeconds": row.elapsed_seconds,
            }
            for row in view.rows
        ],
        "details": {
            row_id: {
                "state": details[row_id].state,
                "explanation": details[row_id].explanation,
                "evidence": [
                    {"label": reference.label, "locator": reference.locator}
                    for reference in details[row_id].evidence
                ],
            }
            for row_id in row_ids
        },
    }


def serialize_payload(payload: Mapping) -> str:
    """JSON that cannot end its own block or introduce a statement."""
    text = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    for character, replacement in _JSON_HTML_ESCAPES:
        text = text.replace(character, replacement)
    return text


# --------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------


def _template_path(override: Optional[Path]) -> Path:
    return Path(override) if override is not None else Path(__file__).with_name(TEMPLATE_NAME)


def _inline_blocks(template: str, tag: str) -> Tuple[Tuple[str, str], ...]:
    """Each inline block as (attributes, body)."""
    pattern = re.compile(r"<{0}([^>]*)>(.*?)</{0}>".format(tag), re.DOTALL)
    return tuple(pattern.findall(template))


def _executable_scripts(template: str) -> Tuple[str, ...]:
    """Only blocks a browser will run.

    The payload ships in a `type="application/json"` block, which is data rather
    than code and is never executed, so it is deliberately not hashed: its content
    changes with every view, and a policy carrying a hash of data would be stale
    the moment it was written.
    """
    executable = []
    for attributes, body in _inline_blocks(template, "script"):
        if "type=" in attributes and "javascript" not in attributes:
            continue
        if body.strip():
            executable.append(body)
    return tuple(executable)


def _hash_source(source: str) -> str:
    digest = hashlib.sha256(source.encode("utf-8")).digest()
    return "'sha256-{0}'".format(base64.b64encode(digest).decode("ascii"))


def _policy(template: str) -> str:
    """A policy naming the exact inline blocks this page ships, and nothing else.

    Hashes rather than 'unsafe-inline': the point of the policy is that only the
    code shipped here may run, and 'unsafe-inline' would readmit anything that
    ever reached the document.
    """
    scripts = list(_executable_scripts(template))
    styles = [body for _, body in _inline_blocks(template, "style") if body.strip()]
    if not scripts or not styles:
        raise RenderError(
            REASON_TEMPLATE_MALFORMED,
            "the template must ship exactly the inline script and style it declares",
        )
    directives = [
        "default-src 'none'",
        "script-src " + " ".join(_hash_source(block) for block in scripts),
        "style-src " + " ".join(_hash_source(block) for block in styles),
        "img-src 'none'",
        "font-src 'none'",
        "connect-src 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
    ]
    return '<meta http-equiv="Content-Security-Policy" content="{0}">'.format("; ".join(directives))


def render_page(
    view: QueueView,
    details: Mapping[str, SelectedDetail],
    *,
    template_path: Optional[Path] = None,
) -> str:
    """The complete page: template, its policy, and this view's data."""
    template = _template_path(template_path).read_text(encoding="utf-8")
    for placeholder in (CSP_PLACEHOLDER, PAYLOAD_PLACEHOLDER):
        if template.count(placeholder) != 1:
            raise RenderError(
                REASON_TEMPLATE_MALFORMED,
                "the template must contain exactly one {0}".format(placeholder),
            )

    payload = serialize_payload(build_payload(view, details))
    # The policy is computed before the payload lands, so hostile fixture text can
    # never change which code the policy admits.
    page = template.replace(CSP_PLACEHOLDER, _policy(template))
    return page.replace(PAYLOAD_PLACEHOLDER, payload)


# --------------------------------------------------------------------------
# Development server
# --------------------------------------------------------------------------


class _PageHandler(http.server.BaseHTTPRequestHandler):
    """Answers one path with one document. There is nothing else to reach."""

    page = ""
    server_version = "ai-dev-decision-queue"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        if self.path.split("?", 1)[0] != PAGE_PATH:
            self.send_error(404, "Not found")
            return
        body = self.page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    # Every mutating method is the same refusal. A development surface that
    # accepted a response would be claiming a routing authority this rail does
    # not have, so there is deliberately no handler to reach.
    def _refuse(self) -> None:
        self.send_error(405, "This surface is read-only")

    do_POST = _refuse
    do_PUT = _refuse
    do_PATCH = _refuse
    do_DELETE = _refuse
    do_HEAD = _refuse
    do_OPTIONS = _refuse

    def log_message(self, format: str, *args: Any) -> None:
        """Silent by default; request logging is not evidence and not wanted here."""


def make_server(
    view: QueueView,
    details: Mapping[str, SelectedDetail],
    *,
    host: str = LOOPBACK_HOST,
    port: int = 0,
    template_path: Optional[Path] = None,
) -> http.server.HTTPServer:
    """A loopback-only server for one already-rendered view.

    The page is rendered once, here, from the caller's view. The server holds no
    queue, refreshes nothing, and cannot answer a question the page did not
    already have the answer to.
    """
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise RenderError(
            REASON_NOT_LOOPBACK,
            "this surface binds loopback only; '{0}' would answer off-host".format(host),
        )

    page = render_page(view, details, template_path=template_path)
    handler = type("_BoundPageHandler", (_PageHandler,), {"page": page})
    return http.server.HTTPServer((host, port), handler)


def serve_forever(server: http.server.HTTPServer) -> None:  # pragma: no cover - blocking
    """Run an already-constructed loopback server until it is shut down."""
    server.serve_forever()

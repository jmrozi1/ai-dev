"""`decision_manager_web` renders the accepted projection and claims nothing more.

These are deterministic tests over the Python surface and the exact page it emits.
No browser runs here, so nothing below asserts that a browser behaved -- it asserts
what the page *declares*. Visual and interaction acceptance is a separate,
human-relayed step.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Dict
from html.parser import HTMLParser
import ast
import contextlib
import http.client
import io
import json
import re
import threading
import unittest

from ai_dev_flow import decision_manager_web as web
from ai_dev_flow.decision_manager_web import (
    LOOPBACK_HOST,
    PAGE_PATH,
    RenderError,
    build_allowance,
    build_payload,
    make_live_server,
    make_observed_server,
    make_server,
    render_page,
    serialize_payload,
    start_serving,
)
from ai_dev_flow.attention_projection import (
    ACTIVITIES,
    ACTIVITY_BLOCKED,
    ACTIVITY_DISCONNECTED_RECOVERY,
    ACTIVITY_EXECUTOR_WORKING,
    OWNER_AGENT,
    OWNER_HUMAN,
)
from ai_dev_flow.claude_allowance import (
    HEALTH_CALIBRATED,
    HEALTH_PROVISIONAL,
    HEALTH_UNAVAILABLE,
    WINDOW_FIVE_HOUR,
    WINDOW_SEVEN_DAY,
)
from ai_dev_flow.claude_allowance_view import AllowanceWindowView
from ai_dev_flow.decision_queue import (
    QUEUE_STATES,
    ActionableBlocker,
    STATE_DISCONNECTED,
    STATE_RUNNING,
    STATE_WAITING,
    EvidenceReference,
    OperationalAgent,
    PendingDecision,
    SelectedDetail,
    build_queue,
)
from ai_dev_flow.session_lifecycle import SessionProjection

PROJECT = "ai-dev"
TICKET = "issue-55"

HOSTILE_TITLE = '</script><img src=x onerror="alert(1)">'
HOSTILE_EXPLANATION = "Ampersand & <b>bold</b> and   line separator"
HOSTILE_LOCATOR = '"><script>alert(2)</script>'
SESSION_SECRET = "session-11111111-that-must-never-reach-the-page"
LIFECYCLE_DETAIL = "LIFECYCLE-DETAIL-that-must-never-reach-the-page"
LIFECYCLE_REASON = "lifecycle-reason-that-must-never-reach-the-page"


def a_decision(**overrides) -> PendingDecision:
    base = dict(
        decision_id="d-1", project=PROJECT, ticket=TICKET, rail="issue-55-rail-one",
        raised_at="raised-1", title="Choose the credential route",
        explanation="The requirements do not say which credential the worker uses.",
        elapsed_seconds=7200,
        activity=ACTIVITY_BLOCKED,
        attention_owner=OWNER_HUMAN,
        evidence=(EvidenceReference(label="review", locator="rails/one/handoff.md"),),
    )
    base.update(overrides)
    return PendingDecision(**base)


def an_agent(**overrides) -> OperationalAgent:
    rail = overrides.pop("rail", "issue-55-rail-two")
    state = overrides.pop("state", STATE_RUNNING)
    elapsed = overrides.pop("elapsed_seconds", 300)
    activity = overrides.pop(
        "activity",
        ACTIVITY_EXECUTOR_WORKING if state == STATE_RUNNING
        else ACTIVITY_DISCONNECTED_RECOVERY,
    )
    base = dict(
        project=PROJECT, ticket=TICKET, rail=rail, title="Implement the seam",
        activity=activity, attention_owner=OWNER_AGENT,
    )
    base.update(overrides)
    return OperationalAgent(
        projection=SessionProjection(
            state=state, reason=LIFECYCLE_REASON, detail=LIFECYCLE_DETAIL,
            session_id=SESSION_SECRET, rail=rail, elapsed_seconds=elapsed,
        ),
        **base
    )


HOSTILE_REASON = '</script><img src=x onerror="alert(3)">'


def a_window(window=WINDOW_FIVE_HOUR, **overrides) -> AllowanceWindowView:
    """One accepted view. Provisional point by default; every field overridable."""
    base = dict(
        window=window,
        meter="claude-usage-percent",
        health=HEALTH_PROVISIONAL,
        reason="provisional-single-interval",
        point_percentage=Decimal("30"),
        lower_percentage=None,
        upper_percentage=None,
        bounded=False,
        resets_at=1_700_000_000,
        newest_calibration_at=1_699_999_000,
        interval_count=1,
        source_healthy=True,
    )
    base.update(overrides)
    return AllowanceWindowView(**base)


def a_range(window=WINDOW_FIVE_HOUR, lower="41.2", upper="46.8", **overrides):
    return a_window(
        window,
        health=HEALTH_CALIBRATED,
        reason="calibrated-interval-range",
        point_percentage=None,
        lower_percentage=Decimal(lower),
        upper_percentage=Decimal(upper),
        interval_count=2,
        **overrides
    )


def unavailable(window=WINDOW_FIVE_HOUR, reason="current-coverage-incomplete",
                *, source_healthy=True, **overrides):
    return a_window(
        window,
        health=HEALTH_UNAVAILABLE,
        reason=reason,
        point_percentage=None,
        lower_percentage=None,
        upper_percentage=None,
        resets_at=None,
        newest_calibration_at=None,
        interval_count=0,
        source_healthy=source_healthy,
        **overrides
    )


def an_allowance(five_hour=None, seven_day=None):
    """The two accepted views a caller hands the page."""
    return (
        a_window(WINDOW_FIVE_HOUR) if five_hour is None else five_hour,
        a_window(WINDOW_SEVEN_DAY) if seven_day is None else seven_day,
    )


def rendered(decisions=(), agents=(), *, filters=QUEUE_STATES, allowance=None):
    """One page plus the view and details it was built from."""
    queue = build_queue(list(decisions), list(agents))
    view = queue.view(filters=filters)
    details: Dict[str, SelectedDetail] = {}
    for row in view.rows:
        details[row.item_id] = queue.view(filters=filters, selected_id=row.item_id).detail
    page = render_page(
        view, details, allowance=an_allowance() if allowance is None else allowance
    )
    return page, view, details


def allowance_of(page: str):
    return payload_of(page)["allowance"]


def payload_of(page: str) -> dict:
    block = page.split('id="queue-payload">', 1)[1].split("</script>", 1)[0]
    return json.loads(block)


def script_of(page: str) -> str:
    return page.rsplit("<script>", 1)[1].split("</script>", 1)[0]


def reset_block(page: str) -> str:
    """Just the allowance draw, so a check cannot pass on unrelated script."""
    script = script_of(page)
    return script.split("function renderAllowance", 1)[1].split("function renderRows", 1)[0]


def code_only(block: str) -> str:
    """The block without // prose, so a comment neither satisfies nor breaks a check."""
    return " ".join(line.split("//", 1)[0] for line in block.splitlines())


def style_of(page: str) -> str:
    return page.split("<style>", 1)[1].split("</style>", 1)[0]


def policy_of(page: str) -> str:
    match = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', page)
    assert match is not None, "no content-security policy in the page"
    return match.group(1)


class _Ancestry(HTMLParser):
    """Records each element id's ancestor ids, so containment is parsed, not assumed."""

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
            "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._stack = []
        self.ancestors = {}
        self.tags = {}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        element_id = attributes.get("id")
        if element_id is not None:
            self.ancestors[element_id] = [entry for entry in self._stack if entry is not None]
            self.tags[element_id] = tag
        if tag not in self.VOID:
            self._stack.append(element_id)

    def handle_startendtag(self, tag, attrs):
        attributes = dict(attrs)
        element_id = attributes.get("id")
        if element_id is not None:
            self.ancestors[element_id] = [entry for entry in self._stack if entry is not None]
            self.tags[element_id] = tag

    def handle_endtag(self, tag):
        if tag not in self.VOID and self._stack:
            self._stack.pop()


def ancestry_of(page: str) -> _Ancestry:
    parser = _Ancestry()
    parser.feed(page)
    return parser


# --------------------------------------------------------------------------
# Serialization and escaping
# --------------------------------------------------------------------------


class SerializationTests(unittest.TestCase):
    def test_the_payload_carries_exactly_the_accepted_row_fields(self) -> None:
        page, _, _ = rendered([a_decision()], [an_agent()])
        payload = payload_of(page)
        self.assertEqual(
            set(payload["rows"][0]),
            {"itemId", "state", "title", "project", "ticket", "elapsedSeconds"},
        )
        self.assertEqual(
            set(payload["details"][payload["rows"][0]["itemId"]]),
            {"state", "activity", "attentionOwner", "explanation", "evidence",
             # D8's nine reach a person through the detail pane and nowhere else.
             # Three routing facts on every item, and the actionable half plus its
             # unavailability reason, which only a decision ever carries.
             "project", "ticket", "rail", "blocker", "blockerUnavailable"},
        )
        # And none of the new detail fields reached a row. The dense contract is
        # the row field set above, and D8 spent nothing out of it.
        for absent in ("rail", "blocker", "blockerUnavailable"):
            self.assertNotIn(absent, set(payload["rows"][0]), absent)
        # The row field set above is the dense contract, and neither of the two
        # new facts appears in it: activity and attention owner reach a person
        # through the filters and the detail pane, never as a row field.
        self.assertNotIn("activity", set(payload["rows"][0]))
        self.assertNotIn("attentionOwner", set(payload["rows"][0]))
        self.assertEqual(payload["defaultFilters"], [STATE_WAITING])
        self.assertEqual(payload["states"], list(QUEUE_STATES))

    def test_hostile_text_cannot_end_the_block_or_become_markup(self) -> None:
        """The property is that data cannot become code -- not that a scary word is absent."""
        page, _, _ = rendered(
            [a_decision(title=HOSTILE_TITLE, explanation=HOSTILE_EXPLANATION,
                        evidence=(EvidenceReference(label="e", locator=HOSTILE_LOCATOR),))],
        )
        block = page.split('id="queue-payload">', 1)[1].split("</script>", 1)[0]
        for character in ("<", ">", "&", "\u2028", "\u2029"):
            self.assertNotIn(character, block, repr(character))
        self.assertNotIn("</script", block)
        self.assertIn("\\u003c", block)
        # The words survive verbatim; they are simply inert.
        self.assertIn("onerror", block)
        self.assertNotIn("innerHTML", script_of(page))
        self.assertIn("textContent", script_of(page))

    def test_hostile_text_survives_escaping_exactly(self) -> None:
        """Escaped for transport, not censored: the person's words are preserved."""
        page, _, _ = rendered([a_decision(title=HOSTILE_TITLE, explanation=HOSTILE_EXPLANATION)])
        payload = payload_of(page)
        self.assertEqual(payload["rows"][0]["title"], HOSTILE_TITLE)
        detail = payload["details"][payload["rows"][0]["itemId"]]
        self.assertEqual(detail["explanation"], HOSTILE_EXPLANATION)

    def test_serialization_is_deterministic(self) -> None:
        _, view, details = rendered([a_decision()], [an_agent()])
        first = serialize_payload(build_payload(view, details, allowance=an_allowance()))
        second = serialize_payload(build_payload(view, details, allowance=an_allowance()))
        self.assertEqual(first, second)

    def test_a_foreign_view_or_detail_is_refused(self) -> None:
        _, view, details = rendered([a_decision()])
        with self.assertRaises(RenderError) as caught:
            build_payload(object(), details, allowance=an_allowance())
        self.assertEqual(caught.exception.reason, web.REASON_INVALID_VIEW)

        with self.assertRaises(RenderError) as caught:
            build_payload(view, {list(details)[0]: object()}, allowance=an_allowance())
        self.assertEqual(caught.exception.reason, web.REASON_INVALID_DETAIL)

    def test_a_row_without_detail_is_refused_rather_than_fetched_or_invented(self) -> None:
        _, view, _ = rendered([a_decision()])
        with self.assertRaises(RenderError) as caught:
            build_payload(view, {}, allowance=an_allowance())
        self.assertEqual(caught.exception.reason, web.REASON_DETAIL_MISSING)

    def test_a_detail_for_an_absent_row_is_refused(self) -> None:
        _, view, details = rendered([a_decision()])
        stray = dict(details)
        stray["not-a-row"] = SelectedDetail(
            item_id="not-a-row", state=STATE_WAITING,
            project="ai-dev", ticket="issue-55", rail="rail-stray",
            activity=ACTIVITY_BLOCKED, attention_owner=OWNER_HUMAN,
        )
        with self.assertRaises(RenderError) as caught:
            build_payload(view, stray, allowance=an_allowance())
        self.assertEqual(caught.exception.reason, web.REASON_DETAIL_UNKNOWN)

    def test_a_mislabelled_detail_is_refused(self) -> None:
        _, view, details = rendered([a_decision()])
        only = list(details)[0]
        with self.assertRaises(RenderError) as caught:
            build_payload(
                view,
                {only: SelectedDetail(
                    item_id="other", state=STATE_WAITING,
                    project="ai-dev", ticket="issue-55", rail="rail-other",
                    activity=ACTIVITY_BLOCKED, attention_owner=OWNER_HUMAN,
                )},
                allowance=an_allowance(),
            )
        self.assertEqual(caught.exception.reason, web.REASON_INVALID_DETAIL)


# --------------------------------------------------------------------------
# Server surface
# --------------------------------------------------------------------------


class ServerSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        _, view, details = rendered([a_decision()], [an_agent()])
        self.server = make_server(view, details, allowance=an_allowance(), port=0)
        self.addCleanup(self.server.server_close)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def request(self, method: str, path: str = PAGE_PATH):
        connection = http.client.HTTPConnection(LOOPBACK_HOST, self.port, timeout=5)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_it_binds_loopback_only(self) -> None:
        self.assertEqual(self.server.server_address[0], LOOPBACK_HOST)

    def test_a_non_loopback_bind_is_refused(self) -> None:
        _, view, details = rendered([a_decision()])
        for host in ("0.0.0.0", "::", "10.0.0.5", "example.invalid"):
            with self.subTest(host=host):
                with self.assertRaises(RenderError) as caught:
                    make_server(view, details, allowance=an_allowance(), host=host, port=0)
                self.assertEqual(caught.exception.reason, web.REASON_NOT_LOOPBACK)

    def test_the_page_is_served_at_exactly_one_path(self) -> None:
        status, body = self.request("GET")
        self.assertEqual(status, 200)
        self.assertIn(b"<!doctype html>", body)
        for path in ("/index.html", "/api", "/respond", "/queue.json", "/../etc/passwd"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 404)

    def test_every_mutating_method_is_refused(self) -> None:
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"):
            with self.subTest(method=method):
                self.assertEqual(self.request(method)[0], 405)

    def test_there_is_no_response_or_control_endpoint(self) -> None:
        """A surface that accepted a response would be claiming routing authority."""
        handler_names = [name for name in dir(web._PageHandler) if name.startswith("do_")]
        self.assertEqual(sorted(handler_names),
                         ["do_DELETE", "do_GET", "do_HEAD", "do_OPTIONS", "do_PATCH",
                          "do_POST", "do_PUT"])
        status, _ = self.request("POST", "/respond")
        self.assertEqual(status, 405)

    def test_the_server_holds_a_rendered_page_not_a_queue(self) -> None:
        """It cannot answer a question the page did not already have the answer to."""
        self.assertIsInstance(self.server.RequestHandlerClass.page, str)
        for attribute in ("queue", "view", "details", "store", "control_plane"):
            self.assertFalse(hasattr(self.server.RequestHandlerClass, attribute), attribute)


class LiveServerSurfaceTests(unittest.TestCase):
    """The one figure this server draws when it is asked, rather than when it was built."""

    def setUp(self) -> None:
        _, self.view, self.details = rendered([a_decision()], [an_agent()])
        # Construction takes one; then one full slot, then an empty one that
        # stays empty. Nothing here sleeps or waits: the occupancy simply changed
        # between two requests, which is the only thing being tested.
        self.readings = [
            {"permitted": 6, "current": 1, "reason": None},
            {"permitted": 6, "current": 1, "reason": None},
            {"permitted": 6, "current": 0, "reason": None},
        ]
        self.taken = []

        def reading():
            value = self.readings[min(len(self.taken), len(self.readings) - 1)]
            self.taken.append(value)
            return value

        self.server = make_live_server(
            self.view, self.details, allowance=an_allowance(), agents=reading, port=0
        )
        self.addCleanup(self.server.server_close)
        self.serving = start_serving(self.server)
        self.addCleanup(self.serving.stop)
        self.port = self.server.server_address[1]

    def fetch(self) -> dict:
        connection = http.client.HTTPConnection(LOOPBACK_HOST, self.port, timeout=5)
        try:
            connection.request("GET", PAGE_PATH)
            body = connection.getresponse().read().decode("utf-8")
        finally:
            connection.close()
        return payload_of(body)

    def test_the_reading_source_is_consulted_once_per_request(self) -> None:
        """Construction takes one, and then every client takes its own."""
        self.assertEqual(len(self.taken), 1)
        self.fetch()
        self.assertEqual(len(self.taken), 2)
        self.fetch()
        self.assertEqual(len(self.taken), 3)

    def test_a_later_client_is_told_the_later_truth(self) -> None:
        """The accuracy property: what changed between two fetches reaches the second."""
        self.assertEqual(self.fetch()["agents"], {"current": 1, "permitted": 6, "reason": None})
        self.assertEqual(self.fetch()["agents"], {"current": 0, "permitted": 6, "reason": None})

    def test_everything_but_the_reading_is_projected_once(self) -> None:
        """A queue row and an allowance window describe state that outlives the render."""
        first, second = self.fetch(), self.fetch()
        for key in ("rows", "details", "allowance", "states", "defaultFilters"):
            with self.subTest(key=key):
                self.assertEqual(first[key], second[key])
        self.assertNotEqual(first["agents"], second["agents"])

    def test_it_holds_a_renderer_not_a_queue(self) -> None:
        """Still no evidence on the handler: one document, and how to draw it."""
        for attribute in ("queue", "view", "details", "store", "control_plane"):
            self.assertFalse(hasattr(self.server.RequestHandlerClass, attribute), attribute)
        self.assertTrue(callable(self.server.RequestHandlerClass.document))

    def test_it_still_answers_exactly_one_path_and_refuses_every_mutation(self) -> None:
        connection = http.client.HTTPConnection(LOOPBACK_HOST, self.port, timeout=5)
        try:
            connection.request("GET", "/queue.json")
            self.assertEqual(connection.getresponse().status, 404)
        finally:
            connection.close()
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"):
            with self.subTest(method=method):
                connection = http.client.HTTPConnection(LOOPBACK_HOST, self.port, timeout=5)
                try:
                    connection.request(method, PAGE_PATH)
                    self.assertEqual(connection.getresponse().status, 405)
                finally:
                    connection.close()

    def test_a_non_loopback_bind_is_refused_here_too(self) -> None:
        for host in ("0.0.0.0", "::", "10.0.0.5"):
            with self.subTest(host=host):
                with self.assertRaises(RenderError) as caught:
                    make_live_server(
                        self.view, self.details, allowance=an_allowance(),
                        agents=lambda: self.readings[0], host=host, port=0,
                    )
                self.assertEqual(caught.exception.reason, web.REASON_NOT_LOOPBACK)

    def test_an_unusable_reading_is_refused_at_construction(self) -> None:
        """The same construction contract the frozen server already has."""
        with self.assertRaises(RenderError) as caught:
            make_live_server(
                self.view, self.details, allowance=an_allowance(),
                agents=lambda: {"permitted": 6, "current": None, "reason": None}, port=0,
            )
        self.assertEqual(caught.exception.reason, web.REASON_INVALID_AGENTS)

    def test_a_failing_reading_source_is_never_answered_with_a_stale_number(self) -> None:
        """A source that breaks must not be papered over with the last good count."""
        broken = []

        def reading():
            if broken:
                raise RuntimeError("the store could not be read")
            broken.append(True)
            return {"permitted": 6, "current": 1, "reason": None}

        server = make_live_server(
            self.view, self.details, allowance=an_allowance(), agents=reading, port=0
        )
        self.addCleanup(server.server_close)
        serving = start_serving(server)
        self.addCleanup(serving.stop)
        port = server.server_address[1]

        connection = http.client.HTTPConnection(LOOPBACK_HOST, port, timeout=5)
        self.addCleanup(connection.close)
        # The accepted socket loop reports the failure on stderr; that is its
        # behavior, not this test's subject, so it is captured rather than printed.
        with contextlib.redirect_stderr(io.StringIO()):
            connection.request("GET", PAGE_PATH)
            with self.assertRaises(Exception):
                response = connection.getresponse()
                if response.status == 200:
                    raise AssertionError(
                        "a stale page was served for a reading that could not be taken"
                    )
                raise ConnectionError(response.status)


class ServingTests(unittest.TestCase):
    """Starting and stopping the accepted socket loop, and nothing more."""

    def setUp(self) -> None:
        _, view, details = rendered([a_decision()])
        self.server = make_server(view, details, allowance=an_allowance(), port=0)
        self.addCleanup(self.server.server_close)

    def test_it_answers_from_the_moment_it_is_started(self) -> None:
        serving = start_serving(self.server)
        self.addCleanup(serving.stop)
        self.assertTrue(serving.answering())
        connection = http.client.HTTPConnection(
            LOOPBACK_HOST, self.server.server_address[1], timeout=5
        )
        try:
            connection.request("GET", PAGE_PATH)
            self.assertEqual(connection.getresponse().status, 200)
        finally:
            connection.close()

    def test_stopping_ends_the_loop_and_does_not_return_before_it_has(self) -> None:
        serving = start_serving(self.server)
        serving.stop()
        self.assertFalse(serving.answering())


# --------------------------------------------------------------------------
# Page structure: what is present, and what must never be
# --------------------------------------------------------------------------


class ActivityAndAttentionPresentationTests(unittest.TestCase):
    """The two checkpoint-7 facts are drawn in Details, and nowhere near a row."""

    def setUp(self) -> None:
        self.page, self.view, _ = rendered([a_decision()], [an_agent()])
        self.payload = payload_of(self.page)

    def test_both_facts_reach_the_payload_for_every_row(self) -> None:
        for row in self.payload["rows"]:
            detail = self.payload["details"][row["itemId"]]
            with self.subTest(item=row["itemId"]):
                self.assertIn(detail["activity"], list(ACTIVITIES))
                self.assertIn(detail["attentionOwner"], [OWNER_HUMAN, OWNER_AGENT])

    def test_the_waiting_row_is_human_owned_and_the_operational_row_is_not(self) -> None:
        owners = {
            row["state"]: self.payload["details"][row["itemId"]]["attentionOwner"]
            for row in self.payload["rows"]
        }
        self.assertEqual(owners[STATE_WAITING], OWNER_HUMAN)
        self.assertEqual(owners[STATE_RUNNING], OWNER_AGENT)

    def test_no_row_in_the_payload_carries_either_fact(self) -> None:
        for row in self.payload["rows"]:
            self.assertNotIn("activity", row)
            self.assertNotIn("attentionOwner", row)

    def test_the_facts_are_drawn_inside_the_existing_details_disclosure(self) -> None:
        details_block = self.page.split("<details id=\"details\">", 1)[1].split(
            "</details>", 1
        )[0]
        self.assertIn('id="facts"', details_block)
        self.assertIn('id="evidence"', details_block)
        # The disclosure is the only place the list is placed in the document.
        self.assertEqual(self.page.count('id="facts"'), 1)

    def test_the_page_adds_no_card_circle_inspector_console_or_sort_control(self) -> None:
        for forbidden in ("card", "circle", "inspector", "console"):
            self.assertNotIn(forbidden, self.page.lower(), forbidden)
        # A sort control would be a second ordering rule beside the projection's.
        # The word itself appears in a comment about not sorting, so the check is
        # on controls and handles rather than on the substring.
        for forbidden in ("<select", 'id="sort', "data-sort", "sortBy", "sortOrder"):
            self.assertNotIn(forbidden, self.page, forbidden)

    def test_the_page_draws_the_two_facts_apart_rather_than_combining_them(self) -> None:
        """Neither is used to choose, colour, or hide the other anywhere in the script."""
        script = self.page.split('<script>', 1)[1]
        for combination in ("detail.activity ===", "detail.attentionOwner ===",
                            "activity ==", "attentionOwner =="):
            self.assertNotIn(combination, script, combination)


class PageStructureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.page, _, _ = rendered([a_decision()], [an_agent()])

    def test_the_three_filters_are_independent_checkboxes_with_waiting_checked(self) -> None:
        for state in QUEUE_STATES:
            with self.subTest(state=state):
                match = re.search(
                    r'<input type="checkbox" id="filter-{0}"[^>]*>'.format(state), self.page)
                self.assertIsNotNone(match, state)
                self.assertIn('data-state="{0}"'.format(state), match.group(0))
                self.assertEqual("checked" in match.group(0), state == STATE_WAITING)
        self.assertEqual(self.page.count('type="checkbox"'), len(QUEUE_STATES))

    def test_the_response_controls_are_exactly_one_textarea_and_one_send(self) -> None:
        self.assertEqual(self.page.count("<textarea"), 1)
        self.assertEqual(self.page.count("<button"), 1)
        self.assertIn(">Send</button>", self.page)

    def test_details_ships_collapsed(self) -> None:
        match = re.search(r"<details[^>]*>", self.page)
        self.assertIsNotNone(match)
        self.assertNotIn("open", match.group(0))
        self.assertIn("<summary>Details</summary>", self.page)

    def test_the_forbidden_controls_are_absent(self) -> None:
        # `allowance` is deliberately not forbidden here any more: this rail puts a
        # read-only usage summary on the page. What stays forbidden is anything that
        # would act -- a control, a router, or an inspector.
        forbidden = ("Accept", "Reject", "Approve", "Deny", "Ask for context", "Defer",
                     "Retry", "Snooze", "Escalate", "Session inspector", "Transcript",
                     "Console", "Sort", "Search", "Next page", "Previous page")
        for label in forbidden:
            with self.subTest(label=label):
                self.assertNotIn(label, self.page)

    def test_no_sort_search_or_pagination_control_exists(self) -> None:
        self.assertNotIn('type="search"', self.page)
        self.assertNotIn("<select", self.page)
        self.assertEqual(self.page.count("<input"), len(QUEUE_STATES))

    def test_no_status_icon_or_state_label_is_rendered_in_a_row(self) -> None:
        script = script_of(self.page)
        row_block = script.split("function renderRows", 1)[1].split("function renderDetail", 1)[0]
        self.assertNotIn("item.state", row_block.replace('item.itemId === selectedId', ""))
        self.assertNotIn("<svg", self.page)
        self.assertNotIn("badge", self.page)

    def test_a_row_declares_only_title_context_and_elapsed(self) -> None:
        script = script_of(self.page)
        row_block = script.split("function renderRows", 1)[1].split("function renderDetail", 1)[0]
        self.assertIn("item.title", row_block)
        self.assertIn("item.project", row_block)
        self.assertIn("item.ticket", row_block)
        self.assertIn("item.elapsedSeconds", row_block)
        for absent in ("explanation", "evidence", "sessionId", "reason", "detail"):
            self.assertNotIn(absent, row_block, absent)

    def test_the_detail_pane_does_not_repeat_the_title(self) -> None:
        script = script_of(self.page)
        detail_block = script.split("function renderDetail", 1)[1].split("function render(", 1)[0]
        self.assertNotIn("item.title", detail_block)
        self.assertNotIn(".title", detail_block)

    def test_the_explanation_precedes_the_response_and_details(self) -> None:
        self.assertLess(self.page.index('id="explanation"'), self.page.index("<textarea"))
        self.assertLess(self.page.index("<textarea"), self.page.index("<details"))

    def test_the_content_security_policy_names_only_this_pages_own_code(self) -> None:
        policy = policy_of(self.page)
        self.assertIn("default-src 'none'", policy)
        self.assertIn("connect-src 'none'", policy)
        self.assertIn("form-action 'none'", policy)
        self.assertIn("base-uri 'none'", policy)
        self.assertIn("frame-ancestors 'none'", policy)
        self.assertNotIn("unsafe-inline", policy)
        self.assertNotIn("unsafe-eval", policy)
        self.assertNotIn("*", policy)
        self.assertEqual(policy.count("'sha256-"), 2)

    def test_the_policy_hashes_match_the_blocks_actually_shipped(self) -> None:
        policy = policy_of(self.page)
        self.assertIn(web._hash_source(script_of(self.page)), policy)
        self.assertIn(web._hash_source(style_of(self.page)), policy)

    def test_a_template_missing_a_placeholder_is_refused(self) -> None:
        _, view, details = rendered([a_decision()])
        broken = Path(__file__).with_name("_broken_template.html")
        broken.write_text("<html><style>a{}</style><script>1</script></html>", encoding="utf-8")
        self.addCleanup(broken.unlink)
        with self.assertRaises(RenderError) as caught:
            render_page(view, details, allowance=an_allowance(), template_path=broken)
        self.assertEqual(caught.exception.reason, web.REASON_TEMPLATE_MALFORMED)


# --------------------------------------------------------------------------
# Declared interaction behavior
# --------------------------------------------------------------------------


class DeclaredInteractionTests(unittest.TestCase):
    """The page's own logic, read from the script it ships. No browser runs here."""

    def setUp(self) -> None:
        self.page, _, _ = rendered([a_decision()], [an_agent()])
        self.script = script_of(self.page)

    def test_the_initial_filter_state_comes_from_the_payload_default(self) -> None:
        self.assertIn("payload.defaultFilters.indexOf(state) !== -1", self.script)
        self.assertEqual(payload_of(self.page)["defaultFilters"], [STATE_WAITING])

    def test_filtering_is_independent_membership_and_never_reorders(self) -> None:
        self.assertIn("items.filter(function (item) { return filters[item.state]; })", self.script)
        for reordering in (".sort(", "reverse()", "elapsedSeconds -", "- b."):
            self.assertNotIn(reordering, self.script, reordering)

    def test_selection_falls_back_to_the_first_visible_row(self) -> None:
        self.assertIn("selectedId = list.length ? list[0].itemId : null;", self.script)
        self.assertIn("var stillVisible = list.some(", self.script)

    def test_an_invisible_selection_is_never_retained(self) -> None:
        block = self.script.split("function render(", 1)[1].split("function select(", 1)[0]
        self.assertIn("if (!stillVisible)", block)

    def test_enter_submits_and_shift_enter_inserts_a_newline(self) -> None:
        block = self.script.split('inputEl.addEventListener("keydown"', 1)[1]
        self.assertIn('event.key !== "Enter" || event.shiftKey', block)
        self.assertIn("event.preventDefault();", block)
        self.assertIn("submit();", block)

    def test_an_empty_or_whitespace_response_fails_inline_and_stays_put(self) -> None:
        block = self.script.split("function submit(", 1)[1].split("payload.states.forEach", 1)[0]
        self.assertIn('response.trim() === ""', block)
        self.assertIn("failureEl.hidden = false;", block)
        failure_index = block.index('response.trim() === ""')
        self.assertLess(failure_index, block.index("items.splice"))
        self.assertIn("return;", block[failure_index:block.index("items.splice")])

    def test_a_sent_fixture_response_leaves_memory_and_is_never_stored(self) -> None:
        block = self.script.split("function submit(", 1)[1].split("payload.states.forEach", 1)[0]
        self.assertIn("items.splice(index, 1);", block)
        self.assertIn("selectedId = null;", block)
        self.assertIn('inputEl.value = "";', block)
        for persistence in ("localStorage", "sessionStorage", "indexedDB", "document.cookie",
                            "fetch(", "XMLHttpRequest", "navigator.sendBeacon", "WebSocket"):
            self.assertNotIn(persistence, self.script, persistence)

    def test_no_success_banner_is_declared(self) -> None:
        for claim in ("Sent", "Success", "Delivered", "Saved", "Submitted", "Thanks"):
            self.assertNotIn(claim, self.page, claim)

    def test_send_is_unavailable_for_an_operational_selection(self) -> None:
        self.assertIn("sendEl.disabled = !waiting;", self.script)
        self.assertIn("inputEl.disabled = !waiting;", self.script)
        self.assertIn("if (sendEl.disabled) { return; }", self.script)

    def test_an_operational_detail_carries_no_explanation(self) -> None:
        page, _, _ = rendered([], [an_agent()])
        payload = payload_of(page)
        only = payload["rows"][0]["itemId"]
        self.assertIsNone(payload["details"][only]["explanation"])
        self.assertEqual(payload["details"][only]["evidence"], [])

    def test_content_is_inserted_as_text_never_as_markup(self) -> None:
        self.assertNotIn("innerHTML", self.script)
        self.assertNotIn("outerHTML", self.script)
        self.assertNotIn("insertAdjacentHTML", self.script)
        self.assertNotIn("document.write", self.script)
        self.assertGreaterEqual(self.script.count("textContent"), 5)


# --------------------------------------------------------------------------
# The response composer is a single state-dependent unit
# --------------------------------------------------------------------------


class OperationalComposerTests(unittest.TestCase):
    """No response action exists for an operational row, so nothing offers one."""

    COMPOSER_CHILDREN = ("response-input", "response-failure", "response-hint", "send")

    def setUp(self) -> None:
        self.page, _, _ = rendered([a_decision()], [an_agent()])
        self.script = script_of(self.page)
        self.ancestry = ancestry_of(self.page)

    def test_every_response_control_lives_inside_the_one_composer(self) -> None:
        self.assertEqual(self.ancestry.tags.get("response"), "form")
        for child in self.COMPOSER_CHILDREN:
            with self.subTest(child=child):
                self.assertIn("response", self.ancestry.ancestors.get(child, []), child)

    def test_the_response_label_is_inside_the_composer_too(self) -> None:
        """The label has no id, so its containment is checked in the markup itself."""
        form = self.page.split('<form class="response" id="response">', 1)[1]
        form = form.split("</form>", 1)[0]
        self.assertIn('<label for="response-input">Response</label>', form)

    def test_the_whole_composer_is_hidden_for_an_operational_selection(self) -> None:
        self.assertIn("formEl.hidden = !waiting;", self.script)

    def test_no_response_control_is_hidden_independently_of_the_composer(self) -> None:
        """Five separately hidden children could drift out of agreement; one parent cannot.

        Scoped to the function that makes the state decision. The failure line is
        shown and cleared by validation elsewhere, which is accepted Waiting
        behavior and has nothing to do with whether responding is possible.
        """
        decision_block = self.script.split("function renderDetail", 1)[1] \
                                    .split("function render(", 1)[0]
        for child in ("inputEl", "sendEl", "failureEl", "hintEl", "labelEl"):
            with self.subTest(child=child):
                self.assertNotIn("{0}.hidden".format(child), decision_block, child)
        self.assertEqual(decision_block.count("formEl.hidden = !waiting;"), 1)
        self.assertEqual(self.script.count("formEl.hidden"), 1)

    def test_validation_still_shows_and_clears_the_failure_line(self) -> None:
        """Preserved exactly: this is Waiting behavior, not composer visibility."""
        self.assertIn("failureEl.hidden = true;", self.script)
        self.assertIn("failureEl.hidden = false;", self.script)
    def test_the_controls_stay_disabled_as_a_backstop(self) -> None:
        self.assertIn("sendEl.disabled = !waiting;", self.script)
        self.assertIn("inputEl.disabled = !waiting;", self.script)
        self.assertIn("if (sendEl.disabled) { return; }", self.script)

    def test_visibility_and_the_backstop_derive_from_the_same_waiting_condition(self) -> None:
        block = self.script.split("var waiting =", 1)[1].split("function render(", 1)[0]
        for assignment in ("formEl.hidden = !waiting;", "sendEl.disabled = !waiting;",
                           "inputEl.disabled = !waiting;"):
            self.assertIn(assignment, block, assignment)

    def test_switching_selection_clears_the_input_and_any_stale_failure(self) -> None:
        block = self.script.split("function select(", 1)[1].split("function submit(", 1)[0]
        self.assertIn("clearFailure();", block)
        self.assertIn('inputEl.value = "";', block)

    def test_details_remains_available_and_collapsed_for_an_operational_selection(self) -> None:
        detail_block = self.script.split("function renderDetail", 1)[1].split("function render(", 1)[0]
        self.assertIn("detailsEl.open = false;", detail_block)
        self.assertNotIn("detailsEl.hidden", self.script)

    def test_no_substitute_operational_prose_or_status_control_was_added(self) -> None:
        """Removing the composer must not smuggle in a replacement that explains itself."""
        paragraphs = [
            element for element, tag in self.ancestry.tags.items() if tag == "p"
        ]
        # `allowance` is the top aggregate band in the queue column, not a
        # substitute for the composer: it is outside the detail pane entirely and
        # says nothing about the selected row.
        # `blocker-withheld` is in the detail pane and it is not a substitute for
        # anything: it is written only from `detail.blockerUnavailable`, which only
        # a decision item can carry, and it is hidden whenever that field is absent.
        # The guard this test exists to be -- no prose explaining an operational
        # row's state -- is kept by the two assertions below it rather than by the
        # element simply not existing.
        self.assertEqual(
            sorted(paragraphs),
            ["allowance", "blocker-withheld", "explanation", "queue-empty",
             "response-failure", "response-hint"],
        )
        withheld = self.script.split("var withheld =", 1)[1].split("fillFacts(factsEl", 1)[0]
        self.assertIn("detail.blockerUnavailable", withheld)
        self.assertIn("withheldEl.hidden = withheld === null", withheld)
        self.assertNotIn("detail", self.ancestry.ancestors["allowance"])
        self.assertEqual(self.page.count("<button"), 1)
        self.assertEqual(self.page.count("<textarea"), 1)
        for substitute in ("No response", "not available", "read-only", "cannot respond",
                           "This agent", "In progress"):
            self.assertNotIn(substitute, self.page, substitute)


# --------------------------------------------------------------------------
# Leakage
# --------------------------------------------------------------------------


class LeakageTests(unittest.TestCase):
    def test_no_session_or_lifecycle_content_reaches_the_page(self) -> None:
        page, _, _ = rendered([a_decision()], [an_agent()])
        for secret in (SESSION_SECRET, LIFECYCLE_DETAIL, LIFECYCLE_REASON):
            self.assertNotIn(secret, page, secret)

    def test_row_payload_entries_carry_no_explanation_or_evidence(self) -> None:
        page, _, _ = rendered([a_decision()], [an_agent()])
        for row in payload_of(page)["rows"]:
            self.assertNotIn("explanation", row)
            self.assertNotIn("evidence", row)

    def test_evidence_reaches_only_the_collapsed_details_section(self) -> None:
        page, _, _ = rendered([a_decision()])
        script = script_of(page)
        self.assertIn("detail.evidence.forEach", script)
        self.assertIn("evidenceEl.appendChild(entry);", script)
        rows_block = script.split("function renderRows", 1)[1].split("function renderDetail", 1)[0]
        self.assertNotIn("evidence", rows_block)


# --------------------------------------------------------------------------
# Density and layout declarations
# --------------------------------------------------------------------------


class DensityTests(unittest.TestCase):
    def test_thirty_waiting_items_render_through_the_same_path(self) -> None:
        decisions = [
            a_decision(decision_id="d-{0:02d}".format(n), rail="rail-{0:02d}".format(n),
                       elapsed_seconds=(30 - n) * 60)
            for n in range(30)
        ]
        page, view, _ = rendered(decisions, filters=(STATE_WAITING,))
        payload = payload_of(page)
        self.assertEqual(len(payload["rows"]), 30)
        ages = [row["elapsedSeconds"] for row in payload["rows"]]
        self.assertEqual(ages, sorted(ages, reverse=True))
        self.assertEqual(len(payload["details"]), 30)

    def test_the_list_scrolls_inside_itself_rather_than_the_page(self) -> None:
        page, _, _ = rendered([a_decision()])
        style = style_of(page)
        self.assertIn("overflow-y: auto", style)
        self.assertIn("height: 100vh", style)
        self.assertIn("min-height: 0", style)

    def test_rows_use_one_repeated_treatment(self) -> None:
        page, _, _ = rendered([a_decision()], [an_agent()])
        script = script_of(page)
        self.assertEqual(script.count('row.className = "row"'), 1)
        self.assertNotIn("card", page)


# --------------------------------------------------------------------------
# Accessibility declarations
# --------------------------------------------------------------------------


class AccessibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.page, _, _ = rendered([a_decision()], [an_agent()])

    def test_every_filter_has_a_visible_associated_label(self) -> None:
        for state in QUEUE_STATES:
            with self.subTest(state=state):
                self.assertIn('<label for="filter-{0}">'.format(state), self.page)

    def test_the_textarea_has_a_label_and_a_keyboard_hint_description(self) -> None:
        self.assertIn('<label for="response-input">Response</label>', self.page)
        self.assertIn('aria-describedby="response-hint"', self.page)
        self.assertIn('id="response-hint"', self.page)

    def test_selection_is_exposed_semantically(self) -> None:
        self.assertIn('role="listbox"', self.page)
        script = script_of(self.page)
        self.assertIn('row.setAttribute("role", "option")', script)
        self.assertIn('aria-selected', script)

    def test_the_selected_row_has_a_visible_treatment_too(self) -> None:
        style = style_of(self.page)
        self.assertIn('.row[aria-selected="true"]', style)
        self.assertIn("box-shadow", style)

    def test_failure_is_announced(self) -> None:
        self.assertIn('id="response-failure" role="alert"', self.page)

    def test_keyboard_focus_remains_visible(self) -> None:
        style = style_of(self.page)
        self.assertIn(":focus-visible", style)
        self.assertNotIn("outline: none", style)

    def test_details_uses_a_native_disclosure(self) -> None:
        self.assertIn("<details", self.page)
        self.assertIn("<summary>", self.page)


# --------------------------------------------------------------------------
# Purity of the surface
# --------------------------------------------------------------------------


# Every `new Date(...)` the production script is allowed to contain, by its exact
# argument. Each one converts a fact the payload already carried -- an epoch the
# allowance view supplied, or an instant the progress view took from the
# control-plane mechanism -- and none of them reads a clock. Pinning the arguments
# rather than banning the word says both things at once: no clock may be read, and
# the supplied fact may not be swapped for something else.
DATE_CONVERSIONS = [
    "progress.revision.at",
    "instant",
    "entry.resetsAt * 1000",
]


class SurfacePurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.page, _, _ = rendered([a_decision()], [an_agent()])
        self.source = Path(web.__file__).read_text(encoding="utf-8")

    def test_the_page_makes_no_external_request(self) -> None:
        self.assertIsNone(re.search(r'(?:src|href)\s*=\s*"(?:https?:)?//', self.page))
        for scheme in ("http://", "https://", "//cdn", "cdnjs", "googleapis", "unpkg"):
            self.assertNotIn(scheme, self.page, scheme)

    def test_the_page_declares_no_timer_clock_or_retry(self) -> None:
        script = script_of(self.page)
        for surface in ("setTimeout", "setInterval", "requestAnimationFrame",
                        "Date.now", "performance.now", "retry", "reload("):
            self.assertNotIn(surface, script, surface)

    def test_the_only_date_use_converts_the_supplied_epoch_and_reads_no_clock(self) -> None:
        """new Date(epoch) converts a fact the payload already carried.

        new Date() with no argument would be the clock read that the blanket
        "Date(" ban used to stand in for. Pinning the exact argument says more
        than that ban did: it forbids reading a clock AND forbids the supplied
        epoch being swapped for anything else.
        """
        script = script_of(self.page)
        uses = [seg.split(")", 1)[0] for seg in script.split("new Date(")[1:]]
        self.assertEqual(uses, DATE_CONVERSIONS)
        self.assertNotIn("new Date()", script)

    def test_every_date_token_is_that_one_conversion_and_nothing_else(self) -> None:
        """The pin above says the one construction is right; this says there is no other.

        Pinning `new Date(` alone leaves every spelling that does not use it --
        `Date()`, `Date.parse(Date())`, parenless `new Date` -- free to read the
        current clock. So account for the word `Date` itself: every occurrence
        in the production script must sit at the offset the one allowed
        supplied-epoch conversion puts it at, and there must be exactly one.
        """
        script = script_of(self.page)
        found = [m.start() for m in re.finditer("Date", script)]
        accounted = sorted(
            m.start() + len("new ")
            for argument in DATE_CONVERSIONS
            for m in re.finditer(re.escape("new Date({0})".format(argument)), script)
        )
        self.assertEqual(found, accounted)
        self.assertEqual(len(accounted), len(DATE_CONVERSIONS))

    def _imported_modules(self):
        """What the module imports, read from the AST rather than from its prose."""
        names = set()
        for node in ast.walk(ast.parse(self.source)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                names.add(("." * (node.level or 0)) + (node.module or ""))
        return names

    def _called_names(self):
        called = set()
        for node in ast.walk(ast.parse(self.source)):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    called.add(func.id)
                elif isinstance(func, ast.Attribute):
                    called.add(func.attr)
        return called

    def test_the_module_imports_only_the_standard_library_and_the_projection(self) -> None:
        self.assertEqual(
            self._imported_modules(),
            {"__future__", "base64", "hashlib", "http.server", "json", "re", "decimal",
             "pathlib", "threading", "typing", ".decision_queue", ".claude_allowance",
             ".claude_allowance_view", ".progress_view"},
        )

    def test_the_module_never_reaches_the_control_plane_a_session_or_a_provider(self) -> None:
        for banned in ("control_plane", "session_binding", "session_lifecycle",
                       "orchestrator_trigger", "orchestrator_invocation",
                       "orchestrator_outcome", "subprocess", "socket", "urllib",
                       "shutil", "os"):
            for name in self._imported_modules():
                self.assertNotEqual(name.lstrip("."), banned, name)
        for banned in ("authorize", "collect_rail_states", "build_snapshot",
                       "observe_session", "run", "Popen", "urlopen", "connect",
                       "write_text", "mkdir"):
            self.assertNotIn(banned, self._called_names(), banned)

    def test_the_module_reads_only_its_own_template_from_disk(self) -> None:
        self.assertEqual(self.source.count("read_text"), 1)
        self.assertEqual(self.source.count("write_text"), 0)
        for surface in ("os.remove", "shutil", "mkdir", "unlink", "open("):
            self.assertNotIn(surface, self.source, surface)

    def test_the_module_cannot_construct_a_waiting_item(self) -> None:
        self.assertNotIn("PendingDecision", self.source)
        self.assertNotIn("OperationalAgent", self.source)
        self.assertNotIn("build_queue", self.source)

    def test_the_module_derives_no_age(self) -> None:
        for surface in ("time.", "datetime", "elapsed_seconds(", "monotonic"):
            self.assertNotIn(surface, self.source, surface)

    def test_no_transcript_or_provider_surface_exists(self) -> None:
        """Allowance is now shown; a provider, transcript or token route still is not."""
        for surface in ("transcript", "anthropic", "token", "rate_limit", "api_key"):
            self.assertNotIn(surface, self.page.lower(), surface)

    def test_the_page_never_reaches_the_allowance_source_itself(self) -> None:
        """Presentation consumes finished views; it never becomes a second opinion."""
        for surface in ("project_window", "AllowanceStore", "projection_inputs",
                        "estimate_current", "record_usage_reading",
                        "human_exclusive_since", "latest_observation"):
            self.assertNotIn(surface, self.source, surface)
        self.assertNotIn("claude_allowance_store", self._imported_modules())


# --------------------------------------------------------------------------
# Allowance display
# --------------------------------------------------------------------------


class AllowanceInputTests(unittest.TestCase):
    """Two accepted views, named exactly. Anything ambiguous is refused, not guessed."""

    def setUp(self) -> None:
        _, self.view, self.details = rendered([a_decision()])

    def _refused(self, allowance):
        with self.assertRaises(RenderError) as caught:
            build_payload(self.view, self.details, allowance=allowance)
        self.assertEqual(caught.exception.reason, web.REASON_INVALID_ALLOWANCE)
        return caught.exception

    def test_a_non_sequence_is_refused(self) -> None:
        self._refused(object())
        self._refused(None)

    def test_a_missing_or_extra_window_is_refused(self) -> None:
        self._refused(())
        self._refused((a_window(WINDOW_FIVE_HOUR),))
        self._refused(an_allowance() + (a_window(WINDOW_FIVE_HOUR),))

    def test_a_foreign_object_is_never_treated_as_a_view(self) -> None:
        self._refused((a_window(WINDOW_FIVE_HOUR), object()))

    def test_a_duplicate_window_is_refused_rather_than_deduplicated(self) -> None:
        exception = self._refused((a_window(WINDOW_FIVE_HOUR), a_window(WINDOW_FIVE_HOUR)))
        self.assertIn(WINDOW_FIVE_HOUR, exception.detail)

    def test_an_unknown_window_is_refused_rather_than_relabelled(self) -> None:
        exception = self._refused((a_window(WINDOW_FIVE_HOUR), a_window("monthly")))
        self.assertIn(WINDOW_SEVEN_DAY, exception.detail)

    def test_the_allowance_argument_has_no_default(self) -> None:
        """A caller that cannot supply the views must say so, not silently omit them."""
        with self.assertRaises(TypeError):
            build_payload(self.view, self.details)


class AllowancePrecisionTests(unittest.TestCase):
    """Whole percentage points, and never finer than the projection supports."""

    def _used(self, view):
        return build_allowance((view, a_window(WINDOW_SEVEN_DAY)))[0]["used"]

    def test_a_provisional_point_is_marked_and_rounded_half_up(self) -> None:
        self.assertEqual(self._used(a_window(point_percentage=Decimal("30"))), "≈30% used")
        self.assertEqual(self._used(a_window(point_percentage=Decimal("30.5"))), "≈31% used")
        self.assertEqual(self._used(a_window(point_percentage=Decimal("30.4"))), "≈30% used")
        self.assertEqual(self._used(a_window(point_percentage=Decimal("29.5"))), "≈30% used")

    def test_a_repeating_decimal_never_reaches_the_page(self) -> None:
        used = self._used(a_window(point_percentage=Decimal("17") + Decimal(7) / Decimal(3)))
        self.assertEqual(used, "≈19% used")
        self.assertNotIn("3333", used)

    def test_a_calibrated_range_widens_outward(self) -> None:
        self.assertEqual(self._used(a_range(lower="41.2", upper="46.8")), "41–47% used")
        self.assertEqual(self._used(a_range(lower="41.0", upper="46.0")), "41–46% used")

    def test_the_range_form_survives_coinciding_endpoints(self) -> None:
        """A band that rounds to one number is still a band, not a measurement."""
        used = self._used(a_range(lower="41", upper="41"))
        self.assertEqual(used, "41–41% used")
        self.assertNotEqual(used, "≈41% used")

    def test_a_clamped_projection_is_shown_without_claiming_confirmation(self) -> None:
        used = self._used(a_window(point_percentage=Decimal("100"), bounded=True))
        self.assertEqual(used, "≈100% used")
        self.assertNotIn("exhausted", used.lower())

    def test_an_unavailable_window_carries_no_number_at_all(self) -> None:
        for view in (unavailable(), unavailable(source_healthy=False, reason="malformed-store")):
            with self.subTest(reason=view.reason):
                self.assertIsNone(self._used(view))

    def test_rounding_is_presentation_only_and_never_written_back(self) -> None:
        view = a_window(point_percentage=Decimal("17") + Decimal(7) / Decimal(3))
        build_allowance((view, a_window(WINDOW_SEVEN_DAY)))
        self.assertEqual(view.point_percentage, Decimal("17") + Decimal(7) / Decimal(3))
        self.assertGreater(len(str(view.point_percentage).split(".")[1]), 6)


class AllowancePayloadTests(unittest.TestCase):
    def test_both_windows_stay_independently_attributable(self) -> None:
        entries = build_allowance(
            an_allowance(
                five_hour=a_window(WINDOW_FIVE_HOUR, point_percentage=Decimal("12")),
                seven_day=a_range(WINDOW_SEVEN_DAY, lower="70.1", upper="70.2"),
            )
        )
        self.assertEqual(
            [entry["window"] for entry in entries], [WINDOW_FIVE_HOUR, WINDOW_SEVEN_DAY]
        )
        self.assertEqual(entries[0]["used"], "≈12% used")
        self.assertEqual(entries[1]["used"], "70–71% used")

    def test_each_window_is_visibly_named_as_itself_and_never_as_the_other(self) -> None:
        """The label is the only five-hour/seven-day identity a human ever sees.

        The canonical `window` value reaches the DOM as an unrendered `data-window`
        attribute, so the id assertions above cannot catch an inverted label. Bind each
        window to the string that is drawn for it, and prove the page really draws that
        string: otherwise seven-day consumption can be presented as five-hour headroom
        with every other assertion in this suite still green.
        """
        page, _, _ = rendered([a_decision()])
        self.assertEqual(
            [(entry["window"], entry["label"]) for entry in allowance_of(page)],
            [(WINDOW_FIVE_HOUR, "5h"), (WINDOW_SEVEN_DAY, "7d")],
        )
        block = script_of(page).split("function renderAllowance", 1)[1].split(
            "function renderRows", 1
        )[0]
        self.assertIn("textContent = entry.label", block)

    def test_the_drawn_order_is_fixed_regardless_of_caller_order(self) -> None:
        forward = build_allowance((a_window(WINDOW_FIVE_HOUR), a_window(WINDOW_SEVEN_DAY)))
        backward = build_allowance((a_window(WINDOW_SEVEN_DAY), a_window(WINDOW_FIVE_HOUR)))
        self.assertEqual(
            [entry["window"] for entry in forward],
            [entry["window"] for entry in backward],
        )

    def test_one_window_may_be_unavailable_while_the_other_is_not(self) -> None:
        entries = build_allowance(
            an_allowance(
                five_hour=unavailable(WINDOW_FIVE_HOUR), seven_day=a_window(WINDOW_SEVEN_DAY)
            )
        )
        self.assertIsNone(entries[0]["used"])
        self.assertEqual(entries[0]["reason"], "current-coverage-incomplete")
        self.assertIsNotNone(entries[1]["used"])

    def test_health_and_source_health_are_carried_not_derived(self) -> None:
        entries = build_allowance(
            an_allowance(
                five_hour=unavailable(
                    WINDOW_FIVE_HOUR, reason="malformed-store", source_healthy=False
                )
            )
        )
        self.assertEqual(entries[0]["health"], HEALTH_UNAVAILABLE)
        self.assertIs(entries[0]["sourceHealthy"], False)
        self.assertIs(entries[1]["sourceHealthy"], True)
        # A healthy store can still be unavailable: it read fine and simply has no
        # coverage to project from. Deriving source health from availability here
        # would silently accuse that store of a fault it does not have.
        healthy = build_allowance(an_allowance(five_hour=unavailable(WINDOW_FIVE_HOUR)))
        self.assertEqual(healthy[0]["health"], HEALTH_UNAVAILABLE)
        self.assertIs(healthy[0]["sourceHealthy"], True)

    def test_the_reset_epoch_is_carried_exactly_or_stays_absent(self) -> None:
        entries = build_allowance(
            an_allowance(
                five_hour=a_window(WINDOW_FIVE_HOUR, resets_at=1700000000),
                seven_day=unavailable(WINDOW_SEVEN_DAY),
            )
        )
        self.assertEqual(entries[0]["resetsAt"], 1700000000)
        self.assertIsNone(entries[1]["resetsAt"])

    def test_no_unavailable_window_is_ever_rendered_as_zero(self) -> None:
        page, _, _ = rendered(
            [a_decision()],
            allowance=an_allowance(
                five_hour=unavailable(WINDOW_FIVE_HOUR),
                seven_day=unavailable(WINDOW_SEVEN_DAY),
            ),
        )
        for entry in allowance_of(page):
            self.assertIsNone(entry["used"])
        # Scoped to the data the page actually draws from: the stylesheet legitimately
        # contains lengths such as `100%`, which are not allowance figures.
        block = page.split('id="queue-payload">', 1)[1].split("</script>", 1)[0]
        for forbidden in ('"used":0', '"used":"0', "0% used", "% used"):
            self.assertNotIn(forbidden, block, forbidden)


class AllowancePlacementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.page, _, _ = rendered([a_decision()], [an_agent()])

    def test_the_summary_sits_in_the_top_aggregate_band_above_the_queue(self) -> None:
        self.assertLess(self.page.index('class="filters"'), self.page.index('id="allowance"'))
        self.assertLess(self.page.index('id="allowance"'), self.page.index('id="rows"'))

    def test_the_summary_never_enters_the_decision_detail_pane(self) -> None:
        self.assertLess(self.page.index('id="allowance"'), self.page.index('id="detail"'))

    def test_the_queue_keeps_its_existing_shape(self) -> None:
        """Allowance is context. It adds no control, no navigation, and no new block."""
        self.assertEqual(self.page.count("<textarea"), 1)
        self.assertEqual(self.page.count("<button"), 1)
        self.assertEqual(self.page.count("<input"), len(QUEUE_STATES))
        self.assertEqual(self.page.count("<details"), 1)
        self.assertNotIn("<select", self.page)
        self.assertEqual(self.page.count("<style"), 1)
        self.assertEqual(policy_of(self.page).count("'sha256-"), 2)

    def test_each_window_declares_its_own_identity_in_the_dom(self) -> None:
        script = script_of(self.page)
        for attribute in ("data-window", "data-health", "data-source-healthy"):
            self.assertIn(attribute, script, attribute)

    def test_the_summary_is_drawn_once_and_not_from_the_queue_loop(self) -> None:
        script = script_of(self.page)
        block = script.split("function renderAllowance", 1)[1].split("function renderRows", 1)[0]
        for absent in ("items", "filters", "selectedId", "elapsed"):
            self.assertNotIn(absent, block, absent)


class AllowanceHostileDataTests(unittest.TestCase):
    def test_a_hostile_reason_cannot_end_the_block_or_become_markup(self) -> None:
        page, _, _ = rendered(
            [a_decision()],
            allowance=an_allowance(
                five_hour=unavailable(WINDOW_FIVE_HOUR, reason=HOSTILE_REASON)
            ),
        )
        block = page.split('id="queue-payload">', 1)[1].split("</script>", 1)[0]
        self.assertNotIn("</script>", block)
        self.assertNotIn("<img", block)
        self.assertEqual(allowance_of(page)[0]["reason"], HOSTILE_REASON)

    def test_a_hostile_reason_is_inserted_as_text_never_as_markup(self) -> None:
        script = script_of(rendered([a_decision()])[0])
        block = script.split("function renderAllowance", 1)[1].split("function renderRows", 1)[0]
        self.assertIn("textContent", block)
        for unsafe in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            self.assertNotIn(unsafe, block, unsafe)


class AllowanceResetRenderingTests(unittest.TestCase):
    """Reset timing: drawn from the accepted epoch, absolutely, and only when stated."""

    def setUp(self) -> None:
        self.page, _, _ = rendered([a_decision()], [an_agent()])
        self.block = reset_block(self.page)
        self.code = code_only(self.block)

    def test_a_known_reset_draws_exactly_one_semantic_time_element(self) -> None:
        self.assertEqual(self.code.count('createElement("time")'), 1)
        self.assertIn('reset.className = "allowance-reset"', self.code)

    def test_the_machine_readable_instant_is_exact_utc_from_the_supplied_epoch(self) -> None:
        self.assertIn("new Date(entry.resetsAt * 1000)", self.code)
        self.assertIn('setAttribute("datetime", resetAt.toISOString())', self.code)

    def test_the_datetime_attribute_is_never_localised_or_re_derived(self) -> None:
        """The exact instant survives only if nothing reshapes it on the way out."""
        call = self.code.split('setAttribute("datetime",', 1)[1].split(")", 1)[0]
        self.assertIn("toISOString", call)
        for wrong in ("toLocale", "slice", "substring", "replace", "split"):
            self.assertNotIn(wrong, call, wrong)

    def test_the_visible_label_is_absolute_local_with_calendar_context(self) -> None:
        label = self.code.split("toLocaleString(", 1)[1].split("});", 1)[0]
        for field in ("weekday", "month", "day", "hour", "minute"):
            self.assertIn(field, label, field)

    def test_a_seven_day_reset_cannot_read_as_a_time_later_today(self) -> None:
        """Strip the date fields and a 7d reset renders as a bare clock time."""
        label = self.code.split("toLocaleString(", 1)[1].split("});", 1)[0]
        self.assertIn("month", label)
        self.assertIn("day", label)

    def test_an_absent_reset_renders_nothing_and_invents_no_time(self) -> None:
        self.assertIn("entry.resetsAt !== null && entry.resetsAt !== undefined", self.code)
        self.assertNotIn("else", self.code)

    def test_no_countdown_timer_or_relative_phrasing_is_introduced(self) -> None:
        for banned in ("setInterval", "setTimeout", "requestAnimationFrame",
                       "Date.now", "performance.now", "ago", "remaining",
                       "countdown", "fromNow", "elapsed"):
            self.assertNotIn(banned, self.code, banned)

    def test_the_label_states_only_that_this_is_a_reset(self) -> None:
        self.assertIn('"resets "', self.code)

    def test_a_reset_renders_independently_of_the_usage_value(self) -> None:
        """An unavailable window still states when it resets; neither field masks the other."""
        self.assertIn("value.textContent = known ? entry.used : entry.reason;", self.code)
        reset_only = self.code.split("if (entry.resetsAt", 1)[1]
        for coupled in ("known", "entry.used", "entry.reason"):
            self.assertNotIn(coupled, reset_only, coupled)

    def test_the_reset_stays_secondary_and_adds_no_new_structure(self) -> None:
        for heavy in ('createElement("div")', 'createElement("section")',
                      'createElement("h2")', 'createElement("button")',
                      "badge", "card", "panel", "dashboard"):
            self.assertNotIn(heavy, self.code, heavy)
        self.assertIn(".allowance-reset", style_of(self.page))

    def test_the_strip_wraps_rather_than_overflowing_the_narrowest_column(self) -> None:
        """Two absolute reset labels do not fit the declared minimum queue width.

        Each reset is `white-space: nowrap` by design, so at `minmax(320px, ...)`
        the strip cannot narrow its own content. Wrapping is the only fit left
        that keeps the wording; a sideways scroll or an ellipsis would hide the
        second window's reset instead of showing it.
        """
        style = style_of(self.page)
        self.assertIn("minmax(320px", style)
        reset = style.split(".allowance-reset", 1)[1].split("}", 1)[0]
        self.assertIn("white-space: nowrap", reset)

        strip = style.split(".allowance {", 1)[1].split("}", 1)[0]
        self.assertIn("display: flex", strip)
        self.assertIn("flex-wrap: wrap", strip)
        for hidden in ("overflow-x", "overflow:", "text-overflow", "ellipsis",
                       "scroll", "nowrap", "max-width", "transform", "zoom"):
            self.assertNotIn(hidden, strip, hidden)

    def test_the_filters_wrap_rather_than_overflowing_the_narrowest_column(self) -> None:
        """The sibling strip must fit the same declared minimum the allowance strip does.

        A real browser at `minmax(320px, ...)` laid the unwrapped fieldset out at
        its ~350px min-content width, so it overran the queue column and painted
        into the detail pane beside it. Wrapping is the same fit the allowance
        strip already uses; a sideways scroll, an ellipsis, or a transform would
        hide a filter the human is meant to be able to reach.
        """
        style = style_of(self.page)
        self.assertIn("minmax(320px", style)

        strip = style.split(".filters {", 1)[1].split("}", 1)[0]
        self.assertIn("display: flex", strip)
        self.assertIn("flex-wrap: wrap", strip)
        for hidden in ("overflow-x", "overflow:", "text-overflow", "ellipsis",
                       "scroll", "nowrap", "max-width", "transform", "zoom"):
            self.assertNotIn(hidden, strip, hidden)

    def test_python_supplies_the_epoch_and_formats_no_reset_itself(self) -> None:
        """One carry, and no second reset authority or formatter on the Python side."""
        source = Path(web.__file__).read_text(encoding="utf-8")
        self.assertEqual(source.count("resets_at"), 1)
        self.assertEqual(source.count("resetsAt"), 1)
        for formatter in ("strftime", "isoformat", "datetime", "toISOString"):
            self.assertNotIn(formatter, source, formatter)


class AgentCountDisplayTests(unittest.TestCase):
    """current / permitted in the aggregate strip: a number, or the reason there is none."""

    def reading(self, **overrides):
        arguments = {"permitted": 6, "current": 3, "reason": None}
        arguments.update(overrides)
        return arguments

    def test_no_reading_draws_nothing_at_all(self) -> None:
        self.assertIsNone(web.build_agents(None))

    def test_an_established_count_carries_both_halves(self) -> None:
        drawn = web.build_agents(self.reading())
        self.assertEqual(drawn["current"], 3)
        self.assertEqual(drawn["permitted"], 6)
        self.assertIsNone(drawn["reason"])

    def test_an_unestablished_count_is_a_reason_not_a_zero(self) -> None:
        drawn = web.build_agents(
            self.reading(current=None, reason="ownership-unprovable")
        )
        self.assertIsNone(drawn["current"])
        self.assertEqual(drawn["reason"], "ownership-unprovable")
        self.assertEqual(drawn["permitted"], 6)

    def test_an_unestablished_count_without_a_reason_is_refused(self) -> None:
        for reason in (None, "", "   "):
            with self.subTest(reason=reason):
                with self.assertRaises(web.RenderError) as caught:
                    web.build_agents(self.reading(current=None, reason=reason))
                self.assertEqual(caught.exception.reason, web.REASON_INVALID_AGENTS)

    def test_a_malformed_reading_is_refused_rather_than_drawn(self) -> None:
        for bad in (
            {"permitted": 0, "current": 0},
            {"permitted": -1, "current": 0},
            {"permitted": "6", "current": 0},
            {"permitted": 6, "current": -1},
            {"permitted": 6, "current": "3"},
            {"permitted": 6, "current": 3, "extra": 1},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(web.RenderError) as caught:
                    web.build_agents(bad)
                self.assertEqual(caught.exception.reason, web.REASON_INVALID_AGENTS)

    def test_zero_running_agents_is_a_real_count_not_an_absence(self) -> None:
        drawn = web.build_agents(self.reading(current=0))
        self.assertEqual(drawn["current"], 0)
        self.assertIsNone(drawn["reason"])

    def test_the_page_draws_the_count_inside_the_existing_strip(self) -> None:
        template = Path(web.__file__).with_name(web.TEMPLATE_NAME).read_text(encoding="utf-8")
        script = template.split("<script>", 1)[1]
        self.assertIn('data-window", "agents"', script)
        self.assertIn("allowanceEl.appendChild(agents)", script)
        # One repeated treatment, not a second container of its own.
        self.assertIn('agents.className = "allowance-window"', script)
        for invented in ("agents-panel", "agent-card", "agent-badge", "agents-strip"):
            with self.subTest(invented=invented):
                self.assertNotIn(invented, template)


if __name__ == "__main__":
    unittest.main()


class ObservedServerSurfaceTests(unittest.TestCase):
    """One source, called once per request, and the whole page drawn from it.

    `make_live_server` keeps the rows and re-reads the figure. That is right while
    the rows carry nothing that can change under the page, and wrong the moment
    they do: a row saying a session is working beside a figure saying nothing
    provable is running is one screen describing two moments. This server takes the
    whole answer from one call so there is no second call for it to interleave
    anything between.
    """

    def setUp(self) -> None:
        self.observations = [
            ([an_agent(state=STATE_RUNNING)], {"permitted": 6, "current": 1, "reason": None}),
            ([an_agent(state=STATE_RUNNING)], {"permitted": 6, "current": 1, "reason": None}),
            ([], {"permitted": 6, "current": 0, "reason": None}),
        ]
        self.taken = []

        def observe():
            agents, reading = self.observations[
                min(len(self.taken), len(self.observations) - 1)
            ]
            self.taken.append(reading)
            _page, view, details = rendered([], agents)
            return view, details, reading

        self.observe = observe
        self.server = make_observed_server(observe, allowance=an_allowance(), port=0)
        self.addCleanup(self.server.server_close)
        self.serving = start_serving(self.server)
        self.addCleanup(self.serving.stop)
        self.port = self.server.server_address[1]

    def fetch(self) -> dict:
        connection = http.client.HTTPConnection(LOOPBACK_HOST, self.port, timeout=5)
        try:
            connection.request("GET", PAGE_PATH)
            body = connection.getresponse().read().decode("utf-8")
        finally:
            connection.close()
        return payload_of(body)

    def test_the_observation_source_is_consulted_exactly_once_per_request(self) -> None:
        """One call is the whole guarantee: two would be two instants."""
        self.assertEqual(len(self.taken), 1)
        self.fetch()
        self.assertEqual(len(self.taken), 2)
        self.fetch()
        self.assertEqual(len(self.taken), 3)

    def test_the_rows_and_the_figure_move_together(self) -> None:
        first, second = self.fetch(), self.fetch()

        self.assertEqual(len(first["rows"]), 1)
        self.assertEqual(first["agents"]["current"], 1)
        self.assertEqual(second["rows"], [])
        self.assertEqual(second["agents"]["current"], 0)

    def test_this_runs_projection_is_still_taken_once(self) -> None:
        """The allowance is the caller's, projected once, and never re-taken."""
        first, second = self.fetch(), self.fetch()

        for key in ("allowance", "states", "defaultFilters"):
            with self.subTest(key=key):
                self.assertEqual(first[key], second[key])

    def test_it_retains_no_document_between_requests(self) -> None:
        """Nothing may be re-served: a page true a moment ago is not true now."""
        for attribute in ("page", "queue", "view", "details", "store"):
            self.assertFalse(
                getattr(self.server.RequestHandlerClass, attribute, None), attribute
            )
        self.assertTrue(callable(self.server.RequestHandlerClass.document))

    def test_a_non_loopback_bind_is_refused_here_too(self) -> None:
        for host in ("0.0.0.0", "::", "10.0.0.5"):
            with self.subTest(host=host):
                with self.assertRaises(RenderError) as caught:
                    make_observed_server(
                        self.observe, allowance=an_allowance(), host=host, port=0
                    )
                self.assertEqual(caught.exception.reason, web.REASON_NOT_LOOPBACK)

    def test_an_unusable_observation_is_refused_at_construction(self) -> None:
        """The same construction contract the other two servers already have."""
        _page, view, details = rendered([], [an_agent()])

        with self.assertRaises(RenderError) as caught:
            make_observed_server(
                lambda: (view, details, {"permitted": 6, "current": None, "reason": None}),
                allowance=an_allowance(), port=0,
            )

        self.assertEqual(caught.exception.reason, web.REASON_INVALID_AGENTS)

    def test_a_failing_observation_is_never_answered_with_a_stale_page(self) -> None:
        """A source that breaks must not be papered over with the last good page."""
        broken = []
        _page, view, details = rendered([], [an_agent()])

        def observe():
            if broken:
                raise RuntimeError("the store could not be read")
            broken.append(True)
            return view, details, {"permitted": 6, "current": 1, "reason": None}

        server = make_observed_server(observe, allowance=an_allowance(), port=0)
        self.addCleanup(server.server_close)
        serving = start_serving(server)
        self.addCleanup(serving.stop)

        connection = http.client.HTTPConnection(
            LOOPBACK_HOST, server.server_address[1], timeout=5
        )
        self.addCleanup(connection.close)
        with contextlib.redirect_stderr(io.StringIO()):
            connection.request("GET", PAGE_PATH)
            with self.assertRaises(Exception):
                connection.getresponse().read()


# --------------------------------------------------------------------------
# D8: the actionable half reaches the page, and only where it belongs
# --------------------------------------------------------------------------


BLOCKER_TEXT = {
    "kind": "permission",
    "what_failed": "publishing the executor handoff to the coordination remote",
    "agent": "executor",
    "missing_capability": "write access to jmrozi1/ai-dev-control-plane for this host key",
    "human_change": "add this host's public key as a deploy key with write access",
    "next_action": "re-run the publish step; the checkpoint is already committed",
}


def a_blocker(**overrides):
    base = dict(BLOCKER_TEXT)
    base["state_changed"] = True
    base.update(overrides)
    return ActionableBlocker(**base)


class ActionablePayloadTests(unittest.TestCase):
    """What a person is handed, field by field, without a second request."""

    def detail_of(self, page, item_id=None):
        payload = payload_of(page)
        if item_id is None:
            item_id = payload["rows"][0]["itemId"]
        return payload["details"][item_id]

    def test_the_blocker_crosses_as_eight_named_fields_and_verbatim_text(self) -> None:
        blocker = a_blocker()
        page, _, _ = rendered([a_decision(blocker=blocker)])

        drawn = self.detail_of(page)["blocker"]

        self.assertEqual(
            set(drawn),
            {"kind", "whatFailed", "agent", "agentUnavailable", "missingCapability",
             "humanChange", "stateChanged", "nextAction"},
        )
        self.assertEqual(drawn["kind"], blocker.kind)
        self.assertEqual(drawn["whatFailed"], blocker.what_failed)
        self.assertEqual(drawn["agent"], blocker.agent)
        self.assertIsNone(drawn["agentUnavailable"])
        self.assertEqual(drawn["missingCapability"], blocker.missing_capability)
        self.assertEqual(drawn["humanChange"], blocker.human_change)
        self.assertEqual(drawn["nextAction"], blocker.next_action)

    def test_the_agent_pair_crosses_uncollapsed_so_a_name_is_not_a_sentence(self) -> None:
        """Both halves cross as themselves. Neither is folded into the other.

        A payload that collapsed them into one `agent` string would leave the page
        unable to tell a rail whose published assignment is `executor` from a rail
        that published no assignment at all -- and a page that cannot tell those
        apart is one keystroke from printing a reason where a name belongs.
        """
        stated = a_blocker(agent=None, agent_unavailable="the rail publishes no role")
        page, _, _ = rendered([a_decision(blocker=stated)])

        drawn = self.detail_of(page)["blocker"]

        self.assertIsNone(drawn["agent"])
        self.assertEqual(drawn["agentUnavailable"], "the rail publishes no role")
        # And the five published facts crossed beside it, not instead of it.
        self.assertEqual(drawn["whatFailed"], stated.what_failed)
        self.assertEqual(drawn["missingCapability"], stated.missing_capability)
        self.assertEqual(drawn["humanChange"], stated.human_change)
        self.assertEqual(drawn["nextAction"], stated.next_action)
        self.assertIs(drawn["stateChanged"], stated.state_changed)

    def test_state_changed_crosses_as_a_boolean_in_both_directions(self) -> None:
        """A phrase in the payload would let two callers ship two phrasings."""
        for changed in (True, False):
            with self.subTest(changed=changed):
                page, _, _ = rendered(
                    [a_decision(blocker=a_blocker(state_changed=changed))]
                )
                drawn = self.detail_of(page)["blocker"]
                self.assertIs(drawn["stateChanged"], changed)
                # And the serialized block carries a JSON literal, not a word.
                block = page.split('id="queue-payload">', 1)[1].split("</script>", 1)[0]
                self.assertIn('"stateChanged":{0}'.format(str(changed).lower()), block)

    def test_the_three_routing_facts_reach_every_item_s_detail(self) -> None:
        page, _, _ = rendered([a_decision()], [an_agent()])
        payload = payload_of(page)

        for row in payload["rows"]:
            with self.subTest(item=row["itemId"]):
                detail = payload["details"][row["itemId"]]
                self.assertEqual(detail["project"], PROJECT)
                self.assertEqual(detail["ticket"], TICKET)
                self.assertTrue(detail["rail"].startswith("issue-55-rail-"))

    def test_an_operational_detail_carries_neither_half(self) -> None:
        """Proof 10 at the payload boundary."""
        page, _, _ = rendered([a_decision(blocker=a_blocker())], [an_agent()])
        payload = payload_of(page)

        by_owner = {
            payload["details"][row["itemId"]]["attentionOwner"]: payload["details"][row["itemId"]]
            for row in payload["rows"]
        }
        self.assertIsNone(by_owner[OWNER_AGENT]["blocker"])
        self.assertIsNone(by_owner[OWNER_AGENT]["blockerUnavailable"])
        self.assertIsNotNone(by_owner[OWNER_HUMAN]["blocker"])

    def test_a_decision_with_no_blocker_reports_null_for_both(self) -> None:
        page, _, _ = rendered([a_decision()])
        detail = self.detail_of(page)
        self.assertIsNone(detail["blocker"])
        self.assertIsNone(detail["blockerUnavailable"])

    def test_an_unsourced_blocker_crosses_as_its_reason_and_no_blocker(self) -> None:
        notice = "blocker-agent-unsourced: the rail publishes no role assignment."
        page, _, _ = rendered([a_decision(blocker_unavailable=notice)])
        detail = self.detail_of(page)
        self.assertIsNone(detail["blocker"])
        self.assertEqual(detail["blockerUnavailable"], notice)

    def test_hostile_blocker_text_cannot_become_markup(self) -> None:
        """Published prose is written by people. It is data here, in every field."""
        blocker = a_blocker(
            what_failed=HOSTILE_TITLE,
            human_change=HOSTILE_EXPLANATION,
            next_action=HOSTILE_LOCATOR,
        )
        page, _, _ = rendered([a_decision(blocker=blocker)])

        drawn = self.detail_of(page)["blocker"]
        self.assertEqual(drawn["whatFailed"], HOSTILE_TITLE)
        self.assertEqual(drawn["humanChange"], HOSTILE_EXPLANATION)
        self.assertEqual(drawn["nextAction"], HOSTILE_LOCATOR)
        # It survived exactly, and it did not survive as markup.
        block = page.split('id="queue-payload">', 1)[1].split("</script>", 1)[0]
        self.assertNotIn("<script>", block)
        self.assertNotIn("</script>", block)
        self.assertNotIn("<img", block)

    def test_the_reducer_names_every_field_rather_than_dumping_the_dataclass(self) -> None:
        """A field added later must not reach a page nobody wrote a place for."""
        source = Path(web.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        # Nothing in this module reflects over a dataclass. Checked on the parsed
        # names rather than on the text, so the comment beside `_blocker` saying
        # it deliberately does not use `asdict` does not fail its own claim.
        names = {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        } | {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        for reflective in ("asdict", "astuple", "fields", "__dict__", "vars"):
            self.assertNotIn(reflective, names, reflective)
        reducer = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_blocker"
        ]
        self.assertEqual(len(reducer), 1)
        emitted = {
            node.value for node in ast.walk(reducer[0])
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        self.assertTrue(
            {"kind", "whatFailed", "agent", "missingCapability", "humanChange",
             "stateChanged", "nextAction"}.issubset(emitted)
        )


class ActionablePageTests(unittest.TestCase):
    """The page's own declarations about how it draws the actionable half."""

    def setUp(self) -> None:
        self.page, _, _ = rendered(
            [a_decision(blocker=a_blocker())], [an_agent()]
        )
        self.script = script_of(self.page)
        self.detail_block = code_only(
            self.script.split("function renderDetail", 1)[1].split("function render(", 1)[0]
        )

    def test_the_block_is_hidden_unless_a_complete_blocker_arrived(self) -> None:
        self.assertIn("actionableEl.hidden = blocker === null", self.detail_block)
        self.assertIn("var blocker = detail.blocker;", self.detail_block)

    def test_all_six_blocker_facts_are_drawn_and_none_is_composed(self) -> None:
        for expression in (
            "blocker.whatFailed", "blocker.agent", "blocker.agentUnavailable",
            "blocker.missingCapability", "blocker.humanChange", "blocker.stateChanged",
            "blocker.nextAction",
        ):
            with self.subTest(expression=expression):
                self.assertIn(expression, self.detail_block)
        # Nothing is concatenated onto published text, and nothing is truncated.
        self.assertNotIn("substring", self.script)
        self.assertNotIn("slice(0", self.script)

    def test_the_agent_row_prints_a_published_name_or_the_stated_absence(self) -> None:
        """The row is always drawn, and the page never authors what goes in it.

        Both halves of the pair are read, and the choice between them is a null
        test on the value the projection set -- not a test for a blank followed by
        wording invented here. The row itself is unconditional: "who is affected"
        is a question a person asks whether or not it has an answer, and a row that
        disappeared would read as an oversight rather than as the fact it is.
        """
        self.assertIn("blocker.agentUnavailable", self.detail_block)
        self.assertIn("blocker.agent === null", self.detail_block)
        self.assertIn('["Agent", affected]', self.detail_block)
        # The page composes no sentence of its own for the absent case: the only
        # string it can put in that row came from the projection.
        chooser = self.detail_block.split("var affected", 1)[1].split(";", 1)[0]
        self.assertNotIn('"', chooser)

    def test_the_absent_agent_reaches_the_page_beside_the_published_facts(self) -> None:
        """Proof 8 on the rendered page: stated, not invented, and not alone."""
        stated = a_blocker(
            agent=None,
            agent_unavailable="the rail publishes no role assignment",
        )
        page, _, _ = rendered([a_decision(blocker=stated)])

        drawn = payload_of(page)["details"]
        blocker = [d["blocker"] for d in drawn.values() if d["blocker"]][0]

        self.assertIsNone(blocker["agent"])
        self.assertEqual(blocker["agentUnavailable"], stated.agent_unavailable)
        self.assertEqual(blocker["whatFailed"], stated.what_failed)
        self.assertEqual(blocker["nextAction"], stated.next_action)

    def test_the_three_routing_facts_are_drawn_for_every_item(self) -> None:
        for expression in ("detail.project", "detail.ticket", "detail.rail"):
            with self.subTest(expression=expression):
                self.assertIn(expression, self.detail_block)
        # Above the fold rather than inside the collapsed Details block: an
        # instruction a person must expand something to finish reading is not
        # actionable on its own.
        ancestry = ancestry_of(self.page)
        self.assertNotIn("details", ancestry.ancestors.get("routing", []))
        self.assertNotIn("details", ancestry.ancestors.get("actionable", []))
        self.assertIn("detail", ancestry.ancestors.get("actionable", []))

    def test_state_changed_has_exactly_two_answers_and_no_third(self) -> None:
        """"May have changed" must be unreachable, not merely unwritten."""
        label = self.script.split("function stateChangedLabel", 1)[1]
        label = label.split("function ", 1)[0]
        self.assertIn("Yes - product or worktree state changed.", label)
        self.assertIn("No - no product or worktree state changed.", label)
        self.assertEqual(label.count("?"), 1)
        # No hedge is reachable as displayed text. Checked over the code with its
        # prose removed and over the markup, because a comment explaining why
        # "may have changed" is refused must not be what fails this test -- nor
        # what passes it.
        readable = code_only(script_of(self.page)).lower()
        readable += self.page.split("<body>", 1)[1].split("<script", 1)[0].lower()
        for hedge in ("may have", "unknown", "possibly", "might"):
            self.assertNotIn(hedge, readable, hedge)

    def test_every_drawn_value_is_a_text_node(self) -> None:
        """Data cannot become code, in the new block as in the old ones."""
        self.assertNotIn("innerHTML", self.script)
        self.assertNotIn("insertAdjacentHTML", self.script)
        self.assertNotIn("document.write", self.script)
        fill = self.script.split("function fillFacts", 1)[1]
        fill = fill.split("function stateChangedLabel", 1)[0]
        self.assertIn("name.textContent = pair[0];", fill)
        self.assertIn("value.textContent = pair[1];", fill)
        self.assertNotIn("innerHTML", fill)

    def test_no_transcript_log_console_or_session_inspector_was_added(self) -> None:
        """Proof 13, over the served page itself."""
        lowered = self.page.lower()
        for forbidden in ("transcript", "raw log", "session inspector", "console.",
                          "stdout", "stderr", "<iframe", "eventsource", "websocket",
                          "setinterval", "settimeout", "fetch(", "xmlhttprequest"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, lowered, forbidden)

    def test_no_session_identity_reached_the_page_as_identity(self) -> None:
        """Proof 6 over the rendered bytes: transport stays inside evidence."""
        payload = payload_of(self.page)
        for row in payload["rows"]:
            with self.subTest(item=row["itemId"]):
                self.assertNotIn(SESSION_SECRET, json.dumps(row))
        for item_id, detail in payload["details"].items():
            with self.subTest(item=item_id):
                self.assertNotIn(SESSION_SECRET, json.dumps(detail["rail"]))
                self.assertNotIn(SESSION_SECRET, json.dumps(detail["blocker"]))
        self.assertNotIn(LIFECYCLE_DETAIL, self.page)
        self.assertNotIn(LIFECYCLE_REASON, self.page)


class ActionableOverHttpTests(unittest.TestCase):
    """A real client, a real loopback socket, and the accepted server."""

    def test_a_real_client_is_told_all_nine_in_one_response(self) -> None:
        blocker = a_blocker()
        _, view, details = rendered([a_decision(blocker=blocker)], [an_agent()])
        server = make_server(view, details, allowance=an_allowance())
        self.addCleanup(server.server_close)
        serving = start_serving(server)
        self.addCleanup(serving.stop)

        connection = http.client.HTTPConnection(
            LOOPBACK_HOST, server.server_address[1], timeout=5
        )
        try:
            connection.request("GET", PAGE_PATH)
            response = connection.getresponse()
            status = response.status
            body = response.read().decode("utf-8")
        finally:
            connection.close()

        self.assertEqual(status, 200)
        payload = json.loads(body.split('id="queue-payload">', 1)[1].split("</script>", 1)[0])
        waiting = [
            payload["details"][row["itemId"]]
            for row in payload["rows"] if row["state"] == STATE_WAITING
        ]
        self.assertEqual(len(waiting), 1)
        detail = waiting[0]
        self.assertEqual(detail["project"], PROJECT)
        self.assertEqual(detail["ticket"], TICKET)
        self.assertEqual(detail["rail"], "issue-55-rail-one")
        self.assertEqual(detail["blocker"]["whatFailed"], blocker.what_failed)
        self.assertEqual(detail["blocker"]["agent"], blocker.agent)
        self.assertEqual(
            detail["blocker"]["missingCapability"], blocker.missing_capability
        )
        self.assertEqual(detail["blocker"]["humanChange"], blocker.human_change)
        self.assertIs(detail["blocker"]["stateChanged"], True)
        self.assertEqual(detail["blocker"]["nextAction"], blocker.next_action)

"""Read-only, fail-closed reporting from Copilot's local diagnostic surfaces."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Iterator

from .copilot_pricing import estimate_copilot_cost


MAX_CONTENT = 400
MAX_ACTIONS = 50


def _state(value: Any, status: str = "validated") -> dict[str, Any]:
    return {"status": status, "value": value}


def _unavailable(detail: str) -> dict[str, Any]:
    result = _state(None, "unavailable")
    result["detail"] = detail[:240]
    return result


def _format_error(detail: str) -> dict[str, Any]:
    result = _state(None, "error: unexpected log format")
    result["detail"] = detail[:240]
    return result


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON at line {line_number}: {exc.msg}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"record at line {line_number} is not an object")
            yield value


def _bounded_text(value: Any) -> dict[str, Any]:
    if not isinstance(value, str):
        return _unavailable("content is absent")
    text = value[:MAX_CONTENT]
    return {
        "status": "validated",
        "value": text,
        "length": len(value),
        "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
        "truncated": len(text) < len(value),
    }


def _readable_text(value: Any, preferred_role: str | None = None) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("[", "{")):
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError:
                return None
            if decoded == value:
                return value
            return _readable_text(decoded, preferred_role)
        return value
    if isinstance(value, list):
        parts = [_readable_text(item, preferred_role) for item in value]
        readable = [part for part in parts if part]
        return "\n".join(readable) if readable else None
    if isinstance(value, dict):
        role = value.get("role")
        if isinstance(role, str) and preferred_role and role != preferred_role:
            return None
        if isinstance(value.get("parts"), list):
            return _readable_text(value["parts"], preferred_role)
        for key in ("messages", "message", "content", "response", "text"):
            if key in value:
                readable = _readable_text(value[key], preferred_role)
                if readable:
                    return readable
    return None


def _bounded_readable(value: Any, preferred_role: str) -> dict[str, Any]:
    text = _readable_text(value, preferred_role)
    if text is None:
        return _unavailable("structured content is absent or malformed")
    return _bounded_text(text)


def _attachment_label(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = re.search(r"#attachment:(.+)$", value.strip())
    return match.group(1).strip() if match else None


def _find_attachment_content(value: Any, label: str, *, attachment_context: bool = False) -> Any:
    if isinstance(value, dict):
        keys = {str(key).lower() for key in value}
        is_attachment = attachment_context or any("attachment" in key for key in keys)
        names = [value.get(key) for key in ("name", "label", "title", "filename")]
        matches = any(isinstance(name, str) and (name == label or label in name) for name in names)
        if (is_attachment or matches) and "content" in value:
            return value["content"]
        for key, child in value.items():
            found = _find_attachment_content(child, label, attachment_context=is_attachment or "attachment" in str(key).lower())
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_attachment_content(child, label, attachment_context=attachment_context)
            if found is not None:
                return found
    return None


def _partial_attachment(label: str) -> dict[str, Any]:
    result = _bounded_text(f"#attachment:{label} (content unavailable)")
    result["status"] = "partial"
    result["detail"] = "attachment content unavailable in accepted sources"
    return result


def _is_report_prompt(segment: list[dict[str, Any]]) -> bool:
    user = next((record for record in segment if record.get("type") == "user_message"), None)
    attrs = user.get("attrs", {}) if isinstance(user, dict) else {}
    content = attrs.get("content") if isinstance(attrs, dict) else None
    readable = _readable_text(content, "user")
    return isinstance(readable, str) and " ".join(readable.split()) == "/report"


def _timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) / (1000 if value > 10_000_000_000 else 1)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _repo_match(record: dict[str, Any], repo_root: str) -> bool:
    attrs = record.get("attrs")
    if not isinstance(attrs, dict):
        return False
    for key in (
        "repository", "repo", "cwd", "workspace", "workspaceFolder",
        "github.copilot.git.repository", "copilot_chat.repo.remote_url",
    ):
        value = attrs.get(key)
        if isinstance(value, str) and (value == repo_root or repo_root in value):
            return True
    def contains_repo(value: Any) -> bool:
        if isinstance(value, str):
            return value == repo_root or repo_root in value
        if isinstance(value, dict):
            return any(contains_repo(item) for item in value.values())
        if isinstance(value, list):
            return any(contains_repo(item) for item in value)
        return False

    return contains_repo(attrs)


def parse_agent_debug(path: Path, repo_root: str, *, exclude_session: str | None = None) -> dict[str, Any]:
    candidate: list[dict[str, Any]] | None = None
    current: list[dict[str, Any]] | None = None
    try:
        for record in _iter_jsonl(path):
            if record.get("type") == "user_message":
                if current is not None:
                    if any(item.get("type") == "turn_end" for item in current):
                        session_ids = {item.get("sid") for item in current if isinstance(item.get("sid"), str)}
                        if (not exclude_session or exclude_session not in session_ids) and not _is_report_prompt(current):
                            if any(_repo_match(item, repo_root) for item in current):
                                candidate = current
                current = []
            if current is not None:
                current.append(record)
        if current is not None:
            if any(item.get("type") == "turn_end" for item in current):
                session_ids = {item.get("sid") for item in current if isinstance(item.get("sid"), str)}
                if (not exclude_session or exclude_session not in session_ids) and not _is_report_prompt(current):
                    if any(_repo_match(item, repo_root) for item in current):
                        candidate = current
    except OSError as exc:
        return {"source": "agent-debug", "status": "unavailable", "detail": str(exc)}
    except ValueError as exc:
        return {"source": "agent-debug", "status": "error: unexpected log format", "detail": str(exc)[:240]}
    if candidate is None:
        return {"source": "agent-debug", "status": "unavailable", "detail": "no completed repository-associated turn"}
    segment = candidate
    user = next((r for r in segment if r.get("type") == "user_message"), None)
    responses = [r for r in segment if r.get("type") == "agent_response"]
    ends = [r for r in segment if r.get("type") == "turn_end"]
    first = _timestamp(segment[0].get("ts"))
    last = _timestamp(segment[-1].get("ts"))
    if first is None or last is None or last < first:
        return {"source": "agent-debug", "status": "error: unexpected log format", "detail": "invalid turn timestamps"}
    attrs = user.get("attrs", {}) if isinstance(user, dict) else {}
    user_content = attrs.get("content") if isinstance(attrs, dict) else None
    final_attrs = responses[-1].get("attrs", {}) if responses else {}
    final_response = final_attrs.get("response") if isinstance(final_attrs, dict) else None
    prompt_label = _attachment_label(user_content)
    if prompt_label:
        attachment_content = _find_attachment_content(segment, prompt_label)
        prompt = _bounded_readable(attachment_content, "user") if attachment_content is not None else _partial_attachment(prompt_label)
    else:
        prompt = _bounded_readable(user_content, "user")
    actions = []
    for record in segment:
        if record.get("type") != "tool_call":
            continue
        record_attrs = record.get("attrs") if isinstance(record.get("attrs"), dict) else {}
        args = record_attrs.get("args")
        tool_name = "unknown"
        if isinstance(args, str):
            try:
                parsed_args = json.loads(args)
                if isinstance(parsed_args, dict):
                    tool_name = str(parsed_args.get("name", parsed_args.get("toolName", tool_name)))
            except json.JSONDecodeError:
                pass
        actions.append({"tool": tool_name[:80], "durationMs": record.get("dur"), "status": "completed"})
    return {
        "source": "agent-debug",
        "status": "validated",
        "session": _state(next(iter({r.get("sid") for r in segment if isinstance(r.get("sid"), str)}), None)),
        "firstTimestamp": _state(first),
        "lastTimestamp": _state(last),
        "turnEndTimestamp": _state(_timestamp(ends[-1].get("ts"))),
        "completion": _state(True if ends else False),
        "prompt": prompt,
        "finalResponse": _bounded_readable(final_response, "assistant"),
        "turnCount": _state(len(ends)),
        "records": _state(len(segment)),
        "actions": _state(actions[:MAX_ACTIONS]),
    }


def parse_agent_debug_files(paths: Iterable[Path], repo_root: str, *, exclude_session: str | None = None) -> dict[str, Any]:
    candidates: list[tuple[float, str, int, dict[str, Any]]] = []
    errors: list[dict[str, Any]] = []
    for path in paths:
        parsed = parse_agent_debug(path, repo_root, exclude_session=exclude_session)
        if parsed.get("status") == "validated":
            completion = parsed.get("turnEndTimestamp", {}).get("value")
            if isinstance(completion, (int, float)):
                candidates.append((completion, str(path), len(candidates), parsed))
        elif parsed.get("status", "").startswith("error:"):
            errors.append(parsed)
    if candidates:
        return sorted(candidates, key=lambda item: (-item[0], item[1], item[2]))[0][3]
    if errors:
        return errors[0]
    return {"source": "agent-debug", **_unavailable("no completed repository-associated turn")}


def _hr_time(record: dict[str, Any]) -> float | None:
    value = record.get("hrTime")
    if isinstance(value, list) and len(value) == 2 and all(isinstance(item, int) for item in value):
        return value[0] + value[1] / 1_000_000_000
    return None


def parse_otel(path: Path, *, session: str, start: float, end: float) -> dict[str, Any]:
    recognized: list[dict[str, Any]] = []
    in_window: list[dict[str, Any]] = []
    matched: list[dict[str, Any]] = []
    try:
        for record in _iter_jsonl(path):
            timestamp = _hr_time(record)
            resource = record.get("resource")
            resource_attrs = resource.get("_rawAttributes") if isinstance(resource, dict) else []
            sessions = {item[1] for item in resource_attrs if isinstance(item, list) and len(item) == 2 and item[0] == "session.id"}
            if timestamp is None:
                continue
            recognized.append(record)
            if start <= timestamp <= end:
                in_window.append(record)
                if session in sessions:
                    matched.append(record)
    except OSError as exc:
        return {"source": "otel", "status": "unavailable", "detail": str(exc)}
    except ValueError as exc:
        return {"source": "otel", "status": "error: unexpected log format", "detail": str(exc)[:240]}

    if not recognized:
        return {"source": "otel", "status": "unavailable", "detail": "validated source is empty"}

    if not in_window:
        detail = f"validated source; no in-window records ({len(recognized)} recognized records, 0 in-window records)"
        return {
            "source": "otel",
            "status": "partial",
            "detail": detail,
            "recognizedCount": _state(len(recognized)),
            "inWindowCount": _state(0),
            "matchedCount": _state(0),
            "session": _state(session),
            "inputTokens": _state(None, "partial"),
            "outputTokens": _state(None, "partial"),
        }

    if not matched:
        detail = f"validated source; session unmatched ({len(in_window)} in-window records, 0 exact-session matches)"
        return {
            "source": "otel",
            "status": "partial",
            "detail": detail,
            "recognizedCount": _state(len(recognized)),
            "inWindowCount": _state(len(in_window)),
            "matchedCount": _state(0),
            "session": _state(session),
            "inputTokens": _state(None, "partial"),
            "outputTokens": _state(None, "partial"),
        }

    selected = matched
    counts = Counter()
    models = Counter()
    model_calls: dict[str, list[dict[str, int]]] = {}
    input_tokens = output_tokens = 0
    actions: list[dict[str, Any]] = []
    errors = 0
    for record in selected:
        attrs = record.get("attributes")
        if not isinstance(attrs, dict) or not isinstance(attrs.get("event.name"), str):
            return {"source": "otel", "status": "error: unexpected log format", "detail": "missing event.name"}
        event = attrs["event.name"]
        counts[event] += 1
        if event == "gen_ai.client.inference.operation.details":
            model = attrs.get("gen_ai.request.model")
            incoming = attrs.get("gen_ai.usage.input_tokens")
            outgoing = attrs.get("gen_ai.usage.output_tokens")
            if not isinstance(model, str) or not isinstance(incoming, int) or not isinstance(outgoing, int):
                return {"source": "otel", "status": "error: unexpected log format", "detail": "inference record missing model/tokens"}
            models[model] += 1
            input_tokens += incoming
            output_tokens += outgoing
            model_calls.setdefault(model, []).append({"inputTokens": incoming, "outputTokens": outgoing, "inputSize": incoming})
        if event == "copilot_chat.tool.call":
            actions.append({"tool": attrs.get("gen_ai.tool.name", "unknown"), "success": attrs.get("success"), "durationMs": attrs.get("duration_ms")})
            if "error.type" in attrs:
                errors += 1
    intervals = []
    for record in selected:
        attrs = record.get("attributes") if isinstance(record.get("attributes"), dict) else {}
        if attrs.get("event.name") != "copilot_chat.tool.call":
            continue
        timestamp = _hr_time(record)
        duration = attrs.get("duration_ms")
        if timestamp is not None and isinstance(duration, (int, float)) and duration >= 0:
            intervals.append((timestamp - duration / 1000, timestamp))
    return {
        "source": "otel", "status": "validated", "session": _state(session),
        "firstTimestamp": _state(min(_hr_time(r) for r in selected if _hr_time(r) is not None)),
        "lastTimestamp": _state(max(_hr_time(r) for r in selected if _hr_time(r) is not None)),
        "eventCounts": _state(dict(counts)), "models": _state(dict(models)),
        "modelCalls": _state(model_calls),
        "inputTokens": _state(input_tokens), "outputTokens": _state(output_tokens),
        "actions": _state(actions[:MAX_ACTIONS]), "errorCount": _state(errors),
        "toolDurationMs": _state(sum(item.get("duration_ms", 0) for item in (r.get("attributes", {}) for r in selected) if isinstance(item, dict) and isinstance(item.get("duration_ms"), (int, float)))),
        "toolUnionMs": merge_intervals(intervals),
    }


def parse_terminal_diagnostics(path: Path, *, start: float | None = None, end: float | None = None) -> dict[str, Any]:
    if path.suffix != ".log":
        return {"source": "terminal-diagnostic", "status": "unavailable", "detail": "validated source is plaintext terminal.log"}
    return _parse_terminal_plaintext(path, start=start, end=end)


_TERMINAL_LINE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2} [0-9:.]+) \[[^]]+\] (?P<body>.*)$")
_PARSED_COMMANDS = re.compile(r"Parsed sub-commands via .*? grammar (?P<commands>\[\[.*\]\])$")
_USING = re.compile(r"RunInTerminalTool: Using .*? execute strategy for command `(?P<command>.*)` \[\]$")
_FINISHED = re.compile(r"RunInTerminalTool: Finished .*? with exitCode `(?P<code>[^`]*)`, result\.length `(?P<length>\d+)`, error `(?P<error>[^`]*)` \[\]$")


def _parse_terminal_plaintext(path: Path, *, start: float | None, end: float | None) -> dict[str, Any]:
    analyzers: list[dict[str, Any]] = []
    executions: list[dict[str, Any]] = []
    pending: dict[str, Any] | None = None
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line_number, line in enumerate(stream, 1):
                match = _TERMINAL_LINE.match(line.rstrip("\n"))
                if not match:
                    continue
                timestamp = _timestamp(match.group("date"))
                if timestamp is None or (start is not None and timestamp < start) or (end is not None and timestamp > end):
                    continue
                body = match.group("body")
                parsed = _PARSED_COMMANDS.search(body)
                if parsed:
                    try:
                        commands = json.loads(parsed.group("commands"))[0]
                    except (json.JSONDecodeError, IndexError, TypeError):
                        return {"source": "terminal-diagnostic", "status": "error: unexpected log format", "detail": f"invalid parsed commands at line {line_number}"}
                    if not isinstance(commands, list) or not all(isinstance(command, str) for command in commands):
                        return {"source": "terminal-diagnostic", "status": "error: unexpected log format", "detail": f"parsed commands are not strings at line {line_number}"}
                    pending = {"timestamp": timestamp, "commands": commands, "denied": False, "reason": None}
                    analyzers.append(pending)
                    continue
                if "RunInTerminalTool#CommandLineAutoApproveAnalyzer: Parsed sub-commands" in body:
                    return {"source": "terminal-diagnostic", "status": "error: unexpected log format", "detail": f"unparseable analyzer record at line {line_number}"}
                if "Sub-command DENIED auto approval" in body or "Command line NOT auto-approved" in body:
                    if pending is not None:
                        pending["denied"] = True
                    continue
                if "no matching auto approve entries" in body.lower():
                    if pending is not None:
                        pending["reason"] = body[:MAX_CONTENT]
                    continue
                using = _USING.search(body)
                if using:
                    executions.append({"timestamp": timestamp, "command": using.group("command")[:MAX_CONTENT]})
                    continue
                finished = _FINISHED.search(body)
                if finished and executions:
                    executions[-1]["exitCode"] = finished.group("code") or None
                    executions[-1]["resultLength"] = int(finished.group("length"))
                    executions[-1]["error"] = finished.group("error")
    except OSError as exc:
        return {"source": "terminal-diagnostic", "status": "unavailable", "detail": str(exc)}
    if not analyzers and not executions:
        return {"source": "terminal-diagnostic", "status": "unavailable", "detail": "no bounded plaintext terminal records"}
    requests: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    denied_commands = {
        _normalize_command(command): entry
        for entry in analyzers
        if entry.get("denied")
        for command in entry["commands"]
    }
    used_execution_indices: set[int] = set()
    for entry in analyzers:
        commands = entry["commands"]
        if not commands:
            continue
        key = (tuple(commands), entry["timestamp"])
        if key in seen:
            continue
        seen.add(key)
        denied = entry.get("denied", False)
        denied_entry = None
        if denied:
            for cmd in commands:
                if _normalize_command(cmd) in denied_commands:
                    denied_entry = denied_commands[_normalize_command(cmd)]
                    break
        command = commands[0]
        execution = None
        matching_indices: list[int] = []
        for idx, candidate in enumerate(executions):
            if idx in used_execution_indices:
                continue
            if candidate["timestamp"] < entry["timestamp"]:
                continue
            candidate_normalized = _normalize_command(candidate["command"])
            for cmd in commands:
                if _normalize_command(cmd) == candidate_normalized:
                    matching_indices.append(idx)
                    break
        disposition = "unresolved"
        if len(matching_indices) == 1:
            execution = executions[matching_indices[0]]
            used_execution_indices.add(matching_indices[0])
            disposition = "executed"
        elif len(matching_indices) > 1:
            execution = executions[matching_indices[0]]
            used_execution_indices.add(matching_indices[0])
            disposition = "ambiguous"
        wait = execution["timestamp"] - entry["timestamp"] if denied and execution else None
        requests.append({"command": command[:MAX_CONTENT], "terminalApprovalRequest": denied, "denialReason": denied_entry.get("reason") if denied_entry else None, "disposition": disposition, "requestTimestamp": entry["timestamp"], "executionTimestamp": execution["timestamp"] if execution else None, "approvalWaitSeconds": wait})
    if not requests:
        return {"source": "terminal-diagnostic", "status": "validated", "terminalRequestCount": _state(0), "terminalApprovalRequestCount": _state(0), "approvalRequests": _state([]), "approvalWaitSeconds": _state({"status": "validated", "value": 0})}
    approvals = [request for request in requests if request["terminalApprovalRequest"]]
    unresolved_approvals = [r for r in approvals if r["disposition"] == "unresolved"]
    ambiguous_approvals = [r for r in approvals if r.get("disposition") == "ambiguous"]
    resolved_approvals = [r for r in approvals if r["disposition"] == "executed"]
    if unresolved_approvals or ambiguous_approvals:
        resolved_count = len(resolved_approvals)
        total_count = len(approvals)
        timing_status = "partial"
        timing_value = f"{resolved_count}/{total_count} approval requests correlated"
    elif approvals:
        waits = [request["approvalWaitSeconds"] for request in resolved_approvals if request["approvalWaitSeconds"] is not None]
        timing_status = "validated"
        timing_value = sum(waits)
    else:
        timing_status = "validated"
        timing_value = 0
    approval_wait_seconds = {"status": timing_status, "value": timing_value}
    return {"source": "terminal-diagnostic", "status": "validated", "terminalRequestCount": _state(len(requests)), "terminalApprovalRequestCount": _state(len(approvals)), "approvalRequests": _state(approvals[:MAX_ACTIONS]), "approvalWaitSeconds": approval_wait_seconds}


def _normalize_command(command: str) -> str:
    return " ".join(command.strip().split())


def merge_intervals(intervals: Iterable[tuple[float, float]]) -> dict[str, Any]:
    ordered = sorted((start, end) for start, end in intervals if end >= start)
    if not ordered:
        return _state(0)
    total = 0.0
    current_start, current_end = ordered[0]
    for start, end in ordered[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            total += current_end - current_start
            current_start, current_end = start, end
    total += current_end - current_start
    return _state(total)


def _source_line(source: dict[str, Any]) -> str:
    label = source.get("source", "source")
    status = source.get("status", "unavailable")
    if status == "validated":
        return f"{label}: {status}"
    detail = source.get("detail")
    if isinstance(detail, str) and detail.strip():
        return f"{label}: {status} - {detail[:240]}"
    return f"{label}: {status}"


def render_copilot_report(
    *, agent_debug: dict[str, Any], otel: dict[str, Any], terminal: dict[str, Any] | None = None,
    active_issue: str | None = None,
) -> str:
    """Render a bounded report; source failures remain visible and never become zero."""
    lines = ["Copilot work report", f"Issue: {active_issue or 'unavailable'}"]
    for source in (agent_debug, otel, terminal or {"source": "terminal-diagnostic", "status": "unavailable"}):
        lines.append(_source_line(source))
    lines.append(f"Provenance: {agent_debug.get('session', {}).get('value', 'unavailable')}")
    def format_content(label: str, content: dict[str, Any]) -> str:
        status = content.get("status", "unavailable")
        prefix = label if status == "validated" else f"{label} [{status}]"
        value = content.get("value") if status in {"validated", "partial"} else "unavailable"
        metadata = ""
        if status == "validated" and content.get("truncated"):
            metadata = f" (truncated; length {content.get('length', 'unknown')}; sha256 {content.get('sha256', 'unknown')})"
        return f"{prefix}: {value}{metadata}"

    lines.append(format_content("Prompt", agent_debug.get("prompt", {})))
    lines.append(format_content("Final outcome", agent_debug.get("finalResponse", {})))
    if terminal is None or terminal.get("status") != "validated":
        lines.append(f"Approvals: {terminal.get('status', 'unavailable') if terminal else 'unavailable'}")
        lines.append("Approval timing: unavailable")
    else:
        requests = terminal.get("approvalRequests", {}).get("value", [])
        lines.append(f"Approvals: {terminal['terminalApprovalRequestCount']['value']} terminal approval requests")
        for request in requests[:MAX_ACTIONS]:
            wait = request.get("approvalWaitSeconds")
            wait_text = f"; approval wait {wait:.3f}s" if isinstance(wait, (int, float)) else "; approval wait unavailable"
            reason = request.get("denialReason") or "denial reason unavailable"
            lines.append(f"Approval request: {request['command']} ({reason}; {request['disposition']}{wait_text})")
        approval_timing = terminal.get("approvalWaitSeconds", {})
        timing_status = approval_timing.get("status", "unavailable")
        timing_value = approval_timing.get("value", "unavailable")
        if timing_status == "partial":
            lines.append(f"Approval timing: {timing_value} (partial)")
        elif timing_status == "validated":
            lines.append(f"Approval timing: {timing_value} seconds validated")
        else:
            lines.append(f"Approval timing: unavailable")
    if terminal is not None and terminal.get("status") == "validated":
        lines.append(f"Timing: approval wait {terminal['approvalWaitSeconds']['value']} seconds validated")
    else:
        lines.append("Timing: partial - approval timing unavailable")
    if otel.get("status") == "validated":
        lines.append(f"Tokens: {otel['inputTokens']['value']} input, {otel['outputTokens']['value']} output")
        usage = {"inputTokens": otel["inputTokens"]["value"], "outputTokens": otel["outputTokens"]["value"], "models": [{"requestModel": model, "calls": calls} for model, calls in otel.get("modelCalls", {}).get("value", {}).items()]}
        cost = estimate_copilot_cost(usage)
        lines.append(f"Cost: {cost.get('status', 'unavailable')}")
        lines.append(f"Actions: {len(otel['actions']['value'])}")
        lines.append(f"Errors: {otel.get('errorCount', {}).get('value', 'unavailable')}")
        lines.append(f"Tool time: {otel.get('toolUnionMs', {}).get('value', 'unavailable')} ms union")
    else:
        lines.append("Tokens: unavailable")
    return "\n".join(lines)


def _latest_file(root: Path, pattern: str) -> Path | None:
    candidates = [path for path in root.glob(pattern) if path.is_file()]
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def _debug_files(root: Path) -> list[Path]:
    return sorted(root.glob("*/GitHub.copilot-chat/debug-logs/*/main.jsonl"))


def render_latest_copilot_report(
    repo_root: Path,
    *,
    exclude_session: str | None = None,
    settings_path: Path | None = None,
    debug_root: Path | None = None,
    terminal_root: Path | None = None,
) -> str:
    """Discover local Copilot sources and render one read-only report."""
    settings_path = settings_path or (Path.home() / ".config/Code/User/settings.json")
    otel_path: Path | None = None
    if settings_path.is_file():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            configured = settings.get("github.copilot.chat.otel.outfile")
            if isinstance(configured, str) and configured.strip():
                otel_path = Path(configured).expanduser()
        except (OSError, json.JSONDecodeError):
            otel_path = None
    debug_paths = _debug_files(debug_root or (Path.home() / ".config/Code/User/workspaceStorage"))
    terminal_path = _latest_file(terminal_root or (Path.home() / ".config/Code/logs"), "*/terminal.log")
    agent = parse_agent_debug_files(debug_paths, str(repo_root), exclude_session=exclude_session) if debug_paths else {"source": "agent-debug", **_unavailable("Agent Debug Log was not found")}
    session = agent.get("session", {}).get("value")
    first = agent.get("firstTimestamp", {}).get("value")
    last = agent.get("lastTimestamp", {}).get("value")
    if otel_path and isinstance(session, str) and isinstance(first, (int, float)) and isinstance(last, (int, float)):
        otel = parse_otel(otel_path, session=session, start=first, end=last)
    elif otel_path is None:
        otel = {"source": "otel", **_unavailable("OTel output path is not configured")}
    else:
        otel = {"source": "otel", **_unavailable("Agent Debug turn boundary is unavailable for OTel correlation")}
    terminal = parse_terminal_diagnostics(terminal_path, start=first, end=last) if terminal_path and isinstance(first, (int, float)) and isinstance(last, (int, float)) else {"source": "terminal-diagnostic", **_unavailable("terminal.log or completed turn boundary is unavailable")}
    active_issue = None
    active_issue_title = None
    workflow_path = repo_root / ".ai-dev/workflow.json"
    try:
        workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
        issue = workflow.get("activeIssueNumber")
        if isinstance(issue, int) and issue > 0:
            active_issue = str(issue)
        if isinstance(workflow.get("activeIssueTitle"), str) and workflow["activeIssueTitle"].strip():
            active_issue_title = workflow["activeIssueTitle"].strip()
    except (OSError, json.JSONDecodeError):
        pass
    report = render_copilot_report(agent_debug=agent, otel=otel, terminal=terminal, active_issue=active_issue)
    if active_issue_title:
        report = report.replace(f"Issue: {active_issue}", f"Issue: {active_issue} {active_issue_title}", 1)
    return report


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="flow-report")
    parser.add_argument("--exclude-session")
    arguments = parser.parse_args()
    print(render_latest_copilot_report(Path.cwd(), exclude_session=arguments.exclude_session))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

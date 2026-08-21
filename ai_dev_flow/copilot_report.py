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
    return False


def parse_agent_debug(path: Path, repo_root: str, *, exclude_session: str | None = None) -> dict[str, Any]:
    candidate: list[dict[str, Any]] | None = None
    current: list[dict[str, Any]] | None = None
    try:
        for record in _iter_jsonl(path):
            if record.get("type") == "user_message":
                if current is not None:
                    if any(item.get("type") == "turn_end" for item in current):
                        session_ids = {item.get("sid") for item in current if isinstance(item.get("sid"), str)}
                        if not exclude_session or exclude_session not in session_ids:
                            if any(_repo_match(item, repo_root) for item in current):
                                candidate = current
                current = []
            if current is not None:
                current.append(record)
        if current is not None:
            if any(item.get("type") == "turn_end" for item in current):
                session_ids = {item.get("sid") for item in current if isinstance(item.get("sid"), str)}
                if not exclude_session or exclude_session not in session_ids:
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
        "completion": _state(True if ends else False),
        "prompt": _bounded_text(user_content),
        "finalResponse": _bounded_text(final_response),
        "turnCount": _state(len(ends)),
        "records": _state(len(segment)),
        "actions": _state(actions[:MAX_ACTIONS]),
    }


def _hr_time(record: dict[str, Any]) -> float | None:
    value = record.get("hrTime")
    if isinstance(value, list) and len(value) == 2 and all(isinstance(item, int) for item in value):
        return value[0] + value[1] / 1_000_000_000
    return None


def parse_otel(path: Path, *, session: str, start: float, end: float) -> dict[str, Any]:
    selected: list[dict[str, Any]] = []
    try:
        for record in _iter_jsonl(path):
            timestamp = _hr_time(record)
            resource = record.get("resource")
            resource_attrs = resource.get("_rawAttributes") if isinstance(resource, dict) else []
            sessions = {item[1] for item in resource_attrs if isinstance(item, list) and len(item) == 2 and item[0] == "session.id"}
            if timestamp is None or not start <= timestamp <= end or session not in sessions:
                continue
            selected.append(record)
    except OSError as exc:
        return {"source": "otel", "status": "unavailable", "detail": str(exc)}
    except ValueError as exc:
        return {"source": "otel", "status": "error: unexpected log format", "detail": str(exc)[:240]}
    if not selected:
        return {"source": "otel", "status": "unavailable", "detail": "no correlated records"}
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
    try:
        if path.suffix == ".log":
            return _parse_terminal_plaintext(path, start=start, end=end)
    except OSError as exc:
        return {"source": "terminal-diagnostic", "status": "unavailable", "detail": str(exc)}
    actions = []
    try:
        records = _iter_jsonl(path)
        for record in records:
            timestamp = _timestamp(record.get("timestamp", record.get("ts")))
            if start is not None and (timestamp is None or timestamp < start): continue
            if end is not None and (timestamp is None or timestamp > end): continue
            if not isinstance(record.get("command"), str) or not isinstance(record.get("status"), (str, int)):
                return {"source": "terminal-diagnostic", "status": "error: unexpected log format", "detail": "terminal record missing command/status"}
            actions.append({"command": record["command"][:MAX_CONTENT], "status": record["status"], "durationMs": record.get("durationMs"), "autoApprovalReason": record.get("autoApprovalReason")})
    except OSError as exc:
        return {"source": "terminal-diagnostic", "status": "unavailable", "detail": str(exc)}
    except ValueError as exc:
        return {"source": "terminal-diagnostic", "status": "error: unexpected log format", "detail": str(exc)[:240]}
    if not actions:
        return {"source": "terminal-diagnostic", "status": "unavailable", "detail": "no terminal records"}
    return {"source": "terminal-diagnostic", "status": "validated", "actions": _state(actions[:MAX_ACTIONS]), "count": _state(len(actions))}


_TERMINAL_LINE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2} [0-9:.]+) \[[^]]+\] (?P<body>.*)$")
_PARSED_COMMANDS = re.compile(r"Parsed sub-commands via .*? grammar (?P<commands>\[\[.*\]\])$")
_USING = re.compile(r"RunInTerminalTool: Using .*? execute strategy for command `(?P<command>.*)` \[\]$")
_FINISHED = re.compile(r"RunInTerminalTool: Finished .*? with exitCode `(?P<code>[^`]*)`, result\.length `(?P<length>\d+)`, error `(?P<error>[^`]*)` \[\]$")


def _parse_terminal_plaintext(path: Path, *, start: float | None, end: float | None) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
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
                    entries.append({"timestamp": timestamp, "commands": commands, "denied": False, "reason": None})
                    continue
                if "RunInTerminalTool#CommandLineAutoApproveAnalyzer: Parsed sub-commands" in body:
                    return {"source": "terminal-diagnostic", "status": "error: unexpected log format", "detail": f"unparseable analyzer record at line {line_number}"}
                if "Sub-command DENIED auto approval" in body or "Command line NOT auto-approved" in body:
                    if entries:
                        entries[-1]["denied"] = True
                    continue
                if "no matching auto approve entries" in body.lower():
                    if entries:
                        entries[-1]["reason"] = body[:MAX_CONTENT]
                    continue
                using = _USING.search(body)
                if using:
                    entries.append({"timestamp": timestamp, "executed": using.group("command")[:MAX_CONTENT]})
                    continue
                finished = _FINISHED.search(body)
                if finished and entries:
                    entries[-1]["exitCode"] = finished.group("code") or None
                    entries[-1]["resultLength"] = int(finished.group("length"))
                    entries[-1]["error"] = finished.group("error")
    except OSError as exc:
        return {"source": "terminal-diagnostic", "status": "unavailable", "detail": str(exc)}
    if not entries:
        return {"source": "terminal-diagnostic", "status": "unavailable", "detail": "no bounded plaintext terminal records"}
    requests: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    denied_commands = {
        command
        for entry in entries
        if entry.get("denied")
        for command in entry.get("commands", [])
    }
    for entry in entries:
        commands = entry.get("commands")
        if not commands:
            continue
        key = tuple(commands)
        if key in seen:
            continue
        seen.add(key)
        command = commands[-1]
        execution = next((candidate for candidate in entries if isinstance(candidate.get("executed"), str) and _normalize_command(candidate["executed"]) == _normalize_command(command) and candidate["timestamp"] >= entry["timestamp"]), None)
        wait = execution["timestamp"] - entry["timestamp"] if execution else None
        requests.append({"command": command[:MAX_CONTENT], "denied": command in denied_commands, "denialReason": entry.get("reason"), "disposition": "executed" if execution else "unresolved", "requestTimestamp": entry["timestamp"], "executionTimestamp": execution["timestamp"] if execution else None, "waitSeconds": wait})
    if not requests:
        return {"source": "terminal-diagnostic", "status": "validated", "requests": _state([]), "approvalCount": _state(0)}
    return {"source": "terminal-diagnostic", "status": "validated", "requests": _state(requests[:MAX_ACTIONS]), "approvalCount": _state(sum(1 for request in requests if request["denied"]))}


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


def render_copilot_report(
    *, agent_debug: dict[str, Any], otel: dict[str, Any], terminal: dict[str, Any] | None = None,
    active_issue: str | None = None,
) -> str:
    """Render a bounded report; source failures remain visible and never become zero."""
    lines = ["Copilot work report", f"Issue: {active_issue or 'unavailable'}"]
    for source in (agent_debug, otel, terminal or {"source": "terminal-diagnostic", "status": "unavailable"}):
        lines.append(f"{source.get('source', 'source')}: {source.get('status', 'unavailable')}")
    lines.append(f"Provenance: {agent_debug.get('session', {}).get('value', 'unavailable')}")
    if agent_debug.get("prompt", {}).get("status") == "validated":
        lines.append(f"Prompt: {agent_debug['prompt']['value']}")
    else:
        lines.append("Prompt: unavailable")
    final = agent_debug.get("finalResponse", {})
    lines.append(f"Final outcome: {final.get('value') if final.get('status') == 'validated' else 'unavailable'}")
    lines.append(f"Approvals: unavailable")
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

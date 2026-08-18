from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from typing import Callable


GITHUB_AI_CREDIT_USAGE_PATH = "/users/{username}/settings/billing/ai_credit/usage"


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


CommandRunner = Callable[[list[str]], CommandResult]


def _run_command(arguments: list[str]) -> CommandResult:
    result = subprocess.run(
        arguments,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return CommandResult(result.returncode, result.stdout, result.stderr)


def _unavailable(
    *,
    captured_at: str,
    reason: str,
    detail: str,
    source: str,
    scope: dict[str, object],
) -> dict[str, object]:
    return {
        "status": "unavailable",
        "provider": "github-copilot",
        "capturedAt": captured_at,
        "source": source,
        "scope": scope,
        "reason": reason,
        "detail": detail,
    }


def _scope(username: str, year: int, month: int, day: int) -> dict[str, object]:
    return {
        "account": username,
        "granularity": "day",
        "date": {"year": year, "month": month, "day": day},
    }


def capture_github_ai_credit_usage(
    *,
    runner: CommandRunner = _run_command,
    now: datetime | None = None,
) -> dict[str, object]:
    captured_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    observation_source = "GET /users/{username}/settings/billing/ai_credit/usage"

    identity = runner(["gh", "api", "user", "-q", ".login"])
    username = identity.stdout.strip()
    if identity.returncode != 0 or not username:
        detail = (identity.stderr or identity.stdout).strip() or "GitHub identity could not be read."
        reason = "permission_denied" if "scope" in detail.lower() else "provider_access_unavailable"
        return _unavailable(
            captured_at=captured_at,
            reason=reason,
            detail=detail,
            source=observation_source,
            scope={"granularity": "day"},
        )

    year, month, day = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).timetuple()[:3]
    scope = _scope(username, year, month, day)
    endpoint = GITHUB_AI_CREDIT_USAGE_PATH.format(username=username)
    response = runner(
        [
            "gh",
            "api",
            "--method",
            "GET",
            endpoint,
            "-f",
            f"year={year}",
            "-f",
            f"month={month}",
            "-f",
            f"day={day}",
        ]
    )
    if response.returncode != 0:
        detail = (response.stderr or response.stdout).strip() or "GitHub usage endpoint could not be read."
        reason = "permission_denied" if "scope" in detail.lower() else "provider_access_unavailable"
        return _unavailable(
            captured_at=captured_at,
            reason=reason,
            detail=detail,
            source=observation_source,
            scope=scope,
        )

    try:
        provider_data = json.loads(response.stdout)
    except json.JSONDecodeError as exc:
        return _unavailable(
            captured_at=captured_at,
            reason="invalid_provider_response",
            detail=f"GitHub usage response was not valid JSON: {exc.msg}",
            source=observation_source,
            scope=scope,
        )

    if not isinstance(provider_data, dict):
        return _unavailable(
            captured_at=captured_at,
            reason="invalid_provider_response",
            detail="GitHub usage response was not a JSON object.",
            source=observation_source,
            scope=scope,
        )

    return {
        "status": "observed",
        "provider": "github-copilot",
        "capturedAt": captured_at,
        "source": observation_source,
        "scope": scope,
        "providerData": provider_data,
    }


def capture_copilot_otel_usage(
    path: Path,
    *,
    captured_at: str | None = None,
) -> dict[str, object]:
    """Capture completed Copilot agent-turn usage from an OTel JSONL export."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return {
            "status": "unavailable",
            "provider": "github-copilot",
            "capturedAt": captured_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "source": str(path),
            "scope": {"granularity": "session"},
            "reason": "otel_file_unavailable",
            "detail": str(exc),
        }

    agent_turns = []
    inference_calls = []

    def session_id_from_record(record: dict[str, object]) -> str | None:
        resource = record.get("resource")
        if isinstance(resource, dict):
            raw_attributes = resource.get("_rawAttributes")
            if isinstance(raw_attributes, list):
                for item in raw_attributes:
                    if isinstance(item, list) and len(item) == 2 and item[0] == "session.id" and isinstance(item[1], str):
                        return item[1]
        return None

    def integer_attribute(attributes: dict[str, object], *keys: str) -> int | None:
        for key in keys:
            value = attributes.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
        return None

    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        attributes = record.get("attributes")
        if not isinstance(attributes, dict):
            continue
        event_name = attributes.get("event.name")
        input_tokens = integer_attribute(attributes, "gen_ai.usage.input_tokens")
        output_tokens = integer_attribute(attributes, "gen_ai.usage.output_tokens")
        if event_name == "copilot_chat.agent.turn":
            turn_index = attributes.get("turn.index")
            if (
                isinstance(turn_index, int)
                and not isinstance(turn_index, bool)
                and input_tokens is not None
                and output_tokens is not None
            ):
                agent_turns.append(
                    {
                        "turn": turn_index,
                        "inputTokens": input_tokens,
                        "outputTokens": output_tokens,
                        "toolCallCount": attributes.get("tool_call_count", 0),
                        "session": session_id_from_record(record),
                    }
                )
            continue
        if event_name != "gen_ai.client.inference.operation.details" or input_tokens is None or output_tokens is None:
            continue
        request_model = attributes.get("gen_ai.request.model")
        response_model = attributes.get("gen_ai.response.model")
        if not isinstance(request_model, str) or not request_model.strip():
            continue
        inference_calls.append(
            {
                "requestModel": request_model,
                "responseModel": response_model if isinstance(response_model, str) and response_model.strip() else None,
                "inputTokens": input_tokens,
                "outputTokens": output_tokens,
                "cacheReadInputTokens": integer_attribute(
                    attributes,
                    "gen_ai.usage.cache_read_input_tokens",
                    "gen_ai.usage.cached_input_tokens",
                ),
                "cacheWriteInputTokens": integer_attribute(
                    attributes,
                    "gen_ai.usage.cache_creation_input_tokens",
                    "gen_ai.usage.cache_write_input_tokens",
                ),
                "reasoningTokens": integer_attribute(
                    attributes,
                    "gen_ai.usage.reasoning_tokens",
                    "gen_ai.usage.reasoning_output_tokens",
                ),
                "session": session_id_from_record(record),
            }
        )

    records = inference_calls or agent_turns
    if not records:
        return {
            "status": "unavailable",
            "provider": "github-copilot",
            "capturedAt": captured_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "source": str(path),
            "scope": {"granularity": "session"},
            "reason": "otel_agent_turns_unavailable",
            "detail": "No completed copilot_chat.agent.turn records were found.",
        }

    session_ids = sorted({item["session"] for item in records if item.get("session")})
    scope: dict[str, object] = {"granularity": "session"}
    if len(session_ids) == 1:
        scope["session"] = session_ids[0]
    input_total = sum(item["inputTokens"] for item in records)
    output_total = sum(item["outputTokens"] for item in records)
    usage_item: dict[str, object] = {
        "unitType": "tokens",
        "quantity": input_total + output_total,
        "inputTokens": input_total,
        "outputTokens": output_total,
        "inputTokenCategories": {
            "total": input_total,
            "fresh": None,
            "cached": None,
            "cacheWrite": None,
        },
        "outputTokenCategories": {"total": output_total, "reasoning": None},
        "scope": scope,
        "nativeEvent": "gen_ai.client.inference.operation.details" if inference_calls else "copilot_chat.agent.turn",
        "aggregation": "inference_calls" if inference_calls else "agent_turn",
    }
    if inference_calls:
        model_groups: dict[tuple[str, str | None], list[dict[str, object]]] = {}
        for call in inference_calls:
            model_groups.setdefault((call["requestModel"], call["responseModel"]), []).append(call)
        usage_item["models"] = [
            {
                "requestModel": request_model,
                "responseModel": response_model,
                "callCount": len(calls),
                "inputTokens": sum(call["inputTokens"] for call in calls),
                "outputTokens": sum(call["outputTokens"] for call in calls),
                "calls": [
                    {
                        "inputTokens": call["inputTokens"],
                        "outputTokens": call["outputTokens"],
                        "inputSize": call["inputTokens"],
                        "cacheReadInputTokens": call["cacheReadInputTokens"],
                        "cacheWriteInputTokens": call["cacheWriteInputTokens"],
                        "reasoningTokens": call["reasoningTokens"],
                    }
                    for call in calls
                ],
            }
            for (request_model, response_model), calls in sorted(model_groups.items())
        ]
        for category_key, field in (
            ("cached", "cacheReadInputTokens"),
            ("cacheWrite", "cacheWriteInputTokens"),
        ):
            values = [call[field] for call in inference_calls]
            if all(value is not None for value in values):
                usage_item["inputTokenCategories"][category_key] = sum(values)
        cached = usage_item["inputTokenCategories"]["cached"]
        cache_write = usage_item["inputTokenCategories"]["cacheWrite"]
        if cached is not None and cache_write is not None:
            usage_item["inputTokenCategories"]["fresh"] = input_total - cached - cache_write
        reasoning_values = [call["reasoningTokens"] for call in inference_calls]
        if all(value is not None for value in reasoning_values):
            usage_item["outputTokenCategories"]["reasoning"] = sum(reasoning_values)
    else:
        usage_item["turns"] = [
            {
                key: item[key]
                for key in ("turn", "inputTokens", "outputTokens", "toolCallCount")
            }
            for item in agent_turns
        ]
    usage_items = [usage_item]
    return {
        "status": "observed",
        "provider": "github-copilot",
        "capturedAt": captured_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": str(path),
        "scope": scope,
        "providerData": {
            "aggregation": "agent_turn",
            "usageItems": usage_items,
        },
    }


def main() -> int:
    print(json.dumps(capture_github_ai_credit_usage(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

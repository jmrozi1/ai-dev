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

    records = []
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
        if not isinstance(attributes, dict) or attributes.get("event.name") != "copilot_chat.agent.turn":
            continue
        turn_index = attributes.get("turn.index")
        input_tokens = attributes.get("gen_ai.usage.input_tokens")
        output_tokens = attributes.get("gen_ai.usage.output_tokens")
        if (
            isinstance(turn_index, bool)
            or not isinstance(turn_index, int)
            or isinstance(input_tokens, bool)
            or not isinstance(input_tokens, int)
            or input_tokens < 0
            or isinstance(output_tokens, bool)
            or not isinstance(output_tokens, int)
            or output_tokens < 0
        ):
            continue
        resource = record.get("resource")
        session_id = None
        if isinstance(resource, dict):
            raw_attributes = resource.get("_rawAttributes")
            if isinstance(raw_attributes, list):
                for item in raw_attributes:
                    if isinstance(item, list) and len(item) == 2 and item[0] == "session.id" and isinstance(item[1], str):
                        session_id = item[1]
                        break
        records.append(
            {
                "turn": turn_index,
                "inputTokens": input_tokens,
                "outputTokens": output_tokens,
                "toolCallCount": attributes.get("tool_call_count", 0),
                "line": line_number,
                "session": session_id,
            }
        )

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

    session_ids = sorted({item["session"] for item in records if item["session"]})
    scope: dict[str, object] = {"granularity": "session"}
    if len(session_ids) == 1:
        scope["session"] = session_ids[0]
    usage_items = [
        {
            "unitType": "tokens",
            "quantity": sum(item["inputTokens"] + item["outputTokens"] for item in records),
            "inputTokens": sum(item["inputTokens"] for item in records),
            "outputTokens": sum(item["outputTokens"] for item in records),
            "scope": scope,
            "nativeEvent": "copilot_chat.agent.turn",
            "turns": [
                {
                    key: item[key]
                    for key in ("turn", "inputTokens", "outputTokens", "toolCallCount")
                }
                for item in records
            ],
        }
    ]
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

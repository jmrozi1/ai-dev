from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from ai_dev_flow.usage_accounting import reconcile_issue_usage
from ai_dev_flow.usage_observation import CommandResult, capture_copilot_otel_usage, capture_github_ai_credit_usage


class UsageObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 18, 16, 0, tzinfo=timezone.utc)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.repo_root = Path(self.temp_dir.name)

    def test_permission_limited_usage_is_explicitly_unavailable(self) -> None:
        calls: list[list[str]] = []

        def runner(arguments: list[str]) -> CommandResult:
            calls.append(arguments)
            return CommandResult(
                returncode=1,
                stdout='{"message":"Not Found","status":"404"}',
                stderr="This API operation needs the user scope.",
            )

        observation = capture_github_ai_credit_usage(runner=runner, now=self.now)

        self.assertEqual(observation["status"], "unavailable")
        self.assertEqual(observation["reason"], "permission_denied")
        self.assertEqual(observation["scope"], {"granularity": "day"})
        self.assertEqual(len(calls), 1)

    def test_successful_response_preserves_native_data_and_scope(self) -> None:
        payload = {
            "timePeriod": {"year": 2026, "month": 8, "day": 18},
            "user": "jmrozi1",
            "product": "Copilot",
            "usageItems": [
                {
                    "product": "Copilot",
                    "sku": "premium_request",
                    "model": "gpt-5",
                    "unitType": "requests",
                    "grossQuantity": 3,
                    "netAmount": 0.12,
                }
            ],
        }
        calls: list[list[str]] = []

        def runner(arguments: list[str]) -> CommandResult:
            calls.append(arguments)
            if arguments[2:5] == ["user", "-q", ".login"]:
                return CommandResult(0, "jmrozi1\n", "")
            return CommandResult(0, json.dumps(payload), "")

        observation = capture_github_ai_credit_usage(runner=runner, now=self.now)

        self.assertEqual(observation["status"], "observed")
        self.assertEqual(observation["providerData"], payload)
        self.assertEqual(
            observation["scope"],
            {
                "account": "jmrozi1",
                "granularity": "day",
                "date": {"year": 2026, "month": 8, "day": 18},
            },
        )
        self.assertEqual(len(calls), 2)

    def test_otel_uses_completed_agent_turn_aggregate_without_inference_double_counting(self) -> None:
        path = self.repo_root / "copilot-otel.jsonl"
        path.write_text(
            "\n".join(
                (
                    json.dumps({
                        "resource": {"_rawAttributes": [["session.id", "session-1"]]},
                        "attributes": {
                            "event.name": "gen_ai.client.inference.operation.details",
                            "gen_ai.usage.input_tokens": 900,
                            "gen_ai.usage.output_tokens": 90,
                        },
                    }),
                    json.dumps({
                        "resource": {"_rawAttributes": [["session.id", "session-1"]]},
                        "attributes": {
                            "event.name": "copilot_chat.agent.turn",
                            "turn.index": 0,
                            "gen_ai.usage.input_tokens": 100,
                            "gen_ai.usage.output_tokens": 10,
                            "tool_call_count": 2,
                        },
                    }),
                    json.dumps({
                        "resource": {"_rawAttributes": [["session.id", "session-1"]]},
                        "attributes": {
                            "event.name": "copilot_chat.agent.turn",
                            "turn.index": 1,
                            "gen_ai.usage.input_tokens": 25,
                            "gen_ai.usage.output_tokens": 5,
                            "tool_call_count": 0,
                        },
                    }),
                )
            ),
            encoding="utf-8",
        )

        observation = capture_copilot_otel_usage(path, captured_at="2026-08-18T16:00:00Z")

        self.assertEqual(observation["status"], "observed")
        self.assertEqual(observation["scope"], {"granularity": "session", "session": "session-1"})
        item = observation["providerData"]["usageItems"][0]
        self.assertEqual(item["quantity"], 140)
        self.assertEqual(item["inputTokens"], 125)
        self.assertEqual(item["outputTokens"], 15)
        self.assertEqual(item["nativeEvent"], "copilot_chat.agent.turn")
        self.assertEqual(len(item["turns"]), 2)

    def test_otel_missing_completed_turns_is_explicitly_unavailable(self) -> None:
        path = self.repo_root / "copilot-otel.jsonl"
        path.write_text(
            json.dumps({"attributes": {"event.name": "gen_ai.client.inference.operation.details"}}),
            encoding="utf-8",
        )

        observation = capture_copilot_otel_usage(path, captured_at="2026-08-18T16:00:00Z")

        self.assertEqual(observation["status"], "unavailable")
        self.assertEqual(observation["reason"], "otel_agent_turns_unavailable")

    def test_unavailable_observation_is_associated_without_becoming_zero(self) -> None:
        observation = {
            "status": "unavailable",
            "provider": "github-copilot",
            "capturedAt": "2026-08-18T16:00:00Z",
            "reason": "permission_denied",
            "scope": {"account": "jmrozi1", "granularity": "day"},
        }

        summary = reconcile_issue_usage(
            self.repo_root,
            "33",
            observation,
            work_period="2026-08-18",
        )

        self.assertEqual(summary["associatedObservations"], 1)
        self.assertEqual(summary["attributableTotals"], [])
        self.assertEqual(summary["observedProviders"], ["github-copilot"])
        self.assertEqual(summary["limitation"], "permission_denied")

    def test_account_day_observation_remains_unattributable(self) -> None:
        observation = {
            "status": "observed",
            "provider": "github-copilot",
            "capturedAt": "2026-08-18T16:00:00Z",
            "scope": {
                "account": "jmrozi1",
                "granularity": "day",
                "date": {"year": 2026, "month": 8, "day": 18},
            },
            "providerData": {
                "usageItems": [{"unitType": "requests", "grossQuantity": 4}],
            },
        }

        summary = reconcile_issue_usage(
            self.repo_root,
            "33",
            observation,
            work_period="2026-08-18",
        )

        self.assertEqual(summary["attributableTotals"], [])
        self.assertEqual(
            summary["observedNativeUnits"],
            [{"provider": "github-copilot", "unit": "requests"}],
        )
        self.assertEqual(
            summary["associatedScopes"],
            [
                {
                    "provider": "github-copilot",
                    "scope": {
                        "account": "jmrozi1",
                        "date": {"year": 2026, "month": 8, "day": 18},
                        "granularity": "day",
                    },
                }
            ],
        )
        self.assertEqual(
            summary["limitation"],
            "observed_usage_scope_is_not_issue_attributable",
        )

    def test_otel_observation_preserves_compact_native_usage_without_attribution(self) -> None:
        observation = {
            "status": "observed",
            "provider": "github-copilot",
            "capturedAt": "2026-08-18T16:00:00Z",
            "scope": {"session": "session-1", "granularity": "session"},
            "providerData": {
                "usageItems": [
                    {
                        "unitType": "tokens",
                        "quantity": 140,
                        "inputTokens": 125,
                        "outputTokens": 15,
                        "scope": {"session": "session-1", "granularity": "session"},
                        "nativeEvent": "copilot_chat.agent.turn",
                        "turns": [{"turn": 0, "inputTokens": 125, "outputTokens": 15}],
                    }
                ]
            },
        }

        summary = reconcile_issue_usage(self.repo_root, "33", observation, work_period="2026-08-18")

        self.assertEqual(summary["attributableTotals"], [])
        self.assertEqual(summary["observedNativeUsage"][0]["quantity"], 140)
        self.assertEqual(summary["observedNativeUsage"][0]["inputTokens"], 125)
        self.assertEqual(summary["associatedScopes"][0]["scope"]["session"], "session-1")
        self.assertEqual(summary["limitation"], "observed_usage_scope_is_not_issue_attributable")
        self.assertNotIn("observations", summary)

    def test_issue_session_observation_remains_unattributable_without_contract(self) -> None:
        observation = {
            "status": "observed",
            "provider": "github-copilot",
            "capturedAt": "2026-08-18T16:00:00Z",
            "scope": {"issue": "33", "session": "fixture", "granularity": "session"},
            "providerData": {
                "usageItems": [{"unitType": "requests", "grossQuantity": 2}],
            },
        }

        summary = reconcile_issue_usage(
            self.repo_root,
            "33",
            observation,
            work_period="period-1",
        )

        self.assertEqual(summary["attributableTotals"], [])
        self.assertEqual(
            summary["limitation"],
            "observed_usage_aggregation_semantics_unestablished",
        )

    def test_repeated_observation_is_idempotent_without_message_history(self) -> None:
        first_observation = {
            "status": "observed",
            "provider": "github-copilot",
            "capturedAt": "2026-08-18T16:00:00Z",
            "scope": {"account": "jmrozi1", "granularity": "day"},
            "providerData": {"usageItems": [{"unitType": "requests", "grossQuantity": 2}]},
        }
        second_observation = {**first_observation, "capturedAt": "2026-08-18T17:00:00Z"}

        first = reconcile_issue_usage(self.repo_root, "33", first_observation, work_period="period-1")
        repeated = reconcile_issue_usage(self.repo_root, "33", second_observation, work_period="period-2")

        self.assertEqual(repeated["attributableTotals"], [])
        self.assertEqual(repeated["associatedObservations"], 2)
        self.assertEqual(repeated["meaningfulObservationCount"], 2)
        self.assertEqual(repeated["workPeriodCount"], 2)
        self.assertEqual(repeated["lastObservedAt"], "2026-08-18T17:00:00Z")
        self.assertNotIn("observations", repeated)
        self.assertEqual(first["attributableTotals"], [])


if __name__ == "__main__":
    unittest.main()

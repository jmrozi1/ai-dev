from __future__ import annotations

import unittest

from ai_dev_flow.copilot_pricing import (
    PRICING_SOURCE,
    estimate_copilot_cost,
)


class CopilotPricingTests(unittest.TestCase):
    def _usage_item(self, *, calls: list[dict[str, object]], model: str = "gpt-5.6-luna") -> dict[str, object]:
        return {
            "unitType": "tokens",
            "scope": {"granularity": "session", "session": "session-1"},
            "models": [
                {
                    "requestModel": model,
                    "responseModel": model,
                    "callCount": len(calls),
                    "calls": calls,
                }
            ],
        }

    def test_complete_categories_produce_dollars_and_credits(self) -> None:
        result = estimate_copilot_cost(
            self._usage_item(
                calls=[
                    {
                        "inputTokens": 100,
                        "outputTokens": 40,
                        "inputSize": 100,
                        "cacheReadInputTokens": 25,
                        "cacheWriteInputTokens": 5,
                        "reasoningTokens": 12,
                    }
                ]
            )
        )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["dollars"], "0.000063750000")
        self.assertEqual(result["aiCredits"], "0.006375000000")
        self.assertEqual(result["scope"], {"granularity": "session", "session": "session-1"})
        self.assertEqual(result["pricingSource"], PRICING_SOURCE)

    def test_missing_cache_is_partial_and_all_fresh_is_only_a_scenario(self) -> None:
        result = estimate_copilot_cost(
            self._usage_item(
                calls=[
                    {
                        "inputTokens": 100,
                        "outputTokens": 40,
                        "inputSize": 100,
                        "cacheReadInputTokens": None,
                        "cacheWriteInputTokens": None,
                    }
                ]
            )
        )

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["dollars"], "0.000048000000")
        self.assertEqual(result["aiCredits"], "0.004800000000")
        scenario = result["scenarios"]["allObservedInputAsFresh"]
        self.assertEqual(scenario["label"], "all-observed-input-treated-as-fresh")
        self.assertTrue(scenario["notActualCost"])
        self.assertEqual(scenario["dollars"], "0.000068000000")

    def test_output_can_be_priced_when_input_is_unresolved(self) -> None:
        result = estimate_copilot_cost(
            self._usage_item(
                calls=[
                    {
                        "inputTokens": 100,
                        "outputTokens": 40,
                        "inputSize": 100,
                        "cacheReadInputTokens": None,
                        "cacheWriteInputTokens": None,
                    }
                ]
            )
        )

        self.assertEqual(result["models"][0]["dollars"], "0.000048000000")
        self.assertTrue(
            any("fresh/cached/cache-write input subdivisions are unknown" in reason for reason in result["unresolved"])
        )

    def test_luna_default_and_long_context_tiers_are_derived_from_input_size(self) -> None:
        result = estimate_copilot_cost(
            self._usage_item(
                calls=[
                    {
                        "inputTokens": 200000,
                        "outputTokens": 0,
                        "inputSize": 200000,
                        "cacheReadInputTokens": 0,
                        "cacheWriteInputTokens": 0,
                    },
                    {
                        "inputTokens": 200001,
                        "outputTokens": 0,
                        "inputSize": 200001,
                        "cacheReadInputTokens": 0,
                        "cacheWriteInputTokens": 0,
                    },
                ]
            )
        )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["dollars"], "0.120000400000")
        self.assertEqual(result["models"][0]["callCount"], 2)
        self.assertEqual(
            result["models"][0]["tierTotals"],
            [
                {"tier": "default", "callCount": 1, "inputTokens": 200000, "outputTokens": 0},
                {"tier": "long-context", "callCount": 1, "inputTokens": 200001, "outputTokens": 0},
            ],
        )

    def test_unknown_model_is_explicitly_unpriced(self) -> None:
        result = estimate_copilot_cost(
            self._usage_item(
                model="gpt-unknown-live-name",
                calls=[
                    {
                        "inputTokens": 100,
                        "outputTokens": 40,
                        "inputSize": 100,
                        "cacheReadInputTokens": 0,
                        "cacheWriteInputTokens": 0,
                    }
                ],
            )
        )

        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["dollars"])
        self.assertIn("unknown model: gpt-unknown-live-name", result["unresolved"])

    def test_known_output_remains_priced_alongside_unknown_model(self) -> None:
        result = estimate_copilot_cost(
            {
                "scope": {"granularity": "session"},
                "models": [
                    {
                        "requestModel": "gpt-5.3-codex",
                        "responseModel": "gpt-5.3-codex",
                        "callCount": 1,
                        "calls": [
                            {
                                "inputTokens": 100,
                                "outputTokens": 40,
                                "inputSize": 100,
                                "cacheReadInputTokens": None,
                                "cacheWriteInputTokens": None,
                            }
                        ],
                    },
                    {
                        "requestModel": "unknown",
                        "responseModel": "unknown",
                        "callCount": 1,
                        "calls": [],
                    },
                ],
            }
        )

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["dollars"], "0.000560000000")
        self.assertIn("unknown model: unknown", result["unresolved"])

    def test_unavailable_observation_shape_is_unavailable_not_zero(self) -> None:
        result = estimate_copilot_cost({"scope": {"granularity": "session"}})

        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["dollars"])
        self.assertIsNone(result["aiCredits"])

    def test_pricing_is_static_and_does_not_require_network(self) -> None:
        result = estimate_copilot_cost(self._usage_item(calls=[]))

        self.assertEqual(result["pricingSource"], PRICING_SOURCE)
        self.assertEqual(result["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

from ai_dev_flow.usage_report import render_usage_report


class UsageReportTests(unittest.TestCase):
    def _detailed_usage(self, *, models: list[dict[str, object]], input_tokens: int = 100, output_tokens: int = 40) -> dict[str, object]:
        return {
            "limitation": "observed_usage_scope_is_not_issue_attributable",
            "attributableTotals": [],
            "associatedScopes": [{"provider": "github-copilot", "scope": {"granularity": "session"}}],
            "observedNativeUsage": [
                {
                    "provider": "github-copilot",
                    "unitType": "tokens",
                    "inputTokens": input_tokens,
                    "outputTokens": output_tokens,
                    "scope": {"granularity": "session", "session": "session-1"},
                    "models": models,
                }
            ],
        }

    def test_complete_cost_rendering_is_human_first(self) -> None:
        report = render_usage_report(
            self._detailed_usage(
                models=[
                    {
                        "requestModel": "gpt-5.6-luna",
                        "calls": [{"inputTokens": 100, "outputTokens": 40, "inputSize": 100, "cacheReadInputTokens": 25, "cacheWriteInputTokens": 5}],
                    }
                ]
            )
        )

        self.assertIn("AI usage: 140 tokens observed (session scope; unattributable; 1 models)", report)
        self.assertIn("AI cost: complete - known priced subtotal 0.006375 AI credits ($0.00)", report)
        self.assertNotIn("tierTotals", report)

    def test_partial_cost_rendering_shows_subtotal_and_scenario(self) -> None:
        report = render_usage_report(
            self._detailed_usage(
                models=[
                    {
                        "requestModel": "gpt-5.6-luna",
                        "calls": [{"inputTokens": 100, "outputTokens": 40, "inputSize": 100, "cacheReadInputTokens": None, "cacheWriteInputTokens": None}],
                    }
                ]
            )
        )

        self.assertIn("AI cost: partial - known priced subtotal 0.0048 AI credits ($0.00)", report)
        self.assertIn("input cost unresolved because cache subdivisions are unavailable", report)
        self.assertIn("AI cost scenario: all-observed-input-treated-as-fresh - 0.0068 AI credits ($0.00), not actual cost", report)

    def test_unknown_model_and_unavailable_cost_are_compact(self) -> None:
        unknown = render_usage_report(self._detailed_usage(models=[{"requestModel": "unknown", "calls": []}]))
        self.assertIn("AI cost: unavailable - no defensible priced component", unknown)
        self.assertIn("AI cost note: unknown model: unknown", unknown)

        unavailable = render_usage_report({"limitation": "permission_denied", "attributableTotals": [], "associatedScopes": []})
        self.assertIn("AI usage: unavailable for issue attribution", unavailable)
        self.assertNotIn("AI cost:", unavailable)

    def test_legacy_usage_summary_remains_unchanged(self) -> None:
        report = render_usage_report(
            {
                "limitation": "observed_usage_scope_is_not_issue_attributable",
                "attributableTotals": [],
                "associatedScopes": [{"provider": "github-copilot", "scope": {"granularity": "day"}}],
            }
        )

        self.assertIn("AI usage: observed but unattributable (day scope)", report)
        self.assertNotIn("AI cost:", report)

    def test_cost_presentation_rounds_human_scale_values(self) -> None:
        report = render_usage_report(
            self._detailed_usage(
                models=[
                    {
                        "requestModel": "gpt-5.6-luna",
                        "calls": [{"inputTokens": 10000000, "outputTokens": 1000000, "inputSize": 10000000, "cacheReadInputTokens": 0, "cacheWriteInputTokens": 0}],
                    }
                ]
            )
        )

        self.assertIn("AI cost: complete - known priced subtotal 580 AI credits ($5.80)", report)
    def test_unavailable_report_is_explicit_and_has_no_variance(self) -> None:
        report = render_usage_report(
            {
                "limitation": "permission_denied",
                "attributableTotals": [],
                "associatedScopes": [],
            },
            progress="0/6 checkpoints completed",
            expectation={"provider": "github-copilot", "unit": "requests", "quantity": 3},
        )

        self.assertIn("Summary: Completed: 0/6 checkpoints completed", report)
        self.assertIn("AI usage: unavailable for issue attribution", report)
        self.assertIn("current credential lacks the required permission", report)
        self.assertNotIn("Variance:", report)

    def test_provider_limited_report_preserves_real_limitation(self) -> None:
        report = render_usage_report(
            {
                "limitation": "native_usage_telemetry_unavailable",
                "limitationDetail": "The executor has no supported native usage telemetry for this provider.",
                "attributableTotals": [],
                "associatedScopes": [{"provider": "chatgpt-orchestrator", "scope": {"granularity": "session"}}],
            }
        )

        self.assertIn("AI usage: unavailable for issue attribution", report)
        self.assertIn("no supported native usage telemetry for this provider", report)

    def test_broader_scope_is_named_without_attribution(self) -> None:
        report = render_usage_report(
            {
                "limitation": "observed_usage_scope_is_not_issue_attributable",
                "attributableTotals": [],
                "associatedScopes": [
                    {"provider": "github-copilot", "scope": {"granularity": "day"}}
                ],
            },
            progress="2/6 checkpoints completed",
        )

        self.assertIn("AI usage: observed but unattributable (day scope)", report)
        self.assertIn("broader-scope usage", report)

    def test_attributable_native_fixture_renders_without_cost_conversion(self) -> None:
        report = render_usage_report(
            {
                "limitation": None,
                "attributableTotals": [
                    {
                        "provider": "github-copilot",
                        "unit": "requests",
                        "quantity": 5,
                        "scope": {"granularity": "session", "issue": "33"},
                    }
                ],
                "associatedScopes": [],
            },
            progress="3/6 checkpoints completed",
        )

        self.assertIn("AI usage: github-copilot 5 requests", report)
        self.assertNotIn("$", report)
        self.assertNotIn("Variance:", report)

    def test_compatible_expectation_renders_native_variance(self) -> None:
        report = render_usage_report(
            {
                "limitation": None,
                "attributableTotals": [
                    {
                        "provider": "github-copilot",
                        "unit": "requests",
                        "quantity": 5,
                        "scope": {"granularity": "session", "issue": "33"},
                    }
                ],
            },
            progress="3/6 checkpoints completed",
            expectation={
                "provider": "github-copilot",
                "unit": "requests",
                "quantity": 3,
                "scope": {"granularity": "session", "issue": "33"},
            },
        )

        self.assertIn("Variance: +2 requests against expected 3 requests", report)

    def test_incompatible_expectation_omits_variance(self) -> None:
        report = render_usage_report(
            {
                "limitation": None,
                "attributableTotals": [
                    {
                        "provider": "github-copilot",
                        "unit": "requests",
                        "quantity": 5,
                        "scope": {"granularity": "session", "issue": "33"},
                    }
                ],
            },
            progress="3/6 checkpoints completed",
            expectation={
                "provider": "github-copilot",
                "unit": "tokens",
                "quantity": 300,
                "scope": {"granularity": "session", "issue": "33"},
            },
        )

        self.assertNotIn("Variance:", report)


if __name__ == "__main__":
    unittest.main()

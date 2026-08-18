from __future__ import annotations

import unittest

from ai_dev_flow.usage_report import render_usage_report


class UsageReportTests(unittest.TestCase):
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

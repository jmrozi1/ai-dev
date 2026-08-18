from __future__ import annotations


def _native_totals(summary: dict[str, object]) -> list[dict[str, object]]:
    totals = summary.get("attributableTotals")
    if not isinstance(totals, list):
        return []
    return [item for item in totals if isinstance(item, dict)]


def _usage_line(summary: dict[str, object]) -> str:
    totals = _native_totals(summary)
    if totals:
        rendered = ", ".join(
            f"{item.get('provider')} {item.get('quantity')} {item.get('unit')}"
            for item in totals
        )
        return f"AI usage: {rendered}"

    limitation = summary.get("limitation")
    scopes = summary.get("associatedScopes")
    if limitation is not None and limitation != "observed_usage_scope_is_not_issue_attributable" and limitation != "observed_usage_aggregation_semantics_unestablished":
        return "AI usage: unavailable for issue attribution"
    if isinstance(scopes, list) and scopes:
        scope = scopes[-1].get("scope") if isinstance(scopes[-1], dict) else None
        if isinstance(scope, dict):
            granularity = scope.get("granularity")
            if granularity:
                return f"AI usage: observed but unattributable ({granularity} scope)"
    return "AI usage: unavailable for issue attribution"


def _notes(summary: dict[str, object]) -> str:
    limitation = summary.get("limitation")
    limitation_detail = summary.get("limitationDetail")
    if limitation == "permission_denied":
        return (
            "Notes: GitHub Copilot usage could not be read because the current "
            "credential lacks the required permission; no usage-based process "
            "conclusion is warranted from this measurement."
        )
    if limitation == "observed_usage_scope_is_not_issue_attributable":
        return (
            "Notes: broader-scope usage is associated with this issue but cannot "
            "be attributed to it; no usage-based process conclusion is warranted "
            "from this measurement."
        )
    if limitation == "observed_usage_aggregation_semantics_unestablished":
        return (
            "Notes: issue/session usage is present but aggregation semantics are "
            "not established; no usage-based process conclusion is warranted "
            "from this measurement."
        )
    if isinstance(limitation_detail, str) and limitation_detail:
        return (
            f"Notes: {limitation_detail} No usage-based process conclusion is "
            "warranted from this measurement."
        )
    return "Notes: usage evidence alone does not warrant a process action."


def _compatible_total(
    summary: dict[str, object],
    expectation: dict[str, object],
) -> dict[str, object] | None:
    provider = expectation.get("provider")
    unit = expectation.get("unit")
    expected_scope = expectation.get("scope")
    if not isinstance(provider, str) or not isinstance(unit, str) or not isinstance(expected_scope, dict):
        return None
    for total in _native_totals(summary):
        if total.get("provider") != provider or total.get("unit") != unit:
            continue
        if total.get("scope") != expected_scope:
            continue
        quantity = total.get("quantity")
        expected_quantity = expectation.get("quantity")
        if isinstance(quantity, bool) or not isinstance(quantity, (int, float)):
            return None
        if isinstance(expected_quantity, bool) or not isinstance(expected_quantity, (int, float)):
            return None
        return {"actual": quantity, "expected": expected_quantity, "unit": unit}
    return None


def render_usage_report(
    summary: dict[str, object],
    *,
    progress: str | None = None,
    expectation: dict[str, object] | None = None,
) -> str:
    lines = []
    if progress is not None:
        lines.append(f"Summary: Completed: {progress}")
    lines.append(_usage_line(summary))
    if expectation is not None:
        compatible = _compatible_total(summary, expectation)
        if compatible is not None:
            variance = compatible["actual"] - compatible["expected"]
            sign = "+" if variance > 0 else ""
            lines.append(
                f"Variance: {sign}{variance} {compatible['unit']} "
                f"against expected {compatible['expected']} {compatible['unit']}"
            )
    lines.append(_notes(summary))
    return "\n".join(lines)

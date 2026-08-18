from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from .copilot_pricing import estimate_copilot_cost


def _native_usage(summary: dict[str, object]) -> dict[str, object] | None:
    usage = summary.get("observedNativeUsage")
    if not isinstance(usage, list):
        return None
    for item in usage:
        if isinstance(item, dict) and item.get("unitType") == "tokens":
            return item
    return None


def _human_number(value: object) -> str:
    try:
        decimal_value = Decimal(str(value))
        magnitude = abs(decimal_value)
        places = Decimal("0.01") if magnitude >= 1 else Decimal("0.0001") if magnitude >= Decimal("0.01") else Decimal("0.000001") if magnitude >= Decimal("0.0001") else Decimal("0.00000001")
        rendered = format(decimal_value.quantize(places, rounding=ROUND_HALF_UP), "f").rstrip("0").rstrip(".")
    except (ArithmeticError, ValueError):
        return str(value)
    return rendered or "0"


def _human_dollars(value: object) -> str:
    try:
        decimal_value = Decimal(str(value))
        places = Decimal("0.01")
        rendered = format(decimal_value.quantize(places, rounding=ROUND_HALF_UP), ".2f")
    except (ArithmeticError, ValueError):
        return str(value)
    return rendered or "0"


def _native_scope(summary: dict[str, object], usage: dict[str, object]) -> str:
    scope = usage.get("scope")
    if not isinstance(scope, dict):
        scopes = summary.get("associatedScopes")
        if isinstance(scopes, list) and scopes and isinstance(scopes[-1], dict):
            scope = scopes[-1].get("scope")
    if isinstance(scope, dict) and isinstance(scope.get("granularity"), str):
        return scope["granularity"]
    return "unknown"


def _native_usage_line(summary: dict[str, object], usage: dict[str, object]) -> str:
    input_tokens = usage.get("inputTokens")
    output_tokens = usage.get("outputTokens")
    if isinstance(input_tokens, int) and isinstance(output_tokens, int):
        quantity = input_tokens + output_tokens
        scope = _native_scope(summary, usage)
        attribution = "unattributable" if summary.get("attributableTotals") == [] else "attributed"
        model_count = len(usage.get("models", ())) if isinstance(usage.get("models"), list) else 0
        model_suffix = f"; {model_count} models" if model_count else ""
        return f"AI usage: {quantity:,} tokens observed ({scope} scope; {attribution}{model_suffix})"
    return "AI usage: native tokens observed"


def _cost_reason(reason: str) -> str:
    if "fresh/cached/cache-write input subdivisions are unknown" in reason:
        return "input cost unresolved because cache subdivisions are unavailable"
    if reason.startswith("unknown model:"):
        return reason
    return reason


def _cost_lines(summary: dict[str, object], usage: dict[str, object]) -> list[str]:
    cost = estimate_copilot_cost(usage)
    status = cost.get("status")
    lines: list[str] = []
    dollars = cost.get("dollars")
    credits = cost.get("aiCredits")
    if status in {"complete", "partial"} and dollars is not None and credits is not None:
        lines.append(
            f"AI cost: {status} - known priced subtotal {_human_number(credits)} AI credits "
            f"(${_human_dollars(dollars)})"
        )
    else:
        lines.append("AI cost: unavailable - no defensible priced component")

    reasons = []
    for reason in cost.get("unresolved", ()):
        rendered = _cost_reason(str(reason))
        if rendered not in reasons:
            reasons.append(rendered)
    for reason in reasons[:2]:
        lines.append(f"AI cost note: {reason}")

    scenarios = cost.get("scenarios")
    if isinstance(scenarios, dict):
        scenario = scenarios.get("allObservedInputAsFresh")
        if isinstance(scenario, dict):
            lines.append(
                "AI cost scenario: all-observed-input-treated-as-fresh - "
                f"{_human_number(scenario.get('aiCredits'))} AI credits "
                f"(${_human_dollars(scenario.get('dollars'))}), not actual cost"
            )
    return lines

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
    native_usage = _native_usage(summary)
    lines.append(_native_usage_line(summary, native_usage) if native_usage is not None else _usage_line(summary))
    if native_usage is not None and isinstance(native_usage.get("models"), list):
        model_names = [
            str(model.get("requestModel"))
            for model in native_usage["models"]
            if isinstance(model, dict) and model.get("requestModel")
        ]
        if model_names:
            lines.append(f"AI models: {', '.join(model_names)}")
        lines.extend(_cost_lines(summary, native_usage))
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

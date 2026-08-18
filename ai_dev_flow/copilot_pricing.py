from __future__ import annotations

from decimal import Decimal
from typing import Any


PRICING_SOURCE = "https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing"
PRICING_VERIFIED_CONTEXT = "Official GitHub Copilot model pricing verified 2026-08-18"
AI_CREDIT_USD = Decimal("0.01")
_RATE_SCALE = Decimal("1000000")


# Static provider data keeps review execution deterministic and auditable.
COPILOT_PRICING_CATALOG: dict[str, dict[str, Any]] = {
    "GPT-5.6 Luna": {
        "aliases": ("gpt-5.6-luna",),
        "currency": "USD",
        "tiers": (
            {
                "name": "default",
                "threshold": {"operator": "<=", "inputTokens": 200000},
                "freshInputPerMillion": "0.20",
                "cachedInputPerMillion": "0.02",
                "cacheWritePerMillion": "0.25",
                "outputPerMillion": "1.20",
            },
            {
                "name": "long-context",
                "threshold": {"operator": ">", "inputTokens": 200000},
                "freshInputPerMillion": "0.40",
                "cachedInputPerMillion": "0.04",
                "cacheWritePerMillion": "0.50",
                "outputPerMillion": "1.80",
            },
        ),
    },
    "GPT-5.3-Codex": {
        "aliases": ("gpt-5.3-codex",),
        "currency": "USD",
        "tiers": (
            {
                "name": "default",
                "threshold": None,
                "freshInputPerMillion": "1.75",
                "cachedInputPerMillion": "0.175",
                "cacheWritePerMillion": None,
                "outputPerMillion": "14.00",
            },
        ),
    },
}


def _catalog_entry(model: object) -> tuple[str, dict[str, Any]] | None:
    if not isinstance(model, str):
        return None
    normalized = model.strip().casefold()
    for canonical, entry in COPILOT_PRICING_CATALOG.items():
        if normalized == canonical.casefold() or normalized in {alias.casefold() for alias in entry["aliases"]}:
            return canonical, entry
    return None


def _tier_for_call(entry: dict[str, Any], input_size: object) -> dict[str, Any] | None:
    if isinstance(input_size, bool) or not isinstance(input_size, int) or input_size < 0:
        return None
    for tier in entry["tiers"]:
        threshold = tier["threshold"]
        if threshold is None:
            return tier
        if threshold["operator"] == "<=" and input_size <= threshold["inputTokens"]:
            return tier
        if threshold["operator"] == ">" and input_size > threshold["inputTokens"]:
            return tier
    return None


def _money(value: Decimal) -> str:
    return format(value, ".12f")


def _credits(dollars: Decimal) -> str:
    return _money(dollars / AI_CREDIT_USD)


def _rate(tier: dict[str, Any], name: str) -> Decimal | None:
    value = tier.get(name)
    return Decimal(value) if isinstance(value, str) else None


def _estimate_call(
    call: dict[str, Any],
    *,
    entry: dict[str, Any],
    scenario_all_fresh: bool = False,
) -> tuple[Decimal, list[str]]:
    input_size = call.get("inputSize", call.get("inputTokens"))
    tier = _tier_for_call(entry, input_size)
    if tier is None:
        return Decimal("0"), ["input size is missing or does not match a published pricing tier"]

    input_tokens = call.get("inputTokens")
    output_tokens = call.get("outputTokens")
    if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        return Decimal("0"), ["input or output token total is missing"]

    fresh_tokens: int | None
    cached_tokens = call.get("cacheReadInputTokens")
    cache_write_tokens = call.get("cacheWriteInputTokens")
    if scenario_all_fresh:
        fresh_tokens = input_tokens
        cached_tokens = 0
        cache_write_tokens = 0
    elif isinstance(cached_tokens, int) and isinstance(cache_write_tokens, int):
        fresh_tokens = input_tokens - cached_tokens - cache_write_tokens
        if fresh_tokens < 0:
            return Decimal("0"), ["input token categories exceed total input tokens"]
    else:
        fresh_tokens = None

    dollars = Decimal("0")
    reasons: list[str] = []
    if fresh_tokens is None:
        reasons.append("fresh/cached/cache-write input subdivisions are unknown")
    else:
        dollars += Decimal(fresh_tokens) / _RATE_SCALE * (_rate(tier, "freshInputPerMillion") or Decimal("0"))
        if cached_tokens:
            dollars += Decimal(cached_tokens) / _RATE_SCALE * (_rate(tier, "cachedInputPerMillion") or Decimal("0"))
        if cache_write_tokens:
            cache_write_rate = _rate(tier, "cacheWritePerMillion")
            if cache_write_rate is None:
                reasons.append("cache-write tokens are not priced for this model")
            else:
                dollars += Decimal(cache_write_tokens) / _RATE_SCALE * cache_write_rate

    output_rate = _rate(tier, "outputPerMillion")
    if output_rate is None:
        reasons.append("output pricing is unavailable")
    else:
        dollars += Decimal(output_tokens) / _RATE_SCALE * output_rate
    return dollars, reasons


def estimate_copilot_cost(usage_item: dict[str, Any]) -> dict[str, Any]:
    """Estimate cost from detailed native usage without treating unknown input as zero."""
    models = usage_item.get("models")
    scope = usage_item.get("scope")
    result: dict[str, Any] = {
        "status": "unavailable",
        "scope": scope,
        "currency": "USD",
        "aiCreditUsd": _money(AI_CREDIT_USD),
        "pricingSource": PRICING_SOURCE,
        "pricingContext": PRICING_VERIFIED_CONTEXT,
        "dollars": None,
        "aiCredits": None,
        "pricedComponents": [],
        "unresolved": [],
        "models": [],
        "scenarios": {},
    }
    if not isinstance(models, list) or not models:
        result["unresolved"] = ["detailed model usage is unavailable"]
        return result

    actual_dollars = Decimal("0")
    scenario_dollars = Decimal("0")
    actual_priced = False
    actual_complete = True
    for model in models:
        if not isinstance(model, dict):
            actual_complete = False
            result["unresolved"].append("invalid model usage entry")
            continue
        lookup = _catalog_entry(model.get("requestModel"))
        model_result: dict[str, Any] = {
            "requestModel": model.get("requestModel"),
            "responseModel": model.get("responseModel"),
            "callCount": model.get("callCount"),
            "tierTotals": [],
        }
        if lookup is None:
            actual_complete = False
            result["unresolved"].append(f"unknown model: {model.get('requestModel')}")
            result["models"].append(model_result)
            continue
        canonical, entry = lookup
        model_result["canonicalModel"] = canonical
        calls = model.get("calls")
        if not isinstance(calls, list) or not calls:
            actual_complete = False
            result["unresolved"].append(f"no per-call details for {canonical}")
            result["models"].append(model_result)
            continue
        model_dollars = Decimal("0")
        scenario_model_dollars = Decimal("0")
        tier_totals: dict[str, dict[str, int]] = {}
        for call in calls:
            if not isinstance(call, dict):
                actual_complete = False
                result["unresolved"].append(f"invalid call detail for {canonical}")
                continue
            call_dollars, reasons = _estimate_call(call, entry=entry)
            scenario_call_dollars, _ = _estimate_call(call, entry=entry, scenario_all_fresh=True)
            tier = _tier_for_call(entry, call.get("inputSize", call.get("inputTokens")))
            if tier is not None:
                tier_total = tier_totals.setdefault(tier["name"], {"callCount": 0, "inputTokens": 0, "outputTokens": 0})
                tier_total["callCount"] += 1
                tier_total["inputTokens"] += int(call.get("inputTokens", 0))
                tier_total["outputTokens"] += int(call.get("outputTokens", 0))
            model_dollars += call_dollars
            scenario_model_dollars += scenario_call_dollars
            if reasons:
                actual_complete = False
                result["unresolved"].extend(f"{canonical}: {reason}" for reason in reasons)
            if call_dollars:
                actual_priced = True
        actual_dollars += model_dollars
        scenario_dollars += scenario_model_dollars
        model_result["dollars"] = _money(model_dollars)
        model_result["aiCredits"] = _credits(model_dollars)
        model_result["tierTotals"] = [
            {"tier": tier_name, **totals}
            for tier_name, totals in sorted(tier_totals.items())
        ]
        result["models"].append(model_result)

    if actual_priced:
        result["dollars"] = _money(actual_dollars)
        result["aiCredits"] = _credits(actual_dollars)
        result["status"] = "complete" if actual_complete else "partial"
    if scenario_dollars:
        result["scenarios"]["allObservedInputAsFresh"] = {
            "status": "scenario",
            "label": "all-observed-input-treated-as-fresh",
            "dollars": _money(scenario_dollars),
            "aiCredits": _credits(scenario_dollars),
            "notActualCost": True,
        }
    if not actual_priced and result["unresolved"]:
        result["status"] = "partial" if scenario_dollars else "unavailable"
    return result

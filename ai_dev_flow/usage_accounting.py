from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from .json_files import JsonFileError, load_json_object, write_json_object_atomic


USAGE_DIRECTORY = ".ai-dev/usage"


class UsageAccountingError(Exception):
    """Raised for invalid or unavailable issue usage accounting state."""


def usage_summary_path(repo_root: Path, issue_id: str) -> Path:
    normalized_issue_id = issue_id.strip()
    if not normalized_issue_id:
        raise UsageAccountingError("Issue id cannot be empty.")
    return repo_root / USAGE_DIRECTORY / f"issue-{normalized_issue_id}.json"


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _observation_fingerprint(observation: dict[str, object]) -> str:
    encoded = json.dumps(observation, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _new_summary(issue_id: str) -> dict[str, object]:
    return {
        "version": 1,
        "issue": {"id": issue_id},
        "observedProviders": [],
        "observedNativeUnits": [],
        "observedNativeUsage": [],
        "associatedScopes": [],
        "attributableTotals": [],
        "associatedObservations": 0,
        "meaningfulObservationCount": 0,
        "workPeriodCount": 0,
        "lastObservedAt": None,
        "lastWorkPeriod": None,
        "limitation": None,
        "limitationDetail": None,
        "lastObservationFingerprint": None,
    }


def _load_summary(path: Path, issue_id: str) -> dict[str, object]:
    try:
        summary = load_json_object(path, missing_default=_new_summary(issue_id))
    except JsonFileError as exc:
        raise UsageAccountingError(str(exc)) from exc
    if not summary:
        return _new_summary(issue_id)
    if summary.get("version") != 1 or summary.get("issue") != {"id": issue_id}:
        raise UsageAccountingError(f"Invalid usage summary for issue {issue_id}.")
    return summary


def _observed_units(observation: dict[str, object]) -> list[dict[str, str]]:
    provider = observation.get("provider")
    provider_data = observation.get("providerData")
    if not isinstance(provider, str) or not isinstance(provider_data, dict):
        return []
    usage_items = provider_data.get("usageItems")
    if not isinstance(usage_items, list):
        return []
    units = {
        item.get("unitType").strip()
        for item in usage_items
        if isinstance(item, dict)
        and isinstance(item.get("unitType"), str)
        and item.get("unitType").strip()
    }
    return [{"provider": provider, "unit": unit} for unit in sorted(units)]


def _associated_scope(observation: dict[str, object]) -> dict[str, object] | None:
    provider = observation.get("provider")
    scope = observation.get("scope")
    if not isinstance(provider, str) or not isinstance(scope, dict):
        return None
    retained = {
        key: scope[key]
        for key in ("account", "date", "granularity", "repository", "issue", "session")
        if key in scope
    }
    if not retained:
        return None
    return {"provider": provider, "scope": retained}


def _observed_native_usage(observation: dict[str, object]) -> list[dict[str, object]]:
    provider = observation.get("provider")
    provider_data = observation.get("providerData")
    if not isinstance(provider, str) or not isinstance(provider_data, dict):
        return []
    usage_items = provider_data.get("usageItems")
    if not isinstance(usage_items, list):
        return []
    retained = []
    for item in usage_items:
        if not isinstance(item, dict):
            continue
        usage = {
            key: item[key]
            for key in ("unitType", "quantity", "inputTokens", "outputTokens", "scope", "nativeEvent", "turns")
            if key in item
        }
        if usage:
            retained.append({"provider": provider, **usage})
    return retained


def _limitation(observation: dict[str, object], issue_id: str) -> str | None:
    if observation.get("status") == "unavailable":
        return str(observation.get("reason") or "usage_unavailable")
    scope = observation.get("scope")
    if not isinstance(scope, dict) or scope.get("issue") != issue_id:
        return "observed_usage_scope_is_not_issue_attributable"
    return "observed_usage_aggregation_semantics_unestablished"


def reconcile_issue_usage(
    repo_root: Path,
    issue_id: str,
    observation: dict[str, object],
    *,
    work_period: str,
    observed_at: str | None = None,
) -> dict[str, object]:
    path = usage_summary_path(repo_root, issue_id)
    summary = _load_summary(path, issue_id)
    fingerprint = _observation_fingerprint(observation)
    if summary.get("lastObservationFingerprint") == fingerprint:
        return summary

    observed_timestamp = observed_at or str(observation.get("capturedAt") or _now_utc())
    summary["attributableTotals"] = []
    summary["observedNativeUsage"] = _observed_native_usage(observation)
    provider = observation.get("provider")
    if isinstance(provider, str):
        providers = summary.get("observedProviders")
        if not isinstance(providers, list):
            providers = []
        summary["observedProviders"] = sorted({*providers, provider})

    native_units = summary.get("observedNativeUnits")
    if not isinstance(native_units, list):
        native_units = []
    existing_units = {
        (item.get("provider"), item.get("unit"))
        for item in native_units
        if isinstance(item, dict)
    }
    existing_units.update(
        (item["provider"], item["unit"])
        for item in _observed_units(observation)
    )
    summary["observedNativeUnits"] = [
        {"provider": provider_name, "unit": unit}
        for provider_name, unit in sorted(existing_units)
    ]

    associated_scope = _associated_scope(observation)
    associated_scopes = summary.get("associatedScopes")
    if not isinstance(associated_scopes, list):
        associated_scopes = []
    if associated_scope is not None and associated_scope not in associated_scopes:
        associated_scopes.append(associated_scope)
    summary["associatedScopes"] = associated_scopes
    summary["associatedObservations"] = int(summary.get("associatedObservations") or 0) + 1
    summary["meaningfulObservationCount"] = int(summary.get("meaningfulObservationCount") or 0) + 1
    if summary.get("lastWorkPeriod") != work_period:
        summary["workPeriodCount"] = int(summary.get("workPeriodCount") or 0) + 1
    summary["lastObservedAt"] = observed_timestamp
    summary["lastWorkPeriod"] = work_period
    summary["limitation"] = _limitation(observation, issue_id)
    if summary["limitation"] is not None:
        summary["limitationDetail"] = str(observation.get("detail") or summary["limitation"])
    else:
        summary["limitationDetail"] = None
    summary["lastObservationFingerprint"] = fingerprint

    try:
        write_json_object_atomic(path, summary)
    except JsonFileError as exc:
        raise UsageAccountingError(str(exc)) from exc
    return summary

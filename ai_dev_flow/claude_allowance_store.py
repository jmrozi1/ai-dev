"""Durable local workload and calibration evidence for the allowance estimator."""

from __future__ import annotations

# The estimator is pure: it accepts a cumulative workload score and a list of human
# `/usage` readings and does arithmetic. Something has to produce those two inputs
# across process restarts without inventing evidence, and that is this module.
#
# Three things make it honest.
#
# First, the workload score is accumulated from completed runtime results, one
# result at a time, through exactly one conversion. The Agent SDK reports
# `total_cost_usd` as a float, and a binary float is not the number the provider
# sent; `Decimal(str(value))` recovers the decimal the caller actually saw, while
# `Decimal(value)` would bake in binary noise that then differs between callers.
# The score is a workload weight, not money and not an allowance percentage.
#
# Second, a result whose cost is missing is a hole in the evidence, not free work.
# It is recorded as a gap, and every calibration span containing one is marked
# incompletely covered, which stops it training a rate. Absence never becomes zero.
#
# Third, everything is append-only and predecessor-relative. A calibration
# reading's coverage describes the span since the immediately preceding recorded
# reading, so inserting a reading into the middle of the history later would
# silently redefine a flag that was already written. Backfill is refused rather
# than reordered, and a profile is always built from the complete recorded history
# for one window and meter -- never from a caller-chosen subset, which could
# change what "preceding" means.

from decimal import Decimal, DecimalException, localcontext
import json
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple

from .claude_allowance import (
    _CALCULATION,
    CalibrationPoint,
    CalibrationProfile,
    WINDOWS,
    build_profile,
)
from .claude_runtime import RuntimeResult
from .json_files import JsonFileError, write_json_object_atomic

__all__ = [
    "AllowanceStoreError",
    "AllowanceStore",
    "CURRENT_METER",
    "SCHEMA_VERSION",
    "allowance_store_path",
]

SCHEMA_VERSION = 1

# The meter names the source and the accumulation semantics, and carries its own
# version so a later change of either starts a new meter instead of silently
# redefining what one unit means. It is not an account, session, model, or money
# identity.
CURRENT_METER = "claude-agent-sdk-result-total-cost-usd-v1"

ALLOWANCE_DIRECTORY = ".ai-dev/allowance"
ALLOWANCE_STORE_NAME = "workload.json"

# Reasons. Every refusal names exactly one.
REASON_INVALID_RESULT = "invalid-runtime-result"
REASON_INVALID_COST = "invalid-result-cost"
REASON_INVALID_KEY = "invalid-idempotency-key"
REASON_KEY_CONFLICT = "duplicate-key-conflict"
REASON_INVALID_COVERAGE = "invalid-coverage-flag"
REASON_OBSERVATION_OUT_OF_ORDER = "observation-out-of-order"
REASON_MALFORMED_STORE = "malformed-store"
REASON_UNREADABLE_STORE = "unreadable-store"
REASON_STORE_WRITE_FAILED = "store-write-failed"
REASON_METER_MISMATCH = "meter-mismatch"
REASON_WORKLOAD_OVERFLOW = "workload-overflow"
REASON_INVALID_WINDOW = "invalid-window"

_STORE_KEYS = ("version", "meter", "workloadUnits", "results", "observations")
_RESULT_KEYS = ("key", "ordinal", "cost")
_OBSERVATION_KEYS = (
    "window", "observedAt", "resetsAt", "usedPercentage",
    "workloadUnits", "ledgerOrdinal", "humanCoverage", "completeCoverage",
)

# An opaque token: enough shape to be a stable key, not enough to carry a
# sentence, a path, or a credential.
_KEY_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


class AllowanceStoreError(Exception):
    """A refusal to accept or trust evidence, carrying the exact reason."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__("{0}: {1}".format(reason, detail))
        self.reason = reason
        self.detail = detail


def allowance_store_path(repo_root: Path) -> Path:
    return Path(repo_root) / ALLOWANCE_DIRECTORY / ALLOWANCE_STORE_NAME


# --------------------------------------------------------------------------
# Exact values
# --------------------------------------------------------------------------


def result_workload(value: object) -> Optional[Decimal]:
    """The one conversion boundary between a runtime float and a workload unit.

    `Decimal(str(value))` and not `Decimal(value)`: the second expands the binary
    float exactly, which is not the decimal anybody reported and differs from what
    a caller spelling the same number as a string would get.
    """
    if value is None:
        return None
    if type(value) is bool:
        raise AllowanceStoreError(
            REASON_INVALID_COST, "total_cost_usd must be a number, got {0!r}".format(value)
        )
    if not isinstance(value, (int, float)):
        raise AllowanceStoreError(
            REASON_INVALID_COST, "total_cost_usd must be a number, got {0!r}".format(value)
        )
    converted = Decimal(str(value))
    if not converted.is_finite():
        raise AllowanceStoreError(
            REASON_INVALID_COST, "total_cost_usd must be finite, got {0!r}".format(value)
        )
    if converted < 0:
        raise AllowanceStoreError(
            REASON_INVALID_COST, "total_cost_usd may not be negative, got {0!r}".format(value)
        )
    return converted


def _exact_key(value: object) -> str:
    if type(value) is not str or not _KEY_PATTERN.match(value):
        raise AllowanceStoreError(
            REASON_INVALID_KEY,
            "the idempotency key must be an opaque token of 1-128 characters "
            "from [A-Za-z0-9._:-] starting alphanumeric, got {0!r}".format(value),
        )
    return value


def _exact_bool(value: object, *, label: str) -> bool:
    if type(value) is not bool:
        raise AllowanceStoreError(
            REASON_INVALID_COVERAGE, "{0} must be a bool, got {1!r}".format(label, value)
        )
    return value


def _sum_costs(costs: List[Optional[Decimal]]) -> Decimal:
    try:
        with localcontext(_CALCULATION):
            total = Decimal(0)
            for cost in costs:
                if cost is not None:
                    total = total + cost
            return total
    except DecimalException as exc:
        raise AllowanceStoreError(
            REASON_WORKLOAD_OVERFLOW, "cumulative workload is not representable: {0}".format(exc)
        ) from exc


def _persisted_decimal(value: object, *, label: str, reason: str) -> Decimal:
    if type(value) is not str:
        raise AllowanceStoreError(
            reason, "{0} must be a canonical decimal string, got {1!r}".format(label, value)
        )
    try:
        converted = Decimal(value)
    except Exception as exc:
        raise AllowanceStoreError(
            reason, "{0} is not a decimal: {1!r}".format(label, value)
        ) from exc
    if not converted.is_finite():
        raise AllowanceStoreError(reason, "{0} must be finite, got {1!r}".format(label, value))
    return converted


def _persisted_int(value: object, *, label: str) -> int:
    if type(value) is not int:
        raise AllowanceStoreError(
            REASON_MALFORMED_STORE, "{0} must be an exact int, got {1!r}".format(label, value)
        )
    return value


def _persisted_bool(value: object, *, label: str) -> bool:
    if type(value) is not bool:
        raise AllowanceStoreError(
            REASON_MALFORMED_STORE, "{0} must be a bool, got {1!r}".format(label, value)
        )
    return value


def _require_exact_keys(payload: object, expected: Tuple[str, ...], *, label: str) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise AllowanceStoreError(
            REASON_MALFORMED_STORE, "{0} must be a JSON object, got {1!r}".format(label, payload)
        )
    unknown = sorted(set(payload) - set(expected))
    missing = sorted(set(expected) - set(payload))
    if unknown or missing:
        raise AllowanceStoreError(
            REASON_MALFORMED_STORE,
            "{0} has unknown key(s) {1} and missing key(s) {2}".format(label, unknown, missing),
        )
    return payload


# --------------------------------------------------------------------------
# The store
# --------------------------------------------------------------------------


class AllowanceStore:
    """Append-only local evidence: a result ledger and a calibration history.

    Everything durable lives in one small JSON object replaced atomically. The
    file is the whole state; nothing is cached across calls, so a second process
    reading the same path sees the same evidence and a retry after a restart is
    checked against what was actually written.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    # -- reading ----------------------------------------------------------

    def _empty(self) -> Dict[str, Any]:
        return {"meter": CURRENT_METER, "workload_units": Decimal(0), "results": [],
                "observations": []}

    def _read_payload(self) -> Optional[Dict[str, Any]]:
        if not self.path.exists():
            # An absent store is a store that has recorded nothing. Every other
            # unreadable shape is a refusal, never an empty ledger.
            return None
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise AllowanceStoreError(
                REASON_UNREADABLE_STORE, "cannot read {0}: {1}".format(self.path, exc)
            ) from exc
        if not text.strip():
            raise AllowanceStoreError(
                REASON_MALFORMED_STORE, "{0} is empty".format(self.path)
            )
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise AllowanceStoreError(
                REASON_MALFORMED_STORE, "{0} is invalid JSON: {1}".format(self.path, exc.msg)
            ) from exc

    def _load(self) -> Dict[str, Any]:
        payload = self._read_payload()
        if payload is None:
            return self._empty()
        _require_exact_keys(payload, _STORE_KEYS, label=str(self.path))
        if payload["version"] != SCHEMA_VERSION:
            raise AllowanceStoreError(
                REASON_MALFORMED_STORE,
                "{0} declares version {1!r}, not {2}".format(
                    self.path, payload["version"], SCHEMA_VERSION
                ),
            )
        if payload["meter"] != CURRENT_METER:
            # A different meter means the unit itself means something else. It is
            # not this store's history and must not be mixed into it.
            raise AllowanceStoreError(
                REASON_METER_MISMATCH,
                "{0} was written for meter {1!r}, not {2!r}".format(
                    self.path, payload["meter"], CURRENT_METER
                ),
            )

        results = self._load_results(payload["results"])
        recorded = _persisted_decimal(
            payload["workloadUnits"], label="workloadUnits", reason=REASON_MALFORMED_STORE
        )
        computed = _sum_costs([cost for _, _, cost in results])
        if recorded != computed:
            # The cumulative total and the ledger are two statements of the same
            # fact. Disagreement means one of them was edited, and nothing here
            # can tell which, so neither is trusted.
            raise AllowanceStoreError(
                REASON_MALFORMED_STORE,
                "{0} records workload {1} but its ledger sums to {2}".format(
                    self.path, recorded, computed
                ),
            )
        observations = self._load_observations(payload["observations"], results)
        return {"meter": CURRENT_METER, "workload_units": computed, "results": results,
                "observations": observations}

    def _load_results(self, raw: object) -> List[Tuple[str, int, Optional[Decimal]]]:
        if not isinstance(raw, list):
            raise AllowanceStoreError(
                REASON_MALFORMED_STORE, "{0} results must be a list".format(self.path)
            )
        loaded = []
        seen = set()
        for index, entry in enumerate(raw):
            label = "{0} results[{1}]".format(self.path, index)
            _require_exact_keys(entry, _RESULT_KEYS, label=label)
            try:
                key = _exact_key(entry["key"])
            except AllowanceStoreError as exc:
                raise AllowanceStoreError(
                    REASON_MALFORMED_STORE, "{0} has an invalid key: {1}".format(label, exc.detail)
                ) from exc
            if key in seen:
                raise AllowanceStoreError(
                    REASON_MALFORMED_STORE, "{0} repeats key {1!r}".format(label, key)
                )
            seen.add(key)
            ordinal = _persisted_int(entry["ordinal"], label=label + " ordinal")
            if ordinal != index + 1:
                # Ordinals are the append-only order evidence. A gap or a repeat
                # means an event was removed, reordered, or added twice.
                raise AllowanceStoreError(
                    REASON_MALFORMED_STORE,
                    "{0} has ordinal {1}, expected {2}".format(label, ordinal, index + 1),
                )
            cost = entry["cost"]
            if cost is not None:
                cost = _persisted_decimal(
                    cost, label=label + " cost", reason=REASON_MALFORMED_STORE
                )
                if cost < 0:
                    raise AllowanceStoreError(
                        REASON_MALFORMED_STORE, "{0} cost may not be negative".format(label)
                    )
            loaded.append((key, ordinal, cost))
        return loaded

    def _load_observations(
        self, raw: object, results: List[Tuple[str, int, Optional[Decimal]]]
    ) -> List[Dict[str, Any]]:
        if not isinstance(raw, list):
            raise AllowanceStoreError(
                REASON_MALFORMED_STORE, "{0} observations must be a list".format(self.path)
            )
        loaded = []
        previous_ordinal = 0
        previous_observed = None
        for index, entry in enumerate(raw):
            label = "{0} observations[{1}]".format(self.path, index)
            _require_exact_keys(entry, _OBSERVATION_KEYS, label=label)
            ledger_ordinal = _persisted_int(entry["ledgerOrdinal"], label=label + " ledgerOrdinal")
            if not previous_ordinal <= ledger_ordinal <= len(results):
                raise AllowanceStoreError(
                    REASON_MALFORMED_STORE,
                    "{0} names ledger ordinal {1} outside ({2}, {3}]".format(
                        label, ledger_ordinal, previous_ordinal, len(results)
                    ),
                )
            human = _persisted_bool(entry["humanCoverage"], label=label + " humanCoverage")
            complete = _persisted_bool(entry["completeCoverage"], label=label + " completeCoverage")
            expected = human and _span_is_clean(results, previous_ordinal, ledger_ordinal)
            if complete != expected:
                # completeCoverage is derived, so a stored value that disagrees
                # with the ledger it was derived from is edited state.
                raise AllowanceStoreError(
                    REASON_MALFORMED_STORE,
                    "{0} stores completeCoverage {1} but its span implies {2}".format(
                        label, complete, expected
                    ),
                )
            try:
                point = CalibrationPoint(
                    window=entry["window"],
                    observed_at=entry["observedAt"],
                    resets_at=entry["resetsAt"],
                    used_percentage=_persisted_decimal(
                        entry["usedPercentage"], label=label + " usedPercentage",
                        reason=REASON_MALFORMED_STORE,
                    ),
                    workload_units=_persisted_decimal(
                        entry["workloadUnits"], label=label + " workloadUnits",
                        reason=REASON_MALFORMED_STORE,
                    ),
                    meter=CURRENT_METER,
                    complete_coverage=complete,
                )
            except Exception as exc:
                raise AllowanceStoreError(
                    REASON_MALFORMED_STORE, "{0} is not a valid reading: {1}".format(label, exc)
                ) from exc
            if previous_observed is not None and point.observed_at <= previous_observed:
                raise AllowanceStoreError(
                    REASON_MALFORMED_STORE,
                    "{0} observed at {1} does not follow {2}".format(
                        label, point.observed_at, previous_observed
                    ),
                )
            previous_observed = point.observed_at
            previous_ordinal = ledger_ordinal
            loaded.append({"point": point, "ledger_ordinal": ledger_ordinal, "human": human})
        return loaded

    # -- writing ----------------------------------------------------------

    def _save(self, state: Dict[str, Any]) -> None:
        """One fixed key order, decimals as canonical strings, atomic replace."""
        payload = {
            "version": SCHEMA_VERSION,
            "meter": CURRENT_METER,
            "workloadUnits": str(state["workload_units"]),
            "results": [
                {"key": key, "ordinal": ordinal,
                 "cost": None if cost is None else str(cost)}
                for key, ordinal, cost in state["results"]
            ],
            "observations": [
                {
                    "window": entry["point"].window,
                    "observedAt": entry["point"].observed_at,
                    "resetsAt": entry["point"].resets_at,
                    "usedPercentage": str(entry["point"].used_percentage),
                    "workloadUnits": str(entry["point"].workload_units),
                    "ledgerOrdinal": entry["ledger_ordinal"],
                    "humanCoverage": entry["human"],
                    "completeCoverage": entry["point"].complete_coverage,
                }
                for entry in state["observations"]
            ],
        }
        try:
            write_json_object_atomic(self.path, payload)
        except JsonFileError as exc:
            raise AllowanceStoreError(
                REASON_STORE_WRITE_FAILED, "cannot write {0}: {1}".format(self.path, exc)
            ) from exc

    # -- public surface ---------------------------------------------------

    def workload_units(self) -> Decimal:
        return self._load()["workload_units"]

    def record_result(self, result: object, *, idempotency_key: str) -> Decimal:
        """Add one completed runtime result's cost exactly once.

        The key is the caller's; the ordinal is the store's. A replay therefore
        cannot obtain a second ordinal, whatever order it arrives in or how many
        times the process has restarted since.
        """
        if not isinstance(result, RuntimeResult):
            raise AllowanceStoreError(
                REASON_INVALID_RESULT,
                "a workload event is a RuntimeResult, got {0!r}".format(type(result).__name__),
            )
        key = _exact_key(idempotency_key)
        cost = result_workload(result.total_cost_usd)

        state = self._load()
        for existing_key, _, existing_cost in state["results"]:
            if existing_key != key:
                continue
            if existing_cost is None and cost is None:
                return state["workload_units"]
            if existing_cost is not None and cost is not None and existing_cost == cost:
                return state["workload_units"]
            raise AllowanceStoreError(
                REASON_KEY_CONFLICT,
                "key {0!r} already recorded cost {1} and cannot now record {2}".format(
                    key, existing_cost, cost
                ),
            )

        results = list(state["results"])
        results.append((key, len(results) + 1, cost))
        state["results"] = results
        state["workload_units"] = _sum_costs([entry[2] for entry in results])
        self._save(state)
        return state["workload_units"]

    def append_observation(
        self,
        *,
        window: str,
        observed_at: int,
        resets_at: int,
        used_percentage: object,
        human_complete_coverage: bool,
    ) -> CalibrationPoint:
        """Record one human `/usage` reading against the current ledger.

        The human supplies only what a human can see. Workload and meter come
        from the store, so a caller cannot assert a cumulative number, and the
        recorded coverage is the conjunction of what the human stated and what
        the ledger actually shows for the same span.
        """
        human = _exact_bool(human_complete_coverage, label="human_complete_coverage")
        state = self._load()
        results = state["results"]
        observations = state["observations"]
        previous_ordinal = observations[-1]["ledger_ordinal"] if observations else 0
        ledger_ordinal = len(results)
        complete = human and _span_is_clean(results, previous_ordinal, ledger_ordinal)

        point = CalibrationPoint(
            window=window,
            observed_at=observed_at,
            resets_at=resets_at,
            used_percentage=used_percentage,
            workload_units=state["workload_units"],
            meter=CURRENT_METER,
            complete_coverage=complete,
        )
        if observations and point.observed_at <= observations[-1]["point"].observed_at:
            # Coverage is predecessor-relative, so inserting a reading behind an
            # existing one would silently redefine a flag already written.
            raise AllowanceStoreError(
                REASON_OBSERVATION_OUT_OF_ORDER,
                "a reading at {0} cannot follow one at {1}".format(
                    point.observed_at, observations[-1]["point"].observed_at
                ),
            )

        state["observations"] = list(observations) + [
            {"point": point, "ledger_ordinal": ledger_ordinal, "human": human}
        ]
        self._save(state)
        return point

    def observations(self, window: str) -> Tuple[CalibrationPoint, ...]:
        """The complete recorded history for one window, in recorded order."""
        if window not in WINDOWS:
            raise AllowanceStoreError(
                REASON_INVALID_WINDOW,
                "window {0!r} is not one of {1}".format(window, ", ".join(WINDOWS)),
            )
        return tuple(
            entry["point"] for entry in self._load()["observations"]
            if entry["point"].window == window
        )

    def profile(self, window: str) -> CalibrationProfile:
        """A profile over the whole recorded history; there is no subset to pass."""
        return build_profile(self.observations(window))

    def latest_observation(self, window: str) -> Optional[CalibrationPoint]:
        recorded = self.observations(window)
        return recorded[-1] if recorded else None


def _span_is_clean(
    results: List[Tuple[str, int, Optional[Decimal]]], previous: int, current: int
) -> bool:
    """No result in `(previous, current]` was recorded without a cost.

    A missing cost is work this manager launched but cannot weigh, which is the
    same hole in the evidence as work it never saw at all.
    """
    for _, ordinal, cost in results:
        if previous < ordinal <= current and cost is None:
            return False
    return True

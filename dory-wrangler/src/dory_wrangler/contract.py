"""Bridge to the executable contract.

`dory-wrangler/validator/validate_contract.py` is the executable form of the
v0.1 contract. This store does not restate its rules in a second place, because
a second copy is exactly how a rule and its enforcement drift apart. It loads
that module and calls it.

Two things follow, and both are deliberate:

* Fail-closed reading (contract D3) is performed by the contract itself. An
  unknown record type, an unknown record version, or an unrecognized field is
  rejected by the same code that would reject it in a fixture.
* "Every store this code produces validates against the validator" is not a
  claim made by a separate checker written alongside the store. It is the same
  function, called on the same records.

The validator is loaded from source by path because `dory-wrangler` is not a
legal Python package name.
"""

import importlib.util
import os

_MODULE = None


def _validator_path():
    override = os.environ.get("DORY_WRANGLER_VALIDATOR")
    if override:
        return override
    here = os.path.dirname(os.path.abspath(__file__))
    # src/dory_wrangler -> src -> dory-wrangler
    root = os.path.dirname(os.path.dirname(here))
    return os.path.join(root, "validator", "validate_contract.py")


def validator():
    """The loaded validate_contract module."""
    global _MODULE
    if _MODULE is None:
        path = _validator_path()
        spec = importlib.util.spec_from_file_location("dory_validate_contract", path)
        if spec is None or spec.loader is None:  # pragma: no cover - defensive
            raise RuntimeError("cannot load the contract validator from %s" % path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _MODULE = module
    return _MODULE


def record_violations(record, index=0):
    """Violations the contract finds in one record, in isolation.

    Used on every write and on every read. Returns a list of (code, where,
    detail) triples; empty means the record is well formed.
    """
    v = validator()
    report = v.Report()
    v.validate_record(report, index, record)
    return report.sorted()


def store_violations(records):
    """Violations the contract finds across a whole set of records."""
    v = validator()
    report = v.Report()
    v.validate_store(report, list(records))
    return report.sorted()


def terminal_states():
    return frozenset(validator().TERMINAL_SESSION_STATES)


def authorized_transitions():
    return dict(validator().AUTHORIZED_TRANSITIONS)


def transition_precondition_violations(session, tables):
    """Violations contract 5.2's *precondition* table finds in one session.

    The companion of `authorized_transitions()`, which is 5.2's *owner* table.
    Both halves of that table are the contract's, and the store calls them
    rather than restating either: a second copy of the precondition column is
    how the rule and its enforcement drift apart, and this store already carries
    one duplicated contract constant as a reported finding.

    `tables` is the same mapping the validator builds for itself -- `messages`,
    `events`, `requests`, `results_by_request`, `observations`, each id -> record
    -- so the store supplies the records and the contract supplies the rule.

    Returns (code, where, detail) triples; empty means every transition on the
    session satisfies its precondition.
    """
    v = validator()
    checker = getattr(v, "_validate_preconditions", None)
    if checker is None:  # pragma: no cover - the contract changed shape
        raise RuntimeError(
            "the contract validator no longer exposes its precondition check; "
            "the store must not fall back to a restatement of section 5.2"
        )
    report = v.Report()
    checker(report, "session", session, dict(tables))
    return report.sorted()


def stream_end_type():
    """The interpreted_type a launcher-sourced end-of-stream event carries."""
    return validator().STREAM_END_TYPE


def evidence_table_names():
    """The record tables the precondition check resolves evidence against."""
    return ("messages", "events", "requests", "results_by_request", "observations")


def contract_version():
    return validator().CONTRACT_VERSION


RECORD_VERSION = 1

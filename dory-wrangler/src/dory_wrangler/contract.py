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


def contract_version():
    return validator().CONTRACT_VERSION


RECORD_VERSION = 1

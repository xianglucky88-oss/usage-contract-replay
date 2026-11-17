"""Runnable QA suite.

``python3 -m qa.checks --suite`` runs every layer and exits non-zero on the
first failure class.  Individual layers can be selected with flags, which is
what the mutation harness and CI wrappers use.

The suite reports a minimal counterexample for each failing property via
:mod:`qa.reduce`, so a failure is always actionable.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Optional

from usage_contract_replay.pipeline import run

from . import faults, generators, invariants, metamorphic, oracle, reduce


def _describe(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _check_valid_case(payload: Dict[str, Any]) -> Dict[str, Any]:
    result = run(payload)
    invariants.check_all(result, payload)
    metamorphic.apply(run, payload, seed=hash(_describe(payload)) & 0xFFFF)
    metamorphic.split_totals_preserved(run, payload, seed=7)
    return result


def _check_invalid_case(payload: Dict[str, Any]) -> Optional[str]:
    expected = oracle.codes_for(payload)
    result = run(payload)
    if expected is None:
        return None
    if result.get("status") != "error":
        return f"expected error {expected!r}, got status {result.get('status')!r}"
    got = result.get("error", {}).get("code")
    if got != expected:
        return f"expected error {expected!r}, got {got!r}"
    return None


def check_properties(seed: int, count: int) -> List[Dict[str, Any]]:
    failures: List[Dict[str, Any]] = []
    cases = list(generators.valid_payloads(seed, count)) + generators.boundary_payloads()
    for index, payload in enumerate(cases):
        try:
            _check_valid_case(payload)
        except Exception as error:  # noqa: BLE001 - the suite must aggregate
            minimal, steps = reduce.minimize(
                payload, lambda case: _fails_valid(case, error)
            )
            failures.append(
                {
                    "kind": "property",
                    "case": index,
                    "error": f"{type(error).__name__}: {error}",
                    "minimal": minimal,
                    "reduction_steps": steps,
                }
            )
    for index, payload in enumerate(generators.invalid_payloads()):
        message = _check_invalid_case(payload)
        if message:
            failures.append({"kind": "validation", "case": index, "error": message})
    return failures


def _failure_signature(error: Exception) -> tuple:
    """Stable identity of a failure for reduction.

    Reduction must keep failing for the *same* reason; matching only on the
    exception class would let the reducer drift to an unrelated violation.
    """
    return (type(error).__name__, getattr(error, "code", None), str(error))


def _fails_valid(payload: Dict[str, Any], reference: Exception) -> bool:
    """Predicate for reduction: the same failure class still holds."""
    try:
        _check_valid_case(payload)
        return False
    except Exception as error:  # noqa: BLE001
        signature = _failure_signature(error)
        # Message text may mention an event index that changes during
        # reduction; the exception type plus invariant code is the stable part.
        return signature[0] == _failure_signature(reference)[0] and signature[1] == _failure_signature(reference)[1]


def check_metamorphic(seed: int, count: int) -> List[Dict[str, Any]]:
    failures: List[Dict[str, Any]] = []
    for index, payload in enumerate(generators.valid_payloads(seed + 1, count)):
        try:
            metamorphic.apply(run, payload, seed)
            metamorphic.split_totals_preserved(run, payload, seed)
        except Exception as error:  # noqa: BLE001
            minimal, steps = reduce.minimize(payload, lambda case: _fails_meta(case, error, seed))
            failures.append(
                {
                    "kind": "metamorphic",
                    "case": index,
                    "error": f"{type(error).__name__}: {error}",
                    "minimal": minimal,
                    "reduction_steps": steps,
                }
            )
    return failures


def _fails_meta(payload: Dict[str, Any], reference: Exception, seed: int) -> bool:
    try:
        metamorphic.apply(run, payload, seed)
        metamorphic.split_totals_preserved(run, payload, seed)
        return False
    except Exception as error:  # noqa: BLE001
        return _failure_signature(error) == _failure_signature(reference)


def check_faults(seed: int, count: int) -> List[Dict[str, Any]]:
    payloads = list(generators.valid_payloads(seed, count)) + generators.boundary_payloads()
    # Include invalid cases: defects such as int() coercion are only visible
    # on input that must be rejected, not on already-valid input.
    payloads += generators.invalid_payloads()
    failures: List[Dict[str, Any]] = []
    for mutant in faults.MUTANTS:
        caught = any(faults.caught_by(payload, mutant) for payload in payloads)
        if not caught:
            failures.append(
                {
                    "kind": "fault_injection",
                    "mutant": mutant[0],
                    "category": mutant[2],
                    "error": f"planted defect {mutant[1]!r} was not detected",
                }
            )
    return failures


def check_report_layer(seed: int) -> List[Dict[str, Any]]:
    """Test the report envelope *directly* with deliberately unsorted data.

    End-to-end runs cannot catch a missing report sort because the engine's
    detail is already canonical.  The report is a packaging boundary and must
    independently guarantee its own stable order and key set.
    """
    from usage_contract_replay.report import REPORT_VERSION, build_result

    failures: List[Dict[str, Any]] = []
    rows = [
        {"account": "A1", "at": "2026-02-01", "event_id": None, "units": 2, "free_units": 0, "billable_units": 2, "unit_price": "1", "line_amount": "2.00", "line_amount_exact": "2"},
        {"account": "A1", "at": "2026-01-01", "event_id": "e0", "units": 1, "free_units": 1, "billable_units": 0, "unit_price": "1", "line_amount": "0.00", "line_amount_exact": "0"},
        {"account": "A1", "at": "2025-12-31", "event_id": None, "units": 3, "free_units": 0, "billable_units": 3, "unit_price": "1", "line_amount": "3.00", "line_amount_exact": "3"},
    ]
    data = {
        "account": "A1",
        "total_units": 6,
        "billable_units": 5,
        "free_units": 1,
        "amount": 5.0,
        "event_count": 3,
        "events": rows,
    }
    try:
        first = build_result("usage-contract-replay", data)
        second = build_result("usage-contract-replay", data)
        if [item["at"] for item in first["events"]] != ["2025-12-31", "2026-01-01", "2026-02-01"]:
            raise invariants.InvariantViolation("report_order", "report did not sort its detail")
        if first != second:
            raise invariants.InvariantViolation("report_determinism", "report is not deterministic")
        if first.get("report_version") != REPORT_VERSION:
            raise invariants.InvariantViolation("report_version", "report version drift")
        # A missing field must be a loud ReportError, not a silent omission.
        for missing in ("billable_units", "total_units", "events"):
            broken = dict(data)
            del broken[missing]
            try:
                build_result("usage-contract-replay", broken)
            except Exception:
                continue
            raise invariants.InvariantViolation("report_missing_field", f"missing {missing!r} was not rejected")
    except Exception as error:  # noqa: BLE001
        failures.append(
            {
                "kind": "report_layer",
                "error": f"{type(error).__name__}: {error}",
                "minimal": {"note": "report-layer unit case"},
            }
        )
    return failures


def run_suite(seed: int = 20261117, count: int = 60, layers: Optional[List[str]] = None) -> int:
    selected = set(layers or ["property", "metamorphic", "faults", "report"])
    failures: List[Dict[str, Any]] = []
    if "property" in selected:
        failures += check_properties(seed, count)
    if "metamorphic" in selected:
        failures += check_metamorphic(seed, count)
    if "faults" in selected:
        failures += check_faults(seed, count)
    if "report" in selected:
        failures += check_report_layer(seed)

    if failures:
        print(json.dumps({"status": "failed", "failures": failures}, indent=2, sort_keys=True))
        return 1
    print(json.dumps({"status": "ok", "seed": seed, "count": count, "layers": sorted(selected)}))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="usage-contract-replay QA suite")
    parser.add_argument("--seed", type=int, default=20261117)
    parser.add_argument("--count", type=int, default=60)
    parser.add_argument("--suite", action="store_true", help="run all default layers")
    parser.add_argument(
        "--layers",
        default="property,metamorphic,faults,report",
        help="comma separated: property,metamorphic,faults,report",
    )
    args = parser.parse_args(argv)
    layers = [layer for layer in args.layers.split(",") if layer]
    return run_suite(seed=args.seed, count=args.count, layers=layers)


if __name__ == "__main__":
    raise SystemExit(main())

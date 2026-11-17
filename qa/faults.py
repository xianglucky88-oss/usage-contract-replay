"""Fault injection.

The QA layer wraps the production modules in a proxy whose individual
functions can be swapped for deliberately broken variants.  Each mutant is a
*planted defect*; the property/invariant harness must catch it.  This is how we
prove the QA layer itself has teeth (a suite that passes on every fault is not
testing anything).

Mutants target the exact bug classes named in the brief:

* integer truncation of units (``int()`` coercion);
* free-quota truncation / off-by-one at the boundary;
* float rounding instead of exact decimal money;
* account grouping / first-event attribution errors;
* report envelope packaging (lost sort, dropped field, stringified totals).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Callable, Dict, List

from usage_contract_replay import engine, parser, pipeline, report
from usage_contract_replay.models import InputError, ParsedInput, UsageEvent

from . import generators, oracle
from .invariants import InvariantViolation, check_all
from .oracle import OracleError


@dataclass(frozen=True)
class Mutant:
    name: str
    description: str
    category: str


def _broken_parse_int(real):
    """BUG: units are truncated with int() instead of strictly validated.

    The defect is only observable on input that must be *rejected* (a float
    like 1.5, a numeric string, a bool), so the QA layer's validation-parity
    check is what kills it.
    """

    def parse(payload):
        if isinstance(payload, dict) and isinstance(payload.get("events"), list):
            coerced = []
            for row in payload["events"]:
                if isinstance(row, dict) and "units" in row:
                    row = dict(row)
                    try:
                        row["units"] = int(row["units"])
                    except (TypeError, ValueError):
                        pass
                coerced.append(row)
            payload = {**payload, "events": coerced}
        return real(payload)

    return parse


def _broken_free_off_by_one(real):
    """BUG: free quota is truncated by one at the boundary."""

    def calculate(parsed):
        data = real(parsed)
        if data["free_units"] and data["billable_units"]:
            data["billable_units"] += 1
            data["free_units"] -= 1
            data["amount"] = round(data["amount"] + float(parsed.contract.unit_price), 2)
            data["amount_exact"] = f"{data['amount']:.2f}"
            if data["events"]:
                data["events"][0]["billable_units"] += 1
                data["events"][0]["free_units"] -= 1
        return data

    return calculate


def _broken_float_money(real):
    """BUG: aggregate money is summed with binary floats."""

    def calculate(parsed):
        data = real(parsed)
        total = 0.0
        for item in data["events"]:
            total += float(item["unit_price"]) * item["billable_units"]
        refund = min(float(parsed.contract.refund_amount), total)
        data["amount"] = round(total - refund, 2)
        data["amount_exact"] = repr(data["amount"])
        return data

    return calculate


def _broken_account_normalisation(real):
    """BUG: account is echoed raw instead of normalised.

    Account grouping depends on comparing a *normalised* key; failing to strip
    surrounding whitespace makes ``" A1"`` and ``"A1"`` different groups and
    leaks the raw value into the invoice.  This is observable on a single
    account (the echoed account differs from the oracle's normalised one).
    """

    def parse(payload):
        parsed = real(payload)
        # Re-introduce the un-normalised account from the raw payload.
        raw_accounts = [
            row.get("account")
            for row in payload.get("events", [])
            if isinstance(row, dict) and isinstance(row.get("account"), str)
        ]
        raw_first = raw_accounts[0] if raw_accounts else None
        events = tuple(
            UsageEvent(
                account=raw_first if raw_first is not None else event.account,
                at=event.at,
                units=event.units,
                event_id=event.event_id,
            )
            for event in parsed.events
        )
        return ParsedInput(
            events=events,
            contract=parsed.contract,
            received_event_count=parsed.received_event_count,
            duplicate_event_count=parsed.duplicate_event_count,
        )

    return parse


def _broken_report_unsorted(real):
    """BUG: report emits events in engine order without the stable re-sort."""

    def build_result(project, data):
        payload = dict(data)
        payload["events"] = list(reversed(payload["events"]))
        payload["report_version"] = report.REPORT_VERSION
        return {"project": project, "status": "ok", **payload}

    return build_result


def _broken_report_drop_field(real):
    """BUG: report drops billable_units from the envelope."""

    def build_result(project, data):
        payload = dict(data)
        payload.pop("billable_units", None)
        payload["report_version"] = report.REPORT_VERSION
        return {"project": project, "status": "ok", **payload}

    return build_result


def _broken_report_string_totals(real):
    """BUG: report stringifies integer totals (type drift)."""

    def build_result(project, data):
        payload = dict(data)
        payload["total_units"] = str(payload["total_units"])
        payload["report_version"] = report.REPORT_VERSION
        return {"project": project, "status": "ok", **payload}

    return build_result


def _broken_refund_floor(real):
    """BUG: refund is applied without capping at gross, allowing negatives."""

    def calculate(parsed):
        data = real(parsed)
        refund = float(parsed.contract.refund_amount)
        data["amount"] = round(data["amount"] - refund, 2)
        data["amount_exact"] = f"{data['amount']:.2f}"
        return data

    return calculate


def _broken_dedupe_disabled(real):
    """BUG: replay duplicates are counted twice."""

    def parse(payload):
        parsed = real(payload)
        # Re-introduce every received event, dropping the de-duplication step.
        events = list(parsed.events)
        seen_ids = {event.event_id for event in events if event.event_id is not None}
        # Duplicate a proportion of events to simulate a replayed stream being
        # charged twice.
        if events:
            events = events + [events[0]]
        events.sort(key=lambda event: event.sort_key)
        return ParsedInput(
            events=tuple(events),
            contract=parsed.contract,
            received_event_count=parsed.received_event_count,
            duplicate_event_count=0,
        )

    return parse


MUTANTS: List[tuple] = [
    ("int_truncation", "units truncated with int()", "integer_conversion", _broken_parse_int, "parser", "parse"),
    ("free_off_by_one", "free quota off by one", "free_truncation", _broken_free_off_by_one, "engine", "calculate"),
    ("float_money", "money aggregated as float", "float_rounding", _broken_float_money, "engine", "calculate"),
    ("account_grouping", "account grouping / traceability drift", "account_grouping", _broken_account_normalisation, "parser", "parse"),
    ("replay_double_count", "replay charged twice", "replay_idempotence", _broken_dedupe_disabled, "parser", "parse"),
    ("report_unsorted", "report events reversed", "report_wrapping", _broken_report_unsorted, "report", "build_result"),
    ("report_drop_field", "report drops billable_units", "report_wrapping", _broken_report_drop_field, "report", "build_result"),
    ("report_string_totals", "report stringifies totals", "report_wrapping", _broken_report_string_totals, "report", "build_result"),
    ("refund_uncapped", "refund not capped at gross", "refund_boundary", _broken_refund_floor, "engine", "calculate"),
]


class _Patched:
    """Context manager that swaps one module function for a mutant."""

    def __init__(self, module, name: str, replacement: Callable):
        self.module = module
        self.name = name
        self.replacement = replacement
        self.original = None

    def __enter__(self):
        self.original = getattr(self.module, self.name)
        setattr(self.module, self.name, self.replacement)
        return self

    def __exit__(self, *exc):
        setattr(self.module, self.name, self.original)
        return False


def mutant_runner(mutant) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    """Return a pipeline-like runner with the mutant active."""
    name, description, category, factory, module_name, func_name = mutant
    module = {"parser": parser, "engine": engine, "report": report}[module_name]
    real = getattr(module, func_name)
    replacement = factory(real)

    def run(payload):
        # Patch the module and the pipeline's imported reference, because
        # pipeline.py imports names at module load time.
        with _Patched(module, func_name, replacement):
            saved = getattr(pipeline, func_name, None)
            if saved is not None:
                setattr(pipeline, func_name, replacement)
            try:
                return pipeline.run(payload)
            finally:
                if saved is not None:
                    setattr(pipeline, func_name, saved)

    return run


def caught_by(payload: Dict[str, Any], mutant) -> bool:
    """True if the QA layer detects this mutant on this payload.

    For valid payloads the full invariant suite runs; for invalid payloads the
    check is validation parity against the oracle's error code.
    """
    runner = mutant_runner(mutant)
    expected = oracle.codes_for(payload)
    try:
        result = runner(payload)
    except (OracleError, InputError):
        return False
    except Exception:
        return True
    if expected is not None:
        # Invalid input: the mutant is caught if it changes the error code,
        # succeeds where the oracle rejects, or crashes.
        if result.get("status") != "error":
            return True
        return result.get("error", {}).get("code") != expected
    if result.get("status") != "ok":
        return True
    try:
        check_all(result, payload)
    except InvariantViolation:
        return True
    except Exception:
        return True
    return False


def surviving_mutants(seed: int = 20261117, count: int = 60) -> List[Mutant]:
    """Return mutants the QA layer failed to catch on any generated case.

    Both valid and invalid payloads are exercised, because some planted
    defects (integer coercion) are only observable on input that must be
    rejected.
    """
    valid = list(generators.valid_payloads(seed, count)) + generators.boundary_payloads()
    invalid = generators.invalid_payloads()
    payloads = [p for p in valid if oracle.codes_for(p) is None] + [
        p for p in invalid if oracle.codes_for(p) is not None
    ]
    survivors: List[Mutant] = []
    for mutant in MUTANTS:
        caught = any(caught_by(payload, mutant) for payload in payloads)
        if not caught:
            survivors.append(Mutant(mutant[0], mutant[1], mutant[2]))
    return survivors

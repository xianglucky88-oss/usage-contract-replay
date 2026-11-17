"""Conservation and traceability invariants.

Two kinds of checks live here:

* **Internal** invariants that must hold for any successful pipeline result,
  independent of the oracle (totals reconcile with the event detail, the
  report is sorted, no value is silently truncated).
* **Cross-check** invariants comparing the pipeline to :mod:`qa.oracle`.

Every violation is raised as :class:`InvariantViolation` with a stable
``code`` so the reducer and mutation harness can aggregate failures.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction
from typing import Any, Dict

from .oracle import compute, round_half_up_cents

CENT = Decimal("0.01")


def _cents(value: Decimal) -> Decimal:
    """Round exact Decimal money to cents with the documented half-up rule.

    ``Decimal.quantize`` defaults to the context's ``ROUND_HALF_EVEN``; using
    it here by accident would make the invariant disagree with both the engine
    and the oracle on exact-half cases such as 1.665.
    """
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


class InvariantViolation(AssertionError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


def _fail(code: str, message: str) -> None:
    raise InvariantViolation(code, message)


def _decimal(text: str) -> Decimal:
    return Decimal(str(text))


def check_result_shape(result: Dict[str, Any]) -> None:
    """The report envelope has the keys and types the QA contract expects."""
    if result.get("status") != "ok":
        _fail("status", f"expected ok status, got {result.get('status')!r}")
    if result.get("project") != "usage-contract-replay":
        _fail("project", f"unexpected project {result.get('project')!r}")
    if result.get("report_version") != 1:
        _fail("report_version", f"unexpected report_version {result.get('report_version')!r}")
    for field in ("account", "total_units", "billable_units", "free_units", "amount", "events"):
        if field not in result:
            _fail("missing_field", f"missing {field!r}")
    if not isinstance(result["events"], list):
        _fail("events_type", "events must be a list")
    if not isinstance(result["total_units"], int) or isinstance(result["total_units"], bool):
        _fail("total_units_type", "total_units must be int")
    if not isinstance(result["billable_units"], int) or isinstance(result["billable_units"], bool):
        _fail("billable_units_type", "billable_units must be int")
    if not isinstance(result["free_units"], int) or isinstance(result["free_units"], bool):
        _fail("free_units_type", "free_units must be int")


def check_conservation(result: Dict[str, Any]) -> None:
    """Totals must be exactly the sum of the per-event detail."""
    events = result["events"]
    total = sum(item["units"] for item in events)
    billable = sum(item["billable_units"] for item in events)
    free = sum(item["free_units"] for item in events)

    if result["total_units"] != total:
        _fail("total_units_conservation", f"total_units {result['total_units']} != sum(detail) {total}")
    if result["billable_units"] != billable:
        _fail("billable_conservation", f"billable_units {result['billable_units']} != sum(detail) {billable}")
    if result["free_units"] != free:
        _fail("free_conservation", f"free_units {result['free_units']} != sum(detail) {free}")
    if result["free_units"] + result["billable_units"] != result["total_units"]:
        _fail("free_billable_total", "free + billable != total")
    if result["event_count"] != len(events):
        _fail("event_count", f"event_count {result['event_count']} != len(events) {len(events)}")

    for index, item in enumerate(events):
        if item["free_units"] + item["billable_units"] != item["units"]:
            _fail("event_conservation", f"event {index}: free+billable != units")
        if item["free_units"] < 0 or item["billable_units"] < 0:
            _fail("event_negative", f"event {index}: negative allocation")


def check_amount(result: Dict[str, Any]) -> None:
    """The aggregate amount must reconcile with the line items and refund."""
    events = result["events"]
    # Reconciliation uses the exact (unrounded) line amounts and rounds once,
    # because a sub-cent unit price may round differently per line than in
    # aggregate.  Rounding each line and summing would itself be a bug.
    gross = sum((_decimal(item["line_amount_exact"]) for item in events), Decimal("0"))
    gross = _cents(gross)
    if _decimal(result["gross_amount_exact"]) != gross:
        _fail("gross_reconcile", f"gross {result['gross_amount_exact']} != sum(lines) {gross}")

    amount = _decimal(result["amount_exact"])
    if amount != gross - _decimal(result["refund_applied_exact"]):
        _fail("amount_reconcile", "amount != gross - refund_applied")
    if amount < 0:
        _fail("amount_negative", f"amount {amount} < 0")
    if _decimal(result["amount_exact"]) != _decimal(repr(result["amount"])):
        # float() is a lossy view; the exact string is authoritative but the
        # JSON number must round-trip to the same two-decimal value.
        if round(result["amount"], 2) != float(amount):
            _fail("amount_float_view", "amount float view disagrees with exact value")

    for index, item in enumerate(events):
        # The displayed line_amount is the cent-rounded view of the exact line;
        # it must be the correct rounding of billable * unit_price.
        expected_exact = _decimal(item["unit_price"]) * item["billable_units"]
        expected = _cents(expected_exact)
        if _decimal(item["line_amount"]) != expected:
            _fail("line_amount", f"event {index}: line_amount != round(billable*unit_price)")
        if _decimal(item["line_amount_exact"]) != expected_exact:
            _fail("line_amount_exact", f"event {index}: exact line amount drift")

    if _decimal(result["refund_requested_exact"]) < _decimal(result["refund_applied_exact"]):
        _fail("refund_apply", "refund_applied exceeds requested")
    expected_applied = min(_decimal(result["refund_requested_exact"]), gross)
    if _decimal(result["refund_applied_exact"]) != expected_applied:
        _fail("refund_cap", "refund_applied != min(requested, gross)")


def check_traceability(result: Dict[str, Any], payload: Dict[str, Any]) -> None:
    """Contract fields and refund reason are echoed and internally consistent."""
    contract = result["contract"]
    if not isinstance(contract, dict):
        _fail("contract_echo", "contract echo missing")
    if contract.get("free") != payload["contract"].get("free", 0):
        _fail("contract_free", "contract.free not echoed")
    if _decimal(contract.get("unit_price")) != _decimal(str(payload["contract"]["unit_price"])):
        _fail("contract_price", "contract.unit_price not echoed")
    if _decimal(contract.get("refund")) != _decimal(str(payload["contract"].get("refund", 0))):
        _fail("contract_refund", "contract.refund not echoed")

    requested = _decimal(result["refund_requested_exact"])
    if requested > 0 and not result.get("refund_reason"):
        _fail("refund_reason_missing", "refund applied without a traceable reason")
    if result.get("refund_reason") is not None and result["refund_reason"] != contract.get("refund_reason"):
        _fail("refund_reason_echo", "refund_reason not echoed consistently")


def check_order(result: Dict[str, Any]) -> None:
    """Event detail is in the canonical total order."""
    keys = [
        (
            item["account"],
            item["at"],
            item["event_id"] is not None,
            item["event_id"] or "",
            item["units"],
        )
        for item in result["events"]
    ]
    if keys != sorted(keys):
        _fail("event_order", "event detail is not canonically sorted")


def check_against_oracle(result: Dict[str, Any], payload: Dict[str, Any]) -> None:
    """Every scalar the oracle models must match exactly."""
    expected = compute(payload)
    if result["total_units"] != expected.total_units:
        _fail("oracle_total", f"total {result['total_units']} != {expected.total_units}")
    if result["billable_units"] != expected.billable_units:
        _fail("oracle_billable", f"billable {result['billable_units']} != {expected.billable_units}")
    if result["free_units"] != expected.free_units_used:
        _fail("oracle_free", f"free {result['free_units']} != {expected.free_units_used}")
    if result["account"] != expected.account:
        _fail("oracle_account", f"account {result['account']!r} != {expected.account!r}")
    if round_half_up_cents(Fraction(result["amount_exact"])) != expected.amount_cents:
        _fail(
            "oracle_amount",
            f"amount {result['amount_exact']} != oracle cents {expected.amount_cents}",
        )
    if round_half_up_cents(Fraction(result["gross_amount_exact"])) != expected.gross_cents:
        _fail("oracle_gross", "gross amount disagrees with oracle")

    got = [
        (
            item["account"],
            item["at"],
            item["event_id"],
            item["units"],
            item["free_units"],
            item["billable_units"],
        )
        for item in result["events"]
    ]
    if tuple(got) != expected.events:
        _fail("oracle_detail", "per-event allocation disagrees with oracle")


def check_all(result: Dict[str, Any], payload: Dict[str, Any]) -> None:
    check_result_shape(result)
    check_conservation(result)
    check_amount(result)
    check_traceability(result, payload)
    check_order(result)
    check_against_oracle(result, payload)

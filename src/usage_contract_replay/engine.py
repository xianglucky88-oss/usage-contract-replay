"""Domain calculation only.

This module receives an already validated :class:`ParsedInput` and returns a
plain, JSON-safe ``dict``.  It performs no I/O and applies no pricing rule
beyond the contract fields supplied by the caller:

* usage is billed at whole units; the free quota is consumed in canonical
  event order and is never allowed to make usage negative;
* money is exact ``Decimal`` arithmetic rounded once, half-up, to cents, so no
  binary floating point error can leak into the invoice;
* a refund is capped at the amount actually receivable, so the payable amount
  can never go below zero, and the cap is reported rather than hidden;
* every event carries its share of free/billable units so the invoice totals
  can be reconciled against the detail.

The invariants checked by the QA layer are documented in ``qa/oracle.py``.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from .models import Contract, ParsedInput
from .parser import contract_echo, parse

CENT = Decimal("0.01")
ZERO = Decimal("0")


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _money_str(value: Decimal) -> str:
    return format(_money(value), "f")


def _exact_str(value: Decimal) -> str:
    """Full-precision canonical string (no cent quantisation).

    Unit price must keep its full precision: a sub-cent price such as 0.015
    is a legitimate contract value and rounding it to 0.02 at the line level
    would silently change the invoice.
    """
    text = format(value, "f")
    return text


def _allocate_free(events, free_units: int) -> List[Dict[str, Any]]:
    """Consume the free quota in canonical order.

    The allocation is a deterministic function of the canonically sorted
    events, so shuffling the input cannot move billable units between events.
    """
    remaining_free = free_units
    detail: List[Dict[str, Any]] = []
    for event in events:
        used_free = min(event.units, remaining_free)
        remaining_free -= used_free
        billable = event.units - used_free
        detail.append(
            {
                "event_id": event.event_id,
                "account": event.account,
                "at": event.at,
                "units": event.units,
                "free_units": used_free,
                "billable_units": billable,
            }
        )
    return detail


def _refund(contract: Contract, gross: Decimal) -> tuple:
    requested = contract.refund_amount
    applied = min(requested, gross)
    return requested, applied


def calculate(parsed_or_rows, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Calculate the invoice for a parsed replay.

    ``calculate(rows, payload)`` is also accepted for backwards compatibility
    with callers that pass the raw rows and payload separately; the rows are
    validated and normalised through the same parser.
    """
    if isinstance(parsed_or_rows, ParsedInput):
        parsed = parsed_or_rows
    else:
        rows = list(parsed_or_rows)
        base = dict(payload or {})
        base["events"] = rows
        parsed = parse(base)

    contract = parsed.contract
    detail = _allocate_free(parsed.events, contract.free_units)

    total_units = sum(item["units"] for item in detail)
    billable_units = sum(item["billable_units"] for item in detail)
    free_units_used = sum(item["free_units"] for item in detail)

    line_items: List[Dict[str, Any]] = []
    gross = ZERO
    for item in detail:
        # Line amounts keep full precision; only the aggregate is rounded to
        # cents once, so sub-cent unit prices cannot drift the total.
        line_amount = Decimal(item["billable_units"]) * contract.unit_price
        gross += line_amount
        item["unit_price"] = _exact_str(contract.unit_price)
        item["line_amount"] = _money_str(line_amount)
        item["line_amount_exact"] = _exact_str(line_amount)
        line_items.append(item)
    gross = _money(gross)

    refund_requested, refund_applied = _refund(contract, gross)
    refund_applied = _money(refund_applied)
    amount = _money(gross - refund_applied)

    account = parsed.events[0].account if parsed.events else None

    return {
        "account": account,
        "total_units": total_units,
        "billable_units": billable_units,
        "free_units": free_units_used,
        "free_quota": contract.free_units,
        "amount": float(amount),
        "amount_exact": _money_str(amount),
        "gross_amount_exact": _money_str(gross),
        "refund_requested_exact": _money_str(refund_requested),
        "refund_applied_exact": _money_str(refund_applied),
        "refund_capped": refund_applied < refund_requested,
        "refund_reason": contract.refund_reason,
        "contract_version": contract.version,
        "unit_price_exact": _money_str(contract.unit_price),
        "contract": contract_echo(contract),
        "event_count": len(detail),
        "received_event_count": parsed.received_event_count,
        "duplicate_event_count": parsed.duplicate_event_count,
        "events": line_items,
    }

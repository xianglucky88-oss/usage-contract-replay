"""Reference oracle — an independent model of the documented semantics.

Money is handled as exact :class:`fractions.Fraction` and rounded to integer
cents once, half-up, matching ``engine.ROUND_HALF_UP`` by definition but not by
implementation.  Usage is integers throughout.

This module intentionally re-states the semantics from the specification rather
than from the engine source.  The documented policy is:

1. ``units`` are non-negative integers; ``free`` is a non-negative integer.
2. Events are de-duplicated by idempotency key (``event_id`` when present,
   otherwise the content tuple ``(account, at, units)``).  A replay therefore
   never double-counts.  Conflicting payloads under one id are an error.
3. Events are canonically ordered by ``(account, at, event_id-ish, units)``;
   the invoice must not depend on input order.
4. The free quota is consumed in canonical order; ``billable = units - free``
   with no event allowed to go negative.
5. ``gross = round(sum(billable_i * unit_price), 2)`` using half-up on cents.
6. ``refund_applied = min(refund, gross)``; ``amount = gross - refund_applied``
   and is never negative.  A refund above zero requires a reason.
7. ``total_units = sum(units)``; ``billable_units = sum(billable)``;
   ``free_used = total_units - billable_units``.  All are conserved.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Dict, List, Optional, Tuple

CENT = Fraction(1, 100)
VALIDATION_ERROR_CODES = frozenset(
    {
        "payload_not_object",
        "events_not_list",
        "event_not_object",
        "account_invalid",
        "at_invalid",
        "units_not_int",
        "negative_units",
        "event_id_invalid",
        "duplicate_event_conflict",
        "contract_missing",
        "unit_price_missing",
        "unit_price_negative",
        "money_type",
        "money_format",
        "free_not_int",
        "free_negative",
        "refund_negative",
        "refund_reason_invalid",
        "refund_reason_required",
        "contract_version_invalid",
        "mixed_accounts",
    }
)


class OracleError(Exception):
    """The oracle considers the input invalid, with a stable code."""

    def __init__(self, code: str, path: Optional[str] = None):
        self.code = code
        self.path = path
        super().__init__(code)


@dataclass(frozen=True)
class OracleResult:
    account: Optional[str]
    total_units: int
    billable_units: int
    free_units_used: int
    gross_cents: int
    refund_applied_cents: int
    amount_cents: int
    events: Tuple[Tuple[Any, ...], ...]  # canonical (account, at, event_id, units, free, billable)

    @property
    def amount(self) -> Fraction:
        return Fraction(self.amount_cents, 100)

    def event_tuples(self) -> Tuple[Tuple[Any, ...], ...]:
        return self.events


def _as_fraction(value: Any) -> Fraction:
    """Exact conversion; floats are read via ``repr`` like a human decimal."""
    if isinstance(value, bool):
        raise OracleError("money_type")
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise OracleError("money_format")
        return Fraction(repr(value))
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, str):
        text = value.strip()
        if not _is_decimal_text(text):
            raise OracleError("money_format")
        return Fraction(text)
    raise OracleError("money_type")


def _is_decimal_text(text: str) -> bool:
    import re

    return bool(re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", text))


def round_half_up_cents(value: Fraction) -> int:
    """Round an exact Fraction to integer cents using half-up.

    Implemented with integer arithmetic on the exact value; no float touches
    this path, so it is an independent check of the engine's Decimal quantize.
    """
    scaled = value * 100
    sign = -1 if scaled < 0 else 1
    scaled = abs(scaled)
    quotient, remainder = divmod(scaled.numerator, scaled.denominator)
    twice = remainder * 2
    if twice >= scaled.denominator:
        quotient += 1
    return sign * quotient


def _canonical_key(event: Dict[str, Any]) -> tuple:
    event_id = event.get("event_id")
    return (
        event["account"],
        event["at"],
        event_id is not None,
        event_id or "",
        event["units"],
    )


def _identity(event: Dict[str, Any]) -> tuple:
    event_id = event.get("event_id")
    if event_id is not None:
        return ("id", event_id)
    return ("content", event["account"], event["at"], event["units"])


def _validate(payload: Any) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if not isinstance(payload, dict):
        raise OracleError("payload_not_object")
    rows = payload.get("events", [])
    if not isinstance(rows, list):
        raise OracleError("events_not_list", "events")

    events: List[Dict[str, Any]] = []
    for index, row in enumerate(rows):
        path = f"events[{index}]"
        if not isinstance(row, dict):
            raise OracleError("event_not_object", path)
        account = row.get("account")
        if not isinstance(account, str) or not account.strip():
            raise OracleError("account_invalid", f"{path}.account")
        at = row.get("at")
        if not isinstance(at, str) or not at.strip():
            raise OracleError("at_invalid", f"{path}.at")
        units = row.get("units")
        if isinstance(units, bool) or not isinstance(units, int):
            raise OracleError("units_not_int", f"{path}.units")
        if units < 0:
            raise OracleError("negative_units", f"{path}.units")
        event_id = row.get("event_id")
        if event_id is not None:
            if not isinstance(event_id, str) or not event_id.strip():
                raise OracleError("event_id_invalid", f"{path}.event_id")
            event_id = event_id.strip()
        events.append(
            {
                "account": account.strip(),
                "at": at.strip(),
                "units": units,
                "event_id": event_id,
            }
        )

    contract = payload.get("contract")
    if not isinstance(contract, dict):
        raise OracleError("contract_missing", "contract")
    if "unit_price" not in contract:
        raise OracleError("unit_price_missing", "contract.unit_price")
    unit_price = _as_fraction(contract["unit_price"])
    if unit_price < 0:
        raise OracleError("unit_price_negative", "contract.unit_price")
    free = contract.get("free", 0)
    if isinstance(free, bool) or not isinstance(free, int):
        raise OracleError("free_not_int", "contract.free")
    if free < 0:
        raise OracleError("free_negative", "contract.free")
    refund = _as_fraction(contract.get("refund", 0))
    if refund < 0:
        raise OracleError("refund_negative", "contract.refund")
    reason = contract.get("refund_reason")
    if reason is not None and not isinstance(reason, str):
        raise OracleError("refund_reason_invalid", "contract.refund_reason")
    if refund > 0 and not (isinstance(reason, str) and reason.strip()):
        raise OracleError("refund_reason_required", "contract.refund_reason")
    version = contract.get("version")
    if version is not None and not isinstance(version, str):
        raise OracleError("contract_version_invalid", "contract.version")

    accounts = {event["account"] for event in events}
    if len(accounts) > 1:
        raise OracleError("mixed_accounts", "events")

    seen: Dict[tuple, Dict[str, Any]] = {}
    unique: List[Dict[str, Any]] = []
    for event in events:
        key = _identity(event)
        previous = seen.get(key)
        if previous is None:
            seen[key] = event
            unique.append(event)
        elif previous != event:
            raise OracleError("duplicate_event_conflict", "events")

    unique.sort(key=_canonical_key)
    contract_out = {
        "free": free,
        "unit_price": unit_price,
        "refund": refund,
        "refund_reason": reason.strip() if isinstance(reason, str) and reason.strip() else None,
        "version": version.strip() if isinstance(version, str) else None,
    }
    return unique, contract_out


def compute(payload: Any) -> OracleResult:
    """Reference invoice for ``payload``.  Raises :class:`OracleError`."""
    events, contract = _validate(payload)
    free_remaining = contract["free"]
    detail: List[Tuple[Any, ...]] = []
    total = 0
    billable_total = 0
    gross = Fraction(0)
    for event in events:
        used_free = min(event["units"], free_remaining)
        free_remaining -= used_free
        billable = event["units"] - used_free
        total += event["units"]
        billable_total += billable
        gross += Fraction(billable) * contract["unit_price"]
        detail.append(
            (
                event["account"],
                event["at"],
                event["event_id"],
                event["units"],
                used_free,
                billable,
            )
        )

    gross_cents = round_half_up_cents(gross)
    refund_cents = round_half_up_cents(contract["refund"])
    refund_applied = min(refund_cents, gross_cents)
    amount_cents = gross_cents - refund_applied
    account = events[0]["account"] if events else None
    return OracleResult(
        account=account,
        total_units=total,
        billable_units=billable_total,
        free_units_used=total - billable_total,
        gross_cents=gross_cents,
        refund_applied_cents=refund_applied,
        amount_cents=amount_cents,
        events=tuple(detail),
    )


def codes_for(payload: Any) -> Optional[str]:
    """Return the oracle's error code, or ``None`` if the payload is valid."""
    try:
        compute(payload)
    except OracleError as error:
        return error.code
    return None

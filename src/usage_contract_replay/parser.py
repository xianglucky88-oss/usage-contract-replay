"""Input validation and record extraction.

The parser is the only place that knows about raw JSON input.  It produces a
deterministic :class:`~usage_contract_replay.models.ParsedInput`:

* events are validated field by field and never silently coerced;
* events are de-duplicated by idempotency key so a replay is a no-op;
* events are sorted into a canonical total order so input order cannot change
  the invoice;
* the contract is normalised to ``Decimal`` money and an ``int`` free quota,
  and echoed back losslessly (as JSON-safe values) for traceability.

Validation policy (a boundary, not a new pricing rule):

* ``units`` must be a real integer.  Booleans, floats and numeric strings are
  rejected rather than truncated with ``int()``.
* zero units are valid and remain visible in the detail; negative units are
  rejected because a negative usage event is a credit, not usage.
* every event of one replay must belong to the same account; the result
  envelope models a single account, so mixed accounts are rejected instead of
  silently attributed to whichever event happens to be first.
* a refund must carry a reason and may not exceed the amount receivable.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from .models import Contract, InputError, ParsedInput, UsageEvent

_CENT = Decimal("0.01")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def _parse_units(value: Any, path: str) -> int:
    # bool is a subclass of int; treat it as invalid rather than 1/0.
    if isinstance(value, bool) or not isinstance(value, int):
        raise InputError(
            "units_not_int",
            f"units must be an integer, got {type(value).__name__}",
            path,
        )
    if value < 0:
        raise InputError("negative_units", "units must be >= 0", path)
    return value


def _parse_money(value: Any, path: str) -> Decimal:
    if isinstance(value, bool):
        raise InputError("money_type", "expected a decimal number", path)
    if isinstance(value, float):
        # Go through str() so 0.1 becomes Decimal("0.1") rather than the
        # binary expansion of the float.
        text = repr(value)
    elif isinstance(value, (int, Decimal)):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise InputError(
            "money_type",
            f"expected a decimal number, got {type(value).__name__}",
            path,
        )
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", text):
        raise InputError("money_format", f"not a decimal number: {value!r}", path)
    try:
        number = Decimal(text)
    except InvalidOperation:  # pragma: no cover - guarded by the regex
        raise InputError("money_format", f"not a decimal number: {value!r}", path)
    if not number.is_finite():
        raise InputError("money_format", "amount must be finite", path)
    return number


def _parse_int_quota(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InputError(
            "free_not_int",
            f"free must be a non-negative integer, got {type(value).__name__}",
            path,
        )
    if value < 0:
        raise InputError("free_negative", "free must be >= 0", path)
    return value


def _parse_account(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InputError("account_invalid", "account must be a non-empty string", path)
    return value.strip()


def _parse_at(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InputError("at_invalid", "at must be a non-empty string", path)
    return value.strip()


def _parse_event(raw: Dict[str, Any], index: int) -> UsageEvent:
    path = f"events[{index}]"
    account = _parse_account(raw.get("account"), f"{path}.account")
    at = _parse_at(raw.get("at"), f"{path}.at")
    units = _parse_units(raw.get("units"), f"{path}.units")
    event_id = raw.get("event_id")
    if event_id is not None:
        if not isinstance(event_id, str) or not event_id.strip():
            raise InputError(
                "event_id_invalid",
                "event_id must be a non-empty string when present",
                f"{path}.event_id",
            )
        event_id = event_id.strip()
    return UsageEvent(account=account, at=at, units=units, event_id=event_id)


def _contract_raw(contract: Contract) -> Dict[str, Any]:
    raw = {
        "free": contract.free_units,
        "unit_price": str(contract.unit_price),
        "refund": str(contract.refund_amount),
        "refund_reason": contract.refund_reason,
        "version": contract.version,
    }
    # Preserve unknown contract fields so the echo is a superset of the input
    # and downstream consumers can trace every field they supplied.
    for key, value in contract.raw.items():
        raw.setdefault(key, value)
    return raw


def _parse_contract(payload: Dict[str, Any]) -> Contract:
    contract = payload.get("contract")
    if not isinstance(contract, dict):
        raise InputError("contract_missing", "contract must be an object", "contract")
    if "unit_price" not in contract:
        raise InputError("unit_price_missing", "unit_price is required", "contract.unit_price")
    unit_price = _parse_money(contract["unit_price"], "contract.unit_price")
    if unit_price < 0:
        raise InputError("unit_price_negative", "unit_price must be >= 0", "contract.unit_price")

    free_units = _parse_int_quota(contract.get("free", 0), "contract.free")
    refund = _parse_money(contract.get("refund", 0), "contract.refund")
    if refund < 0:
        raise InputError("refund_negative", "refund must be >= 0", "contract.refund")

    reason = contract.get("refund_reason")
    if reason is not None and not isinstance(reason, str):
        raise InputError("refund_reason_invalid", "refund_reason must be a string", "contract.refund_reason")
    if refund > 0 and not (reason and reason.strip()):
        raise InputError(
            "refund_reason_required",
            "a non-empty refund_reason is required when refund > 0",
            "contract.refund_reason",
        )

    version = contract.get("version")
    if version is not None and not isinstance(version, str):
        raise InputError("contract_version_invalid", "version must be a string", "contract.version")

    raw_echo = {
        key: value
        for key, value in contract.items()
        if key not in {"free", "unit_price", "refund", "refund_reason", "version"}
    }
    return Contract(
        free_units=free_units,
        unit_price=unit_price,
        refund_amount=refund,
        refund_reason=reason.strip() if isinstance(reason, str) and reason.strip() else None,
        version=version.strip() if isinstance(version, str) else None,
        raw=raw_echo,
    )


def _dedupe(events: List[UsageEvent]) -> Tuple[List[UsageEvent], int]:
    """Collapse replay duplicates while preserving the first-seen payload.

    Two events sharing an idempotency key but disagreeing on the payload are a
    data-integrity failure: silently choosing one would make the invoice depend
    on input order.
    """
    seen: Dict[tuple, UsageEvent] = {}
    unique: List[UsageEvent] = []
    duplicates = 0
    for event in events:
        key = event.identity
        previous = seen.get(key)
        if previous is None:
            seen[key] = event
            unique.append(event)
            continue
        if previous.to_dict() != event.to_dict():
            raise InputError(
                "duplicate_event_conflict",
                f"same event id with conflicting payload: {event.event_id!r}",
            )
        duplicates += 1
    return unique, duplicates


def load_records(payload: Any) -> List[dict]:
    """Backwards-compatible extraction of raw dict rows.

    Kept for callers that only want the raw rows; ``parse`` is the validated
    entry point used by the pipeline.
    """
    if not isinstance(payload, dict):
        raise InputError("payload_not_object", "payload must be an object")
    rows = payload.get("events", [])
    if not isinstance(rows, list):
        raise InputError("events_not_list", "events must be a list", "events")
    return [dict(row) for row in rows if isinstance(row, dict)]


def parse(payload: Any) -> ParsedInput:
    if not isinstance(payload, dict):
        raise InputError("payload_not_object", "payload must be an object")
    rows = payload.get("events", [])
    if not isinstance(rows, list):
        raise InputError("events_not_list", "events must be a list", "events")
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise InputError(
                "event_not_object",
                f"event must be an object, got {type(row).__name__}",
                f"events[{index}]",
            )

    events = [_parse_event(row, index) for index, row in enumerate(rows)]
    contract = _parse_contract(payload)

    accounts = {event.account for event in events}
    if len(accounts) > 1:
        raise InputError(
            "mixed_accounts",
            "a replay must contain events for exactly one account",
            "events",
        )

    unique, duplicates = _dedupe(events)
    unique.sort(key=lambda event: event.sort_key)

    # The account grouping happens before calculation so downstream layers never
    # see a mixed stream and never have to re-derive the account.
    return ParsedInput(
        events=tuple(unique),
        contract=contract,
        received_event_count=len(rows),
        duplicate_event_count=duplicates,
    )


def contract_echo(contract: Contract) -> Dict[str, Any]:
    """JSON-safe echo of the contract used for the invoice."""
    return _contract_raw(contract)

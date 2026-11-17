"""Small deterministic helpers shared by the replay modules.

The important property of this module is that nothing here depends on the
current time, dictionary insertion order, or floating point arithmetic.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping


CENT = Decimal("0.01")
ONE = Decimal("1")


class InputError(ValueError):
    """Raised for malformed input that cannot be processed as a batch."""


def canonical_json(value: Any) -> str:
    """Return a stable JSON representation for hashing and diagnostics."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return decimal_text(value)
    if isinstance(value, datetime):
        return isoformat_utc(value)
    if isinstance(value, date):
        return value.isoformat()
    if is_dataclass(value):
        return asdict(value)
    raise TypeError("not JSON serializable: %r" % (value,))


def sha256_hex(value: Any) -> str:
    if not isinstance(value, (str, bytes)):
        value = canonical_json(value)
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def short_digest(value: Any, length: int = 20) -> str:
    return sha256_hex(value)[:length]


def decimal_text(value: Decimal) -> str:
    """Plain, non-scientific decimal text (``Decimal('1E+2')`` -> ``'100'``)."""

    if value == 0:
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def number_from_decimal(value: Decimal) -> Any:
    """JSON-friendly number: int when integral, otherwise float.

    Exact values are always available alongside this field as a string or as
    integer cents in the output contract.
    """

    if value == value.to_integral_value():
        return int(value)
    return float(value)


def parse_decimal(value: Any, *, field: str = "value") -> Decimal:
    if isinstance(value, bool) or value is None:
        raise InputError("%s must be a finite number" % field)
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        # JSON parses decimal literals as float.  Going through str keeps the
        # user's textual value (e.g. 0.1 stays 0.1) instead of binary noise.
        result = Decimal(str(value))
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise InputError("%s must not be empty" % field)
        try:
            result = Decimal(text)
        except InvalidOperation as exc:
            raise InputError("%s is not a decimal number: %r" % (field, value)) from exc
    else:
        raise InputError("%s is not a number: %r" % (field, value))
    if not result.is_finite():
        raise InputError("%s must be finite" % field)
    return result


def parse_non_negative_decimal(value: Any, *, field: str = "value") -> Decimal:
    result = parse_decimal(value, field=field)
    if result < 0:
        raise InputError("%s must be >= 0" % field)
    return result


def parse_timestamp(value: Any, *, field: str = "at") -> datetime:
    """Parse a date/datetime/epoch into an aware UTC datetime.

    Date-only values mean midnight UTC.  Naive datetimes are explicitly
    interpreted as UTC.  Period attribution happens after this conversion.
    """

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day)
    elif isinstance(value, bool) or value is None:
        raise InputError("%s must be an ISO date, datetime, or epoch" % field)
    elif isinstance(value, (int, float)):
        try:
            parsed = datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            raise InputError("%s is not a valid epoch timestamp" % field) from exc
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise InputError("%s must not be empty" % field)
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise InputError("%s is not an ISO timestamp: %r" % (field, value)) from exc
    else:
        raise InputError("%s is not a timestamp: %r" % (field, value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def isoformat_utc(value: datetime) -> str:
    value = value.astimezone(timezone.utc)
    text = value.isoformat(timespec="seconds")
    return text.replace("+00:00", "Z")


def cents_from_decimal(amount: Decimal) -> int:
    """Round an exact monetary amount to integer cents, half away from zero."""

    return int((amount * 100).quantize(ONE, rounding=ROUND_HALF_UP))


def money_from_cents(cents: int) -> Decimal:
    return (Decimal(cents) / 100).quantize(CENT)


def money_text(cents: int) -> str:
    return format(money_from_cents(cents), ".2f")


def int_cents_from_value(value: Any, *, field: str = "amount") -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    amount = parse_decimal(value, field=field)
    return cents_from_decimal(amount)


def first_present(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def present(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> bool:
    return any(key in mapping for key in keys)



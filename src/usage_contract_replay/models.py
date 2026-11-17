"""Immutable data contracts shared by parser, engine, pipeline and report.

The types in this module deliberately contain no billing logic.  They only
describe the shape of validated input and the immutable result envelope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, Optional


class InputError(ValueError):
    """A deterministic, machine-readable validation failure.

    ``code`` is part of the reporting contract used by the QA layer; the
    human readable message is intentionally not asserted on by tests.
    """

    def __init__(self, code: str, message: str, path: Optional[str] = None):
        self.code = code
        self.path = path
        self.message = message
        prefix = f"{path}: " if path else ""
        super().__init__(f"{prefix}{message}")

    def as_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "path": self.path, "message": self.message}


@dataclass(frozen=True)
class UsageEvent:
    """A single validated usage event.

    ``event_id`` is the idempotency key.  Events without an explicit id use
    their normalised content as the idempotency key, which makes a replay of
    the same input stream a no-op rather than a double charge.
    """

    account: str
    at: str
    units: int
    event_id: Optional[str] = None

    @property
    def identity(self) -> tuple:
        if self.event_id is not None:
            return ("id", self.event_id)
        return ("content", self.account, self.at, self.units)

    @property
    def sort_key(self) -> tuple:
        # A stable total order that does not depend on input order.  The
        # boolean distinguishes "no id" from an explicit empty string while
        # keeping the tuple comparable.
        return (
            self.account,
            self.at,
            self.event_id is not None,
            self.event_id or "",
            self.units,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "account": self.account,
            "at": self.at,
            "units": self.units,
        }


@dataclass(frozen=True)
class Contract:
    """The subset of contract fields that participate in billing.

    ``raw`` keeps the normalised, JSON-safe contract echo so every field used
    to produce an invoice can be traced back from the report.
    """

    free_units: int
    unit_price: Decimal
    refund_amount: Decimal
    refund_reason: Optional[str] = None
    version: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedInput:
    events: tuple
    contract: Contract
    received_event_count: int
    duplicate_event_count: int


@dataclass(frozen=True)
class PipelineResult:
    project: str
    data: dict[str, Any]

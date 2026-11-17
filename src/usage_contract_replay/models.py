"""Immutable domain records for contract replay.

Money is stored as integer cents on every record.  Unit quantities are stored
as ``Decimal`` so storage-style fractional usage can be handled without binary
floating point drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Optional, Tuple


RULES_VERSION = "2026-11-17.1"


@dataclass(frozen=True)
class PipelineResult:
    project: str
    data: Mapping[str, Any]


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    event_ref: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)
    severity: str = "error"

    @property
    def is_error(self) -> bool:
        return self.severity == "error"

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "event_ref": self.event_ref,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class BillingConfig:
    timezone_name: str = "UTC"
    period: str = "month"
    periods: Tuple[str, ...] = ()
    free_scope: str = "contract_version"
    tier_scope: str = "contract_version"

    def as_dict(self) -> dict:
        return {
            "timezone": self.timezone_name,
            "period": self.period,
            "periods": list(self.periods),
            "free_scope": self.free_scope,
            "tier_scope": self.tier_scope,
        }


@dataclass(frozen=True)
class UsageEvent:
    """A normalized event.

    ``identity`` is ``(account, event_id)``.  ``fingerprint`` is the canonical
    content hash used to distinguish idempotent duplicates from conflicts.
    ``event_id`` is either supplied by the producer or content-addressed.
    """

    event_id: str
    identity: str
    account: str
    metric: str
    at: datetime
    units: Decimal
    operation: str
    target_event_id: Optional[str]
    refund_amount_cents: Optional[int]
    refund_ratio: Optional[Decimal]
    received_at: Optional[datetime]
    fingerprint: str
    synthetic_id: bool
    period_key_override: Optional[str] = None
    pre_retracted: bool = False
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def sort_key(self) -> tuple:
        # Event time is primary.  Event id is the explicit tie break for the
        # same instant, so producer order never changes the result.
        return (self.at, self.event_id, self.account, self.metric, self.fingerprint)


@dataclass(frozen=True)
class Tier:
    up_to: Optional[Decimal]
    unit_price: Decimal

    def as_dict(self, index: int) -> dict:
        return {
            "tier": index + 1,
            "up_to": None if self.up_to is None else str(self.up_to),
            "unit_price": str(self.unit_price),
        }


@dataclass(frozen=True)
class MetricPricing:
    metric: str
    unit_price: Optional[Decimal]
    free_units: Decimal
    tiers: Tuple[Tier, ...]
    refund_cents: int = 0

    def as_dict(self) -> dict:
        return {
            "metric": self.metric,
            "unit_price": None if self.unit_price is None else str(self.unit_price),
            "free_units": str(self.free_units),
            "tiers": [tier.as_dict(i) for i, tier in enumerate(self.tiers)],
        }


@dataclass(frozen=True)
class ContractVersion:
    contract_id: str
    version: int
    account: Optional[str]
    effective_from: Optional[datetime]
    effective_to: Optional[datetime]
    pricing: Mapping[str, MetricPricing]
    default_pricing: Optional[MetricPricing]
    refund_cents: int
    fingerprint: str
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def key(self) -> str:
        owner = self.account if self.account is not None else "*"
        return "%s:%s:v%s" % (owner, self.contract_id, self.version)

    def pricing_for(self, metric: str) -> Optional[MetricPricing]:
        return self.pricing.get(metric, self.default_pricing)

    def as_lock(self) -> dict:
        return {
            "key": self.key,
            "contract_id": self.contract_id,
            "version": self.version,
            "account": self.account,
            "effective_from": (
                None if self.effective_from is None else self.effective_from.isoformat()
            ),
            "effective_to": (
                None if self.effective_to is None else self.effective_to.isoformat()
            ),
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class EffectiveUsage:
    event: UsageEvent
    units: Decimal
    reason: str
    adjustment_event_ids: Tuple[str, ...]
    pricing: MetricPricing
    contract: ContractVersion
    period: str
    late: bool


@dataclass(frozen=True)
class ParsedInput:
    events: Tuple[UsageEvent, ...]
    issues: Tuple[Issue, ...]
    duplicates: Tuple[Mapping[str, Any], ...]
    accounts: Tuple[str, ...]
    contracts_raw: Tuple[Mapping[str, Any], ...]
    billing: BillingConfig
    frozen_raw: Tuple[Mapping[str, Any], ...]
    rules_version: str
    currency: str
    on_error: str = "report"
    batch_ids: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Period:
    key: str
    start_local: datetime
    end_local: datetime
    start_utc: datetime
    end_utc: datetime

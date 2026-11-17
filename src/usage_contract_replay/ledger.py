"""Event-ledger semantics: retractions, corrections, restores and refunds.

The ledger converts the raw event stream into an effective set of usage facts.
It does not know about prices, periods or invoices; that separation keeps the
ordering rules small enough to reason about.

Ordering rules (stable by construction)
---------------------------------------
#. Usage lines are allocated in ``(event_time, event_id, account, metric,
   fingerprint)`` order.  Producer order and arrival order are irrelevant.
#. A retraction (``remove``) and a restore (``restore``) of the same target
   compete; the one with the greatest ``(event_time, event_id)`` wins.  A
   retraction therefore always eliminates the target from every replay in
   which no later restore exists, independent of when it arrived.
#. A correction (``set`` with ``target_event_id``) replaces the target's
   units.  Competing corrections use the same last-writer rule.
#. A ``set`` without a target is a signed manual adjustment line; it never
   touches an existing usage fact.
#. Refunds are processed after effective usage and corrections.  A refund's
   amount is capped at what remains refundable on its target; an over-refund
   is reported and the cap applied.  Refunds against retracted usage are
   rejected because the usage no longer exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Optional

from .models import Issue, UsageEvent
from .util import ROUND_HALF_UP


@dataclass(frozen=True)
class LedgerUsage:
    event: UsageEvent
    units: Decimal
    adjustments: tuple[str, ...] = ()


@dataclass(frozen=True)
class LedgerAdjustment:
    event: UsageEvent
    units: Decimal


@dataclass(frozen=True)
class LedgerRefund:
    event: UsageEvent
    target_identity: Optional[str]
    amount_cents: Optional[int]
    ratio: Optional[Decimal]
    adjustments: tuple[str, ...] = ()


@dataclass(frozen=True)
class LedgerResult:
    usages: tuple[LedgerUsage, ...]
    adjustments: tuple[LedgerAdjustment, ...]
    refunds: tuple[LedgerRefund, ...]
    retracted: tuple[UsageEvent, ...]
    issues: tuple[Issue, ...]

    def usage_by_identity(self) -> dict[str, LedgerUsage]:
        return {usage.event.identity: usage for usage in self.usages}


def _event_order(event: UsageEvent) -> tuple:
    return event.sort_key


def _last_operation(
    operations: Iterable[tuple[UsageEvent, str]],
) -> dict[str, tuple[UsageEvent, str]]:
    winners: dict[str, tuple[UsageEvent, str]] = {}
    for event, action in sorted(operations, key=lambda pair: _event_order(pair[0])):
        target = event.target_event_id or event.event_id
        key = "%s/%s" % (event.account, target)
        winners[key] = (event, action)
    return winners


def build_ledger(events: Iterable[UsageEvent]) -> LedgerResult:
    events = tuple(events)
    # Producer-marked tombstones are treated exactly like a retraction, so a
    # replayed feed that already contains the marker never bills the usage.
    pre_retracted = tuple(
        event for event in sorted(events, key=_event_order) if event.pre_retracted
    )
    ordered = tuple(
        sorted(
            (event for event in events if not event.pre_retracted),
            key=_event_order,
        )
    )
    by_identity = {event.identity: event for event in ordered}
    issues: list[Issue] = []

    retract_ops = []
    for event in ordered:
        if event.operation == "remove":
            retract_ops.append((event, "remove"))
        elif event.operation == "restore":
            retract_ops.append((event, "restore"))
    decisions = _last_operation(retract_ops)

    corrections: dict[str, Decimal] = {}
    correction_ids: dict[str, list[str]] = {}
    for event in ordered:
        if event.operation != "set" or not event.target_event_id:
            continue
        key = "%s/%s" % (event.account, event.target_event_id)
        if event.units < 0:
            issues.append(
                Issue(
                    code="invalid_correction",
                    message="a targeted correction must have non-negative units",
                    event_ref=event.identity,
                )
            )
            continue
        corrections[key] = event.units
        correction_ids.setdefault(key, []).append(event.event_id)

    retracted_identities: list[str] = []
    for key, (event, action) in decisions.items():
        if key not in by_identity:
            issues.append(
                Issue(
                    code="retract_target_missing",
                    message="retraction or restore targets an event that is not in the batch",
                    event_ref=event.identity,
                    details={"target": key},
                )
            )
            continue
        if action == "remove":
            retracted_identities.append(key)

    retracted_set = set(retracted_identities)
    usages: list[LedgerUsage] = []
    adjustments: list[LedgerAdjustment] = []
    retracted: list[UsageEvent] = []
    for event in ordered:
        if event.operation in ("remove", "restore"):
            continue
        if event.operation == "refund":
            continue
        if event.identity in retracted_set:
            retracted.append(event)
            continue
        if event.operation == "set" and event.target_event_id:
            # The correction is folded into the target usage below.
            continue
        if event.operation == "set":
            if event.units != 0:
                adjustments.append(LedgerAdjustment(event=event, units=event.units))
            continue
        units = corrections.get(event.identity, event.units)
        adjustments_for_event = tuple(
            sorted(correction_ids.get(event.identity, ()))
        )
        if units == 0 and not adjustments_for_event:
            # Zero usage carries no money but is still a valid statement; keep
            # it so "no activity" is distinguishable from "unreported".
            usages.append(LedgerUsage(event=event, units=Decimal(0)))
            continue
        usages.append(
            LedgerUsage(event=event, units=units, adjustments=adjustments_for_event)
        )

    for identity, correction_event_ids in sorted(correction_ids.items()):
        target = by_identity.get(identity)
        if target is None:
            issues.append(
                Issue(
                    code="correction_target_missing",
                    message="correction targets an event that is not in the batch",
                    event_ref=identity,
                    details={"corrections": correction_event_ids},
                )
            )
        elif identity in retracted_set:
            issues.append(
                Issue(
                    code="correction_of_retracted_event",
                    message="correction targets a retracted event and was ignored",
                    event_ref=identity,
                    details={"corrections": correction_event_ids},
                )
            )
        elif target.operation != "usage":
            issues.append(
                Issue(
                    code="correction_target_not_usage",
                    message="correction targets a non-usage event and was ignored",
                    event_ref=identity,
                )
            )

    usages.sort(key=lambda usage: _event_order(usage.event))
    adjustments.sort(key=lambda item: _event_order(item.event))

    refunds: list[LedgerRefund] = []
    for event in ordered:
        if event.operation != "refund":
            continue
        target_identity = None
        if event.target_event_id:
            target_identity = "%s/%s" % (event.account, event.target_event_id)
        if target_identity is not None and target_identity in retracted_set:
            # Retracted usage cannot be refunded: the charge no longer exists.
            # A target merely absent from *this* batch is not an error yet -
            # its period may be frozen, and the engine resolves that.
            issues.append(
                Issue(
                    code="refund_target_retracted",
                    message="refund target is retracted and cannot receive the refund",
                    event_ref=event.identity,
                    details={"target": target_identity},
                )
            )
            continue
        if target_identity is None and event.refund_ratio is not None:
            issues.append(
                Issue(
                    code="refund_ratio_without_target",
                    message="a ratio refund requires target_event_id",
                    event_ref=event.identity,
                )
            )
            continue
        refunds.append(
            LedgerRefund(
                event=event,
                target_identity=target_identity,
                amount_cents=event.refund_amount_cents,
                ratio=event.refund_ratio,
            )
        )
    refunds.sort(key=lambda refund: _event_order(refund.event))

    retracted.extend(pre_retracted)
    return LedgerResult(
        usages=tuple(usages),
        adjustments=tuple(adjustments),
        refunds=tuple(refunds),
        retracted=tuple(sorted(retracted, key=_event_order)),
        issues=tuple(issues),
    )


def round_ratio_cents(total_cents: int, ratio: Decimal) -> int:
    """Integer-cent refund for a ratio of a cent total (half away from zero)."""

    return int(
        (Decimal(total_cents) * ratio).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    )


def refund_amount_cents(
    refund: LedgerRefund,
    target_total_cents: int,
    already_refunded_cents: int,
) -> tuple[int, list[str]]:
    """Return the effective refund magnitude and adjustment codes.

    The cap is applied against the *unrefunded remainder of the target*, so
    gross - credits - refunds always reconciles exactly.
    """

    reasons: list[str] = []
    remaining = max(0, target_total_cents - already_refunded_cents)
    requested: int
    if refund.amount_cents is not None:
        requested = refund.amount_cents
    elif refund.ratio is not None:
        requested = round_ratio_cents(target_total_cents, refund.ratio)
        if requested == 0 and target_total_cents > 0 and refund.ratio > 0:
            reasons.append("refund_rounds_to_zero")
    else:
        requested = 0
    if requested > remaining:
        reasons.append("refund_capped_at_remaining")
        requested = remaining
    if requested == 0:
        reasons.append("no_refundable_balance")
    return requested, reasons

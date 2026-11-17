"""Deterministic replay billing engine.

Pipeline inside this module (no I/O):

1. parse and normalize the raw batch                       (``parser``)
2. resolve the event ledger (retract / correct / refund)   (``ledger``)
3. pin each effective usage line to a contract version and billing period
4. allocate free allowance, then tier segments, in stable event order
5. apply refunds against the exact billed amount of their target
6. round once per line and total the lines
7. compare against frozen snapshots, emitting revisions (``snapshots``)

Ordering is ``(event_time, event_id, account, metric, fingerprint)`` at every
stage.  Nothing consults dict order, arrival order or wall-clock time, so a
fixed rules version makes replay a pure function of the raw batch.

Which periods are recomputed
----------------------------
A period is *evidence* when the batch contains at least one event in it, or
when the caller lists it in ``billing.periods``.  Frozen receipts for periods
without evidence are echoed as ``frozen_not_recomputed`` and are never
compared, so replaying only February cannot manufacture a January revision.
A frozen period that does receive evidence is recomputed and, if it differs,
produces a revision - the frozen amount itself is never rewritten.

Refund attribution
------------------
A refund is a monetary credit issued when the refund event occurs, so it is
attributed to the *refund event's* period and records the ``target_period``
and ``target_event_id`` it reverses.  The amount is capped at the target
line's billed amount and at the target's unrefunded remainder, so gross minus
credits always explains net.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable, Mapping, Optional

from .contracts import (
    contract_lock_digest,
    effective_for,
    parse_contracts,
    sorted_versions,
    validate_interval_overlaps,
)
from .digests import invoice_digest, output_digest
from .ledger import LedgerAdjustment, build_ledger, refund_amount_cents
from .models import (
    BillingConfig,
    ContractVersion,
    EffectiveUsage,
    Issue,
    MetricPricing,
    ParsedInput,
    Period,
    UsageEvent,
)
from .parser import parse_payload
from .periods import period_bounds_for_key, period_for
from .util import (
    InputError,
    canonical_json,
    cents_from_decimal,
    decimal_text,
    isoformat_utc,
    money_text,
    number_from_decimal,
    sha256_hex,
    short_digest,
)

ZERO = Decimal(0)


@dataclass
class _Line:
    event: UsageEvent
    contract: ContractVersion
    pricing: MetricPricing
    period: str
    units: Decimal
    kind: str = "usage"
    free_units: Decimal = ZERO
    billable_units: Decimal = ZERO
    tier_units: list[tuple[int, Decimal, Decimal]] = field(default_factory=list)
    exact_amount: Decimal = ZERO
    reasons: list[str] = field(default_factory=list)
    source_event_ids: list[str] = field(default_factory=list)

    @property
    def order(self) -> tuple:
        return self.event.sort_key

    @property
    def amount_cents(self) -> int:
        return cents_from_decimal(self.exact_amount)


@dataclass(frozen=True)
class BillingDocument:
    status: str
    currency: str
    rules_version: str
    input_digest: str
    billing: BillingConfig
    periods: tuple[str, ...]
    invoices: tuple[dict, ...]
    frozen_invoices: tuple[dict, ...]
    revisions: tuple[dict, ...]
    audit: dict

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "currency": self.currency,
            "rules_version": self.rules_version,
            "input_digest": self.input_digest,
            "billing": self.billing.as_dict(),
            "periods": list(self.periods),
            "invoices": list(self.invoices),
            "frozen_invoices": list(self.frozen_invoices),
            "revisions": list(self.revisions),
            "audit": self.audit,
        }


def ITEM_ORDER(item: dict) -> tuple:
    """Stable display order: event time, kind, first source event, line id.

    Contract-level lines have no instant; they sort after every dated line so
    a credit derived from the whole invoice always reads last.
    """

    sources = item.get("source_event_ids") or ()
    occurred_at = item.get("occurred_at", "")
    return (
        1 if not occurred_at else 0,
        occurred_at,
        item.get("kind", ""),
        sources[0] if sources else "",
        item.get("line_id", ""),
    )


# ---------------------------------------------------------------------------
# resolution: ledger -> priced lines
# ---------------------------------------------------------------------------


def _scope_key(
    scope: str, account: str, metric: str, period: str, contract: ContractVersion
) -> tuple:
    if scope == "account":
        return (account, metric, period)
    if scope == "contract_family":
        return (account, metric, period, contract.contract_id)
    return (account, metric, period, contract.key)


def _resolve_usage(
    parsed: ParsedInput,
    versions: tuple[ContractVersion, ...],
    ledger,
    windows: Mapping[str, Period],
) -> tuple[
    list[EffectiveUsage],
    list[tuple[LedgerAdjustment, ContractVersion, MetricPricing, str]],
    list[Issue],
]:
    issues: list[Issue] = []
    effective: list[EffectiveUsage] = []
    for usage in ledger.usages:
        event = usage.event
        contract = effective_for(versions, event.account, event.at)
        if contract is None:
            issues.append(
                Issue(
                    code="missing_contract",
                    message="no contract version is effective for this event",
                    event_ref=event.identity,
                    details={"at": isoformat_utc(event.at)},
                )
            )
            continue
        pricing = contract.pricing_for(event.metric)
        if pricing is None:
            issues.append(
                Issue(
                    code="missing_metric_pricing",
                    message="contract version has no pricing for this metric",
                    event_ref=event.identity,
                    details={"metric": event.metric, "contract": contract.key},
                )
            )
            continue
        period_key = event.period_key_override or period_for(event.at, parsed.billing).key
        window = windows.get(period_key)
        if window is None:
            issues.append(
                Issue(
                    code="period_out_of_window",
                    message="event falls in a period that is not part of this run",
                    event_ref=event.identity,
                    details={"period": period_key},
                )
            )
            continue
        late = event.received_at is not None and event.received_at >= window.end_utc
        reasons = []
        if late:
            reasons.append("late_event")
        if usage.adjustments:
            reasons.append("corrected")
        effective.append(
            EffectiveUsage(
                event=event,
                units=usage.units,
                reason="+".join(reasons),
                adjustment_event_ids=usage.adjustments,
                pricing=pricing,
                contract=contract,
                period=period_key,
                late=late,
            )
        )

    adjustments: list[tuple[LedgerAdjustment, ContractVersion, MetricPricing, str]] = []
    for adjustment in ledger.adjustments:
        event = adjustment.event
        contract = effective_for(versions, event.account, event.at)
        if contract is None:
            issues.append(
                Issue(
                    code="missing_contract",
                    message="no contract version is effective for this adjustment",
                    event_ref=event.identity,
                )
            )
            continue
        pricing = contract.pricing_for(event.metric)
        if pricing is None:
            issues.append(
                Issue(
                    code="missing_metric_pricing",
                    message="contract version has no pricing for this adjustment metric",
                    event_ref=event.identity,
                )
            )
            continue
        period_key = event.period_key_override or period_for(event.at, parsed.billing).key
        if period_key not in windows:
            issues.append(
                Issue(
                    code="period_out_of_window",
                    message="adjustment falls in a period outside this run",
                    event_ref=event.identity,
                    details={"period": period_key},
                )
            )
            continue
        adjustments.append((adjustment, contract, pricing, period_key))
    return effective, adjustments, issues


# ---------------------------------------------------------------------------
# allowance and tiers
# ---------------------------------------------------------------------------


def _split_mixed_pool(
    group: list[_Line], signature, code: str
) -> tuple[list[list[_Line]], Optional[Issue]]:
    """Split a pool whose members disagree about the terms being pooled.

    A family/account-wide pool is only meaningful when every member shares the
    same allowance or tier table.  If a contract revision changed them
    mid-period, pooling would silently apply one version's terms to another
    version's usage, so the pool is split per contract version and the
    operator is told.
    """

    if len({signature(line) for line in group}) <= 1:
        return [group], None
    buckets: dict[str, list[_Line]] = {}
    for line in group:
        buckets.setdefault(line.contract.key, []).append(line)
    issue = Issue(
        code=code,
        message="pooled lines span contract versions with different terms; split by version",
        details={"contracts": sorted(buckets)},
        severity="warning",
    )
    return [buckets[key] for key in sorted(buckets)], issue


def _allocate_free(lines: list[_Line], free_units: Decimal) -> None:
    """Draw down the allowance in stable event order.

    Only forward usage and positive manual adjustments consume free units.
    Credits do not restore allowance, so the allowance stays a function of the
    contract version and the billable event set.
    """

    remaining = free_units
    for line in lines:
        if line.units <= 0:
            line.free_units = ZERO
            line.billable_units = line.units
            continue
        used = min(line.units, remaining)
        line.free_units = used
        line.billable_units = line.units - used
        remaining -= used
        if used > 0:
            line.reasons.append("free_allowance")


def _consume_forward(units, tiers, used, segments) -> Decimal:
    remaining = units
    for index, tier in enumerate(tiers):
        if remaining <= 0:
            break
        capacity = remaining if tier.up_to is None else tier.up_to - used[index]
        if capacity <= 0:
            continue
        take = min(remaining, capacity)
        used[index] += take
        remaining -= take
        segments.append((index, take, tier.unit_price))
    return remaining


def _consume_backward(units, tiers, used, segments) -> Decimal:
    """Consume negative units from the most recently used tier first."""

    remaining = units
    for index in range(len(tiers) - 1, -1, -1):
        if remaining <= 0:
            break
        available = used[index]
        if available <= 0:
            continue
        take = min(remaining, available)
        used[index] -= take
        remaining -= take
        segments.append((index, -take, tiers[index].unit_price))
    return remaining


def _allocate_tiers(lines: list[_Line], tiers) -> None:
    used = [ZERO for _ in tiers]
    for line in lines:
        segments: list[tuple[int, Decimal, Decimal]] = []
        if line.billable_units >= 0:
            leftover = _consume_forward(line.billable_units, tiers, used, segments)
            if leftover > 0:
                line.reasons.append("usage_above_top_tier")
                line.billable_units -= leftover
        else:
            leftover = _consume_backward(-line.billable_units, tiers, used, segments)
            if leftover > 0:
                line.reasons.append("credit_exceeds_period_usage")
                line.billable_units = -(line.billable_units + leftover)
        line.tier_units = segments
        line.exact_amount = sum((units * price for _index, units, price in segments), ZERO)


def _meter_lines(
    parsed: ParsedInput,
    versions: tuple[ContractVersion, ...],
    ledger,
    windows: Mapping[str, Period],
) -> tuple[list[_Line], list[Issue]]:
    issues: list[Issue] = list(ledger.issues) + list(parsed.issues)
    effective, adjustments, resolve_issues = _resolve_usage(
        parsed, versions, ledger, windows
    )
    issues.extend(resolve_issues)

    lines: list[_Line] = [
        _Line(
            event=item.event,
            contract=item.contract,
            pricing=item.pricing,
            period=item.period,
            units=item.units,
            source_event_ids=[item.event.event_id, *item.adjustment_event_ids],
            reasons=[reason for reason in item.reason.split("+") if reason],
        )
        for item in effective
    ]
    lines.extend(
        _Line(
            event=adjustment.event,
            contract=contract,
            pricing=pricing,
            period=period_key,
            units=adjustment.units,
            kind="adjustment",
            source_event_ids=[adjustment.event.event_id],
            reasons=["manual_adjustment"],
        )
        for adjustment, contract, pricing, period_key in adjustments
    )
    lines.sort(key=lambda line: line.order)

    billing = parsed.billing
    free_groups: dict[tuple, list[_Line]] = {}
    for line in lines:
        free_groups.setdefault(
            _scope_key(
                billing.free_scope,
                line.event.account,
                line.event.metric,
                line.period,
                line.contract,
            ),
            [],
        ).append(line)
    for key in sorted(free_groups, key=canonical_json):
        groups, split_issue = _split_mixed_pool(
            free_groups[key], lambda line: line.pricing.free_units, "mixed_free_allowance"
        )
        if split_issue is not None:
            issues.append(split_issue)
        for group in groups:
            _allocate_free(group, group[0].pricing.free_units)

    tier_groups: dict[tuple, list[_Line]] = {}
    for line in lines:
        if line.billable_units == 0 and line.units == 0:
            continue
        tier_groups.setdefault(
            _scope_key(
                billing.tier_scope,
                line.event.account,
                line.event.metric,
                line.period,
                line.contract,
            ),
            [],
        ).append(line)
    for key in sorted(tier_groups, key=canonical_json):
        groups, split_issue = _split_mixed_pool(
            tier_groups[key],
            lambda line: (line.pricing.tiers, line.pricing.unit_price),
            "mixed_tier_table",
        )
        if split_issue is not None:
            issues.append(split_issue)
        for group in groups:
            tiers = group[0].pricing.tiers
            if tiers:
                _allocate_tiers(group, tiers)
            else:
                price = group[0].pricing.unit_price or ZERO
                for line in group:
                    line.reasons.append("flat_price")
                    line.exact_amount = line.billable_units * price

    for line in lines:
        if "usage_above_top_tier" in line.reasons:
            issues.append(
                Issue(
                    code="usage_above_top_tier",
                    message="usage exceeded the highest priced tier and the excess was not billed",
                    event_ref=line.event.identity,
                    details={"units": decimal_text(line.billable_units)},
                )
            )
        if "credit_exceeds_period_usage" in line.reasons:
            issues.append(
                Issue(
                    code="credit_exceeds_period_usage",
                    message="credit exceeded usage available in its tier pool and was clamped",
                    event_ref=line.event.identity,
                )
            )
    return lines, issues


# ---------------------------------------------------------------------------
# line items and invoices
# ---------------------------------------------------------------------------


def _line_item(line: _Line, currency: str) -> dict:
    amount_cents = line.amount_cents
    return {
        "line_id": "ln_"
        + short_digest(
            [
                line.period,
                line.event.account,
                line.event.metric,
                line.kind,
                line.source_event_ids,
                line.contract.key,
                amount_cents,
            ],
            length=24,
        ),
        "period": line.period,
        "account": line.event.account,
        "metric": line.event.metric,
        "kind": line.kind,
        "occurred_at": isoformat_utc(line.event.at),
        "units": decimal_text(line.units),
        "free_units": decimal_text(line.free_units),
        "billable_units": decimal_text(line.billable_units),
        "unit_price": (
            decimal_text(line.pricing.unit_price)
            if not line.pricing.tiers and line.pricing.unit_price is not None
            else None
        ),
        "contract_version": line.contract.key,
        "contract_version_number": line.contract.version,
        "contract_fingerprint": line.contract.fingerprint,
        "tiers": [
            {
                "tier": index + 1,
                "units": decimal_text(units),
                "unit_price": decimal_text(price),
                "amount": money_text(cents_from_decimal(units * price)),
            }
            for index, units, price in line.tier_units
        ],
        "amount": money_text(amount_cents),
        "amount_cents": amount_cents,
        "currency": currency,
        "source_event_ids": list(line.source_event_ids),
        "adjustment_reasons": list(dict.fromkeys(line.reasons)),
    }


def _refund_item(
    *,
    event: UsageEvent,
    period: str,
    account: str,
    metric: str,
    amount_cents: int,
    currency: str,
    contract_key: str,
    source_ids: list[str],
    reasons: list[str],
    target_event_id: Optional[str],
    target_period: Optional[str],
) -> dict:
    return {
        "line_id": "ln_"
        + short_digest([period, account, "refund", event.identity, amount_cents], length=24),
        "period": period,
        "account": account,
        "metric": metric,
        "kind": "refund" if amount_cents else "refund_zero",
        "occurred_at": isoformat_utc(event.at),
        "units": "0",
        "free_units": "0",
        "billable_units": "0",
        "unit_price": None,
        "contract_version": contract_key,
        "contract_version_number": None,
        "contract_fingerprint": "",
        "tiers": [],
        "amount": money_text(-amount_cents),
        "amount_cents": -amount_cents,
        "currency": currency,
        "source_event_ids": source_ids,
        "target_event_id": target_event_id,
        "target_period": target_period,
        "adjustment_reasons": reasons,
    }


def _apply_refunds(
    parsed: ParsedInput,
    ledger,
    usage_lines: dict[str, _Line],
    windows: Mapping[str, Period],
    frozen_targets: Mapping[tuple[str, str], Mapping[str, Any]],
) -> tuple[list[dict], list[Issue]]:
    items: list[dict] = []
    issues: list[Issue] = []
    refunded_by_target: dict[str, int] = {}
    refunded_by_period: dict[tuple[str, str], int] = {}
    for refund in ledger.refunds:
        event = refund.event
        event_period = event.period_key_override or period_for(event.at, parsed.billing).key
        if event_period not in windows:
            issues.append(
                Issue(
                    code="refund_period_out_of_window",
                    message="refund falls in a period that is not part of this run",
                    event_ref=event.identity,
                    details={"period": event_period},
                )
            )
            continue
        if refund.target_identity is not None:
            target = usage_lines.get(refund.target_identity)
            if target is not None:
                target_cents = target.amount_cents
                target_period = target.period
                account = target.event.account
                metric = target.event.metric
                contract_key = target.contract.key
                source_id = target.event.event_id
                reasons_prefix: list[str] = []
            else:
                # The target's period is frozen (or absent).  Recover its
                # billed amount from the frozen receipt so the credit stays
                # bounded by what was actually invoiced.
                recovered = frozen_targets.get(
                    (event.account, refund.target_identity.split("/", 1)[1])
                )
                if recovered is None:
                    issues.append(
                        Issue(
                            code="refund_target_not_billable",
                            message="refund target is not billed in this run or in frozen history",
                            event_ref=event.identity,
                            details={"target": refund.target_identity},
                        )
                    )
                    continue
                target_cents = int(recovered["amount_cents"])
                target_period = str(recovered["period"])
                account = event.account
                metric = event.metric
                contract_key = ""
                source_id = refund.target_identity.split("/", 1)[1]
                reasons_prefix = ["target_from_frozen_history"]
            already = refunded_by_target.get(refund.target_identity, 0)
            amount, reasons = refund_amount_cents(refund, target_cents, already)
            reasons = reasons_prefix + reasons
            refunded_by_target[refund.target_identity] = already + amount
            period_key = (event.account, event_period)
            refunded_by_period[period_key] = refunded_by_period.get(period_key, 0) + amount
            source_ids = [event.event_id, source_id]
            if target_period != event_period:
                reasons.append("refund_cross_period")
        else:
            # Target-less refund: cap against the gross of the refund period.
            candidates = [
                line
                for line in usage_lines.values()
                if line.event.account == event.account and line.period == event_period
            ]
            gross = sum(max(0, line.amount_cents) for line in candidates)
            already = refunded_by_period.get((event.account, event_period), 0)
            # Cap against everything already credited in this period, targeted
            # refunds included, so credits can never exceed the period's usage.
            remaining = max(0, gross - already)
            requested = refund.amount_cents or 0
            amount = min(requested, remaining)
            reasons = []
            if amount < requested:
                reasons.append("refund_capped_at_remaining")
            if amount <= 0:
                reasons.append("no_refundable_balance")
            refunded_by_period[(event.account, event_period)] = already + amount
            target_period = None
            account = event.account
            metric = event.metric
            contract_key = ""
            source_ids = [event.event_id]
        items.append(
            _refund_item(
                event=event,
                period=event_period,
                account=account,
                metric=metric,
                amount_cents=amount,
                currency=parsed.currency,
                contract_key=contract_key,
                source_ids=source_ids,
                reasons=reasons or ["refund"],
                target_event_id=(
                    None
                    if refund.target_identity is None
                    else refund.target_identity.split("/", 1)[1]
                ),
                target_period=target_period,
            )
        )
    return items, issues


def _apply_contract_refunds(
    versions: Iterable[ContractVersion],
    invoices: list[dict],
    currency: str,
) -> list[Issue]:
    """Apply each contract-level refund once, to its earliest affected invoice.

    This is the legacy whole-contract credit (``contract.refund``), distinct
    from event refunds.  It is capped at the invoice net so an invoice can
    never go negative because of it, and it is reported if there is nothing to
    reduce.

    Placement is fully determined by the batch, never by which periods happen
    to be frozen: the credit always lands on the earliest affected invoice.
    Otherwise freezing a period would move the credit to another period and
    manufacture a revision on the next replay.  If the frozen promise predates
    the credit, the resulting difference shows up as a revision, which is the
    correct outcome.
    """

    issues: list[Issue] = []
    for version in sorted_versions(versions):
        if version.refund_cents <= 0:
            continue
        scoped = sorted(
            (
                invoice
                for invoice in invoices
                if any(item["contract_version"] == version.key for item in invoice["items"])
            ),
            key=lambda invoice: (invoice["period"], invoice["account"]),
        )
        if not scoped:
            issues.append(
                Issue(
                    code="contract_refund_unapplied",
                    message="contract refund has no invoice to reduce",
                    details={"contract": version.key},
                    severity="warning",
                )
            )
            continue
        invoice = scoped[0]
        remaining = max(0, invoice["net_cents"])
        amount = min(version.refund_cents, remaining)
        if amount <= 0:
            issues.append(
                Issue(
                    code="contract_refund_unapplied",
                    message="contract refund had no positive balance to reduce",
                    details={"contract": version.key, "period": invoice["period"]},
                    severity="warning",
                )
            )
            continue
        invoice["items"].append(
            {
                "line_id": "ln_"
                + short_digest([version.key, invoice["period"], invoice["account"], amount]),
                "period": invoice["period"],
                "account": invoice["account"],
                "metric": "",
                "kind": "contract_refund",
                "occurred_at": "",
                "units": "0",
                "free_units": "0",
                "billable_units": "0",
                "unit_price": None,
                "contract_version": version.key,
                "contract_version_number": version.version,
                "contract_fingerprint": version.fingerprint,
                "tiers": [],
                "amount": money_text(-amount),
                "amount_cents": -amount,
                "currency": currency,
                "source_event_ids": [],
                "target_event_id": None,
                "target_period": None,
                "adjustment_reasons": ["contract_refund"],
            }
        )
    return issues


def _totals(items: list[dict]) -> dict:
    gross = sum(max(0, item["amount_cents"]) for item in items)
    credit = sum(min(0, item["amount_cents"]) for item in items)
    refund_cents = -sum(
        item["amount_cents"]
        for item in items
        if item["kind"] in ("refund", "contract_refund")
    )
    net = gross + credit
    return {
        "gross": money_text(gross),
        "gross_cents": gross,
        "credits": money_text(credit),
        "credit_cents": credit,
        "refund": money_text(refund_cents),
        "refund_cents": refund_cents,
        "net": money_text(net),
        "net_cents": net,
    }


def _invoice(
    account: str, period: str, items: list[dict], currency: str, rules_version: str
) -> dict:
    ordered = sorted(items, key=ITEM_ORDER)
    return {
        "invoice_id": "inv_" + short_digest([account, period], length=20),
        "account": account,
        "period": period,
        "status": "open" if ordered else "empty",
        "currency": currency,
        "items": ordered,
        **_totals(ordered),
        "rules_version": rules_version,
        "digest": invoice_digest(account, period, ordered, currency, rules_version),
    }


def _refresh_invoice(invoice: dict) -> None:
    ordered = sorted(invoice["items"], key=ITEM_ORDER)
    invoice["items"] = ordered
    invoice.update(_totals(ordered))
    invoice["status"] = "open" if ordered else "empty"
    invoice["digest"] = invoice_digest(
        invoice["account"],
        invoice["period"],
        ordered,
        invoice["currency"],
        invoice.get("rules_version", ""),
    )


def _build_invoices(
    parsed: ParsedInput,
    lines: list[_Line],
    refund_items: list[dict],
    recompute_periods: Iterable[str],
    accounts: Iterable[str],
) -> tuple[list[dict], list[dict]]:
    items_by_slice: dict[tuple[str, str], list[dict]] = {}
    for line in sorted(lines, key=lambda item: (item.period, item.order)):
        items_by_slice.setdefault((line.period, line.event.account), []).append(
            _line_item(line, parsed.currency)
        )
    for item in refund_items:
        items_by_slice.setdefault((item["period"], item["account"]), []).append(item)
    for key in sorted(items_by_slice):
        items_by_slice[key].sort(key=ITEM_ORDER)

    # A configured period is an explicit statement that the batch is the full
    # evidence for it, so every known account gets an invoice - even an empty
    # one, which makes "no activity" auditable.
    configured = set(parsed.billing.periods)

    invoices: list[dict] = []
    for account in sorted(set(accounts)):
        for period in sorted(recompute_periods):
            items = items_by_slice.get((period, account))
            if items is None:
                if period in configured:
                    invoices.append(
                        _invoice(account, period, [], parsed.currency, parsed.rules_version)
                    )
                continue
            invoices.append(
                _invoice(account, period, items, parsed.currency, parsed.rules_version)
            )

    # Rounding audit: per-line exact arithmetic vs the rounded line totals.
    rounding: list[dict] = []
    by_slice: dict[tuple[str, str], list[_Line]] = {}
    for line in lines:
        by_slice.setdefault((line.period, line.event.account), []).append(line)
    for (period, account), group in sorted(by_slice.items()):
        line_total = sum(line.amount_cents for line in group)
        exact_cents = cents_from_decimal(sum((line.exact_amount for line in group), ZERO))
        if line_total != exact_cents:
            rounding.append(
                {
                    "account": account,
                    "period": period,
                    "line_total_cents": line_total,
                    "exact_total_cents": exact_cents,
                    "rounding_delta_cents": line_total - exact_cents,
                }
            )
    return invoices, rounding


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------


def _audit_events(
    parsed: ParsedInput, ledger, effective: Iterable[EffectiveUsage]
) -> list[dict]:
    effective_map = {item.event.identity: item for item in effective}
    retracted = {event.identity for event in ledger.retracted}
    records = []
    for event in sorted(parsed.events, key=lambda item: item.sort_key):
        if event.identity in effective_map:
            item = effective_map[event.identity]
            status = "billed"
            reason = item.reason or "ok"
            units = decimal_text(item.units)
        elif event.identity in retracted:
            status = "retracted"
            reason = "retracted"
            units = "0"
        elif event.operation == "refund":
            status = "refund"
            reason = "refund"
            units = "0"
        elif event.operation in ("remove", "restore"):
            status = "control"
            reason = event.operation
            units = "0"
        elif event.operation == "set" and event.target_event_id:
            status = "control"
            reason = "correction"
            units = decimal_text(event.units)
        elif event.operation == "set":
            status = "adjustment"
            reason = "manual_adjustment"
            units = decimal_text(event.units)
        else:
            status = "unbilled"
            reason = "excluded"
            units = "0"
        records.append(
            {
                "account": event.account,
                "event_id": event.event_id,
                "identity": event.identity,
                "operation": event.operation,
                "at": isoformat_utc(event.at),
                "received_at": (
                    None if event.received_at is None else isoformat_utc(event.received_at)
                ),
                "metric": event.metric,
                "units": units,
                "status": status,
                "reason": reason,
                "fingerprint": event.fingerprint,
                "synthetic_id": event.synthetic_id,
            }
        )
    return records


def _issue_counts(issues: Iterable[Issue]) -> dict:
    counts: dict[str, int] = {}
    for issue in issues:
        counts[issue.code] = counts.get(issue.code, 0) + 1
    return {key: counts[key] for key in sorted(counts)}


def _check_invariants(
    invoices: list[dict], frozen_invoices: list[dict], revisions: list[dict]
) -> dict:
    """Structural guarantees the output must satisfy.

    These are cheap and local; they catch the failure modes that "it ran" does
    not: an invoice whose net stops reconciling, a revision that rewrites the
    frozen amount, a delta that does not equal the difference, or duplicate
    line ids across the export.
    """

    reconciled = all(
        invoice["gross_cents"] + invoice["credit_cents"] == invoice["net_cents"]
        for invoice in invoices
    )
    refunds_are_negative = all(
        item["amount_cents"] <= 0
        for invoice in invoices
        for item in invoice["items"]
        if item["kind"] in ("refund", "refund_zero", "contract_refund")
    )
    frozen_by_key = {
        (invoice["account"], invoice["period"]): invoice["net_cents"]
        for invoice in frozen_invoices
    }
    revisions_keep_frozen = all(
        revision["frozen_net_cents"] is None
        or frozen_by_key.get((revision["scope"], revision["period"]))
        in (None, revision["frozen_net_cents"])
        for revision in revisions
    )
    revision_delta_reconciles = all(
        revision["delta_cents"] is None
        or revision["recomputed_net_cents"] - revision["frozen_net_cents"]
        == revision["delta_cents"]
        for revision in revisions
    )
    line_ids = [
        item["line_id"]
        for invoice in invoices + frozen_invoices
        for item in invoice["items"]
        if item.get("line_id")
    ]
    # Every refund must be traceable to billed usage, including usage whose
    # period is frozen.  Sum(cents refunded) <= sum(gross billed).
    # Deduplicate by line id: a frozen period echoes the line items it was
    # closed with, and a recomputed period repeats them only in its own
    # revision view.  Counting distinct lines makes the invariant independent
    # of which view an item appears in.
    unique_items = {}
    for invoice in invoices + frozen_invoices:
        for item in invoice["items"]:
            unique_items.setdefault(item.get("line_id"), item)
    total_refunded = sum(
        -item.get("amount_cents", 0)
        for item in unique_items.values()
        if item.get("kind") in ("refund", "contract_refund")
    )
    total_gross = sum(
        max(0, item.get("amount_cents", 0)) for item in unique_items.values()
    )
    refunds_within_billed_usage = total_refunded <= total_gross
    return {
        "invoice_net_reconciles": reconciled,
        "refunds_are_negative": refunds_are_negative,
        "refunds_within_billed_usage": refunds_within_billed_usage,
        "revision_preserves_frozen_amount": revisions_keep_frozen,
        "revision_delta_reconciles": revision_delta_reconciles,
        "line_ids_unique": len(line_ids) == len(set(line_ids)),
    }


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


def _evidence_periods(parsed: ParsedInput) -> set[str]:
    """Periods the batch claims to be the full evidence for.

    ``billing.periods`` is authoritative when present: it is the closed set
    the operator asked to settle, and an event outside it is reported rather
    than billed into a period nobody asked for.  Without it the run covers
    exactly the periods its events touch.
    """

    if parsed.billing.periods:
        return set(parsed.billing.periods)
    return {
        event.period_key_override or period_for(event.at, parsed.billing).key
        for event in parsed.events
    }


def _period_windows(parsed: ParsedInput) -> dict[str, Period]:
    """Every period the run may need to locate an event in.

    The evidence periods are guaranteed to exist so an out-of-window event is
    detected as ``period_out_of_window`` rather than looking like an unknown
    period.
    """

    keys: set[str] = _evidence_periods(parsed)
    for frozen in parsed.frozen_raw:
        period = frozen.get("period")
        if isinstance(period, str) and period:
            keys.add(period)
    return {
        key: period_bounds_for_key(
            parsed.billing.period, key, parsed.billing.timezone_name
        )
        for key in sorted(keys)
    }


def build_document(payload: Mapping[str, Any]) -> BillingDocument:
    parsed = parse_payload(payload)
    input_digest = sha256_hex(
        {
            "rules_version": parsed.rules_version,
            "currency": parsed.currency,
            "billing": parsed.billing.as_dict(),
            "on_error": parsed.on_error,
            "events": [canonical_json(event.raw) for event in parsed.events],
            "contracts": sorted(canonical_json(raw) for raw in parsed.contracts_raw),
            "frozen": sorted(canonical_json(raw) for raw in parsed.frozen_raw),
        }
    )

    versions = parse_contracts(parsed.contracts_raw)
    validate_interval_overlaps(versions)
    expected_lock = payload.get("expected_contract_lock_digest")
    actual_lock = contract_lock_digest(versions)
    if expected_lock is not None and str(expected_lock) != actual_lock:
        raise InputError(
            "contract lock digest mismatch: expected %s, got %s"
            % (expected_lock, actual_lock)
        )

    evidence = _evidence_periods(parsed)
    windows = _period_windows(parsed)
    ledger = build_ledger(parsed.events)
    lines, issues = _meter_lines(parsed, versions, ledger, windows)

    from .snapshots import frozen_target_amounts, normalize_frozen

    frozen_records, frozen_issues = normalize_frozen(
        parsed.frozen_raw, parsed.currency
    )
    usage_lines = {line.event.identity: line for line in lines if line.kind == "usage"}
    refund_items, refund_issues = _apply_refunds(
        parsed, ledger, usage_lines, windows, frozen_target_amounts(frozen_records)
    )
    issues.extend(refund_issues)
    issues.extend(frozen_issues)

    known_accounts = sorted(
        set(parsed.accounts)
        | {version.account for version in versions if version.account is not None}
    )
    invoices, rounding = _build_invoices(
        parsed, lines, refund_items, evidence, known_accounts
    )
    issues.extend(_apply_contract_refunds(versions, invoices, parsed.currency))
    for invoice in invoices:
        _refresh_invoice(invoice)

    from .snapshots import apply_freezes

    invoices, frozen_invoices, revisions, freeze_issues, freeze_audit = apply_freezes(
        parsed, invoices, evidence, frozen_records
    )
    issues.extend(freeze_issues)
    invoices.sort(key=lambda invoice: (invoice["period"], invoice["account"]))
    revisions.sort(key=lambda revision: (revision["period"], revision["scope"]))

    errors = [issue for issue in issues if issue.is_error]
    if errors and parsed.on_error == "fail":
        codes = _issue_counts(errors)
        raise InputError(
            "batch rejected by on_error=fail: %s"
            % ", ".join("%s=%d" % item for item in codes.items())
        )
    if errors:
        status = (
            "partial"
            if any(invoice["items"] for invoice in invoices) or revisions
            else "failed"
        )
    else:
        status = "ok"

    event_audit = _audit_events(parsed, ledger, _effective_lines(lines))
    audit = {
        "rules_version": parsed.rules_version,
        "currency": parsed.currency,
        "on_error": parsed.on_error,
        "contract_lock": [version.as_lock() for version in sorted_versions(versions)],
        "contract_lock_digest": actual_lock,
        "events": event_audit,
        "duplicates": list(parsed.duplicates),
        "batches": list(parsed.batch_ids),
        # Late rows are billed into their event period but are also exposed
        # here so an operator can see what arrived after the fact.
        "late_events": [
            entry["identity"] for entry in event_audit if entry["reason"] == "late_event"
        ],
        "rounding": rounding,
        "scopes": {"free": parsed.billing.free_scope, "tiers": parsed.billing.tier_scope},
        **freeze_audit,
    }
    audit["invariants"] = _check_invariants(invoices, frozen_invoices, revisions)
    if not all(audit["invariants"].values()):
        status = "partial"
    audit["issues"] = [
        issue.as_dict()
        for issue in sorted(issues, key=lambda item: (item.code, item.event_ref, item.message))
    ]
    audit["issue_counts"] = _issue_counts(issues)
    audit["issue_error_count"] = len(errors)
    audit["invoice_count"] = len(invoices)
    audit["frozen_count"] = len(frozen_invoices)
    audit["revision_count"] = len(revisions)
    audit["output_digest"] = output_digest(
        {
            "status": status,
            "currency": parsed.currency,
            "rules_version": parsed.rules_version,
            "invoices": invoices,
            "frozen_invoices": frozen_invoices,
            "revisions": revisions,
        }
    )

    return BillingDocument(
        status=status,
        currency=parsed.currency,
        rules_version=parsed.rules_version,
        input_digest=input_digest,
        billing=parsed.billing,
        periods=tuple(sorted(windows)),
        invoices=tuple(invoices),
        frozen_invoices=tuple(frozen_invoices),
        revisions=tuple(revisions),
        audit=audit,
    )


def _effective_lines(lines: Iterable[_Line]) -> list[EffectiveUsage]:
    """Rebuild the audit view of billed usage from the resolved lines."""

    effective = []
    for line in lines:
        if line.kind != "usage":
            continue
        reasons = [
            reason for reason in line.reasons if reason in ("late_event", "corrected")
        ]
        effective.append(
            EffectiveUsage(
                event=line.event,
                units=line.units,
                reason="+".join(reasons),
                adjustment_event_ids=tuple(line.source_event_ids[1:]),
                pricing=line.pricing,
                contract=line.contract,
                period=line.period,
                late="late_event" in reasons,
            )
        )
    return effective


def calculate(rows, payload):
    """Legacy domain entry point.

    Prefer :func:`usage_contract_replay.pipeline.run`.  This wrapper delegates
    to the same document builder; the flat summary keys live in the pipeline.
    """

    if not isinstance(payload, Mapping):
        raise InputError("payload must be an object")
    document = build_document(payload).as_dict()
    invoices = document["invoices"]
    net_cents = sum(invoice["net_cents"] for invoice in invoices)
    return {
        **document,
        "account": (
            invoices[0]["account"] if invoices else (rows[0].get("account") if rows else None)
        ),
        "amount": number_from_decimal(Decimal(net_cents) / 100),
        "amount_cents": net_cents,
        "event_count": len(document["audit"]["events"]),
    }

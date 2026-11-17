"""Contract version parsing and half-open effective interval selection.

Every version records its own start and (optionally) end.  A version is the
billing authority for an event when ``effective_from <= at < effective_to``.
Closed versions are immutable input: changing the terms of a version that has
already produced a frozen invoice creates a new version, never an edit.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping, Optional

from .models import ContractVersion, MetricPricing, Tier
from .util import (
    InputError,
    decimal_text,
    parse_non_negative_decimal,
    parse_timestamp,
    present,
    sha256_hex,
)

DEFAULT_METRIC = "__default__"


def _optional_instant(value: Any, field: str) -> Optional[datetime]:
    if value is None:
        return None
    return parse_timestamp(value, field=field)


def _parse_tiers(raw: Any, metric: str) -> tuple[Tier, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not raw:
        raise InputError("contract %s.tiers must be a non-empty list" % metric)
    tiers = []
    previous: Optional[Decimal] = None
    for index, item in enumerate(raw):
        label = "contract %s.tiers[%d]" % (metric, index)
        if not isinstance(item, Mapping):
            raise InputError("%s must be an object" % label)
        if "up_to" not in item or "unit_price" not in item:
            raise InputError("%s requires up_to and unit_price" % label)
        up_to_raw = item["up_to"]
        if up_to_raw is None:
            up_to = None
        else:
            up_to = parse_non_negative_decimal(up_to_raw, field=label + ".up_to")
            if up_to == 0:
                raise InputError("%s.up_to must be positive" % label)
            if previous is not None and up_to <= previous:
                raise InputError("%s.up_to must strictly increase" % label)
        if up_to is None and index != len(raw) - 1:
            raise InputError("%s is unbounded but not the last tier" % label)
        previous = up_to
        price = parse_non_negative_decimal(item["unit_price"], field=label + ".unit_price")
        tiers.append(Tier(up_to=up_to, unit_price=price))
    if tiers and tiers[-1].up_to is not None:
        # This is allowed; consumption above the final bound uses no price.
        pass
    return tuple(tiers)


def _parse_pricing(raw: Mapping[str, Any], account: Optional[str], index: int) -> MetricPricing:
    metric = str(raw.get("metric", DEFAULT_METRIC))
    label = "contract[%d].%s" % (index, metric)
    has_price = present(raw, ("unit_price",))
    tiers = _parse_tiers(raw.get("tiers"), metric)
    if has_price and tiers:
        raise InputError("%s cannot define both unit_price and tiers" % label)
    unit_price = None
    if has_price:
        unit_price = parse_non_negative_decimal(raw["unit_price"], field=label + ".unit_price")
    elif not tiers:
        raise InputError("%s requires unit_price or tiers" % label)
    free = parse_non_negative_decimal(raw.get("free", 0), field=label + ".free")
    refund = parse_non_negative_decimal(
        raw.get("refund", raw.get("refund_amount", 0)), field=label + ".refund"
    )
    return MetricPricing(
        metric=metric,
        unit_price=unit_price,
        free_units=free,
        tiers=tiers,
        refund_cents=int((refund * 100).to_integral_value()),
    )


def _normalize_pricing_root(raw: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return pricing entries with an explicit ``metric`` key.

    Accepted shapes::

        {"unit_price": 1}                                  single default metric
        {"free": 5, "unit_price": 1}                       same, with allowance
        {"pricing": {"api": {...}, "storage": {...}}}      metric -> terms
        {"pricing": [{"metric": "api", ...}, ...]}         list form
        {"metrics": {...}}                                 alias for pricing
    """

    pricing = raw.get("pricing", raw.get("metrics"))
    if pricing is None:
        return [dict(raw)]
    if isinstance(pricing, Mapping):
        if present(pricing, ("unit_price", "tiers")):
            item = dict(pricing)
            item.update(
                {key: raw[key] for key in ("free", "refund", "refund_amount") if key in raw}
            )
            return [item]
        items = []
        for metric, terms in pricing.items():
            if not isinstance(terms, Mapping):
                raise InputError("contract pricing for metric %r must be an object" % (metric,))
            item = dict(terms)
            item.setdefault("metric", str(metric))
            items.append(item)
        return items
    if isinstance(pricing, list):
        items = []
        for index, terms in enumerate(pricing):
            if not isinstance(terms, Mapping):
                raise InputError("contract pricing[%d] must be an object" % index)
            items.append(dict(terms))
        return items
    raise InputError("contract pricing must be an object or list")


def parse_contracts(raw_contracts: Iterable[Any]) -> tuple[ContractVersion, ...]:
    versions: list[ContractVersion] = []
    seen: set[tuple[str, int]] = set()
    for index, raw in enumerate(raw_contracts):
        if not isinstance(raw, Mapping):
            raise InputError("contract[%d] must be an object" % index)
        contract_id = str(raw.get("id", "contract"))
        version_raw = raw.get("version", 1)
        if isinstance(version_raw, bool) or not isinstance(version_raw, (int, str)):
            raise InputError("contract[%d].version must be an integer" % index)
        try:
            version = int(version_raw)
        except ValueError as exc:
            raise InputError("contract[%d].version must be an integer" % index) from exc
        if version < 1:
            raise InputError("contract[%d].version must be >= 1" % index)
        account = raw.get("account")
        account = None if account is None else str(account)
        key = (contract_id, version)
        if key in seen:
            raise InputError("duplicate contract version %s v%s" % key)
        seen.add(key)
        effective_from = _optional_instant(raw.get("effective_from"), "effective_from")
        effective_to = _optional_instant(raw.get("effective_to"), "effective_to")
        if (
            effective_from is not None
            and effective_to is not None
            and effective_to <= effective_from
        ):
            raise InputError("contract %s v%s has an empty effective interval" % key)
        pricing = {}
        default_pricing = None
        for pricing_raw in _normalize_pricing_root(raw):
            item = _parse_pricing(pricing_raw, account, index)
            if item.metric == DEFAULT_METRIC:
                default_pricing = item
            else:
                if item.metric in pricing:
                    raise InputError(
                        "contract %s v%s repeats metric %r" % (contract_id, version, item.metric)
                    )
                pricing[item.metric] = item
        refund = parse_non_negative_decimal(
            raw.get("refund", raw.get("refund_amount", 0)), field="contract[%d].refund" % index
        )
        fingerprint = sha256_hex(
            {
                "id": contract_id,
                "version": version,
                "account": account,
                "effective_from": None if effective_from is None else effective_from.isoformat(),
                "effective_to": None if effective_to is None else effective_to.isoformat(),
                "pricing": {metric: p.as_dict() for metric, p in sorted(pricing.items())},
                "default": None if default_pricing is None else default_pricing.as_dict(),
                "refund": decimal_text(refund),
            }
        )
        versions.append(
            ContractVersion(
                contract_id=contract_id,
                version=version,
                account=account,
                effective_from=effective_from,
                effective_to=effective_to,
                pricing=pricing,
                default_pricing=default_pricing,
                refund_cents=int((refund * 100).to_integral_value()),
                fingerprint=fingerprint,
                raw=dict(raw),
            )
        )
    if not versions:
        raise InputError("at least one contract version is required")
    return tuple(versions)


def _sort_key(version: ContractVersion) -> tuple:
    # Boundaries first: at an identical instant the version that ends there is
    # not eligible anyway, and the later-starting version wins the interval.
    return (
        version.contract_id,
        version.effective_from or datetime.min.replace(tzinfo=timezone.utc),
        version.effective_to or datetime.max.replace(tzinfo=timezone.utc),
        version.version,
    )


def validate_interval_overlaps(versions: Iterable[ContractVersion]) -> None:
    """Reject ambiguous intervals for the same contract/account owner.

    An end that exactly equals the next start is an unambiguous hand-off and
    is allowed.  Any true overlap has two answers for the same instant, which
    would make the invoice non-deterministic, so it is rejected.
    """

    groups: dict[tuple[str, Optional[str]], list[ContractVersion]] = {}
    for version in versions:
        groups.setdefault((version.contract_id, version.account), []).append(version)
    for owner, group in groups.items():
        ordered = sorted(group, key=_sort_key)
        for previous, current in zip(ordered, ordered[1:]):
            if previous.effective_to is None:
                raise InputError(
                    "contract %s has overlapping versions (v%s is unbounded before v%s)"
                    % (owner[0], previous.version, current.version)
                )
            if previous.effective_to > current.effective_from:
                raise InputError(
                    "contract %s versions v%s and v%s overlap"
                    % (owner[0], previous.version, current.version)
                )
            if previous.effective_to < current.effective_from:
                # A gap is reported per event as missing-version, not fatal.
                continue


def effective_for(
    versions: tuple[ContractVersion, ...], account: str, at: datetime
) -> Optional[ContractVersion]:
    """Most specific version that covers ``at``: account, then global.

    If several account-specific versions cover the instant (which interval
    validation normally prevents) the highest version number wins, making the
    choice explicit rather than dependent on input order.
    """

    for selector in (
        lambda v: v.account == account,
        lambda v: v.account is None,
    ):
        candidates = [
            v
            for v in versions
            if selector(v)
            and (v.effective_from is None or v.effective_from <= at)
            and (v.effective_to is None or at < v.effective_to)
        ]
        if candidates:
            return sorted(candidates, key=lambda v: (v.version, v.contract_id))[-1]
    return None


def sorted_versions(versions: Iterable[ContractVersion]) -> list[ContractVersion]:
    """Input-order-independent version ordering for locks and audit output."""

    return sorted(
        versions,
        key=lambda version: (
            version.contract_id,
            version.version,
            version.account or "",
            "" if version.effective_from is None else version.effective_from.isoformat(),
            "" if version.effective_to is None else version.effective_to.isoformat(),
        ),
    )


def contract_lock_digest(versions: Iterable[ContractVersion]) -> str:
    return sha256_hex([version.as_lock() for version in sorted_versions(versions)])



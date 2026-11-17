"""Input validation and event extraction.

Parsing is deliberately strict about things that change money (accounts,
quantities, timestamps, refund targets) and permissive about extra producer
fields.  A malformed individual event becomes an ``Issue`` so the rest of the
batch can still be billed; structural damage (events not a list, bad timezone,
overlapping contracts) raises ``InputError`` and fails the whole run.

Identity and duplicate semantics
--------------------------------
``(account, event_id)`` is the event identity.

* Same identity, same canonical payload  -> idempotent duplicate.  One copy is
  billed and the collision is reported.
* Same identity, different payload       -> conflict.  The deterministically
  lowest content hash is kept and an issue is reported.  Producers should
  emit ``remove`` + a new ``add`` instead of silently mutating history.

When an event has no producer id, an id derived from the account, event time,
metric, operation and content is used, so replaying the same raw batch is
idempotent even for id-less producers.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import (
    BillingConfig,
    Issue,
    ParsedInput,
    RULES_VERSION,
    UsageEvent,
)
from .periods import SUPPORTED_PERIODS
from .util import (
    InputError,
    first_present,
    parse_decimal,
    parse_non_negative_decimal,
    parse_timestamp,
    present,
    sha256_hex,
    short_digest,
)

OPERATIONS = {
    "add": "usage",
    "usage": "usage",
    "usages": "usage",
    "consume": "usage",
    "consumption": "usage",
    "charge": "usage",
    "set": "set",
    "adjustment": "set",
    "correct": "set",
    "correction": "set",
    "remove": "remove",
    "retract": "remove",
    "withdraw": "remove",
    "void": "remove",
    "delete": "remove",
    "refund": "refund",
    "credit": "refund",
    "restore": "restore",
    "unretract": "restore",
    "undelete": "restore",
}

SCOPES = ("contract_version", "contract_family", "account")


def _operation(raw: Any, *, label: str) -> str:
    if raw is None:
        return "usage"
    if not isinstance(raw, str):
        raise InputError("%s.operation must be a string" % label)
    normalized = raw.strip().lower()
    if normalized not in OPERATIONS:
        raise InputError(
            "%s.operation %r is not one of %s"
            % (label, raw, ", ".join(sorted(set(OPERATIONS.values()))))
        )
    return OPERATIONS[normalized]


def _string_id(raw: Any, *, field: str) -> str:
    if isinstance(raw, bool) or raw is None:
        raise InputError("%s must be a non-empty string" % field)
    text = str(raw).strip()
    if not text:
        raise InputError("%s must be a non-empty string" % field)
    return text


def _refund_terms(
    raw: Mapping[str, Any], label: str
) -> tuple[Optional[int], Optional[Decimal]]:
    has_amount = present(raw, ("refund_amount", "refund_value", "amount_refund"))
    has_ratio = present(raw, ("refund_ratio", "refund_fraction", "refund_percent"))
    if has_amount and has_ratio:
        raise InputError("%s cannot specify both refund amount and ratio" % label)
    amount_cents = None
    ratio = None
    if has_amount:
        amount = parse_non_negative_decimal(
            first_present(raw, ("refund_amount", "refund_value", "amount_refund")),
            field=label + ".refund_amount",
        )
        amount_cents = int((amount * 100).to_integral_value())
    if has_ratio:
        ratio = parse_decimal(
            first_present(raw, ("refund_ratio", "refund_fraction", "refund_percent")),
            field=label + ".refund_ratio",
        )
        if present(raw, ("refund_percent",)):
            ratio = ratio / Decimal(100)
        if ratio < 0 or ratio > 1:
            raise InputError("%s.refund_ratio must be between 0 and 1" % label)
    return amount_cents, ratio


def _event_from_raw(
    raw: Mapping[str, Any], index: int, default_account: Optional[str]
) -> tuple[Optional[UsageEvent], list[Issue]]:
    label = "events[%d]" % index
    issues: list[Issue] = []
    account_raw = raw.get("account", default_account)
    if account_raw is None or not str(account_raw).strip():
        return None, [
            Issue(
                code="missing_account",
                message="%s has no account" % label,
                details={"index": index},
            )
        ]
    account = str(account_raw).strip()
    metric = str(first_present(raw, ("metric", "sku", "dimension", "kind", "type")) or "units")

    try:
        at_raw = first_present(raw, ("at", "timestamp", "occurred_at", "event_time"))
        if at_raw is None:
            raise InputError("%s.at is required" % label)
        at = parse_timestamp(at_raw, field=label + ".at")
        effective_raw = raw.get("effective_at")
        if effective_raw is not None:
            at = parse_timestamp(effective_raw, field=label + ".effective_at")
        received_at = None
        received_raw = first_present(raw, ("received_at", "ingested_at", "recorded_at"))
        if received_raw is not None:
            received_at = parse_timestamp(received_raw, field=label + ".received_at")
        operation = _operation(first_present(raw, ("operation", "op", "action")), label=label)
        units_raw = first_present(raw, ("units", "quantity", "qty", "value"))
        if units_raw is None:
            # Control and monetary events do not require a quantity.
            units_raw = 0 if operation in ("set", "remove", "restore", "refund") else None
        if units_raw is None:
            raise InputError("%s.units is required" % label)
        units = parse_decimal(units_raw, field=label + ".units")
        if not units.is_finite():
            raise InputError("%s.units must be finite" % label)
        if operation == "usage" and units <= 0:
            raise InputError("%s.units must be > 0 for usage events" % label)
        target_raw = first_present(raw, ("target_event_id", "refunds", "target"))
        if operation == "set" and units < 0 and target_raw is not None:
            # A correction replaces observed usage and cannot be negative;
            # a target-less adjustment is a signed credit line.
            raise InputError(
                "%s.units must be >= 0 for a targeted correction" % label
            )
        if operation == "refund" and units < 0:
            raise InputError("%s.units must be >= 0 for refund events" % label)
        amount_cents, ratio = _refund_terms(raw, label)
        target = first_present(raw, ("target_event_id", "refunds", "target"))
        if operation == "refund" and target is None and amount_cents is None and ratio is None:
            raise InputError(
                "%s.refund requires target_event_id, refund_amount, or refund_ratio" % label
            )
        if operation in ("remove", "restore") and target is None:
            # Tombstones must be separate events (their own event_id) that
            # name the usage line they control.  Reusing the target's event_id
            # would make the tombstone an identity conflict.
            raise InputError(
                "%s.%s requires target_event_id (a tombstone needs its own event_id)"
                % (label, operation)
            )
        target_text = None if target is None else _string_id(target, field=label + ".target")
        period_override = first_present(
            raw, ("period", "billing_period", "period_key", "invoice_period")
        )
        if period_override is not None:
            period_override = _string_id(period_override, field=label + ".period")
    except InputError as exc:
        return None, [
            Issue(
                code="invalid_event",
                message=str(exc),
                details={"index": index},
            )
        ]

    raw_id = first_present(raw, ("event_id", "id", "eventId", "usage_id"))
    synthetic = raw_id is None
    if synthetic:
        basis = {
            "account": account,
            "metric": metric,
            "at": at.isoformat(),
            "units": str(units),
            "operation": operation,
            "target": target_text,
            "refund_amount": amount_cents,
            "refund_ratio": None if ratio is None else str(ratio),
        }
        event_id = "auto-" + short_digest(basis, length=24)
    else:
        try:
            event_id = _string_id(raw_id, field=label + ".event_id")
        except InputError as exc:
            return None, [
                Issue(code="invalid_event", message=str(exc), details={"index": index})
            ]

    content = {
        "account": account,
        "event_id": event_id,
        "metric": metric,
        "at": at.isoformat(),
        "units": str(units),
        "operation": operation,
        "target": target_text,
        "refund_amount": amount_cents,
        "refund_ratio": None if ratio is None else str(ratio),
        "received_at": None if received_at is None else received_at.isoformat(),
    }
    fingerprint = sha256_hex(content)
    pre_retracted = bool(raw.get("retracted") or raw.get("void") or raw.get("is_retracted"))
    event = UsageEvent(
        event_id=event_id,
        identity="%s/%s" % (account, event_id),
        account=account,
        metric=metric,
        at=at,
        units=units,
        operation=operation,
        target_event_id=target_text,
        refund_amount_cents=amount_cents,
        refund_ratio=ratio,
        received_at=received_at,
        fingerprint=fingerprint,
        synthetic_id=synthetic,
        period_key_override=period_override,
        pre_retracted=pre_retracted,
        raw=dict(raw),
    )
    if pre_retracted:
        issues.append(
            Issue(
                code="retracted_event_ignored",
                message="event carries a retracted marker and is excluded from metering",
                event_ref=event.identity,
                severity="warning",
            )
        )
    return event, issues


def _dedupe(
    events: Iterable[UsageEvent],
) -> tuple[tuple[UsageEvent, ...], list[Issue], list[dict]]:
    by_identity: dict[str, Optional[UsageEvent]] = {}
    occurrences: dict[str, int] = {}
    conflicted: set[str] = set()
    issues: list[Issue] = []
    duplicates: list[dict] = []
    for event in sorted(events, key=lambda e: (e.identity, e.fingerprint)):
        occurrences[event.identity] = occurrences.get(event.identity, 0) + 1
        if event.identity in conflicted:
            duplicates.append(
                {
                    "event_id": event.event_id,
                    "account": event.account,
                    "occurrences": occurrences[event.identity],
                    "disposition": "conflict_excluded",
                    "fingerprint": event.fingerprint,
                }
            )
            continue
        if event.identity not in by_identity:
            by_identity[event.identity] = event
            continue
        previous = by_identity[event.identity]
        assert previous is not None
        if previous.fingerprint == event.fingerprint:
            duplicates.append(
                {
                    "event_id": event.event_id,
                    "account": event.account,
                    "occurrences": occurrences[event.identity],
                    "disposition": "duplicate_ignored",
                    "fingerprint": event.fingerprint,
                }
            )
            continue
        # Two different payloads under one identity have no defensible
        # ordering.  Never pick one silently: exclude the identity from
        # metering and surface both fingerprints so an operator can issue an
        # explicit retraction plus a new event.
        by_identity[event.identity] = None
        conflicted.add(event.identity)
        issues.append(
            Issue(
                code="event_conflict",
                message="same event identity arrived with different content; identity excluded",
                event_ref=event.identity,
                details={
                    "fingerprints": sorted(
                        {previous.fingerprint, event.fingerprint}
                    )
                },
            )
        )
        duplicates.append(
            {
                "event_id": event.event_id,
                "account": event.account,
                "occurrences": occurrences[event.identity],
                "disposition": "conflict_excluded",
                "fingerprint": event.fingerprint,
            }
        )
    kept = [
        by_identity[identity]
        for identity in sorted(by_identity)
        if by_identity[identity] is not None
    ]
    return tuple(kept), issues, duplicates


def _parse_billing(raw: Any) -> BillingConfig:
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise InputError("billing must be an object")
    timezone_name = str(raw.get("timezone", "UTC"))
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise InputError("unknown billing timezone %r" % timezone_name) from exc
    period = str(raw.get("period", "month")).lower()
    if period not in SUPPORTED_PERIODS:
        raise InputError("billing.period must be one of %s" % (SUPPORTED_PERIODS,))
    periods_raw = raw.get("periods", ())
    if isinstance(periods_raw, str):
        periods_raw = [periods_raw]
    if not isinstance(periods_raw, (list, tuple)):
        raise InputError("billing.periods must be a list of period keys")
    free_scope = str(raw.get("free_scope", "contract_version"))
    tier_scope = str(raw.get("tier_scope", "contract_version"))
    if free_scope not in SCOPES:
        raise InputError("billing.free_scope must be one of %s" % (SCOPES,))
    if tier_scope not in SCOPES:
        raise InputError("billing.tier_scope must be one of %s" % (SCOPES,))
    return BillingConfig(
        timezone_name=timezone_name,
        period=period,
        periods=tuple(str(key) for key in periods_raw),
        free_scope=free_scope,
        tier_scope=tier_scope,
    )


def _parse_frozen(raw: Any) -> tuple[Mapping[str, Any], ...]:
    if raw is None:
        return ()
    if isinstance(raw, Mapping):
        raw = list(raw.values())
    if not isinstance(raw, (list, tuple)):
        raise InputError("frozen_invoices must be a list")
    invoices = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise InputError("frozen_invoices[%d] must be an object" % index)
        period = item.get("period")
        if not isinstance(period, str) or not period:
            raise InputError("frozen_invoices[%d].period is required" % index)
        invoices.append(dict(item))
    return tuple(invoices)


def _expand_events(
    payload: Mapping[str, Any],
) -> tuple[list[tuple[int, Mapping[str, Any]]], list[Issue]]:
    """Flatten ``events`` and ``batches[].events`` with provenance.

    A ``batch`` models one producer delivery.  A batch whose producer declares
    failure contributes no events but is reported; when ``on_error`` is
    ``report`` the remaining batches are still billed, so one broken delivery
    cannot block an entire month.  With ``on_error=fail`` the reported error
    aborts the run instead, which is the choice for settlement pipelines.

    A batch file may also be *delivered broken*: ``batches[].raw_events`` is
    parsed row by row, and rows that fail validation become individual issues
    while the valid rows of the same batch still bill.  There is no partial
    write: replay either produces the full deterministic document or raises.
    """

    issues: list[Issue] = []
    rows: list[tuple[int, Mapping[str, Any]]] = []
    direct = payload.get("events", [])
    if direct is None:
        direct = []
    if not isinstance(direct, list):
        raise InputError("events must be a list")
    rows.extend((index, raw) for index, raw in enumerate(direct))

    batches = payload.get("batches")
    if batches is None:
        return rows, issues
    if not isinstance(batches, list):
        raise InputError("batches must be a list")
    for batch_index, batch in enumerate(batches):
        if not isinstance(batch, Mapping):
            raise InputError("batches[%d] must be an object" % batch_index)
        batch_id = str(batch.get("batch_id", batch.get("id", "batch-%d" % batch_index)))
        failure = first_present(batch, ("error", "failure", "failed_reason"))
        if batch.get("status") in ("failed", "error") or failure is not None:
            issues.append(
                Issue(
                    code="batch_failed",
                    message="producer reported a failed batch; its events are not billed",
                    details={
                        "batch_id": batch_id,
                        "error": None if failure is None else str(failure),
                    },
                )
            )
            continue
        batch_events = batch.get("events", batch.get("raw_events"))
        if batch_events is None:
            batch_events = []
        if not isinstance(batch_events, list):
            raise InputError("batches[%d].events must be a list" % batch_index)
        for raw in batch_events:
            if isinstance(raw, Mapping):
                tagged = dict(raw)
                tagged.setdefault("batch_id", batch_id)
                rows.append((len(rows), tagged))
            else:
                rows.append((len(rows), raw))
    return rows, issues


def parse_payload(payload: Any) -> ParsedInput:
    if not isinstance(payload, Mapping):
        raise InputError("input must be a JSON object")
    rows, issues = _expand_events(payload)
    default_account = payload.get("account")
    default_account = None if default_account is None else str(default_account)
    events: list[UsageEvent] = []
    for index, raw in rows:
        if not isinstance(raw, Mapping):
            issues.append(
                Issue(
                    code="invalid_event",
                    message="event row %d is not an object" % index,
                    details={"index": index},
                )
            )
            continue
        event, event_issues = _event_from_raw(raw, index, default_account)
        issues.extend(event_issues)
        if event is not None:
            events.append(event)
    deduped, duplicate_issues, duplicates = _dedupe(events)
    issues.extend(duplicate_issues)

    accounts = sorted({event.account for event in deduped})
    for raw in payload.get("accounts", ()) or ():
        text = str(raw).strip()
        if text and text not in accounts:
            accounts.append(text)
    accounts = sorted(set(accounts))

    batch_ids = {
        str(raw["batch_id"])
        for _index, raw in rows
        if isinstance(raw, Mapping) and isinstance(raw.get("batch_id"), str)
    }
    contracts_raw: list[Mapping[str, Any]] = []
    if payload.get("contracts") is not None:
        value = payload["contracts"]
        if isinstance(value, Mapping):
            value = [value]
        if not isinstance(value, (list, tuple)):
            raise InputError("contracts must be a list")
        contracts_raw.extend(value)
    if payload.get("contract") is not None:
        value = payload["contract"]
        if not isinstance(value, Mapping):
            raise InputError("contract must be an object")
        contracts_raw.append(value)
    if not contracts_raw:
        raise InputError("contract or contracts is required")

    billing = _parse_billing(payload.get("billing"))
    frozen_raw = list(_parse_frozen(payload.get("frozen_invoices")))
    if payload.get("history") is not None:
        history = payload["history"]
        if not isinstance(history, Mapping):
            raise InputError("history must be an object mapping account to invoices")
        for account, invoices in history.items():
            if not isinstance(invoices, (list, tuple)):
                raise InputError("history[%r] must be a list" % (account,))
            for invoice in invoices:
                if not isinstance(invoice, Mapping):
                    raise InputError("history[%r] entries must be objects" % (account,))
                item = dict(invoice)
                item.setdefault("scope", str(account))
                frozen_raw.append(item)

    rules_version = str(payload.get("rules_version", RULES_VERSION))
    if rules_version != RULES_VERSION:
        # The implementation only knows one rules version.  Accepting an
        # arbitrary stamp would make the digest claim something false.
        raise InputError(
            "rules_version %r is not implemented by this build (expected %r)"
            % (rules_version, RULES_VERSION)
        )
    on_error = str(payload.get("on_error", "report"))
    if on_error not in ("report", "fail"):
        raise InputError("on_error must be 'report' or 'fail'")
    currency = str(payload.get("currency", "USD"))
    if not re.match(r"^[A-Za-z]{3,8}$", currency):
        raise InputError("currency must be a short alphabetic code")

    return ParsedInput(
        events=deduped,
        issues=tuple(issues),
        duplicates=tuple(duplicates),
        accounts=tuple(accounts),
        contracts_raw=tuple(contracts_raw),
        billing=billing,
        frozen_raw=tuple(frozen_raw),
        rules_version=rules_version,
        currency=currency,
        on_error=on_error,
        batch_ids=tuple(sorted(batch_ids)),
    )


def load_records(payload: dict) -> list[dict]:
    """Compatibility helper: return validated raw event dictionaries.

    Kept because the public pipeline previously exposed it; the real parsing
    entry point is :func:`parse_payload`.
    """

    if not isinstance(payload, Mapping):
        raise InputError("input must be a JSON object")
    rows = payload.get("events", [])
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise InputError("events must be a list")
    return [dict(row) for row in rows if isinstance(row, Mapping)]



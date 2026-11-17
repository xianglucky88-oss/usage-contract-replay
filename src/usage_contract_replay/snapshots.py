"""Frozen-period snapshots and contract-revision reporting.

A frozen invoice is a promise.  Replay never rewrites it: the recomputed
invoice for a closed period is compared against the frozen digest/amount, and
any difference is emitted as a ``revision`` for that period.  The frozen
invoice stays in the output with ``status: "frozen"`` and its original digest;
the revision carries the recomputed line items so a reviewer can see exactly
which events, tiers and contract versions produced the delta.

Frozen records are keyed by ``(scope, period)``.  ``scope`` is an account name,
or ``"*"`` for a ledger-wide snapshot that applies to every account.  Accepted
shapes:

* a full ``invoices[]`` object from a previous run (``account`` + ``period``),
* a bundle ``{period, invoices: [...]}`` produced by an export step,
* a minimal ``{period, digest, net_cents}`` receipt,
* a ``history`` entry keyed by account.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

from .digests import invoice_digest
from .models import Issue, ParsedInput
from .util import int_cents_from_value, money_text, sha256_hex


def _normalized_items(items: Iterable[Mapping[str, Any]]) -> list[dict]:
    """Fill the fields required by the canonical invoice digest."""

    normalized = []
    for item in items:
        entry = dict(item)
        if "amount_cents" not in entry:
            entry["amount_cents"] = int_cents_from_value(
                entry.get("amount", 0), field="amount"
            )
        if "units" not in entry:
            entry["units"] = "0"
        entry.setdefault("free_units", "0")
        entry.setdefault("billable_units", "0")
        entry.setdefault("kind", "usage")
        entry.setdefault("metric", "")
        entry.setdefault("contract_version", "")
        normalized.append(entry)
    return normalized


def _items_digest(
    account: str, period: str, items: Iterable[Mapping[str, Any]], currency: str, rules_version: str
) -> str:
    return invoice_digest(account, period, _normalized_items(items), currency, rules_version)


def _scope_of(record: Mapping[str, Any]) -> str:
    scope = record.get("scope", record.get("account", "*"))
    text = str(scope).strip()
    return text or "*"


def _period_of(record: Mapping[str, Any]) -> Optional[str]:
    period = record.get("period")
    if isinstance(period, str) and period.strip():
        return period.strip()
    return None


def _frozen_cents(record: Mapping[str, Any]) -> Optional[int]:
    for key in ("net_cents", "total_cents", "amount_cents"):
        if key in record:
            return int_cents_from_value(record[key], field=key)
    for key in ("net", "total", "amount"):
        if key in record:
            return int_cents_from_value(record[key], field=key)
    return None


def _merge(
    by_key: dict[tuple[str, str], dict], key: tuple[str, str], entry: dict, issues: list[Issue]
) -> None:
    existing = by_key.get(key)
    if existing is None:
        by_key[key] = entry
        return
    conflicting = (
        existing["digest"] is not None
        and entry["digest"] is not None
        and existing["digest"] != entry["digest"]
    ) or (
        existing["net_cents"] is not None
        and entry["net_cents"] is not None
        and existing["net_cents"] != entry["net_cents"]
    )
    if conflicting:
        # Keeping "the first one" would make the freeze depend on the order of
        # the frozen list.  Drop both so the period is recomputed and clearly
        # reported instead of silently picking a promise.
        by_key.pop(key)
        issues.append(
            Issue(
                code="frozen_conflict",
                message="two different frozen records describe the same period; period recomputed",
                details={"scope": key[0], "period": key[1]},
            )
        )
        return
    if existing["digest"] is None and entry["digest"] is not None:
        by_key[key] = entry


def normalize_frozen(
    raw_records: Iterable[Mapping[str, Any]], currency: str
) -> tuple[dict[tuple[str, str], dict], list[Issue]]:
    """Collapse every accepted frozen-invoice shape to ``(scope, period)``."""

    issues: list[Issue] = []
    by_key: dict[tuple[str, str], dict] = {}
    for index, record in enumerate(raw_records):
        period = _period_of(record)
        if period is None:
            issues.append(
                Issue(
                    code="frozen_record_invalid",
                    message="frozen invoice has no period",
                    details={"index": index},
                )
            )
            continue
        record_currency = str(record.get("currency", currency))
        if record_currency != currency:
            issues.append(
                Issue(
                    code="frozen_currency_mismatch",
                    message="frozen invoice currency differs from this run",
                    details={
                        "period": period,
                        "frozen_currency": record_currency,
                        "run_currency": currency,
                    },
                )
            )
            continue
        nested = record.get("invoices")
        if isinstance(nested, (list, tuple)):
            for invoice in nested:
                if not isinstance(invoice, Mapping):
                    issues.append(
                        Issue(
                            code="frozen_record_invalid",
                            message="frozen invoice entry is not an object",
                            details={"index": index, "period": period},
                        )
                    )
                    continue
                _ingest_invoice(
                    by_key,
                    issues,
                    invoice,
                    period=period,
                    currency=currency,
                    fallback_scope=_scope_of(record),
                    outer_rules=record.get("rules_version", ""),
                )
            continue
        scope = _scope_of(record)
        items = record.get("items") if isinstance(record.get("items"), list) else []
        rules_version = str(record.get("rules_version", ""))
        digest = record.get("digest")
        if digest is None and items:
            digest = _items_digest(scope, period, items, currency, rules_version)
        net = _frozen_cents(record)
        if digest is None and net is None:
            issues.append(
                Issue(
                    code="frozen_record_invalid",
                    message="frozen invoice needs a digest or an amount",
                    details={"index": index, "period": period},
                )
            )
            continue
        _merge(
            by_key,
            (scope, period),
            {
                "scope": scope,
                "period": period,
                "digest": None if digest is None else str(digest),
                "net_cents": net,
                "rules_version": rules_version,
                "raw": record,
            },
            issues,
        )
    return by_key, issues


def _ingest_invoice(
    by_key: dict[tuple[str, str], dict],
    issues: list[Issue],
    invoice: Mapping[str, Any],
    *,
    period: str,
    currency: str,
    fallback_scope: str,
    outer_rules: str,
) -> None:
    scope = _scope_of(invoice)
    if scope == "*" and fallback_scope != "*":
        scope = fallback_scope
    items = invoice.get("items") if isinstance(invoice.get("items"), list) else []
    rules_version = str(invoice.get("rules_version", outer_rules))
    digest = invoice.get("digest")
    if digest is None and items:
        digest = _items_digest(scope, period, items, currency, rules_version)
    net = _frozen_cents(invoice)
    if net is None and items:
        net = sum(
            int_cents_from_value(item.get("amount_cents", item.get("amount", 0)), field="amount")
            for item in items
        )
    if digest is None and net is None:
        issues.append(
            Issue(
                code="frozen_record_invalid",
                message="frozen invoice entry needs a digest or an amount",
                details={"scope": scope, "period": period},
            )
        )
        return
    _merge(
        by_key,
        (scope, period),
        {
            "scope": scope,
            "period": period,
            "digest": None if digest is None else str(digest),
            "net_cents": net,
            "rules_version": rules_version,
            "raw": invoice,
        },
        issues,
    )


def _matches(existing: dict, recomputed: dict) -> bool:
    if existing["digest"] is not None:
        return existing["digest"] == recomputed["digest"]
    if existing["net_cents"] is not None:
        return existing["net_cents"] == recomputed["net_cents"]
    return False


def _frozen_snapshot(invoice: dict, record: dict) -> dict:
    """The promised value of a closed period, plus the recomputation.

    ``net_cents`` is the frozen promise (what will actually be billed).
    ``recomputed_*`` and ``items`` describe what the current batch would have
    produced.  When the frozen record carried no amount, ``net_cents`` is
    ``None`` rather than a fabricated zero.
    """

    return {
        "invoice_id": invoice["invoice_id"],
        "account": invoice["account"],
        "period": invoice["period"],
        "status": "frozen",
        "currency": invoice["currency"],
        "net": None if record["net_cents"] is None else money_text(record["net_cents"]),
        "net_cents": record["net_cents"],
        "frozen_digest": record["digest"],
        "frozen_net_cents": record["net_cents"],
        "frozen_rules_version": record["rules_version"],
        "recomputed_digest": invoice["digest"],
        "recomputed_net_cents": invoice["net_cents"],
        "items": invoice["items"],
    }


def apply_freezes(
    parsed: ParsedInput,
    invoices: list[dict],
    evidence_periods: Iterable[str],
    frozen=None,
) -> tuple[list[dict], list[dict], list[dict], list[Issue], dict]:
    """Split recomputed invoices into open vs frozen and emit revisions.

    ``invoices`` holds one recomputed invoice per (account, evidence period),
    so a frozen record with no matching invoice is simply not part of this
    run's evidence and is echoed untouched.  It is compared (and can only
    produce a revision) when the batch actually contains data for its period.
    """


    if frozen is None:
        frozen, issues = normalize_frozen(parsed.frozen_raw, parsed.currency)
    else:
        issues = []
    open_invoices: list[dict] = []
    frozen_invoices: list[dict] = []
    revisions: list[dict] = []
    matched: set[tuple[str, str]] = set()
    evidence = set(evidence_periods)

    for invoice in invoices:
        record = frozen.get((invoice["account"], invoice["period"]))
        if record is None:
            record = frozen.get(("*", invoice["period"]))
        if record is None:
            open_invoices.append(invoice)
            continue
        matched.add((record["scope"], record["period"]))
        snapshot = _frozen_snapshot(invoice, record)
        frozen_invoices.append(snapshot)
        if _matches(record, invoice):
            continue
        frozen_cents = record["net_cents"]
        delta = None if frozen_cents is None else invoice["net_cents"] - frozen_cents
        revisions.append(
            {
                "revision_id": "rev_"
                + sha256_hex(
                    [
                        invoice["account"],
                        invoice["period"],
                        invoice["digest"],
                        record["digest"],
                        invoice["rules_version"],
                    ]
                )[:20],
                "scope": invoice["account"],
                "period": invoice["period"],
                "status": "pending",
                "frozen_digest": record["digest"],
                "recomputed_digest": invoice["digest"],
                "frozen_net_cents": frozen_cents,
                "recomputed_net_cents": invoice["net_cents"],
                "delta_cents": delta,
                "delta": None if delta is None else money_text(delta),
                "items": invoice["items"],
                "reason": "contract_or_event_change_after_freeze",
            }
        )

    for (scope, period), record in sorted(frozen.items()):
        if (scope, period) in matched:
            continue
        if period in evidence and scope != "*":
            # The batch has evidence for this period but no invoice for this
            # account.  That means the frozen promise cannot be verified, so
            # say so instead of silently pretending it matched.
            issues.append(
                Issue(
                    code="frozen_scope_not_in_batch",
                    message="frozen account is absent from the current batch; promise not verified",
                    details={"scope": scope, "period": period},
                    severity="warning",
                )
            )
        # A frozen period that this run does not recompute is echoed with an
        # explicit marker so it never disappears from an export.  Its promised
        # line items are echoed too, so the export remains self-explaining and
        # a later credit can be checked against what was invoiced.
        raw_receipt = record.get("raw") or {}
        receipt_items = (
            raw_receipt.get("items") if isinstance(raw_receipt, Mapping) else None
        )
        frozen_invoices.append(
            {
                "invoice_id": "inv_" + sha256_hex([scope, period])[:20],
                "account": scope,
                "period": period,
                "status": "frozen_not_recomputed",
                "not_recomputed_reason": (
                    "no_evidence_for_period" if period not in evidence else "account_absent"
                ),
                "currency": parsed.currency,
                "net": None if record["net_cents"] is None else money_text(record["net_cents"]),
                "net_cents": record["net_cents"],
                "frozen_digest": record["digest"],
                "frozen_net_cents": record["net_cents"],
                "frozen_rules_version": record["rules_version"],
                "recomputed_digest": None,
                "recomputed_net_cents": None,
                "items": list(receipt_items) if isinstance(receipt_items, list) else [],
            }
        )

    frozen_invoices.sort(key=lambda item: (item["period"], item["account"]))
    audit = {
        "frozen": [
            {
                "scope": scope,
                "period": period,
                "digest": record["digest"],
                "net_cents": record["net_cents"],
                "rules_version": record["rules_version"],
            }
            for (scope, period), record in sorted(frozen.items())
        ],
        "frozen_count": len(frozen_invoices),
        "revision_count": len(revisions),
    }
    return open_invoices, frozen_invoices, revisions, issues, audit


def freeze_document(document: Mapping[str, Any], periods=None) -> list[dict]:
    """Produce frozen receipts for invoices in a finished run.

    ``periods`` names the periods being closed.  When omitted, every open
    invoice in the run is closed.  Freezing is always an explicit operator
    action; the caller decides when a period stops taking evidence.  A later
    replay that still contains evidence for a frozen period will report
    revisions rather than rewriting it.

    The output feeds ``frozen_invoices`` (or ``history``) of the next replay,
    which is what "closing a period" means operationally: from then on the
    amount is a promise and later evidence can only create a revision.
    """

    invoices = list(document.get("invoices", ()))
    if periods is None:
        selected = invoices
    else:
        wanted = set(periods)
        unknown = wanted - {invoice["period"] for invoice in invoices}
        if unknown:
            raise ValueError(
                "cannot freeze periods with no open invoice: %s" % sorted(unknown)
            )
        selected = [invoice for invoice in invoices if invoice["period"] in wanted]
    return _receipts(selected, document)


def _receipts(invoices, document: Mapping[str, Any]) -> list[dict]:
    receipts = []
    for invoice in invoices:
        receipts.append(
            {
                "account": invoice["account"],
                "period": invoice["period"],
                "currency": invoice["currency"],
                "net_cents": invoice["net_cents"],
                "net": invoice["net"],
                "digest": invoice["digest"],
                "rules_version": invoice.get("rules_version", ""),
                "contract_versions": sorted(
                    {
                        item["contract_version"]
                        for item in invoice["items"]
                        if item.get("contract_version")
                    }
                ),
                # Line items are kept so a later refund can be bounded against
                # exactly what was invoiced, and so a dispute can be traced
                # without re-running the original batch.
                "items": invoice["items"],
                "frozen_at_rules_version": document.get("rules_version", ""),
            }
        )
    return sorted(receipts, key=lambda receipt: (receipt["period"], receipt["account"]))


def frozen_target_amounts(
    frozen: Mapping[tuple[str, str], dict]
) -> dict[tuple[str, str], dict]:
    """Recover billed amounts of usage lines from frozen receipts.

    A refund may target usage whose period is frozen and therefore absent from
    the current batch.  When the frozen receipt carries its line items we can
    still bound the credit exactly; when it does not, the refund stays
    unresolved and is reported rather than guessed.

    Key is ``(account, source_event_id)``.
    """

    recovered: dict[tuple[str, str], dict] = {}
    for (scope, period), record in frozen.items():
        if scope == "*":
            continue
        raw = record.get("raw") or {}
        items = raw.get("items") if isinstance(raw, Mapping) else None
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, Mapping):
                continue
            if item.get("kind", "usage") != "usage":
                continue
            amount_cents = int_cents_from_value(
                item.get("amount_cents", item.get("amount", 0)), field="amount"
            )
            for source in item.get("source_event_ids", ()) or ():
                recovered[(scope, str(source))] = {
                    "amount_cents": amount_cents,
                    "period": period,
                }
    return recovered

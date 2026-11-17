"""Canonical invoice and output digests.

One definition of the invoice digest is used by the engine, the snapshot
importer and the audit layer, so a receipt re-imported into a later run
compares equal without a normalisation round-trip.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .util import canonical_json, sha256_hex


def _ordered_items(items: Iterable[Mapping[str, Any]]) -> list[dict]:
    return sorted(
        (dict(item) for item in items),
        key=lambda item: (str(item.get("line_id", "")), canonical_json(item)),
    )


def invoice_digest(
    account: str,
    period: str,
    items: Iterable[Mapping[str, Any]],
    currency: str,
    rules_version: str,
) -> str:
    return sha256_hex(
        {
            "account": account,
            "period": period,
            "currency": currency,
            "rules_version": rules_version,
            "items": _ordered_items(items),
        }
    )


def output_digest(document: Mapping[str, Any]) -> str:
    return sha256_hex(
        {
            "status": document.get("status"),
            "currency": document.get("currency"),
            "rules_version": document.get("rules_version"),
            "invoices": document.get("invoices", []),
            "frozen_invoices": document.get("frozen_invoices", []),
            "revisions": document.get("revisions", []),
        }
    )

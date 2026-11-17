"""Thin orchestration boundary.

No hidden state, no I/O, no clock access: ``run`` is a pure function of the
payload.  The failure model is deliberately coarse-grained - validation and
interval errors raise ``InputError`` for the whole run, while per-event
problems accumulate as audit issues and set ``status`` to ``partial`` or
``failed``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

from .engine import build_document
from .report import build_result
from .util import decimal_text, number_from_decimal

PROJECT = "usage-contract-replay"
ZERO = Decimal(0)


def run(payload: Mapping[str, Any]) -> dict:
    document = build_document(payload).as_dict()
    # Legacy scalar summary keys, kept so existing callers do not break.  They
    # are derived from the same rounded per-line amounts as the invoices.
    invoices = document["invoices"]
    net_cents = sum(invoice["net_cents"] for invoice in invoices)
    document.update(
        {
            "account": invoices[0]["account"] if invoices else None,
            "amount": number_from_decimal(Decimal(net_cents) / 100),
            "amount_cents": net_cents,
            "total_units": decimal_text(
                sum(
                    (Decimal(item["units"]) for invoice in invoices for item in invoice["items"]),
                    ZERO,
                )
            ),
            "billable_units": decimal_text(
                sum(
                    (
                        Decimal(item["billable_units"])
                        for invoice in invoices
                        for item in invoice["items"]
                    ),
                    ZERO,
                )
            ),
            "event_count": len(document["audit"]["events"]),
        }
    )
    return build_result(PROJECT, document)

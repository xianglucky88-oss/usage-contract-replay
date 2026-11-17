"""Stable output envelope and compact summaries."""

from __future__ import annotations

from typing import Any, Mapping

from .models import PipelineResult


def build_result(project: str, data: Mapping[str, Any]) -> dict:
    result = PipelineResult(project=project, data=data)
    payload = {"project": result.project, "status": data.get("status", "ok"), **result.data}
    payload["summary"] = summarize(data)
    return payload


def summarize(data: Mapping[str, Any]) -> dict:
    invoices = data.get("invoices", [])
    frozen = data.get("frozen_invoices", [])
    revisions = data.get("revisions", [])
    open_cents = sum(invoice.get("net_cents") or 0 for invoice in invoices)
    frozen_cents = sum(invoice.get("net_cents") or 0 for invoice in frozen)
    return {
        "invoice_count": len(invoices),
        "frozen_invoice_count": len(frozen),
        "revision_count": len(revisions),
        "open_net_cents": open_cents,
        "frozen_net_cents": frozen_cents,
        # What the customer is actually invoiced: promises plus open periods.
        "billed_net_cents": open_cents + frozen_cents,
        "revision_delta_cents": sum(
            revision.get("delta_cents") or 0 for revision in revisions
        ),
        "accounts": sorted(
            {invoice["account"] for invoice in invoices + frozen}
        ),
        "periods": sorted({invoice["period"] for invoice in invoices + frozen}),
        "issues": data.get("audit", {}).get("issue_counts", {}),
        "contract_lock_digest": data.get("audit", {}).get("contract_lock_digest", ""),
        "input_digest": data.get("input_digest", ""),
        "output_digest": data.get("audit", {}).get("output_digest", ""),
        "invariants_ok": all(data.get("audit", {}).get("invariants", {}).values()),
    }

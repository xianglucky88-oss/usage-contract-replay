"""Stable output envelope.

The report layer is the only place that decides the wire shape.  It never
recomputes billing values; it validates that the engine handed it a complete
record and emits a deterministic envelope:

* a fixed key set with ``sort_keys``-friendly construction;
* the event detail is re-sorted into the canonical order, so a report is
  byte-for-byte stable regardless of dict insertion order upstream;
* the envelope version and status are explicit so consumers can pin them.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .models import PipelineResult

REPORT_VERSION = 1

REQUIRED_FIELDS = (
    "account",
    "total_units",
    "billable_units",
    "free_units",
    "amount",
    "event_count",
    "events",
)


class ReportError(ValueError):
    """The engine produced a record the report envelope cannot publish."""

    def __init__(self, field: str):
        self.field = field
        super().__init__(f"missing required field: {field}")


def _event_sort_key(item: Dict[str, Any]) -> tuple:
    return (
        item.get("account") or "",
        item.get("at") or "",
        item.get("event_id") is not None,
        item.get("event_id") or "",
        item.get("units", 0),
    )


def _stable_events(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(events, key=_event_sort_key)


def build_result(project: str, data: Dict[str, Any]) -> Dict[str, Any]:
    for field in REQUIRED_FIELDS:
        if field not in data:
            raise ReportError(field)

    events = data["events"]
    if not isinstance(events, list):
        raise ReportError("events")

    payload = dict(data)
    payload["events"] = _stable_events(events)
    payload["report_version"] = REPORT_VERSION

    result = PipelineResult(project=project, data=payload)
    return {"project": result.project, "status": "ok", **result.data}


def build_error(project: str, code: str, message: str) -> Dict[str, Any]:
    """A deterministic error envelope with the same top-level keys."""
    return {
        "project": project,
        "status": "error",
        "report_version": REPORT_VERSION,
        "error": {"code": code, "message": message},
    }

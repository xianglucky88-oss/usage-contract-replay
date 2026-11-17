"""Metamorphic relations.

Each relation takes a payload and asserts that a transformation which must not
change the invoice *does* leave it unchanged, or that one which must change it
leaves it changed in a predictable way.

These catch classes of bug that a single-input oracle check can miss:
order dependence, non-idempotent replay, account grouping drift, and unstable
report packaging.
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from typing import Any, Callable, Dict, List, Tuple

from . import rng
from .invariants import InvariantViolation


def _inv(code: str, message: str) -> None:
    raise InvariantViolation(code, message)


def _scalars(result: Dict[str, Any]) -> Tuple:
    return (
        result["account"],
        result["total_units"],
        result["billable_units"],
        result["free_units"],
        result["amount_exact"],
        result["gross_amount_exact"],
        result["refund_applied_exact"],
        result["event_count"],
    )


def _detail(result: Dict[str, Any]) -> List[Tuple]:
    return [
        (
            item["account"],
            item["at"],
            item["event_id"],
            item["units"],
            item["free_units"],
            item["billable_units"],
            item["line_amount"],
        )
        for item in result["events"]
    ]


def reorder_events(payload: Dict[str, Any], seed: int) -> Dict[str, Any]:
    shuffled = copy.deepcopy(payload)
    rng.Random(seed).shuffle(shuffled["events"])
    return shuffled


def duplicate_events(payload: Dict[str, Any]) -> Dict[str, Any]:
    doubled = copy.deepcopy(payload)
    doubled["events"] = doubled["events"] + copy.deepcopy(doubled["events"])
    return doubled


def _expected_duplicates(payload: Dict[str, Any]) -> int:
    """Independent duplicate count for a valid payload."""
    seen = set()
    duplicates = 0
    for event in payload.get("events", []):
        event_id = event.get("event_id")
        key = ("id", event_id) if event_id is not None else (
            "content",
            event["account"],
            event["at"],
            event["units"],
        )
        if key in seen:
            duplicates += 1
        else:
            seen.add(key)
    return duplicates


def split_event(payload: Dict[str, Any], index: int, seed: int) -> Dict[str, Any]:
    """Split one event's units into two adjacent events.

    Free-quota allocation is order-sensitive at the split boundary, so the
    split is only guaranteed to preserve totals (not per-event free split)
    unless the split point is the free boundary.  We therefore assert the
    aggregate conservation, which must always hold.
    """
    result = copy.deepcopy(payload)
    source = rng.Random(seed)
    event = result["events"][index]
    units = event["units"]
    if units < 2:
        return result
    first = source.randrange(1, units)
    second = units - first
    left = dict(event)
    right = dict(event)
    left["units"] = first
    right["units"] = second
    # Split copies must not carry the same explicit id (that would be a
    # conflicting duplicate), and their timestamps distinguish content ids.
    left.pop("event_id", None)
    right.pop("event_id", None)
    right["at"] = event["at"] + "~"
    result["events"][index : index + 1] = [left, right]
    return result


def apply(
    runner: Callable[[Dict[str, Any]], Dict[str, Any]],
    payload: Dict[str, Any],
    seed: int,
) -> List[str]:
    """Run every relation for one valid payload; return the names that held."""
    base = runner(payload)
    if base.get("status") != "ok":
        _inv("metamorphic_base", f"base payload did not succeed: {base.get('error')}")
    held: List[str] = []

    # 1. Order independence.
    reordered = runner(reorder_events(payload, seed))
    if _scalars(reordered) != _scalars(base) or _detail(reordered) != _detail(base):
        _inv("order_dependence", "shuffling input events changed the invoice")
    held.append("order_independence")

    # 2. Replay idempotence.
    replayed = runner(duplicate_events(payload))
    if _scalars(replayed) != _scalars(base) or _detail(replayed) != _detail(base):
        _inv("replay_double_count", "duplicating the event stream changed the invoice")
    expected_duplicates = _expected_duplicates(duplicate_events(payload))
    if replayed["duplicate_event_count"] != expected_duplicates:
        _inv(
            "duplicate_count",
            f"duplicate_event_count {replayed['duplicate_event_count']} != expected {expected_duplicates}",
        )
    held.append("replay_idempotence")

    # 3. Repeated replay is still idempotent (triple).
    tripled = copy.deepcopy(payload)
    tripled["events"] = tripled["events"] + copy.deepcopy(tripled["events"]) + copy.deepcopy(payload["events"])
    again = runner(tripled)
    if _scalars(again) != _scalars(base):
        _inv("replay_triple", "replaying three times changed the invoice")
    held.append("replay_triple_idempotence")

    # 4. Report packaging stability: same payload, re-serialised, same bytes.
    first = json.dumps(runner(payload), sort_keys=True, ensure_ascii=False)
    second = json.dumps(runner(payload), sort_keys=True, ensure_ascii=False)
    if first != second:
        _inv("report_instability", "report envelope is not byte-stable")
    held.append("report_stability")

    # 5. Detail reorder inside the engine input is irrelevant: feed the
    #    engine's own events back through the pipeline.
    if base["events"]:
        chained = {
            "events": [
                {
                    "account": item["account"],
                    "at": item["at"],
                    "units": item["units"],
                    "event_id": item["event_id"],
                }
                for item in base["events"]
            ],
            "contract": payload["contract"],
        }
        replayed_detail = runner(chained)
        if _scalars(replayed_detail) != _scalars(base):
            _inv("detail_roundtrip", "pipeline output is not a stable fixpoint")
        held.append("detail_roundtrip")

    # 6. Increasing free quota never increases billable units.
    contract = payload["contract"]
    bumped = copy.deepcopy(payload)
    bumped["contract"]["free"] = contract.get("free", 0) + 1
    more_free = runner(bumped)
    if more_free.get("status") == "ok" and more_free["billable_units"] > base["billable_units"]:
        _inv("free_monotonicity", "increasing free quota increased billable units")
    held.append("free_monotonicity")

    return held


def split_totals_preserved(
    runner: Callable[[Dict[str, Any]], Dict[str, Any]],
    payload: Dict[str, Any],
    seed: int,
) -> bool:
    """A split event preserves total_units and the gross amount.

    Only defined for payloads whose events are already distinct: splitting a
    payload that contains duplicates would change the de-duplicated stream.
    """
    if _expected_duplicates(payload):
        return True
    candidates = [i for i, event in enumerate(payload["events"]) if event["units"] >= 2]
    if not candidates:
        return True
    index = candidates[rng.Random(seed).randrange(len(candidates))]
    base = runner(payload)
    split_payload = split_event(payload, index, seed)
    # A split may introduce content-identical units that are not duplicates
    # (different ``at``), so totals are the invariant to assert.
    if split_payload == payload:
        return True
    after = runner(split_payload)
    if after.get("status") != "ok":
        _inv("split_status", f"split payload failed: {after.get('error')}")
    if after["total_units"] != base["total_units"]:
        _inv("split_total", "splitting an event changed total_units")
    if Decimal(after["amount_exact"]) != Decimal(base["amount_exact"]):
        _inv("split_amount", "splitting an event changed the amount")
    return True

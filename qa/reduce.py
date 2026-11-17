"""Minimal-counterexample reduction.

When a property fails, the raw failing payload is usually larger than it needs
to be.  This module shrinks it greedily while the failure predicate still
holds:

* drop whole events;
* drop free quota / refund / price toward their simplest values;
* shrink units toward 0;
* drop optional fields.

The predicate is supplied by the caller, e.g. "pipeline result disagrees with
the oracle" or "metamorphic relation X fails".  The output is the smallest
payload (by :func:`complexity`) for which the predicate still returns true,
which is what a bug report should contain.
"""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

Predicate = Callable[[Dict[str, Any]], bool]


def complexity(payload: Dict[str, Any]) -> Tuple[int, int, int, int, int]:
    """Lexicographic size measure.

    ``(events, total_units, contract_magnitude, key_count, text_length)``.
    The trailing text length makes cosmetic cleanups (shorter timestamps,
    accounts, reasons) strictly improving, so reduction reaches a fixed point
    instead of stalling on equal-size candidates.
    """
    events = payload.get("events", []) if isinstance(payload, dict) else []
    contract = payload.get("contract", {}) if isinstance(payload, dict) else {}
    units = sum(event.get("units", 0) for event in events if isinstance(event.get("units"), int))
    magnitude = 0
    for key in ("free", "unit_price", "refund"):
        value = contract.get(key, 0)
        try:
            magnitude += int(abs(Decimal(str(value))) * 100)
        except Exception:
            magnitude += 1000
    keys = len(contract) + sum(len(event) for event in events)
    text_length = len(str(payload))
    return (len(events), units, magnitude, keys, text_length)


def _candidates(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """All one-step simplifications of ``payload``."""
    out: List[Dict[str, Any]] = []
    if not isinstance(payload, dict):
        return out

    events = payload.get("events")
    if isinstance(events, list):
        for index in range(len(events)):
            candidate = copy.deepcopy(payload)
            del candidate["events"][index]
            out.append(candidate)
        for index, event in enumerate(events):
            if not isinstance(event, dict):
                continue
            units = event.get("units")
            if isinstance(units, int) and units > 0:
                for smaller in (0, units // 2, units - 1):
                    if 0 <= smaller < units:
                        candidate = copy.deepcopy(payload)
                        candidate["events"][index]["units"] = smaller
                        out.append(candidate)
            if "event_id" in event:
                candidate = copy.deepcopy(payload)
                del candidate["events"][index]["event_id"]
                out.append(candidate)
            # Simplify the timestamp toward a fixed value only if it changes.
            if event.get("at") not in (None, "t"):
                candidate = copy.deepcopy(payload)
                candidate["events"][index]["at"] = "t"
                out.append(candidate)

    contract = payload.get("contract")
    if isinstance(contract, dict):
        free = contract.get("free", 0)
        if isinstance(free, int) and free > 0:
            for smaller in (0, free // 2, free - 1):
                if 0 <= smaller < free:
                    candidate = copy.deepcopy(payload)
                    candidate["contract"]["free"] = smaller
                    out.append(candidate)
        for key, simple in (("refund", "0"), ("unit_price", "1")):
            if key in contract and str(contract[key]) != simple:
                candidate = copy.deepcopy(payload)
                candidate["contract"][key] = simple
                if key == "refund":
                    candidate["contract"].pop("refund_reason", None)
                out.append(candidate)
        for key in ("refund_reason", "version"):
            if key in contract:
                candidate = copy.deepcopy(payload)
                del candidate["contract"][key]
                out.append(candidate)
    return out


def minimize(
    payload: Dict[str, Any],
    fails: Predicate,
    max_steps: int = 2000,
) -> Tuple[Dict[str, Any], int]:
    """Greedy shrink ``payload`` while ``fails`` holds.

    Returns ``(minimal_payload, steps)``.  The original is returned unchanged
    if it does not fail.
    """
    if not fails(payload):
        return copy.deepcopy(payload), 0
    current = copy.deepcopy(payload)
    steps = 0
    improved = True
    while improved and steps < max_steps:
        improved = False
        best = None
        for candidate in _candidates(current):
            steps += 1
            if steps >= max_steps:
                break
            if complexity(candidate) < complexity(current) and fails(candidate):
                if best is None or complexity(candidate) < complexity(best):
                    best = candidate
        if best is not None:
            current = best
            improved = True
    return current, steps

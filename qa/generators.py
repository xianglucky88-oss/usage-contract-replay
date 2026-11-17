"""Deterministic, seedable generators for valid and boundary-invalid payloads.

Hypothesis is not available in this environment, so the property harness uses
its own small shrink-oriented generator built on :mod:`random` with an explicit
seed.  All randomness is reproducible: same seed => same case stream.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Iterator, List, Optional

from . import rng


# Interesting money values that exercise rounding, truncation and caps.
INTERESTING_PRICES = (
    "0",
    "0.01",
    "0.005",
    "0.015",
    "0.1",
    "0.333",
    "1",
    "2.5",
    "1.005",
    "0.0049",
    "1234.56",
    "99999999.99",
)

INTERESTING_REFUNDS = ("0", "0.01", "0.005", "1", "0.1")


def make_event(
    account: str = "A1",
    at: str = "2026-01-01",
    units: int = 1,
    event_id: Optional[str] = None,
) -> Dict[str, Any]:
    event: Dict[str, Any] = {"account": account, "at": at, "units": units}
    if event_id is not None:
        event["event_id"] = event_id
    return event


def make_payload(
    events: List[Dict[str, Any]],
    free: int = 0,
    unit_price: Any = "1",
    refund: Any = "0",
    refund_reason: Optional[str] = None,
    version: Optional[str] = "v1",
) -> Dict[str, Any]:
    contract: Dict[str, Any] = {"free": free, "unit_price": unit_price, "refund": refund}
    if refund_reason is not None:
        contract["refund_reason"] = refund_reason
    if version is not None:
        contract["version"] = version
    return {"events": events, "contract": contract}


def valid_payloads(seed: int, count: int) -> Iterator[Dict[str, Any]]:
    """Yield ``count`` valid, reproducible payloads spanning the boundaries."""
    source = rng.Random(seed)
    for _ in range(count):
        event_count = source.choice([0, 1, 1, 2, 3, 4, 6, 9])
        accounts = source.choice(["A1", "ACCT-Z", "acct/with space", "9"])
        free = source.choice([0, 0, 1, 3, 10, 50, 1000])
        price = source.choice(INTERESTING_PRICES)
        units_choices = [0, 0, 1, 2, 3, 5, 7, 10, 11, 100]
        events = []
        for index in range(event_count):
            units = source.choice(units_choices)
            at = f"2026-01-{(index % 28) + 1:02d}"
            if source.random() < 0.5:
                events.append(make_event(accounts, at, units, f"evt-{index}"))
            else:
                events.append(make_event(accounts, at, units, None))
            # Replay an exact copy of an already-emitted event: the pipeline
            # must dedupe it rather than double count.
            if source.random() < 0.3:
                events.append(dict(events[-1]))
        refund_choice = source.choice(INTERESTING_REFUNDS + ("0", "0"))
        reason = "promo" if Decimal(refund_choice) > 0 else None
        yield make_payload(
            events=events,
            free=free,
            unit_price=price,
            refund=refund_choice,
            refund_reason=reason,
            version=source.choice(["v1", "2026-01", None]),
        )


def boundary_payloads() -> List[Dict[str, Any]]:
    """Named cases covering every boundary the task calls out."""
    return [
        # Empty event stream.
        make_payload([], free=0, unit_price="2.5"),
        make_payload([], free=10, unit_price="2.5", refund="0"),
        # Zero units.
        make_payload([make_event(units=0)], free=0, unit_price="2.5"),
        make_payload([make_event(units=0), make_event(units=0)], free=5, unit_price="1"),
        # Exactly at the free boundary and one over.
        make_payload([make_event(units=10)], free=10, unit_price="2.5"),
        make_payload([make_event(units=11)], free=10, unit_price="2.5"),
        make_payload([make_event(units=9)], free=10, unit_price="2.5"),
        # Free quota split across events in canonical order.
        make_payload(
            [make_event(at="2026-01-02", units=6), make_event(at="2026-01-01", units=6)],
            free=10,
            unit_price="2.5",
        ),
        # Refund exactly equal to gross.
        make_payload([make_event(units=4)], free=0, unit_price="2.5", refund="10", refund_reason="full"),
        # Refund above gross (must cap, never go negative).
        make_payload([make_event(units=1)], free=0, unit_price="2.5", refund="99", refund_reason="over"),
        # Rounding: half-up at exactly half a cent.
        make_payload([make_event(units=1)], free=0, unit_price="0.005"),
        make_payload([make_event(units=1)], free=0, unit_price="0.015"),
        make_payload([make_event(units=1)], free=0, unit_price="0.0049"),
        make_payload([make_event(units=3)], free=0, unit_price="0.335"),
        make_payload([make_event(units=1000000)], free=0, unit_price="0.0001"),
        # Duplicate replay with explicit ids.
        make_payload(
            [make_event(units=5, event_id="e1"), make_event(units=5, event_id="e1")],
            free=0,
            unit_price="1",
        ),
        # Duplicate replay by content only.
        make_payload(
            [make_event(units=5), make_event(units=5)],
            free=0,
            unit_price="1",
        ),
        # Account grouping: whitespace must be normalised, not treated as a
        # different group or echoed raw.
        make_payload([make_event(account="  A1  ", units=3)], free=0, unit_price="1"),
        make_payload([make_event(account="\tA1", units=3)], free=1, unit_price="1"),
        # Large integers.
        make_payload([make_event(units=2**53)], free=0, unit_price="1"),
    ]


def invalid_payloads() -> List[Dict[str, Any]]:
    """Payloads the oracle rejects, one per stable error code.

    The first entry is a non-object sentinel; callers that need a mapping can
    skip it.  ``oracle.codes_for`` accepts any JSON value.
    """
    return [
        "not an object",  # type: ignore[list-item]
        {"events": "nope"},
        {"events": [1, 2]},
        make_payload([make_event(account="")]),
        make_payload([make_event(at="")]),
        make_payload([make_event(units=1.5)]),
        make_payload([make_event(units=True)]),
        make_payload([make_event(units="3")]),
        make_payload([make_event(units=-1)]),
        make_payload([make_event(units=1, event_id="")]),
        make_payload(
            [make_event(units=1, event_id="dup"), make_event(units=2, event_id="dup")],
        ),
        {"events": []},
        {"events": [], "contract": {"free": 0, "refund": "0"}},
        {"events": [], "contract": {"free": 0, "unit_price": "-1", "refund": "0"}},
        {"events": [], "contract": {"free": 0, "unit_price": "1", "refund": -1}},
        {"events": [], "contract": {"free": -1, "unit_price": "1", "refund": "0"}},
        {"events": [], "contract": {"free": True, "unit_price": "1", "refund": "0"}},
        {"events": [], "contract": {"free": 0, "unit_price": float("nan"), "refund": "0"}},
        {"events": [], "contract": {"free": 0, "unit_price": "abc", "refund": "0"}},
        {"events": [], "contract": {"free": 0, "unit_price": "1", "refund": "1"}},
        {"events": [], "contract": {"free": 0, "unit_price": "1", "refund": "0", "refund_reason": 5}},
        {"events": [], "contract": {"free": 0, "unit_price": "1", "refund": "0", "version": 5}},
        {"events": [make_event(account="A"), make_event(account="B")], "contract": {"free": 0, "unit_price": "1", "refund": "0"}},
    ]

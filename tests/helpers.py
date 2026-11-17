import json
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def event(**overrides) -> dict:
    base = {
        "event_id": "evt-1",
        "account": "A1",
        "at": "2026-01-10T00:00:00Z",
        "metric": "api",
        "units": 1,
    }
    base.update(overrides)
    return base


def contract(**overrides) -> dict:
    base = {
        "id": "C1",
        "version": 1,
        "effective_from": "2026-01-01T00:00:00Z",
        "pricing": {
            "api": {
                "free": 0,
                "tiers": [{"up_to": None, "unit_price": "1.00"}],
            }
        },
    }
    base.update(overrides)
    return base


def item(invoice: dict, kind: str = "usage", metric: Optional[str] = None) -> dict:
    matches = [line for line in invoice["items"] if line["kind"] == kind]
    if metric is not None:
        matches = [line for line in matches if line["metric"] == metric]
    assert len(matches) == 1, matches
    return matches[0]


def invoice(result: dict, account: str, period: str) -> dict:
    matches = [
        candidate
        for candidate in result["invoices"]
        if candidate["account"] == account and candidate["period"] == period
    ]
    assert len(matches) == 1, (account, period, result["invoices"])
    return matches[0]


def only_invoice(result: dict) -> dict:
    assert len(result["invoices"]) == 1, result["invoices"]
    return result["invoices"][0]

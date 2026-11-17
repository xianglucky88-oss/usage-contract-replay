import unittest

from helpers import contract, event, invoice, item, only_invoice
from usage_contract_replay.pipeline import run
from usage_contract_replay.util import InputError


class TestPeriodAttribution(unittest.TestCase):
    def test_cross_month_event_attribution_uses_event_time(self):
        payload = {
            "contracts": [contract()],
            "billing": {"period": "month", "timezone": "UTC"},
            "events": [
                event(event_id="jan", at="2026-01-31T23:59:59Z", units=3),
                event(event_id="feb", at="2026-02-01T00:00:00Z", units=5),
            ],
        }
        result = run(payload)
        self.assertEqual(invoice(result, "A1", "2026-01")["net_cents"], 300)
        self.assertEqual(invoice(result, "A1", "2026-02")["net_cents"], 500)
        # Half-open boundary: exactly midnight belongs to the new period.
        self.assertEqual(
            invoice(result, "A1", "2026-02")["items"][0]["source_event_ids"], ["feb"]
        )

    def test_timezone_moves_period_boundary(self):
        # 2026-01-31T20:00Z is 2026-02-01 05:00 in Tokyo (+09:00).
        payload = {
            "contracts": [contract()],
            "billing": {"period": "month", "timezone": "Asia/Tokyo"},
            "events": [event(event_id="tokyo", at="2026-01-31T20:00:00Z", units=1)],
        }
        result = run(payload)
        self.assertEqual(only_invoice(result)["period"], "2026-02")

    def test_late_event_keeps_original_period_and_is_flagged(self):
        payload = {
            "contracts": [contract()],
            "billing": {"period": "month", "timezone": "UTC"},
            "events": [
                event(
                    event_id="late",
                    at="2026-01-15T00:00:00Z",
                    received_at="2026-03-02T00:00:00Z",
                    units=2,
                )
            ],
        }
        result = run(payload)
        line = item(invoice(result, "A1", "2026-01"))
        self.assertIn("late_event", line["adjustment_reasons"])
        self.assertEqual(
            [entry["reason"] for entry in result["audit"]["events"]], ["late_event"]
        )

    def test_explicit_period_override_does_not_use_ingest_time(self):
        payload = {
            "contracts": [contract()],
            "events": [
                event(event_id="x", at="2026-01-31T00:00:00Z", period="2026-02", units=1)
            ],
        }
        result = run(payload)
        self.assertEqual(only_invoice(result)["period"], "2026-02")

    def test_event_outside_configured_window_is_rejected_not_invented(self):
        payload = {
            "contracts": [contract()],
            "billing": {"period": "month", "periods": ["2026-02"]},
            "events": [event(event_id="jan", at="2026-01-15T00:00:00Z", units=1)],
        }
        result = run(payload)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["audit"]["issue_counts"], {"period_out_of_window": 1})
        self.assertEqual(result["invoices"][0]["net_cents"], 0)

    def test_quarter_period_buckets_three_months(self):
        payload = {
            "contracts": [contract()],
            "billing": {"period": "quarter"},
            "events": [
                event(event_id="q1a", at="2026-02-10", units=1),
                event(event_id="q1b", at="2026-03-31T23:00:00Z", units=1),
            ],
        }
        self.assertEqual(only_invoice(run(payload))["period"], "2026-Q1")


class TestContractSwitching(unittest.TestCase):
    def contracts(self):
        return [
            contract(
                version=1,
                effective_from="2026-01-01T00:00:00Z",
                effective_to="2026-02-01T00:00:00Z",
                pricing={"api": {"unit_price": "1.00"}},
            ),
            contract(
                version=2,
                effective_from="2026-02-01T00:00:00Z",
                pricing={"api": {"unit_price": "2.00"}},
            ),
        ]

    def test_switch_inside_period_prices_each_line_by_its_version(self):
        payload = {
            "contracts": self.contracts(),
            "events": [
                event(event_id="old", at="2026-01-20", units=10),
                event(event_id="new", at="2026-02-10", units=10),
            ],
        }
        result = run(payload)
        jan = item(invoice(result, "A1", "2026-01"))
        feb = item(invoice(result, "A1", "2026-02"))
        self.assertEqual(jan["contract_version"], "*:C1:v1")
        self.assertEqual(feb["contract_version"], "*:C1:v2")
        self.assertEqual((jan["amount_cents"], feb["amount_cents"]), (1000, 2000))

    def test_exact_boundary_belongs_to_new_version(self):
        payload = {
            "contracts": self.contracts(),
            "events": [event(event_id="edge", at="2026-02-01T00:00:00Z", units=1)],
        }
        result = run(payload)
        self.assertEqual(
            only_invoice(result)["items"][0]["contract_version"], "*:C1:v2"
        )

    def test_account_specific_contract_beats_global(self):
        contracts = [
            contract(
                version=1,
                pricing={"api": {"unit_price": "1.00"}},
            ),
            contract(
                id="C2",
                version=1,
                account="VIP",
                pricing={"api": {"unit_price": "0.25"}},
            ),
        ]
        payload = {
            "contracts": contracts,
            "events": [event(event_id="v", account="VIP", units=4)],
        }
        result = run(payload)
        self.assertEqual(only_invoice(result)["net_cents"], 100)

    def test_overlapping_versions_are_rejected(self):
        contracts = self.contracts()
        contracts[1] = contract(
            version=2,
            effective_from="2026-01-15T00:00:00Z",
            pricing={"api": {"unit_price": "2.00"}},
        )
        with self.assertRaises(InputError):
            run({"contracts": contracts, "events": []})

    def test_gap_reports_missing_contract_for_that_event_only(self):
        contracts = [
            contract(
                version=1,
                effective_from="2026-01-01",
                effective_to="2026-01-15",
                pricing={"api": {"unit_price": "1.00"}},
            ),
            contract(
                version=2,
                effective_from="2026-02-01",
                pricing={"api": {"unit_price": "1.00"}},
            ),
        ]
        payload = {
            "contracts": contracts,
            "events": [
                event(event_id="in-gap", at="2026-01-20", units=1),
                event(event_id="ok", at="2026-02-02", units=2),
            ],
        }
        result = run(payload)
        self.assertEqual(result["audit"]["issue_counts"], {"missing_contract": 1})
        self.assertEqual(only_invoice(result)["net_cents"], 200)

    def test_duplicate_version_number_is_rejected(self):
        with self.assertRaises(InputError):
            run({"contracts": [contract(), contract()], "events": []})


class TestTierAndAllowance(unittest.TestCase):
    def tiered(self):
        return contract(
            pricing={
                "api": {
                    "free": 10,
                    "tiers": [
                        {"up_to": 100, "unit_price": "0.10"},
                        {"up_to": None, "unit_price": "0.05"},
                    ],
                }
            }
        )

    def test_free_allowance_then_tiers_across_events(self):
        payload = {
            "contracts": [self.tiered()],
            "events": [
                event(event_id="a", at="2026-01-01", units=60),
                event(event_id="b", at="2026-01-02", units=60),
            ],
        }
        result = run(payload)
        inv = only_invoice(result)
        # 120 units - 10 free = 110 billable: 100 at 0.10 + 10 at 0.05.
        self.assertEqual(inv["net_cents"], 1000 + 50)
        first, second = inv["items"]
        self.assertEqual((first["free_units"], second["free_units"]), ("10", "0"))
        self.assertEqual(first["tiers"], [{"tier": 1, "units": "50", "unit_price": "0.1", "amount": "5.00"}])
        self.assertEqual(
            second["tiers"],
            [
                {"tier": 1, "units": "50", "unit_price": "0.1", "amount": "5.00"},
                {"tier": 2, "units": "10", "unit_price": "0.05", "amount": "0.50"},
            ],
        )

    def test_tier_allocation_is_stable_when_events_are_shuffled(self):
        events = [
            event(event_id=f"e{index}", at="2026-01-%02d" % (index + 1), units=37)
            for index in range(5)
        ]
        first = run({"contracts": [self.tiered()], "events": events})
        shuffled = run({"contracts": [self.tiered()], "events": list(reversed(events))})
        self.assertEqual(first["audit"]["output_digest"], shuffled["audit"]["output_digest"])
        self.assertEqual(
            [line["amount_cents"] for line in first["invoices"][0]["items"]],
            [line["amount_cents"] for line in shuffled["invoices"][0]["items"]],
        )

    def test_allowance_resets_each_period(self):
        payload = {
            "contracts": [self.tiered()],
            "events": [
                event(event_id="jan", at="2026-01-10", units=5),
                event(event_id="feb", at="2026-02-10", units=5),
            ],
        }
        result = run(payload)
        self.assertEqual(invoice(result, "A1", "2026-01")["net_cents"], 0)
        self.assertEqual(invoice(result, "A1", "2026-02")["net_cents"], 0)

    def test_usage_above_final_bound_is_reported_not_priced(self):
        payload = {
            "contracts": [
                contract(pricing={"api": {"tiers": [{"up_to": 5, "unit_price": "1.00"}]}})
            ],
            "events": [event(event_id="over", units=8)],
        }
        result = run(payload)
        self.assertEqual(only_invoice(result)["net_cents"], 500)
        self.assertEqual(result["audit"]["issue_counts"], {"usage_above_top_tier": 1})
        self.assertEqual(result["status"], "partial")

    def test_mixed_terms_in_one_pool_are_split_and_flagged(self):
        contracts = [
            contract(
                version=1,
                effective_from="2026-01-01",
                effective_to="2026-01-20",
                pricing={"api": {"unit_price": "1.00", "free": 5}},
            ),
            contract(
                version=2,
                effective_from="2026-01-20",
                pricing={"api": {"unit_price": "2.00", "free": 5}},
            ),
        ]
        payload = {
            "contracts": contracts,
            "billing": {"period": "month", "tier_scope": "account"},
            "events": [
                event(event_id="a", at="2026-01-10", units=5),
                event(event_id="b", at="2026-01-25", units=5),
            ],
        }
        result = run(payload)
        # Each version keeps its own allowance because the shared tier pool was
        # split; pooling 1.00 and 2.00 units would silently reprice one of them.
        self.assertEqual(only_invoice(result)["net_cents"], 0)
        self.assertEqual(result["audit"]["issue_counts"], {"mixed_tier_table": 1})
        self.assertEqual([line["units"] for line in only_invoice(result)["items"]], ["5", "5"])

    def test_rounding_happens_once_per_line_and_is_audited(self):
        payload = {
            "contracts": [contract(pricing={"api": {"unit_price": "0.333"}})],
            "events": [
                event(event_id="a", at="2026-01-01", units=1),
                event(event_id="b", at="2026-01-02", units=1),
            ],
        }
        result = run(payload)
        inv = only_invoice(result)
        # 0.333 -> 0.33 per line; the exact total 0.666 rounds to 0.67.
        self.assertEqual(inv["net_cents"], 66)
        self.assertEqual(
            result["audit"]["rounding"],
            [
                {
                    "account": "A1",
                    "period": "2026-01",
                    "line_total_cents": 66,
                    "exact_total_cents": 67,
                    "rounding_delta_cents": -1,
                }
            ],
        )


class TestUnits(unittest.TestCase):
    def test_fractional_storage_units(self):
        payload = {
            "contracts": [contract(pricing={"storage": {"unit_price": "0.10"}})],
            "events": [
                event(event_id="s", metric="storage", units=12.5),
            ],
        }
        result = run(payload)
        line = only_invoice(result)["items"][0]
        self.assertEqual(line["units"], "12.5")
        self.assertEqual(line["amount_cents"], 125)

    def test_invalid_units_are_isolated(self):
        payload = {
            "contracts": [contract()],
            "events": [
                event(event_id="bad", units="not-a-number"),
                event(event_id="neg", units=-4),
                event(event_id="zero", units=0),
                event(event_id="good", units=2),
            ],
        }
        result = run(payload)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["audit"]["issue_counts"], {"invalid_event": 3})
        self.assertEqual(only_invoice(result)["net_cents"], 200)

    def test_missing_metric_pricing_is_not_silently_free(self):
        payload = {
            "contracts": [contract(pricing={"api": {"unit_price": "1"}})],
            "events": [event(event_id="x", metric="gpu", units=1)],
        }
        result = run(payload)
        self.assertEqual(result["audit"]["issue_counts"], {"missing_metric_pricing": 1})
        self.assertEqual(result["invoices"], [])


if __name__ == "__main__":
    unittest.main()

import json
import unittest

from helpers import contract, event, invoice, item, only_invoice
from usage_contract_replay.pipeline import run
from usage_contract_replay.replay import verify


class TestIdempotency(unittest.TestCase):
    def payload(self, events):
        return {"contracts": [contract()], "events": events}

    def test_byte_identical_duplicate_is_billed_once(self):
        usage = event(event_id="dup", units=3)
        result = run(self.payload([usage, dict(usage), dict(usage)]))
        self.assertEqual(only_invoice(result)["net_cents"], 300)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            [entry["disposition"] for entry in result["audit"]["duplicates"]],
            ["duplicate_ignored", "duplicate_ignored"],
        )

    def test_idless_events_are_content_addressed_and_idempotent(self):
        anonymous = {
            "account": "A1",
            "at": "2026-01-10",
            "metric": "api",
            "units": 2,
        }
        once = run(self.payload([anonymous]))
        twice = run(self.payload([anonymous, dict(anonymous)]))
        self.assertEqual(
            once["audit"]["output_digest"], twice["audit"]["output_digest"]
        )
        self.assertTrue(once["audit"]["events"][0]["synthetic_id"])

    def test_same_identity_different_content_is_excluded_not_guessed(self):
        result = run(
            self.payload(
                [
                    event(event_id="x", units=5),
                    event(event_id="x", units=7),
                    event(event_id="ok", units=1),
                ]
            )
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["audit"]["issue_counts"], {"event_conflict": 1})
        # Neither 5 nor 7 is billed; only the unambiguous event is.
        inv = only_invoice(result)
        self.assertEqual(inv["net_cents"], 100)
        self.assertEqual(
            [entry["source_event_ids"] for entry in inv["items"]], [["ok"]]
        )
        conflict = [
            entry for entry in result["audit"]["events"] if entry["event_id"] == "x"
        ]
        self.assertEqual(conflict, [])

    def test_replay_of_same_batch_is_bitwise_identical(self):
        events = [
            event(event_id=f"e{i}", at="2026-01-%02d" % (i + 1), units=7)
            for i in range(6)
        ]
        events.insert(2, dict(events[0]))
        payload = self.payload(events)
        self.assertEqual(
            json.dumps(run(payload), sort_keys=True),
            json.dumps(run(payload), sort_keys=True),
        )
        self.assertTrue(verify(payload)["deterministic"])


class TestOrdering(unittest.TestCase):
    def test_late_arrival_does_not_change_allocation(self):
        # Both events compete for the same tier; the earlier event must take
        # tier 1 whether it arrived first or last.
        payload = {
            "contracts": [
                contract(
                    pricing={
                        "api": {
                            "tiers": [
                                {"up_to": 10, "unit_price": "1.00"},
                                {"up_to": None, "unit_price": "5.00"},
                            ]
                        }
                    }
                )
            ],
            "events": [
                event(
                    event_id="late-for-first",
                    at="2026-01-01",
                    received_at="2026-01-09",
                    units=10,
                ),
                event(event_id="second", at="2026-01-02", units=10),
            ],
        }
        first = run(payload)
        payload["events"].reverse()
        second = run(payload)
        self.assertEqual(first["audit"]["output_digest"], second["audit"]["output_digest"])
        by_source = {
            line["source_event_ids"][0]: line
            for line in first["invoices"][0]["items"]
        }
        self.assertEqual(by_source["late-for-first"]["amount_cents"], 1000)
        self.assertEqual(by_source["second"]["amount_cents"], 5000)
        self.assertEqual(
            first["invoices"][0]["items"][0]["source_event_ids"], ["late-for-first"]
        )

    def test_same_instant_order_is_defined_by_event_id_not_input_order(self):
        events = [
            event(event_id="b", at="2026-01-05T00:00:00Z", units=10),
            event(event_id="a", at="2026-01-05T00:00:00Z", units=10),
        ]
        payload = {
            "contracts": [
                contract(
                    pricing={
                        "api": {
                            "free": 5,
                            "tiers": [{"up_to": None, "unit_price": "1.00"}],
                        }
                    }
                )
            ],
            "events": events,
        }
        forward = run(payload)
        payload["events"] = list(reversed(events))
        backward = run(payload)
        self.assertEqual(forward["audit"]["output_digest"], backward["audit"]["output_digest"])
        ordered = forward["invoices"][0]["items"]
        self.assertEqual(ordered[0]["source_event_ids"], ["a"])
        self.assertEqual(ordered[0]["free_units"], "5")


class TestRetractionAndCorrection(unittest.TestCase):
    def test_retraction_removes_usage_everywhere(self):
        payload = {
            "contracts": [contract()],
            "events": [
                event(event_id="u", at="2026-01-05", units=4),
                event(
                    event_id="rm",
                    at="2026-01-10",
                    operation="remove",
                    target_event_id="u",
                ),
            ],
        }
        result = run(payload)
        self.assertEqual(result["invoices"], [])
        self.assertEqual(result["status"], "ok")
        statuses = {entry["event_id"]: entry["status"] for entry in result["audit"]["events"]}
        self.assertEqual(statuses, {"u": "retracted", "rm": "control"})

    def test_retraction_arriving_before_the_event_still_wins(self):
        payload = {
            "contracts": [contract()],
            "events": [
                event(
                    event_id="rm",
                    at="2026-01-10",
                    operation="remove",
                    target_event_id="u",
                ),
                event(event_id="u", at="2026-01-05", units=4),
            ],
        }
        self.assertEqual(run(payload)["invoices"], [])

    def test_later_restore_reactivates_usage(self):
        payload = {
            "contracts": [contract()],
            "events": [
                event(event_id="u", at="2026-01-05", units=4),
                event(
                    event_id="rm",
                    at="2026-01-10",
                    operation="remove",
                    target_event_id="u",
                ),
                event(
                    event_id="restore",
                    at="2026-01-11",
                    operation="restore",
                    target_event_id="u",
                ),
            ],
        }
        result = run(payload)
        self.assertEqual(only_invoice(result)["net_cents"], 400)

    def test_same_instant_tombstone_tie_breaks_on_event_id(self):
        payload = {
            "contracts": [contract()],
            "events": [
                event(event_id="u", at="2026-01-05", units=4),
                event(
                    event_id="z-remove",
                    at="2026-01-10T00:00:00Z",
                    operation="remove",
                    target_event_id="u",
                ),
                event(
                    event_id="a-restore",
                    at="2026-01-10T00:00:00Z",
                    operation="restore",
                    target_event_id="u",
                ),
            ],
        }
        # restore sorts before remove -> remove wins -> usage is retracted.
        result = run(payload)
        self.assertEqual(result["invoices"], [])

    def test_correction_replaces_units_and_keeps_source_ids(self):
        payload = {
            "contracts": [contract()],
            "events": [
                event(event_id="u", at="2026-01-05", units=10),
                event(
                    event_id="fix",
                    at="2026-01-06",
                    operation="set",
                    target_event_id="u",
                    units=3,
                ),
            ],
        }
        result = run(payload)
        line = item(only_invoice(result))
        self.assertEqual(line["units"], "3")
        self.assertEqual(line["amount_cents"], 300)
        self.assertEqual(line["source_event_ids"], ["u", "fix"])
        self.assertIn("corrected", line["adjustment_reasons"])

    def test_correction_of_retracted_usage_is_reported(self):
        payload = {
            "contracts": [contract()],
            "events": [
                event(event_id="u", at="2026-01-05", units=10),
                event(event_id="rm", at="2026-01-06", operation="remove", target_event_id="u"),
                event(
                    event_id="fix",
                    at="2026-01-07",
                    operation="set",
                    target_event_id="u",
                    units=3,
                ),
            ],
        }
        result = run(payload)
        self.assertIn("correction_of_retracted_event", result["audit"]["issue_counts"])
        self.assertEqual(result["invoices"], [])

    def test_marker_retracted_event_never_bills(self):
        payload = {
            "contracts": [contract()],
            "events": [event(event_id="u", units=4, retracted=True)],
        }
        result = run(payload)
        self.assertEqual(result["invoices"], [])
        self.assertEqual(result["audit"]["events"][0]["status"], "retracted")

    def test_retraction_without_target_is_invalid(self):
        payload = {
            "contracts": [contract()],
            "events": [event(event_id="rm", operation="remove")],
        }
        result = run(payload)
        self.assertEqual(result["audit"]["issue_counts"], {"invalid_event": 1})


class TestRefunds(unittest.TestCase):
    def test_refund_is_attributed_to_refund_period_and_capped(self):
        payload = {
            "contracts": [contract()],
            "events": [
                event(event_id="u", at="2026-01-05", units=10),
                event(
                    event_id="r",
                    at="2026-02-03",
                    operation="refund",
                    target_event_id="u",
                    refund_amount="2.50",
                ),
            ],
        }
        result = run(payload)
        jan = invoice(result, "A1", "2026-01")
        feb = invoice(result, "A1", "2026-02")
        self.assertEqual(jan["net_cents"], 1000)
        self.assertEqual(feb["net_cents"], -250)
        line = item(feb, kind="refund")
        self.assertEqual(line["target_event_id"], "u")
        self.assertEqual(line["target_period"], "2026-01")
        self.assertIn("refund_cross_period", line["adjustment_reasons"])

    def test_ratio_refund_rounds_once_and_reconciles(self):
        payload = {
            "contracts": [contract(pricing={"api": {"unit_price": "0.333"}})],
            "events": [
                event(event_id="u", at="2026-01-05", units=3),  # 0.999 -> 1.00
                event(
                    event_id="r",
                    at="2026-01-06",
                    operation="refund",
                    target_event_id="u",
                    refund_ratio="0.5",
                ),
            ],
        }
        result = run(payload)
        inv = only_invoice(result)
        self.assertEqual(item(inv)["amount_cents"], 100)
        self.assertEqual(item(inv, kind="refund")["amount_cents"], -50)
        self.assertEqual(inv["net_cents"], 50)
        self.assertTrue(all(result["audit"]["invariants"].values()))

    def test_over_refund_is_capped_and_explained(self):
        payload = {
            "contracts": [contract()],
            "events": [
                event(event_id="u", at="2026-01-05", units=1),
                event(
                    event_id="r1",
                    at="2026-01-06",
                    operation="refund",
                    target_event_id="u",
                    refund_amount="5",
                ),
                event(
                    event_id="r2",
                    at="2026-01-07",
                    operation="refund",
                    target_event_id="u",
                    refund_amount="1",
                ),
            ],
        }
        result = run(payload)
        inv = only_invoice(result)
        self.assertEqual(inv["net_cents"], 0)
        refunds = [line for line in inv["items"] if line["kind"].startswith("refund")]
        self.assertEqual([line["amount_cents"] for line in refunds], [-100, 0])
        self.assertIn("refund_capped_at_remaining", refunds[0]["adjustment_reasons"])
        self.assertIn("no_refundable_balance", refunds[1]["adjustment_reasons"])
        self.assertEqual(result["status"], "ok")

    def test_refund_of_retracted_usage_is_rejected(self):
        payload = {
            "contracts": [contract()],
            "events": [
                event(event_id="u", at="2026-01-05", units=1),
                event(event_id="rm", at="2026-01-06", operation="remove", target_event_id="u"),
                event(
                    event_id="r",
                    at="2026-01-07",
                    operation="refund",
                    target_event_id="u",
                    refund_amount="1",
                ),
            ],
        }
        result = run(payload)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["audit"]["issue_counts"], {"refund_target_retracted": 1})
        self.assertEqual(result["invoices"], [])

    def test_refund_without_target_cannot_exceed_period_gross(self):
        payload = {
            "contracts": [contract()],
            "events": [
                event(event_id="u", at="2026-01-05", units=1),
                event(
                    event_id="r",
                    at="2026-01-06",
                    operation="refund",
                    refund_amount="99",
                ),
            ],
        }
        result = run(payload)
        inv = only_invoice(result)
        self.assertEqual(inv["net_cents"], 0)
        self.assertEqual(
            item(inv, kind="refund")["adjustment_reasons"],
            ["refund_capped_at_remaining"],
        )


if __name__ == "__main__":
    unittest.main()

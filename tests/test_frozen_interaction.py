import unittest

from helpers import contract, event, invoice, only_invoice
from usage_contract_replay.pipeline import run
from usage_contract_replay.snapshots import freeze_document


class TestFrozenInteraction(unittest.TestCase):
    def january_run(self):
        payload = {"contracts": [contract()], "events": [event(event_id="u", at="2026-01-05", units=10)]}
        return run(payload), freeze_document(run(payload))

    def test_refund_only_replay_uses_frozen_history_for_its_target(self):
        _, frozen = self.january_run()
        payload = {
            "contracts": [contract()],
            "events": [
                event(
                    event_id="r",
                    at="2026-02-10",
                    operation="refund",
                    target_event_id="u",
                    refund_amount="4",
                )
            ],
            "frozen_invoices": frozen,
        }
        result = run(payload)
        self.assertEqual(result["status"], "ok")
        feb = only_invoice(result)
        self.assertEqual(feb["period"], "2026-02")
        self.assertEqual(feb["net_cents"], -400)
        line = feb["items"][0]
        self.assertEqual(
            line["adjustment_reasons"],
            ["target_from_frozen_history", "refund_cross_period"],
        )
        frozen_jan = result["frozen_invoices"][0]
        self.assertEqual(frozen_jan["status"], "frozen_not_recomputed")
        self.assertEqual(frozen_jan["net_cents"], 1000)
        self.assertTrue(result["audit"]["invariants"]["refunds_within_billed_usage"])

    def test_over_refund_of_frozen_usage_is_capped(self):
        _, frozen = self.january_run()
        payload = {
            "contracts": [contract()],
            "events": [
                event(
                    event_id="r",
                    at="2026-02-10",
                    operation="refund",
                    target_event_id="u",
                    refund_amount="99",
                )
            ],
            "frozen_invoices": frozen,
        }
        result = run(payload)
        self.assertEqual(only_invoice(result)["net_cents"], -1000)
        self.assertIn(
            "refund_capped_at_remaining",
            only_invoice(result)["items"][0]["adjustment_reasons"],
        )
        self.assertTrue(result["audit"]["invariants"]["refunds_within_billed_usage"])

    def test_refund_of_unknown_target_still_fails(self):
        payload = {
            "contracts": [contract()],
            "events": [
                event(
                    event_id="r",
                    at="2026-02-10",
                    operation="refund",
                    target_event_id="nope",
                    refund_amount="1",
                )
            ],
        }
        result = run(payload)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["audit"]["issue_counts"], {"refund_target_not_billable": 1})

    def test_cross_period_refund_leaves_frozen_period_untouched(self):
        jan = run(
            {
                "contracts": [contract()],
                "events": [event(event_id="u", at="2026-01-05", units=10)],
            }
        )
        frozen = freeze_document(jan, periods=["2026-01"])
        payload = {
            "contracts": [contract()],
            "events": [
                event(event_id="u", at="2026-01-05", units=10),
                event(
                    event_id="r",
                    at="2026-02-10",
                    operation="refund",
                    target_event_id="u",
                    refund_amount="4",
                ),
            ],
            "frozen_invoices": frozen,
        }
        result = run(payload)
        self.assertEqual(result["revisions"], [])
        self.assertEqual(result["frozen_invoices"][0]["net_cents"], 1000)
        self.assertEqual(invoice(result, "A1", "2026-02")["net_cents"], -400)

    def test_contract_refund_placement_does_not_depend_on_freezing(self):
        payload = {
            "contracts": [contract(refund="2.00")],
            "events": [
                event(event_id="e1", at="2026-01-05", units=10),
                event(event_id="e2", at="2026-02-05", units=10),
            ],
        }
        first = run(payload)
        self.assertEqual(
            [(inv["period"], inv["net_cents"]) for inv in first["invoices"]],
            [("2026-01", 800), ("2026-02", 1000)],
        )
        payload["frozen_invoices"] = freeze_document(first, periods=["2026-01"])
        second = run(payload)
        self.assertEqual(second["revisions"], [])
        self.assertEqual(second["frozen_invoices"][0]["net_cents"], 800)

    def test_retroactive_contract_edit_only_affects_revision(self):
        original = {
            "contracts": [contract()],
            "events": [event(event_id="u", at="2026-01-05", units=10)],
        }
        frozen = freeze_document(run(original))
        edited = {
            "contracts": [contract(pricing={"api": {"unit_price": "2.00"}})],
            "events": original["events"],
            "frozen_invoices": frozen,
        }
        result = run(edited)
        self.assertEqual(result["frozen_invoices"][0]["net_cents"], 1000)
        self.assertEqual(result["frozen_invoices"][0]["recomputed_net_cents"], 2000)
        self.assertEqual(result["revisions"][0]["delta_cents"], 1000)
        self.assertEqual(
            result["revisions"][0]["items"][0]["contract_version"], "*:C1:v1"
        )
        self.assertTrue(all(result["audit"]["invariants"].values()))

    def test_replaying_a_frozen_batch_is_stable(self):
        import json

        original = {
            "contracts": [contract()],
            "events": [event(event_id="u", at="2026-01-05", units=10)],
        }
        frozen = freeze_document(run(original))
        payload = {**original, "frozen_invoices": frozen}
        first = json.dumps(run(payload), sort_keys=True)
        second = json.dumps(run(payload), sort_keys=True)
        self.assertEqual(first, second)
        self.assertEqual(run(payload)["revisions"], [])


if __name__ == "__main__":
    unittest.main()

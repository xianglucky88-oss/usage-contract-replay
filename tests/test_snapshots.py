import copy
import unittest

from helpers import contract, event, invoice
from usage_contract_replay.pipeline import run
from usage_contract_replay.snapshots import freeze_document
from usage_contract_replay.util import InputError


def base_payload():
    return {
        "contracts": [
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
        ],
        "events": [
            event(event_id="jan", at="2026-01-10", units=10),
            event(event_id="feb", at="2026-02-10", units=10),
        ],
    }


class TestFreezing(unittest.TestCase):
    def test_frozen_period_is_not_rewritten(self):
        first = run(base_payload())
        receipts = freeze_document(first, periods=["2026-01"])
        payload = base_payload()
        payload["frozen_invoices"] = receipts
        again = run(payload)

        self.assertEqual(again["revisions"], [])
        self.assertEqual([inv["period"] for inv in again["invoices"]], ["2026-02"])
        frozen = again["frozen_invoices"]
        self.assertEqual(len(frozen), 1)
        self.assertEqual(frozen[0]["period"], "2026-01")
        self.assertEqual(frozen[0]["status"], "frozen")
        self.assertEqual(frozen[0]["net_cents"], 1000)
        self.assertEqual(frozen[0]["frozen_digest"], frozen[0]["recomputed_digest"])
        self.assertEqual(again["status"], "ok")

    def test_new_evidence_creates_revision_not_a_rewritten_invoice(self):
        first = run(base_payload())
        jan_digest = invoice(first, "A1", "2026-01")["digest"]
        payload = base_payload()
        payload["events"].append(event(event_id="jan-late", at="2026-01-20", units=5))
        payload["frozen_invoices"] = freeze_document(first, periods=["2026-01"])

        result = run(payload)
        frozen_jan = [
            inv for inv in result["frozen_invoices"] if inv["period"] == "2026-01"
        ][0]
        self.assertEqual(frozen_jan["frozen_digest"], jan_digest)
        self.assertEqual(frozen_jan["net_cents"], 1000)  # unchanged promise
        revision = result["revisions"][0]
        self.assertEqual(revision["period"], "2026-01")
        self.assertEqual(revision["frozen_net_cents"], 1000)
        self.assertEqual(revision["recomputed_net_cents"], 1500)
        self.assertEqual(revision["delta_cents"], 500)
        self.assertEqual(revision["delta"], "5.00")
        self.assertEqual(revision["status"], "pending")
        self.assertTrue(all(result["audit"]["invariants"].values()))

    def test_contract_revision_does_not_rewrite_frozen_but_flags_period(self):
        first = run(base_payload())
        payload = base_payload()
        # Retroactive terms change for January only.
        payload["contracts"][0] = contract(
            version=1,
            effective_from="2026-01-01T00:00:00Z",
            effective_to="2026-02-01T00:00:00Z",
            pricing={"api": {"unit_price": "1.50"}},
        )
        payload["frozen_invoices"] = freeze_document(first, periods=["2026-01"])
        result = run(payload)
        frozen_jan = [
            inv for inv in result["frozen_invoices"] if inv["period"] == "2026-01"
        ][0]
        self.assertEqual(frozen_jan["net_cents"], 1000)
        self.assertEqual(result["revisions"][0]["delta_cents"], 500)
        self.assertEqual(
            result["revisions"][0]["reason"], "contract_or_event_change_after_freeze"
        )

    def test_revision_line_items_show_source_events_and_versions(self):
        first = run(base_payload())
        payload = base_payload()
        payload["events"].append(event(event_id="jan-late", at="2026-01-20", units=5))
        payload["frozen_invoices"] = freeze_document(first, periods=["2026-01"])
        revision = run(payload)["revisions"][0]
        sources = {
            source for line in revision["items"] for source in line["source_event_ids"]
        }
        self.assertEqual(sources, {"jan", "jan-late"})
        self.assertEqual(
            {line["contract_version"] for line in revision["items"]}, {"*:C1:v1"}
        )

    def test_minimal_receipt_without_items_still_freezes(self):
        first = run(base_payload())
        jan = invoice(first, "A1", "2026-01")
        payload = base_payload()
        payload["frozen_invoices"] = [
            {"account": "A1", "period": "2026-01", "net": jan["net"]}
        ]
        result = run(payload)
        self.assertEqual(result["revisions"], [])
        self.assertEqual(result["frozen_invoices"][0]["net_cents"], 1000)

    def test_history_shape_is_accepted(self):
        first = run(base_payload())
        payload = base_payload()
        payload["history"] = {"A1": [copy.deepcopy(invoice(first, "A1", "2026-01"))]}
        result = run(payload)
        self.assertEqual(result["revisions"], [])

    def test_conflicting_frozen_receipts_are_rejected(self):
        first = run(base_payload())
        payload = base_payload()
        payload["frozen_invoices"] = [
            {"account": "A1", "period": "2026-01", "net_cents": 1000},
            {"account": "A1", "period": "2026-01", "net_cents": 999},
        ]
        result = run(payload)
        self.assertEqual(result["audit"]["issue_counts"], {"frozen_conflict": 1})
        # The ambiguous period is recomputed instead of silently picking one.
        self.assertEqual(invoice(result, "A1", "2026-01")["net_cents"], 1000)

    def test_frozen_currency_mismatch_is_rejected(self):
        payload = base_payload()
        payload["frozen_invoices"] = [
            {"account": "A1", "period": "2026-01", "net_cents": 1, "currency": "EUR"}
        ]
        result = run(payload)
        self.assertEqual(
            result["audit"]["issue_counts"], {"frozen_currency_mismatch": 1}
        )

    def test_empty_period_snapshot_is_recomputed_as_empty_and_matches(self):
        payload = {
            "contracts": [contract()],
            "billing": {"period": "month", "periods": ["2026-03"]},
            "accounts": ["A1"],
            "events": [],
        }
        first = run(payload)
        self.assertEqual(first["invoices"][0]["status"], "empty")
        payload["frozen_invoices"] = freeze_document(first, periods=["2026-03"])
        again = run(payload)
        self.assertEqual(again["revisions"], [])
        self.assertEqual(again["frozen_invoices"][0]["status"], "frozen")
        self.assertEqual(again["frozen_invoices"][0]["net_cents"], 0)


class TestVersionLocking(unittest.TestCase):
    def test_contract_lock_lists_every_version_with_fingerprint(self):
        result = run(base_payload())
        lock = result["audit"]["contract_lock"]
        self.assertEqual([entry["key"] for entry in lock], ["*:C1:v1", "*:C1:v2"])
        self.assertTrue(all(entry["fingerprint"] for entry in lock))
        self.assertEqual(
            result["summary"]["contract_lock_digest"],
            result["audit"]["contract_lock_digest"],
        )

    def test_lock_digest_is_input_order_independent(self):
        first = run(base_payload())
        payload = base_payload()
        payload["contracts"].reverse()
        second = run(payload)
        self.assertEqual(
            first["audit"]["contract_lock_digest"],
            second["audit"]["contract_lock_digest"],
        )

    def test_expected_lock_mismatch_aborts_the_run(self):
        payload = base_payload()
        payload["expected_contract_lock_digest"] = "0" * 64
        with self.assertRaises(InputError):
            run(payload)

    def test_expected_lock_match_allows_the_run(self):
        first = run(base_payload())
        payload = base_payload()
        payload["expected_contract_lock_digest"] = first["audit"][
            "contract_lock_digest"
        ]
        self.assertEqual(run(payload)["status"], "ok")

    def test_rules_version_is_pinned(self):
        payload = base_payload()
        payload["rules_version"] = "1999-01-01.0"
        with self.assertRaises(InputError):
            run(payload)

    def test_rules_version_is_reported_and_impacts_digest(self):
        first = run(base_payload())
        self.assertEqual(first["rules_version"], "2026-11-17.1")
        self.assertEqual(first["audit"]["rules_version"], "2026-11-17.1")
        # The digest covers rules_version: a contract-free change of rules must
        # not be able to claim the same output identity.
        tampered = run(base_payload())
        tampered_invoice = tampered["invoices"][0]
        tampered_invoice["rules_version"] = "2026-11-17.2"
        from usage_contract_replay.digests import output_digest

        self.assertNotEqual(
            output_digest(tampered), first["audit"]["output_digest"]
        )


if __name__ == "__main__":
    unittest.main()

"""End-to-end pipeline behaviour across parser, engine, pipeline and report.

Each test names one boundary from the brief and asserts the observable
contract through the public ``run`` entry point.
"""

import json
import unittest

from qa import generators
from usage_contract_replay.pipeline import run

from usage_contract_replay.engine import calculate
from usage_contract_replay.parser import parse


class TestEventOrdering(unittest.TestCase):
    def test_input_order_does_not_change_invoice(self):
        events = [
            generators.make_event(at="2026-01-03", units=4),
            generators.make_event(at="2026-01-01", units=6),
            generators.make_event(at="2026-01-02", units=2),
        ]
        payload = generators.make_payload(events, free=5, unit_price="2.5")
        base = run(payload)
        for permutation in (
            ["2026-01-01", "2026-01-02", "2026-01-03"],
            ["2026-01-02", "2026-01-01", "2026-01-03"],
            ["2026-01-03", "2026-01-02", "2026-01-01"],
        ):
            reordered = dict(payload)
            reordered["events"] = [e for at in permutation for e in events if e["at"] == at]
            self.assertEqual(json.dumps(run(reordered), sort_keys=True), json.dumps(base, sort_keys=True))

    def test_free_quota_follows_canonical_order(self):
        # Two 6-unit events, 10 free: the earlier timestamp gets 6 free, the
        # later gets 4 free / 2 billable.  Reversing input must not swap them.
        early = generators.make_event(at="2026-01-01", units=6)
        late = generators.make_event(at="2026-01-02", units=6)
        for events in ([early, late], [late, early]):
            result = run(generators.make_payload(events, free=10, unit_price="1"))
            by_at = {item["at"]: item for item in result["events"]}
            self.assertEqual(by_at["2026-01-01"]["free_units"], 6)
            self.assertEqual(by_at["2026-01-02"]["free_units"], 4)
            self.assertEqual(by_at["2026-01-02"]["billable_units"], 2)


class TestDuplicateReplay(unittest.TestCase):
    def test_same_event_id_is_counted_once(self):
        payload = generators.make_payload(
            [
                generators.make_event(units=5, event_id="evt-1"),
                generators.make_event(units=5, event_id="evt-1"),
                generators.make_event(units=5, event_id="evt-1"),
            ],
            unit_price="2",
        )
        result = run(payload)
        self.assertEqual(result["total_units"], 5)
        self.assertEqual(result["amount_exact"], "10.00")
        self.assertEqual(result["received_event_count"], 3)
        self.assertEqual(result["duplicate_event_count"], 2)
        self.assertEqual(result["event_count"], 1)

    def test_content_identical_events_are_deduped(self):
        event = generators.make_event(units=5)
        result = run(generators.make_payload([event, dict(event)], unit_price="2"))
        self.assertEqual(result["total_units"], 5)
        self.assertEqual(result["duplicate_event_count"], 1)

    def test_same_id_conflicting_payload_is_rejected(self):
        payload = generators.make_payload(
            [
                generators.make_event(units=5, event_id="dup"),
                generators.make_event(at="2026-01-09", units=5, event_id="dup"),
            ],
            unit_price="2",
        )
        result = run(payload)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "duplicate_event_conflict")

    def test_replaying_a_full_payload_is_a_noop(self):
        payload = generators.make_payload(
            [generators.make_event(units=12), generators.make_event(at="2026-01-16", units=8)],
            free=10,
            unit_price="2.5",
        )
        base = run(payload)
        doubled = dict(payload)
        doubled["events"] = payload["events"] + payload["events"]
        again = run(doubled)
        self.assertEqual(again["amount_exact"], base["amount_exact"])
        self.assertEqual(again["total_units"], base["total_units"])


class TestUnitsBoundaries(unittest.TestCase):
    def test_zero_units_are_valid_and_visible(self):
        result = run(generators.make_payload([generators.make_event(units=0)], unit_price="3"))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["event_count"], 1)

    def test_negative_units_are_rejected(self):
        result = run(generators.make_payload([generators.make_event(units=-1)], unit_price="3"))
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "negative_units")

    def test_float_units_are_rejected_not_truncated(self):
        result = run(generators.make_payload([generators.make_event(units=1.9)], unit_price="3"))
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "units_not_int")

    def test_bool_units_are_rejected(self):
        result = run(generators.make_payload([generators.make_event(units=True)], unit_price="3"))
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "units_not_int")


class TestFreeQuota(unittest.TestCase):
    def test_exactly_at_free_boundary(self):
        result = run(generators.make_payload([generators.make_event(units=10)], free=10, unit_price="2.5"))
        self.assertEqual(result["billable_units"], 0)
        self.assertEqual(result["amount_exact"], "0.00")

    def test_one_over_free_boundary(self):
        result = run(generators.make_payload([generators.make_event(units=11)], free=10, unit_price="2.5"))
        self.assertEqual(result["billable_units"], 1)
        self.assertEqual(result["amount_exact"], "2.50")

    def test_free_larger_than_usage(self):
        result = run(generators.make_payload([generators.make_event(units=3)], free=100, unit_price="2.5"))
        self.assertEqual(result["free_units"], 3)
        self.assertEqual(result["billable_units"], 0)
        self.assertEqual(result["amount_exact"], "0.00")

    def test_zero_free(self):
        result = run(generators.make_payload([generators.make_event(units=3)], free=0, unit_price="2.5"))
        self.assertEqual(result["free_units"], 0)
        self.assertEqual(result["billable_units"], 3)


class TestRefundBoundary(unittest.TestCase):
    def test_refund_equal_to_gross(self):
        result = run(
            generators.make_payload(
                [generators.make_event(units=4)], unit_price="2.5", refund="10", refund_reason="full"
            )
        )
        self.assertEqual(result["amount_exact"], "0.00")
        self.assertFalse(result["refund_capped"])
        self.assertEqual(result["refund_reason"], "full")

    def test_refund_above_gross_is_capped(self):
        result = run(
            generators.make_payload(
                [generators.make_event(units=1)], unit_price="2.5", refund="99", refund_reason="over"
            )
        )
        self.assertEqual(result["refund_applied_exact"], "2.50")
        self.assertEqual(result["refund_requested_exact"], "99.00")
        self.assertTrue(result["refund_capped"])
        self.assertEqual(result["amount_exact"], "0.00")

    def test_refund_without_reason_is_rejected(self):
        result = run(generators.make_payload([generators.make_event(units=1)], unit_price="2.5", refund="1"))
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "refund_reason_required")

    def test_refund_reason_is_traceable(self):
        result = run(
            generators.make_payload(
                [generators.make_event(units=1)],
                unit_price="2.5",
                refund="1",
                refund_reason="service credit",
            )
        )
        self.assertEqual(result["refund_reason"], "service credit")
        self.assertEqual(result["contract"]["refund_reason"], "service credit")


class TestMoneyPrecision(unittest.TestCase):
    def test_repeated_decimal_addition_is_exact(self):
        # 0.1 * 3 is the classic float trap.
        result = run(generators.make_payload([generators.make_event(units=3)], unit_price="0.1"))
        self.assertEqual(result["amount_exact"], "0.30")

    def test_many_lines_round_once_half_up(self):
        payload = generators.make_payload(
            [generators.make_event(at=f"2026-02-{i:02d}", units=1) for i in range(1, 8)],
            unit_price="0.335",
        )
        result = run(payload)
        # exact gross 7 * 0.335 = 2.345 -> half-up 2.35
        self.assertEqual(result["gross_amount_exact"], "2.35")

    def test_integer_price_has_no_float_artifacts(self):
        result = run(generators.make_payload([generators.make_event(units=3)], unit_price="1"))
        self.assertEqual(result["amount_exact"], "3.00")
        self.assertEqual(json.dumps(result["amount"]), "3.0")


class TestAccountGrouping(unittest.TestCase):
    def test_account_is_normalised(self):
        result = run(generators.make_payload([generators.make_event(account="  A1  ", units=1)], unit_price="1"))
        self.assertEqual(result["account"], "A1")

    def test_mixed_accounts_are_rejected(self):
        payload = generators.make_payload(
            [generators.make_event(account="A"), generators.make_event(account="B")], unit_price="1"
        )
        result = run(payload)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "mixed_accounts")

    def test_empty_stream_has_no_account(self):
        result = run(generators.make_payload([], unit_price="1"))
        self.assertIsNone(result["account"])


class TestContractTraceability(unittest.TestCase):
    def test_contract_echo_is_lossless(self):
        payload = generators.make_payload(
            [generators.make_event(units=1)],
            free=2,
            unit_price="2.5",
            refund="1",
            refund_reason="credit",
            version="v9",
        )
        payload["contract"]["currency"] = "EUR"
        result = run(payload)
        echo = result["contract"]
        self.assertEqual(echo["free"], 2)
        self.assertEqual(echo["unit_price"], "2.5")
        self.assertEqual(echo["refund"], "1")
        self.assertEqual(echo["refund_reason"], "credit")
        self.assertEqual(echo["version"], "v9")
        self.assertEqual(echo["currency"], "EUR")
        self.assertEqual(result["contract_version"], "v9")


class TestReportStability(unittest.TestCase):
    def test_report_is_byte_stable(self):
        payload = generators.make_payload(
            [generators.make_event(at="2026-01-02", units=2), generators.make_event(at="2026-01-01", units=3)],
            free=1,
            unit_price="2.5",
        )
        first = json.dumps(run(payload), sort_keys=True)
        second = json.dumps(run(payload), sort_keys=True)
        self.assertEqual(first, second)

    def test_report_events_are_sorted(self):
        result = run(
            generators.make_payload(
                [generators.make_event(at="2026-03-01", units=1), generators.make_event(at="2026-01-01", units=1)],
                unit_price="1",
            )
        )
        ats = [item["at"] for item in result["events"]]
        self.assertEqual(ats, sorted(ats))


class TestValidationEnvelopes(unittest.TestCase):
    def test_invalid_input_returns_error_envelope_not_exception(self):
        result = run({"events": "nope"})
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["project"], "usage-contract-replay")
        self.assertEqual(result["report_version"], 1)
        self.assertIn("code", result["error"])

    def test_error_envelopes_are_deterministic(self):
        payload = generators.make_payload([generators.make_event(units=-2)], unit_price="1")
        self.assertEqual(json.dumps(run(payload), sort_keys=True), json.dumps(run(payload), sort_keys=True))


class TestDirectModuleUse(unittest.TestCase):
    def test_engine_accepts_parsed_input(self):
        payload = generators.make_payload([generators.make_event(units=4)], free=1, unit_price="2")
        parsed = parse(payload)
        data = calculate(parsed)
        self.assertEqual(data["total_units"], 4)
        self.assertEqual(data["billable_units"], 3)
        self.assertEqual(data["amount_exact"], "6.00")


if __name__ == "__main__":
    unittest.main()
